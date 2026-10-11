"""A light whose LAN address is on another subnet gets a repair issue that names it."""

from ipaddress import ip_network
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        (True, "192.168.1.254", False, False),  # the cloud says it is off
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
    dc.async_login = MagicMock(return_value=_done())
    entry = MagicMock()
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


def _done():
    import asyncio

    f = asyncio.get_event_loop().create_future()
    f.set_result(None)
    return f


async def test_an_off_subnet_check_that_raises_never_fails_the_light_poll(hass):
    """The check is best effort; a client with no usable id must not take the light down."""
    from custom_components.aidot import coordinator as cm

    dc = _light(True, "192.168.1.254", True)
    dc.async_login = MagicMock(return_value=_done())
    entry = MagicMock()
    entry.runtime_data = SimpleNamespace(lan_networks=NETS)
    coord = cm.AidotDeviceUpdateCoordinator(hass, entry, dc)
    with (
        patch.object(cm, "_is_connected", return_value=True),
        patch.object(cm, "_device_id", side_effect=AttributeError("no dev_id")),
    ):
        assert await coord._async_update_data() is dc.status
