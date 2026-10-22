"""The view path follows the session that is running, and opens none it cannot feed.

The library pins a session's mode when it starts: ``start_keepalive`` is a
no-op while one runs, and ``stream_rtsp_url`` keeps the first URL - rtsp:// for
a session publishing into go2rtc, the local http serve for a pulled one. The
view path used to recompute push or pull from the CURRENT go2rtc target on every
call instead, so the two drifted apart whenever the target changed under a
running session:

- A mains DTLS camera pulled at startup (no go2rtc yet) and viewed once the
  add-on answered was treated as publishing: every view waited the full 18.5 s
  for a publisher that never comes and re-registered the stream with the push
  placeholder, replacing the serve URL go2rtc was pulling from.
- A publishing session viewed while the add-on restarts was handed the local
  http serve, which nothing binds in push mode.

And the view path opened a publishing session before registering the stream,
so when registration then failed (no go2rtc yet while Home Assistant starts, or
an add-on that stopped answering) the keepalive went on publishing into a port
nothing takes - a mains SDES camera reopening for good, a battery one until the
library's 300 s cap.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import CoreState

from custom_components.aidot import camera as camera_mod
from custom_components.aidot import go2rtc_target as gt

from .test_camera import _make_camera

NAME = "aidot_eeee4444ffff"
PUSH_URL = f"rtsp://127.0.0.1:8554/{NAME}"


class _Client:
    """go2rtc's API as the view path sees it, recording what was written."""

    def __init__(self, log, *, answers=True, producers=None):
        self.log = log
        self.answers = answers
        self.producers = producers
        self.srcs = []

    async def list_streams(self):
        if not self.answers:
            return None
        if self.producers is None:
            return {}
        return {NAME: {"producers": self.producers}}

    async def ensure_stream(self, name, src, **kw):
        self.log.append("put")
        self.srcs.append(src)
        if not self.answers:
            return False
        if src == camera_mod._PUSH_PLACEHOLDER_SRC and self.producers is None:
            # A publisher attaches right after the keepalive starts.
            self.producers = [{"url": src}, {"remote_addr": "127.0.0.1:5000"}]
        return True

    async def has_stream(self, name):
        return self.answers

    async def available(self):
        return self.answers

    async def remove_stream(self, name):
        return True


def _target(client, *, kind=gt.KIND_ADDON, answered=True, ha_reads=False):
    t = gt.Go2rtcTarget(
        "http://127.0.0.1:1984",
        object(),
        "127.0.0.1",
        8554,
        kind,
        answered=answered,
        ha_reads=ha_reads,
    )
    object.__setattr__(t, "client", lambda timeout=5.0: client)
    return t


def _camera(monkeypatch, *, sdes, battery=False, url=None, target=None, log=None):
    monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: True)
    monkeypatch.setattr(camera_mod, "_GO2RTC_ENABLED", True)
    monkeypatch.setattr(camera_mod, "_PUSH_PUBLISHER_POLL_S", 0.0)
    monkeypatch.setattr(
        camera_mod, "resolve_go2rtc_target", AsyncMock(return_value=target)
    )
    cam = _make_camera(is_sdes=sdes, is_battery=battery, stream_rtsp_url=url)
    cam._setup_complete = True
    cam.hass.data = {}
    cam.hass.state = CoreState.running
    cam.hass.config.components = {"go2rtc"}
    dc = cam.coordinator.device_client
    if log is not None:

        async def _start(**kw):
            log.append("start")

        dc.start_keepalive = AsyncMock(side_effect=_start)

    async def _stop():
        dc.stream_rtsp_url = None

    dc.async_stop_streaming = AsyncMock(side_effect=_stop)
    return cam


