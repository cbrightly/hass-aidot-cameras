"""Never hand Home Assistant a DeviceInfo key it does not declare.

2026.9 removed `via_device` and made passing it raise a RuntimeError from
inside `device_registry.async_get_or_create`, which surfaces as "Error adding
entity None" and takes the entity out entirely. Fifty entities across eleven
domains went with it on a real box.

**The test environment cannot catch that by upgrading.** The newest
`homeassistant` on PyPI is 2026.2.3, while Home Assistant OS ships 2026.9.x - so
the box runs a version that cannot be installed here at all, and
`pytest-homeassistant-custom-component` pins the PyPI one. Bumping the pin is
not available as a fix.

What IS available is refusing to emit a key the running Home Assistant does not
declare, whichever version that turns out to be. That check passes on the old
HA the suite runs and would have failed on the new one, which is the property
that matters.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.aidot.entity import _SUPPORTS_VIA_DEVICE_ID, aidot_device_info


class _Info:
    dev_id = "dev1"
    model_id = "LK.IPC.A001513"
    mac = "aa:bb:cc:dd:ee:ff"
    name = "A camera"
    hw_version = "V2.0-Y4"


def _keys_ha_accepts() -> set:
    return set(DeviceInfo.__annotations__)


def test_every_key_we_emit_is_one_this_ha_declares():
    got = aidot_device_info(_Info(), "entry1", "hubdev9")
    unknown = set(got) - _keys_ha_accepts()
    assert not unknown, (
        f"DeviceInfo keys this Home Assistant does not declare: {sorted(unknown)}. "
        "Passing one of these is not a warning - 2026.9 raises from inside "
        "async_get_or_create and the entity is never added."
    )


def test_that_holds_without_a_hub_too():
    for args in (("entry1", None), (None, "hubdev9"), (None, None)):
        got = aidot_device_info(_Info(), *args)
        assert not set(got) - _keys_ha_accepts()


def test_the_hub_link_is_actually_emitted_on_this_ha():
    """The guard above passes trivially if we emit no link at all."""
    got = aidot_device_info(_Info(), "entry1", "hubdev9")
    key = "via_device_id" if _SUPPORTS_VIA_DEVICE_ID else "via_device"
    assert key in got, "the hub link silently disappeared"
