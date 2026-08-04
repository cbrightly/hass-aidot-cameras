"""Remaining proxy branches: warm()'s bad-event + under-lock body, get()'s two
cache-hit fast paths, _run_ffmpeg's post-kill drain swallows, _resolve_url's
no-coordinator / resolve-error paths, _ffmpeg_binary delegation, and a few
blocking-helper edges (_ensure_dir, os.stat skip, size eviction).

ffmpeg / GPU probe / network / filesystem finalize are stubbed; ``hass`` is a
MagicMock whose ``async_add_executor_job`` is an AsyncMock, mirroring
``test_proxy_view.py`` / ``test_proxy_transcode.py``.
"""

import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiohttp import web

from custom_components.aidot.proxy import (
    AidotVideoProxyView,
    _EVENT_LOCK_REFS,
    _EVENT_LOCKS,
    _ensure_dir,
    _enforce_cache_limits,
    sign_playback_url,
)

P = "custom_components.aidot.proxy"

NOW = 1_000_000.0
DEVICE = "dev1"
EVENT = "v1:91477a47-e000-4160-a937-e1900c01ee43"


def _make_view() -> AidotVideoProxyView:
    hass = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/aidot_clips")
    view = AidotVideoProxyView(hass)
    view.hass.async_add_executor_job = AsyncMock(return_value=False)
    return view


def _request(params: dict, *, ua: str = "") -> SimpleNamespace:
    return SimpleNamespace(query=dict(params), headers={"User-Agent": ua})


