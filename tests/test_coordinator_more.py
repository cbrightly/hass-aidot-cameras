"""Remaining coordinator branches: per-device connect/update helpers, camera
motion-event isolation + camera_data, the LAN-attach scheduling / lock re-check /
error branches, and the device-registry purge.

Mirrors ``test_coordinator_sync.py`` / ``test_coordinator_auth.py``: coordinators
are built via ``__new__`` with only the attributes each method reads. The purge
test needs a real device registry, so it uses the phacc ``hass`` fixture.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aidot.camera.lan_control import CameraLanError
from aidot.const import CONF_ID

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot.const import CONF_ENABLE_LOCAL_CONTROL, DOMAIN
from custom_components.aidot.coordinator import (
    AidotCameraUpdateCoordinator,
    AidotDeviceManagerCoordinator,
    AidotDeviceUpdateCoordinator,
)

P = "custom_components.aidot.coordinator"


def _bare_mgr() -> AidotDeviceManagerCoordinator:
    return AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)


def _bare_light() -> AidotDeviceUpdateCoordinator:
    return AidotDeviceUpdateCoordinator.__new__(AidotDeviceUpdateCoordinator)


def _bare_camera() -> AidotCameraUpdateCoordinator:
    return AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)


class _FakeBgTasks:
    """Records background-task names and closes fire-and-forget coroutines."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(self, hass, coro, name=None):
        self.names.append(name)
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()


# --------------------------------------------------------------------------- #
# AidotDeviceUpdateCoordinator._async_connect / _handle_status_update / update
# --------------------------------------------------------------------------- #
async def test_async_connect_timeout_is_swallowed_and_logged():
    coord = _bare_light()
    coord.device_client = SimpleNamespace(
        async_login=AsyncMock(side_effect=TimeoutError()),
        device_id="d1",
    )
    await coord._async_connect()  # must not raise (105-106 debug log)
    coord.device_client.async_login.assert_awaited_once()


def test_handle_status_update_forwards_to_set_updated_data():
    coord = _bare_light()
    coord.async_set_updated_data = MagicMock()
    status = SimpleNamespace(online=True)
    coord._handle_status_update(status)  # line 112
    coord.async_set_updated_data.assert_called_once_with(status)


async def test_light_update_data_reconnects_when_link_down():
    coord = _bare_light()
    coord.device_client = SimpleNamespace(connect_and_login=False, status="STATUS")
    coord._async_connect = AsyncMock()
    result = await coord._async_update_data()  # line 117 reconnect
    coord._async_connect.assert_awaited_once()
    assert result == "STATUS"


# --------------------------------------------------------------------------- #
# AidotCameraUpdateCoordinator: motion isolation / camera_data / update swallow
# --------------------------------------------------------------------------- #
def test_handle_motion_event_isolates_a_raising_listener():
    coord = _bare_camera()
    seen: list[dict] = []

    def _bad(_event):
        raise RuntimeError("listener boom")

    def _good(event):
        seen.append(event)

    coord._motion_listeners = [_bad, _good]
    coord._handle_motion_event({"n": 1})  # 159-163: bad caught, good still fires
    assert seen == [{"n": 1}]


def test_camera_data_returns_data_when_present():
    coord = _bare_camera()
    coord.data = SimpleNamespace(online=True)  # truthy
    assert coord.camera_data is coord.data  # line 173 cast+return


async def test_camera_update_data_swallows_refresh_error():
    coord = _bare_camera()
    coord.device_client = SimpleNamespace(device_id="c1", status="STATUS")
    coord._manager = SimpleNamespace(
        async_get_camera_device=AsyncMock(side_effect=RuntimeError("cloud down"))
    )
    result = await coord._async_update_data()  # 194-195 debug log
    assert result == "STATUS"


