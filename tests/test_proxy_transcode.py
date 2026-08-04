"""Transcode / ffmpeg / HTTP-helper branches of ``proxy.py`` not exercised by the
other three proxy test files.

Everything that would otherwise shell out to ffmpeg, probe the GPU, hit the
network, or sleep is stubbed: ``asyncio.create_subprocess_exec`` is replaced with
a deterministic fake proc, ``subprocess.run`` (the HW encoder probe) is patched,
and ``hass.async_add_executor_job`` runs its callable inline.  Mirrors the
mocked-hass approach of ``test_proxy_view.py`` / ``test_proxy_internals.py``.

Targets (current proxy.py line ranges):
* ``async_resolve_event_url`` final ``return None`` + non-tuple/empty-url media
  branches (~206-213).
* ``async_prewarm_events`` no-instance early-out and the warm loop / slice
  (~223-227).
* ``get_ffmpeg_binary`` success + except fallback (~230-237).
* ``_is_webkit`` UA branches (~265-270).
* ``_transcode_to_cache`` HW-encoder-disable branch and per-plan fail->success and
  URL re-resolve-once feeding the fresh url (~399-453).
* ``_run_ffmpeg`` success / timeout / cancel handling (~457-483).
* ``_is_usable`` / ``_touch_if_usable`` / ``_unlink`` edges and ``_finalize`` /
  ``_enforce_cache_limits`` OSError swallow (~539-606).
* ``_detect_hw`` memoisation + copy, and ``_probe_hw_encoder`` probe branches
  (~523-637).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.aidot.proxy as proxy
from custom_components.aidot.proxy import (
    AidotVideoProxyView,
    _enforce_cache_limits,
    _finalize,
    _is_usable,
    _is_webkit,
    _probe_hw_encoder,
    _touch_if_usable,
    _unlink,
    async_prewarm_events,
    async_resolve_event_url,
    get_ffmpeg_binary,
)

P = "custom_components.aidot.proxy"


# --------------------------------------------------------------------------- #
# Shared fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_hw_plan():
    """Keep the memoised module-global HW plan isolated per test."""
    saved = proxy._hw_plan
    proxy._hw_plan = None
    yield
    proxy._hw_plan = saved


def _make_view() -> AidotVideoProxyView:
    hass = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/aidot_clips")
    return AidotVideoProxyView(hass)


def _fake_proc(returncode: int = 0, stderr: bytes = b"err") -> MagicMock:
    """A deterministic stand-in for an asyncio subprocess transport."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


async def _wait_for_timeout(coro, *args, **kwargs):
    """Replacement for asyncio.wait_for that simulates a transcode timeout."""
    coro.close()  # the proc.communicate() coroutine is never awaited here
    raise TimeoutError


async def _wait_for_cancel(coro, *args, **kwargs):
    coro.close()
    raise __import__("asyncio").CancelledError


def _req(ua: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"User-Agent": ua})


# --------------------------------------------------------------------------- #
# async_resolve_event_url - uncovered branches (~206-213)
# --------------------------------------------------------------------------- #
async def test_resolve_event_url_none_when_no_methods():
    # device_client exposes neither helper: media_fn None, url_fn None -> the
    # final ``return None`` (line ~213) that the other tests don't reach.
    coord = SimpleNamespace(device_client=SimpleNamespace())
    assert await async_resolve_event_url(coord, "v1:e") is None


async def test_resolve_event_url_media_plain_string():
    # async_get_event_video_media may return a bare url string (not a tuple).
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(return_value="https://cdn/plain.m3u8"),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") == "https://cdn/plain.m3u8"


async def test_resolve_event_url_empty_media_url_falls_back():
    # media returns a tuple whose url is empty -> fall through to the url helper.
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(return_value=("", "mime")),
            async_get_event_video_url=AsyncMock(return_value="https://cdn/fallback.m3u8"),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") == "https://cdn/fallback.m3u8"


# --------------------------------------------------------------------------- #
# async_prewarm_events (~223-227)
# --------------------------------------------------------------------------- #
async def test_prewarm_noop_when_no_instance():
    saved = proxy._instance
    proxy._instance = None
    try:
        await async_prewarm_events("dev1", ["v1:e1"])  # must not raise
    finally:
        proxy._instance = saved


