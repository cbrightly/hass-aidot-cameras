"""Tests for the AiDot event-clip transcode proxy helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock


import urllib.parse

from custom_components.aidot.proxy import (
    _URL_TTL,
    _cache_name,
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
            async_get_event_video_url=AsyncMock(return_value="https://cdn/should-not-use"),
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
    assert not _verify_sig(q["device"], q["event"], "not-a-number", q["sig"], now=1000.0)
