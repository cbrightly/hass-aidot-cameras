"""Which go2rtc this integration publishes to.

Three servers can exist on one host: one the owner points at explicitly
(`AIDOT_GO2RTC_API`), the WebRTC add-on's on 127.0.0.1:1984 / 8554, and Home
Assistant's bundled one, which keeps its API on a unix socket behind a cosmetic
`http://localhost:11984/` URL and its RTSP on 127.0.0.1:18554 (HA
`components/go2rtc/__init__.py` and `server.py`). Until now only the first two
were ever used, so an installation with only the bundled server never published
at all. The order here keeps every existing installation where it is: an
explicit URL first, then a reachable add-on, then the bundled server.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from aidot_cameras.camera.go2rtc import (
    DEFAULT_BASE_URL,
    DEFAULT_RTSP_PORT,
    Go2rtcClient,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KIND_EXPLICIT = "explicit"
KIND_ADDON = "addon"
KIND_BUNDLED = "bundled"
BUNDLED_RTSP_PORT = 18554
#: The URL Home Assistant gives its own managed go2rtc (HA go2rtc/const.py,
#: HA_MANAGED_URL). A user's `go2rtc: url:` also fills hass.data["go2rtc"],
#: but that server's RTSP is not 127.0.0.1:18554, so only this one counts.
HA_MANAGED_URL = "http://localhost:11984/"
#: Off: never publish into Home Assistant's bundled go2rtc. A published stream
#: carries a producer with no `url` (the RTSP publisher), and HA's go2rtc
#: provider decodes the server's whole stream list on every WebRTC offer with
#: go2rtc_client 0.4.0, whose Producer requires `url`. So while one AiDot
#: camera published there, every camera's WebRTC offer failed with
#: `MissingField: url` (measured on HA 2026.10.0, 2026-10-11). Turn this back on
#: only once HA's client accepts a producer without a url.
PUBLISH_INTO_BUNDLED = False

_TTL_S = 60.0
_STORE = "go2rtc-target"
#: Set once the add-on has answered in this process; see _resolve.
_ADDON_SEEN = "go2rtc-addon-seen"
#: The kind last announced at INFO, so an uncached answer is not re-announced.
_ANNOUNCED = "go2rtc-target-announced"
# Whether the last resolved target answered, kept apart from _STORE so that
# invalidate() (which a failed registration calls on every view) does not make
# the "did not answer" line repeat once per view during an outage.
_LAST_ANSWERED = "go2rtc-target-last-answered"
#: Whether the last resolved target is read by HA's provider; see _ha_reads_target.
_HA_READS = "go2rtc-target-ha-reads"
#: Dispatched with the new value whenever that flips, so cameras holding a
#: publishing session can stop it (a running session keeps its first target).
SIGNAL_GO2RTC_HA_READS = "aidot_go2rtc_ha_reads"
#: Repairs issue raised while HA's provider reads the server we would publish into.
HA_READS_ISSUE = "go2rtc_shared_with_ha"


@dataclass(frozen=True)
class Go2rtcTarget:
    api_url: str
    session: Any
    rtsp_host: str
    rtsp_port: int
    kind: str
    #: False only for an add-on that answered earlier in this process but not
    #: at the latest probe: still the target (see _resolve), but nothing to
    #: publish a pullable camera into right now.
    answered: bool = True
    #: True when Home Assistant's own go2rtc provider reads this same server
    #: (see _ha_reads_target). Nothing may be published into it then: HA's
    #: client cannot decode a published stream and fails every WebRTC offer.
    ha_reads: bool = False

    def client(self, timeout: float = 5.0) -> Go2rtcClient:
        return Go2rtcClient(self.session, self.api_url, timeout=timeout)

    def rtsp_url(self, name: str) -> str:
        return f"rtsp://{self.rtsp_host}:{self.rtsp_port}/{name}"


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or "127.0.0.1"


def _rtsp_port_override() -> int:
    """AIDOT_GO2RTC_RTSP_PORT, defensively: a bad value falls back and warns.

    A typo'd port must not break resolve_go2rtc_target on every call (it
    would never get cached), so a non-integer or out-of-range value logs one
    warning naming the variable and its value and falls back to the add-on's
    default port instead of raising.
    """
    raw = os.environ.get("AIDOT_GO2RTC_RTSP_PORT")
    if not raw:
        return DEFAULT_RTSP_PORT
    try:
        port = int(raw)
    except ValueError:
        port = None
    if port is None or not (1 <= port <= 65535):
        _LOGGER.warning(
            "AIDOT_GO2RTC_RTSP_PORT=%r is not a valid port (1-65535); using %d",
            raw,
            DEFAULT_RTSP_PORT,
        )
        return DEFAULT_RTSP_PORT
    return port


#: The go2rtc versions Home Assistant's client (go2rtc_client 0.4.0,
#: rest.py _MIN_VERSION_SUPPORTED / _MIN_VERSION_UNSUPPORTED) accepts. HA does
#: not load its provider for a server outside this range, so such a server is
#: never read by HA and cannot be broken by a published stream.
_HA_CLIENT_MIN = "1.9.13"
_HA_CLIENT_MAX = "2.0.0"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _ha_client_accepts(value) -> bool | None:
    """Whether HA's client would load its provider for this go2rtc version.

    Parsed the way HA's client parses it (AwesomeVersion), so a version with a
    suffix ("1.9.14-dev") is judged as HA judges it. None when unparseable.
    """
    from awesomeversion import AwesomeVersion, AwesomeVersionException

    try:
        v = AwesomeVersion(str(value))
        return AwesomeVersion(_HA_CLIENT_MIN) <= v < AwesomeVersion(_HA_CLIENT_MAX)
    except (AwesomeVersionException, TypeError, ValueError):
        return None


def _host_port(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme)
    return (parts.hostname or "").lower(), port


def _same_loopback(a: str, b: str) -> bool:
    """Both URLs name this host's loopback at the same port."""
    ha, pa = _host_port(a)
    hb, pb = _host_port(b)
    return ha in _LOOPBACK and hb in _LOOPBACK and pa is not None and pa == pb


