"""A cached Stream must not be handed out once the session under it has ended.

Home Assistant resolves a camera's source only when it CREATES a ``Stream``:
``Camera.async_create_stream`` calls ``stream_source()`` if ``self.stream`` is
empty and otherwise returns the cached object as-is, even one that has been
stopped (identical in 2026.2 and 2026.9). A battery camera's session is released
120 s after its last viewer, so that cached Stream can outlive the source it
points at.

Measured on the box 2026-09-27 (kitchen camera, logger at debug):

    14:17:40  stream_source opens the camera; HA creates the Stream
    14:18:07  the 20 s recording ends; HA stops the Stream but keeps it
    ~14:20:06 no viewer for 120 s - the session is released
    14:20:08  camera.record: HA restarts the SAME Stream on the old URL;
              stream_source() is never called, so nothing opens the camera
    14:20:08  "Not Found error opening stream" (404); worker retries in 10 s
    14:20:11  the 30 s stale-stream watchdog evicts it, taking the in-flight
              recording down with it - no file is ever written

The watchdog cannot close that window because it only runs every 30 s. The
reuse happens inside ``async_create_stream``, the one door that recordings and
the HLS dialog both come through, so that is where a stale Stream is dropped.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homeassistant.components import camera as ha_camera

from custom_components.aidot.camera import AidotCamera

URL = "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"


def _make_camera(*, cached_stream, stream_rtsp_url):
    """A real entity, carrying the state HA's own Camera.__init__ would give it."""
    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = SimpleNamespace(
        device_id="aaaa0000bbbb1111cccc2222dddd3333",
        stream_rtsp_url=stream_rtsp_url,
    )
    cam._rtsp_name = "aaaa0000bbbb1111cccc2222dddd3333"
    cam.hass = MagicMock()
    cam.entity_id = "camera.kitchen"
    # Camera.__init__'s own initial state:
    cam.stream = cached_stream
    cam.stream_options = {}
    cam._create_stream_lock = None
    # stream_source() is what opens the camera; record whether HA asked for it.
    cam.stream_source = AsyncMock(return_value=URL)
    return cam


def _stopped_stream():
    stream = MagicMock(name="cached-stream")
    stream.stop = AsyncMock()
    return stream


async def _create(cam):
    fresh = MagicMock(name="fresh-stream")
    with (
        patch.object(ha_camera, "create_stream", return_value=fresh) as create,
        patch.object(
            ha_camera,
            "get_dynamic_camera_stream_settings",
            AsyncMock(return_value=MagicMock()),
        ),
    ):
        result = await cam.async_create_stream()
    return result, fresh, create


async def test_a_stream_whose_session_ended_is_replaced_not_reused():
    cached = _stopped_stream()
    cam = _make_camera(cached_stream=cached, stream_rtsp_url=None)

    result, fresh, create = await _create(cam)

    cam.stream_source.assert_awaited_once()  # the camera gets opened again
    create.assert_called_once()
    assert result is fresh
    cached.stop.assert_awaited_once()  # its retrying worker does not linger


async def test_a_stream_over_a_live_session_is_still_reused():
    """The preserved path: a warm session keeps its Stream (no re-open)."""
    cached = _stopped_stream()
    cam = _make_camera(cached_stream=cached, stream_rtsp_url=URL)

    result, _fresh, create = await _create(cam)

    assert result is cached
    cam.stream_source.assert_not_awaited()
    create.assert_not_called()
    cached.stop.assert_not_awaited()


async def test_no_cached_stream_creates_one_as_before():
    cam = _make_camera(cached_stream=None, stream_rtsp_url=None)

    result, fresh, _create_ = await _create(cam)

    cam.stream_source.assert_awaited_once()
    assert result is fresh
