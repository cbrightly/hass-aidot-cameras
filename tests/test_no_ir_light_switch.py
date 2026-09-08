"""There is no IR-light switch, and that is deliberate.

`nightVisionIRLight` acknowledges the write and keeps its own value. Confirmed
on both A000088 units on 2026-08-14 by read-back over the local control
channel, so it is model behaviour rather than one bad camera - and there is no
model on the reference fleet where a read-back confirms it takes.

It shipped as a switch a user could toggle with no effect and no error, which
is the same defect that kept `SdcardRecord_Enable` out of the switch set. This
test exists because the model field `ir_light` still parses (the camera reports
a value), so the obvious "we read it, why not write it" change would put the
switch straight back.
"""
from custom_components.aidot.switch import CAMERA_SWITCHES


def test_ir_light_is_not_offered_as_a_switch():
    keys = {d.key for d in CAMERA_SWITCHES}
    assert "ir_light" not in keys, (
        "nightVisionIRLight acks and ignores on the A000088; a switch here "
        "does nothing and says nothing. Confirm a read-back on some model "
        "before re-adding it."
    )


def test_the_switches_that_did_confirm_are_still_offered():
    keys = {d.key for d in CAMERA_SWITCHES}
    for confirmed in ("osd_timestamp", "hdr", "auto_light", "voice_prompts",
                      "motion_detection", "microphone", "status_led"):
        assert confirmed in keys, f"{confirmed} was hardware-confirmed; do not drop it"
