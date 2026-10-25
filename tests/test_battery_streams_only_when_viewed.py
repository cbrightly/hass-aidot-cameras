"""Battery cameras stream only while someone is watching.

Over 23 hours on the reference installation there were 41 battery-camera
sessions. 39 of them started within 90 s of that camera's motion event - the
motion pre-warm - and each streamed about 120 s with nobody watching. A manual
view kept the camera streaming 129-159 s after it closed, because the idle
window was the library's 120 s default.

So a battery camera is not pre-warmed on motion, and it is released about 15 s
after its last viewer leaves. Mains cameras are unchanged.
"""

from unittest.mock import AsyncMock

from custom_components.aidot.const import (
    BATTERY_IDLE_S,
    CONF_MAINS_IDLE_S,
    DEFAULT_MAINS_IDLE_S,
)

from .test_camera import _make_camera


def test_battery_idle_window_is_fifteen_seconds():
    assert BATTERY_IDLE_S == 15.0


def test_battery_motion_event_does_not_prewarm():
    cam = _make_camera(is_battery=True, stream_rtsp_url=None)
    cam._on_motion_prewarm({"id": "evt"})
    cam.hass.async_create_task.assert_not_called()
    assert cam._prewarm_task is None
    cam.coordinator.device_client.start_keepalive.assert_not_called()


def test_mains_motion_event_still_prewarms():
    cam = _make_camera(is_battery=False, stream_rtsp_url=None)
    cam.hass.async_create_task = lambda coro: coro.close() or "task"
    cam._on_motion_prewarm({"id": "evt"})
    assert cam._prewarm_task == "task"


async def test_mains_prewarm_starts_the_keepalive():
    cam = _make_camera(is_battery=False, stream_rtsp_url=None)
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    await cam._prewarm_stream()
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


def test_battery_idle_is_fifteen_seconds():
    cam = _make_camera(is_battery=True)
    assert cam._stream_idle_s() == 15.0


def test_mains_idle_is_unchanged():
    assert _make_camera(is_battery=False)._stream_idle_s() == float(
        DEFAULT_MAINS_IDLE_S
    )
    cam = _make_camera(is_battery=False, options={CONF_MAINS_IDLE_S: 200})
    assert cam._stream_idle_s() == 200.0


async def test_battery_view_passes_the_short_idle_window():
    cam = _make_camera(is_battery=True, is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    await cam.stream_source()
    dc = cam.coordinator.device_client
    dc.start_keepalive.assert_awaited_once()
    assert dc.start_keepalive.await_args.kwargs["stream_idle_s"] == 15.0


def test_an_owner_set_aidot_stream_idle_s_wins_for_battery_cameras(monkeypatch):
    """2.36.0 passed 15 s explicitly, which silently overrode the library's
    documented AIDOT_STREAM_IDLE_S for battery cameras."""
    cam = _make_camera(is_battery=True)
    monkeypatch.setenv("AIDOT_STREAM_IDLE_S", "60")
    assert cam._stream_idle_s() is None  # the library reads the 60
    monkeypatch.setenv("AIDOT_STREAM_IDLE_S", "")
    assert cam._stream_idle_s() == 15.0
    monkeypatch.delenv("AIDOT_STREAM_IDLE_S")
    assert cam._stream_idle_s() == 15.0
