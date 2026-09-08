"""The settings under "light up when someone appears".

The master toggle is `switch.<camera>_floodlight_automation` and has shipped for
a long time; the app calls it "When Someone Appears". These are the two settings
that sit under it and now have controls of their own:

    how long the light stays on   LingerDuration   20 / 30 / 40 / 50 seconds
    how bright it comes up        Dimming          10-100

Both were verified on hardware by read-back before being given an entity. The
third setting the camera reports, `lightBehavior` (Constant/Flash), is
deliberately NOT given one: the write acks and the value never changes. See
test_no_light_behavior_control below - the same rule that removed the
resolution select.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator
from custom_components.aidot.number import CAMERA_NUMBERS, AidotCameraNumber
from custom_components.aidot.select import (
    CAMERA_SELECTS,
    LIGHT_LINGER_OPTIONS,
    AidotCameraSelect,
)


class _Status:
    def __init__(self, **kw):
        self.light_linger_duration = kw.get("light_linger_duration")
        self.light_brightness = kw.get("light_brightness")
        self.light_behavior = kw.get("light_behavior")
        self.night_vision_mode = "auto"
        self.motion_sensitivity = 3
        self.speaker_volume = 50


class _Info:
    def __init__(self, declared=()):
        self.declared_properties = frozenset(declared)


class _Client:
    def __init__(self, declared=("LingerDuration", "Dimming")):
        self.device_id = "dev1"
        self.is_battery_camera = True
        self.calls: list = []
        self.info = _Info(declared)

    async def async_set_light_linger_duration(self, seconds):
        self.calls.append(("linger", seconds))
        return True

    async def async_set_light_brightness(self, level):
        self.calls.append(("brightness", level))
        return True


def _coord(status, declared=("LingerDuration", "Dimming")):
    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    c.device_client = _Client(declared)
    c._listeners = {}
    c.data = status
    return c


def _desc(descs, key):
    for d in descs:
        if d.key == key:
            return d
    raise AssertionError(f"no description {key!r} in {[d.key for d in descs]}")


def _entity(cls, coord, desc):
    e = cls.__new__(cls)
    e.coordinator = coord
    e.entity_description = desc
    if hasattr(desc, "options"):
        e._attr_options = list(desc.options or [])
    return e


# --- what the camera actually offers ----------------------------------------

def test_the_durations_offered_are_the_four_the_camera_enumerates():
    assert LIGHT_LINGER_OPTIONS == ("20", "30", "40", "50")


def test_the_brightness_floor_is_ten_not_zero():
    """The camera's minimum is 10. Zero is out of range, not "off"."""
    d = _desc(CAMERA_NUMBERS, "light_brightness")
    assert d.native_min_value == 10
    assert d.native_max_value == 100


# --- reading ----------------------------------------------------------------

def test_the_duration_select_reports_what_the_camera_said():
    coord = _coord(_Status(light_linger_duration=40))
    e = _entity(AidotCameraSelect, coord, _desc(CAMERA_SELECTS, "light_linger_duration"))
    assert e.current_option == "40"


def test_the_brightness_number_reports_what_the_camera_said():
    coord = _coord(_Status(light_brightness=80))
    e = _entity(AidotCameraNumber, coord, _desc(CAMERA_NUMBERS, "light_brightness"))
    assert e.native_value == 80


def test_a_camera_that_has_not_answered_reads_unknown():
    coord = _coord(_Status())
    sel = _entity(AidotCameraSelect, coord, _desc(CAMERA_SELECTS, "light_linger_duration"))
    num = _entity(AidotCameraNumber, coord, _desc(CAMERA_NUMBERS, "light_brightness"))
    assert sel.current_option is None
    assert num.native_value is None


def test_a_duration_the_camera_reports_but_we_do_not_offer_reads_unknown():
    """Never show an option that is not in the list - HA logs and drops it."""
    coord = _coord(_Status(light_linger_duration=35))
    e = _entity(AidotCameraSelect, coord, _desc(CAMERA_SELECTS, "light_linger_duration"))
    assert e.current_option is None


# --- writing ----------------------------------------------------------------

def test_choosing_a_duration_sends_seconds_as_a_number():
    coord = _coord(_Status(light_linger_duration=20))
    desc = _desc(CAMERA_SELECTS, "light_linger_duration")
    import asyncio
    asyncio.run(desc.async_select_option_fn(coord.device_client, "50"))
    assert coord.device_client.calls == [("linger", 50)]


def test_setting_the_brightness_sends_an_integer():
    coord = _coord(_Status(light_brightness=80))
    desc = _desc(CAMERA_NUMBERS, "light_brightness")
    import asyncio
    asyncio.run(desc.async_set_fn(coord.device_client, 55.0))
    assert coord.device_client.calls == [("brightness", 55)]


