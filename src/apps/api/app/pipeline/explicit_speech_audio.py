"""Audio-edge guards for explicit spoken-word removals.

Native ``AudioCutHandles`` normally borrows 25 ms from each side of a same-
source cut.  That is desirable for automatic pause cleanup, but would replay a
creator-selected spoken word.  An authored positive fade disables that borrow.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.kria.recipes_v2 import EditRecipeV2

EXPLICIT_SPEECH_AUDIO_FADE_S = 0.005
_EDGE_TOLERANCE_S = 0.001
_BORROW_HANDLE_S = 0.025


def _applied_spans(cuts: Sequence[Mapping[str, Any]] | None) -> tuple[tuple[float, float], ...]:
    """Read only durable, applied source spans; malformed receipts are ignored."""
    spans: list[tuple[float, float]] = []
    for cut in cuts or ():
        if cut.get("status") != "applied":
            continue
        span = cut.get("source_span")
        start = span.get("start_s") if isinstance(span, Mapping) else cut.get("source_start_s")
        end = span.get("end_s") if isinstance(span, Mapping) else cut.get("source_end_s")
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        try:
            start_s, end_s = float(start), float(end)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(start_s) and math.isfinite(end_s) and 0 <= start_s < end_s:
            spans.append((start_s, end_s))
    return tuple(spans)


def _source_identity(recipe: EditRecipeV2, asset_id: str) -> str | None:
    asset = next((item for item in recipe.asset_manifest.assets if item.id == asset_id), None)
    if asset is None:
        return None
    # Original footage uses media_id; a processed derivative retains the
    # original source_media_id. This makes the same edge guard apply to both.
    value = getattr(asset, "media_id", None) or getattr(asset, "source_media_id", None)
    return value if isinstance(value, str) else None


def _can_borrow_into_selected_gap(edge: float, selected_edge: float, *, outgoing: bool) -> bool:
    """Whether the native 25 ms handle could reach selected spoken audio."""
    distance = (selected_edge - edge) if outgoing else (edge - selected_edge)
    return -_EDGE_TOLERANCE_S <= distance <= _BORROW_HANDLE_S + _EDGE_TOLERANCE_S


def _fade_for(clip_duration_s: float, existing: float | None) -> float:
    return min(
        max(existing or 0, min(EXPLICIT_SPEECH_AUDIO_FADE_S, clip_duration_s)), clip_duration_s
    )


def protect_explicit_speech_cut_audio(
    recipe: EditRecipeV2,
    *,
    source_media_id: str,
    cuts: Sequence[Mapping[str, Any]] | None,
) -> EditRecipeV2:
    """Add <=5 ms in-kept fades only at applied explicit-cut source edges.

    The caller supplies the authoritative bound source identity, so a receipt
    can never fade an unrelated take with coincident timestamps.  If no edge
    qualifies, the original recipe object is returned unchanged.
    """
    spans = _applied_spans(cuts)
    if not source_media_id or not spans:
        return recipe

    changed = False
    tracks = []
    for track in recipe.tracks:
        if track.kind not in {"video", "audio"}:
            tracks.append(track)
            continue
        clips = []
        track_changed = False
        for clip in track.clips:
            if _source_identity(recipe, clip.source_asset_id) != source_media_id:
                clips.append(clip)
                continue
            updates: dict[str, float] = {}
            source_end = clip.source_start + clip.source_duration
            if any(
                _can_borrow_into_selected_gap(source_end, start, outgoing=True)
                for start, _end in spans
            ):
                updates["audio_fade_out"] = _fade_for(clip.source_duration, clip.audio_fade_out)
            if any(
                _can_borrow_into_selected_gap(clip.source_start, end, outgoing=False)
                for _start, end in spans
            ):
                updates["audio_fade_in"] = _fade_for(clip.source_duration, clip.audio_fade_in)
            if updates:
                clips.append(clip.model_copy(update=updates))
                track_changed = changed = True
            else:
                clips.append(clip)
        tracks.append(track.model_copy(update={"clips": clips}) if track_changed else track)
    return recipe.model_copy(update={"tracks": tracks}) if changed else recipe
