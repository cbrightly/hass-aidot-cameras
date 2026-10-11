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
_TTL_S = 60.0
_STORE = "go2rtc-target"
#: Set once the add-on has answered in this process; see _resolve.
_ADDON_SEEN = "go2rtc-addon-seen"
#: The kind last announced at INFO, so an uncached answer is not re-announced.
_ANNOUNCED = "go2rtc-target-announced"


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
    was_answered = not (cached and cached[1] and not cached[1].answered)
    if target is not None and not target.answered and was_answered:
        _LOGGER.info(
            "go2rtc did not answer at %s; cameras that can be pulled will be"
            " served that way instead of publishing",
            target.api_url,
        )
    # While Home Assistant is still starting, the add-on may simply not be up
    # yet, so a None then (see _resolve) is not cached: the next call asks again.
    if hass.state is CoreState.running or kind not in (KIND_BUNDLED, None):
        store[_STORE] = (now, target)
    return target


async def _resolve(hass: HomeAssistant) -> Go2rtcTarget | None:
    explicit = os.environ.get("AIDOT_GO2RTC_API")
    if explicit:
        return Go2rtcTarget(
            explicit,
            async_get_clientsession(hass),
            _host_of(explicit),
            _rtsp_port_override(),
            KIND_EXPLICIT,
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
        return Go2rtcTarget(
            DEFAULT_BASE_URL,
            async_get_clientsession(hass),
            "127.0.0.1",
            DEFAULT_RTSP_PORT,
            KIND_ADDON,
            answered=answers,
        )
    # Never the bundled server before Home Assistant is running: the add-on may
    # simply not be up yet, and a session opened into the bundled server then
    # (motion prewarm, a live view, the opener) stays there until restart. With
    # None, standard cameras pull and SDES cameras get the add-on's push URL,
    # as before the bundled server was ever chosen; resolve_go2rtc_target does
    # not cache that None, so the first call once HA is running decides.
    if hass.state is not CoreState.running:
        return None
    handle = _bundled_handle(hass)
    if handle is not None:
        url, session = handle
        return Go2rtcTarget(url, session, "127.0.0.1", BUNDLED_RTSP_PORT, KIND_BUNDLED)
    return None
