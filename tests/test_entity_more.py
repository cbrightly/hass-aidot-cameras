"""AidotEntity.async_run_command error handling (entity.py 103-112).

A minimal AidotEntity is built directly on a mocked coordinator (no hass
lifecycle); ``async_write_ha_state`` is stubbed and ``name`` bypassed, mirroring
the other entity tests.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.aidot.entity import AidotEntity


def _entity() -> AidotEntity:
    info = SimpleNamespace(
        dev_id="dev1",
        model_id="IPC.A000088",
        mac="aa:bb:cc:dd:ee:ff",
        name="Cam",
        hw_version="1.0",
    )
    dc = MagicMock()
    dc.info = info
    coordinator = MagicMock()
    coordinator.device_client = dc
    coordinator.config_entry = SimpleNamespace(entry_id="e1", options={})
    ent = AidotEntity(coordinator, key="k")
    ent.async_write_ha_state = MagicMock()
    ent.__dict__["name"] = "test"
    return ent


async def test_run_command_reraises_home_assistant_error():
    """A HomeAssistantError from the library command is re-raised verbatim."""
    ent = _entity()

    async def _raise():
        raise HomeAssistantError("original")

    with pytest.raises(HomeAssistantError) as excinfo:
        await ent.async_run_command(_raise(), "do thing")
    # Re-raised as-is (not wrapped into the translated command_failed error).
    assert str(excinfo.value) == "original"
    assert getattr(excinfo.value, "translation_key", None) is None
    ent.async_write_ha_state.assert_not_called()


async def test_run_command_wraps_generic_exception():
    """A generic exception is wrapped into a translated HomeAssistantError."""
    ent = _entity()

    async def _raise():
        raise ValueError("boom")

    with pytest.raises(HomeAssistantError) as excinfo:
        await ent.async_run_command(_raise(), "do thing")
    assert excinfo.value.translation_key == "command_failed"
    assert excinfo.value.translation_placeholders == {"action": "do thing", "error": "boom"}
    ent.async_write_ha_state.assert_not_called()


async def test_run_command_rejects_false_result():
    """A falsy result (device rejected the change) raises command_rejected."""
    ent = _entity()

    async def _false():
        return False

    with pytest.raises(HomeAssistantError) as excinfo:
        await ent.async_run_command(_false(), "do thing")
    assert excinfo.value.translation_key == "command_rejected"
    ent.async_write_ha_state.assert_not_called()


async def test_run_command_success_writes_state():
    """A truthy result writes entity state (the success path)."""
    ent = _entity()

    async def _ok():
        return True

    await ent.async_run_command(_ok(), "do thing")
    ent.async_write_ha_state.assert_called_once()