# --------------------------------------------------------------------------- #
# The reference topology: add-on on 127.0.0.1:1984 answering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sdes", [False, True], ids=["dtls", "sdes"])
async def test_reference_cold_view_opens_one_publishing_session(monkeypatch, sdes):
    log = []
    client = _Client(log)
    cam = _camera(monkeypatch, sdes=sdes, target=_target(client), log=log)
    dc = cam.coordinator.device_client
    url = await cam.stream_source()
    assert url == PUSH_URL
    assert dc.start_keepalive.await_count == 1
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == PUSH_URL
    assert client.srcs == [camera_mod._PUSH_PLACEHOLDER_SRC]
    # The definition exists before anything publishes at it.
    assert log == ["put", "start"]


@pytest.mark.parametrize("sdes", [False, True], ids=["dtls", "sdes"])
async def test_reference_warm_view_reuses_the_publisher(monkeypatch, sdes):
    client = _Client([], producers=[{"url": "x"}, {"remote_addr": "a"}])
    cam = _camera(monkeypatch, sdes=sdes, url=PUSH_URL, target=_target(client))
    dc = cam.coordinator.device_client
    assert await cam.stream_source() == PUSH_URL
    assert dc.start_keepalive.await_count == 0
    assert client.srcs == []  # no PUT over a live publisher


# --------------------------------------------------------------------------- #
# (a) the view follows the running session's mode
# --------------------------------------------------------------------------- #
async def test_pulled_session_stays_pulled_once_the_addon_answers(monkeypatch):
    """Pulled at startup, viewed after the add-on came up: still a pull."""
    cam = _make_camera()
    serve = cam._serve_url
    client = _Client([], producers=[{"url": serve}])
    cam = _camera(monkeypatch, sdes=False, url=serve, target=_target(client))
    dc = cam.coordinator.device_client

    async def _no_wait(timeout):
        raise AssertionError(f"waited {timeout}s for a publisher a pull never has")

    cam._await_publisher_attached = _no_wait
    for _ in range(2):
        assert await cam.stream_source() == PUSH_URL  # go2rtc's view of the pull
    assert dc.start_keepalive.await_count == 0
    # Re-registered with the serve go2rtc pulls from, never the placeholder.
    assert client.srcs == [serve, serve]


async def test_publishing_session_keeps_its_url_while_the_addon_restarts(monkeypatch):
    """A publisher's view is the rtsp URL, never the http serve nothing binds."""
    client = _Client([], answers=False)
    cam = _camera(
        monkeypatch,
        sdes=False,
        url=PUSH_URL,
        target=_target(client, answered=False),
    )
    dc = cam.coordinator.device_client
    assert await cam.stream_source() == PUSH_URL
    assert dc.start_keepalive.await_count == 0
    assert dc.async_stop_streaming.await_count == 0


async def test_publishing_session_into_a_server_ha_reads_is_reopened_pulled(
    monkeypatch,
):
    """Push is impossible there now: the session stops, the next view pulls.

    The view that sees the flip opens nothing: _on_go2rtc_ha_reads is stopping
    the session in the background, and a session opened now could be cancelled
    by that stop.
    """
    client = _Client([], producers=[{"url": "x"}, {"remote_addr": "a"}])
    cam = _camera(
        monkeypatch, sdes=False, url=PUSH_URL, target=_target(client, ha_reads=True)
    )
    dc = cam.coordinator.device_client
    created = []
    cam.hass.async_create_background_task = lambda coro, name: created.append(coro)
    assert await cam.stream_source() == cam._serve_url
    assert dc.start_keepalive.await_count == 0
    for c in created:
        await c
    assert dc.async_stop_streaming.await_count == 1
    assert dc.stream_rtsp_url is None
    await cam.stream_source()
    assert dc.start_keepalive.await_count == 1
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == cam._serve_url


# --------------------------------------------------------------------------- #
# (b) no publishing session into a target that cannot take it
# --------------------------------------------------------------------------- #
async def test_sdes_view_while_ha_starts_without_go2rtc_opens_nothing(monkeypatch):
    cam = _camera(monkeypatch, sdes=True, target=None)
    cam.hass.state = CoreState.starting
    dc = cam.coordinator.device_client
    assert await cam.stream_source() == PUSH_URL  # soft-fail URL, not None
    assert dc.start_keepalive.await_count == 0


