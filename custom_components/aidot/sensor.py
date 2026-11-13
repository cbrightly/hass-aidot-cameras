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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import (
    AidotEntity,
    AidotEntityDescription,
    async_add_camera_entities,
    described_entities,
    per_camera_entities,
    remove_registry_row,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AidotSensorDescription(SensorEntityDescription, AidotEntityDescription):
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
        # On by default: it is made only on battery cameras, where the charge
        # is the first thing an owner wants to see (the app shows it on the
        # settings page). A row an earlier release created disabled stays so:
        # enabling it from here would have Home Assistant reload the entry
        # 30 s into the first start, dropping every camera session.
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
        entity_registry_enabled_default=False,
        get_value=lambda e: (e.get("wifi") or {}).get("ssid"),
    ),
    AidotSensorDescription(
        key="sd_card_total",
        translation_key="sd_card_total",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        get_value=lambda e: (e.get("sd") or {}).get("total"),
    ),
    AidotSensorDescription(
        key="sd_card_used",
        translation_key="sd_card_used",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
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
        return self.entity_description.get_value(self.coordinator.camera_extras)


def _extras_report(c, desc: AidotSensorDescription) -> bool:
    """Whether the camera has answered with a value for this extras sensor."""
    return desc.get_value(c.camera_extras) is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera sensors."""

    def _sensors(c) -> list[SensorEntity]:
        # Battery only exists on battery models; a mains camera gets none, and
        # one an earlier version created for it is removed from the registry.
        battery = c.device_client.is_battery_camera
        if not battery:
            remove_registry_row(
                hass, "sensor", f"{c.device_client.info.dev_id}_battery"
            )
        return [
            AidotCameraSensor(c, desc)
            for desc in CAMERA_SENSORS
            if battery or desc.key != "battery"
        ]

    def _collect(cameras, registered) -> list[SensorEntity]:
        # Extras sensors appear once the camera has actually answered; before
        # that there is nothing to show and no way to know it supports them.
        return per_camera_entities(cameras, registered, _sensors) + described_entities(
            cameras,
            registered,
            CAMERA_EXTRA_SENSORS,
            AidotCameraExtraSensor,
            _extras_report,
            hass=hass,
            platform="sensor",
        )

    # The extras sensors' data lands on the PER-CAMERA coordinator, so each
    # camera is hooked too.
    async_add_camera_entities(entry, async_add_entities, _collect, hook_cameras=True)


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