def _params(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _fake_proc(returncode: int = 0, stderr: bytes = b"err") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


async def _wait_for_timeout(coro, *args, **kwargs):
    coro.close()
    raise TimeoutError


async def _wait_for_cancel(coro, *args, **kwargs):
    import asyncio

    coro.close()
    raise asyncio.CancelledError


# --------------------------------------------------------------------------- #
# warm() - bad event + full under-lock body (302, 311-317)
# --------------------------------------------------------------------------- #
async def test_warm_rejects_unsafe_event():
    view = _make_view()
    view._transcode_to_cache = AsyncMock()
    await view.warm("dev1", "bad/../evt")  # _safe_event -> None -> return (302)
    view._transcode_to_cache.assert_not_awaited()


async def test_warm_returns_when_usable_under_lock():
    view = _make_view()
    # _touch_if_usable False (305), then _is_usable True (312) inside the lock.
    view.hass.async_add_executor_job = AsyncMock(side_effect=[False, True])
    view._transcode_to_cache = AsyncMock()
    await view.warm("dev1", "v1:warm-usable")  # 311, 312-313
    view._transcode_to_cache.assert_not_awaited()


async def test_warm_transcodes_and_swallows_error():
    view = _make_view()
    # Not cached at either check, so the lock body runs the transcode, which
    # raises and is swallowed by warm()'s best-effort except.
    view.hass.async_add_executor_job = AsyncMock(side_effect=[False, False])
    view._transcode_to_cache = AsyncMock(side_effect=RuntimeError("warm boom"))
    await view.warm("dev1", "v1:warm-boom")  # 314-315 transcode, 316-317 except
    view._transcode_to_cache.assert_awaited_once()


# --------------------------------------------------------------------------- #
# get() - cache-hit fast paths (353, 360)
# --------------------------------------------------------------------------- #
async def test_get_fast_path_serves_cached_file():
    view = _make_view()
    view._transcode_to_cache = AsyncMock()
    view.hass.async_add_executor_job = AsyncMock(return_value=True)  # cached
    with patch(f"{P}.time.time", return_value=NOW):
        resp = await view.get(_request(_params(sign_playback_url(DEVICE, EVENT))))
    assert isinstance(resp, web.FileResponse)  # line 353
    view._transcode_to_cache.assert_not_awaited()


async def test_get_serves_file_produced_while_awaiting_lock():
    view = _make_view()
    view._transcode_to_cache = AsyncMock()
    # First check misses (352); another producer finishes while we wait, so the
    # under-lock re-check (359) hits and we serve the file (360).
    view.hass.async_add_executor_job = AsyncMock(side_effect=[False, True])
    with patch(f"{P}.time.time", return_value=NOW):
        resp = await view.get(_request(_params(sign_playback_url(DEVICE, EVENT))))
    assert isinstance(resp, web.FileResponse)  # line 360
    view._transcode_to_cache.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _run_ffmpeg - post-kill drain swallows (473-474, 480-481)
# --------------------------------------------------------------------------- #
async def test_run_ffmpeg_timeout_swallows_drain_error():
    view = _make_view()
    proc = _fake_proc()
    proc.communicate = AsyncMock(side_effect=RuntimeError("drain boom"))
    with patch(f"{P}.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), patch(
        f"{P}.asyncio.wait_for", _wait_for_timeout
    ):
        rc, err = await view._run_ffmpeg(["ffmpeg"])  # 471-474 inner except pass
    assert rc == -1
    assert err == b"transcode timed out"
    proc.kill.assert_called_once()


async def test_run_ffmpeg_cancel_swallows_wait_error():
    import asyncio

    view = _make_view()
    proc = _fake_proc()
    proc.wait = AsyncMock(side_effect=RuntimeError("wait boom"))
    with patch(f"{P}.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), patch(
        f"{P}.asyncio.wait_for", _wait_for_cancel
    ):
        with pytest.raises(asyncio.CancelledError):
            await view._run_ffmpeg(["ffmpeg"])  # 478-481 inner except pass, then re-raise
    proc.kill.assert_called_once()


# --------------------------------------------------------------------------- #
# _resolve_url (487-495) + _ffmpeg_binary (498)
# --------------------------------------------------------------------------- #
async def test_resolve_url_none_when_no_coordinator():
    view = _make_view()
    with patch(f"{P}.get_camera_coordinators", return_value={}):
        assert await view._resolve_url("missing-dev", "v1:e") is None  # 488-490


async def test_resolve_url_none_when_resolve_raises():
    view = _make_view()
    coord = SimpleNamespace(device_client=SimpleNamespace())
    with patch(f"{P}.get_camera_coordinators", return_value={"dev1": coord}), patch(
        f"{P}.async_resolve_event_url", AsyncMock(side_effect=RuntimeError("resolve boom"))
    ):
        assert await view._resolve_url("dev1", "v1:e") is None  # 491-495


def test_ffmpeg_binary_delegates_to_get_ffmpeg_binary():
    view = _make_view()
    with patch(f"{P}.get_ffmpeg_binary", return_value="/opt/ffmpeg") as g:
        assert view._ffmpeg_binary() == "/opt/ffmpeg"  # line 498
    g.assert_called_once_with(view.hass)


# --------------------------------------------------------------------------- #
# Blocking helpers (540, 588-589, 603-604)
# --------------------------------------------------------------------------- #
def test_ensure_dir_makes_dirs():
    with patch(f"{P}.os.makedirs") as md:
        _ensure_dir("/some/cache/dir")  # line 540
    md.assert_called_once_with("/some/cache/dir", exist_ok=True)


def test_enforce_cache_limits_skips_unstattable_file(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 10)
    # os.stat raising on the entry is swallowed and the file is skipped.
    with patch(f"{P}.os.stat", side_effect=OSError("vanished")):
        _enforce_cache_limits(str(tmp_path), 10**9, 10**9)  # 588-589 continue
    assert f.exists()


def test_enforce_cache_limits_size_evicts_oldest(tmp_path):
    import os
    import time

    now = time.time()
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x" * 100)
    os.utime(a, (now - 2, now - 2))
    b = tmp_path / "b.mp4"
    b.write_bytes(b"x" * 100)
    os.utime(b, (now - 1, now - 1))

    # Fresh mtimes (no age eviction); budget 150 < total 200 forces size eviction.
    _enforce_cache_limits(str(tmp_path), 150, 10**9)  # 603-604 unlink + total-=size

    assert not a.exists()  # oldest evicted until under budget
    assert b.exists()


@pytest.fixture(autouse=True)
def _clean_event_locks():
    yield
    _EVENT_LOCKS.clear()
    _EVENT_LOCK_REFS.clear()
