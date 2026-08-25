"""The HTTP and WebSocket API for the whole platform.

SHAPE: thin handlers over `Platform`. Anything with a lifetime longer than a request — the model,
the knowledge bases, the live calls — lives there, and a handler that starts doing real work is a
handler in the wrong place.

TWO KINDS OF ENDPOINT, and the difference matters:

  REST        Configuration and history. Agents, documents, numbers, campaigns, past calls.
              Ordinary request/response, ordinary caching, nothing clever.
  WEBSOCKET   A live call. Bidirectional because voice needs it, and streaming because the
              entire latency argument of this project depends on the first token reaching the
              browser rather than the last. `/ws/call/{id}` carries text and audio alike.

The benchmark endpoints from the original tool are still here. They are what makes the turn-taking
claim checkable, and a product that hides its own measurements is the thing this repo argues
against.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agents.support import build_flow, build_registry
from ..brain.contact import check as contact_check
from ..compliance.redact import redact
from ..eval.endpointing import CORPUS, ablate, run, sweep
from ..flow.graph import GuardrailError
from ..platform import AtCapacity, Platform, _flow_from_dict, _flow_to_dict
from ..scheduling.calendar import as_dict as as_slot_dict
from ..scheduling.calendar import available
from ..sim.call import CANNED_CALLS, replay
from ..turn.endpointing import EndpointConfig, Endpointer, completion_score

log = logging.getLogger("dialtone.server")

platform: Platform | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model and the knowledge bases before reporting ready.

    Warming runs as a background task rather than blocking startup, so the process binds its port
    immediately and `/api/health` can honestly report "starting". A server that refuses
    connections for twenty seconds looks broken; one that answers "not ready yet" is diagnosable.
    """
    global platform
    # Configurable rather than hardcoded. Two reasons, and the second is the one that made this
    # worth changing: an operator running two agents from one checkout needs two databases, and
    # a test needs a scratch one with no model behind it -- eighty seconds of weight loading per
    # test run is the difference between a suite that gets run and one that does not.
    platform = Platform(
        os.environ.get("DIALTONE_DB", "dialtone.db"),
        use_local_model=os.environ.get("DIALTONE_NO_MODEL") != "1",
    )
    task = asyncio.create_task(platform.warm())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    platform.store.close()


app = FastAPI(title="dialtone", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def P() -> Platform:
    if platform is None:                                # pragma: no cover — lifespan guarantees it
        raise HTTPException(503, "platform not initialised")
    return platform


# ── health and overview ──────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict[str, Any]:
    p = P()
    return {
        "ok": True,
        "service": "dialtone",
        "version": "0.2.0",
        "status": p.status,
        "model": getattr(p.brain, "model_name", "scripted"),
        "warm_seconds": round(p.warm_seconds, 1),
        "live_calls": len(p.calls),
        # Published, the way every commercial platform publishes theirs. A limit nobody can see
        # is indistinguishable from no limit until the day it is hit.
        "capacity": p.capacity,
        "voice": {
            "engine": "kokoro-82m" if p.voice.ready else "browser",
            "ready": p.voice.ready,
            "available": p.voice.available,
        },
    }


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    p = P()
    data = p.store.overview()
    data["status"] = p.status
    data["live_calls"] = len(p.calls)
    return data


# ── agents ───────────────────────────────────────────────────────────────────
# ── input limits ─────────────────────────────────────────────────────────────
# EVERY STRING THAT REACHES THE DATABASE IS BOUNDED. Without this, `POST /api/agents` with a
# twenty-thousand-character name is a 200: it is stored, it is rendered into every dropdown, and
# it goes into the system prompt on every turn of every call. Nothing here is a security control
# -- this is a local single-tenant tool -- it is the difference between a field with a shape and
# a field that is whatever arrived.
#
# The numbers are generous on purpose. They exist to stop the absurd, not to argue with a
# legitimately long business name.
SHORT = 120        # a name, a label, a voice
LINE = 400         # a greeting, a persona
TITLE = 200        # a document title
QUERY = 2_000      # a search, a redaction check
DOCUMENT = 500_000 # a knowledge document -- about 80,000 words


class AgentIn(BaseModel):
    name: str = Field("New agent", max_length=SHORT)
    business: str = Field("Acme", max_length=SHORT)
    persona: str = Field("a warm, efficient receptionist", max_length=LINE)
    greeting: str = Field("Hello, how can I help?", max_length=LINE)
    voice: str = Field("female-warm", max_length=SHORT)
    temperature: float = Field(0.4, ge=0.0, le=1.2)
    use_knowledge: bool = True
    status: str = Field("draft", max_length=SHORT)


@app.get("/api/agents")
def list_agents() -> dict[str, Any]:
    return {"agents": P().store.list_agents()}


@app.post("/api/agents")
def create_agent(body: AgentIn) -> dict[str, Any]:
    p = P()
    agent = p.store.create_agent(**body.model_dump(), flow=_flow_to_dict(build_flow()))
    p.knowledge[agent["id"]] = p.knowledge_for(agent["id"])
    return agent


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    agent = P().store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, f"no agent {agent_id}")
    return agent


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
    agent = P().store.update_agent(agent_id, **body)
    if agent is None:
        raise HTTPException(404, f"no agent {agent_id}")
    return agent


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str) -> dict[str, Any]:
    p = P()
    p.knowledge.pop(agent_id, None)
    return {"deleted": p.store.delete_agent(agent_id)}


