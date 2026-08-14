"""Which recording a play identifier means, and which ones it must not.

The browser already uses `/` for cloud play items and `|` for SD browse paths.
An SD play identifier enters that space as a third shape, so the resolver has
to tell it from a cloud uuid without guessing.
"""
from aidot_cameras.camera.sd_events import SdEvent

from custom_components.aidot.sd_playback import (
    find_record,
    parse_sd_play_identifier,
    record_key,
    sd_play_identifier,
)


def _rec(**kw):
    base = dict(year=2026, month=8, day=11, hour=20, minute=41, second=42,
                channel=0, event=1, status=0)
    base.update(kw)
    return SdEvent(**base)


def test_an_identifier_round_trips():
    ident = sd_play_identifier("dev1", _rec())
    assert parse_sd_play_identifier(ident) == ("dev1", record_key(_rec()))


def test_a_cloud_identifier_is_not_an_sd_one():
    # The failure this exists to prevent: a cloud uuid resolving down the SD
    # path, or the reverse, because both contain exactly one slash.
    assert parse_sd_play_identifier("dev1/v1:12345") is None


def test_a_browse_path_is_not_a_play_identifier():
    assert parse_sd_play_identifier("dev1|sd") is None
    assert parse_sd_play_identifier("dev1|sd|2026-08-11") is None


def test_two_events_in_the_same_second_do_not_collide():
    # A timestamp alone is not unique: records carry channel and event too, and
    # two events can share a second on different channels.
    a = record_key(_rec(channel=0, event=1))
    b = record_key(_rec(channel=1, event=1))
    c = record_key(_rec(channel=0, event=2))
    assert len({a, b, c}) == 3


def test_a_key_finds_its_record_and_only_its_record():
    records = [_rec(second=42), _rec(second=43), _rec(second=42, channel=1)]
    found = find_record(records, record_key(_rec(second=43)))
    assert found is not None and found.second == 43


def test_an_unknown_key_finds_nothing():
    # A record that has aged off the card since the page was rendered.
    assert find_record([_rec()], record_key(_rec(year=2020))) is None
