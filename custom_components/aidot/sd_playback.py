"""Which on-device recording a media identifier refers to.

Separate from media_source.py because the rules here are about strings and
collisions, and they are worth testing without Home Assistant's browse types in
the way - the same reason sd_recordings.py is separate.
"""
from __future__ import annotations

from typing import Any, Optional

#: Marks a play identifier as on-device. The browser's play identifiers are
#: `<device>/<something>`, and a cloud event uuid can be any string, so a
#: prefix is what keeps the two apart at the resolver.
SD_PLAY_PREFIX = "sd:"


def record_key(record: Any) -> str:
    """A key unique to one recording on one camera.

    NOT the timestamp alone. Records carry a channel and an event type, and two
    events can share a second on different channels, so a timestamp key would
    play the wrong clip on a multi-channel camera.
    """
    return (f"{record.year:04d}{record.month:02d}{record.day:02d}"
            f"T{record.hour:02d}{record.minute:02d}{record.second:02d}Z"
            f"-c{record.channel}-e{record.event}")


def sd_play_identifier(device_id: str, record: Any) -> str:
    return f"{device_id}/{SD_PLAY_PREFIX}{record_key(record)}"


def parse_sd_play_identifier(identifier: str) -> Optional[tuple[str, str]]:
    """Split an SD play identifier, or None if it is not one.

    Returns None rather than guessing for a cloud play identifier or an SD
    browse path, so the resolver can route on the answer instead of on a
    heuristic.
    """
    if "|" in identifier or "/" not in identifier:
        return None
    device_id, _, rest = identifier.partition("/")
    if not device_id or not rest.startswith(SD_PLAY_PREFIX):
        return None
    key = rest[len(SD_PLAY_PREFIX):]
    return (device_id, key) if key else None


def find_record(records, key: str) -> Optional[Any]:
    """The record a key names, or None if the card no longer lists it."""
    for record in records or ():
        if record_key(record) == key:
            return record
    return None
