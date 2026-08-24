"""The appointment book, the call's memory, and the line between them.

These cover the failure a real call produced. The caller asked for an appointment, gave their
name, their preferred day, then a time, then a phone number and an email — and the agent booked
nothing, because nothing was holding any of it:

    caller:  will you be available tomorrow morning
    agent:   I'm sorry, but I don't have access to real-time scheduling information.
    caller:  I'll be available tomorrow morning
    agent:   Can you tell me when exactly tomorrow morning starts?
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dialtone.brain.memory import CallMemory, _extract, summarise
from dialtone.scheduling.calendar import (
    EVENING_FROM,
    available,
    day_slots,
    match_slot,
    parse_when,
    suggest,
)
from dialtone.store.db import Store

MONDAY = date(2026, 3, 2)
NINE_AM = datetime(2026, 3, 2, 9, 0)


# ── the calendar ─────────────────────────────────────────────────────────────
class TestCalendar:
    def test_the_practice_is_shut_at_weekends(self):
        assert day_slots(date(2026, 3, 7)) == []      # Saturday
        assert day_slots(date(2026, 3, 8)) == []      # Sunday
        assert day_slots(MONDAY)

    def test_nothing_is_offered_over_lunch(self):
        noon = [s for s in day_slots(MONDAY) if s.start.hour == 12]
        assert noon == []

    def test_thursday_runs_late(self):
        thursday = max(s.start.hour for s in day_slots(date(2026, 3, 5)))
        monday = max(s.start.hour for s in day_slots(MONDAY))
        assert thursday > monday

    def test_a_slot_starting_in_ten_minutes_is_not_offered(self):
        """Nobody can keep it, and the caller has to ring back to cancel."""
        soon = datetime(2026, 3, 2, 9, 25)
        offered = available(set(), today=MONDAY, now=soon)
        assert all(s.start > soon for s in offered)
        today_slots = [s for s in offered if s.start.date() == MONDAY]
        assert not any(s.start.hour == 9 and s.start.minute == 30 for s in today_slots)

    def test_a_booked_slot_disappears(self):
        free = available(set(), today=MONDAY, now=NINE_AM)
        taken = free[0]
        after = available({taken.iso}, today=MONDAY, now=NINE_AM)
        assert taken.iso not in {s.iso for s in after}

    def test_times_are_spoken_the_way_a_person_says_them(self):
        slot = next(s for s in available(set(), today=MONDAY, now=NINE_AM)
                    if s.start.date() == MONDAY and s.start.hour == 10 and s.start.minute == 30)
        assert slot.spoken(MONDAY) == "today at ten thirty in the morning"

    def test_evening_means_the_same_thing_to_the_filter_and_the_words(self):
        """Told "Thursday evening" and offered "five in the afternoon", a caller assumes they
        were misheard. The boundary has to be one number, not two."""
        free = available(set(), today=MONDAY, now=NINE_AM)
        picks = suggest(parse_when("thursday evening", MONDAY), free, 3)
        assert picks
        for slot in picks:
            assert slot.start.hour >= EVENING_FROM
            assert "evening" in slot.spoken(MONDAY)


# ── understanding when ───────────────────────────────────────────────────────
class TestParsingWhen:
    @pytest.mark.parametrize("phrase,expected_day", [
        ("tomorrow morning", date(2026, 3, 3)),
        ("the day after tomorrow", date(2026, 3, 4)),
        ("thursday", date(2026, 3, 5)),
        ("next tuesday", date(2026, 3, 10)),
        ("this afternoon", MONDAY),
    ])
    def test_days(self, phrase: str, expected_day: date):
        assert parse_when(phrase, MONDAY).day == expected_day

    @pytest.mark.parametrize("phrase,hour,minute", [
        ("at 10am", 10, 0),
        ("10:30", 10, 30),
        ("at two", 14, 0),
        ("ten thirty", 10, 30),
        ("four o'clock", 16, 0),
    ])
    def test_times(self, phrase: str, hour: int, minute: int):
        when = parse_when(phrase, MONDAY)
        assert (when.hour, when.minute) == (hour, minute)

    def test_a_bare_hour_lands_inside_opening_hours(self):
        """The practice shuts at six, so "four" is the afternoon. Guessing wrong books somebody
        into the wrong half of the day."""
        assert parse_when("four", MONDAY).hour == 16
        assert parse_when("nine", MONDAY).hour == 9

    def test_a_vague_answer_still_says_something(self):
        """"Sometime next week" is real information. A parser that returns nothing unless it
        understands everything throws it away."""
        when = parse_when("sometime in the morning", MONDAY)
        assert when.part == "morning"
        assert when.day is None

    def test_the_day_survives_the_turn_that_names_the_hour(self):
        """THE BUG FROM THE REAL CALL. "tomorrow morning" then "how about ten" is one request in
        two parts, and treating the second as fresh loses the day — which is what produced
        "can you tell me when exactly tomorrow morning starts?"."""
        when = parse_when("will you be available tomorrow morning", MONDAY)
        when = when.merge(parse_when("how about ten", MONDAY))
        assert when.day == date(2026, 3, 3)
        assert when.hour == 10
        assert when.specific

    def test_a_named_hour_beats_a_vague_part_of_day(self):
        when = parse_when("tomorrow morning", MONDAY).merge(parse_when("actually two", MONDAY))
        assert when.hour == 14
        assert when.part is None

    def test_a_phone_number_is_not_a_time(self):
        assert parse_when("my number is 212 555 0142", MONDAY).hour is None

    @pytest.mark.parametrize("phrase", [
        "are you open on thursdays?",
        "how late on thursdays",
        "do you do saturdays",
        "are you open on tuesdays and thursdays?",
    ])
    def test_a_plural_weekday_is_a_question_not_a_request(self, phrase: str):
        """SPOTTED IN A SCREENSHOT. The caller asked "are you open on thursdays?" and the panel
        showing what the agent knew said "Wants: Thursday, August 27" -- a day they had never
        asked for, on its way into a booking.

        "On thursdays" is habitual: a question about the practice. "On thursday" is a date."""
        assert parse_when(phrase, MONDAY).day is None

    @pytest.mark.parametrize("phrase", ["can I come on thursday", "thursday please",
                                        "this thursday", "next thursday"])
    def test_a_singular_weekday_still_is_a_request(self, phrase: str):
        assert parse_when(phrase, MONDAY).day is not None

    @pytest.mark.parametrize("phrase", [
        "one more thing, do you do whitening?",
        "just one moment",
        "give me one second",
        "I need 2 fillings",
        "two of my teeth hurt",
        "one of my crowns came out",
    ])
    def test_a_number_in_a_sentence_is_not_a_time(self, phrase: str):
        """FOUND ON A THIRTY-TURN CALL. The caller finished with "one more thing — do you do
        whitening?", "one" was read as one o'clock, and the appointment they had already agreed
        for nine thirty in the morning silently became one in the afternoon.

        A number followed by the thing it counts is a quantity, not a time."""
        assert parse_when(phrase, MONDAY).hour is None

    @pytest.mark.parametrize("phrase,hour", [
        ("four", 16),                  # answering "what time?" with one word
        ("actually two", 14),          # a correction
        ("maybe three", 15),
        ("at two", 14),                # a preposition puts it in the position of a time
        ("can I come at nine", 9),
        ("how about ten", 10),
        ("nine thirty works", 9),      # an explicit minute
    ])
    def test_a_number_that_is_a_time_still_is(self, phrase: str, hour: int):
        """The other direction, which is what makes the guard above worth having: refusing every
        bare number would mean a caller answering "four" is asked again."""
        assert parse_when(phrase, MONDAY).hour == hour


# ── memory ───────────────────────────────────────────────────────────────────
class TestMemory:
    def test_it_remembers_across_the_whole_call(self):
        memory = CallMemory(today=MONDAY)
        for line in [
            "hello my name is Sam Hassan, I need an appointment for my tooth",
            "will you be available tomorrow morning",
            "how about ten",
            "my number is 212 555 0142",
        ]:
            memory.observe(line)

        assert memory.get("name") == "Sam Hassan"
        assert memory.get("phone") == "(212) 555-0142"
        assert memory.when.day == date(2026, 3, 3)
        assert memory.when.hour == 10

    def test_what_it_knows_is_put_in_front_of_the_model(self):
        memory = CallMemory(today=MONDAY)
        memory.observe("my name is Sam Hassan")
        prompt = memory.as_prompt()
        assert "Sam Hassan" in prompt
        assert "do NOT ask for any of it again" in prompt

    def test_it_knows_what_is_still_missing(self):
        memory = CallMemory(today=MONDAY)
        memory.observe("my name is Sam Hassan and I need a cleaning")
        assert "phone" in memory.missing
        assert "name" not in memory.missing

    def test_a_typed_value_outranks_a_heard_one(self):
        """Recognition mangles exactly the values that must be exact — a real call produced
        "tasty mulasson" for a surname."""
        memory = CallMemory(today=MONDAY)
        memory.observe("my name is Tasty Mulasson")
        assert memory.get("name") == "Tasty Mulasson"

        memory.tell("name", "Tasnimul Hasan")
        memory.observe("my name is Tasty Mulasson again")
        assert memory.get("name") == "Tasnimul Hasan"

    def test_a_spoken_detail_is_not_good_enough_to_book_on(self):
        memory = CallMemory(today=MONDAY)
        memory.observe("my name is Sam Hassan, my number is 212 555 0142, I need a cleaning")
        memory.observe("my email is sam@example.com")
        memory.observe("tomorrow at ten")
        memory.proposed_slot = "tomorrow at ten in the morning"
        memory.slot_confirmed = True

        assert not memory.missing
        assert memory.unconfirmed          # heard, not typed
        assert not memory.ready_to_book

        for field, value in [("name", "Sam Hassan"), ("phone", "(212) 555-0142"),
                             ("email", "sam@example.com")]:
            memory.tell(field, value)
        assert memory.ready_to_book

    def test_nothing_is_booked_until_the_caller_says_yes(self):
        """Everything present is not the same as everything agreed. A form that completes itself
        into a booking is how somebody ends up in a diary they never confirmed."""
        memory = CallMemory(today=MONDAY)
        memory.observe("I need a cleaning")
        for field, value in [("name", "Sam Hassan"), ("phone", "(212) 555-0142"),
                             ("email", "sam@example.com")]:
            memory.tell(field, value)
        memory.proposed_slot = "tomorrow at ten in the morning"

        assert not memory.missing and not memory.unconfirmed
        assert not memory.ready_to_book          # nobody has agreed to anything

        memory.slot_confirmed = True
        assert memory.ready_to_book

    def test_an_email_address_does_not_decide_why_they_are_calling(self):
        """"example.com" contains "exam". Matching anywhere in the text let an email address
        silently set the appointment reason."""
        assert "reason" not in _extract("my email is sam@example.com")

    def test_a_name_stops_at_a_connector(self):
        """One sentence carrying two facts. The pattern read the join as a surname and produced
        "Sam Hassan And", which the agent would then read back to the caller."""
        assert _extract("my name is Sam Hassan and my number is 212 555 0142")["name"] == "Sam Hassan"

    def test_the_start_of_a_long_call_is_not_lost(self):
        turns = [(f"point number {i}", "") for i in range(10)]
        summary = summarise(turns)
        assert "point number 0" in summary        # why they rang survives
        assert len(summary) < 400


# ── booking ──────────────────────────────────────────────────────────────────
class TestBooking:
    def store(self) -> tuple[Store, str]:
        store = Store(Path(tempfile.mkdtemp()) / "b.db")
        agent = store.create_agent(name="Reception", business="Northgate Dental")
        return store, agent["id"]

    def test_an_appointment_is_persisted(self):
        store, agent_id = self.store()
        record = store.book(agent_id, "2026-03-03T10:00", patient_name="Sam Hassan",
                            phone="(212) 555-0142", reason="check-up")
        assert record is not None
        assert record["reference"].startswith("NG")

        stored = store.list_appointments()
        assert len(stored) == 1
        assert stored[0]["patient_name"] == "Sam Hassan"

    def test_two_callers_cannot_have_the_same_slot(self):
        """The guarantee the whole booking rests on. Both calls can find it free; only one
        insert can win, and the loser is told rather than silently double-booked."""
        store, agent_id = self.store()
        first = store.book(agent_id, "2026-03-03T10:00", patient_name="Sam")
        second = store.book(agent_id, "2026-03-03T10:00", patient_name="Alex")
        assert first is not None
        assert second is None
        assert len(store.list_appointments()) == 1

    def test_a_booked_slot_leaves_the_calendar(self):
        store, agent_id = self.store()
        free = available(store.taken_slots(), today=MONDAY, now=NINE_AM)
        target = free[0]

        store.book(agent_id, target.iso, patient_name="Sam")
        after = available(store.taken_slots(), today=MONDAY, now=NINE_AM)
        assert target.iso not in {s.iso for s in after}

    def test_cancelling_gives_the_slot_back(self):
        store, agent_id = self.store()
        record = store.book(agent_id, "2026-03-03T10:00", patient_name="Sam")
        assert "2026-03-03T10:00" in store.taken_slots()

        store.cancel_appointment(record["id"])
        assert "2026-03-03T10:00" not in store.taken_slots()

    def test_the_slot_the_caller_meant(self):
        free = available(set(), today=MONDAY, now=NINE_AM)
        when = parse_when("tomorrow", MONDAY).merge(parse_when("at ten", MONDAY))
        slot = match_slot(when, free)
        assert slot is not None
        assert slot.start == datetime(2026, 3, 3, 10, 0)

    def test_a_time_that_is_not_free_matches_nothing(self):
        when = parse_when("tomorrow at ten", MONDAY)
        free = [s for s in available(set(), today=MONDAY, now=NINE_AM)
                if s.start != datetime(2026, 3, 3, 10, 0)]
        assert match_slot(when, free) is None


class TestConfirmation:
    """Whether the caller actually said yes. Getting this wrong books the wrong thing."""

    @pytest.mark.parametrize("phrase", [
        "yes please", "yeah that works", "perfect", "go ahead", "book it", "sounds good",
        "ok", "that's great",
    ])
    def test_agreement(self, phrase: str):
        from dialtone.brain.conversation import _confirms
        assert _confirms(phrase)

    @pytest.mark.parametrize("phrase", [
        "no thanks", "not that one", "actually can we do friday", "wait",
        "yes but not thursday",          # contains a yes and is not one
        "yes, actually can we change it",
        "hold on",
    ])
    def test_refusal(self, phrase: str):
        from dialtone.brain.conversation import _confirms
        assert not _confirms(phrase)


class TestWhatTheModelIsTold:
    """The scheduling note. Everything the agent believes about times comes from this string,
    so a wrong sentence here is a wrong sentence said out loud to a caller."""

    def conversation(self, taken: set[str] | None = None):
        from dialtone.brain.conversation import AgentConfig, Conversation
        from dialtone.brain.llm import ScriptedBrain

        class Book:
            def __init__(self, taken: set[str]) -> None:
                self._taken = taken
                self.written: list[tuple[str, dict[str, Any]]] = []

            def taken_slots(self) -> set[str]:
                return self._taken

            def book(self, starts_at: str, **fields: Any) -> dict[str, Any]:
                self.written.append((starts_at, fields))
                return {"reference": "NGTEST01", "starts_at": starts_at,
                        "patient_name": fields.get("patient_name", ""),
                        "reason": fields.get("reason", "")}

        book = Book(taken or set())
        convo = Conversation(
            brain=ScriptedBrain(), config=AgentConfig(), booking=book, today=MONDAY,
        )
        return convo, book

    def test_a_free_morning_is_not_reported_as_full(self):
        """THE BUG FROM THE LAST LIVE CALL, and the worst kind: the agent contradicted its own
        diary. The caller asked for tomorrow morning, tomorrow morning was free, and the agent
        said "we don't have available appointments for tomorrow morning" -- because the prompt
        told it so whenever the caller had not yet named an hour, which is most of the time."""
        convo, _ = self.conversation()
        convo.memory.observe("are you free tomorrow morning")
        note = convo._scheduling_note()

        assert "IS available" in note
        assert "NOT free" not in note
        assert "Never tell them there is nothing free" in note

    def test_a_full_morning_is_reported_as_full(self):
        """The other direction, which is what makes the test above worth having: saying yes to
        everything is not a fix, it is the same bug pointing the other way."""
        tuesday_morning = {
            s.iso for s in day_slots(date(2026, 3, 3)) if s.start.hour < 12
        }
        convo, _ = self.conversation(taken=tuesday_morning)
        convo.memory.observe("are you free tomorrow morning")
        note = convo._scheduling_note()

        assert "NOT free" in note
        assert "IS available" not in note

    def test_a_time_that_does_not_exist_is_refused_even_on_a_free_day(self):
        """A caller asked for eight o'clock at a practice that opens at half past. The day was
        free, so the prompt said "what they asked for IS available" and the agent offered eight
        -- a time that has never existed. The hour is the answer when they named one."""
        convo, _ = self.conversation()
        convo.memory.observe("can I come tomorrow")
        convo.memory.observe("how about 8")
        note = convo._scheduling_note()

        assert "NOT free" in note
        assert "IS available" not in note
        assert not convo.memory.proposed_slot

    def test_the_flow_never_asks_for_a_name(self):
        """An objective is the strongest instruction the model gets. While one of them said
        "Get the caller's full name for the booking", no rule elsewhere in the prompt could stop
        it asking -- and it asked on every call."""
        from dialtone.agents.support import build_flow

        for node in build_flow().nodes.values():
            lowered = node.objective.lower()
            assert "full name" not in lowered
            assert not (("get" in lowered or "ask" in lowered) and "phone number" in lowered
                        and "do not" not in lowered)

    def test_it_is_told_never_to_ask_for_details(self):
        """A real call: "Could you please provide me with your name and phone number so I can
        assist you further?" -- then it collected both and had nowhere to put them. The screen
        collects those; the agent's job is the time."""
        convo, _ = self.conversation()
        note = convo._scheduling_note()
        assert "NEVER ask for a name, a phone number" in note
        assert "YOUR ONLY JOB RIGHT NOW is to agree a time" in note

    def test_the_date_is_stated_so_plainly_it_cannot_be_reasoned_away(self):
        """Given only "Today is Sunday 23 August", the model replied "today is already
        Saturday". It is not asked to work anything out any more."""
        convo, _ = self.conversation()
        note = convo._scheduling_note()
        assert "TODAY is Monday 02 March 2026" in note
        assert "TOMORROW is Tuesday 03 March" in note

    def test_a_named_time_that_is_free_is_put_on_the_table(self):
        convo, _ = self.conversation()
        convo.memory.observe("can I come tomorrow")
        convo.memory.observe("how about ten")
        note = convo._scheduling_note()

        assert "IS free" in note
        assert convo.memory.proposed_slot == "tomorrow at ten in the morning"

    def test_only_real_slots_are_ever_offered(self):
        """The list in the prompt is the list the calendar returned, filtered to nothing else.
        A model cannot offer a time it was never shown."""
        convo, _ = self.conversation()
        convo.memory.observe("sometime thursday afternoon")
        note = convo._scheduling_note()

        offered = [line for line in note.splitlines() if line.startswith("  ")][0]
        assert "Thursday" in offered
        assert "in the afternoon" in offered

    def test_a_full_diary_says_so_rather_than_inventing_one(self):
        every_slot = {
            s.iso
            for offset in range(20)
            for s in day_slots(MONDAY + timedelta(days=offset))
        }
        convo, _ = self.conversation(taken=every_slot)
        assert "Nothing is free" in convo._scheduling_note()

    def test_booking_writes_the_typed_values_not_the_heard_ones(self):
        """The whole reason the form exists, checked at the only point it matters: what actually
        reaches the database."""
        convo, book = self.conversation()
        convo.memory.observe("my name is Tasty Mulasson, I need a cleaning")
        convo.memory.observe("tomorrow at ten")
        convo._scheduling_note()                      # puts the slot on the table

        convo.memory.tell("name", "Tasnimul Hasan")
        convo.memory.tell("phone", "(212) 555-0142")
        convo.memory.tell("email", "tasnimul@example.com")
        convo.memory.slot_confirmed = True

        booked = convo.book_if_ready()
        assert booked is not None
        assert booked["reference"] == "NGTEST01"

        starts_at, fields = book.written[0]
        assert starts_at == "2026-03-03T10:00"
        assert fields["patient_name"] == "Tasnimul Hasan"
        assert fields["phone"] == "(212) 555-0142"
        assert fields["reason"] == "cleaning"

    def test_booking_happens_once(self):
        """A caller who says "yes, great, thanks" three times has agreed once."""
        convo, book = self.conversation()
        convo.memory.observe("my name is Sam Hassan, I need a cleaning")
        convo.memory.observe("tomorrow at ten")
        convo._scheduling_note()
        for name, value in [("name", "Sam Hassan"), ("phone", "(212) 555-0142"),
                            ("email", "sam@example.com")]:
            convo.memory.tell(name, value)
        convo.memory.slot_confirmed = True

        assert convo.book_if_ready() is not None
        assert convo.book_if_ready() is None
        assert len(book.written) == 1
