"""Small leftover coverage gaps across const / light / media_source / select /
switch.

Each target touches only a handful of attributes, so entities are built with a
mocked coordinator (no hass lifecycle) mirroring ``test_light.py`` /
``test_select.py`` / ``test_switch.py``. The two RestoreEntity paths patch the
parent ``async_added_to_hass`` (which needs a live hass) to a no-op and stub
``async_get_last_state``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.media_source import MediaSourceError
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.aidot.const import (
    CONF_FAST_CONNECT,
    CONNECTION_MODE_LAN_DIRECT,
    CONNECTION_MODE_RELAY,
    resolve_connection_mode,
)
from custom_components.aidot.light import AidotLight
from custom_components.aidot.media_source import (
    AidotMediaSource,
    async_get_media_source,
)
from custom_components.aidot.select import CAMERA_SELECTS, AidotResolutionSelect
from custom_components.aidot.switch import AidotCameraAudioSwitch


# --------------------------------------------------------------------------- #
# const.resolve_connection_mode - legacy CONF_FAST_CONNECT migration (line 99)
# --------------------------------------------------------------------------- #
def test_resolve_connection_mode_legacy_fast_connect_true():
    assert (
        resolve_connection_mode({CONF_FAST_CONNECT: True}) == CONNECTION_MODE_LAN_DIRECT
    )


def test_resolve_connection_mode_legacy_fast_connect_false():
    assert (
        resolve_connection_mode({CONF_FAST_CONNECT: False}) == CONNECTION_MODE_RELAY
    )


# --------------------------------------------------------------------------- #
# light.AidotLight._handle_coordinator_update (lines 164-165)
# --------------------------------------------------------------------------- #
def _light_coord(data):
    info = SimpleNamespace(
        dev_id="bulb1",
        model_id="A4.BL",
        mac="aa:bb:cc:dd:ee:ff",
        name="Bulb",
        hw_version="1.0",
        enable_rgbw=True,
        enable_cct=False,
        cct_min=None,
        cct_max=None,
    )
    dc = MagicMock()
    dc.info = info
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def test_light_handle_coordinator_update_refreshes_and_writes_state():
    initial = SimpleNamespace(on=True, dimming=10, cct=3000, rgbw=(0, 0, 0, 255), online=True)
    light = AidotLight(_light_coord(initial))
    light.async_write_ha_state = MagicMock()
    light.__dict__["name"] = "test"

    # New push from the coordinator: _handle_coordinator_update must re-map it.
    light.coordinator.data = SimpleNamespace(
        on=False, dimming=200, cct=4000, rgbw=(9, 8, 7, 6), online=True
    )
    light._handle_coordinator_update()

    assert light.is_on is False
    assert light.brightness == 200
    assert light.color_temp_kelvin == 4000
    light.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------------- #
# media_source (lines 32, 72)
# --------------------------------------------------------------------------- #
async def test_async_get_media_source_returns_instance():
    src = await async_get_media_source(MagicMock())
    assert isinstance(src, AidotMediaSource)


async def test_browse_media_raises_not_browsable_for_event_identifier():
    src = AidotMediaSource(MagicMock())
    with pytest.raises(MediaSourceError):
        await src.async_browse_media(SimpleNamespace(identifier="dev1/v1:evt"))


# --------------------------------------------------------------------------- #
# select.AidotResolutionSelect.async_added_to_hass restore path (line 138)
# --------------------------------------------------------------------------- #
def _cam_coord():
    info = SimpleNamespace(
        dev_id="dev1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = None
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    coordinator.sdes_audio_override = None
    return coordinator


def _resolution_desc():
    return next(d for d in CAMERA_SELECTS if d.key == "resolution")


async def test_resolution_select_restores_last_option():
    sel = AidotResolutionSelect(_cam_coord(), _resolution_desc())
    sel.__dict__["name"] = "test"
    sel.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="sd"))

    # The parent RestoreEntity.async_added_to_hass needs a live hass; no-op it.
    with patch.object(RestoreEntity, "async_added_to_hass", AsyncMock()):
        await sel.async_added_to_hass()

    assert sel._optimistic_option == "sd"
    assert sel.current_option == "sd"


# --------------------------------------------------------------------------- #
# switch.AidotCameraAudioSwitch.async_added_to_hass restore path (line 175)
# --------------------------------------------------------------------------- #
async def test_camera_audio_switch_restores_last_on_state():
    coord = _cam_coord()
    sw = AidotCameraAudioSwitch(coord)
    sw.__dict__["name"] = "test"
    sw.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="on"))

    # super().async_added_to_hass() runs CoordinatorEntity/RestoreEntity which
    # both need a live hass; no-op the CoordinatorEntity chain entry point.
    with patch.object(CoordinatorEntity, "async_added_to_hass", AsyncMock()):
        await sw.async_added_to_hass()

    assert sw.coordinator.sdes_audio_override is True
    assert sw.is_on is True
