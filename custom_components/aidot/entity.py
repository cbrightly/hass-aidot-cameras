"""Base entity for Aidot devices - shared DeviceInfo + failure-surfacing commands."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from aidot.device_client import DeviceInformation

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AidotDeviceUpdateCoordinator


def remove_stale_switch_entity(hass: HomeAssistant, dev_id: str, suffix: str) -> None:
    """Remove a pre-migration ``switch.*`` entity orphaned in the registry.

    The siren and floodlight controls used to be ``switch`` entities; they moved
    to dedicated ``siren`` / ``light`` domains (reusing the same unique_id,
    ``f"{dev_id}_{suffix}"``, in the new domain). HA leaves the old switch entry
    behind as a restored/unavailable entity, so the user still sees a stale
    ``switch.<name>_siren`` toggle. This removes only that old switch entry
    (matched by domain=switch + unique_id); the new-domain entity is untouched.
    """
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id("switch", DOMAIN, f"{dev_id}_{suffix}")
    if eid:
        ent_reg.async_remove(eid)


def aidot_device_info(info: DeviceInformation, via_device_id: str | None = None) -> DeviceInfo:
    """Build the HA DeviceInfo for an Aidot device from its library info.

    ``via_device_id`` links the device to the integration's hub device (the
    AiDot account) so HA groups lights and cameras under it - the manifest
    declares ``integration_type: hub``.
    """
    model_id = info.model_id or ""
    manufacturer = model_id.split(".")[0] if model_id else "AiDot"
    model = model_id[len(manufacturer) + 1:] if model_id else model_id
    mac = info.mac or ""
    extra: dict[str, Any] = {}
    if via_device_id is not None:
        extra["via_device"] = (DOMAIN, via_device_id)
    return DeviceInfo(
        identifiers={(DOMAIN, info.dev_id)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        manufacturer=manufacturer,
        model=model,
        name=info.name,
        hw_version=info.hw_version,
        **extra,
    )


class AidotEntity(CoordinatorEntity[AidotDeviceUpdateCoordinator]):
    """Common base: builds DeviceInfo once and runs library commands safely."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AidotDeviceUpdateCoordinator, key: str | None = None
    ) -> None:
        super().__init__(coordinator)
        info = coordinator.device_client.info
        if key is not None:
            self._attr_unique_id = f"{info.dev_id}_{key}"
        entry = getattr(coordinator, "config_entry", None)
        self._attr_device_info = aidot_device_info(
            info, entry.entry_id if entry else None
        )

    @property
    def device_client(self):
        """The underlying aidot DeviceClient for this entity."""
        return self.coordinator.device_client

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator has no data or the device is offline."""
        if not super().available:
            return False
        data = self.coordinator.data
        if data is None:
            return False
        return getattr(data, "online", True)

    async def async_run_command(self, coro: Awaitable[Any], action: str) -> None:
        """Await a library command, surfacing failures to the user.

        Library setters return ``False`` when the device rejects the change and
        may raise on network/auth errors; both become a ``HomeAssistantError``
        (which HA shows to the user) instead of an optimistic silent success.
        On success the entity state is written immediately.
        """
        try:
            ok = await coro
        except HomeAssistantError:
            raise
        except Exception as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"action": action, "error": str(exc)},
            ) from exc
        if ok is False:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"action": action},
            )
        self.async_write_ha_state()
