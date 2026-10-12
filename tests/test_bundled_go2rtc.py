"""With only Home Assistant's bundled go2rtc, every camera publishes into it.

Every go2rtc call in camera.py goes through the resolved target
(go2rtc_target.resolve_go2rtc_target): an explicit AIDOT_GO2RTC_API, else a
go2rtc answering on 127.0.0.1:1984, else Home Assistant's own go2rtc. These
tests pin what each answer does to a camera, including the reference box's
add-on answer, which must keep the URLs it has always had.
"""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

from custom_components.aidot import camera as camera_mod
from custom_components.aidot import coordinator as coordinator_module
from custom_components.aidot import go2rtc_target as gt
from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator

from .test_camera import _make_camera

_SESSION = object()
BUNDLED = gt.Go2rtcTarget(
    "http://localhost:11984/", _SESSION, "127.0.0.1", 18554, gt.KIND_BUNDLED
)
ADDON = gt.Go2rtcTarget(
    "http://127.0.0.1:1984", _SESSION, "127.0.0.1", 8554, gt.KIND_ADDON
)


def _resolves_to(target):
    return patch.object(
        camera_mod, "resolve_go2rtc_target", AsyncMock(return_value=target)
    )


def _publishes(monkeypatch):
    monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: True)


async def test_a_dtls_camera_publishes_to_the_bundled_server(monkeypatch):
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    with _resolves_to(BUNDLED):
        assert await cam._push_mode() is True
        assert (
            await cam._push_serve_url() == "rtsp://127.0.0.1:18554/" + cam._go2rtc_name
        )


async def test_an_sdes_camera_publishes_to_the_bundled_server(monkeypatch):
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    with _resolves_to(BUNDLED):
        url = await cam.stream_source()
    assert url == "rtsp://127.0.0.1:18554/" + cam._go2rtc_name
    dc = cam.coordinator.device_client
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url


async def test_an_sdes_camera_with_no_go2rtc_gets_no_dead_url(caplog):
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    with _resolves_to(None):
        assert await cam._push_mode() is False
        assert await cam.stream_source() is None  # setup-time answer
        cam._setup_complete = True
        assert await cam.stream_source() is None  # view-time answer
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()
    assert "no go2rtc answered" in caplog.text


async def test_the_no_go2rtc_warning_is_logged_once_per_camera(caplog):
    caplog.set_level(logging.WARNING)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    with _resolves_to(None):
        for _ in range(3):
            assert await cam.stream_source() is None
    warnings = [r for r in caplog.records if "no go2rtc answered" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


async def test_an_sdes_prewarm_with_no_go2rtc_opens_nothing():
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    with _resolves_to(None):
        await cam._prewarm_stream()
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


async def test_a_dtls_camera_with_no_go2rtc_keeps_its_pull_serve(monkeypatch):
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    with _resolves_to(None):
        assert await cam._push_mode() is False
        url = await cam.stream_source()
    assert url == cam._serve_url and url.startswith("http://")


async def test_go2rtc_disabled_never_resolves(monkeypatch):
    """AIDOT_GO2RTC=0 still means "never go2rtc": nothing is even asked."""
    _publishes(monkeypatch)
    monkeypatch.setattr(camera_mod, "_GO2RTC_ENABLED", False)
    resolver = AsyncMock(return_value=ADDON)
    with patch.object(camera_mod, "resolve_go2rtc_target", resolver):
        cam = _make_camera(is_sdes=False)
        assert await cam._go2rtc_reachable() is False
        assert await cam._publish_to_go2rtc(cam._serve_url) is None
        await cam._unpublish_from_go2rtc()
        sdes = _make_camera(is_sdes=True)
        assert await sdes._push_mode() is False
    resolver.assert_not_awaited()


async def test_the_reference_box_stays_on_its_addon(monkeypatch):
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    with _resolves_to(ADDON):
        assert (
            await cam._push_serve_url() == "rtsp://127.0.0.1:8554/" + cam._go2rtc_name
        )


async def test_registration_goes_to_the_resolved_server(monkeypatch):
    """The bundled server is reached through HA's own session and URL, and the
    URL handed back is its RTSP port, not the add-on's."""
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    client = AsyncMock()
    client.ensure_stream.return_value = True
    with (
        _resolves_to(BUNDLED),
        patch.object(gt, "Go2rtcClient", return_value=client) as ctor,
    ):
        url = await cam._publish_to_go2rtc(cam._serve_url)
    assert url == "rtsp://127.0.0.1:18554/" + cam._go2rtc_name
    assert ctor.call_args.args == (_SESSION, "http://localhost:11984/")
    client.ensure_stream.assert_awaited_once()


async def test_the_addon_client_is_built_exactly_as_before(monkeypatch):
    """Reference box: same base URL, same timeouts as the old construction
    sites (3.0 s register, 2.0 s publisher check, library default 5.0 s
    for teardown)."""
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=True)
    client = AsyncMock()
    client.ensure_stream.return_value = True
    client.list_streams.return_value = {}
    with (
        _resolves_to(ADDON),
        patch.object(gt, "Go2rtcClient", return_value=client) as ctor,
    ):
        assert (
            await cam._publish_to_go2rtc(cam._serve_url)
            == "rtsp://127.0.0.1:8554/" + cam._go2rtc_name
        )
        assert ctor.call_args.args == (_SESSION, "http://127.0.0.1:1984")
        assert ctor.call_args.kwargs == {"timeout": 3.0}
        await cam._go2rtc_publisher_state(True)
        assert ctor.call_args.kwargs == {"timeout": 2.0}
        await cam._unpublish_from_go2rtc()
        assert ctor.call_args.kwargs == {"timeout": 5.0}
        client.remove_stream.assert_awaited_with(cam._go2rtc_name)


