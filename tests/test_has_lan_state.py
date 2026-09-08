"""has_lan_state must track the LAN session, exercised on the real coordinator.

The existing light tests stub the coordinator with a SimpleNamespace and assign
``has_lan_state`` directly, so the real property never runs - which is how it went
unnoticed that the flag latched True and never cleared. On a real coordinator that
assignment would raise, because it is a read-only property.

Why the flag matters: without a LAN session the library's status object holds only
its defaults (on=False, no colour). Publishing those as though the device had
reported them shows a powered-on bulb as "off" and invites a command that cannot
be delivered, since control is LAN-only.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.aidot.coordinator import AidotDeviceUpdateCoordinator


def _coordinator() -> AidotDeviceUpdateCoordinator:
    """A real coordinator with DataUpdateCoordinator.__init__ bypassed."""
    coord = object.__new__(AidotDeviceUpdateCoordinator)
    coord.device_client = SimpleNamespace(status=SimpleNamespace(online=False))
    coord.async_set_updated_data = MagicMock()
    return coord


def test_starts_false_so_defaults_are_never_published_as_device_state():
    assert _coordinator().has_lan_state is False


def test_a_lan_push_sets_it():
    coord = _coordinator()
    coord._handle_status_update(SimpleNamespace(online=True, on=True))
    assert coord.has_lan_state is True


def test_a_lan_drop_clears_it_again():
    # The regression this test exists for: the flag used to latch. The library's
    # reset() sets status.online False and then notifies, so a bulb that loses
    # Wi-Fi correctly goes unavailable - but a latched flag meant that when the
    # 6-hourly device-list refresh carried the cloud's (sticky) online flag back
    # on, the light became available again AND republished its last-known on /
    # brightness / colour as though they were current, with every command
    # failing.
    coord = _coordinator()
    coord._handle_status_update(SimpleNamespace(online=True, on=True))
    assert coord.has_lan_state is True
    coord._handle_status_update(SimpleNamespace(online=False, on=True))
    assert coord.has_lan_state is False


def test_the_cloud_online_carry_cannot_resurrect_it():
    # _sync_light_coordinators writes status.online directly and calls
    # async_set_updated_data itself, deliberately bypassing _handle_status_update.
    # That is what keeps "reachable via the cloud" (available) separate from
    # "reported its state over the LAN" (has real values to show).
    coord = _coordinator()
    coord.device_client.status.online = True          # what the carry does
    assert coord.has_lan_state is False


def test_it_does_not_depend_on_the_private_state_attribute():
    # has_lan_state must stay independent of however the LAN session is tracked,
    # so that a library/upstream rename costs at most a cheap no-op reconnect per
    # poll - never pinning every light to unknown forever.
    coord = _coordinator()
    coord._handle_status_update(SimpleNamespace(online=True, on=True))
    assert not hasattr(coord.device_client, "_state")
    assert coord.has_lan_state is True


# --------------------------------------------------------------------------- #
# _is_connected - the LAN-session check the periodic poll gates a reconnect on
# --------------------------------------------------------------------------- #

def test_is_connected_delegates_to_the_library():
    """It must not re-implement the check by reading a private attribute.

    Upstream python-aidot ships two live shapes of this: a private ``_state``
    enum (0.3.55) and a ``connect_and_login`` property (0.3.56, which deleted the
    enum). Comparing against ``_state`` directly is silently wrong on 0.3.56 -
    the attribute is absent, so the comparison is False forever and every device
    reads as permanently disconnected without anything raising.

    The library owns that difference via device_session_authenticated, so this
    asserts the delegation rather than either shape's spelling: a client the
    library considers authenticated must read as connected, whichever signal the
    installed upstream actually maintains.
    """
    from aidot_cameras.device_client import device_session_authenticated

    from custom_components.aidot.coordinator import _is_connected

    for authed in (True, False):
        client = _client_the_library_reports(authed)
        assert device_session_authenticated(client) is authed, "test fixture wrong"
        assert _is_connected(client) is authed


def test_is_connected_is_false_for_a_client_with_neither_signal():
    """A shape with neither signal must read disconnected, not raise.

    That is the safe direction: it costs a no-op reconnect attempt per poll
    (the library's async_login returns early unless the session is idle) rather
    than taking down the coordinator's update loop with an AttributeError.
    """
    from custom_components.aidot.coordinator import _is_connected

    assert _is_connected(SimpleNamespace()) is False


def _client_the_library_reports(authenticated: bool):
    """A device-client stub the INSTALLED upstream shape reports as (un)authenticated.

    Built against whichever signal is real rather than hardcoding one, so this
    test keeps its meaning if the dependency moves between shapes.
    """
    from aidot_cameras import _upstream

    if _upstream.DEVICE_STATE_IS_UPSTREAMS:
        state = (
            _upstream.DeviceState.AUTHENTICATED
            if authenticated
            else _upstream.DeviceState.IDLE
        )
        return SimpleNamespace(_state=state)
    return SimpleNamespace(connect_and_login=authenticated)
