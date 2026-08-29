"""The options page no longer offers the two not-recommended knobs.

sdes_adaptive was documented as "leave it off unless you want to help test
it" (a fast failure costs ~40 s). The off position of sdes_push is the legacy
pull serve that can jam under Home Assistant. Neither belongs on the settings
page; sdes_push keeps its stored value and code default so an entry that
already turned it off keeps behaving the same.
"""
import pathlib

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aidot import config_flow, const
from custom_components.aidot.const import DOMAIN


def test_sdes_adaptive_is_gone_entirely():
    assert not hasattr(const, "CONF_SDES_ADAPTIVE")
    assert not hasattr(const, "DEFAULT_SDES_ADAPTIVE")
    src = pathlib.Path(config_flow.__file__).read_text()
    assert "SDES_ADAPTIVE" not in src


async def test_sdes_push_is_off_the_page_but_still_honoured(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "streaming"})
    assert result["type"] == FlowResultType.FORM
    keys = {str(k) for k in result["data_schema"].schema}
    assert "sdes_push" not in keys
    assert "sdes_adaptive" not in keys
    assert "sdes_fast_liveplay" in keys
    # The constant and its default survive for the camera platform.
    assert const.CONF_SDES_PUSH == "sdes_push"
    assert const.DEFAULT_SDES_PUSH is True


def test_camera_no_longer_forwards_sdes_adaptive():
    from custom_components.aidot import camera
    src = pathlib.Path(camera.__file__).read_text()
    assert "sdes_adaptive" not in src
