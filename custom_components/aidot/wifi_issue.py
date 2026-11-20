"""A repair issue for a camera whose weak Wi-Fi keeps stalling its live view.

A battery camera at the edge of its access point's range answers the stream
request and then sends nothing; the attempt is abandoned and the retry is
what serves, 30 s or more after the click, or nothing arrives before the view
gives up. The fix is moving the camera or the access point, and the only sign
was a blank card. This module says so in Settings, per camera, when BOTH hold:

- the Wi-Fi signal the camera reports (the cloud's ``networkRssi``, the source
  of the wifi_rssi sensor) has stayed below WIFI_ISSUE_RSSI_RAISE_DBM for
  WIFI_ISSUE_RSSI_WINDOW_S, over at least WIFI_ISSUE_RSSI_MIN_READINGS
  readings, and
- at least WIFI_ISSUE_STALLED_OPENS live views inside WIFI_ISSUE_OPEN_WINDOW_S
  were slow to start or did not start (see ``note_open``).

Either alone is not enough. A weak signal that streams is not a fault worth a
repair, and a slow open on a good signal is the camera's wake, not its Wi-Fi.

It clears when either recovers, with hysteresis both ways: the signal reads
WIFI_ISSUE_RSSI_CLEAR_DBM or better on WIFI_ISSUE_RSSI_CLEAR_READINGS readings
in a row, or the last WIFI_ISSUE_GOOD_OPENS_TO_CLEAR views all started
promptly, or every counted stall has aged out of the window. A camera that is
offline or switched off has no issue: its signal reading is stale and it is
not being opened, so there is nothing to say about its Wi-Fi.

The open signal is the integration's own: how long a cold view waited for the
camera's publisher to attach to go2rtc. Nothing here reads library internals.
The state lives in memory, so a restart starts the count again.
"""

from __future__ import annotations

from collections import deque
import logging

from homeassistant.core import HomeAssistant

