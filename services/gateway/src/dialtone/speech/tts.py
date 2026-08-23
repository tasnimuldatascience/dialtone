"""Turning the agent's words into a voice worth listening to.

WHY NOT THE BROWSER. Web Speech synthesis is free, instant, and sounds like a train station
announcement — on Windows it falls back to SAPI voices that were state of the art in about 2005.
For a product whose entire pitch is that talking to it feels like a conversation, that is not a
detail. The first thing anyone judges is the voice.

KOKORO-82M. Apache-2.0, 82 million parameters, ~330 MB, and genuinely natural. It runs locally on
CPU at roughly twice real time, which sounds like a problem and is not — see below.

THE LATENCY TRICK IS THE SAME ONE THIS WHOLE PROJECT IS BUILT ON. Two times real time means a
five-second reply takes two and a half seconds to synthesise, and waiting for that would put the
agent right back where it started. So synthesis runs CLAUSE BY CLAUSE: the first clause of a
sentence is about a second of audio and takes ~400ms to produce, and playback starts there while
the rest is still being generated. The caller hears speech in under half a second, and every
clause after that is produced faster than the previous one is spoken.

That only works because the audio is streamed rather than returned. A synthesiser that hands back
a finished WAV is a synthesiser that has already lost the argument.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("dialtone.tts")

MODELS = Path(__file__).resolve().parents[3] / "models"
MODEL_FILE = MODELS / "kokoro-v1.0.onnx"
VOICES_FILE = MODELS / "voices-v1.0.bin"

#: The product's voice names, mapped to Kokoro's. Keeping our own names means the voice can be
#: swapped for a different engine without every stored agent config becoming wrong.
VOICES: dict[str, tuple[str, str]] = {
    "female-warm": ("bf_emma", "en-gb"),
    "female-clear": ("bf_isabella", "en-gb"),
    "male-warm": ("bm_george", "en-gb"),
    "male-clear": ("bm_lewis", "en-gb"),
    "female-us": ("af_heart", "en-us"),
    "male-us": ("am_michael", "en-us"),
}

#: Slightly above natural. A receptionist speaks briskly, and synthesised speech reads as slower
#: than it measures -- there are none of the disfluencies a listener uses to track pace.
DEFAULT_SPEED = 1.06

#: THE FIRST CHUNK IS DELIBERATELY TINY, and this is the single most important number in the
#: file. Synthesis runs at ~2x real time, so the caller waits half the DURATION of the first
#: chunk before hearing anything. Splitting on clauses alone gave a 2.4-second opening phrase and
#: therefore 1.3 seconds of silence -- respectable for a chatbot and far too slow for a phone
#: call, where 500ms of dead air after a question is the point people say "hello?".
#:
#: Measured on this machine, generation costs about 25ms per character plus ~150ms of fixed
#: overhead, so the opening is sized straight off that curve: ~12 characters lands near 500ms.
#: A short opening phrase is not a prosody problem -- "Of course," and "A routine" are things
#: people say -- because the next chunk follows gaplessly.
FIRST_CHUNK_CHARS = 12

#: Later chunks. Longer is better once playback has started: fewer calls, less per-call overhead,
#: and noticeably better intonation because the model can see a whole clause of context.
CHUNK_CHARS = 90

#: Below this a fragment is not worth its own synthesis call -- the overhead dominates and a
#: one-word chunk has no prosodic context, so it comes out flat and clipped.
MIN_CLAUSE_CHARS = 16

_CLAUSE = re.compile(r"(?<=[,;:.!?])\s+")
_WORD_BREAK = re.compile(r"\s+")


def split_clauses(
    text: str, *, first_chars: int = FIRST_CHUNK_CHARS, chunk_chars: int = CHUNK_CHARS,
) -> list[str]:
    """Break a reply into the units synthesis starts on, smallest first.

    The opening chunk is cut at the earliest word boundary past `first_chars`, punctuation or
    not, because the caller is waiting on it and nothing else. After that the text is regrouped
    into clause-aligned chunks up to `chunk_chars`, which sound better and no longer cost
    anything -- they are produced while the previous chunk plays.
    """
    text = " ".join(text.split())
    if not text:
        return []

    out: list[str] = []

    # ── the opening ──────────────────────────────────────────────────────
    # Only worth splitting when there is enough left to be worth streaming; a short reply is
    # produced fast enough whole.
    if len(text) > first_chars + MIN_CLAUSE_CHARS:
        cut = _first_cut(text, first_chars)
        if cut:
            out.append(text[:cut].strip())
            text = text[cut:].strip()

    # ── the rest ─────────────────────────────────────────────────────────
    buffer = ""
    for part in (p.strip() for p in _CLAUSE.split(text) if p.strip()):
        if not buffer:
            buffer = part
        elif len(buffer) + len(part) + 1 <= chunk_chars:
            buffer = f"{buffer} {part}"
        else:
            out.append(buffer)
            buffer = part
    if buffer:
        # Never leave a scrap on its own at the end.
        if out and len(buffer) < MIN_CLAUSE_CHARS:
            out[-1] = f"{out[-1]} {buffer}"
        else:
            out.append(buffer)

    return out


def _first_cut(text: str, target: int) -> int:
    """Where to end the opening chunk: a clause boundary if one is near, else a word boundary.

    A clause boundary is preferred because it carries its own intonation, but only when it lands
    close to the target -- waiting for a comma that arrives forty characters later would give
    back everything this is trying to save.
    """
    # A clause boundary carries its own intonation, so it is preferred -- but only if it is
    # RIGHT THERE. Allowing it fourteen characters past the target let "For a routine check-up,"
    # become the opening chunk: 1.5 seconds of audio, and therefore 1.5 seconds of silence
    # before the caller heard anything.
    clause = _CLAUSE.search(text)
    if clause and clause.end() <= target + 4:
        return clause.end()
    word = _WORD_BREAK.search(text, target)
    return word.start() if word else 0


@dataclass(slots=True)
class Clip:
    """One synthesised clause."""

    text: str
    #: 16-bit mono PCM in a WAV container, ready for an <audio> element or Web Audio.
    wav: bytes
    sample_rate: int
    duration_ms: float
    generate_ms: float
    index: int = 0


class Synthesizer:
    """Kokoro, loaded once and shared by every call."""

    def __init__(self, model: Path = MODEL_FILE, voices: Path = VOICES_FILE) -> None:
        self.model_path = model
        self.voices_path = voices
        self._kokoro: Any = None
        self._lock = threading.Lock()
        #: One at a time. The model is not thread-safe and the CPU has finite cores; two
        #: concurrent syntheses would make both slow rather than one fast, and a voice agent
        #: would much rather be fast for the caller currently speaking.
        self._gate = asyncio.Semaphore(1)
        self.load_seconds = 0.0

    @property
    def available(self) -> bool:
        return self.model_path.exists() and self.voices_path.exists()

    @property
    def ready(self) -> bool:
        return self._kokoro is not None

    def load(self) -> None:
        if self._kokoro is not None or not self.available:
            return
        with self._lock:
            if self._kokoro is not None:
                return
            from kokoro_onnx import Kokoro

            started = time.perf_counter()
            self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))

            # One throwaway synthesis. A cold ONNX session pays graph optimisation and arena
            # setup on its first call, and paying that on a live caller's opening sentence is
            # the worst available place for it.
            try:
                self._kokoro.create("Ready.", voice="bf_emma", speed=1.0, lang="en-gb")
            except Exception:  # noqa: BLE001 -- warming is best effort
                log.debug("voice warm-up failed; the first reply will be slower", exc_info=True)

            self.load_seconds = time.perf_counter() - started
            log.info("loaded Kokoro in %.1fs", self.load_seconds)

    def _synth(self, text: str, voice: str, speed: float) -> Clip:
        kokoro_voice, lang = VOICES.get(voice, VOICES["female-warm"])
        started = time.perf_counter()
        samples, rate = self._kokoro.create(text, voice=kokoro_voice, speed=speed, lang=lang)
        generate_ms = (time.perf_counter() - started) * 1000
        return Clip(
            text=text,
            wav=_to_wav(samples, rate),
            sample_rate=rate,
            duration_ms=len(samples) / rate * 1000,
            generate_ms=generate_ms,
        )

    async def speak(
        self, text: str, *, voice: str = "female-warm", speed: float = DEFAULT_SPEED,
    ) -> AsyncIterator[Clip]:
        """Synthesise a reply clause by clause, yielding each as it is ready.

        The first clip is what the caller waits for; everything after it is produced while the
        previous one is still playing. Returning a finished WAV instead would be simpler and
        would cost the entire saving.
        """
        if self._kokoro is None:
            self.load()
        if self._kokoro is None:
            return

        clauses = split_clauses(text)
        for index, clause in enumerate(clauses):
            async with self._gate:
                # Off the event loop: synthesis is CPU-bound and holding the loop would stall
                # every other call on the box, including the one waiting for its first clause.
                clip = await asyncio.to_thread(self._synth, clause, voice, speed)
            clip.index = index
            yield clip

    async def speak_once(
        self, text: str, *, voice: str = "female-warm", speed: float = DEFAULT_SPEED,
    ) -> Clip | None:
        """One WAV for the whole text. For previews, never for a live call."""
        if self._kokoro is None:
            self.load()
        if self._kokoro is None:
            return None
        async with self._gate:
            return await asyncio.to_thread(self._synth, text.strip(), voice, speed)


def _to_wav(samples: Any, rate: int) -> bytes:
    """Float samples to a 16-bit PCM WAV.

    A container rather than raw PCM because the browser needs to know the sample rate, and
    getting that wrong produces audio that plays at the wrong pitch — which sounds like a broken
    model rather than a broken header, and is debugged accordingly.
    """
    import numpy as np

    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()
