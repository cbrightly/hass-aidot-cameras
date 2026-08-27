"""Tests for the Aidot config flow."""

from unittest.mock import AsyncMock, patch

from aiohttp import ClientError

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_COUNTRY_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from aidot_cameras.exceptions import AidotUserOrPassIncorrect

from custom_components.aidot.const import CONF_NOTIFICATIONS

DOMAIN = "aidot"

USER_INPUT = {
    CONF_COUNTRY_CODE: "US",
    CONF_USERNAME: "test@example.com",
    CONF_PASSWORD: "correct-password",
}

MOCK_LOGIN_INFO = {
    "id": "test-user-id-123",
    "username": "test@example.com",
    "password": "correct-password",
    "country_code": "US",
    "accessToken": "fake-token",
    "mqttPassword": "fake-mqtt-pw",
}


def _patch_client(return_value=MOCK_LOGIN_INFO, side_effect=None):
    mock = AsyncMock()
    mock.async_post_login = AsyncMock(return_value=return_value, side_effect=side_effect)
    return patch("custom_components.aidot.config_flow.AidotClient", return_value=mock)


async def test_form_shows(hass: HomeAssistant) -> None:
    """Initial step renders the user form with no errors."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_create_entry_on_success(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    """Successful login creates a config entry with the expected title and data."""
    with _patch_client():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "test@example.com US"
    assert result["data"]["id"] == "test-user-id-123"
    assert mock_setup_entry.call_count == 1


async def test_invalid_auth_error(hass: HomeAssistant) -> None:
    """Bad credentials surface the invalid_auth error on the form."""
    with _patch_client(side_effect=AidotUserOrPassIncorrect):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect_error(hass: HomeAssistant) -> None:
    """Network error surfaces the cannot_connect error on the form."""
    with _patch_client(side_effect=ClientError):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_abort_if_already_configured(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    """Duplicate unique ID aborts with already_configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data=MOCK_LOGIN_INFO,
    )
    existing.add_to_hass(hass)

    with _patch_client():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(hass: HomeAssistant, mock_setup_entry) -> None:
    """Reauth with correct credentials updates the entry and reloads."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data={**MOCK_LOGIN_INFO, CONF_COUNTRY_CODE: "US", CONF_USERNAME: "test@example.com"},
    )
    existing.add_to_hass(hass)

    result = await existing.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with _patch_client():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "correct-password"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_invalid_auth(hass: HomeAssistant) -> None:
    """Reauth with bad password shows invalid_auth on the reauth form."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data={**MOCK_LOGIN_INFO, CONF_COUNTRY_CODE: "US", CONF_USERNAME: "test@example.com"},
    )
    existing.add_to_hass(hass)

    result = await existing.start_reauth_flow(hass)

    with _patch_client(side_effect=AidotUserOrPassIncorrect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong-password"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_opens_a_menu(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={"serve_port_base": 18600})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) == {"streaming", "notifications", "camera_notifications"}


async def test_streaming_step_saves_and_keeps_other_options(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"id": "u"},
        options={"serve_port_base": 18600, CONF_NOTIFICATIONS: {"targets": ["phone"]}},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "streaming"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "streaming"
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"serve_port_base": 19000})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["serve_port_base"] == 19000
    assert entry.options[CONF_NOTIFICATIONS] == {"targets": ["phone"]}


async def test_notifications_step_saves_global_settings(hass: HomeAssistant, mock_setup_entry) -> None:
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={"serve_port_base": 18600})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "notifications"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "notifications"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "targets": ["mobile_app_phone", "typed_in_service"],
            "cooldown_s": 30,
            "title": "{camera}!",
            "message": "{event_title} at {time}",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["serve_port_base"] == 18600
    assert entry.options[CONF_NOTIFICATIONS] == {
        "targets": ["mobile_app_phone", "typed_in_service"],
        "cooldown_s": 30,
        "title": "{camera}!",
        "message": "{event_title} at {time}",
    }


async def test_notifications_step_keeps_per_camera_settings(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"id": "u"},
        options={CONF_NOTIFICATIONS: {"cameras": {"dev1": {"events": "all", "targets": []}}}},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "notifications"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"targets": ["p"], "cooldown_s": 0, "title": "t", "message": "m"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NOTIFICATIONS]["cameras"] == {"dev1": {"events": "all", "targets": []}}
    assert entry.options[CONF_NOTIFICATIONS]["targets"] == ["p"]


async def test_notifications_step_lists_notify_services_but_not_meta_ones(hass: HomeAssistant, mock_setup_entry) -> None:
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    hass.services.async_register("notify", "send_message", lambda call: None)
    hass.services.async_register("notify", "persistent_notification", lambda call: None)
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={CONF_NOTIFICATIONS: {"targets": ["stored_one"]}})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "notifications"})
    schema = result["data_schema"].schema
    targets_key = next(k for k in schema if k == "targets")
    options = schema[targets_key].config["options"]
    assert "mobile_app_phone" in options
    assert "stored_one" in options          # stored values render as chips
    assert "send_message" not in options
    assert "persistent_notification" not in options
    assert schema[targets_key].config["custom_value"] is True
    assert schema[targets_key].config["multiple"] is True


def _loaded_cameras(entry, cams: dict[str, str]) -> None:
    """Pretend the entry is loaded with these cameras (dev_id -> name)."""
    from types import SimpleNamespace

    coords = {
        dev_id: SimpleNamespace(device_client=SimpleNamespace(info=SimpleNamespace(name=name)))
        for dev_id, name in cams.items()
    }
    entry.runtime_data = SimpleNamespace(camera_coordinators=coords)


async def test_camera_step_aborts_when_nothing_is_loaded(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "camera_notifications"})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_cameras"


async def test_camera_step_lists_cameras_by_name(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)
    _loaded_cameras(entry, {"dev2": "Porch", "dev1": "Garage"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "camera_notifications"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "camera_notifications"
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "camera")
    assert schema[key].config["options"] == [
        {"value": "dev1", "label": "Garage"},
        {"value": "dev2", "label": "Porch"},
    ]


async def test_camera_notify_step_saves_that_camera_only(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"id": "u"},
        options={CONF_NOTIFICATIONS: {"targets": ["g"], "cameras": {"dev1": {"events": "all", "targets": []}}}},
    )
    entry.add_to_hass(hass)
    _loaded_cameras(entry, {"dev1": "Garage", "dev2": "Porch"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "camera_notifications"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"camera": "dev2"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "camera_notify"
    assert result["description_placeholders"] == {"camera": "Porch"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"events": "person", "targets": ["phone"]}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NOTIFICATIONS] == {
        "targets": ["g"],
        "cameras": {
            "dev1": {"events": "all", "targets": []},
            "dev2": {"events": "person", "targets": ["phone"]},
        },
    }


async def test_camera_notify_step_defaults_to_off_and_shows_stored_values(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"id": "u"},
        options={CONF_NOTIFICATIONS: {"cameras": {"dev1": {"events": "person", "targets": ["p"]}}}},
    )
    entry.add_to_hass(hass)
    _loaded_cameras(entry, {"dev1": "Garage", "dev2": "Porch"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "camera_notifications"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"camera": "dev2"})
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults == {"events": "off", "targets": []}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "camera_notifications"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"camera": "dev1"})
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults == {"events": "person", "targets": ["p"]}
