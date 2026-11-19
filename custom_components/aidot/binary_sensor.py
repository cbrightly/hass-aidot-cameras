"""Support for Aidot camera binary sensors."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .coordinator import (
    AidotCameraUpdateCoordinator,
    AidotConfigEntry,
    AidotDeviceUpdateCoordinator,
)
from .entity import (
    AidotEntity,
    async_add_camera_entities,
    keyed_entities,
    per_camera_entities,
)
from .notify_dispatch import (
    KIND_PACKAGE,
    KIND_PERSON,
    KIND_PET,
    KIND_VEHICLE,
    event_kind,
)
from .switch import AidotDetectionTypeSwitch

PARALLEL_UPDATES = 0

# How long a live motion event implies "occupied" before we fall back to the
# cloud-polled value. The cloud Occupancy attribute only refreshes on the 5-min
# camera poll, so motion events (polled every ~30 s) give a much faster signal.
# The per-kind sensors hold for the same time.
MOTION_OCCUPANCY_WINDOW = 120.0


@dataclass(frozen=True, kw_only=True)
class AidotBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an Aidot camera binary sensor."""

    get_is_on: Any = None  # callable(DeviceStatusData) -> bool | None
    # Also drive the sensor ON from live cloud motion events (OR'd with the
    # slower cloud-polled value) for a near-real-time presence signal.
    motion_live: bool = False


CAMERA_BINARY_SENSORS: tuple[AidotBinarySensorDescription, ...] = (
    AidotBinarySensorDescription(
        key="occupancy",
        translation_key="occupancy",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        get_is_on=lambda s: s.occupancy,
        motion_live=True,
    ),
)


@dataclass(frozen=True, kw_only=True)
class AidotDetectionKindDescription(BinarySensorEntityDescription):
    """Describes the binary sensor for one kind of detection."""

    #: The event kind (notify_dispatch.event_kind) that turns the sensor on.
    kind: str
    #: The detection type the camera reports for this kind, the key of its
    #: detect_* switch. A camera that answers with its types and does not
    #: report this one gets no sensor (see kind_sensor_keys).
    detect_key: str


#: One per kind the cloud events carry besides plain motion (KINDS). Off by
#: default: the Motion event entity already carries the kind, and four more
#: entities per camera are for the user who wants them.
DETECTION_KIND_SENSORS: tuple[AidotDetectionKindDescription, ...] = (
    AidotDetectionKindDescription(
        key="person_detected",
        translation_key="person_detected",
        device_class=BinarySensorDeviceClass.MOTION,
        entity_registry_enabled_default=False,
        kind=KIND_PERSON,
        detect_key="humanDetect",
    ),
    AidotDetectionKindDescription(
        key="vehicle_detected",
        translation_key="vehicle_detected",
        device_class=BinarySensorDeviceClass.MOTION,
        entity_registry_enabled_default=False,
        kind=KIND_VEHICLE,
        detect_key="vehicleDetect",
    ),
    AidotDetectionKindDescription(
        key="package_detected",
        translation_key="package_detected",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        entity_registry_enabled_default=False,
        kind=KIND_PACKAGE,
        detect_key="packageDetect",
    ),
    AidotDetectionKindDescription(
        key="pet_detected",
        translation_key="pet_detected",
        device_class=BinarySensorDeviceClass.MOTION,
        entity_registry_enabled_default=False,
        kind=KIND_PET,
        detect_key="petDetect",
    ),
)

_KIND_SENSOR_BY_DETECT_KEY = {d.detect_key: d for d in DETECTION_KIND_SENSORS}


def detection_kind_keys(coordinator: AidotCameraUpdateCoordinator) -> list[str]:
    """The detection types this camera reports that have a kind sensor.

    The detect_* switches' own gate (what the camera answers getRoiHuman
    with), so a camera has a kind's sensor exactly when it has its switch.
    """
    return [
        key
        for key in AidotDetectionTypeSwitch.family.reported_keys(coordinator)
        if key in _KIND_SENSOR_BY_DETECT_KEY
    ]


