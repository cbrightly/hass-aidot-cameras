"""Every declared translation_key must have a name, in both string files.

CI validates that these files parse, not that they agree. That let three sensor
names ship in `translations/en.json` while `strings.json` -- the maintained
source a regeneration or a hassfest check reads -- still knew only the three
older ones, so the new sensors would have rendered as `wifi_ssid` and
`sd_card_total` and non-English locales had nothing to translate against.
"""
import json
import pathlib
import re

import pytest

_COMPONENT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "aidot"
_PLATFORMS = ("sensor", "switch", "button", "select", "number", "binary_sensor")
_FILES = ("strings.json", "translations/en.json")


def _declared() -> dict[str, set[str]]:
    """translation_keys the platform modules declare.

    Both the literal `translation_key="x"` form and the constant one
    (`_KEY = "x"` then `translation_key=_KEY`), so a key defined via a constant
    is not silently treated as absent.
    """
    out: dict[str, set[str]] = {}
    for plat in _PLATFORMS:
        src = _COMPONENT / f"{plat}.py"
        if not src.exists():
            continue
        text = src.read_text()
        keys = set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"', text))
        for const in re.findall(r'translation_key\s*=\s*([A-Za-z_][A-Za-z0-9_]*)', text):
            for value in re.findall(rf'^{re.escape(const)}\s*(?::[^=]+)?=\s*"([a-z0-9_]+)"',
                                    text, re.M):
                keys.add(value)
        if keys:
            out[plat] = keys
    return out


@pytest.mark.parametrize("filename", _FILES)
def test_every_declared_key_has_a_name(filename):
    data = json.loads((_COMPONENT / filename).read_text())
    entity = data.get("entity", {})
    missing = {
        plat: sorted(keys - set(entity.get(plat, {})))
        for plat, keys in _declared().items()
        if keys - set(entity.get(plat, {}))
    }
    assert not missing, f"{filename} has no name for: {missing}"


def test_the_two_files_describe_the_same_entities():
    """Drift either way is a bug: a name only in en.json is lost on regeneration,
    and one only in strings.json advertises an entity that does not exist."""
    a, b = (json.loads((_COMPONENT / f).read_text()).get("entity", {}) for f in _FILES)
    drift = {
        section: {
            "strings.json only": sorted(set(a.get(section, {})) - set(b.get(section, {}))),
            "en.json only": sorted(set(b.get(section, {})) - set(a.get(section, {}))),
        }
        for section in set(a) | set(b)
        if set(a.get(section, {})) != set(b.get(section, {}))
    }
    assert not drift, f"translation drift: {drift}"
