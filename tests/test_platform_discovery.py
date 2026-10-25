"""How the platforms pick up cameras, now and as they are discovered.

Every platform runs the same late-add loop: make entities for the cameras it
has not seen, now and again on every account refresh, and - for entities whose
data comes from the camera itself - on every per-camera refresh too. The loop
lives in entity.py; these tests pin it directly and then pin what each
platform makes of it, filters included.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.helpers import entity_registry as er

from custom_components.aidot import (
    binary_sensor,
    button,
    camera,
    event,
    light,
    number,
    select,
    sensor,
    siren,
    switch,
)
from custom_components.aidot.entity import (
    async_add_per_camera,
    async_add_when_discovered,
    forget_departed,
    pending_coordinators,
)


class _Coordinator:
    """What a platform reads off a coordinator: device, data and listeners."""

    def __init__(
        self,
        dev_id: str,
        *,
        model_id: str = "A000088.x",
        data=None,
        extras=None,
        ptz_directions=None,
        is_sdes: bool = False,
        is_battery: bool = False,
        info_extra: dict | None = None,
    ) -> None:
        self.device_client = SimpleNamespace(
            info=SimpleNamespace(
                dev_id=dev_id,
                model_id=model_id,
                name=dev_id,
                mac="",
                hw_version="",
                ptz_directions=ptz_directions or [],
                **(info_extra or {}),
            ),
            is_sdes_camera=is_sdes,
            is_battery_camera=is_battery,
        )
        self.camera_info = self.device_client.info
        self.data = data
        self.camera_extras = extras
        self.config_entry = None
        self.listeners: list = []

    def async_add_listener(self, update_callback, context=None):
        self.listeners.append(update_callback)
        return lambda: self.listeners.remove(update_callback)

    def refresh(self) -> None:
        for cb in list(self.listeners):
            cb()


class _Account(_Coordinator):
    def __init__(self) -> None:
        super().__init__("account")
        self.camera_coordinators: dict[str, _Coordinator] = {}
        self.device_coordinators: dict[str, _Coordinator] = {}


class _Entry:
    entry_id = "entry-1"
    options: dict = {}

    def __init__(self, account: _Account) -> None:
        self.runtime_data = account
        self.unloads: list = []

    def async_on_unload(self, fn) -> None:
        self.unloads.append(fn)


def _camera(dev_id: str = "cam-1", **kw) -> _Coordinator:
    return _Coordinator(dev_id, **kw)


def _setup():
    account = _Account()
    entry = _Entry(account)
    added: list = []

    def add_entities(new):
        added.extend(list(new))

    return account, entry, added, add_entities


def _keys(entities) -> list[str]:
    return [e._attr_unique_id for e in entities]


# --------------------------------------------------------------------------- #
# The helpers themselves
# --------------------------------------------------------------------------- #


def test_pending_coordinators_skips_the_registered_and_keeps_order():
    a, b, c = _camera("a"), _camera("b"), _camera("c")
    registered = {"b"}
    out = pending_coordinators({"a": a, "b": b, "c": c}, registered)
    assert list(out.items()) == [("a", a), ("c", c)]
    # Recording them is the caller's decision, once it has made entities.
    assert registered == {"b"}


def test_add_when_discovered_runs_now_and_on_every_account_refresh():
    account, entry, _, _ = _setup()
    calls: list[int] = []
    async_add_when_discovered(entry, lambda: calls.append(1))
    assert calls == [1]
    account.refresh()
    account.refresh()
    assert calls == [1, 1, 1]
    # The listener is unsubscribed with the entry.
    assert len(entry.unloads) == 1
    entry.unloads[0]()
    account.refresh()
    assert calls == [1, 1, 1]


def test_add_when_discovered_hooks_each_camera_once():
    account, entry, _, _ = _setup()
    first = _camera("a")
    account.camera_coordinators["a"] = first
    calls: list[int] = []
    async_add_when_discovered(entry, lambda: calls.append(1), hook_cameras=True)
    assert calls == [1]
    assert len(first.listeners) == 1

    # A per-camera refresh is what makes a camera-reported entity appear.
    first.refresh()
    assert calls == [1, 1]

    # A second account refresh does not hook the same camera twice...
    account.refresh()
    assert len(first.listeners) == 1
    # ...but a camera discovered later is hooked, on the refresh that found it.
    later = _camera("b")
    account.camera_coordinators["b"] = later
    account.refresh()
    assert len(later.listeners) == 1
    later.refresh()
    assert calls == [1, 1, 1, 1, 1]

    # Every subscription is unsubscribed with the entry: the account's and
    # one per camera.
    assert len(entry.unloads) == 3
    for unload in entry.unloads:
        unload()
    assert first.listeners == [] and later.listeners == []
    assert account.listeners == []


def test_add_per_camera_makes_each_camera_its_entities_once():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a")
    made: list[str] = []

    def factory(c):
        made.append(c.device_client.info.dev_id)
        return [SimpleNamespace(_attr_unique_id=f"{c.device_client.info.dev_id}_x")]

    async_add_per_camera(entry, add_entities, factory)
    assert _keys(added) == ["a_x"]
    account.refresh()
    assert made == ["a"]  # not asked again once it has its entities

    account.camera_coordinators["b"] = _camera("b")
    account.refresh()
    assert _keys(added) == ["a_x", "b_x"]
    assert made == ["a", "b"]


def test_add_per_camera_asks_again_for_a_camera_that_made_nothing():
    account, entry, added, add_entities = _setup()
    cam = _camera("a")
    account.camera_coordinators["a"] = cam
    ready = False

    def factory(c):
        return [SimpleNamespace(_attr_unique_id="a_x")] if ready else []

    async_add_per_camera(entry, add_entities, factory)
    assert added == []
    ready = True
    account.refresh()
    assert _keys(added) == ["a_x"]
    account.refresh()
    assert _keys(added) == ["a_x"]


# --------------------------------------------------------------------------- #
# What each platform makes of it
# --------------------------------------------------------------------------- #


async def test_binary_sensor_platform_adds_an_occupancy_sensor_per_camera():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a")
    await binary_sensor.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_occupancy"]
    assert isinstance(added[0], binary_sensor.AidotOccupancyBinarySensor)

    account.camera_coordinators["b"] = _camera("b")
    account.refresh()
    account.refresh()
    assert _keys(added) == ["a_occupancy", "b_occupancy"]


async def test_event_platform_adds_a_motion_event_per_camera():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a")
    await event.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_motion"]
    account.camera_coordinators["b"] = _camera("b")
    account.refresh()
    account.refresh()
    assert _keys(added) == ["a_motion", "b_motion"]
    assert all(isinstance(e, event.AidotMotionEvent) for e in added)


async def test_siren_platform_adds_a_siren_per_camera():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a")
    await siren.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_siren"]
    account.camera_coordinators["b"] = _camera("b")
    account.refresh()
    account.refresh()
    assert _keys(added) == ["a_siren", "b_siren"]


def _bulb(dev_id: str) -> _Coordinator:
    return _Coordinator(
        dev_id,
        model_id="light.A001497",
        data=SimpleNamespace(
            online=True,
            on=True,
            dimming=255,
            cct=3000,
            rgbw=None,
            active_color_mode=None,
        ),
        info_extra={
            "enable_rgbw": False,
            "enable_cct": True,
            "cct_min": 1800,
            "cct_max": 6500,
        },
    )


async def test_light_platform_adds_bulbs_and_camera_floodlights_separately():
    account, entry, added, add_entities = _setup()
    account.device_coordinators["bulb"] = _bulb("bulb")
    account.camera_coordinators["cam"] = _camera("cam")
    await light.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["bulb", "cam_floodlight"]
    assert isinstance(added[0], light.AidotLight)
    assert isinstance(added[1], light.AidotCameraFloodlight)

    # Each kind has its own memo: a new camera gets a floodlight and nothing else.
    account.camera_coordinators["cam2"] = _camera("cam2")
    account.refresh()
    account.refresh()
    assert _keys(added) == ["bulb", "cam_floodlight", "cam2_floodlight"]


async def test_camera_platform_forgets_a_camera_that_left_so_its_return_is_added():
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera("a")
    made: list[str] = []

    def fake_camera(c):
        made.append(c.device_client.info.dev_id)
        return SimpleNamespace(_attr_unique_id=c.device_client.info.dev_id)

    platform = MagicMock()
    with (
        patch.object(camera, "AidotCamera", fake_camera),
        patch.object(
            camera.entity_platform, "async_get_current_platform", lambda: platform
        ),
    ):
        await camera.async_setup_entry(None, entry, add_entities)
        assert _keys(added) == ["a"]
        account.refresh()
        assert made == ["a"]
        # The camera leaves the account, then comes back with a fresh coordinator.
        del account.camera_coordinators["a"]
        account.refresh()
        account.camera_coordinators["a"] = _camera("a")
        account.refresh()
        assert made == ["a", "a"]
    assert platform.async_register_entity_service.call_count == 2


async def test_number_platform_gates_the_brightness_on_the_camera_reporting_it():
    account, entry, added, add_entities = _setup()
    cam = _camera("a", data=SimpleNamespace(motion_sensitivity=3, speaker_volume=50))
    account.camera_coordinators["a"] = cam
    await number.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_motion_sensitivity", "a_speaker_volume"]

    # The value lands on the camera's own coordinator, and that is what adds it.
    cam.data.light_brightness = 40
    cam.refresh()
    assert _keys(added) == [
        "a_motion_sensitivity",
        "a_speaker_volume",
        "a_light_brightness",
    ]
    cam.refresh()
    account.refresh()
    assert len(added) == 3


async def test_select_platform_gates_the_light_selects_on_the_camera_reporting_them():
    account, entry, added, add_entities = _setup()
    cam = _camera("a", data=SimpleNamespace(night_vision_mode="auto"))
    account.camera_coordinators["a"] = cam
    await select.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["a_night_vision"]

    cam.data.light_linger_duration = 30
    cam.data.light_behavior = "Flash"
    cam.refresh()
    assert _keys(added) == [
        "a_night_vision",
        "a_light_linger_duration",
        "a_light_behavior",
    ]
    cam.refresh()
    assert len(added) == 3


async def test_sensor_platform_filters_the_battery_and_adds_extras_when_reported():
    account, entry, added, add_entities = _setup()
    mains = _camera("mains")
    battery = _camera("batt", is_battery=True)
    account.camera_coordinators["mains"] = mains
    account.camera_coordinators["batt"] = battery
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch.object(er, "async_get", lambda hass: registry):
        await sensor.async_setup_entry(None, entry, add_entities)
        assert _keys(added) == [
            "mains_sd_card_status",
            "mains_wifi_rssi",
            "batt_battery",
            "batt_sd_card_status",
            "batt_wifi_rssi",
        ]
        # A stale battery sensor on the mains camera would have been removed.
        registry.async_get_entity_id.assert_called_once_with(
            "sensor", "aidot", "mains_battery"
        )

        # Extras arrive on the camera's own coordinator, and only the reported
        # ones become sensors.
        mains.camera_extras = {"wifi": {"ssid": "home"}, "sd": {}}
        mains.refresh()
        assert _keys(added)[5:] == ["mains_wifi_ssid"]
        mains.camera_extras["sd"] = {"total": 1, "used": 0}
        mains.refresh()
        assert _keys(added)[6:] == ["mains_sd_card_total", "mains_sd_card_used"]
        mains.refresh()
        assert len(added) == 8


async def test_switch_platform_filters_audio_ptz_and_extras_switches():
    account, entry, added, add_entities = _setup()
    plain = _camera("plain")
    sdes = _camera("sdes", is_sdes=True)
    account.camera_coordinators["plain"] = plain
    account.camera_coordinators["sdes"] = sdes
    await switch.async_setup_entry(None, entry, add_entities)
    ungated = [d.key for d in switch.CAMERA_SWITCHES if d.supported_fn is None]
    assert _keys(added) == [f"plain_{k}" for k in ungated] + [
        f"sdes_{k}" for k in ungated
    ] + ["sdes_camera_audio"]

    # PTZ arrives on a later refresh and the gated switch follows it.
    plain.device_client.info.ptz_directions = [3, 6]
    account.refresh()
    assert _keys(added)[-1] == "plain_ptz_tracking"

    # Extras land on the camera's own coordinator. Sound detectors are taken
    # as reported, the other two families only for the keys we have names for.
    plain.camera_extras = {
        "sound": {"glass_Break": True, "newDetector": False},
        "detect": {"humanDetect": True, "publicZone": 1},
        "alarm": {"autoAlarm": False, "motionDetection": True, "other": 1},
    }
    before = len(added)
    plain.refresh()
    assert _keys(added)[before:] == [
        "plain_sound_glass_Break",
        "plain_sound_newDetector",
        "plain_detect_humanDetect",
        "plain_auto_alarm_autoAlarm",
        "plain_auto_alarm_motionDetection",
    ]
    plain.refresh()
    account.refresh()
    assert len(added) == before + 5


async def test_button_platform_adds_ptz_buttons_once_the_camera_says_it_can_ptz():
    account, entry, added, add_entities = _setup()
    cam = _camera("a")
    account.camera_coordinators["a"] = cam
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch.object(er, "async_get", lambda hass: registry):
        await button.async_setup_entry(None, entry, add_entities)
        assert _keys(added) == ["entry-1_reload", "a_refresh_sd_recordings"]

        cam.device_client.info.ptz_directions = [3, 6]
        account.refresh()
        assert _keys(added)[2:] == ["a_ptz_left", "a_ptz_right", "a_ptz_stop"]
        account.refresh()
        assert len(added) == 5


# --------------------------------------------------------------------------- #
# A device that leaves the account and comes back
# --------------------------------------------------------------------------- #
#
# When a device leaves the account its coordinator is dropped and its device
# entry detached, which removes its entities. Each platform remembers what it
# has added by device id, so without forgetting the departed device a return
# (with a fresh coordinator) got no entities until the entry was reloaded. The
# camera platform has pruned since 2.37.11; every platform does now.


def _full_camera(dev_id: str) -> _Coordinator:
    return _camera(
        dev_id,
        is_sdes=True,
        ptz_directions=[3, 6],
        data=SimpleNamespace(
            motion_sensitivity=3,
            speaker_volume=50,
            light_brightness=40,
            night_vision_mode="auto",
        ),
        extras={
            "wifi": {"ssid": "home"},
            "sound": {"glass_Break": True},
        },
    )


@pytest.mark.parametrize(
    "platform",
    [binary_sensor, button, event, light, number, select, sensor, siren, switch],
    ids=lambda p: p.__name__.rsplit(".", 1)[-1],
)
async def test_a_camera_that_leaves_and_returns_gets_its_entities_back(platform):
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _full_camera("a")
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch.object(er, "async_get", lambda hass: registry):
        await platform.async_setup_entry(None, entry, add_entities)
        account.camera_coordinators["a"].refresh()
        first = [k for k in _keys(added) if k.startswith("a_")]
        assert first, "the platform made nothing for the camera"

        del account.camera_coordinators["a"]
        account.refresh()
        before = len(added)
        account.camera_coordinators["a"] = _full_camera("a")
        account.refresh()
        account.camera_coordinators["a"].refresh()
        assert sorted(_keys(added)[before:]) == sorted(first)

        # And only once: further refreshes add nothing.
        after = len(added)
        account.refresh()
        account.camera_coordinators["a"].refresh()
        assert len(added) == after


async def test_a_bulb_that_leaves_and_returns_gets_its_light_back():
    account, entry, added, add_entities = _setup()
    account.device_coordinators["bulb"] = _bulb("bulb")
    await light.async_setup_entry(None, entry, add_entities)
    assert _keys(added) == ["bulb"]
    del account.device_coordinators["bulb"]
    account.refresh()
    account.device_coordinators["bulb"] = _bulb("bulb")
    account.refresh()
    account.refresh()
    assert _keys(added) == ["bulb", "bulb"]


def test_forget_departed_drops_every_memo_shape_of_a_gone_device():
    memo = {"a", "a:extra:ssid", "a:sound:glass", ("a", "ptz"), "b", "b:x", ("b", "y")}
    forget_departed(memo, {"b": object()})
    assert memo == {"b", "b:x", ("b", "y")}


async def test_a_returning_camera_is_hooked_so_its_late_data_adds_entities():
    # The per-camera hook is remembered by device id too. A returning camera has
    # a fresh coordinator, and an entity whose data arrives on that coordinator
    # later (the floodlight brightness, once the camera reports it) is only
    # created if the fresh coordinator gets its own listener.
    account, entry, added, add_entities = _setup()
    account.camera_coordinators["a"] = _camera(
        "a", data=SimpleNamespace(light_brightness=40)
    )
    await number.async_setup_entry(None, entry, add_entities)
    assert "a_light_brightness" in _keys(added)

    del account.camera_coordinators["a"]
    account.refresh()
    before = len(added)
    back = _camera("a", data=SimpleNamespace())
    account.camera_coordinators["a"] = back
    account.refresh()
    assert "a_light_brightness" not in _keys(added)[before:]

    back.data.light_brightness = 40
    back.refresh()
    assert _keys(added)[before:].count("a_light_brightness") == 1
