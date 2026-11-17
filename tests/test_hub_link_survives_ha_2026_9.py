"""Linking a device to the account hub must not use a parameter HA removed.

Home Assistant 2026.9 dropped `via_device` from `DeviceInfo` in favour of
`via_device_id`, and turned the deprecation into a hard `RuntimeError` raised
from inside `device_registry.async_get_or_create`. On the box that took out 50
entities across 11 domains - six cameras among them - with

    Error adding entity None for domain camera with platform aidot
    RuntimeError: Detected code that calls `device_registry.async_get_or_create`
    with a deprecated `via_device` parameter; use `via_device_id` instead

The two spellings are not interchangeable: `via_device` takes an IDENTIFIER
TUPLE `(DOMAIN, config_entry_id)`, `via_device_id` takes the hub's REGISTRY ID.
Passing one where the other belongs links nothing.

The support flag is a parameter rather than something read from the installed
Home Assistant, because the test environment is pinned well behind the versions
users run - 2026.2.3 here against 2026.9.2 on the box - so a test that asked the
local HA would only ever exercise one branch, which is exactly how this reached
a user.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.aidot.const import DOMAIN
from custom_components.aidot.entity import _via_device_extra


def test_new_ha_gets_the_registry_id():
    assert _via_device_extra("entry1", "hubdev9", supports_via_device_id=True) == {
        "via_device_id": "hubdev9"
    }


def test_old_ha_still_gets_the_identifier_tuple():
    """Users on older Home Assistant must keep working."""
    assert _via_device_extra("entry1", "hubdev9", supports_via_device_id=False) == {
        "via_device": (DOMAIN, "entry1")
    }


def test_new_ha_never_falls_back_to_the_removed_key():
    """The fallback is what raised; it must not survive as a last resort."""
    got = _via_device_extra("entry1", None, supports_via_device_id=True)
    assert "via_device" not in got


def test_an_unresolvable_hub_links_nothing_rather_than_raising():
    """Losing the grouping is a cosmetic loss; raising takes the entity out."""
    assert _via_device_extra("entry1", None, supports_via_device_id=True) == {}


def test_no_entry_links_nothing():
    assert _via_device_extra(None, "hubdev9", supports_via_device_id=True) == {}
    assert _via_device_extra(None, None, supports_via_device_id=False) == {}


def test_the_installed_ha_decides_which_spelling_is_used():
    """The module-level flag must reflect the HA actually running, not a guess."""
    from homeassistant.helpers.device_registry import DeviceInfo

    from custom_components.aidot.entity import _SUPPORTS_VIA_DEVICE_ID

    assert _SUPPORTS_VIA_DEVICE_ID == ("via_device_id" in DeviceInfo.__annotations__)
