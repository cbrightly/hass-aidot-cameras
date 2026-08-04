"""Playback proxy for AiDot cloud event clips.

The AiDot cloud only ever returns a *type-2 HLS* (``.m3u8``) playback URL for an
event clip (``getEventVideoUrl`` offers no MP4).  Those HLS segments are
**H.265/HEVC video + AAC audio**, which browsers cannot decode via MSE - so
feeding the playlist (or its raw ``.ts`` segments) to a ``<video>`` element just
produces an endless "loading" flicker no remux or MIME tweak can fix.

This view fixes playback by handing the ``.m3u8`` URL to **ffmpeg** and serving
a real ``video/mp4`` the browser can play natively.  Two strategies are used:

* **HEVC passthrough** (Apple WebKit - Safari, iOS/macOS HA app): iOS and
  macOS support HEVC natively, so we remux the raw HEVC stream into an MP4
  container with ``-c:v copy`` - ~2 s instead of ~20 s for a cold clip, with
  ``-c:a copy`` to avoid re-encoding the (already-AAC) audio.  Prewarm always
  uses this path; ``get()`` uses it for any Apple WebKit ``User-Agent``.
* **H.264 transcode** (Chrome, Firefox, other non-WebKit clients): the HEVC
  video is decoded and re-encoded to H.264 (downscaled to 1280 wide) so the
  clip plays everywhere.

Both strategies write a complete ``+faststart`` MP4 and serve it with
:class:`aiohttp.web.FileResponse`, which supports HTTP ``Range`` (``206``).
WebKit *requires* Range for ``<video>`` and refuses a chunked ``200`` stream, so
streaming-while-transcoding is not viable - hence file-first for both paths.

* **Cache by ``eventUuid`` + codec**: finished files are promoted from a
  ``.part`` temp to the cache so replays are served instantly; entries are
  evicted on a size/age budget.
* **Auth**: the view is unauthenticated (``requires_auth = False``) because the
  media-browser ``<video>`` request carries no HA ``authSig``.  Instead the media
  source signs each playback URL with a per-process HMAC over
  ``device + event + expiry`` (:func:`sign_playback_url`) and the view verifies it
  (:func:`_verify_sig`), so a leaked or guessed ``eventUuid`` can't be replayed
  against this endpoint past the short expiry window.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
import urllib.parse

from aiohttp import web

from homeassistant.helpers.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .coordinator import AidotCameraUpdateCoordinator, get_camera_coordinators

_LOGGER = logging.getLogger(__name__)

PROXY_PATH = "/api/aidot/video"

# Cache budget for transcoded clips (clips are short; this holds plenty).
_CACHE_DIRNAME = "aidot_clips"
_CACHE_MAX_BYTES = 512 * 1024 * 1024      # 512 MiB total
_CACHE_MAX_AGE = 24 * 3600                 # 24 h

# ffmpeg input flags shared by every attempt.  The protocol whitelist is the
# minimum HLS-over-HTTPS needs (``crypto`` for AES-128 keyed segments); ``file``
# is deliberately excluded so a malicious playlist can't make ffmpeg read local
# files.  We also only ever feed ffmpeg a URL we resolved server-side from the
# AiDot cloud - never a client-supplied one - so there is no SSRF surface.
_INPUT_ARGS = (
    "-hide_banner",
    "-loglevel", "error",
    "-nostdin",
    # Start emitting output sooner: cap input probing (the default ~5s/5MB
    # analyse delays the first frame) and don't buffer the input.  2s/2MB still
    # reliably detects the HEVC+AAC streams in the first HLS segment.
    "-fflags", "+nobuffer",
    "-analyzeduration", "2000000",
    "-probesize", "2000000",
    "-protocol_whitelist", "https,tls,tcp,crypto",
)
# Output args shared by every plan: write a complete +faststart MP4 so
# FileResponse can serve it with Range (required by WebKit for <video>).
_OUTPUT_ARGS = (
    "-movflags", "+faststart",
    "-f", "mp4",
)

# HEVC passthrough plan: remux the raw HEVC stream into an MP4 container without
# decode/encode.  Safari and the iOS HA app support HEVC natively, so this is
# the fast path for WebKit clients (~2 s vs ~20 s for a full transcode on Pi).
# -tag:v hvc1 is required: Safari/iOS refuse the hev1 fourcc that ffmpeg writes
# by default for stream-copy; hvc1 carries in-band parameter sets and plays fine.
_HEVC_PASSTHROUGH_PLAN: tuple[str, list[str], list[str]] = (
    "hevc-passthrough", [], ["-c:v", "copy", "-tag:v", "hvc1"],
)

_TRANSCODE_TIMEOUT = 300            # hard ceiling for one ffmpeg transcode
# HEVC passthrough is ~2 s per clip on Pi; pre-warm more clips so tapping any
# of the most recent ones plays instantly.
_PREWARM_COUNT = 5                  # recent clips to warm ahead on browse

# The registered view (singleton) - lets the media source pre-warm the cache.
_instance: "AidotVideoProxyView | None" = None

# Hardware-encoder detection is memoised after the first probe.
_hw_plan: tuple[str, list[str], list[str]] | list[str] | None = None
_HW_LOCK = asyncio.Lock()

# One transcode at a time per event so concurrent plays share a cache entry
# instead of racing two ffmpeg runs onto the same file.
_EVENT_LOCKS: dict[str, asyncio.Lock] = {}
_EVENT_LOCK_REFS: dict[str, int] = {}


def _acquire_event_lock(key: str) -> asyncio.Lock:
    """Get-or-create the per-event lock and bump its refcount.

    Refcounted (not locked()-based) eviction: the increment happens
    synchronously at call time, before any await, so a waiter already holds a
    ref and the lock is never popped out from under it (which would let two
    transcodes run for the same event).
    """
    lock = _EVENT_LOCKS.get(key)
    if lock is None:
        lock = _EVENT_LOCKS[key] = asyncio.Lock()
    _EVENT_LOCK_REFS[key] = _EVENT_LOCK_REFS.get(key, 0) + 1
    return lock


def _release_event_lock(key: str) -> None:
    """Drop one ref; remove the lock only when the last holder/waiter leaves."""
    remaining = _EVENT_LOCK_REFS.get(key, 0) - 1
    if remaining <= 0:
        _EVENT_LOCK_REFS.pop(key, None)
        _EVENT_LOCKS.pop(key, None)
    else:
        _EVENT_LOCK_REFS[key] = remaining

# Cap concurrent cold transcodes so several clips opened at once can't pin the
# CPU (each cold clip runs an ffmpeg).  Generous default - rarely blocks; caps
# runaway.  Override with AIDOT_MAX_TRANSCODES.
_TRANSCODE_SEM = asyncio.Semaphore(int(os.environ.get("AIDOT_MAX_TRANSCODES", "3")))


# --------------------------------------------------------------------------- #
# Signed-URL auth for the unauthenticated playback view
# --------------------------------------------------------------------------- #
# The <video> element fetches PROXY_PATH without HA's authSig, so the view can't
# use requires_auth. Instead the media source signs device+event+expiry with this
# per-process secret and the view verifies it. The secret is regenerated each HA
# start (so an already-issued URL stops working across a restart - harmless, the
# media browser just re-resolves) and is never persisted.
_URL_SECRET = secrets.token_bytes(32)
# Validity window for a signed playback URL. The media source mints it at browse
# time; the user taps a clip moments later, so a few hours is ample while still
# bounding indefinite replay of a leaked URL.
_URL_TTL = 6 * 3600


def _compute_sig(device_id: str, event: str, exp: int) -> str:
    """HMAC-SHA256 over device+event+expiry, hex-encoded."""
    msg = f"{device_id}\n{event}\n{exp}".encode()
    return hmac.new(_URL_SECRET, msg, hashlib.sha256).hexdigest()


def sign_playback_url(device_id: str, event: str, *, now: float | None = None) -> str:
    """Return a signed, time-limited proxy URL for an event clip.

    Called by the media source when resolving a clip to a playback URL. Binds the
    signature to ``device`` + ``event`` + ``exp`` so a token can't be replayed for
    a different clip or past its expiry.
    """
    exp = int((time.time() if now is None else now) + _URL_TTL)
    sig = _compute_sig(device_id, event, exp)
    q = urllib.parse.urlencode(
        {"device": device_id, "event": event, "exp": exp, "sig": sig}
    )
    return f"{PROXY_PATH}?{q}"


def _verify_sig(
    device_id: str, event: str, exp_raw: str, sig: str, *, now: float | None = None
) -> bool:
    """Validate a signed playback URL: signature matches and not expired."""
    try:
        exp = int(exp_raw)
    except (TypeError, ValueError):
        return False
    if exp < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_compute_sig(device_id, event, exp), sig or "")


async def async_resolve_event_url(coord: AidotCameraUpdateCoordinator, event_uuid: str) -> str | None:
    """Resolve an event's playback (``.m3u8``) URL, tolerant of library version.

    Newer ``python-aidot-cameras`` exposes ``async_get_event_video_media`` which
    returns ``(url, mime)``; older builds only have ``async_get_event_video_url``
    returning the URL string.  Try the richer one first, fall back to the other -
    so the integration works against whichever ``@main`` is actually deployed.
    """
    dc = coord.device_client
    media_fn = getattr(dc, "async_get_event_video_media", None)
    if media_fn is not None:
        result = await media_fn(event_uuid)
        if result:
            url = result[0] if isinstance(result, (tuple, list)) else result
            if url:
                return url
    url_fn = getattr(dc, "async_get_event_video_url", None)
    if url_fn is not None:
        return await url_fn(event_uuid)
    return None


async def async_prewarm_events(device_id: str, event_uuids: list[str]) -> None:
    """Transcode the most recent clips to the cache ahead of a user play.

    Called by the media source when a camera's event list is browsed, so tapping
    a recent clip plays instantly instead of waiting on a cold transcode.  Runs
    sequentially (one clip at a time) and is fully best-effort.
    """
    view = _instance
    if view is None:
        return
    for event_uuid in event_uuids[:_PREWARM_COUNT]:
        await view.warm(device_id, event_uuid)


def get_ffmpeg_binary(hass: HomeAssistant) -> str:
    """Return the configured ffmpeg binary path, or 'ffmpeg'."""
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except Exception:
        return "ffmpeg"


def _safe_event(event: str) -> str | None:
    """Validate an event id, returning it unchanged, or ``None`` if unsafe.

    The id is kept verbatim (event uuids look like ``v1:<uuid>`` - the colon is
    significant and must survive for the library re-fetch).  Only path-traversal
    and absurd lengths are rejected; the filesystem-safe form is derived
    separately by :func:`_cache_name`.
    """
    if not event or len(event) > 200:
        return None
    if "/" in event or "\\" in event or ".." in event or "\x00" in event:
        return None
    return event


def _cache_name(event: str) -> str:
    """Derive a filesystem-safe cache filename stem from an event id."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in event)


