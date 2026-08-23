"""The command line. Everything the studio shows, reproducible from a terminal.

DESIGN RULE: NO COMMAND HERE READS A CACHED RESULT. Every number is recomputed from the same
code that runs a call. That is slower and it is the point — a benchmark you can only reproduce
by trusting a checked-in JSON file is a claim, not a measurement.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

# Windows terminals default to cp1252 and every table below uses box-drawing characters. Without
# this the tool crashes on its own output, which is a poor first impression for a CLI.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    add_completion=False,
    help="dialtone — a voice-agent platform whose turn-taking is measured, not asserted.",
)
bench = typer.Typer(help="Endpointing benchmarks.")
call = typer.Typer(help="Simulated calls.")
flow = typer.Typer(help="Conversation flows.")
app.add_typer(bench, name="bench")
app.add_typer(call, name="call")
app.add_typer(flow, name="flow")

_RULE = "─"


def _table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> None:
    widths = [
        max(len(str(headers[i])), max((len(str(r[i])) for r in rows), default=0))
        for i in range(len(headers))
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    typer.echo(line)
    typer.echo(_RULE * len(line))
    for row in rows:
        typer.echo("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)))


# ── benchmarks ───────────────────────────────────────────────────────────────
@bench.command("ablate")
def bench_ablate(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Which signal is doing the work?

    An adaptive endpointer that beats a fixed one only because its base threshold happens to be
    tuned is not adaptive, it is tuned. Turning each signal off in turn is the only way to tell.
    """
    from .eval.endpointing import ablate

    results = ablate()
    if json_out:
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
        return

    _table(
        [
            (
                r.label,
                f"{r.median_latency_ms:.0f}ms",
                f"{r.p90_latency_ms:.0f}ms",
                f"{r.false_cutoff_rate:.1%}",
                f"{r.completion_recall:.0%}",
            )
            for r in results
        ],
        ("configuration", "median", "p90", "false cutoff", "recall"),
    )
    typer.echo(
        "\nfalse cutoff = share of unfinished turns the agent talked over.\n"
        "It is the number no vendor publishes, and it is the cost of every latency figure."
    )


