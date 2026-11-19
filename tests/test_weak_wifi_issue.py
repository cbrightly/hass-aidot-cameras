"""A camera whose weak Wi-Fi keeps stalling its live view gets a repair issue.

Both halves are needed - a signal that has stayed weak AND views that keep
stalling - and each half has its own hysteresis, so one bad reading or one
slow view never raises it and one good one never clears it.
"""

import asyncio
import inspect
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.aidot import coordinator as coord_mod
from custom_components.aidot import wifi_issue
from custom_components.aidot.const import (
    DOMAIN,
    WIFI_ISSUE_GOOD_OPENS_TO_CLEAR,
    WIFI_ISSUE_OPEN_WINDOW_S,
    WIFI_ISSUE_RSSI_CLEAR_DBM,
    WIFI_ISSUE_RSSI_CLEAR_READINGS,
    WIFI_ISSUE_RSSI_MIN_SPACING_S,
    WIFI_ISSUE_RSSI_RAISE_DBM,
    WIFI_ISSUE_RSSI_WINDOW_S,
    WIFI_ISSUE_SLOW_OPEN_S,
    WIFI_ISSUE_STALLED_OPENS,
)
from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator

from . import test_cold_view_waits_for_publisher as cold

WEAK = WIFI_ISSUE_RSSI_RAISE_DBM - 5
STRONG = WIFI_ISSUE_RSSI_CLEAR_DBM + 5
BETWEEN = WIFI_ISSUE_RSSI_RAISE_DBM + 1
POLL_S = 300.0


def _weak_for_a_window(tracker, start=0.0, rssi=WEAK):
    """Poll a weak signal every five minutes across one whole window."""
    now = start
    while now - start <= WIFI_ISSUE_RSSI_WINDOW_S:
        tracker.note_rssi(rssi, now)
        now += POLL_S
    return now - POLL_S


def _stall(tracker, now, count=WIFI_ISSUE_STALLED_OPENS):
    for i in range(count):
        tracker.note_open(True, now + i)
    return now + count


def _raised_tracker():
    tracker = wifi_issue.WeakWifiTracker()
    now = _weak_for_a_window(tracker)
    now = _stall(tracker, now)
    assert tracker.evaluate(now) is True
    return tracker, now


# --------------------------------------------------------------------------- #
# What one view says
# --------------------------------------------------------------------------- #
def test_a_view_that_waited_past_the_slow_line_is_a_stall_attached_or_not():
    assert wifi_issue.open_outcome(WIFI_ISSUE_SLOW_OPEN_S, True) is True
    assert wifi_issue.open_outcome(WIFI_ISSUE_SLOW_OPEN_S + 40, False) is True


def test_a_prompt_view_is_a_good_one():
    assert wifi_issue.open_outcome(8.0, True) is False


def test_a_short_wait_that_gave_up_says_nothing():
    """The HLS budget, a mains budget, an unreadable go2rtc: none waited long
    enough to tell a slow open from a normal one."""
    assert wifi_issue.open_outcome(8.5, False) is None
    assert wifi_issue.open_outcome(0.0, False) is None


# --------------------------------------------------------------------------- #
# Raising
# --------------------------------------------------------------------------- #
def test_a_weak_signal_and_stalled_views_raise_the_issue():
    tracker, _now = _raised_tracker()
    assert tracker.raised is True
    assert tracker.last_rssi == WEAK
    assert tracker.stalled_opens() == WIFI_ISSUE_STALLED_OPENS


def test_a_weak_signal_alone_never_raises():
    tracker = wifi_issue.WeakWifiTracker()
    now = _weak_for_a_window(tracker)
    tracker.note_open(False, now)
    assert tracker.evaluate(now + 1) is False


def test_stalled_views_on_a_good_signal_never_raise():
    tracker = wifi_issue.WeakWifiTracker()
    now = _weak_for_a_window(tracker, rssi=STRONG)
    now = _stall(tracker, now, count=10)
    assert tracker.evaluate(now) is False


def test_one_weak_reading_is_not_a_sustained_signal():
    tracker = wifi_issue.WeakWifiTracker()
    tracker.note_rssi(WEAK, 0.0)
    now = _stall(tracker, 1.0)
    assert tracker.evaluate(now) is False
    # Not even once the window has passed with no further reading.
    assert tracker.evaluate(WIFI_ISSUE_RSSI_WINDOW_S + 10) is False