def _short(err: bytes | None) -> str:
    """Decode and truncate ffmpeg stderr for a log line."""
    return (err or b"").decode("utf-8", "replace").strip()[:300]


def _is_webkit(request: web.Request) -> bool:
    """Return True for Apple WebKit clients (iOS/macOS Safari, HA app) that natively decode HEVC."""
    ua = request.headers.get("User-Agent", "")
    if "Chrome" in ua or "AppleWebKit" not in ua:
        return False
    return any(p in ua for p in ("iPhone", "iPad", "iPod", "Macintosh", "Mac OS"))


class AidotVideoProxyView(HomeAssistantView):
    """Transcode an AiDot event clip's HLS stream to a playable MP4."""

    url = PROXY_PATH
    name = "api:aidot:video"
    # The media-browser play dialog fetches this URL in a <video> element without
    # adding HA's authSig, so the view must self-authenticate.  Access is gated by
    # the media source's HMAC signature (device+event+expiry; see _verify_sig); the
    # only thing fed to ffmpeg is a CloudFront URL we resolve server-side (never a
    # client-supplied one) - so no SSRF surface.
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Capture hass at registration (avoids relying on request.app keys)."""
        global _instance
        self.hass = hass
        self._cache_dir = hass.config.path(_CACHE_DIRNAME)
        _instance = self

    async def warm(self, device_id: str, event_uuid: str) -> None:
        """Best-effort: remux a clip to the HEVC cache ahead of a user play.

        Uses HEVC passthrough (fast) so iOS plays instantly.  Skips clips
        already cached or currently being produced.  Shares the per-event lock
        and semaphore with interactive plays, so a concurrent play just waits
        and is then served the cached file.
        """
        event = _safe_event(event_uuid)
        if not event:
            return
        lock_key = f"{event}:hevc"
        cache_path = os.path.join(self._cache_dir, f"{_cache_name(event)}_hevc.mp4")
        if await self.hass.async_add_executor_job(_touch_if_usable, cache_path):
            return
        lock = _acquire_event_lock(lock_key)
        try:
            if lock.locked():
                return  # a play or another warm is already handling it
            async with lock:
                if await self.hass.async_add_executor_job(_is_usable, cache_path):
                    return
                async with _TRANSCODE_SEM:
                    await self._transcode_to_cache(device_id, event, cache_path, hevc=True)
        except Exception as exc:
            _LOGGER.debug("aidot clip prewarm failed for %s: %s", event, exc)
        finally:
            _release_event_lock(lock_key)

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Serve an MP4 for the requested event clip.

        Takes only ``device`` + ``event``; the playback URL is resolved
        server-side from the AiDot cloud - a client-supplied ``url`` is never
        trusted (would be an SSRF/LFI vector on this unauthenticated endpoint).

        WebKit (Safari / iOS HA app): remux HEVC directly - fast, no transcode.
        Other clients (Chrome, Firefox): transcode to H.264 for compatibility.
        """
        device_id = request.query.get("device", "")
        event = _safe_event(request.query.get("event", ""))
        if not event:
            return web.Response(status=400, text="event parameter required")
        # This endpoint is unauthenticated; require the media source's signature
        # (HMAC over device+event+expiry) so a leaked/guessed event id can't be
        # replayed here past the short expiry window.
        if not _verify_sig(
            device_id, event, request.query.get("exp", ""), request.query.get("sig", "")
        ):
            return web.Response(status=403, text="invalid or expired signature")

        webkit = _is_webkit(request)
        stem = _cache_name(event)
        cache_path = os.path.join(
            self._cache_dir,
            f"{stem}_hevc.mp4" if webkit else f"{stem}.mp4",
        )
        lock_key = f"{event}:hevc" if webkit else event

        # Fast path: an earlier play (or prewarm) already produced this clip.
        if await self.hass.async_add_executor_job(_touch_if_usable, cache_path):
            return web.FileResponse(cache_path)

        lock = _acquire_event_lock(lock_key)
        try:
            async with lock:
                # Another request may have produced it while we waited for the lock.
                if await self.hass.async_add_executor_job(_touch_if_usable, cache_path):
                    return web.FileResponse(cache_path)

                async with _TRANSCODE_SEM:
                    ok = await self._transcode_to_cache(
                        device_id, event, cache_path, hevc=webkit
                    )
                if not ok:
                    return web.Response(status=502, text="clip transcode failed")
                return web.FileResponse(cache_path)
        finally:
            # Bound _EVENT_LOCKS growth: drop once nobody else holds or awaits it.
            _release_event_lock(lock_key)

    async def _transcode_to_cache(
        self, device_id: str, event: str, cache_path: str, *, hevc: bool = False
    ) -> bool:
        """Produce a clip MP4 in the cache; return True on success.

        When ``hevc=True``, remux the HEVC stream directly (passthrough) -
        fast for WebKit/iOS clients that support HEVC natively.  Otherwise
        transcode to H.264 for broad browser compatibility.

        Resolves the URL server-side, tries each plan, and on all-fail
        re-resolves a fresh signed URL once (expired-CloudFront case).  Writes
        a complete ``+faststart`` MP4 so FileResponse can serve it with Range.

        We deliberately do NOT abort on client disconnect: WebKit issues a Range
        request, gets nothing during the cold encode, and disconnects/retries -
        aborting would mean it never finishes.  The per-event lock ensures only
        one encode runs; it completes, caches, and the retry is served the file.
        """
        await self.hass.async_add_executor_job(_ensure_dir, self._cache_dir)
        tmp_path = f"{cache_path}.part"
        try:
            url = await self._resolve_url(device_id, event)
            if not url:
                return False
            urls = [url]
            tried_refresh = False
            while True:
                # Recompute plans each iteration so a disabled HW encoder (set to []
                # below) is excluded on the URL-refresh retry pass.
                if hevc:
                    plans = [_HEVC_PASSTHROUGH_PLAN, *await self._encoder_plans()]
                else:
                    plans = await self._encoder_plans()
                for plan_name, input_args, video_args in plans:
                    for candidate in urls:
                        # Always re-encode audio: -c:a copy preserves HLS per-segment
                        # DTS/PTS discontinuities verbatim, producing periodic jitter at
                        # the segment interval (~2-10 s).  Re-encoding establishes a
                        # monotonic clock.  Overhead is sub-second; -c:v copy is untouched.
                        argv = [
                            self._ffmpeg_binary(),
                            *_INPUT_ARGS, *input_args,
                            "-i", candidate, *video_args,
                            "-c:a", "aac", "-ac", "2",
                            *_OUTPUT_ARGS, tmp_path,
                        ]
                        _LOGGER.debug("aidot clip plan=%s: %s", plan_name, " ".join(argv))
                        rc, err = await self._run_ffmpeg(argv)
                        if rc == 0 and await self.hass.async_add_executor_job(
                            _is_usable, tmp_path
                        ):
                            await self.hass.async_add_executor_job(
                                _finalize, tmp_path, cache_path,
                                self._cache_dir, _CACHE_MAX_BYTES, _CACHE_MAX_AGE,
                            )
                            return True
                        await self.hass.async_add_executor_job(_unlink, tmp_path)
                        _LOGGER.debug(
                            "aidot clip plan=%s rc=%s err=%s", plan_name, rc, _short(err)
                        )
                        if plan_name not in ("libx264", "hevc-passthrough"):
                            # A HW encoder that's listed + has a render node but can't
                            # actually init (e.g. VAAPI libva error in this container)
                            # fails every time - stop offering it so we don't waste a
                            # spawn per clip.  libx264 still handles it.
                            global _hw_plan
                            async with _HW_LOCK:
                                _hw_plan = []
                            _LOGGER.info(
                                "aidot clip: disabling HW encoder %s for this session "
                                "(unusable); using libx264", plan_name,
                            )

                if tried_refresh:
                    _LOGGER.warning("aidot clip encode failed for event %s", event)
                    return False
                tried_refresh = True
                fresh = await self._resolve_url(device_id, event)
                if not fresh:
                    return False
                urls = [fresh]
        finally:
            await self.hass.async_add_executor_job(_unlink, tmp_path)

    async def _run_ffmpeg(self, argv: list[str]):
        """Run one ffmpeg transcode to completion; return ``(returncode, stderr)``."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=_TRANSCODE_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.warning("aidot clip: transcode timed out, killing ffmpeg")
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return (-1, b"transcode timed out")
        except asyncio.CancelledError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise
        return (proc.returncode, err)

    async def _resolve_url(self, device_id: str, event: str) -> str | None:
        """Resolve the clip's signed HLS URL server-side from device+event."""
        coord = get_camera_coordinators(self.hass).get(device_id)
        if coord is None:
            _LOGGER.debug("aidot clip: no coordinator for device %s", device_id)
            return None
        try:
            return await async_resolve_event_url(coord, event)
        except Exception as exc:
            _LOGGER.debug("aidot clip url resolve failed: %s", exc)
            return None

    def _ffmpeg_binary(self) -> str:
        return get_ffmpeg_binary(self.hass)

    async def _encoder_plans(self) -> list[tuple[str, list[str], list[str]]]:
        """Ordered (name, input_args, video_args) encoder plans, HW preferred.

        Hardware plans are placed first only when both the encoder and a render
        node are present; libx264 is always appended as the reliable fallback -
        and because this is file-first, a HW failure falls through to it before
        any bytes are served.  ``input_args`` carry options that must precede
        ``-i`` (e.g. VAAPI's ``-vaapi_device``); ``video_args`` carry the output
        codec/filter options.
        """
        software = (
            "libx264",
            [],
            # Downscale 2560x1920 (5MP) -> 1280x960: ~halves the cold transcode and
            # shrinks the file - plenty for a media-browser preview.  -g 30 keeps
            # keyframes ~2s apart for smooth scrubbing.  veryfast balances speed and
            # size (the HEVC decode is the remaining floor; HW accel unavailable).
            ["-vf", "scale=1280:-2", "-c:v", "libx264", "-preset", "veryfast",
             "-g", "30", "-pix_fmt", "yuv420p"],
        )
        hw = await self._detect_hw()
        return ([hw] if hw else []) + [software]

    async def _detect_hw(self) -> tuple[str, list[str], list[str]] | None:
        global _hw_plan
        async with _HW_LOCK:
            if _hw_plan is None:
                _hw_plan = await self.hass.async_add_executor_job(
                    _probe_hw_encoder, self._ffmpeg_binary()
                ) or []
            if not _hw_plan:
                return None
            name, input_args, video_args = _hw_plan
            return (name, list(input_args), list(video_args))


