"""Editable semantic camera effects shared by Smart Captions and render paths.

Two emphasis shapes live on ONE wire token (``semantic_crop_pulse``), chosen by
``easing``:

``sine_pulse``
    The original symmetric accent: crop scale rises and returns inside a short
    window (sin² over the whole window). Reads as a heartbeat under a spoken
    beat.

``ease_in_hold``  (KRI-7, the "zoom-in emphasis")
    Punch in, HOLD, release. The scale eases in over a bounded ramp, stays at
    full intensity for the body of the window, then eases back out. Holding is
    what makes the moment feel emphasised rather than merely accented, so this
    shape is allowed a longer window than the pulse.

The amount curve is defined ONCE here (:func:`camera_effect_amount`) and mirrored
by every renderer that has to agree with it frame-for-frame:

  * export render — ``app/pipeline/reframe.py`` (ffmpeg scale expression)
  * web editor preview — ``src/apps/web/src/lib/camera-effects.ts``

A change to the curve that lands in only one of those is the "looks right in the
preview, wrong in the export" bug class, so the ramp constants below are the
single source of truth and the mirrors carry a pointer back here.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, NamedTuple

CAMERA_EFFECT_EASING_PULSE = "sine_pulse"
CAMERA_EFFECT_EASING_HOLD = "ease_in_hold"
CAMERA_EFFECT_EASINGS = frozenset({CAMERA_EFFECT_EASING_PULSE, CAMERA_EFFECT_EASING_HOLD})
CAMERA_EFFECT_TOKEN = "semantic_crop_pulse"


class EasingBounds(NamedTuple):
    min_duration_s: float
    max_duration_s: float
    default_duration_s: float
    default_intensity: float


# The hold shape earns a longer ceiling because its body is a steady framing, not
# a moving one — a 5s pulse would read as a slow wobble, a 5s hold reads as a
# deliberate push-in that sits under the whole sentence.
_EASING_BOUNDS: dict[str, EasingBounds] = {
    CAMERA_EFFECT_EASING_PULSE: EasingBounds(0.4, 2.0, 1.2, 0.04),
    CAMERA_EFFECT_EASING_HOLD: EasingBounds(0.6, 6.0, 1.8, 0.06),
}

_PULSE_BOUNDS = _EASING_BOUNDS[CAMERA_EFFECT_EASING_PULSE]
CAMERA_EFFECT_DEFAULT_DURATION_S = _PULSE_BOUNDS.default_duration_s
CAMERA_EFFECT_MIN_DURATION_S = _PULSE_BOUNDS.min_duration_s
CAMERA_EFFECT_MAX_DURATION_S = _PULSE_BOUNDS.max_duration_s
CAMERA_EFFECT_DEFAULT_INTENSITY = _PULSE_BOUNDS.default_intensity
CAMERA_EFFECT_MIN_INTENSITY = 0.0
CAMERA_EFFECT_MAX_INTENSITY = 0.08
# Longest window any easing may occupy — used where a caller needs one bound
# before it knows the easing (timeline hit-testing, copilot validation).
CAMERA_EFFECT_ABS_MAX_DURATION_S = max(bounds.max_duration_s for bounds in _EASING_BOUNDS.values())

# Ramp shape for `ease_in_hold`. Proportional caps keep short holds from being
# all-ramp-no-hold; absolute caps keep long holds from crawling in.
CAMERA_HOLD_RAMP_IN_S = 0.5
CAMERA_HOLD_RAMP_OUT_S = 0.4
CAMERA_HOLD_RAMP_IN_RATIO = 0.35
CAMERA_HOLD_RAMP_OUT_RATIO = 0.25

# Total crop-scale increase the renderer will apply at any instant, however many
# effects overlap. Mirrored by the ffmpeg expression and the web preview.
CAMERA_EFFECT_MAX_STACKED_AMOUNT = 0.12


def resolve_easing(value: Any) -> str:
    """Return a supported easing name, defaulting to the legacy pulse."""

    easing = str(value or "").strip()
    return easing if easing in CAMERA_EFFECT_EASINGS else CAMERA_EFFECT_EASING_PULSE


def easing_bounds(easing: str) -> EasingBounds:
    """Duration/intensity envelope for one emphasis shape."""

    return _EASING_BOUNDS[resolve_easing(easing)]


def camera_hold_ramps(duration_s: float) -> tuple[float, float]:
    """Ease-in / ease-out ramp lengths for an `ease_in_hold` window."""

    duration = max(1e-3, float(duration_s))
    ramp_in = min(CAMERA_HOLD_RAMP_IN_S, duration * CAMERA_HOLD_RAMP_IN_RATIO)
    ramp_out = min(CAMERA_HOLD_RAMP_OUT_S, duration * CAMERA_HOLD_RAMP_OUT_RATIO)
    return max(1e-3, ramp_in), max(1e-3, ramp_out)


def camera_effect_amount(effect: dict[str, Any], time_s: float) -> float:
    """Crop-scale increase contributed by one effect at ``time_s`` (0.0 outside)."""

    start = _finite_float(effect.get("start_s"), 0.0)
    end = _finite_float(effect.get("end_s"), start)
    duration = end - start
    if duration <= 0 or not math.isfinite(time_s) or time_s < start or time_s > end:
        return 0.0
    intensity = max(
        CAMERA_EFFECT_MIN_INTENSITY,
        min(CAMERA_EFFECT_MAX_INTENSITY, _finite_float(effect.get("intensity"), 0.04)),
    )
    elapsed = time_s - start
    if resolve_easing(effect.get("easing")) == CAMERA_EFFECT_EASING_HOLD:
        ramp_in, ramp_out = camera_hold_ramps(duration)
        rise = math.sin(math.pi / 2 * min(1.0, elapsed / ramp_in)) ** 2
        fall = math.sin(math.pi / 2 * min(1.0, (duration - elapsed) / ramp_out)) ** 2
        return intensity * min(rise, fall)
    return intensity * math.sin(math.pi * (elapsed / duration)) ** 2


def camera_scale_at(effects: list[dict[str, Any]] | None, time_s: float) -> float:
    """Crop scale (>= 1.0) applied by the whole effect list at ``time_s``."""

    amount = sum(camera_effect_amount(effect, time_s) for effect in effects or [])
    return 1.0 + min(CAMERA_EFFECT_MAX_STACKED_AMOUNT, amount)


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
        easing = resolve_easing(raw.get("easing"))
        bounds = easing_bounds(easing)
        start = max(0.0, _finite_float(raw.get("start_s"), 0.0))
        end = _finite_float(raw.get("end_s"), start + bounds.default_duration_s)
        if max_end is not None:
            start = min(start, max_end)
            end = min(end, max_end)
        min_end = start + bounds.min_duration_s
        max_effect_end = start + bounds.max_duration_s
        end = min(max(end, min_end), max_effect_end)
        if max_end is not None:
            end = min(end, max_end)
        if end <= start:
            continue
        intensity = _finite_float(raw.get("intensity"), bounds.default_intensity)
        intensity = max(CAMERA_EFFECT_MIN_INTENSITY, min(CAMERA_EFFECT_MAX_INTENSITY, intensity))
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
        for key in ("event_id", "effect_group_id", "role"):
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
        easing = resolve_easing(intent.get("easing"))
        bounds = easing_bounds(easing)
        start = _finite_float(intent.get("start_s", intent.get("at_s")), 0.0)
        # An intent that NAMES its easing also owns its window: the emphasis
        # planner sizes a hold to the phrase it is holding, and a hold without
        # its phrase is meaningless. Intents that don't name one are the
        # Smart-preset accents, which keep their fixed default window exactly as
        # they did before this easing existed.
        if intent.get("easing"):
            end = _finite_float(intent.get("end_s"), start + bounds.default_duration_s)
        else:
            end = start + bounds.default_duration_s
        if end <= start:
            end = start + bounds.default_duration_s
        effect = {
            "id": _stable_camera_id(intent),
            "start_s": start,
            "end_s": end,
            "intensity": _finite_float(intent.get("intensity"), bounds.default_intensity),
            "easing": easing,
            "source": "smart_captions",
            "event_id": intent.get("event_id"),
            "effect_group_id": intent.get("effect_group_id") or intent.get("event_id"),
            "role": intent.get("role"),
        }
        normalized = normalize_camera_effects([effect], duration_s=duration_s)
        if normalized and normalized[0]["id"] not in by_id:
            by_id[str(normalized[0]["id"])] = normalized[0]
    return sorted(by_id.values(), key=lambda item: (float(item["start_s"]), str(item["id"])))
