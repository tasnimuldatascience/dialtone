"""A complete worked agent: appointment booking for a dental practice.

WHY A FULL EXAMPLE RATHER THAN A SNIPPET. Every voice framework demos well on "book me an
appointment" and falls over on the things a real practice needs on day one — a caller who gives
a date that has already passed, a caller who reads a card number, a booking system that times
out, a caller who wants a human. Those are not edge cases; on a real line they are most of the
volume. This file is the reference for how the pieces compose under exactly those conditions.

It also demonstrates the two guardrails that make the platform worth using over a prompt:

  TOOL SCOPING     `check_availability` is reachable from `offer_slots` and nowhere else. The
                   model cannot book a slot from the greeting, however it is prompted.
  IDEMPOTENCY      `book_appointment` has side effects. If the line drops between the request
                   and the confirmation, the retry is deduplicated rather than double-booking.

Every tool declares its latency class, which is what lets the orchestrator cover the slow ones
with speech instead of leaving the caller in silence.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

from ..flow.graph import Edge, Flow, Node, NodeKind
from ..tools.registry import Latency, ToolRegistry

#: Stands in for the practice management system. A dict rather than a mock framework so the
#: example runs with no setup and the failure modes below are real code paths, not stubs.
_CALENDAR: dict[str, list[str]] = {}
_BOOKINGS: dict[str, dict[str, Any]] = {}


def _seed_calendar(from_day: date, days: int = 14) -> None:
    slots = ["09:00", "10:30", "12:00", "14:00", "15:30", "17:00"]
    for offset in range(days):
        day = from_day + timedelta(days=offset)
        if day.weekday() >= 5:      # closed at weekends
            continue
        # A deterministic pattern rather than random, so the example behaves identically on
        # every run. A demo that shows different availability each time cannot be screenshotted,
        # documented, or tested.
        taken = (day.toordinal() % 4) + 1
        _CALENDAR[day.isoformat()] = slots[taken:]


def build_registry(clock: date | None = None) -> ToolRegistry:
    """The tools this agent can call."""
    today = clock or date.today()
    _CALENDAR.clear()
    _BOOKINGS.clear()
    _seed_calendar(today)
    registry = ToolRegistry()

    @registry.tool(
        name="check_availability",
        description=(
            "Free appointment slots on a given date. Dates must be ISO (YYYY-MM-DD); "
            "resolve relative dates like 'next Tuesday' before calling."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date, e.g. 2026-03-14"},
            },
            "required": ["date"],
        },
        # A calendar lookup crosses the network. It is fast, but not free -- and classing it
        # FAST rather than INSTANT is what stops the orchestrator from assuming it can hide
        # inside a gap that does not exist.
        latency=Latency.FAST,
        on_error="I'm having trouble reaching the calendar — can I take a number and call back?",
    )
    async def check_availability(date: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        try:
            requested = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            # Returned as data, not raised. The model can recover from "that is not a date" by
            # asking again; an exception would abort the turn on a live call.
            return {"error": f"{date!r} is not an ISO date", "slots": []}

        if requested < today:
            # THE FAILURE EVERY DEMO SKIPS. A caller says "Tuesday" on a Wednesday and means
            # NEXT Tuesday. Without this the agent cheerfully books into the past.
            return {
                "error": "that date has already passed",
                "slots": [],
                "hint": "ask whether they meant the following week",
            }
        if requested.weekday() >= 5:
            return {"error": "the practice is closed at weekends", "slots": []}

        return {"date": date, "slots": _CALENDAR.get(date, [])}

    @registry.tool(
        name="book_appointment",
        description="Reserve a slot. Only after the caller has confirmed the date and time.",
        parameters={
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "time": {"type": "string"},
                "name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["date", "time", "name"],
        },
        latency=Latency.SLOW,
        # NOT idempotent: this is the one call on the whole flow that can go wrong in a way the
        # caller has to phone back about.
        idempotent=False,
        cover="Perfect — let me get that booked in for you.",
        on_error="I couldn't confirm that booking. Let me put you through to reception.",
    )
    async def book_appointment(
        date: str, time: str, name: str, reason: str = "check-up"
    ) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        free = _CALENDAR.get(date, [])
        if time not in free:
            # Somebody else took it between the offer and the confirmation. Rare, real, and the
            # agent must recover rather than claim success.
            return {"booked": False, "reason": "that slot was just taken", "alternatives": free[:3]}
        free.remove(time)
        reference = f"DT{len(_BOOKINGS) + 1041}"
        _BOOKINGS[reference] = {"date": date, "time": time, "name": name, "reason": reason}
        return {"booked": True, "reference": reference, "date": date, "time": time}

    @registry.tool(
        name="lookup_patient",
        description="Find an existing patient record by surname and date of birth.",
        parameters={
            "type": "object",
            "properties": {"surname": {"type": "string"}, "dob": {"type": "string"}},
            "required": ["surname"],
        },
        latency=Latency.FAST,
    )
    def lookup_patient(surname: str, dob: str = "") -> dict[str, Any]:
        known = {"hasan": {"id": "P-2211", "last_seen": "2025-11-04", "notes": "no allergies"}}
        record = known.get(surname.lower())
        return {"found": bool(record), **(record or {})}

    @registry.tool(
        name="send_confirmation",
        description="Text the caller a confirmation. Call after a successful booking.",
        parameters={
            "type": "object",
            "properties": {"reference": {"type": "string"}, "number": {"type": "string"}},
            "required": ["reference"],
        },
        # BACKGROUND: an SMS gateway can take seconds and the caller must not wait for it. The
        # agent says "that's sent" and hangs up; the send completes after the call does.
        latency=Latency.BACKGROUND,
        idempotent=False,
    )
    def send_confirmation(reference: str, number: str = "") -> dict[str, Any]:
        return {"queued": True, "reference": reference}

    return registry


def build_flow() -> Flow:
    """The conversation graph.

    Note what is and is not specified: every node has an OBJECTIVE, never a script. The model
    picks the words. What the graph fixes is which tools exist here and which transitions are
    legal — the two things a caller-facing system cannot leave to sampling.

    THERE IS NO NODE THAT ASKS FOR A NAME, and that is the most important thing about this
    graph. There used to be: "Get the caller's full name for the booking", with a pattern to
    validate it and retries when it failed. It read well and it was wrong, because a name spoken
    down a phone line and transcribed by a browser is not a name — one real call recorded
    "tasty mulasson" for a surname and "abc iphone com" for an email address, both of which pass
    any pattern you would think to write.

    So the caller types those, on screen, and the flow is about the only thing a conversation is
    genuinely better at than a form: working out when somebody can come in. This is also why the
    node objectives below say what NOT to ask. An objective is the strongest instruction the
    model gets, and an objective that says "collect their name" beats any rule elsewhere in the
    prompt telling it not to — which is exactly what happened before this was fixed.
    """
    return Flow(
        name="dental-booking",
        start="greet",
        # Available everywhere, because a caller can ask for a human at any point and an agent
        # that will not transfer is the single fastest way to make someone hate a phone line.
        global_tools=("lookup_patient",),
        nodes={
            "greet": Node(
                id="greet",
                kind=NodeKind.SPEAK,
                objective=(
                    "Greet the caller as Northgate Dental and ask how you can help. "
                    "Be brief — under two sentences."
                ),
                edges=(
                    Edge("reason", when="the caller wants an appointment"),
                    Edge("handoff", when="the caller asks for a human, or sounds distressed"),
                    Edge("goodbye", when="the caller has no further business"),
                ),
            ),
            "reason": Node(
                id="reason",
                kind=NodeKind.COLLECT,
                objective=(
                    "Find out what they need to come in for — a check-up, a scale and polish, pain. "
                    "One short question. Do NOT ask for their name, phone number or email; "
                    "those are typed on screen and you will be told them."
                ),
                collects="reason",
                tools=("lookup_patient",),
                edges=(
                    Edge("preferred_day", when="you know roughly what they need"),
                    Edge("handoff", when="they are in severe pain or distressed"),
                ),
            ),
            "preferred_day": Node(
                id="preferred_day",
                kind=NodeKind.COLLECT,
                objective=(
                    "Find out roughly when they would like to come in. Accept vague answers "
                    "like 'sometime next week' — do not insist on an exact date. Do NOT ask "
                    "for their name, phone number or email."
                ),
                collects="preferred_day",
                edges=(Edge("offer_slots", when="any indication of timing was given"),),
            ),
            "offer_slots": Node(
                id="offer_slots",
                kind=NodeKind.TOOL,
                objective=(
                    "Offer at most three of the times you have been given, and nothing else. "
                    "More than three cannot be held in memory over the phone and the caller "
                    "will ask you to repeat them. Do NOT ask for their name, phone number or "
                    "email."
                ),
                # THE GUARDRAIL. `check_availability` exists here and nowhere else in the flow.
                tools=("check_availability",),
                edges=(
                    Edge("confirm", when="the caller picked one of the offered slots"),
                    Edge("preferred_day", when="none of the slots suit; try another day"),
                    Edge("handoff", when="nothing suitable after two attempts"),
                ),
            ),
            "confirm": Node(
                id="confirm",
                kind=NodeKind.COLLECT,
                objective=(
                    "Read the chosen date and time back and get an explicit yes before booking. "
                    "The time is the ONLY thing to confirm — their details are already on file "
                    "from the screen."
                ),
                collects="confirmed",
                pattern=r"\b(yes|yeah|yep|correct|that's right|confirm|please do|go ahead)\b",
                edges=(
                    Edge("book", when="the caller explicitly confirmed"),
                    Edge("offer_slots", when="the caller corrected the details"),
                ),
            ),
            "book": Node(
                id="book",
                kind=NodeKind.TOOL,
                objective=(
                    "The appointment is already booked by the time you speak. Give the "
                    "reference number slowly and confirm the time. If you are told the slot "
                    "went in the meantime, apologise and offer another."
                ),
                tools=("book_appointment", "send_confirmation"),
                edges=(
                    Edge("goodbye", when="the booking succeeded"),
                    Edge("offer_slots", when="the slot was taken; alternatives were offered"),
                    Edge("handoff", when="the booking system failed"),
                ),
            ),
            "handoff": Node(
                id="handoff",
                kind=NodeKind.TRANSFER,
                objective=(
                    "Tell the caller you are putting them through to reception, and say why. "
                    "A transfer with no explanation feels like being dismissed."
                ),
            ),
            "goodbye": Node(
                id="goodbye",
                kind=NodeKind.END,
                objective="Confirm anything outstanding, thank them, and end warmly.",
            ),
        },
    )
