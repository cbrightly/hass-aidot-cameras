"""Tests for the Aidot camera motion-event entity.

_on_motion maps the cloud eventCode to a HA event type and fires _trigger_event
with the per-event attributes; available follows the coordinator online state.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.aidot.event import AidotMotionEvent


def _coordinator(data=None, *, last_update_success=True):
    info = SimpleNamespace(
        dev_id="cam1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = last_update_success
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _event(data=None, *, last_update_success=True):
    ev = AidotMotionEvent(_coordinator(data, last_update_success=last_update_success))
    ev._trigger_event = MagicMock()
    ev.async_write_ha_state = MagicMock()
    return ev


def test_event_types_and_unique_id():
    ev = _event()
    assert set(ev.event_types) == {"motion", "person"}
    assert ev.unique_id == "cam1_motion"


def test_on_motion_maps_person_code():
    ev = _event()
    ev._on_motion({
        "eventCode": "4",
        "eventUuid": "u-1",
        "picUrl": "https://cdn/x.jpg",
        "eventDesc": "Person",
    })
    ev._trigger_event.assert_called_once()
    event_type, attrs = ev._trigger_event.call_args.args
    assert event_type == "person"
    assert attrs == {
        "event_uuid": "u-1",
        "pic_url": "https://cdn/x.jpg",
        "description": "Person",
    }
    ev.async_write_ha_state.assert_called_once()


def test_on_motion_maps_motion_code():
    ev = _event()
    ev._on_motion({"eventCode": "1"})
    assert ev._trigger_event.call_args.args[0] == "motion"


def test_on_motion_unknown_code_defaults_to_motion():
    ev = _event()
    ev._on_motion({"eventCode": "99"})
    assert ev._trigger_event.call_args.args[0] == "motion"


def test_on_motion_missing_code_defaults_to_motion():
    ev = _event()
    ev._on_motion({})
    event_type, attrs = ev._trigger_event.call_args.args
    assert event_type == "motion"
    assert attrs == {"event_uuid": None, "pic_url": None, "description": None}


def test_available_true_when_online():
    ev = _event(SimpleNamespace(online=True))
    assert ev.available is True


def test_available_false_when_offline():
    ev = _event(SimpleNamespace(online=False))
    assert ev.available is False


def test_available_false_when_no_data():
    ev = _event(None)
    assert ev.available is False


def test_available_false_when_update_failed():
    ev = _event(SimpleNamespace(online=True), last_update_success=False)
    assert ev.available is False
