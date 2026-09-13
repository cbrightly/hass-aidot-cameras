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

from .const import SD_CACHE_TTL_S, SD_UNANSWERED_TTL_S


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
    #: How many listings in a row this camera has answered nothing to. Zero once
    #: it answers. Only used to widen the retry window - never to decide what to
    #: tell a user, because a streak says how often we asked, not what is in the
    #: slot.
    unanswered_streak: int = 0

    def is_stale(self, now: float) -> bool:
        """Is this listing old enough to be worth replacing?

        Asymmetric on purpose. A camera that ANSWERED gave a reading, and
        re-taking it costs a session. A camera that said nothing gave no
        reading at all - the same "we learned nothing" as having had no session,
        which deliberately leaves the cache untouched - so it expires far sooner
        and the next session that happens along retries it.

        Repeated silence backs off, because one silence is probably a moment and
        repeated silence is probably the model. Measured 2026-08-13: an A001064
        answered nothing to five asks over 84 s on a live session, and it is a
        spotlight cam that sees motion, so a flat short window would have it
        re-interrogated on every motion prewarm for the life of the process.
        Capped at the normal TTL so a camera that has never answered never
        becomes MORE durable than a real listing - a card put in later still has
        to be discoverable without a restart.

        The asymmetry lives here rather than at the callers so that every path
        that consults staleness inherits it and none of them can disagree.
        """
        if self.answered:
            ttl: float = SD_CACHE_TTL_S
        else:
            widened = SD_UNANSWERED_TTL_S * 2 ** max(0, self.unanswered_streak - 1)
            ttl = min(widened, SD_CACHE_TTL_S)
        return (now - self.fetched_at) > ttl


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


def sd_card_known_absent(cache: Optional[SdCache],
                         card_present: Optional[bool]) -> bool:
    """Do we actually know there is no card in this camera?

    Shared by the folder's title and the note inside it so the two cannot come
    to different conclusions about the same camera.

    Two guards, and both are load-bearing. ``None`` is not absence: four of the
    seven cameras measured 2026-08-12 report nothing about their slot, including
    an A000088 - the same model as all three that do report - so a missing
    reading says nothing about the hardware. And a camera that ANSWERED outranks
    the flag: it is a cloud attribute that lags a card inserted a moment ago,
    while a reply is first-hand evidence that something was there to reply about.
    """
    return card_present is False and (cache is None or not cache.answered)


def sd_empty_message(cache: Optional[SdCache],
                     card_present: Optional[bool] = None) -> str:
    """Why an on-device folder has nothing in it, in words a user can act on.

    Several conditions produce no items and they call for different actions. An
    empty folder that means four things is how this project's own diagnosis kept
    going in circles.

    ``card_present`` is the library's tri-state reading of the slot, and only
    an explicit False says anything here. None means nobody reported - four of
    seven measured cameras look like that, including an A000088, the same model
    as every camera that does report - so it is NOT evidence that a model cannot
    answer, and must leave the wording exactly as it was.

    There is still deliberately no "this model does not report its card
    contents" case. A listing can only prove support, and silence looks
    identical for a dead channel, a missing session and an empty slot. What the
    silent case CAN say is that we asked and got nothing - true, actionable, and
    it does not blame the card.
    """
    if sd_card_known_absent(cache, card_present):
        # Measured 2026-08-12: an online, healthy A000088 reporting
        # SDcardExistFlag false was being told the camera did not answer and to
        # press Refresh - three wrong claims and an action that cannot help.
        return "There is no SD card in this camera."
    if cache is None:
        return ("Not listed yet - press the 'Refresh on-device recordings' "
                "button, or open the live view.")
    if not cache.answered:
        # Never "nothing is on the card": the camera did not say that, and
        # saying it for them is how an unexplained empty folder gets read as a
        # fact.
        #
        # This used to add "some models do not report their card contents", on
        # the strength of an A001064 that answered nothing to five asks over
        # 84 s. That reading was wrong. The camera did reply; the reply is a few
        # KB and arrives as several SCTP fragments, and the SDES receive path
        # parsed each fragment as a whole frame - so the first was refused as
        # truncated and the rest decoded as noise. Fixed in library 1.0.0b34.
        # No model on the reference fleet is known to lack these commands, so
        # the message no longer sends anyone hunting a model limitation that
        # may not exist.
        return ("The camera did not answer when asked what it holds. It may "
                "have been offline or busy - try Refresh again.")
    if not cache.complete:
        return ("The camera sent a partial list - there may be more on the "
                "card than is shown.")
    return "The camera reports nothing recorded on its card in this window."
