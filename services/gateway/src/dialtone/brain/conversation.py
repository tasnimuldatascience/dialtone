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
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

# `time` is aliased: the stdlib `time` module is already imported above for perf_counter,
# and datetime's `time` would shadow it.
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any, Protocol

from ..compliance.redact import redact
from ..flow.graph import Flow, FlowRunner, GuardrailError, Node, NodeKind
from ..scheduling.calendar import (
    EVENING_FROM,
    Slot,
    When,
    available,
    match_slot,
    offer_text,
    parse_when,
    suggest,
)
from ..tools.registry import ToolCall, ToolRegistry, ToolTrace
from .grounding import Grounding
from .grounding import check as check_grounding
from .knowledge import Hit, KnowledgeBase
from .llm import Brain, Turn, build_system_prompt, split_marker
from .memory import CallMemory, summarise
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
    #: Set when the caller talked over this reply. `heard` is the part that actually reached
    #: them, which is what the model is told it said -- see Conversation.interrupted.
    interrupted: bool = False
    heard: str = ""
    #: A template placeholder the model wrote, which was replaced before the caller heard it.
    #: Surfaced rather than logged away: it means the knowledge base has a gap, and that is
    #: something an operator can actually fix.
    placeholder: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "caller": self.caller, "agent": self.agent, "spoken": self.spoken,
            "timing": self.timing.as_dict(),
            "node": self.node, "moved_to": self.moved_to, "citations": self.citations,
            "tools": self.tools, "redacted": self.redacted, "refused": self.refused,
            "grounding": self.grounding.as_dict(),
            "interrupted": self.interrupted, "heard": self.heard,
            "placeholder": self.placeholder,
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
        booking: BookingBackend | None = None,
        today: date | None = None,
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
        #: Where appointments actually go. None means the agent may discuss times but not
        #: commit to one, which is a legitimate configuration and must not look like a
        #: broken booking.
        self.booking = booking
        self.today = today or date.today()
        #: The slots put in front of the model on the current turn. Set while the prompt is
        #: built and read after the reply, to check whether the agent offered one of them.
        self._offered: list[Slot] = []
        self.memory = CallMemory(today=self.today)

    # -- scheduling ---------------------------------------------------------
    def _now(self) -> datetime:
        """The clock the diary is read against.

        Tied to `today` rather than read fresh. A call pinned to a date -- a test, a replay of a
        recorded call, a fixture -- must see the diary that date saw; reading the wall clock
        while the date is fixed filters every slot out as already past, and the agent then tells
        the caller it is fully booked for a fortnight.
        """
        real = datetime.now()
        return real if real.date() == self.today else datetime.combine(self.today, clock_time(0, 1))

    def open_slots(self) -> list[Slot]:
        """Slots the practice can actually offer right now."""
        if self.booking is None:
            return []
        return available(self.booking.taken_slots(), today=self.today, now=self._now())

    def _scheduling_note(self) -> str:
        """Real availability, phrased for the model.

        GIVEN AS FACT RATHER THAN HIDDEN BEHIND A TOOL CALL. A small model asked to emit a
        structured tool call gets it right often enough to demo and not often enough to ship,
        and a missed call produces the exact sentence a real transcript captured: "I'm sorry,
        but I don't have access to real-time scheduling information." The open slots are cheap
        to compute and short to express, so the model is simply told them and cannot fail to
        look them up.
        """
        if self.booking is None:
            return ""

        # JUST BOOKED. This is the whole of the agent's job for this turn, so it goes first and
        # alone -- buried in a list of availability the model read past it and asked the caller
        # to confirm a time it had already reserved for them.
        if self.memory.booked_reference:
            return (
                f"THE APPOINTMENT IS NOW BOOKED for {self.memory.proposed_slot}. The reference "
                f"is {self.memory.booked_reference}.\n"
                f"Say three things and nothing else: that it is booked, the day and time, and "
                f"the reference letter by letter. Ask no questions. Offer nothing further."
            )

        slots = self.open_slots()
        if not slots:
            return "Nothing is free in the next two weeks. Offer to take a number instead."

        picks = suggest(self.memory.when, slots)
        exact = match_slot(self.memory.when, slots)
        # Kept for the check after the reply: whatever the agent ends up saying, it can only be
        # taken as an offer if it is one of these.
        self._offered = picks

        # Whether the offer actually answers what they asked for. `suggest` deliberately falls
        # back to other days rather than returning nothing -- a caller who wanted Tuesday will
        # usually take Wednesday -- but the model has to be told WHICH of those two happened.
        #
        # It was not, and the result was a flat contradiction on a real call: the caller asked
        # for tomorrow morning, tomorrow morning was free, and the agent said "we don't have
        # available appointments for tomorrow morning" -- because the prompt said so. The
        # earlier version announced "what they asked for is not free" whenever the hour was
        # missing, which is most of the time, and the model repeated it.
        want = self.memory.when
        satisfied = (
            bool(picks)
            # An hour that was asked for and did not match is the whole answer. `exact` is None
            # at this point, so if they named a time, that time is gone -- and saying "yes, that
            # is available" because the DAY matched is how the agent came to offer eight o'clock
            # at a practice that opens at half past.
            and want.hour is None
            and all(
                (want.day is None or slot.start.date() == want.day)
                and (want.part is None or _part_of(slot.start.hour) == want.part)
                for slot in picks
            )
        )

        # The date is stated three ways -- name, number, and what "tomorrow" resolves to --
        # because a small model given only "Today is Sunday 23 August" still answered "today is
        # already Saturday". Leaving it any room to reason about the calendar is leaving it room
        # to be wrong about the one fact the caller will check.
        tomorrow = self.today + timedelta(days=1)
        lines = [
            # THE JOB, FIRST. Everything below is detail; this is the sentence that stops the
            # agent wandering off into collecting a phone number it cannot spell.
            "YOUR ONLY JOB RIGHT NOW is to agree a time for the appointment.",
            f"TODAY is {self.today.strftime('%A %d %B %Y')}. "
            f"TOMORROW is {tomorrow.strftime('%A %d %B')}. "
            f"Never contradict these two dates or work out a different one.",
            "",
            "REAL availability. These are the ONLY times you may offer, and you must not invent "
            "or alter one:",
            f"  {offer_text(picks, self.today)}",
        ]

        if exact:
            self.memory.proposed_slot = exact.spoken(self.today)
            self.memory.proposed_iso = exact.iso
            lines.append(
                f"The caller has asked for {exact.spoken(self.today)} and it IS free. Say that "
                f"time back to them in one sentence and ask them to confirm it. Ask for nothing "
                f"else."
            )
        elif satisfied and (want.day or want.part):
            # They asked for a window and the window has slots in it. Say yes.
            lines.append(
                "What they asked for IS available -- the times above are exactly what they "
                "asked for. Say yes and offer one or two of them. Never tell them there is "
                "nothing free."
            )
        elif want.day or want.part:
            lines.append(
                "What they asked for is NOT free. Say so in half a sentence, then offer the "
                "closest time from the list above."
            )
        else:
            lines.append("Offer one or two of the times above.")

        # THE FORM COLLECTS THE DETAILS, NOT THE CONVERSATION. Speech recognition mangles
        # precisely the values that have to be exact -- a real call produced "tasty mulasson"
        # for a surname and "abc iphone com" for an email address -- so the agent must not spend
        # turns collecting them badly. It is also the difference between an agent that sounds
        # like a receptionist and one that sounds like a form being read aloud.
        lines.append(
            "NEVER ask for a name, a phone number, an email address, or how to spell anything. "
            "The caller types those on screen and you will be told them. Asking is WRONG even "
            "if you do not know them yet. Talk only about the time."
        )
        return "\n".join(lines)

    # -- prompt ------------------------------------------------------------
    def _system_prompt(self, knowledge_text: str) -> str:
        node = self.runner.node(self.state) if self.runner and self.state else None
        transitions = [e.to for e in node.edges] if node else []

        # Memory first, then availability. Both are FACTS about this call and neither is
        # optional -- the model is not being asked to remember, it is being told.
        extra = "\n\n".join(
            part for part in (self.memory.as_prompt(), self._scheduling_note()) if part
        )
        knowledge_text = f"{extra}\n\n{knowledge_text}".strip() if extra else knowledge_text

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

        # Learn from the turn BEFORE the prompt is built, so anything just said reaches
        # this reply rather than only the one after it.
        learned = self.memory.observe(safe_text)

        # ── the caller's answer to an offer, read BEFORE the reply is written ──
        # A slot offered on the previous turn plus a "yes" on this one is a booking, and it has
        # to happen here rather than after generation. It used to run at the end of the turn:
        # the appointment was made, correctly, and the agent -- which had already written its
        # reply -- said "could you please confirm your preferred time?" to a caller who had just
        # confirmed it. The reference only appeared a turn later.
        #
        # Now the booking is in the prompt before the model writes, so it can say what happened.
        early_booked = None
        if self.memory.proposed_slot and not self.memory.booked_reference and _confirms(safe_text):
            self.memory.slot_confirmed = True
            early_booked = self.book_if_ready()
            if early_booked:
                yield {"type": "booked", **early_booked}
            elif self.memory.ready_to_book:
                yield {"type": "booking_failed", "reason": "that slot was just taken"}
        if learned:
            yield {"type": "learned", "fields": learned, "memory": self.memory.as_dict()}

        # Older turns are compressed rather than dropped. A caller who explained why they
        # rang nine turns ago should not have to explain again because the window moved.
        if len(self.history) > 14:
            older = [(t.content, "") for t in self.history[:-12] if t.role == "user"]
            self.memory.summary = summarise(older)

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

        # A reply with a template placeholder in it must never reach a caller. Replaced whole
        # rather than patched: "Northgate Dental is located at" with the bracket cut out is a
        # sentence that stops mid-thought, which is not better.
        placeholder = find_placeholder(reply)
        if placeholder:
            log.warning("placeholder %r in reply on %s", placeholder, self.call_id)
            reply = _NO_ANSWER

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

        if not moved_to:
            # The model said nothing about moving on. If the step is finished, move anyway --
            # see _advance for why this is not the model's decision to make.
            before = self.state.node_id if self.state else ""
            moved_to = self._advance(tuple(t["name"] for t in tool_records))
            if moved_to:
                yield {"type": "moved", "from": before, "to": moved_to}

        if self.state and (self.state.ended or self.state.transferred):
            self.ended = True

        # ── booking ──
        # Decided HERE, not by the model. Confirming an appointment is the one
        # irreversible act on the call, so a model that hallucinates a Thursday at nine
        # cannot bring one into existence.
        # What the agent just offered, so the caller's answer NEXT turn has something to attach
        # to. The confirmation itself was handled before generation -- see above.
        if not self.memory.proposed_slot:
            self.memory.proposed_slot = self._slot_the_agent_offered(reply)

        booked = early_booked
        if booked is None and self.memory.proposed_slot and _confirms(safe_text):
            # The agent offered a slot and the caller had ALREADY agreed in the same breath
            # ("yes, tomorrow morning is fine"). Rare, but it is a booking.
            self.memory.slot_confirmed = True
            booked = self.book_if_ready()
            if booked:
                yield {"type": "booked", **booked}

        self.history.append(Turn("assistant", reply))
        record = TurnRecord(
            caller=safe_text, agent=reply, spoken=speakable(reply), timing=timing,
            node=node.id if node else "", moved_to=moved_to,
            citations=[_citation(h) for h in hits], tools=tool_records,
            redacted=removed, refused=refused, grounding=grounding,
            placeholder=placeholder,
        )
        self.turns.append(record)
        yield {
            "type": "done", **record.as_dict(), "ended": self.ended,
            "memory": self.memory.as_dict(), "booked": booked,
        }

    def _slot_the_agent_offered(self, reply: str) -> str:
        """The time the agent just named, if it is one it was actually given.

        THE OTHER HALF OF "THE MODEL PROPOSES, CODE DECIDES". Booking already refuses times the
        model invents. But it was ALSO ignoring times the model proposed correctly:

            caller:  can I come tomorrow morning?
            agent:   Of course! Tomorrow morning at eight thirty would be perfect.
            caller:  yes that works
                     -> nothing booked; proposed_slot was still empty

        `proposed_slot` was only ever set when the CALLER named a precise time. When the agent
        made the offer and the caller simply agreed -- which is the ordinary shape of the
        conversation, and the shape the prompt asks for -- there was no record of what had been
        agreed to, so the confirmation had nothing to attach to.

        The agent's own sentence is parsed and matched against the slots it was handed this
        turn. It cannot conjure a time this way: a reply naming something that is not in that
        list matches nothing and is ignored.
        """
        offered = getattr(self, "_offered", None)
        if not offered:
            return ""
        heard = parse_when(reply, self.today)
        if heard.hour is None:
            return ""
        # No day named in the reply means the day under discussion.
        if heard.day is None:
            heard = When(day=self.memory.when.day, hour=heard.hour,
                         minute=heard.minute, part=heard.part)
        slot = match_slot(heard, offered)
        if slot is None:
            return ""
        self.memory.proposed_iso = slot.iso
        return slot.spoken(self.today)

    # -- moving through the flow -------------------------------------------
    #: How a node's `collects` name maps onto what this call actually knows. The graph names
    #: values in the operator's vocabulary; the call holds them in the agent's. Anything not
    #: listed falls through to `state.collected`, which is what a custom flow with its own
    #: field names will use.
    _COLLECTS: dict[str, str] = {
        "reason": "reason", "name": "name", "phone": "phone", "email": "email",
    }

    def _collected(self, key: str) -> str:
        """The value this call has for a node's `collects` field, if any."""
        if key in self._COLLECTS:
            return self.memory.get(self._COLLECTS[key])
        if key in ("preferred_day", "when", "day", "time", "slot"):
            when = self.memory.when
            if when.day or when.hour is not None or when.part:
                return self.memory.proposed_slot or str(when.day or when.part or when.hour)
            return ""
        if key in ("confirmed", "confirmation"):
            return "yes" if self.memory.slot_confirmed else ""
        return str(self.state.collected.get(key, "")) if self.state else ""

    def _node_satisfied(self, node: Node, ran: tuple[str, ...]) -> bool:
        """Has this step done what it is there to do?

        `ran` is the tools that fired on the turn being finished. It is passed in rather than
        read from `self.turns`, because the record for this turn is appended AFTER the
        transition is decided -- reading it there made every step advance one turn late, which
        on a four-turn booking is the difference between reaching the booking node and not.
        """
        if node.kind is NodeKind.COLLECT:
            return bool(node.collects) and bool(self._collected(node.collects))
        if node.kind is NodeKind.TOOL:
            # Done once its tool has run, or -- for the booking node -- once the thing the tool
            # exists to produce actually exists.
            if "book_appointment" in node.tools:
                return bool(self.memory.booked_reference)
            return any(name in node.tools for name in ran)
        if node.kind is NodeKind.SPEAK:
            # A reply has just been generated from it, so it has said its piece.
            return True
        return False                         # TRANSFER and END are terminal

    def _advance(self, ran: tuple[str, ...] = ()) -> str:
        """Move to the next step when the current one is finished. Returns the new node, or "".

        WHY CODE DECIDES THIS. The graph was built to be driven by the model emitting `[[node_id]]`
        at the end of a reply. It never did. A 1.5B model given a prompt that opens with "one or
        two sentences, never use markdown, this is a phone call" does not then append a bracketed
        token, and a trace of a real booking call showed every single turn still sitting on
        `greet` with no tool ever called -- so `offer_slots` and `book` were unreachable and the
        flow was decoration.

        This is the same conclusion the booking already reached: the model proposes, code decides.
        The graph is not weakened by it -- every move still goes through `FlowRunner.transition`,
        so an edge the graph does not permit is still refused. What changes is who proposes.

        A model-emitted marker is still honoured if one ever arrives; this only fires when the
        current step is demonstrably complete and the model has said nothing.
        """
        if not (self.runner and self.state):
            return ""

        moved = ""
        # SEVERAL STEPS AT ONCE, because a caller can answer several at once. "Hi, I'd like to
        # book a cleaning tomorrow at ten thirty" satisfies the reason AND the timing in one
        # sentence, and a flow that advances one node per turn would spend three more turns
        # asking for things it already has.
        #
        # Bounded by the number of nodes: a graph with a cycle in it must not spin here.
        for _ in range(len(self.runner.flow.nodes)):
            if self.state.ended or self.state.transferred:
                break
            node = self.runner.node(self.state)
            if not self._node_satisfied(node, ran):
                break

            # Hand the value to the GRAPH as well. `transition` refuses to leave a collect node
            # whose value is missing from `state.collected` -- that guardrail is the point of the
            # node, and it reads the graph's own store rather than the call's memory. Writing it
            # here keeps the two in step wherever _advance is called from.
            if node.kind is NodeKind.COLLECT and node.collects:
                self.state.collected.setdefault(node.collects, self._collected(node.collects))

            legal = self.runner.legal_transitions(self.state)
            if not legal:
                break

            # The happy path first: graph authors put the ordinary continuation at the top, and
            # the escape hatches -- handoff, retry, goodbye -- after it.
            try:
                self.runner.transition(self.state, legal[0].to)
            except GuardrailError:                  # pragma: no cover -- legal_transitions gave it
                break
            moved = self.state.node_id
        return moved

    def interrupted(self, heard: str) -> bool:
        """The caller talked over the agent. Keep only what they actually heard.

        THE POINT OF THE WHOLE FEATURE, and it is one line of state. The agent WROTE a sentence;
        the caller only HEARD the part that played before they cut in:

            written:  "I've got Tuesday at nine, Tuesday at ten thirty, Wednesday at noon..."
            heard:    "I've got Tuesday at..."

        Leave the written version in the history and the model believes it offered four times. Two
        turns later it says "as I mentioned, Wednesday at noon" and the caller has no idea what it
        is talking about -- which is worse than the interruption itself, because it is the moment
        the caller stops believing the agent was listening.

        WHY THE BROWSER DECIDES WHAT WAS HEARD. Only it has the audio clock. The server knows what
        it sent, not what came out of a speaker, and the gap between those is exactly the thing
        being measured. `turn/bargein.py` does the same job for the simulator, where the clock is
        synthetic and the server owns it.

        Returns whether anything was actually trimmed.
        """
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index].role != "assistant":
                continue
            written = self.history[index].content
            kept = heard.strip()
            if len(kept) >= len(written.strip()):
                # They cut in after it had finished, or close enough. Nothing to trim.
                return False
            # An ellipsis, so the model can see its own sentence was cut off rather than
            # concluding it phrased something strangely.
            self.history[index] = Turn("assistant", f"{kept}…" if kept else "…")
            if self.turns:
                self.turns[-1].interrupted = True
                self.turns[-1].heard = kept
            return True
        return False

    def book_if_ready(self) -> dict[str, Any] | None:
        """Book, if and only if everything a booking needs is actually in hand.

        The single gate. Called after a spoken turn AND after the details form is submitted,
        because those are the two ways the last missing piece arrives and the caller should not
        have to repeat themselves just because they finished on the keyboard rather than out
        loud.
        """
        if self.booking is None or self.memory.booked_reference or not self.memory.ready_to_book:
            return None
        return self._commit_booking()

    def _commit_booking(self) -> dict[str, Any] | None:
        """Reserve the slot under discussion. Returns None if it went in the meantime."""
        if self.booking is None:
            return None
        # The slot that was actually agreed, not a re-derivation from what the caller said --
        # see CallMemory.proposed_iso. Still checked against the live calendar, so a slot taken
        # since it was offered fails here rather than double-booking.
        free = {s.iso: s for s in self.open_slots()}
        slot = free.get(self.memory.proposed_iso) or match_slot(self.memory.when, list(free.values()))
        if slot is None:
            return None

        record = self.booking.book(
            slot.iso,
            call_id=self.call_id,
            patient_name=self.memory.get("name"),
            phone=self.memory.get("phone"),
            email=self.memory.get("email"),
            reason=self.memory.get("reason") or "appointment",
        )
        if record is None:
            return None

        self.memory.booked_reference = record["reference"]
        return {
            "reference": record["reference"],
            "starts_at": record["starts_at"],
            "spoken": slot.spoken(self.today),
            "name": record["patient_name"],
            "reason": record["reason"],
        }

    @property
    def transcript(self) -> list[dict[str, str]]:
        return [t.as_dict() for t in self.history]


