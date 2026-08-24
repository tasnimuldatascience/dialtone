"""Deciding whether the documents have anything to say.

THE MEASUREMENT THAT SHAPED THIS. Cosine scores against the seed corpus, bge-small-en-v1.5:

    "how much is a check-up?"        0.735   in scope
    "do you offer physiotherapy?"    0.582   OUT of scope
    "hi how are you doing"           0.527   not a question at all
    "what is your street address?"   0.520   in scope
    "where are you exactly?"         0.457   in scope

Read that ordering carefully, because three things follow from it and each one is a design
decision in this module:

  NO THRESHOLD SEPARATES SMALL TALK FROM REAL QUESTIONS. "Hi how are you doing" outscores a
  genuine question about the address. Raise the gate to exclude it and you silence the question;
  lower it and you hand the agent a page to answer "hello" from. So small talk is filtered
  LEXICALLY, before anything is embedded, and the gate never has to judge intent.

  NO THRESHOLD SEPARATES IN-SCOPE FROM OUT-OF-SCOPE EITHER. "Do you offer physiotherapy?" scores
  higher than "what is your street address?". That is a property of the embedding model, not of
  the number, and it is the reason the gate stays where it is rather than being loosened to catch
  the last few real questions.

  SO RETRIEVAL QUALITY IS MOSTLY A CONTENT PROBLEM. Rewriting the location document to use the
  words callers actually say -- "our address", "where we are", "how to get here" -- moved those
  queries from 0.42 to 0.59 without touching a single threshold. That is where the leverage is.
"""

from __future__ import annotations

import pytest

from dialtone.brain.knowledge import MIN_RELEVANCE, is_small_talk

SMALL_TALK = [
    "hi how are you doing",
    "hello",
    "good morning",
    "thanks very much",
    "great, thank you",
    "ok",
    "that sounds good",
    "yes that works",
    "no thanks",
    "perfect",
    "mm-hmm",
    "uh-huh",
    "sure, that is fine",
    "how are you",
    "bye",
    "um",
]

REAL_QUESTIONS = [
    "where are you exactly?",
    "how much is a check-up?",
    "are you open on saturdays?",
    "is there parking?",
    "do you take insurance?",
    "my tooth hurts",
    "can I book an appointment",
    "what are your opening hours",
    "how do I find you",
    "do you do whitening",
    "how late on thursdays",
    "what is your address",
    "how much is a filling",
    "can I come tomorrow morning",
    "what happens if I cancel",
    # Chatty, but there is a question inside it.
    "hi, sorry, could you tell me how much a cleaning is?",
    "ok great, and what time do you close?",
]


class TestSmallTalk:
    @pytest.mark.parametrize("query", SMALL_TALK)
    def test_it_is_recognised(self, query: str):
        assert is_small_talk(query), f"{query!r} should not reach retrieval"

    @pytest.mark.parametrize("query", REAL_QUESTIONS)
    def test_a_real_question_is_not_mistaken_for_it(self, query: str):
        """The expensive direction. Dropping a real question means the agent answers from
        nothing, which is how it came to say "located at [insert location]"."""
        assert not is_small_talk(query), f"{query!r} would be silently dropped"

    def test_an_empty_query_asks_nothing(self):
        assert is_small_talk("")
        assert is_small_talk("   ")
        assert is_small_talk("...")

    def test_no_word_that_names_something_is_in_the_list(self):
        """The list is only safe because it contains no domain vocabulary. One "open" or "price"
        in there and every question containing it is silently dropped."""
        from dialtone.brain.knowledge import _SMALL_TALK

        forbidden = {
            "open", "close", "closed", "price", "cost", "book", "booking", "appointment",
            "tooth", "teeth", "dental", "dentist", "clean", "cleaning", "filling", "crown",
            "parking", "address", "insurance", "emergency", "pain", "hurt", "cancel", "time",
            "when", "where", "why", "day", "week", "today", "tomorrow", "hour", "hours",
        }
        overlap = forbidden & _SMALL_TALK
        assert not overlap, f"domain words in the small-talk list: {sorted(overlap)}"


class TestTheGate:
    def test_it_is_where_the_measurements_put_it(self):
        """Not a round number, and not arbitrary. Documented in the module header with the
        scores it came from."""
        assert 0.50 <= MIN_RELEVANCE <= 0.60

    def test_small_talk_never_reaches_it(self):
        """The gate's job is judging PASSAGES. Intent is decided before it, because the two
        distributions overlap and no single number can do both."""
        assert all(is_small_talk(q) for q in SMALL_TALK)


class TestTheCorpusItself:
    """Because retrieval quality is mostly a content problem, the content gets tested too."""

    def test_no_two_documents_describe_the_same_thing(self):
        """A DRAFT OF THE LOCATION PAGE CAUSED THIS. It repeated the car park, the buses and the
        step-free entrance from "Parking and access" -- and got two of the three WRONG, naming a
        different street for the lot and different bus numbers.

        Contradictory documents are the worst thing a knowledge base can hold. Retrieval picks
        one, grounding verifies the answer against the one it picked, and the agent states a
        false fact with complete confidence and a citation to back it up.
        """
        from dialtone.platform import SEED_DOCUMENTS

        # Each topic should be owned by exactly one page.
        topics = {
            "the car park": ("parking", "car park"),
            "bus routes": ("bus", "buses"),
            "step-free access": ("step-free", "accessible", "wheelchair"),
        }
        for topic, cues in topics.items():
            owners = [
                title for title, body in SEED_DOCUMENTS.items()
                if any(cue in body.lower() for cue in cues)
            ]
            assert len(owners) <= 1, (
                f"{topic} is described in {len(owners)} documents ({', '.join(owners)}); "
                f"they can drift apart and one of them will be wrong"
            )

    def test_every_document_says_something(self):
        from dialtone.platform import SEED_DOCUMENTS

        for title, body in SEED_DOCUMENTS.items():
            assert len(body.split()) >= 20, f"{title!r} is too short to retrieve reliably"

    def test_the_practice_can_answer_the_obvious_questions(self):
        """Not retrieval scores -- just that the FACT is written down somewhere. An agent cannot
        retrieve what nobody wrote, and that gap shows up as a hallucination rather than as a
        miss."""
        from dialtone.platform import SEED_DOCUMENTS

        everything = " ".join(SEED_DOCUMENTS.values()).lower()
        for topic, cue in [
            ("where it is", "avenue"),
            ("opening hours", "eight thirty"),
            ("what things cost", "dollars"),
            ("parking", "parking"),
            ("cancellations", "cancel"),
            ("emergencies", "emergency"),
            ("insurance", "insurance"),
        ]:
            assert cue in everything, f"nothing in the corpus answers {topic!r}"
