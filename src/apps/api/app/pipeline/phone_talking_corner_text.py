"""Corner text on a phone Talking edit (KRI-1342 item 7).

'A small "Yorumla" label in the top left for the whole video' (Çiğdem's series label), "keep
@handle bottom right", "sol üstte 'bölüm 8' yazsın": the planner already records these as the
strategy's ``pinned_texts`` (KRI-523/525: a corner, the whole video or a time range). Montage and
voiceover writers draw them; a Talking edit had no lane, so one top line became a 1-3 s opening
title and anything else was asked about. This module draws them on the Talking text lane.

* Rows come from the same builder every other writer uses (`pinned_text.pinned_text_elements`,
  ids ``guided-pinned-<i>``): the corner anchors, small size, one line, stacking per corner.
* Each row is then placed off every face the speaker take shows during the row's window: faces
  are sampled in short slices across the WHOLE window (`caption_window_faces`), not at the first
  seconds, and every sampled face is protected, not only the dominant one. The row keeps its
  corner: it steps inward from the frame edge (down from the top, up from the bottom), then
  shrinks; it also keeps clear of the captions in its window when any spot allows it. A row
  that cannot clear a face anywhere in its corner is dropped (receipt ``face_blocked``), never
  drawn on the face.
* A whole-video row is tagged ``whole_video`` so the compiler stretches it to the video's real
  end (after an ending clip or the closing card); a ranged row keeps its own window.

The rows are fixed in the shared style pass (their spot was chosen off the faces; a restyle
that grew them could undo that). No new recipe field: every app build draws them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import structlog

from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

CORNER_TEXT_SOURCE = "corner_text"
_SLICE_S = 2.0
_STEP = 0.02
_MAX_INWARD = 0.2
_SHRINK = (1.0, 0.85, 0.72, 0.6)
_MAX_FACE_COVERAGE = 0.0
_MAX_CAPTION_COVERAGE = 0.05
_FACE_PAD = 0.01


def is_corner_text_row(row: dict) -> bool:
    return (row.get("source_params") or {}).get("source") == CORNER_TEXT_SOURCE


def corner_text_rows(pins: Sequence[Any], *, talk_end_s: float, canvas: Any) -> list[dict]:
    """The pins as tagged text rows on the talk's timeline (before placement)."""
    from app.pipeline.pinned_text import pinned_text_elements, resolve_pin_windows  # noqa: PLC0415

    windows = resolve_pin_windows(pins, talk_end_s)
    rows = pinned_text_elements(windows, font_family=None, text_color=None, canvas=canvas)
    tagged = []
    for window, row in zip(windows, rows, strict=True):
        tagged.append(
            {
                **row,
                "source_params": {
                    **(row.get("source_params") or {}),
                    "source": CORNER_TEXT_SOURCE,
                    "corner": window.pin.corner,
                    "whole_video": window.pin.is_whole_video,
                },
            }
        )
    return tagged


def _slices(start_s: float, end_s: float) -> list[tuple[float, float]]:
    out = []
    at = start_s
    while at < end_s - 1e-6:
        out.append((round(at, 3), round(min(end_s, at + _SLICE_S), 3)))
        at += _SLICE_S
    return out or [(start_s, end_s)]


def _clear(box: NormalizedBox, others: Sequence[NormalizedBox], limit: float) -> bool:
    return all(box.coverage_by(other) <= limit for other in others)


def place_corner_row(
    row: dict,
    *,
    faces: Sequence[NormalizedBox],
    captions: Sequence[NormalizedBox],
    measure: Callable[[dict], NormalizedBox],
) -> tuple[dict | None, dict[str, Any]]:
    """``row`` at the first spot in its corner clear of every face (and of the captions when
    any spot allows), stepping inward then shrinking; ``(None, receipt)`` when no spot in the
    corner clears the faces."""
    corner = str((row.get("source_params") or {}).get("corner") or "top_left")
    zone = corner.split("_", 1)[0]
    direction = {"top": 1.0, "bottom": -1.0}.get(zone, 0.0)
    padded = [face.padded(_FACE_PAD) for face in faces]
    base_y = float(row["y_frac"])
    base_size = float(row.get("size_px") or 52)
    face_only: tuple[dict, NormalizedBox, float, float] | None = None
    steps = int(round(_MAX_INWARD / _STEP)) + 1 if direction else 1
    for shrink in _SHRINK:
        for step in range(steps):
            y = round(base_y + direction * step * _STEP, 4)
            candidate = {**row, "y_frac": y, "size_px": round(base_size * shrink, 1)}
            box = measure(candidate)
            if box.top < 0.0 or box.bottom > 1.0:
                continue
            if not _clear(box, padded, _MAX_FACE_COVERAGE):
                continue
            if _clear(box, captions, _MAX_CAPTION_COVERAGE):
                return candidate, {
                    "status": "placed",
                    "corner": corner,
                    "y_frac": y,
                    "default_y_frac": base_y,
                    "shrink": shrink,
                    "box": box.as_dict(),
                    "moved_off": ["face"] if (step or shrink != 1.0) else [],
                    "text_overlap": False,
                }
            if face_only is None:
                face_only = (candidate, box, y, shrink)
    if face_only is not None:
        candidate, box, y, shrink = face_only
        return candidate, {
            "status": "placed",
            "corner": corner,
            "y_frac": y,
            "default_y_frac": base_y,
            "shrink": shrink,
            "box": box.as_dict(),
            "moved_off": ["face"] if (y != base_y or shrink != 1.0) else [],
            "text_overlap": True,
        }
    return None, {"status": "face_blocked", "corner": corner, "default_y_frac": base_y}


def place_corner_text(
    rows: Sequence[dict],
    *,
    clip_path: str | None,
    keep_segments: Sequence[tuple[float, float]],
    display_width: float,
    display_height: float,
    canvas: Any,
    scale: float,
    position_x: float = 0.0,
    caption_boxes: Callable[[tuple[float, float]], Sequence[NormalizedBox]] | None = None,
    sample: Any = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Every corner row placed off the faces of its own window; the rows kept and a receipt
    (one entry per row, plus the face-sampling receipt). Never raises: a sampling failure
    protects the conservative fallback face, as the title placement does."""
    from app.pipeline import phone_subtitled_title as pst  # noqa: PLC0415

    def measure(candidate: dict) -> NormalizedBox:
        return pst._measure_title_box(candidate, canvas=canvas)

    placed: list[dict] = []
    entries: list[dict[str, Any]] = []
    sampling: dict[str, Any] = {}
    for row in rows:
        window = (float(row["start_s"]), float(row["end_s"]))
        try:
            faces, _bands, sampling = pst.caption_window_faces(
                clip_path,
                windows=_slices(*window),
                keep_segments=keep_segments,
                display_width=display_width,
                display_height=display_height,
                canvas=canvas,
                scale=scale,
                sample=sample or pst.sample_face_regions,
                position_x=position_x,
            )
            captions = list(caption_boxes(window)) if caption_boxes else []
            kept, entry = place_corner_row(row, faces=faces, captions=captions, measure=measure)
        except Exception as exc:  # noqa: BLE001 - never draw unplaced text on a face
            log.warning("corner_text_placement_failed", error=str(exc)[:200])
            kept, entry = None, {"status": "error", "error": str(exc)[:200]}
        entries.append({"id": row["id"], "text": row["text"], **entry})
        if kept is not None:
            placed.append(kept)
    return placed, {"rows": entries, **{k: v for k, v in sampling.items() if k != "windows"}}
