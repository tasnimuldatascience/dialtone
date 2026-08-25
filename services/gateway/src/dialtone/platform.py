"""The platform: one object that owns agents, their knowledge, and every live call.

WHY THIS EXISTS RATHER THAN WIRING IT IN THE ROUTE HANDLERS. Three things have awkward lifetimes
and all three are shared:

  THE MODEL          ~3 GB of weights and five seconds to load. Loaded once, at startup, and
                     shared by every call. Loading it per request would be absurd; loading it
                     lazily would make the first caller of the day wait.
  KNOWLEDGE BASES    One per agent, rebuilt from the database on boot. Embedding a document set
                     takes seconds, so it is not something a request can do inline.
  LIVE CALLS         A conversation outlives the request that created it. It has to live
                     somewhere that is not a route handler's stack frame.

Route handlers should read as "take this input, ask the platform, return the answer". Everything
below is what makes that possible.

STARTUP IS EXPLICIT AND ORDERED. `warm()` loads the model, then the encoder, then rebuilds every
knowledge base. It is called once from the app's lifespan hook, and until it finishes the API
reports itself as starting rather than pretending to be ready — a health check that goes green
before the model can answer is worse than no health check.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents.support import build_flow, build_registry
from .brain.conversation import AgentConfig, Conversation
from .brain.intake import load as load_intake
from .brain.knowledge import KnowledgeBase
from .brain.llm import Brain, LocalBrain, ScriptedBrain
from .flow.graph import Edge, Flow, Node, NodeKind
from .speech.tts import Synthesizer
from .store.db import Store

log = logging.getLogger("dialtone.platform")


@dataclass(slots=True)
class LiveCall:
    """A conversation in progress, plus the bookkeeping the store needs when it ends."""

    call_id: str
    agent_id: str
    conversation: Conversation
    started: float = field(default_factory=time.perf_counter)
    channel: str = "text"

    @property
    def duration_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


@dataclass(slots=True)
class StoreBooking:
    """The store, in the shape a conversation needs to book with.

    An adapter rather than passing the store straight in, for two reasons: the conversation
    should not be able to reach the rest of the database, and this is where the agent is bound
    to the booking, so a call cannot write an appointment against somebody else's practice.
    """

    store: Store
    agent_id: str

    def taken_slots(self) -> set[str]:
        return self.store.taken_slots()

    def book(self, starts_at: str, **fields: Any) -> dict[str, Any] | None:
        return self.store.book(self.agent_id, starts_at, **fields)


class Platform:
    """Everything the API serves, and everything that outlives a request."""

    def __init__(self, db_path: str | Path = "dialtone.db", *, use_local_model: bool = True):
        self.store = Store(db_path)
        self.use_local_model = use_local_model
        self.brain: Brain = (
            LocalBrain() if use_local_model else ScriptedBrain(
                replies=("Of course — let me help you with that.",)
            )
        )
        self.knowledge: dict[str, KnowledgeBase] = {}
        #: Neural voice. Shares the process with the language model rather than running as a
        #: separate service: it is 330MB and CPU-bound, so it costs nothing the GPU wanted.
        self.voice = Synthesizer()
        self.calls: dict[str, LiveCall] = {}
        self.status = "starting"
        self.warm_seconds = 0.0
        self._warm_lock = asyncio.Lock()

    # -- startup -----------------------------------------------------------
    async def warm(self) -> None:
        """Load the model and rebuild every knowledge base. Called once, at boot."""
        async with self._warm_lock:
            if self.status == "ready":
                return
            started = time.perf_counter()

            if isinstance(self.brain, LocalBrain):
                # Off the event loop: loading weights is several seconds of blocking work and
                # the health endpoint should stay answerable throughout.
                await asyncio.to_thread(self.brain.load)

            if self.voice.available:
                await asyncio.to_thread(self.voice.load)

            if not self.store.list_agents():
                self._seed()

            for agent in self.store.list_agents():
                await asyncio.to_thread(self._rebuild_knowledge, agent["id"])

            self.warm_seconds = time.perf_counter() - started
            self.status = "ready"
            log.info("platform ready in %.1fs", self.warm_seconds)

    def _rebuild_knowledge(self, agent_id: str) -> KnowledgeBase:
        base = KnowledgeBase()
        for doc in self.store.list_documents(agent_id):
            full = self.store.get_document(doc["id"])
            if full:
                chunks = base.add_document(full["id"], full["title"], full["body"])
                self.store.set_document_chunks(full["id"], chunks)
        self.knowledge[agent_id] = base
        return base

    def knowledge_for(self, agent_id: str) -> KnowledgeBase:
        if agent_id not in self.knowledge:
            self._rebuild_knowledge(agent_id)
        return self.knowledge[agent_id]

    # -- agents ------------------------------------------------------------
    def agent_config(self, agent: dict[str, Any]) -> AgentConfig:
        return AgentConfig(
            id=agent["id"], name=agent["name"], business=agent["business"],
            persona=agent["persona"], greeting=agent["greeting"], voice=agent["voice"],
            temperature=agent["temperature"], use_knowledge=agent["use_knowledge"],
            # An agent with no schema of its own gets the default, so an existing database
            # keeps working and an operator opts in by editing the fields rather than by
            # having to declare them before the agent will run at all.
            intake=load_intake(agent.get("intake")),
        )

    def agent_flow(self, agent: dict[str, Any]) -> Flow | None:
        raw = agent.get("flow")
        if not raw:
            return None
        try:
            return _flow_from_dict(raw)
        except (KeyError, ValueError) as exc:
            # A malformed stored flow must not take the agent offline. It runs without a graph,
            # which is degraded but answerable, and the problem is visible in the studio.
            log.warning("agent %s has an unloadable flow: %s", agent["id"], exc)
            return None

    # -- calls -------------------------------------------------------------
    def start_call(self, agent_id: str, *, from_number: str = "",
                   channel: str = "text") -> tuple[str, str]:
        """Begin a call. Returns (call_id, greeting)."""
        agent = self.store.get_agent(agent_id)
        if agent is None:
            raise KeyError(f"no agent {agent_id!r}")

        call_id = self.store.start_call(agent_id, from_number=from_number, channel=channel)
        conversation = Conversation(
            brain=self.brain,
            config=self.agent_config(agent),
            flow=self.agent_flow(agent),
            tools=build_registry(),
            knowledge=self.knowledge_for(agent_id) if agent["use_knowledge"] else None,
            call_id=call_id,
            booking=StoreBooking(self.store, agent_id),
        )
        greeting = conversation.opening()
        self.calls[call_id] = LiveCall(call_id, agent_id, conversation, channel=channel)
        return call_id, greeting

    def end_call(self, call_id: str, *, outcome: str = "completed") -> dict[str, Any] | None:
        live = self.calls.pop(call_id, None)
        if live is None:
            return None
        convo = live.conversation
        escalated = bool(convo.state and convo.state.transferred)
        self.store.end_call(
            call_id,
            outcome="transferred" if escalated else outcome,
            # "Resolved" means the agent finished without handing over. It is the number an
            # operator is actually buying, so it is derived from the flow rather than guessed.
            resolved=bool(convo.state and convo.state.ended) or (not escalated and outcome == "completed"),
            escalated=escalated,
            sentiment=_sentiment(convo),
            summary=_summary(convo),
            duration_ms=live.duration_ms,
        )
        return self.store.get_call(call_id)

    def live_call(self, call_id: str) -> LiveCall | None:
        return self.calls.get(call_id)

    # -- seed --------------------------------------------------------------
    def _seed(self) -> None:
        """A working agent on first boot.

        An empty product is impossible to evaluate — the first screen shows nothing, so nothing
        can be clicked, so nobody finds out whether it works. This is a complete dental practice:
        an agent, its documents, a phone number, and a booking flow.
        """
        agent = self.store.create_agent(
            name="Reception",
            business="Northgate Dental",
            persona="a warm, efficient receptionist",
            greeting="Northgate Dental, how can I help?",
            status="live",
            flow=_flow_to_dict(build_flow()),
        )
        for title, body in SEED_DOCUMENTS.items():
            self.store.add_document(agent["id"], title, body, source="seed")
        self.store.add_number("+12125550142", label="Main line", agent_id=agent["id"])
        self.store.add_number("+12125550188", label="Emergencies", agent_id=agent["id"])
        log.info("seeded starter agent %s", agent["id"])


SEED_DOCUMENTS: dict[str, str] = {
    # ADDED AFTER A REAL CALL. Asked "where are you exactly?", the agent replied "Northgate
    # Dental is located at [insert location]" -- because there was no address anywhere in here
    # and a model with a gap in a form writes the shape of the answer. The placeholder guard in
    # brain/conversation.py now stops that reaching a caller, but the actual fix for "the agent
    # cannot answer this" is to give it the answer.
    # DELIBERATELY NARROW. Parking, step-free access and buses are covered by "Parking and
    # access" and are NOT repeated here. A first draft of this page duplicated all three and
    # contradicted them -- a different street for the car park, different bus numbers -- which
    # is the worst thing a knowledge base can contain: retrieval picks one, grounding verifies
    # against it, and the agent states a wrong fact with complete confidence.
    "Where we are": """
