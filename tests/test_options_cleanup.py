"""The options page no longer offers the two not-recommended knobs.

sdes_adaptive was documented as "leave it off unless you want to help test
it" (a fast failure costs ~40 s). The off position of sdes_push is the legacy
pull serve that can jam under Home Assistant. Neither belongs on the settings
page; sdes_push keeps its stored value and code default so an entry that
already turned it off keeps behaving the same.
"""
from custom_components.aidot import config_flow, const


def test_sdes_adaptive_is_gone_entirely():
    assert not hasattr(const, "CONF_SDES_ADAPTIVE")
    assert not hasattr(const, "DEFAULT_SDES_ADAPTIVE")
    src = open(config_flow.__file__).read()
    assert "SDES_ADAPTIVE" not in src


def test_sdes_push_is_off_the_page_but_still_honoured():
    src = open(config_flow.__file__).read()
    assert "CONF_SDES_PUSH" not in src
    # The constant and its default survive for the camera platform.
    assert const.CONF_SDES_PUSH == "sdes_push"
    assert const.DEFAULT_SDES_PUSH is True


def test_camera_no_longer_forwards_sdes_adaptive():
    from custom_components.aidot import camera
    src = open(camera.__file__).read()
    assert "sdes_adaptive" not in src