async def test_mains_sdes_view_with_unanswered_addon_opens_nothing(monkeypatch):
    client = _Client([], answers=False)
    cam = _camera(monkeypatch, sdes=True, target=_target(client, answered=False))
    dc = cam.coordinator.device_client
    assert await cam.stream_source() == PUSH_URL
    assert dc.start_keepalive.await_count == 0


async def test_battery_sdes_with_unanswered_addon_has_no_go2rtc(monkeypatch):
    client = _Client([], answers=False)
    cam = _camera(
        monkeypatch, sdes=True, battery=True, target=_target(client, answered=False)
    )
    dc = cam.coordinator.device_client
    assert await cam._sdes_has_no_go2rtc() is True
    assert await cam.stream_source() == PUSH_URL
    assert dc.start_keepalive.await_count == 0
    assert client.srcs == []  # not even a registration attempt
    assert await cam._prewarm_stream() is False


async def test_publishing_sdes_session_stops_when_ha_reads_its_server(monkeypatch):
    client = _Client([], producers=[{"url": "x"}, {"remote_addr": "a"}])
    cam = _camera(
        monkeypatch, sdes=True, url=PUSH_URL, target=_target(client, ha_reads=True)
    )
    dc = cam.coordinator.device_client
    created = []
    cam.hass.async_create_background_task = lambda coro, name: created.append(coro)
    await cam.stream_source()
    for c in created:
        await c
    assert dc.async_stop_streaming.await_count == 1
    assert dc.start_keepalive.await_count == 0


def _real_resolver(monkeypatch, client):
    """The real resolve_go2rtc_target, with the add-on probe under test control."""
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(gt, "_addon_answers", probe)
    monkeypatch.setattr(gt, "_ha_reads_target", AsyncMock(return_value=False))
    monkeypatch.setattr(gt, "async_get_clientsession", lambda h: object())
    monkeypatch.setattr(gt, "Go2rtcClient", lambda session, url, timeout=5.0: client)
    monkeypatch.setattr(camera_mod, "resolve_go2rtc_target", gt.resolve_go2rtc_target)
    return probe


async def test_one_failed_addon_probe_neither_stops_nor_refuses_a_battery_camera(
    monkeypatch,
):
    """Review D1: an add-on probe that fails once (cached for 60 s) while the
    add-on is up must not stop a battery SDES session someone is watching, and
    must not refuse its next open when go2rtc answers when asked directly."""
    client = _Client([], producers=[{"url": "x"}, {"remote_addr": "a"}])
    cam = _camera(monkeypatch, sdes=True, battery=True, url=PUSH_URL)
    probe = _real_resolver(monkeypatch, client)
    dc = cam.coordinator.device_client
    created = []
    cam.hass.async_create_background_task = lambda coro, name: created.append(coro)

    assert (await gt.resolve_go2rtc_target(cam.hass)).answered  # add-on seen
    gt.invalidate(cam.hass)
    probe.return_value = False  # one probe times out; sticky, answered=False
    assert not (await gt.resolve_go2rtc_target(cam.hass)).answered

    # A second viewer, a re-offer, the HLS dialog, camera.record: all of these
    # call stream_source on the running session.
    assert await cam.stream_source() == PUSH_URL
    assert created == []
    assert dc.async_stop_streaming.await_count == 0
    assert dc.start_keepalive.await_count == 0

    # The session ends; the next view opens, because go2rtc answers directly.
    dc.stream_rtsp_url = None
    client.producers = None
    assert await cam.stream_source() == PUSH_URL
    assert dc.start_keepalive.await_count == 1
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == PUSH_URL


