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


from custom_components.aidot import _apply_library_env as _apply  # the real block


from custom_components.aidot import go2rtc_target as gt

ADDON = gt.Go2rtcTarget(
    "http://127.0.0.1:1984", object(), "127.0.0.1", 8554, gt.KIND_ADDON
)
EXPLICIT = gt.Go2rtcTarget(
    "http://10.0.0.5:1984", object(), "10.0.0.5", 8554, gt.KIND_EXPLICIT
)


def _resolves_to(monkeypatch, target):
    """Pin which go2rtc the integration resolves (None: no go2rtc at all)."""
    monkeypatch.setattr(
        camera_mod, "resolve_go2rtc_target", AsyncMock(return_value=target)
    )


def _reachable(monkeypatch, ok: bool):
    """A go2rtc answers (the add-on) or none does."""
    _resolves_to(monkeypatch, ADDON if ok else None)


def test_default_is_on():
    # Since 2.35.0: validated on every model, and the AAC track and in-sync
    # recordings are built on it.
    assert DEFAULT_DIRECT_PUBLISH is True


def test_option_sets_and_clears_the_library_switch():
    with patch.dict(os.environ, {}, clear=False):
        _apply({CONF_DIRECT_PUBLISH: True})
        assert os.environ["AIDOT_DIRECT_PUBLISH"] == "1"
        # Off is said explicitly: the library's own default is on since
        # 1.0.0rc44, so an unset variable would mean on.
        _apply({CONF_DIRECT_PUBLISH: False})
        assert os.environ["AIDOT_DIRECT_PUBLISH"] == "0"
        _apply({})  # an entry that never saved the option gets the default: on
        assert os.environ["AIDOT_DIRECT_PUBLISH"] == "1"


def test_option_is_exposed_in_the_options_schema_and_strings():
    import json
    from pathlib import Path

    from custom_components.aidot import config_flow

    assert "CONF_DIRECT_PUBLISH" in open(config_flow.__file__).read()
    base = Path(config_flow.__file__).parent
    for name in ("strings.json", "translations/en.json"):
        step = json.loads((base / name).read_text())["options"]["step"]["audio"]
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
    _reachable(monkeypatch, True)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    dc = cam.coordinator.device_client
    url = await cam.stream_source()
    assert url == await cam._push_serve_url()
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == url
    # The stream definition exists before the publish (go2rtc refuses a
    # publish into an unknown stream), using the inert placeholder source.
    cam._publish_to_go2rtc.assert_awaited_once_with(cam._serve_url, push=True)
    cam._await_publisher_attached.assert_awaited()
    dc.async_wait_serve_ready.assert_not_awaited()  # no local serve to wait on


async def test_dtls_setup_time_answer_is_the_push_url(monkeypatch):
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(monkeypatch, True)
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    assert await cam.stream_source() == await cam._push_serve_url()
    # No placeholder registered at setup for a push camera (see stream_source).
    cam._publish_to_go2rtc.assert_not_awaited()


def test_the_switch_is_read_from_the_library(monkeypatch):
    """With a library that has direct publish, the integration follows its
    AIDOT_DIRECT_PUBLISH switch (skipped against an older library).

    Unset is the library's own default - off before 1.0.0rc44, on from it -
    so that case is checked against the library rather than against a fixed
    answer; the integration writes "1" or "0" explicitly for that reason.
    """
    import pytest

    rtsp_publish = pytest.importorskip("aidot_cameras.camera.rtsp_publish")
    monkeypatch.setenv("AIDOT_DIRECT_PUBLISH", "1")
    assert camera_mod._direct_publish_enabled() is True
    monkeypatch.setenv("AIDOT_DIRECT_PUBLISH", "0")
    assert camera_mod._direct_publish_enabled() is False
    monkeypatch.delenv("AIDOT_DIRECT_PUBLISH")
    assert camera_mod._direct_publish_enabled() is rtsp_publish.direct_publish_enabled()


# --------------------------------------------------------------------------- #
# push mode requires a go2rtc this integration can actually reach              #
# --------------------------------------------------------------------------- #


