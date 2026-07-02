"""Camera entity methods exercised in isolation with a mocked device_client.

Mirrors ``test_camera.py``: build an ``AidotCamera`` backed by a mocked
coordinator/device_client (no live camera, no hass lifecycle, no ffmpeg/network).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.exceptions import ServiceValidationError

from custom_components.aidot import camera as camera_mod
from custom_components.aidot.camera import AidotCamera


def _make_camera() -> AidotCamera:
    info = SimpleNamespace(
        dev_id="ecf4937b640c0000",
        model_id="A000088.x",
        mac="aa:bb:cc:dd:ee:ff",
        name="Test Cam",
        hw_version="1.0",
    )
    device_client = MagicMock()
    device_client.info = info
    device_client.device_id = info.dev_id

    coordinator = MagicMock()
    coordinator.device_client = device_client
    coordinator.config_entry = SimpleNamespace(entry_id="entry1", options={})
    coordinator.sdes_audio_override = None

    cam = AidotCamera(coordinator)
    cam.hass = MagicMock()
    cam.hass.async_add_executor_job = AsyncMock()
    return cam


# --------------------------------------------------------------------------- #
# async_camera_image
# --------------------------------------------------------------------------- #
async def test_async_camera_image_returns_live_jpeg():
    cam = _make_camera()
    cam.coordinator.device_client.latest_jpeg = b"LIVEFRAME"
    assert await cam.async_camera_image() == b"LIVEFRAME"


async def test_async_camera_image_falls_back_to_base_snapshot():
    cam = _make_camera()
    cam.coordinator.device_client.latest_jpeg = None
    cam._base_snapshot = AsyncMock(return_value=b"SNAP")
    assert await cam.async_camera_image() == b"SNAP"
    cam._base_snapshot.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _base_snapshot
# --------------------------------------------------------------------------- #
async def test_base_snapshot_returns_cached_within_ttl():
    cam = _make_camera()
    cam._cached_image = b"cached"
    with patch("custom_components.aidot.camera.time.monotonic", return_value=1000.0):
        cam._cached_image_ts = 1000.0  # diff 0 < 300 -> serve cache, no cloud hit
        result = await cam._base_snapshot()
    assert result == b"cached"
    cam.coordinator.device_client.async_get_latest_thumbnail.assert_not_called()


async def test_base_snapshot_rolls_back_ts_on_failed_fetch():
    cam = _make_camera()
    cam._cached_image = b"old"
    cam._cached_image_ts = 0.0
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        side_effect=RuntimeError("cloud down")
    )
    with patch("custom_components.aidot.camera.time.monotonic", return_value=5000.0):
        result = await cam._base_snapshot()
    # Failed refresh returns the prior cached bytes...
    assert result == b"old"
    # ...and rolls the timestamp back so the very next call retries (no 5-min lock).
    assert cam._cached_image_ts == 0.0


# --------------------------------------------------------------------------- #
# async_talk
# --------------------------------------------------------------------------- #
async def test_async_talk_raises_when_no_pcm_decoded():
    cam = _make_camera()
    cam._resolve_media = AsyncMock(return_value="http://x/audio.mp3")
    cam._decode_pcm_8k = AsyncMock(return_value=b"")  # nothing decoded
    with pytest.raises(ServiceValidationError):
        await cam.async_talk("http://x/audio.mp3")
    cam.coordinator.device_client.async_speak.assert_not_called()


async def test_async_talk_success_calls_async_speak():
    cam = _make_camera()
    cam._resolve_media = AsyncMock(return_value="http://x/audio.mp3")
    cam._decode_pcm_8k = AsyncMock(return_value=b"\x00" * 64)
    cam.coordinator.device_client.async_speak = AsyncMock(return_value=True)
    await cam.async_talk("http://x/audio.mp3")
    cam.coordinator.device_client.async_speak.assert_awaited_once()


async def test_async_talk_raises_when_session_fails():
    cam = _make_camera()
    cam._resolve_media = AsyncMock(return_value="http://x/audio.mp3")
    cam._decode_pcm_8k = AsyncMock(return_value=b"\x00" * 64)
    cam.coordinator.device_client.async_speak = AsyncMock(return_value=False)
    with pytest.raises(ServiceValidationError):
        await cam.async_talk("http://x/audio.mp3")


# --------------------------------------------------------------------------- #
# async_ptz
# --------------------------------------------------------------------------- #
async def test_async_ptz_raises_on_failure():
    cam = _make_camera()
    cam.coordinator.device_client.async_ptz_move = AsyncMock(return_value=False)
    with pytest.raises(ServiceValidationError):
        await cam.async_ptz("up")


async def test_async_ptz_succeeds():
    cam = _make_camera()
    cam.coordinator.device_client.async_ptz_move = AsyncMock(return_value=True)
    await cam.async_ptz("left", speed=6)  # must not raise
    cam.coordinator.device_client.async_ptz_move.assert_awaited_once_with("left", 6)


# --------------------------------------------------------------------------- #
# _publish_to_go2rtc / _unpublish_from_go2rtc
# --------------------------------------------------------------------------- #
def _patch_go2rtc(client):
    return (
        patch.object(camera_mod, "_GO2RTC_ENABLED", True),
        patch.object(camera_mod, "async_get_clientsession", MagicMock(return_value=MagicMock())),
        patch.object(camera_mod, "Go2rtcClient", MagicMock(return_value=client)),
    )


async def test_publish_to_go2rtc_returns_rtsp_on_success():
    cam = _make_camera()
    client = MagicMock()
    client.ensure_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value="rtsp://go2rtc/x")
    p1, p2, p3 = _patch_go2rtc(client)
    with p1, p2, p3:
        url = await cam._publish_to_go2rtc("http://127.0.0.1:1234/x.ts")
    assert url == "rtsp://go2rtc/x"


async def test_publish_to_go2rtc_returns_none_when_disabled():
    cam = _make_camera()
    with patch.object(camera_mod, "_GO2RTC_ENABLED", False):
        assert await cam._publish_to_go2rtc("http://x") is None


async def test_publish_to_go2rtc_returns_none_when_unreachable():
    cam = _make_camera()
    client = MagicMock()
    client.ensure_stream = AsyncMock(return_value=False)  # go2rtc down -> PUT fails
    p1, p2, p3 = _patch_go2rtc(client)
    with p1, p2, p3:
        assert await cam._publish_to_go2rtc("http://x") is None


async def test_unpublish_from_go2rtc_removes_stream():
    cam = _make_camera()
    client = MagicMock()
    client.remove_stream = AsyncMock()
    p1, p2, p3 = _patch_go2rtc(client)
    with p1, p2, p3:
        await cam._unpublish_from_go2rtc()
    client.remove_stream.assert_awaited_once()


async def test_unpublish_from_go2rtc_noop_when_disabled():
    cam = _make_camera()
    with patch.object(camera_mod, "_GO2RTC_ENABLED", False):
        await cam._unpublish_from_go2rtc()  # must not raise


# --------------------------------------------------------------------------- #
# available property
# --------------------------------------------------------------------------- #
def test_available_true_when_online():
    cam = _make_camera()
    cam.coordinator.last_update_success = True
    cam.coordinator.data = SimpleNamespace(online=True)
    assert cam.available is True


def test_available_false_when_no_data():
    cam = _make_camera()
    cam.coordinator.last_update_success = True
    cam.coordinator.data = None
    assert cam.available is False


def test_available_false_when_offline():
    cam = _make_camera()
    cam.coordinator.last_update_success = True
    cam.coordinator.data = SimpleNamespace(online=False)
    assert cam.available is False


def test_available_false_when_coordinator_unsuccessful():
    cam = _make_camera()
    cam.coordinator.last_update_success = False
    cam.coordinator.data = SimpleNamespace(online=True)
    assert cam.available is False
