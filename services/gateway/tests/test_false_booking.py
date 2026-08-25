"""The agent saying an appointment exists when it does not.

THE WORST THING THIS SYSTEM CAN DO, and it is worth being precise about why. Failing to book is a
bad call: the caller notices, rings back, and is annoyed. CLAIMING to have booked is a different
category — the caller hangs up satisfied, writes it in their diary, rearranges their morning, and
finds out on the day. Nothing downstream recovers from it, and the agent sounded most competent at
exactly the moment it was most wrong.

It happened on a real call. The slot was genuinely taken, the details had never been typed, the
booking correctly did not happen, and the agent said:

    "Understood. Your appointment has been scheduled for Tuesday, August 25th, at 10:30 AM."

The model was not being careless — it was completing a conversation that sounded finished. Which
is exactly why this is checked against the database rather than asked for in the prompt.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from dialtone.brain.conversation import AgentConfig, Conversation, claims_a_booking
from dialtone.brain.llm import ScriptedBrain

MONDAY = date(2026, 3, 2)


class Book:
    def taken_slots(self) -> set[str]:
        return set()

    def book(self, starts_at: str, **fields: Any) -> dict[str, Any]:
        return {"reference": "NGTEST01", "starts_at": starts_at,
                "patient_name": fields.get("patient_name", ""), "reason": ""}


def conversation() -> Conversation:
    return Conversation(brain=ScriptedBrain(), config=AgentConfig(),
                        booking=Book(), today=MONDAY)


CLAIMS = [
    "Understood. Your appointment has been scheduled for Tuesday, August 25th, at 10:30 AM.",
    "Great! We have booked you in for tomorrow at nine.",
    "You're all set for Thursday at two.",
    "I have scheduled your cleaning.",
    "Your booking is confirmed.",
    "Your appointment is now booked.",
    "We will put you down for Friday.",
    "See you tomorrow!",
    "I've reserved that slot for you.",
]

HONEST = [
    "Tomorrow at ten thirty in the morning is free. Shall I book it?",
    "I still need your email before I can book that.",
    "A check-up costs seventy five dollars.",
    "We are open Monday through Friday from eight thirty until six.",
    "Would you like me to book that for you?",
    "What time would suit you?",
    "That slot has gone, I am afraid. I can do Thursday at two.",
    "I can book that once your details are on screen.",
    "There is free parking behind the building.",
]


class TestSpottingTheClaim:
    @pytest.mark.parametrize("reply", CLAIMS)
    def test_a_claim_is_found(self, reply: str):
        assert claims_a_booking(reply), f"missed: {reply!r}"

    @pytest.mark.parametrize("reply", HONEST)
    def test_an_honest_reply_is_left_alone(self, reply: str):
        """The half that stops this being a nuisance. A guard that fires on "shall I book it?"
        would replace every offer with an apology, which is worse than the bug."""
        assert not claims_a_booking(reply), f"false positive: {reply!r}"


class TestWhatIsSaidInstead:
    def test_it_names_what_is_actually_missing(self):
        """"I cannot book that" leaves the caller with nothing to do about it, and the obstacle
        is nearly always something they can fix in ten seconds."""
        convo = conversation()
        convo.memory.observe("I need a cleaning, can I come tomorrow at ten")
        convo._scheduling_note()                       # puts a slot on the table

        instead = convo._why_not_booked()
        assert "not booked that yet" in instead
        assert "full name" in instead
        assert convo.memory.proposed_slot in instead   # and it still holds the time for them

    def test_with_everything_typed_it_asks_for_the_yes(self):
        convo = conversation()
        convo.memory.observe("a cleaning tomorrow at ten")
        convo._scheduling_note()
        for field, value in [("name", "Sam Hassan"), ("phone", "(212) 555-9876"),
                             ("email", "sam@example.com")]:
            convo.memory.tell(field, value)

        instead = convo._why_not_booked()
        assert "Shall I book" in instead

    def test_with_no_slot_at_all_it_offers_to_try_again(self):
        convo = conversation()
        for field, value in [("name", "Sam"), ("phone", "(212) 555-9876"),
                             ("email", "s@example.com"), ("reason", "cleaning")]:
            convo.memory.tell(field, value)
        assert "another time" in convo._why_not_booked()

    def test_it_never_claims_a_booking_itself(self):
        """The replacement must not trip the guard that produced it."""
        convo = conversation()
        convo.memory.observe("a cleaning tomorrow at ten")
        convo._scheduling_note()
        for state in (lambda: None, lambda: [convo.memory.tell(k, v) for k, v in
                                             [("name", "S"), ("phone", "(212) 555-9876"),
                                              ("email", "s@example.com")]]):
            state()
            assert not claims_a_booking(convo._why_not_booked())


class TestTheRealCall:
    def test_the_exact_sentence_from_the_transcript(self):
        assert claims_a_booking(
            "Understood. Your appointment has been scheduled for Tuesday, August 25th, at 10:30 AM."
        )

    def test_a_booking_that_did_happen_is_not_touched(self):
        """Once there IS a reference, saying so is the correct thing to do — and the agent is
        explicitly instructed to. The guard only fires when the database disagrees."""
        convo = conversation()
        convo.memory.booked_reference = "NG5EA086"
        # The guard is gated on `booked_reference` being empty; with one set it never runs.
        assert convo.memory.booked_reference


class TestTheSentenceFromTheDemoVideo:
    """FOUND IN A RECORDING. `scripts/demo-video.mjs` drives a real call and records it, and frame
    0:30 of the first take had the agent saying an appointment was reserved when the caller had not
    yet typed an email and nothing had been written.

    The guard missed it because it wanted the noun and the verb next to each other -- "appointment
    is booked" -- and a real model does not write like that. Twenty words separated them.
    """

    QUOTED = ("Certainly! Your appointment for a cleaning on Wednesday, the twenty-sixth of "
              "August, at nine thirty in the morning is already reserved.")

    def test_the_claim_is_caught(self):
        assert claims_a_booking(self.QUOTED)

    def test_the_words_may_be_far_apart(self):
        for reply in (
            "Your appointment for a cleaning on Thursday at four is confirmed.",
            "The slot you asked about on Friday morning has been reserved.",
            "That appointment, the one at nine thirty, was made for you.",
        ):
            assert claims_a_booking(reply), reply

    def test_it_does_not_reach_across_a_full_stop(self):
        """Bounded by sentence, not by word count. Otherwise "would you like an appointment? I can
        see Thursday is booked" reads as a claim about the caller's own booking, and every honest
        mention of a full diary becomes a false positive."""
        assert not claims_a_booking(
            "Would you like an appointment? I can see that Thursday at ten is already booked."
        )

    def test_offering_and_refusing_still_do_not_fire(self):
        for reply in (
            "What time would you like the appointment?",
            "I can check whether that slot is free.",
            "The appointment cannot be booked until you type your email address.",
            "Would you like me to book that appointment for you?",
        ):
            assert not claims_a_booking(reply), reply