def test_a_burst_of_weak_readings_does_not_stand_in_for_the_window():
    tracker = wifi_issue.WeakWifiTracker()
    for i in range(10):
        tracker.note_rssi(WEAK, float(i))
    now = _stall(tracker, 20.0)
    assert tracker.evaluate(now) is False


def test_a_reading_above_the_line_restarts_the_weak_window():
    tracker = wifi_issue.WeakWifiTracker()
    now = _weak_for_a_window(tracker)
    tracker.note_rssi(BETWEEN, now + POLL_S)
    tracker.note_rssi(WEAK, now + 2 * POLL_S)
    after = _stall(tracker, now + 2 * POLL_S + 1)
    assert tracker.evaluate(after) is False


def test_fewer_stalls_than_the_count_do_not_raise():
    tracker = wifi_issue.WeakWifiTracker()
    now = _weak_for_a_window(tracker)
    now = _stall(tracker, now, count=WIFI_ISSUE_STALLED_OPENS - 1)
    assert tracker.evaluate(now) is False


def test_stalls_older_than_the_window_do_not_count():
    tracker = wifi_issue.WeakWifiTracker()
    _stall(tracker, 0.0, count=WIFI_ISSUE_STALLED_OPENS - 1)
    start = WIFI_ISSUE_OPEN_WINDOW_S
    now = _weak_for_a_window(tracker, start=start)
    tracker.note_open(True, now)
    assert tracker.evaluate(now + 1) is False


def test_recent_good_views_hold_the_issue_off():
    tracker = wifi_issue.WeakWifiTracker()
    now = _weak_for_a_window(tracker)
    now = _stall(tracker, now)
    for i in range(WIFI_ISSUE_GOOD_OPENS_TO_CLEAR):
        tracker.note_open(False, now + i)
    assert tracker.evaluate(now + 10) is False


@pytest.mark.parametrize("reading", [None, 0, 55, "n/a", True])
def test_a_reading_that_is_not_dbm_is_no_reading(reading):
    tracker = wifi_issue.WeakWifiTracker()
    tracker.note_rssi(reading, 0.0)
    assert tracker.last_rssi is None
    # ... and it neither breaks a weak run nor counts toward clearing.
    tracker, now = _raised_tracker()
    for i in range(5):
        tracker.note_rssi(reading, now + i)
    assert tracker.evaluate(now + 10) is True


# --------------------------------------------------------------------------- #
# Clearing
# --------------------------------------------------------------------------- #
def test_one_good_reading_does_not_clear():
    tracker, now = _raised_tracker()
    tracker.note_rssi(STRONG, now + 1)
    assert tracker.evaluate(now + 2) is True


def test_a_signal_that_recovers_clears_the_issue():
    tracker, now = _raised_tracker()
    for i in range(WIFI_ISSUE_RSSI_CLEAR_READINGS):
        tracker.note_rssi(STRONG, now + 1 + i)
    assert tracker.evaluate(now + 10) is False


def test_a_signal_between_the_thresholds_holds_the_issue():
    tracker, now = _raised_tracker()
    for i in range(10):
        tracker.note_rssi(BETWEEN, now + 1 + i)
    assert tracker.evaluate(now + 20) is True


def test_good_readings_must_be_in_a_row_to_clear():
    tracker, now = _raised_tracker()
    for i in range(WIFI_ISSUE_RSSI_CLEAR_READINGS * 3):
        tracker.note_rssi(STRONG if i % 2 == 0 else BETWEEN, now + 1 + i)
    assert tracker.evaluate(now + 20) is True


def test_one_good_view_does_not_clear():
    tracker, now = _raised_tracker()
    tracker.note_open(False, now + 1)
    assert tracker.evaluate(now + 2) is True


def test_views_that_start_promptly_again_clear_the_issue():
    tracker, now = _raised_tracker()
    for i in range(WIFI_ISSUE_GOOD_OPENS_TO_CLEAR):
        tracker.note_open(False, now + 1 + i)
    assert tracker.evaluate(now + 10) is False


