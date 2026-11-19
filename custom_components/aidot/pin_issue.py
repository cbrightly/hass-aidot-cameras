"""A repair issue for a standard camera whose DTLS certificate no longer matches.

Once the library has pinned a standard (DTLS) camera's certificate
(dtls_pins.py), a handshake that presents any other certificate is refused and
the live view stays dark. Two things cause that: something on the network
answering in the camera's place, or the camera itself changing certificate
after a factory reset, a firmware update or a replacement unit. Only the owner
can tell which, so this module says so in Settings, per camera, and the
issue's fix (repairs.py) is the "trust the new certificate" action: it drops
the pin, and the camera's next connection learns the certificate again.

The signal is the library's own: the camera's stream health reports the end
reason ``dtls_pin_mismatch``. The issue is raised when a camera's stream
first ends that way, and cleared when the camera streams again. A camera that
keeps failing on the pin raises it once, not on every retry, so a fix that has
just been applied is not undone by the retry that was already under way.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback

from . import repair_issue
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_KEY = "dtls_pin_mismatch"
#: The stream-health end reason the library reports for a refused certificate.
END_REASON = "dtls_pin_mismatch"
_STREAMING = "streaming"


def issue_id(dev_id: str) -> str:
    return repair_issue.issue_id(ISSUE_KEY, dev_id)


def _supported(device_client: Any) -> bool:
    return callable(getattr(device_client, "add_stream_health_listener", None)) and (
        callable(getattr(device_client, "forget_dtls_pin", None))
    )


def note_mismatch(
    hass: HomeAssistant,
    dev_id: str,
    *,
    name: str,
    entry_id: str,
    raise_it: bool,
) -> None:
    """Raise or clear one camera's issue. Best-effort: never raises."""
    repair_issue.update_issue(
        hass,
        ISSUE_KEY,
        dev_id,
        raise_it=raise_it,
        placeholders=lambda: {"name": name},
        logger=_LOGGER,
        what="DTLS certificate",
        fixable=True,
        data={"dev_id": dev_id, "entry_id": entry_id, "name": name},
    )


def clear_for(hass: HomeAssistant, dev_ids) -> None:
    """Delete the certificate issues of these cameras (unload, removal).

    Best-effort: never raises.
    """
    repair_issue.clear_issues(
        hass, ISSUE_KEY, dev_ids, logger=_LOGGER, what="DTLS certificate"
    )


class PinIssueWatch:
    """Follows one camera's stream health and keeps its issue in step."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_client: Any,
        *,
        dev_id: str,
        name: str,
        entry_id: str,
    ) -> None:
        self._hass = hass
        self._dc = device_client
        self._dev_id = dev_id
        self._name = name
        self._entry_id = entry_id
        #: Whether this watch raised the issue and has not cleared it since.
        self.raised = False
        # The end reason seen on the previous change, so a camera that keeps
        # failing on the pin is raised once, on the first such end.
        self._last_reason: str | None = None

    @callback
    def changed(self) -> None:
        try:
            health = self._dc.stream_health
            state = getattr(health, "state", None)
            reason = getattr(health, "last_end_reason", None)
        except Exception:  # a client mid-teardown; nothing to say
            return
        if state == _STREAMING:
            # A handshake went through: whatever the certificate was, the
            # camera is streaming again. Forget the old end reason too, so a
            # later refusal is a new one.
            self._last_reason = None
            if self.raised:
                self.raised = False
                _LOGGER.info("%s: DTLS certificate issue cleared", self._dev_id)
                note_mismatch(
                    self._hass,
                    self._dev_id,
                    name=self._name,
                    entry_id=self._entry_id,
                    raise_it=False,
                )
            return
        first = reason == END_REASON and self._last_reason != END_REASON
        self._last_reason = reason
        if not first:
            return
        self.raised = True
        _LOGGER.warning(
            "%s: the camera's DTLS certificate does not match its pin; "
            "live view is refused until it is trusted (Settings > Repairs)",
            self._dev_id,
        )
        note_mismatch(
            self._hass,
            self._dev_id,
            name=self._name,
            entry_id=self._entry_id,
            raise_it=True,
        )


def watch(
    hass: HomeAssistant,
    device_client: Any,
    *,
    dev_id: str,
    name: str,
    entry_id: str,
) -> Callable[[], None] | None:
    """Start following one camera; returns the function that stops it.

    None for a library without per-camera pinning, where there is nothing to
    watch for.
    """
    if not _supported(device_client):
        return None
    tracker = PinIssueWatch(
        hass, device_client, dev_id=dev_id, name=name, entry_id=entry_id
    )
    try:
        remove = device_client.add_stream_health_listener(tracker.changed)
    except Exception:
        _LOGGER.debug("%s: stream health listener unavailable", dev_id, exc_info=True)
        return None
    return remove if callable(remove) else None


def forget_pin(hass: HomeAssistant, dev_id: str) -> bool:
    """Drop one camera's learned pin, wherever it is loaded.

    True when the camera was found (and its pin dropped), False when no
    loaded config entry has it.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinators = getattr(
            getattr(entry, "runtime_data", None), "camera_coordinators", None
        )
        coord = (coordinators or {}).get(dev_id)
        if coord is None:
            continue
        forget = getattr(coord.device_client, "forget_dtls_pin", None)
        if not callable(forget):
            return False
        forget()
        _LOGGER.info("%s: DTLS certificate pin dropped; it is learned again", dev_id)
        return True
    return False
