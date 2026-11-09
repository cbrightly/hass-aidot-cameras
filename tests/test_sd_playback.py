"""Playing a recording off a camera's SD card.

The library plays a card recording into an MPEG-TS (async_open_sd_playback,
played end to end on the A001064, A001513 and A000088 on 2026-10-27). Here:

* the media browser offers a listed recording as playable, keyed by the
  recording rather than its place in the list, on the models that have
  played, and explains everything else instead of failing;
* resolving it only checks and signs - a tap opens nothing;
* the view captures the recording once (holding the camera's one media slot
  for the clip), then serves an MP4 made from that file, so the browser seeks
  a finished file and a replay costs no session;
* while the slot is held, nothing opens a live session.
"""

import asyncio
import os
import pathlib
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aidot_cameras.camera.sd_events import SdEvent
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.config_entries import ConfigEntryState

from custom_components.aidot import coordinator as coordinator_mod
from custom_components.aidot import sd_recordings as sd_mod
from custom_components.aidot.coordinator import SLOT_SD, AidotCameraUpdateCoordinator
from custom_components.aidot.media_source import SOURCE_SD, AidotMediaSource
from custom_components.aidot.sd_proxy import (
    SD_PROXY_PATH,
    _safe_key,
    _sd_event,
    mp4_argv,
    play_refusal,
    sign_sd_playback_url,
)
from custom_components.aidot.proxy import (
    SD_CACHE_PREFIX,
    _enforce_cache_limits,
    _verify_sig,
)
from custom_components.aidot.sd_recordings import (
    SdCache,
    find_sd_record,
    sd_battery_too_low,
    sd_capture_done,
    sd_playable_model,
    sd_record_key,
)

DEV = "c0ffee11c0ffee22c0ffee33c0ffee44"
REC = SdEvent(2026, 10, 27, 21, 47, 20, 0, 0, 0)


def _cache(records=(REC,), answered=True):
    return SdCache(records=list(records), answered=answered, fetched_at=time.time())


# --- the record key and the checks ------------------------------------------


def test_the_key_names_the_recording_not_its_place_in_the_list():
    assert sd_record_key(REC) == "sd-20261027T214720-0"
    later = SdEvent(2026, 10, 27, 22, 0, 0, 0, 3, 0)  # channel, event, status
    assert sd_record_key(later) == "sd-20261027T220000-3"
    # A new recording ahead of it does not change what the key finds.
    assert find_sd_record(_cache([later, REC]), sd_record_key(REC)) is REC


def test_only_a_listed_recording_can_be_found():
    assert find_sd_record(_cache(), "sd-20261027T214721-0") is None
    assert find_sd_record(_cache(), "v1:abcd") is None
    assert find_sd_record(None, sd_record_key(REC)) is None
    # A camera that did not answer listed nothing.
    assert find_sd_record(_cache(answered=False), sd_record_key(REC)) is None


def test_the_models_that_have_played_are_allowed():
    for model in ("LK.IPC.A001064", "LK.IPC.A001513", "LK.IPC.A000088-1"):
        assert sd_playable_model(model)
    assert not sd_playable_model("LK.IPC.A009999")
    assert not sd_playable_model(None)


def test_a_battery_camera_below_the_floor_is_spared():
    assert sd_battery_too_low(True, 19)
    assert not sd_battery_too_low(True, 20)
    assert not sd_battery_too_low(True, None)  # unknown is allowed
    assert not sd_battery_too_low(False, 5)  # mains