# --------------------------------------------------------------------------- #
# _sync_camera_coordinators: schedules the LAN-attach task when enabled
# --------------------------------------------------------------------------- #
def test_sync_camera_schedules_lan_attach_when_enabled():
    mgr = _bare_mgr()
    mgr._sync_coordinators = MagicMock()  # skip real per-device coordinator build
    mgr.camera_coordinators = {}
    mgr._lan_attempted = set()
    mgr.hass = MagicMock()
    tasks = _FakeBgTasks()
    mgr.config_entry = SimpleNamespace(
        options={CONF_ENABLE_LOCAL_CONTROL: True},
        async_create_background_task=tasks,
    )

    mgr._sync_camera_coordinators({"c1": {CONF_ID: "c1"}})  # line 316

    assert any("lan-attach" in (n or "") for n in tasks.names)


# --------------------------------------------------------------------------- #
# _async_attach_local_control: re-check under lock + error branches
# --------------------------------------------------------------------------- #
class _AddingLock:
    """An async lock that marks the pending device attempted on entry, so the
    under-lock re-check finds nothing pending (exercises the early return)."""

    def __init__(self, attempted: set, dev_id: str) -> None:
        self._attempted = attempted
        self._dev_id = dev_id

    async def __aenter__(self):
        self._attempted.add(self._dev_id)
        return self

    async def __aexit__(self, *exc):
        return False


async def test_attach_local_control_early_return_after_relock():
    mgr = _bare_mgr()
    mgr._lan_attempted = set()
    mgr._lan_lock = _AddingLock(mgr._lan_attempted, "c1")
    with patch(f"{P}.discover_subnet", AsyncMock()) as sweep:
        await mgr._async_attach_local_control({"c1": {CONF_ID: "c1"}})  # line 335
    # Returned before sweeping because the re-check found nothing pending.
    sweep.assert_not_awaited()


async def test_attach_local_control_handles_lan_and_generic_errors():
    mgr = _bare_mgr()
    mgr._lan_attempted = set()
    mgr._lan_attached = set()
    mgr._lan_lock = asyncio.Lock()
    mgr.client = SimpleNamespace(login_info={})
    coord1 = SimpleNamespace(device_client=SimpleNamespace(attach_lan_client=MagicMock()))
    coord2 = SimpleNamespace(device_client=SimpleNamespace(attach_lan_client=MagicMock()))
    mgr.camera_coordinators = {"c1": coord1, "c2": coord2}

    inst_lan_err = MagicMock()
    inst_lan_err.async_resolve = AsyncMock(side_effect=CameraLanError("ineligible"))
    inst_generic = MagicMock()
    inst_generic.async_resolve = AsyncMock(side_effect=RuntimeError("attach boom"))

    with patch(
        f"{P}.discover_subnet",
        AsyncMock(return_value={"c1": "10.0.0.1", "c2": "10.0.0.2"}),
    ), patch(f"{P}.CameraLanClient", MagicMock(side_effect=[inst_lan_err, inst_generic])):
        await mgr._async_attach_local_control(
            {"c1": {CONF_ID: "c1"}, "c2": {CONF_ID: "c2"}}
        )  # 366-369 CameraLanError, 370-373 generic

    # Neither camera attached (both errored).
    assert mgr._lan_attached == set()
    coord1.device_client.attach_lan_client.assert_not_called()
    coord2.device_client.attach_lan_client.assert_not_called()


# --------------------------------------------------------------------------- #
# _purge_deleted_entries: prune obsolete devices, keep hub + live devices
# --------------------------------------------------------------------------- #
def test_purge_deleted_entries_removes_orphans(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="acct", data={})
    entry.add_to_hass(hass)
    reg = dr.async_get(hass)

    # Hub device (keyed by entry id), a live camera, and an orphan not in coords.
    reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "cam1")}
    )
    reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "orphan")}
    )

    mgr = _bare_mgr()
    mgr.hass = hass
    mgr.config_entry = entry
    mgr.device_coordinators = {}
    mgr.camera_coordinators = {
        "cam1": SimpleNamespace(
            device_client=SimpleNamespace(info=SimpleNamespace(dev_id="cam1"))
        )
    }

    mgr._purge_deleted_entries()  # 475-489

    assert reg.async_get_device(identifiers={(DOMAIN, "orphan")}) is None
    assert reg.async_get_device(identifiers={(DOMAIN, "cam1")}) is not None
    assert reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)}) is not None
