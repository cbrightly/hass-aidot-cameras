"""The SD card sensor must say whether there IS a card, not echo a vendor field.

`SDcardStatus` is a raw cloud property whose meaning is not "a card is in the
slot". On an A000088 it reads INVERTED against reality - the library records
"0" with a card and "1" without, 3 of 3 - so a dashboard row labelled "SD card"
showing that number tells a user the opposite of the truth. Measured on the
live fleet 2026-08-13: the camera holding 125 recordings showed 0 and the one
with an empty slot showed 1.

The trustworthy signal is `sd_card_present`, which the media browser has always
used and which is a deliberate tri-state: True in the slot, False slot empty,
None nobody reported. None must stay None so it renders as Unknown rather than
being flattened into "empty" - four of seven cameras report neither key, and
saying "no card" about a camera that never answered is the same class of lie
this sensor is being fixed for.

The raw value is not lost: diagnostics still carries `sd_card_status`.
"""

from custom_components.aidot.sensor import CAMERA_SENSORS


def _sd_description():
    for desc in CAMERA_SENSORS:
        if desc.key == "sd_card_status":
            return desc
    raise AssertionError("no sd_card_status sensor description")


class _Status:
    def __init__(self, present, raw):
        self.sd_card_present = present
        self.sd_card_status = raw


def test_a_card_in_the_slot_reads_present():
    desc = _sd_description()
    # raw "0" is what the camera holding 125 recordings actually reports.
    assert desc.get_value(_Status(True, "0")) == "present"


def test_an_empty_slot_reads_empty():
    desc = _sd_description()
    # raw "1" is what the camera with the empty slot actually reports.
    assert desc.get_value(_Status(False, "1")) == "empty"


def test_nobody_reporting_stays_unknown():
    """None must not become "empty" - it means the camera never said."""
    assert _sd_description().get_value(_Status(None, None)) is None


def test_the_sensor_does_not_echo_the_raw_vendor_field():
    desc = _sd_description()
    for present, raw in ((True, "0"), (False, "1")):
        assert desc.get_value(_Status(present, raw)) != raw


def test_it_is_an_enum_so_the_states_are_translatable():
    desc = _sd_description()
    assert desc.device_class is not None
    assert set(desc.options or ()) == {"present", "empty"}
