"""Tests for the unauthenticated playback view's signature enforcement.

``AidotVideoProxyView.get`` is registered with ``requires_auth = False`` (the
media-browser ``<video>`` element can't carry HA's authSig), so the only thing
guarding this SSRF/auth boundary is the per-process HMAC signature checked by
``_verify_sig``.  These tests drive ``get()`` directly with a stubbed transcoder
(no real ffmpeg, no network) and assert: a VALID signature is accepted, an
EXPIRED ``exp`` is 403, a FORGED/missing ``sig`` is 403, and a missing ``event``
is 400.

``get()`` is exercised against a mocked ``hass`` whose ``async_add_executor_job``
always reports "no cache hit", so control reaches the (stubbed) transcode path.
"""

import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web

from custom_components.aidot.proxy import (
    _URL_TTL,
    AidotVideoProxyView,
    sign_playback_url,
)

NOW = 1_000_000.0
DEVICE = "dev1"
EVENT = "v1:91477a47-e000-4160-a937-e1900c01ee43"


def _make_view() -> AidotVideoProxyView:
    """Build a view backed by a mocked hass (no cache hit, real event locks)."""
    hass = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/aidot_clips")
    view = AidotVideoProxyView(hass)
    # Every executor probe (_touch_if_usable / _is_usable) reports "not cached",
    # so get() always falls through to the (stubbed) transcode path.
    view.hass.async_add_executor_job = AsyncMock(return_value=False)
    return view


def _request(params: dict[str, str], *, ua: str = "") -> SimpleNamespace:
    """A minimal stand-in for aiohttp's web.Request (only .query/.headers used)."""
    return SimpleNamespace(query=dict(params), headers={"User-Agent": ua})


def _params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


async def test_valid_signature_is_accepted_and_transcodes():
    """A correctly-signed, unexpired request proceeds to transcode and serves a file."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=True)
    with patch("custom_components.aidot.proxy.time.time", return_value=NOW):
        params = _params(sign_playback_url(DEVICE, EVENT))
        resp = await view.get(_request(params))
    # On success the view hands back the cached MP4 via FileResponse (status 200).
    assert isinstance(resp, web.FileResponse)
    view._transcode_to_cache.assert_awaited_once()


async def test_transcode_failure_returns_502():
    """A valid signature that fails to transcode surfaces a 502 (not a crash)."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=False)
    with patch("custom_components.aidot.proxy.time.time", return_value=NOW):
        params = _params(sign_playback_url(DEVICE, EVENT))
        resp = await view.get(_request(params))
    assert isinstance(resp, web.Response)
    assert resp.status == 502


async def test_expired_signature_is_rejected_403():
    """A signature valid for its own exp but now past expiry is rejected 403."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=True)
    # Mint the URL in the past so its exp is < NOW (the signature itself is valid;
    # this exercises the expiry branch of _verify_sig, not a bad-sig rejection).
    with patch("custom_components.aidot.proxy.time.time", return_value=NOW):
        params = _params(
            sign_playback_url(DEVICE, EVENT, now=NOW - _URL_TTL - 100)
        )
        resp = await view.get(_request(params))
    assert isinstance(resp, web.Response)
    assert resp.status == 403
    view._transcode_to_cache.assert_not_awaited()


async def test_forged_signature_is_rejected_403():
    """A tampered sig over an otherwise-valid (unexpired) URL is rejected 403."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=True)
    with patch("custom_components.aidot.proxy.time.time", return_value=NOW):
        params = _params(sign_playback_url(DEVICE, EVENT))
        params["sig"] = "deadbeef"  # forged
        resp = await view.get(_request(params))
    assert isinstance(resp, web.Response)
    assert resp.status == 403
    view._transcode_to_cache.assert_not_awaited()


async def test_missing_signature_is_rejected_403():
    """A request with no sig param at all is rejected 403."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=True)
    with patch("custom_components.aidot.proxy.time.time", return_value=NOW):
        params = _params(sign_playback_url(DEVICE, EVENT))
        params.pop("sig", None)
        resp = await view.get(_request(params))
    assert isinstance(resp, web.Response)
    assert resp.status == 403


async def test_missing_event_param_returns_400():
    """No event param short-circuits to 400 before any signature work."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=True)
    resp = await view.get(_request({"device": DEVICE}))
    assert isinstance(resp, web.Response)
    assert resp.status == 400
    view._transcode_to_cache.assert_not_awaited()


async def test_signature_is_bound_to_device_and_event():
    """A signature minted for one device/event can't be replayed for another."""
    view = _make_view()
    view._transcode_to_cache = AsyncMock(return_value=True)
    with patch("custom_components.aidot.proxy.time.time", return_value=NOW):
        params = _params(sign_playback_url(DEVICE, EVENT))
        params["device"] = "other-device"  # swap the device the sig is bound to
        resp = await view.get(_request(params))
    assert isinstance(resp, web.Response)
    assert resp.status == 403