async def test_dtls_stays_pullable_when_go2rtc_cannot_be_reached(monkeypatch):
    """With no go2rtc to publish into (no explicit URL, no add-on, no
    bundled handle), handing back a push URL would point HA at a port
    nothing listens on and the camera would go dark."""
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(monkeypatch, False)
    assert cam._sdes_push_enabled() is True  # the option is on...
    assert await cam._push_mode() is False  # ...but there is nowhere to publish


async def test_dtls_pushes_when_go2rtc_answers(monkeypatch):
    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(monkeypatch, True)
    assert await cam._push_mode() is True


async def test_sdes_pushes_into_whichever_go2rtc_resolves(monkeypatch):
    """SDES has no working pull path, so it publishes wherever a go2rtc is:
    the add-on or an explicit AIDOT_GO2RTC_API. (With none at all it is left
    without a stream - see test_bundled_go2rtc.py.)"""
    _library_publishes(monkeypatch)
    for target in (ADDON, EXPLICIT):
        cam = _make_camera(is_sdes=True)
        _resolves_to(monkeypatch, target)
        assert await cam._push_mode() is True


async def test_the_reachability_answer_is_shared_and_cached(monkeypatch):
    """One probe per window for the whole fleet: this runs on setup and on
    every view, and a down go2rtc must not cost each camera its own timeout.

    Only an explicit AIDOT_GO2RTC_API is probed here (the resolver takes it on
    trust); the add-on and bundled answers are already the resolver's probe."""
    from unittest.mock import AsyncMock, patch

    _library_publishes(monkeypatch)
    _resolves_to(
        monkeypatch,
        gt.Go2rtcTarget(
            "http://10.0.0.5:1984", object(), "10.0.0.5", 8554, gt.KIND_EXPLICIT
        ),
    )
    cam = _make_camera(is_sdes=False)
    cam.hass.data = {}
    client = AsyncMock()
    client.available.return_value = True
    with patch(
        "custom_components.aidot.go2rtc_target.Go2rtcClient", return_value=client
    ):
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
    _reachable(monkeypatch, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)  # go2rtc unreachable
    url = await cam.stream_source()
    assert url == cam._serve_url and url.startswith("http://")
    dc = cam.coordinator.device_client
    assert dc.start_keepalive.await_args.kwargs["rtsp_push_url"] == cam._serve_url


# --------------------------------------------------------------------------- #
# the bundled-go2rtc deployment, end to end through stream_source              #
# --------------------------------------------------------------------------- #
#
# Home Assistant's bundled go2rtc is never published into, so an install with
# only that server resolves no target. A blanked DTLS camera once shipped
# because nothing exercised that case; with no go2rtc resolved, a DTLS camera
# hands back its pullable serve, which Home Assistant's go2rtc pulls.


async def test_no_go2rtc_dtls_view_is_pullable(monkeypatch):
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    _reachable(monkeypatch, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    cam._setup_complete = True
    url = await cam.stream_source()
    # http:// is the point: Home Assistant registers this with its own go2rtc.
    assert url.startswith("http://") and url == cam._serve_url


async def test_no_go2rtc_dtls_setup_answer_is_pullable(monkeypatch):
    """The setup-time answer decides whether HA offers WebRTC at all, and it
    must not be a push URL pointing at a port nothing listens on."""
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False)
    _reachable(monkeypatch, False)
    cam._publish_to_go2rtc = AsyncMock(return_value=None)
    assert cam._setup_complete is False
    url = await cam.stream_source()
    assert url is not None and not url.startswith("rtsp://")


async def test_a_reachable_go2rtc_still_pushes_dtls(monkeypatch):
    """The other half of the contract: where the probe succeeds, nothing
    about the previous behaviour changes."""
    from unittest.mock import AsyncMock

    _library_publishes(monkeypatch)
    cam = _make_camera(is_sdes=False, stream_rtsp_url=None)
    _reachable(monkeypatch, True)
    cam._setup_complete = True
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://127.0.0.1:8554/aidot_x")
    cam._await_publisher_attached = AsyncMock(return_value=True)
    assert (await cam.stream_source()) == await cam._push_serve_url()


