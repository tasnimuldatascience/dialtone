"""What the agent knows about the call so far.

A SLIDING WINDOW OF RAW TURNS IS NOT MEMORY. It is the obvious implementation and it fails in
two directions at once: it forgets things that were said twelve turns ago and were the whole
point of the call, while faithfully carrying "um, hang on" from thirty seconds ago. A real
transcript showed both failures inside four turns:

    caller:  will you be available tomorrow morning
    agent:   I'm sorry, I don't have access to scheduling information
    caller:  I'll be available tomorrow morning
    agent:   Can you tell me when exactly tomorrow morning starts?

The day was given twice and lost twice. Then the agent asked for a phone number and an email,
was given both, and did nothing with either -- because nothing was holding them.

SO MEMORY IS TWO THINGS, KEPT SEPARATELY:

  FACTS      A small set of named values -- who is calling, what they want, when, how to reach
             them. Extracted once, held for the whole call, and put in front of the model on
             every turn so it CANNOT ask twice. Deterministic where the value has a shape worth
             validating; a phone number is not a thing to ask a 1.5B model to remember.
  NARRATIVE  The recent turns verbatim, and a running summary of everything before them. A long
             call stays inside the context window without the beginning of it vanishing.

WHAT IS EXTRACTED FROM SPEECH AND WHAT IS NOT. Speech recognition mangles exactly the values that
must be exact. The same call produced "tasty mulasson" for a surname and "abc iphone com" for an
email address. So names, emails and phone numbers are accepted from TYPED input and only guessed
from speech when the shape is unmistakable -- and a guessed value is marked as such, so the agent
reads it back for confirmation instead of booking on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..scheduling.calendar import When, parse_when


@dataclass(slots=True)
class Fact:
    """One thing the agent knows, and how confident it is entitled to be."""

    value: str
    #: "typed" from a form, "spoken" guessed from the transcript, "given" stated in structured
    #: form. Only typed values are trusted enough to book on without reading back.
    source: str = "spoken"
    #: The turn it was learned on, for the call record.
    turn: int = 0

    @property
    def confirmed(self) -> bool:
        return self.source in ("typed", "confirmed")

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source, "confirmed": self.confirmed}


#: The values a booking needs. Named here rather than inferred, because "what does this call
#: still need?" has to be answerable at any moment -- it is what the agent asks for next.
BOOKING_FIELDS = ("name", "phone", "email", "reason")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
#: Ten or eleven digits, however they are grouped or spoken.
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
_NAME = re.compile(
    r"\b(?:my name is|this is|it's|i'm|i am|speaking is|name's)\s+"
    r"([A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2})",
    re.IGNORECASE,
)

#: Why they are calling. Matched on the thing itself rather than on a phrase, because people say
#: "my tooth hurts", "I need a cleaning" and "it's about a filling" and mean the same kind of
#: thing by all three.
_REASONS = {
    "check-up": ("check up", "checkup", "check-up", "routine", "exam", "examination"),
    "cleaning": ("cleaning", "clean", "hygienist", "hygiene", "scale", "polish"),
    "filling": ("filling", "cavity", "hole", "chipped", "broke", "broken"),
    "emergency": ("emergency", "urgent", "pain", "painful", "hurts", "hurting", "ache",
                  "aching", "toothache", "swollen", "swelling", "knocked out", "bleeding",
                  "abscess", "throbbing"),
    #: "my tooth" on its own says why they rang without saying what they need. Worth capturing --
    #: it is the commonest opening there is -- but as the vaguest category, so a later, more
    #: specific word still wins.
    "dental issue": ("tooth", "teeth", "gum", "gums", "crown", "molar", "wisdom"),
    "consultation": ("consultation", "consult", "new patient", "register", "advice"),
}


@dataclass(slots=True)
class CallMemory:
    """Everything the agent has learned on this call."""

    today: date
    facts: dict[str, Fact] = field(default_factory=dict)
    #: Timing accumulated across turns rather than re-read from the latest one. "Tomorrow
    #: morning" and "how about ten" are one request delivered in two parts, and treating the
    #: second as a fresh one is what made the agent ask when tomorrow morning starts.
    when: When = field(default_factory=When)
    #: The slot currently on the table, once one has been offered and understood.
    proposed_slot: str = ""
    #: Whether the caller has actually agreed to it. Kept as state rather than read off the
    #: latest turn, because agreement and the details often arrive in either order -- they say
    #: "yes, that works" and then type their phone number, or type it first and then say yes.
    #: A booking that only fires on the turn containing the "yes" loses half of those calls.
    slot_confirmed: bool = False
    booked_reference: str = ""
    #: Turns older than the verbatim window, compressed to a few lines.
    summary: str = ""
    turn: int = 0

    # -- learning ----------------------------------------------------------
    def observe(self, text: str) -> list[str]:
        """Read one caller turn. Returns the names of anything newly learned."""
        self.turn += 1
        learned: list[str] = []

        # Timing is merged, never replaced.
        heard = parse_when(text, self.today)
        if heard.day or heard.hour is not None or heard.part:
            before = (self.when.day, self.when.hour, self.when.part)
            self.when = self.when.merge(heard)
            if (self.when.day, self.when.hour, self.when.part) != before:
                learned.append("when")

        for name, value in _extract(text).items():
            # A typed value is never overwritten by a spoken guess. The form is the authority on
            # anything with a shape, because recognition mangles precisely those values.
            existing = self.facts.get(name)
            if existing and existing.confirmed:
                continue
            if existing and existing.value == value:
                continue
            self.facts[name] = Fact(value=value, source="spoken", turn=self.turn)
            learned.append(name)

        return learned

    def tell(self, name: str, value: str, source: str = "typed") -> None:
        """Record a value the caller supplied directly, which outranks anything heard."""
        cleaned = value.strip()
        if cleaned:
            self.facts[name] = Fact(value=cleaned, source=source, turn=self.turn)
        else:
            self.facts.pop(name, None)

    # -- reading -----------------------------------------------------------
    def get(self, name: str) -> str:
        fact = self.facts.get(name)
        return fact.value if fact else ""

    @property
    def missing(self) -> list[str]:
        """What a booking still needs, in the order worth asking for it."""
        return [f for f in BOOKING_FIELDS if not self.get(f)]

    @property
    def unconfirmed(self) -> list[str]:
        """Values guessed from speech that have not been read back or typed.

        Booking on these is how "tasty mulasson" ends up in a patient record.
        """
        return [n for n, f in self.facts.items() if not f.confirmed and n in ("name", "phone", "email")]

    @property
    def ready_to_book(self) -> bool:
        """Everything present, confirmed, and pointed at a real slot the caller agreed to."""
        return (
            bool(self.proposed_slot)
            and self.slot_confirmed
            and not self.missing
            and not self.unconfirmed
        )

    def as_prompt(self) -> str:
        """What the model is told it already knows.

        Put in front of it on EVERY turn. The whole reason this class exists is that a model
        cannot be relied on to remember something from nine turns ago, and asking a caller for
        their phone number twice is the single most obvious way to sound like a machine.
        """
        lines: list[str] = []
        if self.summary:
            lines.append(f"Earlier in this call: {self.summary}")

        known = [f"{name} is {fact.value}" for name, fact in self.facts.items() if fact.value]
        if self.when.day:
            known.append(f"they want {self.when.day.strftime('%A %d %B')}")
        if self.when.hour is not None:
            known.append(f"at {self.when.hour:02d}:{self.when.minute:02d}")
        elif self.when.part:
            known.append(f"in the {self.when.part}")

        if known:
            lines.append(
                "Already established, do NOT ask for any of it again: " + "; ".join(known) + "."
            )
        if self.proposed_slot:
            lines.append(f"The appointment being discussed is {self.proposed_slot}.")
        if self.booked_reference:
            lines.append(
                f"The appointment is BOOKED. The reference is {self.booked_reference}. "
                f"Confirm it and do not offer to book again."
            )
        elif self.missing:
            lines.append(f"Still needed before booking: {', '.join(self.missing)}.")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": {n: f.as_dict() for n, f in self.facts.items()},
            "when": {
                "day": self.when.day.isoformat() if self.when.day else None,
                "hour": self.when.hour,
                "minute": self.when.minute,
                "part": self.when.part,
            },
            "proposed_slot": self.proposed_slot,
            "slot_confirmed": self.slot_confirmed,
            "booked_reference": self.booked_reference,
            "missing": self.missing,
            "unconfirmed": self.unconfirmed,
            "ready_to_book": self.ready_to_book,
        }


def _extract(text: str) -> dict[str, str]:
    """Pull structured values out of a caller turn.

    Deliberately conservative. A wrong name is worse than no name: the agent reads it back, the
    caller corrects it, and two turns are gone. Nothing here fires on a maybe.
    """
    found: dict[str, str] = {}

    email = _EMAIL.search(text)
    if email:
        found["email"] = email.group().lower()

    phone = _PHONE.search(text)
    if phone:
        digits = re.sub(r"\D", "", phone.group())
        # Ten digits, or eleven starting with the country code. Anything else is a reference
        # number, a date, or the recogniser mangling something.
        if len(digits) == 10 or (len(digits) == 11 and digits.startswith("1")):
            found["phone"] = _format_phone(digits)

    name = _NAME.search(text)
    if name:
        parts = name.group(1).split()
        # Stop at a connector. "my name is Sam Hassan and my number is..." is one sentence
        # carrying two facts, and the pattern happily read the join as a surname -- producing
        # "Sam Hassan And", which the agent would then read back to the caller.
        cut = len(parts)
        for index, word in enumerate(parts):
            if word.lower() in _CONNECTORS:
                cut = index
                break
        parts = parts[:cut]

        # Guard against "I'm looking for", "this is regarding" and friends -- the pattern is
        # cheap and these are the words that follow it most often.
        if parts and parts[0].lower() not in _NOT_A_NAME:
            found["name"] = " ".join(w.capitalize() for w in parts)

    # WORD boundaries, not substrings. Matching anywhere meant "sam@example.com" set the
    # appointment reason to a check-up, because "example" contains "exam" -- an email address
    # silently deciding what the patient was coming in for.
    words = set(re.findall(r"[a-z'-]+", text.lower()))
    for reason, cues in _REASONS.items():
        if any(
            (cue in words) if " " not in cue else (f" {cue} " in f" {text.lower()} ")
            for cue in cues
        ):
            found["reason"] = reason
            break

    return found


#: Words that end a name rather than continue it.
_CONNECTORS = frozenset("""
and but so then also plus with from for my our your the a an is was calling here speaking
""".split())

_NOT_A_NAME = frozenset("""
looking calling wondering hoping trying interested regarding about after ringing phoning here
just still not sure sorry afraid
""".split())


def _format_phone(digits: str) -> str:
    if len(digits) == 11:
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def summarise(turns: list[tuple[str, str]], limit: int = 3) -> str:
    """Compress older turns into a couple of lines.

    Extractive rather than generated: a model call per turn to summarise the turns is a cost on
    every single exchange, and what a summary is FOR here is stopping the beginning of the call
    from falling out of the window. The caller's own words do that adequately.
    """
    said = [caller.strip() for caller, _ in turns if len(caller.split()) >= 3]
    if not said:
        return ""
    if len(said) <= limit:
        return " ".join(f"they said {s!r}." for s in said)
    # The opening matters most -- it is why they rang -- and so does the most recent context.
    keep = [said[0], *said[-(limit - 1):]]
    return " ".join(f"they said {s!r}." for s in keep)
