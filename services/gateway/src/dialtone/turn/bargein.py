"""Barge-in: the caller interrupts while the agent is speaking.

THE BUG ALMOST EVERY IMPLEMENTATION HAS. When a caller interrupts, the obvious handling is:
stop the audio, and append the agent's utterance to the conversation history. That is wrong,
and it is wrong in a way that produces bizarre behaviour two turns later.

The agent GENERATED the whole sentence. The caller only HEARD the part that was played before
they cut in. If the full sentence goes into history, the model believes it said things the
caller never heard:

    agent generates:  "Sure — I can see three appointments available. Tuesday at 2pm,
                       Wednesday at 10am, or Friday at 4pm. Which suits you?"
    caller hears:     "Sure — I can see three appointments avail—"
    caller says:      "wait, what about next week?"

With the full text in history, the agent's next turn is built on the belief that it already
offered three specific slots. It will say "as I mentioned, Tuesday at 2pm..." — and the caller
has no idea what it is talking about. The conversation degrades from there and nobody can tell
why, because every individual component behaved correctly.

So this module tracks PLAYED AUDIO, not generated text, and truncates the history to what was
actually heard. `truncate_to_played` is the whole point of the file.

THE SECOND PROBLEM: FALSE BARGE-IN. A cough, a door, a "mm-hmm", or the agent's own voice
leaking through a speakerphone all look like the caller speaking. Cutting the agent off on any
of them makes it impossible to deliver a sentence. So an interruption must clear three bars:

  ENERGY      loud enough to be speech, not background
  DURATION    sustained, not a click or a cough
  INTENT      not a pure backchannel ("mm-hmm", "okay", "right"), which means "keep going",
              not "stop"

Getting the third one wrong is why some agents stop dead every time the caller says "uh huh".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Sustained speech required before an interruption is believed. Below ~120ms almost everything
#: is a transient; above ~400ms a genuine interruption feels ignored.
MIN_INTERRUPT_MS = 220.0

#: Energy relative to the session's noise floor. A ratio rather than an absolute so it works on
#: a quiet headset and a noisy car alike.
INTERRUPT_ENERGY_RATIO = 3.0

#: Words that mean "I am listening", not "stop talking". Cutting the agent off on these is the
#: most common false barge-in and it makes long answers impossible to deliver.
BACKCHANNELS = frozenset("""
mm mmm hmm mhm mm-hmm mmhmm mhm-hmm uh-huh uhhuh huh-uh yeah yep yup yes ok okay
right sure gotcha i-see isee got-it go-on continue
""".split())

#: Words that mean stop, immediately, whatever else is true. These bypass the duration check --
#: a caller saying "stop" must not wait 220ms to be obeyed.
HARD_INTERRUPTS = frozenset("""
stop wait hold no nope hang hang-on hangon actually sorry excuse
""".split())


class BargeDecision(StrEnum):
    IGNORE = "ignore"                # transient or background
    BACKCHANNEL = "backchannel"      # caller is agreeing; keep talking
    INTERRUPT = "interrupt"          # stop now


@dataclass(slots=True)
class SpeechFrame:
    """One VAD frame from the caller's channel."""

    energy: float
    is_speech: bool
    duration_ms: float = 20.0


@dataclass(slots=True)
class Utterance:
    """What the agent is currently saying.

    `played_ms` is maintained by the audio sink as frames are actually written to the caller,
    NOT as they are synthesised. The gap between the two is exactly the mistake this module
    exists to prevent — a TTS engine can be several seconds ahead of the speaker.
    """

    text: str
    total_ms: float
    played_ms: float = 0.0
    #: Word boundaries in milliseconds from the start, if the TTS engine reports them.
    #: Without them the truncation falls back to proportional estimation, which is coarser but
    #: still far better than keeping the whole sentence.
    word_timings: list[tuple[str, float]] = field(default_factory=list)

    @property
    def fraction_played(self) -> float:
        return min(1.0, self.played_ms / self.total_ms) if self.total_ms > 0 else 0.0

    @property
    def finished(self) -> bool:
        return self.played_ms >= self.total_ms


@dataclass(slots=True)
class BargeIn:
    decision: BargeDecision
    reason: str
    #: What the caller actually heard. Only meaningful on INTERRUPT.
    heard_text: str = ""
    fraction_played: float = 0.0


def truncate_to_played(utterance: Utterance) -> str:
    """The text the caller actually heard.

    Word timings when the engine provides them; proportional truncation on a word boundary
    otherwise. Never mid-word: a history containing "three appointments avail" is confusing to
    the model in a different way, so the cut lands on the last fully-spoken word.

    An ellipsis is appended so the model can SEE it was cut off. That matters — an agent that
    knows it was interrupted mid-sentence behaves correctly (it re-offers the information);
    one that thinks it finished does not.
    """
    if utterance.finished:
        return utterance.text
    if utterance.played_ms <= 0:
        return ""

    if utterance.word_timings:
        heard = [w for w, at in utterance.word_timings if at <= utterance.played_ms]
        text = " ".join(heard)
    else:
        words = utterance.text.split()
        cut = int(len(words) * utterance.fraction_played)
        text = " ".join(words[:cut])

    text = text.strip()
    if not text:
        return ""
    return text if text.endswith("…") else text + "…"


