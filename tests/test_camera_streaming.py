"""Streaming / lifecycle branch coverage for the AiDot camera entity.

Complements ``test_camera.py`` / ``test_camera_methods.py`` (which cover the
serve-port math, connection options, the high-level stream_source state machine,
the evict watchdog, snapshots and the audio/ptz service happy-paths). This file
targets the still-uncovered branches:

  * ``_on_motion_prewarm`` scheduling guards
  * ``_prewarm_stream`` (real start_keepalive call + failure logging)
  * ``_prefetch_thumbnail`` success / failure paths
  * the stream_source DTLS waiter-raise branch
  * ``_await_serve_listening`` poll loop + ``_serve_port_listening`` try-bind probe
  * ``_active_status`` / ``extra_state_attributes`` edges
  * ``async_talk`` frame-provider path
  * ``_decode_pcm_8k`` success / reject-scheme / timeout-kill paths
  * ``_resolve_media`` pass-through and media-source resolution

Everything is deterministic: no real sleep, subprocess or network - the only
real syscall is a loopback ``bind()`` in the ``_serve_port_listening`` probe
tests (that is the behaviour under test).
"""

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.aidot import camera as camera_mod
from custom_components.aidot.camera import AidotCamera


def _make_camera(
    *,
    is_sdes: bool = False,
    is_battery: bool = False,
    stream_rtsp_url: str | None = None,
    options: dict | None = None,
    sdes_audio_override: bool | None = None,
) -> AidotCamera:
    """Build an AidotCamera backed by a mocked coordinator/device_client.

    Mirrors ``test_camera.py._make_camera``.
    """
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


class _FakeResp:
    """Minimal async-context-manager stand-in for an aiohttp response."""

    def __init__(self, status: int, data: bytes = b"") -> None:
        self.status = status
        self._data = data

    async def read(self) -> bytes:
        return self._data

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _inline_executor(cam: AidotCamera) -> None:
    """Make hass.async_add_executor_job run the passed sync helper inline."""

    async def _exec(func, *args):
        return func(*args)

    cam.hass.async_add_executor_job = AsyncMock(side_effect=_exec)


# --------------------------------------------------------------------------- #
# _on_motion_prewarm  (~305-319)
# --------------------------------------------------------------------------- #
def _capturing_create_task(cam: AidotCamera) -> list:
    created: list = []

    def _ct(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()  # avoid "coroutine was never awaited" warnings
        task = MagicMock(name="task")
        created.append(task)
        return task

    cam.hass.async_create_task = MagicMock(side_effect=_ct)
    return created


def test_on_motion_prewarm_skips_when_session_active():
    # start_keepalive is idempotent; skip the task entirely when already warm.
    cam = _make_camera(stream_rtsp_url="rtsp://active")
    created = _capturing_create_task(cam)
    cam._on_motion_prewarm({})
    assert created == []
    assert cam._prewarm_task is None


def test_on_motion_prewarm_skips_when_task_pending():
    cam = _make_camera(stream_rtsp_url=None)
    created = _capturing_create_task(cam)
    pending = MagicMock()
    pending.done = MagicMock(return_value=False)
    cam._prewarm_task = pending
    cam._on_motion_prewarm({})
    assert created == []
    assert cam._prewarm_task is pending  # unchanged


def test_on_motion_prewarm_schedules_prewarm():
    cam = _make_camera(stream_rtsp_url=None)
    created = _capturing_create_task(cam)
    cam._on_motion_prewarm({})
    assert len(created) == 1
    assert cam._prewarm_task is created[0]


def test_on_motion_prewarm_reschedules_after_prior_task_done():
    # A finished prior task must not block a fresh prewarm.
    cam = _make_camera(stream_rtsp_url=None)
    created = _capturing_create_task(cam)
    done = MagicMock()
    done.done = MagicMock(return_value=True)
    cam._prewarm_task = done
    cam._on_motion_prewarm({})
    assert len(created) == 1
    assert cam._prewarm_task is created[0]


# --------------------------------------------------------------------------- #
# _prewarm_stream  (~358-369)
# --------------------------------------------------------------------------- #
async def test_prewarm_stream_starts_keepalive_with_options():
    cam = _make_camera(is_battery=False, options={})
    cam.coordinator.device_client.start_keepalive = AsyncMock()
    await cam._prewarm_stream()
    ka = cam.coordinator.device_client.start_keepalive
    ka.assert_awaited_once()
    kwargs = ka.await_args.kwargs
    assert kwargs["rtsp_push_url"] == cam._serve_url
    assert kwargs["stream_idle_s"] == cam._stream_idle_s()
    # Connection options are merged in.
    assert "sdes_audio" in kwargs
    assert "fast_connect" in kwargs


async def test_prewarm_stream_swallows_failure():
    cam = _make_camera()
    cam.coordinator.device_client.start_keepalive = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    await cam._prewarm_stream()  # must not raise (failure is logged only)


# --------------------------------------------------------------------------- #
# _prefetch_thumbnail  (~387-413)
# --------------------------------------------------------------------------- #
async def test_prefetch_thumbnail_caches_and_writes_state():
    cam = _make_camera()
    cam.async_write_ha_state = MagicMock()
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value="http://cdn/thumb.jpg"
    )
    session = MagicMock()
    session.get = MagicMock(return_value=_FakeResp(200, b"IMGBYTES"))
    with patch(
        "custom_components.aidot.camera.async_get_clientsession",
        return_value=session,
    ):
        await cam._prefetch_thumbnail()
    assert cam._cached_image == b"IMGBYTES"
    assert cam._cached_image_ts > 0.0
    cam.async_write_ha_state.assert_called_once()


