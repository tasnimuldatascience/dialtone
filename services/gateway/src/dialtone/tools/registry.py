"""Function calling on a live phone call.

WHY THIS IS NOT THE SAME PROBLEM AS FUNCTION CALLING IN A CHAT APP. In a chat app a slow tool
is a spinner. On a phone call it is silence, and silence is indistinguishable from a dropped
line. Past about 1.5 seconds the caller says "hello? are you there?" — and now the agent has to
handle an interruption on top of the tool call it is still waiting for.

So every tool here declares a LATENCY CLASS, and the class determines how the call is covered:

    INSTANT    (<150ms)   a lookup in memory; just do it
    FAST       (<800ms)   fits inside a natural pause; do it silently
    SLOW       (<4s)      MUST be covered with speech before it starts
    BACKGROUND (>4s)      cannot be awaited at all; promise a callback instead

The cover phrase is not decoration. "Let me pull that up" is what makes a 2-second database
query feel like a person checking rather than a machine hanging, and it has to START BEFORE the
tool does, not after — which means the orchestrator needs to know the class in advance. That is
why it is declared on the tool rather than measured after the fact.

THE SECOND THING CHATS DO NOT HAVE: side effects on a line that can drop. If the caller hangs
up between "charge the card" and the confirmation, the charge either happened or it did not,
and the agent has no way to ask. So every tool declares whether it is IDEMPOTENT, and
non-idempotent tools run behind a keyed guard that makes a retry safe.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

log = logging.getLogger("dialtone.tools")


class Latency(StrEnum):
    INSTANT = "instant"        # <150ms  — in-memory
    FAST = "fast"              # <800ms  — fits in a pause
    SLOW = "slow"              # <4s     — must be covered with speech
    BACKGROUND = "background"  # >4s     — cannot be awaited; promise a callback


#: How long each class gets before it is abandoned. A tool that overruns its own declared class
#: is a bug in the declaration, and it is better to fail loudly on a test call than to leave a
#: caller listening to nothing.
DEADLINE_MS: dict[Latency, float] = {
    Latency.INSTANT: 300.0,
    Latency.FAST: 1_200.0,
    Latency.SLOW: 5_000.0,
    Latency.BACKGROUND: 500.0,   # only the handoff is awaited, never the work
}

#: Spoken while a SLOW tool runs. Several, because hearing the identical phrase on every lookup
#: is one of the fastest ways to make an agent sound like a machine.
COVER_PHRASES: tuple[str, ...] = (
    "Let me pull that up for you.",
    "One moment while I check that.",
    "Give me just a second to look.",
    "Okay, let me find that.",
)


class ToolError(RuntimeError):
    """A tool failed in a way the agent must tell the caller about."""


@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    #: JSON Schema for the arguments. Handed to the model verbatim.
    parameters: dict[str, Any]
    latency: Latency
    #: Safe to run twice with the same arguments? Anything that moves money, sends a message, or
    #: books a slot is NOT, and gets the dedupe guard.
    idempotent: bool = True
    #: Spoken before a SLOW tool starts. Falls back to a rotating generic phrase.
    cover: str | None = None
    #: What the caller hears when the tool fails. Never the exception text — a caller must not
    #: hear a stack trace, and "I could not reach the booking system" is more useful anyway.
    on_error: str = "Sorry — I wasn't able to do that just now."

    def as_schema(self) -> dict[str, Any]:
        """Tool schema in the shape both major model APIs accept."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass(slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: Stable key for a non-idempotent call. Two calls sharing a key are the SAME call — this is
    #: what makes a retry after a dropped line safe.
    idempotency_key: str = ""


@dataclass(slots=True)
class ToolResult:
    name: str
    ok: bool
    value: Any = None
    error: str = ""
    duration_ms: float = 0.0
    #: True when served from the dedupe guard rather than executed. Surfaced so a transcript
    #: shows what actually happened instead of implying the work ran twice.
    deduplicated: bool = False
    #: True when the tool overran the deadline for its declared latency class.
    timed_out: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "deduplicated": self.deduplicated,
            "timed_out": self.timed_out,
        }


Handler = Callable[..., Any | Awaitable[Any]]


