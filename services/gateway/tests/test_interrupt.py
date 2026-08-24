"""What the agent believes it said, after being talked over.

THE FAILURE. The agent WRITES a whole sentence; the caller only HEARS the part that played before
they cut in:

    written:  "I've got Tuesday at nine, Tuesday at ten thirty, Wednesday at noon, and Friday at
               four."
    heard:    "I've got Tuesday at..."

Leave the written version in the history and the model now believes it offered four appointment
times. Two turns later it says "as I mentioned, Wednesday at noon" — and the caller has no idea
what it is talking about. That second failure is worse than the interruption, because it is the
moment the caller stops believing the agent was listening at all.
"""

from __future__ import annotations

import pytest

from dialtone.brain.conversation import AgentConfig, Conversation
from dialtone.brain.llm import ScriptedBrain, Turn

OFFER = "I've got Tuesday at nine, Tuesday at ten thirty, Wednesday at noon, and Friday at four."


def conversation(*, said: str = OFFER) -> Conversation:
    convo = Conversation(brain=ScriptedBrain(), config=AgentConfig())
    convo.history.append(Turn("user", "when are you free?"))
    convo.history.append(Turn("assistant", said))
    return convo


class TestKeepingOnlyWhatWasHeard:
    def test_the_history_keeps_what_played(self):
        convo = conversation()
        assert convo.interrupted("I've got Tuesday at")

        kept = convo.history[-1].content
        assert kept.startswith("I've got Tuesday at")
        assert "Wednesday" not in kept
        assert "Friday" not in kept

    def test_it_is_marked_as_cut_off(self):
        """So the model can see its own sentence was interrupted rather than concluding it
        phrased something strangely and trying again."""
        convo = conversation()
        convo.interrupted("I've got Tuesday at")
        assert convo.history[-1].content.endswith("…")

    def test_letting_it_finish_changes_nothing(self):
        """A caller who speaks the instant the reply ends has not interrupted anything, and
        trimming there would tell the model it said less than it did."""
        convo = conversation()
        assert not convo.interrupted(OFFER)
        assert convo.history[-1].content == OFFER

    def test_cutting_in_before_a_word_came_out_leaves_nothing(self):
        convo = conversation()
        assert convo.interrupted("")
        assert convo.history[-1].content == "…"

    def test_it_finds_the_reply_even_after_the_caller_has_spoken(self):
        """The interruption arrives over the socket, and the caller's own turn may already have
        been recorded by then. Trimming the wrong entry would edit the caller's words."""
        convo = conversation()
        convo.history.append(Turn("user", "no, not that one"))
        convo.interrupted("I've got Tuesday at")

        assert convo.history[-1].content == "no, not that one"
        assert convo.history[-2].content.startswith("I've got Tuesday at")

    def test_nothing_to_interrupt_is_not_an_error(self):
        """A stray message, or an interruption during the greeting before anything was said."""
        convo = Conversation(brain=ScriptedBrain(), config=AgentConfig())
        assert not convo.interrupted("anything")

    @pytest.mark.parametrize("heard", ["I've got", "I've got Tuesday at nine,", "I've"])
    def test_it_never_keeps_more_than_was_heard(self, heard: str):
        convo = conversation()
        convo.interrupted(heard)
        kept = convo.history[-1].content.rstrip("…")
        assert len(kept) <= len(OFFER)
        assert OFFER.startswith(kept.strip()) or kept.strip() == heard.strip()


class TestTheCallRecord:
    def test_an_interrupted_turn_says_so(self):
        """An operator reviewing the call needs to see that the caller cut in — otherwise the
        transcript shows a reply that reads as inexplicably short."""
        from dialtone.brain.conversation import StageTiming, TurnRecord

        convo = conversation()
        convo.turns.append(TurnRecord(caller="when are you free?", agent=OFFER,
                                      spoken=OFFER, timing=StageTiming()))
        convo.interrupted("I've got Tuesday at")

        record = convo.turns[-1]
        assert record.interrupted
        assert record.heard == "I've got Tuesday at"
        # The transcript keeps the full text: it is what the agent said, and the record is a
        # record. `heard` is the separate fact.
        assert record.agent == OFFER
        assert record.as_dict()["interrupted"] is True