async def test_battery_open_refused_only_when_go2rtc_does_not_answer_directly(
    monkeypatch,
):
    client = _Client([], answers=False)
    cam = _camera(monkeypatch, sdes=True, battery=True)
    probe = _real_resolver(monkeypatch, client)
    await gt.resolve_go2rtc_target(cam.hass)
    gt.invalidate(cam.hass)
    probe.return_value = False
    dc = cam.coordinator.device_client
    assert await cam.stream_source() == PUSH_URL
    assert dc.start_keepalive.await_count == 0
    assert client.srcs == []


async def test_a_view_between_two_stops_waits_for_the_pending_one(monkeypatch):
    """Review D2: the ha_reads signal schedules a stop (T1); a view landing
    before it ran must not schedule a second, and a view landing while it
    runs must not open a session that T1 then cancels."""
    client = _Client([])
    cam = _camera(
        monkeypatch, sdes=False, url=PUSH_URL, target=_target(client, ha_reads=True)
    )
    dc = cam.coordinator.device_client
    log = []
    gate = asyncio.Event()

    async def _stop():
        dc.stream_rtsp_url = None  # _streaming_active goes False first
        await gate.wait()
        log.append("stopped")

    async def _start(**kw):
        log.append("start")

    dc.async_stop_streaming = AsyncMock(side_effect=_stop)
    dc.start_keepalive = AsyncMock(side_effect=_start)
    cam.hass.async_create_background_task = lambda coro, name: asyncio.ensure_future(
        coro
    )

    cam._on_go2rtc_ha_reads(True)  # T1, not yet run
    await cam.stream_source()  # view A: sees the publishing session
    await asyncio.sleep(0)  # T1 starts and parks mid-stop
    assert dc.async_stop_streaming.await_count == 1  # no second stop
    view_b = asyncio.ensure_future(cam.stream_source())
    for _ in range(5):
        await asyncio.sleep(0)
    assert log == []  # B has not opened under the running stop
    gate.set()
    await view_b
    assert log == ["stopped", "start"]
    assert dc.async_stop_streaming.await_count == 1


async def test_a_pulled_sdes_session_is_not_stopped(monkeypatch):
    """Only a publishing (rtsp://) session is stopped, as in _on_go2rtc_ha_reads."""
    cam = _camera(monkeypatch, sdes=True, url="http://127.0.0.1:18600/x.ts")
    dc = cam.coordinator.device_client
    created = []
    cam.hass.async_create_background_task = lambda coro, name: created.append(coro)
    await cam.stream_source()
    assert created == []
    assert dc.async_stop_streaming.await_count == 0


async def test_a_stop_that_never_finishes_does_not_hang_the_view(monkeypatch):
    """The wait for a pending stop is bounded: the WebRTC path has no outer
    timeout, so a stuck stop must not hang the view. The stop is neither
    cancelled nor raised."""
    monkeypatch.setattr(camera_mod, "_PENDING_STOP_WAIT_S", 0.05)
    client = _Client([])
    cam = _camera(
        monkeypatch, sdes=False, url=PUSH_URL, target=_target(client, ha_reads=True)
    )
    dc = cam.coordinator.device_client
    never = asyncio.Event()

    async def _stop():
        dc.stream_rtsp_url = None
        await never.wait()

    dc.async_stop_streaming = AsyncMock(side_effect=_stop)
    tasks = []

    def _bg(coro, name):
        tasks.append(asyncio.ensure_future(coro))
        return tasks[-1]

    cam.hass.async_create_background_task = _bg
    cam._on_go2rtc_ha_reads(True)
    await asyncio.sleep(0)  # the stop starts and never returns
    url = await asyncio.wait_for(cam.stream_source(), timeout=2.0)
    assert url == PUSH_URL  # go2rtc's view of the pulled serve
    assert dc.start_keepalive.await_count == 1
    assert not tasks[0].done()  # not cancelled
    tasks[0].cancel()
