"""Four switches for settings that were readable but had no control.

Each maps to an attribute read off a live A000088 and proven to accept a write
and hold it - OSDEnable was toggled and restored on hardware before any of this
was written. That mattered: probing the wider attribute set showed the camera
acks writes it then ignores (StreamType, spkNSLevel kept their own value) and
also accepts values it should reject (VideoAngle took 7). An ack is not
acceptance on this firmware, so only attributes with a demonstrated read-back
get a switch.

They are CONFIG-category: settings a user changes rarely, not controls that
belong on a dashboard beside the stream.
"""

from homeassistant.helpers.entity import EntityCategory

from custom_components.aidot.switch import CAMERA_SWITCHES

_NEW = {"osd_timestamp", "auto_light", "voice_prompts", "hdr"}


def _by_key():
    return {d.key: d for d in CAMERA_SWITCHES}


def test_all_three_exist():
    missing = _NEW - set(_by_key())
    assert not missing, f"missing switches: {sorted(missing)}"


def test_each_reads_its_own_status_field():
    class _S:
        osd_timestamp = True
        auto_light = False
        voice_prompts = True
        hdr = None

    for key, expected in (("osd_timestamp", True), ("auto_light", False),
                          ("voice_prompts", True), ("hdr", None)):
        assert _by_key()[key].get_is_on(_S()) is expected


def test_unknown_stays_unknown():
    """None must not become False - a camera that never reported is not 'off'."""
    class _S:
        osd_timestamp = None
        auto_light = None
        voice_prompts = None
        hdr = None

    for key in _NEW:
        assert _by_key()[key].get_is_on(_S()) is None


async def test_on_and_off_call_the_right_library_setter():
    calls = []

    class _C:
        async def async_set_osd_timestamp(self, v): calls.append(("osd", v)); return True
        async def async_set_auto_light(self, v): calls.append(("light", v)); return True
        async def async_set_voice_prompts(self, v): calls.append(("voice", v)); return True
        async def async_set_hdr(self, v): calls.append(("hdr", v)); return True

    d = _by_key()
    c = _C()
    for key, tag in (("osd_timestamp", "osd"), ("auto_light", "light"),
                     ("voice_prompts", "voice"), ("hdr", "hdr")):
        await d[key].async_turn_on_fn(c)
        await d[key].async_turn_off_fn(c)
        assert (tag, True) in calls and (tag, False) in calls


def test_they_are_config_not_dashboard_clutter():
    for key in _NEW:
        assert _by_key()[key].entity_category is EntityCategory.CONFIG
