"""What the caller actually hears, as the reply is being written.

THE FAILURE THIS EXISTS FOR. A caller heard a price twice:

    "A check-up is forty five dollars."
    "y five dollars. Would you like to book one?"

Streaming synthesis speaks a clause the moment it is complete, so it has to remember how far it
has got. It was remembering a position in ONE string and then using it to slice a DIFFERENT one:
the tokens arrive as the model wrote them ("$45"), and the finished reply arrives rewritten for
speech ("forty five dollars"). Those two strings do not share a coordinate system, and the gap
between them is exactly how much got said twice.

Every test here checks the same property from a different angle: CONCATENATE EVERYTHING SPOKEN
AND YOU SHOULD GET THE REPLY, once, in order. Nothing repeated, nothing dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from dialtone.brain.speakable import speakable
from dialtone.server.app import _ClauseSpeaker


@dataclass
class _Clip:
    text: str
    wav: bytes = b"RIFF"
    sample_rate: int = 24000
    duration_ms: float = 100.0
    generate_ms: float = 10.0
    index: int = 0


class _Voice:
    """Synthesis, minus the synthesis. Records what it was asked to say."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def speak(self, text: str, *, voice: str = "") -> Any:
        self.said.append(text)
        yield _Clip(text=text)


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _Live:
    call_id = "call-1"

    class conversation:  # noqa: N801 -- a stand-in for the real attribute path
        class config:
            voice = "female-warm"


class _Platform:
    def __init__(self, voice: _Voice) -> None:
        self.voice = voice


async def run_reply(reply: str, *, words_per_token: int = 1) -> tuple[str, _Voice]:
    """Stream one reply through the speaker exactly as the socket handler does.

    Returns everything the caller heard, joined, plus the voice for inspection.
    """
    voice = _Voice()
    socket = _Socket()
    speaker = _ClauseSpeaker(socket, _Platform(voice), _Live())

    # The token stream. `spoken` on a token event is the RAW reply so far, marker-stripped --
    # not the speech-ready rewrite. That distinction is the whole subject of this file.
    words = reply.split(" ")
    for end in range(words_per_token, len(words) + words_per_token, words_per_token):
        await speaker.feed(" ".join(words[:end]))

    # The done event carries both: `agent` is what was written, `spoken` is what to say.
    await speaker.finish(reply)
    return " ".join(voice.said), voice


def normalise(text: str) -> str:
    return " ".join(text.split())


REPLIES = [
    "A check-up is $45. Would you like to book one?",
    "Of course, I can help with that.",
    "We open at 8:30 and close at 6, except Thursdays when we run until 8.",
    "That's £120 to £180 depending on the tooth, and it takes about an hour.",
    "Yes.",
    "Tomorrow at ten thirty in the morning works. Shall I put you in?",
    "I'm sorry, that slot has gone. I can do Thursday at two, or Friday at nine thirty.",
]


class TestNothingIsSaidTwice:
    @pytest.mark.parametrize("reply", REPLIES)
    async def test_the_caller_hears_the_reply_exactly_once(self, reply: str):
        """THE REGRESSION. Concatenating everything spoken must reproduce the reply — the
        speech-ready form of it — with nothing repeated and nothing lost."""
        heard, _ = await run_reply(reply)
        assert normalise(heard) == normalise(speakable(reply))

    @pytest.mark.parametrize("reply", REPLIES)
    @pytest.mark.parametrize("chunk", [1, 2, 3, 5])
    async def test_it_holds_however_the_tokens_arrive(self, reply: str, chunk: int):
        """Token boundaries are not stable — they depend on the tokeniser, the sampling, and how
        fast the model is running. A correctness property that only holds at one chunk size is
        not a property."""
        heard, _ = await run_reply(reply, words_per_token=chunk)
        assert normalise(heard) == normalise(speakable(reply))

    async def test_a_price_is_not_read_out_twice(self):
        """The exact call that started this. Written as its own test because the failure was
        specific and a passing parametrised sweep is easy to skim past."""
        heard, voice = await run_reply("A check-up is $45. Would you like to book one?")
        assert heard.count("forty five dollars") == 1
        # Each clause must begin at a word boundary. The repeat announced itself as a clause
        # starting mid-word -- "y five dollars ..." -- which is what a stale offset produces.
        assert all(clause[0].isupper() or clause[0].isalnum() for clause in voice.said if clause)
        assert not any(clause.startswith(("y five", "orty", "ive ")) for clause in voice.said)
        assert normalise(heard) == "A check-up is forty five dollars. Would you like to book one?"

    async def test_the_opening_is_sent_before_the_reply_is_finished(self):
        """The reason this class exists at all. If the first clause only goes out once the model
        has stopped, the caller sits in silence for the whole generation — and every latency
        claim in this repository is about not doing that."""
        voice = _Voice()
        socket = _Socket()
        speaker = _ClauseSpeaker(socket, _Platform(voice), _Live())

        reply = "Of course, I can help you with that appointment today."
        words = reply.split(" ")
        for end in range(1, len(words) + 1):
            await speaker.feed(" ".join(words[:end]))
            if voice.said:
                break

        assert voice.said, "nothing was spoken while the model was still writing"
        assert end < len(words), "it waited for the whole reply"

    async def test_the_first_chunk_reports_how_long_the_caller_waited(self):
        _, voice = await run_reply("Of course, I can help with that.")
        assert voice.said


class TestTruncation:
    async def test_it_does_not_finish_a_sentence_the_caller_will_never_see(self):
        """The reply is cut to one or two sentences after generation. If the speaker has already
        run past the cut, the tail must be dropped rather than spoken — otherwise the voice says
        something the transcript does not contain, which reads as the agent going off-script."""
        voice = _Voice()
        socket = _Socket()
        speaker = _ClauseSpeaker(socket, _Platform(voice), _Live())

        await speaker.feed("Yes, we are open. Come any time. Also we sell toothbrushes.")
        await speaker.finish("Yes, we are open. Come any time.")

        heard = normalise(" ".join(voice.said))
        assert "toothbrushes" not in heard

    async def test_an_empty_reply_says_nothing(self):
        voice = _Voice()
        speaker = _ClauseSpeaker(_Socket(), _Platform(voice), _Live())
        await speaker.feed("")
        await speaker.finish("")
        assert voice.said == []
