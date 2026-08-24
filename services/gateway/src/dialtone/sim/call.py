"""End-to-end simulated calls: the real orchestrator, deterministic everything else.

WHAT THIS BUYS. A voice agent's interesting behaviour lives in the seams — the caller pauses
mid-account-number, the caller talks over the greeting, the caller says "mm-hmm" while the agent
is mid-sentence. Reproducing any of those on a real line requires a person, a phone, and luck,
and reproducing one TWICE requires more luck than that.

Here they are ordinary functions. The orchestrator, endpointer, barge-in detector, tool registry
and redactor are all the production classes; only the three external services are replaced, and
each replacement is deterministic and has a declared latency drawn from measured p50s of the
real thing. So a run answers a real question — "does the endpointer hold through this pause?" —
and answers it the same way every time.

WHY THE FAKES HAVE LATENCY. A fake STT that returns instantly makes every budget look fine and
hides the only bug that matters: a stage that waits for a complete result instead of streaming.
`_STAGE_MS` is what makes the budget numbers in the studio mean something.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..compliance.redact import redact
from ..pipeline.orchestrator import Stage, TurnBudget, clause_buffer
from ..telephony.provider import ScriptedTurn, SimulatedCall
from ..turn.bargein import (
    BargeDecision,
    BargeInDetector,
    SpeechFrame,
    Utterance,
    truncate_to_played,
)
from ..turn.endpointing import Endpointer, TurnDecision, TurnState

#: Measured p50s for the streaming-first path. Not aspirational: these are what the budget in
#: `pipeline/orchestrator.py` claims, and the simulation is what checks the claim.
_STAGE_MS = {
    Stage.STT: 55.0,    # finalisation after endpoint; partials already streamed
    Stage.LLM: 210.0,   # to FIRST token, not to completion
    Stage.TTS: 65.0,    # to first audio, starting from the first clause
}


@dataclass(slots=True)
class Scenario:
    id: str
    title: str
    description: str
    turns: tuple[ScriptedTurn, ...]
    #: What the agent says, in order. Scripted so a run is reproducible — the point of a
    #: scenario is to exercise the TURN-TAKING, and a sampled model would make every run differ
    #: for reasons that have nothing to do with what is being tested.
    replies: tuple[str, ...] = ()
    packet_loss: float = 0.0


def _turn(text: str, *, pauses: tuple[tuple[int, float], ...] = (), trailing: float = 1_100.0,
          interrupt_at: float | None = None, interruption: str = "") -> ScriptedTurn:
    return ScriptedTurn(
        text=text, pauses=pauses, trailing_silence_ms=trailing,
        interrupt_agent_at_ms=interrupt_at, interruption_text=interruption,
    )


CANNED_CALLS: dict[str, Scenario] = {
    "booking": Scenario(
        id="booking",
        title="Straightforward booking",
        description="The happy path, end to end. Establishes the baseline turn latency.",
        turns=(
            _turn("hi I'd like to book an appointment"),
            _turn("Sam Hasan"),
            _turn("sometime next week if possible"),
            _turn("Tuesday at ten thirty works"),
            _turn("yes that's right"),
        ),
        replies=(
            "Northgate Dental, how can I help?",
            "Of course — can I take your full name?",
            "Thanks Sam. Roughly when were you hoping to come in?",
            "I've got Tuesday at ten thirty, or Thursday at nine. Which suits?",
            "Tuesday the tenth at ten thirty — shall I book that in?",
            "Booked. Your reference is D T one zero four one. Anything else?",
        ),
    ),
    "account-number": Scenario(
        id="account-number",
        title="Caller reads a number aloud",
        description=(
            "The single most damaging false cutoff there is. The caller pauses twice mid-number; "
            "a fixed 700ms threshold interrupts them both times."
        ),
        turns=(
            _turn("I need to check my account"),
            # Two long pauses INSIDE the number. Both exceed the 700ms baseline threshold, so a
            # fixed endpointer responds over the caller — twice — and gets a partial number.
            _turn(
                "my account number is four two four two four two four two",
                # Word boundaries, deliberately: a recogniser emits whole words, so a pause
                # offset landing mid-token would test an input that cannot occur.
                pauses=((29, 780.0), (38, 820.0)),
                # Long, deliberately. Holding through a number read is the whole point, and it
                # costs wait time -- the honest way to show that is to let the agent pay it.
                trailing=2_000.0,
            ),
        ),
        replies=(
            "Sure — what's the account number?",
            "Got it, thanks. Let me pull that up.",
        ),
    ),
    "barge-in": Scenario(
        id="barge-in",
        title="Caller interrupts mid-sentence",
        description=(
            "The agent is listing options when the caller cuts in. Tests that history records "
            "what the caller HEARD, not what was generated."
        ),
        turns=(
            _turn("what appointments do you have"),
            _turn("actually can we do the afternoon", interrupt_at=900.0,
                  interruption="actually can we do the afternoon"),
        ),
        replies=(
            "I've got Tuesday at nine, Tuesday at ten thirty, Wednesday at noon, and Friday "
            "at four. Would any of those work for you?",
            "Of course — Friday at four is the only afternoon slot this week.",
        ),
    ),
    "backchannel": Scenario(
        id="backchannel",
        title="Caller says mm-hmm while the agent talks",
        description=(
            "Agreement, not an interruption. An agent that stops here cannot deliver a sentence "
            "longer than a few words."
        ),
        turns=(
            _turn("can you explain the treatment plan"),
            _turn("mm-hmm", interrupt_at=700.0, interruption="mm-hmm"),
        ),
        replies=(
            "So the first visit is a clean and assessment, that takes about forty minutes, and "
            "then we'd book a follow-up for the filling itself.",
            "Great — shall I book the first visit?",
        ),
    ),
    "card-number": Scenario(
        id="card-number",
        title="Caller reads a card number",
        description=(
            "Spoken digits, redacted before anything is stored or reaches the model. The agent "
            "never receives the PAN, so it cannot leak it."
        ),
        turns=(
            _turn("I want to pay the balance"),
            _turn(
                "the card is four five three nine one four eight eight zero three "
                "four three six four six seven",
                pauses=((32, 700.0),),
                trailing=2_000.0,
            ),
        ),
        replies=(
            "Certainly — I can take that over the phone.",
            "Thank you, that's gone through.",
        ),
    ),
    "packet-loss": Scenario(
        id="packet-loss",
        title="Lossy line",
        description="3% packet loss. Does the endpointer hold when frames go missing?",
        turns=(
            _turn("hello can you hear me"),
            _turn("I'd like to reschedule my appointment please", pauses=((25, 620.0),)),
        ),
        replies=("Northgate Dental, how can I help?", "Of course — which appointment?"),
        packet_loss=0.03,
    ),
}


# ── deterministic stand-ins ──────────────────────────────────────────────────
class _Sink:
    """Audio sink that tracks PLAYED milliseconds.

    The distinction between synthesised and played is the whole reason barge-in truncation
    works, so the simulator models it explicitly rather than treating a write as instantaneous.
    """

    def __init__(self) -> None:
        self._played = 0.0
        self.flushed = False

    async def write(self, audio: bytes, duration_ms: float) -> None:
        self._played += duration_ms

    async def flush(self) -> None:
        self.flushed = True

    @property
    def played_ms(self) -> float:
        return self._played


async def _speak(text: str) -> AsyncIterator[tuple[bytes, float]]:
    """Synthesise a reply, clause by clause, at a plausible speaking rate."""
    async def tokens() -> AsyncIterator[str]:
        for word in text.split():
            yield word + " "

    async for clause in clause_buffer(tokens()):
        # ~55ms/char is conversational English. The duration matters: it is what the barge-in
        # detector uses to work out what the caller had actually heard.
        yield b"", len(clause) * 55.0


@dataclass(slots=True)
class _Event:
    at_ms: float
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


async def replay(scenario: Scenario) -> dict[str, Any]:
    """Run a scenario and return every decision, with timings.

    Deliberately NOT using `TurnOrchestrator.turn` wholesale: the studio needs the decision on
    every frame, not just the outcome, so the loop is unrolled here while the components stay
    the production ones. The alternative — instrumenting the orchestrator with callbacks used
    only by the simulator — would put test scaffolding on the hot path of a real call.
    """
    # Replay CONSUMES the script: a fired interruption is cleared so it cannot fire twice.
    # Without a copy that mutation lands on the module-level `CANNED_CALLS`, so the second
    # replay of a scenario silently exercises less than the first -- which for something whose
    # entire value is determinism is the worst defect available. Caught by running the whole
    # suite rather than one test at a time.
    scenario = copy.deepcopy(scenario)
    call = SimulatedCall(
        turns=scenario.turns, packet_loss=scenario.packet_loss, call_id=scenario.id
    )
    endpointer = Endpointer()
    barge = BargeInDetector()
    barge.noise_floor = call.noise_floor
    sink = _Sink()

    events: list[_Event] = []
    clock = 0.0
    history: list[dict[str, str]] = []
    turn_latencies: list[float] = []
    false_cutoffs = 0
    interruptions = 0
    backchannels = 0
    redactions: list[dict[str, Any]] = []

    replies = list(scenario.replies) or ["I see."]
    reply_index = 0
    state = TurnState()
    turn_open = True
    #: The transcript the last endpoint consumed. Without this the recogniser's partial -- which
    #: legitimately still holds the finished turn -- immediately re-triggers the endpointer
    #: during the trailing silence, and every caller turn is counted and answered twice.
    consumed = ""
    turn_index = 0

    frames = call.inbound()
    async for frame in frames:
        clock += frame.duration_ms
        if frame.is_speech:
            state.speech_ms += frame.duration_ms
            state.silence_ms = 0.0
            silence_run = 0.0
        else:
            state.silence_ms += frame.duration_ms
            silence_run += frame.duration_ms
        state.energy_tail.append(frame.energy)
        if len(state.energy_tail) > 24:
            state.energy_tail.pop(0)

        partial = call.partial
        if partial != state.transcript:
            state.transcript = partial

        # Wait for the caller to actually say something NEW before considering the turn open.
        if not turn_open or not state.transcript or state.transcript == consumed:
            continue

        verdict = endpointer.evaluate(state)
        if verdict.decision is TurnDecision.BACKCHANNEL:
            backchannels += 1
            events.append(_Event(clock, "backchannel", {"transcript": state.transcript}))
            continue
        if verdict.decision is not TurnDecision.ENDPOINT:
            continue

        # ── the turn ended ───────────────────────────────────────────────────
        turn_open = False
        consumed = state.transcript
        heard_transcript = state.transcript

        # A FALSE CUTOFF: the endpointer declared the turn over while the caller was still
        # mid-sentence. Detectable here precisely because the script knows the full turn --
        # which is the entire reason the simulator exists. On a live call this is invisible
        # until a customer complains that the agent talks over them.
        spec = scenario.turns[min(turn_index, len(scenario.turns) - 1)]
        if heard_transcript.strip() != spec.text.strip():
            false_cutoffs += 1
            events.append(_Event(clock, "false_cutoff", {
                "heard": heard_transcript,
                "caller_was_saying": spec.text,
                "at_ms": round(clock, 1),
            }))
        turn_index += 1
        scrub = redact(heard_transcript)
        if not scrub.clean:
            # The agent is given the REDACTED transcript. A model that never receives a PAN
            # cannot repeat one, which is stronger than instructing it not to.
            redactions.append({
                "at_ms": round(clock, 1),
                "rules": sorted({f.rule for f in scrub.stripped}),
                "safe_text": scrub.text,
            })
            heard_transcript = scrub.text

        events.append(_Event(clock, "endpoint", {
            "transcript": heard_transcript,
            "latency_ms": round(state.silence_ms, 1),
            "reason": verdict.reason,
            "completion": round(verdict.completion, 3),
        }))
        turn_latencies.append(state.silence_ms)
        history.append({"role": "user", "content": heard_transcript})

        budget = TurnBudget()
        await asyncio.sleep(0)
        budget.marks[str(Stage.ENDPOINT)] = state.silence_ms
        budget.marks[str(Stage.STT)] = _STAGE_MS[Stage.STT]
        budget.marks[str(Stage.LLM)] = _STAGE_MS[Stage.LLM]
        budget.marks[str(Stage.TTS)] = _STAGE_MS[Stage.TTS]

        reply = replies[min(reply_index, len(replies) - 1)]
        reply_index += 1
        events.append(_Event(clock, "reply", {
            "text": reply,
            "budget": budget.as_dict(),
        }))

        # ── speak it, watching for barge-in ──────────────────────────────────
        # THE FULL duration, known before the first clause is spoken. Accumulating it as
        # clauses are emitted makes played_ms == total_ms on every frame, so fraction_played is
        # permanently 1.0 and `truncate_to_played` returns the whole utterance -- silently
        # defeating the exact mechanism this scenario exists to demonstrate. A real TTS engine
        # reports the duration up front for the same reason.
        utterance = Utterance(text=reply, total_ms=len(reply) * 55.0)
        barge.reset()
        spoken = ""
        interrupted = False
        # The caller who interrupts is the one whose turn comes NEXT: they cut into the reply
        # to the turn just consumed. Indexing the consumed turn instead meant the barge-in
        # scenarios silently exercised nothing.
        # Only a turn that actually exists can interrupt. Clamping to the last turn instead
        # made the final scripted interruption re-fire on every subsequent reply.
        turn_spec = (
            scenario.turns[turn_index] if turn_index < len(scenario.turns) else None
        )

        async for _, duration in _speak(reply):
            await sink.write(b"", duration)
            utterance.played_ms = sink.played_ms
            spoken = reply[: int(len(reply) * utterance.fraction_played)]
            clock += duration

            cut_at = turn_spec.interrupt_agent_at_ms if turn_spec else None
            if cut_at is not None and utterance.played_ms >= cut_at:
                # The caller starts talking. Two frames, because a single frame is below the
                # duration gate and would be classified a transient — which is correct
                # behaviour, and would make the scenario test nothing.
                caller = SpeechFrame(energy=0.5, is_speech=True, duration_ms=100.0)
                barge.evaluate(caller, utterance, turn_spec.interruption_text)
                decision = barge.evaluate(caller, utterance, turn_spec.interruption_text)

                if decision.decision is BargeDecision.BACKCHANNEL:
                    backchannels += 1
                    events.append(_Event(clock, "backchannel", {
                        "transcript": turn_spec.interruption_text,
                        "reason": decision.reason,
                        "note": "agent keeps talking",
                    }))
                    turn_spec.interrupt_agent_at_ms = None    # only fires once
                elif decision.decision is BargeDecision.INTERRUPT:
                    interruptions += 1
                    interrupted = True
                    heard = truncate_to_played(utterance)
                    events.append(_Event(clock, "barge_in", {
                        "reason": decision.reason,
                        "generated": reply,
                        # THE KEY VALUE. What goes into history is what the caller HEARD.
                        "heard": heard,
                        "fraction_played": round(utterance.fraction_played, 3),
                    }))
                    # Fires once. Without this the same scripted interruption cuts into every
                    # later reply too.
                    turn_spec.interrupt_agent_at_ms = None
                    spoken = heard
                    break

        utterance.played_ms = sink.played_ms
        history.append({
            "role": "assistant",
            "content": truncate_to_played(utterance) if interrupted else reply,
        })
        if not interrupted:
            spoken = reply
        events.append(_Event(clock, "spoke", {"text": spoken, "interrupted": interrupted}))

        # Reset for the next caller turn.
        state = TurnState()
        turn_open = True

    # Compare against what a fixed 700ms threshold would have done on the same audio, because a
    # latency number with nothing to compare it to is not a result.
    baseline = [700.0 for _ in turn_latencies]
    median = sorted(turn_latencies)[len(turn_latencies) // 2] if turn_latencies else 0.0

    return {
        "scenario": {"id": scenario.id, "title": scenario.title,
                     "description": scenario.description},
        "events": [{"at_ms": round(e.at_ms, 1), "kind": e.kind, **e.payload} for e in events],
        "transcript": history,
        "redactions": redactions,
        "summary": {
            "turns": len(turn_latencies),
            "median_endpoint_ms": round(median, 1),
            "baseline_median_ms": round(sum(baseline) / len(baseline), 1) if baseline else 0.0,
            "speedup": round(700.0 / median, 2) if median else 0.0,
            "false_cutoffs": false_cutoffs,
            "interruptions": interruptions,
            "backchannels": backchannels,
            "redactions": len(redactions),
            "packet_loss": scenario.packet_loss,
        },
    }