# --------------------------------------------------------------------------- #
# a push camera's placeholder must not be a port we stopped binding            #
# --------------------------------------------------------------------------- #


async def test_push_placeholder_is_not_the_local_serve_port(monkeypatch):
    """go2rtc needs a source before it will accept a publish, but that source
    must not be this camera's local serve port.

    In push mode nothing ever binds that port - the media arrives by ANNOUNCE -
    so the definition names a local address we have stopped listening on. If the
    publisher drops, go2rtc dials it; and any other process that later binds the
    port is then served to viewers as this camera. The placeholder has to be
    inert by construction instead.
    """
    from unittest.mock import AsyncMock, patch

    cam = _make_camera(is_sdes=True)
    _reachable(monkeypatch, True)
    client = AsyncMock()
    client.ensure_stream.return_value = True
    client.rtsp_url.return_value = "rtsp://127.0.0.1:8554/aidot_x"
    with patch(
        "custom_components.aidot.go2rtc_target.Go2rtcClient", return_value=client
    ):
        _library_publishes(monkeypatch)
        await cam._publish_to_go2rtc(cam._serve_url)

    src = client.ensure_stream.await_args.args[1]
    assert src != cam._serve_url, "push registers the serve port it never binds"
    # Whatever it is, it must not name the serve port at all.
    port = cam._serve_url.rsplit(":", 1)[-1].split("/")[0]
    assert port not in src, f"placeholder still names the serve port: {src}"


async def test_pull_mode_still_registers_the_real_serve_url(monkeypatch):
    """The other half: a camera that is genuinely pulled must keep its serve
    URL as the source, or go2rtc has nothing to dial."""
    from unittest.mock import AsyncMock, patch

    cam = _make_camera(is_sdes=False)
    # A go2rtc answers, and direct publish is off below -> DTLS is pulled.
    _reachable(monkeypatch, True)
    client = AsyncMock()
    client.ensure_stream.return_value = True
    client.rtsp_url.return_value = "rtsp://127.0.0.1:8554/aidot_x"
    with patch(
        "custom_components.aidot.go2rtc_target.Go2rtcClient", return_value=client
    ):
        monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: False)
        await cam._publish_to_go2rtc(cam._serve_url)

    assert client.ensure_stream.await_args.args[1] == cam._serve_url


async def test_no_aac_transcode_source_is_ever_offered(monkeypatch):
    """go2rtc must never be given an AAC transcoding source, either way.

    Measured on the box 2026-09-23, same stream, same 15 s wall-clock window:
        ?audio=pcma  ->  15.000 s container  (1.000x, exact)
        ?audio=aac   -> 163.143 s container  (10.88x)
    The transcode is stamped on a 90 kHz clock while it advertises
    MPEG4-GENERIC/8000, so every HLS viewer plays about eleven times too slow.
    It is not camera-specific: 5 of 5 cameras across 3 models sat between
    10.76x and 11.12x.

    A newer go2rtc downstream does not rescue it. Offered the source, Home
    Assistant's bundled go2rtc prefers it and relays the bad timestamps
    unchanged (166.3 s for that same window), which is why simply moving
    viewers to the native WebRTC card did not help.

    WebRTC viewers keep audio as G.711 passthrough; HA's HLS dialog loses
    audio, which is the deliberate trade - a silent stream at 1x beats an 11x
    one with sound.
    """
    from unittest.mock import AsyncMock, patch

    for publishes in (True, False):
        cam = _make_camera(is_sdes=True)
        client = AsyncMock()
        client.ensure_stream.return_value = True
        client.rtsp_url.return_value = "rtsp://127.0.0.1:8554/aidot_x"
        with patch(
            "custom_components.aidot.go2rtc_target.Go2rtcClient", return_value=client
        ):
            monkeypatch.setattr(
                camera_mod, "_direct_publish_enabled", lambda: publishes
            )
            await cam._publish_to_go2rtc(cam._serve_url)
            sources = client.ensure_stream.await_args.kwargs["extra_sources"]
            assert sources == (), f"direct_publish={publishes} offered {sources}"
