"""Standard cameras keep their learned DTLS certificate pin across restarts.

The library learns each standard (DTLS) camera's certificate and pins it after
a few handshakes in a row present the same one; where the pins are kept is the
caller's choice. Without a store of the integration's own they lived only as
long as the process, so every restart began learning again. These tests cover
the store (Home Assistant storage), the account client being handed it, the
repair issue a refused certificate raises, and its "trust the new
certificate" fix.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import aidot_cameras
from custom_components.aidot import coordinator as coord_mod
from custom_components.aidot import dtls_pins, pin_issue, repairs
from custom_components.aidot.const import DOMAIN

from .test_library_settings import CAMERA

RECORD = {"fingerprint": "ab" * 32, "seen": 3, "enforced": True, "changes": 0}


class _FakeStore:
    """Home Assistant's Store, reduced to what the pin store calls."""

    def __init__(self, data=None):
        self.data = data
        self.saved = None
        self.delay_saves = 0

    async def async_load(self):
        return self.data

    def async_delay_save(self, data_func, delay):
        self.delay_saves += 1
        self.saved = data_func()


@pytest.fixture(autouse=True)
def _fresh_store(hass):
    hass.data.setdefault(DOMAIN, {}).pop(dtls_pins._DATA_KEY, None)
    yield
    hass.data.get(DOMAIN, {}).pop(dtls_pins._DATA_KEY, None)


def _store(hass, data=None):
    fake = _FakeStore(data)
    pins = dtls_pins.HassDtlsPinStore(hass, fake)
    pins.load(data)
    return pins, fake


# ---------------------------------------------------------------- the store


def test_the_store_meets_the_librarys_protocol(hass):
    pins, _ = _store(hass)
    # The library refuses a store that does not pass this check.
    assert isinstance(pins, aidot_cameras.DtlsPinStore)


async def test_records_are_kept_returned_as_copies_and_saved(hass):
    pins, fake = _store(hass)
    assert pins.get("cam") is None
    pins.set("cam", RECORD)
    await hass.async_block_till_done()
    got = pins.get("cam")
    assert got == RECORD
    got["enforced"] = False
    assert pins.get("cam")["enforced"] is True
    assert fake.saved == {"pins": {"cam": RECORD}}
    pins.delete("cam")
    await hass.async_block_till_done()
    assert pins.get("cam") is None
    assert fake.saved == {"pins": {}}


async def test_an_unchanged_record_or_a_missing_delete_writes_nothing(hass):
    pins, fake = _store(hass, {"pins": {"cam": RECORD}})
    pins.set("cam", dict(RECORD))
    pins.delete("other")
    await hass.async_block_till_done()
    assert fake.delay_saves == 0


def test_malformed_records_are_skipped_on_load(hass):
    pins, _ = _store(
        hass,
        {
            "pins": {
                "good": RECORD,
                "list": ["x"],
                "nested": {"fingerprint": {"x": 1}},
                "empty": {},
            }
        },
    )
    assert pins.get("good") == RECORD
    for dev_id in ("list", "nested", "empty"):
        assert pins.get(dev_id) is None
    _store(hass, "not a dict")  # does not raise


def test_a_record_no_json_writer_can_take_is_not_stored(hass):
    pins, _ = _store(hass)
    pins.set("cam", {"fingerprint": object()})
    assert pins.get("cam") is None


async def test_the_store_survives_a_restart(hass, hass_storage):
    hass_storage[dtls_pins.STORAGE_KEY] = {
        "version": dtls_pins.STORAGE_VERSION,
        "key": dtls_pins.STORAGE_KEY,
        "data": {"pins": {"cam": RECORD}},
    }
    pins = await dtls_pins.async_load(hass)
    assert pins is not None
    assert pins.get("cam") == RECORD
    # Loaded once per instance: a second entry gets the same store.
    assert await dtls_pins.async_load(hass) is pins
    assert dtls_pins.store_of(hass) is pins


