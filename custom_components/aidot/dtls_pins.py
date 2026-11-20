"""Each standard (DTLS) camera's learned certificate pin, kept across restarts.

A standard camera (A000088) secures its media with DTLS, and its signaling
carries nothing to check the camera's certificate against. The library learns
each camera's certificate instead: the first one it presents is recorded, and
once the same certificate has come back on a few handshakes in a row it is
pinned, after which any other certificate fails the handshake. A camera whose
certificate changes between sessions never gets that far, so it keeps
streaming exactly as before and is never locked out.

The library keeps those records wherever its caller says
(``AidotClient(dtls_pin_store=...)``); without a store they last only as long
as the process, so every restart of Home Assistant would start learning again
and a camera would spend its first few connections unverified each time. This
module is that store, kept in Home Assistant's own storage
(``.storage/aidot_dtls_pins``).

- One store per Home Assistant instance, keyed by device id, like the serve
  port map (serve_ports.py): device ids are unique across accounts, and a
  camera moved between config entries keeps its pin.
- The library calls ``get``, ``set`` and ``delete`` from inside the handshake,
  so they are synchronous and only touch memory. A change schedules a delayed
  save on the event loop; Home Assistant writes any pending save at shutdown.
- A record is the library's own JSON-safe dict. It is stored as given and
  treated as opaque, apart from the short summary diagnostics shows.
- The records hold certificate fingerprints: digests of a public
  certificate, not key material. Even so, nothing here logs one, and
  diagnostics shows only its first 8 hex digits, as the library's logs do.

A library without per-camera pinning (1.4.x) has nothing to hand a store to:
the store is then not created and the client is built exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "aidot_dtls_pins"
STORAGE_VERSION = 1
#: Seconds a change waits before it is written, so the few records a camera
#: writes while it learns (one per handshake) land in one save.
SAVE_DELAY_S = 10

_DATA_KEY = "dtls-pin-store"
_LOAD_LOCK = asyncio.Lock()


def library_supports_pins() -> bool:
    """Whether the installed library takes a per-camera pin store."""
    try:
        from aidot_cameras import DtlsPinStore  # noqa: F401
    except ImportError:
        return False
    return True


def _clean_record(record: Any) -> dict[str, Any] | None:
    """A JSON-safe copy of ``record``, or None when it is not a record.

    Only flat dicts of str, int and bool values survive: anything else (an
    edited file, a value no JSON writer can take) would fail the save, and a
    record the library cannot read it treats as none and learns again.
    """
    if not isinstance(record, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in record.items():
        if not isinstance(key, str):
            return None
        if not isinstance(value, (str, int, bool)):
            return None
        out[key] = value
    return out or None


class HassDtlsPinStore:
    """The library's ``DtlsPinStore``, saved to Home Assistant storage."""

    def __init__(self, hass: HomeAssistant, store: Any) -> None:
        self._hass = hass
        self._store = store
        self._records: dict[str, dict[str, Any]] = {}
        # The library may call in from a thread other than the event loop's,
        # so the records are guarded and the save is handed to the loop.
        self._lock = threading.Lock()

    def load(self, data: Any) -> None:
        """Take the records a previous run saved; anything malformed is skipped."""
        pins = data.get("pins") if isinstance(data, dict) else None
        if not isinstance(pins, dict):
            return
        with self._lock:
            for device_id, record in pins.items():
                clean = _clean_record(record)
                if clean is not None:
                    self._records[str(device_id)] = clean

    # ------------------------------------------------- the library's protocol
    def get(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(str(device_id))
            return dict(record) if record is not None else None

    def set(self, device_id: str, record: dict[str, Any]) -> None:
        clean = _clean_record(record)
        if clean is None:
            return
        with self._lock:
            if self._records.get(str(device_id)) == clean:
                return
            self._records[str(device_id)] = clean
        self._schedule_save()

    def delete(self, device_id: str) -> None:
        with self._lock:
            if self._records.pop(str(device_id), None) is None:
                return
        self._schedule_save()

    # ------------------------------------------------------------- saving
    def _schedule_save(self) -> None:
        try:
            self._hass.loop.call_soon_threadsafe(self._delay_save)
        except RuntimeError:
            # The loop is closed (shutdown is over); the change is lost with
            # it, and the camera learns its certificate again next run.
            _LOGGER.debug("DTLS pin change not saved: event loop closed")

    @callback
    def _delay_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_S)

    def _data_to_save(self) -> dict[str, Any]:
        with self._lock:
            return {"pins": {k: dict(v) for k, v in self._records.items()}}


async def async_load(hass: HomeAssistant) -> HassDtlsPinStore | None:
    """Load (once per Home Assistant instance) and return the pin store.

    None when the installed library has no per-camera pinning. Tolerant of a
    missing or unreadable file: it starts empty, and each camera learns its
    certificate again.
    """
    if not library_supports_pins():
        return None
    async with _LOAD_LOCK:
        domain_data = hass.data.setdefault(DOMAIN, {})
        existing = domain_data.get(_DATA_KEY)
        if isinstance(existing, HassDtlsPinStore):
            return existing
        from homeassistant.helpers.storage import Store

        store: Any = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        pins = HassDtlsPinStore(hass, store)
        try:
            pins.load(await store.async_load())
        except Exception:
            _LOGGER.warning("DTLS pin store unreadable; cameras learn their pins again")
        domain_data[_DATA_KEY] = pins
        return pins


def store_of(hass: HomeAssistant) -> HassDtlsPinStore | None:
    """The loaded pin store, or None (not loaded, or no library support)."""
    store = hass.data.get(DOMAIN, {}).get(_DATA_KEY)
    return store if isinstance(store, HassDtlsPinStore) else None


def client_kwargs(hass: HomeAssistant) -> dict[str, Any]:
    """The keyword that hands the store to ``AidotClient``, when there is one."""
    store = store_of(hass)
    return {"dtls_pin_store": store} if store is not None else {}


def summary(device_client: Any) -> dict[str, Any] | None:
    """A camera's pin record for diagnostics: short fingerprint and counters.

    None for a camera with no record, or a library without per-camera pins.
    """
    try:
        record = getattr(device_client, "dtls_pin", None)
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    fp = record.get("fingerprint")
    short = (
        fp.replace(":", "").replace(" ", "").strip().lower()[:8]
        if isinstance(fp, str) and fp
        else None
    )
    return {
        "fingerprint": short,
        "seen": record.get("seen"),
        "enforced": record.get("enforced"),
        "changes": record.get("changes"),
    }
