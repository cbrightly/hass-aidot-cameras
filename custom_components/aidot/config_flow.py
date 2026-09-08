"""Config flow for Aidot integration."""

import logging
from typing import Any

from aidot_cameras.client import AidotClient
from aidot_cameras.const import CONF_ID, DEFAULT_COUNTRY_CODE, SUPPORTED_COUNTRY_CODES
from aidot_cameras.exceptions import AidotUserOrPassIncorrect
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
    CONF_NOTIFICATIONS,
    CONF_NOTIFY_CAMERAS,
    CONF_NOTIFY_COOLDOWN_S,
    CONF_NOTIFY_EVENTS,
    CONF_NOTIFY_MESSAGE,
    CONF_NOTIFY_TARGETS,
    CONF_NOTIFY_TITLE,
    CONF_STARTUP_PREWARM,
    CONF_SDES_AUDIO,
    CONF_SDES_AUDIO_GAIN_DB,
    CONF_SDES_FAST_LIVEPLAY,
    CONF_SDES_PIN_H264,
    CONF_SERVE_PORT_BASE,
    DEFAULT_ENABLE_LOCAL_CONTROL,
    DEFAULT_MAINS_IDLE_S,
    DEFAULT_NOTIFY_COOLDOWN_S,
    DEFAULT_NOTIFY_MESSAGE,
    DEFAULT_NOTIFY_TITLE,
    DEFAULT_STARTUP_PREWARM,
    DEFAULT_SDES_AUDIO,
    DEFAULT_SDES_AUDIO_GAIN_DB,
    DEFAULT_SDES_PIN_H264,
    DEFAULT_SDES_FAST_LIVEPLAY,
    DEFAULT_SERVE_PORT_BASE,
    DOMAIN,
    NOTIFY_EVENT_CHOICES,
    NOTIFY_EVENTS_OFF,
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


_LOGGER = logging.getLogger(__name__)

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
        except Exception:
            # The library maps only one of the server's several
            # bad-credential codes to AidotUserOrPassIncorrect; the others
            # arrive as a bare Exception raised from its ClientError handler,
            # which previously escaped this flow and surfaced as an opaque
            # "Unknown error" (and a traceback in the log) instead of telling
            # the user their credentials were rejected. Timeouts and transport
            # errors are handled above, so what is left is a rejected login.
            _LOGGER.exception("AiDot login failed with an unmapped error")
            errors["base"] = "invalid_auth"
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


# notify.* service names that are not delivery targets. notify.notify is a
# legitimate legacy group target, so it stays out of this set.
_NOTIFY_META_SERVICES = {"send_message", "persistent_notification"}


