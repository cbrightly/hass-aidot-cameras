"""The stream health sensors: why a camera card is blank.

They read the library's public ``stream_health`` and listen for its change
callback. The fakes here stand in for a device client; the last tests hold the
integration's copies of the states and reasons against the library's own.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aidot_cameras import StreamHealth
from aidot_cameras.camera import stream_health as lib_health

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers import entity_registry as er

from custom_components.aidot import sensor
from custom_components.aidot.sensor import (
    STREAM_HEALTH_SENSORS,
    STREAM_STATES,
    AidotStreamHealthSensor,
)

from .test_platform_discovery import _camera, _keys, _setup

_COMPONENT = Path(sensor.__file__).parent


def _with_health(coordinator, health: StreamHealth | None = None):
    """Give a fake camera the library's stream health surface."""
    dc = coordinator.device_client
    dc.stream_health = health or StreamHealth()
    dc.listeners = []

    def _add(callback):
        dc.listeners.append(callback)
        return lambda: dc.listeners.remove(callback)

    dc.add_stream_health_listener = _add
    coordinator.data = SimpleNamespace(online=True)
    coordinator.last_update_success = True
    return coordinator


def _entity(key: str, health: StreamHealth | None = None):
    coordinator = _with_health(_camera("cam"), health)
    desc = next(d for d in STREAM_HEALTH_SENSORS if d.key == key)
    return AidotStreamHealthSensor(coordinator, desc), coordinator


def test_both_sensors_are_diagnostic():
    by_key = {d.key: d for d in STREAM_HEALTH_SENSORS}
    assert set(by_key) == {"stream_status", "last_connect_time"}
    for desc in by_key.values():
        assert desc.entity_category is EntityCategory.DIAGNOSTIC
        assert desc.translation_key == desc.key
    status = by_key["stream_status"]
    assert status.device_class is SensorDeviceClass.ENUM
    assert status.options == list(STREAM_STATES)
    connect = by_key["last_connect_time"]
    assert connect.device_class is SensorDeviceClass.DURATION
    assert connect.native_unit_of_measurement == UnitOfTime.SECONDS


def test_the_status_sensor_reads_the_state_and_why():
    entity, coordinator = _entity(
        "stream_status",
        StreamHealth(
            state="retrying", last_end_reason="busy", retry_delay_s=8.0, failures=2
        ),
    )
    assert entity.unique_id == "cam_stream_status"
    assert entity.native_value == "retrying"
    assert entity.extra_state_attributes == {
        "last_end_reason": "busy",
        "failures": 2,
    }
    coordinator.device_client.stream_health = StreamHealth(state="dormant")
    assert entity.native_value == "dormant"


def test_a_state_this_version_does_not_know_reads_unknown():
    entity, _ = _entity("stream_status", StreamHealth(state="something-new"))
    assert entity.native_value is None


def test_the_connect_time_is_unknown_until_a_session_delivered():
    entity, coordinator = _entity("last_connect_time")
    assert entity.native_value is None
    assert entity.extra_state_attributes == {"last_end_reason": None}
    coordinator.device_client.stream_health = StreamHealth(
        state="streaming", last_connect_s=3.25, last_end_reason="no_media"
    )
    assert entity.native_value == 3.25
    assert entity.extra_state_attributes == {"last_end_reason": "no_media"}


async def test_a_change_in_the_library_writes_the_state_at_once():
    entity, coordinator = _entity("stream_status")
    entity.async_write_ha_state = MagicMock()
    await entity.async_added_to_hass()
    dc = coordinator.device_client
    assert len(dc.listeners) == 1

    dc.stream_health = StreamHealth(state="streaming", last_connect_s=2.0)
    dc.listeners[0]()
    entity.async_write_ha_state.assert_called_once_with()

    # The camera's refresh writes it too: that is what picks up a power
    # switch changed outside Home Assistant.
    coordinator.refresh()
    assert entity.async_write_ha_state.call_count == 2

    # Removed with the entity: a camera that keeps streaming must not write
    # to an entity that is gone.
    entity._call_on_remove_callbacks()
    assert dc.listeners == []
    assert coordinator.listeners == []


def test_they_stay_available_whatever_the_camera_is_doing():
    """They say why a card is blank, so offline is when they are read."""
    entity, coordinator = _entity("stream_status", StreamHealth(state="retrying"))
    for data, success in (
        (SimpleNamespace(online=False), True),
        (None, True),
        (SimpleNamespace(online=True), False),
    ):
        coordinator.data = data
        coordinator.last_update_success = success
        assert entity.available is True
        assert entity.native_value == "retrying"


async def test_a_library_with_the_snapshot_but_no_change_callback_gets_none():
    account, entry, added, add_entities = _setup()
    half = _camera("half")
    half.device_client.stream_health = StreamHealth()
    account.camera_coordinators["half"] = half
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch.object(er, "async_get", lambda hass: registry):
        await sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["half_sd_card_status", "half_wifi_rssi"]


async def test_the_platform_adds_them_only_when_the_library_reports_health():
    account, entry, added, add_entities = _setup()
    old_library = _camera("old")
    new_library = _with_health(_camera("new"))
    new_library.data = None
    account.camera_coordinators["old"] = old_library
    account.camera_coordinators["new"] = new_library
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch.object(er, "async_get", lambda hass: registry):
        await sensor.async_setup_entry(None, entry, add_entities)
        assert _keys(added) == [
            "old_sd_card_status",
            "old_wifi_rssi",
            "new_sd_card_status",
            "new_wifi_rssi",
            "new_stream_status",
            "new_last_connect_time",
        ]
        # Once per camera, however often the coordinators refresh.
        new_library.refresh()
        old_library.refresh()
        assert len(added) == 6


def test_the_status_is_on_by_default_and_the_connect_time_is_not():
    by_key = {d.key: d for d in STREAM_HEALTH_SENSORS}
    assert by_key["stream_status"].entity_registry_enabled_default is True
    assert by_key["last_connect_time"].entity_registry_enabled_default is False


def test_the_states_are_the_librarys():
    assert STREAM_STATES == lib_health.STREAM_STATES


#: End reasons named before the required library reports them: the refused
#: DTLS certificate comes with per-camera pinning. Empty this once the
#: manifest's library floor is a release that reports each of them.
NAMED_AHEAD_OF_THE_LIBRARY = {"dtls_pin_mismatch"}


def test_every_state_and_end_reason_has_a_name_and_every_state_an_icon():
    strings = json.loads((_COMPONENT / "strings.json").read_text())
    sensors = strings["entity"]["sensor"]
    status = sensors["stream_status"]
    assert set(status["state"]) == set(lib_health.STREAM_STATES)
    library_reasons = set(lib_health.STREAM_END_REASONS)
    for key in ("stream_status", "last_connect_time"):
        reasons = sensors[key]["state_attributes"]["last_end_reason"]["state"]
        # Every reason the library reports has a name; a name beyond those is
        # one a library release not yet required adds (see
        # NAMED_AHEAD_OF_THE_LIBRARY), and nothing else.
        assert library_reasons <= set(reasons)
        assert set(reasons) <= library_reasons | NAMED_AHEAD_OF_THE_LIBRARY
    icons = json.loads((_COMPONENT / "icons.json").read_text())
    state_icons = icons["entity"]["sensor"]["stream_status"]["state"]
    assert set(state_icons) == set(lib_health.STREAM_STATES)
    assert "last_connect_time" in icons["entity"]["sensor"]
