"""Tests for the AiDot cloud-recording media source.

The source only touches ``item.identifier`` and the camera coordinators, so we
drive it with a SimpleNamespace "item", patch ``get_camera_coordinators`` to a
fixed dict, and patch ``sign_playback_url`` / ``async_prewarm_events`` so no real
signing or background transcode runs.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.media_source import Unresolvable

from custom_components.aidot.media_source import AidotMediaSource


def _source() -> AidotMediaSource:
    return AidotMediaSource(MagicMock())


def _item(identifier):
    return SimpleNamespace(identifier=identifier)


def _patch_coords(mapping):
    return patch(
        "custom_components.aidot.media_source.get_camera_coordinators",
        return_value=mapping,
    )


# --------------------------------------------------------------------------- #
# async_resolve_media
# --------------------------------------------------------------------------- #
async def test_resolve_media_rejects_malformed_identifier():
    src = _source()
    with _patch_coords({}):
        with pytest.raises(Unresolvable):
            await src.async_resolve_media(_item("no-slash-here"))


async def test_resolve_media_rejects_unknown_camera():
    src = _source()
    with _patch_coords({}):  # device not present
        with pytest.raises(Unresolvable):
            await src.async_resolve_media(_item("dev1/v1:abc"))


async def test_resolve_media_returns_signed_mp4_for_known_camera():
    src = _source()
    with _patch_coords({"dev1": MagicMock()}), patch(
        "custom_components.aidot.media_source.sign_playback_url",
        return_value="/api/aidot/video?signed",
    ) as sign:
        result = await src.async_resolve_media(_item("dev1/v1:abc"))
    assert result.url == "/api/aidot/video?signed"
    assert result.mime_type == "video/mp4"
    sign.assert_called_once_with("dev1", "v1:abc")


async def test_resolve_media_splits_only_on_first_slash():
    """An event id can itself contain a slash; only the first separates device."""
    src = _source()
    with _patch_coords({"dev1": MagicMock()}), patch(
        "custom_components.aidot.media_source.sign_playback_url",
        return_value="/signed",
    ) as sign:
        await src.async_resolve_media(_item("dev1/v1:abc/extra"))
    sign.assert_called_once_with("dev1", "v1:abc/extra")


# --------------------------------------------------------------------------- #
# async_browse_media
# --------------------------------------------------------------------------- #
def _coord_with_events(events, name="Cam One"):
    dc = MagicMock()
    dc.info = SimpleNamespace(name=name)
    dc.async_get_cloud_recordings = AsyncMock(return_value=events)
    return SimpleNamespace(device_client=dc)


async def test_browse_events_filters_to_events_with_video():
    """Only events with hasVideo AND an eventUuid become playable children."""
    events = [
        {"hasVideo": True, "eventUuid": "v1:a", "eventDesc": "Person",
         "eventTime": 1_700_000_000_000, "picUrl": "http://pic/a.jpg"},
        {"hasVideo": False, "eventUuid": "v1:b"},          # no video -> skipped
        {"hasVideo": True},                                 # no uuid -> skipped
        {"hasVideo": True, "eventUuid": "v1:c", "eventDesc": "Motion"},
    ]
    coord = _coord_with_events(events)
    src = _source()
    src.hass = MagicMock()
    with _patch_coords({"dev1": coord}), patch(
        "custom_components.aidot.media_source.async_prewarm_events",
        new=MagicMock(),
    ):
        result = await src.async_browse_media(_item("dev1"))

    ids = [c.identifier for c in result.children]
    assert ids == ["dev1/v1:a", "dev1/v1:c"]
    # Each child is a playable VIDEO leaf.
    assert all(c.can_play and not c.can_expand for c in result.children)
    # The directory itself is titled after the camera.
    assert result.title == "Cam One"
    assert result.can_expand and not result.can_play


async def test_browse_events_schedules_prewarm_for_found_clips():
    """Found clips are pre-warmed in the background (best-effort)."""
    coord = _coord_with_events(
        [{"hasVideo": True, "eventUuid": "v1:a"}]
    )
    src = _source()
    src.hass = MagicMock()
    with _patch_coords({"dev1": coord}), patch(
        "custom_components.aidot.media_source.async_prewarm_events",
        new=MagicMock(),
    ) as warm:
        await src.async_browse_media(_item("dev1"))
    warm.assert_called_once_with("dev1", ["v1:a"])
    src.hass.async_create_background_task.assert_called_once()


async def test_browse_events_unknown_camera_raises():
    src = _source()
    src.hass = MagicMock()
    with _patch_coords({}):
        with pytest.raises(Exception):
            await src.async_browse_media(_item("missing"))


async def test_browse_root_lists_cameras_as_directories():
    """The root level lists each camera coordinator as an expandable directory."""
    coord = SimpleNamespace(
        device_client=SimpleNamespace(info=SimpleNamespace(name="Front Door"))
    )
    src = _source()
    with _patch_coords({"dev1": coord}):
        result = await src.async_browse_media(_item(""))
    assert [c.identifier for c in result.children] == ["dev1"]
    child = result.children[0]
    assert child.title == "Front Door"
    assert child.can_expand and not child.can_play
