"""The aidot integration."""

import logging
import os

# Serve ffmpeg directly instead of through the library's serve-relay. The relay
# exists to keep the public serve port connectable before ffmpeg binds, but its
# threaded accept loop was observed dead on HA 2026.7 (public port stops
# accepting, backlog fills) with no recovery while a mains camera holds its
# session - and a cold wake without it still reaches first HLS parts in ~16 s
# (validated live). setdefault, so AIDOT_SERVE_RELAY=1 in the environment can
# re-enable it for testing.
os.environ.setdefault("AIDOT_SERVE_RELAY", "0")

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import upstream_log
from .const import CONF_SERVE_PORT_BASE, DOMAIN
from .coordinator import AidotConfigEntry, AidotDeviceManagerCoordinator

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
    """Drop registry entries for camera entities this integration no longer creates.

    Two cases, both leaving a row behind that Home Assistant shows as an
    unavailable entity forever:

    - ``switch.*_siren`` / ``switch.*_floodlight``. The siren moved to the
      ``siren`` platform and the floodlight to ``light`` (so they're off the
      switch domain and harder to trigger by accident); drop the old switch
      entries so the new entities take over cleanly.
    - ``select.*_resolution``. Removed in 2.11.9 and not replaced. Every install
      upgrading from 2.11.8 or earlier carries one per camera.

    Matched on this integration's own rows (``platform == DOMAIN``) plus domain
    and unique_id, so an identically-named entity from another integration is
    untouched. Automations and dashboards referencing the old IDs must be
    repointed - noted in the changelog.
    """
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    for ent in list(reg.entities.values()):
        if ent.platform != DOMAIN:
            continue
        relocated = (ent.domain == "switch"
                     and (ent.unique_id.endswith("_siren")
                          or ent.unique_id.endswith("_floodlight")))
        withdrawn = ent.domain == "select" and ent.unique_id.endswith("_resolution")
        if relocated or withdrawn:
            reg.async_remove(ent.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Set up aidot from a config entry."""
    # Upstream python-aidot 0.3.56 logs routine per-device chatter at WARNING -
    # and the device's password and AES key with it. Filter by message rather
    # than muting the logger, so "connect device error" (the only signal that a
    # light failed to connect) still comes through. Idempotent; see upstream_log.
    upstream_log.install()

    # Work out which video decoder this host can actually use, off the event
    # loop. ffmpeg -decoders reports what the binary was compiled with, not what
    # runs here - a Raspberry Pi advertises decoders for hardware it does not
    # have - so each candidate is proven by decoding a sample, which costs a few
    # seconds on a first run and must not block setup.
    #
    # Run on Home Assistant's own executor, as a background task owned by this
    # entry, rather than on a thread this integration starts itself. An
    # untracked thread outliving a config entry is a leak, and one outliving a
    # test is reported as such - which is how this was found.
    async def _warm_decoders() -> None:
        try:
            from aidot_cameras.camera.hwaccel import probe_decoder

            # probe_decoder reads the remembered answer from disk and holds it
            # in memory itself, so this one call both loads and measures. The
            # reader used later on the event loop is memory-only by design.
            await hass.async_add_executor_job(probe_decoder, "h264")
        except Exception:  # pragma: no cover - never let this affect setup
            logging.getLogger(__name__).debug(
                "decoder detection unavailable", exc_info=True)

    entry.async_create_background_task(
        hass, _warm_decoders(), "aidot-decoder-probe", eager_start=False
    )

    # Apply the optional SDES HTTP-serve port base (camera._serve_port reads this).
    # Clear it when unset so removing the option reverts to the default on reload
    # instead of leaving the stale process-global value until an HA restart.
    if (port_base := entry.options.get(CONF_SERVE_PORT_BASE)) is not None:
        os.environ["AIDOT_SERVE_PORT_BASE"] = str(port_base)
    else:
        os.environ.pop("AIDOT_SERVE_PORT_BASE", None)
    # add_update_listener fires on ANY entry change - including the library
    # persisting a refreshed token back into the entry via async_update_entry
    # (coordinator.token_fresh_cb). Snapshot the options so the listener reloads
    # ONLY on a real options change and skips those data-only writes: reloading
    # on every token refresh churned all entities, re-primed the motion poll
    # (dropping events), and interrupted active streams. Seeded before the
    # coordinator starts, which is what can trigger a token refresh.
    hass.data.setdefault(DOMAIN, {})[f"options-{entry.entry_id}"] = dict(entry.options)
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
    except Exception:
        pass  # already registered from a previous entry setup

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
    """Reload the entry only when its OPTIONS actually change.

    add_update_listener also fires on bare data writes - the library persists a
    refreshed token into the entry via async_update_entry
    (coordinator.token_fresh_cb) - and reloading on those churns every entity,
    re-primes the motion poll (dropping events), and interrupts active streams.
    Compare against the snapshot taken in async_setup_entry and return early on a
    data-only update.
    """
    store = hass.data.setdefault(DOMAIN, {})
    key = f"options-{entry.entry_id}"
    current = dict(entry.options)
    if current == store.get(key):
        return
    store[key] = current
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_cleanup()
    hass.data.get(DOMAIN, {}).pop(f"options-{entry.entry_id}", None)
    return ok
