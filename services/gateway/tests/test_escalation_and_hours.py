"""Two things a caller does that the flow could not do.

BOTH FOUND BY LOOKING AT A CALL LIST, not by reading code. Seven calls were seeded to fill the
history screen for a screenshot, and two rows were wrong in a way no test asserted on:

    answered   text   I want to speak to a human please
    answered   text   Wanted an appointment            <- the caller asked about opening hours

The first is an escalation that never happened. The second is a question about the practice read
as a request for a date.
"""

from __future__ import annotations

from datetime import date

import pytest

from dialtone.agents.support import build_flow
from dialtone.brain.conversation import AgentConfig, Conversation
from dialtone.brain.llm import ScriptedBrain
from dialtone.flow.graph import FlowRunner, NodeKind
from dialtone.scheduling.calendar import asks_about_hours, parse_when

MONDAY = date(2026, 3, 2)


async def run(convo: Conversation, *said: str) -> list[dict]:
    """Drive real turns through `respond`, which is the only order that proves anything.

    THE FIRST VERSION OF THIS FILE APPENDED TURNS BY HAND and then called `_advance` -- and
    passed, while the live system did not escalate at all. `TurnRecord` is appended at the END of
    a turn, so anything reading `self.turns[-1]` during one is reading the PREVIOUS turn. A test
    that builds the state it expects tests the test.
    """
    events: list[dict] = []
    for line in said:
        async for event in convo.respond(line):
            events.append(event)
    return events


def convo() -> Conversation:
    return Conversation(brain=ScriptedBrain(), config=AgentConfig(),
                        flow=build_flow(), today=MONDAY)


class TestAskingForAPerson:
    """The `handoff` node existed, three nodes had an edge to it, and it was unreachable."""

    @pytest.mark.asyncio
    async def test_the_call_is_transferred(self):
        c = convo()
        await run(c, "I want to speak to a human please")
        assert c.state is not None and c.state.transferred
        assert c.runner is not None
        assert c.runner.node(c.state).kind is NodeKind.TRANSFER

    @pytest.mark.asyncio
    async def test_it_moves_before_the_reply_is_written(self):
        """THE POINT. Transferring after generation marks the call correctly and still answers
        the caller from the step they were leaving -- "what do you need to come in for?" to
        somebody who just asked for a person."""
        c = convo()
        events = await run(c, "just put me through to someone")
        moved = [e for e in events if e["type"] == "moved"]
        assert moved and moved[0]["to"] == "handoff"
        # Nothing generated before the move.
        assert events.index(moved[0]) < next(
            i for i, e in enumerate(events) if e["type"] in ("token", "done"))

    @pytest.mark.asyncio
    async def test_it_does_not_wait_for_the_step_to_finish(self):
        """"Put me through" never collects a reason, so a handoff gated on the node being
        satisfied would never fire."""
        c = convo()
        await run(c, "put me through, I do not want to book anything")
        assert c.state is not None and c.state.transferred

    @pytest.mark.asyncio
    async def test_an_ordinary_booking_is_not_transferred(self):
        c = convo()
        await run(c, "I would like to book a cleaning tomorrow morning")
        assert c.state is not None and not c.state.transferred

    @pytest.mark.asyncio
    async def test_pain_is_not_an_escalation(self):
        """A dental agent that hands every sore tooth to a human has no reason to exist. The
        booking IS the answer, which is why the flow's "or sounds distressed" is deliberately
        not implemented."""
        c = convo()
        await run(c, "my tooth really hurts and I need to come in")
        assert c.state is not None and not c.state.transferred

    @pytest.mark.asyncio
    async def test_only_this_turn_counts(self):
        """A caller who mentioned a person once and then booked happily has not asked for one."""
        c = convo()
        await run(c, "could I speak to a human?")
        assert c.state is not None and c.state.transferred

        fresh = convo()
        await run(fresh, "hello", "actually tomorrow morning is fine")
        assert fresh.state is not None and not fresh.state.transferred

    @pytest.mark.asyncio
    async def test_the_prompt_stops_asking_for_details(self):
        """THE OTHER HALF OF THE FIX, and it is not the graph. The transfer fired, the call was
        filed correctly, and the caller was told "we're currently unable to assist with that
        request -- could you provide your full name and phone number". The state had moved and
        the prompt had not, so the model was still reading an instruction to collect intake.

        A transferred call has no availability and no intake note; the handoff objective stands
        alone, exactly as the just-booked case does."""
        c = convo()
        await run(c, "put me through to a person please")
        assert c._scheduling_note() == ""

    def test_the_transfer_is_still_a_declared_edge(self):
        """`forced=True` skips the collect guard, NOT the graph. An undeclared edge is still
        refused, which is the guarantee the whole flow is built on."""
        runner = FlowRunner(build_flow())
        state = runner.start()
        assert any(e.to == "handoff" for e in runner.flow.nodes[state.node_id].edges)


class TestAskingAboutOpeningHours:
    """"Are you open on Saturday?" names a day and requests nothing."""

    def test_it_sets_no_day(self):
        assert parse_when("are you open on saturday?", MONDAY).day is None

    def test_the_singular_and_the_plural_behave_the_same(self):
        """The plural was fixed first, with a word boundary. That was the wrong diagnosis: the
        singular is just as common and just as much a question."""
        assert parse_when("are you open on thursdays?", MONDAY).day is None
        assert parse_when("are you open on thursday?", MONDAY).day is None

    def test_a_time_in_an_hours_question_is_not_kept_either(self):
        """A half-parsed request -- an hour with no day -- is harder to spot than none at all."""
        heard = parse_when("are you open at eight?", MONDAY)
        assert heard.day is None and heard.hour is None

    def test_a_request_in_the_same_breath_still_counts(self):
        assert parse_when("are you open tomorrow? I need an appointment", MONDAY).day is not None

    def test_asking_for_a_time_is_untouched(self):
        for line in ("can I come tomorrow morning?", "are you free tomorrow afternoon?",
                     "could I book something on thursday at ten"):
            assert parse_when(line, MONDAY).day is not None, line

    def test_the_predicate_is_public_and_says_what_it_means(self):
        assert asks_about_hours("what time do you open on monday?")
        assert not asks_about_hours("can I have an appointment on monday?")
