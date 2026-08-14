"""stream_source() must not open a camera the cloud already reports offline.

The library serialises every WebRTC handshake through a global open-gate
(``_WEBRTC_OPEN_GATE``, default 2 concurrent opens). An open against a camera
that is not there does not fail fast: it holds that gate for the full no-answer
timeout - 45s in the fleet harness - and every other camera's cold open queues
behind it. Fleet evidence 2026-08-08: one A000088 whose cloud record has read
``online: False`` since 2026-06-27 still produced "no webrtcResp received within
45.0s" on 8 of 8 attempts across four runs, 90s of gate time per run spent on a
camera the cloud had already declared dead six weeks earlier.

The integration already refuses that open on three of its four doors -
``available``, ``_prewarm_stream`` and ``_startup_prewarm`` - but not on
``stream_source()``, which is the live-view / go2rtc-lazy-pull door. go2rtc pulls
on a viewer connection and never consults HA's ``available``, so the guarded
doors do not cover it.

Two properties this file pins, beyond "it does not open":

* **It still returns a URL, never None.** HA's go2rtc provider calls
  ``teardown()`` on a falsy stream_source, closing every session it holds - so
  refusing this camera with None would blank every other camera's live view. See
  ``_soft_fail_url``.
* **It fails open, twice over.** The check reads the cloud flag with a default of
  True (a device client with no status, or a status with no ``online``, still
  opens), exactly as the three existing guards do; and it sits on the
  session-start branch only, so a camera that is already streaming keeps being
  served whatever the cloud flag says. That is the answer to the reverse-stale
  case - a flag that reads offline while the camera is really reachable.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.camera import AidotCamera
from custom_components.aidot.const import CONF_SDES_PUSH

GO2RTC_URL = "rtsp://127.0.0.1:8554/aidot_dc3bf570cafe"


def _make_camera(*, status=SimpleNamespace(online=True), stream_rtsp_url=None):
    """A DTLS/pull camera past setup - the model and the door in the evidence.

    ``_setup_complete = True`` is load-bearing: the setup-time branch of
    stream_source() returns before it ever reaches start_keepalive, so a camera
    built with False would make every assertion below pass with the production
    change deleted.
    """
    device_client = MagicMock()
    device_client.device_id = "dc3bf570cafe1111beef2222feed3333"
    device_client.is_sdes_camera = False
    device_client.is_battery_camera = False
    device_client.stream_rtsp_url = stream_rtsp_url
    device_client.status = status
    device_client.start_keepalive = AsyncMock()
    device_client.async_wait_serve_ready = AsyncMock()

    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = device_client
    cam.coordinator.config_entry = SimpleNamespace(
        entry_id="e1", options={CONF_SDES_PUSH: False}
    )
    cam.coordinator.sdes_audio_override = None
    cam._rtsp_name = device_client.device_id
    cam._setup_complete = True
    cam.hass = MagicMock()
    cam._set_stream_status = MagicMock()
    cam._stream_status = None
    cam._publish_to_go2rtc = AsyncMock(return_value=GO2RTC_URL)
    cam._go2rtc_publisher_state = AsyncMock(return_value="live")
    return cam


@pytest.mark.asyncio
async def test_offline_camera_is_not_opened():
    """The defect: the cloud says offline and we open it anyway, for 45s, under
    the global gate."""
    cam = _make_camera(status=SimpleNamespace(online=False))

    await cam.stream_source()

    cam.coordinator.device_client.start_keepalive.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_camera_still_gets_a_url_not_none():
    """Refusing the open must stay local to this camera. A falsy stream_source
    makes HA's go2rtc provider tear down every camera's session."""
    cam = _make_camera(status=SimpleNamespace(online=False))

    url = await cam.stream_source()

    assert url is not None
    assert url.split(":", 1)[0] in ("rtsp", "http")


@pytest.mark.asyncio
async def test_online_camera_still_opens():
    """The control. Without it the guard could refuse everything and pass."""
    cam = _make_camera(status=SimpleNamespace(online=True))

    await cam.stream_source()

    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, SimpleNamespace()], ids=["no-status", "no-online-attr"]
)
async def test_unknown_online_state_still_opens(status):
    """Fail open on absence of information, like the three existing guards: the
    flag has to positively say False before we refuse."""
    cam = _make_camera(status=status)

    await cam.stream_source()

    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_live_session_is_served_even_if_the_flag_says_offline():
    """The reverse-stale case. A camera with a live go2rtc publisher is proof the
    flag is wrong, and the guard must not take that stream away: it sits on the
    session-start branch, which a warm camera never enters.

    Asserted on side effects, not the URL - the refusal path and the warm path
    return the same string, so a URL assertion here would prove nothing.
    """
    cam = _make_camera(
        status=SimpleNamespace(online=False), stream_rtsp_url="rtsp://127.0.0.1/live"
    )

    await cam.stream_source()

    # Served, not refused: it went on to register with go2rtc and cleared the
    # status overlay (connected), rather than bailing out at the guard.
    cam._publish_to_go2rtc.assert_awaited_once()
    cam._set_stream_status.assert_any_call(None)