async def test_a_failed_registration_re_resolves_next_time(monkeypatch):
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    client = AsyncMock()
    client.ensure_stream.return_value = False
    client.has_stream.return_value = False
    invalidate = MagicMock()
    with (
        _resolves_to(BUNDLED),
        patch.object(gt, "Go2rtcClient", return_value=client),
        patch.object(camera_mod, "invalidate_go2rtc_target", invalidate),
    ):
        assert await cam._publish_to_go2rtc(cam._serve_url) is None
    invalidate.assert_called_once_with(cam.hass)


async def test_no_target_means_nothing_to_register_or_check():
    cam = _make_camera(is_sdes=True)
    with (
        _resolves_to(None),
        patch.object(gt, "Go2rtcClient") as ctor,
    ):
        assert await cam._publish_to_go2rtc(cam._serve_url) is None
        assert await cam._go2rtc_publisher_state(True) == "unknown"
        await cam._unpublish_from_go2rtc()
    ctor.assert_not_called()


async def test_a_dead_explicit_server_keeps_dtls_on_its_pull_serve(monkeypatch):
    """An explicit AIDOT_GO2RTC_API is not probed by the resolver, but a DTLS
    camera must not push into a server that does not answer: it was probed
    before, and still is."""
    _publishes(monkeypatch)
    explicit = gt.Go2rtcTarget(
        "http://10.0.0.5:1984", _SESSION, "10.0.0.5", 8554, gt.KIND_EXPLICIT
    )
    cam = _make_camera(is_sdes=False)
    cam.hass.data = {}
    client = AsyncMock()
    client.available.return_value = False
    with (
        _resolves_to(explicit),
        patch.object(gt, "Go2rtcClient", return_value=client),
    ):
        assert await cam._push_mode() is False
    client.available.assert_awaited_once()


async def test_the_hls_path_also_gets_none_for_an_sdes_camera_with_no_go2rtc(
    monkeypatch,
):
    """Recordings and the HLS dialog come through the same stream_source, with
    the AAC track option on by default; they must get None too, not a crash."""
    monkeypatch.setattr(camera_mod, "_hls_audio_enabled", lambda: True)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    token = camera_mod._STREAM_SOURCE_HLS.set(True)
    try:
        with _resolves_to(None):
            assert await cam.stream_source() is None
    finally:
        camera_mod._STREAM_SOURCE_HLS.reset(token)


# --------------------------------------------------------------------------- #
# review round 1: camera behaviour through the REAL resolver                  #
# --------------------------------------------------------------------------- #

