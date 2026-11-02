"""The in-sync HLS option: Home Assistant's HLS path reads the library's TS.

With it on (library AIDOT_HLS_DIRECT_TS), a DTLS camera's HLS view and
recordings read a library-muxed MPEG-TS whose audio and video share one clock,
so a recording that joins a running stream is in step - through go2rtc's RTSP it
came out 0.1-0.75 s late (measured 2026-10-03). The camera is still opened the
usual way first: that session is what feeds the TS. WebRTC never uses it.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aidot import camera as cam_mod
from custom_components.aidot.const import CONF_HLS_DIRECT_TS, DEFAULT_HLS_DIRECT_TS

GO2RTC = "rtsp://127.0.0.1:8554/aidot_0123456789ab"
TS = "http://127.0.0.1:41000/aidot_0123456789ab.ts"


def _entity(ts_url):
    ent = cam_mod.AidotCamera.__new__(cam_mod.AidotCamera)
    dc = SimpleNamespace()
    if ts_url is not ...:
        dc.hls_ts_url = lambda: ts_url
    ent.coordinator = SimpleNamespace(device_client=dc)
    ent._resolve_stream_source = AsyncMock(return_value=GO2RTC)
    return ent


async def _source(ent, hls):
    token = cam_mod._STREAM_SOURCE_HLS.set(hls)
    try:
        with patch.object(cam_mod, "_hls_audio_enabled", return_value=True):
            return await ent.stream_source()
    finally:
        cam_mod._STREAM_SOURCE_HLS.reset(token)


def test_default_is_on():
    assert DEFAULT_HLS_DIRECT_TS is True


async def test_hls_reads_the_library_ts_when_it_is_available():
    ent = _entity(TS)
    assert await _source(ent, hls=True) == TS
    ent._resolve_stream_source.assert_awaited_once()  # the camera is still opened


@pytest.mark.parametrize("ts_url", [None, ...])  # off / SDES camera / older library
async def test_otherwise_hls_keeps_go2rtc_with_the_aac_track(ts_url):
    ent = _entity(ts_url)
    assert await _source(ent, hls=True) == GO2RTC + "?video&audio=aac"


async def test_webrtc_never_uses_the_ts():
    ent = _entity(TS)
    assert await _source(ent, hls=False) == GO2RTC


async def test_a_failing_library_call_falls_back_to_go2rtc():
    ent = _entity(TS)

    def boom():
        raise RuntimeError("router failed")

    ent.coordinator.device_client.hls_ts_url = boom
    assert await _source(ent, hls=True) == GO2RTC + "?video&audio=aac"


def test_option_sets_and_clears_the_library_switch():
    from custom_components.aidot import _apply_library_env

    with patch.dict(os.environ, {}, clear=False):
        _apply_library_env({CONF_HLS_DIRECT_TS: True})
        assert os.environ["AIDOT_HLS_DIRECT_TS"] == "1"
        _apply_library_env({CONF_HLS_DIRECT_TS: False})
        assert (
            os.environ["AIDOT_HLS_DIRECT_TS"] == "0"
        )  # explicit: the library defaults on


@pytest.mark.parametrize("resolved", [None, "http://127.0.0.1:18765/serve.ts"])
async def test_pull_mode_or_no_source_never_gets_the_ts(resolved):
    # Pull mode (go2rtc unreachable) has no direct publisher to feed the TS, and
    # with no source at all there is no session: both stay as they were.
    ent = _entity(TS)
    ent._resolve_stream_source = AsyncMock(return_value=resolved)
    assert await _source(ent, hls=True) == resolved


def _unloading(hass, others_loaded: int):
    from homeassistant.config_entries import ConfigEntryState

    entry = MagicMock(entry_id="this")
    entry.runtime_data.async_cleanup = AsyncMock()
    others = [
        MagicMock(entry_id=f"o{i}", state=ConfigEntryState.LOADED)
        for i in range(others_loaded)
    ]
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_entries = MagicMock(return_value=[entry, *others])
    hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
    hass.data = {}
    return entry


@pytest.mark.parametrize(("others", "stops"), [(0, 1), (1, 0)])
async def test_the_ts_server_stops_with_the_last_entry(monkeypatch, others, stops):
    # A reload or removal used to leave every camera's mux thread and the
    # loopback listener running for the life of the process.
    from aidot_cameras.camera import hls_ts

    from custom_components.aidot import async_unload_entry

    calls = []
    monkeypatch.setattr(hls_ts, "shutdown", lambda: calls.append(1))
    hass = MagicMock()
    entry = _unloading(hass, others)
    assert await async_unload_entry(hass, entry) is True
    assert len(calls) == stops


@pytest.mark.parametrize(
    ("in_sync", "prereqs", "pin", "pinned"),
    [
        (True, True, False, True),  # in-sync on: SDES cameras are pinned
        (True, False, False, False),  # in-sync cannot run: nothing pinned
        (False, True, False, False),  # today's default
        (False, True, True, True),  # the pin option alone, as before
    ],
)
def test_in_sync_audio_pins_sdes_cameras_to_h264(in_sync, prereqs, pin, pinned):
    # An SDES camera feeds the in-sync stream only when its offer is pinned to
    # H.264 (an H.265 answer keeps the ffmpeg serve), and the pin is off by
    # default - so with in-sync on, SDES cameras quietly stayed on go2rtc.
    from custom_components.aidot import _apply_library_env
    from custom_components.aidot.const import (
        CONF_DIRECT_PUBLISH,
        CONF_HLS_AUDIO,
        CONF_SDES_PIN_H264,
    )

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AIDOT_SDES_VIDEO_PT", None)
        _apply_library_env(
            {
                CONF_HLS_DIRECT_TS: in_sync,
                CONF_DIRECT_PUBLISH: prereqs,
                CONF_HLS_AUDIO: prereqs,
                CONF_SDES_PIN_H264: pin,
            }
        )
        assert os.environ.get("AIDOT_SDES_VIDEO_PT") == ("96" if pinned else "none")
