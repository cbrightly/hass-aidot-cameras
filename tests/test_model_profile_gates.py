"""Per-model controls follow what the model's profile declares, as the app's do.

The app shows Status LED, Voice Prompt, WDR, Watermark, Microphone, Auto Light
and the light only when the model's cloud profile declares LedOnOff,
voiceEnable, HDRStatus, OSDEnable, micEnable, autoLightEnable and LightOnOff
(its LedOnOffConfig, VoiceEnableConfig, ...). A model that does not declare
one gets no entity, and a row an earlier release made is removed. A profile
that could not be read declares nothing we can trust, so it keeps everything.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot.const import DOMAIN
from custom_components.aidot.model_profile import declares
from custom_components.aidot.entity import described_entities
from custom_components.aidot.select import CAMERA_SELECTS
from custom_components.aidot.switch import CAMERA_SWITCHES, _switch_supported


def _pending_switches(cams, memo=None, hass=None):
    """The (camera, description) pairs the switch platform would add now."""
    with patch.object(er, "async_get", return_value=MagicMock()):
        return described_entities(
            cams,
            {} if memo is None else memo,
            CAMERA_SWITCHES,
            lambda c, d: (c, d),
            _switch_supported,
            hass=hass,
            platform="switch",
        )


_GATED = {
    "status_led": "LedOnOff",
    "voice_prompts": "voiceEnable",
    "hdr": "HDRStatus",
    "osd_timestamp": "OSDEnable",
    "microphone": "micEnable",
    "auto_light": "autoLightEnable",
}


def _client(declared=(), dev_id="cam1"):
    info = SimpleNamespace(
        dev_id=dev_id,
        declared_properties=frozenset(declared),
        model_id="LK.IPC.A000088",
        mac="",
        name="Cam",
        hw_version="1",
        ptz_directions=[],
    )
    return SimpleNamespace(info=info, is_battery_camera=False, is_sdes_camera=False)


def _coord(client):
    return SimpleNamespace(
        device_client=client, camera_info=client.info, camera_extras={}, data=None
    )


def test_each_gated_switch_names_the_property_the_app_gates_on():
    by_key = {d.key: d.profile_property for d in CAMERA_SWITCHES}
    for key, prop in _GATED.items():
        assert by_key[key] == prop
    # Motion detection is on every camera the app shows, and PTZ tracking has
    # its own capability gate.
    assert by_key["motion_detection"] is None
    assert by_key["ptz_tracking"] is None


def test_an_unreadable_profile_keeps_every_control():
    client = _client(declared=())
    assert declares(client, "LedOnOff")
    keys = {d.key for _c, d in _pending_switches({"cam1": _coord(client)})}
    assert set(_GATED) <= keys


def test_a_model_gets_only_the_switches_it_declares():
    client = _client(declared=("LedOnOff", "micEnable", "MotionDetection_Enable"))
    keys = {d.key for _c, d in _pending_switches({"cam1": _coord(client)})}
    assert {"status_led", "microphone", "motion_detection"} <= keys
    assert not keys & {"voice_prompts", "hdr", "osd_timestamp", "auto_light"}


async def test_removal_is_keyed_on_the_declaration(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    for dev_id in ("camA", "camB", "camC"):
        reg.async_get_or_create("switch", DOMAIN, f"{dev_id}_hdr", config_entry=entry)
    # Declares something else: removed. Declares it, or unreadable: kept.
    cams = {
        "camA": _coord(_client(("LedOnOff",), "camA")),
        "camB": _coord(_client(("HDRStatus",), "camB")),
        "camC": _coord(_client((), "camC")),
    }
    described_entities(
        cams,
        {},
        CAMERA_SWITCHES,
        lambda c, d: (c, d),
        _switch_supported,
        hass=hass,
        platform="switch",
    )
    left = {e.unique_id for e in reg.entities.values() if e.platform == DOMAIN}
    assert left == {"camB_hdr", "camC_hdr"}


def test_an_undeclared_pair_is_settled_once():
    # A model's declaration does not change while the entry is loaded, so the
    # undeclared pairs are not asked about (or removed) again.
    client = _client(declared=("LedOnOff",))
    cams, memo = {"cam1": _coord(client)}, {}
    registry = MagicMock()
    registry.async_get_entity_id.return_value = None
    with patch.object(er, "async_get", return_value=registry):
        for _ in range(3):
            described_entities(
                cams,
                memo,
                CAMERA_SWITCHES,
                lambda c, d: (c, d),
                _switch_supported,
                hass=None,
                platform="switch",
            )
    asked = [c.args[2] for c in registry.async_get_entity_id.call_args_list]
    assert sorted(asked) == sorted(
        f"cam1_{k}" for k, prop in _GATED.items() if prop != "LedOnOff"
    )


async def test_select_setup_removes_rows_for_undeclared_selects(hass):
    from custom_components.aidot.select import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    linger = reg.async_get_or_create(
        "select", DOMAIN, "cam1_light_linger_duration", config_entry=entry
    )
    behavior = reg.async_get_or_create(
        "select", DOMAIN, "cam1_light_behavior", config_entry=entry
    )
    coord = _coord(_client(("lightBehavior",)))
    # Both values reported, as the A000088's cloud does for LingerDuration.
    coord.data = SimpleNamespace(
        night_vision_mode="auto", light_linger_duration=30, light_behavior="flash"
    )
    coord.async_add_listener = lambda _cb: lambda: None
    entry.runtime_data = _runtime({"cam1": coord})
    added = []
    await async_setup_entry(hass, entry, added.extend)
    keys = {e.unique_id for e in added}
    assert keys == {"cam1_night_vision", "cam1_light_behavior"}
    assert reg.async_get(linger.entity_id) is None
    assert reg.async_get(behavior.entity_id) is not None


def test_the_selects_name_the_property_the_app_gates_on():
    by_key = {d.key: d.profile_property for d in CAMERA_SELECTS}
    assert by_key == {
        "light_linger_duration": "LingerDuration",
        "light_behavior": "lightBehavior",
        "night_vision": None,
    }


def _runtime(coords):
    return SimpleNamespace(
        camera_coordinators=coords,
        device_coordinators={},
        async_add_listener=lambda _cb: lambda: None,
    )


@pytest.mark.parametrize(
    ("declared", "made"),
    [(("LightOnOff",), True), (("LedOnOff",), False), ((), True)],
)
async def test_the_floodlight_follows_the_profile(hass, declared, made):
    from custom_components.aidot.light import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    row = reg.async_get_or_create(
        "light", DOMAIN, "cam1_floodlight", config_entry=entry
    )
    entry.runtime_data = _runtime({"cam1": _coord(_client(declared))})
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert bool(added) is made
    assert (reg.async_get(row.entity_id) is not None) is made


async def test_the_siren_is_not_gated_on_the_profile(hass):
    # No model declares sirenRing (measured live on the A001513, A001064 and
    # A000088), and the app has no settings gate for the siren: gating it
    # would remove the siren from every camera.
    from custom_components.aidot.siren import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    row = reg.async_get_or_create("siren", DOMAIN, "cam1_siren", config_entry=entry)
    entry.runtime_data = _runtime(
        {"cam1": _coord(_client(("LedOnOff", "LightOnOff", "micEnable")))}
    )
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert [e.unique_id for e in added] == ["cam1_siren"]
    assert reg.async_get(row.entity_id) is not None


async def test_switch_setup_removes_rows_for_undeclared_switches(hass):
    from custom_components.aidot.switch import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    hdr = reg.async_get_or_create("switch", DOMAIN, "cam1_hdr", config_entry=entry)
    led = reg.async_get_or_create(
        "switch", DOMAIN, "cam1_status_led", config_entry=entry
    )
    coord = _coord(_client(("LedOnOff",)))
    coord.async_add_listener = lambda _cb: lambda: None
    entry.runtime_data = _runtime({"cam1": coord})
    added = []
    await async_setup_entry(hass, entry, added.extend)
    keys = {e.unique_id for e in added}
    assert "cam1_status_led" in keys and "cam1_hdr" not in keys
    assert reg.async_get(hdr.entity_id) is None
    assert reg.async_get(led.entity_id) is not None
