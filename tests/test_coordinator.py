"""Tests for AidotDeviceManagerCoordinator's token-refresh persistence.

Focused, lightweight unit coverage for token_fresh_cb: it must persist a
JSON-safe view of login_info, not a shallow .copy() of the live dict - see
the docstring on the method itself for why a shallow copy is not enough.
"""

from unittest.mock import MagicMock

from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator


def _make_coordinator() -> AidotDeviceManagerCoordinator:
    """Build a coordinator with token_fresh_cb's dependencies mocked out,
    bypassing DataUpdateCoordinator.__init__ (no real hass lifecycle needed
    to exercise this one method).
    """
    coord = object.__new__(AidotDeviceManagerCoordinator)
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.client = MagicMock()
    return coord


def test_token_fresh_cb_persists_the_json_safe_view_not_a_raw_copy():
    # login_info doubles as the account-shared cache for the persistent-MQTT
    # connection and its guarding asyncio.Lock - a plain .copy() is shallow,
    # so the same live Lock would end up in config_entry.data, which HA later
    # serializes to JSON when persisting config entries to disk. Must use
    # serializable_login_info() instead.
    coord = _make_coordinator()
    coord.client.serializable_login_info.return_value = {"access_token": "abc"}
    coord.token_fresh_cb()
    coord.client.serializable_login_info.assert_called_once()
    coord.hass.config_entries.async_update_entry.assert_called_once_with(
        coord.config_entry, data={"access_token": "abc"}
    )


def test_token_fresh_cb_does_not_touch_login_info_directly():
    # Regression guard for the original bug: login_info.copy() must not be
    # what gets persisted.
    coord = _make_coordinator()
    coord.client.serializable_login_info.return_value = {"access_token": "abc"}
    coord.token_fresh_cb()
    coord.client.login_info.copy.assert_not_called()
