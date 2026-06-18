"""Config flow for Aidot integration."""

from typing import Any

from aidot.client import AidotClient
from aidot.const import CONF_ID, DEFAULT_COUNTRY_CODE, SUPPORTED_COUNTRY_CODES
from aidot.exceptions import AidotUserOrPassIncorrect
from aiohttp import ClientError
import voluptuous as vol

from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo  # pyright: ignore[reportMissingImports]
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_COUNTRY_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONNECTION_MODES,
    CONF_CONNECTION_MODE,
    CONF_ENABLE_LOCAL_CONTROL,
    CONF_MAINS_IDLE_S,
    CONF_SDES_ADAPTIVE,
    CONF_SDES_AUDIO,
    CONF_SDES_FAST_LIVEPLAY,
    CONF_SERVE_PORT_BASE,
    DEFAULT_ENABLE_LOCAL_CONTROL,
    DEFAULT_MAINS_IDLE_S,
    DEFAULT_SDES_ADAPTIVE,
    DEFAULT_SDES_AUDIO,
    DEFAULT_SDES_FAST_LIVEPLAY,
    DEFAULT_SERVE_PORT_BASE,
    DOMAIN,
    resolve_connection_mode,
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_COUNTRY_CODE,
            default=DEFAULT_COUNTRY_CODE,
        ): selector.CountrySelector(
            selector.CountrySelectorConfig(
                countries=SUPPORTED_COUNTRY_CODES,
            )
        ),
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
    }
)


class AidotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle aidot config flow."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return AidotOptionsFlow()

    async def _async_try_login(
        self, country_code: str, username: str, password: str
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Validate credentials with a throwaway client; return (login_info, errors).

        The validation client must always be closed: async_post_login starts LAN
        discovery and a token-refresh timer that must not outlive the flow.
        """
        errors: dict[str, str] = {}
        client = AidotClient(
            session=async_get_clientsession(self.hass),
            country_code=country_code,
            username=username,
            password=password,
        )
        login_info: dict[str, Any] | None = None
        try:
            login_info = await client.async_post_login()
        except AidotUserOrPassIncorrect:
            errors["base"] = "invalid_auth"
        except (TimeoutError, ClientError):
            errors["base"] = "cannot_connect"
        finally:
            await client.async_close()
        return login_info, errors

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle a discovered AiDot device on the local network.

        One account entry covers all devices, so suggest setup only when none
        exists yet; otherwise abort silently.
        """
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            login_info, errors = await self._async_try_login(
                user_input[CONF_COUNTRY_CODE],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )

            if not errors:
                assert login_info is not None
                await self.async_set_unique_id(login_info[CONF_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{user_input[CONF_USERNAME]} {user_input[CONF_COUNTRY_CODE]}",
                    data={
                        **login_info,
                        CONF_COUNTRY_CODE: user_input[CONF_COUNTRY_CODE],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration - allows changing credentials or country."""
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            login_info, errors = await self._async_try_login(
                user_input[CONF_COUNTRY_CODE],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )

            if not errors:
                assert login_info is not None
                new_uid = login_info[CONF_ID]
                await self.async_set_unique_id(new_uid)
                # Only gate against duplicates when the account actually changes;
                # same account reconfigures would otherwise abort on themselves.
                if new_uid != reconfigure_entry.unique_id:
                    self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    unique_id=new_uid,
                    title=f"{user_input[CONF_USERNAME]} {user_input[CONF_COUNTRY_CODE]}",
                    data_updates={
                        **login_info,
                        CONF_COUNTRY_CODE: user_input[CONF_COUNTRY_CODE],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COUNTRY_CODE,
                        default=reconfigure_entry.data.get(
                            CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE
                        ),
                    ): selector.CountrySelector(
                        selector.CountrySelectorConfig(
                            countries=SUPPORTED_COUNTRY_CODES,
                        )
                    ),
                    vol.Required(
                        CONF_USERNAME,
                        default=reconfigure_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={
                "username": reconfigure_entry.data.get(CONF_USERNAME, "")
            },
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            login_info, errors = await self._async_try_login(
                reauth_entry.data[CONF_COUNTRY_CODE],
                reauth_entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )

            if not errors:
                assert login_info is not None
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=login_info
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={
                "username": reauth_entry.data.get(CONF_USERNAME, "")
            },
            errors=errors,
        )


class AidotOptionsFlow(OptionsFlow):
    """Handle aidot options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_SERVE_PORT_BASE, DEFAULT_SERVE_PORT_BASE
        )
        current_mode = resolve_connection_mode(self.config_entry.options)
        current_sdes_audio = self.config_entry.options.get(
            CONF_SDES_AUDIO, DEFAULT_SDES_AUDIO
        )
        current_local_control = self.config_entry.options.get(
            CONF_ENABLE_LOCAL_CONTROL, DEFAULT_ENABLE_LOCAL_CONTROL
        )
        current_mains_idle = self.config_entry.options.get(
            CONF_MAINS_IDLE_S, DEFAULT_MAINS_IDLE_S
        )
        current_sdes_fast_liveplay = self.config_entry.options.get(
            CONF_SDES_FAST_LIVEPLAY, DEFAULT_SDES_FAST_LIVEPLAY
        )
        current_sdes_adaptive = self.config_entry.options.get(
            CONF_SDES_ADAPTIVE, DEFAULT_SDES_ADAPTIVE
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SERVE_PORT_BASE, default=current
                    ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65100)),
                    vol.Optional(
                        CONF_CONNECTION_MODE, default=current_mode
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=CONNECTION_MODES,
                            translation_key=CONF_CONNECTION_MODE,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_SDES_AUDIO, default=current_sdes_audio
                    ): bool,
                    # EXPERIMENTAL: skip the ~2s livePlayResp wait for SDES cameras.
                    vol.Optional(
                        CONF_SDES_FAST_LIVEPLAY, default=current_sdes_fast_liveplay
                    ): bool,
                    # Adaptive fast-with-fallback for SDES (opt-in): fast-first,
                    # fall back to the full relay path on no media.
                    vol.Optional(
                        CONF_SDES_ADAPTIVE, default=current_sdes_adaptive
                    ): bool,
                    # Mains-camera warm-hold seconds (instant re-views). 0 = never
                    # release; raise within the concurrent-stream cap (default 3).
                    vol.Optional(
                        CONF_MAINS_IDLE_S, default=current_mains_idle
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Optional(
                        CONF_ENABLE_LOCAL_CONTROL, default=current_local_control
                    ): bool,
                }
            ),
        )
