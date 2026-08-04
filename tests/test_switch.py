"""Tests for the Aidot camera switch entities.

Each switch is built with a mocked camera coordinator/device_client (no hass
lifecycle): we assert that ``is_on`` reads the right status field, that
turn_on/off await the matching library coroutine, and the camera-audio switch's
local override behaviour. ``async_write_ha_state`` is stubbed since the entity is
never actually added to hass.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aidot.const import CONF_SDES_AUDIO, DEFAULT_SDES_AUDIO
from custom_components.aidot.switch import (
    CAMERA_SWITCHES,
    AidotCameraAudioSwitch,
    AidotCameraSwitch,
)

# key -> (status field read by get_is_on, device_client setter coroutine name)
SWITCH_FIELDS = {
    "motion_detection": ("motion_detection", "async_set_motion_detection"),
    "status_led": ("status_led", "async_set_status_led"),
    "microphone": ("microphone", "async_set_microphone"),
    "ptz_tracking": ("ptz_tracking", "async_set_ptz_tracking"),
    "ir_light": ("ir_light", "async_set_ir_light"),
}

SETTERS = [name for _, name in SWITCH_FIELDS.values()]


def _coordinator(data=None, *, is_sdes=False, options=None, last_update_success=True):
    info = SimpleNamespace(
        dev_id="dev1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    dc.is_sdes_camera = is_sdes
    for setter in SETTERS:
        setattr(dc, setter, AsyncMock(return_value=True))
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = last_update_success
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options=options or {})
    coordinator.sdes_audio_override = None
    return coordinator


def _desc(key):
    return next(d for d in CAMERA_SWITCHES if d.key == key)


def _make_switch(key, data):
    coord = _coordinator(data=data)
    sw = AidotCameraSwitch(coord, _desc(key))
    sw.async_write_ha_state = MagicMock()
    sw.__dict__["name"] = "test"  # bypass platform-less name lookup
    return sw


@pytest.mark.parametrize("key", list(SWITCH_FIELDS))
def test_is_on_reads_status_field(key):
    field, _ = SWITCH_FIELDS[key]
    sw = _make_switch(key, SimpleNamespace(**{field: True}))
    assert sw.is_on is True
    sw = _make_switch(key, SimpleNamespace(**{field: False}))
    assert sw.is_on is False


@pytest.mark.parametrize("key", list(SWITCH_FIELDS))
def test_is_on_none_when_no_data(key):
    sw = _make_switch(key, None)
    assert sw.is_on is None


@pytest.mark.parametrize("key", list(SWITCH_FIELDS))
async def test_turn_on_awaits_setter_true(key):
    field, setter = SWITCH_FIELDS[key]
    sw = _make_switch(key, SimpleNamespace(**{field: False}))
    await sw.async_turn_on()
    getattr(sw.device_client, setter).assert_awaited_once_with(True)
    sw.async_write_ha_state.assert_called_once()


@pytest.mark.parametrize("key", list(SWITCH_FIELDS))
async def test_turn_off_awaits_setter_false(key):
    field, setter = SWITCH_FIELDS[key]
    sw = _make_switch(key, SimpleNamespace(**{field: True}))
    await sw.async_turn_off()
    getattr(sw.device_client, setter).assert_awaited_once_with(False)


def test_available_follows_online_and_data():
    sw = _make_switch("motion_detection", SimpleNamespace(motion_detection=True, online=True))
    assert sw.available is True
    # offline device -> unavailable
    sw2 = _make_switch("motion_detection", SimpleNamespace(motion_detection=True, online=False))
    assert sw2.available is False
    # no data -> unavailable
    sw3 = _make_switch("motion_detection", None)
    assert sw3.available is False


def test_available_false_when_coordinator_failed():
    coord = _coordinator(
        data=SimpleNamespace(motion_detection=True, online=True),
        last_update_success=False,
    )
    sw = AidotCameraSwitch(coord, _desc("motion_detection"))
    assert sw.available is False


# --------------------------------------------------------------------------- #
# Camera-audio switch (local streaming override, no device attribute)
# --------------------------------------------------------------------------- #
def _audio_switch(options=None):
    coord = _coordinator(is_sdes=True, options=options or {})
    sw = AidotCameraAudioSwitch(coord)
    sw.async_write_ha_state = MagicMock()
    sw.__dict__["name"] = "test"  # bypass platform-less name lookup
    return sw


async def test_camera_audio_turn_on_sets_override():
    sw = _audio_switch()
    await sw.async_turn_on()
    assert sw.coordinator.sdes_audio_override is True
    assert sw.is_on is True
    sw.async_write_ha_state.assert_called_once()


async def test_camera_audio_turn_off_clears_override():
    sw = _audio_switch()
    await sw.async_turn_off()
    assert sw.coordinator.sdes_audio_override is False
    assert sw.is_on is False


def test_camera_audio_global_default_reads_option():
    sw = _audio_switch({CONF_SDES_AUDIO: False})
    assert sw._global_default() is False
    sw2 = _audio_switch({})
    assert sw2._global_default() is DEFAULT_SDES_AUDIO