_MANAGED = SimpleNamespace(url="http://localhost:11984/", session=object())


@pytest.fixture
def real_resolver(monkeypatch):
    """Undo conftest's add-on stand-in: camera.py asks the real resolver, with
    only the add-on probe and the client session stubbed."""
    monkeypatch.setattr(camera_mod, "resolve_go2rtc_target", gt.resolve_go2rtc_target)
    monkeypatch.setattr(gt, "async_get_clientsession", lambda hass: _SESSION)
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    # The bundled path is off by default (see gt.PUBLISH_INTO_BUNDLED); these
    # tests keep it working for the day it can be turned back on.
    monkeypatch.setattr(gt, "PUBLISH_INTO_BUNDLED", True)


def _real_hass(cam, *, state=CoreState.running, handle=_MANAGED, components=()):
    cam.hass.data = {} if handle is None else {"go2rtc": handle}
    cam.hass.state = state
    cam.hass.config.components = set(components)
    return cam


async def test_a_failed_probe_after_the_addon_answered_keeps_the_addon(
    monkeypatch, real_resolver
):
    """A failed probe of 1984 after the add-on has answered must not resolve to the
    bundled server (whose handle always exists) and cache it."""
    _publishes(monkeypatch)
    mains = _real_hass(_make_camera(is_sdes=True))
    dtls = _real_hass(_make_camera(is_sdes=False))
    dtls.hass = mains.hass
    probe = AsyncMock(side_effect=[True, False])
    client = AsyncMock()
    client.ensure_stream.return_value = True
    with (
        patch.object(gt, "_addon_answers", probe),
        patch.object(gt, "Go2rtcClient", return_value=client) as ctor,
    ):
        assert (
            await mains._push_serve_url()
            == "rtsp://127.0.0.1:8554/" + mains._go2rtc_name
        )
        gt.invalidate(mains.hass)  # the 60 s cache expires
        assert (
            await mains._push_serve_url()
            == "rtsp://127.0.0.1:8554/" + mains._go2rtc_name
        )
        assert await mains._push_mode() is True  # SDES still publishes, as before
        assert await dtls._push_mode() is False  # DTLS falls back to pull, as before
        await mains._publish_to_go2rtc(mains._serve_url)
    assert ctor.call_args.args == (_SESSION, "http://127.0.0.1:1984")
    assert probe.await_count == 2  # no extra probe from the camera


async def test_before_ha_runs_a_failed_probe_is_not_cached(monkeypatch, real_resolver):
    _publishes(monkeypatch)
    cam = _real_hass(_make_camera(is_sdes=False), state=CoreState.starting)
    probe = AsyncMock(return_value=False)
    with patch.object(gt, "_addon_answers", probe):
        await cam._push_mode()
        await cam._push_mode()
    assert probe.await_count == 2


async def test_a_user_configured_handle_is_not_treated_as_bundled(
    monkeypatch, real_resolver
):
    _publishes(monkeypatch)
    user = SimpleNamespace(url="http://192.168.1.5:1984", session=object())
    cam = _real_hass(_make_camera(is_sdes=False), handle=user)
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert await cam._push_mode() is False
        assert "18554" not in await cam._push_serve_url()


async def test_sdes_with_a_dead_explicit_url_and_ha_go2rtc_gets_the_push_url(
    monkeypatch, real_resolver
):
    """HA's go2rtc provider tears down every session on a None stream source,
    so an SDES camera only answers None when that provider is not loaded."""
    monkeypatch.setenv("AIDOT_GO2RTC_API", "http://10.0.0.5:1984")
    cam = _real_hass(_make_camera(is_sdes=True), components={"go2rtc"})
    client = AsyncMock()
    client.available.return_value = False
    with patch.object(gt, "Go2rtcClient", return_value=client):
        url = await cam.stream_source()  # the setup-time answer
    assert url == "rtsp://10.0.0.5:8554/" + cam._go2rtc_name


