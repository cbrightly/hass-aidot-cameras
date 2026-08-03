"""A view must restart a session whose publisher has died.

Measured on the box 2026-08-02. ``stream_rtsp_url`` is set when a session starts
and is never cleared when the publisher dies underneath it - the SDES bridge logs
``ffmpeg exited with code 255 - stream ended`` and the URL stays set. So
``stream_source()`` took its warm-session fast path on every later view, skipped
``start_keepalive``, and handed HA the go2rtc URL for a stream with no publisher.
go2rtc answered DESCRIBE with 404 and kept doing so until Home Assistant was
restarted.

Confirmed directly: with the publisher gone, a WebRTC offer logged
``Registered ... with go2rtc`` (so ``stream_source()`` *did* run) but no
``Started HTTP stream serve`` (so ``start_keepalive`` did not), and
``/api/streams`` showed only the inert HTTP placeholder producer. With a
publisher attached the very same stream serves h264 1280x720 + pcm_alaw.

That is why battery cameras - never prewarmed, so never given a session by
anything else - were permanently blank, and why the mains SDES PTZ went dead the
moment its startup-prewarm session ended.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.camera import AidotCamera
from custom_components.aidot.const import CONF_SDES_PUSH

# What go2rtc reports. A push-mode stream always carries the registered HTTP
# serve URL as an inert placeholder; a live publisher shows up as an extra
# producer with no "url" key.
PLACEHOLDER = {"url": "http://127.0.0.1:18981/aaaa3333bbbb.ts"}
PUBLISHER = {"remote_addr": "127.0.0.1:59878"}


def _make_camera(*, is_sdes: bool, stream_rtsp_url: str | None) -> AidotCamera:
    device_client = MagicMock()
    device_client.device_id = "aaaa3333bbbb1111cccc2222dddd3333"
    device_client.is_sdes_camera = is_sdes
    device_client.is_battery_camera = False
    device_client.stream_rtsp_url = stream_rtsp_url
    device_client.status = SimpleNamespace(online=True)
    device_client.start_keepalive = AsyncMock()
    device_client.async_wait_serve_ready = AsyncMock()

    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = device_client
    cam.coordinator.config_entry = SimpleNamespace(
        entry_id="e1", options={CONF_SDES_PUSH: True}
    )
    cam.coordinator.sdes_audio_override = None
    cam._rtsp_name = device_client.device_id
    cam._setup_complete = True
    cam.hass = MagicMock()
    cam._set_stream_status = MagicMock()
    return cam


async def _stream_source(cam: AidotCamera, producers: list | None):
    """Drive stream_source() with go2rtc reporting ``producers``."""
    streams = (
        {} if producers is None else {"aidot_aaaa3333bbbb": {"producers": producers}}
    )
    client = MagicMock()
    client.list_streams = AsyncMock(return_value=streams)
    client.ensure_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value="rtsp://127.0.0.1:8554/aidot_aaaa3333bbbb")
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch.object(
            AidotCamera,
            "_push_serve_url",
            new_callable=PropertyMock,
            return_value="rtsp://127.0.0.1:8554/aidot_aaaa3333bbbb",
        ),
    ):
        return await cam.stream_source()


@pytest.mark.asyncio
async def test_dead_publisher_restarts_the_session():
    """The defect: a stale stream_rtsp_url must not suppress the restart."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/stale")

    await _stream_source(cam, [PLACEHOLDER])

    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_publisher_keeps_the_warm_fast_path():
    """A healthy session must not be restarted on every view."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/live")

    await _stream_source(cam, [PLACEHOLDER, PUBLISHER])

    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreachable_go2rtc_does_not_thrash_the_session():
    """Fail safe: an empty/unreachable go2rtc must not look like a dead publisher,
    or every view would tear down a working session."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/live")

    await _stream_source(cam, None)

    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_session_still_starts_one():
    """Unchanged behaviour when there is no session at all."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)

    await _stream_source(cam, [PLACEHOLDER])

    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


@pytest.mark.asyncio
async def test_pull_mode_is_untouched():
    """DTLS/pull cameras keep the old guard: there the registered HTTP serve URL
    *is* the source, and a dead serve is handled by _evict_stale_stream."""
    cam = _make_camera(is_sdes=False, stream_rtsp_url="rtsp://127.0.0.1:8554/live")

    await _stream_source(cam, [PLACEHOLDER])

    cam.coordinator.device_client.start_keepalive.assert_not_awaited()
