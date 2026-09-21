"""Tests for the AiDot event-clip transcode proxy helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock


import urllib.parse

from custom_components.aidot import proxy
from custom_components.aidot.proxy import (
    _URL_TTL,
    AidotVideoProxyView,
    _cache_name,
    _clip_error_response,
    _safe_event,
    _short,
    _verify_sig,
    async_resolve_event_url,
    sign_playback_url,
)


def test_safe_event_keeps_colon_event_ids():
    # Event uuids look like "v1:<uuid>" - the colon is significant and must
    # survive (a past bug rejected it, 400-ing every clip).
    eid = "v1:91477a47-e000-4160-a937-e1900c01ee43"
    assert _safe_event(eid) == eid


def test_safe_event_rejects_traversal_and_empty():
    assert _safe_event("") is None
    assert _safe_event("../secret") is None
    assert _safe_event("a/b") is None
    assert _safe_event("a\\b") is None
    assert _safe_event("x" * 201) is None


def test_cache_name_is_filesystem_safe():
    # Colon (and any non [alnum-_.]) becomes "_" for the on-disk cache filename.
    assert _cache_name("v1:91477a47-abc") == "v1_91477a47-abc"
    assert "/" not in _cache_name("a/b:c")
    assert ":" not in _cache_name("v1:abc")


def test_short_decodes_and_truncates():
    assert _short(b"  boom  ") == "boom"
    assert _short(None) == ""
    assert len(_short(b"x" * 1000)) == 300


async def test_resolve_event_url_prefers_media_then_url():
    # New library: async_get_event_video_media returns (url, mime).
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(
                return_value=("https://cdn/x.m3u8", "application/x-mpegURL")
            ),
            async_get_event_video_url=AsyncMock(
                return_value="https://cdn/should-not-use"
            ),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") == "https://cdn/x.m3u8"


async def test_resolve_event_url_falls_back_to_url_only():
    # Older library: only async_get_event_video_url exists.
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_url=AsyncMock(return_value="https://cdn/y.m3u8"),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") == "https://cdn/y.m3u8"


async def test_resolve_event_url_none_when_unavailable():
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(return_value=None),
            async_get_event_video_url=AsyncMock(return_value=None),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") is None


# --------------------------------------------------------------------------- #
# Signed playback URLs
# --------------------------------------------------------------------------- #
def _params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_sign_playback_url_roundtrips():
    url = sign_playback_url("dev1", "v1:abc", now=1000.0)
    q = _params(url)
    assert q["device"] == "dev1"
    assert q["event"] == "v1:abc"
    assert int(q["exp"]) == int(1000.0 + _URL_TTL)
    # The signature it produced verifies before expiry...
    assert _verify_sig(q["device"], q["event"], q["exp"], q["sig"], now=1000.0)
    # ...and is rejected once expired.
    assert not _verify_sig(
        q["device"], q["event"], q["exp"], q["sig"], now=int(q["exp"]) + 1
    )


def test_verify_sig_rejects_tampered_device_event_and_bad_sig():
    url = sign_playback_url("dev1", "v1:abc", now=1000.0)
    q = _params(url)
    # Swapping the device or event invalidates the signature (it's bound to both).
    assert not _verify_sig("dev2", q["event"], q["exp"], q["sig"], now=1000.0)
    assert not _verify_sig(q["device"], "v1:other", q["exp"], q["sig"], now=1000.0)
    # A forged / empty signature is rejected.
    assert not _verify_sig(q["device"], q["event"], q["exp"], "deadbeef", now=1000.0)
    assert not _verify_sig(q["device"], q["event"], q["exp"], "", now=1000.0)


def test_verify_sig_rejects_nonnumeric_exp():
    url = sign_playback_url("dev1", "v1:abc", now=1000.0)
    q = _params(url)
    assert not _verify_sig(
        q["device"], q["event"], "not-a-number", q["sig"], now=1000.0
    )


# --------------------------------------------------------------------------- #
# "clip not ready yet" vs a genuine transcode failure
#
# A motion push fires the instant motion is detected; the recorded clip takes a
# few seconds to finish uploading to the cloud. A user tapping the push right
# away used to get a bare 502 "clip transcode failed" - when the URL had not
# resolved and no transcode was even attempted. That is a transient, retriable
# condition and must read as one, distinct from ffmpeg actually failing.
# --------------------------------------------------------------------------- #
def _make_view(tmp_path):
    async def _exec(fn, *a):
        return fn(*a)

    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *a: str(tmp_path)),
        async_add_executor_job=_exec,
    )
    return AidotVideoProxyView(hass)


class _FakeReq:
    def __init__(self, query, ua="Mozilla/5.0 (X11) Chrome/120"):
        self.query = query
        self.headers = {"User-Agent": ua}


def test_not_ready_maps_to_a_retriable_503():
    r = _clip_error_response("not_ready")
    assert r.status == 503
    assert r.headers.get("Retry-After")
    assert "not ready" in r.text.lower()
    assert "transcode failed" not in r.text.lower()


def test_a_genuine_encode_failure_maps_to_502():
    r = _clip_error_response("failed")
    assert r.status == 502
    assert "clip transcode failed" in r.text


async def test_get_returns_a_retriable_503_when_the_clip_is_not_ready(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="not_ready")
    q = _params(sign_playback_url("dev", "v1:e", now=1000.0))
    resp = await v.get(_FakeReq(q))
    assert resp.status == 503
    assert "not ready" in resp.text.lower()
    assert resp.headers.get("Retry-After")
    assert "transcode failed" not in resp.text.lower()


async def test_get_still_502s_a_genuine_transcode_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="failed")
    q = _params(sign_playback_url("dev", "v1:e", now=1000.0))
    resp = await v.get(_FakeReq(q))
    assert resp.status == 502
    assert "clip transcode failed" in resp.text


async def test_resolve_when_ready_returns_once_the_clip_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "_CLIP_READY_DELAY_S", 0)
    monkeypatch.setattr(proxy, "_CLIP_READY_RETRIES", 5)
    v = _make_view(tmp_path)
    v._resolve_url = AsyncMock(side_effect=[None, None, "https://cdn/x.m3u8"])
    assert await v._resolve_when_ready("dev", "v1:e") == "https://cdn/x.m3u8"
    assert v._resolve_url.await_count == 3


async def test_resolve_when_ready_gives_up_after_the_bounded_retries(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(proxy, "_CLIP_READY_DELAY_S", 0)
    monkeypatch.setattr(proxy, "_CLIP_READY_RETRIES", 3)
    v = _make_view(tmp_path)
    v._resolve_url = AsyncMock(return_value=None)
    assert await v._resolve_when_ready("dev", "v1:e") is None
    assert v._resolve_url.await_count == 4  # 1 initial + 3 retries


async def test_resolve_when_ready_does_not_wait_when_the_clip_is_already_there(
    tmp_path, monkeypatch
):
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(proxy.asyncio, "sleep", _fake_sleep)
    v = _make_view(tmp_path)
    v._resolve_url = AsyncMock(return_value="https://cdn/x.m3u8")
    assert await v._resolve_when_ready("dev", "v1:e") == "https://cdn/x.m3u8"
    assert v._resolve_url.await_count == 1
    assert slept == []