async def _get_json(session, url: str, timeout: float = 3.0):
    """GET url and parse JSON with the raw session, never HA's decoder."""
    import aiohttp

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


async def _this_host_addresses(hass: HomeAssistant) -> set[str]:
    """This host's own addresses: loopback plus every adapter address HA knows."""
    found = set(_LOOPBACK)
    try:
        from homeassistant.components.network import async_get_adapters

        for adapter in await async_get_adapters(hass):
            for v4 in adapter.get("ipv4") or []:
                if v4.get("address"):
                    found.add(str(v4["address"]).lower())
            for v6 in adapter.get("ipv6") or []:
                if v6.get("address"):
                    found.add(str(v6["address"]).lower())
    except Exception:
        _LOGGER.debug("could not read the network adapters", exc_info=True)
    return found


async def _same_server_by_address(hass: HomeAssistant, a: str, b: str) -> bool:
    """Both URLs name this host (loopback or one of its own addresses) at the
    same port: one listener, so one server."""
    ha, pa = _host_port(a)
    hb, pb = _host_port(b)
    if pa is None or pa != pb:
        return False
    if ha == hb:
        return True
    mine = await _this_host_addresses(hass)
    return ha in mine and hb in mine


async def _ha_reads_target(hass: HomeAssistant, api_url: str, session) -> bool | None:
    """Whether Home Assistant's go2rtc provider reads the server at api_url.

    HA's provider lists the server's whole /api/streams on every WebRTC offer,
    and its client cannot decode a stream a camera publishes into (the
    publisher has no url), so publishing into the server HA reads fails every
    camera's live view. True, False, or None when it cannot be told right now
    (the caller keeps its last answer). Gates, cheapest first:

    1. No handle, or HA's own managed server: False (installs without a
       ``go2rtc: url:`` line, the reference topology, stop here, no probe).
    2. HA's ``url:`` server outside the versions HA's client accepts: False,
       HA never loads its provider for it.
    3. Identity: HA's server lists our ``aidot_*`` streams (only this
       integration names streams so), or both URLs name this host at the same
       port, or both list the same non-empty set of streams. Build details
       (version, revision, config path) are not compared: two installs of one
       release share them.
    4. HA's server cannot be read: True only by address (same host and port),
       otherwise None.
    """
    cfg = hass.data.get("go2rtc")
    url = getattr(cfg, "url", None)
    ha_session = getattr(cfg, "session", None)
    if not (isinstance(url, str) and url and ha_session is not None):
        return False
    if url.rstrip("/") == HA_MANAGED_URL.rstrip("/"):
        return False
    base = url.rstrip("/")
    info = await _get_json(ha_session, f"{base}/api")
    if not isinstance(info, dict):
        return True if await _same_server_by_address(hass, url, api_url) else None
    accepts = _ha_client_accepts(info.get("version"))
    if accepts is False:
        return False
    theirs = await _get_json(ha_session, f"{base}/api/streams")
    if isinstance(theirs, dict) and any(str(k).startswith("aidot_") for k in theirs):
        return True
    if await _same_server_by_address(hass, url, api_url):
        return True
    our_streams = await _get_json(session, f"{api_url.rstrip('/')}/api/streams")
    if (
        isinstance(theirs, dict)
        and isinstance(our_streams, dict)
        and theirs
        and set(theirs) == set(our_streams)
    ):
        return True
    if not isinstance(theirs, dict) or accepts is None:
        return None
    return False


