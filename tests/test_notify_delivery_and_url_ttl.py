"""Regressions for three defects found reviewing the 2.19.0-2.19.2 band.

Each of these was invisible in normal use, which is why each gets a test rather
than just a fix:

1. The dispatcher called notify with ``blocking=False`` and then set
   ``sent = True``. That call returns as soon as the payload validates; the
   platform's real failure - a stale ``mobile_app`` registration, a revoked
   token, an unreachable push service - happens afterwards in a detached task
   where nothing observes it. So a feature whose entire value is that the
   message arrived reported success for messages that never went anywhere, and
   cleared its own "no target" warning on that basis.

2. Playback URLs carried a 6 h expiry chosen when only the media source minted
   them (browse, then tap moments later). ``build_payload`` now mints the same
   URL at push time, so an overnight motion notification 403s by morning.

3. ``camera.py`` kept reading the ``sdes_push`` option after 2.19.0 removed its
   toggle from the options page, pinning any entry that had turned it off to
   the pull serve - the mode that jams under HA - with no UI left to undo it.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aidot import proxy
from custom_components.aidot.notify_dispatch import AidotMotionNotifier
from custom_components.aidot.proxy import (
    NOTIFY_URL_TTL,
    _URL_TTL,
    _verify_sig,
    sign_playback_url,
)


# --------------------------------------------------------------------------- #
# 1. a send that fails must not be recorded as sent
# --------------------------------------------------------------------------- #
def _notifier(service_side_effect=None):
    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=service_side_effect)
    n = object.__new__(AidotMotionNotifier)
    n.hass = hass
    n._warned_no_target = set()
    n._gate = SimpleNamespace(record=lambda *_a, **_k: None,
                              allows=lambda *_a, **_k: True)
    return n, hass


@pytest.mark.asyncio
async def test_notify_send_is_awaited_not_fire_and_forget():
    """blocking=True, so the platform's failure can actually reach us."""
    n, hass = _notifier()
    await hass.services.async_call("notify", "mobile_app_x", {}, blocking=True)
    kwargs = hass.services.async_call.await_args.kwargs
    assert kwargs.get("blocking") is True, (
        "blocking=False returns before delivery is attempted, so a failure "
        "lands in a detached task where nothing observes it"
    )


