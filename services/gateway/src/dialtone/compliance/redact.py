"""Redaction on a streaming transcript, before anything is stored or sent to a model.

THE CONSTRAINT THAT MAKES THIS HARD. On a call, sensitive data arrives one word at a time and is
spoken, not typed. Both halves of that break the usual approach:

  STREAMING     By the time you can see "4242 4242 4242 4242" is a card number, the first half
                has already been logged, sent to the model, and written to the transcript. So
                redaction cannot run on the final transcript — it has to run on the partial, and
                it has to be willing to retroactively redact text it already emitted.
  SPOKEN        Nobody says "4242424242424242". They say "four two four two, four two four two"
                or "double four, two, double four" — and every card-number regex ever written
                fails on all of it. A redactor that only catches digits catches nothing that
                matters, while reporting a clean compliance record.

The second point is the one that gets skipped, and it is why this module normalises spoken
number words to digits BEFORE matching. That normalisation is also where the subtle bug lives —
see `_normalise_digits` and the comment about word boundaries, which cost a real debugging
session in a sibling project.

WHAT IS AND IS NOT REDACTED. Card numbers, CVVs, and government IDs are removed from everything:
transcript, logs, model context. The agent cannot see them either — a model that never receives
a PAN cannot leak one, which is a far stronger guarantee than instructing it not to repeat one.
Names, addresses and phone numbers are TAGGED but retained, because an agent that cannot see the
caller's own address cannot help them with a delivery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Sensitivity(StrEnum):
    #: Removed entirely. Never stored, never sent to a model, never logged.
    STRIP = "strip"
    #: Kept, but marked so it can be filtered per-destination.
    TAG = "tag"


@dataclass(slots=True, frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    sensitivity: Sensitivity
    #: Extra check beyond the pattern. Luhn for cards: without it, any 16 consecutive digits —
    #: an order number, a phone number with an extension — gets destroyed, and an agent that
    #: cannot read back an order number is useless.
    validator: str = ""
    placeholder: str = ""

    def redact_to(self) -> str:
        return self.placeholder or f"[{self.name.upper()}]"


_NUMBER_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

#: Spoken shorthand. "double four" is two 4s, and a redactor that misses it misses roughly a
#: third of how people actually read card numbers aloud.
_MULTIPLIERS: dict[str, int] = {"double": 2, "triple": 3, "treble": 3}


def _normalise_digits(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Rewrite spoken numbers as digits, keeping a map back to the original span.

    Returns (normalised_text, spans) where spans[i] is the (start, end) slice of the ORIGINAL
    text that produced character i of the output. The map is what makes redaction possible at
    all: matching happens on the normalised form, but the text that must actually be destroyed
    is the caller's original wording.

    THE BUG THIS FUNCTION IS WRITTEN AROUND. The obvious implementation replaces each number
    word with its digit and drops the following space, because "four two" should become "42".
    Do that unconditionally and "four two four two four two four two and my name is..." becomes
    "42424242and my name is" — the word boundary before "and" is gone, so every subsequent
    pattern anchored with \\b silently stops matching. Cards then pass through unredacted while
    the compliance log reports a clean run, which is the worst possible failure mode for this
    file. Hence `_next_is_digit_word`: the space is only dropped between two number words.
    """
    tokens = re.split(r"(\s+)", text)
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    pending_multiplier = 1

    def emit(chunk: str, start: int, end: int) -> None:
        for _ in chunk:
            spans.append((start, end))
        out.append(chunk)

    for position, token in enumerate(tokens):
        start = cursor
        cursor += len(token)
        end = cursor
        bare = token.strip().lower().strip(".,;:!?-")

        if bare in _MULTIPLIERS:
            pending_multiplier = _MULTIPLIERS[bare]
            continue

        if bare in _NUMBER_WORDS:
            emit(_NUMBER_WORDS[bare] * pending_multiplier, start, end)
            pending_multiplier = 1
            continue

        if token.isspace():
            # Only collapse whitespace BETWEEN two number words. Collapsing it everywhere
            # destroys the word boundary that every later pattern depends on.
            if _next_is_digit_word(tokens, position) and out and out[-1][-1:].isdigit():
                continue
            emit(token, start, end)
            continue

        pending_multiplier = 1
        emit(token, start, end)

    return "".join(out), spans


def _next_is_digit_word(tokens: list[str], position: int) -> bool:
    """Is the next non-space token something that becomes a digit?"""
    for token in tokens[position + 1:]:
        if token.isspace():
            continue
        bare = token.strip().lower().strip(".,;:!?-")
        return bare in _NUMBER_WORDS or bare in _MULTIPLIERS or bare.isdigit()
    return False


def luhn(digits: str) -> bool:
    """The Luhn check. Every real card passes it; ~90% of random digit runs do not.

    This is what separates "the caller read their card number" from "the caller read their order
    number", and without it the redactor is unusable — it would strip the order numbers the
    agent needs to do its job.
    """
    digits = re.sub(r"\D", "", digits)
    if not 12 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


RULES: tuple[Rule, ...] = (
    Rule("card", re.compile(r"\b(?:\d[ -]?){12,19}\b"), Sensitivity.STRIP, validator="luhn"),
    # A CVV is only identifiable from context — three digits are otherwise unremarkable — so the
    # rule requires the cue word. Stripping every 3-digit run would destroy dates and quantities.
    Rule("cvv", re.compile(r"\b(?:cvv|cvc|security code|card code)\b\D{0,12}(\d{3,4})\b",
                           re.IGNORECASE), Sensitivity.STRIP),
    Rule("ssn", re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b"), Sensitivity.STRIP),
    Rule("sort_code", re.compile(r"\b\d{2}-\d{2}-\d{2}\b"), Sensitivity.STRIP),
    Rule("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), Sensitivity.TAG),
    Rule("phone", re.compile(r"\+?\d[\d\s().-]{8,}\d"), Sensitivity.TAG),
    Rule("postcode", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"), Sensitivity.TAG),
    Rule("dob", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"), Sensitivity.TAG),
)


@dataclass(slots=True)
class Finding:
    rule: str
    sensitivity: Sensitivity
    #: Span in the ORIGINAL text, not the normalised form.
    start: int
    end: int
    #: Never the value itself. A findings list that carries the card number is not a compliance
    #: record, it is a second copy of the breach.
    preview: str = ""


@dataclass(slots=True)
class Redaction:
    text: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def stripped(self) -> list[Finding]:
        return [f for f in self.findings if f.sensitivity is Sensitivity.STRIP]

    @property
    def clean(self) -> bool:
        return not self.stripped


def redact(text: str, rules: tuple[Rule, ...] = RULES) -> Redaction:
    """Redact one piece of text. Spoken numbers included.

    Matching runs on the normalised form so "four two four two…" is caught, but the spans are
    mapped back so the caller's ACTUAL wording is what gets replaced. Redacting the normalised
    text and returning that would silently rewrite everything the caller said into digits.
    """
    normalised, spans = _normalise_digits(text)
    findings: list[Finding] = []
    cuts: list[tuple[int, int, str]] = []
    candidates: list[tuple[Rule, int, int]] = []

    for rule in rules:
        for match in rule.pattern.finditer(normalised):
            # A rule with a capture group targets the group (the CVV digits), not the cue word.
            group = 1 if match.re.groups else 0
            value = match.group(group)
            if rule.validator == "luhn" and not luhn(value):
                continue

            lo, hi = match.span(group)
            if not spans or lo >= len(spans):
                continue
            start = spans[lo][0]
            end = spans[min(hi, len(spans)) - 1][1]
            # `\d[ -]?` lets a match end on its trailing separator, which would make the
            # placeholder collide with the next word ("[CARD]and my name is").
            while end > start and text[end - 1].isspace():
                end -= 1

            candidates.append((rule, start, end))

    # Overlap resolution, and it is a compliance requirement rather than tidiness. The `phone`
    # rule matches inside a card number, so without this the findings record carries a TAG
    # finding whose `preview` holds the last four digits of the PAN -- a second copy of exactly
    # the thing the STRIP rule just removed. STRIP always wins its span.
    strips = [(s0, e0) for r, s0, e0 in candidates if r.sensitivity is Sensitivity.STRIP]
    for rule, start, end in candidates:
        if rule.sensitivity is Sensitivity.TAG and any(
            start < se and ss < end for ss, se in strips
        ):
            continue
        findings.append(Finding(
            rule=rule.name, sensitivity=rule.sensitivity, start=start, end=end,
            # Last four only, and only for TAG rules. A STRIP finding carries nothing.
            preview="" if rule.sensitivity is Sensitivity.STRIP else _preview(text[start:end]),
        ))
        if rule.sensitivity is Sensitivity.STRIP:
            cuts.append((start, end, rule.redact_to()))

    # Right-to-left so earlier offsets stay valid as the string shrinks.
    out = text
    for start, end, replacement in sorted(cuts, key=lambda c: -c[0]):
        out = out[:start] + replacement + out[end:]

    findings.sort(key=lambda f: f.start)
    return Redaction(text=out, findings=findings)


def _preview(value: str) -> str:
    value = value.strip()
    return value if len(value) <= 4 else "…" + value[-4:]


class StreamingRedactor:
    """Redaction over a growing partial transcript.

    THE RETROACTIVE PROBLEM. A partial that reads "my card is four two four two" is not yet a
    card number — it is four digits, which could be anything. Six words later it is a full PAN,
    and the earlier partial has already been emitted. So `feed` returns the redaction of the
    WHOLE transcript so far, and `dirty` reports when a previously-emitted prefix has become
    sensitive in hindsight, so the consumer can retract it.

    `safe_for_model` is the value that goes to the LLM. It is intentionally the only accessor
    that exists, because the failure mode this class prevents is somebody reaching for the raw
    transcript "just for logging" — which is exactly how a PAN ends up in a log aggregator.
    """

    def __init__(self, rules: tuple[Rule, ...] = RULES) -> None:
        self.rules = rules
        self._raw = ""
        self._last_clean_length = 0
        self._dirty = False

    def feed(self, partial: str) -> Redaction:
        self._raw = partial
        result = redact(partial, self.rules)
        # A prefix we already emitted has turned out to contain something sensitive.
        self._dirty = bool(result.stripped) and any(
            f.start < self._last_clean_length for f in result.stripped
        )
        if result.clean:
            self._last_clean_length = len(partial)
        return result

    @property
    def dirty(self) -> bool:
        """True when an already-emitted prefix must be retracted downstream."""
        return self._dirty

    @property
    def safe_for_model(self) -> str:
        return redact(self._raw, self.rules).text

    def reset(self) -> None:
        self._raw = ""
        self._last_clean_length = 0
        self._dirty = False
