"""A battery camera's card is listed over the session a view opened.

A battery camera is never pre-warmed, so the listing that rides a pre-warm
never reaches it, and its "On device" folder stayed empty until somebody
pressed the refresh button - which wakes the camera for that alone. With the
``sd_list_on_view`` option on, a view's own session carries the listing.

What has to hold, and is held here:

* it opens nothing and wakes nothing: it waits for a session, and gives up
  the moment the keepalive that would have produced one has ended;
* it is rate limited per camera, and skipped while a card recording is being
  fetched or another listing is running;
* a listing that failed - no answer, or cut off because the viewer left -
  leaves the list the camera gave earlier in place;
* it is off unless the option is on, and never runs for a mains camera.
"""

import asyncio
import time
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aidot import coordinator as coordinator_module
from custom_components.aidot.camera import AidotCamera
from custom_components.aidot.const import (
    CONF_SD_LIST_ON_VIEW,
    DEFAULT_SD_LIST_ON_VIEW,
    SD_CACHE_TTL_S,
    SD_RIDE_MIN_INTERVAL_S,
)
from custom_components.aidot.coordinator import SLOT_SD, AidotCameraUpdateCoordinator
from custom_components.aidot.sd_recordings import SdCache, sd_record_key


_Record = namedtuple("_Record", "year month day hour minute second event")


def _record(hour):
    return _Record(2026, 11, 10, hour, 0, 0, 1)


class _List:
    def __init__(self, records=(), answered=True, complete=True):
        self.records = list(records)
        self.hours = None
        self.answered = answered
        self.complete = complete


class _Client:
    """The parts of the library client a listing touches, and a count of what
    was sent to the camera."""

    device_id = "dev1"
    cloud_reports_offline = False

    def __init__(self, result=None, *, session=True):
        self.result = result
        self.session = session
        # Set for as long as a keepalive runs, session or not.
        self.stream_rtsp_url = "rtsp://127.0.0.1:8554/dev1"
        self.listings = 0
        self.thumbnail_asks = 0
        self.pictures = {}
        self.ends_session = False
        self.start_keepalive = AsyncMock()
        self.async_wake_camera = AsyncMock()

    @property
    def has_live_session(self):
        return self.session

    async def async_get_sd_recordings(self, **kwargs):
        self.listings += 1
        if self.ends_session:
            self.session = False
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def async_get_sd_thumbnails(self, records, **kwargs):
        self.thumbnail_asks += 1
        return {r: data for r, data in self.pictures.items() if r in records}


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_RIDE_SETTLE_S", 0.0)


def _coord(result=None, **client_kwargs) -> AidotCameraUpdateCoordinator:
    """A camera coordinator with only the card state initialised (see
    test_coordinator._coord for why not the constructor)."""
    coord = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    coord.device_client = _Client(result, **client_kwargs)
    coord.sd_cache = None
    coord._sd_opener = None
    coord._sd_lock = asyncio.Lock()
    coord.media_slot_owner = None
    coord._sd_waiting = {}
    coord._sd_ride_running = False
    coord._sd_ride_sent_at = None
    return coord


def _old_cache(records=(), **kwargs) -> SdCache:
    return SdCache(
        records=list(records), fetched_at=time.time() - SD_CACHE_TTL_S - 60, **kwargs
    )


# --- it lists, over the session that is there -------------------------------


async def test_a_view_session_lists_the_card_once():
    r = _record(9)
    coord = _coord(_List([r]))
    assert await coord.async_ride_sd_listing() is True
    assert coord.sd_cache.records == [r]
    assert coord.device_client.listings == 1


async def test_it_opens_nothing_and_wakes_nothing():
    opened = []
    coord = _coord(_List([_record(9)]))

    async def _open():
        opened.append(1)

    coord.register_session_opener(_open)
    await coord.async_ride_sd_listing()
    assert opened == []
    coord.device_client.start_keepalive.assert_not_awaited()
    coord.device_client.async_wake_camera.assert_not_awaited()


async def test_it_waits_for_the_session_the_view_asked_for():
    # The view path returns from start_keepalive before the handshake is done.
    coord = _coord(_List([_record(9)]), session=False)
    client = coord.device_client

    async def _handshake():
        await asyncio.sleep(0.05)
        client.session = True

    handshake = asyncio.ensure_future(_handshake())
    assert await coord.async_ride_sd_listing() is True
    await handshake
    assert client.listings == 1


