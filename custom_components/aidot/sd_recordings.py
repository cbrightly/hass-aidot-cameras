"""What a camera's card holds, in the shapes the browser needs it.

Separate from media_source.py for the same reason recordings.py is: the rules
here are about the cards and the clock, and they are worth testing without Home
Assistant's browse types in the way.

The rule that matters most: RECORDS WIN. The HASLISTEVENT occupancy map is a
convenience, never the source of truth. Measured 2026-08-11 - one A000088
returned four real recordings and an all-zero map for the same window, so a tree
built from the map would have told that user the card was empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Optional

from .const import SD_CACHE_TTL_S


@dataclass
class SdCache:
    """One camera's last on-device listing, and when it was taken."""

    records: list = field(default_factory=list)
    hours: Optional[bytes] = None
    #: Did the camera reply to either request? An empty ``records`` is only a
    #: statement about the card when this is True. Carried through from the
    #: library rather than re-derived, because nothing on this side can tell
    #: silence from an empty card after the fact.
    answered: bool = True
    complete: bool = True
    start_ts: float = 0.0
    end_ts: float = 0.0
    fetched_at: float = 0.0

    def is_stale(self, now: float) -> bool:
        return (now - self.fetched_at) > SD_CACHE_TTL_S


def _local(record, tz: tzinfo) -> datetime:
    """A record's UTC stamp on the user's clock.

    The record is UTC - the vendor's own field is named startutctime - and the
    day folders are local, because a user browsing "2026-08-11" means their day.
    """
    return datetime(
        record.year, record.month, record.day,
        record.hour, record.minute, record.second, tzinfo=UTC,
    ).astimezone(tz)


def record_days(records, tz: tzinfo) -> list[tuple[str, list]]:
    """Group records into local day folders, newest day and record first.

    Days with nothing in them do not appear at all - an empty day folder is a
    thing a user has to open to learn nothing, and this browser exists to stop
    exactly that.
    """
    by_day: dict[str, list] = {}
    for record in records:
        try:
            when = _local(record, tz)
        except ValueError:
            # A record whose date cannot exist is a decode that went wrong, and
            # dropping it is better than filing a recording under year 0. The
            # rest of the page is still real.
            continue
        by_day.setdefault(when.strftime("%Y-%m-%d"), []).append((when, record))
    out = []
    for label in sorted(by_day, reverse=True):
        rows = sorted(by_day[label], key=lambda r: r[0], reverse=True)
        out.append((label, [record for _when, record in rows]))
    return out


def map_days(hours: Optional[bytes], start_ts: float,
             tz: tzinfo) -> list[tuple[str, list[int]]]:
    """Local days holding footage, from the occupancy map, newest first.

    Each value is the list of LOCAL hour numbers the card says hold something.
    The map is one byte per hour counting from ``start_ts``, so without the
    window a byte cannot be placed on a clock at all.

    Used only when there are no records. It cannot say how many recordings an
    hour holds and does not pretend to.
    """
    if not hours:
        return []
    start = datetime.fromtimestamp(start_ts, tz=UTC)
    by_day: dict[str, list[int]] = {}
    for index, value in enumerate(hours):
        if not value:
            continue
        when = (start + timedelta(hours=index)).astimezone(tz)
        by_day.setdefault(when.strftime("%Y-%m-%d"), []).append(when.hour)
    return [(label, sorted(by_day[label]))
            for label in sorted(by_day, reverse=True)]


def sd_empty_message(cache: Optional[SdCache]) -> str:
    """Why an on-device folder has nothing in it, in words a user can act on.

    Three conditions produce no items and they call for three different
    actions. An empty folder that means three things is how this project's own
    diagnosis kept going in circles.

    There is deliberately no "this model does not report its card contents"
    case. Nothing available here can establish it: a listing can only prove
    support, and silence looks identical for a dead channel, a missing session
    and an empty slot. What the silent case CAN say is that we asked and got
    nothing - which is true, actionable, and does not blame the card.
    """
    if cache is None:
        return ("Not listed yet - press the 'Refresh on-device recordings' "
                "button, or open the live view.")
    if not cache.answered:
        # Never "nothing is on the card": the camera did not say that, and
        # saying it for them is how an unexplained empty folder gets read as a
        # fact. Some models may not implement these commands at all, and a
        # channel that died looks the same from here - so name both.
        return ("The camera did not answer when asked what it holds. Some "
                "models do not report their card contents; try Refresh again "
                "if the camera was offline.")
    if not cache.complete:
        return ("The camera sent a partial list - there may be more on the "
                "card than is shown.")
    return "The camera reports nothing recorded on its card in this window."
