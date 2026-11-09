"""The HLS audio option (library AIDOT_PUBLISH_AAC).

With it on, the library publishes an AAC copy of each directly published
camera's audio, and Home Assistant's HLS path asks go2rtc for it by name. On
by default since python-aidot-cameras 1.0.0rc33 starts the AAC track in step
with the picture on a cold start.
"""

import json
from pathlib import Path

from custom_components.aidot import config_flow
from custom_components.aidot.const import CONF_HLS_AUDIO, DEFAULT_HLS_AUDIO


from custom_components.aidot.library_settings import stream_settings


def test_default_is_on():
    assert DEFAULT_HLS_AUDIO is True


def test_option_sets_the_library_switch():
    assert stream_settings({CONF_HLS_AUDIO: True}).publish_aac is True
    # Explicit: the library defaults on.
    assert stream_settings({CONF_HLS_AUDIO: False}).publish_aac is False
    # An entry that never saved the option gets the default: on.
    assert stream_settings({}).publish_aac is True


def test_option_is_exposed_in_the_options_schema_and_strings():
    assert "CONF_HLS_AUDIO" in open(config_flow.__file__).read()
    base = Path(config_flow.__file__).parent
    for name in ("strings.json", "translations/en.json"):
        step = json.loads((base / name).read_text())["options"]["step"]["audio"]
        assert "hls_audio" in step["data"]
        assert "hls_audio" in step["data_description"]
        # Home Assistant rejects braces in these strings as placeholders.
        assert "{" not in step["data_description"]["hls_audio"]
