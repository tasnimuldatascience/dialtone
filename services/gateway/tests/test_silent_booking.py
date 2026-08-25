"""An appointment that exists, and a reply that never mentions it.

FOUND ON VIDEO. `scripts/demo-video.mjs` records a real call, and the take showed this:

    caller:  yes, that works
    agent:   Alright, let's proceed with booking your appointment. Could you confirm your
             name, phone number, and email address?

The appointment had already been made. Tracing the event stream settled what was wrong with it:

    [booked] NGCD9C74 — tomorrow at nine in the morning
    [first token]
    reply: Alright, let's proceed with booking your appointment...

`booked` arrives BEFORE the first token, so the ordering was right and the prompt for that turn
opened with "THE APPOINTMENT IS NOW BOOKED ... say the reference letter by letter". The model
ignored it — on the single most important turn of the call, asking for details it already had,
about a booking it had already made.

`test_false_booking.py` guards the opposite direction and has since the first week. This one had
nothing. It is the same damage seen from the other side: there, a caller hangs up believing in an
appointment that does not exist; here, a caller hangs up not knowing they have one.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from dialtone.brain.conversation import AgentConfig, Conversation, silent_booking
from dialtone.brain.llm import ScriptedBrain

MONDAY = date(2026, 3, 2)
REF = "NGCD9C74"


class Diary:
    def taken_slots(self) -> set[str]:
        return set()

    def book(self, starts_at: str, **fields: Any) -> dict[str, Any]:
        return {"reference": REF, "starts_at": starts_at,
                "patient_name": fields.get("patient_name", ""), "reason": ""}


def booked_call() -> Conversation:
    convo = Conversation(brain=ScriptedBrain(), config=AgentConfig(),
                         booking=Diary(), today=MONDAY)
    convo.memory.observe("a cleaning tomorrow at ten")
    convo._scheduling_note()
    for field, value in [("name", "Sam Hassan"), ("phone", "(212) 774-1188"),
                         ("email", "sam@example.com")]:
        convo.memory.tell(field, value)
    convo.memory.slot_confirmed = True
    assert convo.book_if_ready() is not None
    return convo


class TestTheDetection:
    def test_the_exact_reply_from_the_recording(self):
        assert silent_booking(
            "Alright, let's proceed with booking your appointment. Could you confirm your "
            "name, phone number, and email address?", REF)

    def test_naming_the_reference_is_not_silent(self):
        assert not silent_booking(f"All done — your reference is {REF}.", REF)

    def test_the_reference_may_be_written_in_any_case(self):
        assert not silent_booking(f"Your reference is {REF.lower()}.", REF)

    def test_saying_it_in_words_is_not_silent(self):
        """Once a reference exists, claiming a booking is CORRECT — it is what the agent is asked
        to do. The same phrase is a lie without one, which is `claims_a_booking`'s job."""
        for reply in ("You're all set for tomorrow at ten.",
                      "I've booked you in for tomorrow morning.",
                      "Your appointment is confirmed."):
            assert not silent_booking(reply, REF), reply

    def test_no_booking_means_nothing_to_be_silent_about(self):
        assert not silent_booking("Could you confirm your name?", "")


class TestWhatIsSaidInstead:
    def test_it_says_the_three_things(self):
        convo = booked_call()
        said = convo._confirm_booking()
        assert REF in said
        assert convo.memory.proposed_slot in said
        assert "booked" in said.lower()

    def test_it_is_not_silent_by_its_own_test(self):
        """The replacement must pass the guard that produced it, or the next turn replaces it
        again and the caller hears the same sentence twice."""
        convo = booked_call()
        assert not silent_booking(convo._confirm_booking(), convo.memory.booked_reference)

    def test_it_does_not_claim_more_than_happened(self):
        """It is written from `booked_reference` and `proposed_slot`, both of which came from the
        database — so it cannot invent a time the way the reply it replaces did."""
        convo = booked_call()
        said = convo._confirm_booking()
        assert convo.memory.booked_reference and convo.memory.booked_reference in said
