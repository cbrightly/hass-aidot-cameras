"""Config-entry setup/reload behaviour.

Regression coverage for the self-reload loop: the library persists a refreshed
token by writing it back into the config entry (coordinator.token_fresh_cb ->
async_update_entry). add_update_listener fires on that data-only write too, so an
unconditional reload churned every entity, re-primed the motion poll (dropping
events), and interrupted streams on every token refresh. The listener must reload
only on a real OPTIONS change.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.aidot import _async_reload_on_options, proxy
from custom_components.aidot.const import DOMAIN


@pytest.fixture(autouse=True)
def _restore_url_secret_state():
    """Some tests here call the real async_setup_entry, which loads (and,
    with the once-per-process sentinel, latches) the clip-link signing
    secret - reset the sentinel up front (another module can have left it
    latched from an unrelated real-setup call earlier in the session) and
    restore both globals afterward so this module doesn't leak forward.
    """
    original_secret = proxy._URL_SECRET
    original_loaded = proxy._SECRET_LOADED
    proxy._SECRET_LOADED = False
    try:
        yield
    finally:
        proxy._URL_SECRET = original_secret
        proxy._SECRET_LOADED = original_loaded


def _hass_with_options(entry_id: str, seeded: dict) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {f"options-{entry_id}": dict(seeded)}}
    hass.config_entries.async_reload = AsyncMock()
    return hass


async def test_reload_skips_data_only_updates():
    # A token persist (async_update_entry data write) leaves options unchanged.
    hass = _hass_with_options("e1", {"serve_port_base": 5000})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 5000})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()


async def test_reload_on_actual_options_change():
    hass = _hass_with_options("e1", {"serve_port_base": 5000})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 6000})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("e1")
    # snapshot advanced so the next data-only write won't reload again
    assert hass.data[DOMAIN]["options-e1"] == {"serve_port_base": 6000}


async def test_options_snapshot_seeded_before_coordinator_starts(hass):
    # The whole fix depends on the options snapshot being in place BEFORE the
    # coordinator starts: a token refresh during first-refresh persists data and
    # fires the update listener, and if the snapshot isn't seeded yet that first
    # persist would spuriously reload. Abort setup at the coordinator to prove the
    # seed already happened by then (guards the ordering against a refactor).
    from unittest.mock import patch

    import pytest
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot import DOMAIN, async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={"marker": 1})
    entry.add_to_hass(hass)
    captured: dict = {}

    class _Stop(Exception):
        pass

    def _coord_ctor(_hass, _entry):
        captured["snapshot"] = hass.data.get(DOMAIN, {}).get(f"options-{entry.entry_id}")
        raise _Stop  # bail before the platform-setup tail; the seed already ran

    with patch("custom_components.aidot.AidotDeviceManagerCoordinator", side_effect=_coord_ctor), \
            patch("custom_components.aidot._migrate_relocated_camera_entities"):
        with pytest.raises(_Stop):
            await async_setup_entry(hass, entry)
    assert captured["snapshot"] == {"marker": 1}


async def test_notification_only_change_does_not_reload():
    # Notification settings are read live by the dispatcher; a reload would
    # tear down every camera session for nothing.
    hass = _hass_with_options("e1", {"serve_port_base": 5000, "notifications": {"cooldown_s": 60}})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 5000, "notifications": {"cooldown_s": 5}})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()
    # snapshot advanced so the same write is not re-examined
    assert hass.data[DOMAIN]["options-e1"]["notifications"] == {"cooldown_s": 5}


async def test_notification_change_alongside_streaming_change_still_reloads():
    hass = _hass_with_options("e1", {"serve_port_base": 5000, "notifications": {}})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 6000, "notifications": {"cooldown_s": 5}})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_adding_notifications_key_for_the_first_time_does_not_reload():
    hass = _hass_with_options("e1", {"serve_port_base": 5000})
    entry = MagicMock(entry_id="e1", options={"serve_port_base": 5000, "notifications": {"targets": ["x"]}})
    await _async_reload_on_options(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()
    assert hass.data[DOMAIN]["options-e1"]["notifications"] == {"targets": ["x"]}


async def test_setup_starts_the_motion_notifier_and_detaches_on_unload(hass):
    from unittest.mock import AsyncMock, MagicMock, patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot import DOMAIN, async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)

    manager = MagicMock()
    manager.async_config_entry_first_refresh = AsyncMock()
    notifier = MagicMock()

    with patch("custom_components.aidot.AidotDeviceManagerCoordinator", return_value=manager), \
            patch("custom_components.aidot.AidotMotionNotifier", return_value=notifier) as ctor, \
            patch("custom_components.aidot._migrate_relocated_camera_entities"), \
            patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        assert await async_setup_entry(hass, entry) is True
        await hass.async_block_till_done()

    ctor.assert_called_once_with(hass, entry)
    notifier.start.assert_called_once_with()
    # detach is registered as an unload hook
    for cb in entry._on_unload:
        cb()
    notifier.detach.assert_called()


async def test_url_secret_loaded_before_coordinator_starts(hass, hass_storage):
    # The clip-link signing secret must be persisted (and swapped into
    # proxy._URL_SECRET) before the coordinator can run - a motion event
    # during first refresh can mint a signed URL, and that URL must be signed
    # with the persisted secret, not the per-process fallback. Abort setup at
    # the coordinator to prove the secret is already in the store by then.
    from unittest.mock import patch

    import pytest
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.aidot import DOMAIN, async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)

    class _Stop(Exception):
        pass

    def _coord_ctor(_hass, _entry):
        raise _Stop  # bail before the platform-setup tail

    with patch("custom_components.aidot.AidotDeviceManagerCoordinator", side_effect=_coord_ctor), \
            patch("custom_components.aidot._migrate_relocated_camera_entities"):
        with pytest.raises(_Stop):
            await async_setup_entry(hass, entry)

    assert "aidot_url_secret" in hass_storage
