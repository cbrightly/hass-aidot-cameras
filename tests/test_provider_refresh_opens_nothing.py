"""A WebRTC provider refresh asks for the stream URL; it must not open the camera.

Home Assistant re-runs ``Camera.async_refresh_providers`` for every camera when
a WebRTC provider registers (the go2rtc config entry loading late, or being
reloaded). That refresh picks the provider by calling ``stream_source()``
(camera/webrtc.py ``async_get_supported_provider``). Once setup had completed,
our ``stream_source()`` treated that call as a real view and started a
keepalive on every camera - battery cameras included - with nobody watching.

During a refresh the camera now answers from the URL alone, exactly as it does
during entity setup, and that URL is the one a later real view hands out, so
HA still picks the go2rtc provider.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.camera import Camera
from homeassistant.components.camera.webrtc import DATA_WEBRTC_PROVIDERS

from custom_components.aidot import camera as camera_mod

from .test_camera import _make_camera


class _FakeProvider:
    """Stands in for HA's go2rtc provider: records the URL it was offered."""

    def __init__(self, *, raises: bool = False) -> None:
        self.seen: list[str] = []
        self.raises = raises
        self.async_register_camera = AsyncMock()
        self.async_unregister_camera = AsyncMock()

    def async_is_supported(self, stream_source: str) -> bool:
        self.seen.append(stream_source)
        if self.raises:
            raise RuntimeError("provider check failed")
        return stream_source.partition(":")[0] in ("rtsp", "rtsps")


def _ready_camera(*, push: bool, **kwargs):
    cam = _make_camera(stream_rtsp_url=None, **kwargs)
    cam._setup_complete = True
    cam._push_mode = AsyncMock(return_value=push)
    cam._publish_to_go2rtc = AsyncMock(return_value="rtsp://go2rtc/x")
    cam._await_serve_listening = AsyncMock(return_value=True)
    return cam


def _with_provider(cam, provider):
    cam.hass.data = {DATA_WEBRTC_PROVIDERS: {provider}}
    return cam


@pytest.mark.parametrize(
    ("kwargs", "push"),
    [
        ({"is_battery": True}, False),
        ({"is_battery": True}, True),
        ({"is_battery": False}, False),
        ({"is_sdes": True}, True),
    ],
    ids=["battery-pull", "battery-push", "mains-pull", "sdes-push"],
)
async def test_refresh_after_setup_opens_nothing_and_picks_go2rtc(kwargs, push):
    provider = _FakeProvider()
    cam = _with_provider(_ready_camera(push=push, **kwargs), provider)
    dc = cam.coordinator.device_client

    await cam.async_refresh_providers(write_state=False)

    dc.start_keepalive.assert_not_called()
    assert cam._stream_status is None  # no "Connecting..." overlay either
    assert cam._webrtc_provider is provider
    assert len(provider.seen) == 1
    refresh_url = provider.seen[0]

    # The answer is the URL a real view then hands out, so the provider HA
    # chose is the one that view needs.
    view_url = await cam.stream_source()
    dc.start_keepalive.assert_awaited_once()
    assert view_url == refresh_url
    if push:
        assert refresh_url == await cam._push_serve_url()


async def test_refresh_flag_does_not_outlive_the_refresh():
    cam = _with_provider(_ready_camera(push=False), _FakeProvider())
    await cam.async_refresh_providers(write_state=False)
    assert camera_mod._PROVIDER_REFRESH.get() is False
    await cam.stream_source()
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


async def test_refresh_flag_is_cleared_when_the_refresh_raises():
    provider = _FakeProvider(raises=True)
    cam = _with_provider(_ready_camera(push=False), provider)
    with pytest.raises(RuntimeError, match="provider check failed"):
        await cam.async_refresh_providers(write_state=False)
    assert provider.seen  # stream_source ran inside the refresh
    cam.coordinator.device_client.start_keepalive.assert_not_called()
    assert camera_mod._PROVIDER_REFRESH.get() is False
    await cam.stream_source()
    cam.coordinator.device_client.start_keepalive.assert_awaited_once()


async def test_pull_refresh_url_matches_a_real_go2rtc_registration():
    # The real _publish_to_go2rtc against a stubbed go2rtc client, so the
    # refresh answer is the RTSP URL go2rtc actually hands back - the scheme
    # HA's go2rtc provider accepts - and the one the view then uses.
    client = MagicMock()
    client.ensure_stream = AsyncMock(return_value=True)
    client.has_stream = AsyncMock(return_value=True)
    client.remove_stream = AsyncMock(return_value=True)
    provider = _FakeProvider()
    cam = _make_camera(is_battery=True, stream_rtsp_url=None)
    cam._setup_complete = True
    cam._push_mode = AsyncMock(return_value=False)
    _with_provider(cam, provider)
    with (
        patch.object(camera_mod, "_GO2RTC_ENABLED", True),
        patch(
            "custom_components.aidot.go2rtc_target.Go2rtcClient", return_value=client
        ),
        patch.object(camera_mod, "async_get_clientsession", MagicMock()),
        patch.object(camera_mod, "_sprop_is_unstable", return_value=False),
    ):
        await cam.async_refresh_providers(write_state=False)
        dc = cam.coordinator.device_client
        dc.start_keepalive.assert_not_called()
        assert cam._webrtc_provider is provider
        refresh_url = provider.seen[0]
        assert refresh_url.startswith("rtsp://")
        assert refresh_url.endswith("/" + cam._go2rtc_name)
        assert await cam.stream_source() == refresh_url
        dc.start_keepalive.assert_awaited_once()


async def test_state_is_written_after_the_refresh_flag_is_cleared():
    # Default write_state=True. A state listener runs in the writer's context,
    # so a stream_source() it makes (camera.record from an automation) must see
    # the flag already cleared and open the camera.
    provider = _FakeProvider()
    cam = _with_provider(_ready_camera(push=False), provider)
    seen_flag: list[bool] = []

    def _write():
        seen_flag.append(camera_mod._PROVIDER_REFRESH.get())

    cam.async_write_ha_state = _write
    await cam.async_refresh_providers()
    assert seen_flag == [False]
    cam.coordinator.device_client.start_keepalive.assert_not_called()


async def test_state_is_not_written_when_the_provider_is_unchanged():
    provider = _FakeProvider()
    cam = _with_provider(_ready_camera(push=False), provider)
    cam._webrtc_provider = provider
    cam.async_write_ha_state = MagicMock()
    await cam.async_refresh_providers()
    cam.async_write_ha_state.assert_not_called()


async def test_refresh_passes_unknown_keywords_through():
    cam = _ready_camera(push=False)
    base = AsyncMock()
    with patch.object(Camera, "async_refresh_providers", base):
        await cam.async_refresh_providers(write_state=True, future_option=1)
    base.assert_awaited_once_with(write_state=False, future_option=1)
