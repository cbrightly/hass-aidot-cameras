"""No two go2rtc config writes from this integration ever overlap - any names.

go2rtc 1.9.9 - the build the WebRTC custom integration runs on 127.0.0.1:1984,
which is where these registrations go - persists every ``PUT /api/streams`` and
``DELETE`` by reading its ``go2rtc.yaml``, patching the stream in, and writing
the file back with ``os.WriteFile`` (truncate, then write), with nothing
guarding that read-modify-write. Two writes that overlap - for ANY two names,
not only the same one - can read an empty or half-written file and write back
whatever they made of it: whole sections gone, a camera's key lost, or a key
duplicated when a reader got the head of one write and the tail of another. A
duplicate mapping key is fatal to every later write: go2rtc answers each
subsequent PUT for ANY camera with ``400 yaml: unmarshal errors: mapping key
"<name>" already defined``, and only a hand edit of the file ends it.

Reproduced against a private go2rtc 1.9.9 on the box, 2026-10-08: 40 bursts of
15 PUTs over 5 different names (what a Home Assistant start sends) left 18
files with keys or sections missing and 1 with a duplicate key and a torn line.
The per-NAME lock of 2.30.6 (built on the 2026-09-22 observation that only
same-name overlaps duplicated) let the file be corrupted again on 2026-10-06,
64 s after a restart: the same camera duplicated, every later PUT 400.

The DELETE-then-PUT that rebuilds an unstable camera's stream has to be atomic
for the same reason: a second caller's PUT landing between the two re-creates
the definition the first caller is about to replace.

The cost of one queue for the whole fleet is a few milliseconds per write - a
PUT to a loopback go2rtc - against a dashboard open that sends seven at once.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.aidot import camera as cam_mod
from custom_components.aidot.camera import AidotCamera


class _RecordingGo2rtc:
    """A go2rtc client that records how many calls are in flight at once."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.in_flight: dict[str, int] = {}
        self.max_in_flight: dict[str, int] = {}
        self.max_in_flight_total = 0

    async def _call(self, op: str, name: str) -> bool:
        self.in_flight[name] = self.in_flight.get(name, 0) + 1
        self.max_in_flight[name] = max(
            self.max_in_flight.get(name, 0), self.in_flight[name]
        )
        self.max_in_flight_total = max(
            self.max_in_flight_total, sum(self.in_flight.values())
        )
        self.events.append((op, name))
        await asyncio.sleep(0.02)  # a real PUT takes milliseconds; let others run
        self.in_flight[name] -= 1
        return True

    async def ensure_stream(self, name, source, *, extra_sources=()):
        return await self._call("put", name)

    async def remove_stream(self, name):
        return await self._call("delete", name)

    async def has_stream(self, name):
        return True

    def rtsp_url(self, name):
        return f"rtsp://127.0.0.1:8554/{name}"


def _new_hass():
    hass = MagicMock()
    hass.data = {}
    return hass


def _make_camera(device_id: str, hass=None) -> AidotCamera:
    """A real entity; entities of one Home Assistant share its ``hass``."""
    cam = object.__new__(AidotCamera)
    cam.coordinator = MagicMock()
    cam.coordinator.device_client = SimpleNamespace(device_id=device_id)
    cam._rtsp_name = device_id
    cam.hass = hass if hass is not None else _new_hass()
    cam._push_mode = AsyncMock(return_value=True)
    return cam


async def _publish_concurrently(cams, *, unstable: bool) -> _RecordingGo2rtc:
    go2rtc = _RecordingGo2rtc()
    with (
        patch.object(cam_mod, "_GO2RTC_ENABLED", True),
        patch.object(cam_mod, "Go2rtcClient", return_value=go2rtc),
        patch.object(cam_mod, "async_get_clientsession", MagicMock()),
        patch.object(cam_mod, "_sprop_is_unstable", return_value=unstable),
    ):
        results = await asyncio.gather(
            *(c._publish_to_go2rtc("http://127.0.0.1:1/x.ts") for c in cams)
        )
    assert all(results), results
    return go2rtc


async def test_two_registrations_of_one_camera_never_overlap():
    cam = _make_camera("aaaa0000bbbb1111cccc2222dddd3333")

    go2rtc = await _publish_concurrently([cam, cam], unstable=False)

    assert go2rtc.max_in_flight == {"aidot_aaaa0000bbbb": 1}
    assert [op for op, _ in go2rtc.events] == ["put", "put"]


async def test_the_delete_then_put_rebuild_is_not_interleaved():
    cam = _make_camera("aaaa0000bbbb1111cccc2222dddd3333")

    go2rtc = await _publish_concurrently([cam, cam], unstable=True)

    assert go2rtc.max_in_flight == {"aidot_aaaa0000bbbb": 1}
    assert [op for op, _ in go2rtc.events] == ["delete", "put", "delete", "put"]


async def test_the_same_camera_seen_through_two_entities_is_still_serialised():
    """Two entity objects for one camera (e.g. across a reload) share the name."""
    hass = _new_hass()
    first = _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass)
    second = _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass)

    go2rtc = await _publish_concurrently([first, second], unstable=False)

    assert go2rtc.max_in_flight == {"aidot_aaaa0000bbbb": 1}


async def test_different_cameras_never_write_go2rtc_at_once():
    # go2rtc's config write is one unguarded read-modify-write of one file, so
    # two writes for two names tear it as surely as two for one name.
    hass = _new_hass()
    cams = [
        _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass),
        _make_camera("eeee4444ffff5555aaaa6666bbbb7777", hass),
        _make_camera("1111222233334444555566667777888a", hass),
    ]

    go2rtc = await _publish_concurrently(cams, unstable=True)

    assert go2rtc.max_in_flight_total == 1
    assert len(go2rtc.events) == 6  # every camera's delete and put still ran


async def test_one_cameras_removal_never_overlaps_anothers_registration():
    hass = _new_hass()
    going = _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass)
    coming = _make_camera("eeee4444ffff5555aaaa6666bbbb7777", hass)
    go2rtc = _RecordingGo2rtc()
    with (
        patch.object(cam_mod, "_GO2RTC_ENABLED", True),
        patch.object(cam_mod, "Go2rtcClient", return_value=go2rtc),
        patch.object(cam_mod, "async_get_clientsession", MagicMock()),
        patch.object(cam_mod, "_sprop_is_unstable", return_value=False),
    ):
        await asyncio.gather(
            going._unpublish_from_go2rtc(),
            coming._publish_to_go2rtc("http://127.0.0.1:1/x.ts"),
        )

    assert go2rtc.max_in_flight_total == 1


async def test_a_removal_never_overlaps_a_registration_of_the_same_camera():
    """A reload removes the old entity (DELETE) while the new one registers (PUT)."""
    hass = _new_hass()
    old = _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass)
    new = _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass)
    go2rtc = _RecordingGo2rtc()
    with (
        patch.object(cam_mod, "_GO2RTC_ENABLED", True),
        patch.object(cam_mod, "Go2rtcClient", return_value=go2rtc),
        patch.object(cam_mod, "async_get_clientsession", MagicMock()),
        patch.object(cam_mod, "_sprop_is_unstable", return_value=False),
    ):
        await asyncio.gather(
            old._unpublish_from_go2rtc(),
            new._publish_to_go2rtc("http://127.0.0.1:1/x.ts"),
        )

    assert go2rtc.max_in_flight == {"aidot_aaaa0000bbbb": 1}
