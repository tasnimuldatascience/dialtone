"""How the caller sounded, judged from their own words.

WHAT THIS IS FOR, which sets how accurate it needs to be. An operator uses it to FILTER — "show
me the unhappy calls" — so cheap and roughly right beats expensive and slightly better. It is
word lists, not a model, because a classifier on every call teardown is a real cost for a number
nobody makes a decision on alone.

THE FAILURE THAT ADDED THIS FILE. A call reading

    caller:  when are you open this week?
    caller:  sorry, say that again?

was listed as NEGATIVE. "Never" and "again" had both been added to the negative list -- they
arrived as the phrase "never again", and the list is split on whitespace, so each became an
independent cue. "Say that again" is one of the commonest things anyone says on a phone line.

A false positive here is the expensive direction: it puts a perfectly ordinary call in front of
whoever is reviewing complaints, and after the second one they stop trusting the filter.
"""

from __future__ import annotations

import pytest

from dialtone.brain.conversation import StageTiming, TurnRecord
from dialtone.platform import _sentiment


class FakeCall:
    def __init__(self, lines: list[str]) -> None:
        self.turns = [
            TurnRecord(caller=line, agent="", spoken="", timing=StageTiming())
            for line in lines
        ]


def mood(*lines: str) -> str:
    return _sentiment(FakeCall(list(lines)))


class TestOrdinaryCallsAreNotComplaints:
    """The expensive direction, and every case here was a real false positive or nearly one."""

    @pytest.mark.parametrize("line", [
        "sorry, say that again?",
        "can you say that again please",
        "could you repeat that again",
        "I have never been here before",
        "I never got the letter",
        "what time again?",
    ])
    def test_it_stays_neutral(self, line: str):
        assert mood(line) == "neutral"

    def test_the_call_that_started_this(self):
        assert mood("when are you open this week?", "sorry, say that again?") == "neutral"

    def test_an_ordinary_enquiry(self):
        assert mood("how much is a check-up?", "and how long does it take?") == "neutral"


class TestRealComplaints:
    """The other direction, which is what makes the tests above worth having: a filter that never
    fires is not a cautious filter, it is a broken one."""

    @pytest.mark.parametrize("line", [
        "this is ridiculous, I want a refund",
        "that is completely unacceptable",
        "your receptionist was rude",
        "what a waste of time",
        "I am never coming here again",
        "never again",
        "I want to speak to a manager",
        "I am fed up with this",
    ])
    def test_it_is_caught(self, line: str):
        assert mood(line) == "negative"

    def test_never_and_again_only_count_together(self):
        """The distinction the original list could not make. Both words are ordinary alone; the
        complaint is in the combination, and it is usually spread across a clause."""
        assert mood("I have never used this before") == "neutral"
        assert mood("say that again") == "neutral"
        assert mood("I am never using this again") == "negative"

    def test_it_does_not_reach_across_sentences(self):
        """Bounded to one clause, so "never" in one sentence and "again" three sentences later
        is not a complaint."""
        assert mood("I have never been. Anyway, could you say that again?") == "neutral"


class TestHappyCalls:
    @pytest.mark.parametrize("line", [
        "thanks, that is perfect",
        "brilliant, thank you",
        "that is really helpful, I appreciate it",
    ])
    def test_it_is_caught(self, line: str):
        assert mood(line) == "positive"


def test_only_the_caller_is_counted():
    """The agent is unfailingly polite by construction, so counting its words would score every
    call positive and the column would carry no information at all."""
    call = FakeCall(["this is ridiculous"])
    call.turns[0].agent = "Thank you so much, that is wonderful, brilliant, perfect, lovely."
    assert _sentiment(call) == "negative"


def test_a_call_with_nothing_said():
    assert mood() == "neutral"
    assert mood("") == "neutral"
