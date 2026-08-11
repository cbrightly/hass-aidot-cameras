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
from homeassistant.util import dt as dt_util

from .const import CLOUD_LOOKBACK_DAYS, DOMAIN, MAX_EVENTS_PER_DAY
from .coordinator import get_camera_coordinators
from .proxy import async_prewarm_events, sign_playback_url
from .recordings import (
    day_summaries,
    day_windows,
    empty_cloud_message,
    events_for_window,
)

_LOGGER = logging.getLogger(__name__)


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
        """Browse the cloud recording library: root -> camera -> day -> events."""
        identifier = item.identifier or ""

        if not identifier:
            return self._build_root()

        if "/" in identifier:
            raise MediaSourceError(f"Not browsable: {identifier!r}")

        if "|" in identifier:
            device_id, day = identifier.split("|", 1)
            return await self._build_day(device_id, day)

        return await self._build_days(identifier)

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

    async def _empty_message(self, dc) -> str:
        """Why a folder has nothing in it, in words a user can act on.

        Shared by the camera node and the day node: a day folder that opens
        onto nothing is the same failure as a camera that does, and an
        unexplained empty folder is what this browser is here to stop.
        """
        plan = None
        _plan_fn = getattr(dc, "async_get_cloud_plan", None)
        if _plan_fn is not None:
            plan = await _plan_fn()
        return empty_cloud_message(
            plan, int(time.time() * 1000), dt_util.DEFAULT_TIME_ZONE)

    async def _day_empty_message(
        self, dc, total: int | None, fetched: int
    ) -> str:
        """Why a DAY opened onto nothing. Three different reasons, three answers.

        A day the server says holds events, that we then fetched nothing
        for, is a failed request - not an account problem, and pointing the
        user at their subscription sends them to look at the wrong thing.

        A day we DID fetch events for, that has nothing playable in it, is
        not a failure at all: the events exist and none of them carry
        video. Telling that user to try again is telling them to retry a
        request that worked.

        Anything else falls through to the camera-level explanation.
        """
        if fetched:
            return "No playable recordings for this day."
        if total:
            return (f"Could not load this day's recordings - the server "
                    f"reports {total}. Try again.")
        return await self._empty_message(dc)

    async def _build_days(self, device_id: str) -> BrowseMediaSource:
        """The camera's day folders, newest first, empties dropped."""
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            raise MediaSourceError(f"Camera {device_id} not found")
        dc = coord.device_client

        now_ms = int(time.time() * 1000)
        windows = day_windows(
            now_ms, CLOUD_LOOKBACK_DAYS, dt_util.DEFAULT_TIME_ZONE)
        summaries = await day_summaries(dc, windows)

        if summaries:
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{device_id}|{label}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    # The day's TRUE count, from one request, not the number
                    # this browser is willing to fetch. A bare "200" against a
                    # day of 1517 is the silent cap this plan exists to
                    # remove. None means the count could not be obtained -
                    # say nothing rather than imply zero.
                    title=(f"{label}  ({count})" if count is not None
                           else f"{label}"),
                    can_play=False,
                    can_expand=True,
                )
                for label, _s, _e, count in summaries
            ]
        else:
            # Never a bare empty folder: four different conditions produce no
            # events and the user cannot tell them apart without being told.
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{device_id}|none",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=await self._empty_message(dc),
                    can_play=False,
                    can_expand=False,
                )
            ]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=device_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=dc.info.name or device_id,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _build_day(self, device_id: str, day: str) -> BrowseMediaSource:
        """One day's events, paged out of the ten-per-page cap."""
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            raise MediaSourceError(f"Camera {device_id} not found")
        dc = coord.device_client

        now_ms = int(time.time() * 1000)
        window = next(
            (w for w in day_windows(
                now_ms, CLOUD_LOOKBACK_DAYS, dt_util.DEFAULT_TIME_ZONE)
             if w[0] == day),
            None,
        )
        if window is None:
            raise MediaSourceError(f"Not browsable: {day!r}")

        _label, start_ms, end_ms = window
        events, total = await events_for_window(dc, start_ms, end_ms)

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
                local = dt_util.as_local(
                    datetime.fromtimestamp(ts_ms / 1000, tz=UTC))
                title = f"{desc} - {local.strftime('%H:%M:%S')}"
            else:
                title = desc

            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{device_id}/{event_uuid}",
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=ev.get("picUrl") or None,
                )
            )

        # Pre-warm the newest clips so tapping one plays instead of waiting
        # on a cold transcode. Bounded: a 200-event day must not warm 200
        # clips.
        if warm_ids:
            self.hass.async_create_task(
                async_prewarm_events(device_id, warm_ids[:10])
            )

        # Before any placeholder is substituted: the placeholder is not an
        # event, and counting it as one is how a day with nothing in it came
        # to title itself "(1)".
        event_count = len(children)
        # Truncation means OUR ceiling stopped the fetch. It does not mean
        # some of what we fetched had no video - filtering is not hiding,
        # and reporting it as a cap fires on days where nothing was capped.
        capped = len(events) >= MAX_EVENTS_PER_DAY

        if not children:
            # Reached when the day's count was unknown so the folder could
            # not be dropped up front - an older library, or a failed count.
            # An empty folder here would be exactly the unexplained blank
            # the camera node avoids.
            children = [
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{device_id}|none",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=await self._day_empty_message(
                        dc, total, len(events)),
                    can_play=False,
                    can_expand=False,
                )
            ]

        title = f"{day}  ({event_count})"
        if capped and total is not None:
            # A silent cap reads as "this is everything" when it is not. The
            # server's own total is known, so say exactly how much is hidden
            # rather than hinting that something is.
            title = f"{day}  (newest {event_count} of {total})"
        elif capped:
            # The count could not be obtained, but the ceiling was reached,
            # so there is more and we cannot say how much. Saying nothing
            # here would render a capped list as if it were the whole day.
            title = f"{day}  (newest {event_count}, total unknown)"

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{device_id}|{day}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )
