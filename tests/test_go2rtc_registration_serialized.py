"""Two registrations of the same camera must never reach go2rtc at once.

go2rtc 1.9.9 - the build the WebRTC custom integration runs on 127.0.0.1:1984,
which is where these registrations go - persists every ``PUT /api/streams`` by
reading its ``go2rtc.yaml``, patching the stream in, and writing the file back,
with nothing guarding that read-modify-write. Two PUTs for the same name that
overlap can therefore both append the key, and a duplicate mapping key is fatal
to every later write: go2rtc answers each subsequent PUT for ANY camera with
``400 yaml: unmarshal errors: mapping key "<name>" already defined``.

Reproduced on the box 2026-09-22 (sequential PUTs replace cleanly; only
overlapping ones duplicate) and found again 2026-09-27: ``aidot_b5284fc70d1e``
held twice in a file last written 09-25, a day after the tolerance fix for the
resulting 400 shipped - that fix kept cameras on go2rtc but never stopped the
duplicate from being written. The box log shows how ordinary the overlap is:
four cameras each completed two registrations 0.12-0.57 s apart when a
dashboard opened, because ``stream_source()`` - which registers - runs on every
WebRTC offer and every image request.

The DELETE-then-PUT that rebuilds an unstable camera's stream has to be atomic
for the same reason: a second caller's PUT landing between the two re-creates
the definition the first caller is about to replace.

Different cameras stay concurrent - serialising the whole fleet would put seven
registrations in a queue on every dashboard open.
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


async def test_different_cameras_still_register_concurrently():
    hass = _new_hass()
    cams = [
        _make_camera("aaaa0000bbbb1111cccc2222dddd3333", hass),
        _make_camera("eeee4444ffff5555aaaa6666bbbb7777", hass),
    ]

    go2rtc = await _publish_concurrently(cams, unstable=False)

    assert go2rtc.max_in_flight_total == 2


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
