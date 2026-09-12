"""Absolute-time motion used by authored text preview/export.

Swift mirror: KriaMediaEngine/TextAnimationPhases.swift. Animation does not
retime the containing item. Short windows share their available time equally.
"""

from dataclasses import dataclass
from math import floor, pi, sin

from app.agents._schemas.text_animation_phases import TextAnimationPhases


@dataclass(frozen=True)
class TextPhaseSample:
    alpha: float = 1
    scale: float = 1
    x: float = 0
    y: float = 0
    reveal: float = 1


def sample_text_phases(
    phases: TextAnimationPhases, time: float, duration: float
) -> TextPhaseSample:
    if duration <= 0 or time < 0 or time >= duration:
        return TextPhaseSample(alpha=0, reveal=0)
    phase_duration = min(0.4 / phases.speed, duration / 2)
    alpha, scale, x, y, reveal = 1.0, 1.0, 0.0, 0.0, 1.0
    for effect, progress in (
        (phases.entrance, min(1.0, time / phase_duration)),
        (phases.exit, min(1.0, (duration - time) / phase_duration)),
    ):
        eased = 1 - (1 - progress) ** 3
        if effect == "fade":
            alpha *= eased
        elif effect == "pop":
            scale *= 0.7 + 0.3 * eased
            alpha *= eased
        elif effect == "slide":
            x += 40 * (1 - eased)
            alpha *= eased
        elif effect == "typewriter":
            reveal = min(reveal, progress)
    wave = sin(2 * pi * time * phases.speed / 1.2)
    if phases.loop == "pulse":
        scale *= 1 + 0.04 * wave
    elif phases.loop == "bounce":
        y -= 12 * abs(wave)
    elif phases.loop == "float":
        y -= 8 * wave
    return TextPhaseSample(alpha, scale, x, y, reveal)


def visible_grapheme_count(count: int, reveal: float) -> int:
    return min(count, max(0, floor(count * reveal + 1e-9)))
