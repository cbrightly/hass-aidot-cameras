"""The automation blueprints under blueprints/ load, validate and behave.

HACS does not install blueprints and nothing in the integration imports
them, so without this a YAML or schema error would only show when a user
imports one. Two layers:

- each file is read with Home Assistant's own YAML loader and held to the
  automation blueprint schema;
- each is then set up as a real automation with the aidot, camera and notify
  actions replaced by recorders, which holds the triggers, conditions and
  actions to the automation schema and shows the calls a run makes, in order,
  including when the integration refuses one.
"""

import asyncio
import json
import pathlib
import re
from datetime import timedelta

import pytest
from homeassistant.components import automation, blueprint
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml
from pytest_homeassistant_custom_component.common import async_fire_time_changed

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_AUTOMATION_BLUEPRINTS = _ROOT / "blueprints" / "automation"
_FOLDER = _AUTOMATION_BLUEPRINTS / "aidot"
_RECORD = "prewake_record.yaml"
_NOTIFY = "detection_snapshot_notify.yaml"
_EXPECTED = {_RECORD, _NOTIFY}

_CAMERA = "camera.porch"
_EVENT = "event.porch_motion"
_CDN_URL = "https://cdn.invalid/picture.jpg?sig=secret"


def _documentation_url() -> str:
    manifest = _ROOT / "custom_components" / "aidot" / "manifest.json"
    return json.loads(manifest.read_text())["documentation"]