async def test_it_gives_up_when_the_keepalive_has_ended(monkeypatch):
    # The viewer left before the camera connected. Nothing is on its way, and
    # waiting out the window - let alone asking - would be for nobody.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 30.0)
    coord = _coord(_List([_record(9)]), session=False)
    coord.device_client.stream_rtsp_url = None
    started = time.monotonic()
    assert await coord.async_ride_sd_listing() is False
    assert time.monotonic() - started < 1.0
    assert coord.device_client.listings == 0
    # Nothing was sent, so nothing counts against the rate limit.
    assert coord._sd_ride_sent_at is None


async def test_it_gives_up_when_no_session_arrives(monkeypatch):
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 0.05)
    coord = _coord(_List([_record(9)]), session=False)
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 0


async def test_a_session_that_ends_while_settling_is_not_asked(monkeypatch):
    monkeypatch.setattr(coordinator_module, "SD_RIDE_SETTLE_S", 0.05)
    coord = _coord(_List([_record(9)]))
    client = coord.device_client
    ride = asyncio.ensure_future(coord.async_ride_sd_listing())
    await asyncio.sleep(0.01)
    client.session = False
    assert await ride is False
    assert client.listings == 0


# --- when it is skipped -------------------------------------------------------


async def test_a_recent_list_is_not_taken_again():
    coord = _coord(_List([_record(9)]))
    coord.sd_cache = SdCache(fetched_at=time.time())
    assert coord.sd_ride_skip_reason() is not None
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 0


async def test_a_failed_listing_is_not_retried_inside_the_rate_limit():
    # An unanswered listing leaves the old list in place, which is no
    # fresher for it, so without its own limit every view of a silent camera
    # would ask again.
    coord = _coord(_List(answered=False))
    coord.sd_cache = _old_cache([_record(8)])
    assert await coord.async_ride_sd_listing() is False
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 1
    # Past the limit it is tried again.
    coord._sd_ride_sent_at = time.monotonic() - SD_RIDE_MIN_INTERVAL_S - 1
    coord.device_client.result = _List([_record(9)])
    assert await coord.async_ride_sd_listing() is True
    assert coord.device_client.listings == 2


@pytest.mark.parametrize("state", ["playing", "queued"])
async def test_it_is_skipped_while_a_card_recording_is_fetched(state):
    coord = _coord(_List([_record(9)]))
    if state == "playing":
        coord.media_slot_owner = SLOT_SD
    coord._sd_waiting = {"sd-20261110T090000-1": 1}
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 0


async def test_it_is_skipped_not_queued_behind_another_listing():
    coord = _coord(_List([_record(9)]))
    async with coord._sd_lock:
        # Returns at once: queued, it would wait here for ever.
        assert await asyncio.wait_for(coord.async_ride_sd_listing(), 1.0) is False
    assert coord.device_client.listings == 0


async def test_a_playback_that_starts_during_the_wait_stops_it():
    coord = _coord(_List([_record(9)]), session=False)
    client = coord.device_client
    ride = asyncio.ensure_future(coord.async_ride_sd_listing())
    await asyncio.sleep(0.02)
    coord.media_slot_owner = SLOT_SD
    client.session = True
    assert await ride is False
    assert client.listings == 0


async def test_only_one_waits_on_a_session_at_a_time():
    coord = _coord(_List([_record(9)]), session=False)
    client = coord.device_client
    first = asyncio.ensure_future(coord.async_ride_sd_listing())
    await asyncio.sleep(0.02)
    assert coord.sd_ride_skip_reason() is not None
    assert await coord.async_ride_sd_listing() is False
    client.session = True
    assert await first is True
    assert client.listings == 1
    assert coord._sd_ride_running is False


# --- a failed listing leaves the previous list intact ------------------------


async def test_an_unanswered_listing_keeps_the_previous_list():
    r = _record(9)
    coord = _coord(_List(answered=False))
    before = coord.sd_cache = _old_cache([r])
    assert await coord.async_ride_sd_listing() is False
    assert coord.sd_cache is before


async def test_an_unanswered_first_listing_is_stored_as_the_button_s_would_be():
    # There is no previous list to protect. "The camera did not answer" is
    # what the browser then says, instead of a list that was never taken.
    coord = _coord(_List(answered=False))
    assert await coord.async_ride_sd_listing() is True
    assert coord.sd_cache is not None
    assert coord.sd_cache.answered is False
    assert coord.sd_cache.records == []


# --- the rate limit counts listings that reached the camera ------------------


async def test_a_listing_that_was_sent_starts_the_rate_limit():
    coord = _coord(_List([_record(9)]))
    before = time.monotonic()
    assert await coord.async_ride_sd_listing() is True
    assert coord._sd_ride_sent_at is not None
    assert coord._sd_ride_sent_at >= before