Our address. Northgate Dental is located at 118 Northgate Avenue, New York, NY 10014. If you are
asking where we are, where to find us, or how to get here, this is the page.

We are on the first floor. The entrance is beside the pharmacy.

Getting here by subway. The nearest station is Christopher Street, about four minutes' walk.

Reception is on 212-555-0142. The emergency line is 212-555-0188.
""",
    "Opening hours": """
Northgate Dental is open Monday through Friday, eight thirty in the morning until six in the
evening. We are closed on weekends and on federal holidays.

Late appointments are available on Thursdays until eight in the evening. These are popular and we
recommend booking at least two weeks ahead.

The office is closed for lunch between noon and one o'clock every weekday.
""".strip(),
    "Prices": """
A routine check-up costs seventy five dollars. This includes a full exam, x-rays if needed, and a
cleaning.

A white filling costs between one hundred eighty and three hundred forty dollars, depending on
the size of the cavity.

A hygienist appointment is ninety five dollars for thirty minutes.

There is no charge for a first consultation if you are joining the practice as a new patient.

We accept most PPO insurance plans and we file the claim for you. We do not accept HMO plans. For
patients without insurance we offer an in-house membership plan at twenty nine dollars a month,
which covers two cleanings a year and twenty percent off other treatment.
""".strip(),
    "Emergencies": """
