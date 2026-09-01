"""Sound-detection switches, and the coordinator refresh behind them.

Two behaviours matter more than the plumbing: a camera that has not answered
must read as unknown rather than off, and a user on an older library (which has
no such methods) must not have their cameras broken by a feature they lack.
"""
import asyncio

from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator
from custom_components.aidot.switch import AidotSoundDetectionSwitch


class _Client:
    def __init__(self, sound=None, battery=False, has_methods=True):
        self.device_id = "dev1"
        self.is_battery_camera = battery
        self._sound = sound
        self.set_calls = []
        self.calls = []
        if not has_methods:
            return
        self.async_get_sound_detection = self._get_sound
        self.async_get_wifi_info = self._none
        self.async_get_sd_card_info = self._none
        self.async_set_sound_detection = self._set_sound

    async def _get_sound(self):
        self.calls.append("sound")
        return self._sound

    async def _none(self):
        self.calls.append("other")
        return

    async def _set_sound(self, key, on):
        self.set_calls.append((key, on))
        return True


def _coord(client):
    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    c.device_client = client
    return c


class TestTheRefresh:
    def test_it_stores_what_the_camera_reports(self):
        c = _coord(_Client({"glass_Break": True}))
        asyncio.run(c._async_fetch_one_extra())
        assert c.camera_extras["sound"] == {"glass_Break": True}

    def test_a_silent_camera_does_not_blank_a_good_value(self):
        client = _Client({"glass_Break": True})
        c = _coord(client)
        asyncio.run(c._async_fetch_one_extra())
        client._sound = None
        c._extras_index = 0                     # ask for "sound" again
        asyncio.run(c._async_fetch_one_extra())
        assert c.camera_extras["sound"] == {"glass_Break": True}

    def test_battery_cameras_are_never_asked(self):
        """They are asleep; the request only holds a socket open for nothing."""
        client = _Client({"glass_Break": True}, battery=True)
        c = _coord(client)
        c._schedule_camera_extras()
        assert getattr(c, "_extras_task", None) is None
        asyncio.run(c.async_refresh_camera_extras())
        assert getattr(c, "camera_extras", {}) == {}

    def test_an_older_library_without_these_methods_does_not_raise(self):
        c = _coord(_Client(has_methods=False))
        asyncio.run(c.async_refresh_camera_extras())
        assert c.camera_extras == {}

    def test_one_poll_fetches_one_action_not_three(self):
        """Three inline round trips at 8s each is what stalled the poll before;
        the rotation is what keeps a cycle cheap."""
        client = _Client({"glass_Break": True})
        c = _coord(client)
        asyncio.run(c._async_fetch_one_extra())
        assert client.calls == ["sound"]

    def test_it_rotates_through_the_actions(self):
        client = _Client({"glass_Break": True})
        c = _coord(client)
        for _ in range(3):
            asyncio.run(c._async_fetch_one_extra())
        assert c._extras_index == 3


class TestTheSwitch:
    def _switch(self, extras):
        c = _coord(_Client({"glass_Break": True}))
        c.camera_extras = extras
        sw = AidotSoundDetectionSwitch.__new__(AidotSoundDetectionSwitch)
        sw.coordinator = c
        sw._key = "glass_Break"
        return sw

    def test_it_reads_the_cached_flag(self):
        assert self._switch({"sound": {"glass_Break": True}}).is_on is True

    def test_unknown_when_the_camera_has_not_answered(self):
        """Not False - the camera never said it was off."""
        assert self._switch({}).is_on is None


class TestTheFillCadence:
    """One action is fetched per window. With only the slow cadence, a fresh
    start would take an hour and a half to show a full set of entities."""

    def _coord_with_hass(self, extras):
        c = _coord(_Client({"glass_Break": True}))
        c.camera_extras = dict(extras)
        c._extras_due = 0.0
        c._extras_task = None

        class _Hass:
            def async_create_background_task(self, coro, name=None):
                coro.close()
                return object()

        c.hass = _Hass()
        return c

    def test_an_incomplete_set_is_filled_quickly(self):
        import time as _t
        from custom_components.aidot.coordinator import EXTRAS_FILL_SECONDS
        c = self._coord_with_hass({"sound": {"x": True}})
        c._schedule_camera_extras()
        assert c._extras_due - _t.monotonic() <= EXTRAS_FILL_SECONDS + 1

    def test_a_complete_set_settles_to_the_slow_cadence(self):
        import time as _t
        from custom_components.aidot.coordinator import EXTRAS_REFRESH_SECONDS
        c = self._coord_with_hass({"sound": {}, "wifi": {}, "sd": {}})
        c._schedule_camera_extras()
        assert c._extras_due - _t.monotonic() > EXTRAS_REFRESH_SECONDS - 60
