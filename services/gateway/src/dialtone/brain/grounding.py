"""Checking the agent's numbers against the documents it was given.

THE FAILURE THIS CATCHES. A small model asked "how much for a check-up and a hygienist?" reads
"seventy five dollars" and "ninety five dollars" in the retrieved passages and answers "around a
hundred and seventy". It has not misread anything — it has helpfully synthesised, which is what
these models are for and exactly what a price must never be subjected to.

Prompting reduces it and does not remove it. So the numbers are checked afterwards, against the
passages the agent was actually given, and anything that was not in them is flagged.

WHY FLAG RATHER THAN BLOCK. Regenerating costs a whole turn of latency on a live call, and the
second attempt is not reliably better. Blocking outright turns a slightly-wrong answer into
silence, which is worse. So an unverified number is reported: the caller hears the reply, and the
operator reviewing the call sees exactly which figure had no source. On a transcript that has
been checked, "no unverified numbers" is a meaningful statement.

WHAT IS DELIBERATELY NOT CHECKED. Times of day the agent computes ("we close in twenty minutes"),
counts of things it just said, and quantities the caller supplied. Those are legitimately derived
rather than quoted, and flagging them would train an operator to ignore the flag — which costs
more than the checking is worth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def _key(text: str) -> str:
    """Reduce a spelled-out number to a lookup key: no spaces, hyphens, or joining "and"."""
    return re.sub(r"[\s\-]|\band\b", "", text.lower())


#: Number words the model writes out, mapped back to digits so a spoken "forty five" and a
#: written "45" compare equal. Built from the same table the speech normaliser uses, so the two
#: can never disagree about what a number is called.
#:
#: EVERY value up to 1000, in BOTH dialects, not a hand-picked list. Two versions of this have
#: been wrong in the same way:
#:
#:   listing only the round hundreds it expected meant "two hundred thirty dollars" -- an
#:   entirely invented total -- was reported as clean, because the checker could not read the
#:   number it was meant to be checking
#:   building the table in one dialect meant every price written the other way became unreadable
#:
#: A gap in a safety check is worse than no safety check, because the clean report is believed.
def _both_dialects(n: int) -> list[str]:
    from . import speakable as _sp

    was = _sp.SAY_AND_IN_HUNDREDS
    try:
        _sp.SAY_AND_IN_HUNDREDS = False
        american = _key(_sp.number_to_words(n))
        _sp.SAY_AND_IN_HUNDREDS = True
        british = _key(_sp.number_to_words(n))
    finally:
        _sp.SAY_AND_IN_HUNDREDS = was
    return [american, british]


_WORD_TO_DIGIT: dict[str, int] = {}
for _n in range(1001):
    for _spelling in _both_dialects(_n):
        _WORD_TO_DIGIT[_spelling] = _n

_DIGITS = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")

# Spelled-out numbers. The words allowed after "hundred and" are restricted to actual number
# words rather than any word: an earlier version used `[a-z-]+` there, which greedily swallowed
# the unit, so "one hundred fifty dollars" produced the lookup key "onehundredfiftydollars",
# matched nothing, and an invented price sailed through unflagged.
#
# Alternation order matters throughout — the longest form has to come first, or "twenty five"
# matches as "twenty" and the five is read as a separate number.
_UNITS_RE = (
    "nineteen|eighteen|seventeen|sixteen|fifteen|fourteen|thirteen|twelve|eleven|ten|"
    "nine|eight|seven|six|five|four|three|two|one"
)
_TENS_RE = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_TENS_OR_UNIT = rf"(?:(?:{_TENS_RE})(?:[\s\-](?:{_UNITS_RE}))?|(?:{_UNITS_RE}))"

# The "and" is OPTIONAL, because the two dialects disagree about it: British English says "one
# hundred and eighty", American English says "one hundred eighty". Requiring it split every
# American amount into two numbers -- "nine hundred ninety dollars" read as 900 and 90 -- so a
# price quoted straight out of the documents came back as two figures with no source.
_NUMBER_PHRASE = re.compile(
    rf"\b(?:(?:{_UNITS_RE})\s+hundred(?:\s+(?:and\s+)?{_TENS_OR_UNIT})?|{_TENS_OR_UNIT}|zero)\b",
    re.IGNORECASE,
)

#: Words that mean the model is estimating rather than quoting. On a price, every one of these
#: is a small lie, because the source document was exact.
_HEDGES = re.compile(
    r"\b(?:around|about|approximately|roughly|circa|somewhere near|in the region of|"
    r"give or take|or so|ish)\b",
    re.IGNORECASE,
)

#: Numbers that are almost never a quoted fact. Small counts appear constantly in ordinary
#: sentences ("one moment", "two options") and flagging them would bury the real findings.
_IGNORED = frozenset(range(0, 13))


@dataclass(slots=True)
class Finding:
    value: float
    #: The words around it, so an operator can see what was claimed without opening the call.
    context: str
    kind: str = "unverified_number"

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "context": self.context, "kind": self.kind}


@dataclass(slots=True)
class Grounding:
    """The result of checking one reply against its sources."""

    verified: list[float] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    hedged: bool = False

    @property
    def ok(self) -> bool:
        return not self.findings and not self.hedged

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "hedged": self.hedged,
            "findings": [f.as_dict() for f in self.findings],
        }


def extract_numbers(text: str) -> list[tuple[float, str]]:
    """Every number in the text, digits or words, with the phrase around it."""
    found: list[tuple[float, str]] = []

    for match in _DIGITS.finditer(text):
        try:
            value = float(match.group().replace(",", ""))
        except ValueError:
            continue
        found.append((value, _around(text, match.start(), match.end())))

    for match in _NUMBER_PHRASE.finditer(text):
        key = _key(match.group())
        if key in _WORD_TO_DIGIT:
            found.append((float(_WORD_TO_DIGIT[key]), _around(text, match.start(), match.end())))

    return found


def _around(text: str, start: int, end: int, width: int = 28) -> str:
    return " ".join(text[max(0, start - width):min(len(text), end + width)].split())


def check(reply: str, sources: str) -> Grounding:
    """Which numbers in `reply` appear in `sources`.

    Comparison is on VALUE, not on spelling: the documents say "seventy five dollars", the model
    may write "$75", and those are the same claim. Both sides are reduced to numbers first.
    """
    result = Grounding()
    if not reply.strip():
        return result

    source_values = {value for value, _ in extract_numbers(sources)}

    if _HEDGES.search(reply) and source_values:
        # "Around seventy five dollars" when the document says exactly that. The figure checks
        # out and the sentence is still wrong, because it turns a fixed price into an estimate.
        result.hedged = True

    for value, context in extract_numbers(reply):
        if value in _IGNORED:
            continue
        if value in source_values:
            result.verified.append(value)
        else:
            result.findings.append(Finding(value=value, context=context))

    return result
