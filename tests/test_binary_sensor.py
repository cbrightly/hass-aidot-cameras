"""Tests for the Aidot camera occupancy binary sensor.

The occupancy sensor's cloud-polled value maps via get_is_on with the right
device_class and None handling. The live-motion override path (which needs
``hass.loop`` and timers) is exercised only via the base cloud value here; the
camera-side motion fan-out is covered in test_event.py.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.aidot.binary_sensor import (
    CAMERA_BINARY_SENSORS,
    AidotCameraBinarySensor,
    AidotOccupancyBinarySensor,
)


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


def _desc():
    return CAMERA_BINARY_SENSORS[0]


def _base_sensor(data):
    return AidotCameraBinarySensor(_coordinator(data), _desc())


def test_description_is_occupancy():
    desc = _desc()
    assert desc.key == "occupancy"
    assert desc.device_class == BinarySensorDeviceClass.OCCUPANCY
    assert desc.motion_live is True


def test_base_is_on_maps_occupancy_field():
    assert _base_sensor(SimpleNamespace(occupancy=True)).is_on is True
    assert _base_sensor(SimpleNamespace(occupancy=False)).is_on is False


def test_base_is_on_none_passthrough():
    assert _base_sensor(SimpleNamespace(occupancy=None)).is_on is None


def test_base_is_on_none_when_no_data():
    assert _base_sensor(None).is_on is None


# --------------------------------------------------------------------------- #
# Occupancy sensor: with no live motion it falls back to the cloud value, and a
# missing cloud value resolves to False (never "unknown" forever).
# --------------------------------------------------------------------------- #
def _occ_sensor(data):
    occ = AidotOccupancyBinarySensor(_coordinator(data), _desc())
    occ._last_motion = None  # no live motion event seen
    return occ


def test_occupancy_falls_back_to_cloud_true():
    assert _occ_sensor(SimpleNamespace(occupancy=True)).is_on is True


def test_occupancy_falls_back_to_cloud_false():
    assert _occ_sensor(SimpleNamespace(occupancy=False)).is_on is False


def test_occupancy_none_cloud_resolves_to_false():
    # Cameras that never report Occupancy must not sit at "unknown" forever.
    assert _occ_sensor(SimpleNamespace(occupancy=None)).is_on is False
