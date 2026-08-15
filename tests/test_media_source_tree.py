"""The browser tree: camera -> day -> events.

Day folders are not decoration. The server caps a page at 10, so a flat
seven-day list either shows ten events out of hundreds (what shipped) or fires
thirty-odd requests to build one screen. A day folder is one narrow query that
pages inside itself.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aidot_cameras.camera.sd_events import SdEvent
from homeassistant.util import dt as dt_util

from custom_components.aidot.media_source import AidotMediaSource
from custom_components.aidot.recordings import day_windows
from custom_components.aidot.sd_recordings import SdCache


class _Item:
    """Stands in for MediaSourceItem: async_browse_media reads .identifier."""

    def __init__(self, identifier: str | None):
        self.identifier = identifier


def _today_ms() -> int:
    """Start of the current local day, plus a minute of margin.

    Derived rather than hardcoded: the day windows under test come from the
    live clock, so a literal timestamp pins these tests to the date they were
    written and turns CI red the next day.
    """
    now_ms = int(time.time() * 1000)
    return day_windows(now_ms, 1, dt_util.DEFAULT_TIME_ZONE)[0][1] + 60_000


def _event(i: int) -> dict:
    return {
        "eventUuid": f"v1:{i}",
        "eventTime": _today_ms() + i * 1000,
        "eventDesc": "Person",
        "hasVideo": True,
        "picUrl": "https://example.invalid/x.jpg",
    }


class _Coord:
    def __init__(self, dc):
        self.device_client = dc


class _DC:
    def __init__(self, name="Family Room Cam"):
        from types import SimpleNamespace
        self.info = SimpleNamespace(name=name)
        self.device_id = "dev1"
        self.plan = {"subscribeStatus": 1, "endTime": 4_000_000_000_000}
        self.events: list[dict] = []
        # Bound as an instance attribute (not a class method) so a test can
        # `del dc.async_count_cloud_recordings` to genuinely exercise the
        # getattr-absent path an older installed library takes, rather than
        # simulating it by returning None.
        self.async_count_cloud_recordings = self._async_count_cloud_recordings
        # When True, the listing endpoint returns nothing even though the
        # count endpoint still reports the true total - a failed fetch, not
        # an empty or account-restricted day.
        self.fail_listing = False

    async def async_get_cloud_plan(self):
        return self.plan

    async def _async_count_cloud_recordings(self, s, e):
        # The real library counts events in the window in one request; a
        # fake without this returns None for every window, which is a
        # client that cannot count, not the one this suite is testing.
        return sum(1 for ev in self.events if s <= ev["eventTime"] < e)

    async def async_get_cloud_recordings(self, s, e, *, page=1, page_size=10):
        if self.fail_listing:
            return []
        # Windowed, like the real endpoint, so a folder for the wrong day
        # cannot accidentally pass by reusing another day's events.
        window = [ev for ev in self.events if s <= ev["eventTime"] < e]
        first = (page - 1) * page_size
        return window[first:first + page_size]


def _source(hass, dc):
    src = AidotMediaSource(hass)
    return src, patch(
        "custom_components.aidot.media_source.get_camera_coordinators",
        return_value={"dev1": _Coord(dc)})


async def test_a_camera_expands_into_day_folders_with_counts(hass):
    dc = _DC()
    dc.events = [_event(i) for i in range(23)]
    src, p = _source(hass, dc)
    with p:
        node = await src.async_browse_media(_Item("dev1|cloud"))
    assert node.children, "a camera with events must offer day folders"
    assert all(c.can_expand and not c.can_play for c in node.children)
    assert "23" in node.children[0].title


async def test_a_day_folder_lists_every_event_not_the_first_ten(hass):
    dc = _DC()
    dc.events = [_event(i) for i in range(23)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(
            _Item("dev1|cloud"))
        day_id = days.children[0].identifier
        node = await src.async_browse_media(
            _Item(day_id))
    assert len(node.children) == 23
    assert all(c.can_play for c in node.children)


async def test_a_day_identifier_is_not_mistaken_for_an_event(hass):
    dc = _DC()
    dc.events = [_event(0)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(
            _Item("dev1|cloud"))
    # "/" is the event separator; a day must not contain one.
    assert "/" not in days.children[0].identifier


async def test_no_events_shows_an_explanation_not_an_empty_folder(hass):
    dc = _DC()
    dc.events = []
    src, p = _source(hass, dc)
    with p:
        node = await src.async_browse_media(
            _Item("dev1|cloud"))
    assert len(node.children) == 1
    child = node.children[0]
    assert not child.can_play and not child.can_expand
    assert "owner" in child.title.lower()


async def test_a_lapsed_plan_is_named_in_the_empty_folder(hass):
    dc = _DC()
    dc.events = []
    dc.plan = {"subscribeStatus": 1, "endTime": 1_000_000_000_000}
    src, p = _source(hass, dc)
    with p:
        node = await src.async_browse_media(
            _Item("dev1|cloud"))
    assert "expired" in node.children[0].title.lower()


async def test_events_without_video_are_filtered(hass):
    dc = _DC()
    dc.events = [_event(0), dict(_event(1), hasVideo=False)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(
            _Item("dev1|cloud"))
        node = await src.async_browse_media(
            _Item(days.children[0].identifier))
    assert len(node.children) == 1


async def test_an_old_library_without_a_count_still_explains_an_empty_day(hass):
    # The production case today: the installed library predates
    # async_count_cloud_recordings, so every count is None, no day can be
    # dropped up front, and the camera node's empty branch never runs.
    dc = _DC()
    dc.events = []
    del dc.async_count_cloud_recordings
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert len(node.children) == 1
    child = node.children[0]
    assert not child.can_play and not child.can_expand
    # The default plan is active, so the owner-visibility message applies.
    assert "owner" in child.title.lower()


async def test_a_capped_day_discloses_the_cap_even_with_no_count(hass):
    # 250 events, no count method: the ceiling is hit, the total is unknown,
    # and the folder must still not present 200 as the whole day.
    dc = _DC()
    dc.events = [_event(i) for i in range(250)]
    del dc.async_count_cloud_recordings
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert len(node.children) == 200
    assert "unknown" in node.title.lower()
    assert "200" in node.title


async def test_a_capped_day_names_how_many_were_not_shown(hass):
    # The exact string this whole plan exists to produce. Deleting the branch
    # that builds it used to leave the suite green.
    dc = _DC()
    dc.events = [_event(i) for i in range(250)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert "newest 200 of 250" in node.title, node.title


async def test_a_day_that_fails_to_load_does_not_blame_the_account(hass):
    # The server says 23; the listing returns nothing. That is a failed
    # request, and pointing the user at their account sends them to look at
    # the wrong thing.
    dc = _DC()
    dc.events = [_event(i) for i in range(23)]
    dc.fail_listing = True
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert len(node.children) == 1
    assert "23" in node.children[0].title
    assert "owner" not in node.children[0].title.lower()
    assert "(0)" in node.title, node.title


async def test_filtered_non_video_events_are_not_reported_as_a_cap(hass):
    # 2 events, 1 without video. Nothing was capped, so the title must not
    # claim anything was held back.
    dc = _DC()
    dc.events = [_event(0), dict(_event(1), hasVideo=False)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert "newest" not in node.title.lower(), node.title


async def test_a_day_of_non_playable_events_is_not_reported_as_a_failure(hass):
    # Five events, none with video. The fetch worked and nothing was capped,
    # so "try again" would be telling the user to retry a request that
    # succeeded.
    dc = _DC()
    dc.events = [dict(_event(i), hasVideo=False) for i in range(5)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert len(node.children) == 1
    title = node.children[0].title
    assert "try again" not in title.lower(), title
    assert "owner" not in title.lower(), title
    assert "playable" in title.lower(), title


async def test_a_camera_offers_two_sources(hass):
    src, p = _source(hass, _DC())
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert [c.title for c in node.children] == [
        "Cloud", "On device"]
    assert [c.identifier for c in node.children] == ["dev1|cloud", "dev1|sd"]
    assert all(c.can_expand and not c.can_play for c in node.children)


async def test_the_cloud_days_moved_under_the_cloud_folder(hass):
    dc = _DC()
    dc.events = [_event(i) for i in range(3)]
    src, p = _source(hass, dc)
    with p:
        node = await src.async_browse_media(_Item("dev1|cloud"))
    assert node.children[0].identifier.startswith("dev1|cloud|")


async def test_a_cloud_day_still_lists_its_playable_events(hass):
    dc = _DC()
    dc.events = [_event(i) for i in range(3)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(
            _Item(days.children[0].identifier))
    assert len(node.children) == 3
    assert all(c.can_play for c in node.children)


async def test_the_play_identifier_is_unchanged(hass):
    # It keeps its slash form: async_resolve_media parses it, and any
    # dashboard shortcut a user already saved holds exactly this string.
    dc = _DC()
    dc.events = [_event(0)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(_Item("dev1|cloud"))
        node = await src.async_browse_media(
            _Item(days.children[0].identifier))
    assert node.children[0].identifier == "dev1/v1:0"


async def test_an_unknown_source_is_refused(hass):
    from homeassistant.components.media_source import MediaSourceError

    src, p = _source(hass, _DC())
    with p, pytest.raises(MediaSourceError):
        await src.async_browse_media(_Item("dev1|nonsense"))


async def test_a_camera_never_listed_does_not_date_its_on_device_folder(hass):
    # No cache means no "as of", not "as of 00:00" - a fabricated time reads
    # as a listing that happened.
    src, p = _source(hass, _DC())
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children[1].title == "On device"


def _sd_source(hass, cache):
    """A source over one camera holding a given on-device cache.

    The fake device client counts the two calls this subtree is forbidden to
    make. Counting them is the point: "browsing never opens a session" is the
    property the whole cost model rests on, and it is the one that would be
    quietly wrong in production.
    """
    dc = _DC()
    dc.open_count = 0
    dc.list_calls = 0

    async def _open(*a, **k):
        dc.open_count += 1

    async def _list(*a, **k):
        # Falls off the end, which is the library's "could not ask" answer.
        # Written this way rather than `return None` only because ruff's RET501
        # is on for tests; the value it yields is the same.
        dc.list_calls += 1

    dc.start_keepalive = _open
    dc.async_get_sd_recordings = _list

    coord = _Coord(dc)
    coord.sd_cache = cache

    # The coordinator's own listing entry points, counted on the same
    # counters. Without these the stand-in has no such attributes at all, so a
    # browse that reached for one would raise instead of registering - and an
    # AttributeError is a much weaker signal than a count that must stay zero.
    async def _coord_list(*a, **k):
        dc.list_calls += 1
        return False

    async def _coord_piggyback(*a, **k):
        dc.list_calls += 1

    coord.async_list_sd_recordings = _coord_list
    coord.async_piggyback_sd_refresh = _coord_piggyback

    src = AidotMediaSource(hass)
    return src, dc, patch(
        "custom_components.aidot.media_source.get_camera_coordinators",
        return_value={"dev1": coord})


def _sd_record(hour, day=11, mi=0):
    return SdEvent(2026, 8, day, hour, mi, 0, 0, 1, 0)


def _sd_day() -> str:
    """The local day the fixed UTC records fall on, in this runner's zone."""
    from datetime import UTC, datetime

    return dt_util.as_local(
        datetime(2026, 8, 11, 12, 0, tzinfo=UTC)).strftime("%Y-%m-%d")


