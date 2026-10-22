"""Unload and cleanup must not half-tear-down an entry.

- A platform that refuses to unload leaves the entry loaded, so nothing under
  its live entities may be torn down.
- One camera whose stop raises must not leave the other cameras streaming or
  skip the client's own cleanup.
- The SD-card refresh press is bound to the config entry, so an unload
  cancels a listing that can wait 75 s for a session.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.aidot import async_unload_entry
from custom_components.aidot.button import AidotSdRefreshButton
from custom_components.aidot.const import DOMAIN
from custom_components.aidot.coordinator import AidotDeviceManagerCoordinator

from .test_hls_in_sync_source import _unloading


async def test_a_refused_platform_unload_tears_nothing_down(monkeypatch):
    from aidot_cameras.camera import hls_ts

    stops = []
    monkeypatch.setattr(hls_ts, "shutdown", lambda: stops.append(1))
    hass = MagicMock()
    entry = _unloading(hass, 0)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    hass.data = {DOMAIN: {f"options-{entry.entry_id}": {"x": 1}}}

    assert await async_unload_entry(hass, entry) is False
    entry.runtime_data.async_cleanup.assert_not_awaited()
    assert f"options-{entry.entry_id}" in hass.data[DOMAIN]
    assert stops == []


async def test_an_accepted_platform_unload_tears_down(monkeypatch):
    from aidot_cameras.camera import hls_ts

    monkeypatch.setattr(hls_ts, "shutdown", lambda: None)
    hass = MagicMock()
    entry = _unloading(hass, 0)
    hass.data = {DOMAIN: {f"options-{entry.entry_id}": {"x": 1}}}

    assert await async_unload_entry(hass, entry) is True
    entry.runtime_data.async_cleanup.assert_awaited_once()
    assert f"options-{entry.entry_id}" not in hass.data[DOMAIN]


def _camera(*, stop_raises=None, poll_raises=None):
    client = SimpleNamespace(
        async_stop_motion_polling=AsyncMock(side_effect=poll_raises),
        async_stop_streaming=AsyncMock(side_effect=stop_raises),
    )
    return SimpleNamespace(cancel_camera_extras=MagicMock(), device_client=client)


async def test_one_camera_failing_to_stop_does_not_skip_the_rest(caplog):
    bad = _camera(stop_raises=RuntimeError("stop failed"))
    polls_bad = _camera(poll_raises=RuntimeError("poll stop failed"))
    good = _camera()
    manager = SimpleNamespace(
        device_coordinators={},
        camera_coordinators={"dev1": bad, "dev2": polls_bad, "dev3": good},
        client=SimpleNamespace(async_cleanup=AsyncMock()),
    )

    await AidotDeviceManagerCoordinator.async_cleanup(manager)

    good.device_client.async_stop_streaming.assert_awaited_once()
    good.cancel_camera_extras.assert_called_once()
    # A failed motion-poll stop still stops that camera's stream.
    polls_bad.device_client.async_stop_streaming.assert_awaited_once()
    manager.client.async_cleanup.assert_awaited_once()
    assert "dev1" in caplog.text and "dev2" in caplog.text


async def test_the_sd_refresh_press_is_bound_to_the_entry():
    async def _list(*, open_session=False):
        return True

    spawned = []

    def _spawn(hass, coro, name=None):
        spawned.append((hass, name))
        coro.close()

    entry = SimpleNamespace(async_create_background_task=_spawn)
    hass = SimpleNamespace(async_create_background_task=MagicMock())
    coord = SimpleNamespace(async_list_sd_recordings=_list, config_entry=entry)

    await AidotSdRefreshButton.async_press(
        SimpleNamespace(coordinator=coord, hass=hass)
    )

    assert spawned == [(hass, "aidot sd refresh")]
    hass.async_create_background_task.assert_not_called()
