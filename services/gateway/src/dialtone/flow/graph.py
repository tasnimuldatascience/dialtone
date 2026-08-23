"""Conversation flows: a declarative state machine with guardrails.

WHY A GRAPH AND NOT JUST A PROMPT. A single system prompt is the fastest way to build a voice
agent and the fastest way to make one that cannot be operated. Three things a prompt cannot
give you, all of which a business needs before it will put an agent on its main line:

  DETERMINISM WHERE IT MATTERS. "Never quote a price outside the published table" is not a
  request you make of a language model, it is a constraint you enforce. In a graph, a node
  either has the pricing tool or it does not.
  TESTABILITY. A graph has paths. Paths can be enumerated, walked, and asserted. A prompt has
  vibes, and its regression suite is a person reading transcripts.
  OBSERVABILITY. When a call goes wrong, "it was in `collect_payment` and took the
  `card_declined` edge" is an answer. "The model decided to" is not.

WHAT STAYS SOFT. The graph controls STRUCTURE — which tools are reachable, which transitions
are legal, what must be collected before proceeding. It does not script the words. Scripted
wording is what made IVR trees hated, and the whole reason to use a language model is that it
can say the same thing fifty different ways.

So: the model chooses the words, the graph chooses what is possible. Every transition is either
explicitly triggered by a tool result, or proposed by the model and VALIDATED against the
declared edges — a model that hallucinates a transition to a node that does not exist gets a
refusal, not a state change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeKind(StrEnum):
    SPEAK = "speak"          # say something, then move on
    COLLECT = "collect"      # gather a value from the caller before proceeding
    BRANCH = "branch"        # choose an edge from what is known
    TOOL = "tool"            # call a function
    TRANSFER = "transfer"    # hand to a human
    END = "end"


class GuardrailError(RuntimeError):
    """A transition or tool call the flow does not permit."""


@dataclass(slots=True, frozen=True)
class Edge:
    to: str
    #: Natural-language description of when to take this edge. Given to the model as the menu
    #: of legal moves; it chooses among them but cannot invent one.
    when: str
    #: Optional deterministic condition over collected values. When present it is checked
    #: FIRST, and a satisfied condition takes the edge without asking the model at all --
    #: which is both faster and not subject to a model changing its mind.
    condition: str | None = None


@dataclass(slots=True)
class Node:
    id: str
    kind: NodeKind
    #: What the agent is trying to achieve here. Becomes part of the prompt; deliberately an
    #: objective rather than a script.
    objective: str = ""
    #: For COLLECT nodes: the value that must be obtained before any edge may be taken.
    collects: str | None = None
    #: Validation for the collected value. A caller who mishears must be re-asked, not advanced.
    pattern: str | None = None
    #: Tools reachable from this node. THE GUARDRAIL: a tool not listed here cannot be called,
    #: whatever the model attempts.
    tools: tuple[str, ...] = ()
    edges: tuple[Edge, ...] = ()
    #: Hard limit on re-asks before escalating. Without it a COLLECT node can trap a caller in
    #: a loop forever, which is the single worst experience a voice agent can produce.
    max_attempts: int = 3

    def edge_to(self, target: str) -> Edge | None:
        return next((e for e in self.edges if e.to == target), None)


@dataclass(slots=True)
class Flow:
    name: str
    start: str
    nodes: dict[str, Node]
    #: Global tools, reachable from every node. Kept small: the point of per-node tools is that
    #: most tools are NOT reachable most of the time.
    global_tools: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        """Structural problems, found before a call rather than during one.

        Every one of these is a defect that would otherwise surface as a live call dead-ending
        on a customer, which is the most expensive possible place to discover it.
        """
        problems: list[str] = []
        if self.start not in self.nodes:
            problems.append(f"start node {self.start!r} does not exist")

        for node in self.nodes.values():
            for edge in node.edges:
                if edge.to not in self.nodes:
                    problems.append(f"{node.id} -> {edge.to!r}: no such node")
            if node.kind is NodeKind.COLLECT and not node.collects:
                problems.append(f"{node.id} is a COLLECT node but declares no value to collect")
            # TRANSFER is terminal too: the call has left the agent. Treating only END as
            # terminal made every realistic flow fail validation, since a handoff node
            # legitimately has nowhere to go.
            terminal = node.kind in (NodeKind.END, NodeKind.TRANSFER)
            if not terminal and not node.edges:
                problems.append(f"{node.id} is not terminal but has no outgoing edges")
            if terminal and node.edges:
                problems.append(f"{node.id} is terminal ({node.kind}) but has outgoing edges")

        # Unreachable nodes are dead configuration -- usually a rename that missed an edge.
        reachable = self.reachable()
        for node_id in self.nodes:
            if node_id not in reachable:
                problems.append(f"{node_id} is unreachable from {self.start!r}")

        # A flow with no path to an END node can never hang up.
        if not any(self.nodes[n].kind is NodeKind.END for n in reachable):
            problems.append("no END node is reachable — this flow cannot terminate")
        return problems

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [self.start]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            stack.extend(e.to for e in self.nodes[current].edges)
        return seen

    def paths(self, limit: int = 200) -> list[list[str]]:
        """Every path from start to an END node, up to `limit`.

        This is what makes a flow testable. Each path is a conversation shape that can be
        walked and asserted, which is the thing a prompt-only agent cannot offer.
        """
        out: list[list[str]] = []

        def walk(node_id: str, path: list[str], seen: frozenset[str]) -> None:
            if len(out) >= limit or node_id not in self.nodes:
                return
            node = self.nodes[node_id]
            path = [*path, node_id]
            if node.kind in (NodeKind.END, NodeKind.TRANSFER) or not node.edges:
                out.append(path)
                return
            for edge in node.edges:
                # Cycles are legal at runtime (a re-ask loops back) but a path enumeration
                # must not follow them or it never terminates.
                if edge.to in seen:
                    continue
                walk(edge.to, path, seen | {edge.to})

        walk(self.start, [], frozenset({self.start}))
        return out


@dataclass(slots=True)
class FlowState:
    """Where a call is, and what it has learned."""

    node_id: str
    collected: dict[str, Any] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    visited: list[str] = field(default_factory=list)
    transferred: bool = False
    ended: bool = False


class FlowRunner:
    """Drives a flow, enforcing every guardrail."""

    def __init__(self, flow: Flow):
        problems = flow.validate()
        if problems:
            # Refusing to load an invalid flow is the point. A structurally broken flow that
            # loads "mostly fine" fails on a live call, on the one path nobody tested.
            raise GuardrailError(
                "flow is not valid:\n" + "\n".join(f"  - {p}" for p in problems)
            )
        self.flow = flow

    def start(self) -> FlowState:
        return FlowState(node_id=self.flow.start, visited=[self.flow.start])

    def node(self, state: FlowState) -> Node:
        return self.flow.nodes[state.node_id]

    def available_tools(self, state: FlowState) -> tuple[str, ...]:
        """Tools callable right now. THE GUARDRAIL SURFACE.

        The model never sees a tool it may not call, which is stronger than telling it not to:
        a tool absent from the schema cannot be invoked even by a model that has decided it
        should be.
        """
        return tuple({*self.node(state).tools, *self.flow.global_tools})

    def legal_transitions(self, state: FlowState) -> list[Edge]:
        return list(self.node(state).edges)

    def collect(self, state: FlowState, value: str) -> tuple[bool, str]:
        """Record a value at a COLLECT node. Returns (accepted, reason).

        Rejection is not failure — it means re-ask. But `max_attempts` bounds that, because a
        caller trapped re-answering the same question is worse than a transfer.
        """
        node = self.node(state)
        if node.kind is not NodeKind.COLLECT or not node.collects:
            return False, f"{node.id} does not collect a value"

        attempts = state.attempts.get(node.collects, 0) + 1
        state.attempts[node.collects] = attempts

        if node.pattern and not re.search(node.pattern, value, re.IGNORECASE):
            if attempts >= node.max_attempts:
                return False, (
                    f"{attempts} failed attempts at {node.collects!r} — escalate rather than "
                    f"asking a fourth time"
                )
            return False, f"{value!r} does not match the expected form; re-ask"

        state.collected[node.collects] = value
        return True, f"collected {node.collects}"

    def transition(self, state: FlowState, target: str, *, forced: bool = False) -> FlowState:
        """Move to another node, if the flow allows it.

        `forced` is for deterministic moves (a tool result, an escalation) that bypass the
        model's choice. It still validates the target exists — a forced transition to a
        typo'd node is a crash, not a silent no-op.
        """
        node = self.node(state)
        if target not in self.flow.nodes:
            raise GuardrailError(f"no node {target!r} in flow {self.flow.name!r}")

        if not forced and node.edge_to(target) is None:
            # The model proposed a transition the graph does not permit. Refused rather than
            # allowed, because the entire value of the graph is that this cannot happen.
            legal = ", ".join(e.to for e in node.edges) or "none"
            raise GuardrailError(
                f"{node.id} -> {target} is not a declared edge (legal: {legal})"
            )

        if node.kind is NodeKind.COLLECT and node.collects and not forced:
            if node.collects not in state.collected:
                raise GuardrailError(
                    f"cannot leave {node.id} before collecting {node.collects!r}"
                )

        state.node_id = target
        state.visited.append(target)
        moved_to = self.flow.nodes[target]
        if moved_to.kind is NodeKind.END:
            state.ended = True
        if moved_to.kind is NodeKind.TRANSFER:
            state.transferred = True
        return state

    def prompt(self, state: FlowState) -> str:
        """The instruction for this node. Objective plus legal moves, never a script."""
        node = self.node(state)
        lines = [f"Current step: {node.id}", f"Objective: {node.objective}"]
        if node.collects:
            lines.append(f"You must obtain: {node.collects}")
            if node.collects in state.collected:
                lines.append(f"Already obtained: {state.collected[node.collects]}")
        if state.collected:
            known = ", ".join(f"{k}={v}" for k, v in state.collected.items())
            lines.append(f"Known so far: {known}")
        if node.edges:
            lines.append("You may move to exactly one of:")
            lines.extend(f"  - {e.to}: {e.when}" for e in node.edges)
        lines.append(
            "Speak naturally and in your own words. Do not read this instruction aloud."
        )
        return "\n".join(lines)
