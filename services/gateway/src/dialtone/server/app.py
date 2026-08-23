"""The HTTP and WebSocket surface the studio talks to.

DESIGN NOTE: EVERYTHING HERE IS DERIVED, NOTHING IS DECORATIVE. Each endpoint returns a value
computed from the same code that runs a real call — the benchmark numbers come from replaying
the corpus through the actual endpointer, the simulated call runs the actual orchestrator, the
flow validation is the actual validator that would refuse to load the flow in production.

That constraint is deliberate and it is the difference between a dashboard and a demo. A studio
whose charts are fed by a fixtures file will show green while the system is broken, and the
person it fools most reliably is the person who built it.

THE WEBSOCKET IS THE POINT. A voice agent's behaviour is only legible in time: which frame the
endpointer fired on, how long the model took to first token, where the caller barged in. A
request/response API can show you the outcome; only a stream can show you the decision. So the
call monitor streams every turn event as it happens, and the studio renders them on a timeline.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agents.support import build_flow, build_registry
from ..compliance.redact import redact
from ..eval.endpointing import CORPUS, ablate, run, sweep
from ..flow.graph import Flow, GuardrailError, NodeKind
from ..sim.call import CANNED_CALLS, replay
from ..turn.endpointing import EndpointConfig, Endpointer, completion_score

log = logging.getLogger("dialtone.server")

app = FastAPI(
    title="dialtone",
    version="0.1.0",
    description="A voice-agent platform whose turn-taking is measured, not asserted.",
)

# The studio is served from a different port in development. Locked to localhost origins
# because this API exposes call transcripts.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "dialtone", "version": "0.1.0"}


# ── endpointing benchmark ────────────────────────────────────────────────────
@app.get("/api/benchmark/ablation")
def benchmark_ablation() -> dict[str, Any]:
    """Which signal is doing the work.

    Computed on request rather than cached, because it takes ~40ms and a cached benchmark is a
    benchmark that silently stops reflecting the code.
    """
    return {"results": [r.as_dict() for r in ablate()]}


@app.get("/api/benchmark/sweep")
def benchmark_sweep() -> dict[str, Any]:
    """The latency/false-cutoff curve — the headline claim of the project."""
    return {"results": [r.as_dict() for r in sweep()]}


class ThresholdRequest(BaseModel):
    base_silence_ms: float = Field(520, ge=100, le=2000)
    enable_semantic: bool = True
    enable_prosody: bool = True


@app.post("/api/benchmark/custom")
def benchmark_custom(request: ThresholdRequest) -> dict[str, Any]:
    """Score an arbitrary configuration. Powers the studio's interactive slider.

    Letting the operator move the threshold and watch BOTH numbers move is the fastest way to
    make the central point: latency is not free, and any vendor quoting one number without the
    other is quoting the half of the trade-off that flatters them.
    """
    config = EndpointConfig(
        base_silence_ms=request.base_silence_ms,
        enable_semantic=request.enable_semantic,
        enable_prosody=request.enable_prosody,
    )
    label = f"base {request.base_silence_ms:.0f}ms"
    return {"result": run(Endpointer(config), label).as_dict()}


@app.get("/api/benchmark/corpus")
def benchmark_corpus() -> dict[str, Any]:
    """The labelled corpus itself.

    Published deliberately. A benchmark whose test set is private is a marketing number, and
    the entire argument of this project is that these figures should be checkable.
    """
    return {
        "items": [
            {
                "id": s.id, "transcript": s.transcript, "complete": s.complete,
                "pause_ms": s.pause_ms, "note": s.note,
                "completion_score": round(completion_score(s.transcript)[0], 3),
                "reason": completion_score(s.transcript)[1],
            }
            for s in CORPUS
        ]
    }


# ── flows ────────────────────────────────────────────────────────────────────
def _flow_payload(flow: Flow) -> dict[str, Any]:
    return {
        "name": flow.name,
        "start": flow.start,
        "global_tools": list(flow.global_tools),
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind.value,
                "objective": n.objective,
                "collects": n.collects,
                "pattern": n.pattern,
                "tools": list(n.tools),
                "max_attempts": n.max_attempts,
                "edges": [{"to": e.to, "when": e.when, "condition": e.condition} for e in n.edges],
            }
            for n in flow.nodes.values()
        ],
        "problems": flow.validate(),
        "paths": flow.paths(),
    }


@app.get("/api/flow")
def get_flow() -> dict[str, Any]:
    return _flow_payload(build_flow())


@app.get("/api/tools")
def get_tools() -> dict[str, Any]:
    registry = build_registry()
    return {
        "tools": [
            {
                **(registry.spec(name).as_schema()),
                "latency": registry.spec(name).latency.value,
                "idempotent": registry.spec(name).idempotent,
                "cover": registry.cover_for(name),
            }
            for name in registry.names
        ]
    }


class NodeEdit(BaseModel):
    id: str
    kind: str
    objective: str = ""
    collects: str | None = None
    pattern: str | None = None
    tools: list[str] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class FlowEdit(BaseModel):
    name: str
    start: str
    nodes: list[NodeEdit]
    global_tools: list[str] = Field(default_factory=list)


@app.post("/api/flow/validate")
def validate_flow(edit: FlowEdit) -> dict[str, Any]:
    """Validate an edited flow without loading it.

    The studio calls this on every change, so a broken graph is visible while it is being built
    rather than on the first call that reaches the broken path. Structural mistakes in a
    conversation graph are cheap to find here and extremely expensive to find on a live line.
    """
    from ..flow.graph import Edge, Node

    try:
        nodes = {
            n.id: Node(
                id=n.id,
                kind=NodeKind(n.kind),
                objective=n.objective,
                collects=n.collects,
                pattern=n.pattern,
                tools=tuple(n.tools),
                edges=tuple(
                    Edge(e["to"], e.get("when", ""), e.get("condition")) for e in n.edges
                ),
            )
            for n in edit.nodes
        }
    except (ValueError, KeyError) as exc:
        # A malformed edit is a validation result, not a 500. The studio renders it in the same
        # place as every other problem.
        return {"valid": False, "problems": [f"malformed flow: {exc}"], "paths": []}

    flow = Flow(edit.name, edit.start, nodes, tuple(edit.global_tools))
    problems = flow.validate()
    return {
        "valid": not problems,
        "problems": problems,
        "paths": flow.paths() if not problems else [],
    }


# ── compliance ───────────────────────────────────────────────────────────────
class RedactRequest(BaseModel):
    text: str


@app.post("/api/redact")
def redact_text(request: RedactRequest) -> dict[str, Any]:
    result = redact(request.text)
    return {
        "text": result.text,
        "clean": result.clean,
        "findings": [
            {
                "rule": f.rule, "sensitivity": f.sensitivity.value,
                "start": f.start, "end": f.end, "preview": f.preview,
            }
            for f in result.findings
        ],
    }


# ── simulated calls ──────────────────────────────────────────────────────────
@app.get("/api/calls/scenarios")
def scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {"id": k, "title": v.title, "description": v.description, "turns": len(v.turns)}
            for k, v in CANNED_CALLS.items()
        ]
    }


@app.post("/api/calls/{scenario_id}/run")
async def run_call(scenario_id: str) -> dict[str, Any]:
    if scenario_id not in CANNED_CALLS:
        return {"error": f"no scenario {scenario_id!r}"}
    return await replay(CANNED_CALLS[scenario_id])


@app.websocket("/ws/call/{scenario_id}")
async def call_stream(socket: WebSocket, scenario_id: str) -> None:
    """Stream a simulated call event by event.

    Paced at roughly real time rather than dumped at once. That is not a gimmick: the point of
    the monitor is to show WHEN each decision was made relative to the caller's speech, and an
    instantaneous dump collapses exactly the axis that matters.
    """
    await socket.accept()
    scenario = CANNED_CALLS.get(scenario_id)
    if scenario is None:
        await socket.send_json({"type": "error", "message": f"no scenario {scenario_id!r}"})
        await socket.close()
        return

    try:
        result = await replay(scenario)
        await socket.send_json({"type": "start", "scenario": scenario.title})
        previous = 0.0
        for event in result["events"]:
            # Compress the wait: a 40-second call would make the studio unusable, but the
            # RELATIVE spacing of events is preserved, which is the information being conveyed.
            delay = max(0.0, (event["at_ms"] - previous)) / 1000 / 6
            previous = event["at_ms"]
            await asyncio.sleep(min(delay, 0.6))
            await socket.send_json({"type": "event", **event})
        await socket.send_json({"type": "done", "summary": result["summary"]})
    except WebSocketDisconnect:
        # The operator closed the tab mid-call. Entirely normal; not an error.
        log.debug("call monitor disconnected during %s", scenario_id)
    finally:
        with suppress(RuntimeError):
            await socket.close()


@app.exception_handler(GuardrailError)
def guardrail_handler(request: Any, exc: GuardrailError) -> Any:
    from fastapi.responses import JSONResponse

    # 422 rather than 500: a guardrail refusal is the system working, not failing.
    return JSONResponse(status_code=422, content={"guardrail": str(exc)})
