"""Spoken-rule text for phone Narrated edits.

Recorded voiceover words are the timing authority for this lane.  The rows are
compiled into the same title layer as the opening title, which keeps the
device recipe and the editor's persisted rows in sync.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.kria.spoken_text_ask import SpokenTextAsk
from app.pipeline.phone_spoken_text import spoken_text_rows
from app.pipeline.render_geometry import (
    NormalizedBox,
    _translate_centered_box_to_y,
)

# The opening title owns the top band and captions own the bottom band.  These
# middle-band candidates keep narrated montage text away from both while still
# allowing more than one rule to appear without overlap.
_Y_CANDIDATES = (0.38, 0.50, 0.62)


def _overlaps(left: NormalizedBox, right: NormalizedBox) -> bool:
    return (
        left.left < right.right
        and left.right > right.left
        and left.top < right.bottom
        and left.bottom > right.top
    )


def _place_narrated_row(
    row: dict, avoid: Sequence[NormalizedBox], *, canvas: Any
) -> tuple[dict, dict[str, Any]]:
    """Choose a middle-band position clear of captions and earlier text rows."""
    from app.pipeline.phone_subtitled_title import _measure_title_box

    probe = _measure_title_box(row, canvas=canvas)
    for y_frac in _Y_CANDIDATES:
        box = _translate_centered_box_to_y(probe, y_frac)
        if (
            box.left < 0
            or box.right > 1
            or box.top < 0
            or box.bottom > 1
            or any(_overlaps(box, other) for other in avoid)
        ):
            continue
        placed = {**row, "position": "custom", "x_frac": 0.5, "y_frac": y_frac}
        return placed, {
            "status": "placed",
            "y_frac": y_frac,
            "placement": "narrated_middle_band",
        }
    raise ValueError("no narrated spoken-text position clear of captions or other text")


def compile_narrated_spoken_text(
    asks: Sequence[SpokenTextAsk],
    *,
    words: Sequence[Any],
    cues: Sequence[dict],
    caption_style: str,
    timeline_duration_s: float,
    canvas: Any,
    shown_rows: Sequence[dict] = (),
) -> tuple[list[dict], dict[str, Any]]:
    """Compile spoken-rule rows from recorded voiceover words.

    ``sequential=True`` makes the shared compiler bound each rule to the next
    spoken cue and reject same-time collisions, preserving the narrated
    montage's conservative one-at-a-time placement contract.
    """

    return spoken_text_rows(
        asks,
        words=words,
        cues=cues,
        caption_style=caption_style,
        timeline_duration_s=timeline_duration_s,
        canvas=canvas,
        place=lambda row, avoid: _place_narrated_row(row, avoid, canvas=canvas),
        shown_rows=shown_rows,
        sequential=True,
    )


__all__ = ["compile_narrated_spoken_text"]
