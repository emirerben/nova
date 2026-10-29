"""A clip that keeps its sound because the creator named a line said in it (KRI-1028).

Plaza de Mayo M1 (prod thread A228E85B, job 97d55f9a): "Only the clip where I say 'we are
going to Recoleta' keeps its sound, mute the rest." The brief recorded the clip
(``sound_except: [{"sound_media": ...}]``) and the clip's window was seated on the timed
sentence the ask describes (`spoken_quotes.described_lines`, KRI-681). Two things were left to
chance: the length plan floors that window at 4 s, so a shorter video cuts the sentence from
its end, and the receipt only checked that the clip was audible, never that its cut holds the
sentence ("met (10.5-15.8 s)" whatever the cut).

* ``kept_sound_clips`` -- the clips a sound ask keeps audible, each with the line the ask names
  it by (the creator's words that clip says, `spoken_quotes.said_line`, else the timed sentence
  the ask describes) and that timed sentence. A clip kept without a line is left alone.
* ``kept_line_records`` -- one plan record per such clip: the sentence's source seconds and the
  window that plays it (the same padded window `described_lines` seats).

`length_planning` keeps each window whole while the silent clips share the rest, and
`clip_sound_toggle` checks that the finished cut holds the sentence, so the receipt can say so.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.services.spoken_quotes import (
    _padded_window,
    _row_analysis,
    _said_in_clip,
    _video_rows,
    described_lines,
    said_line,
)

if TYPE_CHECKING:
    from app.kria.brief import CreativeBrief

# A cut holds the sentence when it misses at most this much at either edge (frame rounding).
HELD_TOLERANCE_S = 0.06


@dataclass(frozen=True)
class KeptClip:
    """A clip a sound ask keeps audible, the line the ask names it by and where it is said."""

    requirement_id: str
    media_id: str
    line: str
    sentence: tuple[float, float]  # source seconds of the timed sentence that says it


def kept_sound_clips(
    brief: CreativeBrief | None,
    media_snapshot: Mapping[str, Any] | None,
    *,
    skip_ids: Sequence[str] = (),
) -> list[KeptClip]:
    """Each clip a sound ask keeps audible by naming what is said in it, first ask first.

    The parts that keep a clip's sound are an "off" ask's exceptions and an "on" ask's own
    target, when they name a clip and no line or seconds of their own (those play only where
    they say, `clip_sound_toggle`). The ask's words name the line: the run of them that clip's
    transcript says, else the timed sentence of that clip the ask describes. ``skip_ids`` (a
    held closing line) are never touched: that clip's window belongs to the ending.
    """
    from app.kria.clip_sound_toggle_ask import SOUND_OFF, brief_sound_toggle_asks  # noqa: PLC0415
    from app.pipeline.clip_sound_toggle import _media_match  # noqa: PLC0415

    rows = {str(row["media_id"]): row for row in _video_rows(media_snapshot, skip_ids)}
    out: list[KeptClip] = []
    for ask in brief_sound_toggle_asks(brief):
        parts = ask.exceptions if ask.state == SOUND_OFF else (ask.target,)
        texts = [t for t in (ask.requirement.description, ask.requirement.literal) if t]
        for part in parts:
            if part.media_ref is None or part.lines or part.has_window:
                continue
            media_id = _media_match(part.media_ref, list(rows))
            if media_id is None or any(kept.media_id == media_id for kept in out):
                continue
            row = rows[media_id]
            analysis = _row_analysis(row)
            line = next((said for text in texts if (said := said_line(text, analysis))), None)
            sentence = _said_in_clip(line, row) if line else None
            if sentence is None:
                described = described_lines(texts, {"clip_assignments": [row]}).get(media_id)
                if described is None:
                    continue
                sentence = described.line
                line = line or texts[0]
            out.append(KeptClip(ask.requirement_id, media_id, str(line), sentence))
    return out


def kept_line_records(
    brief: CreativeBrief | None,
    media_snapshot: Mapping[str, Any] | None,
    *,
    skip_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The length plan's ``kept_lines``: where each kept clip's sentence is and what plays it.

    ``start_s`` / ``end_s`` are the timed sentence and ``window_start_s`` / ``window_end_s``
    the window seated on it (source seconds), padded as `spoken_quotes.described_lines` pads.
    """
    durations = {
        str(row["media_id"]): float(row["duration_s"])
        for row in _video_rows(media_snapshot, skip_ids)
    }
    out: list[dict[str, Any]] = []
    for kept in kept_sound_clips(brief, media_snapshot, skip_ids=skip_ids):
        start, end = kept.sentence
        window = _padded_window(durations[kept.media_id], start, end)
        out.append(
            {
                "requirement_id": kept.requirement_id,
                "media_id": kept.media_id,
                "line": kept.line,
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "window_start_s": window[0],
                "window_end_s": window[1],
            }
        )
    return out


def kept_line_held(record: Mapping[str, Any], windows: Sequence[Mapping[str, Any]]) -> bool:
    """True when one of ``windows`` (source seconds) of the record's clip holds its sentence."""
    start, end = record.get("start_s"), record.get("end_s")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return False
    return any(
        w.get("media_id") == record.get("media_id")
        and float(w.get("source_start_s", 0.0)) <= start + HELD_TOLERANCE_S
        and float(w.get("source_end_s", 0.0)) >= end - HELD_TOLERANCE_S
        for w in windows
        if isinstance(w, Mapping)
    )


__all__ = [
    "HELD_TOLERANCE_S",
    "KeptClip",
    "kept_line_held",
    "kept_line_records",
    "kept_sound_clips",
]
