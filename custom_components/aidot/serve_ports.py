"""Stable, collision-free local serve ports for cameras.

Each pulled camera serves its stream on a loopback HTTP port, and that port is
part of the URL handed to go2rtc and Home Assistant, so it must not move
between restarts. The port used to be ``base + crc32(name) % 400`` - stable,
but two cameras could land on the same port (about 5% with seven cameras),
and the second one to bind lost its stream.

Each camera now gets a SLOT (0-399, the offset from the configurable base)
that is assigned once and persisted in Home Assistant storage:

- A new camera takes its crc32 slot when that slot is free, so every camera
  that did not collide keeps exactly the port it had before this module.
- When the crc32 slot is taken, it takes the next free slot after it
  (wrapping). A batch of new cameras is assigned in sorted name order, so
  which camera of a colliding pair moves does not depend on discovery order.
- Slots are never reassigned or freed: adding a camera never moves an
  existing one, and a camera removed from the account and added back gets
  its old port again. 400 slots is far more than an account holds.

The map is process-wide (one store, not one per config entry) because every
entry's cameras share the same loopback port range.
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from typing import Any

_LOGGER = logging.getLogger(__name__)

SLOTS = 400
STORAGE_KEY = "aidot_serve_ports"
STORAGE_VERSION = 1

# name -> slot. Filled from storage at setup, then by assignment.
_SLOTS: dict[str, int] = {}
_LOCK = asyncio.Lock()
_LOADED = False
_STORE: Any = None
_HASS: Any = None


def crc_slot(name: str) -> int:
    """The slot the original scheme gave ``name`` (and its first choice now)."""
    return zlib.crc32(name.encode()) % SLOTS


def _take(name: str) -> int:
    taken = set(_SLOTS.values())
    first = crc_slot(name)
    for step in range(SLOTS):
        slot = (first + step) % SLOTS
        if slot not in taken:
            break
    else:
        _LOGGER.warning(
            "All %d serve-port slots are in use; a camera shares its port", SLOTS
        )
        slot = first
    if slot != first:
        _LOGGER.info(
            "Serve port for a camera moved from base+%d to base+%d "
            "(another camera already holds that port)",
            first,
            slot,
        )
    _SLOTS[name] = slot
    return slot


def assign(names: Any) -> None:
    """Give every name in ``names`` that has no slot yet one, in sorted order."""
    new = sorted({n for n in names if isinstance(n, str) and n not in _SLOTS})
    if not new:
        return
    for name in new:
        _take(name)
    _schedule_save()


def slot_for(name: str) -> int:
    """The camera's slot, assigning one on first use."""
    slot = _SLOTS.get(name)
    if slot is None:
        assign([name])
        slot = _SLOTS[name]
    return slot


def _schedule_save() -> None:
    if _STORE is None or _HASS is None:
        return  # not loaded (unit use): in memory only
    store = _STORE
    snapshot = {"slots": dict(_SLOTS)}

    async def _save() -> None:
        try:
            await store.async_save(snapshot)
        except Exception:
            _LOGGER.warning(
                "could not persist the camera serve ports; a camera added "
                "since the last restart may get a different port next time"
            )

    _HASS.async_create_task(_save(), "aidot-serve-ports-save")


def _valid(data: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(data, dict) or not isinstance(data.get("slots"), dict):
        return out
    used: set[int] = set()
    for name, slot in data["slots"].items():
        if (
            isinstance(name, str)
            and isinstance(slot, int)
            and not isinstance(slot, bool)
            and 0 <= slot < SLOTS
            and slot not in used
        ):
            out[name] = slot
            used.add(slot)
    return out


async def async_load(hass) -> None:
    """Load the persisted slots once per process (per hass instance).

    Tolerant of a missing or corrupt store: it starts empty and keeps going,
    which hands every camera its crc32 slot as before. Names assigned in
    memory before the load (none in normal setup) are re-assigned after it,
    around the persisted ones.
    """
    global _LOADED, _STORE, _HASS

    async with _LOCK:
        if _LOADED and _HASS is hass:
            return
        from homeassistant.helpers.storage import Store

        store: Any = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        data = None
        try:
            data = await store.async_load()
        except Exception:
            _LOGGER.warning("camera serve-port store unreadable; starting fresh")
        stored = _valid(data)
        pending = [n for n in _SLOTS if n not in stored]
        _SLOTS.clear()
        _SLOTS.update(stored)
        _STORE, _HASS, _LOADED = store, hass, True
        if pending:
            assign(pending)


def reset() -> None:
    """Forget all state (tests: a simulated restart)."""
    global _LOADED, _STORE, _HASS
    _SLOTS.clear()
    _LOADED, _STORE, _HASS = False, None, None
