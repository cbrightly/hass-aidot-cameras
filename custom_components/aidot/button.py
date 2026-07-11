"""PTZ button entities for Aidot cameras."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, cast

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import AidotEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AidotButtonDescription(ButtonEntityDescription):
    """Describes an Aidot PTZ button."""

    async_press_fn: object = None  # async callable(DeviceClient) -> bool


# Map TUTK IOCtrl direction codes (from ptzDirection product property) to button keys.
# 1=up, 2=down, 3=left, 6=right, 23=zoom_in, 24=zoom_out - confirmed from device data.
_DIRECTION_CODE_KEYS: dict[int, str] = {
    1: "ptz_up",
    2: "ptz_down",
    3: "ptz_left",
    6: "ptz_right",
    23: "ptz_zoom_in",
    24: "ptz_zoom_out",
}

PTZ_BUTTONS: tuple[AidotButtonDescription, ...] = (
    AidotButtonDescription(
        key="ptz_up",
        translation_key="ptz_up",
        icon="mdi:arrow-up-circle-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_move("up"),
    ),
    AidotButtonDescription(
        key="ptz_down",
        translation_key="ptz_down",
        icon="mdi:arrow-down-circle-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_move("down"),
    ),
    AidotButtonDescription(
        key="ptz_left",
        translation_key="ptz_left",
        icon="mdi:arrow-left-circle-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_move("left"),
    ),
    AidotButtonDescription(
        key="ptz_right",
        translation_key="ptz_right",
        icon="mdi:arrow-right-circle-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_move("right"),
    ),
    AidotButtonDescription(
        key="ptz_stop",
        translation_key="ptz_stop",
        icon="mdi:stop-circle-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_stop(),
    ),
    AidotButtonDescription(
        key="ptz_zoom_in",
        translation_key="ptz_zoom_in",
        icon="mdi:magnify-plus-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_move("zoom_in"),
    ),
    AidotButtonDescription(
        key="ptz_zoom_out",
        translation_key="ptz_zoom_out",
        icon="mdi:magnify-minus-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        async_press_fn=lambda c: c.async_ptz_move("zoom_out"),
    ),
)


def _is_ptz_camera(coordinator: AidotCameraUpdateCoordinator) -> bool:
    """Return True if the camera supports PTZ.

    Prefer the advertised PTZ direction codes (the authoritative capability,
    reliably populated) and fall back to the model id - model_id is sometimes
    empty at setup, which previously suppressed all PTZ buttons.
    """
    info = coordinator.device_client.info
    if getattr(info, "ptz_directions", None):
        return True
    return "A001064" in (info.model_id or "")


def _ptz_buttons_for(coordinator: AidotCameraUpdateCoordinator) -> list[AidotButtonDescription]:
    """Return button descriptions appropriate for this camera's capabilities.

    Uses ptzDirection codes from the product definition to gate which buttons are
    shown.  A pan-only camera advertises [3,6] (left/right) - up/down/zoom are
    suppressed.  When direction codes are unknown (empty list) all buttons are
    returned for backward compatibility.
    """
    dirs = coordinator.camera_info.ptz_directions  # [] = unknown
    if not dirs:
        return list(PTZ_BUTTONS)

    supported_keys = {_DIRECTION_CODE_KEYS[c] for c in dirs if c in _DIRECTION_CODE_KEYS}
    return [
        desc for desc in PTZ_BUTTONS
        if desc.key == "ptz_stop" or desc.key in supported_keys
        # zoom buttons omitted when capabilities are known but no zoom code present
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot PTZ buttons + the integration reload button."""
    async_add_entities([AidotReloadButton(entry)])
    coordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    registered: set[str] = set()

    def _add_new_buttons() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered and _is_ptz_camera(c)
        }
        new = []
        for c in new_coords.values():
            valid = _ptz_buttons_for(c)
            new.extend(AidotPtzButton(c, desc) for desc in valid)
            # Drop stale button entities for directions this camera no longer
            # advertises (e.g. up/down/zoom left behind on a pan-only camera).
            valid_keys = {desc.key for desc in valid}
            dev_id = c.device_client.info.dev_id
            for desc in PTZ_BUTTONS:
                if desc.key not in valid_keys:
                    eid = ent_reg.async_get_entity_id(
                        "button", DOMAIN, f"{dev_id}_{desc.key}"
                    )
                    if eid:
                        ent_reg.async_remove(eid)
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_buttons()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_buttons()))


class AidotReloadButton(ButtonEntity):
    """Reload the AiDot integration (unload + set up again) on demand.

    A one-click alternative to the entry's built-in Reload - handy after a
    network change or to re-prime the camera motion poll without restarting Home
    Assistant. Lives on the AiDot hub device. The reload is scheduled as a task
    because it tears this very entity down, so async_press must return first.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reload"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: AidotConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reload"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._entry.entry_id)
        )


class AidotPtzButton(AidotEntity, ButtonEntity):
    """A button that sends one PTZ command when pressed."""

    entity_description: AidotButtonDescription

    def __init__(
        self,
        coordinator: AidotCameraUpdateCoordinator,
        description: AidotButtonDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        press_fn = cast(Callable[..., Any], self.entity_description.async_press_fn)
        await self.async_run_command(
            press_fn(self.device_client),
            f"{self.name}",
        )
