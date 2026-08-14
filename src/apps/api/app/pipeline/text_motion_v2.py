"""Deterministic Text Motion v2 timing/evaluator mirror.

Keep constants and equations in lockstep with web/src/lib/text-motion-v2.ts.
All renderer phase timestamps are rounded to the 30fps output grid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import regex

SPEED_MIN = 0.25
SPEED_MAX = 4.0
STAGGER_MAX_MS = 250.0
BLUR_MAX_PX = 12.0
HOLD_MAX_S = 3600.0
EXIT_MAX_S = 2.0
REVEAL_RAMP_MIN_MS = 40.0
REVEAL_RAMP_MAX_MS = 400.0
OUTPUT_FPS = 30
MAX_TEXT_MOTION_UNIQUE_FRAMES = 240
MAX_ACTIVE_TEXT_MOTION_FRAME_WEIGHT = 240

V2_HOLDABLE_TEXT_EFFECTS = frozenset(
    {
        "smooth-type",
        "fade-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "pop-in",
        "bounce",
        "typewriter",
        "stream-in",
        "staggered-slice",
        "handwriting",
        "ink-reveal",
    }
)

Easing = Literal["linear", "ease-out-cubic", "ease-in-out-cubic"]
Order = Literal["forward", "reverse", "center-out"]
Direction = Literal["none", "up", "down", "left", "right"]


@dataclass(frozen=True)
class NormalizedTextMotion:
    speed: float
    intensity: float
    easing: Easing
    stagger_ms: float
    order: Order
    direction: Direction
    travel_px: float
    overshoot: float
    blur_px: float
    cursor_style: str
    cursor_blink_ms: float
    hold_s: float
    exit_s: float
    reveal_ramp_ms: float


@dataclass(frozen=True)
class SmoothTypeState:
    alpha: float
    x_translate: float
    y_translate: float
    blur_px: float
    reveal_progress: float
    reveal_origin: Order
    settled: bool


def _finite(value: object, fallback: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _clamp(value: object, low: float, high: float, fallback: float) -> float:
    return max(low, min(high, _finite(value, fallback)))


def _defaults(effect: str) -> dict:
    if effect == "smooth-type":
        return {
            "speed": 1.0,
            "intensity": 0.7,
            "easing": "ease-out-cubic",
            "stagger_ms": 45.0,
            "order": "forward",
            "direction": "up",
            "travel_px": 18.0,
            "overshoot": 0.0,
            "blur_px": 4.0,
            "cursor_style": "none",
            "cursor_blink_ms": 500.0,
            "hold_s": 1.0,
            "exit_s": 0.0,
            "reveal_ramp_ms": 120.0,
        }
    return {
        "speed": 1.0,
        "intensity": 1.0,
        "easing": "ease-out-cubic",
        "stagger_ms": 0.0,
        "order": "forward",
        "direction": "down" if effect == "slide-down" else "up" if effect == "slide-up" else "none",
        "travel_px": 220.0 if effect in {"slide-up", "slide-down"} else 0.0,
        "overshoot": 0.15 if effect in {"pop-in", "bounce"} else 0.0,
        "blur_px": 0.0,
        "cursor_style": "bar" if effect == "stream-in" else "none",
        "cursor_blink_ms": 500.0,
        "hold_s": 1.0,
        "exit_s": 1.0 if effect == "dissolve-out" else 0.0,
        "reveal_ramp_ms": 120.0,
    }


def normalize_text_motion(effect: str, raw: object) -> NormalizedTextMotion:
    defaults = _defaults(effect)
    motion = raw if isinstance(raw, dict) and raw.get("version") == 2 else {}
    easing = motion.get("easing")
    if easing not in {"linear", "ease-out-cubic", "ease-in-out-cubic"}:
        easing = defaults["easing"]
    order = motion.get("order")
    if order not in {"forward", "reverse", "center-out"}:
        order = defaults["order"]
    direction = motion.get("direction")
    if direction not in {"none", "up", "down", "left", "right"}:
        direction = defaults["direction"]
    cursor = motion.get("cursor_style")
    if cursor not in {"none", "bar", "block", "underscore"}:
        cursor = defaults["cursor_style"]
    return NormalizedTextMotion(
        speed=_clamp(motion.get("speed"), SPEED_MIN, SPEED_MAX, defaults["speed"]),
        intensity=_clamp(motion.get("intensity"), 0.0, 1.0, defaults["intensity"]),
        easing=easing,
        stagger_ms=_clamp(motion.get("stagger_ms"), 0.0, STAGGER_MAX_MS, defaults["stagger_ms"]),
        order=order,
        direction=direction,
        travel_px=_clamp(motion.get("travel_px"), 0.0, 600.0, defaults["travel_px"]),
        overshoot=_clamp(motion.get("overshoot"), 0.0, 1.0, defaults["overshoot"]),
        blur_px=_clamp(motion.get("blur_px"), 0.0, BLUR_MAX_PX, defaults["blur_px"]),
        cursor_style=cursor,
        cursor_blink_ms=_clamp(
            motion.get("cursor_blink_ms"), 100.0, 2000.0, defaults["cursor_blink_ms"]
        ),
        hold_s=_clamp(motion.get("hold_s"), 0.0, HOLD_MAX_S, defaults["hold_s"]),
        exit_s=_clamp(motion.get("exit_s"), 0.0, EXIT_MAX_S, defaults["exit_s"]),
        reveal_ramp_ms=_clamp(
            motion.get("reveal_ramp_ms"),
            REVEAL_RAMP_MIN_MS,
            REVEAL_RAMP_MAX_MS,
            defaults["reveal_ramp_ms"],
        ),
    )


def grapheme_count(text: str) -> int:
    return len(regex.findall(r"\X", text))


def effect_base_duration_s(effect: str, text: str, raw: object) -> float:
    motion = normalize_text_motion(effect, raw)
    if effect == "smooth-type":
        count = max(1, grapheme_count(text))
        return max(0.12, ((count - 1) * motion.stagger_ms + motion.reveal_ramp_ms) / 1000.0)
    if effect == "typewriter":
        return max(0.12, grapheme_count(text) / 12.0)
    if effect == "stream-in":
        return max(0.12, len(regex.findall(r"\S+", text)) / 6.0)
    line_count = len(text.split("\n"))
    return {
        "staggered-slice": (
            1.35 if line_count <= 1 else min(2.4, 1.5 + max(0, line_count - 2) * 0.12 + 0.35)
        ),
        "handwriting": 2.2,
        "ink-reveal": 2.2,
        "scale-up": 0.6,
        "bounce": 0.5,
        "fade-in": 0.4,
        "slide-up": 0.35,
        "slide-down": 0.35,
        "pop-in": 0.25,
    }.get(effect, 0.0)


def settle_duration_s(effect: str, text: str, raw: object) -> float:
    motion = normalize_text_motion(effect, raw)
    return effect_base_duration_s(effect, text, {"version": 2, **motion.__dict__}) / motion.speed


def total_duration_s(effect: str, text: str, raw: object) -> float:
    motion = normalize_text_motion(effect, raw)
    return (
        settle_duration_s(effect, text, {"version": 2, **motion.__dict__})
        + motion.hold_s
        + motion.exit_s
    )


def round_output_frame(value: float) -> float:
    return math.floor(max(0.0, value) * OUTPUT_FPS + 0.5) / OUTPUT_FPS


def renderer_settle_duration_s(effect: str, text: str, raw: object) -> float:
    """Renderer-only settle boundary; authored duration math stays continuous."""
    return max(1.0 / OUTPUT_FPS, round_output_frame(settle_duration_s(effect, text, raw)))


def authored_motion_time_s(effect: str, text: str, t_local: float, raw: object) -> float:
    """Map output time onto the authored curve with a frame-snapped settle."""
    motion = normalize_text_motion(effect, raw)
    base = effect_base_duration_s(effect, text, {"version": 2, **motion.__dict__})
    if base <= 0.0:
        return max(0.0, t_local) * motion.speed
    return max(0.0, t_local) * (base / renderer_settle_duration_s(effect, text, raw))


def text_motion_unique_frame_count(
    effect: str,
    text: str,
    duration_s: float,
    raw: object,
    *,
    extra_exit_s: float = 0.0,
) -> int:
    """Predict v2 Skia frames whose pixels can differ, plus one held frame."""
    duration_s = max(0.0, duration_s)
    if duration_s <= 0.0:
        return 0
    wanted = max(1, math.floor(duration_s * OUTPUT_FPS + 0.5))
    n_render = wanted + 1
    settle_s = min(duration_s, renderer_settle_duration_s(effect, text, raw))
    motion = normalize_text_motion(effect, raw)
    exit_s = min(duration_s, round_output_frame(max(motion.exit_s, extra_exit_s)))
    exit_start_s = duration_s - exit_s
    changing = 0
    has_hold = False
    for index in range(n_render):
        t_local = index / OUTPUT_FPS
        if t_local < settle_s or (exit_s > 0.0 and t_local >= exit_start_s):
            changing += 1
        else:
            has_hold = True
    return changing + (1 if has_hold else 0)


def text_motion_complexity_error(elements: list[object]) -> str | None:
    """Fail closed on expensive individual or overlapping v2 text motion."""
    weighted_intervals: list[tuple[float, int, int]] = []
    for element in elements:
        if isinstance(element, dict):
            get = element.get
        else:

            def get(key: str, default: object = None) -> object:
                return getattr(element, key, default)

        effect = str(get("effect", "none"))
        if effect not in V2_HOLDABLE_TEXT_EFFECTS:
            continue
        motion = get("motion")
        if hasattr(motion, "model_dump"):
            motion = motion.model_dump(exclude_none=True)
        if not isinstance(motion, dict) or motion.get("version") != 2:
            continue
        start_s = _finite(get("start_s"), 0.0)
        end_s = _finite(get("end_s"), start_s)
        duration_s = max(0.0, end_s - start_s)
        theme_transition = get("theme_transition")
        if bool(get("behind_subject", False)) or theme_transition is not None:
            # Subject occlusion samples a changing matte on every frame, so the
            # settled hold cannot be hard-linked like an ordinary text layer.
            weight = max(1, math.floor(duration_s * OUTPUT_FPS + 0.5) + 1)
        else:
            weight = text_motion_unique_frame_count(
                effect,
                str(get("text", "")),
                duration_s,
                motion,
                extra_exit_s=max(0.0, _finite(get("fade_out_ms"), 0.0) / 1000.0),
            )
        if weight > MAX_TEXT_MOTION_UNIQUE_FRAMES:
            return (
                f"Text motion exceeds the {MAX_TEXT_MOTION_UNIQUE_FRAMES}-frame motion budget "
                f"({weight} unique frames). Increase speed, reduce stagger, or shorten the text."
            )
        if weight > 0:
            # End events sort before start events at a shared boundary.
            weighted_intervals.append((start_s, 1, weight))
            weighted_intervals.append((end_s, 0, -weight))

    active = 0
    for _, _, delta in sorted(weighted_intervals):
        active += delta
        if active > MAX_ACTIVE_TEXT_MOTION_FRAME_WEIGHT:
            return (
                "Overlapping text motion scenes exceed the active motion complexity budget "
                f"({active} > {MAX_ACTIVE_TEXT_MOTION_FRAME_WEIGHT})."
            )
    return None


def ease(progress: float, easing: Easing) -> float:
    t = max(0.0, min(1.0, progress))
    if easing == "linear":
        return t
    if easing == "ease-in-out-cubic":
        return 4 * t**3 if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2
    return 1 - (1 - t) ** 3


def smooth_type_state_at(text: str, t_local: float, raw: object) -> SmoothTypeState:
    motion = normalize_text_motion("smooth-type", raw)
    count = max(1, grapheme_count(text))
    stagger_s = motion.stagger_ms / 1000.0
    ramp_s = motion.reveal_ramp_ms / 1000.0
    base = effect_base_duration_s("smooth-type", text, {"version": 2, **motion.__dict__})
    settle_s = renderer_settle_duration_s("smooth-type", text, raw)
    authored_t = authored_motion_time_s("smooth-type", text, t_local, raw)
    # Averaging complete cluster ramps keeps both value and velocity continuous
    # as a new cluster begins; an active-index shortcut creates a visible jump.
    reveal = min(
        1.0,
        sum(
            ease((authored_t - index * stagger_s) / max(ramp_s, 1e-6), motion.easing)
            for index in range(count)
        )
        / count,
    )
    entrance = ease(authored_t / max(base, 1e-6), motion.easing)
    remaining = 1.0 - entrance
    distance = motion.travel_px * motion.intensity * remaining
    x_translate = (
        -distance
        if motion.direction == "left"
        else distance
        if motion.direction == "right"
        else 0.0
    )
    y_translate = (
        -distance if motion.direction == "up" else distance if motion.direction == "down" else 0.0
    )
    alpha = 1.0 - motion.intensity * (1.0 - entrance)
    return SmoothTypeState(
        alpha=max(0.0, min(1.0, alpha)),
        x_translate=x_translate,
        y_translate=y_translate,
        blur_px=motion.blur_px * motion.intensity * remaining,
        reveal_progress=reveal,
        reveal_origin=motion.order,
        settled=max(0.0, t_local) + 1e-9 >= settle_s,
    )


def smooth_type_line_progresses(lines: list[str], t_local: float, raw: object) -> list[float]:
    """Per-shaped-line masks following global logical grapheme order."""
    motion = normalize_text_motion("smooth-type", raw)
    clusters_by_line = [regex.findall(r"\X", line) for line in lines]
    # Each visual-line boundary replaces one source separator (an authored
    # newline or the space consumed by wrapping). Keep that invisible cluster
    # in the global schedule so line masks and the complete run settle together.
    separator_count = max(0, len(lines) - 1)
    total = max(1, sum(len(clusters) for clusters in clusters_by_line) + separator_count)
    authored_t = authored_motion_time_s("smooth-type", "\n".join(lines), t_local, raw)
    stagger_s = motion.stagger_ms / 1000.0
    ramp_s = max(motion.reveal_ramp_ms / 1000.0, 1e-6)
    if motion.order == "center-out":
        ranked_indices = sorted(
            range(total), key=lambda index: (abs(index - (total - 1) / 2), index)
        )
        ranks = {index: rank for rank, index in enumerate(ranked_indices)}
    elif motion.order == "reverse":
        ranks = {index: total - 1 - index for index in range(total)}
    else:
        ranks = {index: index for index in range(total)}

    progresses: list[float] = []
    offset = 0
    for line_index, clusters in enumerate(clusters_by_line):
        if not clusters:
            progresses.append(1.0)
            if line_index < len(clusters_by_line) - 1:
                offset += 1
            continue
        values = [
            ease((authored_t - ranks[offset + index] * stagger_s) / ramp_s, motion.easing)
            for index in range(len(clusters))
        ]
        progresses.append(max(0.0, min(1.0, sum(values) / len(values))))
        offset += len(clusters) + (1 if line_index < len(clusters_by_line) - 1 else 0)
    return progresses
