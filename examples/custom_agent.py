"""Build a voice agent from scratch: flow, tools, guardrails, and a simulated call.

Run it:  python examples/custom_agent.py

Everything here is the production code path. There is no demo mode — the guardrails that refuse
a tool call below are the same ones that would refuse it on a live line, and the endpointing
decisions are the ones the benchmark measures.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Runnable from a checkout without installing, which is what an example is for.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "gateway" / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dialtone.compliance.redact import redact  # noqa: E402
from dialtone.flow.graph import Edge, Flow, FlowRunner, GuardrailError, Node, NodeKind  # noqa: E402
from dialtone.sim.call import Scenario, replay  # noqa: E402
from dialtone.telephony.provider import ScriptedTurn  # noqa: E402
from dialtone.tools.registry import Latency, ToolCall, ToolRegistry  # noqa: E402
from dialtone.turn.endpointing import completion_score  # noqa: E402


def build_tools() -> ToolRegistry:
    """A pizza shop's two tools, with honest latency classes."""
    registry = ToolRegistry()

    @registry.tool(
        name="check_delivery_area",
        description="Is a postcode inside the delivery zone?",
        parameters={
            "type": "object",
            "properties": {"postcode": {"type": "string"}},
            "required": ["postcode"],
        },
        # An in-memory lookup. Genuinely instantaneous, so no cover phrase — announcing
        # "let me check" before a 2ms lookup makes the agent sound slower than it is.
        latency=Latency.INSTANT,
    )
    def check_delivery_area(postcode: str) -> dict:
        zone = postcode.strip().upper().replace(" ", "")[:3]
        return {"in_area": zone.startswith(("SW1", "SW2", "SE1")), "zone": zone}

    @registry.tool(
        name="place_order",
        description="Place the order. Only after the caller has confirmed it.",
        parameters={
            "type": "object",
            "properties": {"items": {"type": "array"}, "postcode": {"type": "string"}},
            "required": ["items", "postcode"],
        },
        latency=Latency.SLOW,
        # Money moves. If the line drops mid-confirmation the retry must not order twice.
        idempotent=False,
        cover="Great — let me get that order in for you.",
        on_error="I couldn't get that order through. Let me put you to the shop directly.",
    )
    async def place_order(items: list, postcode: str) -> dict:
        await asyncio.sleep(0.04)
        return {"ordered": True, "reference": "PZ-8814", "eta_minutes": 35}

    return registry


def build_flow() -> Flow:
    """The graph. Objectives, never scripts — the model chooses the words."""
    return Flow(
        name="pizza-orders",
        start="greet",
        nodes={
            "greet": Node(
                id="greet",
                kind=NodeKind.SPEAK,
                objective="Greet the caller and ask what they'd like. Keep it to one sentence.",
                edges=(
                    Edge("take_order", when="the caller wants to order"),
                    Edge("goodbye", when="the caller has no further business"),
                ),
            ),
            "take_order": Node(
                id="take_order",
                kind=NodeKind.COLLECT,
                objective="Take the items. Read them back before moving on.",
                collects="items",
                edges=(Edge("check_area", when="at least one item was collected"),),
            ),
            "check_area": Node(
                id="check_area",
                kind=NodeKind.COLLECT,
                objective="Get the postcode and confirm it is in the delivery zone.",
                collects="postcode",
                pattern=r"^[A-Za-z]{1,2}\d",
                # THE GUARDRAIL: the area check exists here and nowhere else.
                tools=("check_delivery_area",),
                edges=(
                    Edge("confirm", when="the postcode is in the delivery area"),
                    Edge("goodbye", when="the postcode is outside the area"),
                ),
            ),
            "confirm": Node(
                id="confirm",
                kind=NodeKind.TOOL,
                objective="Place the order and give the reference and ETA.",
                tools=("place_order",),
                edges=(Edge("goodbye", when="the order was placed"),),
            ),
            "goodbye": Node(
                id="goodbye", kind=NodeKind.END, objective="Thank them and end the call warmly."
            ),
        },
    )