async def _addon_answers(hass: HomeAssistant) -> bool:
    """Does a go2rtc answer on the add-on's address? A 3 s probe."""
    client = Go2rtcClient(async_get_clientsession(hass), DEFAULT_BASE_URL, timeout=3.0)
    return await client.available()


def _bundled_handle(hass: HomeAssistant):
    """HA's own go2rtc, if its handle has the shape this integration knows."""
    cfg = hass.data.get("go2rtc")
    url = getattr(cfg, "url", None)
    session = getattr(cfg, "session", None)
    if not (isinstance(url, str) and url and session is not None):
        return None
    if url.rstrip("/") != HA_MANAGED_URL.rstrip("/"):
        return None  # a user-configured server: its RTSP address is unknown
    return url, session


def invalidate(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMAIN, {}).pop(_STORE, None)


async def resolve_go2rtc_target(hass: HomeAssistant) -> Go2rtcTarget | None:
    """The go2rtc to publish to, cached for a minute; None when there is none."""
    store = hass.data.setdefault(DOMAIN, {})
    cached = store.get(_STORE)
    now = time.monotonic()
    if cached and now - cached[0] < _TTL_S:
        return cached[1]
    target = await _resolve(hass)
    kind = target.kind if target else None
    if kind != store.get(_ANNOUNCED, ""):
        store[_ANNOUNCED] = kind
        _LOGGER.info(
            "go2rtc: publishing to %s",
            f"the {target.kind} server at {target.api_url} (RTSP {target.rtsp_host}:{target.rtsp_port})"
            if target
            else "nothing - no go2rtc answered; cameras that can be pulled are served that way",
        )
    was_answered = store.get(_LAST_ANSWERED, True)
    store[_LAST_ANSWERED] = target is None or target.answered
    if target is not None and not target.answered and was_answered:
        _LOGGER.info(
            "go2rtc did not answer at %s; cameras that can be pulled will be"
            " served that way instead of publishing",
            target.api_url,
        )
    _note_ha_reads(hass, store, target)
    # While Home Assistant is still starting, the add-on may simply not be up
    # yet, so a None then (see _resolve) is not cached: the next call asks again.
    if hass.state is CoreState.running or kind not in (KIND_BUNDLED, None):
        store[_STORE] = (now, target)
    return target