async def test_sdes_with_only_a_remote_ha_go2rtc_opens_no_session(
    monkeypatch, real_resolver, caplog
):
    """A go2rtc Home Assistant was pointed at elsewhere is not a publish target
    (its RTSP address is unknown); the add-on's 127.0.0.1:8554 has nothing
    listening, so a session there could only loop. AIDOT_GO2RTC_API names it."""
    user = SimpleNamespace(url="http://192.168.1.5:1984", session=object())
    cam = _real_hass(_make_camera(is_sdes=True), handle=user, components={"go2rtc"})
    cam._setup_complete = True
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        await cam.stream_source()
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()
    assert "stays unavailable" in caplog.text


async def test_sdes_with_nothing_at_all_still_answers_none(monkeypatch, real_resolver):
    cam = _real_hass(_make_camera(is_sdes=True), handle=None)
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert await cam.stream_source() is None


async def test_a_dtls_battery_camera_stays_on_pull_with_the_bundled_server(
    monkeypatch, real_resolver
):
    """Stopgap: the library's viewer check asks the add-on's address, which on
    a bundled-only install answers "unknown" and never releases a battery
    session. The pull serve's socket check does work there."""
    _publishes(monkeypatch)
    cam = _real_hass(_make_camera(is_sdes=False, is_battery=True))
    mains = _real_hass(_make_camera(is_sdes=False))
    mains.hass = cam.hass
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert await cam._push_mode() is False
        assert await mains._push_mode() is True


async def test_one_stream_source_call_uses_one_target(monkeypatch):
    """Registration and publish in one call cannot straddle a cache expiry."""
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._await_publisher_attached = AsyncMock(return_value=True)
    client = AsyncMock()
    client.ensure_stream.return_value = True
    resolver = AsyncMock(side_effect=[BUNDLED, ADDON, ADDON, ADDON, ADDON])
    with (
        patch.object(camera_mod, "resolve_go2rtc_target", resolver),
        patch.object(gt, "Go2rtcClient", return_value=client) as ctor,
    ):
        url = await cam.stream_source()
    assert resolver.await_count == 1
    assert url == "rtsp://127.0.0.1:18554/" + cam._go2rtc_name
    assert {c.args[1] for c in ctor.call_args_list} == {"http://localhost:11984/"}
    dc = cam.coordinator.device_client
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url


async def test_the_startup_prewarm_waits_for_home_assistant_to_start():
    cam = _make_camera(is_sdes=True)
    cam.hass.state = CoreState.starting
    cam._startup_prewarm = MagicMock(return_value="coro")
    cam.async_on_remove = MagicMock()
    cam._schedule_startup_prewarm()
    cam.hass.async_create_task.assert_not_called()
    event, handler = cam.hass.bus.async_listen_once.call_args.args
    assert event == EVENT_HOMEASSISTANT_STARTED
    handler(None)
    cam.hass.async_create_task.assert_called_once_with("coro")


async def test_the_startup_prewarm_runs_at_once_when_ha_is_running():
    cam = _make_camera(is_sdes=True)
    cam.hass.state = CoreState.running
    cam._startup_prewarm = MagicMock(return_value="coro")
    cam.async_on_remove = MagicMock()
    cam._schedule_startup_prewarm()
    cam.hass.async_create_task.assert_called_once_with("coro")
    cam.hass.bus.async_listen_once.assert_not_called()


async def test_a_mains_motion_prewarm_before_ha_started_pulls_not_bundled(
    monkeypatch, real_resolver
):
    """A mains session never releases, so one published into the bundled server
    before the add-on answers would stay there until restart."""
    _publishes(monkeypatch)
    cam = _real_hass(
        _make_camera(is_sdes=False, stream_rtsp_url=None),
        state=CoreState.starting,
        components={"go2rtc"},
    )
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert await cam._prewarm_stream() is True
    dc = cam.coordinator.device_client
    dc.start_keepalive.assert_awaited_once()
    url = dc.start_keepalive.await_args.kwargs["rtsp_push_url"]
    assert "18554" not in url
    assert url == cam._serve_url  # it pulls, as before