async def test_prefetch_thumbnail_returns_when_cloud_lookup_fails():
    cam = _make_camera()
    cam.async_write_ha_state = MagicMock()
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        side_effect=RuntimeError("cloud down")
    )
    await cam._prefetch_thumbnail()  # must not raise
    assert cam._cached_image is None
    cam.async_write_ha_state.assert_not_called()


async def test_prefetch_thumbnail_returns_when_no_url():
    cam = _make_camera()
    cam.async_write_ha_state = MagicMock()
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value=""
    )
    await cam._prefetch_thumbnail()
    assert cam._cached_image is None
    cam.async_write_ha_state.assert_not_called()


async def test_prefetch_thumbnail_non_200_does_not_cache():
    cam = _make_camera()
    cam.async_write_ha_state = MagicMock()
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value="http://cdn/thumb.jpg"
    )
    session = MagicMock()
    session.get = MagicMock(return_value=_FakeResp(500))
    with patch(
        "custom_components.aidot.camera.async_get_clientsession",
        return_value=session,
    ):
        await cam._prefetch_thumbnail()
    assert cam._cached_image is None
    cam.async_write_ha_state.assert_not_called()


async def test_prefetch_thumbnail_swallows_fetch_exception():
    cam = _make_camera()
    cam.async_write_ha_state = MagicMock()
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value="http://cdn/thumb.jpg"
    )
    session = MagicMock()
    session.get = MagicMock(side_effect=RuntimeError("boom"))
    with patch(
        "custom_components.aidot.camera.async_get_clientsession",
        return_value=session,
    ):
        await cam._prefetch_thumbnail()  # must not raise
    assert cam._cached_image is None


# --------------------------------------------------------------------------- #
# stream_source: DTLS waiter-raise branch  (~574-585)
# --------------------------------------------------------------------------- #
async def test_stream_source_dtls_waiter_exception_still_returns_url():
    # A cold DTLS session: async_wait_serve_ready raising is swallowed and HA is
    # handed the URL anyway (the worker retries until ffmpeg binds).
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam.coordinator.device_client.async_wait_serve_ready = AsyncMock(
        side_effect=RuntimeError("not ready")
    )
    assert await cam.stream_source() == "rtsp://go2rtc/x"
    cam.coordinator.device_client.async_wait_serve_ready.assert_awaited_once()
    # connected -> the "Connecting.../Negotiating..." overlay is cleared.
    assert cam._stream_status is None


# --------------------------------------------------------------------------- #
# _await_serve_listening  (~620-632)
# --------------------------------------------------------------------------- #
async def test_await_serve_listening_returns_immediately_when_bound():
    cam = _make_camera()
    _inline_executor(cam)
    cam._serve_port_listening = MagicMock(return_value=True)
    with patch(
        "custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()
    ) as sleeper:
        assert await cam._await_serve_listening(12345, timeout=5.0) is True
    sleeper.assert_not_awaited()


async def test_await_serve_listening_times_out():
    cam = _make_camera()
    _inline_executor(cam)
    cam._serve_port_listening = MagicMock(return_value=False)
    with patch(
        "custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()
    ) as sleeper, patch(
        "custom_components.aidot.camera.time.monotonic",
        side_effect=[1000.0, 1006.0],  # deadline=1005; next check 1006 >= 1005
    ):
        assert await cam._await_serve_listening(12345, timeout=5.0) is False
    sleeper.assert_not_awaited()


async def test_await_serve_listening_sleeps_between_misses_then_binds():
    cam = _make_camera()
    _inline_executor(cam)
    cam._serve_port_listening = MagicMock(side_effect=[False, True])
    with patch(
        "custom_components.aidot.camera.asyncio.sleep", new=AsyncMock()
    ) as sleeper, patch(
        "custom_components.aidot.camera.time.monotonic",
        side_effect=[1000.0, 1001.0],  # deadline=1005; still before it -> sleep
    ):
        assert await cam._await_serve_listening(12345, timeout=5.0) is True
    sleeper.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _serve_port_listening try-bind probe  (~601-618)