def kind_sensor_keys(coordinator: AidotCameraUpdateCoordinator) -> list[str]:
    """The detection types whose kind sensor this camera gets.

    A camera that has said which detection types it has gets the sensors of
    those (detection_kind_keys). A battery camera is never asked - the
    question is a round trip to a camera that is asleep - so its types stay
    unknown, and gating on them would leave it with no kind sensor at all
    although its events carry the kinds like any other camera's. It gets all
    four, which are disabled by default: the user enables the kinds the
    camera detects. A mains camera that has not answered yet gets none until
    it does.
    """
    if isinstance(coordinator.camera_extras.get("detect"), dict):
        return detection_kind_keys(coordinator)
    if coordinator.device_client.is_battery_camera:
        return [d.detect_key for d in DETECTION_KIND_SENSORS]
    return []


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera binary sensors."""

    def _binary_sensors(c: AidotCameraUpdateCoordinator) -> list[BinarySensorEntity]:
        return [
            (
                AidotOccupancyBinarySensor
                if desc.motion_live
                else AidotCameraBinarySensor
            )(c, description=desc)
            for desc in CAMERA_BINARY_SENSORS
        ]

    def _collect(cameras, registered) -> list[Entity]:
        new = per_camera_entities(cameras, registered, _binary_sensors)
        # The kind sensors appear once the camera has said which detection
        # types it has, after the first extras refresh, as their switches do;
        # at once on a battery camera, which is never asked.
        new += keyed_entities(
            cameras,
            registered,
            kind_sensor_keys,
            AidotDetectionKindBinarySensor,
            marker="detected",
        )
        return new

    # The detection types land on the PER-CAMERA coordinator, so each camera
    # is hooked too.
    async_add_camera_entities(entry, async_add_entities, _collect, hook_cameras=True)


class AidotCameraBinarySensor(AidotEntity, BinarySensorEntity):
    """A read-only binary sensor backed by a cloud-polled camera attribute."""

    entity_description: AidotBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_is_on(self.coordinator.data)


class MotionHoldMixin:
    """Holds an entity on for MOTION_OCCUPANCY_WINDOW after a motion event.

    Shared by the occupancy sensor and the per-kind sensors, which differ only
    in which events count (``_motion_counts``) and in what they report once
    the hold lapses. Mixed in ahead of an AidotEntity on a camera coordinator.
    """

    _last_motion: float | None = None
    _motion_expiry_unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        coord = cast(AidotCameraUpdateCoordinator, self.coordinator)
        self.async_on_remove(coord.add_motion_listener(self._on_motion))
        # Cancel any pending expiry timer on removal so it can't fire
        # async_write_ha_state() on a dead entity (and keep it alive).
        self.async_on_remove(self._cancel_motion_timer)

    def _motion_counts(self, event: dict[str, Any]) -> bool:
        """Whether this event starts (or extends) the hold. Every one does."""
        return True

    @callback
    def _cancel_motion_timer(self) -> None:
        if self._motion_expiry_unsub is not None:
            self._motion_expiry_unsub()
            self._motion_expiry_unsub = None

    @callback
    def _on_motion(self, event: dict[str, Any]) -> None:
        """A live cloud motion event arrived: hold on for the window."""
        if not self._motion_counts(event):
            return
        self._last_motion = self.hass.loop.time()
        if self._motion_expiry_unsub is not None:
            self._motion_expiry_unsub()
        # Re-evaluate when the window lapses so the sensor can fall back off.
        self._motion_expiry_unsub = async_call_later(
            self.hass, MOTION_OCCUPANCY_WINDOW + 1.0, self._on_motion_expired
        )
        self.async_write_ha_state()

    @callback
    def _on_motion_expired(self, _now: datetime.datetime | None = None) -> None:
        self._motion_expiry_unsub = None
        self.async_write_ha_state()

    @property
    def _motion_held(self) -> bool:
        """Whether a counted event arrived within the window."""
        return (
            self._last_motion is not None
            and self.hass.loop.time() - self._last_motion < MOTION_OCCUPANCY_WINDOW
        )


class AidotOccupancyBinarySensor(MotionHoldMixin, AidotCameraBinarySensor):
    """Occupancy sensor that OR's the cloud-polled value with live motion events.

    The cloud Occupancy attribute only refreshes on the 5-min camera poll, so a
    motion event (polled ~every 30 s) flips presence on much sooner; the sensor
    falls back to the cloud value once the motion window lapses.
    """

    @property
    def is_on(self) -> bool | None:
        # A live motion event within the window wins (fast presence signal);
        # otherwise fall back to the slower cloud-polled value (super()).
        if self._motion_held:
            return True
        cloud = super().is_on
        # Some cameras (e.g. the A001064 PTZ) never report the Occupancy
        # attribute, so the cloud value is None.  Without this fallback the
        # sensor would sit at "unknown" forever - even while motion events keep
        # logging to history - because nothing ever resolves it to a state.  For
        # a motion-presence sensor, "no live motion event and no cloud presence"
        # means clear, so report False (the entity already shows "unavailable"
        # when the camera is offline, via AidotEntity.available).
        return cloud if cloud is not None else False


class AidotDetectionKindBinarySensor(MotionHoldMixin, AidotEntity, BinarySensorEntity):
    """On for the hold after the camera records an event of one kind.

    Person, vehicle, package or pet: what the Motion event entity reports as
    its event type, as a state an automation or a dashboard can read without
    a template. There is no cloud value behind these, so outside the hold the
    sensor is off.
    """

    entity_description: AidotDetectionKindDescription

    def __init__(
        self, coordinator: AidotDeviceUpdateCoordinator, detect_key: str
    ) -> None:
        super().__init__(
            coordinator, description=_KIND_SENSOR_BY_DETECT_KEY[detect_key]
        )

    def _motion_counts(self, event: dict[str, Any]) -> bool:
        return event_kind(event) == self.entity_description.kind

    @property
    def is_on(self) -> bool:
        return self._motion_held
