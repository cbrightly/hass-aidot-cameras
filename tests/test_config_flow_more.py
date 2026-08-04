"""Config-flow paths not covered by ``test_config_flow.py``: the DHCP discovery
step and the reconfigure step (form + same-account / different-account success).

Driven through ``hass.config_entries.flow`` exactly like ``test_config_flow.py``;
``AidotClient`` is patched at its module name so no real login runs.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_DHCP
from homeassistant.const import CONF_COUNTRY_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo  # pyright: ignore[reportMissingImports]

from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "aidot"

USER_INPUT = {
    CONF_COUNTRY_CODE: "US",
    CONF_USERNAME: "test@example.com",
    CONF_PASSWORD: "new-password",
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


def _dhcp_info() -> DhcpServiceInfo:
    return DhcpServiceInfo(ip="1.2.3.4", hostname="aidot-cam", macaddress="aabbccddeeff")


# --------------------------------------------------------------------------- #
# async_step_dhcp
# --------------------------------------------------------------------------- #
async def test_dhcp_aborts_when_already_configured(hass: HomeAssistant) -> None:
    """A discovered device with an account already set up aborts silently."""
    existing = MockConfigEntry(
        domain=DOMAIN, unique_id="test-user-id-123", data=MOCK_LOGIN_INFO
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=_dhcp_info()
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_dhcp_proceeds_to_user_when_no_entry(hass: HomeAssistant) -> None:
    """With no account configured yet, discovery falls through to the user form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=_dhcp_info()
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


# --------------------------------------------------------------------------- #
# async_step_reconfigure
# --------------------------------------------------------------------------- #
def _entry(hass: HomeAssistant, unique_id: str = "test-user-id-123") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={
            **MOCK_LOGIN_INFO,
            CONF_COUNTRY_CODE: "US",
            CONF_USERNAME: "test@example.com",
            "id": unique_id,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_shows_form(hass: HomeAssistant) -> None:
    """Opening reconfigure with no input renders the reconfigure form."""
    entry = _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


async def test_reconfigure_same_account_updates_and_reloads(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    """Reconfiguring the same account (new uid == entry unique_id) skips the
    duplicate guard and merges the new password/country into entry data."""
    entry = _entry(hass, unique_id="test-user-id-123")
    result = await entry.start_reconfigure_flow(hass)

    # login_info.id matches the existing unique_id -> same-account path.
    with _patch_client({**MOCK_LOGIN_INFO, "id": "test-user-id-123"}):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_COUNTRY_CODE] == "US"


async def test_reconfigure_different_account_runs_dup_guard_then_updates(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    """A different account (new uid != entry unique_id) runs the duplicate guard;
    with no colliding entry it proceeds to update + reload."""
    entry = _entry(hass, unique_id="old-user-id")
    result = await entry.start_reconfigure_flow(hass)

    with _patch_client({**MOCK_LOGIN_INFO, "id": "brand-new-user-id"}):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "brand-new-user-id"
    assert entry.data[CONF_PASSWORD] == "new-password"