async def test_prewarm_warms_up_to_limit():
    view = _make_view()  # __init__ registers this as the module singleton
    view.warm = AsyncMock()
    events = [f"v1:e{i}" for i in range(proxy._PREWARM_COUNT + 3)]
    await async_prewarm_events("dev1", events)
    # Only the first _PREWARM_COUNT clips are warmed, in order.
    assert view.warm.await_count == proxy._PREWARM_COUNT
    warmed = [c.args[1] for c in view.warm.await_args_list]
    assert warmed == events[: proxy._PREWARM_COUNT]


# --------------------------------------------------------------------------- #
# get_ffmpeg_binary (~230-237)
# --------------------------------------------------------------------------- #
def test_get_ffmpeg_binary_returns_manager_path():
    # get_ffmpeg_binary does a local `from homeassistant.components.ffmpeg import
    # get_ffmpeg_manager`; that component pulls in haffmpeg (absent under phacc),
    # so inject a fake module the local import resolves to.
    fake = SimpleNamespace(
        get_ffmpeg_manager=lambda hass: SimpleNamespace(binary="/usr/bin/ffmpeg")
    )
    with patch.dict("sys.modules", {"homeassistant.components.ffmpeg": fake}):
        assert get_ffmpeg_binary(MagicMock()) == "/usr/bin/ffmpeg"


def test_get_ffmpeg_binary_falls_back_on_error():
    def _boom(hass):
        raise RuntimeError("boom")

    fake = SimpleNamespace(get_ffmpeg_manager=_boom)
    with patch.dict("sys.modules", {"homeassistant.components.ffmpeg": fake}):
        assert get_ffmpeg_binary(MagicMock()) == "ffmpeg"


# --------------------------------------------------------------------------- #
# _is_webkit (~265-270)
# --------------------------------------------------------------------------- #
def test_is_webkit_chrome_is_false():
    # Chrome ships AppleWebKit in its UA but must NOT take the HEVC path.
    assert _is_webkit(_req("Mozilla/5.0 (Macintosh) AppleWebKit Chrome/120")) is False


def test_is_webkit_non_webkit_is_false():
    assert _is_webkit(_req("Mozilla/5.0 (X11; Linux) Gecko Firefox/120")) is False


def test_is_webkit_apple_devices_true():
    for ua in (
        "Mozilla/5.0 (iPhone) AppleWebKit Version/17 Safari",
        "Mozilla/5.0 (iPad) AppleWebKit Safari",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit Safari",
    ):
        assert _is_webkit(_req(ua)) is True


def test_is_webkit_webkit_but_not_apple_is_false():
    # AppleWebKit present, no Chrome, but no Apple device token -> not WebKit.
    assert _is_webkit(_req("Mozilla/5.0 (X11; Linux) AppleWebKit Safari")) is False


# --------------------------------------------------------------------------- #
# _transcode_to_cache - plan loop branches (~399-453)
# --------------------------------------------------------------------------- #
async def test_transcode_first_plan_fails_then_next_plan_succeeds():
    # hevc=True -> [hevc-passthrough, libx264]; passthrough fails, libx264 wins.
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=True)
    view._resolve_url = AsyncMock(return_value="https://cdn/x.m3u8")
    view._detect_hw = AsyncMock(return_value=None)
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    view._run_ffmpeg = AsyncMock(side_effect=[(1, b"bad"), (0, b"")])

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/c/x.mp4", hevc=True)

    assert ok is True
    assert view._run_ffmpeg.await_count == 2
    first_argv = view._run_ffmpeg.await_args_list[0].args[0]
    second_argv = view._run_ffmpeg.await_args_list[1].args[0]
    assert "copy" in first_argv and "hvc1" in first_argv      # passthrough
    assert "libx264" in second_argv                            # fallback


async def test_transcode_hw_plan_failure_disables_hw_then_libx264_wins():
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=True)
    view._resolve_url = AsyncMock(return_value="https://cdn/x.m3u8")
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    hw_plan = (
        "vaapi",
        ["-vaapi_device", "/dev/dri/renderD128"],
        ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"],
    )
    view._detect_hw = AsyncMock(return_value=hw_plan)
    # HW plan fails (can't init), libx264 then succeeds.
    view._run_ffmpeg = AsyncMock(side_effect=[(1, b"libva error"), (0, b"")])

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/c/x.mp4")

    assert ok is True
    # The failing HW encoder is disabled for the session (module-global -> []).
    assert proxy._hw_plan == []
    first_argv = view._run_ffmpeg.await_args_list[0].args[0]
    second_argv = view._run_ffmpeg.await_args_list[1].args[0]
    assert "h264_vaapi" in first_argv
    assert "libx264" in second_argv


