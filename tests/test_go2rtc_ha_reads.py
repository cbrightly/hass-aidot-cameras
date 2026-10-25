"""Home Assistant's go2rtc provider must never read a server we publish into.

HA's go2rtc client (0.4.0) lists the server's whole /api/streams on every
WebRTC offer and cannot decode a published stream (the publisher has no url),
so one AiDot camera publishing into the server HA reads failed every camera's
live view. Upstream #344/#345 were stale-closed unreviewed. When HA's `url:`
server is the one we would publish into: standard cameras pull, SDES cameras
are not streamed, a Repairs issue explains, and live publishers are stopped.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aidot import camera as camera_mod
from custom_components.aidot import go2rtc_target as gt

from .test_camera import _make_camera


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """GET url -> (status, json) from a map; anything else raises."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if url not in self.routes:
            raise OSError("unreachable")
        return _Resp(*self.routes[url])


def _hass(ha_url, ha_routes):
    hass = MagicMock()
    hass.data = {"go2rtc": SimpleNamespace(url=ha_url, session=_Session(ha_routes))}
    return hass


ADDON = "http://127.0.0.1:1984"


async def test_no_handle_or_ha_managed_server_is_never_read_and_not_probed():
    hass = MagicMock()
    hass.data = {}
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is False
    hass = _hass(gt.HA_MANAGED_URL, {})
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is False
    assert hass.data["go2rtc"].session.calls == []


async def test_a_server_older_than_ha_accepts_is_not_read():
    # WebRTC Camera's own go2rtc (1.9.9): HA never loads its provider for it.
    hass = _hass(
        "http://localhost:1984",
        {"http://localhost:1984/api": (200, {"version": "1.9.9"})},
    )
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is False


async def test_ha_server_listing_our_streams_is_read():
    hass = _hass(
        "http://homeassistant.local:1984",
        {
            "http://homeassistant.local:1984/api": (200, {"version": "1.9.14"}),
            "http://homeassistant.local:1984/api/streams": (
                200,
                {"aidot_abc": {}, "camera.porch": {}},
            ),
        },
    )
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is True


async def test_a_different_server_is_not_read():
    hass = _hass(
        "http://192.168.1.5:1984",
        {
            "http://192.168.1.5:1984/api": (
                200,
                {"version": "1.9.14", "revision": "x", "config_path": "/a", "rtsp": {}},
            ),
            "http://192.168.1.5:1984/api/streams": (200, {"camera.porch": {}}),
        },
    )
    ours = _Session(
        {
            f"{ADDON}/api/streams": (200, {"aidot_abc": {}}),
            f"{ADDON}/api": (
                200,
                {"version": "1.9.9", "revision": "y", "config_path": "/b", "rtsp": {}},
            ),
        }
    )
    assert await gt._ha_reads_target(hass, ADDON, ours) is False


async def test_two_servers_of_one_release_are_not_mistaken_for_one():
    """Review defect 1: version, revision, config path and rtsp come from the
    build; two installs of one release share them all."""
    info = {
        "version": "1.9.14",
        "revision": "b5948cf",
        "config_path": "/config/go2rtc.yaml",
        "rtsp": {"listen": ":8554"},
    }
    hass = _hass(
        "http://192.168.0.50:1984",
        {
            "http://192.168.0.50:1984/api": (200, info),
            "http://192.168.0.50:1984/api/streams": (200, {}),
        },
    )
    ours = _Session({f"{ADDON}/api/streams": (200, {}), f"{ADDON}/api": (200, info)})
    assert await gt._ha_reads_target(hass, ADDON, ours) is False


async def test_this_hosts_own_address_at_the_same_port_is_the_same_server(
    monkeypatch,
):
    """Review defect 6: url: by the host's LAN address, before any stream
    exists, is still this host's listener on 1984."""
    hass = _hass(
        "http://192.168.0.175:1984",
        {
            "http://192.168.0.175:1984/api": (200, {"version": "1.9.14"}),
            "http://192.168.0.175:1984/api/streams": (200, {}),
        },
    )

    async def adapters(_hass):
        return [{"ipv4": [{"address": "192.168.0.175"}], "ipv6": []}]

    monkeypatch.setattr("homeassistant.components.network.async_get_adapters", adapters)
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is True


async def test_a_version_with_a_suffix_is_judged_as_ha_judges_it():
    """Review defect 5: HA's client parses with AwesomeVersion."""
    hass = _hass(
        "http://localhost:1984",
        {
            "http://localhost:1984/api": (200, {"version": "1.9.14-dev"}),
            "http://localhost:1984/api/streams": (200, {"aidot_abc": {}}),
        },
    )
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is True


@pytest.mark.parametrize(
    ("ha_url", "expect"),
    [("http://localhost:1984", True), ("http://192.168.1.5:1984", None)],
)
async def test_an_unreadable_ha_server_is_read_by_address_or_unknown(ha_url, expect):
    hass = _hass(ha_url, {})
    assert await gt._ha_reads_target(hass, ADDON, _Session({})) is expect


