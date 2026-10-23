"""Creator text timed to words in a recorded phone voiceover.

The narrated timeline is the original narration's clock.  This module never
infers a moment from captions or from the accompanying B-roll: it needs a
unique spoken-word match, unless the structured ask explicitly chooses an
occurrence.  It has no sampled face evidence for arbitrary narrated B-roll, so
it protects the existing conservative fallback face unless a caller supplies
known regions, and records that limited evidence honestly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import structlog

from app.kria.spoken_text_ask import SpokenTextAsk
from app.pipeline.phone_spoken_text import (
    SPOKEN_TEXT_FIELD,
    _text_boxes,
    caption_boxes,
    find_spoken_word_matches,
    spoken_cue_length,
    spoken_text_element,
)
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

_MAX_CHARS = 80


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


def _ask_occurrence(ask: SpokenTextAsk) -> str | int | None:
    """Read an optional structured occurrence without making prose a contract.

    Keeping this tolerant lets older persisted asks continue to work while the
    structured brief field chooses ``first``, ``last`` or a one-based integer
    without interpreting words such as "second" from the description.
    """
    value = getattr(ask, "occurrence", None)
    if isinstance(value, str) and value in {"first", "last"}:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _selected_match(
    ask: SpokenTextAsk, words: Sequence[Any]
) -> tuple[str, float, float] | tuple[None, str]:
    # An ask's triggers are alternative cues for one moment, not a list of moments: N2's
    # "1" and "leave before 11" both name rule one (PR #1758). The longest cue heard
    # decides; a shorter one counts only when no longer cue is heard at all, so a bare
    # number never outvotes its phrase and a phrase said twice is never hidden behind it.
    by_length: dict[int, list[str]] = {}
    for trigger in ask.triggers:
        by_length.setdefault(spoken_cue_length(trigger), []).append(trigger)
    matches: list[tuple[str, float, float]] = []
    for length in sorted(by_length, reverse=True):
        matches = find_spoken_word_matches(words, by_length[length])
        if matches:
            break
    if not matches:
        return None, "not_heard"
    occurrence = _ask_occurrence(ask)
    if occurrence == "first":
        return matches[0]
    if occurrence == "last":
        return matches[-1]
    if isinstance(occurrence, int):
        return matches[occurrence - 1] if occurrence <= len(matches) else (None, "not_heard")
    if len(matches) != 1:
        return None, "ambiguous"
    return matches[0]


def _place_clear_of_text(
    row: dict,
    avoid: Sequence[Any],
    *,
    canvas: _Canvas,
    face_regions: Sequence[NormalizedBox] | None,
) -> tuple[dict | None, dict]:
    """Keep clear of known text and conservative or supplied face regions."""
    from app.pipeline.phone_subtitled_title import (  # noqa: PLC0415
        _FALLBACK_FACE_BOX,
        _measure_title_box,
        _translate_centered_box_to_y,
        choose_title_y_frac,
    )

    faces = list(face_regions) if face_regions is not None else [_FALLBACK_FACE_BOX]
    face_protection = "provided" if face_regions is not None else "fallback"
    default_y = float(row.get("y_frac") or 0.15)
    probe = _translate_centered_box_to_y(_measure_title_box(row, canvas=canvas), default_y)
    chosen, decision = choose_title_y_frac(probe, default_y, faces, avoid=avoid)
    if decision.get("status") not in {"preset", "moved", "moved_below_face"}:
        return None, {
            "status": "no_clear_text_space",
            "face_protection": face_protection,
            "default_y_frac": default_y,
        }
    placed = dict(row)
    placed["y_frac"] = round(chosen, 4)
    return placed, {"status": "clear_of_text", "face_protection": face_protection, **decision}


def narrated_spoken_text_rows(
    asks: Sequence[SpokenTextAsk],
    *,
    words: Sequence[Any],
    cues: Sequence[dict],
    caption_style: str,
    timeline_duration_s: float,
    canvas: _Canvas,
    shown_rows: Sequence[dict] = (),
    face_regions: Sequence[NormalizedBox] | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Build editable text rows and one honest placement receipt per ask.

    ``words`` must be the original recorded narration's timings, expressed on
    the narrated recipe clock.  ``shown_rows`` contains the opening-title and
    any caller-owned rows already visible on that clock.
    """
    rows: list[dict] = []
    receipt_rows: list[dict[str, Any]] = []
    for ask in asks:
        entry: dict[str, Any] = {
            "requirement_id": ask.requirement_id,
            "text": ask.text,
            "triggers": list(ask.triggers),
        }
        if ask.speaker:
            entry["speaker"] = ask.speaker
        occurrence = _ask_occurrence(ask)
        if occurrence is not None:
            entry["occurrence"] = occurrence
        if ask.speaker:
            # Word timings do not identify speakers.  Unlike Talking's legacy
            # first-hit fallback, a narration is an authored audio track and
            # must not claim that its selected word belongs to someone named.
            receipt_rows.append({**entry, "status": "unsupported_speaker"})
            continue
        text = " ".join(ask.text.split())
        if len(text) > _MAX_CHARS:
            receipt_rows.append({**entry, "status": "text_too_long"})
            continue
        try:
            selected = _selected_match(ask, words)
            if selected[0] is None:
                receipt_rows.append({**entry, "status": selected[1]})
                continue
            trigger, start_s, _end_s = selected
            row = spoken_text_element(
                ask, start_s=start_s, timeline_duration_s=timeline_duration_s, canvas=canvas
            )
            if row is None:
                receipt_rows.append({**entry, "status": "no_time", "heard_at_s": start_s})
                continue
            window = (float(row["start_s"]), float(row["end_s"]))
            avoid = [*_text_boxes([*shown_rows, *rows], window=window, canvas=canvas)]
            if cues:
                try:
                    avoid += caption_boxes(
                        cues, window=window, caption_style=caption_style, canvas=canvas
                    )
                except Exception as exc:  # noqa: BLE001 - never place across unmeasured captions
                    log.warning(
                        "phone_narrated_spoken_text.caption_boxes_failed", error=str(exc)[:200]
                    )
                    receipt_rows.append(
                        {**entry, "status": "error", "error": "caption_boxes_failed"}
                    )
                    continue
            placed, placement = _place_clear_of_text(
                row, avoid, canvas=canvas, face_regions=face_regions
            )
            if placed is None:
                receipt_rows.append(
                    {**entry, "status": "no_clear_text_space", "placement": placement}
                )
                continue
        except Exception as exc:  # noqa: BLE001 - one ask must not abort a render
            log.warning("phone_narrated_spoken_text.failed", error=str(exc)[:200])
            receipt_rows.append({**entry, "status": "error", "error": str(exc)[:200]})
            continue
        rows.append(placed)
        receipt_rows.append(
            {
                **entry,
                "status": "placed",
                "trigger": trigger,
                "element_id": placed["id"],
                "start_s": placed["start_s"],
                "end_s": placed["end_s"],
                "placement": placement,
            }
        )
    return rows, {"version": 1, "rows": receipt_rows}


__all__ = ["SPOKEN_TEXT_FIELD", "narrated_spoken_text_rows"]
