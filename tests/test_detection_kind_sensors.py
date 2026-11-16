"""Person / vehicle / package / pet binary sensors, and the hold they share.

The Motion event entity reports the kind of each detection as its event type,
which an automation can only read through a template. These sensors give each
kind a state: on when an event of that kind arrives, off once the occupancy
sensor's hold has passed with no further one.

What these lock in:

- The hold is the occupancy sensor's own (one mixin, one window), and the
  occupancy sensor behaves as it did before the mixin was split out.
- A sensor counts only its own kind, read the way the event entity reads it.
- A camera that reports its detection types gets a kind's sensor exactly when
  it reports that kind's type, the gate of the detect_* switches. A battery
  camera is never asked for its types and gets all four.
- They are disabled by default.
"""

import json
import pathlib
from types import SimpleNamespace

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.aidot import binary_sensor
from custom_components.aidot.binary_sensor import (
    DETECTION_KIND_SENSORS,
    MOTION_OCCUPANCY_WINDOW,
    AidotDetectionKindBinarySensor,
    AidotOccupancyBinarySensor,
    MotionHoldMixin,
    detection_kind_keys,
)
from custom_components.aidot.notify_dispatch import KIND_MOTION, KINDS
from custom_components.aidot.switch import (
    DETECTION_TYPE_TRANSLATION_KEYS,
    AidotDetectionTypeSwitch,
)

from .test_platform_discovery import _camera, _keys, _setup

_COMPONENT = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "aidot"
)

_ALL_TYPES = {
    "humanDetect": True,
    "vehicleDetect": False,
    "packageDetect": False,
    "petDetect": False,
    "publicZone": True,
}

_PERSON = {"eventCode": "4"}
_VEHICLE = {"eventCode": "15"}
_PACKAGE = {"eventCode": "18"}
_PET = {"eventCode": "25"}
_MOTION = {"eventCode": "1"}


class _Clock:
    """A loop clock the test moves, and the one timer the entity may hold."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.timers: list[dict] = []

    def time(self) -> float:
        return self.now

    def call_later(self, hass, delay, action):
        timer = {"at": self.now + delay, "action": action, "cancelled": False}
        self.timers.append(timer)

        def _cancel() -> None:
            timer["cancelled"] = True

        return _cancel

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for timer in self.timers:
            if not timer["cancelled"] and timer["at"] <= self.now:
                timer["cancelled"] = True
                timer["action"](None)

    @property
    def pending(self) -> list[dict]:
        return [t for t in self.timers if not t["cancelled"]]


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(binary_sensor, "async_call_later", c.call_later)
    return c


def _wire(entity, clock):
    """Attach the entity to the test clock; returns its list of state writes."""
    writes: list = []
    entity.hass = SimpleNamespace(loop=clock)
    entity.async_write_ha_state = lambda: writes.append(entity.is_on)
    return writes


def _kind_sensor(clock, detect_key="humanDetect"):
    entity = AidotDetectionKindBinarySensor(_camera("cam"), detect_key)
    return entity, _wire(entity, clock)


def _occupancy(clock, occupancy=None):
    coord = _camera("cam", data=SimpleNamespace(occupancy=occupancy))
    entity = AidotOccupancyBinarySensor(coord, binary_sensor.CAMERA_BINARY_SENSORS[0])
    return entity, _wire(entity, clock)


# --------------------------------------------------------------------------- #
# What exists
# --------------------------------------------------------------------------- #


def test_there_is_a_sensor_for_every_kind_but_plain_motion():
    assert sorted(d.kind for d in DETECTION_KIND_SENSORS) == sorted(
        k for k in KINDS if k != KIND_MOTION
    )


def test_each_sensor_is_gated_on_one_detection_switch_key():
    """One sensor per detect_* switch and the reverse, so the two cannot
    drift into a camera having a kind's switch without its sensor."""
    assert sorted(d.detect_key for d in DETECTION_KIND_SENSORS) == sorted(
        DETECTION_TYPE_TRANSLATION_KEYS
    )
    by_key = {d.detect_key: d.kind for d in DETECTION_KIND_SENSORS}
    assert by_key == {
        "humanDetect": "person",
        "vehicleDetect": "vehicle",
        "packageDetect": "package",
        "petDetect": "pet",
    }


@pytest.mark.parametrize("desc", DETECTION_KIND_SENSORS, ids=lambda d: d.key)
def test_a_kind_sensor_is_disabled_by_default(desc, clock):
    entity, _ = _kind_sensor(clock, desc.detect_key)
    assert entity.entity_registry_enabled_default is False
    assert entity._attr_unique_id == f"cam_{desc.kind}_detected"
    assert entity.translation_key == f"{desc.kind}_detected"
    assert entity.device_class in (
        BinarySensorDeviceClass.MOTION,
        BinarySensorDeviceClass.OCCUPANCY,
    )


