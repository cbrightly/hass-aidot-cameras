"""Diagnostics must say whether LAN control attached, and why not if it did not.

Local control is the one camera feature with no observable surface at all: it
attaches in a background task, logs a single INFO line on success and DEBUG on
every failure, and appears in no entity, attribute or diagnostic. On live
hardware there is no way to answer "is local control working for this camera"
from HA, from CI, or from a bug report - which is exactly the question a user
with slow attribute writes needs answered.

The tri-state matters. "not attached" spans several very different causes:
the subnet sweep never found the camera, the camera does not advertise
localCtrFlag, it is battery powered and sleeps through unicast, or the account
is a shared-home member and the device refuses the login (measured: the owning
account gets 200 on all ten devices, a shared member gets 400/4354/4352).
Collapsing those into one false is what makes this untestable.
"""

from custom_components.aidot.diagnostics import _lan_control_state


class _Lan:
    def __init__(self, ip):
        self.ip = ip


class _Client:
    def __init__(self, lan=None):
        self._lan_client = lan


def test_attached_reports_the_address_it_attached_to():
    state = _lan_control_state(_Client(_Lan("192.168.7.42")), attempted=True)
    assert state["attached"] is True
    assert state["ip"] == "192.168.7.42"


def test_attempted_but_not_attached_is_distinguishable_from_never_tried():
    tried = _lan_control_state(_Client(None), attempted=True)
    untried = _lan_control_state(_Client(None), attempted=False)
    assert tried["attached"] is False and tried["attempted"] is True
    assert untried["attached"] is False and untried["attempted"] is False
    assert tried != untried, (
        "a camera the sweep considered and rejected must not look like one it "
        "never reached"
    )


def test_a_client_with_no_lan_support_does_not_raise():
    """Lights and older clients have no _lan_client attribute at all."""
    class _Bare:
        pass

    state = _lan_control_state(_Bare(), attempted=False)
    assert state["attached"] is False


def test_no_address_is_reported_when_nothing_attached():
    assert _lan_control_state(_Client(None), attempted=True)["ip"] is None
