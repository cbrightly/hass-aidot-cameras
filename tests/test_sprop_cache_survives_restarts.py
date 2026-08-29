"""The sprop cache must live under /config, or every update wipes it.

The library caches each camera's H.264 parameter sets
(``sprop-parameter-sets``) and injects them into the ffmpeg serve SDP so a
stream can start without waiting for an intact in-band keyframe. Its default
cache directory is under the process home - which, in a Home Assistant
container, is EPHEMERAL: recreating the container (a core update, an
integration update that rebuilds) starts every camera sprop-less, and each
first serve must win the in-band-SPS lottery inside ffmpeg's 2 s
analyzeduration window.

Observed 2026-08-24 on the reference install, first minutes after the
2.17.10 update restart: the cache directory was born at container start, and
the loss-prone PTZ cycled ``Could not find codec parameters ... unspecified
size`` -> ``dimensions not set`` -> exit 234 repeatedly until its sprop was
re-captured - the "odd behavior for some devices" seen on the live page.

The fix: setup points ``AIDOT_SPROP_DIR`` at a directory under HA's
persistent config path before any camera client exists. ``setdefault``-style,
so an operator override in the add-on/environment still wins.
"""
import os
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot import DOMAIN, async_setup_entry


class _Stop(Exception):
    pass


async def _run_setup(hass):
    """Drive setup far enough for the env to be configured, then bail."""
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    with patch("custom_components.aidot.AidotDeviceManagerCoordinator",
               side_effect=_Stop), \
            patch("custom_components.aidot._migrate_relocated_camera_entities"):
        try:
            await async_setup_entry(hass, entry)
        except _Stop:
            pass


async def test_setup_points_the_sprop_cache_at_persistent_config(hass):
    os.environ.pop("AIDOT_SPROP_DIR", None)
    try:
        await _run_setup(hass)
        got = os.environ.get("AIDOT_SPROP_DIR")
        assert got, "setup must set AIDOT_SPROP_DIR"
        assert got.startswith(hass.config.config_dir), (
            "the sprop cache must live under the persistent config dir, not "
            "the container-ephemeral home directory")
        assert os.path.isdir(got), "setup must create the directory"
    finally:
        os.environ.pop("AIDOT_SPROP_DIR", None)


async def test_an_operator_override_wins(hass):
    os.environ["AIDOT_SPROP_DIR"] = "/tmp/operator-choice"
    try:
        await _run_setup(hass)
        assert os.environ["AIDOT_SPROP_DIR"] == "/tmp/operator-choice", (
            "an explicit operator setting must not be overwritten")
    finally:
        os.environ.pop("AIDOT_SPROP_DIR", None)
