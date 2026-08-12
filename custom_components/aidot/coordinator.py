"""Coordinator for Aidot."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
import time
from typing import Any, cast

from aidot_cameras import configure_stream_limits
from aidot_cameras.client import AidotClient
from aidot_cameras.const import (
    CONF_ACCESS_TOKEN,
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_PRODUCT,
    CONF_SERVICE_MODULES,
    CONF_IDENTITY,
    CONF_MODEL_ID,
)
from aidot_cameras.camera.lan_control import (
    CameraLanClient,
    CameraLanError,
    discover_subnet,
)
from aidot_cameras.camera.models import CameraDeviceInformation, CameraStatusData
# Import from aidot_cameras, never `aidot`: the latter is an implementation
# detail of the camera library and an undeclared dependency for this
# integration.  `device_session_authenticated` is the library's shape-aware
# LAN-session check - see _is_connected below for why it is not a comparison.
from aidot_cameras.device_client import (
    CameraDeviceClient,
    DeviceClient,
    DeviceStatusData,
    device_session_authenticated,
)
from aidot_cameras.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ENABLE_LOCAL_CONTROL,
    DEFAULT_ENABLE_LOCAL_CONTROL,
    DOMAIN,
    SD_LOOKBACK_DAYS,
    SD_SESSION_POLL_S,
    SD_SESSION_WAIT_S,
)
from .sd_recordings import SdCache

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


def _has_usable_aes_key(device: dict[str, Any]) -> bool:
    """True when the record carries an aesKey the device client can actually use.

    Written defensively: ``aesKey`` may be absent, ``[]`` or ``[None]``, and the
    previous ``device[CONF_AES_KEY][0]`` indexed blindly - an empty list raised
    IndexError out of the comprehension that filters the device list, which
    dropped *every* remaining light rather than just the odd one.
    """
    aes_key = device.get(CONF_AES_KEY)
    return isinstance(aes_key, list) and bool(aes_key) and aes_key[0] is not None


def _advertises_light_control(device: dict[str, Any]) -> bool:
    """True when the product advertises a light control service module.

    Not every bulb reports ``type == "light"`` - controllers and strips report
    other types - so capability is the more reliable signal.
    """
    product = device.get(CONF_PRODUCT) or {}
    for module in product.get(CONF_SERVICE_MODULES) or []:
        ident = (module.get(CONF_IDENTITY) or "").lower()
        if ident.startswith("control.light"):
            return True
    return False


def _is_light_device(device: dict[str, Any]) -> bool:
    """Return True for a non-camera device this integration can drive as a light.

    Requires a usable aesKey (the device client cannot be built without one) and
    either ``type == "light"`` or an advertised light-control service module.
    """
    if _is_camera_device(device):
        return False
    if not _has_usable_aes_key(device):
        return False
    return device.get(_CONF_TYPE) == "light" or _advertises_light_control(device)


def _set_status_callback(device_client: Any, callback: Any) -> None:
    """Register (or clear) the device's status-update callback.

    Non-camera devices are served by upstream's own ``DeviceClient``, which
    exposes the plain ``on_status_update`` attribute; the camera client adds a
    ``set_status_fresh_cb()`` helper that assigns the same attribute.  Prefer
    the helper when it exists so camera clients keep whatever bookkeeping it
    does, and fall back to the attribute for upstream's client.
    """
    setter = getattr(device_client, "set_status_fresh_cb", None)
    if callable(setter):
        setter(callback)
    else:
        device_client.on_status_update = callback


def _device_id(device_client: Any) -> str:
    """Return the device id for either client flavor.

    The camera client carries ``device_id`` directly; upstream's client exposes
    it as ``info.dev_id``.
    """
    return getattr(device_client, "device_id", None) or device_client.info.dev_id


def _is_connected(device_client: Any) -> bool:
    """Return True when the device's LAN control session is authenticated.

    Delegates to the library, which is the only layer that knows how the
    installed upstream tracks this.  It is not one thing: upstream kept a
    private ``_state`` enum in 0.3.55 and replaced it with a
    ``connect_and_login`` property in 0.3.56, and both releases are live.

    This used to read ``_state`` directly.  That is not merely stale on 0.3.56
    - there is no ``_state`` at all, so the comparison evaluates False forever
    and every device reads as permanently disconnected, without raising.  The
    cost is bounded (a no-op reconnect attempt per poll; ``has_lan_state`` does
    not depend on this, precisely so a rename cannot pin every light to
    unknown), but it is invisible, which is why it goes through the library.
    """
    return device_session_authenticated(device_client)


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

    # True once the device itself has reported state over its LAN control
    # channel.  Until then the library's status object holds only defaults
    # (on=False, dimming/cct/rgbw unset), which must NOT be published as though
    # the device had said so - a bulb that is powered on but off-LAN would show
    # as "off" and invite a command that cannot be delivered.
    _has_lan_state: bool = False

    @property
    def has_lan_state(self) -> bool:
        """True when the status object holds real values the device reported."""
        return self._has_lan_state

    async def _async_setup(self) -> None:
        _set_status_callback(self.device_client, self._handle_status_update)
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
                _device_id(self.device_client),
            )

    def _handle_status_update(self, status: DeviceStatusData) -> None:
        # Only the device's own LAN push reaches here, so this is the signal that
        # the status object holds real values rather than defaults.  It also has
        # to be able to go BACK to defaults: the library's reset() sets
        # status.online False and then notifies, so following that flag both ways
        # is what keeps a dropped bulb from being republished with its last-known
        # values.  The cloud-online carry in _sync_light_coordinators writes
        # status.online directly and never comes through here, so it cannot
        # resurrect this flag.
        self._has_lan_state = bool(getattr(status, "online", False))
        self.async_set_updated_data(status)

    async def _async_update_data(self) -> DeviceStatusData:
        # Periodic poll: attempt reconnect if the TCP link went down.
        if not _is_connected(self.device_client):
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
        # Per-camera serve-audio override set by the "Camera audio" switch:
        # None = follow the global SDES-audio option; True/False = force it for
        # this camera.  Read by AidotCamera._connect_options; applied on the next
        # stream open.
        self.sdes_audio_override: bool | None = None
        # The last on-device listing, and the machinery for taking another.
        # The library is stateless about this on purpose - it answers what a
        # session says, and the policy about when a session is worth having is
        # here, because only this side knows what one costs.
        self.sd_cache: SdCache | None = None
        self._sd_opener: Callable[[], Awaitable[None]] | None = None
        self._sd_lock = asyncio.Lock()

    def register_session_opener(
        self, opener: "Callable[[], Awaitable[None]]"
    ) -> Callable[[], None]:
        """Let the camera entity lend the coordinator its way of opening a session.

        The entity owns the serve URLs and the connect options; the coordinator
        owns the cache. Rather than copy the former into the latter, the entity
        hands over the one call - which is already the prewarm path, so it goes
        through the open-gate and skips a camera the cloud says is offline.
        """
        self._sd_opener = opener

        def _remove() -> None:
            if self._sd_opener is opener:
                self._sd_opener = None

        return _remove

    async def _async_list_when_the_session_lands(
        self, fn: "Callable[..., Awaitable[Any]]", dc: Any
    ) -> Any:
        """Keep asking until the session the opener asked for exists, or give up.

        The opener does not leave a session behind when it returns. It starts
        the library's keepalive, which sets its options, schedules the handshake
        and returns at once - the session it will hold is assigned inside that
        background loop when the open completes, 15-21 s later on DTLS and
        25-70 s later on a cold SDES camera. Listing at the instant the opener
        returns therefore lists with no session, gets "could not ask", and
        leaves the cache untouched: the one path allowed to spend a camera wake
        spends it and lists nothing, and the user sees "Not listed yet - press
        the ... button" after pressing exactly that button.

        Re-asking IS the wait. A listing with no session costs nothing - the
        library answers before it sends a request - so this polls the same call
        rather than reaching into the client for the private attribute that
        holds the session. The first answer wins, including a camera that
        answered nothing: re-asking for a better answer would spend the second
        set of requests this lock exists to avoid.
        """
        deadline = time.monotonic() + SD_SESSION_WAIT_S
        while time.monotonic() < deadline:
            # A camera the cloud says has gone offline is not going to hand us a
            # session, and waiting out the full window for it would hold this
            # lock - and a pressed button - for over a minute for nothing.
            if not getattr(getattr(dc, "status", None), "online", True):
                _LOGGER.debug(
                    "Gave up waiting for a session to list %s: camera is offline",
                    getattr(dc, "device_id", "?"))
                return None
            await asyncio.sleep(SD_SESSION_POLL_S)
            result = await fn(days=SD_LOOKBACK_DAYS)
            if result is not None:
                return result
        _LOGGER.debug("No session to list %s arrived within %ss",
                      getattr(dc, "device_id", "?"), SD_SESSION_WAIT_S)
        return None

    async def async_list_sd_recordings(
        self, *, open_session: bool = False, only_if_stale: bool = False
    ) -> bool:
        """Take a fresh on-device listing. Returns True if the cache was updated.

        ``open_session`` is the ONLY path in this integration that deliberately
        opens a session to list, and it exists solely behind a button a person
        presses. Never call it on a schedule: the open-gate serialises
        handshakes across every camera, and that serialisation was this
        project's signature failure.

        ``only_if_stale`` re-decides, under the lock, whether the listing is
        still worth taking. A piggyback that queued behind another listing
        judged staleness against a cache that listing has since replaced, so
        without this it refreshes what is already fresh.
        """
        dc = self.device_client
        fn = getattr(dc, "async_get_sd_recordings", None)
        if fn is None:
            # An older library. Not a defect and not an empty card.
            return False

        # One listing at a time per camera: a button press during a piggyback
        # would otherwise send two sets of requests down the same channel.
        async with self._sd_lock:
            if (only_if_stale and self.sd_cache is not None
                    and not self.sd_cache.is_stale(time.time())):
                # Whoever held the lock has just listed. Serialising the second
                # listing behind the first is not the same as not sending it,
                # and sending it costs two more AVIO requests - up to 16 s of
                # timeouts on a silent camera - to re-fetch what was written
                # milliseconds ago.
                return False

            opened = False
            if open_session and self._sd_opener is not None:
                try:
                    await self._sd_opener()
                    opened = True
                except Exception as exc:
                    _LOGGER.debug("Could not open a session to list %s: %s",
                                  dc.device_id, exc)
            try:
                result = await fn(days=SD_LOOKBACK_DAYS)
                if result is None and opened:
                    # We asked for this session ourselves, so it is worth
                    # waiting for. A piggyback never gets here: it lists from a
                    # session that exists or not at all.
                    result = await self._async_list_when_the_session_lands(fn, dc)
            except Exception as exc:
                _LOGGER.debug("On-device listing failed for %s: %s",
                              dc.device_id, exc)
                return False

            if result is None:
                # "Could not ask", not "the card is empty". Overwriting the
                # cache here would publish a claim the camera never made.
                return False

            self.sd_cache = SdCache(
                records=list(result.records),
                hours=result.hours,
                # Carried, not re-derived: after this point nothing can tell a
                # silent camera from an empty card, so the library's answer is
                # the only place the distinction exists.
                answered=result.answered,
                complete=result.complete,
                start_ts=result.start_ts,
                end_ts=result.end_ts,
                fetched_at=time.time(),
            )
            # Deliberately NOT async_update_listeners(): the camera
            # coordinator's listeners are entity state writes driven by device
            # data, nothing subscribes to this cache, and the browser pulls it
            # on demand. Firing them from a background listing would push a
            # state write for every camera entity with no new data behind it.
            return True

    async def async_piggyback_sd_refresh(self) -> None:
        """List from a session somebody else opened, if the cache is old.

        Costs one AVIO round trip on a session that exists anyway. Deliberately
        cannot open one: that is the property the whole cost model rests on.

        It does, however, have to WAIT for one. Its caller runs the instant the
        keepalive returns, and the keepalive returns before the handshake it
        scheduled has produced anything - the session is assigned inside the
        background loop that follows, seconds later on DTLS and up to a minute
        later on a cold SDES camera. Listing at that instant asks with no
        session every single time, which is why this path never once refreshed
        a card until the wait below existed.

        Waiting is not polling. It reads a flag and sends nothing, so it cannot
        become the listing it is waiting to be able to do - re-asking for the
        listing instead would send a full listing the moment a session appeared,
        before staleness gets re-decided under the lock, which is the second
        listing ``only_if_stale`` exists to prevent.

        Staleness is decided twice, and the second time is the one that counts.
        The check here is a fast path that avoids the lock entirely; the check
        inside the lock is what stops a piggyback scheduled during a button
        press - which is every press, because the camera entity backgrounds one
        as soon as the keepalive starts - from listing the card a second time
        the moment the press finishes.
        """
        if self.sd_cache is not None and not self.sd_cache.is_stale(time.time()):
            return

        dc = self.device_client
        # An older library cannot be asked whether a session exists, so it does
        # not wait - which is exactly the behaviour this replaces, not a new
        # failure mode.
        if hasattr(dc, "has_live_session"):
            deadline = time.monotonic() + SD_SESSION_WAIT_S
            while not dc.has_live_session:
                if time.monotonic() >= deadline:
                    _LOGGER.debug(
                        "No session to piggyback a listing of %s appeared",
                        getattr(dc, "device_id", "?"))
                    return
                # A camera the cloud says is offline is not going to hand
                # anyone a session, so there is nothing left to wait for.
                if not getattr(getattr(dc, "status", None), "online", True):
                    _LOGGER.debug(
                        "Gave up waiting to piggyback a listing of %s: "
                        "camera is offline", getattr(dc, "device_id", "?"))
                    return
                await asyncio.sleep(SD_SESSION_POLL_S)

        await self.async_list_sd_recordings(open_session=False, only_if_stale=True)

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
        await self.device_client.async_start_motion_polling(self._handle_motion_event)

    async def _async_update_data(self) -> DeviceStatusData:
        # Refresh sensors + control-entity states from the cloud device payload
        # (battery, SD-card, occupancy, motion/night-vision, ...).  This is the
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
        # Anything the cloud returned that became neither a camera nor a light is
        # reported once per refresh.  Without this the drop was silent, so a bulb
        # that is online in the app but missing in Home Assistant gave nothing to
        # go on.
        unclaimed = [
            d for d in all_devices
            if not _is_camera_device(d) and d[CONF_ID] not in current_lights
        ]
        if unclaimed:
            _LOGGER.info(
                "%d cloud device(s) are neither camera nor light and have no "
                "entities: %s",
                len(unclaimed),
                ", ".join(
                    f"{d.get(CONF_MODEL_ID) or '?'}"
                    f"(type={d.get(_CONF_TYPE)},"
                    f"aesKey={'ok' if _has_usable_aes_key(d) else 'missing'})"
                    for d in unclaimed
                ),
            )
        self._sync_light_coordinators(current_lights)

        current_cameras = {
            d[CONF_ID]: d for d in all_devices if _is_camera_device(d)
        }
        self._sync_camera_coordinators(current_cameras)

        # Refresh camera sensors / control-entity states from the just-fetched
        # cloud "properties" (battery, SD-card, occupancy, motion, night-vision,
        # ...) - the reliable source the app reads; cameras don't push these over
        # MQTT.  Also seeds the short-TTL cache the per-camera polls reuse.
        self._dev_cache = current_cameras
        self._dev_cache_ts = self.hass.loop.time()
        self._refresh_camera_attributes(current_cameras)

    def _refresh_camera_attributes(
        self, current_cameras: dict[str, dict[str, Any]]
    ) -> None:
        """Push fresh cloud attributes onto each camera, and stop any stream whose
        camera has just gone offline."""
        for dev_id, device in current_cameras.items():
            coord = self.camera_coordinators.get(dev_id)
            if coord is None:
                continue
            dc = coord.device_client
            was_online = bool(getattr(getattr(dc, "status", None), "online", False))
            dc.update_status_from_device(device)
            now_online = bool(getattr(getattr(dc, "status", None), "online", False))
            # A camera that has gone offline must stop streaming. Nothing else
            # does this: the entities simply go unavailable, which is presentation
            # only, while the keepalive stays latched on - a renew POST every 20s
            # and an HTTP wake every 10 minutes at a camera nobody can view. Seen
            # live on a battery camera already down to 5%.
            # There is no public "is streaming" flag; diagnostics uses
            # stream_rtsp_url, which is None unless a serve is up, and the
            # underlying latch covers the window before the URL is assigned.
            streaming = (getattr(dc, "stream_rtsp_url", None) is not None
                         or bool(getattr(dc, "_streaming_active", False)))
            if was_online and not now_online and streaming:
                _LOGGER.info(
                    "Camera %s went offline while streaming; stopping the stream",
                    dev_id,
                )
                self.config_entry.async_create_background_task(
                    self.hass,
                    dc.async_stop_streaming(),
                    name=f"aidot-stop-offline-{dev_id}",
                )

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
        # Carry the cloud's reachability flag onto non-camera clients.  Upstream's
        # device client only sets ``status.online`` after a successful LAN login,
        # so a bulb that is powered and reachable through the cloud - but whose
        # LAN control port has not been reached (not yet discovered, different
        # VLAN, sleeping) - would otherwise stay unavailable in Home Assistant
        # forever.  The camera path gets the same flag via
        # ``update_status_from_device``.
        for dev_id, device in current.items():
            coord = self.device_coordinators.get(dev_id)
            if coord is None:
                continue
            online = device.get("online")
            if online is None:
                continue
            status = coord.device_client.status
            if status.online != bool(online):
                status.online = bool(online)
                coord.async_set_updated_data(status)

    def _sync_camera_coordinators(self, current: dict[str, dict[str, Any]]) -> None:
        self._sync_coordinators(cast(dict[str, AidotDeviceUpdateCoordinator], self.camera_coordinators), current, is_camera=True)
        # Size the library's concurrent-serve cap to the fleet. Its default of 3
        # is a host-protection guard, and a camera holds its slot for the life of
        # its serve - so on an account with more cameras than the cap, the extras
        # do not merely queue, they never stream at all and nothing reports why.
        # Confirmed on a 4-camera fleet: the library logged "waiting for a stream
        # slot (cap reached)" for the fourth every time, and that was exactly the
        # camera that would not play here. AIDOT_MAX_CONCURRENT_STREAMS still
        # wins, so an operator who capped a small host keeps their cap.
        if current:
            try:
                configure_stream_limits(len(current))
            except Exception:  # never let a tuning call break a refresh
                _LOGGER.debug("could not size the stream cap", exc_info=True)
        # Opt-in: attach LAN control to eligible cameras not yet attached.
        if self.config_entry.options.get(
            CONF_ENABLE_LOCAL_CONTROL, DEFAULT_ENABLE_LOCAL_CONTROL
        ) and (set(current) - self._lan_attempted):
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_attach_local_control(dict(current)),
                name="aidot-lan-attach",
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
            _set_status_callback(coord.device_client, None)
            if is_camera:
                self.config_entry.async_create_background_task(
                    self.hass,
                    coord.device_client.async_stop_streaming(),
                    name=f"aidot-stop-streaming-{dev_id}",
                )
                self.config_entry.async_create_background_task(
                    self.hass,
                    coord.device_client.async_stop_motion_polling(),
                    name=f"aidot-stop-motion-{dev_id}",
                )
        if removed:
            self._purge_deleted_entries()
        for dev_id, device in current.items():
            if dev_id not in coord_dict:
                dc = self.client.get_device_client(device)
                # The library decides which class it hands back, so it - not our
                # own guess at what a camera is - decides which coordinator may
                # drive it.  The two predicates are not identical (ours also
                # accepts a camera-ish service module on a non-IPC model), and a
                # camera coordinator over a plain device client fails on the
                # first camera-only call, which takes a whole platform's setup
                # down rather than just that device.
                if is_camera and not isinstance(dc, CameraDeviceClient):
                    _LOGGER.warning(
                        "Device %s looks like a camera but the library built a "
                        "plain device client for it; skipping it rather than "
                        "driving it with camera calls it cannot answer",
                        dev_id,
                    )
                    continue
                coord: AidotDeviceUpdateCoordinator
                if is_camera:
                    coord = AidotCameraUpdateCoordinator(
                        self.hass, self.config_entry, dc, self
                    )
                else:
                    coord = AidotDeviceUpdateCoordinator(
                        self.hass, self.config_entry, dc
                    )
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_init_coordinator(coord, is_camera=is_camera),
                    name=f"aidot-init-coordinator-{dev_id}",
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
                _device_id(coord.device_client), exc,
            )

    async def async_cleanup(self) -> None:
        for coord in self.device_coordinators.values():
            _set_status_callback(coord.device_client, None)
        for coord in self.camera_coordinators.values():
            await coord.device_client.async_stop_motion_polling()
            await coord.device_client.async_stop_streaming()
        await self.client.async_cleanup()

    def token_fresh_cb(self) -> None:
        # login_info doubles as the account-shared cache for the persistent-
        # MQTT connection and its guarding asyncio.Lock (persistent MQTT is on
        # by default - see python-aidot-cameras' AIDOT_PERSISTENT_MQTT docs).
        # A plain .copy() is shallow: the same live Lock ends up in
        # config_entry.data, which HA later serializes to JSON when it
        # persists config entries to disk - the exact crash
        # python-aidot-cameras 0.11.2 fixed for its own standalone CLI.
        # serializable_login_info() (added in that release) is the
        # JSON-safe view; requires >=0.11.2 (see manifest.json).
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self.client.serializable_login_info()
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
