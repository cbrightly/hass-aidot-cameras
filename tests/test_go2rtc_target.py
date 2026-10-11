"""Which go2rtc this integration publishes to, in order: explicit, add-on, bundled, none."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aidot import go2rtc_target as gt


@pytest.fixture(autouse=True)
def _no_cache(hass):
    gt.invalidate(hass)


async def test_an_explicit_api_url_wins(hass, monkeypatch):
    monkeypatch.setenv("AIDOT_GO2RTC_API", "http://10.0.0.5:1984")
    monkeypatch.setenv("AIDOT_GO2RTC_RTSP_PORT", "8555")
    hass.data["go2rtc"] = SimpleNamespace(
        url="http://localhost:11984/", session=object()
    )
    t = await gt.resolve_go2rtc_target(hass)
    assert (t.kind, t.api_url, t.rtsp_host, t.rtsp_port) == (
        "explicit",
        "http://10.0.0.5:1984",
        "10.0.0.5",
        8555,
    )


async def test_a_reachable_addon_wins_over_the_bundled_server(hass, monkeypatch):
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    hass.data["go2rtc"] = SimpleNamespace(
        url="http://localhost:11984/", session=object()
    )
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=True)):
        t = await gt.resolve_go2rtc_target(hass)
    assert (t.kind, t.api_url, t.rtsp_host, t.rtsp_port) == (
        "addon",
        "http://127.0.0.1:1984",
        "127.0.0.1",
        8554,
    )


async def test_the_bundled_server_is_used_when_nothing_answers_on_1984(
    hass, monkeypatch
):
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    session = object()
    hass.data["go2rtc"] = SimpleNamespace(
        url="http://localhost:11984/", session=session
    )
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        t = await gt.resolve_go2rtc_target(hass)
    assert (t.kind, t.api_url, t.rtsp_host, t.rtsp_port) == (
        "bundled",
        "http://localhost:11984/",
        "127.0.0.1",
        18554,
    )
    assert t.session is session


@pytest.mark.parametrize(
    "value", [None, "http://127.0.0.1:11984", SimpleNamespace(url="x"), 42]
)
async def test_an_unknown_handle_shape_resolves_to_nothing(hass, monkeypatch, value):
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    if value is not None:
        hass.data["go2rtc"] = value
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert await gt.resolve_go2rtc_target(hass) is None


async def test_the_answer_is_cached_and_invalidated(hass, monkeypatch):
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    probe = AsyncMock(return_value=True)
    with patch.object(gt, "_addon_answers", probe):
        await gt.resolve_go2rtc_target(hass)
        await gt.resolve_go2rtc_target(hass)
        assert probe.await_count == 1
        gt.invalidate(hass)
        await gt.resolve_go2rtc_target(hass)
        assert probe.await_count == 2


@pytest.mark.parametrize("value", ["abc", "0", "70000"])
async def test_a_malformed_rtsp_port_falls_back_with_a_warning(
    hass, monkeypatch, caplog, value
):
    monkeypatch.setenv("AIDOT_GO2RTC_API", "http://10.0.0.5:1984")
    monkeypatch.setenv("AIDOT_GO2RTC_RTSP_PORT", value)
    with caplog.at_level("WARNING"):
        t = await gt.resolve_go2rtc_target(hass)
    assert t.rtsp_port == 8554
    assert "AIDOT_GO2RTC_RTSP_PORT" in caplog.text
    assert value in caplog.text


def test_rtsp_url_per_kind():
    addon = gt.Go2rtcTarget(
        "http://127.0.0.1:1984", object(), "127.0.0.1", 8554, gt.KIND_ADDON
    )
    assert addon.rtsp_url("aidot_x") == "rtsp://127.0.0.1:8554/aidot_x"

    bundled = gt.Go2rtcTarget(
        "http://localhost:11984/", object(), "127.0.0.1", 18554, gt.KIND_BUNDLED
    )
    assert bundled.rtsp_url("aidot_x") == "rtsp://127.0.0.1:18554/aidot_x"


# --------------------------------------------------------------------------- #
# review round 1: sticky add-on, no early caching, user-configured handles    #
# --------------------------------------------------------------------------- #

_MANAGED = SimpleNamespace(url="http://localhost:11984/", session=object())


async def test_once_the_addon_answered_a_failed_probe_keeps_the_addon(
    hass, monkeypatch
):
    """A late or failed probe of 1984 must not move cameras to the bundled
    server: mains sessions started there stay there, and the view path later
    hands out the add-on URL with nobody publishing to it."""
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    hass.data["go2rtc"] = _MANAGED
    probe = AsyncMock(side_effect=[True, False])
    with patch.object(gt, "_addon_answers", probe):
        first = await gt.resolve_go2rtc_target(hass)
        gt.invalidate(hass)
        second = await gt.resolve_go2rtc_target(hass)
    assert (first.kind, first.answered) == ("addon", True)
    assert (second.kind, second.answered) == ("addon", False)
    assert second.rtsp_url("x") == "rtsp://127.0.0.1:8554/x"


async def test_before_ha_is_running_the_bundled_server_is_never_chosen(
    hass, monkeypatch
):
    """Before Home Assistant has started the add-on may simply not be up yet,
    and any session opened into the bundled server then (motion prewarm, a
    live view, the opener) stays there until restart. So nothing resolves to
    it before HA is running, and that None is not cached."""
    from homeassistant.core import CoreState

    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    hass.data["go2rtc"] = _MANAGED
    hass.set_state(CoreState.starting)
    probe = AsyncMock(return_value=False)
    with patch.object(gt, "_addon_answers", probe):
        assert await gt.resolve_go2rtc_target(hass) is None
        assert await gt.resolve_go2rtc_target(hass) is None
        assert probe.await_count == 2  # not cached
        hass.set_state(CoreState.running)
        t = await gt.resolve_go2rtc_target(hass)
        assert (t.kind, t.rtsp_port) == ("bundled", 18554)
        await gt.resolve_go2rtc_target(hass)
        assert probe.await_count == 3  # cached once running


@pytest.mark.parametrize(
    "url", ["http://192.168.1.5:1984", "http://localhost:1984/", "http://go2rtc:1984"]
)
async def test_a_user_configured_go2rtc_url_is_not_the_bundled_server(
    hass, monkeypatch, url
):
    """`go2rtc: url:` in configuration.yaml fills hass.data["go2rtc"] too, but
    that server's RTSP is not 127.0.0.1:18554."""
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    hass.data["go2rtc"] = SimpleNamespace(url=url, session=object())
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert await gt.resolve_go2rtc_target(hass) is None


async def test_the_managed_url_matches_with_or_without_its_slash(hass, monkeypatch):
    monkeypatch.delenv("AIDOT_GO2RTC_API", raising=False)
    hass.data["go2rtc"] = SimpleNamespace(
        url="http://localhost:11984", session=object()
    )
    with patch.object(gt, "_addon_answers", AsyncMock(return_value=False)):
        assert (await gt.resolve_go2rtc_target(hass)).kind == "bundled"