async def test_transcode_reresolves_url_once_with_fresh_url():
    # Every encode fails -> URL re-resolved once; the fresh url feeds the retry.
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=False)
    view._detect_hw = AsyncMock(return_value=None)
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    view._resolve_url = AsyncMock(side_effect=["https://cdn/first", "https://cdn/second"])
    view._run_ffmpeg = AsyncMock(return_value=(1, b"nope"))

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/c/x.mp4")

    assert ok is False
    assert view._resolve_url.await_count == 2
    # First encode used the original url; the retry used the freshly resolved one.
    assert "https://cdn/first" in view._run_ffmpeg.await_args_list[0].args[0]
    assert "https://cdn/second" in view._run_ffmpeg.await_args_list[-1].args[0]


async def test_transcode_returns_false_when_url_unresolvable():
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=False)
    view._resolve_url = AsyncMock(return_value=None)
    view._run_ffmpeg = AsyncMock()

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/c/x.mp4")

    assert ok is False
    view._run_ffmpeg.assert_not_awaited()


async def test_transcode_returns_false_when_refresh_unresolvable():
    # First url resolves and all plans fail; the refresh resolves to None -> False.
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=False)
    view._detect_hw = AsyncMock(return_value=None)
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    view._resolve_url = AsyncMock(side_effect=["https://cdn/first", None])
    view._run_ffmpeg = AsyncMock(return_value=(1, b"nope"))

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/c/x.mp4")

    assert ok is False
    assert view._resolve_url.await_count == 2


