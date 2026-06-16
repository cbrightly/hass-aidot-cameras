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
from homeassistant.const import PERCENTAGE, EntityCategory
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
    AidotSensorDescription(
        key="sd_card_status",
        translation_key="sd_card_status",
        icon="mdi:micro-sd",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        get_value=lambda s: s.sd_card_status,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera sensors."""
    coordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    registered: set[str] = set()

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
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_sensors()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_sensors()))


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
