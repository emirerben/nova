"""Deterministic ffmpeg stand-ins for incident media. Generated on demand, never committed.

Real footage is private and never enters git. A record's ``synthetic`` clips are
tone-tagged / colour-tagged substitutes with known durations, so an output proof
(owned by a later PR) can ask "is the voice tone present?" or "does the song tone
play once?" on media anyone can regenerate.
"""

from __future__ import annotations

from pathlib import Path

from tests.incidents.models import SyntheticClip

SIZE = "180x320"
FPS = 30


def ffmpeg_argv(clip: SyntheticClip, out: Path) -> list[str]:
    """The exact ffmpeg command for one synthetic clip."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    d = f"{clip.duration_s}"
    if clip.role == "song":
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={clip.tone_hz or 880}:duration={d}"]
        return [*cmd, "-c:a", "aac", "-ar", "44100", str(out)]
    cmd += ["-f", "lavfi", "-i", f"color=c={clip.color or 'black'}:s={SIZE}:d={d}:r={FPS}"]
    if clip.tone_hz:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={clip.tone_hz}:duration={d}"]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={d}"]
    return [
        *cmd,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(out),
    ]  # fmt: skip


def suffix(clip: SyntheticClip) -> str:
    return ".m4a" if clip.role == "song" else ".mp4"