def test_one_stall_after_clearing_does_not_raise_it_again():
    tracker, now = _raised_tracker()
    for i in range(WIFI_ISSUE_GOOD_OPENS_TO_CLEAR):
        tracker.note_open(False, now + 1 + i)
    assert tracker.evaluate(now + 10) is False
    tracker.note_rssi(WEAK, now + 11)
    tracker.note_open(True, now + 12)
    assert tracker.evaluate(now + 13) is False
    # A full count of new stalls does.
    _stall(tracker, now + 14, count=WIFI_ISSUE_STALLED_OPENS - 1)
    assert tracker.evaluate(now + 20) is True


def test_stalls_that_age_out_clear_the_issue():
    tracker, now = _raised_tracker()
    assert tracker.evaluate(now + WIFI_ISSUE_OPEN_WINDOW_S - 10) is True
    assert tracker.evaluate(now + WIFI_ISSUE_OPEN_WINDOW_S + 10) is False


def test_an_unavailable_camera_has_no_issue_and_starts_the_window_again():
    tracker, now = _raised_tracker()
    tracker.note_unavailable()
    assert tracker.evaluate(now + 1) is False
    # Back, still weak, stalls still in the window: one reading is not enough.
    tracker.note_rssi(WEAK, now + 2)
    assert tracker.evaluate(now + 3) is False
    later = _weak_for_a_window(tracker, start=now + 4)
    assert tracker.evaluate(later) is True


