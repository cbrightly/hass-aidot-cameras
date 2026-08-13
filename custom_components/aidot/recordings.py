"""Fetching cloud recordings, in the shapes the browser needs them.

Separate from media_source.py on purpose. The rules here are about the AiDot
cloud - a page is capped at 10 items whatever we ask for, a window's true count
comes back even on a one-item page - and they are worth testing without Home
Assistant's browse types in the way.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, tzinfo
from typing import Optional

from .const import CLOUD_PAGE_SIZE, MAX_EVENTS_PER_DAY

_LOGGER = logging.getLogger(__name__)


def day_windows(
    now_ms: int, days: int, tz: tzinfo
) -> list[tuple[str, int, int]]:
    """``days`` local-midnight windows ending with the one containing now.

    Returns ``(label, start_ms, end_ms)`` newest first. Local, not UTC: a user
    browsing "2026-08-11" means their day, and a UTC split would file the
    evening's events under tomorrow.
    """
    now = datetime.fromtimestamp(now_ms / 1000, tz=tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[tuple[str, int, int]] = []
    for back in range(days):
        start = midnight - timedelta(days=back)
        end = start + timedelta(days=1)
        out.append((
            start.strftime("%Y-%m-%d"),
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        ))
    return out


async def count_for_window(dc, start_ms: int, end_ms: int) -> Optional[int]:
    """The window's true event count, in one request.

    Returns None when the count could not be obtained. That is not zero: "the
    server did not answer" and "the day is empty" lead a UI to opposite
    conclusions, and collapsing them is a defect this project has had to fix
    before.
    """
    fn = getattr(dc, "async_count_cloud_recordings", None)
    if fn is None:
        return None
    try:
        return await fn(start_ms, end_ms)
    except Exception as exc:
        _LOGGER.debug("cloud count failed: %s", exc)
        return None


async def events_for_window(
    dc, start_ms: int, end_ms: int
) -> tuple[list[dict], Optional[int]]:
    """Events up to the ceiling, and the window's TRUE total.

    Returns ``(events, total)``. ``total`` is the server's own count, so a
    caller can say "newest 200 of 1517" rather than showing 200 and implying
    that is all there is. It is None when the count could not be obtained -
    a caller must not render that as a complete listing.

    Pages because the server serves at most ten at a time, and stops on a short
    page: the server is the authority on what it will actually serve. An
    exception mid-paging returns what was collected alongside the true total,
    so a partial fetch reads as partial rather than as a short day.
    """
    total = await count_for_window(dc, start_ms, end_ms)
    events: list[dict] = []
    page = 1
    while len(events) < MAX_EVENTS_PER_DAY:
        try:
            batch = await dc.async_get_cloud_recordings(
                start_ms, end_ms, page=page, page_size=CLOUD_PAGE_SIZE)
        except Exception as exc:
            _LOGGER.debug("cloud page %d failed: %s", page, exc)
            break
        if not batch:
            break
        events.extend(batch)
        if len(batch) < CLOUD_PAGE_SIZE:
            break
        page += 1
    return events[:MAX_EVENTS_PER_DAY], total


async def day_summaries(
    dc, windows: list[tuple[str, int, int]]
) -> list[tuple[str, int, int, Optional[int]]]:
    """``(label, start_ms, end_ms, count)`` per window, empty days dropped.

    A day whose count is unknown is KEPT, with None - dropping it would hide a
    day that may well hold recordings because one request failed. Only a day
    the server said is empty is dropped.

    Counted concurrently: seven days is seven independent single requests, and
    doing them in series is seven round trips of latency for no reason.
    """
    # return_exceptions so one bad day cannot empty the whole tree. Today
    # count_for_window swallows everything itself, which makes this look
    # redundant - but that is exactly the kind of safety that disappears the
    # moment someone tightens the swallow, and the failure would be the entire
    # recordings folder vanishing rather than one day going unknown.
    counts = await asyncio.gather(
        *(count_for_window(dc, s, e) for _label, s, e in windows),
        return_exceptions=True,
    )
    counts = [None if isinstance(n, BaseException) else n for n in counts]
    return [(label, s, e, n)
            for (label, s, e), n in zip(windows, counts) if n is None or n > 0]


def empty_cloud_message(plan: dict | None, now_ms: int, tz: tzinfo) -> str:
    """Why this camera's cloud folder is empty, in words a user can act on.

    Deliberately does NOT claim to detect a shared-home account. Nothing in
    the API has been shown to report that, and the failure being fixed is a
    user staring at an empty folder with no idea why - naming the account as
    the thing to check is enough, and inventing a detection would not be
    honest.

    ``tz`` is required rather than defaulted for the same reason
    ``day_windows`` requires it: Home Assistant often runs in a UTC container
    while the user lives somewhere else, and a date rendered on the host's
    clock can be a day out. The caller passes Home Assistant's configured
    zone.
    """
    end = (plan or {}).get("endTime")
    if end and end < now_ms:
        when = datetime.fromtimestamp(end / 1000, tz=tz).strftime("%Y-%m-%d")
        return f"No cloud events - the cloud recording plan expired {when}."
    return (
        "No cloud events in this window. Cloud events are visible only to "
        "the home owner's account; a shared-home member sees none."
    )
