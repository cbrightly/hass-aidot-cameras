"""Tests for AidotDeviceManagerCoordinator's token-refresh persistence.

Focused, lightweight unit coverage for token_fresh_cb: it must persist a
JSON-safe view of login_info, not a shallow .copy() of the live dict - see
the docstring on the method itself for why a shallow copy is not enough.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.aidot import coordinator as coordinator_module
from custom_components.aidot.coordinator import (
    AidotCameraUpdateCoordinator,
    AidotDeviceManagerCoordinator,
)
from custom_components.aidot.sd_recordings import SdCache


def _make_coordinator() -> AidotDeviceManagerCoordinator:
    """Build a coordinator with token_fresh_cb's dependencies mocked out,
    bypassing DataUpdateCoordinator.__init__ (no real hass lifecycle needed
    to exercise this one method).
    """
    coord = object.__new__(AidotDeviceManagerCoordinator)
    coord.hass = MagicMock()
    coord.config_entry = MagicMock()
    coord.client = MagicMock()
    return coord


def test_token_fresh_cb_persists_the_json_safe_view_not_a_raw_copy():
    # login_info doubles as the account-shared cache for the persistent-MQTT
    # connection and its guarding asyncio.Lock - a plain .copy() is shallow,
    # so the same live Lock would end up in config_entry.data, which HA later
    # serializes to JSON when persisting config entries to disk. Must use
    # serializable_login_info() instead.
    coord = _make_coordinator()
    coord.client.serializable_login_info.return_value = {"access_token": "abc"}
    coord.token_fresh_cb()
    coord.client.serializable_login_info.assert_called_once()
    coord.hass.config_entries.async_update_entry.assert_called_once_with(
        coord.config_entry, data={"access_token": "abc"}
    )


def test_token_fresh_cb_does_not_touch_login_info_directly():
    # Regression guard for the original bug: login_info.copy() must not be
    # what gets persisted.
    coord = _make_coordinator()
    coord.client.serializable_login_info.return_value = {"access_token": "abc"}
    coord.token_fresh_cb()
    coord.client.login_info.copy.assert_not_called()


class _FakeList:
    """What async_get_sd_recordings returns: records, an optional map, a window."""

    def __init__(self, records=(), hours=None, answered=True, complete=True):
        self.records = list(records)
        self.hours = hours
        self.answered = answered
        self.complete = complete
        self.start_ts = 0.0
        self.end_ts = 0.0


class _FakeClient:
    # A session is already up. The piggyback waits on this flag before it
    # lists, so a fake without it silently skips the wait and the tests below
    # would prove nothing about the code they exercise; a fake that sets it
    # False would make every one of them sit out the full window. Class-level
    # so a subclass can replace it with a property.
    has_live_session = True

    def __init__(self, result=None):
        self._result = result
        self.calls = 0
        self.device_id = "dev1"

    async def async_get_sd_recordings(self, **kwargs):
        self.calls += 1
        return self._result


def _coord(result=None) -> AidotCameraUpdateCoordinator:
    """A camera coordinator with only the SD-cache state initialised.

    __new__ rather than __init__: the constructor needs hass, a config entry
    and the device manager, and none of the behaviour under test touches any of
    them. Building the real thing here would test Home Assistant's plumbing
    instead of this cache's rules.
    """
    import asyncio

    coord = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    coord.device_client = _FakeClient(result)
    coord.sd_cache = None
    coord._sd_opener = None
    coord._sd_lock = asyncio.Lock()
    return coord


async def test_listing_stores_a_cache_with_a_timestamp():
    coord = _coord(_FakeList(records=[1, 2]))
    assert await coord.async_list_sd_recordings() is True
    assert coord.sd_cache.records == [1, 2]
    assert coord.sd_cache.fetched_at > 0


async def test_no_session_leaves_the_cache_alone():
    # None means "could not ask". Writing an empty cache here would turn a
    # missing session into a claim that the card is empty, and the next browse
    # would show that claim for fifteen minutes.
    coord = _coord(None)
    assert await coord.async_list_sd_recordings() is False
    assert coord.sd_cache is None


async def test_a_piggyback_does_not_re_ask_a_fresh_cache():
    coord = _coord(_FakeList())
    coord.sd_cache = SdCache(fetched_at=time.time())
    await coord.async_piggyback_sd_refresh()
    assert coord.device_client.calls == 0


async def test_a_piggyback_re_asks_a_stale_cache():
    coord = _coord(_FakeList())
    coord.sd_cache = SdCache(fetched_at=time.time() - 10_000)
    await coord.async_piggyback_sd_refresh()
    assert coord.device_client.calls == 1


async def test_a_piggyback_never_opens_a_session():
    # The property the whole cost model rests on: a listing that rides an
    # existing session is nearly free, and one that opens its own is 15-70 s
    # and wakes the camera.
    opened = []
    coord = _coord(None)

    async def _open():
        opened.append(1)

    coord.register_session_opener(_open)
    await coord.async_piggyback_sd_refresh()
    assert opened == []


async def test_the_button_path_opens_a_session_first():
    opened = []
    coord = _coord(_FakeList())

    async def _open():
        opened.append(1)

    coord.register_session_opener(_open)
    assert await coord.async_list_sd_recordings(open_session=True) is True
    assert opened == [1] and coord.device_client.calls == 1


async def test_repeated_silence_never_latches_anything_off():
    # Silence is not evidence about the model: it looks the same for a dead
    # channel, a missing session and an empty slot. Latching "unsupported" off
    # it would tell a user their camera cannot do something it can, and would
    # stay latched after they insert a card.
    coord = _coord(None)
    for _ in range(5):
        assert await coord.async_list_sd_recordings() is False
    coord.device_client = _FakeClient(_FakeList(records=[1]))
    assert await coord.async_list_sd_recordings() is True
    assert coord.sd_cache.records == [1]


class _LateSessionClient(_FakeClient):
    """A client whose session does not exist when the opener returns.

    This is what the library does: starting the keepalive schedules the
    handshake and returns at once, and the session it will hold is assigned
    inside that background loop 15-70 s later. Until then a listing answers
    None, because there is nothing to ask through. The fake in the tests above
    answers regardless of session, which is precisely why they could not see a
    listing taken too early.
    """

    def __init__(self, result=None):
        super().__init__(result)
        self.session = False

    @property
    def has_live_session(self) -> bool:
        return self.session

    async def async_get_sd_recordings(self, **kwargs):
        self.calls += 1
        if not self.session:
            return None
        return self._result


async def test_the_button_waits_for_the_session_it_opened(monkeypatch):
    # The press is the only path allowed to spend a camera wake. Listing at the
    # instant the opener returns spends the wake and lists nothing, and the
    # browser then tells the user to press the button they just pressed.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 5.0)
    coord = _coord()
    client = _LateSessionClient(_FakeList(records=[1]))
    coord.device_client = client
    handshakes = []

    async def _open():
        async def _handshake():
            await asyncio.sleep(0.05)
            client.session = True

        handshakes.append(asyncio.ensure_future(_handshake()))

    coord.register_session_opener(_open)
    assert await coord.async_list_sd_recordings(open_session=True) is True
    assert coord.sd_cache.records == [1]
    await asyncio.gather(*handshakes)


async def test_a_session_that_never_arrives_writes_nothing(monkeypatch):
    # Giving up is a result, not a hang: the cache stays untouched rather than
    # recording an empty card the camera never reported.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 0.05)
    coord = _coord()
    coord.device_client = _LateSessionClient(_FakeList(records=[1]))

    async def _open():
        pass

    coord.register_session_opener(_open)
    assert await coord.async_list_sd_recordings(open_session=True) is False
    assert coord.sd_cache is None


async def test_a_listing_that_opened_nothing_does_not_wait_inside_the_lock(monkeypatch):
    # This wait belongs to the path that asked for the session, and it runs
    # while holding the lock. A listing that opened nothing has no open to wait
    # on, so it must answer at once rather than hold the lock - and a pressed
    # button behind it - for a minute. The piggyback's own wait is a separate
    # thing and happens before the lock is taken (see the tests below).
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 30.0)
    coord = _coord(None)
    started = time.monotonic()
    assert await coord.async_list_sd_recordings() is False
    assert time.monotonic() - started < 1.0
    assert coord.device_client.calls == 1


async def test_a_piggyback_gives_up_when_no_session_ever_arrives(monkeypatch):
    # Waiting is not the same as opening. Nobody opened a session here, so the
    # piggyback must run out its window and list nothing - and, crucially, send
    # nothing at all on the way: a listing IS the request, so a piggyback that
    # polled by asking would spend the requests it is waiting to be able to
    # spend cheaply.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 0.05)
    coord = _coord()
    coord.device_client = _LateSessionClient(_FakeList(records=[1]))
    await coord.async_piggyback_sd_refresh()
    assert coord.sd_cache is None
    assert coord.device_client.calls == 0


async def test_a_piggyback_lists_once_when_the_session_lands_late(monkeypatch):
    # The defect this whole change exists for. The piggyback is started the
    # instant the keepalive returns, and the keepalive returns before the
    # handshake it scheduled has produced a session - 8 s later on DTLS, up to
    # 70 s on a cold SDES camera. Listing at that instant asks with no session,
    # gets "could not ask", and leaves every card reading "Not listed yet"
    # however many times a session opens.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 5.0)
    coord = _coord()
    client = _LateSessionClient(_FakeList(records=[1]))
    coord.device_client = client

    async def _handshake():
        await asyncio.sleep(0.05)
        client.session = True

    handshake = asyncio.ensure_future(_handshake())
    await coord.async_piggyback_sd_refresh()
    await handshake
    assert coord.sd_cache is not None and coord.sd_cache.records == [1]
    # Once the session is there the listing is one round trip, not a poll.
    assert client.calls == 1


async def test_a_piggyback_stops_waiting_on_a_camera_the_cloud_calls_offline(
        monkeypatch):
    # An offline camera is not going to hand anyone a session, so there is
    # nothing to wait for and no reason to keep a task alive for a minute.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 30.0)
    coord = _coord()
    coord.device_client = _LateSessionClient(_FakeList(records=[1]))
    coord.device_client.status = SimpleNamespace(online=False)
    started = time.monotonic()
    await coord.async_piggyback_sd_refresh()
    assert time.monotonic() - started < 1.0
    assert coord.sd_cache is None


async def test_an_offline_camera_does_not_hold_the_lock_for_the_full_wait(monkeypatch):
    # A camera the cloud says is offline will not hand anyone a session, and
    # waiting out the window for it holds both this lock and a pressed button.
    monkeypatch.setattr(coordinator_module, "SD_SESSION_POLL_S", 0.01)
    monkeypatch.setattr(coordinator_module, "SD_SESSION_WAIT_S", 30.0)
    coord = _coord()
    coord.device_client = _LateSessionClient(_FakeList(records=[1]))
    coord.device_client.status = SimpleNamespace(online=False)

    async def _open():
        pass

    coord.register_session_opener(_open)
    started = time.monotonic()
    assert await coord.async_list_sd_recordings(open_session=True) is False
    assert time.monotonic() - started < 1.0


async def test_a_press_does_not_list_the_card_twice():
    # The real opener backgrounds a piggyback refresh as soon as the keepalive
    # is running, so every press schedules one while the press still holds the
    # lock. The lock serialises those two listings; only re-deciding staleness
    # inside it stops the second from being sent at all - two more AVIO
    # requests, up to 16 s of timeouts, to re-fetch what was just written.
    coord = _coord(_FakeList(records=[1]))
    piggybacks = []

    async def _open():
        piggybacks.append(
            asyncio.ensure_future(coord.async_piggyback_sd_refresh()))
        # Home Assistant starts tasks eagerly, so the piggyback's staleness
        # check really does run before the opener returns.
        await asyncio.sleep(0)

    coord.register_session_opener(_open)
    assert await coord.async_list_sd_recordings(open_session=True) is True
    await asyncio.gather(*piggybacks)
    assert coord.device_client.calls == 1


async def test_consecutive_silences_accumulate_and_an_answer_clears_them():
    # The streak is what stops a camera that never answers from being re-asked
    # on every session. It has to survive across listings, and any answer -
    # including "the card is empty" - has to reset it, or a camera that
    # recovers stays on the widened window forever.
    coord = _coord(_FakeList(answered=False))
    for expected in (1, 2, 3):
        await coord.async_list_sd_recordings()
        assert coord.sd_cache.unanswered_streak == expected

    coord.device_client = _FakeClient(_FakeList(records=[], answered=True))
    await coord.async_list_sd_recordings()
    assert coord.sd_cache.unanswered_streak == 0


async def test_a_listing_does_not_push_entity_state():
    # The camera coordinator's listeners are entity state writes driven by
    # device data. A background listing has no new device data behind it, so
    # firing them would write state for every camera entity for nothing.
    coord = _coord(_FakeList())
    fired = []
    coord.async_update_listeners = lambda: fired.append(1)
    await coord.async_list_sd_recordings()
    assert fired == []
