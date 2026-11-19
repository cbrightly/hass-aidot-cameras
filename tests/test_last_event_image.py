"""The last-event image: the newest detection's picture, fetched server-side.

The cloud's ``picUrl`` is a signed link that expires, so the entity fetches
the bytes once, when the event arrives, and serves them from memory. These
tests pin that: one fetch per event, none on a read, the previous picture kept
when a fetch fails, the event's own time as the entity's state, and nothing
that reaches for the camera. Every fetch is a double; no URL here is real.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import Platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.aidot import PLATFORMS, image
from custom_components.aidot.image import AidotLastEventImage

_COMPONENT = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "aidot"
)

T1 = 1_760_000_000_000
T2 = T1 + 60_000


class _Body:
    """The response's stream: hands the body over in pieces, counting them."""

    def __init__(self, response):
        self._response = response

    async def iter_chunked(self, size):
        body = self._response._body
        for start in range(0, len(body), size):
            self._response.chunks_read += 1
            yield body[start : start + size]


class _Response:
    def __init__(
        self, status=200, body=b"jpeg", content_type="image/jpeg", content_length=None
    ):
        self.status = status
        self._body = body
        self.content_type = content_type
        self.content_length = content_length
        self.content = _Body(self)
        self.chunks_read = 0

    async def read(self):
        raise AssertionError("the whole body was read before its size was known")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Answers each GET with the next queued reply; records what was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _Client:
    """A device client that fails the test if anything camera-facing is touched."""

    def __init__(self, listing=None):
        self.info = SimpleNamespace(
            dev_id="dev-1", model_id="A000088.x", name="Cam", mac="", hw_version="1"
        )
        self.async_get_cloud_recordings = AsyncMock(return_value=listing or [])

    def __getattr__(self, name):
        raise AssertionError(f"the image entity reached for device_client.{name}")


class _Coordinator:
    def __init__(self, listing=None):
        self.device_client = _Client(listing)
        self.data = None
        self.listeners: list = []

    def add_motion_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def fire(self, event):
        for cb in list(self.listeners):
            cb(event)


def _event(uuid="u1", t=T1, pic="https://cdn.invalid/u1.jpg"):
    ev = {"eventUuid": uuid, "eventTime": t, "eventCode": "1"}
    if pic is not None:
        ev["picUrl"] = pic
    return ev


def _entity(hass, coordinator=None):
    ent = AidotLastEventImage(hass, coordinator or _Coordinator())
    ent.hass = hass
    ent.async_write_ha_state = MagicMock()
    return ent


def _serve(session):
    return patch.object(image, "async_get_clientsession", lambda hass: session)


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    monkeypatch.setattr(image, "_EVENT_FETCH_DELAYS_S", (0, 0, 0))


async def _settle(ent):
    while ent._tasks:
        await asyncio.gather(*list(ent._tasks), return_exceptions=True)
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Identity and registration
# --------------------------------------------------------------------------- #


def test_the_platform_is_registered():
    assert Platform.IMAGE in PLATFORMS


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_the_entity_has_a_name(filename):
    data = json.loads((_COMPONENT / filename).read_text())
    assert data["entity"]["image"]["last_event"]["name"] == "Last event"


def test_the_entity_has_an_icon():
    data = json.loads((_COMPONENT / "icons.json").read_text())
    assert data["entity"]["image"]["last_event"]["default"].startswith("mdi:")


async def test_the_platform_adds_one_image_per_camera(hass):
    cams = {"a": _Coordinator()}
    cams["a"].device_client.info.dev_id = "a"
    refreshers = []
    account = SimpleNamespace(
        camera_coordinators=cams,
        async_add_listener=lambda cb: refreshers.append(cb) or (lambda: None),
    )
    entry = SimpleNamespace(runtime_data=account, async_on_unload=lambda cb: None)
    added = []
    await image.async_setup_entry(hass, entry, added.extend)
    assert [e.unique_id for e in added] == ["a_last_event"]
    assert isinstance(added[0], AidotLastEventImage)
    assert added[0].translation_key == "last_event"

    cams["b"] = _Coordinator()
    cams["b"].device_client.info.dev_id = "b"
    for cb in refreshers:
        cb()
        cb()
    assert [e.unique_id for e in added] == ["a_last_event", "b_last_event"]