class TestWhenACaptureEnds:
    def _done(self, **kw):
        base = dict(
            now=100.0,
            opened_at=90.0,
            first_bytes_at=None,
            last_growth_at=None,
            clip_seconds=None,
        )
        base.update(kw)
        return sd_capture_done(**base)

    def test_nothing_yet_waits(self):
        assert self._done() is None

    def test_the_clip_length_plus_margin_from_the_first_byte(self):
        # 60 s clip + 8 s margin, counted from the first byte.
        assert (
            self._done(
                now=167.0,
                first_bytes_at=100.0,
                last_growth_at=167.0,
                clip_seconds=60,
                opened_at=99.0,
            )
            is None
        )
        assert (
            self._done(
                now=168.0,
                first_bytes_at=100.0,
                last_growth_at=168.0,
                clip_seconds=60,
                opened_at=99.0,
            )
            == "clip"
        )

    def test_silence_after_media_ends_it(self):
        assert (
            self._done(now=105.0, first_bytes_at=95.0, last_growth_at=100.0) == "idle"
        )
        assert self._done(now=104.0, first_bytes_at=95.0, last_growth_at=100.0) is None

    def test_a_stall_short_of_the_clip_is_waited_out(self):
        # 19 s of a 60 s clip, then 6 s of nothing: a stall, not the end.
        kw = dict(
            first_bytes_at=100.0, last_growth_at=119.0, clip_seconds=60, opened_at=99.0
        )
        assert self._done(now=125.0, **kw) is None
        assert self._done(now=139.0, **kw) == "idle"
        # Near the clip's end, the short idle applies again.
        kw = dict(
            first_bytes_at=100.0, last_growth_at=155.0, clip_seconds=60, opened_at=99.0
        )
        assert self._done(now=160.0, **kw) == "idle"

    def test_the_cap_whatever_else(self):
        assert self._done(now=210.0, opened_at=90.0) == "cap"

    def test_the_camera_end_wins_over_every_timer(self):
        # A DTLS night clip: 15 s of a 60 s clip in, short of 0.9 x clip,
        # where the stall wait would hold it and then call it idle.
        kw = dict(
            first_bytes_at=100.0, last_growth_at=115.0, clip_seconds=60, opened_at=99.0
        )
        assert self._done(now=115.5, **kw) is None
        assert self._done(now=115.5, ended=True, **kw) == "ended"
        # Ahead of the cap, the clip length and the idle too.
        assert self._done(now=210.0, opened_at=90.0, ended=True) == "ended"
        assert self._done(now=168.0, ended=True, **kw) == "ended"
        assert self._done(now=139.0, ended=True, **kw) == "ended"

    def test_without_the_end_the_fallbacks_still_apply(self):
        kw = dict(
            first_bytes_at=100.0, last_growth_at=115.0, clip_seconds=60, opened_at=99.0
        )
        assert self._done(now=135.0, ended=False, **kw) == "idle"
        assert self._done(now=210.0, opened_at=90.0, ended=False) == "cap"


# --- the media browser -------------------------------------------------------


class _Item:
    def __init__(self, identifier):
        self.identifier = identifier


def _coord(
    model="LK.IPC.A001064", cache=None, online=True, battery=None, is_battery=False
):
    dc = SimpleNamespace(
        info=SimpleNamespace(name="Cam", model_id=model),
        device_id=DEV,
        async_open_sd_playback=AsyncMock(),
        is_battery_camera=is_battery,
        status=SimpleNamespace(
            online=online, battery_remaining=battery, sd_card_present=True
        ),
    )
    return SimpleNamespace(
        device_client=dc, sd_cache=cache if cache is not None else _cache()
    )


@pytest.fixture
def coords(monkeypatch):
    found = {}
    from custom_components.aidot import media_source, sd_proxy

    for mod in (media_source, sd_proxy):
        monkeypatch.setattr(mod, "get_camera_coordinators", lambda _hass: found)
    return found


async def _day(coords, coord):
    coords[DEV] = coord
    src = AidotMediaSource(hass=None)
    from custom_components.aidot.sd_recordings import record_days
    from homeassistant.util import dt as dt_util

    ((label, _records),) = record_days(
        coord.sd_cache.records, dt_util.DEFAULT_TIME_ZONE
    )
    return await src.async_browse_media(_Item(f"{DEV}|{SOURCE_SD}|{label}"))


async def test_a_listed_recording_is_playable_on_a_model_that_has_played(coords):
    day = await _day(coords, _coord())
    (leaf,) = day.children
    assert leaf.can_play is True
    assert leaf.identifier == f"{DEV}/{sd_record_key(REC)}"
    assert leaf.media_content_type == "video/mp4"


async def test_any_other_model_keeps_the_reference_listing(coords):
    day = await _day(coords, _coord(model="LK.IPC.A009999"))
    (leaf,) = day.children
    assert leaf.can_play is False
    assert f"|{SOURCE_SD}|" in leaf.identifier


async def test_resolving_signs_a_play_url_and_opens_nothing(coords):
    coords[DEV] = _coord()
    play = await AidotMediaSource(hass=None).async_resolve_media(
        _Item(f"{DEV}/{sd_record_key(REC)}")
    )
    assert play.url.startswith(SD_PROXY_PATH + "?")
    assert play.mime_type == "video/mp4"


@pytest.mark.parametrize(
    ("coord", "words"),
    [
        (lambda: _coord(cache=_cache(records=[])), "refresh"),
        (lambda: _coord(online=False), "offline"),
        (lambda: _coord(is_battery=True, battery=8), "battery"),
        (lambda: _coord(model="LK.IPC.A009999"), "model"),
    ],
)
async def test_what_cannot_play_says_why_at_the_tap(coords, coord, words):
    coords[DEV] = coord()
    with pytest.raises(Unresolvable) as exc:
        await AidotMediaSource(hass=None).async_resolve_media(
            _Item(f"{DEV}/{sd_record_key(REC)}")
        )
    assert words in str(exc.value).lower()


# --- the view's URL and command ---------------------------------------------


def test_a_play_url_is_signed_for_the_card_and_only_the_card():
    url = sign_sd_playback_url(DEV, "sd-20261027T214720-0", now=1000.0)
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert _verify_sig(DEV, _sd_event(q["key"]), q["exp"], q["sig"], now=1001.0)
    # The same signature names no cloud clip: the namespaces differ.
    assert not _verify_sig(DEV, q["key"], q["exp"], q["sig"], now=1001.0)


def test_only_keys_of_the_recording_shape_get_through():
    assert _safe_key("sd-20261027T214720-0")
    for bad in ("v1:abc", "sd-../../etc", "sd-" + "9" * 60, "sd-a b"):
        assert _safe_key(bad) is None


def _argv(**kw):
    base = dict(
        video_codec="hevc",
        audio=None,
        alaw_path=None,
        webkit=True,
        transcode_args=["-c:v", "libx264"],
    )
    base.update(kw)
    return " ".join(mp4_argv("ffmpeg", "in.ts", "out.mp4", **base))


def test_hevc_is_copied_for_webkit_and_transcoded_for_the_rest():
    assert "-c:v copy -tag:v hvc1" in _argv()
    assert "-c:v libx264" in _argv(webkit=False)


def test_an_h264_recording_is_copied_for_everyone():
    assert "-c:v copy" in _argv(video_codec="h264", webkit=False)
    assert "hvc1" not in _argv(video_codec="h264")


def test_sdes_g711_is_decoded_as_alaw_and_dtls_audio_is_mapped():
    sdes = _argv(audio="g711", alaw_path="a.alaw")
    assert "-f alaw -ar 8000 -ac 1 -i a.alaw" in sdes and "-map 1:a:0" in sdes
    assert "-map 0:a:0" in _argv(audio="audio")
    assert "-c:a aac" in sdes


def test_aac_from_a_dtls_recording_is_copied_not_encoded_again():
    dtls = _argv(audio="aac")
    assert "-map 0:a:0" in dtls and "-c:a copy" in dtls
    assert "-c:a aac" not in dtls
    assert "-c:a aac" in _argv(audio="audio")


