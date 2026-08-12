"""Turning what a card reports into what a browser can show.

The rule this file pins hardest: RECORDS WIN. Measured 2026-08-11, an A000088
returned four real recordings and an all-zero occupancy map for the same window.
A tree built from the map would have shown that user an empty card.
"""
from datetime import UTC, datetime, timedelta, timezone

from aidot_cameras.camera.sd_events import SdEvent

from custom_components.aidot.sd_recordings import (
    SdCache,
    map_days,
    record_days,
    sd_empty_message,
)

# UTC-5, fixed so the test does not move with the machine: a UTC evening is the
# same local day and a UTC small hour is the previous one, which is exactly the
# boundary a UTC-keyed grouping would file under the wrong date.
_TZ = timezone(timedelta(hours=-5))


def _ev(hour, day=11, mi=0):
    return SdEvent(2026, 8, day, hour, mi, 0, 0, 1, 0)


def test_records_group_into_local_days_newest_first():
    days = record_days([_ev(20), _ev(2), _ev(22, mi=30)], _TZ)
    # 02:00 UTC on the 11th is 21:00 local on the 10th.
    assert [d for d, _ in days] == ["2026-08-11", "2026-08-10"]
    assert [r.hour for r in days[0][1]] == [22, 20], "newest first inside a day"


def test_a_day_with_no_records_does_not_appear():
    assert [d for d, _ in record_days([_ev(20)], _TZ)] == ["2026-08-11"]


def test_no_records_is_no_days_not_a_placeholder_day():
    assert record_days([], _TZ) == []


def test_the_map_places_its_bytes_from_the_window_start():
    start = datetime(2026, 8, 10, 0, 0, tzinfo=UTC).timestamp()
    hours = bytearray(48)
    hours[0] = 1     # 2026-08-10 00:00 UTC -> 2026-08-09 19:00 local
    hours[30] = 1    # 2026-08-11 06:00 UTC -> 2026-08-11 01:00 local
    days = map_days(bytes(hours), start, _TZ)
    assert days == [("2026-08-11", [1]), ("2026-08-09", [19])]


def test_a_map_of_all_zeroes_yields_no_days():
    start = datetime(2026, 8, 10, 0, 0, tzinfo=UTC).timestamp()
    assert map_days(bytes(48), start, _TZ) == []


def test_a_cache_is_stale_after_the_ttl_and_not_before():
    cache = SdCache(records=[], hours=None, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=1000.0)
    assert cache.is_stale(1000.0 + 899) is False
    assert cache.is_stale(1000.0 + 901) is True


def test_never_listed_says_how_to_list_it():
    assert "Refresh" in sd_empty_message(None)


def test_a_listed_empty_card_is_not_the_same_as_never_listed():
    # Telling a user to press Refresh when the camera has already answered
    # "nothing here" sends them to retry a request that worked.
    cache = SdCache(records=[], hours=None, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=1.0)
    assert sd_empty_message(cache) != sd_empty_message(None)
    assert "Refresh" not in sd_empty_message(cache)


def test_a_truncated_list_says_it_is_truncated():
    cache = SdCache(records=[_ev(20)], hours=None, complete=False,
                    start_ts=0.0, end_ts=0.0, fetched_at=1.0)
    assert "more" in sd_empty_message(cache).lower()


def test_a_silent_camera_is_not_reported_as_an_empty_card():
    # The distinction the library's `answered` flag exists to carry. Both of
    # these hold an empty record list; only one of them is the camera saying
    # its card is empty.
    silent = SdCache(records=[], hours=None, answered=False, complete=True,
                     start_ts=0.0, end_ts=0.0, fetched_at=1.0)
    empty = SdCache(records=[], hours=None, answered=True, complete=True,
                    start_ts=0.0, end_ts=0.0, fetched_at=1.0)
    assert sd_empty_message(silent) != sd_empty_message(empty)
    assert "did not answer" in sd_empty_message(silent)
    assert "nothing recorded" in sd_empty_message(empty)


def test_the_silent_message_does_not_blame_the_card():
    silent = SdCache(records=[], hours=None, answered=False, complete=True,
                     start_ts=0.0, end_ts=0.0, fetched_at=1.0)
    msg = sd_empty_message(silent)
    assert "nothing recorded" not in msg
    assert "empty" not in msg.lower()