class AidotOptionsFlow(OptionsFlow):
    """Handle aidot options: a menu of streaming, global notification and
    per-camera notification pages. Every page writes back into the same
    options dict, so the keys it does not show survive untouched."""

    _camera_dev_id: str | None = None
    _camera_name: str = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["streaming", "notifications", "camera_notifications"],
        )

    # -- shared helpers -------------------------------------------------------

    def _notify_cfg(self) -> dict[str, Any]:
        return dict(self.config_entry.options.get(CONF_NOTIFICATIONS) or {})

    def _save_notify_cfg(self, cfg: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_NOTIFICATIONS: cfg}
        )

    def _targets_selector(self, stored: list[str]) -> selector.SelectSelector:
        """Multi-select of the notify.* services on this instance, plus any
        stored value (so it renders as a chip) and free text for a service that
        does not exist yet (custom_value)."""
        services = self.hass.services.async_services_for_domain("notify")
        names = {name for name in services if name not in _NOTIFY_META_SERVICES}
        names.update(stored)
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=sorted(names),
                multiple=True,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

    def _camera_choices(self) -> list[tuple[str, str]]:
        """(dev_id, name) for every loaded camera, sorted by name."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        coords = getattr(runtime, "camera_coordinators", None) or {}
        out = []
        for dev_id, coord in coords.items():
            info = getattr(getattr(coord, "device_client", None), "info", None)
            out.append((dev_id, getattr(info, "name", None) or dev_id))
        return sorted(out, key=lambda pair: pair[1].lower())

    # -- streaming (the historical page) ----------------------------------------

    async def async_step_streaming(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Streaming / connection options."""
        if user_input is not None:
            return self.async_create_entry(
                data={**self.config_entry.options, **user_input}
            )

        opts = self.config_entry.options
        current = opts.get(CONF_SERVE_PORT_BASE, DEFAULT_SERVE_PORT_BASE)
        current_mode = resolve_connection_mode(opts)
        current_pin_h264 = opts.get(CONF_SDES_PIN_H264, DEFAULT_SDES_PIN_H264)
        current_sdes_audio = opts.get(CONF_SDES_AUDIO, DEFAULT_SDES_AUDIO)
        current_sdes_audio_gain = opts.get(
            CONF_SDES_AUDIO_GAIN_DB, DEFAULT_SDES_AUDIO_GAIN_DB
        )
        current_local_control = opts.get(
            CONF_ENABLE_LOCAL_CONTROL, DEFAULT_ENABLE_LOCAL_CONTROL
        )
        current_mains_idle = opts.get(CONF_MAINS_IDLE_S, DEFAULT_MAINS_IDLE_S)
        current_sdes_fast_liveplay = opts.get(
            CONF_SDES_FAST_LIVEPLAY, DEFAULT_SDES_FAST_LIVEPLAY
        )
        return self.async_show_form(
            step_id="streaming",
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
                    # Served-audio gain (dB): the camera mic runs hot; raise toward
                    # 0 if audio is too quiet, lower if it clips.
                    vol.Optional(
                        CONF_SDES_AUDIO_GAIN_DB, default=current_sdes_audio_gain
                    ): vol.All(vol.Coerce(float), vol.Range(min=-30, max=30)),
                    # Pin the SDES video offer to H.264: stops a camera that
                    # ignores the offer's stated preference from flipping to
                    # H265 (and to a much larger frame), which players using
                    # Media Source Extensions may not decode.
                    vol.Optional(
                        CONF_SDES_PIN_H264, default=current_pin_h264
                    ): bool,
                    # Skip the ~2s livePlayResp wait for SDES cameras (app parity).
                    vol.Optional(
                        CONF_SDES_FAST_LIVEPLAY, default=current_sdes_fast_liveplay
                    ): bool,
                    # Mains-camera warm-hold seconds (instant re-views). 0 = never
                    # release; raise within the concurrent-stream cap (default 3).
                    vol.Optional(
                        CONF_MAINS_IDLE_S, default=current_mains_idle
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Optional(
                        CONF_STARTUP_PREWARM,
                        default=opts.get(CONF_STARTUP_PREWARM, DEFAULT_STARTUP_PREWARM),
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_LOCAL_CONTROL, default=current_local_control
                    ): bool,
                }
            ),
        )

    # -- global notification settings -----------------------------------------------

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Targets, cooldown and wording shared by every camera."""
        cfg = self._notify_cfg()
        if user_input is not None:
            cfg.update(
                {
                    CONF_NOTIFY_TARGETS: list(user_input.get(CONF_NOTIFY_TARGETS) or []),
                    CONF_NOTIFY_COOLDOWN_S: user_input[CONF_NOTIFY_COOLDOWN_S],
                    CONF_NOTIFY_TITLE: user_input[CONF_NOTIFY_TITLE],
                    CONF_NOTIFY_MESSAGE: user_input[CONF_NOTIFY_MESSAGE],
                }
            )
            return self._save_notify_cfg(cfg)

        stored = list(cfg.get(CONF_NOTIFY_TARGETS) or [])
        return self.async_show_form(
            step_id="notifications",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NOTIFY_TARGETS, default=stored): self._targets_selector(stored),
                    vol.Optional(
                        CONF_NOTIFY_COOLDOWN_S,
                        default=cfg.get(CONF_NOTIFY_COOLDOWN_S, DEFAULT_NOTIFY_COOLDOWN_S),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Optional(
                        CONF_NOTIFY_TITLE, default=cfg.get(CONF_NOTIFY_TITLE, DEFAULT_NOTIFY_TITLE)
                    ): str,
                    vol.Optional(
                        CONF_NOTIFY_MESSAGE, default=cfg.get(CONF_NOTIFY_MESSAGE, DEFAULT_NOTIFY_MESSAGE)
                    ): str,
                }
            ),
        )

    # -- per-camera notification settings -------------------------------------------

    async def async_step_camera_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the camera whose notification settings to edit."""
        cameras = self._camera_choices()
        if not cameras:
            return self.async_abort(reason="no_cameras")
        if user_input is not None:
            self._camera_dev_id = user_input["camera"]
            self._camera_name = dict(cameras).get(self._camera_dev_id, self._camera_dev_id)
            return await self.async_step_camera_notify()
        return self.async_show_form(
            step_id="camera_notifications",
            data_schema=vol.Schema(
                {
                    vol.Required("camera"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[{"value": dev_id, "label": name} for dev_id, name in cameras],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_camera_notify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Event filter and target override for one camera."""
        dev_id = self._camera_dev_id
        if dev_id is None:
            return await self.async_step_camera_notifications()
        cfg = self._notify_cfg()
        cameras = dict(cfg.get(CONF_NOTIFY_CAMERAS) or {})
        cam = cameras.get(dev_id) or {}
        if user_input is not None:
            cameras[dev_id] = {
                CONF_NOTIFY_EVENTS: user_input[CONF_NOTIFY_EVENTS],
                CONF_NOTIFY_TARGETS: list(user_input.get(CONF_NOTIFY_TARGETS) or []),
            }
            cfg[CONF_NOTIFY_CAMERAS] = cameras
            return self._save_notify_cfg(cfg)

        stored = list(cam.get(CONF_NOTIFY_TARGETS) or [])
        return self.async_show_form(
            step_id="camera_notify",
            description_placeholders={"camera": self._camera_name},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NOTIFY_EVENTS, default=cam.get(CONF_NOTIFY_EVENTS, NOTIFY_EVENTS_OFF)
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=NOTIFY_EVENT_CHOICES,
                            translation_key="notify_events",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_NOTIFY_TARGETS, default=stored): self._targets_selector(stored),
                }
            ),
        )