@dataclass(slots=True)
class BargeConfig:
    min_interrupt_ms: float = MIN_INTERRUPT_MS
    energy_ratio: float = INTERRUPT_ENERGY_RATIO
    #: Below this fraction played, an interruption is likelier to be the caller's previous turn
    #: bleeding in or an echo of the agent's own first syllable than a real interruption.
    min_fraction_before_interrupt: float = 0.0
    allow_backchannel: bool = True
    #: Grace window after the agent starts speaking, during which echo from the caller's
    #: speakerphone is most likely. Real interruptions in the first 150ms are rare; echo is not.
    echo_guard_ms: float = 150.0


class BargeInDetector:
    """Decides whether caller speech during agent audio is an interruption."""

    def __init__(self, config: BargeConfig | None = None):
        self.config = config or BargeConfig()
        self.noise_floor = 0.01
        self._speech_ms = 0.0

    def observe_noise(self, frame: SpeechFrame) -> None:
        """Track the ambient floor while nobody is speaking.

        A slow exponential average: it must adapt to a caller moving into a noisy room without
        chasing the caller's own voice, which would raise the floor until interruptions stop
        registering at all.
        """
        if not frame.is_speech:
            self.noise_floor = 0.97 * self.noise_floor + 0.03 * max(frame.energy, 1e-6)

    def reset(self) -> None:
        """Call at the start of each agent utterance."""
        self._speech_ms = 0.0

    def evaluate(
        self, frame: SpeechFrame, utterance: Utterance, partial_transcript: str = ""
    ) -> BargeIn:
        cfg = self.config

        if not frame.is_speech:
            self._speech_ms = 0.0
            self.observe_noise(frame)
            return BargeIn(BargeDecision.IGNORE, "no speech in frame",
                           fraction_played=utterance.fraction_played)

        self._speech_ms += frame.duration_ms
        loud_enough = frame.energy >= self.noise_floor * cfg.energy_ratio

        if not loud_enough:
            return BargeIn(
                BargeDecision.IGNORE,
                f"energy {frame.energy:.4f} below {cfg.energy_ratio:.1f}x noise floor "
                f"{self.noise_floor:.4f}",
                fraction_played=utterance.fraction_played,
            )

        words = re.findall(r"[\w'-]+", partial_transcript.lower())
        last = words[-1] if words else ""

        # Hard interrupts bypass the duration gate. Making someone say "stop" twice is the
        # worst possible behaviour for a voice agent.
        if last in HARD_INTERRUPTS or (words and words[0] in HARD_INTERRUPTS):
            return BargeIn(
                BargeDecision.INTERRUPT,
                f"explicit interrupt {last or words[0]!r}",
                heard_text=truncate_to_played(utterance),
                fraction_played=utterance.fraction_played,
            )

        if utterance.played_ms < cfg.echo_guard_ms:
            return BargeIn(
                BargeDecision.IGNORE,
                f"within {cfg.echo_guard_ms:.0f}ms echo guard — likely the agent's own audio",
                fraction_played=utterance.fraction_played,
            )

        # Backchannel is checked BEFORE the duration gate, because it is a claim about
        # CONTENT, not duration -- "mm-hmm" is agreement whether it lasted 80ms or 800ms.
        # Gating it behind the duration check classified a short one as a transient and a
        # long one as an interruption, which is exactly backwards.
        if cfg.allow_backchannel and words and all(w in BACKCHANNELS for w in words):
            return BargeIn(
                BargeDecision.BACKCHANNEL,
                f"{partial_transcript.strip()!r} is agreement, not an interruption",
                fraction_played=utterance.fraction_played,
            )

        if self._speech_ms < cfg.min_interrupt_ms:
            return BargeIn(
                BargeDecision.IGNORE,
                f"{self._speech_ms:.0f}ms < {cfg.min_interrupt_ms:.0f}ms — transient",
                fraction_played=utterance.fraction_played,
            )

        if utterance.fraction_played < cfg.min_fraction_before_interrupt:
            return BargeIn(
                BargeDecision.IGNORE,
                f"only {utterance.fraction_played:.0%} played — below the interrupt floor",
                fraction_played=utterance.fraction_played,
            )

        return BargeIn(
            BargeDecision.INTERRUPT,
            f"{self._speech_ms:.0f}ms of speech at "
            f"{frame.energy / max(self.noise_floor, 1e-9):.1f}x noise floor",
            heard_text=truncate_to_played(utterance),
            fraction_played=utterance.fraction_played,
        )
