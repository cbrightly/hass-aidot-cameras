"""A camera that goes offline must stop streaming, not just go unavailable.

Entities going unavailable is presentation only. The keepalive stays latched on,
which keeps a renew POST going every 20s and an HTTP wake every 10 minutes at a
camera nobody can view. Observed live on a battery camera already down to 5%.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator


def _mgr(dc):
    c = object.__new__(AidotDeviceManagerCoordinator)
    c.hass = MagicMock()
    c.config_entry = MagicMock()
    c.camera_coordinators = {"dev1": SimpleNamespace(device_client=dc)}
    c._dev_cache = {}
    c._dev_cache_ts = 0
    return c


def _client(online, streaming):
    dc = MagicMock()
    dc.status = SimpleNamespace(online=online)
    dc.stream_rtsp_url = "rtsp://x" if streaming else None
    dc._streaming_active = streaming

    def _update(device):
        dc.status.online = bool(device.get("online"))

    dc.update_status_from_device.side_effect = _update
    return dc


def _sync(mgr, online):
    mgr._refresh_camera_attributes({"dev1": {"id": "dev1", "online": online}})


def test_going_offline_while_streaming_stops_the_stream():
    dc = _client(online=True, streaming=True)
    mgr = _mgr(dc)
    _sync(mgr, online=False)
    mgr.config_entry.async_create_background_task.assert_called_once()
    dc.async_stop_streaming.assert_called_once()


def test_going_offline_while_idle_does_nothing():
    dc = _client(online=True, streaming=False)
    mgr = _mgr(dc)
    _sync(mgr, online=False)
    mgr.config_entry.async_create_background_task.assert_not_called()


def test_staying_online_does_not_stop_a_live_stream():
    dc = _client(online=True, streaming=True)
    mgr = _mgr(dc)
    _sync(mgr, online=True)
    mgr.config_entry.async_create_background_task.assert_not_called()
