"""Number entities for Aidot cameras (e.g. motion detection sensitivity)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import AidotEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AidotNumberDescription(NumberEntityDescription):
    """Describes an Aidot camera number entity."""

    get_value: Any = None  # callable(DeviceStatusData) -> float | None
    async_set_fn: Any = None  # async callable(DeviceClient, float) -> bool
    #: callable(DeviceStatusData, DeviceClient) -> bool. None means every
    #: camera gets it. A camera that does not report the setting gets no entity
    #: rather than one stuck at unknown.
    is_supported: Any = None


CAMERA_NUMBERS: tuple[AidotNumberDescription, ...] = (
    AidotNumberDescription(
        key="light_brightness",
        translation_key="light_brightness",
        icon="mdi:brightness-6",
        entity_category=EntityCategory.CONFIG,
        # The camera's own range (Dimming: min 10, max 100). The floor is 10,
        # not 0 -- 0 is out of range rather than "off", and off is the light
        # entity itself.
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        get_value=lambda s: getattr(s, "light_brightness", None),
        async_set_fn=lambda c, v: c.async_set_light_brightness(int(v)),
        # Deliberately NOT profile-gated, unlike the duration select. The
        # A000088's profile omits Dimming too, but the camera really has it: a
        # LAN write on 2026-09-08 was applied by the camera and echoed back by
        # the cloud. Gating this on the profile would remove a control that
        # demonstrably works.
        is_supported=lambda s, c: getattr(s, "light_brightness", None) is not None,
    ),
    AidotNumberDescription(
        key="motion_sensitivity",
        translation_key="motion_sensitivity",
        icon="mdi:motion-sensor",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=5,
        native_step=1,
        mode=NumberMode.SLIDER,
        get_value=lambda s: s.motion_sensitivity,
        async_set_fn=lambda c, v: c.async_set_motion_sensitivity(int(v)),
    ),
    AidotNumberDescription(
        key="speaker_volume",
        translation_key="speaker_volume",
        icon="mdi:volume-high",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        get_value=lambda s: s.speaker_volume,
        async_set_fn=lambda c, v: c.async_set_speaker_volume(int(v)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera number entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_numbers() -> None:
        new = []
        for dev_id, c in coordinator.camera_coordinators.items():
            for desc in CAMERA_NUMBERS:
                marker = f"{dev_id}:{desc.key}"
                if marker in registered:
                    continue
                if desc.is_supported is not None and not desc.is_supported(
                    getattr(c, "data", None) or object(),
                    getattr(c, "device_client", None),
                ):
                    continue
                registered.add(marker)
                new.append(AidotCameraNumber(c, desc))
        if new:
            async_add_entities(new)

    # A gated entity's data arrives on the PER-CAMERA coordinator, and the
    # parent's listener does not fire for that, so each camera is hooked too.
    hooked: set = set()

    def _refresh_all() -> None:
        for dev_id, cam in coordinator.camera_coordinators.items():
            if dev_id in hooked:
                continue
            hooked.add(dev_id)
            entry.async_on_unload(cam.async_add_listener(lambda: _add_new_numbers()))
        _add_new_numbers()

    _refresh_all()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_all))


class AidotCameraNumber(AidotEntity, NumberEntity):
    """A number entity for an Aidot camera setting."""

    entity_description: AidotNumberDescription

    def __init__(
        self,
        coordinator: AidotCameraUpdateCoordinator,
        description: AidotNumberDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_value(self.coordinator.data)

    async def async_set_native_value(self, value: float) -> None:
        await self.async_run_command(
            self.entity_description.async_set_fn(self.device_client, value),
            f"set {self.name}",
        )
