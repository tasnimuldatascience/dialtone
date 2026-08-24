"""The conversation engine: one caller turn, from words in to words out.

This is where every other module meets. A turn passes through, in order:

    endpointing  →  redaction  →  knowledge  →  flow  →  model  →  tools  →  reply

Each stage is timed and every timing is reported, because the whole argument of this project is
that a voice agent's latency is a budget you either measure or lose.

WHY THE MODEL IS NOT IN CHARGE. The obvious design hands the model the whole conversation and a
list of tools and lets it decide everything. That produces a demo quickly and a system nobody can
operate: no way to guarantee a price is never quoted from memory, no way to test a path, no
answer to "what was it doing when the call went wrong".

Here the graph decides what is POSSIBLE — which tools exist at this step, which transitions are
legal, what must be collected before moving on — and the model decides what to SAY. A transition
the model proposes is validated against the declared edges; one that does not exist is refused,
and the refusal is a normal event rather than an error.

STREAMING IS THE POINT. `respond` is an async generator that yields text as the model produces
it, so the caller can begin synthesising audio on the first clause. Collecting the reply and
returning it whole would be four lines shorter and would cost ~400ms of dead air per turn.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..compliance.redact import redact
from ..flow.graph import Flow, FlowRunner, GuardrailError, NodeKind
from ..tools.registry import ToolCall, ToolRegistry, ToolTrace
from .grounding import Grounding
from .grounding import check as check_grounding
from .knowledge import Hit, KnowledgeBase
from .llm import Brain, Turn, build_system_prompt, split_marker
from .speakable import speakable

log = logging.getLogger("dialtone.conversation")


@dataclass(slots=True)
class AgentConfig:
    """Everything an operator configures about one agent."""

    id: str = "default"
    name: str = "Receptionist"
    business: str = "Northgate Dental"
    persona: str = "a warm, efficient receptionist"
    greeting: str = "Northgate Dental, how can I help?"
    voice: str = "female-warm"
    #: Higher is more varied. Kept LOW deliberately: at 0.4 this agent turned a documented
    #: "£120 to £180" range into "around £150", and merged a £45 check-up and a £60 hygienist
    #: into "around £45". Both are invented figures quoted down a phone line, which is the exact
    #: failure the knowledge base exists to prevent. Varied wording is not worth that.
    temperature: float = 0.15
    #: Whether to consult the knowledge base at all. Off makes a measurably faster agent that
    #: can only chat, which is occasionally what an operator wants.
    use_knowledge: bool = True
    max_turns: int = 40

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "business": self.business,
            "persona": self.persona, "greeting": self.greeting, "voice": self.voice,
            "temperature": self.temperature, "use_knowledge": self.use_knowledge,
            "max_turns": self.max_turns,
        }


@dataclass(slots=True)
class StageTiming:
    """Wall-clock milliseconds per stage of one turn. Measured, never estimated."""

    marks: dict[str, float] = field(default_factory=dict)
    _last: float = field(default_factory=time.perf_counter)

    def mark(self, stage: str) -> float:
        now = time.perf_counter()
        delta = (now - self._last) * 1000
        # Accumulated rather than overwritten: a turn with two tool calls should report the
        # total time spent in tools, not just the last one.
        self.marks[stage] = self.marks.get(stage, 0.0) + delta
        self._last = now
        return delta

    def restart(self) -> None:
        self._last = time.perf_counter()

    @property
    def total_ms(self) -> float:
        return round(sum(self.marks.values()), 1)

    def as_dict(self) -> dict[str, Any]:
        return {**{k: round(v, 1) for k, v in self.marks.items()}, "total_ms": self.total_ms}


@dataclass(slots=True)
class TurnRecord:
    """One completed exchange, as it will be stored and shown."""

    caller: str
    #: What is shown in the transcript -- the model's own wording.
    agent: str
    #: What is sent to the voice engine. Differs from `agent` wherever the model wrote a number
    #: the way it is TYPED rather than the way it is SAID: "£45" reads as "pound forty five" on
    #: several engines. Kept as a separate field rather than replacing `agent` so the transcript
    #: stays readable and the audio stays correct.
    spoken: str
    timing: StageTiming
    node: str = ""
    moved_to: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    #: Set when the model proposed a transition the graph does not allow. Kept on the record
    #: rather than logged away, because "the model tried to skip the payment step" is exactly
    #: what an operator reviewing a call needs to see.
    refused: str = ""
    #: Every number the agent said, checked against the passages it was given. A reply that
    #: quotes a price the documents do not contain is the single most damaging thing this
    #: system can do, and it does not look like an error from anywhere else.
    grounding: Grounding = field(default_factory=Grounding)

    def as_dict(self) -> dict[str, Any]:
        return {
            "caller": self.caller, "agent": self.agent, "spoken": self.spoken,
            "timing": self.timing.as_dict(),
            "node": self.node, "moved_to": self.moved_to, "citations": self.citations,
            "tools": self.tools, "redacted": self.redacted, "refused": self.refused,
            "grounding": self.grounding.as_dict(),
        }


class Conversation:
    """A live call. Holds the history, the flow position, and what has been collected."""

    def __init__(
        self,
        *,
        brain: Brain,
        config: AgentConfig,
        flow: Flow | None = None,
        tools: ToolRegistry | None = None,
        knowledge: KnowledgeBase | None = None,
        call_id: str = "call-1",
    ) -> None:
        self.brain = brain
        self.config = config
        self.knowledge = knowledge
        self.tools = tools or ToolRegistry()
        self.call_id = call_id
        self.runner = FlowRunner(flow) if flow else None
        self.state = self.runner.start() if self.runner else None
        self.history: list[Turn] = []
        self.turns: list[TurnRecord] = []
        self.trace = ToolTrace()
        self.ended = False

    # -- prompt ------------------------------------------------------------
    def _system_prompt(self, knowledge_text: str) -> str:
        node = self.runner.node(self.state) if self.runner and self.state else None
        transitions = [e.to for e in node.edges] if node else []
        return build_system_prompt(
            persona=self.config.persona,
            business=self.config.business,
            objective=node.objective if node else "",
            knowledge=knowledge_text,
            collected=dict(self.state.collected) if self.state else None,
            transitions=transitions,
        )

    def opening(self) -> str:
        """The first thing the caller hears.

        Fixed text rather than generated. It is the one line where latency is fully visible --
        the caller has just been connected and any delay reads as a bad line -- and it is also
        the line most worth an operator controlling word for word.
        """
        self.history.append(Turn("assistant", self.config.greeting))
        return self.config.greeting

    # -- the turn ----------------------------------------------------------
    async def respond(self, caller_text: str) -> AsyncIterator[dict[str, Any]]:
        """Handle one caller turn, streaming the reply.

        Yields dicts describing what is happening, so the UI can render the turn as it unfolds
        rather than after it: `{"type": "token"}` for speech, plus `stage`, `citation`, `tool`,
        `redacted`, `moved`, and a final `done` carrying the whole record.
        """
        timing = StageTiming()

        # ── redaction, before anything sees the text ──────────────────────
        scrub = redact(caller_text)
        safe_text = scrub.text
        removed = sorted({f.rule for f in scrub.stripped})
        if removed:
            # The model is handed the redacted version. A model that never receives a card
            # number cannot repeat one, which is stronger than instructing it not to.
            yield {"type": "redacted", "rules": removed, "text": safe_text}
        timing.mark("redact")

        self.history.append(Turn("user", safe_text))

        # ── knowledge ─────────────────────────────────────────────────────
        knowledge_text, hits = "", []
        if self.config.use_knowledge and self.knowledge is not None:
            knowledge_text, hits = self.knowledge.context_for(safe_text)
            for hit in hits:
                yield {"type": "citation", **_citation(hit)}
        timing.mark("knowledge")

        # ── the model ─────────────────────────────────────────────────────
        node = self.runner.node(self.state) if self.runner and self.state else None
        messages = [Turn("system", self._system_prompt(knowledge_text)), *self.history[-12:]]

        spoken = ""
        first_token = True
        async for piece in self.brain.stream(
            messages, temperature=self.config.temperature
        ):
            if first_token:
                # The number that matters. Everything after the first token overlaps with
                # speaking it, so this is what the caller actually waits for.
                timing.mark("think")
                first_token = False
            spoken += piece
            # Markers are stripped before anything is spoken, so the agent never reads
            # "[[confirm]]" aloud. Emitting the cleaned delta keeps the UI honest.
            visible, _ = split_marker(spoken)
            yield {"type": "token", "text": piece, "spoken": visible}

        if first_token:
            timing.mark("think")

        reply, marker = split_marker(spoken)
        reply = _one_or_two_sentences(reply)
        timing.mark("speak")

        # Check the reply's numbers against the passages the model was actually given -- not
        # against the whole knowledge base, because a figure from a document that was never
        # retrieved is still a figure the model did not read.
        grounding = check_grounding(reply, knowledge_text)
        if not grounding.ok:
            yield {"type": "grounding", **grounding.as_dict()}

        # ── tools ─────────────────────────────────────────────────────────
        tool_records: list[dict[str, Any]] = []
        if node and node.kind is NodeKind.TOOL and node.tools:
            for name in node.tools:
                spec = self.tools.spec(name)
                if spec is None:
                    continue
                cover = self.tools.cover_for(name, len(self.turns))
                if cover:
                    # Spoken BEFORE the tool runs. This is the whole reason the latency class is
                    # declared rather than measured: the cover has to start first.
                    yield {"type": "cover", "text": cover}
                call = ToolCall(
                    name=name,
                    arguments=_arguments_for(spec, self.state),
                    idempotency_key=f"{self.call_id}:{name}:{len(self.turns)}",
                )
                result = await self.tools.invoke(
                    call, allowed=self.runner.available_tools(self.state) if self.runner else None
                )
                self.trace.record(call, result, covered=cover)
                record = {"name": name, "ok": result.ok, "value": result.value,
                          "error": result.error, "ms": round(result.duration_ms, 1)}
                tool_records.append(record)
                yield {"type": "tool", **record}
                break       # one tool per turn; a chain of them is a flow, not a turn
        timing.mark("tools")

        # ── transition ────────────────────────────────────────────────────
        moved_to, refused = "", ""
        if marker and self.runner and self.state:
            try:
                before = self.state.node_id
                self.runner.transition(self.state, marker)
                moved_to = self.state.node_id
                yield {"type": "moved", "from": before, "to": moved_to}
            except GuardrailError as exc:
                # The model proposed something the graph forbids. Refusing is the feature.
                refused = str(exc)
                log.info("refused transition on %s: %s", self.call_id, refused)
                yield {"type": "refused", "reason": refused}

        if self.state and (self.state.ended or self.state.transferred):
            self.ended = True

        self.history.append(Turn("assistant", reply))
        record = TurnRecord(
            caller=safe_text, agent=reply, spoken=speakable(reply), timing=timing,
            node=node.id if node else "", moved_to=moved_to,
            citations=[_citation(h) for h in hits], tools=tool_records,
            redacted=removed, refused=refused, grounding=grounding,
        )
        self.turns.append(record)
        yield {"type": "done", **record.as_dict(), "ended": self.ended}

    @property
    def transcript(self) -> list[dict[str, str]]:
        return [t.as_dict() for t in self.history]


def _citation(hit: Hit) -> dict[str, Any]:
    return {
        "document": hit.chunk.document_title,
        "document_id": hit.chunk.document_id,
        "text": hit.chunk.text,
        "score": round(hit.score, 3),
        "via": hit.via,
    }


def _arguments_for(spec: Any, state: Any) -> dict[str, Any]:
    """Fill a tool's required arguments from what the call has collected.

    Deliberately not asking the model to emit JSON. A 1.5B model producing a tool call as
    structured text fails often enough to be the least reliable part of the system, and
    everything these tools need has already been captured by a COLLECT node -- which validated
    it on the way in. Where a value is genuinely missing the tool reports it and the agent asks.
    """
    if state is None:
        return {}
    required = spec.parameters.get("required", []) if isinstance(spec.parameters, dict) else []
    return {key: state.collected[key] for key in required if key in state.collected}


def _one_or_two_sentences(text: str, limit: int = 2) -> str:
    """Trim a reply to what a person can absorb over the phone.

    The system prompt already asks for this and a small model does not always comply. Enforcing
    it here rather than trusting the prompt is the difference between an agent that occasionally
    monologues and one that cannot.
    """
    import re

    text = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= limit:
        return text
    return " ".join(sentences[:limit])
