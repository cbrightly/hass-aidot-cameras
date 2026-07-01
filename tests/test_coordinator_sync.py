"""Pure/mockable coordinator logic in the manager coordinator.

Mirrors ``test_coordinator.py`` / ``test_local_control.py``: the methods under
test touch only a handful of attributes, so we build a bare manager via
``__new__`` and set exactly what each method reads - no hass lifecycle, no real
network, no ``AidotClient``.  The per-device coordinator classes are patched at
their module names when ``_sync_coordinators`` would otherwise construct a real
``DataUpdateCoordinator`` (which needs a live hass).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aidot.const import (
    CONF_ACCESS_TOKEN,
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_IDENTITY,
    CONF_MODEL_ID,
    CONF_PRODUCT,
    CONF_SERVICE_MODULES,
)

from custom_components.aidot.coordinator import (
    AidotDeviceManagerCoordinator,
    _is_camera_device,
    _is_light_device,
)


def _bare() -> AidotDeviceManagerCoordinator:
    return AidotDeviceManagerCoordinator.__new__(AidotDeviceManagerCoordinator)


class _FakeBgTasks:
    """Records ``config_entry.async_create_background_task`` calls and closes the
    fire-and-forget coroutines so pytest doesn't warn 'never awaited'."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(self, hass, coro, name=None):
        self.names.append(name)
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()


# --------------------------------------------------------------------------- #
# _is_camera_device / _is_light_device classification
# --------------------------------------------------------------------------- #
def test_is_camera_device_by_ipc_model():
    assert _is_camera_device({CONF_MODEL_ID: "A001064-IPC"}) is True
    # Case-insensitive (.upper() in the impl).
    assert _is_camera_device({CONF_MODEL_ID: "foo-ipc-bar"}) is True


def test_is_camera_device_by_service_module_identity():
    dev = {
        CONF_MODEL_ID: "A000088",
        CONF_PRODUCT: {CONF_SERVICE_MODULES: [{CONF_IDENTITY: "cameraStream"}]},
    }
    assert _is_camera_device(dev) is True
    dev2 = {CONF_PRODUCT: {CONF_SERVICE_MODULES: [{CONF_IDENTITY: "IpcThing"}]}}
    assert _is_camera_device(dev2) is True


def test_is_camera_device_false_for_plain_light():
    assert _is_camera_device({CONF_MODEL_ID: "A000099", "type": "light"}) is False
    assert _is_camera_device({}) is False
    # A non-camera service module doesn't match.
    assert _is_camera_device(
        {CONF_PRODUCT: {CONF_SERVICE_MODULES: [{CONF_IDENTITY: "switch"}]}}
    ) is False


def test_is_light_device_table():
    assert _is_light_device({"type": "light", CONF_AES_KEY: ["k", "x"]}) is True
    # Wrong type.
    assert _is_light_device({"type": "camera", CONF_AES_KEY: ["k"]}) is False
    # Missing aesKey.
    assert _is_light_device({"type": "light"}) is False
    # aesKey present but first element None.
    assert _is_light_device({"type": "light", CONF_AES_KEY: [None]}) is False


# --------------------------------------------------------------------------- #
# async_get_camera_device - 60s cache
# --------------------------------------------------------------------------- #
def _camera(dev_id: str) -> dict:
    return {CONF_ID: dev_id, CONF_MODEL_ID: f"{dev_id}-IPC"}


async def test_get_camera_device_cache_hit_avoids_refetch():
    mgr = _bare()
    mgr._dev_fetch_lock = asyncio.Lock()
    mgr._dev_cache = {"c1": _camera("c1")}
    mgr._dev_cache_ts = 1000.0
    mgr.client = SimpleNamespace(async_get_all_device=AsyncMock())
    # 30s later: within the 60s TTL, so the cached entry is served directly.
    mgr.hass = SimpleNamespace(loop=SimpleNamespace(time=MagicMock(return_value=1030.0)))

    result = await mgr.async_get_camera_device("c1")

    assert result == _camera("c1")
    mgr.client.async_get_all_device.assert_not_awaited()


async def test_get_camera_device_expiry_triggers_refetch():
    mgr = _bare()
    mgr._dev_fetch_lock = asyncio.Lock()
    mgr._dev_cache = {"stale": _camera("stale")}
    mgr._dev_cache_ts = 1000.0
    fresh = _camera("c1")
    light = {CONF_ID: "l1", "type": "light", CONF_AES_KEY: ["k"]}
    mgr.client = SimpleNamespace(
        async_get_all_device=AsyncMock(return_value={CONF_DEVICE_LIST: [fresh, light]})
    )
    # 100s later: past the 60s TTL, so the list is re-pulled and re-classified.
    mgr.hass = SimpleNamespace(loop=SimpleNamespace(time=MagicMock(return_value=1100.0)))

    result = await mgr.async_get_camera_device("c1")

    assert result == fresh
    mgr.client.async_get_all_device.assert_awaited_once()
    # The refreshed cache holds only the camera (the light is filtered out).
    assert set(mgr._dev_cache) == {"c1"}
    assert mgr._dev_cache_ts == 1100.0


