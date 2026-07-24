"""Editable semantic camera effects shared by Smart Captions and render paths."""

from __future__ import annotations

import hashlib
import math
from typing import Any

CAMERA_EFFECT_DEFAULT_DURATION_S = 1.2
CAMERA_EFFECT_MIN_DURATION_S = 0.4
CAMERA_EFFECT_MAX_DURATION_S = 2.0
CAMERA_EFFECT_DEFAULT_INTENSITY = 0.04
CAMERA_EFFECT_MIN_INTENSITY = 0.0
CAMERA_EFFECT_MAX_INTENSITY = 0.08
CAMERA_EFFECT_EASINGS = frozenset({"sine_pulse"})
CAMERA_EFFECT_TOKEN = "semantic_crop_pulse"


def _stable_camera_id(intent: dict[str, Any]) -> str:
    event_id = str(intent.get("event_id") or "").strip()
    if event_id:
        return f"camera-{event_id}"
    material = "|".join(str(intent.get(key) or "") for key in ("role", "at_s", "start_s", "end_s"))
    return "camera-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _finite_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def normalize_camera_effects(
    raw_effects: list[dict[str, Any]] | None,
    *,
    duration_s: float | None = None,
) -> list[dict[str, Any]]:
    """Validate and clamp a full replacement camera-effect list."""

    out: list[dict[str, Any]] = []
    max_end = duration_s if duration_s is not None and duration_s > 0 else None
    seen: set[str] = set()
    for raw in raw_effects or []:
        if not isinstance(raw, dict):
            continue
        effect_id = str(raw.get("id") or "").strip()
        if not effect_id or effect_id in seen:
            continue
        start = max(0.0, _finite_float(raw.get("start_s"), 0.0))
        end = _finite_float(raw.get("end_s"), start + CAMERA_EFFECT_DEFAULT_DURATION_S)
        if max_end is not None:
            start = min(start, max_end)
            end = min(end, max_end)
        min_end = start + CAMERA_EFFECT_MIN_DURATION_S
        max_effect_end = start + CAMERA_EFFECT_MAX_DURATION_S
        end = min(max(end, min_end), max_effect_end)
        if max_end is not None:
            end = min(end, max_end)
        if end <= start:
            continue
        intensity = _finite_float(raw.get("intensity"), CAMERA_EFFECT_DEFAULT_INTENSITY)
        intensity = max(CAMERA_EFFECT_MIN_INTENSITY, min(CAMERA_EFFECT_MAX_INTENSITY, intensity))
        easing = str(raw.get("easing") or "sine_pulse").strip()
        if easing not in CAMERA_EFFECT_EASINGS:
            easing = "sine_pulse"
        source = str(raw.get("source") or "user").strip() or "user"
        item = {
            "id": effect_id,
            "token": CAMERA_EFFECT_TOKEN,
            "start_s": round(start, 3),
            "end_s": round(end, 3),
            "intensity": round(intensity, 4),
            "easing": easing,
            "source": source,
        }
        for key in ("event_id", "role"):
            value = raw.get(key)
            if value is not None and str(value).strip():
                item[key] = str(value).strip()
        out.append(item)
        seen.add(effect_id)
    return out


def camera_effects_from_intents(
    intents: list[dict[str, Any]] | None,
    *,
    existing_effects: list[dict[str, Any]] | None = None,
    duration_s: float | None = None,
) -> list[dict[str, Any]]:
    """Materialize compiler camera intents while preserving user-edited effects."""

    existing = normalize_camera_effects(existing_effects, duration_s=duration_s)
    user_effects = [effect for effect in existing if effect.get("source") != "smart_captions"]
    by_id: dict[str, dict[str, Any]] = {str(effect["id"]): effect for effect in user_effects}
    for intent in intents or []:
        if not isinstance(intent, dict):
            continue
        if intent.get("token") not in (None, CAMERA_EFFECT_TOKEN):
            continue
        start = _finite_float(intent.get("start_s", intent.get("at_s")), 0.0)
        end = start + CAMERA_EFFECT_DEFAULT_DURATION_S
        effect = {
            "id": _stable_camera_id(intent),
            "start_s": start,
            "end_s": end,
            "intensity": CAMERA_EFFECT_DEFAULT_INTENSITY,
            "easing": "sine_pulse",
            "source": "smart_captions",
            "event_id": intent.get("event_id"),
            "role": intent.get("role"),
        }
        normalized = normalize_camera_effects([effect], duration_s=duration_s)
        if normalized and normalized[0]["id"] not in by_id:
            by_id[str(normalized[0]["id"])] = normalized[0]
    return sorted(by_id.values(), key=lambda item: (float(item["start_s"]), str(item["id"])))