@app.get("/api/agents/{agent_id}/flow")
def agent_flow(agent_id: str) -> dict[str, Any]:
    p = P()
    agent = p.store.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, f"no agent {agent_id}")
    flow = p.agent_flow(agent) or build_flow()
    return {**_flow_to_dict(flow), "problems": flow.validate(), "paths": flow.paths()}


@app.put("/api/agents/{agent_id}/flow")
def put_agent_flow(agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Save an edited flow, but only if it is structurally valid.

    Refusing to store a broken graph is the point. A flow that saves "mostly fine" fails on a
    live call, on the one path nobody tested — and by then the person who broke it has moved on.
    """
    try:
        flow = _flow_from_dict(body)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"malformed flow: {exc}") from exc

    problems = flow.validate()
    if problems:
        raise HTTPException(422, {"problems": problems})

    P().store.update_agent(agent_id, flow=_flow_to_dict(flow))
    return {"saved": True, "paths": flow.paths()}


# ── knowledge ────────────────────────────────────────────────────────────────
class DocumentIn(BaseModel):
    title: str = Field(max_length=TITLE)
    # Generous: a company handbook is a legitimate upload. The point of the cap is that
    # "how big can a document be?" has an answer.
    body: str = Field(max_length=DOCUMENT)
    source: str = Field("upload", max_length=SHORT)


@app.get("/api/agents/{agent_id}/documents")
def list_documents(agent_id: str) -> dict[str, Any]:
    p = P()
    # A 404, not an empty list. POST to this same path already 404s, and the inconsistency was
    # worse than either answer on its own: asking for the documents of an agent that has been
    # deleted in another tab showed "no documents yet" -- which reads as "this agent has no
    # knowledge" rather than "this agent is gone", and the operator uploads a file into nothing.
    if p.store.get_agent(agent_id) is None:
        raise HTTPException(404, f"no agent {agent_id}")
    return {
        "documents": p.store.list_documents(agent_id),
        "index": p.knowledge_for(agent_id).stats,
    }


@app.post("/api/agents/{agent_id}/documents")
async def add_document(agent_id: str, body: DocumentIn) -> dict[str, Any]:
    p = P()
    if p.store.get_agent(agent_id) is None:
        raise HTTPException(404, f"no agent {agent_id}")

    doc = p.store.add_document(agent_id, body.title, body.body, source=body.source)
    base = p.knowledge_for(agent_id)
    # Embedding is seconds of GPU work; off the loop so the rest of the API stays responsive.
    chunks = await asyncio.to_thread(base.add_document, doc["id"], body.title, body.body)
    p.store.set_document_chunks(doc["id"], chunks)
    return {**doc, "chunks": chunks, "index": base.stats}


@app.delete("/api/agents/{agent_id}/documents/{doc_id}")
def delete_document(agent_id: str, doc_id: str) -> dict[str, Any]:
    p = P()
    removed = p.knowledge_for(agent_id).remove_document(doc_id)
    return {"deleted": p.store.delete_document(doc_id), "chunks_removed": removed}


class SearchIn(BaseModel):
    query: str = Field(max_length=QUERY)
    k: int = Field(3, ge=1, le=10)


@app.post("/api/agents/{agent_id}/knowledge/search")
async def search_knowledge(agent_id: str, body: SearchIn) -> dict[str, Any]:
    """What the agent would retrieve for this question.

    Exposed because "why did it say that?" is the most common question an operator has, and the
    retrieved passages answer it more directly than any log line.
    """
    base = P().knowledge_for(agent_id)
    started = time.perf_counter()
    hits = await asyncio.to_thread(base.search, body.query, k=body.k)
    return {
        "query": body.query,
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "hits": [
            {"document": h.chunk.document_title, "document_id": h.chunk.document_id,
             "text": h.chunk.text, "score": round(h.score, 3), "via": h.via}
            for h in hits
        ],
    }


# ── numbers ──────────────────────────────────────────────────────────────────
class NumberIn(BaseModel):
    e164: str = Field(max_length=SHORT)
    label: str = Field("", max_length=SHORT)
    agent_id: str | None = None


@app.get("/api/numbers")
def list_numbers() -> dict[str, Any]:
    return {"numbers": P().store.list_numbers()}


@app.post("/api/numbers")
def add_number(body: NumberIn) -> dict[str, Any]:
    return P().store.add_number(body.e164, label=body.label, agent_id=body.agent_id)


@app.patch("/api/numbers/{number_id}")
def assign_number(number_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"assigned": P().store.assign_number(number_id, body.get("agent_id"))}


# ── the appointment book ─────────────────────────────────────────────────────
# The half of the product a caller can point at afterwards. A call that "went well" and left
# nothing in a diary is a demo; these endpoints are what makes it a booking.
@app.get("/api/agents/{agent_id}/availability")
def availability(agent_id: str, limit: int = 24) -> dict[str, Any]:
    """The open slots, exactly as the agent sees them on a live call.

    Served from the same function the conversation uses, so the screen and the voice can never
    disagree about what is free -- a caller told one thing and shown another rightly stops
    trusting both.
    """
    store = P().store
    if store.get_agent(agent_id) is None:
        raise HTTPException(404, f"no agent {agent_id}")
    today = date.today()
    slots = available(store.taken_slots(), today=today, now=datetime.now())
    return {
        "today": today.isoformat(),
        "open": [as_slot_dict(s, today) for s in slots[:limit]],
        "total_open": len(slots),
    }


@app.get("/api/appointments")
def list_appointments(agent_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    return {"appointments": P().store.list_appointments(agent_id=agent_id, limit=limit)}


@app.delete("/api/appointments/{appointment_id}")
def cancel_appointment(appointment_id: str) -> dict[str, Any]:
    if not P().store.cancel_appointment(appointment_id):
        raise HTTPException(404, f"no appointment {appointment_id}")
    return {"cancelled": appointment_id}


# ── calls ────────────────────────────────────────────────────────────────────
@app.get("/api/calls")
def list_calls(agent_id: str | None = None, limit: int = 100,
               outcome: str | None = None) -> dict[str, Any]:
    return {"calls": P().store.list_calls(agent_id=agent_id, limit=limit, outcome=outcome)}


@app.get("/api/calls/{call_id}")
def get_call(call_id: str) -> dict[str, Any]:
    call = P().store.get_call(call_id)
    if call is None:
        raise HTTPException(404, f"no call {call_id}")
    return call


class StartCallIn(BaseModel):
    agent_id: str = Field(max_length=SHORT)
    from_number: str = Field("+447700900123", max_length=SHORT)
    channel: str = Field("text", max_length=SHORT)


@app.post("/api/calls")
def start_call(body: StartCallIn) -> dict[str, Any]:
    p = P()
    if p.status != "ready":
        # A 503 with a reason, rather than a call that hangs while the weights load.
        raise HTTPException(503, f"platform is {p.status}; the model is still loading")
    try:
        call_id, greeting = p.start_call(
            body.agent_id, from_number=body.from_number, channel=body.channel
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except AtCapacity as exc:
        # 503 with a Retry-After, not a 500 and not a call that connects into silence. A voice
        # agent that answers and then makes you wait five seconds for the first word is worse
        # than one that never answered: the caller is already committed. See Platform.capacity.
        raise HTTPException(503, str(exc), headers={"Retry-After": "30"}) from exc
    return {"call_id": call_id, "greeting": greeting, "capacity": p.capacity}


@app.post("/api/calls/{call_id}/end")
def end_call(call_id: str) -> dict[str, Any]:
    record = P().end_call(call_id)
    if record is None:
        raise HTTPException(404, f"no live call {call_id}")
    return record


class DetailsIn(BaseModel):
    """What the caller typed rather than said.

    Free-form keys, because the fields are the operator's to choose -- an agent may ask for an
    age, a registration, a party size. Each value is checked against that agent's declared
    schema before anything is stored; see `brain/intake.py`.
    """

    model_config = {"extra": "allow"}

    def values(self) -> dict[str, str]:
        raw = self.model_dump()
        return {
            str(k): str(v)[:SHORT] for k, v in raw.items()
            if v is not None and str(k).isidentifier()
        }


def _field_check(memory: Any, key: str, value: str) -> Any:
    """Validate one value against the schema this call is working to."""
    for declared in memory.fields:
        if declared.key == key:
            return declared.check(value)
    # Not a field this agent asks for. Kept, trimmed, unvalidated -- refusing it would make the
    # API brittle against an operator adding a field the UI has not caught up with.
    return contact_check(key, value)


@app.get("/api/calls/{call_id}/memory")
def call_memory(call_id: str) -> dict[str, Any]:
    """Everything the agent believes about this call, and how it came to believe it."""
    live = P().live_call(call_id)
    if live is None:
        raise HTTPException(404, f"no live call {call_id}")
    return live.conversation.memory.as_dict()


@app.patch("/api/calls/{call_id}/details")
def set_details(call_id: str, body: DetailsIn) -> dict[str, Any]:
    """Take the caller's details from the form instead of from the microphone.

    Speech recognition mangles precisely the values that have to be exact -- one real call
    produced "tasty mulasson" for a surname and "abc iphone com" for an email address. Typing
    them is not a lesser path to the same place; it is the only way those fields are ever right,
    which is why a typed value outranks anything heard and the agent stops asking once it is in.
    """
    live = P().live_call(call_id)
    if live is None:
        raise HTTPException(404, f"no live call {call_id}")

    memory = live.conversation.memory
    problems: dict[str, str] = {}
    warnings: dict[str, str] = {}

    for field_name, value in body.values().items():
        # CHECKED BEFORE IT IS STORED. "hello there" was accepted as a phone number and
        # "not-an-email" as an email address, and both went straight into an appointment -- where
        # the first anyone finds out is a reminder that never arrives and a slot nobody keeps.
        result = _field_check(memory, field_name, value)
        if not result.ok:
            problems[field_name] = result.problem
            continue
        if result.warning:
            warnings[field_name] = result.warning
        memory.tell(field_name, result.value, source="typed")

    # Typing the last missing detail can be the thing that completes a booking, so the same
    # check the conversation runs after a turn runs here too. Otherwise the caller fills the
    # form, nothing happens, and they have to say "yes" again to a question already answered.
    booked = live.conversation.book_if_ready()
    return {
        "memory": memory.as_dict(), "booked": booked,
        # Reported rather than raised: a caller correcting one field of three should not lose
        # the other two, so the good values are stored and the bad ones come back named.
        "problems": problems, "warnings": warnings,
    }


@app.websocket("/ws/call/{call_id}")
async def call_socket(socket: WebSocket, call_id: str) -> None:
    """A live call. Caller text in, agent tokens out, everything timed.

    One message per event rather than one per turn: the browser needs the first token to start
    speaking, and a socket that waits for the finished reply would put the entire latency budget
    back where this project started.
    """
    await socket.accept()
    p = P()
    live = p.live_call(call_id)
    if live is None:
        await socket.send_json({"type": "error", "message": f"no live call {call_id}"})
        await socket.close()
        return

    await socket.send_json({
        "type": "ready",
        "agent": live.conversation.config.as_dict(),
        "greeting": live.conversation.config.greeting,
    })

    # The greeting needs a voice too. It was the one line that never got synthesised, so on a
    # voice call the agent opened in silence -- and the browser's own speech engine filled the
    # gap, which is both the wrong voice and the source of the echo the microphone kept hearing.
    if live.channel == "voice" and p.voice.ready:
        from ..brain.speakable import speakable

        await _stream_voice(socket, p, live, speakable(live.conversation.config.greeting))

    try:
        while True:
            message = await socket.receive_json()
            kind = message.get("type")

            if kind == "hangup":
                break

            if kind == "interrupt":
                # The caller talked over the agent. The browser sends what actually came out of
                # the speaker, because only it has the audio clock -- the server knows what it
                # sent, and the gap between those two is the entire point.
                heard = str(message.get("heard") or "")
                trimmed = live.conversation.interrupted(heard)
                await socket.send_json({
                    "type": "interrupted", "heard": heard, "trimmed": trimmed,
                })
                continue

            if kind != "say":
                continue

            text = (message.get("text") or "").strip()
            if not text:
                continue

            await socket.send_json({"type": "caller", "text": text})

            voicing = live.channel == "voice" and p.voice.ready
            speaker = _ClauseSpeaker(socket, p, live) if voicing else None

            async for event in live.conversation.respond(text):
                await socket.send_json(event)

                # SYNTHESISE WHILE THE MODEL IS STILL WRITING. Waiting for the finished reply
                # meant the caller heard nothing until generation AND synthesis had both
                # completed -- around two and a half seconds of silence, and the exact mistake
                # `pipeline/orchestrator.py` spends four hundred words warning against.
                if speaker and event["type"] == "token":
                    await speaker.feed(str(event.get("spoken") or ""))

                if event["type"] == "done":
                    p.store.add_turn(call_id, len(live.conversation.turns) - 1, event)
                    if speaker:
                        # `agent`, not `spoken`. The tokens carried the written text and this
                        # has to be the same string, or every offset after the first rewritten
                        # number lands mid-word. See _ClauseSpeaker.
                        await speaker.finish(str(event.get("agent") or ""))

            if live.conversation.ended:
                await socket.send_json({"type": "ended", "reason": "the flow reached an end"})
                break
    except WebSocketDisconnect:
        # The caller hung up. Entirely normal, and the most common way a call ends.
        log.debug("caller disconnected from %s", call_id)
    finally:
        record = p.end_call(call_id, outcome="completed")
        # The socket may already be closing -- the caller hung up, the tab closed, the network
        # went. Sending into it then raises from deep inside the websockets library and surfaces
        # as "Exception in ASGI application", which looks like a server fault and is not one.
        # Ending a call must never be able to fail; the record is already written by this point.
        try:
            if record:
                await socket.send_json({"type": "summary", **_call_summary(record)})
        except Exception:  # noqa: BLE001 -- the connection is gone; there is nothing to report to
            log.debug("could not deliver the summary for %s; caller already gone", call_id)
        with suppress(Exception):
            await socket.close()


class _ClauseSpeaker:
    """Speaks a reply as the model writes it, one clause at a time.

    THE WHOLE POINT OF STREAMING, applied to the last stage. The model emits tokens over a couple
    of seconds; synthesis of the opening phrase takes about half a second. Done in sequence that
    is a caller listening to silence for the sum of both. Overlapped, the caller hears the first
    words while the model is still deciding the last ones.

    A clause is only handed over once it is COMPLETE. Synthesising a half-written phrase produces
    the wrong intonation -- the model rises at the end of "A routine check-up costs forty" because
    it thinks that is the whole thought -- and the seam is audible when the rest arrives.

    THREE RULES, EACH ONE A CALL THAT WENT WRONG.

    EVERYTHING IS TRACKED IN THE MODEL'S OWN WORDS, never the speech-ready rewrite. There are two
    versions of every reply -- "$45" as written, "forty five dollars" as spoken -- and they have
    different lengths, so a position in one means nothing in the other. Keeping a position in the
    written text and using it to slice the spoken text made a caller hear:

        "A check-up is forty five dollars."
        "y five dollars. Would you like to book one?"

    The gap between the two strings was said twice. So this class keeps the text it has spoken
    VERBATIM rather than an index into it, and `finish` refuses anything that is not a
    continuation of what was already said.

    A BOUNDARY MUST BE FOLLOWED BY A SPACE. Punctuation alone is not a clause ending: "8:30" and
    "$1,200" both contain one, and cutting there produced "We open at eight" ... "thirty and
    close at six" -- the agent reading a time as two separate numbers.

    IT STOPS WHERE THE TRANSCRIPT STOPS. The reply is trimmed to one or two sentences AFTER
    generation, so a speaker that keeps going says a third sentence the caller never sees.
    """

    #: Punctuation that ends a clause -- but only where a space or the end of the text follows.
    _BOUNDARY = re.compile(r"[,.!?;:](?=\s|$)")
    #: Sentence enders, same rule.
    _SENTENCE = re.compile(r"[.!?](?=\s|$)")
    #: The same limit `_one_or_two_sentences` applies to the transcript.
    _MAX_SENTENCES = 2

    def __init__(self, socket: WebSocket, platform: Platform, live: Any) -> None:
        self.socket = socket
        self.platform = platform
        self.live = live
        #: The reply as written, up to the last thing handed to synthesis. The text itself, not
        #: a position in it -- see the class docstring for what a position cost.
        self.said = ""
        self.index = 0
        self.started = time.perf_counter()
        self.first_sent = False

    async def feed(self, visible: str) -> None:
        """Called on every token. Emits audio for any clause that has just completed.

        `visible` is the reply as the model has written it so far, marker-stripped -- the same
        string that grows on screen.
        """
        if not visible.startswith(self.said):
            # Not a continuation of what was spoken. Should not happen; saying more would
            # repeat rather than continue, so say nothing.
            return
        if self._sentences_in(self.said) >= self._MAX_SENTENCES:
            return

        pending = visible[len(self.said):]

        # The opening is cut short deliberately -- it is the only part the caller waits on, and
        # it is short for TWO compounding reasons: less text to wait for the model to write, and
        # less text to synthesise. Measured, both matter: a 21-character opening cost 587ms to
        # generate plus ~500ms waiting for the model to produce it, where a 3-character one cost
        # 302ms in total. Later clauses can be whole, since they are made while this one plays.
        target = 10 if not self.first_sent else 48
        if len(pending) < target:
            return

        cut = self._last_boundary(pending)
        if cut < 0:
            # No boundary yet. For the opening only, break on a word instead: waiting for the
            # model to reach a comma can cost more silence than the whole phrase is worth.
            if self.first_sent or len(pending) < 18:
                return
            cut = pending.rfind(" ", 0, 18)
            if cut < 0:
                return

        clause = pending[: cut + 1]
        if clause.strip():
            await self._advance(self.said + clause)

    async def finish(self, reply: str) -> None:
        """Speak whatever is left once the model has stopped.

        `reply` is the finished text AS WRITTEN -- the `agent` field, not `spoken`. Handing it
        the speech-ready rewrite is the bug this class is built around, so a mismatch is treated
        as "there is nothing safe to add" rather than sliced anyway.
        """
        if reply.startswith(self.said):
            await self._advance(reply)

    # -- internals ---------------------------------------------------------
    async def _advance(self, upto: str) -> None:
        """Speak the part of `upto` that has not been said, stopping at the sentence limit."""
        capped = upto[: self._sentence_limit(upto)]
        clause = capped[len(self.said):]
        if not clause.strip():
            return
        self.said = capped
        await self._say(clause.strip())

    def _last_boundary(self, text: str) -> int:
        matches = list(self._BOUNDARY.finditer(text))
        return matches[-1].start() if matches else -1

    def _sentences_in(self, text: str) -> int:
        return len(self._SENTENCE.findall(text))

    def _sentence_limit(self, text: str) -> int:
        """Offset just past the last sentence the transcript will keep."""
        for count, match in enumerate(self._SENTENCE.finditer(text), start=1):
            if count >= self._MAX_SENTENCES:
                return match.end()
        return len(text)

    async def _say(self, clause: str) -> None:
        from ..brain.speakable import speakable

        try:
            async for clip in self.platform.voice.speak(
                speakable(clause), voice=self.live.conversation.config.voice
            ):
                await self.socket.send_json({
                    "type": "audio",
                    "index": self.index,
                    "text": clip.text,
                    "wav": base64.b64encode(clip.wav).decode("ascii"),
                    "duration_ms": round(clip.duration_ms, 1),
                    "generate_ms": round(clip.generate_ms, 1),
                    # Measured from the moment the caller stopped speaking, not from the moment
                    # synthesis began -- the caller is waiting through the model's thinking too.
                    "first_audio_ms": round((time.perf_counter() - self.started) * 1000, 1)
                    if not self.first_sent else None,
                })
                self.index += 1
                self.first_sent = True
        except WebSocketDisconnect:
            raise
        except Exception:  # noqa: BLE001 -- a voice failure must not end the call
            log.exception("synthesis failed on %s", self.live.call_id)
            await self.socket.send_json({"type": "audio_failed"})



async def _stream_voice(socket: WebSocket, p: Platform, live: Any, text: str) -> None:
    """Synthesise the reply and send it down the socket, chunk by chunk.

    Chunks rather than one file, because the first one is what the caller waits for. Sending a
    finished WAV would mean waiting for the whole reply to synthesise -- about half its spoken
    duration -- and would undo the streaming the rest of this pipeline is built around.

    Base64 over the existing JSON socket rather than a second binary channel: a reply is a
    handful of chunks of tens of kilobytes, and the 33% encoding overhead on a localhost socket
    costs less than the complexity of a second transport.
    """
    if not text.strip():
        return
    started = time.perf_counter()
    try:
        async for clip in p.voice.speak(text, voice=live.conversation.config.voice):
            await socket.send_json({
                "type": "audio",
                "index": clip.index,
                "text": clip.text,
                "wav": base64.b64encode(clip.wav).decode("ascii"),
                "duration_ms": round(clip.duration_ms, 1),
                "generate_ms": round(clip.generate_ms, 1),
                # Only meaningful on the first chunk, and it is the number that matters: how long
                # the caller sat in silence before hearing anything at all.
                "first_audio_ms": round((time.perf_counter() - started) * 1000, 1)
                if clip.index == 0 else None,
            })
    except WebSocketDisconnect:
        raise
    except Exception:  # noqa: BLE001 -- a voice failure must not end the call
        log.exception("synthesis failed on %s", live.call_id)
        await socket.send_json({"type": "audio_failed"})


def _call_summary(record: dict[str, Any]) -> dict[str, Any]:
    turns = record.get("turns", [])
    latencies = sorted(t["timing"].get("total_ms", 0) for t in turns if t.get("timing"))
    return {
        "call_id": record["id"],
        "turns": len(turns),
        "duration_ms": record["duration_ms"],
        "outcome": record["outcome"],
        "sentiment": record["sentiment"],
        "median_turn_ms": latencies[len(latencies) // 2] if latencies else 0,
    }


# ── campaigns ────────────────────────────────────────────────────────────────
class CampaignIn(BaseModel):
    agent_id: str
    name: str
    script: str = ""


@app.get("/api/campaigns")
def list_campaigns() -> dict[str, Any]:
    return {"campaigns": P().store.list_campaigns()}


@app.post("/api/campaigns")
def create_campaign(body: CampaignIn) -> dict[str, Any]:
    return P().store.create_campaign(body.agent_id, body.name, body.script)


@app.post("/api/campaigns/{campaign_id}/contacts")
def add_contacts(campaign_id: str, body: dict[str, Any]) -> dict[str, Any]:
    added = P().store.add_contacts(campaign_id, body.get("contacts", []))
    return {"added": added}


@app.get("/api/campaigns/{campaign_id}/contacts")
def campaign_contacts(campaign_id: str) -> dict[str, Any]:
    return {"contacts": P().store.campaign_contacts(campaign_id)}


@app.patch("/api/campaigns/{campaign_id}")
def set_campaign_status(campaign_id: str, body: dict[str, Any]) -> dict[str, Any]:
    P().store.set_campaign_status(campaign_id, body.get("status", "draft"))
    return {"ok": True}


# ── compliance ───────────────────────────────────────────────────────────────
class RedactIn(BaseModel):
    text: str = Field(max_length=QUERY)


@app.post("/api/redact")
def redact_text(body: RedactIn) -> dict[str, Any]:
    result = redact(body.text)
    return {
        "text": result.text,
        "clean": result.clean,
        "findings": [
            {"rule": f.rule, "sensitivity": f.sensitivity.value, "start": f.start,
             "end": f.end, "preview": f.preview}
            for f in result.findings
        ],
    }


# ── the turn-taking benchmark ────────────────────────────────────────────────
@app.get("/api/benchmark/ablation")
def benchmark_ablation() -> dict[str, Any]:
    return {"results": [r.as_dict() for r in ablate()]}


@app.get("/api/benchmark/sweep")
def benchmark_sweep() -> dict[str, Any]:
    return {"results": [r.as_dict() for r in sweep()]}


class ThresholdIn(BaseModel):
    base_silence_ms: float = Field(520, ge=100, le=2000)
    enable_semantic: bool = True
    enable_prosody: bool = True


@app.post("/api/benchmark/custom")
def benchmark_custom(body: ThresholdIn) -> dict[str, Any]:
    config = EndpointConfig(
        base_silence_ms=body.base_silence_ms,
        enable_semantic=body.enable_semantic,
        enable_prosody=body.enable_prosody,
    )
    return {"result": run(Endpointer(config), f"base {body.base_silence_ms:.0f}ms").as_dict()}


@app.get("/api/benchmark/corpus")
def benchmark_corpus() -> dict[str, Any]:
    return {
        "items": [
            {"id": s.id, "transcript": s.transcript, "complete": s.complete,
             "pause_ms": s.pause_ms, "note": s.note,
             "completion_score": round(completion_score(s.transcript)[0], 3),
             "reason": completion_score(s.transcript)[1]}
            for s in CORPUS
        ]
    }


@app.get("/api/benchmark/score")
def benchmark_score(text: str) -> dict[str, Any]:
    """Why the endpointer would or would not respond to this sentence."""
    from ..turn.endpointing import TurnState

    score, reason = completion_score(text)
    verdict = Endpointer().evaluate(TurnState(transcript=text, silence_ms=0.0, speech_ms=800.0))
    return {
        "text": text, "completion": round(score, 3), "reason": reason,
        "threshold_ms": round(verdict.threshold_ms),
        "reading": "complete" if score >= 0.5 else "unfinished",
    }


# ── tools and the simulator ──────────────────────────────────────────────────
class SpeakIn(BaseModel):
    text: str
    voice: str = "female-warm"


@app.post("/api/voice/preview")
async def voice_preview(body: SpeakIn) -> dict[str, Any]:
    """One WAV for the whole text. For auditioning a voice, never for a live call."""
    p = P()
    if not p.voice.ready:
        raise HTTPException(503, "the voice engine is not loaded")
    clip = await p.voice.speak_once(body.text[:400], voice=body.voice)
    if clip is None:
        raise HTTPException(503, "synthesis failed")
    return {
        "wav": base64.b64encode(clip.wav).decode("ascii"),
        "duration_ms": round(clip.duration_ms, 1),
        "generate_ms": round(clip.generate_ms, 1),
    }


@app.get("/api/voice/voices")
def list_voices() -> dict[str, Any]:
    from ..speech.tts import VOICES

    p = P()
    return {
        "engine": "kokoro-82m" if p.voice.ready else "browser",
        "ready": p.voice.ready,
        "voices": [{"id": k, "kokoro": v[0], "lang": v[1]} for k, v in VOICES.items()],
    }


@app.get("/api/tools")
def get_tools() -> dict[str, Any]:
    registry = build_registry()
    return {
        "tools": [
            {**registry.spec(name).as_schema(),
             "latency": registry.spec(name).latency.value,
             "idempotent": registry.spec(name).idempotent,
             "cover": registry.cover_for(name)}
            for name in registry.names
        ]
    }


@app.get("/api/scenarios")
def scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {"id": k, "title": v.title, "description": v.description, "turns": len(v.turns)}
            for k, v in CANNED_CALLS.items()
        ]
    }


@app.post("/api/scenarios/{scenario_id}/run")
async def run_scenario(scenario_id: str) -> dict[str, Any]:
    if scenario_id not in CANNED_CALLS:
        raise HTTPException(404, f"no scenario {scenario_id}")
    return await replay(CANNED_CALLS[scenario_id])


@app.websocket("/ws/scenario/{scenario_id}")
async def scenario_socket(socket: WebSocket, scenario_id: str) -> None:
    """Replay a scripted call, paced so the timing between events stays visible."""
    await socket.accept()
    scenario = CANNED_CALLS.get(scenario_id)
    if scenario is None:
        await socket.send_json({"type": "error", "message": f"no scenario {scenario_id}"})
        await socket.close()
        return
    try:
        result = await replay(scenario)
        await socket.send_json({"type": "start", "scenario": scenario.title})
        previous = 0.0
        for event in result["events"]:
            delay = max(0.0, event["at_ms"] - previous) / 1000 / 6
            previous = event["at_ms"]
            await asyncio.sleep(min(delay, 0.6))
            await socket.send_json({"type": "event", **event})
        await socket.send_json({"type": "done", "summary": result["summary"]})
    except WebSocketDisconnect:
        log.debug("scenario viewer disconnected")
    finally:
        with suppress(RuntimeError):
            await socket.close()


@app.exception_handler(GuardrailError)
def guardrail_handler(request: Any, exc: GuardrailError) -> Any:
    from fastapi.responses import JSONResponse

    # 422, not 500: a guardrail refusal is the system working.
    return JSONResponse(status_code=422, content={"guardrail": str(exc)})
