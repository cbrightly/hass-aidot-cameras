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
    # _is_connected reads the library's private _state. If a future version
    # renames it, that must cost a cheap no-op reconnect per poll and nothing
    # more - it must not pin every light to unknown forever.
    coord = _coordinator()
    coord._handle_status_update(SimpleNamespace(online=True, on=True))
    assert not hasattr(coord.device_client, "_state")
    assert coord.has_lan_state is True
