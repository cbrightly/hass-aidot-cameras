"""Turning what a card reports into what a browser can show."""

from datetime import timedelta, timezone

from aidot_cameras.camera.sd_events import SdEvent

from custom_components.aidot.sd_recordings import (
    SdCache,
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


def test_a_cache_is_stale_after_the_ttl_and_not_before():
    cache = SdCache(
        records=[],
        complete=True,
        fetched_at=1000.0,
    )
    assert cache.is_stale(1000.0 + 899) is False
    assert cache.is_stale(1000.0 + 901) is True


def test_a_camera_never_listed_is_pointed_at_the_button():
    from custom_components.aidot.sd_recordings import sd_empty_message

    assert sd_empty_message(None) == (
        "Not listed yet - press the 'Refresh on-device recordings' button."
    )


def test_a_camera_that_said_nothing_goes_stale_much_sooner():
    # Measured 2026-08-13. A restart caught a camera mid-something, its
    # piggyback listing came back unanswered, and that got cached for the full
    # 15 minutes - so the folder read "the camera did not answer" on a camera
    # holding 108 recordings that answered on the very next attempt.
    #
    # Silence is not a reading. It is the same "we learned nothing" as having no
    # session at all, and THAT case deliberately leaves the cache untouched so
    # the next session retries. Caching silence for as long as a real listing
    # made the least informative answer the most durable one.
    silent = SdCache(
        records=[],
        answered=False,
        complete=True,
        fetched_at=1000.0,
    )
    assert silent.is_stale(1000.0 + 30) is False
    assert silent.is_stale(1000.0 + 200) is True


def test_silence_still_gets_a_window_so_a_mute_camera_is_not_re_asked_every_open():
    # Not zero. A listing on a camera that never answers costs two AVIO requests
    # and up to 16 s of timeouts, and motion prewarm can open sessions minutes
    # apart, so "always stale" would re-interrogate a genuinely mute camera on
    # every single open - which is what only_if_stale exists to prevent.
    silent = SdCache(
        records=[],
        answered=False,
        complete=True,
        fetched_at=1000.0,
    )
    assert silent.is_stale(1000.0) is False


def test_a_camera_that_answered_keeps_the_full_ttl():
    # The asymmetry is the whole point, so it is pinned from both sides: a real
    # listing - including one that legitimately reports an empty card - is a
    # reading, and re-taking it costs a session.
    answered = SdCache(
        records=[],
        answered=True,
        complete=True,
        fetched_at=1000.0,
    )
    assert answered.is_stale(1000.0 + 200) is False


def test_never_listed_says_how_to_list_it():
    assert "Refresh" in sd_empty_message(None)


def test_a_listed_empty_card_is_not_the_same_as_never_listed():
    # Telling a user to press Refresh when the camera has already answered
    # "nothing here" sends them to retry a request that worked.
    cache = SdCache(records=[], complete=True, fetched_at=1.0)
    assert sd_empty_message(cache) != sd_empty_message(None)
    assert "Refresh" not in sd_empty_message(cache)


def test_a_truncated_list_says_it_is_truncated():
    cache = SdCache(
        records=[_ev(20)],
        complete=False,
        fetched_at=1.0,
    )
    assert "more" in sd_empty_message(cache).lower()


def test_a_silent_camera_is_not_reported_as_an_empty_card():
    # The distinction the library's `answered` flag exists to carry. Both of
    # these hold an empty record list; only one of them is the camera saying
    # its card is empty.
    silent = SdCache(
        records=[],
        answered=False,
        complete=True,
        fetched_at=1.0,
    )
    empty = SdCache(
        records=[],
        answered=True,
        complete=True,
        fetched_at=1.0,
    )
    assert sd_empty_message(silent) != sd_empty_message(empty)
    assert "did not answer" in sd_empty_message(silent)
    assert "nothing recorded" in sd_empty_message(empty)


def test_the_silent_message_does_not_blame_the_card():
    silent = SdCache(
        records=[],
        answered=False,
        complete=True,
        fetched_at=1.0,
    )
    msg = sd_empty_message(silent)
    assert "nothing recorded" not in msg
    assert "empty" not in msg.lower()


def test_no_card_is_said_plainly_instead_of_blaming_the_silence():
    # The measured case. unit 88-B is an A000088, online and healthy, and
    # reported SDcardExistFlag false - the identical model two rooms away
    # returns 25 records. It was being told "the camera did not answer. Some
    # models do not report their card contents", every clause of which is
    # wrong, and the suggested action (press Refresh) can never help.
    silent = SdCache(
        records=[],
        answered=False,
        complete=True,
        fetched_at=1.0,
    )
    msg = sd_empty_message(silent, card_present=False)
    assert "no SD card" in msg
    assert "did not answer" not in msg
    assert "Refresh" not in msg


def test_no_card_is_said_before_anyone_has_listed_anything():
    # A camera with no card should say so the first time it is browsed. Telling
    # someone to press a button, or to open a live view, to discover that their
    # slot is empty spends a camera wake to learn what the cloud already said.
    msg = sd_empty_message(None, card_present=False)
    assert "no SD card" in msg
    assert "Refresh" not in msg


def test_an_unknown_card_keeps_the_wording_it_had():
    # Four of seven cameras report neither key, including an A000088 - the same
    # model as every camera that does report. So absence is NOT "this model
    # cannot say", and a folder that claimed "no SD card" here would be
    # inventing the reading that `answered` exists to prevent one level down.
    silent = SdCache(
        records=[],
        answered=False,
        complete=True,
        fetched_at=1.0,
    )
    assert sd_empty_message(silent, card_present=None) == sd_empty_message(silent)
    assert sd_empty_message(None, card_present=None) == sd_empty_message(None)
    assert "no SD card" not in sd_empty_message(silent, card_present=None)


def test_a_card_that_is_present_changes_nothing():
    # Knowing a card IS there adds no information to any of these messages -
    # the cache already says what came back. Only the absent case is new.
    for cache in (
        None,
        SdCache(
            records=[],
            answered=False,
            complete=True,
            fetched_at=1.0,
        ),
        SdCache(
            records=[],
            answered=True,
            complete=True,
            fetched_at=1.0,
        ),
    ):
        assert sd_empty_message(cache, card_present=True) == sd_empty_message(cache)


def test_a_card_that_answered_is_believed_over_a_stale_absent_flag():
    # The flag is a cloud attribute and can lag reality: a card inserted a
    # moment ago still reads absent. If the camera has actually answered with
    # records, that is first-hand evidence and it wins - and this message is
    # only ever reached when there is nothing to show anyway.
    answered = SdCache(
        records=[],
        answered=True,
        complete=True,
        fetched_at=1.0,
    )
    msg = sd_empty_message(answered, card_present=False)
    assert "nothing recorded" in msg
    assert "no SD card" not in msg


def test_a_signed_thumbnail_url_names_the_recording_and_its_own_route():
    from custom_components.aidot.sd_proxy import SD_THUMB_PATH, sign_sd_thumbnail_url

    url = sign_sd_thumbnail_url("dev1", "sd-abc", now=1000.0)
    assert url.startswith(SD_THUMB_PATH + "?")
    assert "device=dev1" in url and "key=sd-abc" in url and "sig=" in url