If you have severe toothache, a knocked-out tooth, or facial swelling, call us immediately and we
will find you a same-day emergency appointment. We keep two emergency slots open every morning.

After hours, call the main number and the answering service will page the on-call dentist, who
returns calls within thirty minutes. If you have swelling that is affecting your breathing or
swallowing, go to the emergency room right away.

For a knocked-out adult tooth, keep it in milk and come in within the hour if you possibly can.
""".strip(),
    "Appointments and cancellations": """
Please give us at least twenty four hours notice if you need to cancel. Appointments cancelled
with less notice may be charged a fifty dollar fee.

We send a text reminder two days before every appointment. If you would like to change how we
contact you, tell the front desk and we will update your record.

New patients should arrive fifteen minutes early to complete a medical history form, or fill it
out online beforehand.
""".strip(),
    "Parking and access": """
There is free parking for patients in the lot behind the building, accessed from Northgate Lane.
There are twelve spaces including two accessible spots.

The office is fully accessible on the first floor. There is a step-free entrance on the left hand
side of the building and an elevator to the second floor operatories.

The number forty two and number sixteen buses both stop directly outside.
""".strip(),
}


# ── flow serialisation ───────────────────────────────────────────────────────
def _flow_to_dict(flow: Flow) -> dict[str, Any]:
    return {
        "name": flow.name,
        "start": flow.start,
        "global_tools": list(flow.global_tools),
        "nodes": [
            {
                "id": n.id, "kind": n.kind.value, "objective": n.objective,
                "collects": n.collects, "pattern": n.pattern, "tools": list(n.tools),
                "max_attempts": n.max_attempts,
                "edges": [{"to": e.to, "when": e.when, "condition": e.condition}
                          for e in n.edges],
            }
            for n in flow.nodes.values()
        ],
    }


def _flow_from_dict(raw: dict[str, Any]) -> Flow:
    nodes = {
        n["id"]: Node(
            id=n["id"],
            kind=NodeKind(n["kind"]),
            objective=n.get("objective", ""),
            collects=n.get("collects"),
            pattern=n.get("pattern"),
            tools=tuple(n.get("tools", ())),
            max_attempts=int(n.get("max_attempts", 3)),
            edges=tuple(
                Edge(e["to"], e.get("when", ""), e.get("condition"))
                for e in n.get("edges", ())
            ),
        )
        for n in raw["nodes"]
    }
    return Flow(raw["name"], raw["start"], nodes, tuple(raw.get("global_tools", ())))


# ── call summarisation ───────────────────────────────────────────────────────
# SINGLE WORDS ONLY, and every one of them has to be unambiguous on its own.
#
# "never" and "again" used to be in here -- they arrived as the phrase "never again" and the list
# is split on whitespace, so both became independent cues. The result was that "sorry, say that
# again?" scored as an angry call, which is one of the commonest things anyone says on a phone
# line. A word goes in this list only if hearing it alone would make you think the caller was
# upset.
_NEGATIVE = frozenset("""
angry furious ridiculous unacceptable terrible awful useless complaint complain rude disgusted
appalling worst disgrace sue lawyer refund
""".split())

#: Phrases, matched as phrases. This is where "never again" belongs, and anything else whose
#: meaning lives in the combination rather than in either word.
_NEGATIVE_PHRASES = (
    "waste of time", "not good enough", "speak to a manager", "this is a joke",
    "fed up", "sick of", "no use",
)

#: "Never ... again", with the middle allowed to vary -- "I am never coming here again" is the
#: complaint an operator most wants to find in a list, and it rarely arrives as the bare phrase.
#: Bounded to one clause so it cannot span a whole call and match by accident.
_NEVER_AGAIN = re.compile(r"\bnever\b[^.!?]{0,40}\bagain\b")
_POSITIVE = frozenset("""
thanks thank great perfect lovely brilliant wonderful helpful excellent appreciate cheers
fantastic pleased happy good
""".split())


def _sentiment(convo: Conversation) -> str:
    """A coarse read on how the caller felt.

    Word lists rather than a model, and that is a deliberate trade. Running a classifier here
    would add a model call to every call teardown for a number nobody makes decisions on alone;
    what an operator actually does with this is FILTER — "show me the unhappy calls" — and for
    filtering, cheap and roughly right beats expensive and slightly better.

    Only the CALLER's words are counted. Including the agent's would score every call positive,
    because the agent is unfailingly polite by construction.
    """
    words: list[str] = []
    said: list[str] = []
    for turn in convo.turns:
        words.extend(w.strip(".,!?").lower() for w in turn.caller.split())
        said.append(turn.caller.lower())

    spoken = " ".join(said)
    negative = sum(1 for w in words if w in _NEGATIVE)
    negative += sum(spoken.count(phrase) for phrase in _NEGATIVE_PHRASES)
    negative += len(_NEVER_AGAIN.findall(spoken))
    positive = sum(1 for w in words if w in _POSITIVE)
    if negative > positive:
        return "negative"
    if positive > negative:
        return "positive"
    return "neutral"


def _summary(convo: Conversation) -> str:
    """One line describing what the call was about, for the call list.

    The caller's first substantive turn, trimmed. A generated summary would be nicer and would
    cost a model call per call teardown; the opening line is what a human skimming a call list
    is looking for anyway, because it is the reason they rang.
    """
    for turn in convo.turns:
        text = turn.caller.strip()
        if len(text.split()) >= 3:
            return text[:120]
    return convo.turns[0].caller[:120] if convo.turns else "No caller speech"