# --------------------------------------------------------------------------- #
# _sync_coordinators - add / remove
# --------------------------------------------------------------------------- #
async def test_sync_adds_new_light_coordinator():
    mgr = _bare()
    mgr.device_coordinators = {}
    mgr.hass = MagicMock()
    tasks = _FakeBgTasks()
    mgr.config_entry = SimpleNamespace(async_create_background_task=tasks)
    dc = MagicMock()
    mgr.client = SimpleNamespace(get_device_client=MagicMock(return_value=dc))
    fake_coord = MagicMock()

    with patch(
        "custom_components.aidot.coordinator.AidotDeviceUpdateCoordinator",
        return_value=fake_coord,
    ):
        mgr._sync_light_coordinators({"l1": {CONF_ID: "l1"}})

    assert mgr.device_coordinators["l1"] is fake_coord
    mgr.client.get_device_client.assert_called_once()
    # The new coordinator is brought up via a scheduled background init task.
    assert any("init-coordinator-l1" in (n or "") for n in tasks.names)


async def test_sync_removes_camera_schedules_teardown():
    mgr = _bare()
    dc = MagicMock()
    dc.set_status_fresh_cb = MagicMock()
    dc.async_stop_streaming = AsyncMock()
    dc.async_stop_motion_polling = AsyncMock()
    coord = SimpleNamespace(device_client=dc, async_shutdown=AsyncMock())
    mgr.camera_coordinators = {"c1": coord}
    tasks = _FakeBgTasks()
    mgr.config_entry = SimpleNamespace(options={}, async_create_background_task=tasks)
    mgr.hass = MagicMock()
    mgr._lan_attempted = set()
    # Avoid the device-registry purge (needs a live hass); assert it's invoked.
    mgr._purge_deleted_entries = MagicMock()

    mgr._sync_camera_coordinators({})  # device left the account

    assert "c1" not in mgr.camera_coordinators
    dc.set_status_fresh_cb.assert_called_once_with(None)
    assert any("stop-streaming-c1" in (n or "") for n in tasks.names)
    assert any("stop-motion-c1" in (n or "") for n in tasks.names)
    assert any("shutdown-coordinator-c1" in (n or "") for n in tasks.names)
    mgr._purge_deleted_entries.assert_called_once()


# --------------------------------------------------------------------------- #
# _async_update_data happy path
# --------------------------------------------------------------------------- #
async def test_async_update_data_classifies_seeds_cache_and_updates_cameras():
    mgr = _bare()
    cam = _camera("c1")
    light = {CONF_ID: "l1", "type": "light", CONF_AES_KEY: ["k"]}
    mgr.client = SimpleNamespace(
        async_get_all_device=AsyncMock(
            return_value={CONF_DEVICE_LIST: [cam, light]}
        )
    )
    mgr._dev_fetch_lock = asyncio.Lock()
    mgr.hass = SimpleNamespace(loop=SimpleNamespace(time=MagicMock(return_value=2000.0)))
    cam_coord = SimpleNamespace(device_client=MagicMock())
    mgr.camera_coordinators = {"c1": cam_coord}
    # Stub the sync helpers (exercised separately) so no real coordinator is built.
    mgr._sync_light_coordinators = MagicMock()
    mgr._sync_camera_coordinators = MagicMock()

    await mgr._async_update_data()

    mgr._sync_light_coordinators.assert_called_once_with({"l1": light})
    mgr._sync_camera_coordinators.assert_called_once_with({"c1": cam})
    # The short-TTL cache is seeded with the just-fetched cameras only.
    assert mgr._dev_cache == {"c1": cam}
    assert mgr._dev_cache_ts == 2000.0
    # Existing camera coordinators get their device status refreshed.
    cam_coord.device_client.update_status_from_device.assert_called_once_with(cam)


# --------------------------------------------------------------------------- #
# async_auto_login
# --------------------------------------------------------------------------- #
async def test_auto_login_posts_when_no_access_token():
    mgr = _bare()
    mgr.client = SimpleNamespace(login_info={}, async_post_login=AsyncMock())
    await mgr.async_auto_login()
    mgr.client.async_post_login.assert_awaited_once()


async def test_auto_login_skips_when_token_present():
    mgr = _bare()
    mgr.client = SimpleNamespace(
        login_info={CONF_ACCESS_TOKEN: "tok"}, async_post_login=AsyncMock()
    )
    await mgr.async_auto_login()
    mgr.client.async_post_login.assert_not_awaited()
