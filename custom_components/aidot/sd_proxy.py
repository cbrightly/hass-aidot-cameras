"""Play a recording off a camera's own SD card in the browser.

The cloud clip proxy's model (proxy.py), with the camera as the source: the
media source hands the player a signed URL at once, and the work happens when
the ``<video>`` fetches it. The recording is played off the card once into a
raw MPEG-TS in the clip cache (coordinator.async_capture_sd_recording, which
holds the camera's one media slot for the clip's length), and every client is
then served a +faststart MP4 made from that file - so the browser seeks and
pauses a finished file, and a replay costs no camera session.

What the MP4 carries, by recording and client:

* H264 recording (an A000088 whose encoder is set to H264): video copied, for
  everyone - every browser plays it.
* HEVC recording, WebKit (Safari, the iOS/macOS app): video copied as hvc1.
* HEVC recording, anything else: transcoded to H.264 with the proxy's plans.
* Audio: an SDES recording carries the camera's G.711 as an unlabelled data
  stream (MPEG-TS has no type for it), so it is pulled out and decoded as
  A-law; a DTLS recording already carries AAC. Either way it leaves as AAC.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import urllib.parse

from aiohttp import web
from homeassistant.core import HomeAssistant

from .coordinator import get_camera_coordinators
from homeassistant.components.http import HomeAssistantView

from .proxy import (
    _CACHE_DIRNAME,
    _CACHE_MAX_AGE,
    _CACHE_MAX_BYTES,
    _EVENT_LOCKS,
    _OUTPUT_ARGS,
    _TRANSCODE_SEM,
    _URL_TTL,
    SD_CACHE_PREFIX,
    AidotVideoProxyView,
    _clip_response,
    _compute_sig,
    _ensure_dir,
    _finalize,
    _is_usable,
    _is_webkit,
    _short,
    _touch_if_usable,
    _unlink,
    _verify_sig,
)
from .sd_recordings import (
    SD_KEY_PREFIX,
    find_sd_record,
    sd_battery_too_low,
    sd_playable_model,
)

_LOGGER = logging.getLogger(__name__)

SD_PROXY_PATH = "/api/aidot/sd"

#: Bumped when what a cached card clip contains changes.
_SD_CACHE_VERSION = "v1"

#: Input flags for our own capture file: a local path we wrote, nothing else.
_LOCAL_INPUT_ARGS = ("-hide_banner", "-loglevel", "error", "-nostdin", "-y")

#: What each outcome of a play tells the client. 503 is worth retrying, 502
#: and 409 are not (until something changes), 404 means refresh the list.
_OUTCOME_RESPONSES = {
    "gone": (404, "this recording is no longer in the card's list - refresh it"),
    "model": (409, "recordings on this camera model cannot be played here yet"),
    "battery": (409, "the camera's battery is too low to wake it for a recording"),
    "offline": (503, "the camera is offline"),
    "unavailable": (503, "the camera cannot play recordings right now"),
    "failed": (503, "the camera did not start the recording; try again"),
    "refused": (502, "the camera refused to play this recording"),
    "cancelled": (409, "the fetch was stopped before it began"),
    "no_media": (502, "the camera sent nothing for this recording"),
    "encode": (502, "the recording could not be converted for this browser"),
}


def _sd_event(key: str) -> str:
    """The signed name of a card recording, kept apart from cloud event ids so
    a signature minted for one view can never be replayed on the other."""
    return f"sd:{key}"


def sign_sd_playback_url(device_id: str, key: str, *, now: float | None = None) -> str:
    """A signed, time-limited URL that plays card recording ``key``."""
    exp = int((time.time() if now is None else now) + _URL_TTL)
    sig = _compute_sig(device_id, _sd_event(key), exp)
    q = urllib.parse.urlencode(
        {"device": device_id, "key": key, "exp": exp, "sig": sig}
    )
    return f"{SD_PROXY_PATH}?{q}"


SD_THUMB_PATH = "/api/aidot/sd_thumb"


def _sd_thumb_event(key: str) -> str:
    """The signed name of a card thumbnail; apart from the playback name so a
    signature for a picture cannot start a playback."""
    return f"sdthumb:{key}"


def sign_sd_thumbnail_url(device_id: str, key: str, *, now: float | None = None) -> str:
    """A signed, time-limited URL for the thumbnail of card recording ``key``."""
    exp = int((time.time() if now is None else now) + _URL_TTL)
    sig = _compute_sig(device_id, _sd_thumb_event(key), exp)
    q = urllib.parse.urlencode(
        {"device": device_id, "key": key, "exp": exp, "sig": sig}
    )
    return f"{SD_THUMB_PATH}?{q}"


def _safe_key(key: str) -> str | None:
    """``key`` if it has the shape sd_record_key makes, else None."""
    if not key.startswith(SD_KEY_PREFIX) or len(key) > 40:
        return None
    if not all(c.isalnum() or c in "-" for c in key):
        return None
    return key


def _safe_device(device_id: str) -> str | None:
    return (
        device_id
        if device_id and device_id.isalnum() and len(device_id) <= 64
        else None
    )


def play_refusal(coord) -> str | None:
    """Why this camera's recordings cannot be played now, before any work; None
    when they can. Shared by the media source (which says so at the tap) and
    the view (which a URL can reach directly)."""
    dc = coord.device_client
    if not sd_playable_model(getattr(getattr(dc, "info", None), "model_id", None)):
        return "model"
    status = getattr(dc, "status", None)
    if not getattr(status, "online", True):
        return "offline"
    if sd_battery_too_low(
        getattr(dc, "is_battery_camera", False),
        getattr(status, "battery_remaining", None),
    ):
        return "battery"
    return None


def describe_refusal(outcome: str) -> str:
    """The sentence a user is shown for ``outcome``."""
    return _OUTCOME_RESPONSES.get(outcome, (502, "the recording could not be played"))[
        1
    ]


def _streams(path: str) -> tuple[str | None, str | None]:
    """The capture's video codec and what carries its audio: ``"aac"`` (an
    AAC stream, which the DTLS recorder writes), ``"audio"`` (another
    decodable audio stream), ``"g711"`` (the SDES data stream), or None."""
    import av

    video = audio = None
    with av.open(path) as container:
        for stream in container.streams:
            if stream.type == "video" and video is None:
                video = stream.codec_context.name
            elif stream.type == "audio" and audio is None:
                audio = "aac" if stream.codec_context.name == "aac" else "audio"
            elif stream.type == "data" and audio is None:
                audio = "g711"
    return video, audio


#: A-law's zero sample, which fills the holes in a recording's audio.
_ALAW_SILENCE = 0xD5


def place_alaw(packets, t0: float | None) -> bytes:
    """A-law samples from ``(seconds, bytes)`` packets, each at its own time
    from ``t0`` (8000 a second) or straight after the one before if that is
    later, silence in the holes; sound from before ``t0`` is dropped. ``t0``
    None starts at the first packet."""
    buf = bytearray()
    for at, payload in packets:
        if t0 is None:
            t0 = at
        pos = round((at - t0) * 8000)
        if pos < 0:  # sound from before the first frame has no picture
            payload, pos = payload[-pos:], 0
        if pos > len(buf):
            buf.extend(bytes([_ALAW_SILENCE]) * (pos - len(buf)))
        # A packet stamped before the previous one ends (the first two of an
        # A001064 capture: 0.256 s apart, 0.32 s long) goes on after it rather
        # than over it; no sound is lost, and the next hole resyncs.
        buf.extend(payload)
    return bytes(buf)


def g711_aligned(ts_path: str, alaw_path: str) -> bool:
    """Rebuild an SDES capture's G.711 as raw A-law on the video's clock.

    The data stream it rides in carries timestamps but no codec, so ffmpeg can
    only copy its bytes - which drops the recording's own audio holes (four of
    ~0.4 s near an A001064 clip's start, 2026-10-27) and left the sound 1.2 s
    ahead of the picture by the end of a 25 s capture. Placed by timestamp,
    with silence in the holes, it stays in step (25.60 s of audio for 25.45 s
    of video on the same capture). The clock starts at the file's own start
    time, the earliest of its streams: that is the zero ffmpeg moves the
    video to, so a separate A-law input starting at 0 lines up with it. False
    when there is no audio to rebuild.
    """
    import av

    with av.open(ts_path) as container:
        video = next((s for s in container.streams if s.type == "video"), None)
        data = next((s for s in container.streams if s.type == "data"), None)
        if data is None:
            return False
        t0 = (
            container.start_time / 1_000_000
            if container.start_time is not None
            else (
                float(video.start_time * video.time_base)
                if video is not None and video.start_time is not None
                else None
            )
        )
        packets = [
            (float(packet.pts * packet.time_base), bytes(packet))
            for packet in container.demux(data)
            if packet.size and packet.pts is not None
        ]
    samples = place_alaw(packets, t0)
    if not samples:
        return False
    with open(alaw_path, "wb") as fh:
        fh.write(samples)
    return True


def mp4_argv(
    binary: str,
    ts_path: str,
    out_path: str,
    *,
    video_codec: str | None,
    audio: str | None,
    alaw_path: str | None,
    webkit: bool,
    transcode_args: list[str] | None,
    transcode_input: list[str] | None = None,
) -> list[str]:
    """The ffmpeg command that turns a capture into a playable MP4."""
    argv = [binary, *_LOCAL_INPUT_ARGS, *(transcode_input or []), "-i", ts_path]
    if audio == "g711" and alaw_path:
        argv += ["-f", "alaw", "-ar", "8000", "-ac", "1", "-i", alaw_path]
    argv += ["-map", "0:v:0"]
    if audio in ("aac", "audio"):
        argv += ["-map", "0:a:0"]
    elif audio == "g711" and alaw_path:
        argv += ["-map", "1:a:0"]
    if video_codec == "hevc" and not webkit and transcode_args is not None:
        argv += transcode_args
    elif video_codec == "hevc":
        argv += ["-c:v", "copy", "-tag:v", "hvc1"]
    else:
        argv += ["-c:v", "copy"]
    if audio == "aac":
        # Already AAC: copied, not decoded and encoded again on a small box.
        argv += ["-c:a", "copy"]
    elif audio:
        argv += ["-c:a", "aac", "-ac", "2"]
    return [*argv, *_OUTPUT_ARGS, out_path]


#: How long a partial capture (stopped, capped, or a camera that went quiet
#: mid-clip) is served before it is dropped, so the next play fetches the
#: whole recording instead of a week of the fragment.
_PARTIAL_KEEP_S = 600
#: Beside a partial capture: its mtime starts the window above. A restart
#: loses the timer that drops it, not this, so the next play still does.
_PARTIAL_MARK = ".incomplete"


def _mark_partial(raw_path: str) -> None:
    with open(f"{raw_path[: -len('.ts')]}{_PARTIAL_MARK}", "wb"):
        pass


def _drop_expired_partial(raw_path: str, now: float | None = None) -> None:
    """Drop a partial capture whose window is over, MP4s and all."""
    stem = raw_path[: -len(".ts")]
    try:
        marked = os.stat(f"{stem}{_PARTIAL_MARK}").st_mtime
    except OSError:
        return
    if (time.time() if now is None else now) - marked >= _PARTIAL_KEEP_S:
        _unlink_stem(os.path.dirname(raw_path), os.path.basename(stem))


@contextlib.asynccontextmanager
async def _keyed_lock(key: str):
    """One holder per key, the proxy's way: the lock is dropped from the
    shared table once nobody holds or awaits it."""
    lock = _EVENT_LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            yield
    finally:
        # _waiters stays non-empty until a woken waiter resumes, so this never
        # drops a lock someone is about to take (see proxy._produce_clip).
        if not lock.locked() and not getattr(lock, "_waiters", None):
            _EVENT_LOCKS.pop(key, None)


def _unlink_stem(cache_dir: str, stem: str) -> None:
    """Remove every file of one recording (capture, audio, MP4 variants)."""
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for name in names:
        if name.startswith(stem):
            _unlink(os.path.join(cache_dir, name))


def _log_orphaned_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        _LOGGER.warning(
            "card recording fetch failed after the player left: %r",
            exc,
            exc_info=exc,
        )


class AidotSdPlaybackView(AidotVideoProxyView):
    """Serve a card recording as an MP4, playing it off the card the first time."""

    url = SD_PROXY_PATH
    name = "api:aidot:sd"
    # Same reason as the cloud view: the <video> element sends no auth, so the
    # media source's HMAC signature (device + key + expiry) is the gate.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        # Not the parent's: it registers itself as the cloud prewarm target.
        self.hass = hass
        self._cache_dir = hass.config.path(_CACHE_DIRNAME)

    def _authorise(self, request: web.Request):
        """The camera, device id and recording key a signed request names, or
        the response that refuses it. Shared by play and stop, so the two
        cannot come to check different things."""
        device_id = _safe_device(request.query.get("device", ""))
        key = _safe_key(request.query.get("key", ""))
        if not device_id or not key:
            return web.Response(status=400, text="device and key required")
        if not _verify_sig(
            device_id,
            _sd_event(key),
            request.query.get("exp", ""),
            request.query.get("sig", ""),
        ):
            return web.Response(status=403, text="invalid or expired signature")
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            return web.Response(status=404, text="camera not found")
        return coord, device_id, key

    async def get(self, request: web.Request) -> web.StreamResponse:
        authorised = self._authorise(request)
        if isinstance(authorised, web.Response):
            return authorised
        coord, device_id, key = authorised

        webkit = _is_webkit(request)
        stem = f"{SD_CACHE_PREFIX}{device_id}_{key}_{_SD_CACHE_VERSION}"
        raw_path = os.path.join(self._cache_dir, f"{stem}.ts")
        out_path = os.path.join(
            self._cache_dir, f"{stem}_{'webkit' if webkit else 'any'}.mp4"
        )
        # Shielded: a browser that gives up on a long first play must not
        # cancel the capture. It finishes into the cache, and the retry is
        # served the file. The task is held by hass, and a failure after the
        # browser has gone is logged here rather than lost.
        task = self.hass.async_create_background_task(
            self._produce(coord, key, raw_path, out_path, webkit),
            "aidot sd playback fetch",
        )
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_log_orphaned_failure)
            raise
        if outcome != "ok":
            status, text = _OUTCOME_RESPONSES.get(outcome, (502, outcome))
            headers = {"Retry-After": "10"} if status == 503 else None
            return web.Response(status=status, text=text, headers=headers)
        return _clip_response(out_path)

    async def post(self, request: web.Request) -> web.StreamResponse:
        """Stop fetching this recording (the clip panel's Stop button).

        Gated by the same signature as the play URL, which is what the panel
        holds, and scoped to the recording it signs: a fetch of this one is
        stopped (or cancelled while queued); another recording's fetch on the
        same camera is never touched. 204 when stopped, 409 when this
        recording was not being fetched.
        """
        authorised = self._authorise(request)
        if isinstance(authorised, web.Response):
            return authorised
        coord, _device_id, key = authorised
        if not coord.request_sd_playback_stop(key):
            return web.Response(status=409, text="this recording is not being fetched")
        return web.Response(status=204)

    async def _produce(self, coord, key, raw_path, out_path, webkit) -> str:
        await self.hass.async_add_executor_job(_drop_expired_partial, raw_path)
        if await self.hass.async_add_executor_job(_touch_if_usable, out_path):
            return "ok"
        async with _keyed_lock(out_path):
            if await self.hass.async_add_executor_job(_touch_if_usable, out_path):
                return "ok"
            outcome = await self._capture(coord, key, raw_path)
            if outcome not in ("ok", "partial"):
                return outcome
            async with _TRANSCODE_SEM:
                return await self._to_mp4(raw_path, out_path, webkit)

    async def _capture(self, coord, key, raw_path) -> str:
        """See that ``raw_path`` holds the recording, played off the card once;
        ``"ok"``, ``"partial"`` (served for a while, then fetched again) or
        why not."""
        async with _keyed_lock(raw_path):
            if await self.hass.async_add_executor_job(_touch_if_usable, raw_path):
                return "ok"
            record = find_sd_record(getattr(coord, "sd_cache", None), key)
            if record is None:
                return "gone"
            refusal = play_refusal(coord)
            if refusal is not None:
                return refusal
            await self.hass.async_add_executor_job(_ensure_dir, self._cache_dir)
            # .part.ts: still a .ts, which the DTLS recorder requires.
            part = f"{raw_path[: -len('.ts')]}.part.ts"
            try:
                outcome = await coord.async_capture_sd_recording(record, part)
                if outcome not in ("ok", "partial"):
                    return outcome
                if not await self.hass.async_add_executor_job(_is_usable, part):
                    return "no_media"
                await self.hass.async_add_executor_job(
                    _finalize,
                    part,
                    raw_path,
                    self._cache_dir,
                    _CACHE_MAX_BYTES,
                    # The cloud clips' age: the sweep keeps sd_ files for
                    # SD_CLIP_MAX_AGE_S on its own.
                    _CACHE_MAX_AGE,
                )
                # The SDES audio, rebuilt once here under the capture's lock,
                # so the MP4 variants share it instead of racing to write it.
                await self._rebuild_g711(raw_path)
                if outcome == "partial":
                    await self.hass.async_add_executor_job(_mark_partial, raw_path)
                    self.hass.async_create_background_task(
                        self._drop_later(raw_path), "aidot drop partial sd clip"
                    )
                return outcome
            finally:
                await self.hass.async_add_executor_job(_unlink, part)

    async def _rebuild_g711(self, raw_path: str) -> None:
        try:
            _video, audio = await self.hass.async_add_executor_job(_streams, raw_path)
            if audio != "g711":
                return
            tmp = f"{raw_path}.alaw.part"
            if await self.hass.async_add_executor_job(g711_aligned, raw_path, tmp):
                await self.hass.async_add_executor_job(
                    os.replace, tmp, f"{raw_path}.alaw"
                )
            else:
                await self.hass.async_add_executor_job(_unlink, tmp)
        except Exception as exc:
            # The clip still plays, without sound.
            _LOGGER.debug("card recording audio not rebuilt: %s", exc)

    async def _drop_later(self, raw_path: str) -> None:
        await asyncio.sleep(_PARTIAL_KEEP_S)
        stem = os.path.basename(raw_path)[: -len(".ts")]
        await self.hass.async_add_executor_job(_unlink_stem, self._cache_dir, stem)

    async def _to_mp4(self, raw_path: str, out_path: str, webkit: bool) -> str:
        try:
            video, audio = await self.hass.async_add_executor_job(_streams, raw_path)
        except Exception as exc:
            _LOGGER.warning("card recording %s unreadable: %s", raw_path, exc)
            return "no_media"
        if video is None:
            return "no_media"
        alaw = None
        if audio == "g711":
            alaw = f"{raw_path}.alaw"
            if not await self.hass.async_add_executor_job(_is_usable, alaw):
                alaw, audio = None, None
        tmp = f"{out_path}.part"
        try:
            plans: list[tuple[str, list[str] | None, list[str] | None]] = [
                ("copy", None, None)
            ]
            if video == "hevc" and not webkit:
                plans = list(await self._encoder_plans())
            for plan_name, transcode_input, transcode_args in plans:
                argv = mp4_argv(
                    # A hardware plan runs on the ffmpeg that proved it.
                    self._binary_for(plan_name),
                    raw_path,
                    tmp,
                    video_codec=video,
                    audio=audio,
                    alaw_path=alaw,
                    webkit=webkit,
                    transcode_args=transcode_args,
                    transcode_input=transcode_input,
                )
                rc, err = await self._run_ffmpeg(argv)
                if rc == 0 and await self.hass.async_add_executor_job(_is_usable, tmp):
                    await self.hass.async_add_executor_job(
                        _finalize,
                        tmp,
                        out_path,
                        self._cache_dir,
                        _CACHE_MAX_BYTES,
                        _CACHE_MAX_AGE,
                    )
                    return "ok"
                _LOGGER.debug("card recording mp4 failed rc=%s: %s", rc, _short(err))
            return "encode"
        finally:
            await self.hass.async_add_executor_job(_unlink, tmp)


class AidotSdThumbnailView(HomeAssistantView):
    """Serve the picture the camera gave for a card recording, from the cache
    the last listing filled. Signed like playback, with its own name; 404 when
    the listing has no picture for the recording."""

    url = SD_THUMB_PATH
    name = "api:aidot:sd_thumb"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        device_id = _safe_device(request.query.get("device", ""))
        key = _safe_key(request.query.get("key", ""))
        if not device_id or not key:
            return web.Response(status=400, text="device and key required")
        if not _verify_sig(
            device_id,
            _sd_thumb_event(key),
            request.query.get("exp", ""),
            request.query.get("sig", ""),
        ):
            return web.Response(status=403, text="invalid or expired signature")
        coord = get_camera_coordinators(self.hass).get(device_id)
        cache = getattr(coord, "sd_cache", None)
        data = (getattr(cache, "thumbnails", None) or {}).get(key)
        if not data:
            return web.Response(status=404, text="no thumbnail for this recording")
        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )
