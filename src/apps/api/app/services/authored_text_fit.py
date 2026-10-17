"""Fit a Save's text lane onto the authored cut's new total (KRI-736).

A clip delete/trim shortens the timeline, and ``EditRecipeV2`` rejects any text layer that
ends past it with a zero-tolerance compare. The current phone build re-times its labels
(``GuidedLabelRebase``), but builds that predate that, a stale draft, or a label whose
window the client could not re-anchor still reach the server with a window on the OLD
clock. The recipe compiler would then silently drop the text (``render_presentation``) and
the creator contract would 422 "missing confirmed on-screen text", or the Save would hard
fail. This module is the server-side guarantee that a Save never ships a text window past
the new total: it clamps a small overshoot, re-anchors a window that is wholly past the end
to the footage it was on, and drops text only when ALL of that footage was deleted.

Exempt, exactly like ``project_lane_times`` / ``GuidedLabelRebase``: caption cues, lyric
bars and narration labels (they live on the narration clock, not the cut's).
"""

from __future__ import annotations

from typing import Any

from app.agents._schemas.text_element import CAPTION_CUE_SOURCE
from app.services.guided_timeline import GUIDED_FPS, make_time_projector
from app.services.kria_editor_timeline import _is_edge_titled, _project_span

_FRAME_S = 1.0 / GUIDED_FPS
# An overshoot this small (3 frames) is a late edge, not an old-clock window.
_CLAMP_OVERSHOOT_S = 0.1
_MIN_WINDOW_S = 0.1
_EPS = 1e-6


def _segments(rows: list[dict]) -> list[dict]:
    """Output-clock segments of the visible rows, with the compiler's crossfade overlap."""
    segments: list[dict] = []
    cursor = 0.0
    previous: dict | None = None
    for row in rows:
        if row.get("removed"):
            continue
        duration = float(row.get("duration_s") or 0.0)
        rate = float(row.get("playback_rate") or 1.0)
        if duration <= 0 or rate <= 0:
            continue
        if previous is not None and (previous.get("transition_after") or "cut") != "cut":
            overlap = min(
                float(previous.get("transition_duration_s") or 0.3),
                duration * 0.3,
                float(previous.get("duration_s") or duration) * 0.3,
                0.3,
            )
            if overlap >= 0.1:
                cursor -= overlap
        source_start = float(row.get("in_s") or 0.0)
        slot_id = str(row.get("slot_id") or row.get("segment_id") or len(segments))
        segments.append(
            {
                "segment_id": slot_id,
                "media_id": str(row.get("media_id") or row.get("clip_index")),
                "output_start_s": cursor,
                "output_end_s": cursor + duration,
                "duration_s": duration,
                "source_start_s": source_start,
                "source_end_s": source_start + duration * rate,
            }
        )
        cursor += duration
        previous = row
    return segments


def _is_exempt(element: dict) -> bool:
    params = element.get("source_params")
    params = params if isinstance(params, dict) else {}
    label_kind = params.get("narration_label_kind")
    return (
        params.get("source") == CAPTION_CUE_SOURCE
        or (label_kind is not None and label_kind != "participant")
        or element.get("role") == "lyric_line"
        or str(element.get("id") or "").startswith("lyric_")
    )


def fit_text_to_timeline(
    elements: list[dict], *, old_rows: list[dict], new_rows: list[dict]
) -> list[dict]:
    """Return ``elements`` with every window inside ``[0, new total]``.

    Windows that already fit are returned untouched (the same dict objects), so a Save
    whose text was re-timed correctly is byte-identical.
    """
    new_segments = _segments(new_rows)
    total = max((s["output_end_s"] for s in new_segments), default=0.0)
    if total <= 0:
        return elements
    old_segments = _segments(old_rows)
    old_total = max((s["output_end_s"] for s in old_segments), default=0.0)
    project = make_time_projector(old_segments, new_segments) if old_segments else None

    def readable(element: dict) -> tuple[float, float] | None:
        try:
            return float(element["start_s"]), float(element["end_s"])
        except (KeyError, TypeError, ValueError):
            return None

    def follows_clips(element: dict) -> bool:
        return not _is_exempt(element) and not _is_edge_titled(str(element.get("id") or ""))

    # A re-timed lane never ends meaningfully past the cut. One clip-following text that
    # does proves the whole lane is still on the pre-edit clock, so a sibling that happens
    # to sit inside the new total (the label of the clip now second-to-last) is stale too.
    stale_lane = any(
        (window := readable(e)) is not None
        and follows_clips(e)
        and window[1] > total + _CLAMP_OVERSHOOT_S
        for e in elements
    )
    fitted: list[dict] = []
    for element in elements:
        window_s = readable(element)
        if window_s is None or _is_exempt(element):
            fitted.append(element)
            continue
        start, end = window_s
        edge_titled = _is_edge_titled(str(element.get("id") or ""))
        if end <= total + _EPS and not (stale_lane and follows_clips(element)):
            fitted.append(element)
            continue
        overshoot = end - total
        length = max(end - start, _MIN_WINDOW_S)
        if (
            end > total + _EPS
            and start < total - _FRAME_S
            and (overshoot <= _CLAMP_OVERSHOOT_S or edge_titled)
        ):
            fitted.append({**element, "end_s": round(total, 6)})
            continue
        # The window is on the pre-edit clock: follow the footage it was on.
        on_old_clock = project is not None and start < old_total - _EPS
        span = (
            _project_span(project, old_segments, start, min(end, old_total))
            if on_old_clock
            else None
        )
        if span is not None:
            new_start = min(span[0][0], total - _MIN_WINDOW_S)
            new_end = min(total, max(span[1][0] + _FRAME_S, new_start + _MIN_WINDOW_S))
            fitted.append({**element, "start_s": round(new_start, 6), "end_s": round(new_end, 6)})
        elif on_old_clock:
            # Every clip under the text was deleted: it goes with its footage.
            continue
        else:
            new_start = max(0.0, total - min(length, total))
            fitted.append({**element, "start_s": round(new_start, 6), "end_s": round(total, 6)})
    return fitted


def authored_text_fit_inputs(job: Any, variant: dict) -> list[dict]:
    """The pre-edit visible placements, on the server's authoritative clock."""
    from app.routes.generative_jobs import editor_deletion_timeline  # noqa: PLC0415

    return [row for row in editor_deletion_timeline(job, variant) if not row.get("removed")]
