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

_COMPONENT = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "aidot"
)
_PLATFORMS = (
    "sensor",
    "switch",
    "button",
    "select",
    "number",
    "binary_sensor",
    "update",
)
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
        for const in re.findall(
            r"translation_key\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", text
        ):
            for value in re.findall(
                rf'^{re.escape(const)}\s*(?::[^=]+)?=\s*"([a-z0-9_]+)"', text, re.M
            ):
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
            "strings.json only": sorted(
                set(a.get(section, {})) - set(b.get(section, {}))
            ),
            "en.json only": sorted(set(b.get(section, {})) - set(a.get(section, {}))),
        }
        for section in set(a) | set(b)
        if set(a.get(section, {})) != set(b.get(section, {}))
    }
    assert not drift, f"translation drift: {drift}"


def test_the_two_files_describe_the_same_repairs_issues():
    """strings.json is the maintained source; translations/en.json is what a
    user actually sees. A Repairs issue (title/description) defined in only
    one of them is either lost on regeneration or shown as its bare key."""
    a, b = (json.loads((_COMPONENT / f).read_text()).get("issues", {}) for f in _FILES)
    assert a == b, "strings.json and translations/en.json issues must be identical"


# --- every language file agrees with en.json ---------------------------------

_TRANSLATIONS = _COMPONENT / "translations"
_LANGUAGE_FILES = sorted(p.name for p in _TRANSLATIONS.glob("*.json"))
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def _strings(node: dict, path: tuple[str, ...] = ()) -> dict[str, str]:
    """Flatten a translation file to {"dotted.key.path": "text"}."""
    out: dict[str, str] = {}
    for key, value in node.items():
        if isinstance(value, dict):
            out.update(_strings(value, (*path, key)))
        else:
            assert isinstance(value, str), f"{'.'.join((*path, key))} is not a string"
            out[".".join((*path, key))] = value
    return out


def test_the_expected_languages_ship():
    assert set(_LANGUAGE_FILES) >= {
        "de.json",
        "en.json",
        "es.json",
        "fr.json",
        "it.json",
        "nl.json",
        "pt.json",
    }


@pytest.mark.parametrize("filename", _LANGUAGE_FILES)
def test_language_file_is_ascii_json(filename):
    """Non-ASCII characters are written as \\uXXXX escapes, so the bytes on
    disk stay pure ASCII like every other file in the repository."""
    raw = (_TRANSLATIONS / filename).read_bytes()
    assert raw.isascii(), f"{filename} holds non-ASCII bytes"
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert isinstance(json.loads(raw), dict)


@pytest.mark.parametrize("filename", _LANGUAGE_FILES)
def test_language_file_has_the_same_keys_as_english(filename):
    """A missing key shows English (or the bare key) in the middle of a
    translated dialog; an extra one is a string nothing displays."""
    english = _strings(json.loads((_TRANSLATIONS / "en.json").read_text()))
    other = _strings(json.loads((_TRANSLATIONS / filename).read_text()))
    assert sorted(set(english) - set(other)) == [], f"{filename} lacks keys"
    assert sorted(set(other) - set(english)) == [], f"{filename} has extra keys"
    # Same order too, so a diff between two language files lines up.
    assert list(other) == list(english)
    empty = sorted(key for key, text in other.items() if not text.strip())
    assert not empty, f"{filename} has empty strings: {empty}"


@pytest.mark.parametrize("filename", _LANGUAGE_FILES)
def test_language_file_keeps_every_placeholder(filename):
    """Home Assistant fills {name}-style placeholders by name; one that is
    dropped, added or renamed in a translation breaks that string at runtime."""
    english = _strings(json.loads((_TRANSLATIONS / "en.json").read_text()))
    other = _strings(json.loads((_TRANSLATIONS / filename).read_text()))
    wrong = {
        key: {
            "en": sorted(_PLACEHOLDER.findall(english[key])),
            filename: sorted(_PLACEHOLDER.findall(text)),
        }
        for key, text in other.items()
        if key in english
        and sorted(_PLACEHOLDER.findall(text))
        != sorted(_PLACEHOLDER.findall(english[key]))
    }
    assert not wrong, f"placeholder mismatch: {wrong}"
