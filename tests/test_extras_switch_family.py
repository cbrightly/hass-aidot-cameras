"""The camera-reported switches are one class, parametrised per family.

Sound detectors, detection types and the automatic siren differ in where the
camera's answer lands, how the write goes out and what the entities are
called; everything else is the same read-the-cache, write-then-read-back
switch. These tests pin each family's settings and the one write path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.aidot.switch import (
    EXTRAS_SWITCHES,
    AidotAutoAlarmSwitch,
    AidotDetectionTypeSwitch,
    AidotExtrasSwitch,
    AidotSoundDetectionSwitch,
)


def _coord(extras=None):
    client = SimpleNamespace(
        info=SimpleNamespace(
            dev_id="cam", model_id="A000088.x", name="Cam", mac="", hw_version=""
        ),
        async_set_sound_detection=AsyncMock(return_value=True),
        async_set_detection_type=AsyncMock(return_value=True),
        async_set_auto_alarm=AsyncMock(return_value=True),
    )
    return SimpleNamespace(
        device_client=client,
        camera_extras=extras or {},
        config_entry=None,
        async_refresh_camera_extras=AsyncMock(),
    )


def test_the_three_families_are_the_three_classes_in_add_order():
    assert EXTRAS_SWITCHES == (
        AidotSoundDetectionSwitch,
        AidotDetectionTypeSwitch,
        AidotAutoAlarmSwitch,
    )
    assert all(issubclass(cls, AidotExtrasSwitch) for cls in EXTRAS_SWITCHES)
    assert [cls.family.extras_key for cls in EXTRAS_SWITCHES] == [
        "sound",
        "detect",
        "alarm",
    ]
    assert [cls.family.unique_prefix for cls in EXTRAS_SWITCHES] == [
        "sound",
        "detect",
        "auto_alarm",
    ]
    # Only the sound detectors take a key the camera reports but we did not name.
    assert [cls.family.accept_unknown_keys for cls in EXTRAS_SWITCHES] == [
        True,
        False,
        False,
    ]


def test_ids_and_translation_keys_per_family():
    sound = AidotSoundDetectionSwitch(_coord(), "glass_Break")
    assert sound.unique_id == "cam_sound_glass_Break"
    assert sound._attr_translation_key == "sound_glass_break"

    detect = AidotDetectionTypeSwitch(_coord(), "humanDetect")
    assert detect.unique_id == "cam_detect_humanDetect"
    assert detect._attr_translation_key == "detect_human"

    alarm = AidotAutoAlarmSwitch(_coord(), "autoAlarm")
    assert alarm.unique_id == "cam_auto_alarm_autoAlarm"
    assert alarm._attr_translation_key == "auto_siren"


def test_an_unnamed_sound_detector_is_named_from_its_key():
    sw = AidotSoundDetectionSwitch(_coord(), "smoke_T9")
    assert sw.unique_id == "cam_sound_smoke_T9"
    assert getattr(sw, "_attr_translation_key", None) is None
    assert sw._attr_name == "Smoke T9"


def test_the_other_families_keep_to_the_keys_they_name():
    with pytest.raises(KeyError):
        AidotDetectionTypeSwitch(_coord(), "publicZone")
    with pytest.raises(KeyError):
        AidotAutoAlarmSwitch(_coord(), "other")


def test_is_on_reads_the_family_slot_and_is_unknown_until_the_camera_answers():
    extras = {"sound": {"glass_Break": True}, "detect": {"humanDetect": False}}
    assert AidotSoundDetectionSwitch(_coord(extras), "glass_Break").is_on is True
    assert AidotDetectionTypeSwitch(_coord(extras), "humanDetect").is_on is False
    assert AidotAutoAlarmSwitch(_coord(extras), "autoAlarm").is_on is None


@pytest.mark.parametrize(
    ("cls", "key", "setter", "only"),
    [
        (AidotSoundDetectionSwitch, "dog_bark", "async_set_sound_detection", "sound"),
        (AidotDetectionTypeSwitch, "petDetect", "async_set_detection_type", "detect"),
        (AidotAutoAlarmSwitch, "humanDetect", "async_set_auto_alarm", "alarm"),
    ],
)
async def test_a_write_goes_through_the_family_setter_and_reads_back_only_its_slot(
    cls, key, setter, only
):
    coord = _coord()
    sw = cls(coord, key)
    sw.async_write_ha_state = lambda: None
    await sw.async_turn_on()
    getattr(coord.device_client, setter).assert_awaited_once_with(key, True)
    coord.async_refresh_camera_extras.assert_awaited_once_with(only=only)
    await sw.async_turn_off()
    getattr(coord.device_client, setter).assert_awaited_with(key, False)
