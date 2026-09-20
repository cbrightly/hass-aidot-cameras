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


def _reachable(cam, ok: bool):
    """Pin the cached go2rtc reachability answer for this camera's hass."""
    import time

    cam.hass.data = {"aidot": {"go2rtc-reachable": (time.monotonic(), ok)}}


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
    _reachable(cam, True)
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
    _reachable(cam, True)
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


async def test_hls_gets_an_aac_source_only_with_direct_publish(monkeypatch):
    """HA's HLS player takes aac/mp3 only, and a direct publish sends G.711.
    go2rtc gets a transcoding source AFTER the live one, so that consumer is
    served while WebRTC viewers keep the passthrough."""
    from unittest.mock import AsyncMock, patch

    cam = _make_camera(is_sdes=True)
    client = AsyncMock()
    client.ensure_stream.return_value = True
    client.rtsp_url.return_value = "rtsp://127.0.0.1:8554/aidot_x"
    with patch.object(camera_mod, "Go2rtcClient", return_value=client):
        _library_publishes(monkeypatch)
        await cam._publish_to_go2rtc(cam._serve_url)
        kwargs = client.ensure_stream.await_args.kwargs
        assert kwargs["extra_sources"] == (f"ffmpeg:{cam._go2rtc_name}#audio=aac",)

        client.ensure_stream.reset_mock()
        monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: False)
        await cam._publish_to_go2rtc(cam._serve_url)
        # The ffmpeg serve already produces AAC: no second source, no transcode.
        assert client.ensure_stream.await_args.kwargs["extra_sources"] == ()


# --------------------------------------------------------------------------- #
# push mode requires a go2rtc this integration can actually reach              #
# --------------------------------------------------------------------------- #


async def test_dtls_stays_pullable_when_go2rtc_cannot_be_reached(monkeypatch):
    """HA's bundled go2rtc keeps its API on a unix socket and its RTSP on
    18554, so this integration cannot reach it - but HA can, which is why the
    pull serve works there. Handing back a push URL would point HA at a port
    nothing listens on and the camera would go dark."""
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(cam, False)
    assert cam._sdes_push_enabled() is True  # the option is on...
    assert await cam._push_mode() is False  # ...but there is nowhere to publish


async def test_dtls_pushes_when_go2rtc_answers(monkeypatch):
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(cam, True)
    assert await cam._push_mode() is True


async def test_sdes_pushes_regardless_of_the_probe(monkeypatch):
    """SDES has no working pull path, so push is its only mode either way."""
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=True)
    _reachable(cam, False)
    assert await cam._push_mode() is True


async def test_the_reachability_answer_is_shared_and_cached(monkeypatch):
    """One probe per window for the whole fleet: this runs on setup and on
    every view, and a down go2rtc must not cost each camera its own timeout."""
    from unittest.mock import AsyncMock, patch

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    cam.hass.data = {}
    client = AsyncMock()
    client.available.return_value = True
    with patch.object(camera_mod, "Go2rtcClient", return_value=client):
        assert await cam._go2rtc_reachable() is True
        assert await cam._go2rtc_reachable() is True
        other = _make_camera(is_sdes=False)
        other.hass = cam.hass
        assert await other._go2rtc_reachable() is True
    assert client.available.await_count == 1


async def test_a_view_falls_back_to_the_pull_serve_when_go2rtc_is_down(monkeypatch):
    """The regression this guards: with the option on and go2rtc unreachable,
    a DTLS camera must still hand back its pullable http:// serve."""
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    cam._setup_complete = True
    _reachable(cam, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)  # go2rtc unreachable
    url = await cam.stream_source()
    assert url == cam._serve_url and url.startswith("http://")
    dc = cam.coordinator.device_client
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == cam._serve_url


# --------------------------------------------------------------------------- #
# the bundled-go2rtc deployment, end to end through stream_source              #
# --------------------------------------------------------------------------- #
#
# Home Assistant's bundled go2rtc keeps its API on a unix socket and its RTSP
# on 18554, so this integration's probe of 127.0.0.1:1984 fails while Home
# Assistant itself can register streams with it. That deployment is the
# default for most users and nothing exercised it, which is how a blanked
# DTLS camera shipped.


async def test_bundled_deployment_dtls_view_is_pullable(monkeypatch):
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    _reachable(cam, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    cam._setup_complete = True
    url = await cam.stream_source()
    # http:// is the point: Home Assistant registers this with its own go2rtc.
    assert url.startswith("http://") and url == cam._serve_url


async def test_bundled_deployment_dtls_setup_answer_is_pullable(monkeypatch):
    """The setup-time answer decides whether HA offers WebRTC at all, and it
    must not be a push URL pointing at a port nothing listens on."""
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(cam, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    assert cam._setup_complete is False
    url = await cam.stream_source()
    assert url is not None and not url.startswith("rtsp://")


async def test_bundled_deployment_sdes_still_publishes(monkeypatch):
    """SDES has no working pull path, so it publishes either way - and says
    so by handing back the publish URL rather than a local serve."""
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    _reachable(cam, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    cam._setup_complete = True
    cam._await_publisher_attached = AsyncMock(return_value=False)
    url = await cam.stream_source()
    assert url == cam._push_serve_url


async def test_a_reachable_go2rtc_still_pushes_dtls(monkeypatch):
    """The other half of the contract: where the probe succeeds, nothing
    about the previous behaviour changes."""
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    _reachable(cam, True)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://127.0.0.1:8554/aidot_x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    assert (await cam.stream_source()) == cam._push_serve_url
