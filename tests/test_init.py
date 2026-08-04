"""Tests for the Aidot integration setup/unload and the entity migration.

Mirrors the config-flow tests' use of the phacc ``hass`` fixture + MockConfigEntry
and patches the manager coordinator so no real network/MQTT runs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot import (
    PLATFORMS,
    _migrate_relocated_camera_entities,
    async_setup_entry,
    async_unload_entry,
)

DOMAIN = "aidot"

MOCK_LOGIN_INFO = {
    "id": "test-user-id-123",
    "username": "test@example.com",
    "password": "correct-password",
    "country_code": "US",
    "accessToken": "fake-token",
    "mqttPassword": "fake-mqtt-pw",
}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data=MOCK_LOGIN_INFO,
        options={},
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_cleanup = AsyncMock()
    return coordinator


async def test_setup_entry_forwards_platforms(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    coordinator = _coordinator()
    with patch(
        "custom_components.aidot.AidotDeviceManagerCoordinator",
        return_value=coordinator,
    ), patch.object(
        hass.config_entries, "async_forward_entry_setups", AsyncMock()
    ) as fwd:
        assert await async_setup_entry(hass, entry) is True

    coordinator.async_config_entry_first_refresh.assert_awaited_once()
    fwd.assert_awaited_once_with(entry, PLATFORMS)
    assert entry.runtime_data is coordinator


async def test_unload_entry_cleans_up(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    coordinator = _coordinator()
    entry.runtime_data = coordinator
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ) as unl:
        assert await async_unload_entry(hass, entry) is True

    unl.assert_awaited_once_with(entry, PLATFORMS)
    coordinator.async_cleanup.assert_awaited_once()


async def test_unload_failure_skips_cleanup(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    coordinator = _coordinator()
    entry.runtime_data = coordinator
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    ):
        assert await async_unload_entry(hass, entry) is False

    coordinator.async_cleanup.assert_not_awaited()


async def test_migrate_removes_old_switch_siren_and_floodlight(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    reg = er.async_get(hass)

    old_siren = reg.async_get_or_create(
        "switch", DOMAIN, "cam1_siren", config_entry=entry
    )
    old_flood = reg.async_get_or_create(
        "switch", DOMAIN, "cam1_floodlight", config_entry=entry
    )
    # These must survive: an unrelated switch and the new-domain entities that
    # reuse the same unique_id in siren/light.
    keep_switch = reg.async_get_or_create(
        "switch", DOMAIN, "cam1_motion_detection", config_entry=entry
    )
    keep_new_siren = reg.async_get_or_create(
        "siren", DOMAIN, "cam1_siren", config_entry=entry
    )
    keep_new_flood = reg.async_get_or_create(
        "light", DOMAIN, "cam1_floodlight", config_entry=entry
    )

    _migrate_relocated_camera_entities(hass)

    assert reg.async_get(old_siren.entity_id) is None
    assert reg.async_get(old_flood.entity_id) is None
    assert reg.async_get(keep_switch.entity_id) is not None
    assert reg.async_get(keep_new_siren.entity_id) is not None
    assert reg.async_get(keep_new_flood.entity_id) is not None
