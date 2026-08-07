"""The resolution select is gone on purpose - keep it gone.

The cameras ignore the command it sent. `SETSTREAMCTRL` (cmd 800) is delivered -
the library sends it over the live session and re-sends it when a session starts
- and the encode never changes. Measured 2026-08-07 by reading `videoWidth` off
live WebRTC tracks, which is the encode itself rather than a scaled snapshot
(an earlier attempt via snapshots was inconclusive for exactly that reason):

    A001064 PTZ    (SDES)  1280x720 under sd, mid-session AND at session start
    A000088 M3 Pro (DTLS)  1280x720 before sd, 1280x720 30s after

Two models across both transports, so it is not one camera's firmware.

The entity accepted a value, restored it across restarts, and reported a setting
the camera had never applied - it lied persistently, which is worse than not
offering the control. `async_set_resolution` remains in the library: the command
is correct and a future firmware may honour it.

Re-adding this entity means having new evidence that a camera actually changes
its encode. This test is here so that has to be a decision rather than an
oversight.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.aidot.select import CAMERA_SELECTS


def test_no_resolution_select_is_offered():
    keys = [d.key for d in CAMERA_SELECTS]
    assert "resolution" not in keys, (
        "the cameras ignore SETSTREAMCTRL - measured on two models across both "
        "transports. Re-adding this needs evidence that an encode actually "
        "changes, not just that the command was sent"
    )


def test_the_remaining_selects_read_back_from_the_device():
    """Every surviving select reflects device state rather than guessing.

    That is the property the resolution select could not satisfy: no cloud
    readback, no observable effect, so nothing to reflect.
    """
    for d in CAMERA_SELECTS:
        assert d.get_current_option is not None, (
            f"select {d.key!r} has no readback - it can only report what it was "
            "told, which is how the resolution control came to lie"
        )
