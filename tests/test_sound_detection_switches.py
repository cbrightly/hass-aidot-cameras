"""Sound-detection switches, and the coordinator refresh behind them.

Two behaviours matter more than the plumbing: a camera that has not answered
must read as unknown rather than off, and a user on an older library (which has
no such methods) must not have their cameras broken by a feature they lack.
"""
import asyncio

import pytest

from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator
from custom_components.aidot.switch import AidotSoundDetectionSwitch


class _Client:
    def __init__(self, sound=None, battery=False, has_methods=True,
                 set_result=True):
        self.device_id = "dev1"
        self.is_battery_camera = battery
        self._sound = sound
        self._set_result = set_result
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
        return self._set_result


def _coord(client):
    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    c.device_client = client
    # The real DataUpdateCoordinator sets this up; give the stub enough that
    # async_update_listeners() actually runs. It used to be swallowed by a
    # try/except AttributeError in production, which also hid anything the
    # listener callbacks themselves raised.
    c._listeners = {}
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


class TestTheInFlightSlot:
    """The slot must survive a fetch that finishes without ever suspending.

    Home Assistant eager-starts background tasks, so such a fetch runs its whole
    body -- and its `finally` -- inside `async_create_background_task`. When the
    slot was the task handle itself, that `finally` cleared it and the finished
    Task was assigned back over it, so the guard stayed occupied forever and no
    camera ever fetched an extra again.
    """

    def _coord_with_eager_hass(self, client):
        c = _coord(client)
        c.camera_extras = {}
        c._extras_due = 0.0
        c._extras_index = 0
        c._extras_task = None
        c._extras_inflight = False

        class _Hass:
            def __init__(self):
                self.created = 0

            def async_create_background_task(self, coro, name=None):
                # Mirror HA's eager start: the body runs synchronously here, up
                # to its first suspension. The old stub called coro.close(),
                # which is exactly why this defect was invisible to the suite.
                self.created += 1
                # loop= is required with eager_start: without it the Task
                # constructor raises, the schedule aborts before the fetch ever
                # runs, and these tests pass without exercising anything.
                return asyncio.Task(
                    coro, loop=asyncio.get_running_loop(), eager_start=True)

        c.hass = _Hass()
        return c

    def test_a_fetch_that_never_suspends_leaves_the_slot_free(self):
        async def go():
            # No getters on the client, so the fetch returns without suspending.
            c = self._coord_with_eager_hass(_Client(has_methods=False))
            c._schedule_camera_extras()
            assert c._extras_inflight is False, "slot still claimed after a finished fetch"
            assert c._extras_task is None, "a finished task was kept as the handle"
            return c

        asyncio.run(go())

    def test_a_later_poll_can_still_schedule(self):
        """The failure users would see: extras stop updating, permanently."""
        async def go():
            c = self._coord_with_eager_hass(_Client(has_methods=False))
            c._schedule_camera_extras()
            c._extras_due = 0.0          # the cadence says it is due again
            c._schedule_camera_extras()
            assert c.hass.created == 2, (
                f"second poll never scheduled (created={c.hass.created}) -- "
                "the in-flight guard was left claimed")

        asyncio.run(go())

    def test_a_fetch_that_suspends_still_holds_the_slot(self):
        """The guard must keep doing its job for a fetch that really is running."""
        gate = None

        class _Slow(_Client):
            async def _get_sound(self):
                await gate                # suspends; the task stays pending
                return {"glass_Break": True}

        async def go():
            nonlocal gate
            gate = asyncio.get_running_loop().create_future()
            c = self._coord_with_eager_hass(_Slow({"glass_Break": True}))
            c._schedule_camera_extras()
            assert c._extras_inflight is True, "slot not held by a running fetch"
            c._extras_due = 0.0
            c._schedule_camera_extras()
            assert c.hass.created == 1, "a second fetch started while one was running"
            gate.set_result(None)
            await c._extras_task
            assert c._extras_inflight is False, "slot not released when the fetch finished"

        asyncio.run(go())


class TestARefusedWrite:
    """A setter that returns False must reach the user as an error.

    async_set_sound_detection returns False when the camera does not answer
    soundAlgorithmGet, or does not report the requested key -- it logs and writes
    nothing. Awaiting it bare made that indistinguishable from success: the
    switch moved, the read-back reverted it, and nothing was ever shown.
    """

    def _switch(self, set_result):
        c = _coord(_Client({"glass_Break": True}, set_result=set_result))
        c.camera_extras = {"sound": {"glass_Break": False}}
        c._extras_tried = set()
        sw = AidotSoundDetectionSwitch.__new__(AidotSoundDetectionSwitch)
        sw.coordinator = c
        sw._key = "glass_Break"
        sw.async_write_ha_state = lambda: None
        return c, sw

    def test_a_refused_write_raises_rather_than_reverting_silently(self):
        from homeassistant.exceptions import HomeAssistantError
        c, sw = self._switch(set_result=False)
        with pytest.raises(HomeAssistantError):
            asyncio.run(sw.async_turn_on())
        assert c.device_client.set_calls == [("glass_Break", True)]

    def test_an_accepted_write_does_not_raise(self):
        c, sw = self._switch(set_result=True)
        asyncio.run(sw.async_turn_on())
        assert c.device_client.set_calls == [("glass_Break", True)]

    def test_the_read_back_asks_only_for_what_changed(self):
        """Three 25 s reads inside a PARALLEL_UPDATES = 1 service call is a
        minute of every other switch on the account queued behind one toggle."""
        c, sw = self._switch(set_result=True)
        asyncio.run(sw.async_turn_on())
        assert c.device_client.calls == ["sound"], (
            f"expected only the sound read-back, got {c.device_client.calls}")


class TestACameraThatAnswersNone:
    """A key counts as covered once asked for, not once it has a value."""

    def _coord_with_hass(self, client):
        c = _coord(client)
        c.camera_extras = {}
        c._extras_due = 0.0
        c._extras_index = 0
        c._extras_task = None
        c._extras_inflight = False
        c._extras_tried = set()

        class _Hass:
            def async_create_background_task(self, coro, name=None):
                coro.close()

        c.hass = _Hass()
        return c

    def test_it_settles_to_the_slow_cadence_anyway(self):
        import time as _t
        from custom_components.aidot.coordinator import EXTRAS_REFRESH_SECONDS
        # Answers None to everything, so camera_extras never gains a key.
        c = self._coord_with_hass(_Client(None))
        asyncio.run(c.async_refresh_camera_extras())
        assert c.camera_extras == {}, "nothing should have been stored"
        c._extras_due = 0.0
        c._schedule_camera_extras()
        assert c._extras_due - _t.monotonic() > EXTRAS_REFRESH_SECONDS - 60, (
            "a camera that answers None was left polling at the fill rate forever")


class TestUnload:
    """A reload must not leave a fetch running against a dead coordinator."""

    def test_cancel_stops_an_in_flight_fetch(self):
        async def go():
            gate = asyncio.get_running_loop().create_future()

            async def never():
                await gate

            c = _coord(_Client())
            c._extras_inflight = True
            c._extras_task = asyncio.get_running_loop().create_task(never())
            await asyncio.sleep(0)
            task = c._extras_task

            c.cancel_camera_extras()

            assert c._extras_task is None
            assert c._extras_inflight is False
            assert task.cancelled() or task.cancelling(), "the fetch was left running"

        asyncio.run(go())

    def test_cancel_is_safe_with_nothing_running(self):
        c = _coord(_Client())
        c._extras_task = None
        c._extras_inflight = False
        c.cancel_camera_extras()          # must not raise
        assert c._extras_task is None
