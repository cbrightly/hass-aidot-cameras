"""Support for Aidot camera switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_SDES_AUDIO, DEFAULT_SDES_AUDIO
from .coordinator import (
    AidotCameraUpdateCoordinator,
    AidotConfigEntry,
    AidotDeviceUpdateCoordinator,
)
from .entity import AidotEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AidotSwitchDescription(SwitchEntityDescription):
    """Describes an Aidot camera switch."""

    get_is_on: Any = None        # callable(DeviceStatusData) -> bool | None
    async_turn_on_fn: Any = None  # async callable(DeviceClient) -> bool
    async_turn_off_fn: Any = None  # async callable(DeviceClient) -> bool


CAMERA_SWITCHES: tuple[AidotSwitchDescription, ...] = (
    AidotSwitchDescription(
        key="motion_detection",
        translation_key="motion_detection",
        icon="mdi:motion-sensor",
        get_is_on=lambda s: s.motion_detection,
        async_turn_on_fn=lambda c: c.async_set_motion_detection(True),
        async_turn_off_fn=lambda c: c.async_set_motion_detection(False),
    ),
    AidotSwitchDescription(
        key="status_led",
        translation_key="status_led",
        icon="mdi:led-on",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.status_led,
        async_turn_on_fn=lambda c: c.async_set_status_led(True),
        async_turn_off_fn=lambda c: c.async_set_status_led(False),
    ),
    AidotSwitchDescription(
        key="microphone",
        translation_key="microphone",
        icon="mdi:microphone",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.microphone,
        async_turn_on_fn=lambda c: c.async_set_microphone(True),
        async_turn_off_fn=lambda c: c.async_set_microphone(False),
    ),
    # NOTE: the floodlight is now a `light` entity and the siren a `siren` entity
    # (see light.py / siren.py) so they're off the switch domain and harder to
    # trigger accidentally.
    AidotSwitchDescription(
        key="ptz_tracking",
        translation_key="ptz_tracking",
        icon="mdi:radar",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.ptz_tracking,
        async_turn_on_fn=lambda c: c.async_set_ptz_tracking(True),
        async_turn_off_fn=lambda c: c.async_set_ptz_tracking(False),
    ),
    AidotSwitchDescription(
        key="ir_light",
        translation_key="ir_light",
        icon="mdi:led-off",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.ir_light,
        async_turn_on_fn=lambda c: c.async_set_ir_light(True),
        async_turn_off_fn=lambda c: c.async_set_ir_light(False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera switches."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_switches() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new: list[SwitchEntity] = [
            AidotCameraSwitch(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_SWITCHES
        ]
        # The serve-audio toggle only applies to SDES (battery) cameras.
        new += [
            AidotCameraAudioSwitch(c)
            for c in new_coords.values()
            if getattr(c.device_client, "is_sdes_camera", False)
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_switches()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_switches()))


class AidotCameraSwitch(AidotEntity, SwitchEntity):
    """A switch entity for an Aidot camera control."""

    entity_description: AidotSwitchDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSwitchDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_is_on(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.entity_description.async_turn_on_fn(self.device_client),
            f"turn on {self.name}",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.entity_description.async_turn_off_fn(self.device_client),
            f"turn off {self.name}",
        )


class AidotCameraAudioSwitch(AidotEntity, SwitchEntity, RestoreEntity):
    """Per-camera toggle for whether the live stream includes the camera audio.

    This is a local streaming preference (not a device attribute): it overrides
    the account-wide "SDES camera audio" option for one camera. State is restored
    across restarts; on first run it follows the global option. The change takes
    effect the next time the camera's live view is opened (re-open the camera
    card to apply it immediately). Distinct from the Microphone (audio privacy)
    switch, which disables the camera mic everywhere (app + recordings).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "camera_audio"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG

    coordinator: AidotCameraUpdateCoordinator

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        super().__init__(coordinator, key="camera_audio")

    def _global_default(self) -> bool:
        entry = self.coordinator.config_entry
        opts = entry.options if entry else {}
        return bool(opts.get(CONF_SDES_AUDIO, DEFAULT_SDES_AUDIO))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            on = last.state == "on"
        else:
            on = self._global_default()
        self.coordinator.sdes_audio_override = on
        self._attr_is_on = on

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.sdes_audio_override = True
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.sdes_audio_override = False
        self._attr_is_on = False
        self.async_write_ha_state()
