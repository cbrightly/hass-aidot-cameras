"""Branch coverage for the per-platform ``async_setup_entry`` closures and the
occupancy sensor's live-motion path.

These target the ``_add_new_*`` closures the per-entity platform tests skip:
we drive ``async_setup_entry(hass, entry, add_entities)`` with a MagicMock
coordinator whose ``camera_coordinators`` is a real dict and assert which
entities get added.  ``entity_registry`` lookups (sensor/siren/button) are
patched to a MagicMock so no live hass is needed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.aidot import (
    binary_sensor,
    button,
    event,
    number,
    select,
    sensor,
    siren,
    switch,
)


# --------------------------------------------------------------------------- #
# Fakes: a camera coordinator + the (hass, entry, add_entities) setup args.
# --------------------------------------------------------------------------- #
def _camera_coord(
    data=None,
    *,
    dev_id="cam1",
    model_id="IPC.A000088",
    ptz_directions=None,
    is_sdes=False,
    is_battery=False,
):
    info = SimpleNamespace(
        dev_id=dev_id,
        model_id=model_id,
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
        ptz_directions=ptz_directions or [],
    )
    dc = MagicMock()
    dc.info = info
    dc.is_sdes_camera = is_sdes
    dc.is_battery_camera = is_battery
    coord = MagicMock()
    coord.device_client = dc
    coord.data = data
    coord.last_update_success = True
    coord.config_entry = SimpleNamespace(entry_id="e1", options={})
    coord.camera_info = info
    return coord


def _setup_args(camera_coords):
    coordinator = MagicMock()
    coordinator.camera_coordinators = camera_coords
    coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.options = {}
    add_entities = MagicMock()
    return MagicMock(), entry, add_entities  # hass, entry, add_entities


def _added(add_entities):
    """Return the list of entities passed to async_add_entities (may be a gen)."""
    return list(add_entities.call_args.args[0])


# --------------------------------------------------------------------------- #
# binary_sensor.async_setup_entry
# --------------------------------------------------------------------------- #
async def test_binary_sensor_setup_adds_occupancy():
    hass, entry, add = _setup_args({"cam1": _camera_coord()})
    await binary_sensor.async_setup_entry(hass, entry, add)
    ents = _added(add)
    assert len(ents) == 1
    assert isinstance(ents[0], binary_sensor.AidotOccupancyBinarySensor)


async def test_binary_sensor_setup_noop_when_no_cameras():
    hass, entry, add = _setup_args({})
    await binary_sensor.async_setup_entry(hass, entry, add)
    add.assert_not_called()


# --------------------------------------------------------------------------- #
# switch.async_setup_entry - SDES cameras also get the audio switch
# --------------------------------------------------------------------------- #
async def test_switch_setup_adds_controls_and_audio_for_sdes():
    hass, entry, add = _setup_args({"cam1": _camera_coord(is_sdes=True)})
    await switch.async_setup_entry(hass, entry, add)
    ents = _added(add)
    # 5 control switches + 1 camera-audio switch.
    assert len(ents) == 6
    assert any(isinstance(e, switch.AidotCameraAudioSwitch) for e in ents)


async def test_switch_setup_no_audio_switch_for_non_sdes():
    hass, entry, add = _setup_args({"cam1": _camera_coord(is_sdes=False)})
    await switch.async_setup_entry(hass, entry, add)
    ents = _added(add)
    assert len(ents) == 5
    assert not any(isinstance(e, switch.AidotCameraAudioSwitch) for e in ents)


# --------------------------------------------------------------------------- #
# number.async_setup_entry
# --------------------------------------------------------------------------- #
async def test_number_setup_adds_all_numbers():
    hass, entry, add = _setup_args({"cam1": _camera_coord()})
    await number.async_setup_entry(hass, entry, add)
    ents = _added(add)
    keys = {e.entity_description.key for e in ents}
    assert keys == {"motion_sensitivity", "speaker_volume"}


# --------------------------------------------------------------------------- #
# select.async_setup_entry - both the cloud and optimistic selects
# --------------------------------------------------------------------------- #
async def test_select_setup_adds_cloud_and_optimistic_selects():
    hass, entry, add = _setup_args({"cam1": _camera_coord()})
    await select.async_setup_entry(hass, entry, add)
    ents = _added(add)
    assert len(ents) == 2
    assert any(isinstance(e, select.AidotResolutionSelect) for e in ents)
    assert any(
        isinstance(e, select.AidotCameraSelect)
        and not isinstance(e, select.AidotResolutionSelect)
        for e in ents
    )


# --------------------------------------------------------------------------- #
# event.async_setup_entry - one motion-event entity per camera
# --------------------------------------------------------------------------- #
async def test_event_setup_adds_one_per_camera():
    hass, entry, add = _setup_args(
        {"cam1": _camera_coord(dev_id="cam1"), "cam2": _camera_coord(dev_id="cam2")}
    )
    await event.async_setup_entry(hass, entry, add)
    ents = _added(add)
    assert len(ents) == 2
    assert all(isinstance(e, event.AidotMotionEvent) for e in ents)


async def test_event_setup_noop_when_no_cameras():
    hass, entry, add = _setup_args({})
    await event.async_setup_entry(hass, entry, add)
    add.assert_not_called()


# --------------------------------------------------------------------------- #
# siren.async_setup_entry - adds siren + drops the pre-migration switch orphan
# --------------------------------------------------------------------------- #
async def test_siren_setup_adds_siren_and_removes_stale_switch():
    hass, entry, add = _setup_args({"cam1": _camera_coord()})
    with patch("custom_components.aidot.entity.er.async_get") as get:
        ent_reg = get.return_value
        ent_reg.async_get_entity_id.return_value = "switch.cam1_siren"
        await siren.async_setup_entry(hass, entry, add)
    ents = _added(add)
    assert len(ents) == 1
    assert isinstance(ents[0], siren.AidotCameraSiren)
    ent_reg.async_remove.assert_called_once_with("switch.cam1_siren")


# --------------------------------------------------------------------------- #
# sensor.async_setup_entry - battery sensor only on battery models
# --------------------------------------------------------------------------- #
async def test_sensor_setup_battery_camera_includes_battery():
    hass, entry, add = _setup_args({"cam1": _camera_coord(is_battery=True)})
    with patch("custom_components.aidot.sensor.er.async_get"):
        await sensor.async_setup_entry(hass, entry, add)
    keys = {e.entity_description.key for e in _added(add)}
    assert keys == {"battery", "sd_card_status", "wifi_rssi"}


async def test_sensor_setup_mains_camera_skips_and_removes_battery():
    hass, entry, add = _setup_args({"cam1": _camera_coord(is_battery=False)})
    with patch("custom_components.aidot.sensor.er.async_get") as get:
        ent_reg = get.return_value
        ent_reg.async_get_entity_id.return_value = "sensor.cam1_battery"
        await sensor.async_setup_entry(hass, entry, add)
    keys = {e.entity_description.key for e in _added(add)}
    assert "battery" not in keys
    assert keys == {"sd_card_status", "wifi_rssi"}
    ent_reg.async_remove.assert_called_once_with("sensor.cam1_battery")


# --------------------------------------------------------------------------- #
# button.async_setup_entry - PTZ gating + stale-direction cleanup
# --------------------------------------------------------------------------- #
async def test_button_setup_adds_supported_and_removes_stale_directions():
    # Pan-only camera: only left/right/stop are valid; up/down/zoom are stale.
    hass, entry, add = _setup_args({"cam1": _camera_coord(ptz_directions=[3, 6])})
    with patch("custom_components.aidot.button.er.async_get") as get:
        ent_reg = get.return_value
        ent_reg.async_get_entity_id.return_value = "button.cam1_stale"
        await button.async_setup_entry(hass, entry, add)
    keys = {e.entity_description.key for e in _added(add)}
    assert keys == {"ptz_left", "ptz_right", "ptz_stop"}
    # The unsupported directions were pruned from the registry.
    assert ent_reg.async_remove.called


async def test_button_setup_skips_non_ptz_camera():
    hass, entry, add = _setup_args(
        {"cam1": _camera_coord(model_id="IPC.A000088", ptz_directions=[])}
    )
    with patch("custom_components.aidot.button.er.async_get"):
        await button.async_setup_entry(hass, entry, add)
    add.assert_not_called()


# --------------------------------------------------------------------------- #
# binary_sensor occupancy: live-motion callback, timer reset, expiry, is_on
# --------------------------------------------------------------------------- #
def _occ(data=None, *, now=1000.0):
    coord = _camera_coord(data)
    occ = binary_sensor.AidotOccupancyBinarySensor(
        coord, binary_sensor.CAMERA_BINARY_SENSORS[0]
    )
    occ.async_write_ha_state = MagicMock()
    occ.__dict__["name"] = "test"  # bypass platform-less name lookup
    occ.hass = SimpleNamespace(loop=SimpleNamespace(time=MagicMock(return_value=now)))
    return occ


def test_on_motion_marks_live_schedules_timer_and_writes():
    occ = _occ(SimpleNamespace(occupancy=None))
    with patch(
        "custom_components.aidot.binary_sensor.async_call_later",
        return_value=MagicMock(),
    ) as acl:
        occ._on_motion({})
    assert occ._last_motion == 1000.0
    assert occ._motion_expiry_unsub is not None
    acl.assert_called_once()
    occ.async_write_ha_state.assert_called_once()
    # A live motion event wins even though the cloud value is None.
    assert occ.is_on is True


def test_on_motion_resets_existing_timer():
    occ = _occ(SimpleNamespace(occupancy=None))
    old_unsub = MagicMock()
    new_unsub = MagicMock()
    with patch(
        "custom_components.aidot.binary_sensor.async_call_later",
        side_effect=[old_unsub, new_unsub],
    ):
        occ._on_motion({})  # schedules old_unsub
        occ._on_motion({})  # cancels old_unsub, schedules new_unsub
    old_unsub.assert_called_once()
    assert occ._motion_expiry_unsub is new_unsub


def test_on_motion_expired_clears_unsub_and_writes():
    occ = _occ(SimpleNamespace(occupancy=False))
    occ._motion_expiry_unsub = MagicMock()
    occ._on_motion_expired()
    assert occ._motion_expiry_unsub is None
    occ.async_write_ha_state.assert_called_once()


def test_is_on_falls_back_to_cloud_after_window_lapses():
    # Last motion is older than the window, so the cloud value is used again.
    occ = _occ(SimpleNamespace(occupancy=True), now=1000.0)
    occ._last_motion = 1000.0 - (binary_sensor.MOTION_OCCUPANCY_WINDOW + 5.0)
    assert occ.is_on is True  # from cloud, not the (lapsed) live window


def test_cancel_motion_timer_is_idempotent():
    occ = _occ()
    unsub = MagicMock()
    occ._motion_expiry_unsub = unsub
    occ._cancel_motion_timer()
    unsub.assert_called_once()
    assert occ._motion_expiry_unsub is None
    occ._cancel_motion_timer()  # second call must not raise


# --------------------------------------------------------------------------- #
# sensor availability follows the coordinator (AidotEntity.available)
# --------------------------------------------------------------------------- #
def _sensor_entity(data, *, last_update_success=True):
    coord = _camera_coord(data)
    coord.last_update_success = last_update_success
    s = sensor.AidotCameraSensor(coord, sensor.CAMERA_SENSORS[0])
    return s


@pytest.mark.parametrize(
    "data, ok, expected",
    [
        (SimpleNamespace(battery_remaining=50, online=True), True, True),
        (SimpleNamespace(battery_remaining=50, online=False), True, False),
        (None, True, False),
        (SimpleNamespace(battery_remaining=50, online=True), False, False),
    ],
)
def test_sensor_available_tracks_coordinator(data, ok, expected):
    assert _sensor_entity(data, last_update_success=ok).available is expected
