"""The integration must ask go2rtc who is watching, without registering.

Two independent failure modes meet here, and both are silent:

* Passing ``go2rtc_url`` WITHOUT ``go2rtc_register=False`` makes the library
  register the stream a second time (this integration already registers it via
  ``_publish_to_go2rtc``). That re-points the source mid-stream, and on this
  fleet left the DTLS cameras producing nothing at all.
* Withholding ``go2rtc_url`` altogether removes the only viewer signal SDES
  push mode has - the serve port is go2rtc's shared RTSP port, so the library
  refuses the socket check and answers "unknown", and unknown never releases.
  Battery cameras then renew a keepalive forever against a camera nobody is
  watching.

So the assertion is not "a URL is passed" but the exact pair. Either half alone
is a regression, in opposite directions.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from custom_components.aidot.camera import _GO2RTC_API, AidotCamera


class _Coordinator:
    def __init__(self, options=None):
        self.config_entry = type("E", (), {"options": options or {}})()
        self.sdes_audio_override = None


def _connect_options(options=None) -> dict:
    """Call the real _connect_options with the coordinator it reads."""
    cam = object.__new__(AidotCamera)
    cam.coordinator = _Coordinator(options)
    return AidotCamera._connect_options(cam)


def test_go2rtc_url_is_passed_so_viewer_detection_works():
    """Without this, push-mode cameras can only ever answer 'unknown'."""
    opts = _connect_options()
    assert opts.get("go2rtc_url") == _GO2RTC_API
    assert opts["go2rtc_url"], "an empty URL disables the query just as surely"


def test_registration_is_disabled_so_the_stream_is_not_re_pointed():
    """The integration registers the stream itself; a second PUT breaks DTLS."""
    opts = _connect_options()
    assert opts.get("go2rtc_register") is False


def test_both_halves_travel_together():
    """Guards the failure mode of 'fixing' one and dropping the other.

    Passing the URL with registration left on is worse than passing nothing,
    because it breaks live video rather than just battery life.
    """
    opts = _connect_options()
    assert ("go2rtc_url" in opts) == ("go2rtc_register" in opts)
    if opts.get("go2rtc_url"):
        assert opts.get("go2rtc_register") is False


def test_the_library_actually_accepts_these_kwargs():
    """A silent typo here would be swallowed by **kwargs at the call site."""
    import inspect

    from aidot_cameras.camera.client import CameraMixin

    params = inspect.signature(CameraMixin.start_keepalive).parameters
    for key in _connect_options():
        assert key in params, f"start_keepalive has no parameter {key!r}"


@pytest.mark.parametrize("mode_options", [{}, {"connection_mode": "lan_direct"}])
def test_go2rtc_pair_survives_every_connection_mode(mode_options):
    """The pair is independent of relay/lan_direct; neither may drop it."""
    opts = _connect_options(mode_options)
    assert opts.get("go2rtc_url") == _GO2RTC_API
    assert opts.get("go2rtc_register") is False
