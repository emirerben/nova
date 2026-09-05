"""Trusted materialization helpers for explicit licensed-SFX requests."""

from __future__ import annotations

import math
import uuid
from typing import Any

from app.agents._schemas.sfx_intent import LicensedSfxIntent
from app.schemas.edit_proposal import EditProposalSnapshot

MAX_EXPLICIT_SFX_PLACEMENTS = 6
MIN_EXPLICIT_SFX_SPACING_S = 1.5
EXPLICIT_SFX_END_KEEPOUT_S = 0.5
EXPLICIT_SFX_MARK_EPSILON_S = 0.25


def canonical_effect_id(value: object) -> str:
    """Normalize catalog ids at the trusted intent/materialization boundary."""

    return str(value or "").strip().casefold()


def trusted_visual_moments(
    snapshot: EditProposalSnapshot,
    story_timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project server-owned visual descriptions onto authored output windows."""

    by_id = {ref.media_id: ref for ref in snapshot.media}
    moments: list[dict[str, Any]] = []
    for row in story_timeline:
        media_id = str(row.get("media_id") or "")
        ref = by_id.get(media_id)
        if ref is None:
            continue
        try:
            output_start_s = float(row.get("output_start_s") or 0.0)
            output_end_s = float(row.get("output_end_s") or 0.0)
            source_start_s = float(row.get("source_start_s") or 0.0)
            source_end_s = float(row.get("source_end_s") or source_start_s)
        except (TypeError, ValueError):
            continue
        if output_end_s <= output_start_s:
            continue

        analysis = ref.analysis if isinstance(ref.analysis, dict) else {}
        descriptions: list[str] = []
        raw_best = analysis.get("best_moments")
        if isinstance(raw_best, list):
            for candidate in raw_best:
                if not isinstance(candidate, dict):
                    continue
                try:
                    candidate_start = float(candidate.get("start_s") or 0.0)
                    candidate_end = float(candidate.get("end_s") or candidate_start)
                except (TypeError, ValueError):
                    continue
                if candidate_end < source_start_s or candidate_start > source_end_s:
                    continue
                description = str(candidate.get("description") or "").strip()
                if description and description not in descriptions:
                    descriptions.append(description)
        for fallback in (
            analysis.get("description"),
            analysis.get("subject"),
        ):
            description = str(fallback or "").strip()
            if description and description not in descriptions:
                descriptions.append(description)
        if not descriptions:
            continue
        moments.append(
            {
                "start_s": round(output_start_s, 3),
                "end_s": round(output_end_s, 3),
                "description": " · ".join(descriptions)[:240],
                "media_id": media_id,
            }
        )
    return moments


def materialize_explicit_sfx_placements(
    raw: list[Any],
    *,
    intent: LicensedSfxIntent,
    effect: dict[str, Any],
    visual_moments: list[dict[str, Any]],
    duration_s: float,
) -> list[dict[str, Any]]:
    """Validate grounded agent output into ordinary editor SFX placements."""

    effect_id = str(effect.get("id") or "").strip()
    audio_gcs_path = str(effect.get("audio_gcs_path") or "")
    if (
        not effect_id
        or canonical_effect_id(effect_id) != canonical_effect_id(intent.effect_id)
        or not audio_gcs_path
    ):
        return []
    marks = sorted(
        {
            round(float(moment[key]), 3)
            for moment in visual_moments
            for key in ("start_s", "end_s")
            if isinstance(moment.get(key), (int, float))
            and math.isfinite(float(moment[key]))
            and str(moment.get("description") or "").strip()
        }
    )
    if not marks:
        return []

    def value(item: Any, key: str, default: Any = None) -> Any:
        return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)

    candidates: list[tuple[float, float]] = []
    for suggestion in raw or []:
        if canonical_effect_id(value(suggestion, "effect_id")) != canonical_effect_id(effect_id):
            continue
        try:
            at_s = float(value(suggestion, "at_s"))
            gain = float(value(suggestion, "gain", 0.8))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(at_s) or not math.isfinite(gain) or at_s < 0.0:
            continue
        nearest = min(marks, key=lambda mark: abs(mark - at_s))
        if abs(nearest - at_s) > EXPLICIT_SFX_MARK_EPSILON_S:
            continue
        if nearest >= max(0.0, float(duration_s) - EXPLICIT_SFX_END_KEEPOUT_S):
            continue
        candidates.append((nearest, max(0.1, min(1.5, gain))))

    placements: list[dict[str, Any]] = []
    for at_s, gain in sorted(candidates):
        if placements and at_s - float(placements[-1]["at_s"]) < MIN_EXPLICIT_SFX_SPACING_S:
            continue
        placements.append(
            {
                "id": uuid.uuid4().hex,
                "sound_effect_id": effect_id,
                "src_gcs_path": audio_gcs_path,
                "at_s": round(at_s, 3),
                "gain": round(gain, 2),
                "duration_s": effect.get("duration_s"),
                "label": str(effect.get("name") or "")[:160] or None,
                "source": "creator_explicit",
                "smart_role": "funny_moments",
            }
        )
        if len(placements) >= min(intent.max_placements, MAX_EXPLICIT_SFX_PLACEMENTS):
            break
    return placements
