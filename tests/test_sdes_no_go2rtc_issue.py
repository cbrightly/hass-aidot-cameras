"""On the default Home Assistant OS/Container topology - only the bundled
go2rtc, no add-on, no AIDOT_GO2RTC_API - every SDES camera (the battery and
PTZ models) has no live view. Before this, the only signal was a once-per-
camera WARNING pointing at AIDOT_GO2RTC_API, which an HA OS install cannot
set. A Repairs issue now explains it: raised once per process while Home
Assistant is running and an SDES camera has no go2rtc target at all, cleared
as soon as any target resolves or on the last entry's unload.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import CoreState

from custom_components.aidot import camera as camera_mod
from custom_components.aidot import go2rtc_target as gt

from .test_camera import _make_camera

ADDON = gt.Go2rtcTarget(
    "http://127.0.0.1:1984", object(), "127.0.0.1", 8554, gt.KIND_ADDON
)


def _resolves_to(target):
    return patch.object(
        camera_mod, "resolve_go2rtc_target", AsyncMock(return_value=target)
    )


def _running_camera():
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam.hass.data = {}
    cam.hass.state = CoreState.running
    cam.hass.config.components = {"go2rtc"}
    return cam


async def test_the_issue_is_raised_once_an_sdes_camera_has_no_target_at_all():
    cam = _running_camera()
    with (
        _resolves_to(None),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
    ):
        assert await cam._sdes_has_no_go2rtc() is True
        assert await cam._sdes_has_no_go2rtc() is True
    create.assert_called_once()
    assert create.call_args.args[2] == gt.SDES_NO_GO2RTC_ISSUE
    assert create.call_args.kwargs["translation_key"] == gt.SDES_NO_GO2RTC_ISSUE


async def test_a_second_camera_does_not_re_raise_an_already_open_issue():
    cam_a, cam_b = _running_camera(), _running_camera()
    cam_b.hass = cam_a.hass
    with (
        _resolves_to(None),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
    ):
        assert await cam_a._sdes_has_no_go2rtc() is True
        assert await cam_b._sdes_has_no_go2rtc() is True
    create.assert_called_once()


async def test_the_issue_is_cleared_once_a_target_resolves():
    cam = _running_camera()
    cam.hass.data[gt.DOMAIN] = {gt._NO_GO2RTC_RAISED: True}
    with (
        _resolves_to(ADDON),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        assert await cam._sdes_has_no_go2rtc() is False
    delete.assert_called_once_with(cam.hass, gt.DOMAIN, gt.SDES_NO_GO2RTC_ISSUE)


async def test_an_already_cleared_issue_is_not_deleted_again():
    cam = _running_camera()
    with (
        _resolves_to(ADDON),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        assert await cam._sdes_has_no_go2rtc() is False
    delete.assert_not_called()


async def test_the_warning_leads_with_the_add_on(caplog):
    caplog.set_level(logging.WARNING)
    cam = _running_camera()
    with _resolves_to(None):
        assert await cam._sdes_has_no_go2rtc() is True
    line = next(
        r.getMessage() for r in caplog.records if "no go2rtc answered" in r.getMessage()
    )
    assert line.index("127.0.0.1:1984") < line.index("AIDOT_GO2RTC_API")


async def test_a_resolve_finding_a_target_clears_the_issue_without_any_camera_call():
    """The resolver clears the issue itself: a battery-only install with no
    live view yet still sees it clear as soon as the add-on or
    AIDOT_GO2RTC_API answers, not only on an SDES camera's next check."""
    hass = MagicMock()
    hass.data = {gt.DOMAIN: {gt._NO_GO2RTC_RAISED: True}}
    hass.state = CoreState.running
    with (
        patch.object(gt, "_resolve", AsyncMock(return_value=ADDON)),
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        target = await gt.resolve_go2rtc_target(hass)
    assert target is ADDON
    delete.assert_called_once_with(hass, gt.DOMAIN, gt.SDES_NO_GO2RTC_ISSUE)
    assert hass.data[gt.DOMAIN][gt._NO_GO2RTC_RAISED] is False


async def test_unloading_the_last_entry_clears_the_issue(monkeypatch):
    from aidot_cameras.camera import hls_ts

    from custom_components.aidot import async_unload_entry

    from .test_hls_in_sync_source import _unloading

    monkeypatch.setattr(hls_ts, "shutdown", lambda: None)
    hass = MagicMock()
    entry = _unloading(hass, 0)
    hass.data = {gt.DOMAIN: {gt._NO_GO2RTC_RAISED: True}}
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete:
        assert await async_unload_entry(hass, entry) is True
    assert gt._NO_GO2RTC_RAISED not in hass.data[gt.DOMAIN]
    assert delete.call_args_list[-1].args == (hass, gt.DOMAIN, gt.SDES_NO_GO2RTC_ISSUE)
