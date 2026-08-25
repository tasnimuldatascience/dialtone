"""Two callers wanting the same slot at the same time.

FOUND BY RUNNING FIVE CALLERS AT ONCE. Two of them were offered tomorrow at eight thirty --
correctly, it was free when each was offered it -- and one of them booked it. The database did
exactly what it was built to do: `appointments.starts_at` is UNIQUE, one INSERT won, and there was
no double booking.

NOBODY TOLD THE OTHER CALLER. Their call simply carried on. They had agreed a time, said "yes,
that works", and were answered with something unrelated; the appointment never existed and the
conversation gave no sign of it. That is worse than the double booking it was preventing, because
a double booking is at least visible to somebody.

The guarantee was never the problem. The silence was.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from dialtone.brain.conversation import AgentConfig, Conversation
from dialtone.brain.llm import ScriptedBrain

MONDAY = date(2026, 3, 2)


class Book:
    """A diary another caller can reach into mid-conversation."""

    def __init__(self) -> None:
        self.taken: set[str] = set()
        self.written: list[str] = []

    def taken_slots(self) -> set[str]:
        return set(self.taken)

    def book(self, starts_at: str, **fields: Any) -> dict[str, Any] | None:
        if starts_at in self.taken:
            return None                      # the UNIQUE constraint, in miniature
        self.taken.add(starts_at)
        self.written.append(starts_at)
        return {"reference": "NGTEST01", "starts_at": starts_at,
                "patient_name": fields.get("patient_name", ""), "reason": ""}


def ready_caller() -> tuple[Conversation, Book]:
    """A caller with everything typed, who has agreed a time."""
    book = Book()
    convo = Conversation(brain=ScriptedBrain(), config=AgentConfig(),
                         booking=book, today=MONDAY)
    convo.memory.observe("a cleaning tomorrow at ten")
    convo._scheduling_note()                 # puts the slot on the table
    for field, value in [("name", "Sam Hassan"), ("phone", "(212) 555-9876"),
                         ("email", "sam@example.com")]:
        convo.memory.tell(field, value)
    convo.memory.slot_confirmed = True
    return convo, book


class TestTheGuarantee:
    def test_the_second_caller_cannot_have_it(self):
        first, book = ready_caller()
        assert first.book_if_ready() is not None

        second = Conversation(brain=ScriptedBrain(), config=AgentConfig(),
                              booking=book, today=MONDAY)
        second.memory.observe("a cleaning tomorrow at ten")
        second._scheduling_note()
        for field, value in [("name", "Alex Reed"), ("phone", "(212) 555-9877"),
                             ("email", "alex@example.com")]:
            second.memory.tell(field, value)
        second.memory.slot_confirmed = True

        assert second.book_if_ready() is None
        assert len(book.written) == 1


class TestTheLoserIsTold:
    def test_the_note_says_the_time_has_gone(self):
        """THE ACTUAL FIX. The guarantee held all along; the caller was never informed of it."""
        convo, book = ready_caller()
        agreed = convo.memory.proposed_slot
        book.taken.add(convo.memory.proposed_iso)      # somebody else got there first

        note = convo._scheduling_note()
        assert "JUST been taken" in note
        assert agreed in note
        assert "Do not pretend it is still available" in note

    def test_it_offers_something_else(self):
        """"That has gone" and nothing further makes the caller start over. They came to book."""
        convo, book = ready_caller()
        book.taken.add(convo.memory.proposed_iso)
        note = convo._scheduling_note()
        assert "instead" in note
        assert "in the morning" in note or "in the afternoon" in note

    def test_the_agreement_is_cleared(self):
        """Otherwise the caller is left confirming something that no longer exists, and the next
        "yes" attaches to a slot that is gone."""
        convo, book = ready_caller()
        book.taken.add(convo.memory.proposed_iso)
        convo._scheduling_note()

        assert convo.memory.proposed_slot == ""
        assert convo.memory.proposed_iso == ""
        assert not convo.memory.slot_confirmed
        assert not convo.memory.ready_to_book

    def test_a_fresh_time_can_be_agreed_afterwards(self):
        """The call must be able to recover. Losing a race is a detour, not the end."""
        convo, book = ready_caller()
        book.taken.add(convo.memory.proposed_iso)
        convo._scheduling_note()                       # told, and cleared

        convo.memory.observe("how about eleven then")
        convo._scheduling_note()
        assert convo.memory.proposed_slot
        assert "eleven" in convo.memory.proposed_slot

    def test_a_slot_that_is_still_free_is_left_alone(self):
        """The other direction: a note that cries "taken" over a free slot would make every
        booking restart forever."""
        convo, _ = ready_caller()
        note = convo._scheduling_note()
        assert "JUST been taken" not in note

    def test_it_says_nothing_once_the_booking_exists(self):
        """After booking, the slot IS taken -- by this caller. Announcing that would tell them
        their own appointment had been given away."""
        convo, _ = ready_caller()
        booked = convo.book_if_ready()
        assert booked is not None

        note = convo._scheduling_note()
        assert "JUST been taken" not in note
        assert booked["reference"] in note
