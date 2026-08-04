"""Proxy helper internals: cache-limit eviction, per-event lock refcounting, the
transcode-to-cache plan loop, and warm()'s early-outs.

ffmpeg, hardware probing and the filesystem finalize are fully stubbed (no real
ffmpeg, no HW probe), mirroring ``test_proxy_view.py``'s mocked-hass approach.
The age/size eviction is exercised against real temp files (``tmp_path``), which
is the cleanest way to validate the os.stat/os.listdir logic.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

from custom_components.aidot.proxy import (
    _EVENT_LOCK_REFS,
    _EVENT_LOCKS,
    _acquire_event_lock,
    _enforce_cache_limits,
    _finalize,
    _release_event_lock,
    AidotVideoProxyView,
)


def _mkfile(d, name: str, size: int, mtime: float):
    p = d / name
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


# --------------------------------------------------------------------------- #
# _enforce_cache_limits
# --------------------------------------------------------------------------- #
def test_enforce_age_evicts_old_but_keeps_keep(tmp_path):
    import time

    old = _mkfile(tmp_path, "old.mp4", 10, 1.0)          # mtime far in the past
    keepf = _mkfile(tmp_path, "keep.mp4", 10, 1.0)       # also old, but kept
    fresh = _mkfile(tmp_path, "fresh.mp4", 10, time.time())

    # Huge size budget so only the age rule fires; 10s max age.
    _enforce_cache_limits(str(tmp_path), 10**9, 10.0, keep=str(keepf))

    assert not old.exists()   # aged out
    assert keepf.exists()     # the kept path is never aged out (TOCTOU guard)
    assert fresh.exists()     # within max age


def test_enforce_size_evicts_oldest_skipping_keep(tmp_path):
    old = _mkfile(tmp_path, "old.mp4", 100, 1.0)
    mid = _mkfile(tmp_path, "mid.mp4", 100, 2.0)
    new = _mkfile(tmp_path, "new.mp4", 100, 3.0)

    # Huge max age so the size rule alone fires; budget 150 < total 300.
    # keep = the oldest file: it must survive even though it would be evicted first.
    _enforce_cache_limits(str(tmp_path), 150, 10**9, keep=str(old))

    assert old.exists()       # kept though it's the oldest (skipped by `keep`)
    assert not mid.exists()   # evicted (oldest non-kept)
    assert not new.exists()   # evicted next, until under budget


# --------------------------------------------------------------------------- #
# _acquire_event_lock / _release_event_lock refcounting
# --------------------------------------------------------------------------- #
def test_event_lock_refcount_lifecycle():
    key = "test-refcount:hevc"
    _EVENT_LOCKS.pop(key, None)
    _EVENT_LOCK_REFS.pop(key, None)
    try:
        l1 = _acquire_event_lock(key)
        l2 = _acquire_event_lock(key)
        # Same lock object both times; two refs hold it in the table.
        assert l1 is l2
        assert key in _EVENT_LOCKS
        assert _EVENT_LOCK_REFS[key] == 2

        _release_event_lock(key)
        assert key in _EVENT_LOCKS        # one holder remains
        assert _EVENT_LOCK_REFS[key] == 1

        _release_event_lock(key)
        # Last holder left: lock + refcount entry are removed.
        assert key not in _EVENT_LOCKS
        assert key not in _EVENT_LOCK_REFS
    finally:
        _EVENT_LOCKS.pop(key, None)
        _EVENT_LOCK_REFS.pop(key, None)


# --------------------------------------------------------------------------- #
# _transcode_to_cache
# --------------------------------------------------------------------------- #
def _make_view() -> AidotVideoProxyView:
    hass = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/aidot_clips")
    return AidotVideoProxyView(hass)


async def test_transcode_success_finalizes_into_cache():
    view = _make_view()
    # Every executor probe reports success (_is_usable True); _finalize is a no-op
    # mock but we assert it WAS scheduled with the os.replace path.
    view.hass.async_add_executor_job = AsyncMock(return_value=True)
    view._resolve_url = AsyncMock(return_value="https://cdn/x.m3u8")
    view._detect_hw = AsyncMock(return_value=None)  # no HW probe / no real ffmpeg
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    view._run_ffmpeg = AsyncMock(return_value=(0, b""))

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/aidot_clips/x.mp4")

    assert ok is True
    view._run_ffmpeg.assert_awaited_once()
    # The finished .part is promoted into the cache via _finalize (os.replace).
    called_fns = [c.args[0] for c in view.hass.async_add_executor_job.call_args_list]
    assert _finalize in called_fns


async def test_transcode_all_plans_fail_returns_false():
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=False)
    view._resolve_url = AsyncMock(return_value="https://cdn/x.m3u8")
    view._detect_hw = AsyncMock(return_value=None)
    view._ffmpeg_binary = MagicMock(return_value="ffmpeg")
    view._run_ffmpeg = AsyncMock(return_value=(1, b"boom"))

    ok = await view._transcode_to_cache("dev1", "v1:e", "/tmp/aidot_clips/x.mp4")

    assert ok is False
    # All plans failed once, so the URL is re-resolved once (expired-CDN retry).
    assert view._resolve_url.await_count == 2


# --------------------------------------------------------------------------- #
# warm()
# --------------------------------------------------------------------------- #
async def test_warm_returns_early_when_already_cached():
    view = _make_view()
    # _touch_if_usable reports a cache hit, so warm short-circuits before transcoding.
    view.hass.async_add_executor_job = AsyncMock(return_value=True)
    view._transcode_to_cache = AsyncMock()

    await view.warm("dev1", "v1:abc")

    view._transcode_to_cache.assert_not_awaited()


async def test_warm_skips_when_lock_held():
    view = _make_view()
    view.hass.async_add_executor_job = AsyncMock(return_value=False)  # not cached
    view._transcode_to_cache = AsyncMock()

    event = "v1:lockheld"
    key = f"{event}:hevc"
    held = asyncio.Lock()
    await held.acquire()
    _EVENT_LOCKS[key] = held
    _EVENT_LOCK_REFS[key] = 1
    try:
        await view.warm("dev1", event)
    finally:
        held.release()
        _EVENT_LOCKS.pop(key, None)
        _EVENT_LOCK_REFS.pop(key, None)

    # A play/another warm already holds the lock, so this warm declines the work.
    view._transcode_to_cache.assert_not_awaited()
