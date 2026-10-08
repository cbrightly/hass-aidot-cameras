"""The HLS audio option (library AIDOT_PUBLISH_AAC).

With it on, the library publishes an AAC copy of each directly published
camera's audio, and Home Assistant's HLS path asks go2rtc for it by name. On
by default since python-aidot-cameras 1.0.0rc33 starts the AAC track in step
with the picture on a cold start.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

from custom_components.aidot import config_flow
from custom_components.aidot.const import CONF_HLS_AUDIO, DEFAULT_HLS_AUDIO


def _apply(options):
    """Run just the env-var block of async_setup_entry for this option."""
    if options.get(CONF_HLS_AUDIO, DEFAULT_HLS_AUDIO):
        os.environ["AIDOT_PUBLISH_AAC"] = "1"
    else:
        os.environ.pop("AIDOT_PUBLISH_AAC", None)


def test_default_is_on():
    assert DEFAULT_HLS_AUDIO is True


def test_option_sets_and_clears_the_library_switch():
    with patch.dict(os.environ, {}, clear=False):
        _apply({CONF_HLS_AUDIO: True})
        assert os.environ["AIDOT_PUBLISH_AAC"] == "1"
        _apply({CONF_HLS_AUDIO: False})
        assert "AIDOT_PUBLISH_AAC" not in os.environ
        # An entry that never saved the option gets the default: on.
        _apply({})
        assert os.environ["AIDOT_PUBLISH_AAC"] == "1"


def test_setup_entry_carries_the_same_block():
    import custom_components.aidot as init_mod

    src = open(init_mod.__file__).read()
    assert "CONF_HLS_AUDIO, DEFAULT_HLS_AUDIO" in src
    assert 'os.environ["AIDOT_PUBLISH_AAC"] = "1"' in src
    assert 'os.environ.pop("AIDOT_PUBLISH_AAC", None)' in src


def test_option_is_exposed_in_the_options_schema_and_strings():
    assert "CONF_HLS_AUDIO" in open(config_flow.__file__).read()
    base = Path(config_flow.__file__).parent
    for name in ("strings.json", "translations/en.json"):
        step = json.loads((base / name).read_text())["options"]["step"]["audio"]
        assert "hls_audio" in step["data"]
        assert "hls_audio" in step["data_description"]
        # Home Assistant rejects braces in these strings as placeholders.
        assert "{" not in step["data_description"]["hls_audio"]
