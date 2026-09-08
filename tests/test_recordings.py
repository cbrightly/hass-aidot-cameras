"""The cloud recording fetch layer.

Kept out of media_source.py so the query rules - the server's 10-item page cap,
the per-day windows, the truncation ceiling - can be tested without Home
Assistant's browse types in the way.
"""

from datetime import datetime, timedelta, timezone

from custom_components.aidot.recordings import (
    count_for_window,
    day_summaries,
    day_windows,
    empty_cloud_message,
    events_for_window,
)

_NY = timezone(timedelta(hours=-4))   # fixed offset; DST is not what this tests


def test_day_windows_are_local_midnights_newest_first():
    # 2026-08-11 11:30 -04:00
    now = int(datetime(2026, 8, 11, 11, 30, tzinfo=_NY).timestamp() * 1000)
    out = day_windows(now, 3, _NY)
    assert [label for label, _s, _e in out] == [
        "2026-08-11", "2026-08-10", "2026-08-09"]
    # Today's window starts at local midnight and ends at tomorrow's.
    start = datetime.fromtimestamp(out[0][1] / 1000, tz=_NY)
    end = datetime.fromtimestamp(out[0][2] / 1000, tz=_NY)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert end - start == timedelta(days=1)


def test_windows_do_not_overlap_and_leave_no_gap():
    now = int(datetime(2026, 8, 11, 23, 59, tzinfo=_NY).timestamp() * 1000)
    out = day_windows(now, 7, _NY)
    assert len(out) == 7
    for newer, older in zip(out, out[1:]):
        assert older[2] == newer[1], "yesterday must end where today begins"


def test_a_window_covers_the_moment_it_was_built_from():
    now = int(datetime(2026, 8, 11, 0, 0, 1, tzinfo=_NY).timestamp() * 1000)
    label, start, end = day_windows(now, 1, _NY)[0]
    assert start <= now < end


class _FakeClient:
    """A camera client whose cloud holds `n` events for any window.

    Mirrors the server's rule that matters: a page is 10 items whatever
    page_size asks for.
    """

    def __init__(self, n: int):
        self.n = n
        self.calls: list[tuple[int, int]] = []
        self.count_calls = 0

    async def async_get_cloud_recordings(
        self, start_ts, end_ts, *, page=1, page_size=10
    ):
        self.calls.append((page, page_size))
        served = min(10, page_size)
        first = (page - 1) * served
        if first >= self.n:
            return []
        return [
            {"eventUuid": f"v1:{i}", "eventTime": 1_786_000_000_000 + i,
             "eventDesc": "Person", "hasVideo": True, "picUrl": None}
            for i in range(first, min(first + served, self.n))
        ]

    async def async_count_cloud_recordings(self, start_ts, end_ts):
        self.count_calls += 1
        return self.n


async def test_paging_walks_until_the_day_is_exhausted():
    dc = _FakeClient(25)
    events, total = await events_for_window(dc, 0, 1)
    assert len(events) == 25
    assert total == 25
    # 3 pages of 10, 10, 5 - and it stopped rather than asking for a fourth.
    assert [p for p, _s in dc.calls] == [1, 2, 3]


async def test_a_day_that_ends_exactly_on_the_ceiling_matches_its_total():
    # 200 events fetched and a true total of 200: nothing is hidden.
    dc = _FakeClient(200)
    events, total = await events_for_window(dc, 0, 1)
    assert len(events) == 200
    assert total == 200


async def test_a_busy_day_reports_a_total_bigger_than_the_fetch():
    dc = _FakeClient(1000)
    events, total = await events_for_window(dc, 0, 1)
    assert len(events) == 200
    assert total == 1000


async def test_a_short_page_ends_the_loop_even_short_of_the_ceiling():
    dc = _FakeClient(12)
    events, total = await events_for_window(dc, 0, 1)
    assert len(events) == 12
    assert total == 12


async def test_count_reports_what_the_window_holds():
    dc = _FakeClient(37)
    assert await count_for_window(dc, 0, 1) == 37


async def test_counting_a_day_costs_one_request_not_one_per_ten():
    # The whole point: a folder title must not page the day to know its size.
    # A 1517-event day counted by paging is 152 requests, and a seven-day tree
    # fires hundreds every time it is opened.
    dc = _FakeClient(1517)
    count = await count_for_window(dc, 0, 1)
    assert count == 1517
    assert dc.count_calls == 1
    assert dc.calls == [], "counting must not fetch a page of events"