async def test_an_unreadable_file_starts_empty(hass, monkeypatch):
    async def _boom(self):
        raise ValueError("corrupt")

    monkeypatch.setattr("homeassistant.helpers.storage.Store.async_load", _boom)
    pins = await dtls_pins.async_load(hass)
    assert pins is not None and pins.get("cam") is None


async def test_an_older_library_gets_no_store(hass, monkeypatch):
    monkeypatch.setattr(dtls_pins, "library_supports_pins", lambda: False)
    assert await dtls_pins.async_load(hass) is None
    assert dtls_pins.client_kwargs(hass) == {}


async def test_the_client_is_handed_the_integrations_store(hass, monkeypatch):
    """The required library pins per camera, so the client gets the store."""

    async def _no_prefetch(_dc):
        return None

    monkeypatch.setattr("aidot_cameras.client._prefetch_ice_config", _no_prefetch)
    await dtls_pins.async_load(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    manager = coord_mod.AidotDeviceManagerCoordinator(hass, entry)
    assert dtls_pins.library_supports_pins()
    assert manager.client.dtls_pin_store is dtls_pins.store_of(hass)


# ------------------------------------------------- the client is handed it


async def test_each_camera_reads_its_pin_from_the_integrations_store(hass, monkeypatch):
    async def _no_prefetch(_dc):
        return None

    monkeypatch.setattr("aidot_cameras.client._prefetch_ice_config", _no_prefetch)
    pins = await dtls_pins.async_load(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    manager = coord_mod.AidotDeviceManagerCoordinator(hass, entry)
    assert manager.client.dtls_pin_store is pins
    dc = manager.client.get_device_client(dict(CAMERA))
    assert dc.dtls_pin_store is pins
    pins.set(str(dc.device_id), RECORD)
    assert dc.dtls_pin == RECORD
    dc.forget_dtls_pin()
    assert pins.get(str(dc.device_id)) is None


def test_diagnostics_shows_only_a_short_fingerprint():
    dc = SimpleNamespace(dtls_pin=dict(RECORD, fingerprint="AB:CD:" + "ef" * 30))
    assert dtls_pins.summary(dc) == {
        "fingerprint": "abcdefef",
        "seen": 3,
        "enforced": True,
        "changes": 0,
    }
    assert dtls_pins.summary(SimpleNamespace(dtls_pin=None)) is None
    assert dtls_pins.summary(object()) is None


# --------------------------------------------------------- the repair issue


class _Camera:
    """A device client reduced to stream health and the pin."""

    device_id = "cam1"

    def __init__(self):
        self.stream_health = SimpleNamespace(state="connecting", last_end_reason=None)
        self.listeners = []
        self.forgotten = 0

    def add_stream_health_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def forget_dtls_pin(self):
        self.forgotten += 1

    def report(self, state, reason):
        self.stream_health = SimpleNamespace(state=state, last_end_reason=reason)
        for cb in list(self.listeners):
            cb()


def _issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, pin_issue.issue_id("cam1"))


async def test_a_refused_certificate_raises_a_fixable_issue_once(hass):
    cam = _Camera()
    stop = pin_issue.watch(hass, cam, dev_id="cam1", name="Porch", entry_id="e1")
    assert stop is not None
    cam.report("retrying", "open_failed")
    assert _issue(hass) is None
    cam.report("retrying", pin_issue.END_REASON)
    issue = _issue(hass)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == pin_issue.ISSUE_KEY
    assert issue.translation_placeholders == {"name": "Porch"}
    assert issue.data == {"dev_id": "cam1", "entry_id": "e1", "name": "Porch"}
    # The fix deletes the issue; the retries still under way must not
    # bring it straight back.
    ir.async_delete_issue(hass, DOMAIN, pin_issue.issue_id("cam1"))
    cam.report("connecting", pin_issue.END_REASON)
    cam.report("retrying", pin_issue.END_REASON)
    assert _issue(hass) is None
    stop()
    assert cam.listeners == []


async def test_streaming_again_clears_it_and_a_later_refusal_raises_it_again(hass):
    cam = _Camera()
    pin_issue.watch(hass, cam, dev_id="cam1", name="Porch", entry_id="e1")
    cam.report("retrying", pin_issue.END_REASON)
    assert _issue(hass) is not None
    # Streaming can still carry the old end reason; it clears regardless.
    cam.report("streaming", pin_issue.END_REASON)
    assert _issue(hass) is None
    cam.report("retrying", pin_issue.END_REASON)
    assert _issue(hass) is not None


def test_an_older_library_is_not_watched(hass):
    cam = SimpleNamespace(add_stream_health_listener=MagicMock())
    assert pin_issue.watch(hass, cam, dev_id="x", name="x", entry_id="e") is None
    cam.add_stream_health_listener.assert_not_called()


async def test_clear_for_deletes_the_issue(hass):
    cam = _Camera()
    pin_issue.watch(hass, cam, dev_id="cam1", name="Porch", entry_id="e1")
    cam.report("retrying", pin_issue.END_REASON)
    pin_issue.clear_for(hass, ["cam1"])
    assert _issue(hass) is None


# ---------------------------------------------------------- the fix flow


async def _flow(hass, data):
    flow = await repairs.async_create_fix_flow(hass, pin_issue.issue_id("cam1"), data)
    flow.hass = hass
    return flow


async def test_trusting_the_new_certificate_drops_the_pin(hass):
    cam = _Camera()
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        camera_coordinators={"cam1": SimpleNamespace(device_client=cam)}
    )
    flow = await _flow(hass, {"dev_id": "cam1", "entry_id": "e1", "name": "Porch"})
    shown = await flow.async_step_init()
    assert shown["type"] == "form"
    assert shown["step_id"] == "confirm"
    assert shown["description_placeholders"] == {"name": "Porch"}
    assert cam.forgotten == 0
    done = await flow.async_step_confirm({})
    assert done["type"] == "create_entry"
    assert cam.forgotten == 1


