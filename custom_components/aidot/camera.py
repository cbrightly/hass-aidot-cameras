"""Support for Aidot cameras."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any
import os
import socket
import time
import zlib
from datetime import timedelta

import aiohttp
import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from aidot_cameras.camera.constants import TALK_PCM_FRAME_BYTES, TALK_PCM_RATE
from aidot_cameras.camera.go2rtc import DEFAULT_BASE_URL, Go2rtcClient

try:  # library >= 1.0.0b22
    from aidot_cameras.camera.protocol import sprop_is_unstable as _sprop_is_unstable
except ImportError:  # older library: never recreate, i.e. today's behaviour
    def _sprop_is_unstable(_devid: str) -> bool:
        return False

from .const import (
    CONNECTION_MODE_LAN_DIRECT,
    CONF_MAINS_IDLE_S,
    CONF_STARTUP_PREWARM,
    CONF_SDES_ADAPTIVE,
    CONF_SDES_AUDIO,
    CONF_SDES_AUDIO_GAIN_DB,
    CONF_SDES_FAST_LIVEPLAY,
    CONF_SDES_PUSH,
    DEFAULT_MAINS_IDLE_S,
    DEFAULT_STARTUP_PREWARM,
    DEFAULT_SDES_ADAPTIVE,
    DEFAULT_SDES_AUDIO,
    DEFAULT_SDES_AUDIO_GAIN_DB,
    DEFAULT_SDES_FAST_LIVEPLAY,
    DEFAULT_SDES_PUSH,
    DEFAULT_SERVE_PORT_BASE,
    DOMAIN,
    resolve_connection_mode,
)
from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import aidot_device_info

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# Pull model: SDES cameras serve their decrypted stream over a local HTTP-listen
# socket and HA's stream integration / go2rtc PULL it the standard way (no go2rtc
# pre-registration, which a default go2rtc rejects). Each camera gets a stable
# local port so the URL is deterministic. Base is env-overridable.
def _serve_port(name: str) -> int:
    """Deterministic loopback HTTP-serve port for a camera (base..base+399)."""
    base = int(os.environ.get("AIDOT_SERVE_PORT_BASE", DEFAULT_SERVE_PORT_BASE))
    return base + (zlib.crc32(name.encode()) % 400)


# HA's bundled go2rtc serves cameras over WebRTC (sub-second) instead of HLS -
# but only when the camera's stream_source is a go2rtc-supported URL (RTSP), not
# our local http-mpegts -listen serve. So we register that serve with go2rtc (via
# the library's Go2rtcClient) and hand HA the go2rtc RTSP URL: go2rtc pulls the
# single-consumer serve once and fans out WebRTC to viewers. Default matches HA's
# bundled go2rtc; overridable; best-effort (falls back to the HLS serve).
_GO2RTC_API = os.environ.get("AIDOT_GO2RTC_API", DEFAULT_BASE_URL)
_GO2RTC_ENABLED = os.environ.get("AIDOT_GO2RTC", "1") != "0"


SERVICE_TALK = "talk"
SERVICE_PTZ = "ptz"

# How often to check for (and evict) a stale cached HA Stream whose underlying
# serve has been released - see AidotCamera._evict_stale_stream.
_STALE_STREAM_CHECK = timedelta(seconds=30)

# Background warm-up of mains cameras at entity setup. HA opening several cameras
# at once (restart / multi-camera dashboard - go2rtc prefetches every stream_source)
# serialises their WebRTC handshakes through the library's global open-gate; under
# concurrency the later cameras miss HA's 30s stream-worker open deadline and surface
# as failed views ("Invalid data ... rtsp://127.0.0.1:8554/aidot_*") with a 20-40s
# retry spinner. Warming each mains camera ahead of view-time moves those handshakes
# to background time (no view-clock), so live views hit an already-warm session.
# Staggered per camera so they don't all handshake in the same tick; battery cameras
# are excluded (a warm session would drain them).
_STARTUP_PREWARM_STAGGER_S = 3.0

# How long a push-mode view waits for the RTSP publisher to reach go2rtc before
# handing Home Assistant the URL anyway.
#
# Sized against a cold SDES open (measured 16-22s: MQTT signalling + ICE + SDES +
# ffmpeg). Below that, the first view is *guaranteed* to fail: go2rtc answers a
# consumer on a stream whose only producer is the inert placeholder with 404, and
# HA's go2rtc provider turns that into a WebRTCError with no retry - the "press
# play twice" symptom.
#
# Waiting this long is safe on the path that matters. HA's 10s
# CAMERA_STREAM_SOURCE_TIMEOUT wraps only `Camera.async_create_stream` (the HLS
# path); the WebRTC path reaches stream_source() through the go2rtc provider's
# `_update_stream_source`, which imposes no timeout at all (verified in HA
# 2026.7.4, the box's version). Snapshots do not reach stream_source() either -
# `use_stream_for_stills` is not set, so HA uses our `async_camera_image`.
#
# The wait ends the instant the publisher attaches, so a warm camera pays
# nothing; only a genuinely cold open waits.
#
# It is capped BELOW HA's 10s CAMERA_STREAM_SOURCE_TIMEOUT, and that cap is the
# whole point. The earlier 30s was reasoned about on the WebRTC path alone -
# which has no timeout - and dismissed the HLS path as "no worse than today".
# Measured 2026-08-06, it is much worse: HA cancels at 10s, so a camera whose
# cold open is longer than that can NEVER be served over HLS. Both battery
# A001513s failed `camera/stream` with "Timeout getting stream source" on every
# attempt, while streaming perfectly well over WebRTC - go2rtc held live
# producers for both and one rendered in a browser. Mains cameras pass only
# because startup prewarm keeps them warm; battery cameras are excluded from
# that on purpose, to save battery, so they are always cold here.
#
# So: spend a budget that fits inside HA's, then hand back the URL and let the
# keepalive keep warming the session in the background. HA's stream_worker
# tolerates the transient failure and retries - the same behaviour the DTLS path
# below already relies on - so the HLS view becomes a spinner that resolves.
# The cost is on the WebRTC path: a genuinely cold open can still hand the
# go2rtc provider a URL before the publisher lands, and that provider does not
# retry. That costs one bad first click, after which the camera is warm and the
# second click works. A bad first click on a cold camera is a better trade than
# a battery camera that can never serve HLS at all.
_PUSH_PUBLISHER_WAIT_S = float(os.environ.get("AIDOT_PUSH_PUBLISHER_WAIT_S", "8.5"))
_PUSH_PUBLISHER_POLL_S = 1.0

# Part of the budget above held back for the library's own readiness signal, so
# the publisher poll cannot eat the whole thing and leave the fallback dead.
_SERVE_READY_RESERVE_S = 1.5

# Directions accepted by the aidot.ptz service - the exact keys the library's
# async_ptz_move() understands (AVIOCTRLDEFs codes), so they pass straight
# through.  Mirrors onvif.ptz / reolink.ptz_move for automation use.
PTZ_DIRECTIONS = [
    "up", "down", "left", "right",
    "left_up", "left_down", "right_up", "right_down",
    "zoom_in", "zoom_out", "stop",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Aidot camera entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_cameras() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [AidotCamera(c) for c in new_coords.values()]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))

    # Register the two-way-audio (push-to-talk / announce) service. Plays an audio
    # source through the camera speaker via the library's async_speak().
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_TALK,
        {
            vol.Required("media"): cv.string,
            vol.Optional("max_seconds", default=30): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=300)
            ),
        },
        "async_talk",
    )
    platform.async_register_entity_service(
        SERVICE_PTZ,
        {
            vol.Required("direction"): vol.In(PTZ_DIRECTIONS),
            vol.Optional("speed", default=4): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=8)
            ),
        },
        "async_ptz",
    )


class AidotCamera(CoordinatorEntity[AidotCameraUpdateCoordinator], Camera):
    """Representation of an Aidot IP camera."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM
    # The transient connecting/buffering status is UI-only; keep it out of history.
    _unrecorded_attributes = frozenset({"stream_status"})

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        CoordinatorEntity.__init__(self, coordinator)  # type: ignore[call-arg]
        Camera.__init__(self)
        info = coordinator.device_client.info
        self._attr_unique_id = info.dev_id
        entry = getattr(coordinator, "config_entry", None)
        self._attr_device_info = aidot_device_info(
            info, entry.entry_id if entry else None
        )

        # Sanitised device ID safe to use as an RTSP stream name.
        self._rtsp_name = info.dev_id.replace("/", "_").replace(":", "_")
        # Cache for the last successfully fetched thumbnail bytes
        self._cached_image: bytes | None = None
        self._cached_image_ts: float = 0.0  # monotonic time of last successful fetch/check
        self._image_lock = asyncio.Lock()
        # Transient connection status exposed via the ``stream_status`` attribute
        # while connecting or after a failed stream start: (text, is_error,
        # monotonic_ts) or None.  Connecting text is cleared by stream_source
        # itself; error text auto-expires (TTL).
        self._stream_status: tuple[str, bool, float] | None = None
        # HA calls stream_source() once per camera during entity setup for
        # go2rtc pre-registration, before async_added_to_hass runs.  Those
        # setup calls must return fast; flag flips to True at the end of
        # async_added_to_hass so real user-initiated calls behave normally.
        self._setup_complete: bool = False
        # Task created by _on_motion_prewarm; stored so it can be cancelled on removal.
        self._prewarm_task: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator has no data or the camera is offline."""
        if not super().available:
            return False
        data = self.coordinator.data
        if data is None:
            return False
        return getattr(data, "online", True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Warm the thumbnail cache in the background so the camera card shows an
        # image on first load (before any stream / go2rtc) instead of a blank
        # tile. Non-blocking (a slow cloud fetch must not delay setup), and the
        # task is cancelled on removal so it can't write state on a dead entity.
        task = self.hass.async_create_task(self._prefetch_thumbnail())
        def _cancel_prefetch() -> None:
            task.cancel()
        self.async_on_remove(_cancel_prefetch)
        # Recover from a wedged stream: if our keepalive serve gets released
        # while HA still holds a cached Stream pointed at the (now dead) serve
        # URL, HA's stream_worker reconnects to it directly - it never re-calls
        # stream_source() - so the camera is stuck on "Connection refused".
        # Periodically evict that stale Stream so the next view restarts cleanly.
        _LOGGER.debug(
            "Stale-stream eviction watchdog armed for %s (every %ss)",
            self._rtsp_name, int(_STALE_STREAM_CHECK.total_seconds()),
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._evict_stale_stream, _STALE_STREAM_CHECK
            )
        )
        # Prewarm the WebRTC session on each new motion event so the stream is
        # already connecting by the time the user taps the HA notification
        # (~5-30 s later). start_keepalive() is idempotent (no-op if already
        # running), so this only fires during idle gaps between views.
        self.async_on_remove(
            self.coordinator.add_motion_listener(self._on_motion_prewarm)
        )

        # Lend the coordinator the one call that opens a session, so the
        # refresh button can use it without this entity's serve URLs and
        # connect options having to be copied anywhere.
        self.async_on_remove(
            self.coordinator.register_session_opener(self._prewarm_stream)
        )

        def _cancel_prewarm() -> None:
            if self._prewarm_task is not None:
                self._prewarm_task.cancel()

        self.async_on_remove(_cancel_prewarm)

        # Drop our go2rtc stream when the entity is removed (best-effort hygiene).
        # Named background task: survives entity removal to land the DELETE, and
        # is tracked/cleaned up properly by HA on shutdown.
        def _deregister_go2rtc() -> None:
            self.hass.async_create_background_task(
                self._unpublish_from_go2rtc(), "aidot-go2rtc-deregister"
            )

        self.async_on_remove(_deregister_go2rtc)

        # Warm mains cameras in the background so the first live view - and the
        # concurrent burst of views after an HA restart - hits a warm session
        # instead of racing HA's 30s stream-worker deadline (see startup-prewarm).
        _startup_task = self.hass.async_create_task(self._startup_prewarm())

        def _cancel_startup_prewarm() -> None:
            _startup_task.cancel()

        self.async_on_remove(_cancel_startup_prewarm)
        self._setup_complete = True

    async def _evict_stale_stream(self, _now: datetime.datetime | None = None) -> None:
        """Drop HA's cached Stream when our keepalive has ended underneath it.

        The DTLS serve idle-releases after 120s of no reader (and either path's
        keepalive can end on teardown), which unbinds the local serve port. But
        HA keeps the cached ``Camera.stream`` object pointed at that now-dead URL
        and its stream_worker reconnects to it *directly* - bypassing
        ``stream_source()`` entirely - so the camera is stuck returning
        "Connection refused" until HA's own (longer) idle timeout finally drops
        the Stream. Proactively stop and clear it here so the next play resolves
        a fresh source and restarts the keepalive.

        Fires only when the keepalive is genuinely gone
        (``stream_rtsp_url is None`` => not ``_streaming_active``): never during a
        live view or a transient session reconnect (those keep the keepalive - and
        thus the serve URL - alive, so HA's reconnect will succeed on its own).
        While the keepalive reports active we leave the Stream alone even if the
        serve port is momentarily unbound: that only happens transiently (the
        ~tens-of-ms idle-release teardown race, or the inter-attempt reconnect
        gap), and the keepalive rebinds the same port, so HA's reconnect recovers
        on its own. Validated live: the port-down-while-active window self-resolved
        by the next 30s tick, so the ``stream_rtsp_url`` guard alone is correct -
        we deliberately do NOT try-bind the serve port on the common path (a probe
        bind() during a respawn gap could itself race ffmpeg's rebind).
        """
        stream = self.stream
        if stream is None:
            return
        if self.coordinator.device_client.stream_rtsp_url is not None:
            return  # keepalive still active - its serve will (re)bind; leave it
        # Keepalive has ended (idle-release / teardown): no ffmpeg is respawning,
        # so a one-off serve-port probe here is safe and confirms it's really dead.
        port = _serve_port(self._rtsp_name)
        listening = await self.hass.async_add_executor_job(
            self._serve_port_listening, port
        )
        _LOGGER.info(
            "Evicting stale cached stream for %s (keepalive ended; serve port "
            "%d listening=%s) so the next view restarts it",
            self._rtsp_name, port, listening,
        )
        try:
            await stream.stop()
        except Exception as exc:
            _LOGGER.debug(
                "stale-stream stop failed for %s: %s", self._rtsp_name, exc
            )
        if self.stream is stream:
            self.stream = None

    @callback
    def _on_motion_prewarm(self, _event: dict[str, Any]) -> None:
        """Schedule a keepalive start when a new motion clip is recorded.

        Motion events arrive ~30 s after recording begins. Starting the WebRTC
        session here means it is already warming when the user taps the
        notification 5-30 s later. start_keepalive() is idempotent: if a session
        is already active (stream_rtsp_url is not None) it returns immediately, so
        this guard skips the task entirely for the common case.
        """
        if self.coordinator.device_client.stream_rtsp_url is not None:
            return
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return
        self._prewarm_task = self.hass.async_create_task(self._prewarm_stream())

    def _connect_options(self) -> dict[str, Any]:
        """Resolve the streaming connection kwargs for start_keepalive.

        The connection mode maps to the library's relay-skip levers:
          - relay (default): keep the cloud TURN relay available and let ICE pick
            the path (fast_connect / skip_turn OFF) - app-parity, works on every
            topology (LAN, VLAN, remote, strict NAT).
          - lan_direct: skip the relay pre-alloc (fast_connect / skip_turn ON) -
            fastest on-LAN, but a camera not on the HA network can't connect."""
        opts = self.coordinator.config_entry.options if self.coordinator.config_entry else {}
        _mode = resolve_connection_mode(opts)
        lan_direct = _mode == CONNECTION_MODE_LAN_DIRECT
        # Stored values keep their historical names; the library speaks
        # auto/lan (its env-only "relay" mode is experimental and not offered
        # here - see the note beside CONNECTION_MODES).
        _lib_mode = "lan" if lan_direct else "auto"
        # Per-camera "Camera audio" switch overrides the global option when set.
        _audio_override = getattr(self.coordinator, "sdes_audio_override", None)
        _audio = (_audio_override if _audio_override is not None
                  else bool(opts.get(CONF_SDES_AUDIO, DEFAULT_SDES_AUDIO)))
        return {
            # Query-only go2rtc: the library needs a base URL to ask WHO IS
            # WATCHING, but must not register the stream - this integration
            # already registers it via _publish_to_go2rtc, and registering it
            # twice re-points the source mid-stream (see the note above).
            #
            # Withholding the URL entirely, which is what this used to do, is not
            # the safe option it looks like. In SDES push mode the serve port is
            # go2rtc's shared RTSP port, so the library's socket fallback is
            # unusable and the viewer check can only answer "unknown" - and
            # "unknown" never releases. That pinned battery cameras into a
            # keepalive that renewed forever against a camera nobody was
            # watching, draining them and leaving every first view landing on a
            # session that never delivers media.
            "go2rtc_url": _GO2RTC_API,
            "go2rtc_register": False,
            "fast_connect": lan_direct,
            "sdes_skip_turn": lan_direct,
            "sdes_connection_mode": _lib_mode,
            "sdes_audio": _audio,
            "sdes_audio_gain_db": float(opts.get(CONF_SDES_AUDIO_GAIN_DB, DEFAULT_SDES_AUDIO_GAIN_DB)),
            "sdes_fast_liveplay": bool(opts.get(CONF_SDES_FAST_LIVEPLAY, DEFAULT_SDES_FAST_LIVEPLAY)),
            "sdes_adaptive": bool(opts.get(CONF_SDES_ADAPTIVE, DEFAULT_SDES_ADAPTIVE)),
        }

    def _stream_idle_s(self) -> float | None:
        """Warm-hold window (s) for MAINS cameras so re-views are instant; None
        for battery cameras (keep motion-prewarm + default idle to save battery).

        Mains: the configured CONF_MAINS_IDLE_S, now defaulting to 0 = never
        release.  A cold wake on these cameras measures 16-22s, which is long
        enough that a live view reads as broken, and a mains camera has no battery
        to protect - so the warm hold is worth its cost.  Each warm mains camera
        does hold a concurrent-stream slot and keeps decrypting, which is why the
        library's stream cap is sized to the fleet.

        Battery cameras keep releasing: that is where dormancy actually saves
        something, and it is the case the idle-release work was really for."""
        dc = self.coordinator.device_client
        if getattr(dc, "is_battery_camera", False):
            return None
        opts = self.coordinator.config_entry.options if self.coordinator.config_entry else {}
        return float(opts.get(CONF_MAINS_IDLE_S, DEFAULT_MAINS_IDLE_S))

    # NOTE: go2rtc_url IS passed to start_keepalive, but with
    # go2rtc_register=False (library >= 0.13.1).
    #
    # It must not switch on the library's own _register_with_go2rtc(), which PUTs
    # the same stream this integration already registers via _publish_to_go2rtc -
    # a duplicate registration that re-points the source while a stream is
    # mid-flight. On this fleet that left the DTLS cameras producing nothing at
    # all, while the version without it served all four.
    #
    # The previous fix was to withhold go2rtc_url altogether, on the reasoning
    # that viewer detection no longer mattered: mains cameras never release
    # (mains_idle_s defaults to 0) and battery cameras "fall back to the socket
    # check". That last part is false in SDES PUSH mode - the serve port is
    # go2rtc's shared RTSP port, so the library refuses the socket check and
    # answers "unknown", and unknown never releases. Battery cameras were
    # therefore pinned into a keepalive that renewed forever against a camera
    # nobody was watching. go2rtc_register=False is the seam that fixes it: keep
    # the viewer query, skip the registration.
    async def _prewarm_stream(self) -> None:
        """Start the keepalive in the background; called from _on_motion_prewarm."""
        dc = self.coordinator.device_client
        # Never open a session against a camera the cloud says is offline. The
        # open cannot succeed, and the keepalive loop parks in its retry pause
        # with streaming latched on - which keeps a renew POST going every 20s and
        # an HTTP wake every 10 minutes at a camera nobody can view. On the
        # battery model that was seen draining a device already down to 5%.
        if not getattr(getattr(dc, "status", None), "online", True):
            _LOGGER.debug("Skipping prewarm for %s: camera is offline",
                          self._rtsp_name)
            return
        try:
            await dc.start_keepalive(
                rtsp_push_url=(self._push_serve_url if self._sdes_push_enabled()
                               else self._serve_url),
                stream_idle_s=self._stream_idle_s(),
                **self._connect_options(),
            )
            _LOGGER.debug("Motion prewarm started for %s", self._rtsp_name)
            # A session is on its way for some other reason, so listing the
            # card costs one AVIO round trip instead of a 15-70 s handshake.
            # It is not up yet - start_keepalive schedules the handshake and
            # returns - so the refresh waits for it to land before it lists.
            # Backgrounded and never awaited, both because that wait is long
            # and because a listing must not be able to delay or fail a stream
            # a viewer is waiting on.
            self.hass.async_create_task(
                self.coordinator.async_piggyback_sd_refresh()
            )
        except Exception as exc:
            _LOGGER.debug("Motion prewarm failed for %s: %s", self._rtsp_name, exc)

    async def _startup_prewarm(self) -> None:
        """Warm the WebRTC session in the background shortly after setup.

        Mains cameras only - a warm session would drain a battery camera. The
        per-camera staggered delay spreads the handshakes (the library also
        serialises them via its global open-gate); because this runs with no
        live-view in progress there is no 30s view-clock, so a slow cold open
        just takes longer to warm rather than surfacing as a failed view. Reuses
        the idempotent ``_prewarm_stream`` (a no-op if a session is already up)."""
        opts = self.coordinator.config_entry.options if self.coordinator.config_entry else {}
        if not opts.get(CONF_STARTUP_PREWARM, DEFAULT_STARTUP_PREWARM):
            return
        dc = self.coordinator.device_client
        if getattr(dc, "is_battery_camera", False):
            return
        if not getattr(getattr(dc, "status", None), "online", True):
            return
        delay = (_serve_port(self._rtsp_name) % 8) * _STARTUP_PREWARM_STAGGER_S
        await asyncio.sleep(delay)
        await self._prewarm_stream()

    async def _prefetch_thumbnail(self) -> None:
        try:
            url = await self.coordinator.device_client.async_get_latest_thumbnail()
        except Exception:
            return
        if not url:
            return
        # Fetch bytes first so the HA camera proxy can serve them immediately
        # on first request.  We do NOT expose the raw CDN URL as entity_picture
        # because CDN URLs expire and produce a broken icon.  Instead we let HA
        # return its own proxy URL (ENTITY_IMAGE_URL -> async_camera_image()).
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    async with self._image_lock:
                        self._cached_image = data
                        self._cached_image_ts = time.monotonic()
                    # Notify HA that a fresh image is available via the proxy.
                    self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.debug("Thumbnail bytes fetch failed for %s: %s", self.unique_id, exc)

    @property
    def _serve_url(self) -> str:
        """Local HTTP-serve URL for this camera's stream (deterministic port)."""
        return f"http://127.0.0.1:{_serve_port(self._rtsp_name)}/{self._rtsp_name}.ts"

    @property
    def _go2rtc_name(self) -> str:
        """go2rtc stream name for this camera.

        Matches the library's scheme (``aidot_{device_id[:12]}``) so a stream
        registered here and the library's own go2rtc path refer to the same one.
        """
        return f"aidot_{self.coordinator.device_client.device_id[:12]}"

    def _sdes_push_enabled(self) -> bool:
        """True when this SDES camera should PUBLISH into go2rtc (push mode).

        Push avoids the pull chain's single-slot -listen/relay timing races
        that stall SDES cold starts under HA (see CONF_SDES_PUSH). Requires
        go2rtc; DTLS cameras keep the proven pull serve.
        """
        dc = self.coordinator.device_client
        if not getattr(dc, "is_sdes_camera", False) or not _GO2RTC_ENABLED:
            return False
        opts = self.coordinator.config_entry.options if self.coordinator.config_entry else {}
        return bool(opts.get(CONF_SDES_PUSH, DEFAULT_SDES_PUSH))

    @property
    def _push_serve_url(self) -> str:
        """go2rtc RTSP URL this camera publishes to (and viewers consume).

        go2rtc accepts RTSP publish on a stream name and fans it out to every
        consumer, so the publish URL and the viewer URL are the same.
        """
        client = Go2rtcClient(async_get_clientsession(self.hass), _GO2RTC_API)
        return client.rtsp_url(self._go2rtc_name)

    async def _go2rtc_publisher_state(self, push: bool) -> str:
        """Whether go2rtc still has a live publisher: live / absent / unknown.

        ``"unknown"`` means go2rtc could not be reached, and is deliberately
        distinct from ``"live"``: callers must not restart a healthy session on
        it (that thrashes), but they must not treat it as proof of a publisher
        either (see ``stream_source``, which skips the re-registration PUT only
        on a positive ``"live"``).

        Only push mode can be checked this way, and only push mode needs it: in
        pull mode the registered HTTP serve URL *is* the source, and a dead serve
        is already handled by ``_evict_stale_stream``. In push mode that URL is
        an inert placeholder and the real publisher appears as an extra producer
        carrying no ``url``, which is exactly what this looks for.
        """
        if not push or not _GO2RTC_ENABLED:
            return "live"
        client = Go2rtcClient(
            async_get_clientsession(self.hass), _GO2RTC_API, timeout=2.0
        )
        streams = await client.list_streams()
        if not streams:
            return "unknown"  # go2rtc unreachable - do not thrash the session
        entry = streams.get(self._go2rtc_name)
        if not isinstance(entry, dict):
            return "absent"  # stream definition is gone; re-register it
        producers = entry.get("producers") or []
        if any(not p.get("url") for p in producers if isinstance(p, dict)):
            return "live"
        return "absent"

    async def _publisher_is_attached(self, push: bool) -> bool:
        """True when go2rtc still has a live publisher for this camera.

        ``stream_rtsp_url`` is set when a session starts and is **not** cleared
        when the publisher dies underneath it. Measured on the box: the SDES
        bridge's ffmpeg exits ("ffmpeg exited with code 255 - stream ended")
        while the URL stays set, so every later view took the warm-session fast
        path, skipped ``start_keepalive`` entirely, and handed HA a URL for a
        go2rtc stream nobody was publishing to. go2rtc answered DESCRIBE with
        404, and it stayed that way until Home Assistant was restarted - which is
        why battery cameras (never prewarmed) looked permanently blank and the
        mains SDES PTZ died the moment its prewarm session ended.

        Only push mode can be checked this way, and only push mode needs it: in
        pull mode the registered HTTP serve URL *is* the source, and a dead serve
        is already handled by ``_evict_stale_stream``. In push mode that URL is
        an inert placeholder and the real publisher appears as an extra producer
        carrying no ``url``, which is exactly what this looks for.

        Fails safe: if go2rtc cannot be reached we answer True (keep the existing
        fast path) rather than restarting a healthy session on every view.
        """
        return await self._go2rtc_publisher_state(push) != "absent"

    def _soft_fail_url(self, push: bool, serve_url: str) -> str | None:
        """What to return when this camera cannot be served right now.

        Never ``None`` once we have a plausible URL. Returning a falsy
        ``stream_source()`` is not the gentle "show the still image and retry"
        it reads as: Home Assistant's go2rtc provider does

            if not (stream_source := await camera.stream_source()):
                await self.teardown()
                raise HomeAssistantError("Camera has no stream source")

        and ``teardown()`` closes **every session the provider holds** - i.e.
        every other camera's live view, not just this one (verified in HA
        2026.7.4, the deployed version). One camera failing to open must not
        blank the dashboard.

        Handing back a URL that may 404 keeps the failure local: HA registers it
        in its own go2rtc, that single offer errors, and other cameras are
        untouched. The scheme still has to be one go2rtc supports or the
        provider takes the same teardown path, so only ever return the RTSP
        push URL or the local serve URL - both are supported schemes.
        """
        return serve_url if push else self._serve_url

    async def _publish_to_go2rtc(self, serve_url: str) -> str | None:
        """Register ``serve_url`` with HA's go2rtc and return its RTSP pull URL.

        This is what makes HA serve the camera over WebRTC instead of HLS: HA's
        go2rtc provider only enables WebRTC when ``stream_source`` is a
        go2rtc-supported URL (RTSP), so handing it the go2rtc RTSP URL flips the
        camera from the slow HLS pipeline to sub-second WebRTC. go2rtc pulls our
        single-consumer ``-listen`` serve once and fans out to all viewers (so no
        ``-listen 1`` conflict). Registered eagerly: go2rtc accepts the stream def
        before the serve is bound and pulls lazily when a viewer connects, so the
        camera advertises WebRTC even on a cold first view. Re-registers on every
        call (the PUT is idempotent and cheap) so it self-heals if go2rtc restarts
        and drops the stream def. Best-effort via the library's ``Go2rtcClient``:
        returns ``None`` (-> caller falls back to the HLS serve) if go2rtc is
        unreachable. No separate availability probe: ``ensure_stream``'s PUT
        fast-fails when go2rtc is down, keeping HA's per-camera setup check fast.
        """
        if not _GO2RTC_ENABLED:
            return None
        # Short timeout: go2rtc is loopback, and this runs in HA's per-camera
        # setup provider check - don't let a down go2rtc stall startup.
        client = Go2rtcClient(async_get_clientsession(self.hass), _GO2RTC_API, timeout=3.0)
        name = self._go2rtc_name
        # A camera that changes its H.264 parameter sets between sessions has to
        # get a FRESH stream definition, or go2rtc keeps serving the decoder
        # configuration (the fMP4 avcC box) it built when the track was first
        # published. A player using Media Source Extensions configures itself from
        # that box once, so a stale one leaves it unable to decode inter-frames and
        # the picture updates only on keyframes. Measured on an A001064: avcC held
        # an SPS from an earlier session, while a freshly created definition
        # matched the stream exactly and stayed stable for the whole session.
        #
        # A PUT alone does not rebuild the track, so drop the definition first.
        # Order matters: this runs at stream_source() time, BEFORE the keepalive
        # publishes, so the publisher never loses its target. Removing a
        # definition a publisher is already attached to leaves it with nowhere to
        # publish and the camera stops streaming entirely.
        if _sprop_is_unstable(self.coordinator.device_client.device_id):
            await client.remove_stream(name)
            _LOGGER.debug(
                "%s changes its parameter sets between sessions - recreating its "
                "go2rtc stream so the decoder config is rebuilt", self._rtsp_name,
            )
        if not await client.ensure_stream(name, serve_url):
            return None
        rtsp = client.rtsp_url(name)
        _LOGGER.debug("Registered %s with go2rtc -> %s (WebRTC)", self._rtsp_name, rtsp)
        return rtsp

    async def _unpublish_from_go2rtc(self) -> None:
        """Remove this camera's go2rtc stream (best-effort, on teardown).

        Runs as a detached task after the entity is removed, so guard ``hass`` and
        swallow everything - the entity is going away and this is only hygiene.
        """
        if not _GO2RTC_ENABLED or self.hass is None:
            return
        try:
            client = Go2rtcClient(async_get_clientsession(self.hass), _GO2RTC_API)
            await client.remove_stream(self._go2rtc_name)
        except Exception:  # best-effort teardown hygiene
            pass

    async def stream_source(self) -> str | None:
        """Serve a pullable HTTP stream for all camera models (go2rtc pulls it).

        Both paths serve H.264 over a local HTTP-listen socket that HA's stream
        integration / go2rtc pull the standard way (no go2rtc pre-registration):

        - SDES (A001064/A001513): ffmpeg receives the decrypted SRTP directly.
          First call takes 25-70s while the SCTP handshake runs.
        - DTLS (A000088): aiortc does ICE/DTLS/decrypt and the library taps the
          encoded H.264 and -c copy's it to the same serve (no decode/re-encode).

        Subsequent viewers reuse the warm session.
        """
        # HA calls stream_source() once per camera during entity setup (for
        # go2rtc pre-registration), before async_added_to_hass has run.
        # Starting a WebRTC keepalive here blocks setup for 7-14s per camera
        # serialised by HA's platform loader - 63s total for 6 cameras.
        # Return None so setup completes immediately; HA re-calls stream_source
        # on the first actual live-view request, at which point we start normally.
        if not self._setup_complete:
            # HA decides WebRTC support here (async_refresh_providers runs during
            # entity add, before async_added_to_hass) by calling stream_source for
            # its scheme. Returning None made it conclude HLS-only and cache that,
            # so these cameras never used go2rtc WebRTC (slow HLS vs the app's
            # instant WebRTC). Instead register the go2rtc stream def and return
            # its RTSP URL: HA's go2rtc provider sees RTSP -> enables WebRTC. No
            # keepalive is started here (go2rtc pulls lazily when a viewer
            # connects; the real view starts the serve), so setup stays fast.
            registered = await self._publish_to_go2rtc(self._serve_url)
            if registered:
                return registered
            # go2rtc could not be reached. Returning None here used to end the
            # story, and it left the camera with NO stream at all - measured on
            # the box: with no reachable go2rtc every camera dropped from
            # ['hls','web_rtc'] to ['hls'] and HLS then served nothing.
            #
            # Registration needs a TCP go2rtc API, but Home Assistant's own
            # bundled go2rtc listens on a unix socket, so on an install with no
            # second instance this branch is the normal case, not an edge case.
            # Hand HA the local serve URL and let it decide: if its go2rtc
            # accepts an http source it registers that itself and still serves
            # WebRTC; if not, HA's HLS pipeline reads the same URL. Either way
            # the camera has a stream, and neither outcome depends on what that
            # go2rtc supports.
            #
            # In SDES push mode nothing ever binds the local serve (the media is
            # published into go2rtc instead), so the local serve URL is not the
            # right answer there - but None is not either, because HA's provider
            # turns a falsy stream_source into a teardown of every camera's
            # session. Hand back the go2rtc RTSP push URL: the same URL a working
            # push-mode view returns, so at worst this camera's own offer errors.
            if self._sdes_push_enabled():
                return self._soft_fail_url(True, self._push_serve_url)
            return self._serve_url
        dc = self.coordinator.device_client

        _push = self._sdes_push_enabled()
        serve_url = self._push_serve_url if _push else self._serve_url
        connected = False
        cancelled = False
        self._set_stream_status("Connecting...")
        try:
            # live / absent / unknown. A never-started session is "absent"
            # without asking go2rtc; "unknown" (go2rtc unreachable) keeps the
            # warm fast path rather than restarting a session that may be fine.
            pub_state = (
                "absent" if dc.stream_rtsp_url is None
                else await self._go2rtc_publisher_state(_push)
            )
            if pub_state == "absent":
                # Never open a session against a camera the cloud says is
                # offline - the same pre-check the two prewarm paths already
                # make. This is the fourth door: go2rtc pulls lazily when a
                # viewer connects and never consults HA's `available`, so the
                # entity being unavailable does not stop the open happening
                # here. The open cannot succeed, and it is not cheap to fail:
                # the library serialises every handshake through its global
                # open-gate and holds it for the whole no-answer timeout (45s
                # observed), so one dead camera delays every other camera's
                # cold open. Fleet evidence: an A000088 whose cloud record has
                # said offline since 2026-06-27 still timed out on 8 of 8
                # attempts across four runs.
                #
                # Reads the flag with a default of True, like the other guards:
                # only a positive False refuses. And it sits here rather than at
                # the top of the method, so a camera already serving media keeps
                # being served no matter what the flag claims - a flag that is
                # stale the other way (offline while the camera is reachable)
                # can therefore cost a cold open, never a live view.
                if not getattr(getattr(dc, "status", None), "online", True):
                    _LOGGER.debug(
                        "Not opening a stream for %s: camera is offline",
                        self._rtsp_name,
                    )
                    return self._soft_fail_url(_push, serve_url)
                try:
                    _opts = self.coordinator.config_entry.options if self.coordinator.config_entry else {}
                    await dc.start_keepalive(
                        rtsp_push_url=serve_url,
                        stream_idle_s=self._stream_idle_s(),
                        **self._connect_options(),
                    )
                    _LOGGER.info(
                        "Started HTTP stream serve for %s -> %s (connection mode: %s)",
                        self._rtsp_name, serve_url, resolve_connection_mode(_opts),
                    )
                except Exception as exc:
                    # Deliberately NOT `return None` - see _soft_fail_url().
                    _LOGGER.warning(
                        "Failed to start stream serve for %s: %s",
                        self._rtsp_name, exc,
                    )
                    return self._soft_fail_url(_push, serve_url)

            # Register the serve with HA's go2rtc so HA serves this camera over
            # WebRTC (sub-second) instead of the slow HLS pipeline; HA's go2rtc
            # provider only enables WebRTC for a go2rtc-supported (RTSP) source.
            # Falls back to the local HLS serve when go2rtc is unavailable.
            # go2rtc stream registration. go2rtc only creates a stream that has
            # a source, and rejects RTSP publish to unknown names - so BOTH modes
            # register the legacy HTTP serve URL. In pull mode it is the live
            # source; in push mode nothing listens on it (an inert placeholder:
            # a consumer attach costs one instantly-refused dial) and the
            # keepalive's RTSP publish feeds the stream instead - go2rtc serves
            # the published tracks to every consumer (validated live).
            #
            # ...but NOT when a publisher is already attached. go2rtc's
            # `PUT /api/streams` REPLACES the stream definition wholesale, and
            # that drops the live RTSP publisher from the stream - even when the
            # `src` is byte-identical to what is already registered. The
            # publishing ffmpeg keeps running and keeps pushing, so nothing looks
            # wrong from our side, but the stream is left holding only the dead
            # placeholder and every consumer gets 404. Since HA calls
            # stream_source() on every WebRTC offer, the unconditional re-PUT
            # meant each SDES view killed the publisher the previous view had
            # established. Reproduced against go2rtc 1.9.9: publish, consume OK,
            # re-PUT the same src, consume -> "DESCRIBE failed: 404".
            # A confirmed-live publisher also proves the stream definition
            # exists, so there is nothing for the self-healing PUT to repair.
            if _push and pub_state == "live":
                registered = serve_url
            else:
                registered = await self._publish_to_go2rtc(self._serve_url)
            if _push:
                if registered is None:
                    # go2rtc is unreachable: push has nowhere to publish and the
                    # pull fallback has no listener in push mode. Hand back the
                    # URL anyway rather than None - see _soft_fail_url().
                    return self._soft_fail_url(_push, serve_url)
                publish_url = serve_url  # the go2rtc RTSP URL (same one we push to)
            else:
                publish_url = registered or serve_url

            # Hand HA the URL only once the serve's HTTP-listen socket is actually
            # bound, so go2rtc connects on its first try.  Otherwise go2rtc gets
            # "connection refused" and HA surfaces it as a hard stream error
            # (and waits out a ~40s reconnect).  On timeout we return None instead:
            # HA shows the cached still image and retries while the keepalive keeps
            # warming the session in the background - a soft failure, no error toast.
            if getattr(dc, "is_sdes_camera", False):
                # SDES binds its -listen serve socket only after the SCTP handshake
                # + first media demux (a 25-70s cold start) - far past HA's 10s
                # stream_source timeout.  Blocking for the bind (the old behaviour)
                # gets cancelled at 10s and returns None -> snapshot with no retry,
                # which is the live-view failure users hit on a cold camera.  So
                # mirror the proven DTLS path: probe briefly with a try-bind (a warm
                # session returns instantly; the probe never connects, so it can't
                # steal the serve's single -listen slot) and otherwise hand HA the
                # URL anyway.  HA's stream_worker tolerates the transient
                # "connection refused" and retries until the serve binds, so the
                # first view becomes a spinner that resolves to video instead of a
                # still image.  The serve is verified to produce valid H.264 once
                # bound, so the worker connects to a good stream.
                self._set_stream_status("Buffering...")
                if _push:
                    # Push mode: there is no local -listen port, so wait on the
                    # thing that actually decides whether the URL works - the
                    # publisher reaching go2rtc. Handing HA the URL before then
                    # cannot succeed: a consumer on a stream whose only producer
                    # is the inert placeholder gets 404, and the go2rtc provider
                    # turns that into a WebRTCError with no retry.
                    #
                    # go2rtc serves a consumer from the moment the publisher's
                    # RTSP ANNOUNCE lands - it does not need buffered media
                    # first (verified against go2rtc 1.9.9: a consumer attaching
                    # 0.5s after the publisher started got h264 1280x720). So
                    # "publisher attached" is exactly the right thing to wait
                    # for, and no longer.
                    # ONE budget for everything below, not one per wait. The
                    # fallback used to add its own 7s on top, so a cold camera
                    # spent 8.5 + 7 = 15.5s here and blew HA's 10s HLS timeout
                    # anyway - which is the failure this budget exists to avoid.
                    #
                    # The budget is SPLIT rather than spent first-come: letting
                    # the publisher poll consume all of it leaves nothing for the
                    # readiness fallback, which silently disables it on exactly
                    # the cold opens it exists for.
                    _deadline = time.monotonic() + _PUSH_PUBLISHER_WAIT_S
                    attached = pub_state == "live" or await self._await_publisher_attached(
                        max(0.5, _PUSH_PUBLISHER_WAIT_S - _SERVE_READY_RESERVE_S)
                    )
                    if not attached:
                        # go2rtc never showed a publisher - either the open is
                        # slower than the budget or go2rtc cannot be read. Fall
                        # back to the library's own readiness signal, with only
                        # whatever is left of the budget, and hand HA the URL
                        # regardless: a URL that may 404 still beats None, which
                        # makes HA's provider tear down every camera's session,
                        # not just this one.
                        _left = _deadline - time.monotonic()
                        waiter = getattr(dc, "async_wait_serve_ready", None)
                        if waiter is not None and _left > 0.5:
                            try:
                                await waiter(timeout=_left)
                            except Exception:
                                pass
                        _LOGGER.debug(
                            "%s: no publisher within %.1fs - handing HA the URL "
                            "while the keepalive keeps warming the session",
                            self._rtsp_name, _PUSH_PUBLISHER_WAIT_S,
                        )
                else:
                    await self._await_serve_listening(_serve_port(self._rtsp_name), timeout=7.0)
                connected = True
                return publish_url

            # DTLS: the library signals readiness once its mux feeds ffmpeg, but a
            # cold session needs ~15-21s (ICE/DTLS + first keyframe + ffmpeg bind)
            # - far past HA's 10s CAMERA_STREAM_SOURCE_TIMEOUT.  Blocking here for
            # readiness therefore *guarantees* the first press fails (HA cancels
            # stream_source at 10s -> CancelledError).  So instead return the URL
            # fast: a warm session is already bound (the short wait returns ready
            # instantly), and a cold one hands HA the URL before the port is up.
            # HA's stream_worker tolerates the transient "connection refused" - it
            # retries with backoff (STREAM_RESTART_INCREMENT, no give-up) and
            # connects once ffmpeg binds (~15-21s) - so the first press becomes a
            # spinner that resolves to video instead of a hard "not responding".
            # The library serve (keyframe gating) is untouched: the worker only
            # connects once ffmpeg has bound *with* a keyframe ready, so there's no
            # garbage / Immediate-exit.  (This differs from the rejected serve-ready
            # trim - that removed the library-side gating itself; this keeps it.)
            waiter = getattr(dc, "async_wait_serve_ready", None)
            if waiter is not None:
                self._set_stream_status("Negotiating...")
                try:
                    # Warm -> returns ready instantly; cold -> falls through after the
                    # short wait and we hand HA the URL anyway (under the 10s cutoff).
                    await waiter(timeout=7.0)
                except Exception:
                    pass

            connected = True
            return publish_url
        except asyncio.CancelledError:
            # Normal when the user closes the live view - not a failure.
            cancelled = True
            raise
        finally:
            if connected or cancelled:
                # Stream took over (or a clean close) - drop the overlay.
                self._set_stream_status(None)
            elif self._stream_status is None or not self._stream_status[1]:
                # Any non-success exit (timeout, exception) that didn't already
                # set an error: surface a soft "retrying" status attribute.
                self._set_stream_status(
                    "Camera unavailable - retrying...", error=True
                )

    @staticmethod
    def _serve_port_listening(port: int) -> bool:
        """True if something is already listening on 127.0.0.1:``port``.

        Uses a try-bind probe: a clean bind means the port is free (nothing
        listening yet); ``EADDRINUSE`` means ffmpeg's ``-listen`` serve socket
        holds it.  We never ``connect()`` - the SDES serve is ``-listen 1`` (a
        single accept slot) and a probe connection would consume the one slot
        go2rtc needs.  Mirrors the library's UDP ``_udp_port_bound`` check.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return False  # bound cleanly -> port free -> not listening yet
        except OSError:
            return True  # EADDRINUSE -> ffmpeg is listening
        finally:
            s.close()

    async def _await_publisher_attached(self, timeout: float) -> bool:
        """Poll go2rtc until this camera's RTSP publisher attaches, or give up.

        Returns True as soon as go2rtc reports a producer with no ``url`` (the
        publisher); False on timeout, and False immediately if go2rtc cannot be
        read at all - there is nothing to wait for in that case, and blocking on
        an unreachable go2rtc would just burn the caller's budget.

        Polling rather than an event because the publisher attaches inside
        go2rtc, out of this process: the library knows when it *started* ffmpeg,
        not when go2rtc accepted the ANNOUNCE, and it is the latter that decides
        whether the URL we are about to hand HA resolves or 404s.
        """
        deadline = time.monotonic() + timeout
        while True:
            state = await self._go2rtc_publisher_state(push=True)
            if state == "live":
                return True
            if state == "unknown":
                return False  # go2rtc unreadable - waiting cannot resolve it
            if time.monotonic() >= deadline:
                _LOGGER.debug(
                    "No go2rtc publisher for %s after %.0fs - handing HA the URL "
                    "anyway", self._rtsp_name, timeout,
                )
                return False
            await asyncio.sleep(_PUSH_PUBLISHER_POLL_S)

    async def _await_serve_listening(self, port: int, timeout: float) -> bool:
        """Poll until ``port`` is listening, or ``timeout`` elapses.

        Probe-first so a warm session (already bound) returns immediately;
        sleep only between misses.
        """
        deadline = time.monotonic() + timeout
        while True:
            if await self.hass.async_add_executor_job(self._serve_port_listening, port):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ #
    # Snapshot status overlay (connecting / error)
    # ------------------------------------------------------------------ #
    # How long an error message stays overlaid on the snapshot before the card
    # falls back to the plain still image again.
    _STATUS_ERROR_TTL = 60.0

    def _set_stream_status(self, text: str | None, *, error: bool = False) -> None:
        """Set (or clear, with ``None``) the connection status and push it.

        ``async_write_ha_state()`` immediately propagates the ``stream_status``
        attribute to the frontend, so a card bound to it reflects the phase live.
        """
        self._stream_status = None if text is None else (text, error, time.monotonic())
        try:
            self.async_write_ha_state()
        except Exception:
            pass

    def _active_status(self) -> str | None:
        """Return the status text to show now, or None.  Expires error text."""
        st = self._stream_status
        if st is None:
            return None
        text, is_error, ts = st
        if is_error and (time.monotonic() - ts) > self._STATUS_ERROR_TTL:
            self._stream_status = None
            return None
        return text

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose the live connection status so a card can render it instantly.

        Unlike a snapshot overlay (which the expanded live-view player never
        shows, and cards only poll), this attribute is pushed to the frontend on
        every ``async_write_ha_state()`` - so a template/badge bound to
        ``stream_status`` updates the moment the phase changes.
        """
        status = self._active_status()
        if status is None:
            return None
        return {"stream_status": status}

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a snapshot for the camera entity icon/card.

        Returns live JPEG frames while the stream is active (latest_jpeg set by the
        streaming loop), otherwise serves the cached cloud event thumbnail.  The
        cloud API is only re-queried once every 5 minutes to avoid rate-limiting.
        Connection status (connecting/buffering/failed) is surfaced via the
        ``stream_status`` state attribute, not drawn onto the snapshot.
        """
        live = self.coordinator.device_client.latest_jpeg
        if live is not None:
            return live
        return await self._base_snapshot()

    async def _base_snapshot(self) -> bytes | None:
        """Return the cached/cloud snapshot bytes (no live frames)."""
        async with self._image_lock:
            if (time.monotonic() - self._cached_image_ts) < 300:
                return self._cached_image
            # Stamp now so concurrent callers skip the refresh; the real image
            # arrives below and is written back under the lock.
            self._cached_image_ts = time.monotonic()
            cached = self._cached_image

        try:
            url = await self.coordinator.device_client.async_get_latest_thumbnail()
        except Exception:
            return cached

        if not url:
            return cached

        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    async with self._image_lock:
                        self._cached_image = data
                    return data
        except Exception as exc:
            _LOGGER.debug("Thumbnail fetch failed for %s: %s", self.unique_id, exc)

        return self._cached_image

    # ------------------------------------------------------------------ #
    # Two-way audio (push-to-talk / announce)
    # ------------------------------------------------------------------ #
    async def async_talk(self, media: str, max_seconds: int = 30) -> None:
        """Play an audio source through the camera speaker (``aidot.talk`` service).

        ``media`` may be a Home Assistant media-source id or an http(s) URL.
        It is transcoded to 8 kHz mono PCM and streamed to the camera over the
        existing WebRTC audio channel (DTLS and SDES cameras alike).
        """
        source = await self._resolve_media(media)
        pcm = await self._decode_pcm_8k(source, max_seconds=max_seconds)
        if not pcm:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="talk_no_audio",
                translation_placeholders={"media": media},
            )

        n = TALK_PCM_FRAME_BYTES
        frames = (pcm[i:i + n].ljust(n, b"\x00") for i in range(0, len(pcm), n))

        def _provider() -> bytes | None:
            return next(frames, None)

        ok = await self.coordinator.device_client.async_speak(
            _provider, max_seconds=max_seconds
        )
        if not ok:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="talk_session_failed",
            )

    async def async_ptz(self, direction: str, speed: int = 4) -> None:
        """Move the camera (``aidot.ptz`` service).

        ``direction`` is one of :data:`PTZ_DIRECTIONS`; ``speed`` is 1-8.
        PTZ commands ride the active stream session, so the camera must be
        streaming (open the live view first). ``stop`` halts continuous motion.
        """
        ok = await self.coordinator.device_client.async_ptz_move(direction, speed)
        if not ok:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ptz_failed",
                translation_placeholders={"direction": direction},
            )

    async def _resolve_media(self, media: str) -> str:
        """Resolve a HA media-source id to a playable URL; pass URLs/paths through."""
        from homeassistant.components import media_source

        if media_source.is_media_source_id(media):
            item = await media_source.async_resolve_media(
                self.hass, media, self.entity_id
            )
            from homeassistant.components.media_player.browse_media import (
                async_process_play_media_url,
            )

            return async_process_play_media_url(self.hass, item.url)
        return media

    async def _decode_pcm_8k(self, source: str, max_seconds: int = 35) -> bytes:
        """Transcode any audio source to raw s16le 8 kHz mono PCM via ffmpeg."""
        from urllib.parse import urlparse
        scheme = urlparse(source).scheme.lower()
        if scheme not in ("http", "https"):
            _LOGGER.warning("aidot.talk: rejecting media source with scheme %r", scheme)
            return b""
        from .proxy import get_ffmpeg_binary
        binary = get_ffmpeg_binary(self.hass)
        proc = await asyncio.create_subprocess_exec(
            binary, "-nostdin",
            "-protocol_whitelist", "http,https,tcp,tls",
            "-t", str(max_seconds),
            "-i", source,
            "-f", "s16le", "-ar", str(TALK_PCM_RATE), "-ac", "1", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            pcm, _ = await asyncio.wait_for(proc.communicate(), timeout=max_seconds + 5)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            pcm = b""
        return pcm