# --------------------------------------------------------------------------- #
# _run_ffmpeg (~457-483)
# --------------------------------------------------------------------------- #
async def test_run_ffmpeg_success_returns_rc_and_stderr():
    view = _make_view()
    proc = _fake_proc(returncode=0, stderr=b"stderr-bytes")
    with patch(f"{P}.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        rc, err = await view._run_ffmpeg(["ffmpeg", "-i", "x"])
    assert rc == 0
    assert err == b"stderr-bytes"
    proc.kill.assert_not_called()


async def test_run_ffmpeg_nonzero_returncode():
    view = _make_view()
    proc = _fake_proc(returncode=1, stderr=b"fail")
    with patch(f"{P}.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        rc, err = await view._run_ffmpeg(["ffmpeg"])
    assert rc == 1
    assert err == b"fail"


async def test_run_ffmpeg_timeout_kills_and_reports():
    view = _make_view()
    proc = _fake_proc()
    with patch(f"{P}.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch(f"{P}.asyncio.wait_for", _wait_for_timeout):
        rc, err = await view._run_ffmpeg(["ffmpeg"])
    assert rc == -1
    assert err == b"transcode timed out"
    proc.kill.assert_called_once()
    # After kill we drain the pipe once more.
    assert proc.communicate.await_count == 1


async def test_run_ffmpeg_cancel_kills_and_reraises():
    import asyncio

    view = _make_view()
    proc = _fake_proc()
    with patch(f"{P}.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
         patch(f"{P}.asyncio.wait_for", _wait_for_cancel):
        with pytest.raises(asyncio.CancelledError):
            await view._run_ffmpeg(["ffmpeg"])
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Blocking filesystem helpers (~539-606)
# --------------------------------------------------------------------------- #
def test_is_usable_branches(tmp_path):
    f = tmp_path / "good.mp4"
    f.write_bytes(b"data")
    assert _is_usable(str(f)) is True
    assert _is_usable(str(tmp_path / "missing.mp4")) is False
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert _is_usable(str(empty)) is False


def test_touch_if_usable_branches(tmp_path):
    f = tmp_path / "good.mp4"
    f.write_bytes(b"data")
    assert _touch_if_usable(str(f)) is True
    assert _touch_if_usable(str(tmp_path / "missing.mp4")) is False


def test_touch_if_usable_swallows_utime_error(tmp_path):
    f = tmp_path / "good.mp4"
    f.write_bytes(b"data")
    # utime failing must not stop the promotion-hit from returning True.
    with patch(f"{P}.os.utime", side_effect=OSError("ro fs")):
        assert _touch_if_usable(str(f)) is True


def test_unlink_swallows_missing(tmp_path):
    _unlink(str(tmp_path / "nope.mp4"))  # no exception


def test_finalize_replaces_and_enforces(tmp_path):
    tmp = tmp_path / "x.part"
    tmp.write_bytes(b"clip-bytes")
    cache = tmp_path / "x.mp4"
    _finalize(str(tmp), str(cache), str(tmp_path), 10**9, 10**9)
    assert cache.exists()
    assert not tmp.exists()


def test_enforce_cache_limits_swallows_missing_dir():
    _enforce_cache_limits("/nonexistent/aidot_dir", 1, 1)  # OSError swallowed


def test_enforce_cache_limits_ignores_non_mp4(tmp_path):
    keep = tmp_path / "k.mp4"
    keep.write_bytes(b"x" * 10)
    other = tmp_path / "notes.txt"
    other.write_bytes(b"x" * 10_000)
    # Tiny byte budget would evict on size, but the .txt is never considered.
    _enforce_cache_limits(str(tmp_path), 1, 10**9, keep=str(keep))
    assert keep.exists()
    assert other.exists()


# --------------------------------------------------------------------------- #
# _detect_hw memoisation + _probe_hw_encoder (~523-637)
# --------------------------------------------------------------------------- #
async def test_detect_hw_probes_once_and_returns_copy():
    view = _make_view()
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    plan = ("vaapi", ["-vaapi_device", "/dev/dri/renderD128"], ["-c:v", "h264_vaapi"])
    view.hass.async_add_executor_job = AsyncMock(return_value=plan)

    got = await view._detect_hw()
    assert got == plan
    # Returns fresh list copies so callers can't mutate the memoised plan.
    assert got[1] is not plan[1]
    assert got[2] is not plan[2]
    assert proxy._hw_plan == plan

    # Second call is served from the memo: no extra probe.
    again = await view._detect_hw()
    assert again == plan
    assert view.hass.async_add_executor_job.await_count == 1


async def test_detect_hw_none_memoises_empty():
    view = _make_view()
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    view.hass.async_add_executor_job = AsyncMock(return_value=None)

    assert await view._detect_hw() is None
    assert proxy._hw_plan == []
    # A disabled/absent HW encoder isn't re-probed.
    assert await view._detect_hw() is None
    assert view.hass.async_add_executor_job.await_count == 1


def test_probe_hw_encoder_no_render_node():
    with patch(f"{P}.os.path.exists", return_value=False):
        assert _probe_hw_encoder("ffmpeg") is None


def test_probe_hw_encoder_vaapi():
    fake = MagicMock()
    fake.stdout = b" V..... h264_vaapi  H.264 via VAAPI\n h264_qsv\n"
    with patch(f"{P}.os.path.exists", return_value=True), \
         patch("subprocess.run", return_value=fake):
        name, input_args, video_args = _probe_hw_encoder("ffmpeg")
    assert name == "vaapi"
    assert "-vaapi_device" in input_args
    assert "h264_vaapi" in video_args


def test_probe_hw_encoder_qsv_when_no_vaapi():
    fake = MagicMock()
    fake.stdout = b" V..... h264_qsv  H.264 via Intel QSV\n"
    with patch(f"{P}.os.path.exists", return_value=True), \
         patch("subprocess.run", return_value=fake):
        name, input_args, video_args = _probe_hw_encoder("ffmpeg")
    assert name == "qsv"
    assert "h264_qsv" in video_args


def test_probe_hw_encoder_none_when_no_hw_encoders():
    fake = MagicMock()
    fake.stdout = b" V..... libx264  H.264 software\n"
    with patch(f"{P}.os.path.exists", return_value=True), \
         patch("subprocess.run", return_value=fake):
        assert _probe_hw_encoder("ffmpeg") is None


def test_probe_hw_encoder_none_when_subprocess_raises():
    with patch(f"{P}.os.path.exists", return_value=True), \
         patch("subprocess.run", side_effect=OSError("ffmpeg missing")):
        assert _probe_hw_encoder("ffmpeg") is None
