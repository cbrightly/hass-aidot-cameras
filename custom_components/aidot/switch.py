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
    # Local-only settings: readable over LAN long before they were controllable.
    # Each was toggled and restored on live hardware first, because probing the
    # wider attribute set showed this firmware acks writes it then ignores
    # (StreamType, spkNSLevel) and accepts values it should reject (VideoAngle
    # took 7). Only attributes with a demonstrated read-back get a switch.
    AidotSwitchDescription(
        key="osd_timestamp",
        translation_key="osd_timestamp",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.osd_timestamp,
        async_turn_on_fn=lambda c: c.async_set_osd_timestamp(True),
        async_turn_off_fn=lambda c: c.async_set_osd_timestamp(False),
    ),
    AidotSwitchDescription(
        key="auto_light",
        translation_key="auto_light",
        icon="mdi:lightbulb-auto",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.auto_light,
        async_turn_on_fn=lambda c: c.async_set_auto_light(True),
        async_turn_off_fn=lambda c: c.async_set_auto_light(False),
    ),
    AidotSwitchDescription(
        key="voice_prompts",
        translation_key="voice_prompts",
        icon="mdi:account-voice",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.voice_prompts,
        async_turn_on_fn=lambda c: c.async_set_voice_prompts(True),
        async_turn_off_fn=lambda c: c.async_set_voice_prompts(False),
    ),
    AidotSwitchDescription(
        key="hdr",
        translation_key="hdr",
        icon="mdi:hdr",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.hdr,
        async_turn_on_fn=lambda c: c.async_set_hdr(True),
        async_turn_off_fn=lambda c: c.async_set_hdr(False),
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
    # No ir_light switch. `nightVisionIRLight` acks the write and keeps its own
    # value - confirmed on BOTH A000088 units on 2026-08-14, so it is model
    # behaviour rather than one bad camera, and there is no model where a
    # read-back confirms it. It shipped as a switch users could toggle with no
    # effect and no error. `CameraStatusData.ir_light` still reports what the
    # camera says, which is why the state is readable but not settable here.
    AidotSwitchDescription(
        key="ptz_tracking",
        translation_key="ptz_tracking",
        icon="mdi:radar",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.ptz_tracking,
        async_turn_on_fn=lambda c: c.async_set_ptz_tracking(True),
        async_turn_off_fn=lambda c: c.async_set_ptz_tracking(False),
    ),
)


def _supports_auto_tracking(coordinator) -> bool:
    """Whether this camera can actually pan or tilt, and so track anything.

    Measured over the local control channel on both A000088 units here:
    `trackingMode=1` is acknowledged with a setDevAttrResp and the camera keeps
    its own value, read-back 0 every time. They have no motor. That is the same
    behaviour `ir_light` was removed for -- a switch that flips in the UI, does
    nothing, and reports nothing wrong.

    Mirrors `button.py::_is_ptz_camera`: prefer the advertised direction codes,
    fall back to the model id, because ptz_directions is sometimes empty at
    setup and arrives later -- gating on it alone would strip the switch from
    the one camera that needs it during that window.
    """
    info = coordinator.device_client.info
    if getattr(info, "ptz_directions", None):
        return True
    return "A001064" in (getattr(info, "model_id", "") or "")


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
            if desc.key != "ptz_tracking" or _supports_auto_tracking(c)
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
