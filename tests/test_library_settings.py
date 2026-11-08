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
    CONF_DIRECT_PUBLISH,
    CONF_HLS_AUDIO,
    CONF_HLS_DIRECT_TS,
    CONF_SDES_PIN_H264,
    CONF_SERVE_PORT_BASE,
    DEFAULT_SERVE_PORT_BASE,
    DOMAIN,
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
    CONF_DIRECT_PUBLISH: False,
    CONF_HLS_AUDIO: False,
    CONF_HLS_DIRECT_TS: False,
    CONF_SDES_PIN_H264: False,
    CONF_SERVE_PORT_BASE: 20000,
}
ALL_ON = {
    CONF_DIRECT_PUBLISH: True,
    CONF_HLS_AUDIO: True,
    CONF_HLS_DIRECT_TS: True,
    CONF_SDES_PIN_H264: True,
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
    assert off.sdes_video_pt == "none"


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
        patch("custom_components.aidot._migrate_relocated_camera_entities"),
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
