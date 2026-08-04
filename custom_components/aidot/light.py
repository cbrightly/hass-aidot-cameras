"""Support for Aidot lights."""

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGBW_COLOR,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity, aidot_device_info, remove_stale_switch_entity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot bulbs (device coordinators) and camera floodlights (camera coordinators)."""
    coordinator = entry.runtime_data
    registered: set[str] = set()
    registered_flood: set[str] = set()

    def _add_new_lights() -> None:
        new_devs = {
            dev_id: c
            for dev_id, c in coordinator.device_coordinators.items()
            if dev_id not in registered
        }
        if new_devs:
            registered.update(new_devs)
            async_add_entities([AidotLight(c) for c in new_devs.values()])
        # Camera floodlight/spotlight as a light entity (its own set), so it's out
        # of the switch domain (not hit by blanket switch.turn_on automations).
        new_flood = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered_flood
        }
        if new_flood:
            registered_flood.update(new_flood)
            for c in new_flood.values():
                # Drop the pre-migration switch.<name>_floodlight orphan so it
                # doesn't linger as a stale switch beside the light entity.
                remove_stale_switch_entity(hass, c.device_client.info.dev_id, "floodlight")
            async_add_entities([AidotCameraFloodlight(c) for c in new_flood.values()])

    _add_new_lights()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_lights()))


class AidotCameraFloodlight(AidotEntity, LightEntity):
    """The camera's floodlight/spotlight, as an on/off light.

    A ``light`` (not a ``switch``) so it's separate from the switch domain, and in
    the device's *config* section - both reduce accidental activation of a bright
    outdoor light by stray switch automations or mis-taps.
    """

    _attr_translation_key = "floodlight"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, coordinator: AidotDeviceUpdateCoordinator) -> None:
        super().__init__(coordinator, key="floodlight")
        self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        # floodlight is a camera-only status field (CameraStatusData), not on the
        # base DeviceStatusData type the coordinator is generically typed to.
        return getattr(self.coordinator.data, "floodlight", None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.device_client.async_set_floodlight(True), f"turn on {self.name}"
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.device_client.async_set_floodlight(False), f"turn off {self.name}"
        )


class AidotLight(CoordinatorEntity[AidotDeviceUpdateCoordinator], LightEntity):
    """Representation of an Aidot Wi-Fi Light."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: AidotDeviceUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.device_client.info.dev_id
        # Always set kelvin bounds (with HA defaults) so color-temp lights never
        # fall back to the deprecated mireds properties - RGBW lights enable
        # COLOR_TEMP without a CCT service that provides cct_min/cct_max.
        self._attr_max_color_temp_kelvin = (
            getattr(coordinator.device_client.info, "cct_max", None) or DEFAULT_MAX_KELVIN
        )
        self._attr_min_color_temp_kelvin = (
            getattr(coordinator.device_client.info, "cct_min", None) or DEFAULT_MIN_KELVIN
        )

        entry = getattr(coordinator, "config_entry", None)
        self._attr_device_info = aidot_device_info(
            coordinator.device_client.info, entry.entry_id if entry else None
        )
        if coordinator.device_client.info.enable_rgbw:
            self._attr_color_mode = ColorMode.RGBW
            self._attr_supported_color_modes = {ColorMode.RGBW, ColorMode.COLOR_TEMP}
        elif coordinator.device_client.info.enable_cct:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._update_status()

    def _update_status(self) -> None:
        if self.coordinator.data is None:
            return
        self._attr_is_on = self.coordinator.data.on
        self._attr_brightness = self.coordinator.data.dimming
        self._attr_color_temp_kelvin = self.coordinator.data.cct
        self._attr_rgbw_color = self.coordinator.data.rgbw
        # Keep color_mode in sync with the device for dual-mode (RGBW + CCT)
        # bulbs. Previously color_mode only changed inside async_turn_on, so an
        # external CCT<->colour switch (the app or another HA) left HA on a stale
        # mode and it rendered the wrong active control (and logged an
        # attribute/mode-mismatch warning). The device exposes no explicit mode
        # flag, so infer it: a non-zero R/G/B means colour (RGBW), otherwise
        # colour-temperature. Single-mode bulbs keep their fixed mode.
        modes = self._attr_supported_color_modes or set()
        if ColorMode.RGBW in modes and ColorMode.COLOR_TEMP in modes:
            rgbw = self.coordinator.data.rgbw
            if rgbw is not None and any(rgbw[:3]):
                self._attr_color_mode = ColorMode.RGBW
            else:
                self._attr_color_mode = ColorMode.COLOR_TEMP

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.online
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_status()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            await self.coordinator.device_client.async_set_brightness(brightness)
            self.coordinator.data.dimming = brightness
            self._attr_brightness = brightness
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            await self.coordinator.device_client.async_set_cct(color_temp_kelvin)
            self.coordinator.data.cct = color_temp_kelvin
            self._attr_color_temp_kelvin = color_temp_kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ATTR_RGBW_COLOR in kwargs:
            rgbw_color = kwargs[ATTR_RGBW_COLOR]
            await self.coordinator.device_client.async_set_rgbw(rgbw_color)
            self.coordinator.data.rgbw = rgbw_color
            self._attr_rgbw_color = rgbw_color
            self._attr_color_mode = ColorMode.RGBW
        else:
            await self.coordinator.device_client.async_turn_on()

        self.coordinator.data.on = True
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.device_client.async_turn_off()
        self.coordinator.data.on = False
        self._attr_is_on = False
        self.async_write_ha_state()
