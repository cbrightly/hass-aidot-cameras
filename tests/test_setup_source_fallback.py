"""Entity setup must still hand HA a usable source when go2rtc is unreachable.

At setup HA calls ``stream_source()`` once to decide what the camera supports,
and caches the answer. That call registered the local serve with go2rtc and
returned go2rtc's RTSP URL - but returned **None** when go2rtc could not be
reached, which makes HA conclude the camera has no stream at all.

Measured on the box 2026-08-02: with the only TCP-reachable go2rtc removed, every
AiDot camera dropped from ``['hls','web_rtc']`` to ``['hls']`` and HLS then
delivered no segment in 90s - the cameras went dark. The integration reaches
go2rtc over TCP, while Home Assistant's own bundled go2rtc exposes its API on a
unix socket only, so on an install without a second instance registration always
fails and every camera is dead.

Returning the local serve URL instead is strictly better than None, and does not
depend on what HA's go2rtc supports:

* if it accepts an ``http`` source, HA registers it and serves WebRTC;
* if it does not, HA falls back to its HLS pipeline against the same URL.

Either way the camera has a stream. The one case with nothing sensible to return
is SDES **push** mode, where nothing ever binds the local serve - the publish goes
into go2rtc - so without go2rtc there is genuinely no source, and None still means
"show the still image and retry".
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.camera import AidotCamera, _serve_port
from custom_components.aidot.const import CONF_SDES_PUSH


def _make_camera(*, is_sdes: bool = False, sdes_push: bool = True) -> AidotCamera:
    device_client = MagicMock()
    device_client.device_id = "aaaa0000bbbb1111cccc2222dddd3333"
    device_client.is_sdes_camera = is_sdes
    device_client.is_battery_camera = False
    device_client.stream_rtsp_url = None
    device_client.status = SimpleNamespace(online=True)
    device_client.start_keepalive = AsyncMock()

    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = device_client
    cam.coordinator.config_entry = SimpleNamespace(
        entry_id="e1", options={CONF_SDES_PUSH: sdes_push}
    )
    cam.coordinator.sdes_audio_override = None
    cam._rtsp_name = device_client.device_id
    cam._setup_complete = False
    cam.hass = MagicMock()
    return cam


def _expected_serve_url(cam: AidotCamera) -> str:
    return f"http://127.0.0.1:{_serve_port(cam._rtsp_name)}/{cam._rtsp_name}.ts"


@pytest.mark.asyncio
async def test_go2rtc_reachable_still_returns_its_rtsp_url():
    """Unchanged happy path: registration wins when it works."""
    cam = _make_camera()
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")

    assert await cam.stream_source() == "rtsp://go2rtc/x"
    cam.coordinator.device_client.start_keepalive.assert_not_called()


@pytest.mark.asyncio
async def test_unreachable_go2rtc_falls_back_to_the_local_serve_url():
    """The defect: returning None here left the camera with no stream at all."""
    cam = _make_camera()
    cam._publish_to_go2rtc = AsyncMock(return_value=None)

    assert await cam.stream_source() == _expected_serve_url(cam)
    # setup must still be fast - no session started here
    cam.coordinator.device_client.start_keepalive.assert_not_called()


@pytest.mark.asyncio
async def test_sdes_push_without_go2rtc_still_returns_none():
    """Push mode has nothing to fall back to: nothing binds the local serve, so
    handing HA that URL would just point it at a dead port."""
    cam = _make_camera(is_sdes=True, sdes_push=True)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)

    assert await cam.stream_source() is None


@pytest.mark.asyncio
async def test_sdes_with_push_disabled_falls_back_like_any_pull_camera():
    """With push off the local serve *is* the source, so the fallback applies."""
    cam = _make_camera(is_sdes=True, sdes_push=False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)

    assert await cam.stream_source() == _expected_serve_url(cam)