async def test_it_stays_available_whatever_the_camera_is_doing(hass):
    ent = _entity(hass)
    assert not isinstance(ent, CoordinatorEntity)
    assert ent.available is True


# --------------------------------------------------------------------------- #
# No event yet
# --------------------------------------------------------------------------- #


async def test_no_event_yet_is_unknown_and_not_an_error(hass):
    coord = _Coordinator(listing=[])
    ent = _entity(hass, coord)
    session = _Session()
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
    assert ent.state is None
    assert ent.image_last_updated is None
    assert await ent.async_image() is None
    assert session.urls == []
    assert ent.async_write_ha_state.call_count == 0
    # Never the raw link.
    assert not isinstance(ent.image_url, str)


async def test_an_event_without_a_picture_changes_nothing(hass):
    coord = _Coordinator()
    ent = _entity(hass, coord)
    session = _Session()
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event(pic=None))
        coord.fire({"eventUuid": "u2", "eventTime": T2, "picUrl": ""})
        await _settle(ent)
    assert ent.state is None
    assert session.urls == []


async def test_a_failed_startup_listing_is_swallowed(hass):
    coord = _Coordinator()
    coord.device_client.async_get_cloud_recordings = AsyncMock(
        side_effect=RuntimeError("cloud down")
    )
    ent = _entity(hass, coord)
    with _serve(_Session()):
        await ent.async_added_to_hass()
        await _settle(ent)
    assert ent.state is None


# --------------------------------------------------------------------------- #
# An event arrives
# --------------------------------------------------------------------------- #


async def test_a_new_event_is_fetched_once_and_served_from_memory(hass):
    coord = _Coordinator()
    ent = _entity(hass, coord)
    session = _Session(_Response(body=b"first"))
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event())
        await _settle(ent)
        assert await ent.async_image() == b"first"
        # Reads (a dashboard reload) never fetch again.
        for _ in range(5):
            assert await ent.async_image() == b"first"
    assert session.urls == ["https://cdn.invalid/u1.jpg"]
    assert ent.image_last_updated == datetime.fromtimestamp(T1 / 1000, tz=UTC)
    assert ent.state == ent.image_last_updated.isoformat()
    assert ent.content_type == "image/jpeg"
    assert ent.async_write_ha_state.call_count == 1
    # The link is never exposed: not as the image URL, not as an attribute.
    assert not isinstance(ent.image_url, str)
    assert "cdn.invalid" not in repr(ent.extra_state_attributes)
    assert "cdn.invalid" not in repr(ent.state_attributes)


async def test_the_same_event_again_is_not_refetched(hass):
    ent = _entity(hass)
    session = _Session(_Response(body=b"first"))
    with _serve(session):
        await ent._async_show(_event(), (0,))
        await ent._async_show(_event(), (0,))
    assert len(session.urls) == 1
    assert ent.async_write_ha_state.call_count == 1


async def test_a_newer_event_replaces_the_picture(hass):
    ent = _entity(hass)
    session = _Session(
        _Response(body=b"first"), _Response(body=b"second", content_type="image/png")
    )
    with _serve(session):
        await ent._async_show(_event(), (0,))
        await ent._async_show(_event("u2", T2, "https://cdn.invalid/u2.jpg"), (0,))
    assert await ent.async_image() == b"second"
    assert ent.content_type == "image/png"
    assert ent.image_last_updated == datetime.fromtimestamp(T2 / 1000, tz=UTC)


async def test_an_older_event_arriving_late_does_not_replace_a_newer_one(hass):
    ent = _entity(hass)
    session = _Session(_Response(body=b"newer"))
    with _serve(session):
        await ent._async_show(_event("u2", T2, "https://cdn.invalid/u2.jpg"), (0,))
        await ent._async_show(_event("u1", T1), (0,))
    assert await ent.async_image() == b"newer"
    assert len(session.urls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        _Response(status=403),
        _Response(status=404),
        _Response(body=b""),
        TimeoutError(),
        OSError("reset"),
    ],
    ids=["expired", "missing", "empty", "timeout", "network"],
)
async def test_a_failed_fetch_keeps_the_previous_picture(hass, reply):
    ent = _entity(hass)
    session = _Session(_Response(body=b"first"), reply)
    with _serve(session):
        await ent._async_show(_event(), (0,))
        await ent._async_show(_event("u2", T2, "https://cdn.invalid/u2.jpg"), (0,))
    assert await ent.async_image() == b"first"
    assert ent.image_last_updated == datetime.fromtimestamp(T1 / 1000, tz=UTC)
    assert ent.async_write_ha_state.call_count == 1


