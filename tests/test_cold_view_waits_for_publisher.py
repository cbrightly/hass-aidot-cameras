"""A cold push-mode view waits for the publisher instead of failing.

Handing Home Assistant the go2rtc URL before the RTSP publisher has attached
cannot work: go2rtc answers a consumer on a stream whose only producer is the
inert placeholder with 404, and HA's go2rtc provider turns that into a
``WebRTCError`` with no retry. That is the "press play twice" symptom - the
first view fails, the session finishes opening in the background ~20s later, and
the second view works.

The old code waited 7s, sized to stay inside HA's 10s
``CAMERA_STREAM_SOURCE_TIMEOUT``. That timeout does not apply here: it wraps only
``Camera.async_create_stream`` (the HLS path). The WebRTC path reaches
``stream_source()`` through the go2rtc provider's ``_update_stream_source``,
which imposes no timeout at all (verified in HA 2026.7.4, the box's version), and
snapshots do not reach ``stream_source()`` at all. So a cold view can wait for
the real thing.

Waiting for "publisher attached" specifically - rather than for media - is what
the transport actually needs: verified against go2rtc 1.9.9, a consumer
attaching 0.5s after the publisher started (before any media had accumulated)
was served h264 1280x720. go2rtc serves from the ANNOUNCE onward.
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


def _make_camera(*, is_sdes: bool = True, stream_rtsp_url: str | None = None):
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
    cam._stream_status = None
    return cam


def _client(stream_sequence):
    """A go2rtc client whose /api/streams answer changes per call."""
    seq = list(stream_sequence)
    client = MagicMock()

    async def _list():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    client.list_streams = AsyncMock(side_effect=_list)
    client.ensure_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value="rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb")
    return client


def _streams(producers):
    return {"aidot_aaaa0000bbbb": {"producers": producers}}


def _patches(client, wait=5.0, poll=0.01):
    return (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_WAIT_S", wait),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_POLL_S", poll),
        patch.object(
            AidotCamera,
            "_push_serve_url",
            new_callable=PropertyMock,
            return_value="rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb",
        ),
    )


@pytest.mark.asyncio
async def test_cold_view_waits_until_the_publisher_attaches():
    """The fix: a cold view must not return until go2rtc has the publisher."""
    cam = _make_camera(stream_rtsp_url=None)
    # No publisher for the first few polls, then it attaches.
    client = _client(
        [
            _streams([PLACEHOLDER]),  # the initial state check
            _streams([PLACEHOLDER]),
            _streams([PLACEHOLDER]),
            _streams([PLACEHOLDER, PUBLISHER]),
        ]
    )
    p = _patches(client)
    with p[0], p[1], p[2], p[3], p[4]:
        url = await cam.stream_source()

    assert url == "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"
    # It polled past the not-yet-attached answers rather than returning on the
    # first one - that is the difference between a spinner and a failed view.
    assert client.list_streams.await_count >= 4
    # ...and having confirmed the publisher, it does not also burn the
    # serve-ready fallback wait.
    cam.coordinator.device_client.async_wait_serve_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_view_does_not_wait_at_all():
    """A warm camera pays nothing: publisher already live -> single check."""
    cam = _make_camera(stream_rtsp_url="rtsp://127.0.0.1:8554/live")
    client = _client([_streams([PLACEHOLDER, PUBLISHER])])
    p = _patches(client)
    with p[0], p[1], p[2], p[3], p[4]:
        url = await cam.stream_source()

    assert url == "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"
    assert client.list_streams.await_count == 1
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


@pytest.mark.asyncio
async def test_publisher_that_never_attaches_still_returns_a_url():
    """On timeout, hand HA the URL anyway. Returning None is worse than a URL
    that may 404: HA's go2rtc provider calls teardown() on a falsy stream_source,
    which closes EVERY camera's WebRTC session, not just this one."""
    cam = _make_camera(stream_rtsp_url=None)
    client = _client([_streams([PLACEHOLDER])])
    p = _patches(client, wait=0.05)
    with p[0], p[1], p[2], p[3], p[4]:
        url = await cam.stream_source()

    assert url == "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"
    # The library's readiness signal is the fallback once go2rtc never confirmed.
    cam.coordinator.device_client.async_wait_serve_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreachable_go2rtc_does_not_burn_the_wait():
    """An unreadable go2rtc cannot be waited out - give up immediately rather
    than blocking the caller for the full budget."""
    cam = _make_camera(stream_rtsp_url=None)
    client = _client([{}])  # go2rtc unreachable
    client.ensure_stream = AsyncMock(return_value=True)
    p = _patches(client, wait=60.0, poll=30.0)  # would hang if it polled
    with p[0], p[1], p[2], p[3], p[4]:
        url = await cam.stream_source()

    assert url == "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"


@pytest.mark.asyncio
async def test_await_publisher_attached_returns_true_on_attach():
    cam = _make_camera()
    client = _client([_streams([PLACEHOLDER]), _streams([PLACEHOLDER, PUBLISHER])])
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_POLL_S", 0.01),
    ):
        assert await cam._await_publisher_attached(timeout=5.0) is True


@pytest.mark.asyncio
async def test_await_publisher_attached_times_out():
    cam = _make_camera()
    client = _client([_streams([PLACEHOLDER])])
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_POLL_S", 0.01),
    ):
        assert await cam._await_publisher_attached(timeout=0.05) is False


@pytest.mark.asyncio
async def test_pull_mode_keeps_the_serve_port_probe():
    """DTLS/pull is untouched: it still waits on the local -listen socket, not on
    a go2rtc publisher (there is none in pull mode)."""
    cam = _make_camera(stream_rtsp_url=None)
    cam.coordinator.config_entry = SimpleNamespace(
        entry_id="e1", options={CONF_SDES_PUSH: False}
    )
    cam._await_serve_listening = AsyncMock(return_value=True)
    client = _client([_streams([PLACEHOLDER])])
    p = _patches(client)
    with p[0], p[1], p[2], p[3], p[4]:
        await cam.stream_source()

    cam._await_serve_listening.assert_awaited_once()
