"""The appointment book: what is free, what is taken, and turning speech into a time.

WHY THIS EXISTS. Without it the agent is a search engine with a voice. A real call transcript
made the gap obvious -- the caller asked for an appointment, the agent collected their name,
phone number and email, and then had nowhere to put any of it:

    caller:  will you be available tomorrow morning
    agent:   I'm sorry, but I don't have access to real-time scheduling information.

Everything the caller wanted was on the other side of that sentence.

TWO DESIGN DECISIONS DO THE WORK.

AVAILABILITY GOES IN THE PROMPT, NOT BEHIND A TOOL CALL. A 1.5B model asked to emit a structured
tool call gets it right often enough to demo and not often enough to ship, and a missed tool call
looks exactly like the transcript above. Real open slots are cheap to compute and small to
express, so they are simply given to the model as fact. It cannot fail to call them.

BOOKING IS DECIDED BY CODE, NOT BY THE MODEL. Confirming an appointment is the one irreversible
thing on the call. The model proposes -- it says a time back to the caller in its own words --
and this module decides whether that time is real, free, and unambiguous. A model that
hallucinates Thursday at nine cannot create a Thursday at nine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

#: The practice's week. Weekdays only, with a lunch break -- the same hours the knowledge base
#: describes, because an agent that offers a slot its own documents deny is worse than one that
#: cannot offer slots at all.
OPEN_HOUR = time(8, 30)
CLOSE_HOUR = time(18, 0)
LUNCH = (time(12, 0), time(13, 0))
#: Thursday runs late.
LATE_DAY = 3
LATE_CLOSE = time(20, 0)

#: When "evening" starts. Used by BOTH the filter and the spoken form, because a caller told
#: "Thursday evening" and then offered "five in the afternoon" assumes they were misunderstood.
EVENING_FROM = 17

SLOT_MINUTES = 30
#: How far ahead the agent will offer. Beyond a fortnight a caller is guessing anyway, and a
#: longer window makes the prompt bigger for no benefit.
HORIZON_DAYS = 14


@dataclass(slots=True, frozen=True)
class Slot:
    start: datetime

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=SLOT_MINUTES)

    @property
    def iso(self) -> str:
        return self.start.isoformat(timespec="minutes")

    def spoken(self, today: date) -> str:
        """How a receptionist would say this slot out loud.

        Relative where a person would be relative. "Tomorrow at ten thirty" is what someone says;
        "Tuesday the third of March at 10:30" is what a screen shows, and reading it aloud is
        the fastest way to sound like a machine.
        """
        day = self.start.date()
        if day == today:
            when = "today"
        elif day == today + timedelta(days=1):
            when = "tomorrow"
        elif (day - today).days < 7:
            when = self.start.strftime("%A")
        else:
            when = self.start.strftime("%A the ") + _ordinal(day.day)
        return f"{when} at {_spoken_time(self.start.time())}"


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _spoken_time(t: time) -> str:
    hour = t.hour % 12 or 12
    names = {0: "", 15: " fifteen", 30: " thirty", 45: " forty five"}
    minute = names.get(t.minute, f" {t.minute:02d}")
    # The same boundaries the filter uses. Having them disagree produced "Thursday evening" ->
    # "Thursday at five in the afternoon", which sounds like the agent misheard.
    part = ("in the morning" if t.hour < 12
            else "in the afternoon" if t.hour < EVENING_FROM
            else "in the evening")
    return f"{_number(hour)}{minute} {part}"


_NUMBERS = ("zero one two three four five six seven eight nine ten eleven twelve").split()


def _number(n: int) -> str:
    return _NUMBERS[n] if n < len(_NUMBERS) else str(n)


def day_slots(day: date) -> list[Slot]:
    """Every slot the practice could offer on this day, before bookings are removed."""
    if day.weekday() >= 5:
        return []

    close = LATE_CLOSE if day.weekday() == LATE_DAY else CLOSE_HOUR
    out: list[Slot] = []
    cursor = datetime.combine(day, OPEN_HOUR)
    end = datetime.combine(day, close)

    while cursor + timedelta(minutes=SLOT_MINUTES) <= end:
        if not (LUNCH[0] <= cursor.time() < LUNCH[1]):
            out.append(Slot(cursor))
        cursor += timedelta(minutes=SLOT_MINUTES)
    return out


def available(
    taken: set[str], *, today: date, days: int = HORIZON_DAYS, now: datetime | None = None,
) -> list[Slot]:
    """Open slots from now to the horizon.

    `taken` is the set of booked ISO start times, which is the only thing this needs from the
    database -- keeping it a plain set means the calendar can be tested without one.
    """
    now = now or datetime.now()
    out: list[Slot] = []
    for offset in range(days):
        for slot in day_slots(today + timedelta(days=offset)):
            # An hour of notice. Offering a slot that starts in ten minutes is a booking nobody
            # can keep, and the caller has to ring back to cancel it.
            if slot.start <= now + timedelta(hours=1):
                continue
            if slot.iso not in taken:
                out.append(slot)
    return out


# ── understanding when the caller means ──────────────────────────────────────
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}

_WORD_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "noon": 12, "midday": 12,
}

_TIME = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|o'?clock)?\b", re.IGNORECASE
)
_WORD_TIME = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midday)"
    r"(?:\s+(thirty|fifteen|forty five|o'?clock))?\s*(am|pm|in the morning|in the afternoon|"
    r"in the evening)?\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class When:
    """What the caller said about timing, as much of it as was understood."""

    day: date | None = None
    hour: int | None = None
    minute: int = 0
    #: "morning" / "afternoon" / "evening" when they gave a part of day rather than a time.
    part: str | None = None

    @property
    def specific(self) -> bool:
        """Enough to name one slot, rather than a range to offer from."""
        return self.day is not None and self.hour is not None

    def merge(self, later: When) -> When:
        """Combine with something said afterwards, newest winning.

        Callers arrive at a time across several turns -- "tomorrow morning", then "how about ten"
        -- and treating each turn as a fresh request loses the day they already gave. That is
        what produced "can you tell me when exactly tomorrow morning starts?" on a real call: the
        day was there, and the turn that named the hour had thrown it away.
        """
        return When(
            day=later.day or self.day,
            hour=later.hour if later.hour is not None else self.hour,
            minute=later.minute if later.hour is not None else self.minute,
            # A named hour supersedes a vague part of day, so "ten" after "morning" does not stay
            # pinned to the morning band once it is precise.
            part=later.part or (None if later.hour is not None else self.part),
        )


def parse_when(text: str, today: date) -> When:
    """Turn "tomorrow morning" or "ten thirty on Thursday" into a day and a time.

    Deliberately partial. A caller who says "sometime next week" has told you something real, and
    a parser that returns nothing unless it understands everything throws that away -- so a day
    with no time, or a time with no day, both come back and the caller is asked for the rest.
    """
    lowered = text.lower()
    when = When()

    # ── the day ──────────────────────────────────────────────────────────
    if "day after tomorrow" in lowered:
        when.day = today + timedelta(days=2)
    elif "tomorrow" in lowered:
        when.day = today + timedelta(days=1)
    elif "today" in lowered or "this afternoon" in lowered or "this morning" in lowered:
        when.day = today
    else:
        for name, index in _WEEKDAYS.items():
            if name in lowered:
                ahead = (index - today.weekday()) % 7
                # "Monday" said on a Monday means next Monday, unless they said "this".
                if ahead == 0:
                    ahead = 7
                if "next" in lowered and ahead < 7:
                    ahead += 7
                when.day = today + timedelta(days=ahead)
                break

    # ── part of day ──────────────────────────────────────────────────────
    for part in ("morning", "afternoon", "evening"):
        if part in lowered:
            when.part = part
            break

    # ── the time ─────────────────────────────────────────────────────────
    digits = _TIME.search(lowered)
    if digits and not _looks_like_a_date(lowered, digits):
        hour = int(digits.group(1))
        minute = int(digits.group(2) or 0)
        meridiem = (digits.group(3) or "").lower()
        when.hour, when.minute = _to_24h(hour, minute, meridiem, when.part)
    else:
        words = _WORD_TIME.search(lowered)
        if words:
            hour = _WORD_HOURS[words.group(1).lower()]
            minute = {"thirty": 30, "fifteen": 15, "forty five": 45}.get(
                (words.group(2) or "").lower(), 0
            )
            meridiem = (words.group(3) or "").lower()
            when.hour, when.minute = _to_24h(hour, minute, meridiem, when.part)

    return when


def _looks_like_a_date(text: str, match: re.Match[str]) -> bool:
    """Guard against reading a phone number or a date as a time of day."""
    around = text[max(0, match.start() - 4): match.end() + 4]
    return "/" in around or "-" in around or len(match.group(1)) > 2


def _to_24h(hour: int, minute: int, meridiem: str, part: str | None) -> tuple[int, int]:
    """Resolve a bare hour using whatever the caller said around it.

    "Ten" with no am/pm is ten in the morning to a dental practice, because that is when it is
    open -- and guessing wrong here books someone in for the wrong half of the day.
    """
    if "p" in meridiem or part in ("afternoon", "evening"):
        if hour < 12:
            hour += 12
    elif "a" in meridiem or part == "morning":
        if hour == 12:
            hour = 0
    elif hour < 8:
        # Nothing said. The practice opens at 8:30 and shuts at 6, so "four" is the afternoon.
        hour += 12
    return hour, minute


def match_slot(when: When, slots: list[Slot]) -> Slot | None:
    """The one open slot the caller meant, or nothing if it is not clear."""
    if not when.specific:
        return None
    wanted = time(when.hour or 0, when.minute)
    for slot in slots:
        if slot.start.date() == when.day and slot.start.time() == wanted:
            return slot
    return None


def suggest(when: When, slots: list[Slot], limit: int = 3) -> list[Slot]:
    """The best few slots to offer, given whatever the caller has said so far.

    Three at most. More than three cannot be held in memory over the phone, and the caller ends
    up asking for them again -- which costs a whole turn and makes the agent seem inattentive.
    """
    pool = slots
    if when.day:
        same_day = [s for s in pool if s.start.date() == when.day]
        # Falling back to other days rather than saying "nothing" -- a caller who asked for
        # Tuesday will usually take Wednesday, and making them ask is a wasted turn.
        pool = same_day or pool
    if when.part:
        ranges = {"morning": (0, 12), "afternoon": (12, EVENING_FROM), "evening": (EVENING_FROM, 24)}
        low, high = ranges[when.part]
        filtered = [s for s in pool if low <= s.start.hour < high]
        pool = filtered or pool
    return pool[:limit]


def offer_text(slots: list[Slot], today: date) -> str:
    """The available slots, phrased for the system prompt."""
    if not slots:
        return "There is nothing free in the next two weeks."
    return "; ".join(s.spoken(today) for s in slots)


def as_dict(slot: Slot, today: date) -> dict[str, Any]:
    return {"iso": slot.iso, "spoken": slot.spoken(today),
            "date": slot.start.date().isoformat(), "time": slot.start.strftime("%H:%M")}
