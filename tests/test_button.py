"""Tests for the AiDot PTZ button capability gating."""

import asyncio
from types import SimpleNamespace

from custom_components.aidot.button import (
    _SD_REFRESH_KEY,
    AidotSdRefreshButton,
    _ptz_buttons_for,
)
from custom_components.aidot.coordinator import supports_ptz as _is_ptz_camera


def _coord(model_id="", ptz_directions=None):
    # camera_info is just cast(CameraDeviceInformation, device_client.info), so
    # point both at the same stub object: button.py's _ptz_buttons_for reads
    # coordinator.camera_info, while supports_ptz reads device_client.info.
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


def test_there_is_no_reload_button_any_more():
    # Home Assistant's own entry reload does the same.
    from custom_components.aidot import button

    assert not hasattr(button, "AidotReloadButton")


async def test_setup_removes_the_old_reload_buttons_registry_row(hass):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot.button import async_setup_entry
    from custom_components.aidot.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    row = reg.async_get_or_create(
        "button", DOMAIN, f"{entry.entry_id}_reload", config_entry=entry
    )
    entry.runtime_data = SimpleNamespace(
        camera_coordinators={}, async_add_listener=lambda _cb: lambda: None
    )
    await async_setup_entry(hass, entry, lambda _new: None)
    assert reg.async_get(row.entity_id) is None


async def test_reboot_asks_the_library_and_says_so_when_refused():
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.aidot.button import AidotRebootButton

    calls = []

    async def _reboot(ok):
        calls.append("reboot")
        return ok

    for ok in (True, False):
        coord = _entity_coord()
        coord.device_client.async_reboot = lambda ok=ok: _reboot(ok)
        button = AidotRebootButton(coord)
        button.async_write_ha_state = lambda: None
        if ok:
            await button.async_press()
        else:
            # The library refuses when the cloud reports the camera offline.
            with pytest.raises(HomeAssistantError):
                await button.async_press()
    assert calls == ["reboot", "reboot"]


def test_the_reboot_button_is_a_restart_button_on_every_camera():
    from homeassistant.components.button import ButtonDeviceClass

    from custom_components.aidot.button import AidotRebootButton

    button = AidotRebootButton(_entity_coord())
    assert button.device_class is ButtonDeviceClass.RESTART
    assert button.translation_key == "reboot"
    assert button.unique_id == "dev1_reboot"


async def test_pressing_it_opens_a_session_to_list():
    calls = []

    async def _list(*, open_session=False):
        calls.append(open_session)
        return True

    spawned = []

    def _spawn(coro, name):
        spawned.append(name)
        return asyncio.ensure_future(coro)

    coord = SimpleNamespace(async_list_sd_recordings=_list, spawn_entry_task=_spawn)
    # Called unbound on a stand-in rather than through a constructed entity:
    # AidotEntity's constructor wants coordinator plumbing this behaviour does
    # not touch, and building it would test Home Assistant instead of the press.
    # The press backgrounds the listing (it can wait 75 s for a session and
    # must not hold the button platform's semaphore), so run what it schedules.
    hass = SimpleNamespace(
        async_create_background_task=lambda coro, name=None: asyncio.ensure_future(coro)
    )
    await AidotSdRefreshButton.async_press(
        SimpleNamespace(coordinator=coord, hass=hass)
    )
    await asyncio.sleep(0)
    # True, not False: this is the one path allowed to wake a camera, and it is
    # worthless if it only reads a session that is not there.
    assert calls == [True]
    assert spawned == ["aidot sd refresh"]


def test_the_refresh_button_is_not_gated_on_ptz():
    # Every camera can be asked what its card holds. A model that does not
    # answer says so in the browser rather than by silently lacking the
    # control, which would read as a missing feature instead of a missing card.
    assert _is_ptz_camera(_coord()) is False


def _entity_coord(dev_id="dev1"):
    """Enough coordinator for AidotEntity's constructor and nothing more."""
    info = SimpleNamespace(
        dev_id=dev_id,
        model_id="IPC.A000088",
        mac="",
        name="Cam",
        hw_version="1.0",
        ptz_directions=[],
    )
    return SimpleNamespace(
        device_client=SimpleNamespace(info=info),
        camera_info=info,
        config_entry=SimpleNamespace(entry_id="entry1"),
    )


def test_the_key_and_the_unique_id_cannot_drift_apart():
    # One constant behind the translation key and the unique-id suffix. Two
    # literals is how a renamed string leaves every existing install with an
    # orphaned entity and an untranslated name.
    #
    # Read off a built entity, not off the class: Home Assistant rewrites a
    # class-body _attr_* into a property, so the class attribute is a property
    # object and comparing it to a string could never hold for any entity.
    assert _SD_REFRESH_KEY == "refresh_sd_recordings"
    button = AidotSdRefreshButton(_entity_coord())
    assert button.translation_key == _SD_REFRESH_KEY
    assert button.unique_id == f"dev1_{_SD_REFRESH_KEY}"