async def test_the_no_go2rtc_warning_re_arms_once_a_target_resolves(caplog):
    """Lost, found, lost again: the second loss is logged too, once."""
    caplog.set_level(logging.WARNING)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)

    def _warnings():
        return [r for r in caplog.records if "no go2rtc answered" in r.getMessage()]

    with _resolves_to(None):
        assert await cam._sdes_has_no_go2rtc() is True
        assert await cam._sdes_has_no_go2rtc() is True
    assert len(_warnings()) == 1
    with _resolves_to(ADDON):
        assert await cam._sdes_has_no_go2rtc() is False
    with _resolves_to(None):
        assert await cam._sdes_has_no_go2rtc() is True
        assert await cam._sdes_has_no_go2rtc() is True
    assert len(_warnings()) == 2


def _client_per_server():
    clients = {ADDON.api_url: AsyncMock(), BUNDLED.api_url: AsyncMock()}
    for client in clients.values():
        client.ensure_stream.return_value = True
    return clients, patch.object(
        gt, "Go2rtcClient", side_effect=lambda _s, url, **_kw: clients[url]
    )


async def test_teardown_deletes_from_the_server_the_stream_was_registered_on(
    monkeypatch,
):
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    clients, ctor = _client_per_server()
    with ctor:
        with _resolves_to(ADDON):
            assert await cam._publish_to_go2rtc(cam._serve_url) is not None
        with _resolves_to(BUNDLED):
            await cam._unpublish_from_go2rtc()
    clients[ADDON.api_url].remove_stream.assert_awaited_once_with(cam._go2rtc_name)
    clients[BUNDLED.api_url].remove_stream.assert_not_awaited()


async def test_teardown_with_nothing_registered_uses_the_current_target(monkeypatch):
    _publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    clients, ctor = _client_per_server()
    with ctor, _resolves_to(BUNDLED):
        await cam._unpublish_from_go2rtc()
    clients[BUNDLED.api_url].remove_stream.assert_awaited_once_with(cam._go2rtc_name)
    clients[ADDON.api_url].remove_stream.assert_not_awaited()


# --------------------------------------------------------------------------- #
# An SDES battery camera is not streamed into the bundled server yet
# --------------------------------------------------------------------------- #
# Stopgap until the library can count viewers on Home Assistant's built-in
# go2rtc: there a battery session could not be released when nobody watches.
def _bundled_hass(cam):
    cam.hass.config.components = {"go2rtc"}
    return cam


def _not_streamed_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "could not be released" in r.getMessage()
    ]


async def test_an_sdes_battery_camera_is_not_streamed_into_the_bundled_server(
    caplog,
):
    caplog.set_level(logging.WARNING)
    cam = _bundled_hass(_make_camera(is_sdes=True, is_battery=True))
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    dc = cam.coordinator.device_client
    with _resolves_to(BUNDLED):
        assert await cam._push_mode() is False
        # HA's go2rtc provider is loaded here, and a None stream source makes it
        # tear down every camera's session, so the answer is a URL that fails
        # locally with nothing published behind it.
        setup_url = await cam.stream_source()  # the setup-time answer
        cam._setup_complete = True
        view_url = await cam.stream_source()  # the view-time answer
        assert await cam._prewarm_stream() is False
    for url in (setup_url, view_url):
        assert url is not None and url.startswith("rtsp://")
    dc.start_keepalive.assert_not_awaited()
    cam._publish_to_go2rtc.assert_not_awaited()
    warnings = _not_streamed_warnings(caplog)
    assert len(warnings) == 1
    assert cam._rtsp_name in warnings[0].getMessage()
    assert "built-in go2rtc" in warnings[0].getMessage()


async def test_the_sd_refresh_button_returns_at_once_for_a_bundled_battery_camera(
    monkeypatch,
):
    """_prewarm_stream is the SD refresh button's session opener. For a
    battery camera stuck on the bundled server it now opens nothing and says
    so, so the coordinator must not wait out the full session-wait window
    (75 s in production) for a session that will never be asked for. The
    window here is shortened (not removed) so a regression fails in seconds
    rather than passing slowly."""
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 5.0)
    cam = _bundled_hass(_make_camera(is_sdes=True, is_battery=True))
    dc = cam.coordinator.device_client
    dc.async_get_sd_recordings = AsyncMock(return_value=None)

    coord = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    coord.device_client = dc
    coord.sd_cache = None
    coord._sd_opener = None
    coord._sd_lock = asyncio.Lock()
    coord.register_session_opener(cam._prewarm_stream)

    with _resolves_to(BUNDLED):
        started = time.monotonic()
        assert await coord.async_list_sd_recordings(open_session=True) is False
        elapsed = time.monotonic() - started
    assert elapsed < 1.0
    dc.start_keepalive.assert_not_awaited()


