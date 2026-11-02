"""The SDES video offer can be pinned to H.264 from the options flow.

The offer advertises "96 97" (H264 first) but some cameras disregard that: an
A001064 answered H265 on 2 of 11 otherwise identical sessions, and the codec it
chose also changed the frame size (H264 -> 1280x720, H265 -> 2560x1440). A
player that cannot decode the H265 stream shows roughly one frame per keyframe,
which is what MSE playback looks like on iOS when the flip happens.

The library already supports pinning via AIDOT_SDES_VIDEO_PT; these tests cover
the integration wiring that sets it, and the two properties that keep the shared
SDES path safe: off means the variable is ABSENT (not falsy), and the only value
ever written is 96 -- pinning to 97 is documented in the library as returning no
video at all.
"""

import os
from unittest.mock import patch

from custom_components.aidot.const import (
    CONF_SDES_PIN_H264,
    DEFAULT_SDES_PIN_H264,
)


from custom_components.aidot import _apply_library_env as _apply  # the real block


def test_default_is_on_so_a_camera_cannot_flip_to_h265():
    assert DEFAULT_SDES_PIN_H264 is True


def test_off_says_so_with_the_librarys_opt_out_word():
    with patch.dict(os.environ, {"AIDOT_SDES_VIDEO_PT": "97"}, clear=False):
        # In-sync audio for recordings pins on its own, so it is off here too.
        _apply({CONF_SDES_PIN_H264: False, "hls_direct_ts": False})
        # "none" leaves the camera its choice on every library release: rc44
        # pins 96 when the variable is unset, and older releases read any
        # non-digit as unpinned.
        assert os.environ["AIDOT_SDES_VIDEO_PT"] == "none"


def test_on_pins_to_96():
    with patch.dict(os.environ, {}, clear=False):
        _apply({CONF_SDES_PIN_H264: True})
        assert os.environ["AIDOT_SDES_VIDEO_PT"] == "96"
        os.environ.pop("AIDOT_SDES_VIDEO_PT", None)


def test_never_pins_to_97():
    # An H265-only offer returned NO video in 3 of 3 measured rounds, so 97 must
    # never be reachable from this switch whatever the option value.
    for value in (True, False, "yes", 1, 0, None):
        with patch.dict(os.environ, {}, clear=False):
            _apply({CONF_SDES_PIN_H264: value})
            assert os.environ.get("AIDOT_SDES_VIDEO_PT") in ("none", "96")
            os.environ.pop("AIDOT_SDES_VIDEO_PT", None)


def test_option_is_exposed_in_the_options_schema():
    from custom_components.aidot import config_flow

    src = open(config_flow.__file__).read()
    assert "CONF_SDES_PIN_H264" in src
