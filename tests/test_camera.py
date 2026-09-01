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

from custom_components.aidot import camera as camera_mod

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
        dev_id="eeee4444ffff5555",
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
    # 0 means never release: mains cameras stay warm so a live view is instant.
    assert cam._stream_idle_s() == float(DEFAULT_MAINS_IDLE_S)
    assert DEFAULT_MAINS_IDLE_S == 0


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


def test_connect_options_carry_the_library_connection_mode():
    """Every mode maps onto the library's sdes_connection_mode knob.

    The stored option values keep their historical names (relay/lan_direct)
    for entry compatibility; the library speaks auto/lan/relay. relay_only is
    the one new value - it forces the cloud relay, which needs the
    pre-allocation kept, so it must not also set the skip levers.
    """
    cam = _make_camera(options={})
    assert cam._connect_options()["sdes_connection_mode"] == "auto"

    cam = _make_camera(options={CONF_CONNECTION_MODE: CONNECTION_MODE_LAN_DIRECT})
    assert cam._connect_options()["sdes_connection_mode"] == "lan"


def test_there_is_no_relay_only_option():
    """Measured 2026-08-24: even with c=/m= at the relay allocation and the
    WAN permission pre-installed, both camera families dialed our host
    address directly (they learn it from our own ICE probes). An option that
    says relay-only while sessions run direct is a control that lies - the
    same standard that removed the resolution select in 2.11.9."""
    from custom_components.aidot.const import CONNECTION_MODES

    assert "relay_only" not in CONNECTION_MODES


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


async def test_stream_source_sdes_push_publishes_and_returns_go2rtc_url():
    # Default (sdes_push on): keepalive publishes to go2rtc's RTSP ingest and
    # stream_source hands HA that same URL; no pull registration, no local
    # -listen port probe.
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam._await_serve_listening = AsyncMock(return_value=True)
    url = await cam.stream_source()
    assert url is not None and url.startswith("rtsp://") and cam._go2rtc_name in url
    dc = cam.coordinator.device_client
    dc.start_keepalive.assert_awaited_once()
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url
    # go2rtc needs a source to create the stream, so the legacy serve URL is
    # registered as an inert placeholder even in push mode.
    cam._publish_to_go2rtc.assert_awaited_once_with(cam._serve_url)
    cam._await_serve_listening.assert_not_awaited()  # no local port in push mode


async def test_stream_source_sdes_push_returns_url_even_without_go2rtc():
    # This used to assert None, on the reasoning that a down go2rtc should
    # soft-fail to a still image rather than hand out a dead URL. That reasoning
    # was wrong and the None was actively harmful: HA's go2rtc provider calls
    # teardown() on a falsy stream_source, which closes EVERY camera's session,
    # so one camera failing to open blanked the whole dashboard. A URL that may
    # 404 keeps the failure local to this camera.
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    cam._await_serve_listening = AsyncMock(return_value=True)
    url = await cam.stream_source()
    assert url is not None
    assert url.startswith("rtsp://")  # a scheme go2rtc supports


async def test_stored_sdes_push_off_is_ignored_not_honoured():
    # 2.19.0 took the toggle off the options page but kept reading the key, so
    # an entry that turned it off in 2.18.x was pinned to the pull serve - the
    # mode that jams under HA - with no UI left to undo it. The stored value is
    # now ignored and every SDES camera gets the mode that works.
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None,
                       options={"sdes_push": False})
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam._await_serve_listening = AsyncMock(return_value=True)
    assert cam._sdes_push_enabled() is True
    # Push mode hands back the go2rtc publish URL, not the local serve, and does
    # not wait on the local -listen socket (nothing binds it in push mode).
    assert await cam.stream_source() == cam._push_serve_url
    cam._await_serve_listening.assert_not_awaited()


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


async def test_stream_source_keepalive_failure_flags_error_but_keeps_a_url():
    # A failed keepalive still must not answer None - that tears down every
    # other camera's session (see _soft_fail_url). The error is surfaced through
    # the status overlay instead, which is local to this entity.
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    cam.coordinator.device_client.start_keepalive = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    assert await cam.stream_source() == cam._serve_url
    assert cam._stream_status is not None
    assert cam._stream_status[1] is True  # is_error


