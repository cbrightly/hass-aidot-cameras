"""A device's firmware version is its sw_version, as the app shows it.

The cloud device record carries `firmwareVersion` (the app's "Firmware
Version" row). The library parses it into a camera's device info
(`firmware_version`), entities hand it to the device registry as sw_version,
and a later change (an over-the-air update) is written to the registry on the
next device-list refresh.
"""

import pytest
from types import SimpleNamespace

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot.const import DOMAIN
from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator
from custom_components.aidot.entity import aidot_device_info, device_info_for
from custom_components.aidot.sensor import CAMERA_SENSORS


class _Info:
    dev_id = "dev1"
    model_id = "LK.IPC.A001513"
    mac = ""
    name = "A camera"
    hw_version = "V2.0-Y4"


def test_sw_version_is_emitted_only_when_known():
    assert aidot_device_info(_Info(), sw_version="1.2.3")["sw_version"] == "1.2.3"
    # Unknown leaves the key out, so a version in the registry is not blanked.
    assert "sw_version" not in aidot_device_info(_Info())


def test_entities_read_it_off_the_library_device_info():
    info = _Info()
    info.firmware_version = "4.5.6"
    coord = SimpleNamespace(
        device_client=SimpleNamespace(info=info),
        config_entry=SimpleNamespace(entry_id="e1", runtime_data=SimpleNamespace()),
    )
    assert device_info_for(coord)["sw_version"] == "4.5.6"


async def test_a_refresh_records_it_and_follows_an_update(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = dr.async_get(hass)
    device = reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "dev1")}
    )
    cam_info = SimpleNamespace(firmware_version="0.9.0")
    coord = AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)
    coord.hass = hass
    coord.camera_coordinators = {
        "dev1": SimpleNamespace(device_client=SimpleNamespace(info=cam_info))
    }
    coord._follow_firmware_updates(
        [
            {"id": "dev1", "firmwareVersion": "1.0.0"},
            {"id": "dev2"},  # none reported: nothing written
            {"id": "dev3", "firmwareVersion": 7},  # not a string: ignored
        ]
    )
    assert reg.async_get(device.id).sw_version == "1.0.0"
    # The camera's library info follows too, for diagnostics.
    assert cam_info.firmware_version == "1.0.0"
    coord._follow_firmware_updates([{"id": "dev1", "firmwareVersion": "1.0.1"}])
    assert reg.async_get(device.id).sw_version == "1.0.1"


def test_the_battery_sensor_is_on_by_default():
    # It is made only on battery cameras, where the charge is the point.
    battery = next(d for d in CAMERA_SENSORS if d.key == "battery")
    assert battery.entity_registry_enabled_default is True


async def test_the_firmware_follow_uses_the_entry_scoped_lookup(hass, monkeypatch):
    """Home Assistant 2026.10 deprecates async_get_device (identifiers are no
    longer unique across entries) and the box logged that warning at this call
    site; where the registry has async_get_device_by_identifier it is used,
    scoped to our entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = dr.async_get(hass)
    device = reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "dev1")}
    )
    asked = []

    def by_identifier(identifier, entry_id):
        asked.append((identifier, entry_id))
        return device

    monkeypatch.setattr(
        reg, "async_get_device_by_identifier", by_identifier, raising=False
    )
    monkeypatch.setattr(
        reg, "async_get_device", lambda **kw: pytest.fail("deprecated lookup used")
    )
    coord = AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)
    coord.hass = hass
    coord.config_entry = entry
    coord.camera_coordinators = {}
    coord._follow_firmware_updates([{"id": "dev1", "firmwareVersion": "2.0.0"}])
    assert asked == [((DOMAIN, "dev1"), entry.entry_id)]
    assert reg.async_get(device.id).sw_version == "2.0.0"
