"""The SDES video offer can be pinned to H.264 from the options flow.

The offer advertises "96 97" (H264 first) but some cameras disregard that: an
A001064 answered H265 on 2 of 11 otherwise identical sessions, and the codec it
chose also changed the frame size (H264 -> 1280x720, H265 -> 2560x1440). A
player that cannot decode the H265 stream shows roughly one frame per keyframe,
which is what MSE playback looks like on iOS when the flip happens.

The library pins through its StreamSettings.sdes_video_pt (AIDOT_SDES_VIDEO_PT);
these tests cover the integration wiring that sets it, and the two properties
that keep the shared SDES path safe: off is said with the library's opt-out
word, and the only pin ever set is 96 -- pinning to 97 is documented in the
library as returning no video at all.
"""

from custom_components.aidot.const import (
    CONF_SDES_PIN_H264,
    DEFAULT_SDES_PIN_H264,
)


from custom_components.aidot.library_settings import stream_settings


def test_default_is_on_so_a_camera_cannot_flip_to_h265():
    assert DEFAULT_SDES_PIN_H264 is True


def test_off_says_so_with_the_librarys_opt_out_word():
    # In-sync audio for recordings pins on its own, so it is off here too.
    s = stream_settings({CONF_SDES_PIN_H264: False, "hls_direct_ts": False})
    # "none" leaves the camera its choice: the library pins 96 when unset.
    assert s.sdes_video_pt == "none"


def test_on_pins_to_96():
    assert stream_settings({CONF_SDES_PIN_H264: True}).sdes_video_pt == "96"


def test_never_pins_to_97():
    # An H265-only offer returned NO video in 3 of 3 measured rounds, so 97 must
    # never be reachable from this switch whatever the option value.
    for value in (True, False, "yes", 1, 0, None):
        s = stream_settings({CONF_SDES_PIN_H264: value})
        assert s.sdes_video_pt in ("none", "96")


def test_option_is_exposed_in_the_options_schema():
    from custom_components.aidot import config_flow

    src = open(config_flow.__file__).read()
    assert "CONF_SDES_PIN_H264" in src
