"""A repair issue for a light whose LAN address is on another subnet.

The Mujoy light on the reference installation sat at 192.168.1.254 while Home
Assistant lives on 192.168.0.x: every LAN login failed with no route for weeks,
and the only sign was a warning in the log. The library's retry ceiling says
when a device's login keeps failing (`lan_login_over_ceiling`); this module
says WHY, when the why is the network, in Settings where it is seen.

Only the off-subnet case gets an issue. A light inside one of Home Assistant's
networks that still fails is a different fault (powered off, mostly), and the
cloud's online flag already covers it.
"""

from __future__ import annotations

import logging
from ipaddress import IPv4Network, ip_address

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_KEY = "light_off_subnet"


def issue_id(dev_id: str) -> str:
    return f"{ISSUE_KEY}_{dev_id}"


def address_is_off_subnet(address: str | None, networks: list[IPv4Network]) -> bool:
    """True when ``address`` is an IPv4 address inside none of ``networks``.

    No networks, or an unparseable address, is never "off subnet": the
    question cannot be answered, and an issue must not be raised on a guess.
    """
    if not address or not networks:
        return False
    try:
        ip = ip_address(address)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return not any(ip in net for net in networks)


def wants_issue(device_client, networks: list[IPv4Network]) -> bool:
    """Whether this light's state calls for the issue right now.

    Reads only the library's public accessors; an older library without them
    answers False, so nothing here depends on a floor bump.
    """
    over = getattr(device_client, "lan_login_over_ceiling", None)
    if over is None or not over:
        return False
    status = getattr(device_client, "status", None)
    if not bool(getattr(status, "online", False)):
        return False
    return address_is_off_subnet(getattr(device_client, "lan_address", None), networks)


def note_light_off_subnet(
    hass: HomeAssistant,
    dev_id: str,
    *,
    name: str,
    address: str | None,
    networks: list[IPv4Network],
    raise_it: bool,
) -> None:
    """Raise or clear the issue for one light. Best-effort: never raises."""
    from homeassistant.helpers import issue_registry as ir

    try:
        if raise_it:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id(dev_id),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_KEY,
                translation_placeholders={
                    "name": name,
                    "address": address or "?",
                    "networks": ", ".join(str(n) for n in networks),
                },
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id(dev_id))
    except Exception:  # a test double for hass, or a registry not yet loaded
        _LOGGER.debug("could not update the off-subnet repair issue", exc_info=True)
