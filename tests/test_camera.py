"""Tests for the AiDot camera entity: serve-port math, connection options, the
stream_source() state machine, the stale-stream eviction watchdog, and the
status-overlay TTL.

These exercise the entity in isolation with a mocked coordinator/device_client
(no live camera, no hass lifecycle), so the intricate stream_source / evict
logic - the code most likely to regress and the hardest to validate on the box -
gets unit coverage.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aidot.camera import AidotCamera, _serve_port
from custom_components.aidot.const import (
    CONF_CONNECTION_MODE,
    CONF_MAINS_IDLE_S,
    CONF_SDES_AUDIO,
    CONNECTION_MODE_LAN_DIRECT,
    DEFAULT_MAINS_IDLE_S,
    DEFAULT_SERVE_PORT_BASE,
)


def _make_camera(
    *,
    is_sdes: bool = False,
    is_battery: bool = False,
    stream_rtsp_url: str | None = None,
    options: dict | None = None,
    sdes_audio_override: bool | None = None,
) -> AidotCamera:
    """Build an AidotCamera backed by a mocked coordinator/device_client."""
    info = SimpleNamespace(
        dev_id="ecf4937b640c0000",
        model_id="A000088.x",
        mac="aa:bb:cc:dd:ee:ff",
        name="Test Cam",
        hw_version="1.0",
    )
    device_client = MagicMock()
    device_client.info = info
    device_client.device_id = info.dev_id
    device_client.is_sdes_camera = is_sdes
    device_client.is_battery_camera = is_battery
    device_client.stream_rtsp_url = stream_rtsp_url
    device_client.start_keepalive = AsyncMock()
    device_client.async_wait_serve_ready = AsyncMock()

    coordinator = MagicMock()
    coordinator.device_client = device_client
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry1", options=options or {}
    )
    coordinator.sdes_audio_override = sdes_audio_override

    cam = AidotCamera(coordinator)
    cam.hass = MagicMock()
    cam.hass.async_add_executor_job = AsyncMock()
    return cam


# --------------------------------------------------------------------------- #
# _serve_port
# --------------------------------------------------------------------------- #
def test_serve_port_is_deterministic_and_in_range():
    p1 = _serve_port("Test Cam")
    p2 = _serve_port("Test Cam")
    assert p1 == p2
    assert DEFAULT_SERVE_PORT_BASE <= p1 < DEFAULT_SERVE_PORT_BASE + 400


def test_serve_port_honors_env_base(monkeypatch):
    monkeypatch.setenv("AIDOT_SERVE_PORT_BASE", "30000")
    assert 30000 <= _serve_port("x") < 30400


# --------------------------------------------------------------------------- #
# _stream_idle_s
# --------------------------------------------------------------------------- #
def test_stream_idle_mains_uses_configured_option():
    cam = _make_camera(is_battery=False, options={CONF_MAINS_IDLE_S: 200})
    assert cam._stream_idle_s() == 200.0


def test_stream_idle_mains_defaults():
    cam = _make_camera(is_battery=False, options={})
    assert cam._stream_idle_s() == float(DEFAULT_MAINS_IDLE_S)


def test_stream_idle_battery_is_none():
    # Battery cameras keep the default idle (don't warm-hold a stream slot).
    cam = _make_camera(is_battery=True)
    assert cam._stream_idle_s() is None


# --------------------------------------------------------------------------- #
# _connect_options
# --------------------------------------------------------------------------- #
def test_connect_options_relay_default_keeps_turn():
    cam = _make_camera(options={})
    opts = cam._connect_options()
    assert opts["fast_connect"] is False
    assert opts["sdes_skip_turn"] is False


def test_connect_options_lan_direct_skips_turn():
    cam = _make_camera(options={CONF_CONNECTION_MODE: CONNECTION_MODE_LAN_DIRECT})
    opts = cam._connect_options()
    assert opts["fast_connect"] is True
    assert opts["sdes_skip_turn"] is True


def test_connect_options_audio_override_beats_global():
    # The per-camera "Camera audio" switch overrides the global SDES-audio option.
    cam = _make_camera(options={CONF_SDES_AUDIO: False}, sdes_audio_override=True)
    assert cam._connect_options()["sdes_audio"] is True


# --------------------------------------------------------------------------- #
# stream_source state machine
# --------------------------------------------------------------------------- #
async def test_stream_source_setup_incomplete_only_publishes_go2rtc():
    # During entity setup (before async_added_to_hass) stream_source must return
    # fast: register go2rtc and return its RTSP URL, never start a keepalive.
    cam = _make_camera()
    assert cam._setup_complete is False
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    assert await cam.stream_source() == "rtsp://go2rtc/x"
    cam.coordinator.device_client.start_keepalive.assert_not_called()


async def test_stream_source_sdes_starts_keepalive_and_returns_url():
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam._await_serve_listening = AsyncMock(return_value=True)
    assert await cam.stream_source() == "rtsp://go2rtc/x"
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()
    cam._await_serve_listening.assert_awaited_once()


async def test_stream_source_dtls_returns_url():
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    assert await cam.stream_source() == "rtsp://go2rtc/x"
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


async def test_stream_source_warm_session_reuses_without_restart():
    # A warm session (stream_rtsp_url set) must not re-start the keepalive.
    cam = _make_camera(is_sdes=False, stream_rtsp_url="rtsp://existing")
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    assert await cam.stream_source() == "rtsp://go2rtc/x"
    cam.coordinator.device_client.start_keepalive.assert_not_called()


async def test_stream_source_falls_back_to_serve_url_when_go2rtc_down():
    cam = _make_camera(is_sdes=False, stream_rtsp_url="rtsp://existing")
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value=None)  # go2rtc unavailable
    assert await cam.stream_source() == cam._serve_url


async def test_stream_source_keepalive_failure_returns_none_and_flags_error():
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    cam.coordinator.device_client.start_keepalive = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    assert await cam.stream_source() is None
    assert cam._stream_status is not None
    assert cam._stream_status[1] is True  # is_error


async def test_stream_source_cancelled_propagates():
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    cam._await_serve_listening = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await cam.stream_source()
    # A clean close clears the status overlay rather than flagging an error.
    assert cam._stream_status is None


# --------------------------------------------------------------------------- #
# _evict_stale_stream watchdog
# --------------------------------------------------------------------------- #
async def test_evict_noop_without_stream():
    cam = _make_camera()
    cam.stream = None
    await cam._evict_stale_stream()  # must not raise


async def test_evict_keeps_stream_while_keepalive_active():
    cam = _make_camera(stream_rtsp_url="rtsp://active")
    stream = MagicMock()
    stream.stop = AsyncMock()
    cam.stream = stream
    await cam._evict_stale_stream()
    stream.stop.assert_not_called()
    assert cam.stream is stream


async def test_evict_drops_stale_stream_when_keepalive_ended():
    cam = _make_camera(stream_rtsp_url=None)
    stream = MagicMock()
    stream.stop = AsyncMock()
    cam.stream = stream
    cam.hass.async_add_executor_job = AsyncMock(return_value=False)  # port free
    await cam._evict_stale_stream()
    stream.stop.assert_awaited_once()
    assert cam.stream is None


# --------------------------------------------------------------------------- #
# status overlay TTL
# --------------------------------------------------------------------------- #
def test_active_status_expires_error_text_after_ttl():
    cam = _make_camera()
    with patch("custom_components.aidot.camera.time.monotonic", return_value=1000.0):
        cam._set_stream_status("oops", error=True)
    later = 1000.0 + cam._STATUS_ERROR_TTL + 1
    with patch("custom_components.aidot.camera.time.monotonic", return_value=later):
        assert cam._active_status() is None


def test_active_status_keeps_fresh_text():
    cam = _make_camera()
    with patch("custom_components.aidot.camera.time.monotonic", return_value=500.0):
        cam._set_stream_status("Connecting...")
        assert cam._active_status() == "Connecting..."


# --------------------------------------------------------------------------- #
# _startup_prewarm
# --------------------------------------------------------------------------- #
async def test_startup_prewarm_warms_mains_camera():
    # A mains camera warms its session in the background (delegates to the
    # idempotent _prewarm_stream). asyncio.sleep is patched so the stagger is a no-op.
    cam = _make_camera(is_battery=False)
    cam._prewarm_stream = AsyncMock()
    with patch("custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()):
        await cam._startup_prewarm()
    cam._prewarm_stream.assert_awaited_once()


async def test_startup_prewarm_skips_battery_camera():
    # Battery cameras must never be warm-held at startup (drains the battery).
    cam = _make_camera(is_battery=True)
    cam._prewarm_stream = AsyncMock()
    with patch("custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()):
        await cam._startup_prewarm()
    cam._prewarm_stream.assert_not_awaited()


async def test_async_added_to_hass_schedules_and_cancels_startup_prewarm():
    """Setup schedules a startup-prewarm task and registers its cancel-on-remove."""
    cam = _make_camera(is_battery=False)
    cam._prefetch_thumbnail = AsyncMock()
    cam.coordinator.add_motion_listener = MagicMock(return_value=lambda: None)

    removed: list = []
    cam.async_on_remove = MagicMock(side_effect=lambda cb: removed.append(cb))

    created: list = []

    def _create_task(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()  # avoid "coroutine was never awaited" warnings
        task = MagicMock(name="task")
        created.append(task)
        return task

    cam.hass.async_create_task = MagicMock(side_effect=_create_task)
    cam.hass.async_create_background_task = MagicMock(side_effect=_create_task)

    with patch(
        "custom_components.aidot.camera.CoordinatorEntity.async_added_to_hass",
        new=AsyncMock(),
    ), patch(
        "custom_components.aidot.camera.async_track_time_interval",
        return_value=lambda: None,
    ):
        await cam.async_added_to_hass()

    # The startup-prewarm task is the last one scheduled during setup.
    assert created, "expected async_create_task to be called during setup"
    startup_task = created[-1]
    startup_task.cancel.assert_not_called()
    # A registered remove callback must cancel it on teardown.
    for cb in removed:
        cb()
    startup_task.cancel.assert_called_once()
    assert cam._setup_complete is True
