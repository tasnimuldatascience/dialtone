"""Narration for the product tour, spoken by the project's own voice engine.

WHY KOKORO RATHER THAN A STOCK VOICEOVER. dialtone ships a text-to-speech engine and argues about
its latency at length. Narrating its own tour with somebody else's would be a small dishonesty,
and the video is meant to be evidence.

TWO VOICES, DELIBERATELY. The narrator is `af_heart`; the agent inside the recording keeps its own
`bf_emma`. A documentation video where the commentary and the product sound identical is a video
where the viewer cannot tell which one is talking — and the moment that matters most here is the
one where you hear the agent itself.

THE AUDIO IS MADE FIRST, AND THE PICTURE IS PACED TO IT. The obvious order — record a tour, then
try to talk over it — needs an editor, and produces something that cannot be regenerated. So this
runs first, measures every line, and writes a manifest. `demo-video.mjs` reads that manifest and
holds each scene for exactly as long as its line takes to say. Nothing is trimmed afterwards and
the two tracks cannot drift, because neither was cut to fit the other.

    python scripts/narrate.py            # writes docs/video/audio/*.wav + narration.json
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('DIALTONE_PORT', '8071')}"
OUT = pathlib.Path(os.environ.get("VIDEO_DIR", "docs/video")) / "audio"

#: af_heart. Kokoro's best-regarded voice, and NOT the one the seeded agent uses -- see above.
VOICE = "female-us"

#: A beat of silence after each line. Sentences need air more than they need brevity, and a
#: narrator that runs headlong from one scene into the next sounds like a machine reading.
TAIL_MS = 700

#: WRITTEN TO BE SAID, NOT READ. Short sentences, contractions, and full stops where a person
#: would draw breath -- Kokoro takes its pauses from punctuation, so the punctuation is doing
#: prosody rather than grammar. Long clauses are the single biggest reason synthesised narration
#: sounds synthetic.
#:
#: IT DESCRIBES THE PIPELINE, NOT THE FEATURES. A tour that lists screens is a tour anybody could
#: give about anybody's product. What is worth three minutes of somebody's attention is where the
#: latency goes, what is guaranteed by code rather than by prompt, and what happens when two
#: callers want the same thing at the same time.
LINES: list[dict[str, object]] = [
    {"id": "title", "tail": 1000,
     "say": "This is dialtone. A voice agent that answers the phone, books the appointment, and "
            "proves it did."},
    {"id": "dashboard",
     "say": "Start with the numbers, because they're the argument. These are real percentiles, "
            "measured at the socket, across real turns."},
    {"id": "call-open",
     "say": "Here's a live call on the text channel. Watch the timings under each reply."},
    {"id": "pipeline",
     "say": "Every turn runs the same seven stages. Endpointing, recognition, redaction, "
            "retrieval, the model, grounding, then speech."},
    {"id": "pipeline-stream", "tail": 900,
     "say": "And they're pipelined. Each stage starts on the first token of the one before it, "
            "not on its last. Waiting for whole sentences is where most agents lose their second."},
    {"id": "knowledge",
     "say": "Retrieval is lexical and dense, fused. And the reply is checked back against it. "
            "Every figure the model wrote has to appear in a passage the model was actually "
            "given, or it gets flagged, right there in the transcript."},
    {"id": "typed",
     "say": "Names and email addresses are typed, never transcribed. Recognition mangles exactly "
            "the values that have to be exact. One real call heard tasty mulasson for a surname."},
    {"id": "booking",
     "say": "Now the important part. The model proposes a time. Code decides. It checks the slot "
            "is real, and free, and unambiguous, writes the row, and only then lets the agent "
            "speak. A model that invents Thursday at nine cannot create a Thursday at nine."},
    {"id": "voice",
     "say": "Same pipeline on the voice channel. Two more stages: streaming recognition in, "
            "streaming synthesis out. Here's a caller."},
    {"id": "bargein", "tail": 900,
     "say": "And you can talk over it. When you do, its history is truncated to the audio that "
            "actually played. So it never believes it said the half you never heard."},
    {"id": "concurrency",
     "say": "It handles many calls at once, and every one of them is isolated. Its own memory, "
            "its own position in the flow, its own proposed slot. Nothing crosses between them."},
    {"id": "concurrency-full", "tail": 900,
     "say": "Past the measured capacity, there's admission control. The next caller is refused, "
            "with a reason and a retry-after. Nothing degrades quietly, because ten callers "
            "waiting five seconds each is worse than one being told to ring back."},
    {"id": "appointment",
     "say": "The appointment is a row, and the start time is unique. So a race between two live "
            "calls fails at insert, instead of double booking. The one who loses is told, and "
            "offered the next slot in the same breath."},
    {"id": "history",
     "say": "Afterwards, every call says what it was about, and what it achieved. Both derived "
            "from what the call did, not from what was said first."},
    {"id": "detail",
     "say": "Open one. The node the agent was on, the transition it took, and the document it "
            "cited are printed under every single reply. There's no log correlation to do."},
    {"id": "flow",
     "say": "Because a directed graph decides what's possible. The model still picks its words. "
            "But it cannot book during the greeting, because that tool does not exist at that "
            "node."},
    {"id": "flow-scroll",
     "say": "Every node, what it must collect, the tools reachable from it, and every legal "
            "transition out. Declared, enumerated, and enforced at runtime, not suggested in a "
            "prompt."},
    {"id": "turntaking",
     "say": "Endpointing is the hardest stage, and the one nobody publishes. Silence, syntax and "
            "pitch, together, answer in two hundred and eighty milliseconds. And cut nobody off."},
    {"id": "compliance",
     "say": "Last one. A card number, typed in full."},
    {"id": "compliance-gone", "tail": 900,
     "say": "Gone before it leaves the socket. Not before it's stored. A model that never "
            "receives a card number cannot repeat one, and that's structural. It isn't an "
            "instruction it might ignore."},
    {"id": "end", "tail": 1800,
     "say": "Five hundred and thirty six tests. And a written record of every bug that earned "
            "one."},
]


def synthesise(text: str, voice: str = VOICE) -> tuple[bytes, float]:
    request = urllib.request.Request(
        f"{BASE}/api/voice/preview", method="POST",
        data=json.dumps({"text": text, "voice": voice}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.loads(response.read())
    return base64.b64decode(payload["wav"]), float(payload["duration_ms"])


def main() -> int:
    try:
        health = json.load(urllib.request.urlopen(f"{BASE}/api/health", timeout=10))
    except urllib.error.URLError:
        print("The gateway is not running.")
        return 2
    if not health.get("voice", {}).get("ready"):
        print("The voice engine is not loaded, so there is nothing to narrate with.")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    total = 0.0

    for line in LINES:
        text = str(line["say"])
        # The endpoint takes 400 characters, and a narration line longer than that is too long to
        # listen to anyway. A real limit rather than one to work around.
        if len(text) > 400:
            print(f"  {line['id']}: {len(text)} characters is more than one breath")
            return 1

        wav, duration = synthesise(text)
        path = OUT / f"{line['id']}.wav"
        path.write_bytes(wav)

        tail = float(line.get("tail", TAIL_MS))
        manifest.append({
            "id": line["id"], "file": path.name, "say": text,
            "duration_ms": round(duration, 1), "tail_ms": tail,
            "hold_ms": round(duration + tail, 1),
        })
        total += duration + tail
        print(f"  {line['id']!s:<12} {duration / 1000:5.1f}s  {text[:56]}…")

    (OUT.parent / "narration.json").write_text(
        json.dumps({"voice": VOICE, "total_ms": round(total, 1), "lines": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"\n  {len(manifest)} lines, {total / 1000:.1f}s of narration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
