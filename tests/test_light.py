"""Tests for AiDot light color-mode tracking on RGBW+CCT bulbs.

Regression coverage for the bug where a dual-mode bulb resting at a color
temperature was reported in HA as its last (stale) RGB color. The device sends
unambiguous single-field deltas (CCT-only or RGBW-only) but an ambiguous
login-sync that returns both registers; the entity must default to color temp
at startup and follow the library's active_color_mode on deltas.
"""

from types import SimpleNamespace

from custom_components.aidot.light import AidotLight

from homeassistant.components.light import ColorMode

# A retained RGB register value the device keeps returning in the ambiguous sync.
_STALE_PURPLE = (157, 0, 137, 0)


def _coord(
    active_color_mode=None,
    cct=3000,
    rgbw=_STALE_PURPLE,
    enable_rgbw=True,
    enable_cct=True,
):
    """Build a stub coordinator sufficient for AidotLight init + _update_status."""
    info = SimpleNamespace(
        dev_id="dev-1",
        model_id="light.A001497",
        name="Stairs Bulb",
        mac="",
        hw_version="",
        enable_rgbw=enable_rgbw,
        enable_cct=enable_cct,
        cct_min=1800,
        cct_max=6500,
    )
    data = SimpleNamespace(
        online=True,
        on=True,
        dimming=255,
        cct=cct,
        rgbw=rgbw,
        active_color_mode=active_color_mode,
    )
    return SimpleNamespace(
        device_client=SimpleNamespace(info=info),
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1"),
    )


def test_ambiguous_sync_at_init_shows_color_temp_not_stale_rgbw():
    # The crux: at startup the sync is ambiguous (active_color_mode None) and the
    # retained rgbw is a stale purple. A dual-mode bulb must default to color
    # temperature, NOT surface the stale RGB color.
    light = AidotLight(_coord(active_color_mode=None, cct=3000, rgbw=_STALE_PURPLE))
    assert light.color_mode == ColorMode.COLOR_TEMP


def test_cct_delta_sets_color_temp():
    light = AidotLight(_coord(active_color_mode="cct"))
    assert light.color_mode == ColorMode.COLOR_TEMP


def test_rgbw_delta_sets_rgbw():
    light = AidotLight(_coord(active_color_mode="rgbw"))
    assert light.color_mode == ColorMode.RGBW


def test_ambiguous_sync_does_not_revert_established_rgbw_mode():
    # Once a color (rgbw) delta established the mode, a later ambiguous sync
    # (active_color_mode None) must NOT revert color_mode away from RGBW.
    coord = _coord(active_color_mode="rgbw")
    light = AidotLight(coord)
    assert light.color_mode == ColorMode.RGBW
    coord.data.active_color_mode = None
    light._update_status()
    assert light.color_mode == ColorMode.RGBW


def test_color_temp_only_bulb_mode_unaffected():
    # A color-temp-only bulb (no RGBW capability) reports COLOR_TEMP and is not
    # touched by the active-mode logic even if data carries an rgbw tuple.
    light = AidotLight(
        _coord(enable_rgbw=False, enable_cct=True, active_color_mode=None)
    )
    assert light.color_mode == ColorMode.COLOR_TEMP
    assert light.supported_color_modes == {ColorMode.COLOR_TEMP}


async def test_command_cct_sets_active_mode_and_survives_a_later_update():
    # Commanding color temp must also set active_color_mode, so a subsequent
    # (possibly ambiguous) coordinator update does not revert the command back to
    # the pre-command mode. Bulb starts resting in rgbw.
    from unittest.mock import AsyncMock, MagicMock

    coord = _coord(active_color_mode="rgbw")
    coord.device_client.async_set_cct = AsyncMock()
    light = AidotLight(coord)
    light.async_write_ha_state = MagicMock()
    await light.async_turn_on(color_temp_kelvin=3000)
    assert coord.data.active_color_mode == "cct"
    assert light.color_mode == ColorMode.COLOR_TEMP
    light._update_status()  # a later refresh must not revert to RGBW
    assert light.color_mode == ColorMode.COLOR_TEMP


async def test_command_rgbw_sets_active_mode_and_survives_a_later_update():
    from unittest.mock import AsyncMock, MagicMock

    coord = _coord(active_color_mode="cct")
    coord.device_client.async_set_rgbw = AsyncMock()
    light = AidotLight(coord)
    light.async_write_ha_state = MagicMock()
    await light.async_turn_on(rgbw_color=(0, 255, 0, 0))
    assert coord.data.active_color_mode == "rgbw"
    assert light.color_mode == ColorMode.RGBW
    light._update_status()  # a later refresh must not revert to COLOR_TEMP
    assert light.color_mode == ColorMode.RGBW