# --------------------------------------------------------------------------- #
def test_serve_port_listening_true_when_port_bound(socket_enabled):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert AidotCamera._serve_port_listening(port) is True
    finally:
        s.close()


def test_serve_port_listening_false_when_port_free(socket_enabled):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # release it -> probe should bind cleanly
    assert AidotCamera._serve_port_listening(port) is False


# --------------------------------------------------------------------------- #
# _active_status / extra_state_attributes edges  (~661-681)
# --------------------------------------------------------------------------- #
def test_active_status_none_when_unset():
    cam = _make_camera()
    cam._stream_status = None
    assert cam._active_status() is None


def test_extra_state_attributes_none_when_no_status():
    cam = _make_camera()
    cam._stream_status = None
    assert cam.extra_state_attributes is None


def test_extra_state_attributes_exposes_active_status():
    cam = _make_camera()
    cam._set_stream_status("Connecting...")  # async_write_ha_state is swallowed
    assert cam.extra_state_attributes == {"stream_status": "Connecting..."}


# --------------------------------------------------------------------------- #
# async_talk frame-provider path  (~764-772)
# --------------------------------------------------------------------------- #
async def test_async_talk_builds_and_streams_frames():
    cam = _make_camera()
    cam._resolve_media = AsyncMock(return_value="http://x/a.mp3")
    n = camera_mod.TALK_PCM_FRAME_BYTES
    pcm = b"\x01" * (2 * n + 5)  # -> 3 frames, last one padded to n
    cam._decode_pcm_8k = AsyncMock(return_value=pcm)

    collected: list = []

    async def _fake_speak(provider, max_seconds=30):
        while True:
            frame = provider()
            if frame is None:
                break
            collected.append(frame)
        return True

    cam.coordinator.device_client.async_speak = AsyncMock(side_effect=_fake_speak)
    await cam.async_talk("http://x/a.mp3")
    assert len(collected) == 3
    assert all(len(f) == n for f in collected)  # trailing frame padded


# --------------------------------------------------------------------------- #
# _decode_pcm_8k  (~809-833)
# --------------------------------------------------------------------------- #
async def test_decode_pcm_8k_rejects_non_http_scheme():
    cam = _make_camera()
    assert await cam._decode_pcm_8k("file:///etc/passwd") == b""


async def test_decode_pcm_8k_success_returns_pcm():
    cam = _make_camera()
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"PCMDATA", b""))
    with patch(
        "custom_components.aidot.camera.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ), patch(
        "custom_components.aidot.proxy.get_ffmpeg_binary",
        return_value="/usr/bin/ffmpeg",
    ):
        assert await cam._decode_pcm_8k("http://x/a.mp3", max_seconds=5) == b"PCMDATA"


async def test_decode_pcm_8k_timeout_kills_process():
    cam = _make_camera()
    proc = MagicMock()
    proc.kill = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))

    async def _timeout(coro, timeout=None):
        # Close the (unused) communicate() coroutine, then simulate the timeout.
        if hasattr(coro, "close"):
            coro.close()
        raise TimeoutError()

    with patch(
        "custom_components.aidot.camera.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ), patch(
        "custom_components.aidot.proxy.get_ffmpeg_binary",
        return_value="/usr/bin/ffmpeg",
    ), patch(
        "custom_components.aidot.camera.asyncio.wait_for",
        new=_timeout,
    ):
        assert await cam._decode_pcm_8k("https://x/a.mp3", max_seconds=5) == b""
    proc.kill.assert_called_once()


# --------------------------------------------------------------------------- #
# _resolve_media  (~794-807)
# --------------------------------------------------------------------------- #
async def test_resolve_media_passes_through_plain_url():
    cam = _make_camera()
    with patch(
        "homeassistant.components.media_source.is_media_source_id",
        return_value=False,
    ):
        assert await cam._resolve_media("http://x/a.mp3") == "http://x/a.mp3"


async def test_resolve_media_resolves_media_source_id():
    cam = _make_camera()
    item = SimpleNamespace(url="/local/a.mp3")
    with patch(
        "homeassistant.components.media_source.is_media_source_id",
        return_value=True,
    ), patch(
        "homeassistant.components.media_source.async_resolve_media",
        new=AsyncMock(return_value=item),
    ), patch(
        "homeassistant.components.media_player.browse_media."
        "async_process_play_media_url",
        return_value="http://ha/local/a.mp3",
    ):
        assert (
            await cam._resolve_media("media-source://x")
            == "http://ha/local/a.mp3"
        )
