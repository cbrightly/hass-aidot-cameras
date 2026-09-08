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
from .sd_recordings import (
    map_days,
    record_days,
    sd_card_known_absent,
    sd_empty_message,
)

_LOGGER = logging.getLogger(__name__)

#: The two halves of a camera's folder. They are kept apart because they differ
#: by three orders of magnitude in cost and have opposite failure modes: the
#: cloud lists a camera that is offline and shows nothing without internet, the
#: card is the other way round.
SOURCE_CLOUD = "cloud"
SOURCE_SD = "sd"


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
        """Browse: root -> camera -> source -> day -> events."""
        identifier = item.identifier or ""

        if not identifier:
            return self._build_root()

        # The play identifier is the only one with a slash, and it is not
        # browsable - resolve_media handles it.
        if "/" in identifier:
            raise MediaSourceError(f"Not browsable: {identifier!r}")

        parts = identifier.split("|")
        if len(parts) == 1:
            return self._build_sources(parts[0])
        if len(parts) == 2 and parts[1] == SOURCE_CLOUD:
            return await self._build_days(parts[0])
        if len(parts) == 2 and parts[1] == SOURCE_SD:
            return await self._build_sd_days(parts[0])
        if len(parts) == 3 and parts[1] == SOURCE_CLOUD:
            return await self._build_day(parts[0], parts[2])
        if len(parts) == 3 and parts[1] == SOURCE_SD:
            return await self._build_sd_day(parts[0], parts[2])
        raise MediaSourceError(f"Not browsable: {identifier!r}")

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

    def _build_sources(self, device_id: str) -> BrowseMediaSource:
        """The camera's two sources. Neither is opened to build this."""
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            raise MediaSourceError(f"Camera {device_id} not found")

        cache = getattr(coord, "sd_cache", None)
        card_present = getattr(
            getattr(coord.device_client, "status", None), "sd_card_present", None)
        if sd_card_known_absent(cache, card_present):
            # Said here and not only inside the folder, because the note inside
            # is one expand away and this is the view a user actually scans. A
            # bare "On device" for an empty slot looks identical to a bare "On
            # device" for a camera nobody has any reading about, which is the
            # collapse of two different answers this subsystem exists to undo.
            sd_title = "On device - no card"
        elif cache is None or not cache.answered:
            # No timestamp when nothing was read. "as of 14:32" asserts that we
            # know what is on the card as of 14:32, and a camera that answered
            # nothing never told us - so the stamp would be the same invented
            # answer that `answered` exists to prevent one level down, just
            # phrased as a time. The folder itself says which of the two it is.
            sd_title = "On device"
        else:
            when = dt_util.as_local(
                datetime.fromtimestamp(cache.fetched_at, tz=UTC))
            # The age is stated rather than hidden, because browsing this
            # folder deliberately does NOT refresh it - a user looking at a
            # stale list has to be able to see that it is stale.
            #
            # The date appears as soon as the listing is not from today. A bare
            # clock time reads as this morning, and a listing days old is the
            # normal state rather than an edge case: only the button and the
            # piggyback ever take one, so a camera nobody streams and nobody
            # presses Refresh for keeps its listing for the life of the process.
            # A format that cannot express the age is worse than no age at all,
            # because it asserts a reading that did not happen.
            today = dt_util.as_local(dt_util.utcnow()).date()
            stamp = "%H:%M" if when.date() == today else "%Y-%m-%d %H:%M"
            sd_title = f"On device - as of {when.strftime(stamp)}"

        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{device_id}|{SOURCE_CLOUD}",
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title="Cloud",
                can_play=False,
                can_expand=True,
            ),
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{device_id}|{SOURCE_SD}",
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=sd_title,
                can_play=False,
                can_expand=True,
            ),
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=device_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=coord.device_client.info.name or device_id,
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
                    identifier=f"{device_id}|{SOURCE_CLOUD}|{label}",
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
                    identifier=f"{device_id}|{SOURCE_CLOUD}|none",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=await self._empty_message(dc),
                    can_play=False,
                    can_expand=False,
                )
            ]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{device_id}|{SOURCE_CLOUD}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title="Cloud",
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
                    identifier=f"{device_id}|{SOURCE_CLOUD}|none",
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
            identifier=f"{device_id}|{SOURCE_CLOUD}|{day}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )

    def _sd_note(self, device_id: str, text: str) -> BrowseMediaSource:
        """A folder that explains itself instead of opening onto nothing.

        Not expandable and not playable: it is a sentence, and a user who
        opens it to find out more finds another empty folder.
        """
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{device_id}|{SOURCE_SD}|none",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=text,
            can_play=False,
            can_expand=False,
        )

    async def _build_sd_days(self, device_id: str) -> BrowseMediaSource:
        """Day folders for what the card holds. Reads the cache, nothing else.

        This must never take a listing. Listing costs a WebRTC session - 15-21 s
        on DTLS, 25-70 s cold on SDES - and wakes the camera, where the cloud
        equivalent is one ~200 ms request. The refresh button and the piggyback
        are the only paths that list; browsing reads what they left.
        """
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            raise MediaSourceError(f"Camera {device_id} not found")

        cache = getattr(coord, "sd_cache", None)
        tz = dt_util.DEFAULT_TIME_ZONE
        # Whether there is a card in the slot, if anyone said. Read off status
        # the coordinator already polls - no request, no session, no wake - and
        # left as None on an older library, which is the same "nobody said" the
        # tri-state already means.
        card_present = getattr(
            getattr(coord.device_client, "status", None), "sd_card_present", None)

        children: list[BrowseMediaSource] = []
        if cache is not None:
            # Records first and records only, when there are any. The occupancy
            # map read ALL ZERO on the same camera and window that returned four
            # real records (measured 2026-08-11), so a tree that consulted the
            # map first would report an empty card to a user holding one.
            days = record_days(cache.records, tz)
            for label, records in days:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{device_id}|{SOURCE_SD}|{label}",
                        media_class=MediaClass.DIRECTORY,
                        media_content_type="",
                        title=f"{label}  ({len(records)})",
                        can_play=False,
                        can_expand=True,
                    )
                )
            if not days:
                # No records at all is the only case where the map is worth
                # reading. It cannot say how many recordings an hour holds, so
                # the count is of HOURS and the title says which it is.
                for label, hours in map_days(cache.hours, cache.start_ts, tz):
                    unit = "hour" if len(hours) == 1 else "hours"
                    children.append(
                        BrowseMediaSource(
                            domain=DOMAIN,
                            identifier=f"{device_id}|{SOURCE_SD}|{label}",
                            media_class=MediaClass.DIRECTORY,
                            media_content_type="",
                            title=f"{label}  ({len(hours)} {unit} with footage)",
                            can_play=False,
                            can_expand=True,
                        )
                    )
            if children and not cache.complete:
                # A short list that reads as the whole card is the silent cap
                # this browser exists to remove, so it is said out loud and
                # placed where the days are, not hidden in a log.
                children.append(self._sd_note(
                    device_id, sd_empty_message(cache, card_present)))

        if not children:
            children = [self._sd_note(
                device_id, sd_empty_message(cache, card_present))]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{device_id}|{SOURCE_SD}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title="On device",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _build_sd_day(self, device_id: str, day: str) -> BrowseMediaSource:
        """One local day of what the card holds. Cache only, no session."""
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            raise MediaSourceError(f"Camera {device_id} not found")

        cache = getattr(coord, "sd_cache", None)
        if cache is None:
            raise MediaSourceError(f"Not browsable: {day!r}")
        tz = dt_util.DEFAULT_TIME_ZONE

        children: list[BrowseMediaSource] = []
        records = dict(record_days(cache.records, tz)).get(day)
        if records:
            for index, record in enumerate(records):
                when = datetime(
                    record.year, record.month, record.day, record.hour,
                    record.minute, record.second, tzinfo=UTC)
                local = dt_util.as_local(when)
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        # Stable within the page and never resolved: an
                        # on-device item is not playable until stage 3, so this
                        # only has to be unique, not meaningful.
                        identifier=f"{device_id}|{SOURCE_SD}|{day}|{index}",
                        media_class=MediaClass.VIDEO,
                        media_content_type="",
                        title=f"Recording - {local.strftime('%H:%M:%S')}",
                        # Stage 2 is a list. Pulling video off the card is a
                        # protocol nobody here has exercised.
                        can_play=False,
                        can_expand=False,
                    )
                )
        else:
            hours = dict(map_days(cache.hours, cache.start_ts, tz)).get(day)
            if hours is None:
                raise MediaSourceError(f"Not browsable: {day!r}")
            for hour in hours:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{device_id}|{SOURCE_SD}|{day}|h{hour}",
                        media_class=MediaClass.DIRECTORY,
                        media_content_type="",
                        # The map says an hour holds something and cannot say
                        # what or how much. That still answers "was anything
                        # recorded while the internet was down".
                        title=f"{hour:02d}:00 - footage on the card",
                        can_play=False,
                        can_expand=False,
                    )
                )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{device_id}|{SOURCE_SD}|{day}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=f"{day}  ({len(children)})",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )
