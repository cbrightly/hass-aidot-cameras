"""A light whose LAN address is on another subnet gets a repair issue that names it."""

from ipaddress import ip_network
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aidot import lan_issue
from custom_components.aidot.const import DOMAIN

NETS = [ip_network("192.168.0.0/24"), ip_network("10.0.0.0/8")]


def test_an_address_outside_every_ha_network_is_off_subnet():
    assert lan_issue.address_is_off_subnet("192.168.1.254", NETS) is True
    assert lan_issue.address_is_off_subnet("192.168.0.9", NETS) is False
    assert lan_issue.address_is_off_subnet("10.4.4.4", NETS) is False


def test_garbage_or_no_networks_is_never_off_subnet():
    assert lan_issue.address_is_off_subnet("not-an-ip", NETS) is False
    assert lan_issue.address_is_off_subnet("192.168.1.254", []) is False
    assert lan_issue.address_is_off_subnet(None, NETS) is False


def test_the_issue_is_raised_with_the_light_named_and_cleared_again():
    hass = MagicMock()
    with (
        patch("homeassistant.helpers.issue_registry.async_create_issue") as create,
        patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete,
    ):
        lan_issue.note_light_off_subnet(
            hass,
            "dev1",
            name="Corner light",
            address="192.168.1.254",
            networks=NETS,
            raise_it=True,
        )
        create.assert_called_once()
        args, kwargs = create.call_args
        assert args[:3] == (hass, DOMAIN, "light_off_subnet_dev1")
        assert kwargs["translation_key"] == "light_off_subnet"
        assert kwargs["translation_placeholders"] == {
            "name": "Corner light",
            "address": "192.168.1.254",
            "networks": "192.168.0.0/24, 10.0.0.0/8",
        }
        assert kwargs["is_fixable"] is False
        lan_issue.note_light_off_subnet(
            hass,
            "dev1",
            name="Corner light",
            address="192.168.1.254",
            networks=NETS,
            raise_it=False,
        )
        delete.assert_called_once_with(hass, DOMAIN, "light_off_subnet_dev1")


def test_a_registry_failure_never_escapes():
    hass = MagicMock()
    with patch(
        "homeassistant.helpers.issue_registry.async_create_issue",
        side_effect=RuntimeError("no registry"),
    ):
        lan_issue.note_light_off_subnet(
            hass,
            "dev1",
            name="x",
            address="192.168.1.254",
            networks=NETS,
            raise_it=True,
        )  # must not raise


def _light(over, address, online, with_accessors=True):
    dc = SimpleNamespace(
        device_id="dev1",
        info=SimpleNamespace(name="Corner light"),
        status=SimpleNamespace(online=online),
    )
    if with_accessors:
        dc.lan_login_over_ceiling = over
        dc.lan_address = address
        dc.lan_login_failures = 6 if over else 0
    return dc


@pytest.mark.parametrize(
    ("over", "address", "online", "expect"),
    [
        (True, "192.168.1.254", True, True),  # the case this exists for
        (True, "192.168.0.9", True, False),  # on our subnet: a different fault
        # The cloud saying offline is no reason to stay quiet: a light on the
        # wrong subnet usually cannot reach the cloud either (the reference
        # installation's Mujoy light, 2026-10-11), and its address came from
        # this run's discovery, so it answered a broadcast - it is powered.
        (True, "192.168.1.254", False, True),
        (True, "192.168.0.9", False, False),  # offline on our subnet: no issue
        (False, "192.168.1.254", True, False),  # not over the ceiling yet
        (True, None, True, False),  # never discovered
    ],
)
def test_decide_off_subnet_issue(over, address, online, expect):
    assert lan_issue.wants_issue(_light(over, address, online), NETS) is expect


def test_an_older_library_without_the_accessors_is_skipped():
    assert (
        lan_issue.wants_issue(
            _light(True, "192.168.1.254", True, with_accessors=False), NETS
        )
        is False
    )


async def test_the_light_poll_raises_and_clears_the_issue(hass):
    from custom_components.aidot import coordinator as cm

    dc = _light(True, "192.168.1.254", True)
    dc.async_login = AsyncMock(return_value=None)
    entry = _entry(hass)
    entry.runtime_data = SimpleNamespace(lan_networks=NETS)
    coord = cm.AidotDeviceUpdateCoordinator(hass, entry, dc)
    with (
        patch.object(cm, "_is_connected", return_value=False),
        patch("custom_components.aidot.lan_issue.note_light_off_subnet") as note,
    ):
        await coord._async_update_data()
        note.assert_called_once()
        assert note.call_args.kwargs["raise_it"] is True
        dc.lan_login_failures = 0
        dc.lan_login_over_ceiling = False
        await coord._async_update_data()
        assert note.call_args.kwargs["raise_it"] is False


def _entry(hass):
    """A config entry whose background tasks are real ones on hass."""
    entry = MagicMock()
    entry.async_create_background_task = lambda h, coro, name: (
        h.async_create_background_task(coro, name)
    )
    return entry


async def test_a_login_slower_than_the_poll_wait_is_not_cancelled(hass, monkeypatch):
    """The library bounds and counts each attempt; cancelling it at the
    integration's 10 s meant an address that never answers was never counted,
    so it never reached the ceiling or raised the repair issue."""
    import asyncio

    from custom_components.aidot import coordinator as cm

    monkeypatch.setattr(cm, "_LIGHT_CONNECT_TIMEOUT", 0.05)
    finished = []

    async def slow_login():
        await asyncio.sleep(0.3)
        finished.append(True)

    dc = _light(False, "192.168.1.254", True)
    dc.async_login = slow_login
    coord = cm.AidotDeviceUpdateCoordinator(hass, _entry(hass), dc)
    await coord._async_connect()  # returns after the 0.05 s wait
    assert finished == []
    task = coord._login_task
    assert task is not None and not task.done()
    await task
    assert finished == [True] and not task.cancelled()


async def test_a_poll_during_a_running_login_does_not_start_another(hass, monkeypatch):
    import asyncio

    from custom_components.aidot import coordinator as cm

    monkeypatch.setattr(cm, "_LIGHT_CONNECT_TIMEOUT", 0.02)
    calls = []

    async def slow_login():
        calls.append(1)
        await asyncio.sleep(0.2)

    dc = _light(False, "192.168.1.254", True)
    dc.async_login = slow_login
    coord = cm.AidotDeviceUpdateCoordinator(hass, _entry(hass), dc)
    await coord._async_connect()
    await coord._async_connect()
    assert calls == [1]
    await coord._login_task
    await coord._async_connect()  # the previous one finished: a new attempt
    assert calls == [1, 1]


async def test_an_off_subnet_check_that_raises_never_fails_the_light_poll(hass):
    """The check is best effort; a client with no usable id must not take the light down."""
    from custom_components.aidot import coordinator as cm

    dc = _light(True, "192.168.1.254", True)
    dc.async_login = AsyncMock(return_value=None)
    entry = _entry(hass)
    entry.runtime_data = SimpleNamespace(lan_networks=NETS)
    coord = cm.AidotDeviceUpdateCoordinator(hass, entry, dc)
    with (
        patch.object(cm, "_is_connected", return_value=True),
        patch.object(cm, "_device_id", side_effect=AttributeError("no dev_id")),
    ):
        assert await coord._async_update_data() is dc.status


def test_clear_for_deletes_only_the_named_lights_issues():
    hass = MagicMock()
    with patch("homeassistant.helpers.issue_registry.async_delete_issue") as delete:
        lan_issue.clear_for(hass, ["dev1", "dev2"])
    assert [c.args for c in delete.call_args_list] == [
        (hass, DOMAIN, "light_off_subnet_dev1"),
        (hass, DOMAIN, "light_off_subnet_dev2"),
    ]


