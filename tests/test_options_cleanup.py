"""The options page no longer offers the two not-recommended knobs.

sdes_adaptive was documented as "leave it off unless you want to help test
it" (a fast failure costs ~40 s). The off position of sdes_push is the legacy
pull serve that can jam under Home Assistant. Neither belongs on the settings
page. 2.19.0 removed the toggle but kept READING the key, which pinned an entry
that had already turned it off to the pull serve with no UI left to undo it;
2.19.3 stops honouring the stored value entirely.
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


async def test_sdes_push_is_off_the_page_and_no_longer_honoured(hass: HomeAssistant, mock_setup_entry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"id": "u"}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "streaming"})
    assert result["type"] == FlowResultType.FORM
    keys = {str(k) for k in result["data_schema"].schema}
    assert "sdes_push" not in keys
    assert "sdes_adaptive" not in keys
    assert "sdes_fast_liveplay" in keys
    # The constants remain only so an old stored key has a name; the camera
    # platform no longer reads them (see test_notify_delivery_and_url_ttl).
    assert const.CONF_SDES_PUSH == "sdes_push"


def test_camera_no_longer_forwards_sdes_adaptive():
    from custom_components.aidot import camera
    src = pathlib.Path(camera.__file__).read_text()
    assert "sdes_adaptive" not in src
