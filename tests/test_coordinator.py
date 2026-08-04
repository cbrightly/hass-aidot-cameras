"""Tests for the manager coordinator's token-merge and cleanup paths.

Both methods under test touch only a handful of attributes, so (mirroring
``test_local_control.py``) we build a bare manager via ``__new__`` and set only
what the method reads - no hass lifecycle, no network, no real ``AidotClient``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import CONF_COUNTRY_CODE, CONF_PASSWORD, CONF_USERNAME

from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator


def _bare_manager() -> AidotDeviceManagerCoordinator:
    return AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)


# --------------------------------------------------------------------------- #
# token_fresh_cb
# --------------------------------------------------------------------------- #
def test_token_fresh_cb_merges_token_keeping_credentials():
    """A token refresh must MERGE the new token into entry.data and never drop the
    config-flow-only CONF_PASSWORD / CONF_COUNTRY_CODE keys (reauth + headless
    re-login read them back; a wholesale replace would KeyError on reauth)."""
    mgr = _bare_manager()
    # Existing entry data carries the credentials the library's login_info lacks.
    original = {
        CONF_USERNAME: "test@example.com",
        CONF_PASSWORD: "correct-password",
        CONF_COUNTRY_CODE: "US",
        "id": "user-123",
        "accessToken": "old-token",
    }
    mgr.config_entry = SimpleNamespace(data=dict(original))
    # The refreshed login_info has a new token but NO password/country.
    mgr.client = SimpleNamespace(
        login_info={"id": "user-123", "accessToken": "new-token", "mqttPassword": "pw"}
    )
    update = MagicMock()
    mgr.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update)
    )

    mgr.token_fresh_cb()

    update.assert_called_once()
    _entry_arg, kwargs = update.call_args
    merged = kwargs["data"]
    # Credentials survive...
    assert merged[CONF_PASSWORD] == "correct-password"
    assert merged[CONF_COUNTRY_CODE] == "US"
    assert merged[CONF_USERNAME] == "test@example.com"
    # ...and the refreshed token + new keys are applied.
    assert merged["accessToken"] == "new-token"
    assert merged["mqttPassword"] == "pw"


def test_token_fresh_cb_passes_the_entry_through():
    """The update targets the coordinator's own config entry."""
    mgr = _bare_manager()
    entry = SimpleNamespace(data={CONF_PASSWORD: "p", CONF_COUNTRY_CODE: "US"})
    mgr.config_entry = entry
    mgr.client = SimpleNamespace(login_info={"accessToken": "tok"})
    update = MagicMock()
    mgr.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=update)
    )

    mgr.token_fresh_cb()

    assert update.call_args.args[0] is entry


# --------------------------------------------------------------------------- #
# async_cleanup
# --------------------------------------------------------------------------- #
def _camera_coord() -> SimpleNamespace:
    dc = MagicMock()
    dc.async_stop_motion_polling = AsyncMock()
    dc.async_stop_streaming = AsyncMock()
    return SimpleNamespace(device_client=dc)


async def test_async_cleanup_stops_cameras_and_client():
    """Cleanup stops motion polling + streaming on every camera coordinator,
    clears light status callbacks, and tears the API client down."""
    mgr = _bare_manager()

    light_dc = MagicMock()
    light_dc.set_status_fresh_cb = MagicMock()
    mgr.device_coordinators = {"light1": SimpleNamespace(device_client=light_dc)}

    cam_a = _camera_coord()
    cam_b = _camera_coord()
    mgr.camera_coordinators = {"camA": cam_a, "camB": cam_b}

    mgr.client = MagicMock()
    mgr.client.async_cleanup = AsyncMock()

    await mgr.async_cleanup()

    # Light coordinators have their push callback detached.
    light_dc.set_status_fresh_cb.assert_called_once_with(None)
    # Each camera is stopped (both awaited).
    for cam in (cam_a, cam_b):
        cam.device_client.async_stop_motion_polling.assert_awaited_once()
        cam.device_client.async_stop_streaming.assert_awaited_once()
    mgr.client.async_cleanup.assert_awaited_once()


async def test_async_cleanup_handles_no_cameras():
    """Cleanup with no camera/light coordinators still tears the client down."""
    mgr = _bare_manager()
    mgr.device_coordinators = {}
    mgr.camera_coordinators = {}
    mgr.client = MagicMock()
    mgr.client.async_cleanup = AsyncMock()

    await mgr.async_cleanup()  # must not raise

    mgr.client.async_cleanup.assert_awaited_once()
