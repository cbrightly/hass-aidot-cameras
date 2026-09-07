"""Motion / person event entities for Aidot cameras."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import aidot_device_info

PARALLEL_UPDATES = 0

EVENT_TYPES = ["motion", "person"]
# Cloud event-list codes -> HA event type (defaults to "motion" for unknown codes).
_CODE_TO_TYPE = {"1": "motion", "4": "person"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera motion-event entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_events() -> None:
        new = []
        for dev_id, c in coordinator.camera_coordinators.items():
            if dev_id not in registered:
                registered.add(dev_id)
                new.append(AidotMotionEvent(c))
        if new:
            async_add_entities(new)

    _add_new_events()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_events()))


class AidotMotionEvent(EventEntity):
    """Fires when the camera records a new motion/person cloud event."""

    _attr_has_entity_name = True
    _attr_translation_key = "motion"
    _attr_device_class = EventDeviceClass.MOTION
    _attr_event_types = EVENT_TYPES
    # Keep the noisy/expiring per-event fields (signed CDN url, uuid, description)
    # out of the recorder so they don't bloat history with single-use values.
    _unrecorded_attributes = frozenset({"event_uuid", "pic_url", "description"})

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        self._coordinator = coordinator
        info = coordinator.device_client.info
        self._attr_unique_id = f"{info.dev_id}_motion"
        entry = getattr(coordinator, "config_entry", None)
        self._attr_device_info = aidot_device_info(
            info, entry.entry_id if entry else None
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's motion-event stream."""
        # EventEntity is a RestoreEntity: super() restores the last fired event so
        # the entity shows it after a restart instead of "unknown" (motion polling
        # primes past the existing backlog, so it won't re-fire historical events).
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.add_motion_listener(self._on_motion)
        )

    @callback
    def _on_motion(self, event: dict[str, Any]) -> None:
        event_type = _CODE_TO_TYPE.get(event.get("eventCode") or "", "motion")
        self._trigger_event(
            event_type,
            {
                "event_uuid": event.get("eventUuid"),
                "pic_url": event.get("picUrl"),
                "description": event.get("eventDesc"),
            },
        )
        self.async_write_ha_state()
