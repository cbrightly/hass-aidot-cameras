"""The aidot integration."""

import logging
import os

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_SERVE_PORT_BASE, DOMAIN
from .coordinator import AidotConfigEntry, AidotDeviceManagerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SIREN,
    Platform.SWITCH,
]


def _migrate_relocated_camera_entities(hass: HomeAssistant) -> None:
    """Remove the old ``switch.*_siren`` / ``switch.*_floodlight`` registry entries.

    The siren moved to the ``siren`` platform and the floodlight to ``light`` (so
    they're off the switch domain and harder to trigger by accident). Their old
    switch entities would otherwise linger as 'unavailable'; drop them so the new
    entities take over cleanly. (Automations/dashboards referencing the old IDs
    must be repointed - noted in the changelog.)
    """
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    for ent in list(reg.entities.values()):
        if (ent.platform == DOMAIN and ent.domain == "switch"
                and (ent.unique_id.endswith("_siren")
                     or ent.unique_id.endswith("_floodlight"))):
            reg.async_remove(ent.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Set up aidot from a config entry."""
    # Apply the optional SDES HTTP-serve port base (camera._serve_port reads this).
    if (port_base := entry.options.get(CONF_SERVE_PORT_BASE)) is not None:
        os.environ["AIDOT_SERVE_PORT_BASE"] = str(port_base)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))

    # Clean up the pre-relocation switch entities for the siren/floodlight before
    # the new siren/light platforms register (avoids lingering 'unavailable' ones).
    _migrate_relocated_camera_entities(hass)

    coordinator = AidotDeviceManagerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Register the AiDot account as a hub device so lights and cameras nest under
    # it (manifest integration_type=hub); each device links back via via_device.
    from homeassistant.helpers import device_registry as dr

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="AiDot",
        name="AiDot",
        entry_type=dr.DeviceEntryType.SERVICE,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    from .proxy import AidotVideoProxyView
    try:
        hass.http.register_view(AidotVideoProxyView(hass))
    except Exception as exc:  # registration is best-effort
        # Typically "already registered" from a previous entry setup; log at
        # debug rather than silently swallowing genuine registration errors.
        _LOGGER.debug("Aidot video proxy view not registered: %s", exc)

    # HA 2026.6 async_process_integration_platforms does not auto-discover
    # media_source.py in custom components, so register it explicitly.
    # hass.data["media_source"] is the dict HA's own platform loader writes to.
    if "media_source" in hass.config.components:
        from .media_source import async_get_media_source as _get_media_source
        _ms_key = "media_source"
        if DOMAIN not in hass.data.get(_ms_key, {}):
            source = await _get_media_source(hass)
            hass.data.setdefault(_ms_key, {})[DOMAIN] = source

    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: AidotConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        # Only tear the client/streams down once the platforms have actually
        # unloaded - a failed platform unload leaves entities live, and they must
        # not be left pointing at a stopped client.
        await entry.runtime_data.async_cleanup()
        # Drop our media-source provider so it doesn't dangle on a dead entry.
        hass.data.get("media_source", {}).pop(DOMAIN, None)
    return ok
