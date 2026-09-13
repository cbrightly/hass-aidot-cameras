"""A failed hub-id stash must cost the grouping, not the config entry.

`async_setup_entry` caches the hub device's registry id on `entry.runtime_data`
so entities can link to it under Home Assistant 2026.9. The assignment is
wrapped in try/except precisely because the runtime_data shape is not ours to
assume - but the handler called `_LOGGER`, a name that module never defines.

So the guard inverted its own purpose: instead of surviving a failed stash, it
raised `NameError` out of setup BEFORE `async_forward_entry_setups`, which would
take the entry and every aidot entity down - the exact outage the hub-id work
was written to fix. Unreachable with today's unslotted coordinator, and shipped
in two releases on the strength of a green test suite while the repo's own lint
gate was red on `F821`.

The test drives the real `async_setup_entry` with a runtime_data object that
cannot take the attribute, and asserts setup still reaches the platform forward.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.aidot import DOMAIN, async_setup_entry


class _RefusesTheStash:
    """Stands in for a runtime_data object that will not take the stash.

    `__slots__ = ()` makes instance-attribute assignment raise AttributeError -
    the real shape of the failure the try/except exists for. Everything setup
    legitimately calls is a CLASS attribute, so only the stash is refused.
    """

    __slots__ = ()

    async def async_config_entry_first_refresh(self):
        return None

    async def async_cleanup(self):
        return None


async def test_a_runtime_data_that_refuses_the_stash_still_sets_up(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)

    manager = _RefusesTheStash()
    forwarded = AsyncMock()
    with patch("custom_components.aidot.AidotDeviceManagerCoordinator", return_value=manager), \
            patch("custom_components.aidot.AidotMotionNotifier", return_value=MagicMock()), \
            patch("custom_components.aidot._migrate_relocated_camera_entities"), \
            patch.object(hass.config_entries, "async_forward_entry_setups", forwarded):
        assert await async_setup_entry(hass, entry) is True
        await hass.async_block_till_done()

    forwarded.assert_awaited_once()
