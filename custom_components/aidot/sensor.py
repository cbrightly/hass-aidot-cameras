"""Support for Aidot camera diagnostic sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AidotSensorDescription(SensorEntityDescription):
    """Describes an Aidot camera sensor."""

    get_value: Any = None  # callable(DeviceStatusData) -> StateType


CAMERA_SENSORS: tuple[AidotSensorDescription, ...] = (
    AidotSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        get_value=lambda s: s.battery_remaining,
    ),
    # Reports whether there IS a card, not the raw `SDcardStatus` cloud
    # property this used to echo. That property does not mean "a card is in the
    # slot": on an A000088 the library measured it INVERTED against reality -
    # "0" with a card, "1" without, 3 of 3 - so a row labelled "SD card"
    # showing it told a user the opposite of the truth. Seen on the live fleet
    # 2026-08-13: the camera holding 125 recordings showed 0, the one with an
    # empty slot showed 1.
    #
    # `sd_card_present` is the signal the media browser has always used, and
    # its None is load-bearing: four of seven cameras report neither cloud key,
    # and calling that "empty" would be the same lie in the other direction.
    # The raw property is still in diagnostics for anyone debugging it.
    AidotSensorDescription(
        key="sd_card_status",
        translation_key="sd_card_status",
        icon="mdi:micro-sd",
        device_class=SensorDeviceClass.ENUM,
        options=["present", "empty"],
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        get_value=lambda s: (
            None
            if s.sd_card_present is None
            else ("present" if s.sd_card_present else "empty")
        ),
    ),
    AidotSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        get_value=lambda s: s.wifi_rssi,
    ),
)


#: Sensors whose value comes from `coordinator.camera_extras` -- the camera's
#: own answers, not the cloud device payload. They appear once the camera has
#: replied, which is a slow round trip, so they arrive after setup.
CAMERA_EXTRA_SENSORS: tuple[AidotSensorDescription, ...] = (
    AidotSensorDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        get_value=lambda e: (e.get("wifi") or {}).get("ssid"),
    ),
    AidotSensorDescription(
        key="sd_card_total",
        translation_key="sd_card_total",
        entity_category=EntityCategory.DIAGNOSTIC,
        get_value=lambda e: (e.get("sd") or {}).get("total"),
    ),
    AidotSensorDescription(
        key="sd_card_used",
        translation_key="sd_card_used",
        entity_category=EntityCategory.DIAGNOSTIC,
        get_value=lambda e: (e.get("sd") or {}).get("used"),
    ),
)


class AidotCameraExtraSensor(AidotEntity, SensorEntity):
    """A sensor whose value the camera reports over its own request path.

    Deliberately no unit and no device class on the SD figures: the camera
    reports bare numbers whose units are unconfirmed (MB is plausible at these
    magnitudes), and declaring bytes or megabytes would render a number that
    looks authoritative and could be wrong by a factor of a million.
    """

    entity_description: AidotSensorDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSensorDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        extras = getattr(self.coordinator, "camera_extras", None)
        if not isinstance(extras, dict):
            return None
        return self.entity_description.get_value(extras)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera sensors."""
    coordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    registered: set[str] = set()
    extra_registered: set[str] = set()

    def _add_new_sensors() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [
            AidotCameraSensor(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_SENSORS
            # Battery only exists on battery models; skip it on mains-powered
            # cameras so they don't show a permanently-"unknown" battery sensor.
            if not (desc.key == "battery" and not c.device_client.is_battery_camera)
        ]
        # Remove a battery sensor previously created for a mains-powered camera.
        for c in new_coords.values():
            if not c.device_client.is_battery_camera:
                eid = ent_reg.async_get_entity_id(
                    "sensor", DOMAIN, f"{c.device_client.info.dev_id}_battery"
                )
                if eid:
                    ent_reg.async_remove(eid)
        # Extras sensors appear once the camera has actually answered; before
        # that there is nothing to show and no way to know the camera supports
        # it. Tracked separately from `registered`, which is per-camera.
        for dev_id, c in coordinator.camera_coordinators.items():
            extras = getattr(c, "camera_extras", None)
            if not isinstance(extras, dict) or not extras:
                continue
            for desc in CAMERA_EXTRA_SENSORS:
                if desc.get_value(extras) is None:
                    continue
                marker = f"{dev_id}:extra:{desc.key}"
                if marker in extra_registered:
                    continue
                extra_registered.add(marker)
                new.append(AidotCameraExtraSensor(c, desc))

        if new:
            registered.update(new_coords)
            async_add_entities(new)

    # Entities whose data comes from the camera itself appear only after that
    # camera answers, and that answer updates the PER-CAMERA coordinator. The
    # parent's listener does not fire for it, so without hooking each camera
    # coordinator the entities are fetched successfully and never created.
    hooked: set = set()

    def _hook_camera_coordinators() -> None:
        for dev_id, cam in coordinator.camera_coordinators.items():
            if dev_id in hooked:
                continue
            hooked.add(dev_id)
            entry.async_on_unload(cam.async_add_listener(lambda: _add_new_sensors()))

    def _refresh_all() -> None:
        _hook_camera_coordinators()
        _add_new_sensors()

    _refresh_all()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_all))


class AidotCameraSensor(AidotEntity, SensorEntity):
    """A read-only diagnostic sensor for an Aidot camera."""

    entity_description: AidotSensorDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSensorDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_value(self.coordinator.data)
