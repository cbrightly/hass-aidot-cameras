"""Tests for the Aidot PTZ button press action.

test_button.py covers the capability-gating helpers; this covers that pressing a
button awaits async_ptz_move with the right direction (and ptz_stop for stop).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aidot.button import PTZ_BUTTONS, AidotPtzButton

# button key -> direction passed to async_ptz_move ("STOP" => async_ptz_stop)
MOVE_DIRECTIONS = {
    "ptz_up": "up",
    "ptz_down": "down",
    "ptz_left": "left",
    "ptz_right": "right",
    "ptz_zoom_in": "zoom_in",
    "ptz_zoom_out": "zoom_out",
}


def _coordinator():
    info = SimpleNamespace(
        dev_id="cam1",
        model_id="IPC.A001064",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    dc.async_ptz_move = AsyncMock(return_value=True)
    dc.async_ptz_stop = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = SimpleNamespace(online=True)
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _desc(key):
    return next(d for d in PTZ_BUTTONS if d.key == key)


def _button(key):
    btn = AidotPtzButton(_coordinator(), _desc(key))
    btn.async_write_ha_state = MagicMock()
    btn.__dict__["name"] = "test"  # bypass platform-less name lookup
    return btn


@pytest.mark.parametrize("key, direction", list(MOVE_DIRECTIONS.items()))
async def test_press_moves_in_direction(key, direction):
    btn = _button(key)
    await btn.async_press()
    btn.device_client.async_ptz_move.assert_awaited_once_with(direction)
    btn.device_client.async_ptz_stop.assert_not_called()
    btn.async_write_ha_state.assert_called_once()


async def test_press_stop_calls_ptz_stop():
    btn = _button("ptz_stop")
    await btn.async_press()
    btn.device_client.async_ptz_stop.assert_awaited_once_with()
    btn.device_client.async_ptz_move.assert_not_called()
