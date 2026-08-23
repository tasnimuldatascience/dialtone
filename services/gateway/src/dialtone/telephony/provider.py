"""The telephony boundary, and a simulator that makes calls testable.

WHY A SIMULATOR IS THE MOST IMPORTANT FILE IN A VOICE PRODUCT. A voice agent is a distributed
system whose integration test costs money, takes 40 seconds, needs a human to talk to it, and
cannot be run in CI. So the failures that matter — the caller who talks over the greeting, the
one who goes silent for nine seconds, the packet loss burst that eats the account number — get
tested by hand, once, near a release, and then never again.

Everything below exists so those become ordinary unit tests. `SimulatedCall` replays a scripted
caller through the real orchestrator at real frame cadence with a virtual clock, so a test can
assert "when the caller pauses 400ms mid-number, the agent does not respond" in three
milliseconds of wall time, deterministically, on every commit.

THE PROVIDER INTERFACE IS DELIBERATELY TINY. Twilio, Telnyx, Vonage and a raw SIP trunk differ
enormously in their control planes and barely at all in what a voice agent needs: frames in,
frames out, a few call-control verbs. Keeping the interface to that means the simulator is a
peer of the real providers rather than a mock of one — the orchestrator genuinely cannot tell
them apart, which is the only way simulator results mean anything.

AUDIO FORMAT. Telephony is 8kHz μ-law, not 16kHz PCM, and this is a real source of bugs: a
model fed μ-law bytes as if they were PCM produces transcripts that are subtly wrong rather
than obviously broken. The codec conversion lives here, at the boundary, exactly once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ..turn.bargein import SpeechFrame

log = logging.getLogger("dialtone.telephony")

#: Telephony standard. Not a choice — it is what the PSTN delivers.
SAMPLE_RATE_HZ = 8_000
#: 20ms at 8kHz. Every provider uses this; it is also the granularity at which the endpointer
#: can possibly react, which is why the whole pipeline is built around it.
FRAME_MS = 20.0
FRAME_BYTES = int(SAMPLE_RATE_HZ * FRAME_MS / 1000)  # μ-law is 1 byte/sample


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    RINGING = "ringing"
    ANSWERED = "answered"
    IN_PROGRESS = "in_progress"
    TRANSFERRING = "transferring"
    COMPLETED = "completed"
    FAILED = "failed"
    #: The caller hung up. Distinct from COMPLETED because it is the single most common way a
    #: call ends and it means something different for the transcript: the agent did not finish.
    ABANDONED = "abandoned"


@dataclass(slots=True)
class CallInfo:
    call_id: str
    from_number: str
    to_number: str
    direction: CallDirection = CallDirection.INBOUND
    status: CallStatus = CallStatus.RINGING
    #: Provider-specific payload, kept opaque on purpose. The moment the orchestrator reads a
    #: Twilio-shaped field out of here the abstraction has failed.
    metadata: dict[str, Any] = field(default_factory=dict)


class TelephonyProvider(Protocol):
    """What a voice agent actually needs from a carrier. Nothing more."""

    async def answer(self, call_id: str) -> None: ...
    async def hangup(self, call_id: str) -> None: ...
    async def transfer(self, call_id: str, to: str) -> None: ...
    #: Inbound caller audio, one 20ms frame at a time.
    def inbound(self, call_id: str) -> AsyncIterator[SpeechFrame]: ...
    #: Outbound agent audio. `duration_ms` lets the sink track PLAYED time, which barge-in
    #: handling depends on completely.
    async def send(self, call_id: str, audio: bytes, duration_ms: float) -> None: ...


# ── μ-law ────────────────────────────────────────────────────────────────────
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def pcm16_to_ulaw(sample: int) -> int:
    """One 16-bit PCM sample to μ-law (G.711).

    Written out rather than pulled from `audioop` because `audioop` was removed in Python 3.13
    and the replacement packages are C extensions that need a compiler on Windows. Twelve lines
    of arithmetic is a better dependency than a build toolchain.
    """
    sign = 0x80 if sample < 0 else 0x00
    sample = min(abs(sample), _ULAW_CLIP) + _ULAW_BIAS

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1

    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_to_pcm16(byte: int) -> int:
    """μ-law back to 16-bit PCM."""
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 0x07, byte & 0x0F
    sample = ((mantissa << 3) + _ULAW_BIAS) << exponent
    sample -= _ULAW_BIAS
    return -sample if sign else sample


def frame_energy(ulaw: bytes) -> float:
    """RMS energy of a μ-law frame, normalised to 0..1.

    Decoding first matters more than it looks: μ-law is logarithmic, so the RMS of the raw bytes
    is not proportional to loudness at all. Computing energy on undecoded bytes produces a VAD
    that works acceptably on loud speech and fails on quiet speech, which is the hardest kind of
    bug to notice in testing and the most annoying kind to experience on a call.
    """
    if not ulaw:
        return 0.0
    total = sum(ulaw_to_pcm16(b) ** 2 for b in ulaw)
    return min(1.0, (total / len(ulaw)) ** 0.5 / 32768.0)


# ── the simulator ────────────────────────────────────────────────────────────
@dataclass(slots=True)
class ScriptedTurn:
    """One caller turn in a simulated call.

    `pauses` is the point of the whole structure: a list of (position_in_text, silence_ms)
    describing where the caller hesitated. That is exactly the input the endpointer has to get
    right, and it is exactly what no live test can reproduce twice.
    """

    text: str
    #: (character offset into `text`, milliseconds of silence at that point)
    pauses: tuple[tuple[int, float], ...] = ()
    #: Silence after the turn genuinely ends.
    trailing_silence_ms: float = 1_200.0
    #: Speaking rate. 55ms/char is close to conversational English.
    ms_per_char: float = 55.0
    #: Does this caller talk over the agent? Milliseconds into the agent's reply, or None.
    interrupt_agent_at_ms: float | None = None
    #: What they say while interrupting. A backchannel here should NOT stop the agent.
    interruption_text: str = ""


@dataclass(slots=True)
class SimulatedCall:
    """A scripted caller, replayed at real frame cadence on a virtual clock.

    Every source of nondeterminism a real call has — network jitter, ASR variance, model
    sampling — is either removed or made an explicit, seeded parameter. A test that passes here
    passes for a reason, and a test that fails here fails the same way tomorrow.
    """

    turns: tuple[ScriptedTurn, ...]
    call_id: str = "sim-1"
    from_number: str = "+15550100"
    to_number: str = "+15550199"
    #: Fraction of frames dropped, simulating packet loss. Real calls lose 0.1–2%; the interesting
    #: question is whether an account number survives a burst, and that needs to be reproducible.
    packet_loss: float = 0.0
    #: Ambient noise floor. A caller in a car is not a caller on a headset, and an endpointer
    #: tuned only on the latter falls apart on the former.
    noise_floor: float = 0.01
    #: Seeds the loss pattern. Same seed, same dropped frames, every run.
    seed: int = 7

    _clock_ms: float = 0.0
    _sent: list[tuple[bytes, float]] = field(default_factory=list)
    _status: CallStatus = CallStatus.RINGING
    _partial: str = ""
    _latest: SpeechFrame | None = None

    # -- provider surface ----------------------------------------------------
    async def answer(self, call_id: str = "") -> None:
        self._status = CallStatus.IN_PROGRESS

    async def hangup(self, call_id: str = "") -> None:
        self._status = CallStatus.COMPLETED

    async def transfer(self, call_id: str = "", to: str = "") -> None:
        self._status = CallStatus.TRANSFERRING

    async def send(self, call_id: str, audio: bytes, duration_ms: float) -> None:
        self._sent.append((audio, duration_ms))
        self._clock_ms += duration_ms

    @property
    def partial(self) -> str:
        """Transcript so far. Read by the orchestrator exactly as a real recogniser's is."""
        return self._partial

    @property
    def latest(self) -> SpeechFrame | None:
        """Most recent inbound frame, for barge-in evaluation during agent speech."""
        return self._latest

    @property
    def status(self) -> CallStatus:
        return self._status

    @property
    def spoken_ms(self) -> float:
        return sum(d for _, d in self._sent)

    def inbound(self, call_id: str = "") -> AsyncIterator[SpeechFrame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[SpeechFrame]:
        """Turn the script into frames, pauses and all.

        Partial transcripts are revealed WORD BY WORD as the audio plays, because that is what a
        streaming recogniser does and it is what the endpointer sees. Revealing the full turn up
        front — the obvious shortcut — would let the endpointer make decisions on text the
        caller has not said yet, and every result measured that way would be fiction.
        """
        rng = _Lcg(self.seed)
        for turn in self.turns:
            self._partial = ""
            # Milliseconds of speech owed but not yet emitted as frames. Accumulating rather
            # than emitting one frame per character is the difference between honouring
            # `ms_per_char` and silently speaking at 20ms/char -- roughly 3000 words a minute,
            # which makes every latency figure measured against it meaningless.
            owed = 0.0
            for index, char in enumerate(turn.text):
                for pause_at, pause_ms in turn.pauses:
                    if index == pause_at:
                        # Reveal what has been said so far, then go quiet. This is the moment the
                        # endpointer has to judge, and the entire benchmark is about it.
                        self._partial = turn.text[:index].strip()
                        async for frame in self._silence(pause_ms, rng):
                            yield frame

                if char == " " or index == len(turn.text) - 1:
                    self._partial = turn.text[: index + 1].strip()

                owed += turn.ms_per_char
                while owed >= FRAME_MS:
                    owed -= FRAME_MS
                    frame = SpeechFrame(
                        energy=0.35 + 0.25 * rng.next(), is_speech=True, duration_ms=FRAME_MS
                    )
                    # A dropped frame still costs wall-clock time -- the audio was sent, it just
                    # did not arrive. Advancing the clock only on delivered frames would make
                    # packet loss look like it speeds the caller up.
                    if rng.next() >= self.packet_loss:
                        self._latest = frame
                        yield frame
                    self._clock_ms += FRAME_MS
                    await asyncio.sleep(0)

            self._partial = turn.text.strip()
            async for frame in self._silence(turn.trailing_silence_ms, rng):
                yield frame

    async def _silence(self, duration_ms: float, rng: _Lcg) -> AsyncIterator[SpeechFrame]:
        elapsed = 0.0
        while elapsed < duration_ms:
            frame = SpeechFrame(
                energy=self.noise_floor * (0.5 + rng.next()), is_speech=False, duration_ms=FRAME_MS
            )
            self._latest = frame
            yield frame
            elapsed += FRAME_MS
            self._clock_ms += FRAME_MS
            # Yield to the loop so the orchestrator's own tasks interleave the way they would on
            # a real call. Without this the simulator runs the whole turn before the orchestrator
            # sees a single frame, and concurrency bugs hide perfectly.
            await asyncio.sleep(0)


class _Lcg:
    """A tiny seeded PRNG.

    `random` is deliberately avoided: it is global mutable state, so a test that seeds it can be
    perturbed by any other test that touches it. A call simulator whose packet loss depends on
    test ordering is worse than no simulator.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = (seed or 1) & 0xFFFFFFFF

    def next(self) -> float:
        self._state = (1_103_515_245 * self._state + 12_345) & 0x7FFFFFFF
        return self._state / 0x7FFFFFFF


def frames_from_pcm(samples: Iterable[int], noise_floor: float = 0.01) -> list[SpeechFrame]:
    """Real PCM to frames, for replaying a recording through the pipeline.

    The bridge from captured audio to the same interface the simulator presents, so a regression
    found on a real call can be turned into a permanent test.
    """
    out: list[SpeechFrame] = []
    buffer: list[int] = []
    for sample in samples:
        buffer.append(sample)
        if len(buffer) >= FRAME_BYTES:
            ulaw = bytes(pcm16_to_ulaw(s) for s in buffer)
            energy = frame_energy(ulaw)
            out.append(
                SpeechFrame(energy=energy, is_speech=energy > noise_floor * 3, duration_ms=FRAME_MS)
            )
            buffer.clear()
    return out
