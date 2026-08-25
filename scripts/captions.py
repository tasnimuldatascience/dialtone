"""Subtitles for the tour, from the timings the recording already wrote down.

WHY THIS IS NOT GUESSWORK. `narrate.py` measured every line before the tour was recorded, and
`demo-video.mjs` wrote down the millisecond each one started. So the subtitle timings are not
transcribed after the fact or nudged by hand — they are the same numbers the mixer used to place
the audio. If the audio is in sync, these are too, by construction.

WHY THEY MATTER MORE THAN THE AUDIO DOES. On LinkedIn, and on most of X and Instagram, video
autoplays muted. A narrated demo with no captions is a silent film to the majority of the people
who will ever scroll past it.

    python scripts/captions.py           # writes docs/video/dialtone-tour.srt
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

OUT = pathlib.Path(os.environ.get("VIDEO_DIR", "docs/video"))

#: Roughly two lines of comfortable reading. Past this a cue is on screen long enough to feel
#: static and short enough to be missed, which is the worst of both.
MAX_CHARS = 74

#: Who is talking, when it is not the narrator. Without this the caller's line and the agent's
#: reply read as more narration, and the one moment the video exists to show is lost.
SPEAKER = {"caller": "CALLER: ", "agent": "AGENT: "}


def sentences(text: str) -> list[str]:
    """Split on sentence ends, then glue the short ones back together.

    A cue per sentence is right for "Gone before it leaves the socket." and wrong for "So." --
    a two-word flash is harder to read than no caption at all.
    """
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]

    # A SENTENCE CAN STILL BE TOO LONG. "Every figure the model wrote has to appear in a passage
    # the model was actually given, or it gets flagged, right there in the transcript" is one
    # sentence and 135 characters, which is four lines of subtitle. Broken at the comma nearest
    # the middle, because that is where the speaker breathed.
    split: list[str] = []
    for part in parts:
        while len(part) > MAX_CHARS + 24:
            comma = part.rfind(", ", 20, len(part) // 2 + 26)
            if comma < 0:
                break
            split.append(part[:comma + 1])
            part = part[comma + 2:]
        split.append(part)

    out: list[str] = []
    for part in split:
        if out and len(out[-1]) + len(part) + 1 <= MAX_CHARS:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out or [text]


def wrap(text: str, width: int = 42) -> str:
    """Two lines at most, broken on a space. Subtitle players do not reflow."""
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    # THREE LINES AT 42 CHARACTERS BEATS TWO AT SIXTY. Rebalancing into two produced
    # "AGENT: Yes, there is free parking available in the lot behind the" on a single line, which
    # is wider than the safe area on a phone and is the first thing a player clips.
    if len(lines) <= 3:
        return "\n".join(lines)
    midpoint = len(text) // 2
    space = text.rfind(" ", 0, midpoint + 12)
    return f"{text[:space]}\n{text[space + 1:]}" if space > 0 else text


def stamp(ms: float) -> str:
    ms = max(0, int(ms))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def main() -> int:
    plan_path, narration_path = OUT / "moments.json", OUT / "narration.json"
    if not plan_path.exists() or not narration_path.exists():
        print("Run scripts/narrate.py and scripts/demo-video.mjs first.")
        return 2

    moments = json.loads(plan_path.read_text(encoding="utf-8"))["moments"]
    spoken = {
        line["id"]: float(line["duration_ms"])
        for line in json.loads(narration_path.read_text(encoding="utf-8"))["lines"]
    }

    cues: list[tuple[float, float, str]] = []
    for moment in moments:
        text = str(moment.get("say", "")).strip()
        if not text:
            continue

        # A narrator line's length is in the manifest. The caller's and the agent's are not --
        # they were synthesised during the recording — so they are timed from the gap to the next
        # moment, which is exactly how long the recording held the frame for them.
        length = spoken.get(moment["id"])
        if length is None:
            later = [m["at"] for m in moments if m["at"] > moment["at"]]
            length = (min(later) - moment["at"] - 400) if later else 4000
        length = max(1200.0, float(length))

        prefix = SPEAKER.get(str(moment.get("kind", "")), "")
        chunks = sentences(text)
        total = sum(len(c) for c in chunks) or 1
        cursor = float(moment["at"])
        for i, chunk in enumerate(chunks):
            share = length * (len(chunk) / total)
            # A 40ms gap between cues, so consecutive lines visibly change rather than
            # appearing to be one caption that grew.
            cues.append((cursor, cursor + share - 40,
                         wrap(f"{prefix if i == 0 else ''}{chunk}")))
            cursor += share

    cues.sort(key=lambda c: c[0])

    # Overlapping cues make a player show one and drop the other. The narration and an agent
    # line can genuinely overlap in the audio; on screen, the later one wins its start.
    for i in range(len(cues) - 1):
        start, end, text = cues[i]
        if end > cues[i + 1][0]:
            cues[i] = (start, max(start + 900, cues[i + 1][0] - 40), text)

    srt = "\n".join(
        f"{i}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for i, (start, end, text) in enumerate(cues, 1)
    )
    path = OUT / "dialtone-tour.srt"
    path.write_text(srt, encoding="utf-8")
    print(f"  {path}   {len(cues)} cues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
