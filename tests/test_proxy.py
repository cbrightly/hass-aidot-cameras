"""Tests for the AiDot event-clip transcode proxy helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock


from custom_components.aidot.proxy import (
    _cache_name,
    _safe_event,
    _short,
    async_resolve_event_url,
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
