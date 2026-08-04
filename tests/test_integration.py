"""End-to-end platform-setup integration test for the Aidot integration.

Unlike the other test modules - which exercise a single entity or coordinator
method in isolation with a hand-built mock - this drives the FULL config-entry
lifecycle through Home Assistant with only the ``aidot`` *library* mocked:

    hass.config_entries.async_setup(entry)
        -> __init__.async_setup_entry
        -> AidotDeviceManagerCoordinator(...)  (the REAL coordinator)
             _async_setup / _async_update_data / _sync_*_coordinators
             -> per-device AidotDeviceUpdateCoordinator / AidotCameraUpdateCoordinator
        -> async_forward_entry_setups(PLATFORMS)
             -> every platform's async_setup_entry + its _add_new_* closure
             -> entities constructed via the AidotEntity base wiring

So it covers the setup paths the per-entity unit tests deliberately skip (they
build a MagicMock coordinator and never forward a platform). The library
``AidotClient`` is patched to return canned device data; a mocked ``DeviceClient``
per device provides the ``info`` / ``status`` / async command surface the real
coordinators and entities read during setup.

Deterministic and fast: no real network / ffmpeg / sleep. The camera entity's
background prewarm / go2rtc / thumbnail work is patched out so setup wiring is
what's exercised, not streaming.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from pytest_homeassistant_custom_component.common import MockConfigEntry

# Library constants the real coordinator uses to shape/parse the device list.
# Imported here so the canned device dicts are keyed exactly as the code reads
# them (correct-by-construction; py_compile doesn't execute these imports).
from aidot.const import (
    CONF_ACCESS_TOKEN,
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_MODEL_ID,
)

from custom_components.aidot.camera import AidotCamera

DOMAIN = "aidot"

LIGHT_ID = "light-0001"
CAMERA_ID = "cam-0001"

MOCK_LOGIN_INFO = {
    "id": "test-user-id-123",
    "username": "test@example.com",
    "password": "correct-password",
    "country_code": "US",
    "accessToken": "fake-token",
    "mqttPassword": "fake-mqtt-pw",
}

# Every platform __init__.PLATFORMS forwards to - each must create at least one
# entity in the registry for the entry after a full setup.
EXPECTED_DOMAINS = {
    "binary_sensor",
    "button",
    "camera",
    "event",
    "light",
    "number",
    "select",
    "sensor",
    "siren",
    "switch",
}


# --------------------------------------------------------------------------- #
# Canned library device data + mocked DeviceClients
# --------------------------------------------------------------------------- #
def _light_device() -> dict:
    """A cloud device dict the coordinator classifies as a light.

    ``_is_light_device`` requires ``type == "light"`` and a non-None first
    ``aesKey`` element; ``_is_camera_device`` must reject it (no IPC in model,
    no camera service module).
    """
    return {
        CONF_ID: LIGHT_ID,
        "type": "light",
        CONF_AES_KEY: ["aeskey-value", None],
        CONF_MODEL_ID: "AiDot.BulbRGBW",
    }


def _camera_device() -> dict:
    """A cloud device dict the coordinator classifies as a camera (IPC model)."""
    return {
        CONF_ID: CAMERA_ID,
        CONF_MODEL_ID: "AiDot.IPC.A000088",
    }


def _light_client() -> MagicMock:
    """DeviceClient for the bulb - drives AidotLight + AidotDeviceUpdateCoordinator."""
    info = SimpleNamespace(
        dev_id=LIGHT_ID,
        model_id="AiDot.BulbRGBW",
        mac="aa:bb:cc:dd:ee:01",
        name="Living Room Bulb",
        hw_version="1.0",
        enable_rgbw=True,
        enable_cct=False,
        cct_min=2700,
        cct_max=6500,
    )
    dc = MagicMock()
    dc.info = info
    dc.device_id = LIGHT_ID
    # DeviceStatusData the light entity + coordinator read.
    dc.status = SimpleNamespace(
        on=True, dimming=128, cct=3000, rgbw=(0, 0, 0, 255), online=True
    )
    # Light coordinator _async_setup / _async_update_data surface.
    dc.set_status_fresh_cb = MagicMock()
    dc.async_login = AsyncMock()
    dc.connect_and_login = True
    return dc


def _camera_client() -> MagicMock:
    """DeviceClient for the camera - drives every camera platform + its coordinator."""
    info = SimpleNamespace(
        dev_id=CAMERA_ID,
        model_id="AiDot.IPC.A000088",
        mac="aa:bb:cc:dd:ee:02",
        name="Front Door Cam",
        hw_version="2.0",
        # PTZ direction codes gate which button entities are created.
        ptz_directions=[1, 2, 3, 6],
    )
    dc = MagicMock()
    dc.info = info
    dc.device_id = CAMERA_ID
    # is_battery -> battery sensor is created (and startup-prewarm short-circuits);
    # is_sdes    -> the "Camera audio" switch is created.
    dc.is_battery_camera = True
    dc.is_sdes_camera = True
    dc.stream_rtsp_url = None
    # CameraStatusData the camera platforms read through get_is_on / get_value /
    # get_current_option lambdas when HA writes the entity's initial state.
    dc.status = SimpleNamespace(
        online=True,
        floodlight=False,
        siren=False,
        occupancy=False,
        motion_detection=True,
        status_led=True,
        microphone=True,
        ptz_tracking=False,
        ir_light=False,
        battery_remaining=82,
        sd_card_status="normal",
        wifi_rssi=-52,
        motion_sensitivity=3,
        speaker_volume=50,
        night_vision_mode="auto",
    )
    # Camera coordinator _async_setup / _async_update_data + cleanup surface.
    dc.async_start_motion_polling = AsyncMock()
    dc.async_stop_motion_polling = AsyncMock()
    dc.async_stop_streaming = AsyncMock()
    dc.update_status_from_device = MagicMock()
    dc.set_status_fresh_cb = MagicMock()
    # Touched by the camera entity's (patched) background helpers - stubbed
    # anyway so nothing reaches the network if a patch is ever relaxed.
    dc.async_get_latest_thumbnail = AsyncMock(return_value=None)
    dc.start_keepalive = AsyncMock()
    dc.latest_jpeg = None
    return dc


def _make_client(light_dc: MagicMock, camera_dc: MagicMock) -> MagicMock:
    """The mocked AidotClient (patched over coordinator.AidotClient)."""
    client = MagicMock()
    # accessToken present -> async_auto_login skips async_post_login.
    client.login_info = {"id": "test-user-id-123", CONF_ACCESS_TOKEN: "fake-token"}
    client.set_token_fresh_cb = MagicMock()
    client.async_post_login = AsyncMock(return_value=MOCK_LOGIN_INFO)
    client.async_cleanup = AsyncMock()
    client.async_get_all_device = AsyncMock(
        return_value={CONF_DEVICE_LIST: [_light_device(), _camera_device()]}
    )

    def _get_device_client(device: dict) -> MagicMock:
        return light_dc if device.get(CONF_ID) == LIGHT_ID else camera_dc

    client.get_device_client = MagicMock(side_effect=_get_device_client)
    return client


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data=MOCK_LOGIN_INFO,
        options={},
    )
    entry.add_to_hass(hass)
    return entry


# --------------------------------------------------------------------------- #
# End-to-end setup / unload
# --------------------------------------------------------------------------- #
async def test_full_entry_setup_creates_all_platform_entities(
    hass: HomeAssistant,
) -> None:
    """Setting up the entry runs the real coordinator and every platform's
    async_setup_entry, producing at least one entity per platform and a LOADED
    entry; unloading tears the library client down."""
    light_dc = _light_client()
    camera_dc = _camera_client()
    client = _make_client(light_dc, camera_dc)
    entry = _entry(hass)

    with patch(
        "custom_components.aidot.coordinator.AidotClient", return_value=client
    ), patch.object(
        AidotCamera, "_publish_to_go2rtc", AsyncMock(return_value=None)
    ), patch.object(
        AidotCamera, "_prefetch_thumbnail", AsyncMock(return_value=None)
    ), patch.object(
        AidotCamera, "_startup_prewarm", AsyncMock(return_value=None)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

        # __init__.async_setup_entry succeeded and stored the real coordinator.
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data
        assert coordinator.client is client
        # The real coordinator classified and spawned per-device coordinators.
        assert LIGHT_ID in coordinator.device_coordinators
        assert CAMERA_ID in coordinator.camera_coordinators
        # _async_setup -> async_auto_login (token present -> no post_login) and
        # the camera coordinator started cloud motion polling.
        client.async_get_all_device.assert_awaited()
        camera_dc.async_start_motion_polling.assert_awaited()

        # Every platform's async_setup_entry created at least one entity.
        reg = er.async_get(hass)
        entities = er.async_entries_for_config_entry(reg, entry.entry_id)
        domains = {e.domain for e in entities}
        assert EXPECTED_DOMAINS <= domains, (
            f"missing platform entities: {EXPECTED_DOMAINS - domains}"
        )

        # Enabled entities also surface live states.
        light_states = hass.states.async_entity_ids("light")
        camera_states = hass.states.async_entity_ids("camera")
        assert light_states, "expected a light.* state"
        assert camera_states, "expected a camera.* state"
        # The bulb and the camera floodlight are both in the light domain.
        assert len(light_states) >= 2

        # ------------------------------------------------------------------ #
        # Unload: platforms unload, then the client + camera streams are torn down.
        # ------------------------------------------------------------------ #
        assert await hass.config_entries.async_unload(entry.entry_id) is True
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    client.async_cleanup.assert_awaited_once()
    camera_dc.async_stop_motion_polling.assert_awaited()
    camera_dc.async_stop_streaming.assert_awaited()
    # Light coordinators have their push callback detached on cleanup.
    light_dc.set_status_fresh_cb.assert_called_with(None)
