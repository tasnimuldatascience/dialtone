"""Lay the narration onto the recording and encode the finished film.

WHY THIS IS A SCRIPT AND NOT AN EDITOR. `narrate.py` measured every line before the tour was
recorded, and `demo-video.mjs` held each scene for exactly as long as its line takes to say, then
wrote down the millisecond at which each one began. So there is nothing to cut: this places each
clip at the offset the recording already agreed to, and any drift would be a bug rather than
something to nudge by hand.

TWO SOURCES OF SPEECH, AND THEY ARE DIFFERENT THINGS. The narrator explains; the agent answers a
caller. The agent's line is the reply it actually gave during the recording, synthesised through
the same endpoint the browser used, because Playwright captures no audio and a voice agent whose
demo is silent is not much of a demo.

    python scripts/soundtrack.py         # docs/video/tour.webm -> dialtone-tour.mp4
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

OUT = pathlib.Path(os.environ.get("VIDEO_DIR", "docs/video"))
VIDEO = OUT / "tour.webm"
FINAL = OUT / "dialtone-tour.mp4"

#: The narrator sits under nothing, so it is mixed at full level. The agent's own voice is left
#: alone too -- it is the product talking, and quieter would be a strange thing to say about it.
NARRATOR_GAIN = 1.0
AGENT_GAIN = 1.0


def ffmpeg() -> str:
    """A real ffmpeg, not Playwright's.

    Playwright ships one, and it is built `--disable-everything`: VP8 in, WebM out, no audio
    encoders and no MP4 muxer. It can record the picture and can do nothing whatever with sound.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    packages = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages"
    for candidate in packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
        return str(candidate)
    raise SystemExit(
        "ffmpeg is not installed. It is needed to add sound and to write an MP4:\n"
        "    winget install --id Gyan.FFmpeg -e"
    )


def main() -> int:
    if not VIDEO.exists():
        print(f"{VIDEO} does not exist. Run scripts/demo-video.mjs first.")
        return 2
    plan = json.loads((OUT / "moments.json").read_text(encoding="utf-8"))
    moments = plan["moments"]
    audio_dir = OUT / "audio"

    clips = [m for m in moments if (audio_dir / m["file"]).exists()]
    missing = [m["id"] for m in moments if not (audio_dir / m["file"]).exists()]
    if missing:
        print(f"  no audio for: {', '.join(missing)}")
    if not clips:
        print("  nothing to mix")
        return 1

    # ── one input per clip, each delayed to the millisecond the scene began ──
    # `adelay` rather than concatenating with silence: the offsets are absolute, so a clip that
    # runs long cannot push everything after it out of sync. They simply overlap, which is also
    # what would happen in a room.
    inputs: list[str] = ["-i", str(VIDEO)]
    filters: list[str] = []
    labels: list[str] = []
    for i, clip in enumerate(clips):
        inputs += ["-i", str(audio_dir / clip["file"])]
        gain = AGENT_GAIN if clip.get("kind") == "agent" else NARRATOR_GAIN
        filters.append(
            f"[{i + 1}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={gain},adelay={int(clip['at'])}|{int(clip['at'])}[a{i}]"
        )
        labels.append(f"[a{i}]")

    # `dropout_transition=0` keeps the level steady across gaps -- amix normally fades up when an
    # input ends, which makes every pause between lines breathe audibly.
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:dropout_transition=0[mix]"
    )

    command = [
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[mix]",
        # H.264 high profile, yuv420p: the combination that plays everywhere, including in a
        # GitHub release embed and on a phone. A WebM does not.
        "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-level", "4.1", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-shortest",
        str(FINAL),
    ]
    print(f"  mixing {len(clips)} clips into {FINAL.name}…")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        print(result.stderr[-2000:])
        return 1

    size_mb = FINAL.stat().st_size / 1_000_000
    print(f"  {FINAL}  {size_mb:.1f} MB")

    # A poster frame, for wherever the video is linked but not yet playing.
    poster = OUT / "poster.png"
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", "3", "-i", str(FINAL), "-frames:v", "1", str(poster)], check=False)
    if poster.exists():
        print(f"  {poster}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
