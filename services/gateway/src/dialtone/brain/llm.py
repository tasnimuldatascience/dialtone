"""The language model that decides what the agent says.

RUNS LOCALLY. No API key, no per-call cost, no data leaving the machine — which for a product
that handles recorded phone calls is a compliance position, not just a convenience. The default
is Qwen2.5-1.5B-Instruct because it fits in 3.1 GB of VRAM and answers a receptionist's questions
well enough to be worth listening to.

STREAMING IS NOT OPTIONAL. Everything in `pipeline/orchestrator.py` is built on starting the next
stage at the FIRST token rather than the last. A model wrapper that returns a finished string
would quietly cost ~400ms per turn and make the entire latency argument of this project false.
So the only generation method here is an async iterator, and there is deliberately no
`generate()` that returns a whole reply.

THE FALLBACK MATTERS AS MUCH AS THE MODEL. CI has no GPU and no model weights, and a test suite
that cannot run without them is a test suite nobody runs. `ScriptedBrain` implements the same
interface with canned replies, so every layer above this is testable on a laptop with no
accelerator at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("dialtone.brain")

#: Small enough to load in ~5s and stream at ~20 tok/s on a laptop GPU, large enough to follow a
#: system prompt and stay on topic. The 0.5B variant is barely faster here -- at this size the
#: GPU is latency-bound rather than throughput-bound -- so the larger model is nearly free.
DEFAULT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

#: Phone replies must be short. A model that writes three paragraphs is unusable on a call
#: however good the prose is, because the caller cannot skim it.
MAX_NEW_TOKENS = 96


@dataclass(slots=True)
class Turn:
    role: str      # "system" | "user" | "assistant"
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class Brain(Protocol):
    """What the conversation loop needs from a model. Deliberately one method."""

    def stream(self, messages: list[Turn], **kwargs: Any) -> AsyncIterator[str]: ...


# ── the local model ──────────────────────────────────────────────────────────
class LocalBrain:
    """Qwen (or any chat model) running on the local GPU, streamed token by token."""

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None) -> None:
        self.model_name = model_name
        self.device = device
        self._tokenizer: Any = None
        self._model: Any = None
        self._lock = threading.Lock()
        #: Generation is not thread-safe and the GPU has one queue anyway. Serialising here is
        #: honest: two simultaneous calls on one laptop GPU would both be slow rather than one
        #: being fast, and a voice agent would rather be fast for one caller.
        self._gpu = asyncio.Semaphore(1)
        self.load_seconds = 0.0

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load the weights. Called at startup, never on the first call.

        A lazily-loaded model makes the first caller of the day wait five seconds for a greeting,
        which is the worst possible first impression and does not show up in any average.
        """
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            started = time.perf_counter()
            device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_name,
                dtype=torch.float16 if device == "cuda" else torch.float32,
                device_map=device,
            )
            self._model.eval()
            self.device = device
            self.load_seconds = time.perf_counter() - started
            log.info("loaded %s on %s in %.1fs", self.model_name, device, self.load_seconds)

            # One throwaway generation. The first CUDA call compiles kernels and costs ~1.5s;
            # paying it here rather than on a live call is the whole point of warming up.
            self._warm()

    def _warm(self) -> None:
        try:
            enc = self._encode([Turn("user", "hi")])
            self._model.generate(**enc, max_new_tokens=1, do_sample=False,
                                 pad_token_id=self._tokenizer.eos_token_id)
        except Exception:  # noqa: BLE001 — warming is best-effort
            log.debug("warm-up generation failed; first call will be slower", exc_info=True)

    def _encode(self, messages: list[Turn]) -> Any:
        text = self._tokenizer.apply_chat_template(
            [m.as_dict() for m in messages], add_generation_prompt=True, tokenize=False
        )
        return self._tokenizer(text, return_tensors="pt").to(self._model.device)

    async def stream(
        self, messages: list[Turn], *, max_new_tokens: int = MAX_NEW_TOKENS,
        temperature: float = 0.4, stop: tuple[str, ...] = (),
    ) -> AsyncIterator[str]:
        """Yield text as the model produces it.

        `TextIteratorStreamer` plus a worker thread, because `generate` is blocking and holding
        the event loop for a whole reply would stall every other call on the box. The queue
        between them is what lets the caller start synthesising audio on the first clause.
        """
        if self._model is None:
            self.load()

        from transformers import TextIteratorStreamer

        async with self._gpu:
            enc = self._encode(messages)
            streamer = TextIteratorStreamer(
                self._tokenizer, skip_prompt=True, skip_special_tokens=True
            )
            kwargs = {
                **enc,
                "max_new_tokens": max_new_tokens,
                "streamer": streamer,
                "pad_token_id": self._tokenizer.eos_token_id,
                # Greedy below ~0.05 -- `do_sample=True` with a near-zero temperature is a
                # numerically unstable way to ask for greedy decoding.
                "do_sample": temperature > 0.05,
            }
            if temperature > 0.05:
                kwargs["temperature"] = temperature
                kwargs["top_p"] = 0.9

            thread = threading.Thread(target=self._model.generate, kwargs=kwargs, daemon=True)
            thread.start()

            emitted = ""
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            def pump() -> None:
                # The streamer is a blocking iterator; bridge it onto the loop rather than
                # polling, so a slow model does not spin a core.
                for piece in streamer:
                    loop.call_soon_threadsafe(queue.put_nowait, piece)
                loop.call_soon_threadsafe(queue.put_nowait, None)

            threading.Thread(target=pump, daemon=True).start()

            while True:
                piece = await queue.get()
                if piece is None:
                    break
                emitted += piece
                if any(s in emitted for s in stop):
                    break
                yield piece


