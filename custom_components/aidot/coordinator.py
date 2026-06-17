"""Coordinator for Aidot."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any, cast

from aidot.client import AidotClient
from aidot.const import (
    CONF_ACCESS_TOKEN,
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_PRODUCT,
    CONF_SERVICE_MODULES,
    CONF_IDENTITY,
    CONF_MODEL_ID,
)
from aidot.camera.lan_control import (
    CameraLanClient,
    CameraLanError,
    discover_subnet,
)
from aidot.camera.client import CameraDeviceInformation, CameraStatusData
from aidot.device_client import DeviceClient, DeviceStatusData
from aidot.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ENABLE_LOCAL_CONTROL,
    CONF_PERSISTENT_MQTT,
    DEFAULT_ENABLE_LOCAL_CONTROL,
    DEFAULT_PERSISTENT_MQTT,
    DOMAIN,
)

type AidotConfigEntry = ConfigEntry[AidotDeviceManagerCoordinator]
_LOGGER = logging.getLogger(__name__)

UPDATE_DEVICE_LIST_INTERVAL = timedelta(hours=6)
UPDATE_CAMERA_ATTRS_INTERVAL = timedelta(minutes=5)
UPDATE_LIGHT_RECONNECT_INTERVAL = timedelta(minutes=5)

_LIGHT_CONNECT_TIMEOUT = 10.0

_CONF_TYPE = "type"


def _is_camera_device(device: dict[str, Any]) -> bool:
    """Return True if the device is a camera (IPC model or camera service module)."""
    model = (device.get(CONF_MODEL_ID) or "").upper()
    if "IPC" in model:
        return True
    product = device.get(CONF_PRODUCT) or {}
    for module in product.get(CONF_SERVICE_MODULES) or []:
        ident = (module.get(CONF_IDENTITY) or "").lower()
        if "camera" in ident or "ipc" in ident:
            return True
    return False


def _is_light_device(device: dict[str, Any]) -> bool:
    """Return True if the device is a light (has aesKey and type=light)."""
    return (
        device.get(_CONF_TYPE) == "light"
        and CONF_AES_KEY in device
        and device[CONF_AES_KEY][0] is not None
    )


class AidotDeviceUpdateCoordinator(DataUpdateCoordinator[DeviceStatusData]):
    """Manage data for a single Aidot light device (TCP push updates)."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
        device_client: DeviceClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            # Periodic interval drives reconnect attempts when TCP drops.
            update_interval=UPDATE_LIGHT_RECONNECT_INTERVAL,
        )
        self.device_client = device_client

    async def _async_setup(self) -> None:
        self.device_client.set_status_fresh_cb(self._handle_status_update)
        await self._async_connect()

    async def _async_connect(self) -> None:
        """Initiate TCP connection to the light device (non-blocking on failure)."""
        try:
            await asyncio.wait_for(
                self.device_client.async_login(), timeout=_LIGHT_CONNECT_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.debug(
                "Light %s: TCP connect timed out (will retry)",
                self.device_client.device_id,
            )

    def _handle_status_update(self, status: DeviceStatusData) -> None:
        self.async_set_updated_data(status)

    async def _async_update_data(self) -> DeviceStatusData:
        # Periodic poll: attempt reconnect if the TCP link went down.
        if not self.device_client.connect_and_login:
            await self._async_connect()
        return self.device_client.status


class AidotCameraUpdateCoordinator(AidotDeviceUpdateCoordinator):
    """Manage data for a single Aidot camera device (MQTT polled attributes)."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
        device_client: DeviceClient,
        manager: "AidotDeviceManagerCoordinator",
    ) -> None:
        super().__init__(hass, config_entry, device_client)
        self.update_interval = UPDATE_CAMERA_ATTRS_INTERVAL
        self._manager = manager
        self._motion_listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_motion_listener(self, cb: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Register a callback fired for each new motion/person cloud event.

        Returns a function that removes the listener.
        """
        self._motion_listeners.append(cb)

        def _remove() -> None:
            if cb in self._motion_listeners:
                self._motion_listeners.remove(cb)

        return _remove

    @callback
    def _handle_motion_event(self, event: dict[str, Any]) -> None:
        # Isolate listeners: one raising callback must not drop the event for the
        # others (event entity + occupancy sensor both subscribe) or propagate
        # into the library's motion-poll task.
        for cb in list(self._motion_listeners):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("Aidot motion listener raised")

    @property
    def camera_info(self) -> CameraDeviceInformation:
        """Return camera-specific device information."""
        return cast(CameraDeviceInformation, self.device_client.info)

    @property
    def camera_data(self) -> CameraStatusData | None:
        """Return camera-specific status data, or None if not yet fetched."""
        return cast(CameraStatusData, self.data) if self.data else None

    async def _async_setup(self) -> None:
        # Camera devices don't push status via TCP - skip set_status_fresh_cb.
        # Streaming is lazy for all models: the camera entity's stream_source()
        # starts the HTTP-listen serve (go2rtc pulls it) only when a viewer
        # connects, so we don't hold a WebRTC session / decode open 24/7 (Pi
        # friendly). Here we only start cloud motion-event polling.
        # Opt-in: reuse one persistent MQTT connection for camera commands
        # (PTZ/settings) instead of reconnecting per command. Account-level, so
        # setting it on this device_client enables it for the shared connection.
        if self.config_entry.options.get(
            CONF_PERSISTENT_MQTT, DEFAULT_PERSISTENT_MQTT
        ):
            self.device_client._persistent_mqtt_opt = True
        await self.device_client.async_start_motion_polling(self._handle_motion_event)

    async def _async_update_data(self) -> DeviceStatusData:
        # Refresh sensors + control-entity states from the cloud device payload
        # (battery, SD-card, occupancy, motion/night-vision, …).  This is the
        # reliable source the official app reads; cameras don't push these over
        # MQTT, so we no longer spin up a per-camera MQTT attribute poll.
        try:
            device = await self._manager.async_get_camera_device(
                self.device_client.device_id
            )
            if device:
                self.device_client.update_status_from_device(device)
        except Exception as exc:
            _LOGGER.debug(
                "Camera status refresh failed for %s (will retry): %s",
                self.device_client.device_id, exc,
            )
        return self.device_client.status


class AidotDeviceManagerCoordinator(DataUpdateCoordinator[None]):
    """Manage the full AiDot device list and spawn per-device coordinators."""

    config_entry: AidotConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_DEVICE_LIST_INTERVAL,
        )
        self.client = AidotClient(
            session=async_get_clientsession(hass),
            token=dict(config_entry.data),
        )
        self.client.set_token_fresh_cb(self.token_fresh_cb)
        self.device_coordinators: dict[str, AidotDeviceUpdateCoordinator] = {}
        self.camera_coordinators: dict[str, AidotCameraUpdateCoordinator] = {}
        # Short-TTL cache of the device list, so the per-camera attribute polls
        # (every 5 min, up to one per camera) share a single cloud fetch instead
        # of each re-pulling the whole list.
        self._dev_cache: dict[str, dict[str, Any]] = {}
        self._dev_cache_ts: float = 0.0
        self._dev_fetch_lock = asyncio.Lock()
        # Opt-in LAN control. ``_lan_attempted`` is every devId the one-shot
        # subnet sweep has already considered (attached OR found ineligible/
        # offline); it gates re-sweeping so battery cameras - which never answer
        # unicast - don't retrigger a full /24 sweep on every device-list refresh.
        # ``_lan_attached`` is the subset that actually got a CameraLanClient.
        self._lan_attempted: set[str] = set()
        self._lan_attached: set[str] = set()
        self._lan_lock = asyncio.Lock()

    async def _async_setup(self) -> None:
        try:
            await self.async_auto_login()
        except AidotUserOrPassIncorrect as error:
            raise ConfigEntryAuthFailed from error

    async def _async_update_data(self) -> None:
        try:
            data = await self.client.async_get_all_device()
        except AidotAuthFailed as error:
            # Access token AND refresh token expired (e.g. the integration was
            # disabled for a while). Try a headless full re-login with the stored
            # credentials before surfacing a reauth prompt to the user.
            _ensure = getattr(self.client, "async_ensure_token", None)
            if _ensure is None or not await _ensure():
                raise ConfigEntryAuthFailed from error
            try:
                data = await self.client.async_get_all_device()
            except AidotAuthFailed as error2:
                raise ConfigEntryAuthFailed from error2

        all_devices = data[CONF_DEVICE_LIST]

        current_lights = {
            d[CONF_ID]: d for d in all_devices if _is_light_device(d)
        }
        self._sync_light_coordinators(current_lights)

        current_cameras = {
            d[CONF_ID]: d for d in all_devices if _is_camera_device(d)
        }
        self._sync_camera_coordinators(current_cameras)

        # Refresh camera sensors / control-entity states from the just-fetched
        # cloud "properties" (battery, SD-card, occupancy, motion, night-vision,
        # …) - the reliable source the app reads; cameras don't push these over
        # MQTT.  Also seeds the short-TTL cache the per-camera polls reuse.
        self._dev_cache = current_cameras
        self._dev_cache_ts = self.hass.loop.time()
        for dev_id, device in current_cameras.items():
            coord = self.camera_coordinators.get(dev_id)
            if coord is not None:
                coord.device_client.update_status_from_device(device)

    async def async_get_camera_device(self, device_id: str) -> dict[str, Any] | None:
        """Return a camera's current cloud device dict (60s-cached list fetch).

        Shared by the per-camera coordinators so they don't each re-pull the
        full device list every 5 minutes.
        """
        now = self.hass.loop.time()
        async with self._dev_fetch_lock:
            if not self._dev_cache or (now - self._dev_cache_ts) > 60:
                data = await self.client.async_get_all_device()
                self._dev_cache = {
                    d[CONF_ID]: d
                    for d in data[CONF_DEVICE_LIST]
                    if _is_camera_device(d)
                }
                self._dev_cache_ts = now
        return self._dev_cache.get(device_id)

    def _sync_light_coordinators(self, current: dict[str, dict[str, Any]]) -> None:
        self._sync_coordinators(self.device_coordinators, current, is_camera=False)

    def _sync_camera_coordinators(self, current: dict[str, dict[str, Any]]) -> None:
        self._sync_coordinators(cast(dict[str, AidotDeviceUpdateCoordinator], self.camera_coordinators), current, is_camera=True)
        # Opt-in: attach LAN control to eligible cameras not yet attached.
        if self.config_entry.options.get(
            CONF_ENABLE_LOCAL_CONTROL, DEFAULT_ENABLE_LOCAL_CONTROL
        ) and (set(current) - self._lan_attempted):
            self.hass.async_create_task(
                self._async_attach_local_control(dict(current))
            )

    async def _async_attach_local_control(self, current: dict[str, dict[str, Any]]) -> None:
        """Resolve camera LAN IPs (one unicast sweep) and attach a CameraLanClient
        to each eligible mains-powered camera so its attribute writes go local-first.

        Idempotent and one-shot per camera: a camera is attached at most once.
        Battery cameras don't answer unicast discovery and are skipped naturally.
        """
        pending = [d for d in current if d not in self._lan_attempted]
        if not pending:
            return
        async with self._lan_lock:
            pending = [d for d in pending if d not in self._lan_attempted]
            if not pending:
                return
            try:
                ip_map = await discover_subnet()
            except Exception as exc:
                # Discovery itself failed (transient network error): leave these
                # cameras un-attempted so the next refresh retries the sweep.
                _LOGGER.debug("Aidot local control: subnet sweep failed: %s", exc)
                return
            # The sweep ran: mark every camera considered this pass so an
            # ineligible/offline one never retriggers another full sweep.
            self._lan_attempted.update(pending)
            for dev_id in pending:
                coord = self.camera_coordinators.get(dev_id)
                ip = ip_map.get(dev_id)
                if coord is None or ip is None:
                    continue  # camera not on this subnet / didn't answer unicast
                device = current.get(dev_id) or {}
                try:
                    lan = CameraLanClient(
                        device, self.client.login_info, ip=ip
                    )
                    if not await lan.async_resolve():
                        continue  # doesn't advertise local control
                    attrs = await lan.async_get_attributes()
                    if not CameraLanClient.is_mains_powered(attrs):
                        continue  # never hold/poll battery models
                    coord.device_client.attach_lan_client(lan)
                    self._lan_attached.add(dev_id)
                    _LOGGER.info(
                        "Aidot local control: attached for %s at %s", dev_id, ip
                    )
                except CameraLanError as exc:
                    _LOGGER.debug(
                        "Aidot local control: %s not eligible (%s)", dev_id, exc
                    )
                except Exception as exc:
                    _LOGGER.debug(
                        "Aidot local control: attach failed for %s: %s", dev_id, exc
                    )

    def _sync_coordinators(
        self,
        coord_dict: dict[str, AidotDeviceUpdateCoordinator],
        current: dict[str, dict[str, Any]],
        *,
        is_camera: bool,
    ) -> None:
        removed = set(coord_dict) - set(current)
        for dev_id in removed:
            coord = coord_dict.pop(dev_id)
            coord.device_client.set_status_fresh_cb(None)
            if is_camera:
                self.hass.async_create_task(
                    coord.device_client.async_stop_streaming()
                )
                self.hass.async_create_task(
                    coord.device_client.async_stop_motion_polling()
                )
        if removed:
            self._purge_deleted_entries()
        for dev_id, device in current.items():
            if dev_id not in coord_dict:
                dc = self.client.get_device_client(device)
                coord: AidotDeviceUpdateCoordinator
                if is_camera:
                    coord = AidotCameraUpdateCoordinator(
                        self.hass, self.config_entry, dc, self
                    )
                else:
                    coord = AidotDeviceUpdateCoordinator(
                        self.hass, self.config_entry, dc
                    )
                self.hass.async_create_task(
                    self._async_init_coordinator(coord, is_camera=is_camera)
                )
                coord_dict[dev_id] = coord

    async def _async_init_coordinator(
        self, coord: AidotDeviceUpdateCoordinator, *, is_camera: bool
    ) -> None:
        """Bring a per-device coordinator up, at setup or at runtime.

        ``async_config_entry_first_refresh`` may only run while the entry is
        SETUP_IN_PROGRESS; on the periodic device-list refresh (entry LOADED) it
        raises. A device added to the account after setup is discovered there, so
        for that path we run the setup hook (which starts camera motion polling -
        ``async_refresh`` skips it) and a plain refresh. Wrapped so this
        fire-and-forget task never surfaces an unhandled exception.
        """
        try:
            if self.config_entry.state is ConfigEntryState.SETUP_IN_PROGRESS:
                await coord.async_config_entry_first_refresh()
            else:
                await coord._async_setup()  # pyright: ignore[reportPrivateUsage]
                await coord.async_refresh()
        except Exception as exc:
            _LOGGER.warning(
                "Aidot: failed to initialise coordinator for %s: %s",
                coord.device_client.device_id, exc,
            )

    async def async_cleanup(self) -> None:
        for coord in self.device_coordinators.values():
            coord.device_client.set_status_fresh_cb(None)
        for coord in self.camera_coordinators.values():
            await coord.device_client.async_stop_motion_polling()
            await coord.device_client.async_stop_streaming()
        await self.client.async_cleanup()

    def token_fresh_cb(self) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self.client.login_info.copy()
        )

    async def async_auto_login(self) -> None:
        if self.client.login_info.get(CONF_ACCESS_TOKEN) is None:
            await self.client.async_post_login()

    def _purge_deleted_entries(self) -> None:
        device_reg = dr.async_get(self.hass)
        all_ids = {
            (DOMAIN, c.device_client.info.dev_id)
            for c in list(self.device_coordinators.values())
            + list(self.camera_coordinators.values())
        }
        # The hub device (the account) is keyed by the entry id, not a dev_id -
        # keep it so it isn't pruned as an "obsolete" device.
        all_ids.add((DOMAIN, self.config_entry.entry_id))
        for device in dr.async_entries_for_config_entry(
            device_reg, self.config_entry.entry_id
        ):
            if not set(device.identifiers) & all_ids:
                _LOGGER.debug("Removing obsolete device entry %s", device.name)
                device_reg.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )


def get_camera_coordinators(hass: HomeAssistant) -> dict[str, "AidotCameraUpdateCoordinator"]:
    """Return all loaded camera coordinators across all config entries, keyed by device ID."""
    result: dict[str, AidotCameraUpdateCoordinator] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coord = getattr(entry, "runtime_data", None)
        if coord is None:
            continue
        result.update(getattr(coord, "camera_coordinators", None) or {})
    return result
