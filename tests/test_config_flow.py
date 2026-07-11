"""Tests for the Aidot config flow."""

from unittest.mock import AsyncMock, patch

from aiohttp import ClientError

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_COUNTRY_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from aidot.exceptions import AidotUserOrPassIncorrect

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


async def test_options_flow(hass: HomeAssistant, mock_setup_entry) -> None:
    """Options flow saves the serve_port_base setting."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id-123",
        data=MOCK_LOGIN_INFO,
        options={"serve_port_base": 18600},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"serve_port_base": 19000}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["serve_port_base"] == 19000
