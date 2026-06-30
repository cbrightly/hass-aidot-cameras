"""Remaining AidotCamera branches: the motion-prewarm-cancel teardown, the stale-
stream stop-error swallow, the go2rtc unpublish swallow, and the _base_snapshot
url/GET branches.

Mirrors ``test_camera_methods.py``: an AidotCamera on a mocked coordinator/
device_client, no live camera / ffmpeg / network. ``hass.async_add_executor_job``
is an AsyncMock, and the thumbnail HTTP GET uses an in-memory fake session.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
# async_added_to_hass -> _cancel_prewarm teardown (line 230)
# --------------------------------------------------------------------------- #
async def test_cancel_prewarm_cancels_pending_task_on_remove():
    cam = _make_camera()
    # Avoid the heavy collaborators async_added_to_hass fans out to.
    cam._prefetch_thumbnail = MagicMock()
    cam._startup_prewarm = MagicMock()
    cam.coordinator.add_motion_listener = MagicMock(return_value=MagicMock())

    with patch.object(camera_mod, "async_track_time_interval", MagicMock()):
        await cam.async_added_to_hass()

    # Find and invoke the _cancel_prewarm on_remove callback with a pending task.
    cancel_prewarm = next(
        cb for cb in cam._on_remove if getattr(cb, "__name__", "") == "_cancel_prewarm"
    )
    task = MagicMock()
    cam._prewarm_task = task
    cancel_prewarm()  # lines 229-230
    task.cancel.assert_called_once()


# --------------------------------------------------------------------------- #
# _evict_stale_stream: stream.stop() raising is swallowed (298-299)
# --------------------------------------------------------------------------- #
async def test_evict_stale_stream_swallows_stop_error():
    cam = _make_camera()
    stream = MagicMock()
    stream.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
    cam.stream = stream
    cam.coordinator.device_client.stream_rtsp_url = None  # keepalive ended
    cam.hass.async_add_executor_job = AsyncMock(return_value=False)  # port probe

    await cam._evict_stale_stream()  # 298-299 debug swallow

    stream.stop.assert_awaited_once()
    assert cam.stream is None  # cleared after the (failed) stop


# --------------------------------------------------------------------------- #
# _unpublish_from_go2rtc: swallow when Go2rtcClient raises (469-470)
# --------------------------------------------------------------------------- #
async def test_unpublish_from_go2rtc_swallows_client_error():
    cam = _make_camera()
    with patch.object(camera_mod, "_GO2RTC_ENABLED", True), patch.object(
        camera_mod, "async_get_clientsession", MagicMock()
    ), patch.object(
        camera_mod, "Go2rtcClient", MagicMock(side_effect=RuntimeError("go2rtc down"))
    ):
        await cam._unpublish_from_go2rtc()  # must not raise (469-470)


# --------------------------------------------------------------------------- #
# _base_snapshot: url + GET branches (723-743)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status: int, data: bytes = b"") -> None:
        self.status = status
        self._data = data

    async def read(self) -> bytes:
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, resp=None, get_exc=None) -> None:
        self._resp = resp
        self._get_exc = get_exc

    def get(self, url, timeout=None):
        if self._get_exc is not None:
            raise self._get_exc
        return self._resp


async def test_base_snapshot_fetches_and_caches_on_200():
    cam = _make_camera()
    cam._cached_image = None
    cam._cached_image_ts = 0.0
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value="http://cam/thumb.jpg"
    )
    session = _Session(resp=_Resp(200, b"IMGBYTES"))
    with patch(
        "custom_components.aidot.camera.async_get_clientsession", return_value=session
    ), patch("custom_components.aidot.camera.time.monotonic", return_value=10_000.0):
        result = await cam._base_snapshot()  # 728-735

    assert result == b"IMGBYTES"
    assert cam._cached_image == b"IMGBYTES"


async def test_base_snapshot_rolls_back_on_get_exception():
    cam = _make_camera()
    cam._cached_image = b"old"
    cam._cached_image_ts = 0.0
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value="http://cam/thumb.jpg"
    )
    session = _Session(get_exc=RuntimeError("net down"))
    with patch(
        "custom_components.aidot.camera.async_get_clientsession", return_value=session
    ), patch("custom_components.aidot.camera.time.monotonic", return_value=10_000.0):
        result = await cam._base_snapshot()  # 736-737 debug + 741-743 rollback

    assert result == b"old"
    assert cam._cached_image_ts == 0.0  # stamp rolled back for immediate retry


async def test_base_snapshot_rolls_back_when_no_url():
    cam = _make_camera()
    cam._cached_image = b"cached"
    cam._cached_image_ts = 0.0
    cam.coordinator.device_client.async_get_latest_thumbnail = AsyncMock(
        return_value=None
    )
    with patch("custom_components.aidot.camera.time.monotonic", return_value=10_000.0):
        result = await cam._base_snapshot()  # 723-726 not-url rollback

    assert result == b"cached"
    assert cam._cached_image_ts == 0.0