async def test_an_oversized_reply_is_rejected(hass, monkeypatch):
    monkeypatch.setattr(image, "_MAX_IMAGE_BYTES", 4)
    ent = _entity(hass)
    with _serve(_Session(_Response(body=b"12345"))):
        await ent._async_show(_event(), (0,))
    assert await ent.async_image() is None
    assert ent.state is None


async def test_an_oversized_reply_is_given_up_on_at_the_cap(hass, monkeypatch):
    """Not after reading all of it: the far end decides how much that is."""
    monkeypatch.setattr(image, "_MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(image, "_READ_CHUNK_BYTES", 4)
    ent = _entity(hass)
    reply = _Response(body=b"x" * 4000)
    with _serve(_Session(reply)):
        await ent._async_show(_event(), (0,))
    assert await ent.async_image() is None
    assert reply.chunks_read == 3


async def test_a_reply_that_declares_too_large_a_size_is_not_read(hass, monkeypatch):
    monkeypatch.setattr(image, "_MAX_IMAGE_BYTES", 10)
    ent = _entity(hass)
    reply = _Response(body=b"x" * 4000, content_length=4000)
    with _serve(_Session(reply)):
        await ent._async_show(_event(), (0,))
    assert await ent.async_image() is None
    assert reply.chunks_read == 0


async def test_a_reply_at_the_cap_is_served(hass, monkeypatch):
    monkeypatch.setattr(image, "_MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(image, "_READ_CHUNK_BYTES", 4)
    ent = _entity(hass)
    with _serve(_Session(_Response(body=b"0123456789", content_length=10))):
        await ent._async_show(_event(), (0,))
    assert await ent.async_image() == b"0123456789"


async def test_a_new_event_is_retried_while_its_link_is_fresh(hass):
    coord = _Coordinator()
    ent = _entity(hass, coord)
    session = _Session(_Response(status=404), OSError("reset"), _Response(body=b"ok"))
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event())
        await _settle(ent)
    assert len(session.urls) == 3
    assert await ent.async_image() == b"ok"


async def test_retries_are_bounded(hass):
    coord = _Coordinator()
    ent = _entity(hass, coord)
    session = _Session(*[_Response(status=500) for _ in range(10)])
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event())
        await _settle(ent)
        # And nothing later goes back to the stale link.
        await ent.async_image()
    assert len(session.urls) == 3
    assert ent.state is None


async def test_a_reply_that_is_not_typed_as_an_image_is_served_as_jpeg(hass):
    ent = _entity(hass)
    with _serve(_Session(_Response(content_type="application/octet-stream"))):
        await ent._async_show(_event(), (0,))
    assert ent.content_type == "image/jpeg"


@pytest.mark.parametrize("bad", [None, "", "soon", 0, -5, True])
async def test_an_event_without_a_usable_time_is_stamped_now(hass, bad):
    ent = _entity(hass)
    ev = _event()
    ev["eventTime"] = bad
    before = datetime.now(tz=UTC)
    with _serve(_Session(_Response())):
        await ent._async_show(ev, (0,))
    assert before <= ent.image_last_updated <= datetime.now(tz=UTC)


async def test_a_made_up_time_does_not_turn_away_the_next_real_event(hass):
    """An event with no time is shown as of now. The next event's own time is
    the cloud's, which can read earlier than our clock did; it is still the
    newer event and its picture is shown."""
    ent = _entity(hass)
    untimed = _event("u1", None, "https://cdn.invalid/u1.jpg")
    # T1 is long before now, as a cloud clock behind ours would read.
    timed = _event("u2", T1, "https://cdn.invalid/u2.jpg")
    with _serve(_Session(_Response(body=b"untimed"), _Response(body=b"timed"))):
        await ent._async_show(untimed, (0,))
        await ent._async_show(timed, (0,))
    assert await ent.async_image() == b"timed"
    assert ent.image_last_updated == datetime.fromtimestamp(T1 / 1000, tz=UTC)


