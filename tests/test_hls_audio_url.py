"""Home Assistant's HLS path asks go2rtc for the AAC track, and nothing else does.

With the HLS audio option on, the direct publish carries A-law first and AAC
after it (python-aidot-cameras 1.0.0rc32, AIDOT_PUBLISH_AAC=1). Home Assistant's stream component - its HLS player and
camera.record - keeps only AAC/MP3 audio, so on that path the go2rtc URL names
the AAC track. Every other caller (HA's WebRTC provider, the webrtc card) gets
the URL unchanged and keeps A-law.
"""

import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot import camera as cam_mod
from custom_components.aidot.camera import AidotCamera, _hls_audio_url

RTSP = "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"


@pytest.mark.parametrize(
    "url,expected",
    [
        (RTSP, RTSP + "?video&audio=aac"),
        (RTSP + "?video", RTSP + "?video&audio=aac"),
        (RTSP + "?audio=pcma", RTSP + "?audio=pcma"),  # an explicit choice wins
        ("http://127.0.0.1:18989/x.ts", "http://127.0.0.1:18989/x.ts"),
        (None, None),
    ],
)
def test_hls_audio_url(url, expected):
    assert _hls_audio_url(url) == expected


def _cam(url):
    cam = object.__new__(AidotCamera)
    cam._resolve_stream_source = AsyncMock(return_value=url)
    return cam


@pytest.mark.asyncio
async def test_the_hls_path_gets_the_aac_track(monkeypatch):
    monkeypatch.setattr(cam_mod, "_hls_audio_enabled", lambda: True)
    token = cam_mod._STREAM_SOURCE_HLS.set(True)
    try:
        assert await _cam(RTSP).stream_source() == RTSP + "?video&audio=aac"
    finally:
        cam_mod._STREAM_SOURCE_HLS.reset(token)


@pytest.mark.asyncio
async def test_every_other_caller_gets_the_url_unchanged():
    assert await _cam(RTSP).stream_source() == RTSP


@pytest.mark.asyncio
async def test_the_hls_path_is_unchanged_when_hls_audio_is_off(monkeypatch):
    # No AAC track is published, so naming one would only change the URL.
    monkeypatch.setattr(cam_mod, "_hls_audio_enabled", lambda: False)
    token = cam_mod._STREAM_SOURCE_HLS.set(True)
    try:
        assert await _cam(RTSP).stream_source() == RTSP
    finally:
        cam_mod._STREAM_SOURCE_HLS.reset(token)


def test_hls_audio_follows_the_library_switch(monkeypatch):
    import types

    fake = types.ModuleType("aidot_cameras.camera.aac_track")
    fake.publish_aac_enabled = lambda: True
    monkeypatch.setitem(sys.modules, "aidot_cameras.camera.aac_track", fake)
    assert cam_mod._hls_audio_enabled() is True
    fake.publish_aac_enabled = lambda: False
    assert cam_mod._hls_audio_enabled() is False


def test_hls_audio_is_off_on_a_library_without_the_aac_track(monkeypatch):
    # sys.modules[name] = None makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "aidot_cameras.camera.aac_track", None)
    assert cam_mod._hls_audio_enabled() is False
