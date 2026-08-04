"""Tests for the Aidot camera number entities.

``native_value`` reads the status field; ``async_set_native_value`` awaits the
matching library setter with the value coerced to int.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.components.number import NumberMode

from custom_components.aidot.number import CAMERA_NUMBERS, AidotCameraNumber

# key -> (status field, device_client setter name)
NUMBER_FIELDS = {
    "motion_sensitivity": ("motion_sensitivity", "async_set_motion_sensitivity"),
    "speaker_volume": ("speaker_volume", "async_set_speaker_volume"),
}


def _coordinator(data=None):
    info = SimpleNamespace(
        dev_id="dev1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    for _, setter in NUMBER_FIELDS.values():
        setattr(dc, setter, AsyncMock(return_value=True))
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _desc(key):
    return next(d for d in CAMERA_NUMBERS if d.key == key)


def _make_number(key, data):
    n = AidotCameraNumber(_coordinator(data), _desc(key))
    n.async_write_ha_state = MagicMock()
    n.__dict__["name"] = "test"  # bypass platform-less name lookup
    return n


@pytest.mark.parametrize("key, value", [
    ("motion_sensitivity", 3),
    ("speaker_volume", 80),
])
def test_native_value_reads_field(key, value):
    field, _ = NUMBER_FIELDS[key]
    n = _make_number(key, SimpleNamespace(**{field: value}))
    assert n.native_value == value


@pytest.mark.parametrize("key", list(NUMBER_FIELDS))
def test_native_value_none_when_no_data(key):
    assert _make_number(key, None).native_value is None


@pytest.mark.parametrize("key, value", [
    ("motion_sensitivity", 4.0),
    ("speaker_volume", 55.0),
])
async def test_set_native_value_calls_setter_as_int(key, value):
    field, setter = NUMBER_FIELDS[key]
    n = _make_number(key, SimpleNamespace(**{field: 1}))
    await n.async_set_native_value(value)
    getattr(n.device_client, setter).assert_awaited_once_with(int(value))
    n.async_write_ha_state.assert_called_once()


def test_description_ranges_and_mode():
    ms = _desc("motion_sensitivity")
    assert ms.native_min_value == 1
    assert ms.native_max_value == 5
    assert ms.mode == NumberMode.SLIDER
    vol = _desc("speaker_volume")
    assert vol.native_min_value == 0
    assert vol.native_max_value == 100
