"""Tests for the Aidot light entities (bulb + camera floodlight).

AidotLight: color-mode selection from enable_rgbw/enable_cct, _update_status
mapping + the dual-mode color_mode inference, and async_turn_on per attribute.
AidotCameraFloodlight: on/off via getattr + the floodlight setter.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGBW_COLOR,
    ColorMode,
)

from custom_components.aidot.light import AidotCameraFloodlight, AidotLight


# --------------------------------------------------------------------------- #
# AidotLight
# --------------------------------------------------------------------------- #
def _light_info(*, enable_rgbw=False, enable_cct=False, cct_min=None, cct_max=None):
    return SimpleNamespace(
        dev_id="bulb1",
        model_id="A4.BL",
        mac="aa:bb:cc:dd:ee:ff",
        name="Bulb",
        hw_version="1.0",
        enable_rgbw=enable_rgbw,
        enable_cct=enable_cct,
        cct_min=cct_min,
        cct_max=cct_max,
    )


def _light_coord(info, data=None):
    dc = MagicMock()
    dc.info = info
    dc.async_set_brightness = AsyncMock(return_value=True)
    dc.async_set_cct = AsyncMock(return_value=True)
    dc.async_set_rgbw = AsyncMock(return_value=True)
    dc.async_turn_on = AsyncMock(return_value=True)
    dc.async_turn_off = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _status(on=True, dimming=128, cct=3000, rgbw=(0, 0, 0, 255), online=True):
    return SimpleNamespace(on=on, dimming=dimming, cct=cct, rgbw=rgbw, online=online)


def _make_light(info, data=None):
    light = AidotLight(_light_coord(info, data))
    light.async_write_ha_state = MagicMock()
    light.__dict__["name"] = "test"  # bypass platform-less name lookup
    return light


def test_rgbw_light_supports_rgbw_and_color_temp():
    light = _make_light(_light_info(enable_rgbw=True))
    assert light.supported_color_modes == {ColorMode.RGBW, ColorMode.COLOR_TEMP}


def test_cct_only_light_supports_color_temp():
    light = _make_light(_light_info(enable_cct=True))
    assert light.supported_color_modes == {ColorMode.COLOR_TEMP}
    assert light.color_mode == ColorMode.COLOR_TEMP


def test_dimmer_only_light_supports_brightness():
    light = _make_light(_light_info())
    assert light.supported_color_modes == {ColorMode.BRIGHTNESS}
    assert light.color_mode == ColorMode.BRIGHTNESS


def test_update_status_maps_fields():
    light = _make_light(
        _light_info(enable_rgbw=True),
        data=_status(on=True, dimming=200, cct=4000, rgbw=(10, 20, 30, 40)),
    )
    assert light.is_on is True
    assert light.brightness == 200
    assert light.color_temp_kelvin == 4000
    assert light.rgbw_color == (10, 20, 30, 40)


def test_color_mode_inference_rgb_nonzero_is_rgbw():
    light = _make_light(
        _light_info(enable_rgbw=True),
        data=_status(rgbw=(255, 0, 0, 0)),
    )
    assert light.color_mode == ColorMode.RGBW


def test_color_mode_inference_rgb_zero_is_color_temp():
    light = _make_light(
        _light_info(enable_rgbw=True),
        data=_status(rgbw=(0, 0, 0, 255)),
    )
    assert light.color_mode == ColorMode.COLOR_TEMP


def test_color_mode_inference_none_rgbw_is_color_temp():
    light = _make_light(
        _light_info(enable_rgbw=True),
        data=_status(rgbw=None),
    )
    assert light.color_mode == ColorMode.COLOR_TEMP


async def test_turn_on_brightness():
    light = _make_light(_light_info(enable_rgbw=True), data=_status())
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 64})
    light.coordinator.device_client.async_set_brightness.assert_awaited_once_with(64)
    assert light.brightness == 64
    assert light.coordinator.data.dimming == 64
    assert light.is_on is True


async def test_turn_on_color_temp_sets_mode():
    light = _make_light(_light_info(enable_rgbw=True), data=_status())
    await light.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 5000})
    light.coordinator.device_client.async_set_cct.assert_awaited_once_with(5000)
    assert light.color_temp_kelvin == 5000
    assert light.color_mode == ColorMode.COLOR_TEMP


async def test_turn_on_rgbw_sets_mode():
    light = _make_light(_light_info(enable_rgbw=True), data=_status())
    await light.async_turn_on(**{ATTR_RGBW_COLOR: (1, 2, 3, 4)})
    light.coordinator.device_client.async_set_rgbw.assert_awaited_once_with((1, 2, 3, 4))
    assert light.rgbw_color == (1, 2, 3, 4)
    assert light.color_mode == ColorMode.RGBW


async def test_turn_on_plain_calls_turn_on():
    light = _make_light(_light_info(), data=_status(on=False))
    await light.async_turn_on()
    light.coordinator.device_client.async_turn_on.assert_awaited_once()
    assert light.is_on is True


async def test_turn_off_calls_turn_off():
    light = _make_light(_light_info(), data=_status(on=True))
    await light.async_turn_off()
    light.coordinator.device_client.async_turn_off.assert_awaited_once()
    assert light.is_on is False
    assert light.coordinator.data.on is False


def test_available_follows_online():
    light = _make_light(_light_info(), data=_status(online=True))
    assert light.available is True
    light_off = _make_light(_light_info(), data=_status(online=False))
    assert light_off.available is False


# --------------------------------------------------------------------------- #
# AidotCameraFloodlight
# --------------------------------------------------------------------------- #
def _flood_coord(data=None):
    info = SimpleNamespace(
        dev_id="cam1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    dc.async_set_floodlight = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _flood(data):
    f = AidotCameraFloodlight(_flood_coord(data))
    f.async_write_ha_state = MagicMock()
    f.__dict__["name"] = "test"  # bypass platform-less name lookup
    return f


def test_floodlight_onoff_mode():
    f = _flood(None)
    assert f.supported_color_modes == {ColorMode.ONOFF}
    assert f.color_mode == ColorMode.ONOFF


def test_floodlight_is_on_via_getattr():
    assert _flood(SimpleNamespace(floodlight=True)).is_on is True
    assert _flood(SimpleNamespace(floodlight=False)).is_on is False
    assert _flood(SimpleNamespace()).is_on is None
    assert _flood(None).is_on is None


async def test_floodlight_turn_on():
    f = _flood(SimpleNamespace(floodlight=False))
    await f.async_turn_on()
    f.device_client.async_set_floodlight.assert_awaited_once_with(True)


async def test_floodlight_turn_off():
    f = _flood(SimpleNamespace(floodlight=True))
    await f.async_turn_off()
    f.device_client.async_set_floodlight.assert_awaited_once_with(False)
