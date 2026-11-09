"""PTZ button entities for Aidot cameras."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, cast

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry, supports_ptz
from .entity import (
    AidotEntity,
    async_add_camera_entities,
    per_camera_entities,
    remove_registry_row,
)

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


#: The translation key AND the unique-id suffix. One string, so they cannot
#: drift apart.
_SD_REFRESH_KEY = "refresh_sd_recordings"


def _ptz_buttons_for(
    coordinator: AidotCameraUpdateCoordinator,
) -> list[AidotButtonDescription]:
    """Return button descriptions appropriate for this camera's capabilities.

    Uses ptzDirection codes from the product definition to gate which buttons are
    shown.  A pan-only camera advertises [3,6] (left/right) - up/down/zoom are
    suppressed.  When direction codes are unknown (empty list) all buttons are
    returned for backward compatibility.
    """
    dirs = coordinator.camera_info.ptz_directions  # [] = unknown
    if not dirs:
        return list(PTZ_BUTTONS)

    supported_keys = {
        _DIRECTION_CODE_KEYS[c] for c in dirs if c in _DIRECTION_CODE_KEYS
    }
    return [
        desc
        for desc in PTZ_BUTTONS
        if desc.key == "ptz_stop" or desc.key in supported_keys
        # zoom buttons omitted when capabilities are known but no zoom code present
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera buttons: reboot, SD refresh and PTZ."""
    # The hub's "Reload" button is gone: Home Assistant's own entry reload does
    # the same. Its registry row would otherwise linger as unavailable.
    remove_registry_row(hass, "button", f"{entry.entry_id}_reload")

    def _ptz_buttons(c: AidotCameraUpdateCoordinator) -> list[ButtonEntity]:
        valid = _ptz_buttons_for(c)
        # Drop stale button entities for directions this camera no longer
        # advertises (e.g. up/down/zoom left behind on a pan-only camera).
        dev_id = c.device_client.info.dev_id
        for desc in PTZ_BUTTONS:
            if desc not in valid:
                remove_registry_row(hass, "button", f"{dev_id}_{desc.key}")
        return [AidotPtzButton(c, desc) for desc in valid]

    def _collect(cameras, registered) -> list[ButtonEntity]:
        # Every camera can be asked what its card holds. Not gated on any
        # capability: a camera that does not answer explains itself in the
        # browser, where a missing button would read as a missing feature.
        # The reboot button rides the same per-camera memo: every camera has
        # both, with no capability to wait for.
        new: list[ButtonEntity] = per_camera_entities(
            cameras,
            registered,
            lambda c: [AidotSdRefreshButton(c), AidotRebootButton(c)],
        )
        # PTZ is its own marker, set only once the buttons are made, because
        # ptz_directions is sometimes empty at setup and arrives later.
        new += per_camera_entities(
            cameras, registered, _ptz_buttons, marker="ptz", supported=supports_ptz
        )
        return new

    async_add_camera_entities(entry, async_add_entities, _collect)


class AidotRebootButton(AidotEntity, ButtonEntity):
    """Reboot the camera, as the app's "Reboot Device" does.

    A press means the request was SENT, not that the camera rebooted: a reboot
    succeeds by taking the camera off the network, so the library awaits no
    answer (see ``async_reboot``). It refuses, and the press fails, when the
    cloud reports the camera offline. The camera is gone for about a minute
    afterwards, and a live view in that time connects and shows nothing.
    """

    _attr_translation_key = "reboot"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        super().__init__(coordinator, "reboot")

    async def async_press(self) -> None:
        await self.async_run_command(self.device_client.async_reboot(), "reboot")


class AidotSdRefreshButton(AidotEntity, ButtonEntity):
    """Take a fresh listing of what this camera holds on its own card.

    The only path in this integration that deliberately opens a WebRTC session
    to list. Browsing never does: a listing costs a 15-70 s handshake and wakes
    the camera, where a cloud listing is one ~200 ms request. So the cost sits
    behind a press rather than behind opening a folder.
    """

    _attr_has_entity_name = True
    _attr_translation_key = _SD_REFRESH_KEY
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sd"

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        # AidotEntity's second argument builds the unique id as
        # f"{dev_id}_{key}" - the same shape the PTZ buttons use. Setting
        # _attr_unique_id by hand here would duplicate that rule.
        super().__init__(coordinator, _SD_REFRESH_KEY)

    async def async_press(self) -> None:
        # Backgrounded: with PARALLEL_UPDATES = 1 every button press on the
        # entry shares one semaphore, and this listing can wait 75 s for a
        # session - a PTZ press elsewhere must not queue behind it.
        # Entry-bound, so an unload cancels it rather than letting it list
        # against a coordinator that is gone.
        self.coordinator.spawn_entry_task(
            self.coordinator.async_list_sd_recordings(open_session=True),
            "aidot sd refresh",
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
