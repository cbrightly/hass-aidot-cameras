"""Base entity for Aidot devices - shared DeviceInfo + failure-surfacing commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

from aidot_cameras.device_client import DeviceInformation

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    AidotCameraUpdateCoordinator,
    AidotConfigEntry,
    AidotDeviceUpdateCoordinator,
)
from .model_profile import declares

_LOGGER = logging.getLogger(__name__)

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
    sw_version: str | None = None,
) -> DeviceInfo:
    """Build the HA DeviceInfo for an Aidot device from its library info.

    ``via_device_id`` is the CONFIG ENTRY id and ``hub_device_id`` the hub
    device's registry id; between them they link the device to the integration's
    hub (the AiDot account) so HA groups lights and cameras under it - the
    manifest declares ``integration_type: hub``. Which one is used depends on
    the Home Assistant running; see :func:`_via_device_extra`. ``sw_version``
    is the firmware version the cloud reports, left out when unknown so it
    does not blank a version already in the registry.
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
        **({"sw_version": sw_version} if sw_version else {}),
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
    runtime = getattr(entry, "runtime_data", None)
    info = coordinator.device_client.info
    return aidot_device_info(
        info,
        entry.entry_id if entry else None,
        getattr(runtime, "hub_device_id", None),
        # Cameras only: a bulb's library info has no firmware field.
        getattr(info, "firmware_version", None),
    )


def remove_registry_row(hass: HomeAssistant, platform: str, unique_id: str) -> None:
    """Delete this integration's registry row for ``unique_id``, if there is one.

    Not creating an entity is not enough to retire it: Home Assistant keeps the
    row and shows it as a permanently unavailable control.
    """
    reg = er.async_get(hass)
    if eid := reg.async_get_entity_id(platform, DOMAIN, unique_id):
        _LOGGER.debug("removing %s", eid)
        reg.async_remove(eid)


@dataclass(frozen=True, kw_only=True)
class AidotEntityDescription(EntityDescription):
    """What every described Aidot camera entity has in common."""

    #: The property the model's cloud profile must declare for this entity to
    #: exist, as the app gates the same setting (model_profile). None = not
    #: gated on the profile. An unreadable profile keeps the entity.
    profile_property: str | None = None


#: What a camera platform has made, by device id: the keys of the entities (or
#: batches of entities) already added, or settled as never to be added.
Memo = dict[str, set[str]]


def forget_departed(memo: Memo, live: Mapping[str, object]) -> None:
    """Drop what ``memo`` remembers about devices no longer in ``live``.

    A device that leaves the account has its coordinator dropped and its
    device entry detached, which removes its entities; if it comes back it gets
    a fresh coordinator and needs them made again.
    """
    for dev_id in memo.keys() - live.keys():
        del memo[dev_id]


def pending_coordinators[CoordinatorT: AidotDeviceUpdateCoordinator](
    coordinators: Mapping[str, CoordinatorT], registered: set[str]
) -> dict[str, CoordinatorT]:
    """The coordinators ``registered`` does not hold yet, keyed by device id.

    The caller records them once it has made their entities, so a device whose
    entities could not be made this time is looked at again on the next update.
    Devices no longer in ``coordinators`` are forgotten from ``registered``
    first, so one that leaves and comes back is added again.
    """
    registered.intersection_update(coordinators)
    return {dev_id: c for dev_id, c in coordinators.items() if dev_id not in registered}


def async_add_when_discovered(
    entry: AidotConfigEntry,
    add_new: Callable[[], None],
    *,
    on_camera_update: Callable[[str, AidotCameraUpdateCoordinator], None] | None = None,
) -> None:
    """Run ``add_new`` now and again on every account refresh.

    This is how every platform picks up a camera that joins the account after
    setup: the account coordinator refreshes its device list, and ``add_new``
    makes entities for the ones it has not seen yet. The listener lives as
    long as the config entry does.

    ``on_camera_update(dev_id, camera)`` also runs on every PER-CAMERA refresh.
    An entity whose data comes from the camera itself (an extras value, a
    setting the camera reports) appears only once that camera answers, and the
    answer updates the camera's own coordinator; the account coordinator's
    listener does not fire for it, so without the hook such entities are
    fetched successfully and never created. Each camera is hooked once, as it
    is discovered, and the hook is a no-op once that coordinator has been
    replaced or dropped.
    """
    coordinator = entry.runtime_data
    if on_camera_update is None:
        add_new()
        entry.async_on_unload(coordinator.async_add_listener(add_new))
        return

    hooked: set[str] = set()

    def _hook(dev_id: str, cam: AidotCameraUpdateCoordinator) -> Callable[[], None]:
        def _on_update() -> None:
            if coordinator.camera_coordinators.get(dev_id) is cam:
                on_camera_update(dev_id, cam)

        return _on_update

    def _refresh_all() -> None:
        cameras = coordinator.camera_coordinators
        # A camera that comes back has a fresh coordinator to hook.
        hooked.intersection_update(cameras)
        for dev_id, cam in cameras.items():
            if dev_id not in hooked:
                hooked.add(dev_id)
                entry.async_on_unload(cam.async_add_listener(_hook(dev_id, cam)))
        add_new()

    _refresh_all()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_all))


