"""Retired options become what replaced them, keeping what they meant.

Done once, by the config entry migration to 1.3.

- direct_publish / hls_audio / hls_direct_ts -> one publish_mode.
- sdes_audio (global "SDES camera audio") -> the per-camera switch; a stored
  False seeds those switches off.
- sdes_pin_h264 -> gone; the pin is always on.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot.const import (
    CONF_CAMERA_AUDIO_DEFAULT,
    CONF_PUBLISH_MODE,
    DOMAIN,
    PUBLISH_MODES,
)
from custom_components.aidot import _retire_stream_options, async_migrate_entry
from custom_components.aidot.library_settings import (
    camera_audio_default,
    stream_settings,
)


def migrated_options(options):
    """The rewritten options, or None when nothing needed rewriting."""
    out = dict(options)
    _retire_stream_options(out)
    return None if out == dict(options) else out


@pytest.mark.parametrize(
    ("stored", "mode"),
    [
        ({"direct_publish": True, "hls_audio": True, "hls_direct_ts": True}, "in_sync"),
        ({"hls_direct_ts": False}, "go2rtc"),
        ({"hls_audio": False}, "no_hls_audio"),
        ({"hls_audio": False, "hls_direct_ts": True}, "no_hls_audio"),
        ({"direct_publish": False}, "ffmpeg"),
        ({"direct_publish": False, "hls_audio": False}, "ffmpeg_no_hls_audio"),
    ],
)
def test_the_three_switches_become_the_mode_that_ran(stored, mode):
    out = migrated_options(stored)
    assert out == {CONF_PUBLISH_MODE: mode}


def test_every_old_combination_keeps_its_library_settings():
    """What the library ran with before, it runs with after."""
    for dp in (True, False):
        for aac in (True, False):
            for ts in (True, False):
                old = {"direct_publish": dp, "hls_audio": aac, "hls_direct_ts": ts}
                new = stream_settings(migrated_options(old))
                # The library's own rule: in-sync needs the other two.
                assert (new.direct_publish, new.publish_aac, new.hls_direct_ts) == (
                    dp,
                    aac,
                    ts and dp and aac,
                )


def test_a_stored_false_seeds_the_camera_audio_switches_off():
    assert migrated_options({"sdes_audio": False}) == {CONF_CAMERA_AUDIO_DEFAULT: False}
    # True was the default the switch starts from anyway.
    assert migrated_options({"sdes_audio": True}) == {}


def test_the_pin_option_is_dropped():
    assert migrated_options({"sdes_pin_h264": False, "x": 1}) == {"x": 1}


def test_nothing_to_do_is_none_and_other_options_are_kept():
    assert migrated_options({}) is None
    assert migrated_options({"serve_port_base": 20000}) is None
    assert migrated_options({"serve_port_base": 20000, "hls_audio": False}) == {
        "serve_port_base": 20000,
        CONF_PUBLISH_MODE: "no_hls_audio",
    }
    # A second run has nothing left to do.
    once = migrated_options({"direct_publish": False, "sdes_audio": False})
    assert migrated_options(once) is None


_RETIRED = {
    "direct_publish": True,
    "hls_audio": True,
    "hls_direct_ts": False,
    "sdes_audio": False,
    "sdes_pin_h264": True,
    "mains_idle_s": 300,
}
_REWRITTEN = {
    CONF_PUBLISH_MODE: "go2rtc",
    CONF_CAMERA_AUDIO_DEFAULT: False,
    "mains_idle_s": 300,
}


@pytest.mark.parametrize("minor_version", [1, 2])
async def test_the_entry_migration_rewrites_them(hass, minor_version):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"id": "u"},
        options=dict(_RETIRED, fast_connect=True),
        minor_version=minor_version,
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert entry.minor_version == 3
    expected = dict(_REWRITTEN)
    if minor_version == 1:
        # 1.1 -> 1.2 ran first and turned fast_connect into its mode.
        expected["connection_mode"] = "lan_direct"
    else:
        # A 1.2 entry has had that step; a key left over is not touched.
        expected["fast_connect"] = True
    assert dict(entry.options) == expected


async def test_a_13_entry_is_left_alone(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"id": "u"}, options=dict(_RETIRED), minor_version=3
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True
    assert dict(entry.options) == _RETIRED


def test_a_new_camera_audio_switch_starts_from_the_seed():
    assert camera_audio_default({CONF_CAMERA_AUDIO_DEFAULT: False}) is False
    assert camera_audio_default({}) is True


def test_each_mode_is_listed_once():
    assert len(set(PUBLISH_MODES.values())) == len(PUBLISH_MODES)
