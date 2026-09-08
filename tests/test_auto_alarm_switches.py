"""Automatic-siren switches: the master and its two triggers.

This is the one settings group where a wrong write makes noise in someone's
house, so two things are locked in here:

- The master (`autoAlarm`) and the triggers are separate entities, and setting a
  trigger must not arm the master. The library does read-modify-write for that;
  this checks the switch does not defeat it.
- A camera that has not answered reads as unknown, not off. Showing a confident
  "off" for an alarm setting the camera never reported is the worst of the
  three states to get wrong.
"""

import asyncio

from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator
from custom_components.aidot.switch import (
    AUTO_ALARM_TRANSLATION_KEYS,
    AidotAutoAlarmSwitch,
)


class _Client:
    def __init__(self, alarm=None, set_result=True):
        self.device_id = "dev1"
        self.is_battery_camera = False
        self._alarm = alarm
        self._set_result = set_result
        self.set_calls: list = []

    async def async_get_auto_alarm(self):
        return self._alarm

    async def async_set_auto_alarm(self, key, on):
        self.set_calls.append((key, on))
        return self._set_result


def _coord(client, extras=None):
    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    c.device_client = client
    c._listeners = {}
    c.camera_extras = extras or {}
    return c


def _switch(coord, key):
    sw = AidotAutoAlarmSwitch.__new__(AidotAutoAlarmSwitch)
    sw.coordinator = coord
    sw._key = key
    return sw


_LIVE = {"autoAlarm": False, "motionDetection": False, "humanDetect": True}


def test_all_three_settings_get_a_switch():
    assert set(AUTO_ALARM_TRANSLATION_KEYS) == {
        "autoAlarm", "motionDetection", "humanDetect"}


def test_a_switch_reports_what_the_camera_said():
    coord = _coord(_Client(_LIVE), {"alarm": _LIVE})
    assert _switch(coord, "humanDetect").is_on is True
    assert _switch(coord, "autoAlarm").is_on is False


def test_a_camera_that_has_not_answered_is_unknown_not_off():
    assert _switch(_coord(_Client(None), {}), "autoAlarm").is_on is None


def test_unknown_even_when_a_sibling_extras_key_has_arrived():
    coord = _coord(_Client(None), {"sound": {"glass_Break": True}})
    assert _switch(coord, "autoAlarm").is_on is None


def test_setting_a_trigger_writes_only_that_key():
    """It must not arm the master as a side effect."""
    client = _Client(_LIVE)
    coord = _coord(client, {"alarm": _LIVE})
    sw = _switch(coord, "motionDetection")

    async def _noop(**kw):
        return None

    coord.async_refresh_camera_extras = _noop
    sw.async_run_command = lambda coro, label: coro
    sw.async_write_ha_state = lambda: None

    asyncio.run(sw._async_set(True))
    assert client.set_calls == [("motionDetection", True)]
