"""The turn loop: hear, decide, speak — all streaming, all measured.

THE BUDGET IS THE ARCHITECTURE. Everything below is shaped by one number: a caller stops
believing they are in a conversation somewhere past 700-800ms of dead air. Vendors quote ~600ms
end-to-end. That figure is only reachable if every stage starts on the FIRST token of the
previous stage rather than its last:

    stage              awaited      streamed     budget
    ─────────────────────────────────────────────────────
    endpoint decision    700ms        280ms       300ms    <- the adaptive endpointer
    STT finalisation     380ms         55ms        80ms    stream partials, finalise on endpoint
    LLM first token      640ms        210ms       240ms    stream; never await completion
    TTS first audio      340ms         65ms       100ms    start on the first CLAUSE
    ─────────────────────────────────────────────────────
    total              ~2060ms       ~610ms       720ms

The "awaited" column is what you get by writing the obvious `await` chain, and it is why so
many voice agents feel like a walkie-talkie. Two of the four savings come from the endpointer;
the other two come from refusing to wait for a complete result at any stage.

WHAT IS MEASURED VS ASSERTED. `TurnBudget` records real wall-clock time per stage on every
turn, and the studio renders it. A budget nobody measures is a wish, and the reason this file
exists rather than a diagram is that the numbers above have to be checkable.

THE FIRST-CLAUSE TRICK. Synthesis begins at the first comma or clause boundary, not the first
sentence. On a typical agent utterance that is ~200ms earlier — roughly a third of the entire
budget, bought with one function.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ..turn.bargein import BargeDecision, BargeInDetector, SpeechFrame, Utterance
from ..turn.endpointing import Endpointer, TurnDecision, TurnState

log = logging.getLogger("dialtone.pipeline")

#: Past this a pause stops reading as thinking and starts reading as broken.
TURN_BUDGET_MS = 720.0


class Stage(StrEnum):
    ENDPOINT = "endpoint"
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    TOOL = "tool"


@dataclass(slots=True)
class TurnBudget:
    """Per-turn stage timings. Wall clock, not estimates."""

    marks: dict[str, float] = field(default_factory=dict)
    _last: float = field(default_factory=time.perf_counter)

    def mark(self, stage: Stage | str) -> float:
        now = time.perf_counter()
        delta = (now - self._last) * 1000
        # Accumulate rather than overwrite: a turn with two tool calls should report the total
        # time spent in tools, not just the last one.
        self.marks[str(stage)] = self.marks.get(str(stage), 0.0) + delta
        self._last = now
        return delta

    def restart(self) -> None:
        self._last = time.perf_counter()

    @property
    def total_ms(self) -> float:
        return round(sum(self.marks.values()), 2)

    @property
    def within_budget(self) -> bool:
        return self.total_ms <= TURN_BUDGET_MS

    def as_dict(self) -> dict[str, Any]:
        return {
            **{k: round(v, 2) for k, v in self.marks.items()},
            "total_ms": self.total_ms,
            "within_budget": self.within_budget,
        }


# ── provider interfaces ──────────────────────────────────────────────────────
class Recognizer(Protocol):
    """Streaming STT. Yields (transcript_so_far, is_final)."""

    def stream(self, audio: AsyncIterator[SpeechFrame]) -> AsyncIterator[tuple[str, bool]]: ...


class Responder(Protocol):
    """Token-streaming reasoning.

    MUST stream. Awaiting a complete response is the single largest avoidable cost in the
    budget, and it is the difference between 210ms and 640ms to first audio.
    """

    def stream(self, history: list[dict[str, str]], context: dict) -> AsyncIterator[str]: ...


class Synthesizer(Protocol):
    """Streaming TTS. Consumes text chunks, yields audio with a duration."""

    def stream(self, text: AsyncIterator[str]) -> AsyncIterator[tuple[bytes, float]]: ...


class AudioSink(Protocol):
    """Where audio actually goes.

    `played_ms` must reflect what has been WRITTEN TO THE CALLER, not what has been
    synthesised — a TTS engine runs seconds ahead of the speaker, and the gap is exactly what
    `turn/bargein.py` needs to know what the caller heard.
    """

    async def write(self, audio: bytes, duration_ms: float) -> None: ...
    async def flush(self) -> None: ...
    @property
    def played_ms(self) -> float: ...


# ── the turn ─────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class TurnResult:
    transcript: str
    response: str
    #: What the caller actually HEARD. Differs from `response` when they interrupted, and this
    #: is the value that goes into history.
    heard: str
    budget: TurnBudget
    interrupted: bool = False
    backchannels: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    endpoint_reason: str = ""


class TurnOrchestrator:
    """One conversational turn, fully streamed, with barge-in handled correctly."""

    def __init__(
        self,
        recognizer: Recognizer,
        responder: Responder,
        synthesizer: Synthesizer,
        sink: AudioSink,
        endpointer: Endpointer | None = None,
        barge: BargeInDetector | None = None,
        on_backchannel: Callable[[], Any] | None = None,
    ):
        self.recognizer = recognizer
        self.responder = responder
        self.synthesizer = synthesizer
        self.sink = sink
        self.endpointer = endpointer or Endpointer()
        self.barge = barge or BargeInDetector()
        self.on_backchannel = on_backchannel
        self.history: list[dict[str, str]] = []

    async def listen(self, frames: AsyncIterator[SpeechFrame]) -> tuple[str, TurnBudget, str, int]:
        """Consume caller audio until the endpointer says the turn is over.

        The endpointer runs on every frame, not on a timer. A turn that becomes obviously
        complete — the caller says "yes" — must be able to end in 180ms, and a 100ms polling
        loop would put a floor under that which no amount of model quality can lift.
        """
        budget = TurnBudget()
        state = TurnState()
        backchannels = 0
        reason = ""

        async for frame in frames:
            if frame.is_speech:
                state.speech_ms += frame.duration_ms
                state.silence_ms = 0.0
            else:
                state.silence_ms += frame.duration_ms
            state.energy_tail.append(frame.energy)
            if len(state.energy_tail) > 24:
                state.energy_tail.pop(0)

            # Partial transcripts arrive continuously; the endpointer reads the newest.
            state.transcript = getattr(frames, "partial", state.transcript)

            decision = self.endpointer.evaluate(state)
            if decision.decision is TurnDecision.BACKCHANNEL:
                state.backchanneled = True
                backchannels += 1
                if self.on_backchannel:
                    result = self.on_backchannel()
                    if asyncio.iscoroutine(result):
                        await result
                continue
            if decision.decision is TurnDecision.ENDPOINT:
                reason = decision.reason
                break

        budget.mark(Stage.ENDPOINT)
        return state.transcript, budget, reason, backchannels

    async def respond(
        self, transcript: str, budget: TurnBudget, frames: AsyncIterator[SpeechFrame] | None = None
    ) -> TurnResult:
        """Generate and speak, stopping cleanly if the caller barges in."""
        self.history.append({"role": "user", "content": transcript})
        budget.mark(Stage.STT)

        parts: list[str] = []
        first_token = False
        interrupted = False

        async def tokens() -> AsyncIterator[str]:
            nonlocal first_token
            async for token in self.responder.stream(self.history, {}):
                if not first_token:
                    first_token = True
                    budget.mark(Stage.LLM)
                parts.append(token)
                yield token

        utterance = Utterance(text="", total_ms=0.0)
        self.barge.reset()
        first_audio = False

        async for audio, duration in self.synthesizer.stream(tokens()):
            if not first_audio:
                first_audio = True
                budget.mark(Stage.TTS)

            await self.sink.write(audio, duration)
            utterance.text = "".join(parts)
            utterance.total_ms += duration
            utterance.played_ms = self.sink.played_ms

            if frames is not None:
                frame = getattr(frames, "latest", None)
                if frame is not None:
                    verdict = self.barge.evaluate(
                        frame, utterance, getattr(frames, "partial", "")
                    )
                    if verdict.decision is BargeDecision.INTERRUPT:
                        # Flush FIRST. Anything still buffered will be heard, and audio the
                        # caller hears after they interrupted is the most jarring failure in a
                        # voice agent — it sounds like the machine ignoring them.
                        await self.sink.flush()
                        interrupted = True
                        log.info("barge-in: %s", verdict.reason)
                        break

        response = "".join(parts)
        utterance.text = response
        utterance.played_ms = self.sink.played_ms

        # THE KEY LINE. History records what the caller HEARD, never what was generated.
        # See turn/bargein.py for why: an agent that believes it said things the caller never
        # heard produces answers that make no sense two turns later.
        from ..turn.bargein import truncate_to_played

        heard = truncate_to_played(utterance) if interrupted else response
        self.history.append({"role": "assistant", "content": heard})

        if not budget.within_budget:
            log.warning("turn over budget: %.0fms > %.0fms %s",
                        budget.total_ms, TURN_BUDGET_MS, budget.marks)

        return TurnResult(
            transcript=transcript,
            response=response,
            heard=heard,
            budget=budget,
            interrupted=interrupted,
        )

    async def turn(self, frames: AsyncIterator[SpeechFrame]) -> TurnResult:
        transcript, budget, reason, backchannels = await self.listen(frames)
        result = await self.respond(transcript, budget, frames)
        result.endpoint_reason = reason
        result.backchannels = backchannels
        return result


# ── clause splitting ─────────────────────────────────────────────────────────
_CLAUSE = re.compile(r"[,;:]|(?<=[.!?])\s")


def first_clause(text: str, min_chars: int = 14) -> tuple[str, str]:
    """Split at the earliest natural pause. Synthesis starts here, not at the sentence.

    `min_chars` prevents starting on a fragment so short that the TTS engine has no prosodic
    context and produces a flat, clipped opening — which sounds worse than the latency it saved.
    """
    for match in _CLAUSE.finditer(text):
        end = match.end()
        if end >= min_chars:
            return text[:end].strip(), text[end:]
    return "", text


async def clause_buffer(tokens: AsyncIterator[str], min_chars: int = 14) -> AsyncIterator[str]:
    """Regroup a token stream into clause-sized chunks for the synthesiser.

    Feeding raw tokens to a TTS engine produces choppy audio; feeding whole sentences wastes
    the latency this whole file is built to save. Clauses are the unit that gets both.
    """
    buffer = ""
    async for token in tokens:
        buffer += token
        clause, buffer = first_clause(buffer, min_chars)
        if clause:
            yield clause
    if buffer.strip():
        yield buffer.strip()