async def test_the_resolver_marks_the_target_raises_the_issue_and_signals(hass):
    sess = _Session(
        {
            "http://localhost:1984/api": (200, {"version": "1.9.14"}),
            "http://localhost:1984/api/streams": (200, {"aidot_abc": {}}),
        }
    )
    hass.data["go2rtc"] = SimpleNamespace(url="http://localhost:1984", session=sess)
    gt.invalidate(hass)
    sent = []
    with (
        patch.object(gt, "_addon_answers", AsyncMock(return_value=True)),
        patch(
            "homeassistant.helpers.dispatcher.async_dispatcher_send",
            lambda h, sig, v: sent.append((sig, v)),
        ),
        patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        t = await gt.resolve_go2rtc_target(hass)
        assert t.ha_reads is True
        gt.invalidate(hass)
        await gt.resolve_go2rtc_target(hass)  # unchanged: no second signal
        assert sent == [(gt.SIGNAL_GO2RTC_HA_READS, True)]
        create.assert_called_once()
        assert create.call_args.args[2] == gt.HA_READS_ISSUE
        hass.data["go2rtc"] = SimpleNamespace(url=gt.HA_MANAGED_URL, session=sess)
        gt.invalidate(hass)
        t = await gt.resolve_go2rtc_target(hass)
        assert t.ha_reads is False
        assert sent[-1] == (gt.SIGNAL_GO2RTC_HA_READS, False)
        delete.assert_called_once_with(hass, gt.DOMAIN, gt.HA_READS_ISSUE)


SHARED = gt.Go2rtcTarget(
    ADDON, object(), "127.0.0.1", 8554, gt.KIND_ADDON, ha_reads=True
)


def _resolves_to(target):
    return patch.object(
        camera_mod, "resolve_go2rtc_target", AsyncMock(return_value=target)
    )


async def test_a_standard_camera_pulls_when_ha_reads_the_server(monkeypatch):
    monkeypatch.setattr(camera_mod, "_direct_publish_enabled", lambda: True)
    cam = _make_camera(is_sdes=False)
    with _resolves_to(SHARED):
        assert await cam._push_mode() is False


async def test_an_sdes_camera_is_not_streamed_and_warns_once(caplog):
    caplog.set_level(logging.WARNING)
    cam = _make_camera(is_sdes=True, stream_rtsp_url=None)
    cam._setup_complete = True
    with _resolves_to(SHARED):
        assert await cam._push_mode() is False
        for _ in range(3):
            await cam.stream_source()
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()
    lines = [
        r for r in caplog.records if "Home Assistant's go2rtc reads" in r.getMessage()
    ]
    assert len(lines) == 1


@pytest.mark.parametrize(
    ("reads", "url", "stops"),
    [
        (True, "rtsp://127.0.0.1:8554/aidot_x", True),
        (True, None, False),
        # Review defect 4: a pull session's URL is the local http serve.
        (True, "http://127.0.0.1:18600/aidot_x.ts", False),
        (False, "rtsp://127.0.0.1:8554/aidot_x", False),
    ],
)
async def test_a_live_publisher_is_stopped_when_ha_turns_out_to_read(reads, url, stops):
    cam = _make_camera(is_sdes=True, stream_rtsp_url=url)
    dc = cam.coordinator.device_client
    dc.async_stop_streaming = AsyncMock()
    created = []
    cam.hass.async_create_background_task = lambda coro, name: created.append(coro)
    cam._on_go2rtc_ha_reads(reads)
    assert bool(created) is stops
    for c in created:
        await c
    assert dc.async_stop_streaming.await_count == (1 if stops else 0)


async def test_an_unknown_or_unanswered_read_keeps_the_last_answer(hass):
    """Review defect 2: an add-on restarting cannot be read; that must not
    clear the flag and let sessions publish into the server HA reads."""
    hass.data.setdefault(gt.DOMAIN, {})[gt._HA_READS] = True
    hass.data["go2rtc"] = SimpleNamespace(
        url="http://192.168.1.5:1984", session=_Session({})
    )
    assert await gt._ha_reads_or_last(hass, ADDON, _Session({}), True) is True
    assert await gt._ha_reads_or_last(hass, ADDON, _Session({}), False) is True
    hass.data[gt.DOMAIN][gt._HA_READS] = False
    assert await gt._ha_reads_or_last(hass, ADDON, _Session({}), False) is False


async def test_unloading_the_last_entry_forgets_the_flag_and_the_cached_target(
    monkeypatch,
):
    """Review defect 3: popping only the flag left a cached target that re-raised
    it and stopped the sessions a reload had just opened."""
    from aidot_cameras.camera import hls_ts

    from custom_components.aidot import async_unload_entry

    from .test_hls_in_sync_source import _unloading

    monkeypatch.setattr(hls_ts, "shutdown", lambda: None)
    hass = MagicMock()
    entry = _unloading(hass, 0)
    hass.data = {gt.DOMAIN: {gt._HA_READS: True, gt._STORE: (0.0, SHARED)}}
    with patch("homeassistant.helpers.issue_registry.async_delete_issue"):
        assert await async_unload_entry(hass, entry) is True
    assert gt._HA_READS not in hass.data[gt.DOMAIN]
    assert gt._STORE not in hass.data[gt.DOMAIN]
