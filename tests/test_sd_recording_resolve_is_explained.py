"""Clicking an on-device SD recording must explain, not throw a raw error.

SD recordings are listed for reference (stage 2), but pulling a clip off the
card is not something this integration can do - stage 3 was closed as not
feasible; only the AiDot app can play them. The browse leaves are built
can_play=False for that reason, but Home Assistant 2026.9's media browser still
issues a resolve on a VIDEO-class leaf when it is clicked, and the resolver -
which only understands the cloud `device/uuid` (slash) form - answered with a
bare `Invalid identifier: '<id>|sd|<day>|<n>'`. That tells a user nothing.

An SD identifier should resolve to a clear, actionable Unresolvable: these are
reference listings, watch them in the app. A genuinely malformed identifier
still gets the generic error.
"""

import os
import sys

import pytest
from homeassistant.components.media_source.error import Unresolvable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.aidot.media_source import (
    SOURCE_CLOUD,
    SOURCE_SD,
    AidotMediaSource,
)


class _Item:
    def __init__(self, identifier):
        self.identifier = identifier


def _source():
    return AidotMediaSource(hass=None)


def _msg(exc):
    return str(exc.value)


async def test_an_sd_clip_click_explains_instead_of_invalid_identifier():
    src = _source()
    ident = f"338603b50fce46ef8d2545fc7362c967|{SOURCE_SD}|2026-09-16|1"
    with pytest.raises(Unresolvable) as exc:
        await src.async_resolve_media(_Item(ident))
    m = _msg(exc)
    assert "Invalid identifier" not in m, (
        "the cryptic message must be gone for SD clips"
    )
    assert "app" in m.lower(), "the message should point the user to the AiDot app"


async def test_the_hour_bucket_identifier_is_also_explained_not_cryptic():
    src = _source()
    ident = f"338603b50fce46ef8d2545fc7362c967|{SOURCE_SD}|2026-09-16|h14"
    with pytest.raises(Unresolvable) as exc:
        await src.async_resolve_media(_Item(ident))
    assert "Invalid identifier" not in _msg(exc)


async def test_a_genuinely_malformed_identifier_still_gets_the_generic_error():
    src = _source()
    with pytest.raises(Unresolvable) as exc:
        await src.async_resolve_media(_Item("garbage-no-slash-no-sd"))
    assert "Invalid identifier" in _msg(exc)


async def test_a_cloud_info_node_is_explained_not_cryptic():
    """Tapping "No cloud recordings" resolves too (the frontend resolves any
    non-expandable leaf); it must not throw the developer error."""
    src = _source()
    ident = f"338603b50fce46ef8d2545fc7362c967|{SOURCE_CLOUD}|none"
    with pytest.raises(Unresolvable) as exc:
        await src.async_resolve_media(_Item(ident))
    m = _msg(exc)
    assert "Invalid identifier" not in m
    assert "listing" in m.lower() or "recording" in m.lower()


async def test_an_empty_sd_folder_node_is_not_told_to_open_a_clip():
    """The empty "no SD recordings" node should not claim there is a clip to
    open in the app - it is a listing message, not a clip message."""
    src = _source()
    ident = f"338603b50fce46ef8d2545fc7362c967|{SOURCE_SD}|none"
    with pytest.raises(Unresolvable) as exc:
        await src.async_resolve_media(_Item(ident))
    assert "Invalid identifier" not in _msg(exc)


async def test_the_cloud_clip_slash_form_is_left_to_the_player_path():
    """A real cloud clip identifier (device/uuid) must NOT be diverted - it must
    reach the camera lookup, not the listing/SD message."""
    from unittest.mock import patch

    src = _source()
    with patch(
        "custom_components.aidot.media_source.get_camera_coordinators",
        return_value={},
    ):
        with pytest.raises(Unresolvable) as exc:
            await src.async_resolve_media(
                _Item("338603b50fce46ef8d2545fc7362c967/v1:abc")
            )
    m = _msg(exc)
    # It got as far as the camera lookup (the slash path), not diverted.
    assert "not found" in m.lower()
    assert "listing" not in m.lower() and "SD card" not in m