async def main() -> None:
    rule = "─" * 76

    # ── 1. the flow validates before it can be loaded ────────────────────────
    print(rule)
    print("1. STRUCTURAL VALIDATION — problems found before a call, not during one")
    print(rule)
    flow = build_flow()
    print(f"   {flow.name}: {flow.validate() or 'valid'}")
    for path in flow.paths():
        print("   " + " → ".join(path))

    # A flow with a dead end is refused outright rather than loading "mostly fine" and
    # failing on the one path nobody tested.
    broken = build_flow()
    broken.nodes["orphan"] = Node(id="orphan", kind=NodeKind.SPEAK, objective="unreachable")
    try:
        FlowRunner(broken)
    except GuardrailError as exc:
        print(f"\n   a broken flow is refused:\n   {str(exc).splitlines()[-1].strip()}")

    # ── 2. tools are scoped to nodes ─────────────────────────────────────────
    print(f"\n{rule}")
    print("2. TOOL SCOPING — the model cannot call what it cannot see")
    print(rule)
    tools = build_tools()
    runner = FlowRunner(flow)
    state = runner.start()

    print(f"   at '{state.node_id}', reachable tools: {runner.available_tools(state) or 'none'}")
    refused = await tools.invoke(
        ToolCall("place_order", {"items": ["margherita"], "postcode": "SW1A 1AA"}),
        allowed=runner.available_tools(state),
    )
    print(f"   ordering from the greeting → {refused.error}")

    runner.transition(state, "take_order")
    runner.collect(state, "one margherita")
    runner.transition(state, "check_area")
    print(f"\n   at '{state.node_id}', reachable tools: {runner.available_tools(state)}")
    allowed = await tools.invoke(
        ToolCall("check_delivery_area", {"postcode": "SW1A 1AA"}),
        allowed=runner.available_tools(state),
    )
    print(f"   checking the area here    → {allowed.value}")

    # ── 3. a dropped line must not order twice ───────────────────────────────
    print(f"\n{rule}")
    print("3. IDEMPOTENCY — the line drops mid-confirmation and the caller redials")
    print(rule)
    call = ToolCall(
        "place_order",
        {"items": ["margherita"], "postcode": "SW1A 1AA"},
        idempotency_key="call-4417:order:1",
    )
    first = await tools.invoke(call)
    second = await tools.invoke(call)
    print(f"   first attempt  → {first.value['reference']}  (deduplicated: {first.deduplicated})")
    print(f"   after redial   → {second.value['reference']}  (deduplicated: {second.deduplicated})")
    print("   one order, not two.")

    # ── 4. endpointing on real caller phrasing ───────────────────────────────
    print(f"\n{rule}")
    print("4. ENDPOINTING — how long to wait, decided from the transcript")
    print(rule)
    for phrase in (
        "yes",
        "I'd like a margherita",
        "my postcode is SW1A",
        "the number is oh seven nine",
        "can I also get",
    ):
        score, reason = completion_score(phrase)
        verdict = "respond now" if score >= 0.5 else "keep waiting"
        print(f"   {score:.2f}  {verdict:<13} {phrase!r}\n         {reason}")

    # ── 5. a card read aloud never reaches the model ─────────────────────────
    print(f"\n{rule}")
    print("5. REDACTION — spoken digits, removed before storage and before the model")
    print(rule)
    spoken = (
        "the card is four two four two four two four two four two "
        "four two four two four two and it expires in June"
    )
    result = redact(spoken)
    print(f"   caller said : {spoken[:62]}…")
    print(f"   model gets  : {result.text}")
    print(f"   findings    : {[(f.rule, f.sensitivity.value) for f in result.findings]}")

    # ── 6. the whole thing, as a call ────────────────────────────────────────
    print(f"\n{rule}")
    print("6. A SIMULATED CALL — the real pipeline, deterministic end to end")
    print(rule)
    scenario = Scenario(
        id="pizza",
        title="Ordering a pizza",
        description="A caller who pauses mid-postcode.",
        turns=(
            ScriptedTurn("hi can I order a pizza", trailing_silence_ms=1_100),
            ScriptedTurn("one margherita please", trailing_silence_ms=1_100),
            # The pause lands right after "SW1A" — a fixed 700ms threshold answers over them.
            ScriptedTurn(
                "my postcode is SW1A one AA",
                pauses=((20, 760.0),),
                trailing_silence_ms=1_600,
            ),
        ),
        replies=(
            "Northgate Pizza, what can I get you?",
            "One margherita. What's the postcode?",
            "You're in our area — that'll be about 35 minutes.",
        ),
    )
    outcome = await replay(scenario)
    summary = outcome["summary"]
    print(f"   turns answered      {summary['turns']}")
    print(f"   median endpoint     {summary['median_endpoint_ms']:.0f}ms "
          f"(a fixed threshold would take {summary['baseline_median_ms']:.0f}ms)")
    print(f"   talked over caller  {summary['false_cutoffs']}")
    print("\n   transcript:")
    for message in outcome["transcript"]:
        print(f"     {message['role']:<10} {message['content']}")


if __name__ == "__main__":
    asyncio.run(main())