# --------------------------------------------------------------------------- #
# The files
# --------------------------------------------------------------------------- #
def test_the_blueprint_files_are_found_from_the_repo_root():
    """The folder holds exactly the blueprints this test knows about.

    A blueprint added without a test, or a moved folder that would make the
    parametrized tests below collect nothing, fails here.
    """
    assert {p.name for p in _FOLDER.glob("*.yaml")} == _EXPECTED
    assert not list(_AUTOMATION_BLUEPRINTS.rglob("*.yml"))
    assert {p.name for p in _AUTOMATION_BLUEPRINTS.rglob("*.yaml")} == _EXPECTED


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_blueprint_is_valid(name):
    """Home Assistant's loader reads it and the blueprint schema accepts it."""
    path = _FOLDER / name
    data = load_yaml(str(path))
    bp = blueprint.Blueprint(
        data,
        path=f"aidot/{name}",
        expected_domain=automation.DOMAIN,
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    # The installed Home Assistant is new enough for its min_version.
    assert bp.validate() is None
    meta = bp.metadata
    assert meta["homeassistant"]["min_version"]
    assert meta["description"]
    # Re-import and update go by source_url; it must be this file in the
    # repository the manifest documents.
    assert meta["source_url"] == (
        f"{_documentation_url()}/blob/main/blueprints/automation/aidot/{name}"
    )
    # Every entity picker is limited to this integration's entities.
    pickers = [
        spec["selector"]["entity"]
        for spec in bp.inputs.values()
        if spec and "entity" in (spec.get("selector") or {})
    ]
    assert pickers
    for picker in pickers:
        assert [f["integration"] for f in picker["filter"]] == ["aidot"]
    # Every input has a name, and all but the pickers a default, so the
    # blueprint can be used by choosing entities alone.
    for key, spec in bp.inputs.items():
        assert spec.get("name"), key
        if "entity" not in spec["selector"]:
            assert "default" in spec, key


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_blueprint_is_ascii(name):
    (_FOLDER / name).read_bytes().decode("ascii")


def test_readme_names_each_blueprint_by_its_import_url():
    readme = (_ROOT / "README.md").read_text()
    for name in _EXPECTED:
        url = f"{_documentation_url()}/blob/main/blueprints/automation/aidot/{name}"
        assert url in readme, name


def test_notify_blueprint_says_when_to_use_the_built_in_notifications():
    data = load_yaml(str(_FOLDER / _NOTIFY))
    description = data["blueprint"]["description"]
    assert "built-in" in description
    assert "Motion notifications" in description


# --------------------------------------------------------------------------- #
# The automations they make
# --------------------------------------------------------------------------- #
class _Calls:
    """Records action calls in order; an action named in `refuse` raises."""

    def __init__(self, hass):
        self.hass = hass
        self.log = []
        self.refuse = set()

    def register(self, domain, service):
        name = f"{domain}.{service}"

        async def _handler(call):
            self.log.append((name, dict(call.data)))
            if name in self.refuse:
                raise ServiceValidationError(f"{name} refused")

        self.hass.services.async_register(domain, service, _handler)

    def names(self):
        return [name for name, _ in self.log]

    def data(self, name):
        return [data for n, data in self.log if n == name]


@pytest.fixture
def repo_blueprints(monkeypatch):
    """Home Assistant reads automation blueprints straight from this repo."""
    monkeypatch.setattr(
        blueprint.models.DomainBlueprints,
        "blueprint_folder",
        property(lambda self: _AUTOMATION_BLUEPRINTS),
    )


@pytest.fixture
def calls(hass):
    recorder = _Calls(hass)
    for domain, service in (
        ("aidot", "wake"),
        ("aidot", "sleep"),
        ("camera", "record"),
        ("camera", "snapshot"),
        ("notify", "phone"),
    ):
        recorder.register(domain, service)
    return recorder


async def _setup(hass, name, inputs):
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: {
                "id": "under_test",
                "alias": "under test",
                "use_blueprint": {"path": f"aidot/{name}", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()
    state = hass.states.get("automation.under_test")
    # An automation whose blueprint or inputs do not validate is set up as
    # unavailable, with a repair issue, rather than failing the component.
    assert state is not None
    assert state.state == "on"


async def _settle():
    """Let a run go as far as it can.

    Not hass.async_block_till_done(): a run parked in a delay is a pending
    task, and that call would wait for it for as long as the delay.
    """
    for _ in range(50):
        await asyncio.sleep(0)


async def _advance(hass, seconds):
    """Fire the timers due within `seconds` of now: the delay a run is in.

    Measured from the real clock, which the tests of delays leave alone, so
    it is the time since the delay started, not since the last call.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await _settle()


_GO = [{"trigger": "event", "event_type": "aidot_test_go"}]


async def _go(hass):
    hass.bus.async_fire("aidot_test_go")
    await _settle()


@pytest.mark.parametrize(
    ("name", "inputs"),
    [
        (_RECORD, {"camera": _CAMERA}),
        (_NOTIFY, {"camera": _CAMERA, "motion_event": _EVENT}),
    ],
)
async def test_blueprint_sets_up_with_only_its_entities_chosen(
    hass, repo_blueprints, calls, name, inputs
):
    """Every default (the time trigger, the notify action, ...) is usable."""
    await _setup(hass, name, inputs)
    # Detach the triggers: the default time trigger otherwise leaves its
    # timer behind, which the test harness reports.
    await hass.services.async_call(
        automation.DOMAIN,
        "turn_off",
        {"entity_id": "automation.under_test"},
        blocking=True,
    )


async def test_record_wakes_waits_records_then_sleeps(hass, repo_blueprints, calls):
    await _setup(hass, _RECORD, {"camera": _CAMERA, "record_trigger": _GO})

    await _go(hass)
    assert calls.log == [("aidot.wake", {"entity_id": [_CAMERA], "hold": 60})]

    # Nothing is recorded until the lead time (45 s) is over.
    await _advance(hass, 40)
    assert calls.names() == ["aidot.wake"]
    await _advance(hass, 46)
    assert calls.names() == ["aidot.wake", "camera.record"]
    (record,) = calls.data("camera.record")
    assert record["entity_id"] == [_CAMERA]
    assert record["duration"] == 60
    assert re.fullmatch(r"/media/aidot/porch_\d{8}_\d{6}\.mp4", record["filename"])

    # The sleep waits for the stream to close (35 s).
    await _advance(hass, 30)
    assert calls.names() == ["aidot.wake", "camera.record"]
    await _advance(hass, 36)
    assert calls.log[2:] == [("aidot.sleep", {"entity_id": [_CAMERA]})]


@pytest.mark.parametrize(
    ("lead", "hold", "sent"),
    [(45, 60, 60), (90, 60, 105), (240, 300, 300), (0, 0, 15), (290, 60, 300)],
)
async def test_record_hold_always_outlasts_the_lead(
    hass, repo_blueprints, calls, lead, hold, sent
):
    """A hold shorter than the lead would let the camera fall asleep again."""
    await _setup(
        hass,
        _RECORD,
        {
            "camera": _CAMERA,
            "record_trigger": _GO,
            "lead_seconds": lead,
            "hold_seconds": hold,
        },
    )
    await _go(hass)
    assert calls.data("aidot.wake") == [{"entity_id": [_CAMERA], "hold": sent}]
    await _advance(hass, lead + 1)
    await _advance(hass, 36)
    assert calls.names() == ["aidot.wake", "camera.record", "aidot.sleep"]


async def test_record_still_records_on_a_mains_camera(hass, repo_blueprints, calls):
    """aidot.wake and aidot.sleep are refused on mains; the run carries on."""
    calls.refuse = {"aidot.wake", "aidot.sleep"}
    await _setup(hass, _RECORD, {"camera": _CAMERA, "record_trigger": _GO})
    await _go(hass)
    await _advance(hass, 46)
    await _advance(hass, 36)
    assert calls.names() == ["aidot.wake", "camera.record", "aidot.sleep"]
    assert hass.states.get("automation.under_test").attributes["current"] == 0


async def test_record_sleeps_the_camera_after_a_failed_recording(
    hass, repo_blueprints, calls
):
    """A camera this run woke is not left awake because the file was refused."""
    calls.refuse = {"camera.record"}
    await _setup(hass, _RECORD, {"camera": _CAMERA, "record_trigger": _GO})
    await _go(hass)
    await _advance(hass, 46)
    await _advance(hass, 36)
    assert calls.names() == ["aidot.wake", "camera.record", "aidot.sleep"]


async def test_record_without_sleep_and_with_its_own_file(hass, repo_blueprints, calls):
    await _setup(
        hass,
        _RECORD,
        {
            "camera": _CAMERA,
            "record_trigger": _GO,
            "sleep_after": False,
            "lead_seconds": 10,
            "duration": 20,
            "filename": "/config/www/{{ camera_slug }}.mp4",
        },
    )
    await _go(hass)
    await _advance(hass, 11)
    await _advance(hass, 120)
    assert calls.names() == ["aidot.wake", "camera.record"]
    (record,) = calls.data("camera.record")
    assert record["filename"] == "/config/www/porch.mp4"
    assert record["duration"] == 20


async def test_record_drops_a_trigger_while_a_run_is_going(
    hass, repo_blueprints, calls
):
    """One media stream per camera: no second wake or recording on top."""
    await _setup(hass, _RECORD, {"camera": _CAMERA, "record_trigger": _GO})
    await _go(hass)
    await _go(hass)
    await _advance(hass, 46)
    assert calls.names() == ["aidot.wake", "camera.record"]
    await _go(hass)
    await _advance(hass, 36)
    assert calls.names() == ["aidot.wake", "camera.record", "aidot.sleep"]


def _fire(hass, kind, when, **attrs):
    """What AidotMotionEvent writes for one event: a new time and its type."""
    hass.states.async_set(
        _EVENT,
        when,
        {
            "event_type": kind,
            "event_types": ["motion", "person", "vehicle", "package", "pet"],
            "event_uuid": "u",
            "pic_url": _CDN_URL,
            "description": "",
            "device_class": "motion",
            **attrs,
        },
    )


async def _notify_setup(hass, **inputs):
    hass.states.async_set(_CAMERA, "idle", {"friendly_name": "Porch"})
    hass.states.async_set(_EVENT, "unknown", {"device_class": "motion"})
    await _setup(
        hass,
        _NOTIFY,
        {
            "camera": _CAMERA,
            "motion_event": _EVENT,
            "notify_action": "notify.phone",
            **inputs,
        },
    )


async def test_notify_sends_a_snapshot_and_never_the_cdn_url(
    hass, repo_blueprints, calls
):
    await _notify_setup(hass)
    _fire(hass, "person", "2026-03-04T05:06:07.000+00:00")
    await hass.async_block_till_done()

    assert calls.names() == ["camera.snapshot", "notify.phone"]
    (snapshot,) = calls.data("camera.snapshot")
    assert snapshot == {
        "entity_id": [_CAMERA],
        "filename": "/media/aidot/porch_last_detection.jpg",
    }
    (sent,) = calls.data("notify.phone")
    assert sent["title"] == "Porch: Person"
    assert sent["message"].startswith("Person detected at ")
    assert sent["data"] == {"image": "/media/local/aidot/porch_last_detection.jpg"}
    assert "cdn.invalid" not in json.dumps(calls.log)


async def test_notify_only_for_the_chosen_kinds(hass, repo_blueprints, calls):
    await _notify_setup(hass, kinds=["person", "package"], cooldown_seconds=0)
    for n, kind in enumerate(["motion", "vehicle", "pet", "package", "person"]):
        _fire(hass, kind, f"2026-03-04T05:06:{10 + n}.000+00:00")
        await hass.async_block_till_done()
    assert [d["title"] for d in calls.data("notify.phone")] == [
        "Porch: Package",
        "Porch: Person",
    ]


async def test_notify_cooldown(hass, repo_blueprints, calls, freezer):
    await _notify_setup(hass)  # 60 s by default
    _fire(hass, "motion", "2026-03-04T05:06:07.000+00:00")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=30))
    _fire(hass, "person", "2026-03-04T05:06:37.000+00:00")
    await hass.async_block_till_done()
    assert len(calls.data("notify.phone")) == 1

    freezer.tick(timedelta(seconds=31))
    _fire(hass, "person", "2026-03-04T05:07:08.000+00:00")
    await hass.async_block_till_done()
    assert [d["title"] for d in calls.data("notify.phone")] == [
        "Porch: Motion",
        "Porch: Person",
    ]


async def test_notify_extra_conditions_and_data(hass, repo_blueprints, calls):
    hass.states.async_set("input_boolean.armed", "off")
    await _notify_setup(
        hass,
        cooldown_seconds=0,
        extra_conditions=[
            {"condition": "state", "entity_id": "input_boolean.armed", "state": "on"}
        ],
        extra_data={"channel": "cameras", "tag": "porch"},
        title="{{ kind }} / {{ camera_name }}",
        message="{{ event_description }}",
        snapshot_folder="/config/www/aidot/",
        snapshot_url_folder="/local/aidot/",
    )
    _fire(hass, "pet", "2026-03-04T05:06:07.000+00:00")
    await hass.async_block_till_done()
    assert calls.log == []

    hass.states.async_set("input_boolean.armed", "on")
    _fire(hass, "pet", "2026-03-04T05:06:08.000+00:00", description="A dog")
    await hass.async_block_till_done()
    (snapshot,) = calls.data("camera.snapshot")
    assert snapshot["filename"] == "/config/www/aidot/porch_last_detection.jpg"
    (sent,) = calls.data("notify.phone")
    assert sent["title"] == "pet / Porch"
    assert sent["message"] == "A dog"
    assert sent["data"] == {
        "image": "/local/aidot/porch_last_detection.jpg",
        "channel": "cameras",
        "tag": "porch",
    }


async def test_notify_ignores_a_restored_or_departing_entity(
    hass, repo_blueprints, calls
):
    """Only a fired event notifies.

    After a restart the entity comes back from unavailable with its last
    event restored; a reload removes the state and adds it again; and other
    attributes can change without a new event.
    """
    await _notify_setup(hass, cooldown_seconds=0)
    when = "2026-03-04T05:06:07.000+00:00"
    _fire(hass, "person", when)
    await hass.async_block_till_done()
    assert len(calls.data("notify.phone")) == 1

    # Attribute-only change.
    _fire(hass, "person", when, friendly_name="Porch Motion")
    # Unavailable and back with the same event restored.
    hass.states.async_set(_EVENT, "unavailable")
    _fire(hass, "person", when)
    # Removed and added again.
    hass.states.async_remove(_EVENT)
    _fire(hass, "person", when)
    await hass.async_block_till_done()
    assert len(calls.data("notify.phone")) == 1

    _fire(hass, "person", "2026-03-04T05:09:00.000+00:00")
    await hass.async_block_till_done()
    assert len(calls.data("notify.phone")) == 2


async def test_notify_goes_out_when_the_snapshot_fails(hass, repo_blueprints, calls):
    calls.refuse = {"camera.snapshot"}
    await _notify_setup(hass)
    _fire(hass, "person", "2026-03-04T05:06:07.000+00:00")
    await hass.async_block_till_done()
    assert calls.names() == ["camera.snapshot", "notify.phone"]