def test_the_occupancy_sensor_stays_enabled_and_keeps_its_id(clock):
    entity, _ = _occupancy(clock)
    assert entity.entity_registry_enabled_default is True
    assert entity._attr_unique_id == "cam_occupancy"
    assert entity.device_class is BinarySensorDeviceClass.OCCUPANCY


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_every_kind_sensor_has_a_name(filename):
    names = json.loads((_COMPONENT / filename).read_text())["entity"]["binary_sensor"]
    for desc in DETECTION_KIND_SENSORS:
        assert names[desc.translation_key]["name"]


def test_every_kind_sensor_has_an_icon_for_both_states():
    icons = json.loads((_COMPONENT / "icons.json").read_text())["entity"]
    for desc in DETECTION_KIND_SENSORS:
        icon = icons["binary_sensor"][desc.translation_key]
        assert icon["default"].startswith("mdi:")
        assert icon["state"]["on"].startswith("mdi:")


# --------------------------------------------------------------------------- #
# Which cameras get them
# --------------------------------------------------------------------------- #


def test_the_gate_is_the_detection_switches_own():
    cam = _camera("a", extras={"detect": _ALL_TYPES})
    assert detection_kind_keys(cam) == AidotDetectionTypeSwitch.family.reported_keys(
        cam
    )


async def test_a_camera_gets_the_sensors_of_the_types_it_reports():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera(
        "a", extras={"detect": {"humanDetect": True, "petDetect": False}}
    )
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_occupancy", "a_person_detected", "a_pet_detected"]


async def test_a_type_switched_off_still_has_its_sensor():
    """Reported is the gate, not enabled: the sensor of a detection type the
    user turned off must not vanish from automations that name it."""
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a", extras={"detect": _ALL_TYPES})
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == [
        "a_occupancy",
        "a_person_detected",
        "a_vehicle_detected",
        "a_package_detected",
        "a_pet_detected",
    ]


async def test_publicZone_gets_no_sensor():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera(
        "a", extras={"detect": {"publicZone": True}}
    )
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_occupancy"]


async def test_a_camera_that_reports_no_types_gets_only_occupancy():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a", extras={"sound": {"x": True}})
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    account.refresh()
    account.camera_coordinators["a"].refresh()
    assert _keys(added) == ["a_occupancy"]


async def test_sensors_appear_when_the_camera_answers_later_and_only_once():
    """The detection types arrive on the camera's own coordinator after
    setup, so the platform has to look again on that camera's refresh."""
    account, entry, added, add_entities = _setup()
    cam = _camera("a")
    account.camera_coordinators["a"] = cam
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_occupancy"]

    cam.camera_extras["detect"] = {"humanDetect": True, "vehicleDetect": True}
    cam.refresh()
    assert _keys(added) == ["a_occupancy", "a_person_detected", "a_vehicle_detected"]

    cam.refresh()
    account.refresh()
    assert len(added) == 3


async def test_a_battery_camera_gets_all_four_without_being_asked():
    """Its detection types are never fetched (it is asleep), so there is no
    answer to gate on; its events carry the kinds all the same."""
    account, entry, added, add_entities = _setup()
    cam = _camera("b", is_battery=True)
    account.camera_coordinators["b"] = cam
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == [
        "b_occupancy",
        "b_person_detected",
        "b_vehicle_detected",
        "b_package_detected",
        "b_pet_detected",
    ]
    assert all(
        e.entity_registry_enabled_default is False
        for e in added
        if e._attr_unique_id != "b_occupancy"
    )
    # Once per camera, however often the coordinators refresh.
    cam.refresh()
    account.refresh()
    assert len(added) == 5


async def test_a_battery_camera_whose_types_are_known_gets_only_those():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["b"] = _camera(
        "b", is_battery=True, extras={"detect": {"humanDetect": True}}
    )
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["b_occupancy", "b_person_detected"]


# --------------------------------------------------------------------------- #
# The hold
# --------------------------------------------------------------------------- #


def test_both_sensors_use_the_one_hold():
    for cls in (AidotOccupancyBinarySensor, AidotDetectionKindBinarySensor):
        assert issubclass(cls, MotionHoldMixin)
        for name in ("_on_motion", "_on_motion_expired", "_motion_held"):
            assert name not in vars(cls), f"{cls.__name__} overrides {name}"


def test_a_kind_sensor_is_off_before_any_event(clock):
    entity, writes = _kind_sensor(clock)
    assert entity.is_on is False
    assert writes == []


