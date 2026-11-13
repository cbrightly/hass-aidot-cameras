"""Every entity on a device builds its DeviceInfo the one way.

The bulb and the motion event used to repeat the base entity's construction
(entry id off the coordinator, hub device id off the entry's runtime data),
and the bulb its availability rule. The bulb now takes both from the base;
the motion event is not a coordinator entity and takes only the info.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.aidot.entity import (
    AidotEntity,
    aidot_device_info,
    device_info_for,
)
from custom_components.aidot.event import AidotMotionEvent
from custom_components.aidot.light import AidotLight


def _info(mac: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        dev_id="dev-1",
        model_id="light.A001497",
        name="Stairs Bulb",
        mac=mac,
        hw_version="1.0",
        enable_rgbw=True,
        enable_cct=True,
        cct_min=1800,
        cct_max=6500,
    )


def _data(**kw) -> SimpleNamespace:
    base = dict(
        online=True, on=True, dimming=255, cct=3000, rgbw=None, active_color_mode=None
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _coord(entry=None, *, data=_data(), success: bool = True, mac: str = ""):
    c = SimpleNamespace(
        device_client=SimpleNamespace(info=_info(mac)),
        data=data,
        last_update_success=success,
    )
    if entry is not None:
        c.config_entry = entry
    return c


_ENTRIES = [
    None,
    SimpleNamespace(entry_id="entry-1"),
    SimpleNamespace(entry_id="entry-1", runtime_data=SimpleNamespace()),
    SimpleNamespace(
        entry_id="entry-1", runtime_data=SimpleNamespace(hub_device_id="hub-9")
    ),
]


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("mac", ["", "aa:bb:cc:dd:ee:ff"])
def test_device_info_for_reads_the_entry_and_hub_off_the_coordinator(entry, mac):
    c = _coord(entry, mac=mac)
    hub = getattr(getattr(entry, "runtime_data", None), "hub_device_id", None)
    expected = aidot_device_info(
        c.device_client.info, entry.entry_id if entry else None, hub
    )
    assert device_info_for(c) == expected


@pytest.mark.parametrize("entry", _ENTRIES)
def test_the_bulb_the_event_and_the_base_agree_on_the_device(entry):
    c = _coord(entry)
    assert (
        AidotLight(c).device_info
        == AidotMotionEvent(c).device_info
        == AidotEntity(c).device_info
        == device_info_for(c)
    )
    # The bulb's id is the device's own; the event's carries its key.
    assert AidotLight(c).unique_id == "dev-1"
    assert AidotMotionEvent(c).unique_id == "dev-1_motion"


def test_the_bulb_is_available_only_with_data_from_an_online_device():
    assert AidotLight(_coord(data=_data(online=True))).available is True
    assert AidotLight(_coord(data=_data(online=False))).available is False
    assert AidotLight(_coord(data=None)).available is False
    assert AidotLight(_coord(data=_data(online=True), success=False)).available is False


def test_the_motion_event_keeps_its_own_availability():
    # Not a coordinator entity: it keeps showing the last event it fired
    # whether or not the camera is online right now.
    ev = AidotMotionEvent(_coord(data=_data(online=False)))
    assert not isinstance(ev, CoordinatorEntity)
    assert ev.available is True
