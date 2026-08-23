"""Auto-tracking belongs only to cameras that can actually pan or tilt.

Measured 2026-08-23 over the local control channel, with the owner account, on
both A000088 units on this fleet: writing `trackingMode=1` is acknowledged with
a `setDevAttrResp` and the camera keeps its own value - read-back returns 0
every time. These cameras have no motor; there is nothing for auto-tracking to
move.

That is the same failure the `ir_light` switch was removed for, and switch.py
already carries the reasoning:

    No ir_light switch. `nightVisionIRLight` acks the write and keeps its own
    value - confirmed on BOTH A000088 units on 2026-08-14 ... It shipped as a
    switch users could toggle with no effect and no error.

`ptz_tracking` shipped exactly that way for every camera, because the switch
platform applies no capability gate at all, while `button.py` has gated PTZ
controls on the advertised direction codes all along. A user toggling
auto-tracking on a fixed camera gets a switch that flips in the UI, reports
back off, and never does anything.

The gate has to keep `button.py`'s fallback: `ptz_directions` is sometimes
empty at setup and arrives later, so a bare truth test on it would strip the
switch from the real PTZ during that window.
"""

from custom_components.aidot.switch import _supports_auto_tracking


class _Info:
    def __init__(self, ptz_directions=None, model_id=""):
        self.ptz_directions = ptz_directions or []
        self.model_id = model_id


class _Client:
    def __init__(self, info):
        self.info = info


class _Coordinator:
    def __init__(self, info):
        self.device_client = _Client(info)


def _coord(**kw):
    return _Coordinator(_Info(**kw))


def test_a_camera_advertising_pan_directions_gets_the_switch():
    assert _supports_auto_tracking(_coord(ptz_directions=[3, 6])) is True


def test_a_fixed_camera_does_not():
    # A000088: no direction codes, no motor. This is the one that acked and
    # ignored every write.
    assert _supports_auto_tracking(_coord(model_id="LK.IPC.A000088")) is False


def test_the_ptz_model_still_gets_it_before_its_directions_arrive():
    # ptz_directions is empty at setup and populated later; gating on it alone
    # would drop the switch from the camera that actually needs it.
    assert _supports_auto_tracking(_coord(model_id="LK.IPC.A001064")) is True


def test_an_unknown_camera_with_no_capabilities_at_all_does_not_get_it():
    # Better a missing switch than one that silently does nothing: the camera
    # reports its direction codes once known, and the entity appears then.
    assert _supports_auto_tracking(_coord()) is False
