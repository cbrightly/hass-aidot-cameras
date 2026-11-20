"""The picture of each camera's most recent detection event.

The cloud event list carries a ``picUrl`` per event: a signed CDN link that
expires. It is never handed to the browser (see ``_prefetch_thumbnail`` in
camera.py for the broken icon that gives). The bytes are fetched here, once,
while the link is fresh, and served from memory through Home Assistant's own
image proxy for as long as the event stays the newest one.

Nothing here contacts a camera: the event comes from the cloud event poll the
coordinator already runs, and the picture from the cloud's CDN.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import async_add_per_camera, device_info_for

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# One request's budget, and the most a still may weigh (they are tens of kB).
_FETCH_TIMEOUT_S = 10
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Seconds to wait before each attempt at a NEW event's picture. The link is
# fresh then, so a failure is the network or a still not uploaded yet, and a
# couple of spaced retries are worth it. After the last one the event is
# given up on and the previous picture stays.
_EVENT_FETCH_DELAYS_S: tuple[float, ...] = (0, 5, 15)
# How far back the one listing at startup looks for the picture to show until
# the next event (the same window the camera entity's thumbnail uses).
_SEED_LOOKBACK_MS = 30 * 86_400_000
# How many of the newest events that listing asks for. Not every event has a
# picture, so asking for one alone leaves the entity blank whenever the
# newest happens to have none.
_SEED_PAGE_SIZE = 10
# The picture is read in pieces of this size, so one that is too large is
# given up on at the cap instead of after it has all been read.
_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_CONTENT_TYPE = "image/jpeg"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one last-event image per camera."""
    async_add_per_camera(
        entry, async_add_entities, lambda c: [AidotLastEventImage(hass, c)]
    )


def _event_ms(event: dict[str, Any]) -> float | None:
    """The event's cloud timestamp in epoch ms, or None when it has none."""
    raw = event.get("eventTime")
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    """When the event happened, or None when the cloud item carries no usable
    time."""
    stamp = _event_ms(event)
    if stamp is None:
        return None
    try:
        return dt_util.utc_from_timestamp(stamp / 1000.0)
    except (ValueError, OverflowError, OSError):
        return None