# ── the stand-in ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class ScriptedBrain:
    """A model-shaped object with no model behind it.

    Exists so CI, the test suite, and anyone without a GPU can exercise every layer above this
    one. It streams word by word at a configurable rate, so the latency arithmetic downstream
    still means something rather than completing instantly and hiding a missing `await`.
    """

    replies: tuple[str, ...] = ("Of course — I can help with that.",)
    #: Milliseconds per word. Roughly what a 1.5B model does on a laptop GPU.
    ms_per_word: float = 55.0
    _index: int = 0
    calls: list[list[Turn]] = field(default_factory=list)

    async def stream(self, messages: list[Turn], **kwargs: Any) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        reply = self.replies[min(self._index, len(self.replies) - 1)]
        self._index += 1
        for word in reply.split():
            await asyncio.sleep(self.ms_per_word / 1000)
            yield word + " "


# ── prompt construction ──────────────────────────────────────────────────────
#: Rules every agent gets, whatever the operator configured. These are not style preferences:
#: each one is a failure mode that makes an agent unusable on a phone line.
_PHONE_RULES = """
You are speaking on a PHONE CALL. Follow these rules absolutely:
- One or two sentences. Never more. The caller cannot skim what you say.
- Never use bullet points, numbered lists, markdown, or emoji. They cannot be heard.
- Write numbers the way they are spoken: "three thirty", not "3:30".
- Never invent facts, prices, availability, or policies. If you do not know, say so and offer
  to check or to pass the caller to a colleague.
- Quote every price, time and number EXACTLY as the company information writes it. If it gives
  a range, give the whole range. Never average a range into one figure, never round, never say
  "around" or "about" a price, and never add two prices together into a single total. A quoted
  price is a commitment, and a wrong one is a complaint.
- If the caller asks for a human, agree immediately and stop trying to help.
- Do not repeat the caller's words back to them. Answer.
""".strip()


def build_system_prompt(
    *,
    persona: str,
    business: str,
    objective: str = "",
    knowledge: str = "",
    collected: dict[str, Any] | None = None,
    transitions: list[str] | None = None,
) -> str:
    """Assemble the system message from the agent's configuration and the live call state.

    Order is deliberate: identity, then the unbreakable phone rules, then retrieved knowledge,
    then the current step. The phone rules sit ABOVE the operator's own instructions because an
    operator prompt that says "be thorough and detailed" would otherwise produce an agent that
    reads a paragraph down the line, and no amount of configuration should be able to do that.
    """
    parts = [f"You are {persona} at {business}.", _PHONE_RULES]

    if knowledge:
        parts.append(
            "Company information you may use. Only state facts that appear here — if the "
            "answer is not below, say you will check.\n\n" + knowledge
        )
    if objective:
        parts.append(f"Right now your goal is: {objective}")
    if collected:
        known = ", ".join(f"{k} = {v}" for k, v in collected.items())
        parts.append(f"Already established in this call: {known}. Do not ask for these again.")
    if transitions:
        parts.append(
            "When this step is complete, end your reply with exactly one of these markers on "
            "its own line, and nothing after it:\n"
            + "\n".join(f"[[{t}]]" for t in transitions)
        )
    return "\n\n".join(parts)


_MARKER = re.compile(r"\[\[([a-z0-9_\-]+)\]\]", re.IGNORECASE)


def split_marker(text: str) -> tuple[str, str | None]:
    """Separate the spoken reply from a transition marker.

    The model signals "I am done with this step" by emitting `[[node_id]]`. Stripping it here
    means the marker never reaches the synthesiser — an agent that says "double bracket confirm
    double bracket" out loud is the kind of bug that only shows up once there is audio.
    """
    match = _MARKER.search(text)
    if not match:
        return text.strip(), None
    return _MARKER.sub("", text).strip(), match.group(1).lower()
