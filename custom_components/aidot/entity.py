"""Base entity for Aidot devices - shared DeviceInfo + failure-surfacing commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from aidot_cameras.device_client import DeviceInformation

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    AidotCameraUpdateCoordinator,
    AidotConfigEntry,
    AidotDeviceUpdateCoordinator,
)


#: Whether the running Home Assistant takes `via_device_id` rather than the
#: `via_device` it removed in 2026.9. Read from the installed HA rather than
#: guessed from a version number, because that is the thing that decides.
_SUPPORTS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def _via_device_extra(
    entry_id: "str | None", hub_device_id: "str | None", *, supports_via_device_id: bool
) -> "dict[str, Any]":
    """How to link a device to the account hub on THIS Home Assistant.

    The two spellings are not interchangeable. `via_device` takes an identifier
    tuple `(DOMAIN, config_entry_id)`; `via_device_id` takes the hub device's
    REGISTRY id. Passing one where the other belongs links nothing.

    2026.9 removed `via_device` and made passing it raise a RuntimeError from
    inside `device_registry.async_get_or_create` - which surfaces as
    "Error adding entity None" and takes the entity out entirely. So on a Home
    Assistant that wants the id, an unresolvable hub links NOTHING rather than
    falling back to the key that raises: losing the grouping is cosmetic,
    losing the entity is not.
    """
    if entry_id is None:
        return {}
    if supports_via_device_id:
        return {"via_device_id": hub_device_id} if hub_device_id else {}
    return {"via_device": (DOMAIN, entry_id)}


def aidot_device_info(
    info: DeviceInformation,
    via_device_id: str | None = None,
    hub_device_id: str | None = None,
) -> DeviceInfo:
    """Build the HA DeviceInfo for an Aidot device from its library info.

    ``via_device_id`` is the CONFIG ENTRY id and ``hub_device_id`` the hub
    device's registry id; between them they link the device to the integration's
    hub (the AiDot account) so HA groups lights and cameras under it - the
    manifest declares ``integration_type: hub``. Which one is used depends on
    the Home Assistant running; see :func:`_via_device_extra`.
    """
    model_id = info.model_id or ""
    manufacturer = model_id.split(".")[0] if model_id else "AiDot"
    model = model_id[len(manufacturer) + 1 :] if model_id else model_id
    mac = info.mac or ""
    extra: dict[str, Any] = _via_device_extra(
        via_device_id, hub_device_id, supports_via_device_id=_SUPPORTS_VIA_DEVICE_ID
    )
    return DeviceInfo(
        identifiers={(DOMAIN, info.dev_id)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        manufacturer=manufacturer,
        model=model,
        name=info.name,
        hw_version=info.hw_version,
        **extra,
    )


def device_info_for(coordinator: AidotDeviceUpdateCoordinator) -> DeviceInfo:
    """The DeviceInfo for the device behind ``coordinator``.

    Reads the config entry off the coordinator when it has one (the stubs in
    tests do not always), and the hub device's registry id off the entry's
    runtime data when that has been stashed. Every entity on a device builds
    its info this way, whether or not it is a coordinator entity.
    """
    entry = getattr(coordinator, "config_entry", None)
    return aidot_device_info(
        coordinator.device_client.info,
        entry.entry_id if entry else None,
        getattr(getattr(entry, "runtime_data", None), "hub_device_id", None),
    )


def forget_departed(memo: set, live: Mapping[str, object]) -> None:
    """Drop what ``memo`` remembers about devices no longer in ``live``.

    A device that leaves the account has its coordinator dropped and its
    device entry detached, which removes its entities; if it comes back it gets
    a fresh coordinator and needs them made again. The platforms remember what
    they added by device id - a bare id, an ``"<id>:..."`` marker, or an
    ``(id, key)`` pair - so each memo forgets a departed device here.
    """
    memo.difference_update(
        [
            m
            for m in memo
            if (m[0] if isinstance(m, tuple) else m.split(":", 1)[0]) not in live
        ]
    )


def pending_coordinators[CoordinatorT: AidotDeviceUpdateCoordinator](
    coordinators: Mapping[str, CoordinatorT], registered: set[str]
) -> dict[str, CoordinatorT]:
    """The coordinators ``registered`` does not hold yet, keyed by device id.

    The caller records them once it has made their entities, so a device whose
    entities could not be made this time is looked at again on the next update.
    Devices no longer in ``coordinators`` are forgotten from ``registered``
    first, so one that leaves and comes back is added again.
    """
    forget_departed(registered, coordinators)
    return {dev_id: c for dev_id, c in coordinators.items() if dev_id not in registered}


def async_add_when_discovered(
    entry: AidotConfigEntry,
    add_new: Callable[[], None],
    *,
    hook_cameras: bool = False,
) -> None:
    """Run ``add_new`` now and again on every account refresh.

    This is how every platform picks up a camera that joins the account after
    setup: the account coordinator refreshes its device list, and ``add_new``
    makes entities for the ones it has not seen yet. The listener lives as
    long as the config entry does.

    ``hook_cameras`` also runs it on every PER-CAMERA refresh. An entity whose
    data comes from the camera itself (an extras value, a setting the camera
    reports) appears only once that camera answers, and the answer updates the
    camera's own coordinator; the account coordinator's listener does not fire
    for it, so without the hook such entities are fetched successfully and
    never created. Each camera is hooked once, as it is discovered.
    """
    coordinator = entry.runtime_data
    if not hook_cameras:
        add_new()
        entry.async_on_unload(coordinator.async_add_listener(add_new))
        return

    hooked: set[str] = set()

    def _refresh_all() -> None:
        # A camera that comes back has a fresh coordinator to hook.
        forget_departed(hooked, coordinator.camera_coordinators)
        for dev_id, cam in coordinator.camera_coordinators.items():
            if dev_id in hooked:
                continue
            hooked.add(dev_id)
            entry.async_on_unload(cam.async_add_listener(add_new))
        add_new()

    _refresh_all()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_all))


def async_add_per_camera(
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[AidotCameraUpdateCoordinator], Iterable[Entity]],
) -> None:
    """Add ``factory``'s entities for each camera once, now and as cameras appear.

    A camera is recorded only when it produced entities, so one the factory
    makes nothing for is asked again on the next refresh.
    """
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new() -> None:
        new_coords = pending_coordinators(coordinator.camera_coordinators, registered)
        new = [entity for c in new_coords.values() for entity in factory(c)]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    async_add_when_discovered(entry, _add_new)


class AidotEntity(CoordinatorEntity[AidotDeviceUpdateCoordinator]):
    """Common base: builds DeviceInfo once and runs library commands safely."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AidotDeviceUpdateCoordinator, key: str | None = None
    ) -> None:
        super().__init__(coordinator)
        if key is not None:
            self._attr_unique_id = f"{coordinator.device_client.info.dev_id}_{key}"
        self._attr_device_info = device_info_for(coordinator)

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
