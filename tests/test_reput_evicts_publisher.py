"""Re-registering a push-mode stream must not evict its live publisher.

go2rtc's ``PUT /api/streams?name=&src=`` REPLACES the stream definition
wholesale. In push mode that definition holds only the inert HTTP placeholder,
so the PUT drops the live RTSP publisher from the stream - **even when ``src`` is
byte-identical to what is already registered**. The publishing ffmpeg never
notices: it keeps running and keeps pushing into a stream that no longer
references it, so nothing looks wrong from our side while every consumer gets
``DESCRIBE failed: 404``.

``stream_source()`` re-registered on every call (for self-healing, in case
go2rtc restarted and dropped the stream def) and Home Assistant calls
``stream_source()`` on every WebRTC offer. So each SDES view killed the publisher
the previous view had established - which is why an SDES camera 404'd on the
first view *and* on a second view taken while the publisher was demonstrably
serving, and why an ``ffprobe`` run straight from a shell succeeded against the
very same URL moments later (no view, so no re-PUT in between).

Reproduced against go2rtc 1.9.9 (linux/arm64) with two chained instances,
mirroring the box's AlexxIT-plus-bundled layout:

    register placeholder -> publish h264+pcm_alaw -> consume  => OK
    re-PUT the identical placeholder src           -> consume  => 404

A confirmed-live publisher also proves the stream definition exists, so there is
nothing for the self-healing PUT to repair - skipping it is safe as well as
necessary.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.camera import AidotCamera
from custom_components.aidot.const import CONF_SDES_PUSH

PLACEHOLDER = {"url": "http://127.0.0.1:18981/aaaa0000bbbb.ts"}
PUBLISHER = {"remote_addr": "127.0.0.1:59878"}


def _make_camera(*, is_sdes: bool, stream_rtsp_url: str | None) -> AidotCamera:
    device_client = MagicMock()
    device_client.device_id = "aaaa0000bbbb1111cccc2222dddd3333"
    device_client.is_sdes_camera = is_sdes
    device_client.is_battery_camera = False
    device_client.stream_rtsp_url = stream_rtsp_url
    device_client.status = SimpleNamespace(online=True)
    device_client.start_keepalive = AsyncMock()
    device_client.async_wait_serve_ready = AsyncMock()

    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = device_client
    cam.coordinator.config_entry = SimpleNamespace(
        entry_id="e1", options={CONF_SDES_PUSH: True}
    )
    cam.coordinator.sdes_audio_override = None
    cam._rtsp_name = device_client.device_id
    cam._setup_complete = True
    cam.hass = MagicMock()
    cam._set_stream_status = MagicMock()
    cam._stream_status = None  # read by stream_source()'s non-success exit
    return cam


async def _stream_source(cam: AidotCamera, producers: list | None):
    """Drive stream_source() with go2rtc reporting ``producers``.

    Returns ``(url, client)`` so a test can assert on the registration PUT
    (``client.ensure_stream``) - the call that does the evicting.
    """
    streams = (
        {} if producers is None else {"aidot_aaaa0000bbbb": {"producers": producers}}
    )
    client = MagicMock()
    client.list_streams = AsyncMock(return_value=streams)
    client.ensure_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value="rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb")
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_WAIT_S", 0.05),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_POLL_S", 0.01),
        patch.object(
            AidotCamera,
            "_push_serve_url",
            new_callable=PropertyMock,
            return_value="rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb",
        ),
    ):
        return await cam.stream_source(), client


# --------------------------------------------------------------------------- #
# the defect
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_live_publisher_is_not_re_registered():
    """The defect: a view must not PUT over a stream that has a live publisher."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/live")

    url, client = await _stream_source(cam, [PLACEHOLDER, PUBLISHER])

    client.ensure_stream.assert_not_awaited()
    # ...and the camera is still handed a usable URL, not soft-failed to None.
    assert url == "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"


@pytest.mark.asyncio
async def test_absent_publisher_still_re_registers():
    """Self-healing is preserved where it is actually needed: with no publisher
    the stream def may genuinely be missing (go2rtc restarted), so re-PUT it."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/stale")

    _url, client = await _stream_source(cam, [PLACEHOLDER])

    client.ensure_stream.assert_awaited_once()
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


@pytest.mark.asyncio
async def test_cold_view_registers_the_placeholder():
    """A camera with no session at all must still create the stream definition -
    go2rtc will not create a source-less stream, and rejects an RTSP publish to
    an unknown name, so the placeholder has to exist before the publish."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)

    _url, client = await _stream_source(cam, None)

    client.ensure_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreachable_go2rtc_still_registers_and_keeps_a_url():
    """"unknown" is not "live": an unreachable go2rtc must not be mistaken for a
    healthy publisher and skip the PUT. It keeps the warm session (no restart)
    but still attempts the registration. The failed registration must NOT become
    a None stream_source - that would tear down every other camera's session -
    so the push URL comes back regardless."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://127.0.0.1:8554/live")
    streams_empty: dict = {}
    client = MagicMock()
    client.list_streams = AsyncMock(return_value=streams_empty)
    client.ensure_stream = AsyncMock(return_value=False)  # go2rtc down
    client.rtsp_url = MagicMock(return_value="rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb")
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_WAIT_S", 0.05),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_POLL_S", 0.01),
        patch.object(
            AidotCamera,
            "_push_serve_url",
            new_callable=PropertyMock,
            return_value="rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb",
        ),
    ):
        url = await cam.stream_source()

    assert url == "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"
    client.ensure_stream.assert_awaited_once()
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


@pytest.mark.asyncio
async def test_pull_mode_still_re_registers_every_view():
    """DTLS/pull is untouched: there the placeholder IS the live source, so
    replacing it is harmless and the re-PUT keeps its self-healing role."""
    cam = _make_camera(is_sdes=False, stream_rtsp_url="rtsp://127.0.0.1:8554/live")

    _url, client = await _stream_source(cam, [PLACEHOLDER, PUBLISHER])

    client.ensure_stream.assert_awaited_once()


# --------------------------------------------------------------------------- #
# the tri-state itself
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producers", "expected"),
    [
        ([PLACEHOLDER, PUBLISHER], "live"),
        ([PLACEHOLDER], "absent"),
        ([], "absent"),
        (None, "unknown"),  # go2rtc unreachable
    ],
)
async def test_publisher_state_tri_state(producers, expected):
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://x")
    streams = (
        {} if producers is None else {"aidot_aaaa0000bbbb": {"producers": producers}}
    )
    client = MagicMock()
    client.list_streams = AsyncMock(return_value=streams)
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
    ):
        assert await cam._go2rtc_publisher_state(push=True) == expected


@pytest.mark.asyncio
async def test_publisher_state_missing_stream_is_absent():
    """A stream definition go2rtc has never heard of must be re-registered."""
    cam = _make_camera(is_sdes=True, stream_rtsp_url="rtsp://x")
    client = MagicMock()
    client.list_streams = AsyncMock(return_value={"someone_else": {"producers": []}})
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
    ):
        assert await cam._go2rtc_publisher_state(push=True) == "absent"


@pytest.mark.asyncio
async def test_publisher_state_pull_mode_is_always_live():
    """Pull mode never consults go2rtc - the old fast-path guard is unchanged."""
    cam = _make_camera(is_sdes=False, stream_rtsp_url="rtsp://x")
    assert await cam._go2rtc_publisher_state(push=False) == "live"
