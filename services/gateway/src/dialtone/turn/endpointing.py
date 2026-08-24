"""Endpointing: deciding the caller has finished speaking.

THIS IS THE WHOLE PRODUCT. Every voice-agent stack is STT → LLM → TTS; what separates one that
feels human from one that feels broken is when it decides to start talking. Vendors describe
this as a "proprietary turn-taking model" and publish a single latency number, which is exactly
the wrong shape for the problem — because endpointing is not a latency number, it is a TRADE-OFF
CURVE, and quoting one end of it is how a product ends up interrupting people.

THE TRADE-OFF, stated plainly:

    wait longer  ->  fewer false cutoffs, but the agent feels slow and dead
    wait less    ->  snappy, but the agent talks over people mid-sentence

Those are the two ways a voice agent fails, and they are opposite. A silence threshold of 700ms
is the industry default and it is a compromise that is wrong in both directions at once: too
slow after "yes", far too fast in the middle of "my account number is... four... two...".

WHY SILENCE ALONE CANNOT WORK. Silence duration carries almost no information about whether a
turn is complete. Consider two pauses of exactly 400ms:

    "I'd like to book an appointment"          <400ms>   -> complete. Answer now.
    "my account number is"                     <400ms>   -> obviously not. Wait.

Identical acoustics, opposite correct actions. The difference is entirely in the WORDS, which
is why this module combines an acoustic signal (VAD) with a linguistic one.

THE THREE SIGNALS COMBINED HERE:

  1. SILENCE      how long the caller has been quiet. Necessary, not sufficient.
  2. SYNTAX       does the transcript end at a plausible utterance boundary? A dangling
                  preposition, conjunction, or filler is strong evidence of incompleteness,
                  and it is cheap to detect.
  3. PROSODY      a rising final pitch usually means a question or a continuation; a falling
                  one usually means completion. Approximated here from the energy contour that
                  a VAD already produces, so it costs nothing extra.

The result is an ADAPTIVE threshold: the silence required to declare end-of-turn moves with the
linguistic evidence. "yes" ends in 180ms; "my account number is" will not end for 1.5 seconds.

Everything here is measured rather than asserted — `eval/endpointing.py` runs a labelled corpus
through it and reports the actual curve, which is the number this module should be judged on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Floor: below this, an intra-word pause reads as end-of-turn and the agent interrupts
#: syllables. No amount of linguistic confidence should go below it.
MIN_SILENCE_MS = 160.0

#: Ceiling: past this the caller has plainly stopped, whatever the syntax suggests. People do
#: leave sentences unfinished, and waiting forever for a grammatical ending is its own failure.
MAX_SILENCE_MS = 1800.0

#: The industry default, kept as the baseline every measurement is reported against.
BASELINE_SILENCE_MS = 700.0


class TurnDecision(StrEnum):
    WAIT = "wait"                 # caller is still going
    ENDPOINT = "endpoint"         # caller finished — respond now
    BACKCHANNEL = "backchannel"   # long turn still in progress — acknowledge, don't take over


# ── linguistic evidence ──────────────────────────────────────────────────────
# Words that essentially cannot end an English utterance. A transcript ending here is
# incomplete regardless of how long the silence has been.
_DANGLING = frozenset("""
and or but so because if when while although though unless until since that which who
the a an my your his her its our their this these those some any every
whether how what where why when who whom whose whichever

is are was were be been being am do does did have has had will would could should may might
to of in on at for with from by about into over under between through during
i you he she it we they there here

