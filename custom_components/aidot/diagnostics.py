"""Diagnostics support for Aidot."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import AidotConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    lights = []
    for dc in coordinator.device_coordinators.values():
        info = dc.device_client.info
        lights.append({
            "model_id": info.model_id,
            "hw_version": info.hw_version,
            "online": dc.data.online if dc.data else None,
        })

    cameras = []
    for dc in coordinator.camera_coordinators.values():
        cam_info = dc.camera_info
        cam_data = dc.camera_data
        cameras.append({
            "model_id": cam_info.model_id,
            "hw_version": cam_info.hw_version,
            "ptz_directions": cam_info.ptz_directions,
            "is_sdes": getattr(dc.device_client, "is_sdes_camera", None),
            "streaming": dc.device_client.stream_rtsp_url is not None,
            "online": cam_data.online if cam_data else None,
            "battery": cam_data.battery_remaining if cam_data else None,
            "sd_card_status": cam_data.sd_card_status if cam_data else None,
            "motion_detection": cam_data.motion_detection if cam_data else None,
            "night_vision_mode": cam_data.night_vision_mode if cam_data else None,
        })

    return {
        "lights": lights,
        "cameras": cameras,
    }
