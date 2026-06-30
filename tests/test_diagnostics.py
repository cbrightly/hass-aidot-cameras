"""Tests for the Aidot config-entry diagnostics.

The diagnostics deliberately surface only non-sensitive device metadata/state -
there is no credential or token field in the payload to leak (the "redaction" is
structural: secrets are never collected). These tests assert the structure, the
None-data handling, the stream-health snapshot, and that no credential/token key
appears anywhere in the output.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.aidot.diagnostics import async_get_config_entry_diagnostics


def _light_coord(*, online=True, data=True):
    return SimpleNamespace(
        device_client=SimpleNamespace(
            info=SimpleNamespace(model_id="A4.BL", hw_version="1.0")
        ),
        data=SimpleNamespace(online=online) if data else None,
    )


def _camera_coord(*, stream_session=None, streaming=False, data=True):
    dc = MagicMock()
    dc.is_sdes_camera = True
    dc.stream_rtsp_url = "rtsp://x" if streaming else None
    dc._stream_session = stream_session
    return SimpleNamespace(
        device_client=dc,
        camera_info=SimpleNamespace(
            model_id="IPC.A001064", hw_version="2.0", ptz_directions=[3, 6]
        ),
        camera_data=SimpleNamespace(
            online=True,
            battery_remaining=80,
            sd_card_status="normal",
            wifi_rssi=-50,
            motion_detection=True,
            night_vision_mode="auto",
        ) if data else None,
    )


def _entry(coordinator):
    return SimpleNamespace(runtime_data=coordinator)


async def test_diagnostics_structure():
    coordinator = SimpleNamespace(
        device_coordinators={"l1": _light_coord()},
        camera_coordinators={"c1": _camera_coord()},
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry(coordinator))

    assert result["lights"] == [
        {"model_id": "A4.BL", "hw_version": "1.0", "online": True}
    ]
    cam = result["cameras"][0]
    assert cam["model_id"] == "IPC.A001064"
    assert cam["ptz_directions"] == [3, 6]
    assert cam["is_sdes"] is True
    assert cam["streaming"] is False
    assert cam["battery"] == 80
    assert cam["sd_card_status"] == "normal"
    assert cam["wifi_rssi"] == -50
    assert cam["stream_health"] is None


async def test_diagnostics_handles_missing_data():
    coordinator = SimpleNamespace(
        device_coordinators={"l1": _light_coord(data=False)},
        camera_coordinators={"c1": _camera_coord(data=False)},
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry(coordinator))
    assert result["lights"][0]["online"] is None
    cam = result["cameras"][0]
    assert cam["online"] is None
    assert cam["battery"] is None
    assert cam["wifi_rssi"] is None


async def test_diagnostics_includes_stream_health_when_streaming():
    session = SimpleNamespace(get_stats=AsyncMock(return_value={"ice": "relay"}))
    coordinator = SimpleNamespace(
        device_coordinators={},
        camera_coordinators={"c1": _camera_coord(stream_session=session, streaming=True)},
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry(coordinator))
    cam = result["cameras"][0]
    assert cam["streaming"] is True
    assert cam["stream_health"] == {"ice": "relay"}


async def test_diagnostics_stream_health_never_raises():
    session = SimpleNamespace(get_stats=AsyncMock(side_effect=RuntimeError("boom")))
    coordinator = SimpleNamespace(
        device_coordinators={},
        camera_coordinators={"c1": _camera_coord(stream_session=session, streaming=True)},
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry(coordinator))
    assert result["cameras"][0]["stream_health"] is None


async def test_diagnostics_leaks_no_credentials():
    coordinator = SimpleNamespace(
        device_coordinators={"l1": _light_coord()},
        camera_coordinators={"c1": _camera_coord()},
    )
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry(coordinator))
    blob = json.dumps(result).lower()
    for secret in ("password", "accesstoken", "mqttpassword", "token", "aeskey"):
        assert secret not in blob
