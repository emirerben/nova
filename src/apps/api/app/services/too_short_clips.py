"""Which clips are "too short to use", from their measured lengths (KRI-1031).

Prod thread A228E85B (Plaza de Mayo M1, 2026-10-28): "Skip anything too short to use" over
clips of 14.3, 14.0, 11.6, 14.5 and 23.4 s plus a 0.97 s square shot and a 0.2 s mis-tap.
The resolver read the ask as a question about what the clips SHOW, matched nothing, and the
creator was asked to tap the short clips, although the server knew every length.

The clip-intent planner marks such an exclusion ``by_length="too_short"`` (and copies the
creator's number into ``under_s`` when they said one: "under a second" -> 1). The server
then decides from durations alone, never from the words:

* With the creator's number: a clip is too short when it is shorter than that number.
* Without one: a clip is too short when it is under ``TOO_SHORT_MAX_S`` (1.5 s) AND under
  ``TOO_SHORT_MEDIAN_SHARE`` (a quarter) of the median clip length. 1.5 s is under two of
  the montage's shortest readable shots (``MIN_READABLE_SHOT_S`` 0.8 s): a clip that short
  gives one beat at best, with no room to trim. The median share keeps a deliberate set of
  quick clips (every clip ~1 s) whole: none of them is far shorter than the others. Plaza:
  median 14.0 s, so 0.97 s and 0.2 s are too short and the 11.6 s bus clip is not.

Only the creator's own video clips are judged: photos have no length to be short, and a
Visuals or library clip (``asset-`` ids) is not a take (``clip_scope._own_videos``). When any
own clip's length is unknown, nothing is decided here and the ask goes to the resolver as
before (it may ask with the picker).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Protocol

from app.schemas.clip_intents import (
    LENGTH_EVIDENCE,
    ClipAssignment,
    ClipIntent,
    ResolvedClipIntent,
)

TOO_SHORT_MAX_S = 1.5
TOO_SHORT_MEDIAN_SHARE = 0.25


class _Clip(Protocol):
    media_id: str
    kind: str
    duration_s: float | None


def _own_videos(clips: Sequence[_Clip]) -> list[_Clip]:
    return [c for c in clips if c.kind == "video" and not c.media_id.startswith("asset-")]


def too_short_clips(clips: Sequence[_Clip], *, under_s: float | None = None) -> list[str] | None:
    """The own video clips too short to use, in clip order; None when a length is unknown."""
    videos = _own_videos(clips)
    if not videos or any(not c.duration_s or c.duration_s <= 0 for c in videos):
        return None
    lengths = [float(c.duration_s or 0) for c in videos]
    if under_s is not None:
        return [c.media_id for c, length in zip(videos, lengths, strict=True) if length < under_s]
    ceiling = min(TOO_SHORT_MAX_S, TOO_SHORT_MEDIAN_SHARE * statistics.median(lengths))
    return [c.media_id for c, length in zip(videos, lengths, strict=True) if length < ceiling]


def resolve_too_short(intent: ClipIntent, clips: Sequence[_Clip]) -> ResolvedClipIntent | None:
    """The ``by_length`` exclusion settled from clip lengths, or None to leave it to the
    resolver. An empty match is a real answer (no clip is too short), never a question."""
    if (
        intent.op != "exclude"
        or intent.by_length != "too_short"
        or not intent.accepts_source_kind("video")
    ):
        return None
    short = too_short_clips(clips, under_s=intent.under_s)
    if short is None:
        return None
    return ResolvedClipIntent(
        **intent.model_dump(),
        status="resolved",
        assignments=[
            ClipAssignment(media_id=media_id, evidence=LENGTH_EVIDENCE, confidence=1.0)
            for media_id in short
        ],
    )


__all__ = [
    "TOO_SHORT_MAX_S",
    "TOO_SHORT_MEDIAN_SHARE",
    "resolve_too_short",
    "too_short_clips",
]
