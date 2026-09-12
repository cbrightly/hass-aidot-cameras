"""The resolution select is gone on purpose - keep it gone.

Not because the camera ignores the command. It does not, and the first version
of this file said so: with the library able to read AVIO replies, an A000088
acknowledges `SETSTREAMCTRL` (cmd 800) within 0.03 s and reports the new value
back through `GETSTREAMCTRL` (802) - 5 (MIDDLE) at session start, 5 after `sd`,
1 after `hd`. The camera takes the command and remembers it.

It just encodes the same video either way. Measured 2026-08-07 with the setting
verified by read-back first, then recorded and read frame by frame:

    quality 1 (MAX / hd)     728 frames   all 1280x720   2592 bytes/frame
    quality 5 (MIDDLE / sd)  651 frames   all 1280x720   2682 bytes/frame

That correction matters for anyone reading this later. Every earlier check had
been made in the `sd` direction - and `sd` sends 5, the value the camera is
already on - so they only ever showed that setting a camera to its current value
changes nothing. The setting also does not survive a session; each one opens at
the camera's default of 5.

The entity accepted a value, restored it across restarts, and reported a setting
with no effect on the video - it lied persistently, which is worse than not
offering the control. `async_set_resolution` remains in the library: the command
is correct and a future firmware may act on it.

Re-adding this entity means having new evidence that a camera actually changes
its encode. This test is here so that has to be a decision rather than an
oversight.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.aidot.const import DOMAIN
from custom_components.aidot.select import CAMERA_SELECTS


def test_no_resolution_select_is_offered():
    keys = [d.key for d in CAMERA_SELECTS]
    assert "resolution" not in keys, (
        "the camera acks SETSTREAMCTRL and reports the value back, and encodes "
        "the same 1280x720 at the same bytes per frame either way. Re-adding "
        "this needs evidence that an encode actually changes, not just that the "
        "command was accepted"
    )


def test_the_remaining_selects_read_back_from_the_device():
    """Every surviving select reflects device state rather than guessing.

    That is the property the resolution select could not satisfy: no cloud
    readback, no observable effect, so nothing to reflect.
    """
    for d in CAMERA_SELECTS:
        assert d.get_current_option is not None, (
            f"select {d.key!r} has no readback - it can only report what it was "
            "told, which is how the resolution control came to lie"
        )


async def test_the_orphaned_resolution_select_is_removed_from_the_registry(hass):
    """Deleting the entity is not enough - its registry entry outlives it.

    Home Assistant keeps the row for an entity the integration no longer
    creates, and shows it as an unavailable `select.<name>_resolution` that
    still appears in the UI and in any automation that referenced it. Every
    upgrader from 2.11.8 or earlier has one per camera, and asking each of them
    to hunt through Settings -> Entities is not a fix.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.aidot import _migrate_relocated_camera_entities

    reg = er.async_get(hass)
    orphan = reg.async_get_or_create("select", DOMAIN, "cam1_resolution")
    keeper = reg.async_get_or_create("select", DOMAIN, "cam1_night_vision")

    _migrate_relocated_camera_entities(hass)

    assert reg.async_get(orphan.entity_id) is None
    assert reg.async_get(keeper.entity_id) is not None, (
        "only the resolution select is orphaned; the other selects are live"
    )


async def test_another_integrations_resolution_select_is_left_alone(hass):
    """Match on this integration's own rows, not on a name that reads familiar."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.aidot import _migrate_relocated_camera_entities

    reg = er.async_get(hass)
    theirs = reg.async_get_or_create("select", "some_other_camera", "cam9_resolution")

    _migrate_relocated_camera_entities(hass)

    assert reg.async_get(theirs.entity_id) is not None