def _four_records() -> SdCache:
    # Midday UTC so every runner timezone files them on the same local day -
    # a record at 23:00 UTC would land on tomorrow in Berlin and yesterday in
    # Los Angeles, and the test would be about the runner, not the code.
    return SdCache(
        records=[_sd_record(12), _sd_record(12, mi=5),
                 _sd_record(13), _sd_record(13, mi=5)],
        hours=None, complete=True,
        start_ts=0.0, end_ts=0.0, fetched_at=time.time())


async def test_browsing_on_device_never_opens_a_session(hass):
    src, dc, p = _sd_source(hass, _four_records())
    with p:
        await src.async_browse_media(_Item("dev1|sd"))
        await src.async_browse_media(_Item(f"dev1|sd|{_sd_day()}"))
    assert dc.open_count == 0
    assert dc.list_calls == 0


async def test_the_day_folders_come_from_the_records(hass):
    src, _dc, p = _sd_source(hass, _four_records())
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
    assert [c.title for c in node.children] == [f"{_sd_day()}  (4)"]


async def test_records_win_over_an_all_zero_map(hass):
    # Measured 2026-08-11: the camera that returned four real records returned
    # an ALL-ZERO occupancy map for the same window. A tree that read the map
    # first would have reported an empty card to a user holding four
    # recordings.
    cache = _four_records()
    cache.hours = bytes(168)
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
    assert node.children[0].title.endswith("(4)")


