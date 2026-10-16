"""The lifecycle fixes from the 2026-10-15 review, pinned.

Each test names the defect it guards against; the behaviours are small, so
most are checked directly, and the two that live inside long entity methods
are checked at the source like the library's own guard tests do.
"""

import inspect
import json
import pathlib

import pytest
from aidot_cameras.exceptions import AidotAuthFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.aidot import button, camera, config_flow, event, notify_dispatch

from .test_camera_goes_unavailable import _coordinator

_PKG = pathlib.Path(camera.__file__).parent


async def test_an_auth_failure_on_a_camera_poll_starts_reauth():
    """It used to count as an ordinary miss and go unavailable 15 min later."""
    c = _coordinator(AidotAuthFailed("both tokens gone"))
    with pytest.raises(ConfigEntryAuthFailed):
        await c._async_update_data()


def test_event_entity_and_notification_share_one_kind_mapping():
    assert not hasattr(event, "_CODE_TO_TYPE")
    src = inspect.getsource(event)
    assert "event_kind(event)" in src
    # The shared mapping coerces, so an integer code reads the same everywhere.
    assert notify_dispatch.event_kind({"eventCode": 4}) == "person"
    assert notify_dispatch.event_kind({"eventCode": "4"}) == "person"
    assert notify_dispatch.event_kind({"eventCode": 1}) == "motion"


def test_registered_cameras_are_pruned_to_the_live_coordinators():
    src = inspect.getsource(camera.async_setup_entry)
    assert "registered.intersection_update(coordinator.camera_coordinators)" in src


def test_the_piggyback_listing_is_entry_bound():
    src = inspect.getsource(camera.AidotCamera)
    i = src.index("async_piggyback_sd_refresh()")
    assert "async_create_background_task" in src[i - 200 : i + 400]
    assert "self.hass.async_create_task(self.coordinator.async_piggyback_sd_refresh())" not in src


def test_the_talk_transcode_kills_ffmpeg_when_cancelled():
    src = inspect.getsource(camera.AidotCamera._decode_pcm_8k)
    assert "except asyncio.CancelledError:" in src
    assert "proc.kill()" in src.split("except asyncio.CancelledError:", 1)[1]


def test_the_sd_refresh_press_does_not_hold_the_platform_semaphore():
    assert button.PARALLEL_UPDATES == 1
    src = inspect.getsource(button.AidotSdRefreshButton.async_press)
    assert "async_create_background_task" in src


def test_dhcp_discovery_sets_a_unique_id():
    src = inspect.getsource(config_flow.AidotConfigFlow.async_step_dhcp)
    assert "async_set_unique_id(" in src


def test_strings_and_translations_agree_on_the_audio_gain_text():
    en = json.loads((_PKG / "translations" / "en.json").read_text())
    strings = json.loads((_PKG / "strings.json").read_text())
    key = ("options", "step", "audio", "data_description", "sdes_audio_gain_db")
    a, b = en, strings
    for k in key:
        a, b = a[k], b[k]
    assert a == b
    assert "-8" not in a.split("Installs")[0]  # the default is 0 dB now


def test_every_flow_abort_reason_has_a_translation():
    en = json.loads((_PKG / "translations" / "en.json").read_text())
    aborts = en["config"]["abort"]
    for reason in ("already_configured", "already_in_progress", "reauth_successful", "reconfigure_successful"):
        assert reason in aborts, reason