async def test_an_untimed_event_does_not_move_the_mark_older_events_are_held_to(hass):
    ent = _entity(hass)
    replies = [_Response(body=b"new"), _Response(body=b"untimed"), _Response()]
    with _serve(_Session(*replies)):
        await ent._async_show(_event("u2", T2, "https://cdn.invalid/u2.jpg"), (0,))
        await ent._async_show(_event("u3", None, "https://cdn.invalid/u3.jpg"), (0,))
        # Older than the newest event that had a time: still turned away.
        await ent._async_show(_event("u1", T1, "https://cdn.invalid/u1.jpg"), (0,))
    assert await ent.async_image() == b"untimed"


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


async def test_startup_shows_the_newest_event_the_cloud_already_holds(hass):
    coord = _Coordinator(listing=[_event("u9", T2, "https://cdn.invalid/u9.jpg")])
    ent = _entity(hass, coord)
    session = _Session(_Response(body=b"stored"))
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
    assert await ent.async_image() == b"stored"
    assert ent.image_last_updated == datetime.fromtimestamp(T2 / 1000, tz=UTC)
    assert session.urls == ["https://cdn.invalid/u9.jpg"]
    lister = coord.device_client.async_get_cloud_recordings
    assert lister.await_count == 1
    assert lister.await_args.kwargs == {"page": 1, "page_size": 10}


async def test_startup_skips_a_newest_event_that_has_no_picture(hass):
    """The newest event is not always one with a picture; the newest that has
    one is what there is to show."""
    coord = _Coordinator(
        listing=[
            _event("u9", T2 + 60_000, None),
            _event("u8", T2, "https://cdn.invalid/u8.jpg"),
            _event("u7", T1, "https://cdn.invalid/u7.jpg"),
        ]
    )
    ent = _entity(hass, coord)
    session = _Session(_Response(body=b"stored"))
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
    assert session.urls == ["https://cdn.invalid/u8.jpg"]
    assert ent.image_last_updated == datetime.fromtimestamp(T2 / 1000, tz=UTC)


async def test_startup_does_not_retry_a_failed_fetch(hass):
    coord = _Coordinator(listing=[_event()])
    ent = _entity(hass, coord)
    session = _Session(_Response(status=403), _Response())
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
    assert len(session.urls) == 1
    assert ent.state is None


async def test_the_event_the_startup_listing_showed_is_not_fetched_twice(hass):
    coord = _Coordinator(listing=[_event()])
    ent = _entity(hass, coord)
    session = _Session(_Response(body=b"stored"))
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event())
        await _settle(ent)
    assert len(session.urls) == 1


# --------------------------------------------------------------------------- #
# It leaves the camera alone
# --------------------------------------------------------------------------- #


async def test_nothing_reaches_for_the_camera(hass):
    # _Client raises on any attribute but the device info and the cloud event
    # listing, so a wake, a stream open or a snapshot here fails the test.
    coord = _Coordinator(listing=[_event()])
    ent = _entity(hass, coord)
    session = _Session(_Response(), _Response())
    with _serve(session):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event("u2", T2, "https://cdn.invalid/u2.jpg"))
        await _settle(ent)
        await ent.async_image()
    assert len(session.urls) == 2


async def test_removal_stops_listening_and_cancels_a_fetch_in_flight(hass):
    coord = _Coordinator()
    ent = _entity(hass, coord)
    started = asyncio.Event()

    class _HangingBody:
        async def iter_chunked(self, size):
            started.set()
            await asyncio.sleep(3600)
            yield b""

    class _Hang(_Response):
        def __init__(self):
            super().__init__()
            self.content = _HangingBody()

    with _serve(_Session(_Hang())):
        await ent.async_added_to_hass()
        await _settle(ent)
        coord.fire(_event())
        await started.wait()
        for remove in list(ent._on_remove or []):
            remove()
        await _settle(ent)
    assert coord.listeners == []
    assert ent.state is None