def test_clear_for_never_raises():
    with patch(
        "homeassistant.helpers.issue_registry.async_delete_issue",
        side_effect=RuntimeError,
    ):
        lan_issue.clear_for(MagicMock(), ["dev1"])


async def test_unloading_an_entry_clears_only_its_own_lights_issues(monkeypatch):
    """A removed or disabled entry used to leave its repair issues in Settings
    until Home Assistant restarted. Another entry's issues must stay."""
    from aidot_cameras.camera import hls_ts

    from custom_components.aidot import async_unload_entry

    from .test_hls_in_sync_source import _unloading

    monkeypatch.setattr(hls_ts, "shutdown", lambda: None)
    cleared = []
    monkeypatch.setattr(
        lan_issue, "clear_for", lambda hass, ids: cleared.append(list(ids))
    )
    hass = MagicMock()
    entry = _unloading(hass, 1)
    entry.runtime_data.device_coordinators = {"lightA": object(), "lightB": object()}
    assert await async_unload_entry(hass, entry) is True
    assert cleared == [["lightA", "lightB"]]


async def test_a_light_disabled_in_home_assistant_is_not_logged_in(hass):
    """The owner disabled the device: no entity polls it, and its startup login
    used to log a connect warning for it anyway."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot import coordinator as cm

    real = MockConfigEntry(domain=DOMAIN)
    real.add_to_hass(hass)
    reg = dr.async_get(hass)
    device = reg.async_get_or_create(
        config_entry_id=real.entry_id, identifiers={(DOMAIN, "dev1")}
    )
    dc = _light(False, "192.168.1.17", True)
    dc.async_login = AsyncMock(return_value=None)
    coord = cm.AidotDeviceUpdateCoordinator(hass, _entry(hass), dc)

    reg.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await coord._async_connect()
    dc.async_login.assert_not_awaited()

    reg.async_update_device(device.id, disabled_by=None)
    await coord._async_connect()
    dc.async_login.assert_awaited_once()


async def test_a_disabled_light_gets_no_device_client_at_all(hass):
    """python-aidot 0.3.59 starts a LAN login the moment a device client is
    built, from the cloud's last-known address: skipping only our own login
    still left one connect warning per restart for a disabled light."""
    from homeassistant.helpers import device_registry as dr
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot import coordinator as cm

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    reg = dr.async_get(hass)
    device = reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "dev1")}
    )
    reg.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    m = object.__new__(cm.AidotDeviceManagerCoordinator)
    m.hass = hass
    m.config_entry = entry
    m.client = MagicMock()
    m._async_init_coordinator = AsyncMock()
    coords: dict = {}
    m._sync_coordinators(coords, {"dev1": {"id": "dev1"}}, is_camera=False)
    m.client.get_device_client.assert_not_called()
    assert coords == {}


def test_the_lookup_prefers_the_entry_scoped_call_where_ha_has_it():
    from custom_components.aidot import coordinator as cm

    class _New:
        def __init__(self):
            self.calls = []

        def async_get_device_by_identifier(self, ident, entry_id):
            self.calls.append((ident, entry_id))
            return SimpleNamespace(disabled_by="user")

        def async_get_device(self, **kw):
            raise AssertionError("deprecated call used")

    reg = _New()
    with patch("homeassistant.helpers.device_registry.async_get", return_value=reg):
        assert cm._device_disabled(MagicMock(), "entry1", "dev1") is True
    assert reg.calls == [((DOMAIN, "dev1"), "entry1")]

    class _Old:
        def async_get_device(self, identifiers):
            return SimpleNamespace(disabled_by=None)

    with patch("homeassistant.helpers.device_registry.async_get", return_value=_Old()):
        assert cm._device_disabled(MagicMock(), "entry1", "dev1") is False