def _note_ha_reads(hass: HomeAssistant, store: dict, target) -> None:
    """On a change of target.ha_reads: log, raise or clear the issue, signal."""
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    reads = bool(target is not None and target.ha_reads)
    if reads == store.get(_HA_READS, False):
        return
    store[_HA_READS] = reads
    ha_url = getattr(hass.data.get("go2rtc"), "url", None) or "?"
    try:
        if reads:
            _LOGGER.warning(
                "go2rtc: Home Assistant's go2rtc (url: %s) is the server the"
                " cameras would publish into, and Home Assistant cannot read a"
                " published stream - every camera's live view would fail."
                " Standard cameras are pulled instead and SDES cameras are not"
                " streamed; see Settings -> Repairs",
                ha_url,
            )
            ir.async_create_issue(
                hass,
                DOMAIN,
                HA_READS_ISSUE,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=HA_READS_ISSUE,
                translation_placeholders={"url": ha_url},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, HA_READS_ISSUE)
    except Exception:  # a test double for hass, or a registry not yet loaded
        _LOGGER.debug("could not update the go2rtc shared issue", exc_info=True)
    async_dispatcher_send(hass, SIGNAL_GO2RTC_HA_READS, reads)


async def _ha_reads_or_last(
    hass: HomeAssistant, api_url: str, session, answered: bool
) -> bool:
    """_ha_reads_target, keeping the last answer while it cannot be told.

    An add-on that is restarting cannot be read, and neither can HA's server
    when it is the same one; letting that read as "not read" cleared the flag,
    let sessions publish into the server when it came back, and broke every
    view until the next resolve. So an unanswered target, or an unknown
    answer, keeps the previous value.
    """
    last = bool(hass.data.setdefault(DOMAIN, {}).get(_HA_READS, False))
    if not answered:
        return last
    reads = await _ha_reads_target(hass, api_url, session)
    return last if reads is None else reads


async def _resolve(hass: HomeAssistant) -> Go2rtcTarget | None:
    explicit = os.environ.get("AIDOT_GO2RTC_API")
    if explicit:
        session = async_get_clientsession(hass)
        return Go2rtcTarget(
            explicit,
            session,
            _host_of(explicit),
            _rtsp_port_override(),
            KIND_EXPLICIT,
            ha_reads=await _ha_reads_or_last(hass, explicit, session, True),
        )
    store = hass.data.setdefault(DOMAIN, {})
    answers = await _addon_answers(hass)
    if answers:
        store[_ADDON_SEEN] = True
    # Sticky: once the add-on has answered in this process, a failed probe does
    # not move cameras to the bundled server. A mains session opened there
    # would stay there (the library keeps a running session), while the view
    # path later hands out the add-on's URL with nobody publishing to it. The
    # add-on stays the target, marked unanswered, which is what a failed probe
    # always meant here: pullable cameras use their serve, SDES still publishes.
    if answers or store.get(_ADDON_SEEN):
        session = async_get_clientsession(hass)
        return Go2rtcTarget(
            DEFAULT_BASE_URL,
            session,
            "127.0.0.1",
            DEFAULT_RTSP_PORT,
            KIND_ADDON,
            answered=answers,
            ha_reads=await _ha_reads_or_last(hass, DEFAULT_BASE_URL, session, answers),
        )
    # Never the bundled server before Home Assistant is running: the add-on may
    # simply not be up yet, and a session opened into the bundled server then
    # (motion prewarm, a live view, the opener) stays there until restart. With
    # None, standard cameras pull and SDES cameras get the add-on's push URL,
    # as before the bundled server was ever chosen; resolve_go2rtc_target does
    # not cache that None, so the first call once HA is running decides.
    if hass.state is not CoreState.running:
        return None
    if not PUBLISH_INTO_BUNDLED:
        return None
    handle = _bundled_handle(hass)
    if handle is not None:
        url, session = handle
        return Go2rtcTarget(url, session, "127.0.0.1", BUNDLED_RTSP_PORT, KIND_BUNDLED)
    return None
