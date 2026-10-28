"""Designed text motion over the renderer's existing shaped ink."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageChops, ImageFilter

from app.agents._schemas.text_animation_phases import DESIGNED_TEXT_EDGES, TextAnimationPhases
from app.pipeline.text_animation_phases import phase_duration, sample_text_phases

DESIGNED_EDGES = DESIGNED_TEXT_EDGES


def has_designed_motion(phases: TextAnimationPhases) -> bool:
    return bool(DESIGNED_EDGES.intersection((phases.entrance, phases.exit)))


def ease(value: float) -> float:
    x = min(1.0, max(0.0, value))
    if x in (0.0, 1.0):
        return x
    lo, hi = 0.0, 1.0
    for _ in range(16):
        t = (lo + hi) / 2
        bx = 3 * (1 - t) ** 2 * t * 0.23 + 3 * (1 - t) * t * t * 0.32 + t**3
        if bx < x:
            lo = t
        else:
            hi = t
    t = (lo + hi) / 2
    return 3 * (1 - t) ** 2 * t + 3 * (1 - t) * t * t + t**3


@dataclass(frozen=True)
class MotionLine:
    box: tuple[int, int, int, int]
    size: float
    glyphs: tuple[tuple[int, int], ...]
    words: tuple[tuple[int, int], ...]
    word_steps: tuple[tuple[int, ...], ...]


@lru_cache(maxsize=64)
def _geometry(payload: str, width: int, height: int) -> tuple[MotionLine, ...]:
    import regex
    import skia

    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.canvas import Canvas
    from app.pipeline.portable_text_layout import compile_text_overlay

    overlay = json.loads(payload)
    overlay.update(effect="static", animation_phases=None, behind_subject=False)
    overlay.pop("theme_transition", None)
    layer, _ = compile_text_overlay(overlay, layer_id="designed", canvas=Canvas(width, height))
    result = []
    for i, run in enumerate(layer.runs):
        font = skia.Font(cloud._typeface_for_overlay(overlay), run.font_size)

        def advance(prefix):
            if not prefix:
                return 0.0
            return cloud._measure_line(font, prefix, run.letter_spacing, shape_text=run.shaped)

        previous = layer.runs[i - 1].baseline_y if i else 0
        following = layer.runs[i + 1].baseline_y if i + 1 < len(layer.runs) else height
        top = (
            max(0, (previous + run.baseline_y) / 2 - run.font_size * 0.5)
            if i
            else max(0, run.baseline_y - run.font_size * 1.7)
        )
        bottom = (
            min(height, (following + run.baseline_y) / 2 - run.font_size * 0.5)
            if i + 1 < len(layer.runs)
            else min(height, run.baseline_y + run.font_size * 0.7)
        )
        bleed = max(4, run.font_size * 0.15, run.stroke_width)
        left, right = run.x - bleed, run.x + advance(run.text) + bleed

        def spans(matches):
            return tuple(
                (
                    math.floor(run.x + advance(run.text[: m.start()])),
                    math.ceil(run.x + advance(run.text[: m.end()])),
                )
                for m in matches
            )

        word_matches = tuple(re.finditer(r"\S+", run.text))
        word_steps = tuple(
            tuple(
                math.ceil(run.x + advance(run.text[: match.start() + offset]))
                for offset in [0, *(part.end() for part in regex.finditer(r"\X", match.group()))]
            )
            for match in word_matches
        )
        result.append(
            MotionLine(
                (math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom)),
                run.font_size,
                spans(regex.finditer(r"\X", run.text)),
                spans(word_matches),
                word_steps,
            )
        )
    return tuple(result)


def _alpha(image: Image.Image, value: float) -> Image.Image:
    image = image.copy()
    image.putalpha(image.getchannel("A").point(lambda a: round(a * min(1, max(0, value)))))
    return image


def paint_designed_motion(
    image: Image.Image, overlay: dict, phases: TextAnimationPhases, time: float, duration: float
) -> Image.Image:
    empty = Image.new("RGBA", image.size)
    if not 0 <= time < duration:
        return empty
    edge = phase_duration(phases, duration)
    if phases.exit in DESIGNED_EDGES and time > duration - edge:
        effect, progress = phases.exit, (duration - time) / edge
    elif phases.entrance == "wordRelay":
        effect, progress = (
            "wordRelay",
            time / max(0.001, duration - (0 if phases.exit == "none" else edge)),
        )
    elif phases.entrance in DESIGNED_EDGES and time < edge:
        effect, progress = phases.entrance, time / edge
    elif phases.entrance == "ribbonWipe" or phases.exit == "ribbonWipe":
        effect, progress = "ribbonWipe", 1.0
    else:
        return image
    p = ease(progress)
    payload = json.dumps(
        {k: v for k, v in overlay.items() if not k.startswith("_")}, sort_keys=True
    )
    lines = _geometry(payload, *image.size)
    if effect == "focusPull":
        return _alpha(
            image.filter(
                ImageFilter.GaussianBlur(max((r.size for r in lines), default=1) * 0.12 * (1 - p))
            ),
            p,
        )
    result = empty

    def place(sprite, x, y):
        result.alpha_composite(sprite, (round(x), round(y)))

    if effect == "wordRelay":
        words = [
            (line, box, steps)
            for line in lines
            for box, steps in zip(line.words, line.word_steps, strict=True)
        ]
        if not words:
            return image
        index = min(len(words) - 1, int(progress * len(words)))
        line, (left, right), steps = words[index]
        q = ease((progress * len(words) - index) * phases.speed / 0.24)
        sprite = image.crop((left, line.box[1], right, line.box[3]))
        original_width = sprite.width
        reveal = sample_text_phases(phases, time, duration).reveal
        if reveal < 1:
            count = min(len(steps) - 1, max(0, math.floor((len(steps) - 1) * reveal)))
            if count == 0:
                return result
            sprite = sprite.crop(
                (0, 0, min(sprite.width, max(0, steps[count] - left)), sprite.height)
            )
        center_x = float(overlay.get("position_x_frac", 0.5)) * image.width
        center_y = sum((line.box[1] + line.box[3]) / 2 for line in lines) / len(lines)
        place(
            _alpha(sprite, q),
            center_x - original_width / 2,
            center_y - sprite.height / 2 + line.size * 0.15 * (1 - q),
        )
        return result
    for i, line in enumerate(lines):
        left, top, right, bottom = line.box
        sprite = image.crop(line.box)
        stagger = min(0.25, i * 0.07)
        q = ease((progress - stagger) / (1 - min(0.25, (len(lines) - 1) * 0.07)))
        if effect == "silkReveal":
            strip = Image.new("RGBA", sprite.size)
            strip.alpha_composite(_alpha(sprite, q), (0, round(sprite.height * (1 - q))))
            place(strip, left, top)
        elif effect == "elasticCondensed":
            for j, (x0, x1) in enumerate(line.glyphs):
                delay = min(0.3, j / max(1, len(line.glyphs)) * 0.3)
                g = ease((progress - delay) / 0.7)
                over = math.sin(min(1, max(0, (progress - delay) / 0.7)) * math.pi) * 0.08
                glyph = image.crop((x0, top, max(x0 + 1, x1), bottom))
                glyph = glyph.resize(
                    (
                        max(1, round(glyph.width * (0.25 + 0.75 * g + over))),
                        max(1, round(glyph.height * (1 + 0.08 * (1 - g)))),
                    ),
                    Image.Resampling.BICUBIC,
                )
                place(
                    _alpha(glyph, g), (x0 + x1 - glyph.width) / 2, top + line.size * 0.3 * (1 - g)
                )
        elif effect == "ribbonWipe":
            color = (overlay.get("text_color") or "#FFFFFF").lstrip("#")
            rgb = tuple(int(color[n : n + 2], 16) for n in (0, 2, 4))
            dark = sum(a * b for a, b in zip(rgb, (0.2126, 0.7152, 0.0722), strict=True)) > 127
            ribbon = Image.new("RGBA", sprite.size, (20, 26, 20) if dark else (245, 240, 214))
            ribbon.alpha_composite(sprite)
            place(ribbon.crop((0, 0, round(ribbon.width * q), ribbon.height)), left, top)
        elif effect == "echoStack":
            mask = sprite.getchannel("A")
            outline = ImageChops.subtract(
                mask.filter(ImageFilter.MaxFilter(3)), mask.filter(ImageFilter.MinFilter(3))
            )
            echo = Image.new("RGBA", sprite.size, overlay.get("text_color") or "#FFFFFF")
            echo.putalpha(outline)
            for offset in (-2, -1, 1, 2):
                place(_alpha(echo, (1 - q) * 0.65), left, top + offset * line.size * 0.9 * (1 - q))
            place(_alpha(sprite, p), left, top)
    return result
