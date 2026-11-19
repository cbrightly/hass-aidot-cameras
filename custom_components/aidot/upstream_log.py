"""Quiet upstream ``python-aidot``'s routine chatter without losing real failures.

Upstream 0.3.56 logs at WARNING what 0.3.55 logged at INFO: every ping, every
received frame, every command, every connect, the full login response, and - on
each ``DeviceClient`` construction - the entire raw device record, which
contains the device's ``password`` and ``aesKey``. On a normal fleet that is a
few lines every 30 seconds per device, which buries everything else in the log
and writes device credentials to disk in plaintext.

Home Assistant's ``logger:`` configuration can only set a *level* per logger, and
a level is the wrong instrument here. Of upstream's nine WARNING call sites in
``device_client``, exactly one is worth keeping:

    connect device error: <exc>     <- the ONLY signal that a connect/login failed

Everything else is routine. So ``aidot.device_client: error`` - the obvious
workaround - silences the flood by trading away the single message that tells
you a light cannot connect, leaving that failure completely silent. This module
filters by message instead, so the flood goes and that warning stays.

Design notes:

* **Fails open.** A message that matches no known-noise pattern is always
  emitted. If upstream rewords or adds a log line, the worst case is that some
  noise comes back - never that a new failure is swallowed.
* **ERROR and above always pass**, unconditionally, before any pattern is
  consulted.
* **Debug is an escape hatch.** Set ``aidot.device_client: debug`` in HA's
  ``logger:`` config and the suppressed chatter returns, so it is available when
  actually debugging the LAN protocol. Nothing is permanently lost.
* **The credential dump is dropped at every level**, including debug. There is no
  reason for a device password or AES key to reach the log, and "you had to opt
  in" is not a good enough reason to write one to disk.
"""

from __future__ import annotations

import logging
import re

# Upstream loggers that emit routine chatter at WARNING. `aidot.discover` and
# `aidot.client` are quieter than `device_client` but log on the same pattern.
_UPSTREAM_LOGGERS = ("aidot.device_client", "aidot.discover", "aidot.client")

# Routine per-device / per-frame chatter. Matched against the formatted message.
# Both spellings of the receive line are listed because upstream's own is a typo
# (`reveive_data`) that may well be corrected without otherwise changing the line.
_NOISE = re.compile(
    r"send_ping\b"
    r"|send_action\b"
    r"|re[cv]eive_data\b"
    r"|:connect device:"
    r"|:connect success:"
    r"|:login result:"
    r"|send_broadcast\b"
    r"|datagram_received\b"
    r"|\bsetup_discover\b"
)

# The raw device record upstream dumps on construction. Keyed on the credential
# fields themselves rather than on message shape, so a reworded line still gets
# caught - this is the one pattern where a miss actually matters.
_CREDENTIALS = re.compile(r"'aesKey':|\"aesKey\":|'password':|\"password\":")


class UpstreamNoiseFilter(logging.Filter):
    """Drop upstream's routine chatter; keep everything that could matter."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return True to emit the record."""
        try:
            message = record.getMessage()
        except Exception:  # a broken record must never break logging itself
            return True

        # Never write device credentials to the log, at any level.
        if _CREDENTIALS.search(message):
            return False

        # Real failures always pass, before any pattern matching.
        if record.levelno > logging.WARNING:
            return True

        # Anything we do not positively recognise as noise is emitted.
        if not _NOISE.search(message):
            return True

        # Recognised noise: keep it only when debug was deliberately enabled for
        # this logger, so `aidot.device_client: debug` still shows the protocol.
        return logging.getLogger(record.name).getEffectiveLevel() <= logging.DEBUG


def install() -> None:
    """Attach the filter to upstream's loggers. Safe to call repeatedly.

    Filters attached to a logger run for records created *by that logger*, so
    each upstream logger is handled by name rather than relying on propagation
    from a parent.
    """
    for name in _UPSTREAM_LOGGERS:
        logger = logging.getLogger(name)
        if any(isinstance(f, UpstreamNoiseFilter) for f in logger.filters):
            continue
        logger.addFilter(UpstreamNoiseFilter())