class BookingBackend(Protocol):
    """What the conversation needs in order to reserve a slot.

    Two methods, so the calendar and the conversation can each be tested without a database and
    without each other.
    """

    def taken_slots(self) -> set[str]: ...
    def book(self, starts_at: str, **fields: Any) -> dict[str, Any] | None: ...


_CONFIRMS = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|please|ok|okay|correct|go ahead|book it|sounds good|"
    r"perfect|great|do it|confirm)\b",
    re.IGNORECASE,
)
_DECLINES = re.compile(
    r"\b(?:no|nope|not|cancel|wait|hold on|actually|instead|rather|change)\b", re.IGNORECASE
)



def _part_of(hour: int) -> str:
    """The part of day an hour falls in, by the SAME boundary the calendar offers on.

    Duplicating this constant is how a caller ends up asked to confirm "Thursday evening" and
    then offered five in the afternoon.
    """
    return "morning" if hour < 12 else "afternoon" if hour < EVENING_FROM else "evening"

def _confirms(text: str) -> bool:
    """Did the caller agree?

    A decline anywhere in the sentence wins. "Yes, but not Thursday" contains a yes and is not
    one, and booking on it puts somebody in a slot they explicitly refused.
    """
    return bool(_CONFIRMS.search(text)) and not _DECLINES.search(text)


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