def async_add_camera_entities(
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
    collect: Callable[
        [Mapping[str, AidotCameraUpdateCoordinator], Memo], Iterable[Entity]
    ],
    *,
    hook_cameras: bool = False,
) -> None:
    """Add what ``collect`` returns, now and on every refresh.

    The one setup path for the camera platforms. ``collect(cameras, registered)``
    gets camera coordinators by device id and the platform's memo, and returns
    the entities to add, recording in the memo what it made them for. Departed
    devices are forgotten on each account refresh, so a camera that comes back
    gets its entities again. ``hook_cameras`` also collects on each camera's
    own refresh, for that camera alone (see async_add_when_discovered).
    """
    coordinator = entry.runtime_data
    registered: Memo = {}

    def _add(cameras: Mapping[str, AidotCameraUpdateCoordinator]) -> None:
        if new := list(collect(cameras, registered)):
            async_add_entities(new)

    def _add_new() -> None:
        cameras = coordinator.camera_coordinators
        forget_departed(registered, cameras)
        _add(cameras)

    async_add_when_discovered(
        entry,
        _add_new,
        on_camera_update=(
            (lambda dev_id, cam: _add({dev_id: cam})) if hook_cameras else None
        ),
    )


def per_camera_entities(
    cameras: Mapping[str, AidotCameraUpdateCoordinator],
    registered: Memo,
    make: Callable[[AidotCameraUpdateCoordinator], Iterable[Entity]],
    *,
    marker: str = "",
    supported: Callable[[AidotCameraUpdateCoordinator], bool] | None = None,
) -> list[Entity]:
    """``make``'s entities for each camera not yet recorded under ``marker``.

    A camera is recorded only once something was made for it, so one that
    ``supported`` turns away, or that made nothing, is looked at again on the
    next refresh. ``marker`` tells apart batches a platform makes separately.
    """
    new: list[Entity] = []
    for dev_id, c in cameras.items():
        done = registered.setdefault(dev_id, set())
        if marker in done or (supported is not None and not supported(c)):
            continue
        if made := list(make(c)):
            done.add(marker)
            new += made
    return new


def keyed_entities[KeyT](
    cameras: Mapping[str, AidotCameraUpdateCoordinator],
    registered: Memo,
    keys: Callable[[AidotCameraUpdateCoordinator], Iterable[KeyT]],
    make: Callable[[AidotCameraUpdateCoordinator, KeyT], Entity],
    *,
    marker: str,
) -> list[Entity]:
    """``make(camera, key)`` for each key ``keys(camera)`` gives, once per key.

    For entities the camera itself enumerates (its sound detectors, say).
    Recorded as ``"<marker>:<key>"``, so families sharing a memo stay apart.
    """
    new: list[Entity] = []
    for dev_id, c in cameras.items():
        done = registered.setdefault(dev_id, set())
        for key in keys(c):
            if (m := f"{marker}:{key}") not in done:
                done.add(m)
                new.append(make(c, key))
    return new


def described_entities[DescT: AidotEntityDescription](
    cameras: Mapping[str, AidotCameraUpdateCoordinator],
    registered: Memo,
    descriptions: Iterable[DescT],
    make: Callable[[AidotCameraUpdateCoordinator, DescT], Entity],
    supported: Callable[[AidotCameraUpdateCoordinator, DescT], bool] | None = None,
    *,
    hass: HomeAssistant,
    platform: str,
) -> list[Entity]:
    """``make(camera, description)`` for each pair not made yet that is supported.

    Recorded per ``(camera, key)`` and only once made: a camera can report a
    setting, or a capability, after setup, so a pair ``supported`` turns away
    is asked again on every refresh. A pair whose model does not declare the
    description's ``profile_property`` is settled at once, and the registry
    row an earlier release made for it removed: a model's declaration does not
    change while the entry is loaded.
    """
    new: list[Entity] = []
    for dev_id, c in cameras.items():
        done = registered.setdefault(dev_id, set())
        for desc in descriptions:
            if desc.key in done:
                continue
            prop = desc.profile_property
            if prop is not None and not declares(c.device_client, prop):
                done.add(desc.key)
                _LOGGER.debug("%s: this model does not declare %s", dev_id, prop)
                remove_registry_row(hass, platform, f"{dev_id}_{desc.key}")
                continue
            if supported is not None and not supported(c, desc):
                continue
            done.add(desc.key)
            new.append(make(c, desc))
    return new


def reports_setting(
    coordinator: AidotCameraUpdateCoordinator, description: Any
) -> bool:
    """A description's ``is_supported(data, client)`` gate; None means every camera."""
    gate = description.is_supported
    return gate is None or gate(coordinator.data or object(), coordinator.device_client)


def async_add_per_camera(
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[AidotCameraUpdateCoordinator], Iterable[Entity]],
) -> None:
    """Add ``factory``'s entities for each camera once, now and as cameras appear."""
    async_add_camera_entities(
        entry,
        async_add_entities,
        lambda cameras, registered: per_camera_entities(cameras, registered, factory),
    )


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
