"""Tests for the Aidot camera diagnostic sensors.

Each sensor's ``native_value`` maps a status field (battery, wifi_rssi,
sd_card_status), including None handling, and the descriptions carry the right
device_class/unit/state_class metadata.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT

from custom_components.aidot.sensor import CAMERA_SENSORS, AidotCameraSensor

# key -> status field read by get_value
SENSOR_FIELDS = {
    "battery": "battery_remaining",
    "sd_card_status": "sd_card_status",
    "wifi_rssi": "wifi_rssi",
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
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _desc(key):
    return next(d for d in CAMERA_SENSORS if d.key == key)


def _make_sensor(key, data):
    return AidotCameraSensor(_coordinator(data), _desc(key))


@pytest.mark.parametrize("key, value", [
    ("battery", 73),
    ("sd_card_status", "normal"),
    ("wifi_rssi", -58),
])
def test_native_value_maps_status_field(key, value):
    field = SENSOR_FIELDS[key]
    s = _make_sensor(key, SimpleNamespace(**{field: value}))
    assert s.native_value == value


@pytest.mark.parametrize("key", list(SENSOR_FIELDS))
def test_native_value_none_when_field_none(key):
    field = SENSOR_FIELDS[key]
    s = _make_sensor(key, SimpleNamespace(**{field: None}))
    assert s.native_value is None


@pytest.mark.parametrize("key", list(SENSOR_FIELDS))
def test_native_value_none_when_no_data(key):
    s = _make_sensor(key, None)
    assert s.native_value is None


def test_battery_description_metadata():
    desc = _desc("battery")
    assert desc.device_class == SensorDeviceClass.BATTERY
    assert desc.native_unit_of_measurement == PERCENTAGE
    assert desc.state_class == SensorStateClass.MEASUREMENT


def test_wifi_rssi_description_metadata():
    desc = _desc("wifi_rssi")
    assert desc.device_class == SensorDeviceClass.SIGNAL_STRENGTH
    assert desc.native_unit_of_measurement == SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    assert desc.state_class == SensorStateClass.MEASUREMENT


def test_sd_card_status_is_plain_text_sensor():
    desc = _desc("sd_card_status")
    assert desc.device_class is None
    assert desc.native_unit_of_measurement is None