from . import repair_issue
from .const import (
    WIFI_ISSUE_GOOD_OPENS_TO_CLEAR,
    WIFI_ISSUE_OPEN_WINDOW_S,
    WIFI_ISSUE_RSSI_CLEAR_DBM,
    WIFI_ISSUE_RSSI_CLEAR_READINGS,
    WIFI_ISSUE_RSSI_MIN_READINGS,
    WIFI_ISSUE_RSSI_RAISE_DBM,
    WIFI_ISSUE_RSSI_WINDOW_S,
    WIFI_ISSUE_SLOW_OPEN_S,
    WIFI_ISSUE_STALLED_OPENS,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_KEY = "weak_wifi"


def issue_id(dev_id: str) -> str:
    return repair_issue.issue_id(ISSUE_KEY, dev_id)


def open_outcome(elapsed_s: float, attached: bool) -> bool | None:
    """What one cold view says: True stalled, False prompt, None nothing.

    ``elapsed_s`` is how long the view waited for the publisher and
    ``attached`` whether it came. A wait of WIFI_ISSUE_SLOW_OPEN_S or more is
    a stall whether or not the publisher arrived in the end: a prompt battery
    open attaches in 5-13 s, and one whose first attempt was abandoned to the
    retry arrives past 45 s. A wait that gave up sooner says nothing: the
    short budgets (the HLS path, a mains camera) end before a slow open could
    be told from a normal one, and an unreadable go2rtc ends the wait at once.
    """
    if elapsed_s >= WIFI_ISSUE_SLOW_OPEN_S:
        return True
    return False if attached else None


class WeakWifiTracker:
    """One camera's readings, and whether they call for the issue.

    Pure: every method takes the time, so the rules run on a fake clock.
    """

    def __init__(self) -> None:
        #: Whether the issue is wanted right now (the hysteresis state).
        self.raised = False
        #: The newest usable signal reading, dBm.
        self.last_rssi: int | None = None
        # The unbroken run of readings below the raise threshold.
        self._weak_since: float | None = None
        self._weak_readings = 0
        # The unbroken run of readings at or above the clear threshold.
        self._strong_readings = 0
        # (time, stalled) per counted view, oldest first.
        self._opens: deque[tuple[float, bool]] = deque()

    # ------------------------------------------------------------- inputs
    def note_rssi(self, rssi: int | None, now: float) -> None:
        """One poll's signal reading. None, zero or positive is no reading:
        a signal in dBm is negative, and anything else is a camera that
        reports something other than dBm, or nothing."""
        if rssi is None or isinstance(rssi, bool):
            return
        try:
            rssi = int(rssi)
        except (TypeError, ValueError):
            return
        if rssi >= 0:
            return
        self.last_rssi = rssi
        if rssi < WIFI_ISSUE_RSSI_RAISE_DBM:
            if self._weak_since is None:
                self._weak_since = now
            self._weak_readings += 1
            self._strong_readings = 0
        elif rssi >= WIFI_ISSUE_RSSI_CLEAR_DBM:
            self._strong_readings += 1
            self._forget_weak_run()
        else:
            # Between the two thresholds: not weak enough to count toward
            # raising, not strong enough to count toward clearing.
            self._strong_readings = 0
            self._forget_weak_run()

    def note_open(self, stalled: bool, now: float) -> None:
        """One counted view: stalled, or started promptly."""
        self._opens.append((now, bool(stalled)))

    def note_unavailable(self) -> None:
        """The camera is offline or switched off: no issue, and the signal
        has to be seen weak for a whole window again once it is back."""
        self.raised = False
        self._strong_readings = 0
        self._forget_weak_run()

    # ------------------------------------------------------------ verdict
    def evaluate(self, now: float) -> bool:
        """Apply the rules to what has been noted; returns ``raised``."""
        self._prune(now)
        if self.raised:
            if self._rssi_recovered():
                self.raised = False
            elif self._opens_recovered():
                self.raised = False
                # The stalls behind this issue are answered. Without this
                # one more stall would find them still in the window and
                # raise it again on a single bad view.
                self._opens.clear()
        elif self._rssi_weak(now) and self.stalled_opens() >= WIFI_ISSUE_STALLED_OPENS:
            if self._last_opens_all_good():
                # Enough stalls in the window, but the newest views are fine.
                return self.raised
            self.raised = True
        return self.raised

    def stalled_opens(self) -> int:
        """Stalled views still inside the window (as of the last evaluate)."""
        return sum(1 for _ts, stalled in self._opens if stalled)

    # ------------------------------------------------------------ helpers
    def _forget_weak_run(self) -> None:
        self._weak_since = None
        self._weak_readings = 0

    def _prune(self, now: float) -> None:
        while self._opens and now - self._opens[0][0] > WIFI_ISSUE_OPEN_WINDOW_S:
            self._opens.popleft()

    def _rssi_weak(self, now: float) -> bool:
        return (
            self._weak_since is not None
            and self._weak_readings >= WIFI_ISSUE_RSSI_MIN_READINGS
            and now - self._weak_since >= WIFI_ISSUE_RSSI_WINDOW_S
        )

    def _rssi_recovered(self) -> bool:
        return self._strong_readings >= WIFI_ISSUE_RSSI_CLEAR_READINGS

    def _last_opens_all_good(self) -> bool:
        if len(self._opens) < WIFI_ISSUE_GOOD_OPENS_TO_CLEAR:
            return False
        newest = list(self._opens)[-WIFI_ISSUE_GOOD_OPENS_TO_CLEAR:]
        return not any(stalled for _ts, stalled in newest)

    def _opens_recovered(self) -> bool:
        return self.stalled_opens() == 0 or self._last_opens_all_good()


def camera_is_up(device_client, status) -> bool:
    """Neither reported offline by the cloud nor switched off."""
    if getattr(status, "powered", None) is False:
        return False
    return not bool(getattr(device_client, "cloud_reports_offline", False))


def clear_for(hass: HomeAssistant, dev_ids) -> None:
    """Delete the weak Wi-Fi issues of these cameras (unload, removal).

    Best-effort: never raises.
    """
    repair_issue.clear_issues(
        hass, ISSUE_KEY, dev_ids, logger=_LOGGER, what="weak Wi-Fi"
    )


def note_weak_wifi(
    hass: HomeAssistant,
    dev_id: str,
    *,
    name: str,
    rssi: int | None,
    stalled_opens: int,
    raise_it: bool,
) -> None:
    """Raise (or refresh the figures of) or clear one camera's issue.

    Best-effort: never raises.
    """
    repair_issue.update_issue(
        hass,
        ISSUE_KEY,
        dev_id,
        raise_it=raise_it,
        placeholders=lambda: {
            "name": name,
            "rssi": "?" if rssi is None else str(rssi),
            "threshold": str(WIFI_ISSUE_RSSI_RAISE_DBM),
            "clear": str(WIFI_ISSUE_RSSI_CLEAR_DBM),
            "minutes": str(int(WIFI_ISSUE_RSSI_WINDOW_S // 60)),
            "stalls": str(stalled_opens),
            "hours": str(int(WIFI_ISSUE_OPEN_WINDOW_S // 3600)),
            "slow": str(int(WIFI_ISSUE_SLOW_OPEN_S)),
        },
        logger=_LOGGER,
        what="weak Wi-Fi",
    )
