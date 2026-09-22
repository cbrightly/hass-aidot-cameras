"""A rejected go2rtc register call does not mean go2rtc cannot serve the stream.

``_publish_to_go2rtc()`` treated any falsy ``ensure_stream()`` as "registration
failed" and returned None, which sends the camera down ``stream_source()``'s
fallback to the local serve - Home Assistant's HLS pipeline - instead of
go2rtc's low-latency path.

go2rtc can reject the PUT while still holding the stream. Observed on the box
2026-09-22: a duplicate stream key in go2rtc's own ``go2rtc.yaml`` made it
answer EVERY ``PUT /api/streams`` with ``400 yaml: unmarshal errors: mapping
key "<name>" already defined``, so the streams stayed registered while every
registration call reported failure - taking all cameras off the low-latency
path at once, not just the one that owned the key. Nothing errored, so the only
symptom was a camera that appeared to play slower than real time.

``has_stream()`` settles it, so the fallback is reserved for a stream that is
genuinely absent.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.aidot import camera as cam_mod


def _cam(dev_id="dev-1"):
    c = MagicMock()
    c.coordinator.device_client.device_id = dev_id
    c._go2rtc_name = "aidot_dev1"
    c._rtsp_name = "aidot_dev1"
    c.hass = MagicMock()
    c._push_mode = AsyncMock(return_value=False)
    return c


def _client(*, ensure, present):
    client = MagicMock()
    client.ensure_stream = AsyncMock(return_value=ensure)
    client.has_stream = AsyncMock(return_value=present)
    client.remove_stream = AsyncMock(return_value=True)
    client.rtsp_url = MagicMock(return_value="rtsp://go2rtc/aidot_dev1")
    return client


async def _publish(client):
    with (
        patch.object(cam_mod, "_GO2RTC_ENABLED", True),
        patch.object(cam_mod, "Go2rtcClient", return_value=client),
        patch.object(cam_mod, "async_get_clientsession", MagicMock()),
        patch.object(cam_mod, "_sprop_is_unstable", return_value=False),
    ):
        return await cam_mod.AidotCamera._publish_to_go2rtc(
            _cam(), "http://127.0.0.1:1/x.ts"
        )


async def test_rejected_put_still_uses_go2rtc_when_the_stream_is_registered():
    """The defect: this returned None and dropped the camera to HLS."""
    client = _client(ensure=False, present=True)

    assert await _publish(client) == "rtsp://go2rtc/aidot_dev1"
    client.has_stream.assert_awaited_once()


async def test_rejected_put_falls_back_when_the_stream_is_absent():
    """The preserved path: a real registration failure still falls back."""
    client = _client(ensure=False, present=False)

    assert await _publish(client) is None