def test_dispatcher_source_uses_blocking_send():
    """Guards the fix at the source level: no fire-and-forget notify call.

    Comment lines are skipped deliberately - the fix's own comment explains what
    blocking=False did wrong, and a naive substring scan fails on that prose.
    """
    import pathlib

    from custom_components.aidot import notify_dispatch

    code = [
        ln for ln in pathlib.Path(notify_dispatch.__file__).read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    assert not [ln for ln in code if "blocking=False" in ln], (
        "a non-blocking notify call cannot report delivery failure, and the "
        "code sets sent=True on the strength of it"
    )
    assert [ln for ln in code if "blocking=True" in ln]


# --------------------------------------------------------------------------- #
# 2. a pushed link has to survive until the phone is picked up
# --------------------------------------------------------------------------- #
def test_push_url_outlives_the_browse_url():
    assert NOTIFY_URL_TTL > _URL_TTL
    assert NOTIFY_URL_TTL >= 24 * 3600, (
        "a notification arriving at 23:00 and tapped at 08:00 must still play"
    )


def test_default_ttl_is_unchanged_for_browse_minted_urls():
    now = time.time()
    url = sign_playback_url("dev", "evt", now=now)
    exp = int(url.split("exp=")[1].split("&")[0])
    assert exp == int(now) + _URL_TTL


def test_overnight_push_url_still_verifies_nine_hours_later():
    now = time.time()
    url = sign_playback_url("dev", "evt", now=now, ttl=NOTIFY_URL_TTL)
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    nine_hours_later = now + 9 * 3600
    assert _verify_sig("dev", "evt", q["exp"], q["sig"], now=nine_hours_later)
    # And the browse-minted equivalent is exactly the case that used to 403.
    old = sign_playback_url("dev", "evt", now=now)
    q_old = dict(p.split("=", 1) for p in old.split("?", 1)[1].split("&"))
    assert not _verify_sig("dev", "evt", q_old["exp"], q_old["sig"],
                           now=nine_hours_later)


def test_signature_is_still_bound_to_device_and_event():
    """The longer window must not weaken what the signature covers."""
    now = time.time()
    url = sign_playback_url("dev", "evt", now=now, ttl=NOTIFY_URL_TTL)
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert not _verify_sig("other-dev", "evt", q["exp"], q["sig"], now=now)
    assert not _verify_sig("dev", "other-evt", q["exp"], q["sig"], now=now)


def test_replay_is_still_bounded_by_the_per_process_secret():
    """A long TTL is only safe because no link survives a restart."""
    assert isinstance(proxy._URL_SECRET, bytes) and len(proxy._URL_SECRET) >= 32


# --------------------------------------------------------------------------- #
# 3. an option nobody can see must not still steer behaviour
# --------------------------------------------------------------------------- #
def test_camera_no_longer_reads_the_unsettable_sdes_push_option():
    import pathlib

    from custom_components.aidot import camera

    src = pathlib.Path(camera.__file__).read_text()
    assert "CONF_SDES_PUSH" not in src, (
        "2.19.0 removed the toggle from the options page but kept reading the "
        "key, stranding an entry that had turned it off on the jamming pull "
        "serve with no UI left to change it"
    )


# --------------------------------------------------------------------------- #
# 4. a clip must be played where it is opened, not downloaded
# --------------------------------------------------------------------------- #
def test_clip_is_served_inline_not_as_a_download():
    """The notification's clickAction navigates straight to the MP4 endpoint.

    aiohttp's FileResponse infers video/mp4 from the extension but sends no
    Content-Disposition, and a bare MP4 navigation is exactly the case a WebView
    may treat as a download - so tapping a motion notification could save a file
    instead of playing it. Saying "inline" explicitly is the correct answer for
    content meant for playback.

    Note this is the standards-correct header, not a verified platform fix: the
    Android behaviour it targets was never reproduced here.
    """
    disp = proxy._CLIP_HEADERS["Content-Disposition"]
    assert disp.startswith("inline")


def test_clip_filename_is_a_fixed_literal():
    """Event ids come from the cloud; nothing user-influenced goes in a header."""
    disp = proxy._CLIP_HEADERS["Content-Disposition"]
    assert disp == 'inline; filename="clip.mp4"'
    assert "\r" not in disp and "\n" not in disp


def test_every_cached_clip_response_carries_the_header():
    """All three return paths - fast path, post-lock recheck, post-transcode."""
    import pathlib

    code = [
        ln for ln in pathlib.Path(proxy.__file__).read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    assert not [ln for ln in code if "web.FileResponse(cache_path)" in ln], (
        "a bare FileResponse sends no Content-Disposition"
    )
    assert len([ln for ln in code if "_clip_response(cache_path)" in ln]) == 3


# --------------------------------------------------------------------------- #
# 5. the clip rides in the notification, not only behind a tap
# --------------------------------------------------------------------------- #
def test_payload_carries_the_companion_app_video_key():
    """`video` is the app's first-class clip field (MessagingManager VIDEO_URL).

    It fetches the URL, extracts frames and animates them in the notification,
    so the clip is visible without opening anything.
    """
    from custom_components.aidot.notify_dispatch import build_payload

    out = build_payload(
        dev_id="dev", camera_name="Cam", kind="motion",
        event={"eventUuid": "evt-1"}, title_template="{camera}",
        message_template="{event}", camera_entity_id="camera.cam",
    )
    data = out["data"]
    assert data["video"] == data["clickAction"] == data["url"]
    assert data["video"].startswith("/api/aidot/video?")


def test_video_link_stays_relative():
    """The app resolves a relative url against the server and fetches it
    authenticated; an absolute one would instead be handed to the system."""
    from custom_components.aidot.notify_dispatch import build_payload

    out = build_payload(
        dev_id="dev", camera_name="Cam", kind="motion",
        event={"eventUuid": "evt-1"}, title_template="t",
        message_template="m", camera_entity_id=None,
    )
    assert not out["data"]["video"].startswith(("http://", "https://"))


def test_no_video_key_when_the_event_has_no_clip():
    from custom_components.aidot.notify_dispatch import build_payload

    out = build_payload(
        dev_id="dev", camera_name="Cam", kind="motion", event={},
        title_template="t", message_template="m", camera_entity_id=None,
    )
    assert "video" not in out["data"]
