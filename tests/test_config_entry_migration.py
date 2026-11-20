"""The config entry migration that replaced the per-start cleanups.

``fast_connect`` used to be read on every start as a fallback for an entry that
never saved ``connection_mode``; the entities 2.11.x withdrew were searched for
on every start too. Both now happen once, in async_migrate_entry (1.1 -> 1.2).
"""

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot import async_migrate_entry
from custom_components.aidot.config_flow import AidotConfigFlow
from custom_components.aidot.const import DOMAIN, resolve_connection_mode


def _entry(hass: HomeAssistant, options: dict, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options=options, **kw)
    entry.add_to_hass(hass)
    return entry


def test_the_flow_is_at_the_version_the_migration_writes():
    assert (AidotConfigFlow.VERSION, AidotConfigFlow.MINOR_VERSION) == (1, 3)


@pytest.mark.parametrize(("fast", "mode"), [(True, "lan_direct"), (False, "relay")])
async def test_fast_connect_keeps_its_meaning(hass: HomeAssistant, fast, mode):
    entry = _entry(hass, {"fast_connect": fast, "serve_port_base": 18900})
    assert await async_migrate_entry(hass, entry) is True
    assert entry.options == {"connection_mode": mode, "serve_port_base": 18900}
    assert entry.minor_version == 3
    assert resolve_connection_mode(entry.options) == mode


async def test_a_saved_connection_mode_wins_over_fast_connect(hass: HomeAssistant):
    entry = _entry(hass, {"fast_connect": True, "connection_mode": "relay"})
    assert await async_migrate_entry(hass, entry) is True
    assert entry.options == {"connection_mode": "relay"}


async def test_an_entry_without_fast_connect_keeps_its_options(hass: HomeAssistant):
    entry = _entry(hass, {"connection_mode": "lan_direct"})
    assert await async_migrate_entry(hass, entry) is True
    assert entry.options == {"connection_mode": "lan_direct"}
    assert entry.minor_version == 3


async def test_the_withdrawn_entities_go_with_the_migration(hass: HomeAssistant):
    entry = _entry(hass, {})
    reg = er.async_get(hass)
    gone = [
        reg.async_get_or_create("switch", DOMAIN, "cam1_siren"),
        reg.async_get_or_create("switch", DOMAIN, "cam1_floodlight"),
        reg.async_get_or_create("select", DOMAIN, "cam1_resolution"),
    ]
    kept = reg.async_get_or_create("select", DOMAIN, "cam1_night_vision")
    theirs = reg.async_get_or_create("switch", "other", "cam9_siren")

    assert await async_migrate_entry(hass, entry) is True

    assert all(reg.async_get(e.entity_id) is None for e in gone)
    assert reg.async_get(kept.entity_id) is not None
    assert reg.async_get(theirs.entity_id) is not None


async def test_a_migrated_entry_is_left_alone(hass: HomeAssistant):
    entry = _entry(hass, {"fast_connect": True}, minor_version=3)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.options == {"fast_connect": True}


async def test_a_newer_major_version_is_refused(hass: HomeAssistant):
    entry = _entry(hass, {}, version=2)
    assert await async_migrate_entry(hass, entry) is False


def test_no_mode_stored_means_the_default():
    assert resolve_connection_mode({}) == "relay"
    assert resolve_connection_mode({"connection_mode": "bogus"}) == "relay"
