"""Tests for the opt-in LAN local-control attach gating in the manager coordinator.

`_async_attach_local_control` does not use `self.hass`, so we build a bare manager
via ``__new__`` and set only the attributes the method touches, then patch the
library's ``discover_subnet`` / ``CameraLanClient`` to drive the gating branches.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.aidot.const import CONF_ENABLE_LOCAL_CONTROL
from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator


class _FakeLan:
    """Stand-in for CameraLanClient; behaviour driven by the device dict."""

    created: list = []

    def __init__(self, device, login_info, ip=None):
        self._device = device
        self.ip = ip
        type(self).created.append(self)

    async def async_resolve(self):
        return self._device.get("_resolve", True)

    async def async_get_attributes(self):
        # mains by default (no Battery_remaining); battery models set it
        return self._device.get("_attrs", {"Battery_remaining": None})

    @staticmethod
    def is_mains_powered(attrs):
        return attrs.get("Battery_remaining") is None


def _manager(current, *, enabled=True):
    mgr = AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)
    mgr.config_entry = SimpleNamespace(options={CONF_ENABLE_LOCAL_CONTROL: enabled})
    mgr.client = SimpleNamespace(login_info={"id": "u1"})
    mgr.camera_coordinators = {
        dev_id: SimpleNamespace(device_client=MagicMock()) for dev_id in current
    }
    mgr._lan_attempted = set()
    mgr._lan_attached = set()
    mgr._lan_lock = asyncio.Lock()
    return mgr


async def _attach(mgr, current, ip_map):
    _FakeLan.created = []
    with patch(
        "custom_components.aidot.coordinator.discover_subnet",
        new=AsyncMock(return_value=ip_map),
    ), patch(
        "custom_components.aidot.coordinator.CameraLanClient", _FakeLan
    ):
        await mgr._async_attach_local_control(current)


def _attached(mgr, dev_id):
    return mgr.camera_coordinators[dev_id].device_client.attach_lan_client.called


async def test_attaches_eligible_mains_camera_on_subnet():
    current = {"camA": {"id": "camA", "aesKey": ["k" * 16], "password": "p"}}
    mgr = _manager(current)
    await _attach(mgr, current, {"camA": "192.168.1.50"})
    assert _attached(mgr, "camA")
    assert "camA" in mgr._lan_attached
    assert _FakeLan.created[0].ip == "192.168.1.50"


async def test_skips_camera_not_on_subnet():
    current = {"camB": {"id": "camB"}}
    mgr = _manager(current)
    await _attach(mgr, current, {})  # sweep found nothing
    assert not _attached(mgr, "camB")
    assert mgr._lan_attached == set()


async def test_skips_battery_camera():
    current = {"camC": {"id": "camC", "_attrs": {"Battery_remaining": 50}}}
    mgr = _manager(current)
    await _attach(mgr, current, {"camC": "192.168.1.51"})
    assert not _attached(mgr, "camC")


async def test_skips_camera_that_does_not_resolve():
    current = {"camD": {"id": "camD", "_resolve": False}}
    mgr = _manager(current)
    await _attach(mgr, current, {"camD": "192.168.1.52"})
    assert not _attached(mgr, "camD")


async def test_idempotent_attach_once():
    current = {"camA": {"id": "camA"}}
    mgr = _manager(current)
    await _attach(mgr, current, {"camA": "192.168.1.50"})
    await _attach(mgr, current, {"camA": "192.168.1.50"})  # second pass
    assert (
        mgr.camera_coordinators["camA"].device_client.attach_lan_client.call_count == 1
    )


def test_disabled_option_skips_the_sweep():
    # The opt-in gate lives in the sync _sync_camera_coordinators path; with the
    # option off it must never schedule the attach task.
    current = {"camA": {"id": "camA"}}
    mgr = _manager(current, enabled=False)
    mgr.hass = SimpleNamespace(async_create_task=MagicMock())
    with patch.object(mgr, "_sync_coordinators"):
        mgr._sync_camera_coordinators(current)
    assert not mgr.hass.async_create_task.called


async def test_ineligible_camera_not_reswept():
    # A camera that can't attach (here: never on the subnet) must be marked
    # attempted so a later sync does NOT re-run the full subnet sweep for it.
    current = {"camB": {"id": "camB"}}
    mgr = _manager(current)
    sweep = AsyncMock(return_value={})  # camB never answers unicast
    with patch(
        "custom_components.aidot.coordinator.discover_subnet", new=sweep
    ), patch("custom_components.aidot.coordinator.CameraLanClient", _FakeLan):
        await mgr._async_attach_local_control(current)
        await mgr._async_attach_local_control(current)  # second sync pass
    assert sweep.call_count == 1
    assert not _attached(mgr, "camB")


async def test_sweep_failure_retries_next_pass():
    # If discovery itself fails, cameras stay un-attempted so the next sync
    # retries the sweep (transient network error, not ineligibility).
    current = {"camA": {"id": "camA"}}
    mgr = _manager(current)
    sweep = AsyncMock(side_effect=OSError("no network"))
    with patch(
        "custom_components.aidot.coordinator.discover_subnet", new=sweep
    ), patch("custom_components.aidot.coordinator.CameraLanClient", _FakeLan):
        await mgr._async_attach_local_control(current)
        await mgr._async_attach_local_control(current)
    assert sweep.call_count == 2
    assert mgr._lan_attempted == set()


async def test_sweep_failure_is_graceful():
    current = {"camA": {"id": "camA"}}
    mgr = _manager(current)
    _FakeLan.created = []
    with patch(
        "custom_components.aidot.coordinator.discover_subnet",
        new=AsyncMock(side_effect=OSError("no network")),
    ), patch(
        "custom_components.aidot.coordinator.CameraLanClient", _FakeLan
    ):
        await mgr._async_attach_local_control(current)  # must not raise
    assert not _attached(mgr, "camA")
