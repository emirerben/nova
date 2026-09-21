"""Ground, clamp and space the camera_emphasis agent's picks (KRI-7).

The agent proposes moments; THIS module disposes. Nothing here trusts a time
from the model: every emphasis is re-derived from the candidate window the
caller offered, then clamped to the easing's bounds, spaced apart, and capped.

`plan_camera_emphasis` is the single entry point used by the render path. It is
failure-isolated by construction: any missing key, agent error, or empty answer
returns the deterministic preset intents the compiler already produced, so the
worst case is exactly the behavior that shipped before this module existed.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.config import settings
from app.pipeline.camera_effects import (
    CAMERA_EFFECT_EASING_HOLD,
    CAMERA_EFFECT_EASING_PULSE,
    CAMERA_EFFECT_TOKEN,
    easing_bounds,
)

log = structlog.get_logger()

# One emphasis per ~10s of edit, never more than 4 — a push-in is punctuation,
# and punctuation everywhere is noise. Short edits still get one.
_SECONDS_PER_EMPHASIS = 10.0
_MAX_EMPHASES = 4
# Clear air between two emphases, measured end→start.
_MIN_GAP_S = 1.0
# A hold longer than this stops reading as emphasis and starts reading as a
# crop change, however long the phrase runs.
_MAX_HOLD_S = 4.0
_STRENGTH_INTENSITY = {"subtle": 0.04, "standard": 0.06, "strong": 0.08}
_STYLE_EASING = {"zoom_in": CAMERA_EFFECT_EASING_HOLD, "pulse": CAMERA_EFFECT_EASING_PULSE}
# Phrases this short carry no emphasis (and would render as a twitch).
_MIN_CANDIDATE_S = 0.35


def max_emphases_for(duration_s: float) -> int:
    if duration_s <= 0:
        return 0
    return max(1, min(_MAX_EMPHASES, int(duration_s // _SECONDS_PER_EMPHASIS)))


def build_camera_candidates(
    cues: list[dict[str, Any]] | None,
    *,
    preset_intents: list[dict[str, Any]] | None = None,
    duration_s: float,
) -> list[dict[str, Any]]:
    """Offer one candidate window per spoken phrase, in timeline order."""

    preset_windows = [
        (
            float(intent.get("start_s", intent.get("at_s", 0.0)) or 0.0),
            str(intent.get("role") or ""),
        )
        for intent in preset_intents or []
        if isinstance(intent, dict)
    ]
    candidates: list[dict[str, Any]] = []
    for cue in cues or []:
        if not isinstance(cue, dict):
            continue
        try:
            start = max(0.0, float(cue.get("start_s")))
            end = min(float(duration_s), float(cue.get("end_s")))
        except (TypeError, ValueError):
            continue
        if end - start < _MIN_CANDIDATE_S:
            continue
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        # A preset pick inside this phrase both flags it and lends its role.
        role = ""
        preset_pick = False
        for preset_start, preset_role in preset_windows:
            if start <= preset_start <= end:
                preset_pick = True
                role = role or preset_role
                break
        candidates.append(
            {
                "index": len(candidates),
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "text": text,
                "role": role,
                "preset_pick": preset_pick,
            }
        )
    return candidates


def _field(emphasis: Any, name: str) -> Any:
    """Read one field off an agent row — a Pydantic model or a plain dict."""

    if isinstance(emphasis, dict):
        return emphasis.get(name)
    return getattr(emphasis, name, None)


def materialize_camera_emphasis(
    emphases: list[Any] | None,
    *,
    candidates: list[dict[str, Any]],
    duration_s: float,
    max_effects: int | None = None,
) -> list[dict[str, Any]]:
    """Turn agent picks into camera intents, grounded in the offered windows."""

    by_index = {int(candidate["index"]): candidate for candidate in candidates}
    cap = max_emphases_for(duration_s) if max_effects is None else max(0, max_effects)
    kept: list[dict[str, Any]] = []
    strong_used = False
    for emphasis in emphases or []:
        if len(kept) >= cap:
            break
        try:
            candidate = by_index[int(_field(emphasis, "candidate_index"))]
        except (TypeError, ValueError, KeyError):
            continue
        style = str(_field(emphasis, "style") or "zoom_in")
        strength = str(_field(emphasis, "strength") or "standard")
        easing = _STYLE_EASING.get(style, CAMERA_EFFECT_EASING_HOLD)
        # "strong" is the video's single biggest moment; a second one is a
        # ranking mistake, not a second peak.
        if strength == "strong" and strong_used:
            strength = "standard"
        intensity = _STRENGTH_INTENSITY.get(strength, _STRENGTH_INTENSITY["standard"])

        bounds = easing_bounds(easing)
        start = float(candidate["start_s"])
        span = float(candidate["end_s"]) - start
        if easing == CAMERA_EFFECT_EASING_HOLD:
            # Hold across the phrase, but never past the emphasis ceiling.
            window = min(max(span, bounds.min_duration_s), _MAX_HOLD_S, bounds.max_duration_s)
        else:
            window = min(
                max(min(span, bounds.default_duration_s), bounds.min_duration_s),
                bounds.max_duration_s,
            )
        end = min(float(duration_s), start + window)
        if end - start < bounds.min_duration_s:
            continue
        if any(
            start < previous["end_s"] + _MIN_GAP_S and end > previous["start_s"] - _MIN_GAP_S
            for previous in kept
        ):
            continue
        if strength == "strong":
            strong_used = True
        kept.append(
            {
                "event_id": f"emphasis-{int(candidate['index'])}",
                "token": CAMERA_EFFECT_TOKEN,
                "role": candidate.get("role") or "emphasis",
                "at_s": round(start, 3),
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                "easing": easing,
                "intensity": intensity,
            }
        )
    return sorted(kept, key=lambda intent: float(intent["start_s"]))


def plan_camera_emphasis(
    *,
    job_id: str | None,
    cues: list[dict[str, Any]] | None,
    preset_intents: list[dict[str, Any]] | None,
    duration_s: float,
    language: str = "en",
    visual_notes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """AI-placed camera emphasis, falling back to the preset intents."""

    fallback = list(preset_intents or [])
    if not settings.camera_emphasis_ai_enabled or not settings.gemini_api_key:
        return fallback
    cap = max_emphases_for(duration_s)
    if cap <= 0:
        return fallback
    candidates = build_camera_candidates(cues, preset_intents=preset_intents, duration_s=duration_s)
    if not candidates:
        return fallback

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents.camera_emphasis import (  # noqa: PLC0415
        MAX_CAMERA_EMPHASES,
        CameraEmphasisAgent,
        CameraEmphasisCandidate,
        CameraEmphasisInput,
    )

    try:
        output = CameraEmphasisAgent(default_client()).run(
            CameraEmphasisInput(
                candidates=[CameraEmphasisCandidate(**candidate) for candidate in candidates],
                duration_s=float(duration_s),
                max_effects=min(cap, MAX_CAMERA_EMPHASES),
                visual_notes=list(visual_notes or []),
                language_hint=language or "en",
            ),
            ctx=RunContext(job_id=job_id),
        )
    except Exception as exc:  # noqa: BLE001 — emphasis is never worth a failed render
        log.warning("camera_emphasis_failed_open", job_id=job_id, error=str(exc)[:300])
        return fallback

    intents = materialize_camera_emphasis(
        output.emphases, candidates=candidates, duration_s=duration_s, max_effects=cap
    )
    if not intents:
        # The agent read the footage and found nothing worth emphasising. That
        # is a real answer, so it wins over the preset picks.
        log.info("camera_emphasis_empty", job_id=job_id, candidates=len(candidates))
        return []
    return intents
