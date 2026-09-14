"""Deterministic visual animation shared by every FFmpeg visual lane.

The paired native implementation is VisualEditorStyle.swift. Local time is
sampled at output frames; changing speed never changes a layer's interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.agents._schemas.visual_editor import VisualEditorStyle


@dataclass(frozen=True)
class VisualSample:
    alpha: float = 1
    scale: float = 1
    x: float = 0
    y: float = 0


def sample_visual(style: VisualEditorStyle, time: float, duration: float) -> VisualSample:
    if not math.isfinite(time) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("visual time and duration must be finite")
    if time < 0 or time >= duration:
        return VisualSample(alpha=0)
    animation = style.animation
    edge = min(0.4 / animation.speed, duration / 2)
    alpha, scale, x, y = 1.0, 1.0, 0.0, 0.0
    for effect, progress in (
        (animation.entrance, min(1, time / edge)),
        (animation.exit, min(1, (duration - time) / edge)),
    ):
        eased = 1 - (1 - progress) ** 3
        if effect in {"fade", "pop", "slide", "zoom"}:
            alpha *= eased
        if effect == "pop":
            scale *= 0.7 + 0.3 * eased
        elif effect == "zoom":
            scale *= 1.2 - 0.2 * eased
        elif effect == "slide":
            x += 40 * (1 - eased)
    wave = math.sin(2 * math.pi * time * animation.speed / 1.2)
    if animation.loop == "pulse":
        scale *= 1 + 0.04 * wave
    elif animation.loop == "bounce":
        y -= 12 * abs(wave)
    elif animation.loop == "float":
        y -= 8 * wave
    return VisualSample(alpha, scale, x, y)


def visual_expressions(
    style: VisualEditorStyle, duration: float, time: str = "t"
) -> dict[str, str]:
    animation = style.animation
    edge = min(0.4 / animation.speed, duration / 2)
    alpha, scale, x, y = ["1"], ["1"], ["0"], ["0"]
    for effect, clock in (
        (animation.entrance, time),
        (animation.exit, f"({duration:.9f}-({time}))"),
    ):
        eased = f"(1-pow(1-min(1,max(0,({clock})/{edge:.9f})),3))"
        if effect != "none":
            alpha.append(eased)
        if effect == "pop":
            scale.append(f"(0.7+0.3*{eased})")
        elif effect == "zoom":
            scale.append(f"(1.2-0.2*{eased})")
        elif effect == "slide":
            x.append(f"40*(1-{eased})")
    wave = f"sin(2*PI*({time})*{animation.speed:.9f}/1.2)"
    if animation.loop == "pulse":
        scale.append(f"(1+0.04*{wave})")
    elif animation.loop == "bounce":
        y.append(f"-12*abs({wave})")
    elif animation.loop == "float":
        y.append(f"-8*{wave}")
    return {"alpha": "*".join(alpha), "scale": "*".join(scale), "x": "+".join(x), "y": "+".join(y)}


def visual_animation_filters(style: VisualEditorStyle, duration: float) -> list[str]:
    """Input has local PTS and its final unanimated dimensions, with alpha intact."""
    expressions = visual_expressions(style, duration)
    filters = ["format=rgba"]
    if expressions["scale"] != "1":
        filters.append(f"scale=w='max(2,round(iw*({expressions['scale']})/2)*2)':h=-2:eval=frame")
    if style.rotation_deg:
        angle = style.rotation_deg * math.pi / 180
        filters.append(f"rotate={angle:.9f}:ow=rotw({angle:.9f}):oh=roth({angle:.9f}):c=none")
    if expressions["alpha"] != "1":
        alpha = visual_expressions(style, duration, "T")["alpha"]
        filters.append(f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({alpha})'")
    return filters
