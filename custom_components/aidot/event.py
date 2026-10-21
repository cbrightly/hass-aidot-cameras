"""Motion / person event entities for Aidot cameras."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .notify_dispatch import event_kind
from .entity import async_add_per_camera, device_info_for

PARALLEL_UPDATES = 0

EVENT_TYPES = ["motion", "person"]


# Cloud event-list codes -> HA event type (defaults to "motion" for unknown codes).
async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera motion-event entities."""
    async_add_per_camera(entry, async_add_entities, lambda c: [AidotMotionEvent(c)])


class AidotMotionEvent(EventEntity):
    """Fires when the camera records a new motion/person cloud event."""

    _attr_has_entity_name = True
    _attr_translation_key = "motion"
    _attr_device_class = EventDeviceClass.MOTION
    _attr_event_types = EVENT_TYPES
    # Keep the noisy/expiring per-event fields (signed CDN url, uuid, description)
    # out of the recorder so they don't bloat history with single-use values.
    _unrecorded_attributes = frozenset({"event_uuid", "pic_url", "description"})

    # Not an AidotEntity: that base is a CoordinatorEntity, whose availability
    # follows the camera being online. An event entity stays available and
    # keeps showing the last event it fired, camera asleep or not.
    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.device_client.info.dev_id}_motion"
        self._attr_device_info = device_info_for(coordinator)

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's motion-event stream."""
        # EventEntity is a RestoreEntity: super() restores the last fired event so
        # the entity shows it after a restart instead of "unknown" (motion polling
        # primes past the existing backlog, so it won't re-fire historical events).
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.add_motion_listener(self._on_motion))

    @callback
    def _on_motion(self, event: dict[str, Any]) -> None:
        event_type = event_kind(event)
        self._trigger_event(
            event_type,
            {
                "event_uuid": event.get("eventUuid"),
                "pic_url": event.get("picUrl"),
                "description": event.get("eventDesc"),
            },
        )
        self.async_write_ha_state()