def test_card_clips_outlive_cloud_clips_in_the_shared_cache(tmp_path):
    old = time.time() - 3 * 24 * 3600
    cloud = tmp_path / "v1_abc_v3.mp4"
    card = tmp_path / f"{SD_CACHE_PREFIX}{DEV}_sd-x_v1.ts"
    part = tmp_path / f"{SD_CACHE_PREFIX}{DEV}_sd-y_v1.part.ts"
    for p in (cloud, card, part):
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    _enforce_cache_limits(str(tmp_path), 10**9, 24 * 3600)
    assert not cloud.exists()
    assert card.exists()
    assert part.exists()  # a capture in progress is never swept


def test_a_camera_that_cannot_play_is_named_before_any_work():
    assert play_refusal(_coord()) is None
    assert play_refusal(_coord(model="X")) == "model"
    assert play_refusal(_coord(online=False)) == "offline"
    assert play_refusal(_coord(is_battery=True, battery=3)) == "battery"


# --- the capture and the media slot -----------------------------------------


def _reply(clip_seconds, result=0):
    payload = struct.pack("<III", 0x10, result, clip_seconds) + struct.pack(
        "<HBBBBBB", 2026, 10, 27, 3, 21, 47, 20
    )
    return SimpleNamespace(payload=payload)


class _Session:
    def __init__(self, reply, packets):
        self._reply, self._packets = reply, iter(packets)
        self.stopped = False
        self.last = 0

    async def playback_reply(self, timeout):
        return self._reply

    def media_stats(self):
        self.last = next(self._packets, self.last)
        return {"packets": self.last}

    async def stop(self):
        self.stopped = True


def _capturing_coordinator(hass, session, *, battery=False, warm=True):
    coord = object.__new__(AidotCameraUpdateCoordinator)
    coord.hass = hass
    seen = {}

    async def _open(record, *, output_path, max_seconds):
        seen["owner"] = coord.media_slot_owner
        seen["path"] = output_path
        return session

    coord.device_client = SimpleNamespace(
        device_id=DEV,
        is_battery_camera=battery,
        async_open_sd_playback=_open,
        async_stop_streaming=AsyncMock(),
    )
    coord.media_slot_owner = None
    coord._slot_lock = asyncio.Lock()
    coord._sd_stop_requested = False
    coord._live_stop = AsyncMock()
    coord._live_is_warm = lambda: warm
    coord._live_rewarm = AsyncMock()
    coord._rewarm_task = None
    coord._sd_playing = None
    coord._sd_waiting = {}
    coord._sd_cancelled = set()
    # A real-shaped entry, so the bound branch of spawn_entry_task is the one
    # exercised (the re-warm runs on it).
    coord.config_entry = SimpleNamespace(
        async_create_background_task=lambda _hass, coro, _name: (
            hass.async_create_background_task(coro, _name)
        ),
        state=ConfigEntryState.LOADED,
    )
    return coord, seen


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(coordinator_mod, "_CAPTURE_POLL_S", 0.01)
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_IDLE_S", 0.1)
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_MARGIN_S", 0.0)


async def test_a_play_holds_the_slot_stops_live_and_gives_it_back(hass, fast):
    # A 1 s clip, delivered in full (media for 2 s of 10 ms polls).
    session = _Session(_reply(1), range(0, 2000, 10))
    coord, seen = _capturing_coordinator(hass, session)
    out = await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts")
    await hass.async_block_till_done()
    assert out == "ok"
    assert seen["owner"] == SLOT_SD
    assert coord.media_slot_owner is None
    coord._live_stop.assert_awaited_once()
    coord._live_rewarm.assert_awaited_once_with(True)  # it was warm, on mains
    assert session.stopped


async def test_a_battery_camera_is_not_warmed_again(hass, fast):
    coord, _ = _capturing_coordinator(
        hass, _Session(_reply(1), range(0, 2000, 10)), battery=True
    )
    assert await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts") == "ok"
    await hass.async_block_till_done()
    # Handed back (its status cleared) but not warmed.
    coord._live_rewarm.assert_awaited_once_with(False)


