"""Aidot camera siren.

Exposed as a dedicated ``siren`` entity (not a generic ``switch``) so it is
separate from the switch domain - blanket ``switch.turn_on`` automations and the
generic Switches card can't fire it - and it sits in the device's *config*
section (``EntityCategory.CONFIG``). Both reduce accidental/easy activation of a
loud, neighbour-disturbing actuator.
"""

from __future__ import annotations

from typing import Any

from aidot.camera.models import CameraStatusData
from homeassistant.components.siren import SirenEntity
from homeassistant.components.siren.const import SirenEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity, remove_stale_switch_entity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Aidot camera siren entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_sirens() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        if new_coords:
            registered.update(new_coords)
            for c in new_coords.values():
                # Drop the pre-migration switch.<name>_siren orphan so users
                # don't see a stale on/off switch alongside the siren entity.
                remove_stale_switch_entity(hass, c.device_client.info.dev_id, "siren")
            async_add_entities(AidotCameraSiren(c) for c in new_coords.values())

    _add_new_sirens()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_sirens))


class AidotCameraSiren(AidotEntity, SirenEntity):
    """The camera's built-in siren/alarm."""

    _attr_translation_key = "siren"
    _attr_icon = "mdi:alarm-light"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = (
        SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: AidotDeviceUpdateCoordinator) -> None:
        super().__init__(coordinator, key="siren")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if not isinstance(data, CameraStatusData):
            return None
        return data.siren

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.device_client.async_set_siren(True), f"turn on {self.name}"
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.device_client.async_set_siren(False), f"turn off {self.name}"
        )
