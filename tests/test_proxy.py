"""Tests for the AiDot event-clip transcode proxy helpers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


import urllib.parse

from aiohttp import web

from custom_components.aidot import proxy
from custom_components.aidot.proxy import (
    _URL_TTL,
    AidotVideoProxyView,
    _cache_name,
    _clip_error_response,
    _safe_event,
    _short,
    _verify_sig,
    async_resolve_event_url,
    sign_playback_url,
)


def test_safe_event_keeps_colon_event_ids():
    # Event uuids look like "v1:<uuid>" - the colon is significant and must
    # survive (a past bug rejected it, 400-ing every clip).
    eid = "v1:91477a47-e000-4160-a937-e1900c01ee43"
    assert _safe_event(eid) == eid


def test_safe_event_rejects_traversal_and_empty():
    assert _safe_event("") is None
    assert _safe_event("../secret") is None
    assert _safe_event("a/b") is None
    assert _safe_event("a\\b") is None
    assert _safe_event("x" * 201) is None


def test_cache_name_is_filesystem_safe():
    # Colon (and any non [alnum-_.]) becomes "_" for the on-disk cache filename.
    assert _cache_name("v1:91477a47-abc").startswith("v1_91477a47-abc")
    assert "/" not in _cache_name("a/b:c")
    assert ":" not in _cache_name("v1:abc")


def test_clips_cached_by_an_older_version_are_not_served():
    # Clips cached before they started at 0 would keep their blank seconds.
    assert _cache_name("v1:abc") != "v1_abc"


def test_short_decodes_and_truncates():
    assert _short(b"  boom  ") == "boom"
    assert _short(None) == ""
    assert len(_short(b"x" * 1000)) == 300


async def test_resolve_event_url_prefers_media_then_url():
    # New library: async_get_event_video_media returns (url, mime).
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(
                return_value=("https://cdn/x.m3u8", "application/x-mpegURL")
            ),
            async_get_event_video_url=AsyncMock(
                return_value="https://cdn/should-not-use"
            ),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") == "https://cdn/x.m3u8"


async def test_resolve_event_url_falls_back_to_url_only():
    # The media lookup has nothing; the URL lookup is asked as well.
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(return_value=None),
            async_get_event_video_url=AsyncMock(return_value="https://cdn/y.m3u8"),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") == "https://cdn/y.m3u8"


async def test_resolve_event_url_none_when_unavailable():
    coord = SimpleNamespace(
        device_client=SimpleNamespace(
            async_get_event_video_media=AsyncMock(return_value=None),
            async_get_event_video_url=AsyncMock(return_value=None),
        )
    )
    assert await async_resolve_event_url(coord, "v1:e") is None


# --------------------------------------------------------------------------- #
# Signed playback URLs
# --------------------------------------------------------------------------- #
def _params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_sign_playback_url_roundtrips():
    url = sign_playback_url("dev1", "v1:abc", now=1000.0)
    q = _params(url)
    assert q["device"] == "dev1"
    assert q["event"] == "v1:abc"
    assert int(q["exp"]) == int(1000.0 + _URL_TTL)
    # The signature it produced verifies before expiry...
    # the signed name carries the cloud prefix, so a card URL can never verify here
    assert _verify_sig(
        q["device"], f"cloud:{q['event']}", q["exp"], q["sig"], now=1000.0
    )
    assert not _verify_sig(q["device"], q["event"], q["exp"], q["sig"], now=1000.0)
    # ...and is rejected once expired.
    assert not _verify_sig(
        q["device"], q["event"], q["exp"], q["sig"], now=int(q["exp"]) + 1
    )


def test_verify_sig_rejects_tampered_device_event_and_bad_sig():
    url = sign_playback_url("dev1", "v1:abc", now=1000.0)
    q = _params(url)
    # Swapping the device or event invalidates the signature (it's bound to both).
    assert not _verify_sig("dev2", q["event"], q["exp"], q["sig"], now=1000.0)
    assert not _verify_sig(q["device"], "v1:other", q["exp"], q["sig"], now=1000.0)
    # A forged / empty signature is rejected.
    assert not _verify_sig(q["device"], q["event"], q["exp"], "deadbeef", now=1000.0)
    assert not _verify_sig(q["device"], q["event"], q["exp"], "", now=1000.0)


def test_verify_sig_rejects_nonnumeric_exp():
    url = sign_playback_url("dev1", "v1:abc", now=1000.0)
    q = _params(url)
    assert not _verify_sig(
        q["device"], q["event"], "not-a-number", q["sig"], now=1000.0
    )


# --------------------------------------------------------------------------- #
# "clip not ready yet" vs a genuine transcode failure
#
# A motion push fires the instant motion is detected; the recorded clip takes a
# few seconds to finish uploading to the cloud. A user tapping the push right
# away used to get a bare 502 "clip transcode failed" - when the URL had not
# resolved and no transcode was even attempted. That is a transient, retriable
# condition and must read as one, distinct from ffmpeg actually failing.
# --------------------------------------------------------------------------- #
def _make_view(tmp_path):
    async def _exec(fn, *a):
        return fn(*a)

    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *a: str(tmp_path)),
        async_add_executor_job=_exec,
        data={},
    )
    return AidotVideoProxyView(hass)


class _FakeReq:
    def __init__(self, query, ua="Mozilla/5.0 (X11) Chrome/120"):
        self.query = query
        self.headers = {"User-Agent": ua}


def test_not_ready_maps_to_a_retriable_503():
    r = _clip_error_response("not_ready")
    assert r.status == 503
    assert r.headers.get("Retry-After")
    assert "not ready" in r.text.lower()
    assert "transcode failed" not in r.text.lower()


def test_a_genuine_encode_failure_maps_to_502():
    r = _clip_error_response("failed")
    assert r.status == 502
    assert "clip transcode failed" in r.text


async def test_get_returns_a_retriable_503_when_the_clip_is_not_ready(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="not_ready")
    q = _params(sign_playback_url("dev", "v1:e", now=1000.0))
    resp = await v.get(_FakeReq(q))
    assert resp.status == 503
    assert "not ready" in resp.text.lower()
    assert resp.headers.get("Retry-After")
    assert "transcode failed" not in resp.text.lower()


async def test_get_still_502s_a_genuine_transcode_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="failed")
    q = _params(sign_playback_url("dev", "v1:e", now=1000.0))
    resp = await v.get(_FakeReq(q))
    assert resp.status == 502
    assert "clip transcode failed" in resp.text


async def test_resolve_when_ready_returns_once_the_clip_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "_CLIP_READY_DELAY_S", 0)
    monkeypatch.setattr(proxy, "_CLIP_READY_RETRIES", 5)
    v = _make_view(tmp_path)
    v._resolve_url = AsyncMock(side_effect=[None, None, "https://cdn/x.m3u8"])
    assert await v._resolve_when_ready("dev", "v1:e") == "https://cdn/x.m3u8"
    assert v._resolve_url.await_count == 3


async def test_resolve_when_ready_gives_up_after_the_bounded_retries(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(proxy, "_CLIP_READY_DELAY_S", 0)
    monkeypatch.setattr(proxy, "_CLIP_READY_RETRIES", 3)
    v = _make_view(tmp_path)
    v._resolve_url = AsyncMock(return_value=None)
    assert await v._resolve_when_ready("dev", "v1:e") is None
    assert v._resolve_url.await_count == 4  # 1 initial + 3 retries


async def test_resolve_when_ready_does_not_wait_when_the_clip_is_already_there(
    tmp_path, monkeypatch
):
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(proxy.asyncio, "sleep", _fake_sleep)
    v = _make_view(tmp_path)
    v._resolve_url = AsyncMock(return_value="https://cdn/x.m3u8")
    assert await v._resolve_when_ready("dev", "v1:e") == "https://cdn/x.m3u8"
    assert v._resolve_url.await_count == 1
    assert slept == []


def test_a_clip_keeps_its_opening_seconds_and_starts_at_0(tmp_path):
    # The cloud serves a clip as HLS whose timestamps start a few seconds in
    # (3.16 s on an A001513 clip, 2026-10-06). Read with "-fflags +nobuffer",
    # ffmpeg dropped every packet it had read while probing the input - the
    # whole first segment, here 2 s and two keyframes - so Safari, where a
    # notification tap opens the clip, showed seconds of blank picture and the
    # motion that triggered the notification was gone. Every frame must be
    # kept, and the clip must start at 0.
    import shutil
    import subprocess

    import pytest

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("no ffmpeg")
    from custom_components.aidot.proxy import _INPUT_ARGS, _OUTPUT_ARGS

    hls = tmp_path / "hls"
    hls.mkdir()
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=15:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=6",
            "-c:v",
            "libx264",
            "-bf",
            "0",  # the cameras send no B-frames
            "-g",
            "15",  # a keyframe a second, so the first segment holds two
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-output_ts_offset",
            "3.16",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            str(hls / "index.m3u8"),
        ],
        check=True,
    )
    # The proxy's own input arguments, but local files allowed for the test.
    args = list(_INPUT_ARGS)
    args[args.index("-protocol_whitelist") + 1] = "file"
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            ffmpeg,
            *args,
            "-i",
            str(hls / "index.m3u8"),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ac",
            "2",
            *_OUTPUT_ARGS,
            str(out),
        ],
        check=True,
    )

    def probe(*entries):
        return subprocess.run(
            [ffprobe, "-v", "error", *entries, "-of", "csv=p=0", str(out)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()

    video = probe("-select_streams", "v", "-show_entries", "packet=pts_time")
    audio = probe("-select_streams", "a", "-show_entries", "packet=pts_time")
    assert len(video) == 90, len(video)  # 6 s at 15 fps: none lost
    assert float(video[0]) < 0.1 and float(audio[0]) < 0.1, (video[0], audio[0])
    assert float(probe("-show_entries", "format=start_time")[0]) < 0.1


# --------------------------------------------------------------------------- #
# A play and a prewarm share one producer
# --------------------------------------------------------------------------- #

_WEBKIT_UA = "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"


def _signed(now: float = 1000.0) -> dict[str, str]:
    return _params(sign_playback_url("dev", "v1:e", now=now))


async def test_warm_remuxes_hevc_without_waiting_for_the_cloud(tmp_path):
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="ok")
    await v.warm("dev", "v1:e")
    v._transcode_to_cache.assert_awaited_once_with(
        "dev",
        "v1:e",
        str(tmp_path / f"{_cache_name('v1:e')}_hevc.mp4"),
        hevc=True,
        wait_ready=False,
    )
    assert "v1:e:hevc" not in proxy._EVENT_LOCKS


async def test_warm_steps_aside_when_the_clip_is_already_being_made(tmp_path):
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="ok")
    lock = proxy._EVENT_LOCKS.setdefault("v1:e:hevc", asyncio.Lock())
    try:
        async with lock:
            await v.warm("dev", "v1:e")
        v._transcode_to_cache.assert_not_awaited()
        # The holder evicts the lock when it is done, not the one that stepped aside.
        assert proxy._EVENT_LOCKS.get("v1:e:hevc") is lock
    finally:
        proxy._EVENT_LOCKS.pop("v1:e:hevc", None)


async def test_warm_only_notes_a_failure(tmp_path):
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(side_effect=RuntimeError("boom"))
    await v.warm("dev", "v1:e")  # must not raise
    assert "v1:e:hevc" not in proxy._EVENT_LOCKS


async def test_a_play_waits_for_the_prewarm_and_is_served_its_file(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    v = _make_view(tmp_path)
    cache_path = tmp_path / f"{_cache_name('v1:e')}_hevc.mp4"
    gate = asyncio.Event()

    async def _produce(*args, **kwargs):
        await gate.wait()
        cache_path.write_bytes(b"clip")
        return "ok"

    v._transcode_to_cache = AsyncMock(side_effect=_produce)
    warm = asyncio.create_task(v.warm("dev", "v1:e"))
    await asyncio.sleep(0)
    assert proxy._EVENT_LOCKS["v1:e:hevc"].locked()
    play = asyncio.create_task(v.get(_FakeReq(_signed(), ua=_WEBKIT_UA)))
    await asyncio.sleep(0)
    gate.set()
    await warm
    resp = await play
    assert isinstance(resp, web.FileResponse)
    v._transcode_to_cache.assert_awaited_once()
    assert "v1:e:hevc" not in proxy._EVENT_LOCKS


async def test_a_play_touches_the_cache_on_its_recheck_and_a_prewarm_only_looks(
    tmp_path, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(
        proxy, "_touch_if_usable", lambda p: calls.append("touch") or False
    )
    monkeypatch.setattr(proxy, "_is_usable", lambda p: calls.append("look") or True)
    v = _make_view(tmp_path)
    v._transcode_to_cache = AsyncMock(return_value="ok")
    await v.warm("dev", "v1:e")
    assert calls == ["touch", "look"]
    v._transcode_to_cache.assert_not_awaited()

    calls.clear()
    monkeypatch.setattr(
        proxy, "_touch_if_usable", lambda p: calls.append("touch") or len(calls) == 2
    )
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    resp = await v.get(_FakeReq(_signed()))
    assert calls == ["touch", "touch"]
    assert isinstance(resp, web.FileResponse)
    v._transcode_to_cache.assert_not_awaited()


async def test_a_play_and_a_prewarm_key_the_lock_by_encode(tmp_path, monkeypatch):
    # Chrome plays the H.264 encode, which no prewarm makes: its lock is the
    # event alone, so it never queues behind an HEVC prewarm.
    monkeypatch.setattr(proxy.time, "time", lambda: 1000.0)
    v = _make_view(tmp_path)
    seen: list[list[str]] = []

    async def _produce(*args, **kwargs):
        seen.append(sorted(proxy._EVENT_LOCKS))
        return "failed"

    v._transcode_to_cache = AsyncMock(side_effect=_produce)
    await v.get(_FakeReq(_signed()))
    await v.get(_FakeReq(_signed(), ua=_WEBKIT_UA))
    assert seen == [["v1:e"], ["v1:e:hevc"]]
    assert not proxy._EVENT_LOCKS


# --------------------------------------------------------------------------- #
# The hardware encoder is the one the library's test encode proved
# --------------------------------------------------------------------------- #
from aidot_cameras.camera.hwaccel import (  # noqa: E402
    SOFTWARE_ENCODER,
    EncoderPlan,
)


def _library_encoder(monkeypatch, verdict, *, cached=None):
    """The library's probe answers ``verdict``; its in-memory cache ``cached``."""
    probes = []

    def probe_encoder(codec="h264", force=False):
        probes.append(codec)
        return verdict

    monkeypatch.setattr(proxy.hwaccel, "probe_encoder", probe_encoder)
    monkeypatch.setattr(proxy.hwaccel, "cached_encoder", lambda codec="h264": cached)
    return probes


_QSV = EncoderPlan((), ("-pix_fmt", "yuv420p", "-c:v", "h264_qsv"))
_VAAPI = EncoderPlan(
    ("-vaapi_device", "/dev/dri/renderD128"),
    ("-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"),
)


async def test_a_proven_hw_encoder_comes_before_libx264(tmp_path, monkeypatch):
    probes = _library_encoder(monkeypatch, _QSV)
    view = _make_view(tmp_path)

    plans = await view._encoder_plans()
    again = await view._encoder_plans()

    assert [n for n, _i, _v in plans] == [n for n, _i, _v in again]
    assert [n for n, _i, _v in plans] == ["h264_qsv", "libx264"]
    assert plans[0][1:] == ([], ["-pix_fmt", "yuv420p", "-c:v", "h264_qsv"])
    assert probes == ["h264"], "probed once, then the shared answer"


async def test_a_vaapi_plan_puts_its_device_before_the_input(tmp_path, monkeypatch):
    _library_encoder(monkeypatch, _VAAPI)
    view = _make_view(tmp_path)
    seen = []

    async def _run(argv):
        seen.append(argv)
        return 1, b"no"

    view._run_ffmpeg = _run
    view._resolve_url = AsyncMock(return_value="https://cdn/clip.m3u8")
    await view._transcode_to_cache(
        "dev1", "v1:e", str(tmp_path / "c.mp4"), wait_ready=False
    )
    vaapi = next(a for a in seen if "h264_vaapi" in a)
    i = vaapi.index("-i")
    assert vaapi.index("-vaapi_device") < i < vaapi.index("format=nv12,hwupload")
    soft = next(a for a in seen if "libx264" in a)
    assert "-vaapi_device" not in soft and "scale=1280:-2" in soft


async def test_no_proven_hw_encoder_means_libx264_alone(tmp_path, monkeypatch):
    _library_encoder(monkeypatch, SOFTWARE_ENCODER)
    view = _make_view(tmp_path)
    assert [n for n, _i, _v in await view._encoder_plans()] == ["libx264"]


async def test_a_cached_verdict_needs_no_probe(tmp_path, monkeypatch):
    probes = _library_encoder(
        monkeypatch,
        SOFTWARE_ENCODER,
        cached=EncoderPlan((), ("-pix_fmt", "yuv420p", "-c:v", "h264_nvenc")),
    )
    view = _make_view(tmp_path)
    assert [n for n, _i, _v in await view._encoder_plans()] == [
        "h264_nvenc",
        "libx264",
    ]
    assert probes == []


async def test_the_first_clip_awaits_the_probe_setup_started(tmp_path, monkeypatch):
    probes = _library_encoder(monkeypatch, _QSV)
    view = _make_view(tmp_path)
    probe = proxy.start_hw_encoder_probe(view.hass)
    assert proxy.start_hw_encoder_probe(view.hass) is probe
    assert [n for n, _i, _v in await view._encoder_plans()][0] == "h264_qsv"
    assert probes == ["h264"], "the clip ran no probe of its own"


async def test_a_probe_that_raises_is_forgotten_and_the_clip_goes_software(
    tmp_path, monkeypatch
):
    """An executor refusing the job (a reload) must not leave a failed probe
    cached for the rest of the run: the clip encodes in software and the next
    clip asks again."""
    calls = []

    def probe_encoder(codec="h264", force=False):
        calls.append(codec)
        if len(calls) == 1:
            raise RuntimeError("cannot schedule new futures after shutdown")
        return _QSV

    monkeypatch.setattr(proxy.hwaccel, "probe_encoder", probe_encoder)
    monkeypatch.setattr(proxy.hwaccel, "cached_encoder", lambda codec="h264": None)
    view = _make_view(tmp_path)
    assert [n for n, _i, _v in await view._encoder_plans()] == ["libx264"]
    assert proxy._HW_PROBE not in view.hass.data, "the failed probe was dropped"
    assert [n for n, _i, _v in await view._encoder_plans()][0] == "h264_qsv"
    assert calls == ["h264", "h264"]


def test_a_hardware_plan_runs_on_the_ffmpeg_that_proved_it(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "get_ffmpeg_binary", lambda hass: "/ha/ffmpeg")
    monkeypatch.setattr(proxy, "library_ffmpeg_binary", lambda: "/lib/ffmpeg")
    view = _make_view(tmp_path)
    assert view._binary_for("libx264") == "/ha/ffmpeg"
    assert view._binary_for("hevc-passthrough") == "/ha/ffmpeg"
    assert view._binary_for("h264_vaapi") == "/lib/ffmpeg"
    assert view._binary_for("h264_qsv") == "/lib/ffmpeg"