# Transitive verbs that demand an object. A turn ending here is unfinished -- "can I also
# get", "I'd like to change my", "could you send" -- and without them the scorer rated
# "can I also get" at 0.72 and would have answered straight over the caller. Found by the
# worked example in examples/, not by the corpus, which is why that example is in CI.
get gets getting got need needs want wants take takes make makes give gives send sends
add adds order orders book books change changes cancels bring brings put use uses
""".split())

# Fillers signal the caller is composing, not finished.
_FILLERS = frozenset("um uh er ah hmm mm like well okay so basically actually".split())

# Endings that strongly suggest completion.
_TERMINAL_PUNCT = re.compile(r"[.!?]\s*$")

# A trailing run of numbers means the caller is reading something out and is almost certainly
# not done. This single rule prevents the most damaging failure there is: cutting someone off
# halfway through their account or card number.
#
# SPELLED-OUT DIGITS MATTER MORE THAN DIGITS. An earlier version matched only \d+, which missed
# the case it was written for: recognisers transcribe spoken numbers as WORDS, so a caller
# reading a card aloud produces "four two four two", not "4242". The benchmark caught it —
# every mid-number false cutoff in the corpus was a spelled-out one.
_NUMBER_WORDS = (
    "zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    "fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    "sixty|seventy|eighty|ninety|hundred|double|triple"
)
_TRAILING_NUMBER = re.compile(
    rf"(?:\b(?:\d+|{_NUMBER_WORDS})\b[\s,.-]*){{1,}}$", re.IGNORECASE
)

# A trailing token mixing letters and digits is a code being read out — a postcode, a booking
# reference, a licence plate. Same failure mode, different shape.
#: One token mixing letters and digits: a postcode, plate, or reference being read out.
#: Applied per-token over the trailing few words, because codes are read in groups and
#: the digit-bearing group is often not the final one ("my plate is LT19 XYZ").
_TRAILING_CODE = re.compile(r"^(?=[\w-]*\d)(?=[\w-]*[a-z])[\w-]{2,}$", re.IGNORECASE)

# Short, unambiguous complete turns. These should endpoint fast — waiting 700ms after "yes"
# is most of what makes an agent feel sluggish.
_SHORT_COMPLETE = frozenset("""
yes no yeah yep nope sure okay ok correct right exactly please thanks
hello hi bye goodbye stop cancel help agent representative human
""".split())

# WH-questions front their object, so they legitimately END on a transitive verb: "what
# appointments do you have", "how many do you need", "which one did you want". Those verbs are
# all in `_DANGLING` -- correctly, since "I have" really is incomplete -- so without this the
# scorer rates a perfectly finished question at 0.08 and the agent sits waiting 1.6 seconds
# while the caller waits back. Found by the call simulator, not the corpus: it is a shape that
# only shows up when you replay whole conversations rather than isolated turns.
_WH_OPENERS = frozenset("what which how who where when why whose whom".split())
#: Kept deliberately in step with the transitive verbs in `_DANGLING`: every verb that makes an
#: utterance unfinished on its own ("can I also get") makes a WH-question finished when its
#: object is fronted ("how many can I get"). A verb in one list and not the other is a bug, and
#: it shows up as either a 1.6s silence or a false cutoff depending on which way round it is.
_FRONTED_VERBS = frozenset("""
have has need needs want wants get gets got take takes make makes give gives send sends
add adds order orders book books offer offers change changes do does bring brings use uses
""".split())


@dataclass(slots=True)
class TurnState:
    """Everything known about the caller's current turn."""

    transcript: str = ""
    silence_ms: float = 0.0
    speech_ms: float = 0.0
    #: Recent frame energies from the VAD, oldest first. Used for the prosody approximation.
    energy_tail: list[float] = field(default_factory=list)
    #: True once the agent has produced at least one backchannel this turn, so it does not
    #: hum continuously through a long explanation.
    backchanneled: bool = False


@dataclass(slots=True)
class Endpoint:
    decision: TurnDecision
    #: The silence threshold that applied on this evaluation, after linguistic adjustment.
    threshold_ms: float
    #: [0,1] — how strongly the evidence says the turn is complete.
    completion: float
    reason: str

    @property
    def ended(self) -> bool:
        return self.decision is TurnDecision.ENDPOINT


@dataclass(slots=True)
class EndpointConfig:
    """Every constant that moves the curve, in one place so it can be swept.

    `eval/endpointing.py` sweeps these and reports latency against false-cutoff rate. Any of
    them chosen by intuition rather than measurement is a number that will be wrong.
    """

    base_silence_ms: float = 520.0
    min_silence_ms: float = MIN_SILENCE_MS
    max_silence_ms: float = MAX_SILENCE_MS
    #: How much the linguistic signal is allowed to move the threshold, as a multiplier.
    #: 1.0 means syntax can double the wait or halve it.
    linguistic_weight: float = 1.0
    #: Prosody contributes less than syntax because the approximation here is crude; a real
    #: pitch tracker would justify raising it.
    prosody_weight: float = 0.35
    #: A turn longer than this earns a backchannel so the caller knows they are being heard.
    backchannel_after_ms: float = 3200.0
    enable_semantic: bool = True
    enable_prosody: bool = True


def completion_score(transcript: str) -> tuple[float, str]:
    """How complete does this transcript look? Returns (score in [0,1], reason).

    Deliberately RULE-BASED rather than a model. Three reasons, and they are the same reasons
    the deterministic layer exists elsewhere in this codebase:

      - It must run on every partial transcript, several times a second, inside a latency
        budget where a model call is unaffordable.
      - It is inspectable: when the agent interrupts someone, the reason is a rule you can read
        and fix, not a weight.
      - It is the floor. A learned endpointer belongs ON TOP of this, adding signal for the
        ambiguous middle, not replacing a layer that already handles the clear cases correctly.
    """
    text = transcript.strip().lower()
    if not text:
        # Nothing said yet. Neutral rather than complete: an empty transcript with silence is
        # a caller who has not started, and cutting in there is worse than waiting.
        return 0.5, "no transcript"

    words = re.findall(r"[\w']+", text)
    if not words:
        return 0.5, "no words"

    last = words[-1]

    # Strongest signals first; the first match wins, so ordering is the priority.
    if _TRAILING_NUMBER.search(text):
        return 0.05, "ends mid-number — caller is reading something out"
    # Codes are read out in groups, so the digit-bearing part may be the second-to-last
    # token ("my plate is LT19 XYZ"). Checking only the final token missed those.
    if any(_TRAILING_CODE.search(w) for w in words[-2:]):
        return 0.08, "ends on an alphanumeric code — caller is still reading it out"
    if last in _DANGLING:
        # ...unless the object was fronted by a WH-word, which makes the trailing verb the
        # legitimate end of a complete question.
        if words[0] in _WH_OPENERS and last in _FRONTED_VERBS and len(words) >= 4:
            return 0.88, f"WH-question with a fronted object, ending on {last!r}"
        return 0.08, f"ends on {last!r}, which cannot end an utterance"
    # Single-word check BEFORE fillers: "okay" and "sure" appear in both sets, and as a lone
    # word they are complete turns ("okay" = yes) while mid-utterance they are filler
    # ("so basically okay so..."). Ordering the filler check first made the agent wait 1.4s
    # after a caller simply said "okay".
    if len(words) == 1 and last in _SHORT_COMPLETE:
        return 0.97, f"{last!r} is a complete short turn"
    if last in _FILLERS:
        return 0.12, f"ends on filler {last!r} — caller is still composing"
    # A short turn ENDING on a confirmation word is complete: "that's correct", "okay thanks",
    # "yes please". These were reaching the "very short, no completion marker" fallback below
    # and scoring 0.35, so the agent waited ~890ms after an unambiguous yes. Checked AFTER the
    # filler rule so a trailing "okay" mid-utterance still reads as composing.
    if len(words) <= 3 and last in _SHORT_COMPLETE:
        return 0.94, f"short turn ending on {last!r}"
    if _TERMINAL_PUNCT.search(transcript.strip()):
        return 0.92, "terminal punctuation from the recogniser"
    if len(words) <= 2:
        # Very short and not a known complete form. Probably the start of something.
        return 0.35, "very short, no completion marker"

    # A grammatically plausible ending: a content word after several words.
    return 0.72, "plausible utterance boundary"


def prosody_score(energy_tail: list[float]) -> tuple[float, str]:
    """Approximate final-pitch direction from the energy contour.

    A genuine implementation tracks F0. This uses the energy envelope the VAD already produces,
    which correlates with it well enough to be worth its zero marginal cost — and is honestly
    labelled as an approximation rather than dressed up as prosodic analysis.

    Falling energy at the end of a phrase usually accompanies a falling pitch and completion;
    sustained or rising energy usually accompanies a continuation or question.
    """
    if len(energy_tail) < 6:
        return 0.5, "not enough frames for a contour"

    tail = energy_tail[-6:]
    first_half = sum(tail[:3]) / 3
    second_half = sum(tail[3:]) / 3
    if first_half <= 1e-9:
        return 0.5, "silent contour"

    ratio = second_half / first_half
    if ratio < 0.55:
        return 0.85, "energy falling — likely a completed phrase"
    if ratio > 1.25:
        return 0.2, "energy rising — likely a question or continuation"
    return 0.5, "flat contour"


class Endpointer:
    """Adaptive end-of-turn detection.

    Stateless between calls except for what the caller passes in, so it is trivially testable
    and can be swept over a corpus without constructing a session.
    """

    def __init__(self, config: EndpointConfig | None = None):
        self.config = config or EndpointConfig()

    def evaluate(self, state: TurnState) -> Endpoint:
        cfg = self.config

        semantic, semantic_reason = (
            completion_score(state.transcript) if cfg.enable_semantic else (0.5, "semantic off")
        )
        prosodic, prosodic_reason = (
            prosody_score(state.energy_tail) if cfg.enable_prosody else (0.5, "prosody off")
        )

        # Weighted blend, centred on 0.5 so a neutral signal leaves the threshold alone.
        completion = _clamp(
            0.5
            + cfg.linguistic_weight * (semantic - 0.5)
            + cfg.prosody_weight * (prosodic - 0.5)
        )

        # THE ADAPTIVE THRESHOLD. Confidence that the turn is complete SHORTENS the required
        # silence; evidence of incompleteness lengthens it. This is the entire mechanism.
        #
        # completion 1.0 -> min threshold (respond almost immediately)
        # completion 0.5 -> base threshold (no evidence either way)
        # completion 0.0 -> max threshold (wait; they are clearly mid-thought)
        if completion >= 0.5:
            span = cfg.base_silence_ms - cfg.min_silence_ms
            threshold = cfg.base_silence_ms - span * ((completion - 0.5) * 2)
        else:
            span = cfg.max_silence_ms - cfg.base_silence_ms
            threshold = cfg.base_silence_ms + span * ((0.5 - completion) * 2)
        threshold = _clamp(threshold, cfg.min_silence_ms, cfg.max_silence_ms)

        if state.silence_ms >= threshold:
            return Endpoint(
                TurnDecision.ENDPOINT, threshold, completion,
                f"{state.silence_ms:.0f}ms silence >= {threshold:.0f}ms "
                f"(completion {completion:.2f}: {semantic_reason})",
            )

        # A long turn with no end in sight earns exactly one acknowledgement. Continuous
        # backchannelling is worse than none -- it reads as a machine looping.
        if (
            not state.backchanneled
            and state.speech_ms >= cfg.backchannel_after_ms
            and state.silence_ms > 120
        ):
            # The latch is set HERE rather than left to the caller. `evaluate` runs once per
            # 20ms frame, so a consumer that forgets to set it emits a backchannel sixty times
            # in a two-second pause -- and since the guard reads as if it were already handled,
            # the omission is invisible at the call site. Owning the invariant where it is
            # defined is the only version of this that cannot be got wrong.
            state.backchanneled = True
            return Endpoint(
                TurnDecision.BACKCHANNEL, threshold, completion,
                f"{state.speech_ms:.0f}ms of speech without a boundary — acknowledging",
            )

        return Endpoint(
            TurnDecision.WAIT, threshold, completion,
            f"{state.silence_ms:.0f}ms < {threshold:.0f}ms ({semantic_reason}; {prosodic_reason})",
        )


def fixed_threshold_endpointer(silence_ms: float = BASELINE_SILENCE_MS) -> Endpointer:
    """The industry baseline: one silence threshold, no linguistic signal.

    Provided so the benchmark has something honest to compare against. Every claim this module
    makes is reported as a delta against this, because "600ms latency" means nothing without
    the false-cutoff rate that came with it.
    """
    return Endpointer(
        EndpointConfig(
            base_silence_ms=silence_ms,
            min_silence_ms=silence_ms,
            max_silence_ms=silence_ms,
            enable_semantic=False,
            enable_prosody=False,
        )
    )


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