# --------------------------------------------------------------------------- #
# Blocking helpers (run in executor)
# --------------------------------------------------------------------------- #
def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _is_usable(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _touch_if_usable(path: str) -> bool:
    """Return True and refresh mtime if path is usable, for LRU promotion."""
    if not _is_usable(path):
        return False
    try:
        os.utime(path, None)
    except OSError:
        pass
    return True


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _finalize(
    tmp_path: str, cache_path: str, cache_dir: str, max_bytes: int, max_age: float
) -> None:
    os.replace(tmp_path, cache_path)
    _enforce_cache_limits(cache_dir, max_bytes, max_age, keep=cache_path)


def _enforce_cache_limits(
    cache_dir: str, max_bytes: int, max_age: float, keep: str | None = None
) -> None:
    try:
        entries = []
        now = time.time()
        total = 0
        for name in os.listdir(cache_dir):
            if not name.endswith(".mp4"):
                continue
            full = os.path.join(cache_dir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            # Age eviction.  Never evict the path we were just asked to keep (a
            # request validated it and is about to serve it - a TOCTOU unlink).
            if full != keep and now - st.st_mtime > max_age:
                _unlink(full)
                continue
            entries.append((st.st_mtime, st.st_size, full))
            total += st.st_size
        # Size eviction: drop oldest until under budget (skip the kept path).
        for _mtime, size, full in sorted(entries):
            if total <= max_bytes:
                break
            if full == keep:
                continue
            _unlink(full)
            total -= size
    except OSError:
        pass


def _probe_hw_encoder(binary: str) -> tuple[str, list[str], list[str]] | None:
    """Return a (name, input_args, video_args) HW plan, or None.

    Conservative: requires both the encoder listed by ffmpeg *and* a DRI render
    node present, so we never select an encoder that can't open a device.  Video
    is software-decoded (HEVC) then uploaded/encoded on the GPU - the reliable
    path that doesn't depend on a working HEVC hwaccel decoder.
    """
    import subprocess

    if not os.path.exists("/dev/dri/renderD128"):
        return None
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            capture_output=True, timeout=10, check=False,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return None

    if "h264_vaapi" in out:
        return (
            "vaapi",
            ["-vaapi_device", "/dev/dri/renderD128"],
            ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"],
        )
    if "h264_qsv" in out:
        return ("qsv", [], ["-c:v", "h264_qsv", "-pix_fmt", "nv12"])
    return None