# --------------------------------------------------------------------------- #
# The registry call
# --------------------------------------------------------------------------- #
def test_the_issue_names_the_camera_and_states_the_signal():
    hass = MagicMock()
    with (
        patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        wifi_issue.note_weak_wifi(
            hass, "dev1", name="Porch cam", rssi=-82, stalled_opens=4, raise_it=True
        )
        create.assert_called_once()
        args, kwargs = create.call_args
        assert args[:3] == (hass, DOMAIN, "weak_wifi_dev1")
        assert kwargs["translation_key"] == "weak_wifi"
        assert kwargs["is_fixable"] is False
        assert kwargs["is_persistent"] is False
        assert kwargs["severity"].value == "warning"
        placeholders = kwargs["translation_placeholders"]
        assert placeholders["name"] == "Porch cam"
        assert placeholders["rssi"] == "-82"
        assert placeholders["threshold"] == str(WIFI_ISSUE_RSSI_RAISE_DBM)
        assert placeholders["stalls"] == "4"
        assert all(isinstance(v, str) for v in placeholders.values())
        wifi_issue.note_weak_wifi(
            hass, "dev1", name="Porch cam", rssi=-60, stalled_opens=0, raise_it=False
        )
        delete.assert_called_once_with(hass, DOMAIN, "weak_wifi_dev1")


def test_every_placeholder_the_text_uses_is_supplied():
    import json
    import pathlib
    import re

    root = pathlib.Path(wifi_issue.__file__).parent
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as create:
        wifi_issue.note_weak_wifi(
            MagicMock(), "d", name="n", rssi=-80, stalled_opens=3, raise_it=True
        )
    supplied = set(create.call_args.kwargs["translation_placeholders"])
    for rel in ("strings.json", "translations/en.json"):
        text = json.loads((root / rel).read_text())["issues"]["weak_wifi"]
        used = set(re.findall(r"\{(\w+)\}", text["title"] + text["description"]))
        assert used <= supplied, rel
        assert {"name", "rssi"} <= used, rel


def test_a_registry_failure_never_propagates():
    with patch(
        "homeassistant.helpers.issue_registry.async_create_issue",
        side_effect=RuntimeError("registry not loaded"),
    ):
        wifi_issue.note_weak_wifi(
            MagicMock(), "d", name="n", rssi=-80, stalled_opens=3, raise_it=True
        )
    with patch(
        "homeassistant.helpers.issue_registry.async_delete_issue",
        side_effect=RuntimeError("registry not loaded"),
    ):
        wifi_issue.clear_for(MagicMock(), ["d"])


def test_clear_for_deletes_each_cameras_issue():
    hass = MagicMock()
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete:
        wifi_issue.clear_for(hass, ["a", "b"])
    assert [c.args for c in delete.call_args_list] == [
        (hass, DOMAIN, "weak_wifi_a"),
        (hass, DOMAIN, "weak_wifi_b"),
    ]


# --------------------------------------------------------------------------- #
# The coordinator feeds it
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    time = staticmethod(time.time)


class _Manager:
    def __init__(self, device):
        self.device = device

    async def async_get_camera_device(self, device_id):
        return self.device


def _coordinator(rssi=WEAK, online=True, powered=None, battery=False):
    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    status = SimpleNamespace(wifi_rssi=rssi, online=online, powered=powered)
    c.device_client = SimpleNamespace(
        device_id="cam1",
        info=SimpleNamespace(name="Porch cam"),
        status=status,
        cloud_reports_offline=not online,
        is_battery_camera=battery,
        has_live_session=False,
        update_status_from_device=lambda device: status,
    )
    c.hass = MagicMock()
    c._manager = _Manager({"id": "cam1"})
    c._consecutive_failures = 0
    # What __init__ sets up for the issue.
    c._weak_wifi = wifi_issue.WeakWifiTracker()
    c._weak_wifi_shown = None
    c._wifi_counted_at = None
    c._wifi_counted_rssi = None
    c._wifi_awake_seen = False
    c._motion_listeners = []
    return c


@pytest.fixture
def clock():
    fake = _Clock()
    with patch.object(coord_mod, "time", fake):
        yield fake


@pytest.fixture
def registry():
    with (
        patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        yield SimpleNamespace(create=create, delete=delete)


def _poll_weak_for_a_window(c, clock):
    start = clock.now
    while clock.now - start <= WIFI_ISSUE_RSSI_WINDOW_S:
        c._note_wifi_reading()
        clock.now += POLL_S


def _stalled_views(c, clock, count=WIFI_ISSUE_STALLED_OPENS):
    for _ in range(count):
        c.note_stream_open(WIFI_ISSUE_SLOW_OPEN_S + 20, True)
        clock.now += 60


def test_polls_and_stalled_views_raise_the_issue_once(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    registry.create.assert_not_called()
    _stalled_views(c, clock)
    registry.create.assert_called_once()
    args, kwargs = registry.create.call_args
    assert args[1:3] == (DOMAIN, "weak_wifi_cam1")
    assert kwargs["translation_placeholders"]["name"] == "Porch cam"
    assert kwargs["translation_placeholders"]["rssi"] == str(WEAK)
    # A poll with the same figures does not write the registry again.
    c._note_wifi_reading()
    registry.create.assert_called_once()
    registry.delete.assert_not_called()


def test_a_changed_signal_refreshes_the_figure_in_the_issue(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    _stalled_views(c, clock)
    c.device_client.status.wifi_rssi = WEAK - 3
    c._note_wifi_reading()
    assert registry.create.call_count == 2
    placeholders = registry.create.call_args.kwargs["translation_placeholders"]
    assert placeholders["rssi"] == str(WEAK - 3)


def test_short_waits_that_gave_up_are_not_counted(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    for _ in range(10):
        c.note_stream_open(8.5, False)
    registry.create.assert_not_called()
    assert c._weak_wifi.stalled_opens() == 0


def test_a_recovered_signal_deletes_the_issue(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    _stalled_views(c, clock)
    c.device_client.status.wifi_rssi = STRONG
    c._note_wifi_reading()
    registry.delete.assert_not_called()
    clock.now += POLL_S
    c._note_wifi_reading()
    registry.delete.assert_called_once_with(c.hass, DOMAIN, "weak_wifi_cam1")


def test_prompt_views_delete_the_issue(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    _stalled_views(c, clock)
    for _ in range(WIFI_ISSUE_GOOD_OPENS_TO_CLEAR):
        c.note_stream_open(7.0, True)
    registry.delete.assert_called_once_with(c.hass, DOMAIN, "weak_wifi_cam1")


def test_a_camera_that_goes_offline_loses_its_issue(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    _stalled_views(c, clock)
    c.device_client.cloud_reports_offline = True
    c._note_wifi_reading()
    registry.delete.assert_called_once_with(c.hass, DOMAIN, "weak_wifi_cam1")


def test_a_camera_switched_off_loses_its_issue(clock, registry):
    c = _coordinator()
    _poll_weak_for_a_window(c, clock)
    _stalled_views(c, clock)
    c.device_client.status.powered = False
    c._note_wifi_reading()
    registry.delete.assert_called_once_with(c.hass, DOMAIN, "weak_wifi_cam1")


@pytest.mark.parametrize("state", [{"online": False}, {"powered": False}])
def test_an_offline_or_switched_off_camera_never_gets_the_issue(clock, registry, state):
    c = _coordinator(**state)
    _poll_weak_for_a_window(c, clock)
    _stalled_views(c, clock)
    c._note_wifi_reading()
    registry.create.assert_not_called()


async def test_a_refresh_feeds_the_reading():
    c = _coordinator(rssi=-81)
    assert await c._async_update_data() is c.device_client.status
    assert c._weak_wifi.last_rssi == -81


async def test_a_broken_tracker_never_fails_a_refresh_or_a_view():
    c = _coordinator()
    c._weak_wifi = SimpleNamespace()  # nothing the hooks need
    assert await c._async_update_data() is c.device_client.status
    c.note_stream_open(60.0, False)


def test_the_issue_state_is_made_by_the_constructor():
    """Not on first use: every reader would have to allow for it missing."""
    source = inspect.getsource(AidotCameraUpdateCoordinator.__init__)
    for name in ("_weak_wifi", "_weak_wifi_shown"):
        assert f"self.{name}" in source
    assert coord_mod.wifi_issue is wifi_issue
    body = inspect.getsource(AidotCameraUpdateCoordinator)
    assert "import wifi_issue" not in body
    assert 'getattr(self, "_weak_wifi' not in body


# --------------------------------------------------------------------------- #
# What counts as a reading
# --------------------------------------------------------------------------- #
def _readings(c):
    return c._weak_wifi._weak_readings


def test_refreshes_closer_than_the_poll_are_one_reading(clock, registry):
    """The cloud's value has no time on it, so a forced refresh re-reads the
    measurement the last poll counted."""
    c = _coordinator()
    c._note_wifi_reading()
    for _ in range(5):
        clock.now += 10
        c._note_wifi_reading()
    assert _readings(c) == 1
    clock.now += WIFI_ISSUE_RSSI_MIN_SPACING_S
    c._note_wifi_reading()
    assert _readings(c) == 2


def test_a_burst_of_refreshes_cannot_raise_the_issue(clock, registry):
    c = _coordinator()
    _stalled_views(c, clock)
    start = clock.now
    while clock.now - start <= WIFI_ISSUE_RSSI_WINDOW_S + POLL_S:
        c._note_wifi_reading()
        clock.now += WIFI_ISSUE_RSSI_WINDOW_S
    # Two readings a window apart: the time is there, the readings are not.
    assert _readings(c) == 2
    registry.create.assert_not_called()


def test_a_sleeping_battery_cameras_unchanged_value_is_not_a_new_reading(
    clock, registry
):
    """It reports only while awake; the same number an hour on is the same
    measurement."""
    c = _coordinator(battery=True)
    for _ in range(12):
        c._note_wifi_reading()
        clock.now += POLL_S
    assert _readings(c) == 1


@pytest.mark.parametrize("woke", ["view", "event", "session", "value"])
def test_a_battery_camera_that_was_awake_gives_a_new_reading(clock, registry, woke):
    c = _coordinator(battery=True)
    c._note_wifi_reading()
    clock.now += POLL_S
    if woke == "view":
        c.note_stream_open(8.0, True)
    elif woke == "event":
        c._handle_motion_event({"eventCode": "1"})
    elif woke == "session":
        c.device_client.has_live_session = True
    else:
        c.device_client.status.wifi_rssi = WEAK - 2
    c._note_wifi_reading()
    assert _readings(c) == 2
    # Counted once: the next poll needs it to have been awake again.
    c.device_client.has_live_session = False
    clock.now += POLL_S
    c._note_wifi_reading()
    assert _readings(c) == 2


def test_a_view_that_never_started_is_not_the_camera_being_awake(clock, registry):
    c = _coordinator(battery=True)
    c._note_wifi_reading()
    clock.now += POLL_S
    c.note_stream_open(WIFI_ISSUE_SLOW_OPEN_S + 30, False)
    c._note_wifi_reading()
    assert _readings(c) == 1


def test_a_camera_back_from_offline_is_read_at_once(clock, registry):
    c = _coordinator()
    c._note_wifi_reading()
    clock.now += 10
    c.device_client.cloud_reports_offline = True
    c._note_wifi_reading()
    clock.now += 10
    c.device_client.cloud_reports_offline = False
    c._note_wifi_reading()
    assert _readings(c) == 1  # the run restarted, and this reading began it


def test_a_removed_camera_loses_its_issue():
    mgr = coord_mod.AidotDeviceManagerCoordinator.__new__(
        coord_mod.AidotDeviceManagerCoordinator
    )
    mgr.hass = MagicMock()
    mgr.config_entry = MagicMock()
    mgr._purge_deleted_entries = MagicMock()
    gone = MagicMock()
    coords = {"cam1": gone}
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete:
        mgr._sync_coordinators(coords, {}, is_camera=True)
    assert coords == {}
    # Its other per-camera issue (the DTLS certificate one) goes with it.
    assert sorted(c.args for c in delete.call_args_list) == [
        (mgr.hass, DOMAIN, "dtls_pin_mismatch_cam1"),
        (mgr.hass, DOMAIN, "weak_wifi_cam1"),
    ]


# --------------------------------------------------------------------------- #
# The camera entity reports its cold opens
# --------------------------------------------------------------------------- #
async def test_a_cold_view_reports_how_long_the_publisher_took():
    cam = cold._make_camera(stream_rtsp_url=None)
    client = cold._client(
        [cold._streams([cold.PLACEHOLDER]), cold._streams([cold.PUBLISHER])]
    )
    p = cold._patches(client)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        await cam.stream_source()
    cam.coordinator.note_stream_open.assert_called_once()
    elapsed, attached = cam.coordinator.note_stream_open.call_args.args
    assert attached is True
    assert 0.0 <= elapsed < 5.0


async def test_a_cold_view_whose_publisher_never_came_reports_the_wait():
    cam = cold._make_camera(stream_rtsp_url=None)
    client = cold._client([cold._streams([cold.PLACEHOLDER])])
    p = cold._patches(client, wait=1.6, poll=0.01)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        await cam.stream_source()
    cam.coordinator.note_stream_open.assert_called_once()
    elapsed, attached = cam.coordinator.note_stream_open.call_args.args
    assert attached is False
    assert elapsed >= 0.05


async def test_the_time_spent_starting_the_session_is_not_the_cameras_wait():
    """Opens are let through one at a time, so that part of a cold view is
    mostly time queued behind the other cameras, not this camera's Wi-Fi."""
    cam = cold._make_camera(stream_rtsp_url=None)
    client = cold._client(
        [cold._streams([cold.PLACEHOLDER]), cold._streams([cold.PUBLISHER])]
    )

    async def _queued_open(dc, serve_url, register=False):
        await asyncio.sleep(0.4)
        return True

    cam._open_live_session = _queued_open
    p = cold._patches(client)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        await cam.stream_source()
    elapsed, attached = cam.coordinator.note_stream_open.call_args.args
    assert attached is True
    assert elapsed < 0.3


async def test_a_warm_view_reports_nothing():
    cam = cold._make_camera(stream_rtsp_url="rtsp://127.0.0.1:8554/live")
    client = cold._client([cold._streams([cold.PLACEHOLDER, cold.PUBLISHER])])
    p = cold._patches(client)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        await cam.stream_source()
    cam.coordinator.note_stream_open.assert_not_called()


async def test_a_running_session_go2rtc_lost_is_not_a_cold_open():
    """The session was already ours; the wait is not how long it took to start."""
    cam = cold._make_camera(stream_rtsp_url="rtsp://127.0.0.1:8554/live")
    client = cold._client(
        [cold._streams([cold.PLACEHOLDER]), cold._streams([cold.PUBLISHER])]
    )
    p = cold._patches(client)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        await cam.stream_source()
    cam.coordinator.note_stream_open.assert_not_called()
