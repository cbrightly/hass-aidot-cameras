"""Only the HLS path is under Home Assistant's 10s clock - the others may wait.

``Camera.async_create_stream`` (HLS) wraps ``stream_source()`` in
``CAMERA_STREAM_SOURCE_TIMEOUT`` (10s). Nothing else does: HA's go2rtc WebRTC
provider reaches it through ``_update_stream_source`` with no timeout, and a
third-party card reaches it through ``async_get_stream_source`` with none
either (both verified against HA 2026.8.3). Neither of those consumers retries
a URL that 404s, so handing them the go2rtc URL before the publisher has
attached costs the view - measured on the box 2026-09-03: cold battery opens
attached their publisher at +5.5s, +10.4s and +11.1s, so the 8.5s budget sits
in the middle of that spread. On the slow half it handed the URL over first and
the WebRTC card got ``dial tcp 127.0.0.1:18812: connect: connection refused``
and stopped, while the vendor app - which has no such cutoff - pulls the same
camera up almost instantly.

So the budget depends on who is asking. The HLS path keeps the short one it
needs to fit HA's clock; every other caller gets one sized for a genuinely
cold open, and pays nothing on a warm camera because the wait ends the moment
the publisher (or the local serve) is ready.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot import camera as cam_mod
from custom_components.aidot.camera import AidotCamera
from custom_components.aidot.const import CONF_SDES_PUSH

PLACEHOLDER = {"url": "http://127.0.0.1:18981/aaaa0000bbbb.ts"}
PUBLISHER = {"remote_addr": "127.0.0.1:59878"}
RTSP = "rtsp://127.0.0.1:8554/aidot_aaaa0000bbbb"


def _make_camera(*, is_sdes: bool = True):
    device_client = MagicMock()
    device_client.device_id = "aaaa0000bbbb1111cccc2222dddd3333"
    device_client.is_sdes_camera = is_sdes
    device_client.is_battery_camera = False
    device_client.stream_rtsp_url = None
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
    seq = list(stream_sequence)
    client = MagicMock()

    async def _list():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    client.list_streams = AsyncMock(side_effect=_list)
    client.ensure_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value=RTSP)
    return client


def _streams(producers):
    return {"aidot_aaaa0000bbbb": {"producers": producers}}


def _patches(client, *, hls_wait, view_wait, poll=0.01):
    return (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_WAIT_S", hls_wait),
        patch("custom_components.aidot.camera._VIEW_PUBLISHER_WAIT_S", view_wait),
        patch("custom_components.aidot.camera._PUSH_PUBLISHER_POLL_S", poll),
        patch.object(
            AidotCamera, "_push_serve_url", new_callable=PropertyMock, return_value=RTSP
        ),
    )


@pytest.mark.asyncio
async def test_a_webrtc_view_waits_past_the_hls_budget_for_the_publisher():
    """The fix: a cold WebRTC/card view keeps waiting after the HLS budget is
    spent, and returns the moment the publisher attaches."""
    cam = _make_camera()
    # 40 polls of "not yet" is far more than the (tiny) HLS budget allows.
    client = _client([_streams([PLACEHOLDER])] * 41 + [_streams([PLACEHOLDER, PUBLISHER])])
    p = _patches(client, hls_wait=0.05, view_wait=10.0)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        url = await cam.stream_source()

    assert url == RTSP
    assert client.list_streams.await_count >= 42
    # Confirmed by go2rtc, so the readiness fallback is not also spent.
    cam.coordinator.device_client.async_wait_serve_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_hls_path_keeps_the_short_budget():
    """Under async_create_stream the wait must still fit HA's 10s clock, however
    long the view budget is - a battery camera that cannot serve HLS at all is
    the failure that budget exists to prevent."""
    cam = _make_camera()
    client = _client([_streams([PLACEHOLDER])])  # never attaches
    p = _patches(client, hls_wait=0.6, view_wait=60.0)
    token = cam_mod._STREAM_SOURCE_HLS.set(True)
    try:
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            url = await cam.stream_source()
    finally:
        cam_mod._STREAM_SOURCE_HLS.reset(token)

    assert url == RTSP
    # 0.6s at a 0.01s poll is ~60 polls; a 60s budget would be thousands.
    assert client.list_streams.await_count < 200
    # ...and the fallback's share of the budget is bounded by the HLS one.
    waiter = cam.coordinator.device_client.async_wait_serve_ready
    if waiter.await_count:
        assert waiter.await_args.kwargs["timeout"] <= 0.6


@pytest.mark.asyncio
async def test_async_create_stream_marks_its_call_as_the_hls_path():
    """The flag is set for exactly the duration of HA's own create_stream."""
    cam = _make_camera()
    seen = []

    async def _base(self):
        seen.append(cam_mod._STREAM_SOURCE_HLS.get())

    with patch.object(cam_mod.Camera, "async_create_stream", _base):
        assert cam_mod._STREAM_SOURCE_HLS.get() is False
        await cam.async_create_stream()

    assert seen == [True]
    assert cam_mod._STREAM_SOURCE_HLS.get() is False


