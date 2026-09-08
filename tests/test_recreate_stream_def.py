"""A camera that changes its parameter sets gets a FRESH go2rtc stream definition.

go2rtc builds its decoder configuration (the fMP4 `avcC` box) when a track is
first published and reuses it for the life of the stream definition. A PUT over
an existing definition does not rebuild it. So for a camera whose SPS differs
between sessions, the definition has to be dropped first, or a Media Source
Extensions player -- which configures from that box once -- cannot decode
inter-frames and the picture updates only on keyframes.

The ordering is the safety property: the drop happens at stream_source() time,
before the keepalive publishes. Removing a definition a publisher is already
attached to leaves it with nowhere to publish.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aidot import camera as cam_mod


def _cam(dev_id="dev-1"):
    c = MagicMock()
    c.coordinator.device_client.device_id = dev_id
    c._go2rtc_name = "aidot_dev1"
    c._rtsp_name = "aidot_dev1"
    c.hass = MagicMock()
    return c


@pytest.mark.parametrize("unstable,expect_remove", [(True, True), (False, False)])
async def test_removes_definition_only_for_flagged_cameras(unstable, expect_remove):
    client = MagicMock()
    client.ensure_stream = AsyncMock(return_value=True)
    client.remove_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value="rtsp://go2rtc/aidot_dev1")
    with patch.object(cam_mod, "_GO2RTC_ENABLED", True), \
         patch.object(cam_mod, "Go2rtcClient", return_value=client), \
         patch.object(cam_mod, "async_get_clientsession", MagicMock()), \
         patch.object(cam_mod, "_sprop_is_unstable", return_value=unstable):
        out = await cam_mod.AidotCamera._publish_to_go2rtc(_cam(), "http://127.0.0.1:1/x.ts")
    assert out == "rtsp://go2rtc/aidot_dev1"
    assert client.remove_stream.await_count == (1 if expect_remove else 0)
    client.ensure_stream.assert_awaited_once()


async def test_remove_happens_before_the_put():
    """Order is the whole point: recreate, then register, then (later) publish."""
    calls = []
    client = MagicMock()
    client.ensure_stream = AsyncMock(side_effect=lambda *a, **k: calls.append("put") or True)
    client.remove_stream = AsyncMock(side_effect=lambda *a, **k: calls.append("delete") or True)
    client.rtsp_url = MagicMock(return_value="rtsp://go2rtc/aidot_dev1")
    with patch.object(cam_mod, "_GO2RTC_ENABLED", True), \
         patch.object(cam_mod, "Go2rtcClient", return_value=client), \
         patch.object(cam_mod, "async_get_clientsession", MagicMock()), \
         patch.object(cam_mod, "_sprop_is_unstable", return_value=True):
        await cam_mod.AidotCamera._publish_to_go2rtc(_cam(), "http://127.0.0.1:1/x.ts")
    assert calls == ["delete", "put"]


async def test_a_failed_delete_does_not_block_registration():
    """Best-effort: if go2rtc will not drop it, still register - a stale decoder
    config is better than no stream at all."""
    client = MagicMock()
    client.ensure_stream = AsyncMock(return_value=True)
    client.remove_stream = AsyncMock(return_value=False)
    client.rtsp_url = MagicMock(return_value="rtsp://go2rtc/aidot_dev1")
    with patch.object(cam_mod, "_GO2RTC_ENABLED", True), \
         patch.object(cam_mod, "Go2rtcClient", return_value=client), \
         patch.object(cam_mod, "async_get_clientsession", MagicMock()), \
         patch.object(cam_mod, "_sprop_is_unstable", return_value=True):
        out = await cam_mod.AidotCamera._publish_to_go2rtc(_cam(), "http://127.0.0.1:1/x.ts")
    assert out == "rtsp://go2rtc/aidot_dev1"


def test_falls_back_to_never_recreating_on_an_older_library():
    """The import guard must default to False, so an older library keeps exactly
    today's behaviour rather than recreating every definition."""
    import inspect
    src = inspect.getsource(cam_mod)
    assert "except ImportError" in src and "return False" in src
