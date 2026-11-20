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
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import (
    AidotEntity,
    AidotEntityDescription,
    async_add_camera_entities,
    described_entities,
    device_info_for,
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

    @property
    def native_value(self) -> Any:
        return self.entity_description.get_value(self.coordinator.camera_extras)


def _extras_report(c, desc: AidotSensorDescription) -> bool:
    """Whether the camera has answered with a value for this extras sensor."""
    return desc.get_value(c.camera_extras) is not None


#: What the stream status sensor can say: the states of the library's
#: ``stream_health`` (aidot_cameras.camera.stream_health.STREAM_STATES). Spelled
#: out because the entity registry and the translations are keyed on them; a
#: state the library adds later reads as unknown until it is listed here.
STREAM_STATES: tuple[str, ...] = (
    "idle",
    "connecting",
    "waking",
    "streaming",
    "retrying",
    "dormant",
    "off",
)


@dataclass(frozen=True, kw_only=True)
class AidotStreamHealthDescription(SensorEntityDescription, AidotEntityDescription):
    """Describes a sensor read from the library's ``stream_health``."""

    get_value: Any = None  # callable(StreamHealth) -> StateType
    get_attributes: Any = None  # callable(StreamHealth) -> dict


#: Why a camera card is blank, from the library's own account of its serve
#: loop (``CameraDeviceClient.stream_health``): the camera may be connecting,
#: a battery camera waking, the loop waiting to retry, dormant after a run of
#: futile sessions, or the camera switched off.
STREAM_HEALTH_SENSORS: tuple[AidotStreamHealthDescription, ...] = (
    AidotStreamHealthDescription(
        key="stream_status",
        translation_key="stream_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(STREAM_STATES),
        entity_category=EntityCategory.DIAGNOSTIC,
        get_value=lambda h: h.state if h.state in STREAM_STATES else None,
        get_attributes=lambda h: {
            "last_end_reason": h.last_end_reason,
            "failures": h.failures,
        },
    ),
    AidotStreamHealthDescription(
        key="last_connect_time",
        translation_key="last_connect_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        get_value=lambda h: h.last_connect_s,
        get_attributes=lambda h: {"last_end_reason": h.last_end_reason},
    ),
)


def _reports_stream_health(c) -> bool:
    """Whether the installed library gives this camera a ``stream_health``.

    It arrived in a library release after the one this integration requires
    at least, so an older library gets no stream health sensors rather than
    ones that never have a value. Both halves are asked for, the snapshot and
    the change callback, because the entity uses both.
    """
    dc = c.device_client
    return getattr(dc, "stream_health", None) is not None and callable(
        getattr(dc, "add_stream_health_listener", None)
    )


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
            AidotCameraSensor(c, description=desc)
            for desc in CAMERA_SENSORS
            if battery or desc.key != "battery"
        ]

    def _collect(cameras, registered) -> list[SensorEntity]:
        # Extras sensors appear once the camera has actually answered; before
        # that there is nothing to show and no way to know it supports them.
        return (
            per_camera_entities(cameras, registered, _sensors)
            + per_camera_entities(
                cameras,
                registered,
                lambda c: [
                    AidotStreamHealthSensor(c, desc) for desc in STREAM_HEALTH_SENSORS
                ],
                marker="stream_health",
                supported=_reports_stream_health,
            )
            + described_entities(
                cameras,
                registered,
                CAMERA_EXTRA_SENSORS,
                lambda c, desc: AidotCameraExtraSensor(c, description=desc),
                _extras_report,
                hass=hass,
                platform="sensor",
            )
        )

    # The extras sensors' data lands on the PER-CAMERA coordinator, so each
    # camera is hooked too.
    async_add_camera_entities(entry, async_add_entities, _collect, hook_cameras=True)


class AidotStreamHealthSensor(SensorEntity):
    """A diagnostic sensor on what the camera's background stream is doing.

    The value comes from the library's ``stream_health`` and changes when the
    serve loop moves on, not on the coordinator's poll, so the entity listens
    for the library's change callback. The coordinator's refresh still writes
    the state, which is what picks up a power switch changed outside Home
    Assistant.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: AidotStreamHealthDescription

    # Not an AidotEntity, for the reason the motion event is not: that base
    # goes unavailable with the camera, and these sensors exist to say why a
    # card is blank. Offline, asleep and retrying are exactly when they are
    # read, and the library keeps the snapshot whatever the cloud says.
    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotStreamHealthDescription,
    ) -> None:
        self._coordinator = coordinator
        self.entity_description = description
        dev_id = coordinator.device_client.info.dev_id
        self._attr_unique_id = f"{dev_id}_{description.key}"
        self._attr_device_info = device_info_for(coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.device_client.add_stream_health_listener(
                self._health_changed
            )
        )
        self.async_on_remove(self._coordinator.async_add_listener(self._health_changed))

    @callback
    def _health_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def _health(self) -> Any:
        return self._coordinator.device_client.stream_health

    @property
    def native_value(self) -> Any:
        return self.entity_description.get_value(self._health)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.get_attributes(self._health)


class AidotCameraSensor(AidotEntity, SensorEntity):
    """A read-only diagnostic sensor for an Aidot camera."""

    entity_description: AidotSensorDescription

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_value(self.coordinator.data)
