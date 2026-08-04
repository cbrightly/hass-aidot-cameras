"""Tests for the manager coordinator's auth / re-login / per-device init paths.

Mirrors ``test_coordinator.py`` / ``test_coordinator_sync.py``: the methods under
test touch only a handful of attributes, so we build a bare manager via
``__new__`` and set exactly what each method reads - no hass lifecycle, no real
network, no ``AidotClient``.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aidot.const import (
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_MODEL_ID,
)
from aidot.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.aidot.coordinator import (
    AidotDeviceManagerCoordinator,
    get_camera_coordinators,
)


def _bare() -> AidotDeviceManagerCoordinator:
    return AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)


_CAMERA = {CONF_ID: "c1", CONF_MODEL_ID: "c1-IPC"}
_LIGHT = {CONF_ID: "l1", "type": "light", CONF_AES_KEY: ["k"]}


def _update_data_scaffold(mgr: AidotDeviceManagerCoordinator) -> None:
    """Wire the non-auth collaborators _async_update_data needs after the fetch."""
    mgr._sync_light_coordinators = MagicMock()
    mgr._sync_camera_coordinators = MagicMock()
    mgr._dev_fetch_lock = asyncio.Lock()
    mgr.camera_coordinators = {}
    mgr.hass = SimpleNamespace(
        loop=SimpleNamespace(time=MagicMock(return_value=2000.0))
    )


# --------------------------------------------------------------------------- #
# _async_setup - bad credentials surface a reauth prompt
# --------------------------------------------------------------------------- #
async def test_async_setup_bad_credentials_raises_reauth():
    mgr = _bare()
    mgr.async_auto_login = AsyncMock(side_effect=AidotUserOrPassIncorrect())
    with pytest.raises(ConfigEntryAuthFailed):
        await mgr._async_setup()
    mgr.async_auto_login.assert_awaited_once()


async def test_async_setup_success_no_raise():
    mgr = _bare()
    mgr.async_auto_login = AsyncMock()
    await mgr._async_setup()  # must not raise
    mgr.async_auto_login.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _async_update_data - AidotAuthFailed handling
# --------------------------------------------------------------------------- #
async def test_update_data_auth_failed_no_ensure_token_reauth():
    """Token expired and the library has no async_ensure_token -> reauth."""
    mgr = _bare()
    # SimpleNamespace (not MagicMock) so the missing attr resolves to None via
    # getattr default, exercising the ``_ensure is None`` branch.
    mgr.client = SimpleNamespace(
        async_get_all_device=AsyncMock(side_effect=AidotAuthFailed())
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await mgr._async_update_data()


async def test_update_data_auth_failed_ensure_token_false_reauth():
    """async_ensure_token returns False (couldn't re-login) -> reauth."""
    mgr = _bare()
    mgr.client = SimpleNamespace(
        async_get_all_device=AsyncMock(side_effect=AidotAuthFailed()),
        async_ensure_token=AsyncMock(return_value=False),
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await mgr._async_update_data()
    mgr.client.async_ensure_token.assert_awaited_once()


async def test_update_data_headless_relogin_success_proceeds():
    """A headless re-login succeeds, so the second fetch drives the normal path."""
    mgr = _bare()
    mgr.client = SimpleNamespace(
        async_get_all_device=AsyncMock(
            side_effect=[AidotAuthFailed(), {CONF_DEVICE_LIST: [_CAMERA, _LIGHT]}]
        ),
        async_ensure_token=AsyncMock(return_value=True),
    )
    _update_data_scaffold(mgr)

    await mgr._async_update_data()

    mgr.client.async_ensure_token.assert_awaited_once()
    assert mgr.client.async_get_all_device.await_count == 2
    mgr._sync_light_coordinators.assert_called_once_with({"l1": _LIGHT})
    mgr._sync_camera_coordinators.assert_called_once_with({"c1": _CAMERA})
    assert mgr._dev_cache == {"c1": _CAMERA}
    assert mgr._dev_cache_ts == 2000.0


async def test_update_data_relogin_then_second_auth_failure_reauth():
    """Re-login reported success but the retried fetch still 401s -> reauth."""
    mgr = _bare()
    mgr.client = SimpleNamespace(
        async_get_all_device=AsyncMock(
            side_effect=[AidotAuthFailed(), AidotAuthFailed()]
        ),
        async_ensure_token=AsyncMock(return_value=True),
    )
    _update_data_scaffold(mgr)
    with pytest.raises(ConfigEntryAuthFailed):
        await mgr._async_update_data()
    assert mgr.client.async_get_all_device.await_count == 2


# --------------------------------------------------------------------------- #
# _async_init_coordinator - setup vs runtime branches + exception wrap
# --------------------------------------------------------------------------- #
async def test_init_coordinator_setup_in_progress_uses_first_refresh():
    mgr = _bare()
    mgr.config_entry = SimpleNamespace(state=ConfigEntryState.SETUP_IN_PROGRESS)
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord._async_setup = AsyncMock()
    coord.async_refresh = AsyncMock()

    await mgr._async_init_coordinator(coord, is_camera=False)

    coord.async_config_entry_first_refresh.assert_awaited_once()
    coord._async_setup.assert_not_awaited()
    coord.async_refresh.assert_not_awaited()


async def test_init_coordinator_runtime_loaded_runs_setup_then_refresh():
    mgr = _bare()
    mgr.config_entry = SimpleNamespace(state=ConfigEntryState.LOADED)
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord._async_setup = AsyncMock()
    coord.async_refresh = AsyncMock()

    await mgr._async_init_coordinator(coord, is_camera=True)

    coord._async_setup.assert_awaited_once()
    coord.async_refresh.assert_awaited_once()
    coord.async_config_entry_first_refresh.assert_not_awaited()


async def test_init_coordinator_swallows_exception():
    mgr = _bare()
    mgr.config_entry = SimpleNamespace(state=ConfigEntryState.LOADED)
    coord = MagicMock()
    coord.device_client = SimpleNamespace(device_id="d1")
    coord._async_setup = AsyncMock(side_effect=RuntimeError("boom"))
    coord.async_refresh = AsyncMock()

    # Fire-and-forget init must never surface an unhandled exception.
    await mgr._async_init_coordinator(coord, is_camera=True)
    coord.async_refresh.assert_not_awaited()


# --------------------------------------------------------------------------- #
# get_camera_coordinators - LOADED-only aggregation across entries
# --------------------------------------------------------------------------- #
def test_get_camera_coordinators_collects_loaded_entries_only():
    loaded = SimpleNamespace(
        state=ConfigEntryState.LOADED,
        runtime_data=SimpleNamespace(camera_coordinators={"c1": "coordA"}),
    )
    # Skipped: not LOADED.
    not_loaded = SimpleNamespace(
        state=ConfigEntryState.NOT_LOADED,
        runtime_data=SimpleNamespace(camera_coordinators={"c2": "coordB"}),
    )
    # Skipped: no runtime_data yet.
    no_runtime = SimpleNamespace(state=ConfigEntryState.LOADED, runtime_data=None)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=MagicMock(return_value=[loaded, not_loaded, no_runtime])
        )
    )

    result = get_camera_coordinators(hass)

    assert result == {"c1": "coordA"}


def test_get_camera_coordinators_empty_when_none_loaded():
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=MagicMock(return_value=[]))
    )
    assert get_camera_coordinators(hass) == {}
