"""Support for Aidot camera switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import (
    AidotCameraUpdateCoordinator,
    AidotConfigEntry,
    AidotDeviceUpdateCoordinator,
    supports_ptz,
)
from .entity import (
    AidotEntity,
    AidotEntityDescription,
    async_add_camera_entities,
    described_entities,
    keyed_entities,
    per_camera_entities,
)
from .library_settings import camera_audio_default

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AidotSwitchDescription(SwitchEntityDescription, AidotEntityDescription):
    """Describes an Aidot camera switch."""

    get_is_on: Any = None  # callable(DeviceStatusData) -> bool | None
    async_turn_on_fn: Any = None  # async callable(DeviceClient) -> bool
    async_turn_off_fn: Any = None  # async callable(DeviceClient) -> bool
    #: callable(coordinator) -> bool. None = every camera gets this switch.
    #: A control the model cannot perform must NOT ship: this firmware
    #: acknowledges writes it then ignores, so an ungated switch is one the
    #: user can toggle with no effect and no error (see the ir_light note).
    supported_fn: Any = None


#: Declarations read live from the cloud profiles (2026-11-07), for the
#: properties these switches, the floodlight and the siren could gate on:
#:   A001513 (L2)      lacks HDRStatus and sirenRing
#:   A001064 (PTZ)     lacks sirenRing
#:   A000088 (M3 Pro)  lacks voiceEnable and sirenRing
#: So the L2 gets no WDR and the M3 Pro no Voice prompts, as in the app. No
#: model declares sirenRing, which is why the siren is not gated (siren.py).
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
        profile_property="OSDEnable",
    ),
    AidotSwitchDescription(
        key="auto_light",
        translation_key="auto_light",
        icon="mdi:lightbulb-auto",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.auto_light,
        async_turn_on_fn=lambda c: c.async_set_auto_light(True),
        async_turn_off_fn=lambda c: c.async_set_auto_light(False),
        profile_property="autoLightEnable",
    ),
    AidotSwitchDescription(
        key="voice_prompts",
        translation_key="voice_prompts",
        icon="mdi:account-voice",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.voice_prompts,
        async_turn_on_fn=lambda c: c.async_set_voice_prompts(True),
        async_turn_off_fn=lambda c: c.async_set_voice_prompts(False),
        profile_property="voiceEnable",
    ),
    AidotSwitchDescription(
        key="hdr",
        translation_key="hdr",
        icon="mdi:hdr",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.hdr,
        async_turn_on_fn=lambda c: c.async_set_hdr(True),
        async_turn_off_fn=lambda c: c.async_set_hdr(False),
        profile_property="HDRStatus",
    ),
    AidotSwitchDescription(
        key="status_led",
        translation_key="status_led",
        icon="mdi:led-on",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.status_led,
        async_turn_on_fn=lambda c: c.async_set_status_led(True),
        async_turn_off_fn=lambda c: c.async_set_status_led(False),
        profile_property="LedOnOff",
    ),
    AidotSwitchDescription(
        key="microphone",
        translation_key="microphone",
        icon="mdi:microphone",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.microphone,
        async_turn_on_fn=lambda c: c.async_set_microphone(True),
        async_turn_off_fn=lambda c: c.async_set_microphone(False),
        profile_property="micEnable",
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


def _switch_supported(c, desc: AidotSwitchDescription) -> bool:
    """Whether this camera gets the switch now.

    Asked again on every refresh until it says yes: ``ptz_directions`` is
    empty at setup and arrives on a later refresh, and a camera recorded as
    done at setup would never get its PTZ switch. The profile gate is
    described_entities' own.
    """
    return desc.supported_fn is None or desc.supported_fn(c)


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


#: Detection types the camera reports under getRoiHuman. `publicZone` rides in
#: the same object but is not a detection type -- it is carried through by the
#: library's read-modify-write and deliberately gets no switch.
DETECTION_TYPE_TRANSLATION_KEYS: dict[str, str] = {
    "humanDetect": "detect_human",
    "vehicleDetect": "detect_vehicle",
    "packageDetect": "detect_package",
    "petDetect": "detect_pet",
}

#: For the error text a rejected write shows, which is not an entity name.
DETECTION_TYPE_NAMES: dict[str, str] = {
    "humanDetect": "Person detection",
    "vehicleDetect": "Vehicle detection",
    "packageDetect": "Package detection",
    "petDetect": "Pet detection",
}


#: The siren's automatic trigger: the master, and the events that fire it.
#: The app calls it "Auto Alarm", with the triggers "Motion Detected", "Person
#: Detected" and "Glass Break Detected" (its getAutoAlarm keys). A camera that
#: does not report a trigger gets no switch for it, so the glass-break one
#: appears only on a model that has it.
AUTO_ALARM_TRANSLATION_KEYS: dict[str, str] = {
    "autoAlarm": "auto_siren",
    "motionDetection": "auto_siren_on_motion",
    "humanDetect": "auto_siren_on_person",
    "glassBreak": "auto_siren_on_glass_break",
}

AUTO_ALARM_NAMES: dict[str, str] = {
    "autoAlarm": "Auto alarm",
    "motionDetection": "Auto alarm on motion",
    "humanDetect": "Auto alarm on person",
    "glassBreak": "Auto alarm on glass break",
}


@dataclass(frozen=True)
class ExtrasSwitchFamily:
    """One family of camera-reported switches: where it reads, how it writes."""

    #: The key under ``camera_extras`` the camera's answer lands in; also the
    #: ``only=`` the read-back after a write refreshes.
    extras_key: str
    #: Unique ids are ``f"{dev_id}_{unique_prefix}_{key}"``.
    unique_prefix: str
    #: Wire key -> translation key, and wire key -> the label a rejected write
    #: shows (which is not an entity name).
    translation_keys: dict[str, str]
    names: dict[str, str]
    #: The DeviceClient method, called as ``(key, on)``.
    setter: str
    #: Whether a key the camera reports but we have no name for still gets a
    #: switch, named from the key. The other families keep to the keys named.
    accept_unknown_keys: bool = False

    def reported_keys(self, coordinator: AidotCameraUpdateCoordinator) -> list[str]:
        """The keys of this family the camera has reported, that get a switch."""
        flags = coordinator.camera_extras.get(self.extras_key)
        if not isinstance(flags, dict):
            return []
        return [
            key
            for key in flags
            if self.accept_unknown_keys or key in self.translation_keys
        ]


class AidotExtrasSwitch(AidotEntity, SwitchEntity):
    """One camera-reported flag on one camera, as a switch.

    These are not cloud attributes: the camera reports them over an MQTT round
    trip that the coordinator refreshes on a slow cadence. So the switch reads
    from the cached value and forces a refresh after a write, rather than
    waiting up to half an hour to show what it just did.
    """

    _attr_entity_category = EntityCategory.CONFIG
    family: ExtrasSwitchFamily

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key=f"{self.family.unique_prefix}_{key}")
        self._key = key
        if not self.family.accept_unknown_keys:
            self._attr_translation_key = self.family.translation_keys[key]
            return
        translation_key = self.family.translation_keys.get(key)
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
        """None when the camera has not told us -- unknown, not off.

        A battery camera answers nothing at all here (it is asleep), and a
        row of confidently-off switches would be a state it never reported.
        Least safe of all to guess at is the automatic siren: a confident
        "off" there would read as "the siren will not sound" without the
        camera having said so.
        """
        flags = (self.coordinator.camera_extras or {}).get(self.family.extras_key)
        if not isinstance(flags, dict):
            return None
        return flags.get(self._key)

    async def _async_set(self, on: bool) -> None:
        # async_run_command, not a bare await: the library setter returns False
        # when the camera does not answer the read or does not report the key,
        # writing nothing. Discarding that turned a refused write into a silent
        # no-op that the read-back then quietly reverted, which is the
        # "pretending a command was delivered" the README promises we do not do.
        label = self.family.names.get(self._key, self._key)
        setter = getattr(self.coordinator.device_client, self.family.setter)
        await self.async_run_command(
            setter(self._key, on),
            f"turn {'on' if on else 'off'} {label}",
        )
        # Read back rather than assume: the write is read-modify-write against
        # the camera's own object, and a refused write must not leave the UI
        # showing a state the camera never took. Only this family's key can
        # have changed; re-reading wifi and SD here would add two more 25 s
        # waits inside a service call on a PARALLEL_UPDATES = 1 platform.
        await self.coordinator.async_refresh_camera_extras(only=self.family.extras_key)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


class AidotSoundDetectionSwitch(AidotExtrasSwitch):
    """One sound detector on one camera.

    Taken as the camera reports them: a detector we have no name for still
    gets a switch, because the camera is the authority on what it supports.
    """

    family = ExtrasSwitchFamily(
        extras_key="sound",
        unique_prefix="sound",
        translation_keys=SOUND_DETECTION_TRANSLATION_KEYS,
        names=SOUND_DETECTION_NAMES,
        setter="async_set_sound_detection",
        accept_unknown_keys=True,
    )


class AidotDetectionTypeSwitch(AidotExtrasSwitch):
    """One detection type on one camera: human, vehicle, package or pet.

    The cameras have always typed their detections -- `humanDetect` reads 1 on
    a stock A000088 and A001064 -- while Home Assistant offered only motion
    on/off and a sensitivity number. Only the four we have names for: the
    camera also reports publicZone in this object, which is not a detection
    type and is carried through by the library instead.
    """

    family = ExtrasSwitchFamily(
        extras_key="detect",
        unique_prefix="detect",
        translation_keys=DETECTION_TYPE_TRANSLATION_KEYS,
        names=DETECTION_TYPE_NAMES,
        setter="async_set_detection_type",
    )


class AidotAutoAlarmSwitch(AidotExtrasSwitch):
    """The camera's automatic siren, and what sets it off.

    Separate from the siren entity, which sounds it now. This decides whether
    the camera does so by itself. The master and the triggers are separate
    entities on purpose: the library writes one flag at a time against the
    camera's own object, so changing a trigger cannot arm the master.
    """

    family = ExtrasSwitchFamily(
        extras_key="alarm",
        unique_prefix="auto_alarm",
        translation_keys=AUTO_ALARM_TRANSLATION_KEYS,
        names=AUTO_ALARM_NAMES,
        setter="async_set_auto_alarm",
    )


#: In the order their switches are added.
EXTRAS_SWITCHES: tuple[type[AidotExtrasSwitch], ...] = (
    AidotSoundDetectionSwitch,
    AidotDetectionTypeSwitch,
    AidotAutoAlarmSwitch,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera switches."""

    def _collect(cameras, registered) -> list[SwitchEntity]:
        new: list[SwitchEntity] = described_entities(
            cameras,
            registered,
            CAMERA_SWITCHES,
            AidotCameraSwitch,
            _switch_supported,
            hass=hass,
            platform="switch",
        )
        # The serve-audio toggle only applies to SDES cameras (the mains
        # A001064 PTZ included, not just the battery ones).
        new += per_camera_entities(
            cameras,
            registered,
            lambda c: [AidotCameraAudioSwitch(c)],
            marker="camera_audio",
            supported=lambda c: c.device_client.is_sdes_camera,
        )
        # The camera-reported switches appear once the camera has said what it
        # has, after the first extras refresh. Memo per (camera, family, key).
        for cls in EXTRAS_SWITCHES:
            new += keyed_entities(
                cameras,
                registered,
                cls.family.reported_keys,
                cls,
                marker=cls.family.unique_prefix,
            )
        return new

    # The extras switches' data lands on the PER-CAMERA coordinator, so each
    # camera is hooked too.
    async_add_camera_entities(entry, async_add_entities, _collect, hook_cameras=True)


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

    This is a local streaming preference (not a device attribute). State is
    restored across restarts; on first run it is on, or off on an entry that
    had the retired global "SDES camera audio" option off (async_migrate_entry).
    The change takes effect the next time the camera's live view is opened
    (re-open the camera card to apply it immediately). Distinct from the Microphone (audio privacy)
    switch, which disables the camera mic everywhere (app + recordings).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "camera_audio"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG

    coordinator: AidotCameraUpdateCoordinator

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        super().__init__(coordinator, key="camera_audio")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            on = last.state == "on"
        else:
            entry = self.coordinator.config_entry
            on = camera_audio_default(entry.options if entry else {})
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
