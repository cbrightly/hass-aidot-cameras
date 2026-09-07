"""Support for Aidot camera select entities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AidotSelectDescription(SelectEntityDescription):
    """Describes an Aidot camera select entity."""

    get_current_option: Any = None       # callable(DeviceStatusData) -> str | None
    async_select_option_fn: Any = None   # async callable(DeviceClient, str) -> None


CAMERA_SELECTS: tuple[AidotSelectDescription, ...] = (
    AidotSelectDescription(
        key="night_vision",
        translation_key="night_vision",
        icon="mdi:weather-night",
        entity_category=EntityCategory.CONFIG,
        options=["auto", "on", "off"],
        get_current_option=lambda s: s.night_vision_mode,
        async_select_option_fn=lambda c, v: c.async_set_night_vision(v),
    ),
    # NO resolution select. It was removed in 2.11.9 because the cameras ignore
    # the command it sends. SETSTREAMCTRL (cmd 800) is delivered - the library
    # sends it over the live session and re-sends it at session start - and the
    # encode never changes. Measured 2026-08-07 by reading videoWidth off live
    # WebRTC tracks, which is the encode itself rather than a scaled snapshot:
    #
    #   A001064 PTZ  (SDES)  1280x720 under sd, mid-session AND at session start
    #   A000088 M3 Pro (DTLS) 1280x720 before sd, 1280x720 30s after
    #
    # Two models across both transports, so this is not one camera's firmware.
    #
    # Confirmed again 2026-08-23 on a different observable, which closes the
    # obvious objection that dimensions alone might hide a quality change: the
    # BITRATE does not move either. Measured in-session on the A001064 with a
    # control arm that waits the same gap and sends nothing, sd scored 0.885
    # against control 0.863 (window B over window A of the same session), where
    # a working SD is about 2:1. That run also had the AVIO framing gap closed,
    # so the header is byte-identical to the vendor app's, dSeq included.
    # The entity accepted a value, restored it across restarts and reported a
    # setting the camera had never applied - a control that lies is worse than
    # no control. `async_set_resolution` stays in the library: the command is
    # correct, and a future firmware may honour it.
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera select entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_selects() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [
            AidotCameraSelect(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_SELECTS
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_selects()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_selects()))


class AidotCameraSelect(AidotEntity, SelectEntity):
    """A select backed by a cloud-polled device attribute."""

    entity_description: AidotSelectDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSelectDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description
        self._attr_options = list(description.options or [])

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_current_option(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        await self.async_run_command(
            self.entity_description.async_select_option_fn(self.device_client, option),
            f"set {self.name} to {option}",
        )