class ToolRegistry:
    """The tools an agent can call, and the rules for calling them."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Handler] = {}
        #: Completed non-idempotent calls, keyed by idempotency key. In production this is Redis
        #: with a TTL; the interface is identical and the semantics are the part that matters.
        self._completed: dict[str, ToolResult] = {}

    def register(self, spec: ToolSpec, handler: Handler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        if not spec.idempotent and spec.latency is Latency.INSTANT:
            # Almost always a mistake: something with a side effect that claims to be
            # instantaneous has not accounted for the network call it must be making.
            log.warning("tool %r has side effects but claims INSTANT latency", spec.name)
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def tool(self, **kwargs: Any) -> Callable[[Handler], Handler]:
        """Decorator form: @registry.tool(name=..., latency=...)."""

        def wrap(fn: Handler) -> Handler:
            self.register(ToolSpec(**kwargs), fn)
            return fn

        return wrap

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def schemas(self, allowed: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Schemas for the tools reachable right now.

        THE GUARDRAIL SURFACE. `allowed` comes from the flow node. A tool absent from this list
        cannot be called by a model that has decided it should be, which is strictly stronger
        than instructing it not to.
        """
        names = list(self._specs) if allowed is None else [n for n in allowed if n in self._specs]
        return [self._specs[n].as_schema() for n in names]

    def cover_for(self, name: str, turn: int = 0) -> str | None:
        """What to say before this tool runs, if anything.

        None for anything fast enough to hide inside a natural pause: speaking a cover phrase
        for a 40ms lookup makes the agent sound slower than it actually is.
        """
        spec = self._specs.get(name)
        if spec is None or spec.latency in (Latency.INSTANT, Latency.FAST):
            return None
        if spec.cover:
            return spec.cover
        return COVER_PHRASES[turn % len(COVER_PHRASES)]

    async def invoke(self, call: ToolCall, allowed: tuple[str, ...] | None = None) -> ToolResult:
        """Run a tool with every rule enforced.

        Refusal is a RESULT, not an exception: the model asked for something it may not have,
        and the right handling is to tell it so and let it choose again. Raising would abort a
        turn that is otherwise perfectly recoverable, on a live call.
        """
        spec = self._specs.get(call.name)
        if spec is None:
            return ToolResult(call.name, ok=False, error=f"no tool named {call.name!r}")
        if allowed is not None and call.name not in allowed:
            return ToolResult(
                call.name,
                ok=False,
                error=(
                    f"{call.name!r} is not available at this step "
                    f"(available: {', '.join(allowed) or 'none'})"
                ),
            )

        missing = _missing_required(spec.parameters, call.arguments)
        if missing:
            # Returned rather than raised for the same reason: the model can supply them.
            return ToolResult(
                call.name, ok=False,
                error=f"missing required argument(s): {', '.join(missing)}",
            )

        if not spec.idempotent and call.idempotency_key:
            cached = self._completed.get(call.idempotency_key)
            if cached is not None:
                # The line dropped and we are retrying. The work already happened — running it
                # again would charge the card twice.
                log.info("tool %s deduplicated on key %s", call.name, call.idempotency_key)
                return ToolResult(
                    cached.name, ok=cached.ok, value=cached.value, error=cached.error,
                    duration_ms=cached.duration_ms, deduplicated=True,
                )

        started = time.perf_counter()
        deadline = DEADLINE_MS[spec.latency] / 1000
        try:
            handler = self._handlers[call.name]
            raw = handler(**call.arguments)
            value = await asyncio.wait_for(raw, deadline) if inspect.isawaitable(raw) else raw
            result = ToolResult(
                call.name, ok=True, value=value,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except TimeoutError:
            # The caller has been listening to silence for the whole deadline. Abandoning is the
            # kind option — the agent can say so and offer an alternative.
            log.warning("tool %s exceeded its %s deadline", call.name, spec.latency)
            result = ToolResult(
                call.name, ok=False, error=spec.on_error, timed_out=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 — a tool must never take down the call
            # The caller hears `spec.on_error`; the operator gets the traceback in the log.
            # Leaking exception text into speech is both confusing and a disclosure risk.
            log.exception("tool %s failed: %r", call.name, exc)
            result = ToolResult(
                call.name, ok=False, error=spec.on_error,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        if not spec.idempotent and call.idempotency_key and result.ok:
            self._completed[call.idempotency_key] = result

        if result.duration_ms > DEADLINE_MS[spec.latency] and not result.timed_out:
            log.warning(
                "tool %s took %.0fms, over the %s budget — its latency class is wrong",
                call.name, result.duration_ms, spec.latency,
            )
        return result


def _missing_required(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    required = schema.get("required", []) if isinstance(schema, dict) else []
    return [r for r in required if r not in arguments]


@dataclass(slots=True)
class ToolTrace:
    """Every tool call on one phone call, for the transcript and the studio."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, call: ToolCall, result: ToolResult, *, covered: str | None = None) -> None:
        self.entries.append({
            "arguments": call.arguments,
            "covered_with": covered,
            **result.as_dict(),
        })

    @property
    def total_ms(self) -> float:
        return round(sum(e["duration_ms"] for e in self.entries), 2)

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if not e["ok"]]
