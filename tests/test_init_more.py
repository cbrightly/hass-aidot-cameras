"""Remaining __init__.py branches: serve-port-base option, media-source
registration, and the options-reload listener."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot import (
    _async_reload_on_options,
    async_setup_entry,
)
from custom_components.aidot.const import CONF_SERVE_PORT_BASE, DOMAIN

MOCK_LOGIN_INFO = {
    "id": "test-user-id-123",
    "username": "test@example.com",
    "password": "correct-password",
    "country_code": "US",
    "accessToken": "fake-token",
}


def _coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_cleanup = AsyncMock()
    return coordinator


async def test_setup_applies_serve_port_base_option(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data=MOCK_LOGIN_INFO,
        options={CONF_SERVE_PORT_BASE: 19123},
    )
    entry.add_to_hass(hass)
    os.environ.pop("AIDOT_SERVE_PORT_BASE", None)
    with patch(
        "custom_components.aidot.AidotDeviceManagerCoordinator",
        return_value=_coordinator(),
    ), patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        assert await async_setup_entry(hass, entry) is True
    assert os.environ["AIDOT_SERVE_PORT_BASE"] == "19123"


async def test_setup_registers_media_source(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data=MOCK_LOGIN_INFO,
        options={},
    )
    entry.add_to_hass(hass)
    # The setup block only runs when the media_source component is loaded.
    hass.config.components.add("media_source")
    hass.data.pop("media_source", None)
    with patch(
        "custom_components.aidot.AidotDeviceManagerCoordinator",
        return_value=_coordinator(),
    ), patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        assert await async_setup_entry(hass, entry) is True
    assert DOMAIN in hass.data["media_source"]


async def test_reload_on_options_reloads_entry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_LOGIN_INFO)
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock()
    ) as reload:
        await _async_reload_on_options(hass, entry)
    reload.assert_awaited_once_with(entry.entry_id)
