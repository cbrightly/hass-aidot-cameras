"""Human / vehicle / package / pet detection switches.

The cameras have always typed their detections -- `humanDetect` reads 1 on a
stock A000088 and A001064 -- while Home Assistant offered only motion on/off
and a sensitivity number.

Two rules these lock in, both from the live probe on 2026-09-07:

- `publicZone` arrives in the same object and is NOT a detection type. It gets
  no switch; the library carries it through so a write cannot reset it.
- A battery A001513 answers nothing at all. That has to read as *unknown*, not
  as four confidently-off switches.
"""

import asyncio

from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator
from custom_components.aidot.switch import (
    DETECTION_TYPE_TRANSLATION_KEYS,
    AidotDetectionTypeSwitch,
)


class _Client:
    def __init__(self, detect=None, set_result=True):
        self.device_id = "dev1"
        self.is_battery_camera = False
        self._detect = detect
        self._set_result = set_result
        self.set_calls: list = []

    async def async_get_detection_types(self):
        return self._detect

    async def async_set_detection_type(self, key, on):
        self.set_calls.append((key, on))
        return self._set_result


def _coord(client, extras=None):
    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    c.device_client = client
    c._listeners = {}
    c.camera_extras = extras or {}
    return c


def _switch(coord, key):
    sw = AidotDetectionTypeSwitch.__new__(AidotDetectionTypeSwitch)
    sw.coordinator = coord
    sw._key = key
    return sw


_LIVE = {
    "humanDetect": True,
    "vehicleDetect": False,
    "packageDetect": False,
    "petDetect": False,
    "publicZone": True,
}


def test_publicZone_is_not_a_detection_type():
    """It rides in the same object and would become a switch that means
    nothing to a user, on a setting we deliberately do not model."""
    assert "publicZone" not in DETECTION_TYPE_TRANSLATION_KEYS
    assert set(DETECTION_TYPE_TRANSLATION_KEYS) == {
        "humanDetect",
        "vehicleDetect",
        "packageDetect",
        "petDetect",
    }


def test_a_switch_reports_what_the_camera_said():
    sw = _switch(_coord(_Client(_LIVE), {"detect": _LIVE}), "humanDetect")
    assert sw.is_on is True


def test_a_camera_that_has_not_answered_is_unknown_not_off():
    sw = _switch(_coord(_Client(None), {}), "humanDetect")
    assert sw.is_on is None


def test_an_unanswered_camera_is_unknown_even_with_other_extras_present():
    """Sound answered, detection did not - the detection switch must not
    inherit 'off' from a sibling key having arrived."""
    sw = _switch(
        _coord(_Client(None), {"sound": {"glass_Break": True}}), "vehicleDetect"
    )
    assert sw.is_on is None


def test_turning_one_on_writes_only_that_key():
    client = _Client(_LIVE)
    coord = _coord(client, {"detect": _LIVE})
    sw = _switch(coord, "packageDetect")

    async def _noop(**kw):
        return None

    coord.async_refresh_camera_extras = _noop
    sw.async_run_command = lambda coro, label: coro
    sw.async_write_ha_state = lambda: None

    asyncio.run(sw._async_set(True))
    assert client.set_calls == [("packageDetect", True)]
