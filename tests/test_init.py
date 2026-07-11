"""Config-entry setup/reload behaviour.

Regression coverage for the self-reload loop: the library persists a refreshed
token by writing it back into the config entry (coordinator.token_fresh_cb ->
async_update_entry). add_update_listener fires on that data-only write too, so an
unconditional reload churned every entity, re-primed the motion poll (dropping
events), and interrupted streams on every token refresh. The listener must reload
only on a real OPTIONS change.
"""
from unittest.mock import AsyncMock, MagicMock

from custom_components.aidot import _async_reload_on_options
from custom_components.aidot.const import DOMAIN


def _hass_with_options(entry_id: str, seeded: dict) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {f"options-{entry_id}": dict(seeded)}}
    hass.config_entries.async_reload = AsyncMock()
    return hass


async def test_reload_skips_data_only_updates():
    # A token persist (async_update_entry data write) leaves options unchanged.
    hass = _hass_with_options("e1", {"serve_port_base": 5000})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 5000})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()


async def test_reload_on_actual_options_change():
    hass = _hass_with_options("e1", {"serve_port_base": 5000})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 6000})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    # snapshot advanced so the next data-only write won't reload again
    assert hass.data[DOMAIN]["options-e1"] == {"serve_port_base": 6000}
