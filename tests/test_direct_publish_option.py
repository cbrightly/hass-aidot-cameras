"""The direct publish option (library AIDOT_DIRECT_PUBLISH).

With it on the library publishes each camera straight into go2rtc with no
ffmpeg in between, and DTLS cameras publish too instead of being pulled from a
local HTTP serve port - so they take the same push path SDES cameras already
use: placeholder stream definition, publish URL handed to HA, and a wait on the
publisher reaching go2rtc rather than on a local port.
"""

import os
from unittest.mock import AsyncMock, patch

from custom_components.aidot import camera as camera_mod
from custom_components.aidot.const import CONF_DIRECT_PUBLISH, DEFAULT_DIRECT_PUBLISH

from .test_camera import _make_camera


def _apply(options):
    """Run just the env-var block of async_setup_entry for this option."""
    if options.get(CONF_DIRECT_PUBLISH, DEFAULT_DIRECT_PUBLISH):
        os.environ["AIDOT_DIRECT_PUBLISH"] = "1"
    else:
        os.environ.pop("AIDOT_DIRECT_PUBLISH", None)


def test_default_is_off():
    assert DEFAULT_DIRECT_PUBLISH is False


def test_option_sets_and_clears_the_library_switch():
    with patch.dict(os.environ, {}, clear=False):
        _apply({CONF_DIRECT_PUBLISH: True})
        assert os.environ["AIDOT_DIRECT_PUBLISH"] == "1"
        _apply({})
        assert "AIDOT_DIRECT_PUBLISH" not in os.environ


def test_setup_entry_carries_the_same_block():
    import custom_components.aidot as init_mod

    src = open(init_mod.__file__).read()
    assert 'os.environ["AIDOT_DIRECT_PUBLISH"] = "1"' in src
    assert 'os.environ.pop("AIDOT_DIRECT_PUBLISH", None)' in src


def test_option_is_exposed_in_the_options_schema_and_strings():
    import json
    from pathlib import Path

    from custom_components.aidot import config_flow

    assert "CONF_DIRECT_PUBLISH" in open(config_flow.__file__).read()
    base = Path(config_flow.__file__).parent
    for name in ("strings.json", "translations/en.json"):
        step = json.loads((base / name).read_text())["options"]["step"]["streaming"]
        assert "direct_publish" in step["data"]
        assert "direct_publish" in step["data_description"]


def test_dtls_camera_stays_on_pull_by_default(monkeypatch):
    monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: False)
    cam = _make_camera(is_sdes=False)
    assert cam._sdes_push_enabled() is False


def _library_publishes(monkeypatch):
    """Stand in for a library that has direct publish switched on, so these
    tests do not depend on which library release CI installed."""
    monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: True)


def test_dtls_camera_pushes_with_direct_publish(monkeypatch):
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    assert cam._sdes_push_enabled() is True


def test_nothing_pushes_with_go2rtc_disabled(monkeypatch):
    _library_publishes(monkeypatch)
    monkeypatch.setattr(camera_mod, "_GO2RTC_ENABLED", False)
    assert _make_camera(is_sdes=False)._sdes_push_enabled() is False
    assert _make_camera(is_sdes=True)._sdes_push_enabled() is False


def test_an_older_library_never_switches_dtls_to_push(monkeypatch):
    """A library without rtsp_publish would serve a DTLS push through ffmpeg;
    the integration must not change the DTLS mode against it."""
    import builtins

    monkeypatch.setenv("AIDOT_DIRECT_PUBLISH", "1")
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "aidot_cameras.camera.rtsp_publish":
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert camera_mod._direct_publish_enabled() is False


async def test_dtls_push_view_waits_on_the_publisher_not_a_local_port(monkeypatch):
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    dc = cam.coordinator.device_client
    url = await cam.stream_source()
    assert url == cam._push_serve_url
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url
    # The stream definition exists before the publish (go2rtc refuses a
    # publish into an unknown stream), using the inert placeholder source.
    cam._publish_to_go2rtc.assert_awaited_once_with(cam._serve_url)
    cam._await_publisher_attached.assert_awaited()
    dc.async_wait_serve_ready.assert_not_awaited()  # no local serve to wait on


async def test_dtls_setup_time_answer_is_the_push_url(monkeypatch):
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    assert await cam.stream_source() == cam._push_serve_url
    # No placeholder registered at setup for a push camera (see stream_source).
    cam._publish_to_go2rtc.assert_not_awaited()


def test_the_switch_is_read_from_the_library(monkeypatch):
    """With a library that has direct publish, the integration follows its
    AIDOT_DIRECT_PUBLISH switch (skipped against an older library)."""
    import pytest

    pytest.importorskip("aidot_cameras.camera.rtsp_publish")
    monkeypatch.setenv("AIDOT_DIRECT_PUBLISH", "1")
    assert camera_mod._direct_publish_enabled() is True
    monkeypatch.delenv("AIDOT_DIRECT_PUBLISH")
    assert camera_mod._direct_publish_enabled() is False
