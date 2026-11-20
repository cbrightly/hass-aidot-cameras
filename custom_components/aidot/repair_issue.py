"""The plumbing the per-device repair issues share.

``lan_issue``, ``wifi_issue`` and ``pin_issue`` each decide when their issue
is wanted and what it says; raising, refreshing and deleting it is the same
for all of them.
Everything here is best-effort and never raises.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN


def issue_id(key: str, dev_id: str) -> str:
    return f"{key}_{dev_id}"


def clear_issues(
    hass: HomeAssistant, key: str, dev_ids, *, logger: logging.Logger, what: str
) -> None:
    """Delete the ``key`` issues of these devices."""
    from homeassistant.helpers import issue_registry as ir

    for dev_id in dev_ids:
        try:
            ir.async_delete_issue(hass, DOMAIN, issue_id(key, dev_id))
        except Exception:
            logger.debug("could not clear the %s repair issue", what, exc_info=True)


def update_issue(
    hass: HomeAssistant,
    key: str,
    dev_id: str,
    *,
    raise_it: bool,
    placeholders: Callable[[], dict[str, str]],
    logger: logging.Logger,
    what: str,
    fixable: bool = False,
    data: dict[str, str] | None = None,
) -> None:
    """Raise (or refresh) one device's ``key`` issue, or delete it.

    ``placeholders`` is only called when the issue is raised. A ``fixable``
    issue offers its fix flow (repairs.py), which receives ``data``.
    """
    from homeassistant.helpers import issue_registry as ir

    try:
        if raise_it:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id(key, dev_id),
                is_fixable=fixable,
                is_persistent=False,
                data=data,
                severity=ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders=placeholders(),
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id(key, dev_id))
    except Exception:  # a test double for hass, or a registry not yet loaded
        logger.debug("could not update the %s repair issue", what, exc_info=True)