async def test_a_client_without_a_count_method_counts_none_not_zero():
    class _Boom:
        async def async_get_cloud_recordings(self, *a, **k):
            raise RuntimeError("cloud down")

    assert await count_for_window(_Boom(), 0, 1) is None
    events, total = await events_for_window(_Boom(), 0, 1)
    assert events == [] and total is None


async def test_a_window_whose_count_is_unknown_is_none_not_zero():
    class _NoCount:
        async def async_count_cloud_recordings(self, s, e):
            return None

        async def async_get_cloud_recordings(self, *a, **k):
            return []

    assert await count_for_window(_NoCount(), 0, 1) is None


async def test_a_day_with_an_unknown_count_is_kept_not_dropped():
    # Dropping it would hide a day that may hold recordings because one
    # request failed.
    class _NoCount:
        async def async_count_cloud_recordings(self, s, e):
            return None

    out = await day_summaries(_NoCount(), [("2026-08-11", 0, 1)])
    assert len(out) == 1 and out[0][3] is None


_NOW = 1_786_460_000_000          # 2026-08-11
_ACTIVE = {"subscribeStatus": 1, "endTime": 1_787_781_600_000}   # 2026-08-26
_LAPSED = {"subscribeStatus": 1, "endTime": 1_785_000_000_000}   # in the past


def test_an_active_plan_points_at_the_account():
    msg = empty_cloud_message(_ACTIVE, _NOW, _NY)
    assert "owner" in msg.lower()
    assert "expired" not in msg.lower()


def test_a_lapsed_plan_says_so_with_its_date():
    msg = empty_cloud_message(_LAPSED, _NOW, _NY)
    assert "expired" in msg.lower()
    assert "2026-07-25" in msg, msg


def test_an_unknown_plan_still_says_something_useful():
    # The plan call can fail on its own; that must not turn into a blank
    # folder, which is the exact failure this whole message exists to end.
    msg = empty_cloud_message(None, _NOW, _NY)
    assert msg
    assert "owner" in msg.lower()


def test_the_message_never_claims_to_know_the_account_type():
    # Nothing in the API reports whether this login is a shared-home member.
    # The message names the account as the thing to check; it must not
    # assert that this install IS one.
    msg = empty_cloud_message(_ACTIVE, _NOW, _NY)
    assert "you are" not in msg.lower()
    assert "this account is a" not in msg.lower()


def test_the_expiry_date_is_rendered_in_the_given_zone_not_the_hosts():
    # 2026-03-01 02:30 UTC is still 2026-02-28 in New York. A host running in
    # UTC - which is how Home Assistant usually runs - would print the wrong
    # day to a user who lives in the zone the integration is configured for.
    expired_utc_2026_03_01_0230 = 1772332200000
    now = expired_utc_2026_03_01_0230 + 86_400_000
    msg = empty_cloud_message(
        {"subscribeStatus": 1, "endTime": expired_utc_2026_03_01_0230},
        now, _NY)
    assert "2026-02-28" in msg, msg


class _RaisingCount:
    """A client whose count call raises rather than returning None.

    `count_for_window` has a try/except around that call and nothing exercised
    it: the existing double omits the method entirely, which takes the getattr
    branch instead. Those are different lines, and only one of them was covered.
    """

    async def async_count_cloud_recordings(self, start_ts, end_ts):
        raise RuntimeError("cloud refused")

    async def async_get_cloud_recordings(self, *a, **k):
        return []


async def test_a_count_that_raises_is_none_not_an_exception():
    assert await count_for_window(_RaisingCount(), 0, 1) is None


async def test_one_days_failure_does_not_empty_the_whole_tree(monkeypatch):
    # day_summaries gathers every day concurrently. Without return_exceptions a
    # single raising day takes down the entire recordings folder instead of
    # leaving that one day unknown.
    #
    # This has to patch count_for_window itself. Going through a raising CLIENT
    # does not reach the gather at all - count_for_window catches that on its
    # own, so such a test passes with or without the fix and pins nothing. The
    # first version of this test did exactly that.
    import custom_components.aidot.recordings as rec

    async def _boom(dc, start_ms, end_ms):
        raise RuntimeError("cloud refused")

    monkeypatch.setattr(rec, "count_for_window", _boom)
    windows = [("2026-08-11", 0, 1), ("2026-08-10", 1, 2)]
    out = await rec.day_summaries(object(), windows)
    assert len(out) == 2
    assert [row[3] for row in out] == [None, None]