async def test_stream_source_cancelled_propagates():
    # The SDES pull branch is now reached only with go2rtc disabled - the
    # sdes_push option can no longer put an SDES camera there.
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    cam._await_serve_listening = AsyncMock(side_effect=asyncio.CancelledError)
    with patch.object(camera_mod, "_GO2RTC_ENABLED", False), \
            pytest.raises(asyncio.CancelledError):
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
async def test_startup_prewarm_is_on_by_default_for_mains():
    # Mains cameras are kept warm: a cold wake measures 16-22s, which reads as a
    # broken live view, and a mains camera has no battery to protect. Battery
    # cameras are still excluded (see the test below).
    cam = _make_camera(is_battery=False)
    cam._prewarm_stream = AsyncMock()
    with patch("custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()):
        await cam._startup_prewarm()
    cam._prewarm_stream.assert_awaited_once()


async def test_startup_prewarm_can_be_turned_off():
    cam = _make_camera(is_battery=False, options={"startup_prewarm": False})
    cam._prewarm_stream = AsyncMock()
    with patch("custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()):
        await cam._startup_prewarm()
    cam._prewarm_stream.assert_not_awaited()


async def test_startup_prewarm_warms_mains_camera_when_enabled():
    # Explicitly enabled behaves the same as the default.
    cam = _make_camera(is_battery=False, options={"startup_prewarm": True})
    cam._prewarm_stream = AsyncMock()
    with patch("custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()):
        await cam._startup_prewarm()
    cam._prewarm_stream.assert_awaited_once()


async def test_startup_prewarm_skips_an_offline_camera_even_when_enabled():
    # Opening a session against an offline camera cannot succeed; the keepalive
    # parks in its retry pause with streaming latched on, which keeps a renew
    # POST and a wake probe going at a camera nobody can view.
    cam = _make_camera(is_battery=False, options={"startup_prewarm": True})
    cam.coordinator.device_client.status = SimpleNamespace(online=False)
    cam._prewarm_stream = AsyncMock()
    with patch("custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()):
        await cam._startup_prewarm()
    cam._prewarm_stream.assert_not_awaited()


async def test_startup_prewarm_skips_battery_camera():
    # Battery cameras must never be warm-held at startup (drains the battery).
    cam = _make_camera(is_battery=True, options={"startup_prewarm": True})
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


# --------------------------------------------------------------------------- #
# On-device listing piggybacks a session opened for something else
# --------------------------------------------------------------------------- #
def _listens(cam):
    """Make backgrounded work actually run, and record the piggybacks."""
    called = []

    async def _piggyback():
        called.append(1)

    cam.coordinator.async_piggyback_sd_refresh = _piggyback
    cam.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    return called


async def test_a_successful_prewarm_lists_the_card_in_the_background():
    cam = _make_camera()
    called = _listens(cam)
    await cam._prewarm_stream()
    await asyncio.sleep(0)
    assert called == [1]


async def test_the_listing_does_not_hold_up_the_stream():
    # Backgrounded, not awaited: a listing must never be able to delay or fail
    # a stream a viewer is waiting on.
    cam = _make_camera()
    started = []

    async def _slow():
        started.append("begin")
        await asyncio.sleep(10)

    cam.coordinator.async_piggyback_sd_refresh = _slow
    tasks = []
    cam.hass.async_create_task = lambda coro: tasks.append(
        asyncio.ensure_future(coro))
    await cam._prewarm_stream()
    assert started == [], "_prewarm_stream returned before the listing ran"
    for task in tasks:
        task.cancel()


async def test_a_failed_prewarm_does_not_list():
    # There is no session to list through, and asking anyway would spend the
    # coordinator's silence budget on a failure that says nothing about the
    # camera's card.
    cam = _make_camera()
    called = _listens(cam)
    cam.coordinator.device_client.start_keepalive = AsyncMock(
        side_effect=RuntimeError("no"))
    await cam._prewarm_stream()
    await asyncio.sleep(0)
    assert called == []


async def test_an_offline_camera_does_not_list():
    cam = _make_camera()
    called = _listens(cam)
    cam.coordinator.device_client.status = SimpleNamespace(online=False)
    await cam._prewarm_stream()
    await asyncio.sleep(0)
    assert called == []


# --------------------------------------------------------------------------- #
# prewarm creates the stream definition the publish needs
# --------------------------------------------------------------------------- #
async def test_push_prewarm_creates_the_stream_definition():
    # go2rtc will not hold a source-less stream and rejects an RTSP publish to a
    # name it does not know, so the definition has to exist before the keepalive
    # publishes. Setup used to create it for every camera; a push camera no
    # longer registers there (that left an unviewed camera holding a producer
    # nothing could dial), so the prewarm path creates it itself.
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")

    await cam._prewarm_stream()

    cam._publish_to_go2rtc.assert_awaited_once_with(cam._serve_url)
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


async def test_push_prewarm_does_not_re_register_over_a_session():
    # PUT /api/streams replaces the definition wholesale and would drop a live
    # publisher, so a camera that already has a session is left alone.
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/live")
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")

    await cam._prewarm_stream()

    cam._publish_to_go2rtc.assert_not_awaited()


async def test_pull_prewarm_registers_nothing():
    # The pull path registered its serve at setup and that registration is
    # correct there (the port really does listen), so prewarm has nothing to do.
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")

    await cam._prewarm_stream()

    cam._publish_to_go2rtc.assert_not_awaited()
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()
