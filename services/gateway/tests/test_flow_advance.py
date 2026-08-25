"""Moving through the conversation graph, and calling the tools it scopes.

THE STATE THIS FILE WAS WRITTEN IN. A trace of a real four-turn booking call:

    1. "hi, I'd like to book a cleaning"   node='greet'   TOOLS=[]
    2. "can I come tomorrow?"              node='greet'   TOOLS=[]
    3. "how about ten thirty"              node='greet'   TOOLS=[]
    4. "yes please, book it"               node='greet'   TOOLS=[]

The flow never left the first node and no tool was ever called. `offer_slots` and `book` were
unreachable, so `check_availability` and `book_appointment` -- the two tools the README documents
-- could not run. `FlowRunner.collect()` and `transition(forced=True)` both existed and nothing
called either of them.

WHY IT DID NOT MOVE. The graph was designed to be driven by the model appending `[[node_id]]` to
its reply. A 1.5B model given a system prompt that opens with "one or two sentences, never use
markdown, this is a phone call" does not then emit a bracketed token, and it never did once.

So the same conclusion the booking already reached applies here: THE MODEL PROPOSES, CODE DECIDES.
The graph is not weakened by it -- every move still goes through `FlowRunner.transition`, so an
edge the graph does not permit is still refused. What changed is who proposes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from dialtone.agents.support import build_flow, build_registry
from dialtone.brain.conversation import AgentConfig, Conversation
from dialtone.brain.llm import ScriptedBrain
from dialtone.scheduling.calendar import available

MONDAY = date(2026, 3, 2)


class Book:
    def __init__(self) -> None:
        self.written: list[tuple[str, dict[str, Any]]] = []

    def taken_slots(self) -> set[str]:
        return set()

    def book(self, starts_at: str, **fields: Any) -> dict[str, Any]:
        self.written.append((starts_at, fields))
        return {"reference": "NGTEST01", "starts_at": starts_at,
                "patient_name": fields.get("patient_name", ""),
                "reason": fields.get("reason", "")}


def conversation() -> tuple[Conversation, Book]:
    book = Book()
    convo = Conversation(
        brain=ScriptedBrain(), config=AgentConfig(), flow=build_flow(),
        tools=build_registry(), booking=book, today=MONDAY,
    )
    return convo, book


def at(convo: Conversation) -> str:
    return convo.state.node_id if convo.state else ""


class TestItMovesWhenTheStepIsDone:
    def test_a_call_starts_at_the_beginning(self):
        convo, _ = conversation()
        assert at(convo) == "greet"

    def test_greeting_once_is_enough_to_move_on(self):
        convo, _ = conversation()
        assert convo._advance() == "reason"

    def test_it_waits_for_a_collect_step(self):
        """`reason` collects why they rang. Until it has that, it stays."""
        convo, _ = conversation()
        convo._advance()
        assert at(convo) == "reason"
        assert convo._advance() == ""          # nothing collected yet
        assert at(convo) == "reason"

    def test_it_moves_once_the_value_arrives(self):
        convo, _ = conversation()
        convo._advance()
        convo.memory.observe("my tooth needs a cleaning")
        assert convo._advance() == "preferred_day"

    def test_several_steps_in_one_turn(self):
        """A caller can answer several questions at once. "Hi, I'd like to book a cleaning
        tomorrow at ten thirty" satisfies the reason AND the timing in one sentence, and a flow
        that moved one node per turn would spend three more turns asking for what it has."""
        convo, _ = conversation()
        convo.memory.observe("hi, I'd like to book a cleaning tomorrow at ten thirty")
        moved = convo._advance()
        assert moved == "offer_slots", f"stopped at {moved!r}"

    def test_it_does_not_run_off_the_end(self):
        convo, _ = conversation()
        convo.memory.observe("a cleaning tomorrow at ten thirty")
        convo._advance()
        # `offer_slots` is a tool node; nothing has run, so it stays put.
        assert convo._advance() == ""
        assert at(convo) == "offer_slots"

    def test_a_terminal_node_is_terminal(self):
        convo, _ = conversation()
        convo.runner.transition(convo.state, "goodbye", forced=True)
        assert convo._advance() == ""
        assert at(convo) == "goodbye"


class TestTheGraphStillDecidesWhatIsLegal:
    def test_every_step_it_takes_is_a_declared_edge(self):
        """Checked over the whole path rather than one hop: `_advance` can cross several nodes
        in a turn, so the interesting property is that the ROUTE is legal end to end."""
        convo, _ = conversation()
        flow = build_flow()
        convo.memory.observe("a cleaning tomorrow at ten thirty")
        convo.memory.slot_confirmed = True
        convo.memory.proposed_slot = "tomorrow at ten thirty in the morning"
        for _ in range(len(flow.nodes)):
            if not convo._advance(ran=("check_availability",)):
                break

        route = convo.state.visited
        assert len(route) > 1, "it never moved"
        for before, after in zip(route, route[1:], strict=False):
            legal = {e.to for e in flow.nodes[before].edges}
            assert after in legal, f"{before} -> {after} is not an edge in the graph"

    def test_a_cycle_cannot_spin_forever(self):
        """`offer_slots` has an edge back to `preferred_day`. A loop that follows satisfied
        nodes must be bounded or a graph with a cycle hangs the call."""
        convo, _ = conversation()
        convo.memory.observe("a cleaning tomorrow at ten thirty")
        convo.memory.slot_confirmed = True
        for _ in range(5):
            convo._advance()                    # returns, every time
        assert at(convo) in build_flow().nodes


class TestWhatTheAgentOffered:
    """`proposed_slot` used to be set only when the CALLER named a precise time. When the agent
    made the offer and the caller simply agreed -- the ordinary shape of the conversation, and
    the one the prompt asks for -- nothing recorded what had been agreed to."""

    def test_a_time_the_agent_offered_is_recorded(self):
        convo, _ = conversation()
        convo.memory.observe("can I come tomorrow morning?")
        convo._scheduling_note()                # puts the free slots in front of the model
        offered = convo._offered
        assert offered

        spoken = offered[0].spoken(MONDAY)
        assert convo._slot_the_agent_offered(f"Of course! {spoken} would be great.")
        assert convo.memory.proposed_iso == offered[0].iso

    def test_a_time_it_was_never_given_is_ignored(self):
        """The other half of "the model proposes, code decides". A reply naming a time that is
        not in the list it was handed matches nothing."""
        convo, _ = conversation()
        convo.memory.observe("can I come tomorrow morning?")
        convo._scheduling_note()
        assert convo._slot_the_agent_offered("How about three in the morning?") == ""
        assert convo.memory.proposed_iso == ""

    def test_a_reply_with_no_time_in_it_offers_nothing(self):
        convo, _ = conversation()
        convo.memory.observe("can I come tomorrow morning?")
        convo._scheduling_note()
        assert convo._slot_the_agent_offered("Certainly, what time suits you?") == ""


class TestBookingWhatWasAgreed:
    def test_it_books_the_slot_that_was_offered(self):
        """NOT a re-derivation from what the caller said. The caller said "tomorrow morning" --
        no hour -- so working back from their words found nothing, and the booking silently did
        not happen with everything else in place and `ready_to_book` reporting True."""
        convo, book = conversation()
        convo.memory.observe("I need a cleaning, can I come tomorrow morning?")
        convo._scheduling_note()
        spoken = convo._offered[0].spoken(MONDAY)
        # As `respond` does it: the method reports what was offered, the caller assigns it.
        convo.memory.proposed_slot = convo._slot_the_agent_offered(f"{spoken} would be great.")

        for field, value in [("name", "Sam Hassan"), ("phone", "(212) 555-0142"),
                             ("email", "sam@example.com")]:
            convo.memory.tell(field, value)
        convo.memory.slot_confirmed = True

        booked = convo.book_if_ready()
        assert booked is not None
        assert book.written[0][0] == convo._offered[0].iso

    def test_a_slot_taken_since_it_was_offered_is_refused(self):
        """Still checked against the live calendar. Storing the ISO makes the booking precise;
        it must not make it stale."""
        convo, _ = conversation()
        free = available(set(), today=MONDAY, now=datetime(2026, 3, 2, 9, 0))
        convo.memory.proposed_slot = free[0].spoken(MONDAY)
        convo.memory.proposed_iso = "2026-03-03T23:30"      # never a real slot
        convo.memory.slot_confirmed = True
        convo.memory.observe("a cleaning")
        for field, value in [("name", "S"), ("phone", "p"), ("email", "e")]:
            convo.memory.tell(field, value)

        assert convo.book_if_ready() is None


class TestTheToolsBecomeReachable:
    def test_the_booking_node_can_be_reached(self):
        """The point of all of the above. `book_appointment` lives on one node and nowhere else,
        so if that node is unreachable the tool may as well not exist."""
        convo, _ = conversation()
        convo.memory.observe("a cleaning tomorrow at ten thirty")
        convo.memory.slot_confirmed = True
        convo.memory.proposed_slot = "tomorrow at ten thirty in the morning"

        for _ in range(len(build_flow().nodes)):
            if not convo._advance(ran=("check_availability",)):
                break
        # `visited`, not the return value: _advance crosses several nodes in one call and
        # reports only where it stopped.
        assert "offer_slots" in convo.state.visited, f"only reached {convo.state.visited}"

    @pytest.mark.parametrize("tool", ["check_availability", "book_appointment"])
    def test_the_tool_exists_and_is_scoped_to_one_node(self, tool: str):
        registry = build_registry()
        assert registry.spec(tool) is not None
        owners = [n.id for n in build_flow().nodes.values() if tool in n.tools]
        assert len(owners) == 1, f"{tool} is on {owners}; scoping it to one node is the guardrail"
