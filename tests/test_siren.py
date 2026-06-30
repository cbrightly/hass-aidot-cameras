"""Tests for the Aidot camera siren entity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.siren.const import SirenEntityFeature
from homeassistant.const import EntityCategory

from custom_components.aidot.siren import AidotCameraSiren


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
    dc.async_set_siren = AsyncMock(return_value=True)
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    return coordinator


def _siren(data):
    s = AidotCameraSiren(_coordinator(data))
    s.async_write_ha_state = MagicMock()
    s.__dict__["name"] = "test"  # bypass platform-less name lookup
    return s


def test_supported_features_and_category():
    s = _siren(None)
    assert s.supported_features == (
        SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    )
    assert s.entity_category == EntityCategory.CONFIG


def test_is_on_reads_status_via_getattr():
    assert _siren(SimpleNamespace(siren=True)).is_on is True
    assert _siren(SimpleNamespace(siren=False)).is_on is False


def test_is_on_none_when_field_missing():
    # getattr default None when the status type lacks a siren field.
    assert _siren(SimpleNamespace()).is_on is None


def test_is_on_none_when_no_data():
    assert _siren(None).is_on is None


async def test_turn_on_calls_setter_true():
    s = _siren(SimpleNamespace(siren=False))
    await s.async_turn_on()
    s.device_client.async_set_siren.assert_awaited_once_with(True)
    s.async_write_ha_state.assert_called_once()


async def test_turn_off_calls_setter_false():
    s = _siren(SimpleNamespace(siren=True))
    await s.async_turn_off()
    s.device_client.async_set_siren.assert_awaited_once_with(False)
