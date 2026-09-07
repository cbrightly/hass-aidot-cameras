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
    supports_ptz,
)
from .entity import AidotEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AidotSwitchDescription(SwitchEntityDescription):
    """Describes an Aidot camera switch."""

    get_is_on: Any = None        # callable(DeviceStatusData) -> bool | None
    async_turn_on_fn: Any = None  # async callable(DeviceClient) -> bool
    async_turn_off_fn: Any = None  # async callable(DeviceClient) -> bool
    #: callable(coordinator) -> bool. None = every camera gets this switch.
    #: A control the model cannot perform must NOT ship: this firmware
    #: acknowledges writes it then ignores, so an ungated switch is one the
    #: user can toggle with no effect and no error (see the ir_light note).
    supported_fn: Any = None


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
        supported_fn=supports_ptz,
    ),
)


def _pending_switches(camera_coordinators, registered: set, gated: set):
    """(coordinator, description) pairs that still need a switch entity.

    Two memos, not one.  Ungated switches are per-camera: once a camera has
    them it is done.  Capability-gated ones are per (camera, key), and are
    only recorded when the switch is actually created -- because a capability
    can arrive AFTER setup.  ``ptz_directions`` is empty at setup and populated
    on a later refresh, and the per-camera memo is filled the instant any of
    the eight ungated switches is created, so a single memo would close the
    door on the real PTZ before it had said what it can do.

    ``button.py`` keeps a separate ``ptz_registered`` set for exactly this
    reason, with a comment saying so; this is the same rule for switches.
    """
    out = []
    for dev_id, c in camera_coordinators.items():
        for desc in CAMERA_SWITCHES:
            if desc.supported_fn is None:
                if dev_id not in registered:
                    out.append((c, desc))
            elif (dev_id, desc.key) not in gated and desc.supported_fn(c):
                gated.add((dev_id, desc.key))
                out.append((c, desc))
        registered.add(dev_id)
    return out


#: Friendly names for the sound detectors the cameras report. A camera may
#: report a key not listed here -- the switch is still created, named from the
#: key itself, because the camera is the authority on what it supports.
#: Wire key -> translation key. Every other entity on this integration is named
#: through strings.json; naming these with _attr_name made seven switches
#: untranslatable in every locale, on a manifest claiming platinum quality.
SOUND_DETECTION_TRANSLATION_KEYS: dict[str, str] = {
    "sound_enable": "sound_enable",
    "all_sound": "sound_all",
    "glass_Break": "sound_glass_break",
    "smoke_T3": "sound_smoke_t3",
    "smoke_T4": "sound_smoke_t4",
    "baby_cry": "sound_baby_cry",
    "dog_bark": "sound_dog_bark",
}

#: Kept for the error text a rejected write shows, which is not an entity name.
SOUND_DETECTION_NAMES: dict[str, str] = {
    "sound_enable": "Sound detection",
    "all_sound": "All sound detection",
    "glass_Break": "Glass break detection",
    "smoke_T3": "Smoke alarm detection (T3)",
    "smoke_T4": "Smoke alarm detection (T4)",
    "baby_cry": "Baby cry detection",
    "dog_bark": "Dog bark detection",
}


class AidotSoundDetectionSwitch(AidotEntity, SwitchEntity):
    """One sound detector on one camera.

    These are not cloud attributes: the camera reports them over an MQTT round
    trip that the coordinator refreshes on a slow cadence. So the switch reads
    from the cached value and forces a refresh after a write, rather than
    waiting up to half an hour to show what it just did.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key=f"sound_{key}")
        self._key = key
        translation_key = SOUND_DETECTION_TRANSLATION_KEYS.get(key)
        if translation_key is not None:
            self._attr_translation_key = translation_key
        else:
            # A detector we have no name for. Fall back to the wire key made
            # readable, capitalising only the first letter: .capitalize() would
            # lower the rest and turn "smoke_T3" into "Smoke t3".
            pretty = key.replace("_", " ").strip()
            self._attr_name = pretty[:1].upper() + pretty[1:]

    @property
    def is_on(self) -> bool | None:
        """None when the camera has not told us -- unknown, not off."""
        flags = (self.coordinator.camera_extras or {}).get("sound")
        if not isinstance(flags, dict):
            return None
        return flags.get(self._key)

    async def _async_set(self, on: bool) -> None:
        # async_run_command, not a bare await: the library setter returns False
        # when the camera does not answer soundAlgorithmGet or does not report
        # the key, writing nothing. Discarding that turned a refused write into
        # a silent no-op that the read-back then quietly reverted, which is the
        # "pretending a command was delivered" the README promises we do not do.
        label = SOUND_DETECTION_NAMES.get(self._key, self._key)
        await self.async_run_command(
            self.coordinator.device_client.async_set_sound_detection(self._key, on),
            f"turn {'on' if on else 'off'} {label}",
        )
        # Read back rather than assume: the write is read-modify-write against
        # the camera's own list, and a refused write must not leave the UI
        # showing a state the camera never took. Only the sound key can have
        # changed; re-reading wifi and SD here would add two more 25 s waits
        # inside a service call on a PARALLEL_UPDATES = 1 platform.
        await self.coordinator.async_refresh_camera_extras(only="sound")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera switches."""
    coordinator = entry.runtime_data
    registered: set[str] = set()
    gated: set = set()

    def _add_new_switches() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new: list[SwitchEntity] = [
            AidotCameraSwitch(c, desc)
            for c, desc in _pending_switches(
                coordinator.camera_coordinators, registered, gated)
        ]
        # The serve-audio toggle only applies to SDES (battery) cameras.
        new += [
            AidotCameraAudioSwitch(c)
            for c in new_coords.values()
            if getattr(c.device_client, "is_sdes_camera", False)
        ]
        # Sound-detection switches appear once the camera has reported which
        # detectors it has. That is a slow round trip, so they arrive after the
        # first extras refresh rather than at setup -- the same late-add path
        # every other camera entity already uses.
        for dev_id, c in coordinator.camera_coordinators.items():
            flags = (getattr(c, "camera_extras", {}) or {}).get("sound")
            if not isinstance(flags, dict):
                continue
            for key in flags:
                marker = f"{dev_id}:sound:{key}"
                if marker in registered:
                    continue
                registered.add(marker)
                new.append(AidotSoundDetectionSwitch(c, key))

        if new:
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
            entry.async_on_unload(cam.async_add_listener(lambda: _add_new_switches()))

    def _refresh_all() -> None:
        _hook_camera_coordinators()
        _add_new_switches()

    _refresh_all()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_all))


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
