"""Tests for the Aidot camera select entities.

The cloud-backed select (night_vision) maps current_option and sends the chosen
value; the optimistic resolution select holds its value write-only.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.aidot.select import (
    CAMERA_SELECTS,
    AidotCameraSelect,
    AidotResolutionSelect,
)


def _coordinator(data=None):
    info = SimpleNamespace(
        dev_id="dev1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    dc.async_set_night_vision = AsyncMock(return_value=True)
    dc.async_set_resolution = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _desc(key):
    return next(d for d in CAMERA_SELECTS if d.key == key)


# --------------------------------------------------------------------------- #
# night_vision: cloud-backed select
# --------------------------------------------------------------------------- #
def _nv(data):
    sel = AidotCameraSelect(_coordinator(data), _desc("night_vision"))
    sel.async_write_ha_state = MagicMock()
    sel.__dict__["name"] = "test"  # bypass platform-less name lookup
    return sel


def test_night_vision_options():
    assert _desc("night_vision").options == ["auto", "on", "off"]
    sel = _nv(SimpleNamespace(night_vision_mode="auto"))
    assert sel.options == ["auto", "on", "off"]


def test_night_vision_current_option_reads_field():
    assert _nv(SimpleNamespace(night_vision_mode="on")).current_option == "on"


def test_night_vision_current_option_none_when_no_data():
    assert _nv(None).current_option is None


async def test_night_vision_select_sends_value():
    sel = _nv(SimpleNamespace(night_vision_mode="auto"))
    await sel.async_select_option("off")
    sel.device_client.async_set_night_vision.assert_awaited_once_with("off")
    sel.async_write_ha_state.assert_called_once()


# --------------------------------------------------------------------------- #
# resolution: optimistic, write-only select
# --------------------------------------------------------------------------- #
def _res():
    sel = AidotResolutionSelect(_coordinator(None), _desc("resolution"))
    sel.async_write_ha_state = MagicMock()
    sel.__dict__["name"] = "test"  # bypass platform-less name lookup
    return sel


def test_resolution_options():
    assert _desc("resolution").options == ["hd", "sd"]
    assert _desc("resolution").optimistic is True


async def test_resolution_select_holds_value_and_calls_setter():
    sel = _res()
    await sel.async_select_option("sd")
    assert sel.current_option == "sd"
    sel.device_client.async_set_resolution.assert_awaited_once_with("sd")
    sel.async_write_ha_state.assert_called_once()


async def test_resolution_select_swallows_setter_failure():
    sel = _res()
    sel.device_client.async_set_resolution = AsyncMock(side_effect=RuntimeError("not streaming"))
    await sel.async_select_option("hd")  # must not raise
    assert sel.current_option == "hd"
