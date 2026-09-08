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
    #: callable(DeviceStatusData) -> bool. None means every camera gets it.
    #: A camera that does not report the setting must get no entity at all
    #: rather than one stuck at unknown, which reads as a broken control.
    is_supported: Any = None


#: How long the floodlight stays on after it triggers. The camera enumerates
#: exactly these four seconds values (``LingerDuration``, default 30); a value
#: outside the set is accepted on the wire and ignored.
LIGHT_LINGER_OPTIONS = ("20", "30", "40", "50")


def _linger_option(status) -> str | None:
    """The camera's duration as one of our options, or None.

    A value the camera reports but we do not offer reads as unknown rather than
    being shown: Home Assistant drops an out-of-list option with a log line, and
    a select showing a value it cannot round-trip is worse than showing none.
    """
    v = getattr(status, "light_linger_duration", None)
    return str(v) if str(v) in LIGHT_LINGER_OPTIONS else None


CAMERA_SELECTS: tuple[AidotSelectDescription, ...] = (
    AidotSelectDescription(
        key="light_linger_duration",
        translation_key="light_linger_duration",
        icon="mdi:timer-outline",
        entity_category=EntityCategory.CONFIG,
        options=list(LIGHT_LINGER_OPTIONS),
        get_current_option=_linger_option,
        async_select_option_fn=lambda c, v: c.async_set_light_linger_duration(int(v)),
        is_supported=lambda s: getattr(s, "light_linger_duration", None) is not None,
    ),
    # NO light-behavior select. The camera reports `lightBehavior`
    # (Constant=0 / Flash=1) and the app offers it, but the write acks and the
    # value never changes: six attempts on 2026-09-07 across an A001513 and an
    # A000088, as int and as string, every read-back over the following 20 s
    # unchanged -- while LingerDuration and Dimming landed within 4 s on the
    # same camera in the same session. So it is the attribute, not a sleeping
    # camera. The one untested hypothesis is that the camera takes it only while
    # the automation is armed, and testing that means making a real floodlight
    # come on in someone's house. `async_set_light_behavior` stays in the
    # library, documented; re-adding a control here needs a read-back that
    # shows the camera changed, not an ack. Same rule as the resolution select.
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
        new = []
        for dev_id, c in coordinator.camera_coordinators.items():
            for desc in CAMERA_SELECTS:
                marker = f"{dev_id}:{desc.key}"
                if marker in registered:
                    continue
                # A gated select waits for the camera to report the setting, so
                # it is checked on every update rather than once at setup.
                if desc.is_supported is not None and not desc.is_supported(
                        getattr(c, "data", None) or object()):
                    continue
                registered.add(marker)
                new.append(AidotCameraSelect(c, desc))
        if new:
            async_add_entities(new)

    # A gated entity's data arrives on the PER-CAMERA coordinator, and the
    # parent's listener does not fire for that, so each camera is hooked too.
    hooked: set = set()

    def _refresh_all() -> None:
        for dev_id, cam in coordinator.camera_coordinators.items():
            if dev_id in hooked:
                continue
            hooked.add(dev_id)
            entry.async_on_unload(cam.async_add_listener(lambda: _add_new_selects()))
        _add_new_selects()

    _refresh_all()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_all))


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