@bench.command("sweep")
def bench_sweep(
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The full latency/false-cutoff curve, fixed thresholds against adaptive.

    A curve against a curve, not a point against a point: comparing one adaptive configuration
    to one fixed threshold proves nothing, since the fixed one could just have been tuned
    differently.
    """
    from .eval.endpointing import sweep

    results = sweep()
    if json_out:
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
        return

    _table(
        [
            (
                r.label,
                "never" if r.median_latency_ms == float("inf") else f"{r.median_latency_ms:.0f}ms",
                f"{r.false_cutoff_rate:.1%}",
                f"{r.completion_recall:.0%}",
            )
            for r in results
        ],
        ("configuration", "median", "false cutoff", "recall"),
    )


@bench.command("score")
def bench_score(
    text: Annotated[str, typer.Argument(help="A partial transcript.")],
) -> None:
    """Why the endpointer would or would not respond to this.

    Inspectable by design. When an agent talks over someone, the reason is a rule you can read
    and fix, not a weight.
    """
    from .turn.endpointing import Endpointer, TurnState, completion_score

    score, reason = completion_score(text)
    endpointer = Endpointer()
    verdict = endpointer.evaluate(TurnState(transcript=text, silence_ms=0.0, speech_ms=800.0))

    typer.echo(f"transcript   {text!r}")
    typer.echo(f"completion   {score:.2f}  ({reason})")
    typer.echo(f"threshold    {verdict.threshold_ms:.0f}ms of silence before responding")
    verdict_word = "complete" if score >= 0.5 else "unfinished"
    typer.echo(f"reading      the caller sounds {verdict_word}")


@bench.command("corpus")
def bench_corpus(
    show: Annotated[str, typer.Option(help="all | complete | incomplete")] = "all",
) -> None:
    """The labelled corpus, with the score the endpointer assigns each item.

    Published on purpose: a benchmark whose test set is private is a marketing number.
    """
    from .eval.endpointing import CORPUS
    from .turn.endpointing import completion_score

    items = [
        s for s in CORPUS
        if show == "all" or (show == "complete") == s.complete
    ]
    rows = []
    for sample in items:
        score, reason = completion_score(sample.transcript)
        # A complete turn should score high and an incomplete one low. Anything else is a
        # disagreement between the corpus and the scorer, and worth seeing at a glance.
        agrees = (score >= 0.5) == sample.complete
        rows.append((
            sample.id,
            "complete" if sample.complete else "unfinished",
            f"{score:.2f}",
            "ok" if agrees else "MISMATCH",
            sample.transcript[:44],
        ))
    _table(rows, ("id", "label", "score", "", "transcript"))
    mismatches = sum(1 for r in rows if r[3] == "MISMATCH")
    typer.echo(f"\n{len(rows)} items, {mismatches} where the scorer disagrees with the label.")


# ── simulated calls ──────────────────────────────────────────────────────────
@call.command("list")
def call_list() -> None:
    """The scenarios available to replay."""
    from .sim.call import CANNED_CALLS

    _table(
        [(k, v.title, str(len(v.turns)), v.description[:56]) for k, v in CANNED_CALLS.items()],
        ("id", "title", "turns", "what it tests"),
    )


@call.command("run")
def call_run(
    scenario: Annotated[str, typer.Argument(help="Scenario id, or 'all'.")] = "all",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Every event.")] = False,
) -> None:
    """Replay a scripted call through the real pipeline."""
    from .sim.call import CANNED_CALLS, replay

    ids = list(CANNED_CALLS) if scenario == "all" else [scenario]
    for scenario_id in ids:
        if scenario_id not in CANNED_CALLS:
            typer.echo(f"no scenario {scenario_id!r}", err=True)
            raise typer.Exit(1)

        result = asyncio.run(replay(CANNED_CALLS[scenario_id]))
        summary = result["summary"]
        typer.echo(f"\n{'═' * 78}")
        typer.echo(f"{result['scenario']['title']}  ({scenario_id})")
        typer.echo(f"{result['scenario']['description']}")
        typer.echo("═" * 78)

        if verbose:
            for event in result["events"]:
                detail = {k: v for k, v in event.items() if k not in ("at_ms", "kind")}
                typer.echo(f"  {event['at_ms']:>8.0f}ms  {event['kind']:<13} "
                           f"{json.dumps(detail)[:96]}")
            typer.echo("")

        typer.echo(
            f"  turns {summary['turns']}   "
            f"median endpoint {summary['median_endpoint_ms']:.0f}ms "
            f"(vs {summary['baseline_median_ms']:.0f}ms fixed)   "
            f"false cutoffs {summary['false_cutoffs']}   "
            f"interruptions {summary['interruptions']}   "
            f"backchannels {summary['backchannels']}"
        )
        if result["redactions"]:
            rules = sorted({r for red in result["redactions"] for r in red["rules"]})
            typer.echo(f"  redacted before storage or the model: {', '.join(rules)}")

        typer.echo("\n  transcript (what the model sees):")
        for message in result["transcript"]:
            typer.echo(f"    {message['role']:<10} {message['content']}")


# ── flows ────────────────────────────────────────────────────────────────────
@flow.command("show")
def flow_show() -> None:
    """The worked example flow, its guardrails, and every path through it."""
    from .agents.support import build_flow, build_registry

    graph = build_flow()
    registry = build_registry()

    typer.echo(f"flow: {graph.name}   start: {graph.start}")
    typer.echo(f"global tools: {', '.join(graph.global_tools) or 'none'}\n")

    _table(
        [
            (
                node.id,
                node.kind.value,
                node.collects or "",
                ", ".join(node.tools) or "—",
                str(len(node.edges)),
            )
            for node in graph.nodes.values()
        ],
        ("node", "kind", "collects", "tools reachable here", "edges"),
    )

    typer.echo(f"\npaths ({len(graph.paths())}):")
    for path in graph.paths():
        typer.echo("  " + " → ".join(path))

    typer.echo("\ntools:")
    _table(
        [
            (
                name,
                registry.spec(name).latency.value,
                "yes" if registry.spec(name).idempotent else "NO — deduped on retry",
                registry.cover_for(name) or "—",
            )
            for name in registry.names
        ],
        ("tool", "latency", "idempotent", "spoken while it runs"),
    )


@flow.command("validate")
def flow_validate(
    path: Annotated[Path | None, typer.Argument(help="A flow JSON file.")] = None,
) -> None:
    """Structural problems, found before a call rather than during one.

    Every problem reported here is a defect that would otherwise surface as a live call
    dead-ending on a customer — the most expensive possible place to find it.
    """
    from .agents.support import build_flow

    if path is None:
        graph = build_flow()
    else:
        from .flow.graph import Edge, Flow, Node, NodeKind

        raw = json.loads(path.read_text(encoding="utf-8"))
        graph = Flow(
            name=raw["name"],
            start=raw["start"],
            nodes={
                n["id"]: Node(
                    id=n["id"],
                    kind=NodeKind(n["kind"]),
                    objective=n.get("objective", ""),
                    collects=n.get("collects"),
                    pattern=n.get("pattern"),
                    tools=tuple(n.get("tools", ())),
                    edges=tuple(
                        Edge(e["to"], e.get("when", ""), e.get("condition"))
                        for e in n.get("edges", ())
                    ),
                )
                for n in raw["nodes"]
            },
            global_tools=tuple(raw.get("global_tools", ())),
        )

    problems = graph.validate()
    if not problems:
        typer.echo(f"{graph.name}: valid — {len(graph.paths())} paths, all terminating.")
        return
    typer.echo(f"{graph.name}: {len(problems)} problem(s)")
    for problem in problems:
        typer.echo(f"  - {problem}")
    raise typer.Exit(1)


# ── compliance ───────────────────────────────────────────────────────────────
@app.command("redact")
def redact_cmd(
    text: Annotated[str, typer.Argument(help="A transcript, as the recogniser produced it.")],
) -> None:
    """What reaches the model and the store, and what never does.

    Try it with a card number read aloud — "four two four two ..." — which is how a recogniser
    actually transcribes one, and which every digit-matching redactor misses entirely.
    """
    from .compliance.redact import redact

    result = redact(text)
    typer.echo(f"in   {text}")
    typer.echo(f"out  {result.text}")
    if not result.findings:
        typer.echo("\nnothing sensitive found.")
        return
    typer.echo("")
    _table(
        [
            (
                f.rule,
                "REMOVED" if f.sensitivity.value == "strip" else "kept, tagged",
                f.preview or "—",
            )
            for f in result.findings
        ],
        ("rule", "handling", "preview"),
    )


@app.command("serve")
def serve(
    host: str = "127.0.0.1",
    port: int = 8071,
    reload: bool = False,
) -> None:
    """Run the API the studio talks to."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        typer.echo("install the serve extra:  pip install -e '.[serve]'", err=True)
        raise typer.Exit(1) from None

    uvicorn.run("dialtone.server.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":  # pragma: no cover
    app()
