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

from unittest.mock import MagicMock, patch

import pytest
from aidot_cameras.camera.go2rtc import Go2rtcClient

from custom_components.aidot import camera as cam_mod


class _Client(Go2rtcClient):
    """A real client with its HTTP writes replaced by a record of them."""

    def __init__(self, *, delete_ok=True) -> None:
        super().__init__(MagicMock())
        self.calls: list[str] = []
        self._delete_ok = delete_ok

    async def _put(self, name, sources):
        self.calls.append("put")
        return True

    async def _delete(self, name):
        self.calls.append("delete")
        return self._delete_ok


def _cam(dev_id="dev-1"):
    c = MagicMock()
    c.coordinator.device_client.device_id = dev_id
    c._go2rtc_name = "aidot_dev1"
    c._rtsp_name = "aidot_dev1"
    c.hass = MagicMock()
    # The real target lookup, so the client is the one the resolved go2rtc
    # builds (conftest: the add-on).
    c._go2rtc_target = lambda: cam_mod.AidotCamera._go2rtc_target(c)
    return c


async def _publish(client, *, unstable):
    with (
        patch.object(cam_mod, "_GO2RTC_ENABLED", True),
        patch(
            "custom_components.aidot.go2rtc_target.Go2rtcClient", return_value=client
        ),
        patch.object(cam_mod, "async_get_clientsession", MagicMock()),
        patch.object(cam_mod, "_sprop_is_unstable", return_value=unstable),
    ):
        return await cam_mod.AidotCamera._publish_to_go2rtc(
            _cam(), "http://127.0.0.1:1/x.ts"
        )


@pytest.mark.parametrize("unstable,expect_remove", [(True, True), (False, False)])
async def test_removes_definition_only_for_flagged_cameras(unstable, expect_remove):
    client = _Client()
    out = await _publish(client, unstable=unstable)
    assert out == "rtsp://127.0.0.1:8554/aidot_dev1"
    assert client.calls.count("delete") == (1 if expect_remove else 0)
    assert client.calls.count("put") == 1


async def test_remove_happens_before_the_put():
    """Order is the whole point: recreate, then register, then (later) publish."""
    client = _Client()
    await _publish(client, unstable=True)
    assert client.calls == ["delete", "put"]


async def test_a_failed_delete_does_not_block_registration():
    """Best-effort: if go2rtc will not drop it, still register - a stale decoder
    config is better than no stream at all."""
    client = _Client(delete_ok=False)
    out = await _publish(client, unstable=True)
    assert out == "rtsp://127.0.0.1:8554/aidot_dev1"
    assert client.calls == ["delete", "put"]