class AidotLastEventImage(ImageEntity):
    """The still the cloud stored for the camera's newest detection event."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_event"
    _attr_content_type = _DEFAULT_CONTENT_TYPE

    # Not an AidotEntity, for the reason the motion event is not: that base
    # follows the camera being online, and the last picture is worth showing
    # while the camera sleeps or is offline.
    def __init__(
        self, hass: HomeAssistant, coordinator: AidotCameraUpdateCoordinator
    ) -> None:
        super().__init__(hass)
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.device_client.info.dev_id}_last_event"
        self._attr_device_info = device_info_for(coordinator)
        self._image: bytes | None = None
        self._event_uuid: str | None = None
        # The cloud's time of the newest event shown that had one: what a
        # later picture is ordered against. Never a time made up here.
        self._newest_event_at: datetime | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def async_added_to_hass(self) -> None:
        """Follow the coordinator's event stream, and show the newest stored event."""
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.add_motion_listener(self._on_event))
        self.async_on_remove(self._cancel_tasks)
        # The image platform restores nothing, so without this the entity
        # would be blank after every restart until the next detection.
        self._spawn(self._async_seed(), "seed")

    async def async_image(self) -> bytes | None:
        """The cached picture. Never fetches: a dashboard reload costs nothing."""
        return self._image

    @callback
    def _on_event(self, event: dict[str, Any]) -> None:
        if not event.get("picUrl"):
            # Nothing to show for this event; the previous picture stays.
            return
        self._spawn(self._async_show(event, _EVENT_FETCH_DELAYS_S), "event")

    def _spawn(self, coro: Any, what: str) -> None:
        # Background: a slow CDN must not hold up startup or the event poll.
        task = self.hass.async_create_background_task(
            coro, name=f"aidot-last-event-image-{what}-{self._attr_unique_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @callback
    def _cancel_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def _async_seed(self) -> None:
        """Show the newest event the cloud already holds, from one listing.

        A cloud request only; it wakes no camera. The link in a listing is
        freshly issued, so it is fetched once, now, like an arriving event's.
        """
        dc = self._coordinator.device_client
        now_ms = int(time.time() * 1000)
        try:
            items = await dc.async_get_cloud_recordings(
                now_ms - _SEED_LOOKBACK_MS, now_ms, page=1, page_size=_SEED_PAGE_SIZE
            )
        except Exception as exc:
            _LOGGER.debug("%s: event listing failed: %s", self._attr_unique_id, exc)
            return
        candidates = [
            it for it in items or [] if isinstance(it, dict) and it.get("picUrl")
        ]
        if not candidates:
            return
        newest = max(candidates, key=lambda it: _event_ms(it) or 0)
        await self._async_show(newest, (0,))

    async def _async_show(
        self, event: dict[str, Any], delays: tuple[float, ...]
    ) -> None:
        """Fetch ``event``'s picture and, if it is still the newest, show it."""
        uuid = event.get("eventUuid")
        uuid = str(uuid) if uuid else None
        if uuid is not None and uuid == self._event_uuid:
            return  # already showing this event
        when = _event_datetime(event)
        fetched: tuple[bytes, str] | None = None
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            if self._is_older(when):
                return  # a newer event's picture landed meanwhile
            fetched = await self._async_fetch(str(event["picUrl"]))
            if fetched is not None:
                break
        if fetched is None or self._is_older(when):
            # Keep serving the previous picture, under its own event time.
            return
        self._image, self._attr_content_type = fetched
        self._event_uuid = uuid
        if when is not None:
            self._newest_event_at = when
        # An event with no time of its own is shown as of now, which is when
        # it arrived. That is for display only: were it to become the mark,
        # a clock running ahead of the cloud's, or an event that reached us
        # late, would have every real event after it turned away as older.
        self._attr_image_last_updated = when or dt_util.utcnow()
        self.async_write_ha_state()

    def _is_older(self, when: datetime | None) -> bool:
        """Whether a picture of an event at ``when`` is older than the one
        shown. An event with no time cannot be ordered and is taken as new."""
        newest = self._newest_event_at
        return when is not None and newest is not None and when < newest

    async def _async_fetch(self, url: str) -> tuple[bytes, str] | None:
        """``(bytes, content type)`` of the picture, or None when it cannot be had.

        The URL is signed, so it is kept out of the log.
        """
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S)
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "%s: event picture answered HTTP %s",
                        self._attr_unique_id,
                        resp.status,
                    )
                    return None
                data = await self._async_read_capped(resp)
                content_type = resp.content_type or ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.debug(
                "%s: event picture fetch failed: %s",
                self._attr_unique_id,
                type(exc).__name__,
            )
            return None
        if not data:
            _LOGGER.debug(
                "%s: event picture rejected (empty, or over %d bytes)",
                self._attr_unique_id,
                _MAX_IMAGE_BYTES,
            )
            return None
        if not content_type.startswith("image/"):
            # Object stores often answer application/octet-stream for a jpeg.
            content_type = _DEFAULT_CONTENT_TYPE
        return data, content_type

    @staticmethod
    async def _async_read_capped(resp: aiohttp.ClientResponse) -> bytes | None:
        """The body, or None when it is larger than a still may be.

        Read piece by piece and dropped at the cap: ``resp.read()`` would
        hold whatever the far end chose to send before the size was looked
        at.
        """
        declared = resp.content_length
        if declared is not None and declared > _MAX_IMAGE_BYTES:
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.content.iter_chunked(_READ_CHUNK_BYTES):
            size += len(chunk)
            if size > _MAX_IMAGE_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
