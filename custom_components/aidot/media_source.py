"""AiDot cloud recording media source."""

from __future__ import annotations

import logging
import time
from datetime import datetime, UTC

from homeassistant.components.media_player.const import MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceError,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import get_camera_coordinators
from .proxy import async_prewarm_events, sign_playback_url

_LOGGER = logging.getLogger(__name__)

_LOOK_BACK_MS = 7 * 86_400_000   # 7 days in milliseconds
_PAGE_SIZE = 30


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up AiDot media source."""
    return AidotMediaSource(hass)


class AidotMediaSource(MediaSource):
    """AiDot cloud recording media source."""

    name = "AiDot"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve an event identifier to a transcoded MP4 playback URL."""
        parts = (item.identifier or "").split("/", 1)
        if len(parts) != 2:
            raise Unresolvable(f"Invalid identifier: {item.identifier!r}")

        device_id, event_uuid = parts
        if get_camera_coordinators(self.hass).get(device_id) is None:
            raise Unresolvable(f"Camera {device_id} not found")

        _LOGGER.debug("AiDot resolve_media: device=%s event=%s", device_id, event_uuid)
        # The cloud only offers an HEVC HLS (.m3u8) stream, which browsers can't
        # decode.  Hand the player a same-origin MP4 URL; the proxy resolves the
        # short-lived signed HLS URL from device+event itself and transcodes to
        # H.264.  We deliberately don't embed a CloudFront URL here - keeping it
        # off this unauthenticated endpoint avoids an SSRF/LFI vector.  The URL is
        # signed (HMAC over device+event+expiry) so the proxy can authenticate the
        # otherwise-unauthenticated <video> request.
        return PlayMedia(sign_playback_url(device_id, event_uuid), "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse the cloud recording library."""
        identifier = item.identifier or ""

        if not identifier:
            return self._build_root()

        if "/" in identifier:
            raise MediaSourceError(f"Not browsable: {identifier!r}")

        return await self._build_events(identifier)

    def _build_root(self) -> BrowseMediaSource:
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=dev_id,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=coord.device_client.info.name or dev_id,
                can_play=False,
                can_expand=True,
            )
            for dev_id, coord in get_camera_coordinators(self.hass).items()
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type="",
            title="AiDot",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _build_events(self, device_id: str) -> BrowseMediaSource:
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            raise MediaSourceError(f"Camera {device_id} not found")

        dc = coord.device_client
        end_ts = int(time.time() * 1000)
        start_ts = end_ts - _LOOK_BACK_MS

        events = await dc.async_get_cloud_recordings(
            start_ts, end_ts, page=1, page_size=_PAGE_SIZE
        )

        children = []
        warm_ids: list[str] = []
        for ev in events:
            if not ev.get("hasVideo"):
                continue
            event_uuid = ev.get("eventUuid")
            if not event_uuid:
                continue
            warm_ids.append(event_uuid)

            desc = ev.get("eventDesc") or "Event"
            ts_ms = ev.get("eventTime") or ev.get("begin") or 0
            if ts_ms:
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                title = f"{desc} - {dt.strftime('%Y-%m-%d %H:%M')}"
            else:
                title = desc

            # Known cosmetic limitation: picUrl is a short-lived CDN URL, so the
            # media-browser thumbnail may go stale (broken icon) once it expires.
            # Playback is unaffected - the camera entity proxies live bytes for
            # exactly this reason; only the static browser preview is affected.
            pic_url = ev.get("picUrl") or None

            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{device_id}/{event_uuid}",
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=pic_url,
                )
            )

        # Pre-warm the cache for the most recent clips so tapping one plays
        # instantly instead of waiting on a cold transcode (best-effort).
        if warm_ids:
            # Named background task: gives it a proper lifecycle and keeps any
            # prewarm failure from surfacing as an unretrieved-task warning.
            self.hass.async_create_background_task(
                async_prewarm_events(device_id, warm_ids), "aidot-prewarm-events"
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=device_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=dc.info.name or device_id,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )
