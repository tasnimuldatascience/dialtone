"""The endpointing benchmark.

WHY THIS FILE IS THE POINT OF THE REPOSITORY. Every voice-agent vendor publishes a latency
number. None of them publishes the false-cutoff rate that came with it — and the two are the
same dial. Any latency figure is achievable by lowering the silence threshold; the question is
what it costs, and that question is never asked out loud.

So this measures BOTH, on a labelled corpus, and reports the curve:

    x-axis   false cutoff rate — how often the agent interrupts someone mid-sentence
    y-axis   endpoint latency  — how long after the caller finished before it responds

A system is better only if it moves the whole curve, not if it slides along it. That is the
distinction a single number cannot express and this benchmark exists to make.

THE CORPUS. Each item is a turn with the ground truth of whether the caller had finished at
each pause. Written by hand, from the actual failure cases: numbers read aloud, dangling
prepositions, fillers, short confirmations, and thinking pauses. It is small and honest about
being small — the point is that the methodology is right and the cases are the real ones, not
that 60 items is a definitive evaluation. `EVALUATION.md` says so.

THE BASELINE. Everything is reported against a fixed 700ms threshold, which is what most
stacks ship. A result that does not beat it on both axes is not an improvement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..turn.endpointing import (
    BASELINE_SILENCE_MS,
    EndpointConfig,
    Endpointer,
    TurnDecision,
    TurnState,
    fixed_threshold_endpointer,
)


@dataclass(slots=True, frozen=True)
class TurnSample:
    """One labelled pause inside a caller turn.

    `transcript` is what the recogniser had produced at the moment of the pause.
    `complete` is the ground truth: had the caller finished?
    `pause_ms` is how long the pause actually lasted before they either resumed or stopped.
    """

    id: str
    transcript: str
    complete: bool
    pause_ms: float
    energy_tail: tuple[float, ...] = ()
    note: str = ""


# ── the corpus ───────────────────────────────────────────────────────────────
# Falling energy contour: a completed phrase. Rising: a continuation.
_FALLING = (0.9, 0.85, 0.8, 0.4, 0.25, 0.15)
_RISING = (0.3, 0.4, 0.5, 0.7, 0.85, 0.95)
_FLAT = (0.6, 0.6, 0.55, 0.6, 0.58, 0.6)

CORPUS: tuple[TurnSample, ...] = (
    # ── complete turns that should endpoint FAST ────────────────────────────
    TurnSample("c01", "yes", True, 900, _FALLING, "short confirmation"),
    TurnSample("c02", "no", True, 900, _FALLING, "short confirmation"),
    TurnSample("c03", "that's correct", True, 900, _FALLING, ""),
    TurnSample("c04", "okay thanks", True, 900, _FALLING, ""),
    TurnSample("c05", "I'd like to book an appointment", True, 800, _FALLING, ""),
    TurnSample("c06", "can you check my order status", True, 800, _RISING, "question"),
    TurnSample("c07", "I want to speak to a human", True, 850, _FALLING, ""),
    TurnSample("c08", "my flight is on the twelfth", True, 800, _FALLING, ""),
    TurnSample("c09", "cancel my subscription please", True, 900, _FALLING, ""),
    TurnSample("c10", "what are your opening hours", True, 800, _RISING, "question"),
    TurnSample("c11", "sure", True, 900, _FALLING, ""),
    TurnSample("c12", "I need help with a refund", True, 850, _FALLING, ""),
    TurnSample("c13", "it stopped working yesterday", True, 800, _FALLING, ""),
    TurnSample("c14", "that works for me", True, 850, _FALLING, ""),
    TurnSample("c15", "no that's everything", True, 900, _FALLING, ""),
    TurnSample("c16", "agent", True, 900, _FALLING, "wants a human"),
    TurnSample("c17", "I'm calling about my bill", True, 820, _FALLING, ""),
    TurnSample("c18", "the address is wrong", True, 830, _FALLING, ""),
    TurnSample("c19", "yeah", True, 900, _FALLING, ""),
    TurnSample("c20", "please transfer me", True, 860, _FALLING, ""),
    # WH-questions ending on a fronted transitive verb. Complete, but they end on a word that
    # normally signals incompleteness -- the shape the call simulator surfaced, which the
    # isolated-turn corpus had entirely missed.
    TurnSample("c21", "what appointments do you have", True, 820, _RISING, "fronted object"),
    TurnSample("c22", "how many slots do you have", True, 820, _RISING, "fronted object"),
    TurnSample("c23", "which options do you offer", True, 830, _RISING, "fronted object"),
    TurnSample("c24", "how many can I order", True, 820, _RISING, "fronted object"),

    # ── INCOMPLETE turns: the agent must NOT respond here ───────────────────
    # Numbers read aloud. The single most damaging false cutoff there is.
    TurnSample("i01", "my account number is", False, 620, _RISING, "dangling copula"),
    TurnSample("i02", "my account number is four two", False, 700, _FLAT, "mid-number"),
    TurnSample("i03", "my account number is four two four two", False, 750, _FLAT, "mid-number"),
    TurnSample("i04", "the card ends in seven three", False, 680, _FLAT, "mid-number"),
    TurnSample("i05", "my postcode is SW1A", False, 640, _FLAT, "mid-code"),
    TurnSample("i06", "the reference is one two three", False, 720, _FLAT, "mid-number"),
    TurnSample("i07", "I can do Tuesday at 2", False, 660, _FLAT, "mid-time"),

    # Dangling function words.
    TurnSample("i08", "I'd like to book an appointment for", False, 600, _RISING, ""),
    TurnSample("i09", "can I speak to", False, 580, _RISING, ""),
    TurnSample("i10", "the problem is that", False, 610, _RISING, ""),
    TurnSample("i11", "I was wondering if", False, 640, _RISING, ""),
    TurnSample("i12", "it happened when I", False, 600, _RISING, ""),
    TurnSample("i13", "my name is", False, 580, _RISING, ""),
    TurnSample("i14", "I need to change my", False, 620, _RISING, ""),
    TurnSample("i15", "could you tell me the", False, 600, _RISING, ""),
    TurnSample("i16", "we've been having trouble with the", False, 650, _RISING, ""),

    # Fillers — the caller is composing.
    TurnSample("i17", "I think the issue is um", False, 700, _FLAT, "filler"),
    TurnSample("i18", "let me see uh", False, 680, _FLAT, "filler"),
    TurnSample("i19", "it was like", False, 620, _FLAT, "filler"),
    TurnSample("i20", "so basically", False, 660, _FLAT, "filler"),
    TurnSample("i21", "well", False, 700, _FLAT, "filler, single word"),

    # Genuine thinking pauses mid-sentence.
    TurnSample("i22", "I bought it about", False, 640, _RISING, ""),
    TurnSample("i23", "the delivery was supposed to arrive on", False, 620, _RISING, ""),
    TurnSample("i24", "I already tried restarting it and", False, 680, _RISING, ""),
    TurnSample("i25", "there were two charges but", False, 660, _RISING, ""),
    TurnSample("i26", "I spoke to someone yesterday who", False, 640, _RISING, ""),
    TurnSample("i27", "my order hasn't arrived and I", False, 700, _RISING, ""),
    TurnSample("i28", "the amount should have been", False, 620, _RISING, ""),
    TurnSample("i29", "I'm trying to work out whether", False, 640, _RISING, ""),
    TurnSample("i30", "it says the payment failed because", False, 660, _RISING, ""),
    # The near-misses for the WH rule: these START with a WH-word but are genuinely unfinished,
    # so they are exactly the cases that break if that rule is written any looser.
    TurnSample("i31", "what I need is", False, 620, _RISING, "WH-word but incomplete"),
    TurnSample("i32", "what about the", False, 600, _RISING, "WH-word but incomplete"),
    TurnSample("i33", "how long does it take to", False, 640, _RISING, "WH-word but incomplete"),
    # Transitive verbs left without their object. Before these were added the scorer rated
    # "can I also get" at 0.72 and would have answered straight over the caller.
    TurnSample("i34", "can I also get", False, 620, _RISING, "transitive verb, no object"),
    TurnSample("i35", "could you send", False, 600, _RISING, "transitive verb, no object"),
    TurnSample("i36", "I'd like to change my", False, 640, _RISING, "transitive verb, no object"),
)


@dataclass(slots=True)
class EndpointingResult:
    label: str
    #: Share of INCOMPLETE turns where the system wrongly declared end-of-turn. The metric
    #: nobody publishes, and the one callers actually feel.
    false_cutoff_rate: float
    #: Median milliseconds of silence before responding to a COMPLETE turn.
    median_latency_ms: float
    p90_latency_ms: float
    #: Share of complete turns the system eventually endpointed at all.
    completion_recall: float
    n_complete: int
    n_incomplete: int
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "false_cutoff_rate": round(self.false_cutoff_rate, 4),
            "median_latency_ms": round(self.median_latency_ms, 1),
            "p90_latency_ms": round(self.p90_latency_ms, 1),
            "completion_recall": round(self.completion_recall, 4),
            "n_complete": self.n_complete,
            "n_incomplete": self.n_incomplete,
            "failures": self.failures[:12],
        }

    def verdict(self) -> str:
        return (
            f"{self.label}: responds in {self.median_latency_ms:.0f}ms (p90 "
            f"{self.p90_latency_ms:.0f}ms) and interrupts the caller on "
            f"{self.false_cutoff_rate:.1%} of unfinished turns."
        )


def run(endpointer: Endpointer, label: str, corpus=CORPUS, step_ms: float = 20.0) -> EndpointingResult:
    """Replay the corpus through an endpointer, one 20ms frame at a time.

    Frame-by-frame rather than evaluating once at the final pause length, because that is how
    the system actually runs: the decision is made continuously, and a system that would have
    endpointed at 300ms must be caught doing so even if the labelled pause was 700ms long.
    Evaluating only at the end would flatter every configuration.
    """
    latencies: list[float] = []
    false_cutoffs = 0
    endpointed_complete = 0
    failures: list[str] = []

    complete = [s for s in corpus if s.complete]
    incomplete = [s for s in corpus if not s.complete]

    for sample in corpus:
        state = TurnState(
            transcript=sample.transcript,
            silence_ms=0.0,
            speech_ms=max(600.0, len(sample.transcript) * 55),
            energy_tail=list(sample.energy_tail),
        )
        fired_at: float | None = None
        elapsed = 0.0
        while elapsed <= sample.pause_ms:
            state.silence_ms = elapsed
            if endpointer.evaluate(state).decision is TurnDecision.ENDPOINT:
                fired_at = elapsed
                break
            elapsed += step_ms

        if sample.complete:
            if fired_at is not None:
                endpointed_complete += 1
                latencies.append(fired_at)
            else:
                failures.append(f"{sample.id}: never endpointed {sample.transcript!r}")
        else:
            if fired_at is not None:
                false_cutoffs += 1
                failures.append(
                    f"{sample.id}: cut off at {fired_at:.0f}ms — {sample.transcript!r}"
                )

    latencies.sort()
    return EndpointingResult(
        label=label,
        false_cutoff_rate=false_cutoffs / len(incomplete) if incomplete else 0.0,
        median_latency_ms=_percentile(latencies, 0.5),
        p90_latency_ms=_percentile(latencies, 0.9),
        completion_recall=endpointed_complete / len(complete) if complete else 0.0,
        n_complete=len(complete),
        n_incomplete=len(incomplete),
        failures=failures,
    )


def sweep(corpus=CORPUS) -> list[EndpointingResult]:
    """The curve.

    Fixed thresholds trace the baseline's trade-off; the adaptive endpointer is evaluated at
    several base thresholds so the comparison is a curve against a curve rather than a point
    against a point. Comparing one adaptive configuration to one fixed threshold proves
    nothing — the fixed one could simply have been tuned differently.
    """
    results: list[EndpointingResult] = []
    for threshold in (300, 400, 500, 600, 700, 900, 1200):
        results.append(
            run(fixed_threshold_endpointer(threshold), f"fixed {threshold}ms", corpus)
        )
    for base in (380, 450, 520, 620, 720):
        results.append(
            run(Endpointer(EndpointConfig(base_silence_ms=base)), f"adaptive base {base}ms", corpus)
        )
    return results


def ablate(corpus=CORPUS) -> list[EndpointingResult]:
    """Which signal is doing the work?

    An adaptive endpointer that is only better because its base threshold happens to be tuned
    is not adaptive, it is tuned. Turning each signal off in turn is the only way to tell.
    """
    base = 520.0
    return [
        run(fixed_threshold_endpointer(BASELINE_SILENCE_MS), "baseline fixed 700ms", corpus),
        run(Endpointer(EndpointConfig(base_silence_ms=base, enable_semantic=False,
                                      enable_prosody=False)), "adaptive, no signals", corpus),
        run(Endpointer(EndpointConfig(base_silence_ms=base, enable_prosody=False)),
            "+ syntax only", corpus),
        run(Endpointer(EndpointConfig(base_silence_ms=base, enable_semantic=False)),
            "+ prosody only", corpus),
        run(Endpointer(EndpointConfig(base_silence_ms=base)), "+ both (default)", corpus),
    ]


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("inf")
    index = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values)) - 1))
    return sorted_values[index]