@pytest.mark.parametrize(
    ("detect_key", "event"),
    [
        ("humanDetect", _PERSON),
        ("vehicleDetect", _VEHICLE),
        ("packageDetect", _PACKAGE),
        ("petDetect", _PET),
    ],
)
def test_an_event_of_its_kind_turns_a_sensor_on_then_off(detect_key, event, clock):
    entity, writes = _kind_sensor(clock, detect_key)
    entity._on_motion(event)
    assert writes == [True]

    clock.advance(MOTION_OCCUPANCY_WINDOW - 1.0)
    assert entity.is_on is True
    assert writes == [True]

    clock.advance(2.0)
    assert writes == [True, False]
    assert entity.is_on is False
    assert clock.pending == []


@pytest.mark.parametrize("event", [_MOTION, _VEHICLE, _PACKAGE, _PET, {}])
def test_another_kind_does_not_turn_the_person_sensor_on(event, clock):
    entity, writes = _kind_sensor(clock)
    entity._on_motion(event)
    assert entity.is_on is False
    assert writes == []
    assert clock.pending == []


def test_another_kind_does_not_extend_the_hold(clock):
    entity, writes = _kind_sensor(clock)
    entity._on_motion(_PERSON)
    clock.advance(100.0)
    entity._on_motion(_VEHICLE)
    clock.advance(MOTION_OCCUPANCY_WINDOW - 99.0)
    assert writes == [True, False]


def test_a_second_event_of_the_kind_restarts_the_hold(clock):
    entity, writes = _kind_sensor(clock)
    entity._on_motion(_PERSON)
    clock.advance(100.0)
    entity._on_motion(_PERSON)
    assert len(clock.pending) == 1

    clock.advance(100.0)
    assert entity.is_on is True
    assert writes == [True, True]

    clock.advance(MOTION_OCCUPANCY_WINDOW - 99.0)
    assert writes == [True, True, False]


def test_the_kind_is_read_as_the_event_entity_reads_it(clock):
    """eventCodes[0] wins over eventCode (notify_dispatch.event_kind)."""
    person, _ = _kind_sensor(clock, "humanDetect")
    vehicle, _ = _kind_sensor(clock, "vehicleDetect")
    event = {"eventCodes": ["15", "4"], "eventCode": "4"}
    person._on_motion(event)
    vehicle._on_motion(event)
    assert person.is_on is False
    assert vehicle.is_on is True


def test_removal_cancels_the_pending_timer(clock):
    entity, _ = _kind_sensor(clock)
    entity._on_motion(_PERSON)
    assert len(clock.pending) == 1
    entity._cancel_motion_timer()
    assert clock.pending == []
    assert entity._motion_expiry_unsub is None


async def test_an_added_sensor_listens_for_motion_and_stops_on_removal(clock):
    entity, _ = _kind_sensor(clock)
    listeners: list = []
    removers: list = []

    def add_motion_listener(cb):
        listeners.append(cb)
        return lambda: listeners.remove(cb)

    entity.coordinator.add_motion_listener = add_motion_listener
    entity.async_on_remove = removers.append
    await entity.async_added_to_hass()
    assert listeners == [entity._on_motion]
    for remove in removers:
        remove()
    assert listeners == []


# --------------------------------------------------------------------------- #
# Occupancy is as it was
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("event", [_MOTION, _PERSON, _VEHICLE, _PACKAGE, _PET, {}])
def test_any_event_marks_occupied_for_the_window(event, clock):
    entity, writes = _occupancy(clock, occupancy=False)
    assert entity.is_on is False
    entity._on_motion(event)
    assert writes == [True]

    clock.advance(MOTION_OCCUPANCY_WINDOW - 1.0)
    assert entity.is_on is True
    clock.advance(2.0)
    assert writes == [True, False]


def test_occupancy_falls_back_to_the_cloud_value_after_the_hold(clock):
    entity, writes = _occupancy(clock, occupancy=True)
    assert entity.is_on is True
    entity._on_motion(_MOTION)
    clock.advance(MOTION_OCCUPANCY_WINDOW + 1.0)
    assert writes == [True, True]


def test_occupancy_with_no_cloud_value_reads_clear_not_unknown(clock):
    entity, _ = _occupancy(clock, occupancy=None)
    assert entity.is_on is False


def test_occupancy_without_data_reads_clear(clock):
    entity = AidotOccupancyBinarySensor(
        _camera("cam"), binary_sensor.CAMERA_BINARY_SENSORS[0]
    )
    _wire(entity, clock)
    assert entity.is_on is False