async def test_the_map_is_used_only_when_there_are_no_records(hass):
    from datetime import UTC, datetime

    start = datetime(2026, 8, 11, 0, 0, tzinfo=UTC).timestamp()
    hours = bytearray(24)
    hours[12] = 1
    cache = SdCache(records=[], hours=bytes(hours), complete=True,
                    start_ts=start, end_ts=start + 86400,
                    fetched_at=time.time())
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
        # The hour rows the map produces are items too, and stage 2's scope
        # fence is every on-device item, not only the record-derived ones.
        day = await src.async_browse_media(_Item(node.children[0].identifier))
    assert len(node.children) == 1
    assert "hour" in node.children[0].title
    assert day.children and all(c.can_play is False for c in day.children)


async def test_an_on_device_item_is_not_playable(hass):
    # Stage 2 is a list. RECORD_PLAYCONTROL has never been sent, and pulling
    # video off the card is a subsystem comparable to the live path.
    src, _dc, p = _sd_source(hass, _four_records())
    with p:
        node = await src.async_browse_media(_Item(f"dev1|sd|{_sd_day()}"))
    assert len(node.children) == 4
    assert all(c.can_play is False for c in node.children)


async def test_a_camera_never_listed_says_how_to_list_it(hass):
    src, _dc, p = _sd_source(hass, None)
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
    assert len(node.children) == 1
    assert "Refresh" in node.children[0].title
    assert node.children[0].can_play is False
    assert node.children[0].can_expand is False, \
        "a sentence a user can open onto another empty folder is not an answer"


