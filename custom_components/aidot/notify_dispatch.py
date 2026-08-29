"""Push notifications for camera motion / person events.

Wiring: the manager coordinator owns one AidotCameraUpdateCoordinator per
camera, each with a motion-listener bus fed by the library's cloud motion poll
(events land ~30 s after a recording starts). AidotMotionNotifier subscribes to
every bus, and re-subscribes whenever the device list refreshes so a camera
added later is covered.

Settings live under options["notifications"] and are read fresh on EVERY
event - an edit on the options page takes effect on the next event with no
entry reload (the reload listener in __init__ ignores that key on purpose: a
reload tears down every camera session).

build_payload() is the one place that decides what a notification looks like.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CONF_NOTIFICATIONS,
    CONF_NOTIFY_CAMERAS,
    CONF_NOTIFY_COOLDOWN_S,
    CONF_NOTIFY_EVENTS,
    CONF_NOTIFY_MESSAGE,
    CONF_NOTIFY_TARGETS,
    CONF_NOTIFY_TITLE,
    DEFAULT_NOTIFY_COOLDOWN_S,
    DEFAULT_NOTIFY_MESSAGE,
    DEFAULT_NOTIFY_TITLE,
    DOMAIN,
    NOTIFY_EVENTS_OFF,
    NOTIFY_EVENTS_PERSON,
)
from .proxy import sign_playback_url

_LOGGER = logging.getLogger(__name__)

# Cloud event-list code for a person detection (event.py maps the same code).
PERSON_EVENT_CODE = "4"
KIND_MOTION = "motion"
KIND_PERSON = "person"
_KIND_TITLES = {KIND_MOTION: "Motion", KIND_PERSON: "Person"}


@dataclass(frozen=True)
class CameraNotifyConfig:
    """The effective notification settings for one camera."""

    events: str          # "all" | "person" (never "off": that resolves to None)
    targets: list[str]   # notify.* service names, camera override or global
    cooldown_s: int
    title: str
    message: str


def resolve_camera_config(options: Mapping[str, Any], dev_id: str) -> CameraNotifyConfig | None:
    """Return the effective settings for ``dev_id``, or None when it is off.

    Off means: no notifications block, no entry for this camera, or the entry's
    ``events`` is "off". Empty/missing per-camera targets fall back to the global
    list; when that is empty too the returned config has ``targets == []`` and
    the caller decides how to report it.
    """
    cfg = options.get(CONF_NOTIFICATIONS) or {}
    cam = (cfg.get(CONF_NOTIFY_CAMERAS) or {}).get(dev_id)
    if not cam:
        return None
    events = cam.get(CONF_NOTIFY_EVENTS, NOTIFY_EVENTS_OFF)
    if events == NOTIFY_EVENTS_OFF:
        return None
    targets = list(cam.get(CONF_NOTIFY_TARGETS) or []) or list(cfg.get(CONF_NOTIFY_TARGETS) or [])
    try:
        cooldown = int(cfg.get(CONF_NOTIFY_COOLDOWN_S, DEFAULT_NOTIFY_COOLDOWN_S))
    except (TypeError, ValueError):
        cooldown = DEFAULT_NOTIFY_COOLDOWN_S
    return CameraNotifyConfig(
        events=events,
        targets=targets,
        cooldown_s=cooldown,
        title=str(cfg.get(CONF_NOTIFY_TITLE) or DEFAULT_NOTIFY_TITLE),
        message=str(cfg.get(CONF_NOTIFY_MESSAGE) or DEFAULT_NOTIFY_MESSAGE),
    )


def event_kind(event: Mapping[str, Any]) -> str:
    """"person" for a person-detection event, "motion" for everything else."""
    code = event.get("eventCode")
    return KIND_PERSON if str(code if code is not None else "") == PERSON_EVENT_CODE else KIND_MOTION


class _Placeholders(dict):
    """format_map source that renders an unknown placeholder as its own name."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, values: Mapping[str, Any]) -> str:
    """Fill ``{placeholder}`` fields; a malformed template comes back unchanged.

    A wording typo must degrade the notification, never drop it.
    """
    try:
        return template.format_map(_Placeholders(values))
    except (ValueError, IndexError, KeyError, AttributeError, TypeError):
        return template


def _event_local_time(event: Mapping[str, Any]) -> str:
    """HH:MM local time of the event, from its cloud timestamp when present."""
    raw = event.get("eventTime")
    try:
        if raw is not None and raw != "":
            stamp = float(raw) / 1000.0
            return dt_util.as_local(dt_util.utc_from_timestamp(stamp)).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return dt_util.now().strftime("%H:%M")


def build_payload(
    *,
    dev_id: str,
    camera_name: str,
    kind: str,
    event: Mapping[str, Any],
    title_template: str,
    message_template: str,
    camera_entity_id: str | None,
    now: float | None = None,
) -> dict[str, Any]:
    """The notify.* service payload - this is the notification's shape.

    ``data`` follows the Home Assistant companion app conventions:
      tag          - repeats for the same camera REPLACE the previous one
      group        - all camera pushes collapse into one group
      image        - the cloud's signed still of the event (phone-reachable)
      url / clickAction - tap opens the clip through the integration's proxy
                     (relative, so the app opens it inside Home Assistant)
      entity_id    - the camera entity, so iOS can show a live attachment
    Any of the rich keys is omitted when its source is absent; the text-only
    notification still goes out. ``time`` is the event's own local time when
    the cloud event carries a parseable ``eventTime``, falling back to now.
    """
    values = {
        "camera": camera_name,
        "event": kind,
        "event_title": _KIND_TITLES.get(kind, "Motion"),
        "time": _event_local_time(event),
        "device_id": dev_id,
    }
    data: dict[str, Any] = {"tag": f"aidot-{dev_id}", "group": "aidot-motion"}
    if pic := event.get("picUrl"):
        data["image"] = pic
    if uuid := event.get("eventUuid"):
        clip = sign_playback_url(dev_id, str(uuid), now=now)
        data["url"] = clip
        data["clickAction"] = clip
    if camera_entity_id:
        data["entity_id"] = camera_entity_id
    return {
        "title": render(title_template, values),
        "message": render(message_template, values),
        "data": data,
    }


