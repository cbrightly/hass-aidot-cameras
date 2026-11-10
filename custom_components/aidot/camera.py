"""Support for Aidot cameras."""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
import datetime
import logging
from collections.abc import Mapping
from typing import Any
import os
import socket
import time
from datetime import timedelta

import aiohttp
import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError, HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from aidot_cameras.camera.constants import TALK_PCM_FRAME_BYTES, TALK_PCM_RATE
from aidot_cameras.camera.go2rtc import (
    DEFAULT_BASE_URL,
    DEFAULT_RTSP_PORT,
    EnsureOutcome,
    async_ensure_stream,
    async_remove_stream,
)

from aidot_cameras.camera.protocol import sprop_is_unstable as _sprop_is_unstable

from .const import (
    BATTERY_IDLE_S,
    CONNECTION_MODE_LAN_DIRECT,
    CONF_MAINS_IDLE_S,
    CONF_MOTION_PREWARM,
    CONF_STARTUP_PREWARM,
    CONF_SDES_AUDIO_GAIN_DB,
    CONF_SDES_FAST_LIVEPLAY,
    CONF_SERVE_PORT_BASE,
    DEFAULT_MAINS_IDLE_S,
    DEFAULT_MOTION_PREWARM,
    DEFAULT_STARTUP_PREWARM,
    DEFAULT_SDES_AUDIO_GAIN_DB,
    DEFAULT_SDES_FAST_LIVEPLAY,
    DEFAULT_SERVE_PORT_BASE,
    DOMAIN,
    resolve_connection_mode,
)
from . import library_settings, serve_ports
from .coordinator import SLOT_SD, AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import (
    aidot_device_info,
    async_add_when_discovered,
    pending_coordinators,
)
from .go2rtc_target import (
    KIND_EXPLICIT,
    SIGNAL_GO2RTC_HA_READS,
    Go2rtcTarget,
    invalidate as invalidate_go2rtc_target,
    log_unanswered as log_go2rtc_unanswered,
    note_sdes_no_go2rtc,
    resolve_go2rtc_target,
)

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


# Local serve: a camera that is pulled rather than published (a standard DTLS
# camera when there is nothing to publish into, or its direct publish is off)
# serves its stream over a local HTTP-listen socket that go2rtc or HA's stream
# integration pulls. SDES cameras only publish. Each camera gets a stable local
# port so the URL is deterministic: a persisted, collision-free slot from
# serve_ports (its crc32 slot when free). The base is the entry's option, else
# AIDOT_SERVE_PORT_BASE, else the default; read, never written.
def _serve_port(name: str, base: int | None = None) -> int:
    """Stable loopback HTTP-serve port for a camera (base..base+399)."""
    if base is None:
        base = int(os.environ.get("AIDOT_SERVE_PORT_BASE", DEFAULT_SERVE_PORT_BASE))
    return base + serve_ports.slot_for(name)


def _rtsp_name(dev_id: str) -> str:
    """Sanitised device id safe to use as a stream name (and serve-port key)."""
    return dev_id.replace("/", "_").replace(":", "_")


# A go2rtc serves cameras over WebRTC (sub-second) instead of HLS. When there
# is a go2rtc to publish into - an explicit AIDOT_GO2RTC_API or an add-on on
# 127.0.0.1:1984, never HA's bundled server, as decided by
# go2rtc_target.resolve_go2rtc_target - the camera's stream is defined there
# (via the library's Go2rtcClient) and HA is handed that go2rtc's RTSP URL; it
# fans the single stream out to every viewer. With no such target a DTLS
# camera hands HA its local serve, which HA's own go2rtc pulls, and an SDES
# camera is not streamed.
#
# _GO2RTC_API is only the URL the library is given to ask who is watching (see
# _connect_options); every call this module makes goes to the resolved target.
_GO2RTC_API = os.environ.get("AIDOT_GO2RTC_API", DEFAULT_BASE_URL)
_GO2RTC_ENABLED = os.environ.get("AIDOT_GO2RTC", "1") != "0"
#: How long an explicit go2rtc's reachability answer is reused across cameras.
_GO2RTC_REACHABLE_TTL_S = 60.0
# How long an SDES battery camera's direct "does the add-on answer?" check is
# reused: long enough that one view asks once, short enough that the next
# view asks again (see _sdes_has_no_go2rtc).
_ADDON_DIRECT_CHECK_TTL_S = 5.0
# The longest an open waits for a pending stop of a publishing session (see
# _await_pending_stop). The WebRTC path has no timeout above it.
_PENDING_STOP_WAIT_S = 15.0

_GO2RTC_REJECTS_ISSUE = "go2rtc_rejects_stream_updates"


def _note_go2rtc_rejection(hass: HomeAssistant, name: str, *, rejected: bool) -> None:
    """Raise (or clear) the repair issue for a go2rtc that rejects every PUT.

    A duplicate key in go2rtc's config makes it answer every registration with
    400 while the streams stay registered: live view keeps working from what
    go2rtc holds, and the only sign was a log line. The issue names it in
    Settings; a registration that succeeds removes it. Best-effort: nothing
    here may fail a registration.
    """
    from homeassistant.helpers import issue_registry as ir

    try:
        if rejected:
            ir.async_create_issue(
                hass,
                DOMAIN,
                _GO2RTC_REJECTS_ISSUE,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_GO2RTC_REJECTS_ISSUE,
                translation_placeholders={"stream": name},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, _GO2RTC_REJECTS_ISSUE)
    except Exception:  # a test double for hass, or a registry not yet loaded
        _LOGGER.debug("could not update the go2rtc repair issue", exc_info=True)


def _hls_audio_enabled(device_client: Any = None) -> bool:
    """Whether the library publishes the AAC track (the HLS audio option).

    Read through the library, with the settings the camera's client was given,
    so the integration and the publisher agree on one switch.
    """
    from aidot_cameras.camera.aac_track import publish_aac_enabled

    return publish_aac_enabled(library_settings.of(device_client))


def _hls_audio_url(url: str | None) -> str | None:
    """Name the AAC track on a go2rtc RTSP URL for Home Assistant's HLS path.

    Home Assistant's stream component keeps AAC/MP3 audio and drops G.711, and
    go2rtc hands a consumer the first audio track unless asked - the A-law one.
    The direct publish carries AAC after it, so HLS asks for that by name. An
    explicit audio= choice is left alone; non-RTSP URLs are not go2rtc's.
    """
    if not url or not url.startswith("rtsp://"):
        return url
    base, sep, query = url.partition("?")
    params = [p for p in query.split("&") if p] if sep else []
    keys = {p.split("=", 1)[0] for p in params}
    if "audio" in keys:
        return url
    if "video" not in keys:
        params.insert(0, "video")
    params.append("audio=aac")
    return f"{base}?{'&'.join(params)}"


def _direct_publish_enabled(device_client: Any = None) -> bool:
    """Whether the library publishes in-process (the direct publish option).

    Read through the library, with the settings the camera's client was given,
    so the integration and the publisher agree on one switch.
    """
    from aidot_cameras.camera.rtsp_publish import direct_publish_enabled

    return direct_publish_enabled(library_settings.of(device_client))


SERVICE_TALK = "talk"
SERVICE_PTZ = "ptz"
SERVICE_STOP_SD_PLAYBACK = "stop_sd_playback"

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
# Sized against a cold SDES open, historically measured at 16-22s (MQTT
# signalling + ICE + SDES + ffmpeg).  That figure predates the cold-start work:
# re-measured 2026-09-07 over 11 real cold opens, the serve binds in 2.6-3.6s.
# The budget is deliberately NOT retuned to match - it is an upper bound on a
# wait that costs nothing when the publisher arrives early, and the failure it
# prevents is expensive.  Below it, the first view is *guaranteed* to fail: go2rtc answers a
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
#
# **The "one bad first click" this used to describe is NO LONGER THE TRADE.**
# That was written when one budget served every caller. `_publisher_wait_budget`
# now splits them: this short one is the HLS path only, and the view path - the
# one a card tap uses, which reaches stream_source() through the go2rtc provider
# and has no timeout at all - gets `_VIEW_PUBLISHER_WAIT_BATTERY_S` (75 s).
# Measured 2026-09-12 on two charged A001513s: the view path answered first try
# in 0.3 s and 6.2 s, no retries.
#
# What remains true is the HLS limit: a cold battery camera cannot beat HA's
# 10 s clock, so `camera.record` and HLS on a cold battery camera still fail and
# retry. Do not read that as the live view failing - it is a different path with
# a budget seven times larger, and testing the first click with `camera.record`
# measures this comment rather than the card.
_PUSH_PUBLISHER_WAIT_S = float(os.environ.get("AIDOT_PUSH_PUBLISHER_WAIT_S", "8.5"))
# How often to ask go2rtc whether the publisher has attached yet.
#
# This is pure latency on every cold open: the wait ends on a poll, so a 1.0s
# interval spends on average half a second, and up to a full one, after the
# publisher is already there. Against a measured cold battery open that reaches
# the card in ~13s, and a warm one that reaches it in ~1s, that is worth
# tightening. The poll is a localhost GET of go2rtc's stream list and only runs
# while a cold open is in flight, so four a second is cheap.
_PUSH_PUBLISHER_POLL_S = 0.25

