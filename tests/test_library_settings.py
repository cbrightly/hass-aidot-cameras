"""Each entry hands its stream options to its own library client.

They used to be written into the process environment, which every entry
shares: a second entry overwrote the first's settings, a removed option left
its value behind, and what a session ran with depended on which entry set up
last. Now the options travel as the client's StreamSettings and nothing is
written to os.environ.
"""

import os
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot import camera as camera_mod
from custom_components.aidot.const import (
    CONF_PUBLISH_MODE,
    CONF_SERVE_PORT_BASE,
    DEFAULT_SERVE_PORT_BASE,
    DOMAIN,
    PUBLISH_MODES,
)
from custom_components.aidot.library_settings import stream_settings

KNOBS = (
    "AIDOT_DIRECT_PUBLISH",
    "AIDOT_PUBLISH_AAC",
    "AIDOT_HLS_DIRECT_TS",
    "AIDOT_SDES_VIDEO_PT",
    "AIDOT_SERVE_PORT_BASE",
)
ALL_OFF = {
    CONF_PUBLISH_MODE: "ffmpeg_no_hls_audio",
    CONF_SERVE_PORT_BASE: 20000,
}
ALL_ON = {
    CONF_PUBLISH_MODE: "in_sync",
    CONF_SERVE_PORT_BASE: 30000,
}
CAMERA = {
    "id": "dev-cam-1",
    "name": "Porch",
    "modelId": "LK.IPC.A000088",
    "aesKey": [None],
    "properties": {"isDTLS": "1"},
}


@pytest.fixture(autouse=True)
def _no_knobs(monkeypatch):
    for name in KNOBS:
        monkeypatch.delenv(name, raising=False)


def _env():
    return {k: os.environ.get(k) for k in KNOBS}


def test_every_switch_is_stated():
    # The library's defaults are on, so each field is set either way.
    for opts in ({}, ALL_OFF, ALL_ON):
        s = stream_settings(opts)
        assert None not in (
            s.direct_publish,
            s.publish_aac,
            s.hls_direct_ts,
            s.sdes_video_pt,
        )
    off = stream_settings(ALL_OFF)
    assert (off.direct_publish, off.publish_aac, off.hls_direct_ts) == (
        False,
        False,
        False,
    )


def test_each_mode_is_its_triple():
    for mode, triple in PUBLISH_MODES.items():
        s = stream_settings({CONF_PUBLISH_MODE: mode})
        assert (s.direct_publish, s.publish_aac, s.hls_direct_ts) == triple
    # Unsaved: in sync, all three on.
    s = stream_settings({})
    assert (s.direct_publish, s.publish_aac, s.hls_direct_ts) == (True, True, True)


def test_no_mode_saves_in_sync_without_what_it_needs():
    # The three switches let in-sync audio be on with direct publish or the
    # AAC track off, where it silently did nothing. No mode can say that.
    for dp, aac, ts in PUBLISH_MODES.values():
        assert not ts or (dp and aac)


def test_an_unknown_stored_mode_reads_as_the_default():
    s = stream_settings({CONF_PUBLISH_MODE: "bogus"})
    assert (s.direct_publish, s.publish_aac, s.hls_direct_ts) == (True, True, True)


def test_the_sdes_offer_is_always_pinned_to_h264():
    # The retired sdes_pin_h264 option; never 97, which returns no video.
    for opts in ({}, ALL_OFF, ALL_ON, {"sdes_pin_h264": False}):
        assert stream_settings(opts).sdes_video_pt == "96"


async def test_setup_writes_nothing_to_the_environment(hass):
    from custom_components.aidot import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options=ALL_OFF)
    entry.add_to_hass(hass)
    before = _env()

    class _Stop(Exception):
        pass

    with (
        patch(
            "custom_components.aidot.AidotDeviceManagerCoordinator",
            side_effect=_Stop,
        ),
    ):
        with pytest.raises(_Stop):
            await async_setup_entry(hass, entry)
    assert _env() == before


async def test_two_entries_keep_their_own_settings(hass, monkeypatch):
    from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator

    async def _no_prefetch(_dc):
        return None

    monkeypatch.setattr("aidot_cameras.client._prefetch_ice_config", _no_prefetch)
    # An operator's variable does not change what either entry runs with.
    monkeypatch.setenv("AIDOT_DIRECT_PUBLISH", "0")
    before = _env()
    coords = []
    for data, opts in (({"id": "u1"}, ALL_ON), ({"id": "u2"}, ALL_OFF)):
        entry = MockConfigEntry(domain=DOMAIN, data=data, options=opts)
        entry.add_to_hass(hass)
        coords.append(AidotDeviceManagerCoordinator(hass, entry))
    on, off = coords
    assert on.client.stream_settings == stream_settings(ALL_ON)
    assert off.client.stream_settings == stream_settings(ALL_OFF)
    dc_on = on.client.get_device_client(dict(CAMERA))
    dc_off = off.client.get_device_client(dict(CAMERA))
    assert dc_on is not dc_off
    assert camera_mod._direct_publish_enabled(dc_on) is True
    assert camera_mod._direct_publish_enabled(dc_off) is False
    assert camera_mod._hls_audio_enabled(dc_on) is True
    assert camera_mod._hls_audio_enabled(dc_off) is False
    # Building the second entry did not touch the first's.
    assert camera_mod._direct_publish_enabled(dc_on) is True
    assert _env() == before


def test_serve_port_base_is_the_option_then_the_variable_then_the_default(
    monkeypatch,
):
    slot = camera_mod._serve_port("x") - DEFAULT_SERVE_PORT_BASE
    assert camera_mod._serve_port("x", 20000) == 20000 + slot
    monkeypatch.setenv("AIDOT_SERVE_PORT_BASE", "30000")
    assert camera_mod._serve_port("x") == 30000 + slot
    assert camera_mod._serve_port("x", 20000) == 20000 + slot
