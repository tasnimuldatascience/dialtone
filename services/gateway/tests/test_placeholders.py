"""Replies that are a form rather than an answer.

FOUND ON A THIRTY-TURN CALL. The caller asked "where are you exactly?" and the agent said:

    "Northgate Dental is located at [insert location], where we provide..."

The knowledge base had no address in it, so the model did what a model does with a gap in a form
— it wrote the shape of the answer. Read out loud, bracket by bracket, that is the single most
obviously broken thing this system can say.

The grounding check cannot catch it. That verifies NUMBERS against the passages the model was
given, and a placeholder has no number in it. So it is caught by shape instead, and the reply is
replaced whole rather than patched: "Northgate Dental is located at" with the brackets cut out is
a sentence that stops mid-thought, which is not an improvement.
"""

from __future__ import annotations

import pytest

from dialtone.brain.conversation import find_placeholder

CAUGHT = [
    "Northgate Dental is located at [insert location], where we provide care.",
    "We are at {address}.",
    "Please call TBD to confirm.",
    "Your appointment is at XXX.",
    "Enter your name here to continue.",
    "The reference is [reference number].",
    "You can reach us on {phone}.",
    "Availability: TBC",
    "Our address is N/A at the moment.",
]

LEFT_ALONE = [
    "A check-up is $75 and takes about an hour.",
    "We are open Monday to Friday, eight thirty until six.",
    "That is between one hundred twenty and one hundred eighty dollars.",
    "Tomorrow at nine thirty in the morning works. Shall I book it?",
    "Your reference is NG5EA086.",
    "We're at 118 Northgate Avenue, on the first floor.",
    "I can do Thursday at two, or Friday at nine thirty.",
    # A perfectly ordinary sentence that happens to contain a capital word run.
    "We accept PPO and HMO plans.",
    "Call the emergency line on 212-555-0188.",
]


@pytest.mark.parametrize("reply", CAUGHT)
def test_a_placeholder_is_caught(reply: str):
    assert find_placeholder(reply), f"missed a placeholder in {reply!r}"


@pytest.mark.parametrize("reply", LEFT_ALONE)
def test_a_real_answer_is_not_touched(reply: str):
    """The half that stops this being a nuisance. A guard that fires on ordinary sentences would
    replace good answers with "I don't have that to hand", which is worse than the bug."""
    assert not find_placeholder(reply), f"false positive on {reply!r}"


def test_the_practice_can_actually_answer_where_it_is():
    """The other half of the fix. A guard stops the caller HEARING a placeholder; it does not
    stop the agent being unable to answer. The knowledge base needed an address in it."""
    from dialtone.platform import SEED_DOCUMENTS

    everything = " ".join(SEED_DOCUMENTS.values()).lower()
    assert "avenue" in everything or "street" in everything, "no address anywhere"
    assert "parking" in everything
    assert "subway" in everything or "bus" in everything