async def test_an_unanswered_listing_was_sent_and_starts_it_too():
    coord = _coord(_List(answered=False))
    coord.sd_cache = _old_cache([_record(8)])
    assert await coord.async_ride_sd_listing() is False
    assert coord._sd_ride_sent_at is not None


async def test_a_listing_that_raised_does_not_start_the_rate_limit():
    coord = _coord(RuntimeError("channel closed"))
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 1
    assert coord._sd_ride_sent_at is None
    # So the next view lists.
    coord.device_client.result = _List([_record(9)])
    assert await coord.async_ride_sd_listing() is True


async def test_a_listing_with_no_session_to_ask_does_not_start_it():
    # None is the library's "could not ask": nothing went to the camera.
    coord = _coord(None)
    assert await coord.async_ride_sd_listing() is False
    assert coord._sd_ride_sent_at is None


async def test_a_listing_a_playback_got_ahead_of_does_not_start_it(monkeypatch):
    coord = _coord(_List([_record(9)]))
    # The slot is taken after the last look and before the listing is sent.
    monkeypatch.setattr(coord, "_sd_ride_not_due", lambda: None, raising=False)
    coord.media_slot_owner = SLOT_SD
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 0
    assert coord._sd_ride_sent_at is None


async def test_a_listing_found_fresh_behind_the_lock_does_not_start_it(monkeypatch):
    coord = _coord(_List([_record(9)]))
    monkeypatch.setattr(coord, "_sd_ride_not_due", lambda: None, raising=False)
    coord.sd_cache = SdCache(fetched_at=time.time())
    assert await coord.async_ride_sd_listing() is False
    assert coord.device_client.listings == 0
    assert coord._sd_ride_sent_at is None


async def test_an_earlier_listing_s_time_is_not_taken_for_this_one():
    coord = _coord(RuntimeError("channel closed"))
    coord._sd_listing_asked_at = time.monotonic()
    assert await coord.async_ride_sd_listing() is False
    assert coord._sd_ride_sent_at is None


async def test_a_listing_that_raises_keeps_the_previous_list():
    coord = _coord(RuntimeError("channel closed"))
    before = coord.sd_cache = _old_cache([_record(9)])
    assert await coord.async_ride_sd_listing() is False
    assert coord.sd_cache is before


async def test_no_session_to_ask_keeps_the_previous_list():
    coord = _coord(None)
    before = coord.sd_cache = _old_cache([_record(9)])
    assert await coord.async_ride_sd_listing() is False
    assert coord.sd_cache is before


async def test_a_listing_cut_off_by_the_viewer_leaving_keeps_the_previous_list():
    # The library reports a channel that died under the request as a list
    # that stops short. Two of three days of it must not replace all seven.
    old = [_record(7), _record(8), _record(9)]
    coord = _coord(_List([_record(9)], complete=False))
    coord.device_client.ends_session = True
    before = coord.sd_cache = _old_cache(old)
    assert await coord.async_ride_sd_listing() is False
    assert coord.sd_cache is before
    assert coord.device_client.thumbnail_asks == 0


async def test_a_cut_off_listing_is_better_than_no_list_at_all():
    r = _record(9)
    coord = _coord(_List([r], complete=False))
    coord.device_client.ends_session = True
    assert await coord.async_ride_sd_listing() is True
    assert coord.sd_cache.records == [r]
    assert coord.sd_cache.complete is False


async def test_a_partial_answer_from_a_connected_camera_is_the_cameras_answer():
    r = _record(9)
    coord = _coord(_List([r], complete=False))
    coord.sd_cache = _old_cache([_record(7), r])
    assert await coord.async_ride_sd_listing() is True
    assert coord.sd_cache.records == [r]
    assert coord.sd_cache.complete is False


async def test_the_button_still_reports_silence_as_silence():
    # keep_on_failure belongs to the ride alone: a person who pressed Refresh
    # and got no answer is told so.
    coord = _coord(_List(answered=False))
    coord.sd_cache = _old_cache([_record(9)])
    assert await coord.async_list_sd_recordings() is True
    assert coord.sd_cache.answered is False


# --- thumbnails ---------------------------------------------------------------


async def test_it_asks_for_thumbnails_as_the_button_does():
    r = _record(9)
    coord = _coord(_List([r]))
    coord.device_client.pictures = {r: b"jpeg"}
    assert await coord.async_ride_sd_listing() is True
    assert coord.sd_cache.thumbnails == {sd_record_key(r): b"jpeg"}