async def test_a_silent_camera_is_not_told_its_card_is_empty(hass):
    # Measured behaviour the library now distinguishes: a camera that answers
    # nothing carries an empty record list exactly like a camera with an empty
    # card, and only `answered` separates them.
    cache = SdCache(records=[], hours=None, answered=False, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=time.time())
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
    assert "did not answer" in node.children[0].title
    assert "nothing recorded" not in node.children[0].title


async def test_a_listed_empty_card_does_not_say_it_was_never_listed(hass):
    # Two different facts. Telling a user to press Refresh when the camera has
    # already answered "nothing here" sends them to retry a request that worked.
    cache = SdCache(records=[], hours=None, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=time.time())
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
    assert "Refresh" not in node.children[0].title


async def test_a_truncated_list_says_so_rather_than_reading_as_the_whole_card(hass):
    cache = _four_records()
    cache.complete = False
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1|sd"))
    assert any("partial" in c.title for c in node.children)
    assert any(c.title.endswith("(4)") for c in node.children), \
        "the days are still shown - a short list is not no list"


async def test_a_listed_camera_dates_its_on_device_folder(hass):
    # The counterpart to the never-listed case: browsing deliberately does not
    # refresh, so a user looking at a stale list has to be able to see its age.
    src, _dc, p = _sd_source(hass, _four_records())
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children[1].title.startswith("On device - as of ")
    # Today's listing keeps the bare clock time: a date on it would be noise on
    # the case that is genuinely "this morning".
    assert node.children[1].title.count("-") == 1


async def test_a_listing_from_another_day_carries_its_date(hass):
    # A bare clock time reads as today, and a days-old listing is the normal
    # state rather than an edge case: only the button and the piggyback ever
    # refresh one, so a camera nobody streams keeps its listing indefinitely.
    # "as of 01:00" for a four-day-old reading asserts a reading that did not
    # happen, in the one string whose stated job is to expose staleness.
    from datetime import UTC, datetime

    cache = _four_records()
    cache.fetched_at = time.time() - 4 * 86400
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    when = dt_util.as_local(datetime.fromtimestamp(cache.fetched_at, tz=UTC))
    assert when.strftime("%Y-%m-%d") in node.children[1].title


async def test_a_camera_with_no_card_says_so_on_the_folder_itself(hass):
    # The message inside the folder only appears once a user expands it, and in
    # the camera view a no-card folder and a genuinely-unknown one were both
    # bare "On device" - the same collapse of two different answers into one
    # appearance that this whole subsystem exists to undo.
    cache = SdCache(records=[], hours=None, answered=False, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=time.time())
    src, dc, p = _sd_source(hass, cache)
    dc.status = SimpleNamespace(sd_card_present=False)
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children[1].title == "On device - no card"
    assert "as of" not in node.children[1].title


async def test_an_unknown_card_leaves_the_folder_title_alone(hass):
    # Four of seven cameras report nothing about their slot. Labelling those
    # "no card" would be the invented answer, just moved into the title.
    cache = SdCache(records=[], hours=None, answered=False, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=time.time())
    src, dc, p = _sd_source(hass, cache)
    dc.status = SimpleNamespace(sd_card_present=None)
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children[1].title == "On device"


async def test_a_camera_that_answered_keeps_its_stamp_over_a_stale_absent_flag(hass):
    # The flag is a cloud attribute and lags a card inserted a moment ago. A
    # camera that actually answered has said more about its own slot than the
    # attribute has.
    src, dc, p = _sd_source(hass, _four_records())
    dc.status = SimpleNamespace(sd_card_present=False)
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children[1].title.startswith("On device - as of ")


async def test_a_silent_camera_gets_no_as_of_stamp(hass):
    # "as of 14:32" asserts that we know what is on the card as of 14:32. A
    # camera that answered nothing never said that, so stamping a time on it is
    # the same invented answer `answered` exists to prevent, phrased as a clock.
    cache = SdCache(records=[], hours=None, answered=False, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=time.time())
    src, _dc, p = _sd_source(hass, cache)
    with p:
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children[1].title == "On device"
    assert "as of" not in node.children[1].title


async def test_an_unknown_on_device_day_is_refused(hass):
    from homeassistant.components.media_source import MediaSourceError

    src, _dc, p = _sd_source(hass, _four_records())
    with p, pytest.raises(MediaSourceError):
        await src.async_browse_media(_Item("dev1|sd|1999-01-01"))
