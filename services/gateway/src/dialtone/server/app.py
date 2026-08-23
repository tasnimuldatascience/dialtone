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
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agents.support import build_flow, build_registry
from ..compliance.redact import redact
from ..eval.endpointing import CORPUS, ablate, run, sweep
from ..flow.graph import GuardrailError
from ..platform import Platform, _flow_from_dict, _flow_to_dict
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
    platform = Platform("dialtone.db", use_local_model=True)
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
    }


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    p = P()
    data = p.store.overview()
    data["status"] = p.status
    data["live_calls"] = len(p.calls)
    return data


# ── agents ───────────────────────────────────────────────────────────────────
class AgentIn(BaseModel):
    name: str = "New agent"
    business: str = "Acme"
    persona: str = "a warm, efficient receptionist"
    greeting: str = "Hello, how can I help?"
    voice: str = "female-warm"
    temperature: float = Field(0.4, ge=0.0, le=1.2)
    use_knowledge: bool = True
    status: str = "draft"


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
    title: str
    body: str
    source: str = "upload"


@app.get("/api/agents/{agent_id}/documents")
def list_documents(agent_id: str) -> dict[str, Any]:
    p = P()
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
    query: str
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
    e164: str
    label: str = ""
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
    agent_id: str
    from_number: str = "+447700900123"
    channel: str = "text"


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
    return {"call_id": call_id, "greeting": greeting}


@app.post("/api/calls/{call_id}/end")
def end_call(call_id: str) -> dict[str, Any]:
    record = P().end_call(call_id)
    if record is None:
        raise HTTPException(404, f"no live call {call_id}")
    return record


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

    try:
        while True:
            message = await socket.receive_json()
            kind = message.get("type")

            if kind == "hangup":
                break
            if kind != "say":
                continue

            text = (message.get("text") or "").strip()
            if not text:
                continue

            await socket.send_json({"type": "caller", "text": text})
            async for event in live.conversation.respond(text):
                await socket.send_json(event)
                if event["type"] == "done":
                    p.store.add_turn(call_id, len(live.conversation.turns) - 1, event)

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
    text: str


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