async def test_pictures_already_held_survive_a_listing_that_got_none():
    # The viewer left between the list and its pictures.
    kept, gone = _record(9), _record(7)
    coord = _coord(_List([kept]))
    coord.sd_cache = _old_cache(
        [gone, kept],
        thumbnails={sd_record_key(kept): b"old", sd_record_key(gone): b"gone"},
    )
    assert await coord.async_ride_sd_listing() is True
    # The one still on the card keeps its picture; the deleted one does not.
    assert coord.sd_cache.thumbnails == {sd_record_key(kept): b"old"}


# --- the camera entity: opt-in, battery only ----------------------------------


def _camera(*, is_battery=True, options=None) -> AidotCamera:
    device_client = MagicMock()
    device_client.is_battery_camera = is_battery
    device_client.is_sdes_camera = True
    device_client.cloud_reports_offline = False
    device_client.stream_rtsp_url = None
    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = device_client
    cam.coordinator.config_entry = SimpleNamespace(entry_id="e1", options=options or {})
    cam.coordinator.sd_ride_skip_reason = MagicMock(return_value=None)
    cam.coordinator.async_ride_sd_listing = MagicMock(return_value="ride")
    cam._rtsp_name = "cam"
    cam.hass = MagicMock()
    cam._set_stream_status = MagicMock()
    return cam


def test_it_is_off_by_default():
    assert DEFAULT_SD_LIST_ON_VIEW is False
    cam = _camera()
    cam._list_sd_card_over_this_view()
    cam.coordinator.spawn_entry_task.assert_not_called()


def test_a_battery_view_lists_when_the_option_is_on():
    cam = _camera(options={CONF_SD_LIST_ON_VIEW: True})
    cam._list_sd_card_over_this_view()
    # Entry-bound, so an unload cancels it.
    cam.coordinator.spawn_entry_task.assert_called_once_with(
        "ride", "aidot sd listing over a view"
    )


def test_a_mains_camera_is_left_to_its_pre_warm_listing():
    cam = _camera(is_battery=False, options={CONF_SD_LIST_ON_VIEW: True})
    cam._list_sd_card_over_this_view()
    cam.coordinator.spawn_entry_task.assert_not_called()


def test_a_view_that_finds_nothing_due_makes_no_task():
    cam = _camera(options={CONF_SD_LIST_ON_VIEW: True})
    cam.coordinator.sd_ride_skip_reason.return_value = "the list is recent"
    cam._list_sd_card_over_this_view()
    cam.coordinator.spawn_entry_task.assert_not_called()
    cam.coordinator.async_ride_sd_listing.assert_not_called()


def _view_ready(cam, *, opened=True):
    """Stub everything in _open_and_wait but the decision under test."""
    cam._camera_offline = MagicMock(return_value=False)
    cam._open_live_session = AsyncMock(return_value=opened)
    cam._publish_to_go2rtc = AsyncMock(return_value="http://serve")
    cam._await_serve_listening = AsyncMock(return_value=True)
    cam._go2rtc_publisher_state = AsyncMock(return_value="live")
    cam._serve_port_base = MagicMock(return_value=None)
    cam._entry_options = MagicMock(return_value={})
    cam._list_sd_card_over_this_view = MagicMock()


async def test_a_view_that_opens_a_session_offers_it_to_the_listing():
    cam = _camera()
    _view_ready(cam)
    await cam._open_and_wait(False, "http://serve")
    cam._open_live_session.assert_awaited_once()
    cam._list_sd_card_over_this_view.assert_called_once_with()


async def test_a_view_of_a_running_session_offers_it_too():
    # A second viewer, or the same one again: no open, same session.
    cam = _camera()
    _view_ready(cam)
    cam.coordinator.device_client.stream_rtsp_url = "rtsp://running"
    await cam._open_and_wait(False, "http://serve")
    cam._open_live_session.assert_not_awaited()
    cam._list_sd_card_over_this_view.assert_called_once_with()


async def test_a_view_that_opened_nothing_offers_nothing():
    cam = _camera()
    _view_ready(cam, opened=False)
    assert await cam._open_and_wait(False, "http://serve") is None
    cam._list_sd_card_over_this_view.assert_not_called()


async def test_the_listing_does_not_hold_up_the_view():
    # Backgrounded: the view returns while the listing has not even started.
    cam = _camera(options={CONF_SD_LIST_ON_VIEW: True})
    _view_ready(cam)
    del cam._list_sd_card_over_this_view
    cam._entry_options = lambda: cam.coordinator.config_entry.options
    await cam._open_and_wait(False, "http://serve")
    cam.coordinator.spawn_entry_task.assert_called_once()
    cam.coordinator.async_ride_sd_listing.assert_called_once_with()