# Part of the budget above held back for the library's own readiness signal, so
# the publisher poll cannot eat the whole thing and leave the fallback dead.
_SERVE_READY_RESERVE_S = 1.5

# The budgets above exist to fit HA's 10s CAMERA_STREAM_SOURCE_TIMEOUT, and
# that timeout wraps exactly one caller: `Camera.async_create_stream`, the HLS
# path. Every other caller reaches stream_source() with no clock at all - HA's
# go2rtc WebRTC provider (`_update_stream_source`) and third-party cards
# (`async_get_stream_source`), both verified against HA 2026.8.3 - and neither
# retries a URL that fails, because go2rtc answers a consumer on a stream whose
# publisher has not landed with an immediate error. Measured on the box
# 2026-09-03: a battery A001513's cold open landed its publisher at +11.1s, the
# 8.5s budget had handed the URL over at +7.2s, and the viewer got
#     dial tcp 127.0.0.1:18812: connect: connection refused
# and stopped. The next cold open took +5.3s and worked - the short budget
# turns the first click into a coin toss on exactly the cameras that are always
# cold (battery, excluded from prewarm).
#
# So the budget is sized by who is asking. async_create_stream marks its own
# call and keeps the short budgets; everyone else waits long enough for a cold
# open. A warm camera pays nothing either way: the wait ends the instant the
# publisher, or the local serve, is ready.
#
# Sized against a cold BATTERY open, which is the slow one and the one the 8.5s
# budget was cutting off. Measured 2026-09-03 with a 150s settle so each open
# was genuinely cold: the publisher attached at +5.5s and +10.4s, and an earlier
# one at +11.1s. The 8.5s budget sits in the middle of that spread, which is
# why the first click failed about half the time while the vendor app - which
# has no such cutoff - pulls the same camera up almost instantly.
#
# 20 s is the MAINS budget: a mains publisher attaches by about 11 s, and a
# camera that is genuinely unreachable should fail fast. Battery cameras get
# their own 75 s budget (_VIEW_PUBLISHER_WAIT_BATTERY_S below), sized by the
# wake rather than by the handshake - the 2026-09-03 note there has the
# measurements. (An earlier single 45 s budget for both is gone.)
_VIEW_PUBLISHER_WAIT_S = float(os.environ.get("AIDOT_VIEW_PUBLISHER_WAIT_S", "20"))

# A BATTERY camera's view budget, which has to be much larger, and the reason is
# the camera rather than us.
#
# A cold battery open is mostly the camera waking, and that wake is wide:
# measured on one camera in one day, its own wake announcement arrived +0.9 s,
# +5.4 s, +5.9 s, +10.9 s and +27.9 s after the open. Most of those reach the
# card in 8-13 s. The slow tail does not: on the +27.9 s open the first attempt
# was abandoned to the retry and media arrived at +46.7 s, which a 45 s budget
# missed by 1.7 s - so the click failed even though the camera was seconds from
# streaming.
#
# There is nothing to shorten there. The library's own signalling is under a
# second of it, and holding the offer back to wait for the camera was measured
# and makes it worse. So the choice is between a first click that sometimes
# fails and one that sometimes takes a while, and for a camera that is about to
# work, waiting is the better answer: the card shows Connecting / Buffering
# throughout, so it reads as slow rather than broken.
#
# 75 s clears the measured tail with room. It is only ever spent by a battery
# camera that is genuinely still waking - a warm one returns in about a second,
# and every other camera type keeps the short budget above. PARALLEL_UPDATES is
# 0, so a camera sitting in this wait blocks nothing else.
_VIEW_PUBLISHER_WAIT_BATTERY_S = float(
    os.environ.get("AIDOT_VIEW_PUBLISHER_WAIT_BATTERY_S", "75")
)


def _view_publisher_budget(battery: bool) -> float:
    """The non-HLS view budget for this kind of camera."""
    return _VIEW_PUBLISHER_WAIT_BATTERY_S if battery else _VIEW_PUBLISHER_WAIT_S


_VIEW_SERVE_READY_WAIT_S = float(os.environ.get("AIDOT_VIEW_SERVE_READY_WAIT_S", "25"))
_HLS_SERVE_READY_WAIT_S = 7.0

# True only while HA's own async_create_stream is calling stream_source().
_STREAM_SOURCE_HLS: ContextVar[bool] = ContextVar(
    "aidot_stream_source_hls", default=False
)

# True only while HA's async_refresh_providers is choosing this camera's WebRTC
# provider - it calls stream_source() for the URL scheme, not to view.
_PROVIDER_REFRESH: ContextVar[bool] = ContextVar(
    "aidot_provider_refresh", default=False
)

_UNRESOLVED = object()
# The go2rtc target one stream_source() / prewarm call resolved, so everything
# that call does - the mode, the registration, the publish URL, the publisher
# checks - talks to the same server even if the resolver's cache expires midway.
_CALL_GO2RTC_TARGET: ContextVar[Any] = ContextVar(
    "aidot_call_go2rtc_target", default=_UNRESOLVED
)