# --- what must NOT be here --------------------------------------------------

def test_no_light_behavior_control():
    """It acks and never lands - six attempts, two models, int and string.

    `LingerDuration` and `Dimming` landed within 4 s on the same camera in the
    same session, so this is the attribute and not a sleeping camera. Adding a
    control means having a read-back that shows the camera changed, not just an
    ack. Same rule that removed the resolution select.
    """
    keys = [d.key for d in CAMERA_SELECTS] + [d.key for d in CAMERA_NUMBERS]
    assert "light_behavior" not in keys


def test_every_new_control_is_gated():
    """A camera that reports nothing gets no entity, rather than one at unknown."""
    for descs, key in ((CAMERA_SELECTS, "light_linger_duration"),
                       (CAMERA_NUMBERS, "light_brightness")):
        d = _desc(descs, key)
        assert d.is_supported is not None
        assert d.is_supported(_Status(), _Client()) is False
    assert _desc(CAMERA_SELECTS, "light_linger_duration").is_supported(
        _Status(light_linger_duration=30), _Client()) is True
    assert _desc(CAMERA_NUMBERS, "light_brightness").is_supported(
        _Status(light_brightness=100), _Client()) is True


def test_the_duration_select_needs_the_MODEL_to_declare_it_not_the_cloud():
    """The bug 2.23.0 shipped, locked shut.

    An A000088's cloud properties report `LingerDuration: "30"`. Asked directly
    over LAN the camera has no such attribute, and writing it acks and changes
    nothing on the camera or in the cloud. A gate that trusts the reported
    value offered a control there that could never work.
    """
    reports_it_but_does_not_declare_it = _Client(declared=("Dimming",))
    assert _desc(CAMERA_SELECTS, "light_linger_duration").is_supported(
        _Status(light_linger_duration=30),
        reports_it_but_does_not_declare_it) is False


def test_brightness_is_NOT_profile_gated_and_that_is_deliberate():
    """The asymmetry is evidence, not an oversight.

    The A000088 profile omits `Dimming` too -- but the camera really has it, and
    a LAN write on 2026-09-08 was applied by the camera and echoed by the cloud.
    Profile-gating brightness would remove a control that demonstrably works.
    """
    declares_neither = _Client(declared=())
    assert _desc(CAMERA_NUMBERS, "light_brightness").is_supported(
        _Status(light_brightness=80), declares_neither) is True


def test_an_unreadable_profile_does_not_silently_remove_the_control():
    """Empty declared_properties means UNKNOWN, not "declares nothing".

    A camera whose profile failed to parse should keep behaving as it did
    before this gate existed rather than quietly losing an entity.
    """
    class _NoInfo:
        device_id = "dev1"
        info = None
    assert _desc(CAMERA_SELECTS, "light_linger_duration").is_supported(
        _Status(light_linger_duration=30), _NoInfo()) is True


# --- cleaning up what 2.23.0 created ----------------------------------------

async def test_the_23_0_duration_select_is_removed_where_it_could_never_work(hass):
    """Dropping the entity is not enough - its registry row outlives it.

    2.23.0 created `select.<camera>_light_stays_on_for` on every camera whose
    cloud properties reported a value, including models that ignore the write.
    Without this the user is left with a permanently unavailable control that
    still shows up in the UI and in any automation that referenced it.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.aidot.const import DOMAIN
    from custom_components.aidot.select import remove_unsupported_linger_select

    reg = er.async_get(hass)
    doomed = reg.async_get_or_create(
        "select", DOMAIN, "camA_light_linger_duration")
    keeper = reg.async_get_or_create(
        "select", DOMAIN, "camB_light_linger_duration")
    other = reg.async_get_or_create("select", DOMAIN, "camA_night_vision")

    remove_unsupported_linger_select(hass, "camA", _Client(declared=("Dimming",)))
    remove_unsupported_linger_select(hass, "camB", _Client())

    assert reg.async_get(doomed.entity_id) is None
    assert reg.async_get(keeper.entity_id) is not None, (
        "a model that does declare LingerDuration keeps its control")
    assert reg.async_get(other.entity_id) is not None, (
        "only the duration select is removed")


async def test_an_unreadable_profile_removes_nothing(hass):
    """Unknown is not "unsupported" - never delete on missing evidence."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.aidot.const import DOMAIN
    from custom_components.aidot.select import remove_unsupported_linger_select

    class _NoInfo:
        info = None

    reg = er.async_get(hass)
    row = reg.async_get_or_create("select", DOMAIN, "camC_light_linger_duration")
    remove_unsupported_linger_select(hass, "camC", _NoInfo())
    assert reg.async_get(row.entity_id) is not None