async def test_a_camera_that_refuses_is_reported_as_refusing(hass, fast):
    session = _Session(_reply(0, result=0xFFFFFFFF), [0])
    coord, _ = _capturing_coordinator(hass, session, warm=False)
    assert await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts") == "refused"
    assert session.stopped and coord.media_slot_owner is None


async def test_nothing_arriving_is_no_media_not_ok(hass, fast, monkeypatch):
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_CAP_S", 0.2)
    coord, _ = _capturing_coordinator(hass, _Session(None, [0]), warm=False)
    assert await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts") == "no_media"


async def test_the_stop_service_ends_a_play_early(hass, fast, monkeypatch):
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_IDLE_S", 60.0)
    session = _Session(_reply(60), range(0, 10**6, 7))
    coord, _ = _capturing_coordinator(hass, session, warm=False)
    assert coord.request_sd_playback_stop() is False  # nothing playing yet
    task = asyncio.ensure_future(
        coord.async_capture_sd_recording(REC, "/tmp/x.part.ts")
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if coord.media_slot_owner == SLOT_SD and session.last:
            break
    assert coord.request_sd_playback_stop() is True
    # Stopped well short of the 60 s clip: kept for a while, not as the
    # recording.
    assert await asyncio.wait_for(task, 2) == "partial"
    assert session.stopped


# --- the live view backs off -------------------------------------------------


async def test_no_live_session_opens_while_a_recording_holds_the_slot():
    from custom_components.aidot.const import CONF_MOTION_PREWARM
    from tests.test_camera import _make_camera

    # Pre-connect on motion turned on, so only the playback guard can stop the
    # motion handler. The guard is checked first, but with the option off
    # the option would stop it too and this would pass without the guard.
    cam = _make_camera(options={CONF_MOTION_PREWARM: True})
    cam.coordinator.media_slot_owner = SLOT_SD
    cam.coordinator.device_client.status = SimpleNamespace(online=True)
    assert await cam._prewarm_stream() is False
    cam.coordinator.device_client.start_keepalive.assert_not_awaited()
    cam._prewarm_task = None
    cam._on_motion_prewarm({})
    assert cam._prewarm_task is None  # nothing scheduled
    cam.coordinator.spawn_entry_task.assert_not_called()


async def test_the_re_warm_after_a_play_is_bound_to_the_entry(hass, fast):
    """So an unload cancels it rather than letting it open a session on a
    coordinator that is gone."""
    from homeassistant.config_entries import ConfigEntryState

    coord, _ = _capturing_coordinator(hass, _Session(_reply(1), range(0, 2000, 10)))
    spawned = []

    def _spawn(hass_, coro, name):
        spawned.append(name)
        return asyncio.ensure_future(coro)

    coord.config_entry = SimpleNamespace(
        async_create_background_task=_spawn, state=ConfigEntryState.LOADED
    )
    assert await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts") == "ok"
    assert spawned == ["aidot rewarm after sd playback"]
    await asyncio.gather(coord._rewarm_task)


async def test_yielding_the_slot_stops_the_live_session():
    from tests.test_camera import _make_camera

    cam = _make_camera()
    cam.coordinator.device_client.async_stop_streaming = AsyncMock()
    cam.async_write_ha_state = MagicMock()
    await cam._yield_slot_to_sd_playback()
    cam.coordinator.device_client.async_stop_streaming.assert_awaited_once()
    assert "SD card" in cam._active_status()


def test_sdes_audio_is_placed_by_time_with_its_holes_kept():
    # Packets as an A001064 sent them: 0.32 s each; the second stamped 0.256 s
    # after the first (an overlap), the third 0.768 s after the second - a
    # hole that copying the bytes would close up.
    from custom_components.aidot.sd_proxy import place_alaw

    a, b, c = b"\x01" * 2560, b"\x02" * 2560, b"\x03" * 2560
    out = place_alaw([(1.4, a), (1.656, b), (2.424, c)], 1.4)
    assert out[:2560] == a  # nothing overwritten
    assert out[2560:5120] == b  # the early packet goes on after a
    assert out[5120:8192] == b"\xd5" * (8192 - 5120)  # the hole is silence
    assert out[8192:] == c  # back on time: 1.024 s in
    # Sound from before the first frame is dropped, not shifted onto it.
    assert place_alaw([(1.3, a)], 1.4) == a[800:]


async def test_a_reply_that_raises_does_not_fail_the_play(hass, fast):
    class _Raising(_Session):
        async def playback_reply(self, timeout):
            raise ConnectionError("channel closed")

    coord, _ = _capturing_coordinator(hass, _Raising(None, [0, 5, 9]), warm=False)
    assert await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts") == "ok"


async def test_a_second_play_keeps_the_live_view_the_first_found_warm(hass, fast):
    # The first play's re-warm is still opening when the second play starts:
    # the camera was warm before the first, so it counts as warm now.
    coord, _ = _capturing_coordinator(
        hass, _Session(_reply(1), range(0, 2000, 10)), warm=False
    )
    started = asyncio.Event()

    async def _slow_rewarm(warm):
        started.set()
        await asyncio.sleep(30)

    coord._rewarm_task = asyncio.ensure_future(_slow_rewarm(True))
    await started.wait()
    rewarm = AsyncMock()
    coord._live_rewarm = rewarm
    first = coord._rewarm_task
    assert await coord.async_capture_sd_recording(REC, "/tmp/x.part.ts") == "ok"
    await hass.async_block_till_done()
    assert first.cancelled()
    rewarm.assert_awaited_once_with(True)


async def test_no_listing_crosses_a_play(hass):
    coord = object.__new__(AidotCameraUpdateCoordinator)
    coord.device_client = SimpleNamespace(async_get_sd_recordings=AsyncMock())
    coord.media_slot_owner = SLOT_SD
    assert await coord.async_list_sd_recordings() is False
    coord.device_client.async_get_sd_recordings.assert_not_awaited()


def test_a_partial_recording_is_dropped_whole(tmp_path):
    from custom_components.aidot.sd_proxy import _unlink_stem

    stem = f"{SD_CACHE_PREFIX}{DEV}_sd-20261027T214720-0_v1"
    keep = tmp_path / f"{SD_CACHE_PREFIX}{DEV}_sd-20261027T214721-0_v1.ts"
    for suffix in (".ts", ".ts.alaw", "_webkit.mp4", "_any.mp4"):
        (tmp_path / f"{stem}{suffix}").write_bytes(b"x")
    keep.write_bytes(b"x")
    _unlink_stem(str(tmp_path), stem)
    assert [p.name for p in tmp_path.iterdir()] == [keep.name]


async def test_the_panel_stops_a_fetch_with_the_play_url_s_own_signature(coords):
    # The clip panel knows the device id and the signed play URL, not the
    # camera entity; the URL's signature is what lets it stop that fetch.
    from urllib.parse import parse_qsl, urlsplit

    from custom_components.aidot.sd_proxy import AidotSdPlaybackView

    coord = _coord()
    coord.request_sd_playback_stop = MagicMock(return_value=True)
    coords[DEV] = coord
    view = object.__new__(AidotSdPlaybackView)
    view.hass = None
    url = sign_sd_playback_url(DEV, sd_record_key(REC))
    query = dict(parse_qsl(urlsplit(url).query))

    def _req(q):
        return SimpleNamespace(query=q)

    assert (await view.post(_req(query))).status == 204
    # Scoped to the recording the URL signs.
    coord.request_sd_playback_stop.assert_called_once_with(sd_record_key(REC))
    forged = dict(query, sig="0" * 64)
    assert (await view.post(_req(forged))).status == 403
    other = dict(query, key="sd-20261027T214721-0")
    assert (await view.post(_req(other))).status == 403
    coord.request_sd_playback_stop.return_value = False
    assert (await view.post(_req(query))).status == 409
    assert (await view.post(_req(dict(query, key="v1:x")))).status == 400
    assert (await view.post(_req(dict(query, device="")))).status == 400
    expired = sign_sd_playback_url(DEV, sd_record_key(REC), now=0.0)
    assert (
        await view.post(_req(dict(parse_qsl(urlsplit(expired).query))))
    ).status == 403
    del coords[DEV]
    assert (await view.post(_req(query))).status == 404


async def test_a_stop_for_one_recording_leaves_another_s_fetch_alone(
    hass, fast, monkeypatch
):
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_IDLE_S", 60.0)
    session = _Session(_reply(60), range(0, 10**6, 7))
    coord, _ = _capturing_coordinator(hass, session, warm=False)
    task = asyncio.ensure_future(
        coord.async_capture_sd_recording(REC, "/tmp/x.part.ts")
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if coord._sd_playing:
            break
    assert coord.request_sd_playback_stop("sd-20990101T000000-0") is False
    assert not task.done()
    assert coord.request_sd_playback_stop(sd_record_key(REC)) is True
    assert await asyncio.wait_for(task, 2) == "partial"


async def test_a_queued_recording_stopped_before_its_turn_never_starts(
    hass, fast, monkeypatch
):
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_IDLE_S", 60.0)
    first = _Session(_reply(60), range(0, 10**6, 7))
    coord, seen = _capturing_coordinator(hass, first, warm=False)
    other = SdEvent(2026, 10, 27, 22, 0, 0, 0, 0, 0)
    t1 = asyncio.ensure_future(coord.async_capture_sd_recording(REC, "/tmp/a.part.ts"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if coord._sd_playing:
            break
    t2 = asyncio.ensure_future(
        coord.async_capture_sd_recording(other, "/tmp/b.part.ts")
    )
    await asyncio.sleep(0.05)
    # Stopping the queued one cancels it and leaves the running one going.
    assert coord.request_sd_playback_stop(sd_record_key(other)) is True
    assert not t1.done()
    assert coord.request_sd_playback_stop(sd_record_key(REC)) is True
    assert await asyncio.wait_for(t1, 2) == "partial"
    assert await asyncio.wait_for(t2, 2) == "cancelled"
    assert seen["path"] == "/tmp/a.part.ts"  # b was never opened


def test_every_live_open_goes_through_the_slot_check():
    # A start_keepalive anywhere else could open the live view in the middle
    # of a card recording and leave the playback empty.
    import ast
    import pathlib

    root = pathlib.Path(__file__).parents[1] / "custom_components" / "aidot"
    found = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "start_keepalive"
                ):
                    found.append((path.name, fn.name))
    assert set(found) == {("camera.py", "_start_live_keepalive")}


async def test_the_live_open_is_skipped_while_a_recording_holds_the_slot():
    from custom_components.aidot.camera import AidotCamera
    from custom_components.aidot.coordinator import SLOT_SD

    cam = object.__new__(AidotCamera)
    cam.coordinator = SimpleNamespace(media_slot_owner=SLOT_SD)
    dc = SimpleNamespace(start_keepalive=AsyncMock())
    assert await cam._start_live_keepalive(dc, rtsp_push_url="x") is False
    dc.start_keepalive.assert_not_awaited()
    cam.coordinator.media_slot_owner = None
    assert await cam._start_live_keepalive(dc, rtsp_push_url="x") is True
    dc.start_keepalive.assert_awaited_once_with(rtsp_push_url="x")


def test_a_partial_capture_is_dropped_after_its_window_even_after_a_restart(
    tmp_path,
):
    # The timer that drops it dies with Home Assistant; the mark on disk
    # does not, so the next play after the window fetches it again.
    from custom_components.aidot.sd_proxy import (
        _PARTIAL_KEEP_S,
        _drop_expired_partial,
        _mark_partial,
    )

    stem = tmp_path / f"{SD_CACHE_PREFIX}{DEV}_sd-20261027T214720-0_v1"
    raw = f"{stem}.ts"
    for suffix in (".ts", "_webkit.mp4"):
        pathlib.Path(f"{stem}{suffix}").write_bytes(b"x")
    _drop_expired_partial(raw)  # not partial: kept
    assert pathlib.Path(raw).exists()
    _mark_partial(raw)
    _drop_expired_partial(raw, now=time.time() + _PARTIAL_KEEP_S - 5)
    assert pathlib.Path(raw).exists()
    _drop_expired_partial(raw, now=time.time() + _PARTIAL_KEEP_S + 5)
    assert list(tmp_path.iterdir()) == []


async def test_a_fetch_that_fails_after_the_player_left_is_logged(caplog):
    from custom_components.aidot.sd_proxy import _log_orphaned_failure

    async def _boom():
        raise OSError("disk full")

    task = asyncio.ensure_future(_boom())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    _log_orphaned_failure(task)
    assert "after the player left" in caplog.text and "disk full" in caplog.text


def test_g711_received_twice_is_recognised_and_one_copy_is_not():
    # One copy at 8000 bytes a second, in 0.32 s packets; the reference box
    # (2026-10-28) recorded every packet twice, one tick apart.
    from custom_components.aidot.sd_proxy import g711_repeated

    once = [(i * 0.32, b"\xd5" * 2560) for i in range(100)]
    twice = sorted(once + [(t + 1 / 90000, b) for t, b in once])
    assert not g711_repeated(once)
    assert g711_repeated(twice)
    assert not g711_repeated(once[:3])  # too short to tell
    assert not g711_repeated([])


async def test_a_recording_the_camera_marks_ended_is_whole_however_fast_it_came(
    hass, fast
):
    # A 60 s night clip at 5 fps arrives in ~15 s (sent at the live 20 fps);
    # the camera then marks the end. That is the whole recording, not a
    # partial one to wait out and throw away.
    class _Ending(_Session):
        polls = 0

        @property
        def playback_ended(self):
            self.polls += 1
            return self.polls > 20

    session = _Ending(_reply(60), range(0, 10**6, 7))
    coord, _ = _capturing_coordinator(hass, session, warm=False)
    out = await asyncio.wait_for(
        coord.async_capture_sd_recording(REC, "/tmp/x.part.ts"), 5
    )
    assert out == "ok"
    assert session.stopped
    assert session.polls == 21  # it stopped at the camera's end mark


async def test_a_dtls_clip_that_ends_short_of_its_length_is_whole_not_partial(
    hass, fast, monkeypatch
):
    # The DTLS night clip: what arrived stops growing well short of 0.9 x the
    # 60 s clip, where the stall wait would hold it and then call it partial.
    # The camera's PLAY_END ends it at once, whole.
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_STALL_S", 60.0)

    class _Ending(_Session):
        polls = 0

        @property
        def playback_ended(self):
            self.polls += 1
            return self.polls > 30

    session = _Ending(_reply(60), range(0, 200, 10))
    coord, _ = _capturing_coordinator(hass, session, warm=False)
    out = await asyncio.wait_for(
        coord.async_capture_sd_recording(REC, "/tmp/x.part.ts"), 5
    )
    assert out == "ok"
    assert session.polls == 31
    assert session.stopped


async def test_without_an_end_mark_the_stall_wait_still_decides(
    hass, fast, monkeypatch
):
    # Firmware that never sends PLAY_END: the fallbacks are unchanged.
    monkeypatch.setattr(sd_mod, "SD_PLAYBACK_STALL_S", 0.2)
    session = _Session(_reply(60), range(0, 200, 10))
    coord, _ = _capturing_coordinator(hass, session, warm=False)
    out = await asyncio.wait_for(
        coord.async_capture_sd_recording(REC, "/tmp/x.part.ts"), 5
    )
    assert out == "partial"
