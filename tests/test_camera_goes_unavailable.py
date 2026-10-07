"""A camera whose cloud refresh keeps failing must eventually go unavailable.

The coordinator swallowed every refresh error and returned the last known
status, so `last_update_success` could never go False. During a cloud outage
each battery / wifi / SD-card sensor and the occupancy binary_sensor kept
serving frozen values and stayed green, indefinitely, while the manifest
declares quality_scale platinum and the quality scale claims
entity-unavailable: done.

Two ways in, and a fix that closes only one leaves the other frozen:
  - the request raised
  - the request returned nothing (falsy device), which used to fall through to
    the same "return the old status" line

A single failure must NOT flap the fleet - these cameras are on a flaky cloud
and one blip is normal - so it takes several consecutive failures before the
entities drop, and any success resets the count.
"""

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.aidot.coordinator import CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE


class _Client:
    device_id = "cam1"

    def __init__(self):
        self.status = object()
        self.updated_with = []

    def update_status_from_device(self, device):
        self.updated_with.append(device)


class _Manager:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def async_get_camera_device(self, device_id):
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        return b


def _coordinator(behaviour):
    from custom_components.aidot.coordinator import AidotCameraUpdateCoordinator

    c = AidotCameraUpdateCoordinator.__new__(AidotCameraUpdateCoordinator)
    c.device_client = _Client()
    c._manager = _Manager(behaviour)
    c._consecutive_failures = 0
    return c


async def _drive(c, times):
    last = None
    for _ in range(times):
        last = await c._async_update_data()
    return last


async def test_one_blip_does_not_take_the_camera_offline():
    c = _coordinator(RuntimeError("cloud hiccup"))
    assert await c._async_update_data() is c.device_client.status


async def test_sustained_failure_finally_raises():
    c = _coordinator(RuntimeError("cloud is down"))
    with pytest.raises(UpdateFailed):
        await _drive(c, CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE)


async def test_an_empty_reply_counts_as_a_failure_too():
    """The falsy-device path used to fall through and freeze silently."""
    c = _coordinator(None)
    with pytest.raises(UpdateFailed):
        await _drive(c, CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE)


async def test_a_success_resets_the_run():
    c = _coordinator(RuntimeError("blip"))
    for _ in range(CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE - 1):
        await c._async_update_data()
    c._manager.behaviour = {"id": "cam1"}
    await c._async_update_data()
    assert c._consecutive_failures == 0
    c._manager.behaviour = RuntimeError("blip again")
    assert await c._async_update_data() is c.device_client.status


async def test_a_good_reply_still_updates_status():
    c = _coordinator({"id": "cam1"})
    await c._async_update_data()
    assert c.device_client.updated_with == [{"id": "cam1"}]