async def test_an_sdes_mains_camera_still_publishes_to_the_bundled_server(caplog):
    cam = _bundled_hass(_make_camera(is_sdes=True, is_battery=False))
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    with _resolves_to(BUNDLED):
        url = await cam.stream_source()
    assert url == "rtsp://127.0.0.1:18554/" + cam._go2rtc_name
    dc = cam.coordinator.device_client
    dc.start_keepalive.assert_awaited_once()
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url
    assert _not_streamed_warnings(caplog) == []


async def test_an_sdes_battery_camera_on_the_addon_is_unchanged(caplog):
    cam = _bundled_hass(_make_camera(is_sdes=True, is_battery=True))
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    with _resolves_to(ADDON):
        url = await cam.stream_source()
    assert url == "rtsp://127.0.0.1:8554/" + cam._go2rtc_name
    dc = cam.coordinator.device_client
    dc.start_keepalive.assert_awaited_once()
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url
    assert _not_streamed_warnings(caplog) == []


async def test_each_no_go2rtc_warning_has_its_own_flag(caplog):
    """The bundled-battery warning used to share its once-flag with the plain
    "no go2rtc anywhere" warning, so whichever fired first permanently
    silenced the other on that camera. They are different conditions and
    each must fire exactly once, regardless of which happens first."""
    caplog.set_level(logging.WARNING)
    cam = _make_camera(is_sdes=True, is_battery=True)
    cam.hass.config.components = set()

    def _no_go2rtc_warnings():
        return [r for r in caplog.records if "no go2rtc answered" in r.getMessage()]

    # "No go2rtc anywhere" fires first, and repeats do not repeat it.
    with _resolves_to(None):
        assert await cam._sdes_has_no_go2rtc() is True
        assert await cam._sdes_has_no_go2rtc() is True
    assert len(_no_go2rtc_warnings()) == 1
    assert _not_streamed_warnings(caplog) == []

    # The bundled server then resolves. This battery camera still cannot be
    # released there, and that warning - on its own flag - must still fire
    # even though the warning above already fired on this same camera.
    cam.hass.config.components = {"go2rtc"}
    with _resolves_to(BUNDLED):
        assert await cam._sdes_has_no_go2rtc() is True
        assert await cam._sdes_has_no_go2rtc() is True
    assert len(_not_streamed_warnings(caplog)) == 1
    assert len(_no_go2rtc_warnings()) == 1


async def test_an_sdes_view_on_a_bundled_only_install_opens_no_session(caplog):
    """2.37.2 stopped publishing into the bundled go2rtc, so a running install
    with only that server resolves no target. Handing an SDES camera the
    add-on's push URL (127.0.0.1:8554, nothing listening) started a keepalive
    whose publisher was refused every time: the session ended, reopened 10 s
    later, and re-woke a battery camera every 35-60 s until restart. Once Home
    Assistant is running, no target means no session for an SDES camera."""
    caplog.set_level(logging.WARNING)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam.hass.state = CoreState.running
    cam.hass.config.components = {"go2rtc"}
    with _resolves_to(None):
        assert await cam._sdes_has_no_go2rtc() is True
        assert await cam._push_mode() is False
        await cam.stream_source()
        await cam._prewarm_stream()
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()
    assert "no go2rtc answered" in caplog.text


async def test_while_ha_starts_an_sdes_camera_keeps_the_add_on_push_url():
    """Before Home Assistant is running the add-on may simply not be up yet,
    and the resolver does not cache that None; the add-on's URL is kept."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam.hass.state = CoreState.starting
    cam.hass.config.components = {"go2rtc"}
    with _resolves_to(None):
        assert await cam._sdes_has_no_go2rtc() is False
