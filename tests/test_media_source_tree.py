"""The browser tree: camera -> day -> events.

Day folders are not decoration. The server caps a page at 10, so a flat
seven-day list either shows ten events out of hundreds (what shipped) or fires
thirty-odd requests to build one screen. A day folder is one narrow query that
pages inside itself.
"""

import time
from unittest.mock import patch

from homeassistant.util import dt as dt_util

from custom_components.aidot.media_source import AidotMediaSource
from custom_components.aidot.recordings import day_windows


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
        node = await src.async_browse_media(_Item("dev1"))
    assert node.children, "a camera with events must offer day folders"
    assert all(c.can_expand and not c.can_play for c in node.children)
    assert "23" in node.children[0].title


async def test_a_day_folder_lists_every_event_not_the_first_ten(hass):
    dc = _DC()
    dc.events = [_event(i) for i in range(23)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(
            _Item("dev1"))
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
            _Item("dev1"))
    # "/" is the event separator; a day must not contain one.
    assert "/" not in days.children[0].identifier


async def test_no_events_shows_an_explanation_not_an_empty_folder(hass):
    dc = _DC()
    dc.events = []
    src, p = _source(hass, dc)
    with p:
        node = await src.async_browse_media(
            _Item("dev1"))
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
            _Item("dev1"))
    assert "expired" in node.children[0].title.lower()


async def test_events_without_video_are_filtered(hass):
    dc = _DC()
    dc.events = [_event(0), dict(_event(1), hasVideo=False)]
    src, p = _source(hass, dc)
    with p:
        days = await src.async_browse_media(
            _Item("dev1"))
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
        days = await src.async_browse_media(_Item("dev1"))
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
        days = await src.async_browse_media(_Item("dev1"))
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
        days = await src.async_browse_media(_Item("dev1"))
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
        days = await src.async_browse_media(_Item("dev1"))
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
        days = await src.async_browse_media(_Item("dev1"))
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
        days = await src.async_browse_media(_Item("dev1"))
        node = await src.async_browse_media(_Item(days.children[0].identifier))
    assert len(node.children) == 1
    title = node.children[0].title
    assert "try again" not in title.lower(), title
    assert "owner" not in title.lower(), title
    assert "playable" in title.lower(), title