@pytest.mark.asyncio
async def test_a_warm_camera_pays_nothing_on_either_path():
    cam = _make_camera()
    cam.coordinator.device_client.stream_rtsp_url = RTSP
    client = _client([_streams([PLACEHOLDER, PUBLISHER])])
    p = _patches(client, hls_wait=0.05, view_wait=60.0)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        url = await cam.stream_source()

    assert url == RTSP
    assert client.list_streams.await_count == 1
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_dtls_serve_wait_is_sized_by_the_caller_too():
    """Pull mode has the same race - go2rtc dials the local serve the instant the
    URL is handed over - so its readiness wait gets the same caller-sized budget."""
    cam = _make_camera(is_sdes=False)
    client = _client([_streams([PLACEHOLDER])])
    with (
        patch("custom_components.aidot.camera.Go2rtcClient", return_value=client),
        patch("custom_components.aidot.camera.async_get_clientsession"),
        patch("custom_components.aidot.camera._VIEW_SERVE_READY_WAIT_S", 23.0),
        patch.object(AidotCamera, "_serve_url", new_callable=PropertyMock, return_value="http://127.0.0.1:18931/x.ts"),
    ):
        await cam.stream_source()
        view_timeout = cam.coordinator.device_client.async_wait_serve_ready.await_args.kwargs["timeout"]

        cam.coordinator.device_client.async_wait_serve_ready.reset_mock()
        token = cam_mod._STREAM_SOURCE_HLS.set(True)
        try:
            await cam.stream_source()
        finally:
            cam_mod._STREAM_SOURCE_HLS.reset(token)
        hls_timeout = cam.coordinator.device_client.async_wait_serve_ready.await_args.kwargs["timeout"]

    assert view_timeout == 23.0
    assert hls_timeout < 10.0


def test_the_view_budget_covers_a_cold_open_and_the_hls_one_still_fits():
    # Cold opens attached their publisher at +5.5s, +10.4s and +11.1s on the
    # fleet, so the mains view budget has to clear that with room. The battery
    # budget is a separate number and is checked below, because what it has to
    # cover is the camera's wake rather than the handshake. The HLS budget is
    # unchanged by this file's existence.
    assert cam_mod._VIEW_PUBLISHER_WAIT_S >= 15.0
    assert cam_mod._VIEW_SERVE_READY_WAIT_S >= 20.0
    assert cam_mod._PUSH_PUBLISHER_WAIT_S < 10.0


def test_the_publisher_poll_is_tight_enough_not_to_be_the_latency():
    """The wait ends on a poll, so the interval is dead time added to every cold
    open after the publisher has already attached. A warm camera reaches the
    card in about a second; a poll interval near that is the wrong order."""
    assert cam_mod._PUSH_PUBLISHER_POLL_S <= 0.3


# --------------------------------------------------------------------------- #
# A battery camera's budget is sized by its wake, not by ours
# --------------------------------------------------------------------------- #

def test_a_battery_camera_gets_a_budget_that_clears_its_slow_wake():
    """The residual failure this closes: on the slowest measured cold open the
    camera announced itself at +27.9s, the first attempt was abandoned to the
    retry, and media arrived at +46.7s - which a 45s budget missed by 1.7s. The
    click failed with the camera seconds from streaming."""
    assert cam_mod._view_publisher_budget(battery=True) >= 60.0


def test_a_mains_camera_keeps_a_short_one():
    """Mains cold opens attach their publisher by ~11s, so making them wait a
    battery camera's budget would only slow down failing fast on a dead one."""
    assert cam_mod._view_publisher_budget(battery=False) <= 30.0
    assert cam_mod._view_publisher_budget(battery=False) >= 15.0


@pytest.mark.asyncio
async def test_the_budget_follows_the_camera_and_the_caller_together():
    """Both dimensions at once: HLS always keeps the short budget, whatever the
    camera; a view gets the budget its camera needs."""
    cam = _make_camera()
    cam.coordinator.device_client.is_battery_camera = True
    assert cam._publisher_wait_budget() == cam_mod._VIEW_PUBLISHER_WAIT_BATTERY_S

    cam.coordinator.device_client.is_battery_camera = False
    assert cam._publisher_wait_budget() == cam_mod._VIEW_PUBLISHER_WAIT_S

    token = cam_mod._STREAM_SOURCE_HLS.set(True)
    try:
        cam.coordinator.device_client.is_battery_camera = True
        assert cam._publisher_wait_budget() == cam_mod._PUSH_PUBLISHER_WAIT_S
    finally:
        cam_mod._STREAM_SOURCE_HLS.reset(token)


def test_the_battery_budget_still_fits_inside_the_librarys_own_ceiling():
    """The library abandons a stalled attempt and retries; the budget has to
    cover that whole recovery, but there is no point waiting past the point
    where the library itself has given up on the session."""
    assert cam_mod._view_publisher_budget(battery=True) <= 120.0
