"""Tests for the AiDot PTZ button capability gating."""

from types import SimpleNamespace

from custom_components.aidot.button import _is_ptz_camera, _ptz_buttons_for


def _coord(model_id="", ptz_directions=None):
    # camera_info is just cast(CameraDeviceInformation, device_client.info), so
    # point both at the same stub object: button.py's _ptz_buttons_for reads
    # coordinator.camera_info, while _is_ptz_camera reads device_client.info.
    info = SimpleNamespace(model_id=model_id, ptz_directions=ptz_directions or [])
    return SimpleNamespace(
        device_client=SimpleNamespace(info=info),
        camera_info=info,
    )


def test_is_ptz_camera_by_model():
    assert _is_ptz_camera(_coord(model_id="IPC.A001064")) is True
    assert _is_ptz_camera(_coord(model_id="IPC.A000088")) is False


def test_is_ptz_camera_by_directions_when_model_missing():
    # model_id is sometimes empty at setup; ptz_directions is authoritative and
    # must still enable the buttons (a past bug suppressed all PTZ buttons).
    assert _is_ptz_camera(_coord(model_id="", ptz_directions=[3, 6])) is True


def test_is_ptz_camera_false_when_no_signal():
    assert _is_ptz_camera(_coord(model_id="", ptz_directions=[])) is False


def test_ptz_buttons_pan_only_subset():
    keys = {d.key for d in _ptz_buttons_for(_coord(ptz_directions=[3, 6]))}
    assert keys == {"ptz_left", "ptz_right", "ptz_stop"}


def test_ptz_buttons_zoom_codes_included_when_advertised():
    keys = {d.key for d in _ptz_buttons_for(_coord(ptz_directions=[3, 6, 23, 24]))}
    assert keys == {"ptz_left", "ptz_right", "ptz_zoom_in", "ptz_zoom_out", "ptz_stop"}


def test_ptz_buttons_unknown_directions_returns_all():
    keys = {d.key for d in _ptz_buttons_for(_coord(ptz_directions=[]))}
    assert "ptz_up" in keys and "ptz_zoom_in" in keys and "ptz_stop" in keys


async def test_reload_button_schedules_a_reload_task():
    # Pressing the hub Reload button reloads the config entry as a background
    # task (it tears the button itself down, so the press must return first).
    from unittest.mock import MagicMock

    from custom_components.aidot.button import AidotReloadButton

    button = AidotReloadButton(MagicMock(entry_id="e1"))
    button.hass = MagicMock()  # async_create_task + async_reload are plain mocks
    await button.async_press()
    button.hass.config_entries.async_reload.assert_called_once_with("e1")
    button.hass.async_create_task.assert_called_once()