async def test_a_camera_that_is_not_loaded_aborts(hass):
    flow = await _flow(hass, {"dev_id": "cam1", "entry_id": "e1", "name": "Porch"})
    await flow.async_step_init()
    done = await flow.async_step_confirm({})
    assert done["type"] == "abort"
    assert done["reason"] == "camera_not_loaded"


async def test_other_issues_get_the_plain_confirm_flow(hass):
    from homeassistant.components.repairs import ConfirmRepairFlow

    flow = await repairs.async_create_fix_flow(hass, "weak_wifi_cam1", None)
    assert isinstance(flow, ConfirmRepairFlow)


# ------------------------------------------------------------- teardown


async def test_stopping_a_camera_stops_its_watch():
    stopped = []
    coord = SimpleNamespace(
        cancel_camera_extras=MagicMock(),
        stop_pin_issue_watch=lambda: stopped.append(True),
        device_client=SimpleNamespace(),
    )
    await coord_mod._async_stop_camera("cam1", coord)
    assert stopped == [True]


async def test_camera_setup_starts_the_watch_and_a_second_setup_replaces_it(hass):
    from unittest.mock import AsyncMock

    cam = _Camera()
    cam.async_start_motion_polling = AsyncMock()
    cam.async_watch_unbind = AsyncMock()
    cam.async_watch_events = AsyncMock()
    cam.poke_motion_poll = MagicMock()
    cam.info = SimpleNamespace(name="Porch")
    coord = coord_mod.AidotCameraUpdateCoordinator.__new__(
        coord_mod.AidotCameraUpdateCoordinator
    )
    coord.hass = hass
    coord.device_client = cam
    coord.config_entry = SimpleNamespace(entry_id="e1")
    await coord._async_setup()
    await coord._async_setup()
    assert len(cam.listeners) == 1
    cam.report("retrying", pin_issue.END_REASON)
    assert _issue(hass).translation_placeholders == {"name": "Porch"}
    coord.stop_pin_issue_watch()
    assert cam.listeners == []