#: Text a model writes when it is filling in a form rather than answering a question.
#: Square brackets and braces are the common shapes; the bare words are the ones that show up
#: without any punctuation around them.
_PLACEHOLDER = re.compile(
    r"\[[^\]]{0,80}\]"                                  # [insert location]
    r"|\{[^}]{0,80}\}"                                   # {address}
    r"|\b(?:insert|enter|your|the)\s+\w+\s+here\b"      # insert address here
    r"|\bTBD\b|\bTBC\b|\bXXX+\b|\bN/?A\b",           # TBD, XXX, N/A
    re.IGNORECASE,
)


def find_placeholder(reply: str) -> str:
    """The first template placeholder in a reply, or "".

    WHY THIS IS CHECKED AT ALL. On a thirty-turn call the caller asked "where are you exactly?"
    and the agent answered:

        "Northgate Dental is located at [insert location], where we provide..."

    The knowledge base has no address in it, so the model did what a model does with a gap in a
    form: it wrote the shape of the answer. Down a phone line that is read out loud, bracket by
    bracket, and it is the single most obviously broken thing this system can say.

    The grounding check cannot catch it -- that verifies NUMBERS against retrieved passages, and
    a placeholder has no number in it. So it is caught here, by shape.
    """
    match = _PLACEHOLDER.search(reply)
    return match.group(0) if match else ""


#: What to say instead. Honest about not knowing rather than inventing, which is the same rule
#: the rest of the system follows for prices.
_NO_ANSWER = "I don't have that to hand, but I can check and get back to you."


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