class CooldownGate:
    """Per-camera suppression window.

    A person event may pass through a window that a plain motion event started
    (a person after a cat still gets through); a window started by a person
    event holds for everything.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[float, str]] = {}

    def allows(self, dev_id: str, kind: str, cooldown_s: int, now: float) -> bool:
        if cooldown_s <= 0:
            return True
        last = self._last.get(dev_id)
        if last is None:
            return True
        last_at, last_kind = last
        if now - last_at >= cooldown_s:
            return True
        return kind == KIND_PERSON and last_kind == KIND_MOTION

    def record(self, dev_id: str, kind: str, now: float) -> None:
        self._last[dev_id] = (now, kind)


class AidotMotionNotifier:
    """Subscribes to every camera's motion bus and dispatches notify.* calls."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self.hass = hass
        self.entry = entry
        self._unsubs: dict[str, Callable[[], None]] = {}
        self._manager_unsub: Callable[[], None] | None = None
        self._gate = CooldownGate()
        self._warned_no_target: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    @callback
    def start(self) -> None:
        """Attach now and re-attach after every device-list refresh."""
        self.attach()
        manager = getattr(self.entry, "runtime_data", None)
        add = getattr(manager, "async_add_listener", None)
        if add is not None and self._manager_unsub is None:
            self._manager_unsub = add(self.attach)

    @callback
    def attach(self) -> None:
        """Subscribe to each camera coordinator's motion bus (idempotent)."""
        manager = getattr(self.entry, "runtime_data", None)
        coordinators: Mapping[str, Any] = getattr(manager, "camera_coordinators", None) or {}
        for dev_id, coord in coordinators.items():
            if dev_id in self._unsubs:
                continue
            add = getattr(coord, "add_motion_listener", None)
            if add is None:
                continue
            self._unsubs[dev_id] = add(partial(self._on_motion, dev_id, coord))
        for dev_id in [d for d in self._unsubs if d not in coordinators]:
            self._unsubs.pop(dev_id)()

    @callback
    def detach(self) -> None:
        """Drop every subscription; called on entry unload."""
        if self._manager_unsub is not None:
            self._manager_unsub()
            self._manager_unsub = None
        for unsub in self._unsubs.values():
            unsub()
        self._unsubs.clear()

    # -- dispatch ------------------------------------------------------------

    @callback
    def _on_motion(self, dev_id: str, coord: Any, event: Mapping[str, Any]) -> None:
        """Bus callback: run the send as an entry-owned task.

        With Home Assistant's eager task start the send runs inline up to its
        first suspension, which is what keeps the cooldown check ordered with
        the event; the task exists so a failure inside the send can never
        surface in the motion poll and so the entry cancels it on unload.
        """
        coro = self._send(dev_id, coord, event)
        try:
            self.entry.async_create_task(self.hass, coro, name=f"aidot-notify-{dev_id}")
        except Exception:  # never let a scheduling problem reach the bus
            coro.close()
            _LOGGER.exception("Could not schedule a motion notification for %s", dev_id)

    async def _send(self, dev_id: str, coord: Any, event: Mapping[str, Any]) -> None:
        try:
            await self._send_inner(dev_id, coord, event)
        except Exception:
            _LOGGER.exception("Motion notification for %s failed", dev_id)

    async def _send_inner(self, dev_id: str, coord: Any, event: Mapping[str, Any]) -> None:
        options = getattr(self.entry, "options", None) or {}
        cfg = resolve_camera_config(options, dev_id)
        if cfg is None:
            return
        kind = event_kind(event)
        if cfg.events == NOTIFY_EVENTS_PERSON and kind != KIND_PERSON:
            return
        now = time.monotonic()
        if not self._gate.allows(dev_id, kind, cfg.cooldown_s, now):
            _LOGGER.debug("Motion notification for %s suppressed by cooldown", dev_id)
            return
        info = getattr(getattr(coord, "device_client", None), "info", None)
        camera_name = getattr(info, "name", None) or dev_id
        if not cfg.targets:
            if dev_id not in self._warned_no_target:
                self._warned_no_target.add(dev_id)
                _LOGGER.warning(
                    "Motion notifications are enabled for %s but no notify target is "
                    "configured (set one under the camera or in the global "
                    "notification options)", camera_name)
            return
        entity_id = er.async_get(self.hass).async_get_entity_id("camera", DOMAIN, dev_id)
        payload = build_payload(
            dev_id=dev_id,
            camera_name=camera_name,
            kind=kind,
            event=event,
            title_template=cfg.title,
            message_template=cfg.message,
            camera_entity_id=entity_id,
        )
        # Recorded before the calls go out: two events landing in the same
        # window must not both pass the gate while the first is still sending.
        self._gate.record(dev_id, kind, now)
        sent = False
        for target in cfg.targets:
            try:
                await self.hass.services.async_call("notify", target, payload, blocking=False)
                sent = True
            except Exception as exc:  # unknown service, bad payload, ...
                _LOGGER.warning(
                    "Motion notification for %s to notify.%s failed: %s",
                    camera_name, target, exc)
        if sent:
            self._warned_no_target.discard(dev_id)