# Directions accepted by the aidot.ptz service - the exact keys the library's
# async_ptz_move() understands (AVIOCTRLDEFs codes), so they pass straight
# through.  Mirrors onvif.ptz / reolink.ptz_move for automation use.
PTZ_DIRECTIONS = [
    "up",
    "down",
    "left",
    "right",
    "left_up",
    "left_down",
    "right_up",
    "right_down",
    "zoom_in",
    "zoom_out",
    "stop",
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
        # pending_coordinators forgets ids that are no longer live, so a camera
        # that leaves the account and comes back gets an entity again.
        new_coords = pending_coordinators(coordinator.camera_coordinators, registered)
        # Assign serve ports for the whole batch, in sorted order, before any
        # entity asks for one: which camera of a colliding pair keeps its crc32
        # port must not depend on the order the cameras were discovered in.
        # The coordinators are keyed by device id.
        serve_ports.assign(_rtsp_name(dev_id) for dev_id in new_coords)
        new = [AidotCamera(c) for c in new_coords.values()]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    async_add_when_discovered(entry, _add_new_cameras)

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
    platform.async_register_entity_service(
        SERVICE_STOP_SD_PLAYBACK, {}, "async_stop_sd_playback"
    )


class AidotCamera(CoordinatorEntity[AidotCameraUpdateCoordinator], Camera):
    """Representation of an Aidot IP camera."""

    _attr_has_entity_name = True
    _attr_name = None
    # ON_OFF is the camera's own power switch (the app's live-screen switch,
    # the TurnOnOff property): camera.turn_off stops it streaming and
    # recording until camera.turn_on. Off, the entity reads "off" and stays
    # available; the library's serve loop pauses rather than retrying opens.
    _attr_supported_features = CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF
    # The transient connecting/buffering status is UI-only; keep it out of history.
    _unrecorded_attributes = frozenset({"stream_status"})

    def __init__(self, coordinator: AidotCameraUpdateCoordinator) -> None:
        CoordinatorEntity.__init__(self, coordinator)  # type: ignore[call-arg]
        Camera.__init__(self)
        info = coordinator.device_client.info
        self._attr_unique_id = info.dev_id
        entry = getattr(coordinator, "config_entry", None)
        self._attr_device_info = aidot_device_info(
            info,
            entry.entry_id if entry else None,
            getattr(getattr(entry, "runtime_data", None), "hub_device_id", None),
        )

        # Sanitised device ID safe to use as an RTSP stream name.
        self._rtsp_name = _rtsp_name(info.dev_id)
        # Cache for the last successfully fetched thumbnail bytes
        self._cached_image: bytes | None = None
        self._cached_image_ts: float = (
            0.0  # monotonic time of last successful fetch/check
        )
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
        # Set once the "an SDES camera with no go2rtc" warning has been logged
        # for this camera, so a dashboard retrying the view does not repeat it.
        # Cleared when a target resolves, so a later loss is logged again.
        self._warned_no_go2rtc: bool = False
        # The once-per-camera warning for an SDES camera whose only go2rtc is
        # the one Home Assistant's provider reads (target.ha_reads).
        self._warned_ha_reads: bool = False
        # The once-per-camera note for an SDES battery camera whose add-on
        # did not answer its latest probe (see _sdes_has_no_go2rtc).
        self._warned_battery_addon_down: bool = False
        # (target, monotonic, answered) of the last direct add-on check.
        self._addon_direct_check: tuple[Go2rtcTarget, float, bool] | None = None
        # The background stop _stop_publishing_session scheduled, while it runs.
        self._pending_stop: asyncio.Future | None = None
        # The go2rtc this camera's stream was last registered on. Teardown
        # deletes from it, not from whatever resolves at teardown time, which
        # can be a different server after the target changed.
        self._registered_go2rtc_target: Go2rtcTarget | None = None
        # Task created by _on_motion_prewarm; stored so it can be cancelled on removal.
        self._prewarm_task: asyncio.Future[Any] | None = None

    @property
    def is_on(self) -> bool:
        """Off only when the camera has reported its power switch off."""
        return getattr(self.coordinator.data, "powered", None) is not False

    async def async_turn_on(self) -> None:
        """Switch the camera on (TurnOnOff=1)."""
        await self._set_power(True)

    async def async_turn_off(self) -> None:
        """Switch the camera off (TurnOnOff=0); it stops streaming until turned on."""
        await self._set_power(False)

    async def _set_power(self, on: bool) -> None:
        ok = await self.coordinator.device_client.async_set_power(on)
        if not ok:
            raise HomeAssistantError(
                f"{self.coordinator.device_client.info.name}: the camera did not "
                f"accept being switched {'on' if on else 'off'}"
            )
        # The library records the state as soon as the write is acknowledged;
        # show it now rather than at the next poll.
        self.async_write_ha_state()

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
            self._rtsp_name,
            int(_STALE_STREAM_CHECK.total_seconds()),
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._evict_stale_stream, _STALE_STREAM_CHECK
            )
        )
        # Pre-connect on motion, when the option is on (off by default - see
        # CONF_MOTION_PREWARM): open the session on each new motion event so a
        # live view opened soon after starts warm. Mains cameras only: the
        # handler returns at once for a battery camera or with the option off.
        self.async_on_remove(
            self.coordinator.add_motion_listener(self._on_motion_prewarm)
        )

        # Lend the coordinator the one call that opens a session, so the
        # refresh button can use it without this entity's serve URLs and
        # connect options having to be copied anywhere.
        self.async_on_remove(
            self.coordinator.register_session_opener(self._prewarm_stream)
        )
        # And the calls a card playback needs: it holds the camera's one media
        # slot for the length of the clip, so it stops this entity's live
        # session first and has it warmed again afterwards.
        _register_live = getattr(self.coordinator, "register_live_controls", None)
        if _register_live is not None:
            self.async_on_remove(
                _register_live(
                    stop=self._yield_slot_to_sd_playback,
                    is_warm=lambda: (
                        self.coordinator.device_client.stream_rtsp_url is not None
                    ),
                    rewarm=self._rewarm_after_sd_playback,
                )
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

        # If Home Assistant turns out to read the go2rtc we publish into, a
        # session that is already publishing keeps doing so (a running session
        # keeps its first target): stop it, so the next open pulls or skips.
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_GO2RTC_HA_READS, self._on_go2rtc_ha_reads
            )
        )

        # Warm mains cameras in the background so the first live view - and the
        # concurrent burst of views after an HA restart - hits a warm session
        # instead of racing HA's 30s stream-worker deadline (see startup-prewarm).
        self._schedule_startup_prewarm()
        self._setup_complete = True

    def _schedule_startup_prewarm(self) -> None:
        """Run _startup_prewarm now, or once Home Assistant has started.

        A mains session keeps the mode it started in, and while Home
        Assistant is still starting the go2rtc add-on may not be answering
        yet - a warm-up then would start pulled, or not at all for an SDES
        camera, and stay that way. Waiting for EVENT_HOMEASSISTANT_STARTED costs nothing
        when HA is already running (a reload): it starts at once, as before.
        """
        held: dict[str, Any] = {}

        @callback
        def _start(_event: Any = None) -> None:
            # Entry-bound, so an unload cancels a warm-up still waiting out
            # its stagger.
            held["task"] = self.coordinator.spawn_entry_task(
                self._startup_prewarm(), "aidot startup pre-warm"
            )

        if self.hass.state is CoreState.running:
            _start()
        else:
            held["unsub"] = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _start
            )

        def _cancel_startup_prewarm() -> None:
            if (task := held.get("task")) is not None:
                task.cancel()
            elif (unsub := held.get("unsub")) is not None:
                unsub()

        self.async_on_remove(_cancel_startup_prewarm)

    async def _evict_stale_stream(self, _now: datetime.datetime | None = None) -> None:
        """Drop HA's cached Stream when our keepalive has ended underneath it.

        The DTLS serve idle-releases once its ``_stream_idle_s()`` window elapses
        with no reader (never, by default, for mains cameras; after
        ``BATTERY_IDLE_S`` for battery cameras) -- and either path's keepalive
        can end on teardown -- which unbinds the local serve port. But
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
        stream = self._stream_past_its_session()
        if stream is None:
            return
        # Keepalive has ended (idle-release / teardown): no ffmpeg is respawning,
        # so a one-off serve-port probe here is safe and confirms it's really dead.
        port = _serve_port(self._rtsp_name, self._serve_port_base())
        listening = await self.hass.async_add_executor_job(
            self._serve_port_listening, port
        )
        _LOGGER.info(
            "Evicting stale cached stream for %s (keepalive ended; serve port "
            "%d listening=%s) so the next view restarts it",
            self._rtsp_name,
            port,
            listening,
        )
        await self._stop_stale_stream(stream)
        if self.stream is stream:
            self.stream = None

    def _stream_past_its_session(self):
        """HA's cached Stream when the session it was made for has ended.

        None when there is no cached Stream, or when the keepalive is still
        active: ``stream_rtsp_url`` is set for as long as the session lives,
        and a Stream pointed at a live session is still good. This is the one
        test both the eviction watchdog and the reuse path apply; what they do
        with a stale Stream differs.
        """
        stream = self.stream
        if stream is None:
            return None
        if self.coordinator.device_client.stream_rtsp_url is not None:
            return None
        return stream

    async def _stop_stale_stream(self, stream) -> None:
        """Stop a Stream whose session has ended; a failure is only noted."""
        try:
            await stream.stop()
        except Exception as exc:
            _LOGGER.debug("stale-stream stop failed for %s: %s", self._rtsp_name, exc)

    @callback
    def _on_motion_prewarm(self, _event: dict[str, Any]) -> None:
        """Pre-connect on motion: start a session when a new clip is recorded,
        so a live view opened soon after starts warm.

        The CONF_MOTION_PREWARM option, off by default, mains cameras only.
        Motion events arrive ~30 s after recording begins. start_keepalive() is
        idempotent, and a camera with a session already (stream_rtsp_url set)
        is skipped before a task is made. Like any pre-warm, the session it
        starts lists the card once the list is stale (the piggyback in
        _prewarm_stream).

        Battery cameras are not pre-warmed on motion, because the stream would
        run with nobody watching. Over 23 hours on the reference installation
        39 of 41 battery-camera sessions started within 90 s of that camera's
        motion event - this pre-warm - and each streamed ~120 s unwatched. A
        notification's tap opens the cloud clip, not a live session (see
        notify_dispatch), so the warm session bought nothing.

        The same holds for a mains camera, so it is opt-in there too
        (CONF_MOTION_PREWARM, off by default): it saves a cold start of about
        3 s for a live view that, measured on the reference installation, did
        not come.
        """
        if self.coordinator.device_client.is_battery_camera:
            return
        if self._sd_playback_holds_slot():
            return
        if self.coordinator.device_client.stream_rtsp_url is not None:
            return
        if not self._entry_options().get(CONF_MOTION_PREWARM, DEFAULT_MOTION_PREWARM):
            return
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return
        # Entry-bound, so an unload cancels a pre-connect still opening.
        self._prewarm_task = self.coordinator.spawn_entry_task(
            self._prewarm_stream(), "aidot motion pre-connect"
        )

    def _entry_options(self) -> Mapping[str, Any]:
        """The entry's options, or none before the entry is attached."""
        entry = self.coordinator.config_entry
        return entry.options if entry else {}

    def _serve_port_base(self) -> int | None:
        """This entry's serve port base option, or None (see _serve_port)."""
        base = self._entry_options().get(CONF_SERVE_PORT_BASE)
        return base if isinstance(base, int) and not isinstance(base, bool) else None

    def _connect_options(self) -> dict[str, Any]:
        """Resolve the streaming connection kwargs for start_keepalive.

        The stored connection mode maps to the library's connection mode:
          - relay (default, "Automatic" on the form): library mode ``auto`` -
            keep the cloud TURN relay available and let ICE pick the path;
            app-parity, works on every topology (LAN, VLAN, remote, strict NAT).
          - lan_direct ("LAN only"): library mode ``lan`` (and fast_connect) -
            skip the relay pre-alloc; fastest on-LAN, but a camera not on the
            HA network can't connect."""
        opts = self._entry_options()
        _mode = resolve_connection_mode(opts)
        lan_direct = _mode == CONNECTION_MODE_LAN_DIRECT
        # Stored values keep their historical names; the library speaks
        # auto/lan (its env-only "relay" mode is experimental and not offered
        # here - see the note beside CONNECTION_MODES).
        _lib_mode = "lan" if lan_direct else "auto"
        # The per-camera "Camera audio" switch decides; until it has restored
        # its state, the default it would start from.
        _audio_override = getattr(self.coordinator, "sdes_audio_override", None)
        _audio = (
            _audio_override
            if _audio_override is not None
            else library_settings.camera_audio_default(opts)
        )
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
            "sdes_connection_mode": _lib_mode,
            "sdes_audio": _audio,
            "sdes_audio_gain_db": float(
                opts.get(CONF_SDES_AUDIO_GAIN_DB, DEFAULT_SDES_AUDIO_GAIN_DB)
            ),
            "sdes_fast_liveplay": bool(
                opts.get(CONF_SDES_FAST_LIVEPLAY, DEFAULT_SDES_FAST_LIVEPLAY)
            ),
        }

    def _stream_idle_s(self) -> float | None:
        """Warm-hold window (s): long for MAINS cameras so re-views are instant;
        BATTERY_IDLE_S (15 s) for battery cameras, so they stream only while
        someone is watching.

        Mains: the configured CONF_MAINS_IDLE_S, now defaulting to 0 = never
        release.  A mains camera has no battery to protect, and each warm one
        holds a concurrent-stream slot and keeps decrypting, which is why the
        library's stream cap is sized to the fleet.

        This used to say "a cold wake on these cameras measures 16-22s, which is
        long enough that a live view reads as broken".  RE-MEASURED 2026-09-07
        across 11 real cold opens on the live fleet: the serve bound in 2.6-3.6s,
        median 2.9s, SDES and DTLS alike.  The cold-start work closed that gap.
        See the note on DEFAULT_MAINS_IDLE_S in const.py -- and note that a
        camera which stops sending when unwatched cannot be held warm at all,
        so a 0 window leaves the watchdog restarting it indefinitely.

        Battery cameras keep releasing: that is where dormancy actually saves
        something, and it is the case the idle-release work was really for.
        They used to get None here, meaning the library's 120 s default, and a
        closed view then kept a battery camera streaming 129 s (HLS dialog) to
        159 s (WebRTC card): the 120 s window plus Home Assistant's own 30 s
        stream linger and a 10 s viewer-check interval. See BATTERY_IDLE_S."""
        dc = self.coordinator.device_client
        if dc.is_battery_camera:
            # An owner who set AIDOT_STREAM_IDLE_S chose the window: None lets
            # the library read it, where an explicit value here overrode it.
            if os.environ.get("AIDOT_STREAM_IDLE_S", "").strip():
                return None
            return BATTERY_IDLE_S
        opts = self._entry_options()
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
    def _sd_playback_holds_slot(self) -> bool:
        """Is a card recording being played on this camera right now?"""
        return getattr(self.coordinator, "media_slot_owner", None) == SLOT_SD

    async def _start_live_keepalive(self, dc, **kwargs) -> bool:
        """Open the live session, unless a card recording holds the slot.

        Every live open goes through here (a test holds the integration to
        it), so the check is made after the caller's last await and no new
        path can open in the middle of a playback. False when it did not.
        """
        if self._sd_playback_holds_slot():
            return False
        await dc.start_keepalive(**kwargs)
        return True

    async def _yield_slot_to_sd_playback(self) -> None:
        """Stop the live session so a card recording can have the media slot."""
        self._set_stream_status("Playing a recording from the SD card")
        await self._await_pending_stop()
        await self.coordinator.device_client.async_stop_streaming()

    async def _rewarm_after_sd_playback(self, warm: bool) -> None:
        """A card playback has given the slot back: clear its status, and warm
        the live session again when it was warm before (``warm``)."""
        self._set_stream_status(None)
        if warm:
            await self._prewarm_stream()

    async def _prewarm_stream(self) -> bool:
        """Start the keepalive in the background.

        Called from _startup_prewarm, from _rewarm_after_sd_playback, and
        from _on_motion_prewarm when the pre-connect on motion option is on
        (mains cameras only - a battery camera's motion event never triggers
        this), and registered with the coordinator as the session opener the
        SD-card refresh button uses to wake a camera before listing its card
        (see register_session_opener): returns False when it opened nothing,
        so the button does not wait out the full session-wait window for a
        session it never asked for.
        """
        async with self._one_go2rtc_target():
            return await self._prewarm_stream_pinned()

    async def _prewarm_stream_pinned(self) -> bool:
        dc = self.coordinator.device_client
        # A card recording holds the camera's one media slot; a live session
        # now would fight it for the slot (and the library would refuse the
        # play). Startup, motion and the SD refresh all come through here.
        if self._sd_playback_holds_slot():
            return False
        # Offline first, so an offline camera never probes go2rtc either.
        if self._camera_offline(dc):
            return False
        # An SDES camera has no pull path, so with no go2rtc to publish into a
        # session would only churn against the serve port nothing reads. The
        # opener must say it opened nothing so the SD refresh button does not
        # wait 75 s for it.
        if await self._sdes_has_no_go2rtc():
            return False
        push = await self._push_mode()
        try:
            # Register only with no session of our own: the PUT replaces the
            # definition and would drop a live publisher (see _open_and_wait).
            if not await self._open_live_session(
                dc,
                await self._push_serve_url() if push else self._serve_url,
                register=push and dc.stream_rtsp_url is None,
            ):
                return False
            _LOGGER.debug("Prewarm started for %s", self._rtsp_name)
            # The session is on its way (start_keepalive schedules the
            # handshake and returns), so the card listing can ride it: one AVIO
            # round trip instead of a 15-70 s handshake. Backgrounded and
            # entry-bound: the wait is long, a listing must not delay a stream a
            # viewer is waiting on, and an unload must cancel it.
            self.coordinator.spawn_entry_task(
                self.coordinator.async_piggyback_sd_refresh(),
                "aidot piggyback sd refresh",
            )
            return True
        except Exception as exc:
            _LOGGER.debug("Prewarm failed for %s: %s", self._rtsp_name, exc)
            return False

    def _camera_offline(self, dc) -> bool:
        """Does the cloud say this camera is offline? Logged when it does.

        Only an explicit offline report refuses an open (the library's
        cloud_reports_offline). The open cannot succeed, and
        failing is not cheap: the library holds its global open-gate for the
        whole no-answer timeout (45 s observed), delaying every other camera's
        cold open, and a keepalive parked in its retry pause keeps renewing and
        waking a camera nobody can view.
        """
        if not dc.cloud_reports_offline:
            return False
        _LOGGER.debug(
            "Not opening a session for %s: camera is offline", self._rtsp_name
        )
        return True

    async def _open_live_session(self, dc, url: str, *, register: bool) -> bool:
        """Open the live session, publishing to (or served at) ``url``.

        The one open both the view and the prewarm use, each after checking
        the camera is not offline (_camera_offline). False when it opened
        nothing: go2rtc would not take the stream definition a publish needs
        (``register``), or a card recording took the slot during the awaits.

        go2rtc refuses an RTSP publish to a name it does not know and will not
        hold a source-less stream, so a publish is preceded by a definition
        with the inert placeholder source. If that fails (go2rtc disabled,
        restarting, or unreachable) nothing is opened: a session publishing
        into a port nothing takes reopens forever on a mains camera and until
        the library's cap on a battery one.
        """
        if register and await self._publish_to_go2rtc() is None:
            _LOGGER.debug(
                "Not opening a session for %s: go2rtc did not accept the stream"
                " definition, so there is nothing to publish to",
                self._rtsp_name,
            )
            return False
        await self._await_pending_stop()
        return await self._start_live_keepalive(
            dc,
            rtsp_push_url=url,
            stream_idle_s=self._stream_idle_s(),
            **self._connect_options(),
        )

    async def _startup_prewarm(self) -> None:
        """Warm the WebRTC session in the background shortly after setup.

        Mains cameras only - a warm session would drain a battery camera. The
        per-camera staggered delay spreads the handshakes (the library also
        serialises them via its global open-gate); because this runs with no
        live-view in progress there is no 30s view-clock, so a slow cold open
        just takes longer to warm rather than surfacing as a failed view. Reuses
        the idempotent ``_prewarm_stream`` (a no-op if a session is already up)."""
        opts = self._entry_options()
        if not opts.get(CONF_STARTUP_PREWARM, DEFAULT_STARTUP_PREWARM):
            return
        dc = self.coordinator.device_client
        if dc.is_battery_camera or dc.cloud_reports_offline:
            return
        delay = (
            _serve_port(self._rtsp_name, self._serve_port_base()) % 8
        ) * _STARTUP_PREWARM_STAGGER_S
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
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    async with self._image_lock:
                        self._cached_image = data
                        self._cached_image_ts = time.monotonic()
                    # Notify HA that a fresh image is available via the proxy.
                    self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.debug(
                "Thumbnail bytes fetch failed for %s: %s", self.unique_id, exc
            )

    @property
    def _serve_url(self) -> str:
        """Local HTTP-serve URL for this camera's stream (deterministic port)."""
        return f"http://127.0.0.1:{_serve_port(self._rtsp_name, self._serve_port_base())}/{self._rtsp_name}.ts"

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
        that stall SDES cold starts under HA. Requires go2rtc; DTLS cameras
        keep the proven pull serve.

        The stored ``sdes_push`` option is deliberately NOT consulted. 2.19.0
        removed the toggle from the options page because publishing into go2rtc
        is the mode that works and the pull serve it could switch to is the one
        that jams - but it kept reading the key, so an entry that had turned it
        off in 2.18.x was pinned to the jamming path with no UI left to undo it.
        Honouring a setting a user can no longer see or change is worse than
        ignoring it, so this returns the working mode for every SDES camera.
        """
        dc = self.coordinator.device_client
        if not _GO2RTC_ENABLED:
            return False
        if dc.is_sdes_camera:
            return True
        # DTLS cameras publish too once the library publishes in-process
        # (direct publish): there is then no ffmpeg or serve port to pull from.
        return _direct_publish_enabled(dc)

    async def _push_mode(self) -> bool:
        """``_sdes_push_enabled`` checked against a go2rtc that exists.

        An SDES camera has no pull path, so it publishes unless there is no
        go2rtc anywhere (_sdes_has_no_go2rtc) - including into an add-on or an
        explicit server whose latest probe failed, as it always has.

        A DTLS camera only publishes into a go2rtc that answers: an explicit
        AIDOT_GO2RTC_API or an add-on on 127.0.0.1:1984 (go2rtc_target).
        Otherwise a push URL points HA at a
        port nothing listens on, so it keeps its pull serve.
        """
        if not self._sdes_push_enabled():
            return False
        async with self._one_go2rtc_target():
            return await self._push_mode_pinned()

    async def _push_mode_pinned(self) -> bool:
        dc = self.coordinator.device_client
        if dc.is_sdes_camera:
            return not await self._sdes_has_no_go2rtc()
        target = await self._go2rtc_target()
        if target is not None and target.ha_reads:
            # Home Assistant's provider reads this server and cannot decode a
            # published stream: pull, whose producer carries a url.
            return False
        return await self._go2rtc_reachable()

    @contextlib.asynccontextmanager
    async def _one_go2rtc_target(self):
        """Resolve the go2rtc target once for everything inside the block.

        Nested blocks reuse the outer one's answer. See _CALL_GO2RTC_TARGET.
        """
        if _CALL_GO2RTC_TARGET.get() is not _UNRESOLVED:
            yield
            return
        token = _CALL_GO2RTC_TARGET.set(await self._go2rtc_target())
        try:
            yield
        finally:
            _CALL_GO2RTC_TARGET.reset(token)

    async def _go2rtc_target(self) -> Go2rtcTarget | None:
        """The go2rtc this integration talks to, or None.

        AIDOT_GO2RTC=0 means never go2rtc: nothing is resolved at all. Inside
        a stream_source() or prewarm call, the target that call resolved.
        """
        if not _GO2RTC_ENABLED:
            return None
        pinned = _CALL_GO2RTC_TARGET.get()
        if pinned is not _UNRESOLVED:
            return pinned
        return await resolve_go2rtc_target(self.hass)

    async def _go2rtc_reachable(self) -> bool:
        """Is there a go2rtc to publish into?

        The resolver probes the add-on (``answered`` is that probe's result)
        and reads Home Assistant's handle; an explicit AIDOT_GO2RTC_API it
        takes on trust, so that one is still probed here, as it always was - a
        DTLS camera must not publish into a server that does not answer.
        Cached briefly and shared by all cameras: this runs on the setup path
        and on every view.
        """
        target = await self._go2rtc_target()
        if target is None:
            return False
        if target.kind != KIND_EXPLICIT:
            return target.answered
        store = self.hass.data.setdefault(DOMAIN, {})
        cached = store.get("go2rtc-reachable")
        now = time.monotonic()
        if cached and now - cached[0] < _GO2RTC_REACHABLE_TTL_S:
            return cached[1]
        ok = await target.client(timeout=3.0).available()
        if not ok and (not cached or cached[1]):
            log_go2rtc_unanswered(target.api_url)
        store["go2rtc-reachable"] = (now, ok)
        return ok

    async def _sdes_has_no_go2rtc(self) -> bool:
        """True for an SDES camera with no go2rtc it may publish into.

        True means "open no session for this camera". Three cases, each with
        its own once-per-camera warning and its own flag - sharing one flag
        would let the one that fires first permanently silence the other:

        - No go2rtc to publish into: no explicit AIDOT_GO2RTC_API and no add-on
          that has answered, once Home Assistant is running. Its bundled go2rtc
          is never a target. While Home Assistant is still starting, with its
          go2rtc integration loaded, this is False and the add-on's push URL is
          handed out, because the add-on may not be up yet. (_warned_no_go2rtc,
          plus the process-wide go2rtc_target.SDES_NO_GO2RTC_ISSUE Repairs
          issue, cleared as soon as any target resolves)
        - A battery camera with no session, whose add-on target did not
          answer its latest probe and does not answer when asked directly now
          (restarting, or gone). A mains camera still publishes into it and
          recovers when it is back; a new battery session would only wake it,
          again and again, into a port nothing takes until the library's cap.
          Only new opens: a running session is never refused or stopped on
          this, since one failed 3 s probe is cached for a minute while the
          add-on may well be up. (_warned_battery_addon_down)
        - Home Assistant's go2rtc provider reads the only target
          (target.ha_reads). (_warned_ha_reads)

        stream_source decides what a True answers: None only when HA's go2rtc
        provider is not loaded; otherwise a URL with nothing published behind
        it, because a None there makes the provider tear down every camera's
        session (see stream_source). AIDOT_GO2RTC=0 is not this case: that
        switch keeps its old meaning.
        """
        if not _GO2RTC_ENABLED:
            return False
        dc = self.coordinator.device_client
        if not dc.is_sdes_camera:
            return False
        target = await self._go2rtc_target()
        if target is not None:
            # A target resolved - whatever this camera does next, it is not
            # the "no go2rtc anywhere" case the Repairs issue below describes.
            note_sdes_no_go2rtc(self.hass, False)
        if (
            target is not None
            and not target.answered
            and dc.is_battery_camera
            and self._running_session_pushes() is None
            and not await self._addon_answers_now(target)
        ):
            if not self._warned_battery_addon_down:
                self._warned_battery_addon_down = True
                _LOGGER.info(
                    "camera %s: the go2rtc add-on at %s did not answer; this"
                    " battery camera is not streamed until it does",
                    self._rtsp_name,
                    target.api_url,
                )
            return True
        if target is not None and target.ha_reads:
            if not self._warned_ha_reads:
                self._warned_ha_reads = True
                _LOGGER.warning(
                    "camera %s: the only go2rtc is the one Home Assistant's"
                    " go2rtc reads, and an SDES camera can only publish, which"
                    " would fail every camera's live view; it is not streamed"
                    " (see Settings -> Repairs)",
                    self._rtsp_name,
                )
            return True
        if target is not None:
            self._warned_no_go2rtc = False
            self._warned_ha_reads = False
            self._warned_battery_addon_down = False
            return False
        # While Home Assistant is starting the add-on may not be up yet and the
        # resolver does not cache that None, so the add-on's push URL is kept.
        # Once it is running, None means nothing this camera can publish into:
        # Home Assistant's bundled go2rtc is never a target (see
        # go2rtc_target). Handing out the add-on's URL then
        # started a keepalive whose publisher was refused every time, which
        # reopened every 35-60 s and kept a battery camera awake until restart.
        if (
            "go2rtc" in self.hass.config.components
            and self.hass.state is not CoreState.running
        ):
            return False
        note_sdes_no_go2rtc(self.hass, True)
        if not self._warned_no_go2rtc:
            self._warned_no_go2rtc = True
            _LOGGER.warning(
                "camera %s: no go2rtc answered (nothing on 127.0.0.1:1984 and no"
                " AIDOT_GO2RTC_API; Home Assistant's built-in go2rtc cannot take"
                " a published stream); an SDES camera has no other path and"
                " is not streamed; see Settings -> Repairs",
                self._rtsp_name,
            )
        return True

    async def _addon_answers_now(self, target: Go2rtcTarget) -> bool:
        """Ask the target directly whether it answers, reused for a few seconds."""
        now = time.monotonic()
        cached = getattr(self, "_addon_direct_check", None)
        if (
            cached is not None
            and cached[0] is target
            and now - cached[1] < _ADDON_DIRECT_CHECK_TTL_S
        ):
            return cached[2]
        ok = bool(await target.client(timeout=3.0).available())
        self._addon_direct_check = (target, now, ok)
        return ok

    @callback
    def _on_go2rtc_ha_reads(self, reads: bool) -> None:
        """Stop a publishing session once HA turns out to read our server."""
        if reads:
            self._stop_publishing_session(
                "Home Assistant's go2rtc reads the server it publishes into"
            )

    def _running_session_pushes(self) -> bool | None:
        """The running session's mode: True publishing, False pulled, None none.

        The library pins a session's mode when it starts (start_keepalive is
        a no-op while one runs) and keeps its first URL: the go2rtc rtsp://
        URL for a publishing session, the local http serve for a pulled one.
        """
        url = self.coordinator.device_client.stream_rtsp_url
        if not isinstance(url, str):
            return None
        return url.startswith("rtsp://")

    @callback
    def _stop_publishing_session(self, why: str) -> None:
        """Stop this camera's session in the background if it is publishing.

        At most one such stop at a time: the library clears the session's URL
        as a stop begins, then swaps out and cancels its tasks, so a second
        stop, or an open, landing in between would cancel the session opened
        meanwhile. Openers wait for it first (_await_pending_stop).
        """
        pending = getattr(self, "_pending_stop", None)
        if pending is not None and not pending.done():
            return
        if not self._running_session_pushes():
            return  # nothing running, or a pulled session
        _LOGGER.info(
            "camera %s: stopping its publishing session - %s", self._rtsp_name, why
        )
        dc = self.coordinator.device_client
        task = self.hass.async_create_background_task(
            dc.async_stop_streaming(), f"aidot stop publishing {self._rtsp_name}"
        )
        self._pending_stop = task if isinstance(task, asyncio.Future) else None

    async def _await_pending_stop(self) -> None:
        """Let a stop _stop_publishing_session scheduled finish before an open.

        asyncio.wait neither raises the stop's own error nor cancels it when
        the waiting view is cancelled. Bounded, because the WebRTC path has no
        timeout above this: a stop that never finishes is left running and the
        open goes ahead rather than hanging the view.
        """
        pending = getattr(self, "_pending_stop", None)
        if pending is not None and not pending.done():
            done, _ = await asyncio.wait({pending}, timeout=_PENDING_STOP_WAIT_S)
            if not done:
                _LOGGER.warning(
                    "camera %s: its previous session has not stopped after"
                    " %.0f s; opening anyway",
                    self._rtsp_name,
                    _PENDING_STOP_WAIT_S,
                )

    async def _push_serve_url(self) -> str:
        """go2rtc RTSP URL this camera publishes to (and viewers consume).

        go2rtc accepts RTSP publish on a stream name and fans it out to every
        consumer, so the publish URL and the viewer URL are the same. With no
        target the add-on's address is returned, so a caller always gets a
        string.
        """
        target = await self._go2rtc_target()
        if target is None:
            return f"rtsp://127.0.0.1:{DEFAULT_RTSP_PORT}/{self._go2rtc_name}"
        return target.rtsp_url(self._go2rtc_name)

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
        target = await self._go2rtc_target()
        if target is None:
            return "unknown"  # no go2rtc - do not thrash the session
        streams = await target.client(timeout=2.0).list_streams()
        if not streams:
            return "unknown"  # go2rtc unreachable - do not thrash the session
        entry = streams.get(self._go2rtc_name)
        if not isinstance(entry, dict):
            return "absent"  # stream definition is gone; re-register it
        producers = entry.get("producers") or []
        if any(not p.get("url") for p in producers if isinstance(p, dict)):
            return "live"
        return "absent"

    async def _publish_to_go2rtc(self, serve_url: str | None = None) -> str | None:
        """Define this camera on the resolved go2rtc target; return its RTSP URL.

        The target is an explicit AIDOT_GO2RTC_API or the add-on on port 1984,
        never Home Assistant's bundled go2rtc (see resolve_go2rtc_target); with
        no target this returns ``None``. A publishing camera passes no
        ``serve_url``: it is defined with the library's inert placeholder source
        (a push camera never binds its serve port) and its publisher attaches as
        a producer. A pulled camera is defined with ``serve_url``, which go2rtc
        pulls once and fans out to every viewer. Handing HA the go2rtc RTSP URL is what makes
        it serve the camera over WebRTC instead of HLS. Defined eagerly, so the
        camera advertises WebRTC even on a cold first view, and on every call
        (the PUT is idempotent and cheap) so it self-heals if go2rtc restarts
        and drops the definition. Returns ``None`` (-> the caller falls back to
        the local serve) if go2rtc is unreachable; ``ensure_stream``'s PUT
        fast-fails then, keeping HA's per-camera setup check fast.
        """
        target = await self._go2rtc_target()
        if target is None:
            return None
        # Short timeout: go2rtc is loopback, and this runs in HA's per-camera
        # setup provider check - don't let a down go2rtc stall startup.
        client = target.client(timeout=3.0)
        name = self._go2rtc_name
        # A camera that changes its H.264 parameter sets between sessions has to
        # get a FRESH stream definition, or go2rtc keeps serving the decoder
        # configuration (the fMP4 avcC box) it built when the track was first
        # published. A player using Media Source Extensions configures itself from
        # that box once, so a stale one leaves it unable to decode inter-frames and
        # the picture updates only on keyframes. Measured on an A001064: avcC held
        # an SPS from an earlier session, while a freshly created definition
        # matched the stream exactly and stayed stable for the whole session.
        # Order matters: this runs at stream_source() time, BEFORE the keepalive
        # publishes, so the publisher never loses its target.
        recreate = _sprop_is_unstable(self.coordinator.device_client.device_id)
        if recreate:
            _LOGGER.debug(
                "%s changes its parameter sets between sessions - recreating its "
                "go2rtc stream so the decoder config is rebuilt",
                self._rtsp_name,
            )
        # No AAC transcoding source: go2rtc's aac transcode stamps audio on a
        # 90 kHz clock while advertising 8 kHz, so HLS viewers played it about
        # eleven times too slow. WebRTC viewers keep G.711 passthrough; HA's HLS
        # dialog has no audio from this source.
        # One locked call in the library: the optional DELETE, the PUT and, if
        # the PUT is rejected, the stream-list check, with no other write to
        # this go2rtc in between (overlapping writes tear go2rtc 1.9.9's config
        # file, after which it rejects every PUT with 400).
        result = await async_ensure_stream(client, name, serve_url, recreate=recreate)
        if not result.ok:
            # Not there either: the server may have gone away, so the next
            # open asks again which go2rtc to use.
            invalidate_go2rtc_target(self.hass)
            return None
        # The streams stay registered while go2rtc rejects every PUT (a
        # duplicate key in its own config), so keep using go2rtc rather than
        # dropping every camera to HLS at once - and say so in Repairs.
        rejected = result.outcome is EnsureOutcome.UNCHANGED
        if rejected:
            _LOGGER.info(
                "go2rtc rejected the register call for %s but the stream is "
                "present - using go2rtc instead of falling back",
                self._rtsp_name,
            )
        _note_go2rtc_rejection(self.hass, name, rejected=rejected)
        self._registered_go2rtc_target = target
        rtsp = target.rtsp_url(name)
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
            # The server the stream was registered on; the current answer only
            # when nothing was ever registered.
            target = self._registered_go2rtc_target or await self._go2rtc_target()
            if target is None:
                return
            await async_remove_stream(target.client(), self._go2rtc_name)
            if self._registered_go2rtc_target is target:
                self._registered_go2rtc_target = None
        except Exception:  # best-effort teardown hygiene
            pass

    async def async_create_stream(self):
        """HA's HLS entry point - the one caller under its 10s clock.

        Marks the stream_source() it makes so the waits inside fit that clock;
        every other caller gets the cold-open-sized budgets (see
        _VIEW_PUBLISHER_WAIT_S).

        Also the door recordings and the HLS dialog come through, and where a
        cached Stream whose session has ended is dropped - see
        _drop_stream_if_session_ended.
        """
        await self._drop_stream_if_session_ended()
        token = _STREAM_SOURCE_HLS.set(True)
        try:
            return await super().async_create_stream()
        finally:
            _STREAM_SOURCE_HLS.reset(token)

    async def async_refresh_providers(
        self, *, write_state: bool = True, **kwargs: Any
    ) -> None:
        """HA's WebRTC provider choice - marked so stream_source() opens nothing.

        HA re-runs this for every camera whenever a WebRTC provider registers
        (the go2rtc entry loading late, or reloading), and it picks the
        provider by calling stream_source(). After setup that call used to take
        the full view path: every camera opened a session with nobody watching,
        a battery camera staying awake for at least its start-up grace. The
        mark makes it answer from the URL alone, as it does during setup.

        The state is written after the mark is cleared, not inside it: state
        listeners run in the writer's context, so an automation reacting to the
        write (camera.record, say) would otherwise get the URL-only answer and
        open nothing.
        """
        old_provider = self._webrtc_provider
        token = _PROVIDER_REFRESH.set(True)
        try:
            await super().async_refresh_providers(write_state=False, **kwargs)
        finally:
            _PROVIDER_REFRESH.reset(token)
        if write_state and self._webrtc_provider != old_provider:
            self.async_write_ha_state()

    async def _drop_stream_if_session_ended(self) -> None:
        """Stop and forget a cached Stream that outlived its session.

        Home Assistant calls stream_source() only when it creates a Stream, and
        otherwise hands back the cached one - even a stopped one - so a Stream
        created for an earlier view keeps the URL of a session that may since
        have been released (within about half a minute of a battery camera's
        last viewer, or the mains warm-hold window on a mains camera). Reusing
        it points the new recording or HLS view at a source nobody publishes:
        404, nothing re-opens the camera, and the 30 s stale-stream watchdog
        then evicts it mid-use, so a recording started in that window never
        writes a file. Measured on the box 2026-09-27: record at 14:20:08, 404
        at 14:20:08, evicted at 14:20:11, no file.

        Same test as the watchdog (no keepalive means no session), applied at
        the moment of reuse instead of on a timer. The reference is cleared
        before awaiting the stop so a concurrent caller cannot pick it up.
        """
        stream = self._stream_past_its_session()
        if stream is None:
            return
        self.stream = None
        _LOGGER.info(
            "Replacing the cached stream for %s: its session has ended, so this "
            "view opens the camera again",
            self._rtsp_name,
        )
        await self._stop_stale_stream(stream)

    def _publisher_wait_budget(self) -> float:
        """How long a push-mode view may wait for the go2rtc publisher.

        The HLS path keeps the short budget it needs to fit Home Assistant's
        10 s clock; everything else is sized by what the camera actually needs,
        which for a battery camera is dominated by its own wake.
        """
        if _STREAM_SOURCE_HLS.get():
            return _PUSH_PUBLISHER_WAIT_S
        return _view_publisher_budget(self.coordinator.device_client.is_battery_camera)

    @staticmethod
    def _serve_ready_wait_budget() -> float:
        """How long a pull-mode view may wait for the local serve to be ready."""
        return (
            _HLS_SERVE_READY_WAIT_S
            if _STREAM_SOURCE_HLS.get()
            else _VIEW_SERVE_READY_WAIT_S
        )

    async def stream_source(self) -> str | None:
        """The camera's stream URL. On the HLS path: the library's in-sync TS
        when that option is on, otherwise go2rtc's with the AAC track named
        when the HLS audio option has the library publish one.

        See _resolve_stream_source for how the URL is chosen and the camera
        opened, and _hls_audio_url for why HLS asks for AAC.
        """
        async with self._one_go2rtc_target():
            url = await self._resolve_stream_source()
        if _STREAM_SOURCE_HLS.get():
            # In-sync HLS: the camera is opened above exactly as usual - that
            # session is what feeds it - and HLS then reads the library's TS.
            # Only when it publishes to go2rtc (an rtsp:// source): in pull mode
            # the direct publisher that feeds the TS never runs.
            ts_url = self._hls_ts_url() if url and url.startswith("rtsp://") else None
            if ts_url:
                return ts_url
            dc = getattr(getattr(self, "coordinator", None), "device_client", None)
            if _hls_audio_enabled(dc):
                return _hls_audio_url(url)
        return url

    def _hls_ts_url(self) -> str | None:
        """The library's in-sync MPEG-TS URL for this camera, or None.

        None when the in-sync HLS option is off, for a battery camera (its lossy
        link rarely delivers the clean keyframe the in-sync stream starts on,
        so since library 1.0.0rc62 it stays on go2rtc's stream), for an SDES
        camera the library cannot promise to feed (a model whose media it does
        not decrypt, or no H.264 pin - see the library's ``_hls_ts_eligible``),
        or if the library cannot serve it - and then the HLS path stays on
        go2rtc.
        """
        try:
            return self.coordinator.device_client.hls_ts_url()
        except Exception:
            _LOGGER.warning(
                "In-sync HLS unavailable for %s; using go2rtc",
                self.entity_id,
                exc_info=True,
            )
            return None

    async def _resolve_stream_source(self) -> str | None:
        """Start (or reuse) this camera's session and return the URL HA reads.

        - SDES (A001064/A001513): always published into the resolved go2rtc
          target; the URL is that go2rtc's RTSP stream. With no target the
          camera opens no session (see _sdes_has_no_go2rtc). The first open
          takes 25-70s while the SCTP handshake runs.
        - DTLS (A000088): published the same way when a target answers and
          direct publish is on; otherwise aiortc's encoded H.264 is copied to
          the local HTTP serve (no decode/re-encode), which go2rtc - HA's own
          one included - pulls.

        Subsequent viewers reuse the warm session.

        Never None while HA's go2rtc integration is loaded: its provider tears
        down EVERY camera's session on a falsy stream_source. Each "cannot be
        served right now" exit hands back a URL that fails locally instead -
        the push URL or the local serve, both schemes go2rtc supports.
        """
        # An SDES camera with no go2rtc has nothing to publish into. A session
        # already publishing would reopen against a dead port, so it is
        # stopped, as _on_go2rtc_ha_reads does.
        if await self._sdes_has_no_go2rtc():
            self._stop_publishing_session("there is no go2rtc to publish into")
            if "go2rtc" not in self.hass.config.components:
                return None
            return await self._push_serve_url()
        # A card recording holds the camera's one media slot.
        if self._sd_playback_holds_slot():
            if "go2rtc" not in self.hass.config.components:
                return None
            return await self._push_serve_url()
        if not self._setup_complete or _PROVIDER_REFRESH.get():
            return await self._setup_time_url()
        return await self._serve_running_session()

    async def _setup_time_url(self) -> str:
        """The URL a real view would get, with nothing opened.

        HA calls stream_source() once per camera during entity setup, before
        async_added_to_hass, to pick a WebRTC provider, and again on a provider
        refresh. Opening a session there blocked setup 7-14 s per camera,
        serialised by the platform loader. An RTSP answer is what makes HA's
        go2rtc provider enable WebRTC; None made it settle on HLS for good.

        A push camera answers with its go2rtc RTSP URL and registers nothing:
        registering its local serve, which nothing binds, left every unviewed
        camera with a producer go2rtc could only fail to dial ("connection
        refused" on every offer). Its definition is made right before a
        publish instead (_open_live_session). Registering the push URL itself
        would make go2rtc its own producer.

        A pulled camera registers its serve. With no reachable go2rtc (the
        normal case with only HA's bundled go2rtc, which listens on a unix
        socket) it hands HA the local serve URL, which HA's go2rtc or its HLS
        pipeline can read; None here once left cameras with no stream at all.
        """
        if await self._push_mode():
            return await self._push_serve_url()
        return await self._publish_to_go2rtc(self._serve_url) or self._serve_url

    async def _serve_running_session(self) -> str:
        """Serve the view in the running session's mode, opening one if needed.

        A running session keeps the mode it started in (the library pins it),
        so the view serves in that mode, not the one the current target would
        pick: a mains camera pulled before the add-on answered must not be
        treated as publishing, and a publishing session viewed while the add-on
        restarts must not be handed the local serve nothing binds.

        A publishing session cannot be served once HA's provider reads its
        server: _on_go2rtc_ha_reads is already stopping it, so this view makes
        sure it stops and fails locally, and the next one opens pulled. A
        target that merely missed its latest probe is an outage: the session
        keeps publishing and the view keeps its rtsp:// URL.
        """
        push = self._running_session_pushes()
        if push:
            target = await self._go2rtc_target()
            if target is not None and target.ha_reads:
                self._stop_publishing_session(
                    "Home Assistant's go2rtc reads the server it publishes into"
                )
                return self._serve_url
        if push is None:
            push = await self._push_mode()
        serve_url = await self._push_serve_url() if push else self._serve_url
        url = None
        cancelled = False
        self._set_stream_status("Connecting...")
        try:
            url = await self._open_and_wait(push, serve_url)
            return url or serve_url
        except asyncio.CancelledError:
            # Normal when the user closes the live view - not a failure.
            cancelled = True
            raise
        finally:
            if url is not None or cancelled:
                # Stream took over (or a clean close) - drop the overlay.
                self._set_stream_status(None)
            elif self._stream_status is None or not self._stream_status[1]:
                # Any other exit that did not already set an error: surface a
                # soft "retrying" status attribute.
                self._set_stream_status("Camera unavailable - retrying...", error=True)

    async def _open_and_wait(self, push: bool, serve_url: str) -> str | None:
        """Open the session if none is live, then wait until the URL can serve.

        ``serve_url`` is the go2rtc RTSP URL when publishing, the local serve
        when pulled. Returns the URL to hand HA, or None when the camera could
        not be opened or served (the caller then hands back ``serve_url``).
        """
        dc = self.coordinator.device_client
        # live / absent / unknown. A never-started session is "absent" without
        # asking go2rtc; "unknown" (go2rtc unreachable) keeps the warm path
        # rather than restarting a session that may be fine.
        pub_state = (
            "absent"
            if dc.stream_rtsp_url is None
            else await self._go2rtc_publisher_state(push)
        )
        if pub_state == "absent":
            # The offline check sits here rather than up front, so a camera
            # already serving media keeps being served whatever the flag says.
            if self._camera_offline(dc):
                return None
            try:
                if not await self._open_live_session(dc, serve_url, register=push):
                    return None
                _LOGGER.info(
                    "Started HTTP stream serve for %s -> %s (connection mode: %s)",
                    self._rtsp_name,
                    serve_url,
                    resolve_connection_mode(self._entry_options()),
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Failed to start stream serve for %s: %s", self._rtsp_name, exc
                )
                return None

        # Both modes define the stream: go2rtc only creates a stream that has a
        # source. In pull mode it is the serve URL; in push mode an inert
        # placeholder, and the publish feeds the stream. The push PUT is
        # skipped while a publisher is attached (or was just defined for this
        # open): go2rtc's PUT /api/streams REPLACES the definition and drops the
        # live publisher, even for an identical src, leaving consumers a 404
        # while the publisher keeps pushing (reproduced on go2rtc 1.9.9).
        if push:
            if (
                pub_state not in ("live", "absent")
                and await self._publish_to_go2rtc() is None
            ):
                return None  # go2rtc unreachable: nowhere to publish
            # go2rtc serves a consumer from the moment the publisher's ANNOUNCE
            # lands, so "publisher attached" is exactly what to wait for; a
            # consumer on the placeholder alone gets a 404 the provider does
            # not retry. One budget for both waits, split so the publisher poll
            # cannot starve the library's readiness fallback. On timeout the URL
            # is handed over anyway while the keepalive keeps warming.
            self._set_stream_status("Buffering...")
            budget = self._publisher_wait_budget()
            deadline = time.monotonic() + budget
            attached = pub_state == "live" or await self._await_publisher_attached(
                max(0.5, budget - _SERVE_READY_RESERVE_S)
            )
            if not attached:
                left = deadline - time.monotonic()
                if left > 0.5:
                    try:
                        await dc.async_wait_serve_ready(timeout=left)
                    except Exception:
                        pass
                _LOGGER.debug(
                    "%s: no publisher within %.1fs - handing HA the URL "
                    "while the keepalive keeps warming the session",
                    self._rtsp_name,
                    budget,
                )
            return serve_url
        publish_url = await self._publish_to_go2rtc(self._serve_url) or serve_url
        if dc.is_sdes_camera:
            # SDES binds its -listen serve only after the SCTP handshake and
            # first media, far past HA's 10 s clock: probe briefly with a
            # try-bind (never a connect, which would take the single accept
            # slot) and hand the URL over; HA's stream worker retries a refusal.
            self._set_stream_status("Buffering...")
            await self._await_serve_listening(
                _serve_port(self._rtsp_name, self._serve_port_base()), timeout=7.0
            )
            return publish_url
        # DTLS pull: a cold open needs 15-21 s before ffmpeg binds. Short wait
        # on the HLS path (under HA's 10 s cutoff, whose stream worker retries
        # a refusal); a cold open's worth otherwise, since go2rtc dials the
        # serve the instant the URL lands and does not retry. The library's
        # keyframe gating still holds the bind until media is valid.
        self._set_stream_status("Negotiating...")
        try:
            await dc.async_wait_serve_ready(timeout=self._serve_ready_wait_budget())
        except Exception:
            pass
        return publish_url

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
                    "anyway",
                    self._rtsp_name,
                    timeout,
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
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
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
        frames = (pcm[i : i + n].ljust(n, b"\x00") for i in range(0, len(pcm), n))

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

    async def async_stop_sd_playback(self) -> None:
        """End a card recording's playback early (``aidot.stop_sd_playback``).

        The capture stops at its next look (within half a second), keeps what
        it has, and gives the media slot back to the live view.
        """
        if not self.coordinator.request_sd_playback_stop():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="sd_playback_not_running",
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
            binary,
            "-nostdin",
            "-protocol_whitelist",
            "http,https,tcp,tls",
            "-t",
            str(max_seconds),
            "-i",
            source,
            "-f",
            "s16le",
            "-ar",
            str(TALK_PCM_RATE),
            "-ac",
            "1",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            pcm, _ = await asyncio.wait_for(proc.communicate(), timeout=max_seconds + 5)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            pcm = b""
        except asyncio.CancelledError:
            # A cancelled service call must not abandon a process whose
            # stdout nobody reads (it would block on a full pipe forever).
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise
        return pcm
