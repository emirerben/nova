"""Compile text layout decisions for the phone; never generate raster media."""

from __future__ import annotations

import skia

from app.kria.portable_text import PositionedGlyph


def resolved_font_variations(font: skia.Font) -> dict[str, float]:
    try:
        coordinates = font.getTypeface().getVariationDesignPosition()
    except RuntimeError:
        return {}
    return {int(axis.axis).to_bytes(4, "big").decode("ascii"): axis.value for axis in coordinates}


def resolve_legacy_glyphs(font: skia.Font, text: str, spacing_px: float) -> list[PositionedGlyph]:
    """Match Skia drawString's unshaped glyph/advance model, including tracking.

    Exact glyph IDs are meaningful only with the recipe's fingerprint-bound font.
    The phone must not apply kerning or shaping again to these positions.
    """
    glyph_ids = font.textToGlyphs(text)
    if len(glyph_ids) != len(text) or any(glyph == 0 for glyph in glyph_ids):
        raise ValueError("font cannot resolve every legacy text glyph")
    widths = font.getWidths(glyph_ids)
    x = 0.0
    glyphs = []
    for glyph_id, width in zip(glyph_ids, widths, strict=True):
        glyphs.append(PositionedGlyph(glyph_id=glyph_id, x=x, y=0))
        x += width + spacing_px
    return glyphs


class UnsupportedPortableText(ValueError):
    """The caller must keep the recipe gated instead of dropping a treatment."""


def _outline_ink(overlay: dict):
    from app.kria.portable_text import TextInk
    from app.pipeline import text_overlay_skia as cloud

    color = cloud._text_outline_color(cloud._text_shadow_style(overlay), 1)
    return TextInk(
        red=skia.ColorGetR(color) / 255,
        green=skia.ColorGetG(color) / 255,
        blue=skia.ColorGetB(color) / 255,
        alpha=skia.ColorGetA(color) / 255,
    )


def _resolve_paints(overlay: dict, *, width: float, height: float, left: float, top: float):
    import math

    from app.kria.portable_text import TextBlurLayer, TextGradient, TextGradientStop, TextInk
    from app.pipeline import text_overlay_skia as cloud

    color = cloud._skia_color_from_hex(overlay.get("text_color", "#FFFFFF"), 255)
    fill = TextInk(
        red=skia.ColorGetR(color) / 255,
        green=skia.ColorGetG(color) / 255,
        blue=skia.ColorGetB(color) / 255,
        alpha=skia.ColorGetA(color) / 255,
    )
    blurs = []
    glow = cloud._resolve_glow_kwargs(overlay)
    if glow.get("glow_rgb"):
        r, g, b = glow["glow_rgb"]
        for sigma, alpha in [(8, 120), (20, 220)]:
            blurs.append(
                TextBlurLayer(
                    color=TextInk(
                        red=r / 255,
                        green=g / 255,
                        blue=b / 255,
                        alpha=cloud._clamp_byte(alpha * glow["glow_strength"]) / 255,
                    ),
                    sigma=sigma,
                    alpha_power=2,
                    dx=0,
                    dy=0,
                )
            )
    for shadow in cloud._text_shadow_layers(cloud._text_shadow_style(overlay)):
        blurs.append(
            TextBlurLayer(
                color=TextInk(
                    red=shadow.red / 255,
                    green=shadow.green / 255,
                    blue=shadow.blue / 255,
                    alpha=shadow.alpha / 255,
                ),
                sigma=shadow.sigma,
                dx=shadow.dx,
                dy=shadow.dy,
            )
        )
    gradient = None
    parsed = cloud._parse_gradient_spec(overlay.get("text_gradient"))
    if parsed:
        angle = math.radians(parsed["angle_deg"] % 360)
        cosine, sine = math.cos(angle), math.sin(angle)
        half = (abs(width * cosine) + abs(height * sine)) / 2
        center_x, center_y = left + width / 2, top + height / 2
        gradient = TextGradient(
            start_x=center_x - cosine * half,
            start_y=center_y - sine * half,
            end_x=center_x + cosine * half,
            end_y=center_y + sine * half,
            stops=[
                TextGradientStop(
                    position=position,
                    color=TextInk(red=r / 255, green=g / 255, blue=b / 255, alpha=1),
                )
                for position, (r, g, b, _) in zip(parsed["stops"], parsed["colors"], strict=True)
            ],
        )
    return fill, blurs, gradient


def compile_text_overlay(overlay: dict, *, layer_id: str, canvas, dissolve_seed: int | None = None):
    from app.kria.portable_text import GiantTitleTransition
    from app.pipeline import text_overlay_skia as cloud

    theme = overlay.get("theme_transition")
    if not theme:
        return _compile_text_overlay(
            overlay, layer_id=layer_id, canvas=canvas, dissolve_seed=dissolve_seed
        )
    if cloud._theme_transition_type(overlay) != "giant-title-wipe":
        raise UnsupportedPortableText("unsupported theme transition")
    if overlay.get("effect") == "dissolve-out":
        # Production dissolves a static opening frame; it never samples the
        # later giant-title camera transform (see _generate_overlay_sequence).
        return _compile_text_overlay(
            {**overlay, "theme_transition": None},
            layer_id=layer_id,
            canvas=canvas,
            dissolve_seed=dissolve_seed,
        )
    if overlay.get("effect", "none") not in {
        "static",
        "none",
        "fade-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "slide-in",
        "pop-in",
        "bounce",
        "ink-reveal",
        "lyric-line",
        "typewriter",
        "stream-in",
        "karaoke-line",
        "smooth-type",
        "staggered-slice",
        "handwriting",
    }:
        raise UnsupportedPortableText("giant title is unsupported for this text painter")
    layer, font = _compile_text_overlay(
        {**overlay, "theme_transition": None},
        layer_id=layer_id,
        canvas=canvas,
        dissolve_seed=dissolve_seed,
    )
    x, y = cloud._resolve_anchor(overlay, canvas)
    dx, dy = cloud._giant_title_wipe_scale_origin(overlay, render_canvas=canvas)
    updates = {"giant_title": GiantTitleTransition(origin_x=x + dx, origin_y=y + dy)}
    raw_motion = overlay.get("motion")
    if (
        overlay.get("effect", "none") in {"static", "none", "slide-in"}
        and isinstance(raw_motion, dict)
        and raw_motion.get("version") == 2
    ):
        from dataclasses import asdict

        from app.kria.portable_text import ResolvedTextMotion
        from app.pipeline.text_motion_v2 import normalize_text_motion

        updates["motion"] = ResolvedTextMotion(
            **asdict(normalize_text_motion(overlay.get("effect", "none"), raw_motion))
        )
    layer = layer.model_copy(update=updates)
    return layer, font


def _compile_text_overlay(
    overlay: dict, *, layer_id: str, canvas, dissolve_seed: int | None = None
):
    """Resolve a supported cloud overlay into geometry plus its exact font asset.

    Reuses the production layout helpers. No image, frame, or encoded media is
    generated. Unimplemented specialized effects remain explicitly unsupported.
    """
    from dataclasses import asdict

    from app.kria.portable_text import (
        DiscreteRevealContent,
        DiscreteRevealLine,
        PortableTextLayer,
        PositionedTextRun,
        ResolvedTextMotion,
        SmoothRevealContent,
        SmoothRevealLine,
        TextRevealBounds,
    )
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.text_motion_v2 import normalize_text_motion
    from app.services.render_library import bundled_font_asset

    effect = overlay.get("effect", "none")
    if effect not in {
        "none",
        "static",
        "fade-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "slide-in",
        "pop-in",
        "bounce",
        "ink-reveal",
        "handwriting",
        "typewriter",
        "stream-in",
        "smooth-type",
        "staggered-slice",
        "dissolve-out",
        "karaoke-line",
        "lyric-line",
    }:
        raise UnsupportedPortableText(f"unsupported text effect: {effect}")
    if effect == "dissolve-out" and dissolve_seed is None:
        raise UnsupportedPortableText("dissolve requires its cloud overlay-index seed")
    # These fields invoke specialized drawing or timing outside the base line
    # painter. Accepting their base text would silently lose creator intent.
    for key in (
        "emoji",
        "emoji_prefix",
        "karaoke_words",
        "theme_transition",
        "behind_subject",
        "spans",
        "masonry_layer_origin_x_px",
    ):
        if overlay.get(key):
            raise UnsupportedPortableText(f"unsupported text treatment: {key}")
    if effect == "pop-in" and overlay.get("pop_animated_suffix"):
        suffix_layer = _compile_pop_suffix_overlay(overlay, layer_id=layer_id, canvas=canvas)
        if suffix_layer is not None:
            return suffix_layer
    if effect == "karaoke-line":
        return _compile_karaoke_overlay(overlay, layer_id=layer_id, canvas=canvas)
    fade = _compile_fade_envelope(overlay)
    raw_motion = overlay.get("motion")
    motion = None
    if (
        (effect not in {"static", "none", "slide-in", "dissolve-out"} or fade is not None)
        and isinstance(raw_motion, dict)
        and raw_motion.get("version") == 2
    ):
        motion = ResolvedTextMotion(**asdict(normalize_text_motion(effect, raw_motion)))
    text = cloud._overlay_text(overlay)
    if not text.strip():
        raise UnsupportedPortableText("empty text has no layer")
    if effect == "handwriting":
        return _compile_handwriting_overlay(
            overlay, layer_id=layer_id, canvas=canvas, motion=motion
        ), None
    original_text = text
    if effect in {"typewriter", "stream-in", "staggered-slice"}:
        text = cloud._normalize_reveal_text(text)
    shaped = effect != "staggered-slice" and (
        effect == "smooth-type" or bool(overlay.get("shape_text"))
    )
    resolved = cloud._resolve_typeface_for_overlay(overlay)
    font_asset = bundled_font_asset(resolved.file, asset_id="font-" + resolved.file)
    spacing_em = cloud.resolve_letter_spacing_em(overlay.get("letter_spacing"))
    wrap = (
        cloud._authored_lines
        if overlay.get("wrap_lines") is False
        else cloud._wrap_at_fixed_size
        if overlay.get("preserve_font_size")
        else cloud._shrink_to_fit
    )
    font, size, lines = wrap(
        text,
        resolved.typeface,
        cloud._resolve_font_size_px(overlay),
        cloud._overlay_max_width_px(overlay, canvas),
        spacing_em,
        shape_text=shaped,
    )
    spacing = spacing_em * size
    block = cloud._measure_block(
        font,
        lines,
        line_spacing=cloud.resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=spacing,
        shape_text=shaped,
    )
    cx, cy = cloud._resolve_anchor(overlay, canvas)
    anchor = cloud._resolve_text_anchor(overlay)
    top = cloud._vertical_block_top(cloud._resolve_vertical_anchor(overlay), cy, block["block_h"])
    fill, blurs, gradient = _resolve_paints(
        overlay,
        width=max(block["widths"]),
        height=block["block_h"],
        left=cloud._anchored_left_x(anchor, cx, max(block["widths"])),
        top=top,
    )
    stroke = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    runs = [
        PositionedTextRun(
            text=line,
            font_asset_id=font_asset.id,
            font_variations=resolved_font_variations(font),
            font_size=size,
            x=cloud._anchored_left_x(anchor, cx, block["widths"][index]),
            baseline_y=top + block["ascent_offset"] + index * block["line_step"],
            letter_spacing=spacing,
            shaped=shaped,
            glyphs=None if shaped else resolve_legacy_glyphs(font, line, spacing),
            fill=fill,
            stroke=_outline_ink(overlay),
            stroke_width=max(0, stroke * 2),
            blur_layers=blurs,
            gradient=gradient,
        )
        for index, line in enumerate(lines)
        if line
    ]
    reveal_bounds = None
    smooth_reveal = None
    if effect == "smooth-type":
        import unicodedata

        shadow = cloud._text_shadow_bleed_px(cloud._text_shadow_style(overlay))
        glow = 62.0 if cloud._finite_float(overlay.get("glow_strength"), 0) > 0 else 0.0
        bleed = [max(float(stroke + 2), part, glow) for part in shadow]
        smooth_lines = []
        run_index = 0
        for index, line in enumerate(lines):
            first = next(
                (
                    unicodedata.bidirectional(c)
                    for c in line
                    if unicodedata.bidirectional(c) in {"R", "AL", "AN", "L"}
                ),
                "L",
            )
            left = cloud._anchored_left_x(anchor, cx, block["widths"][index]) if line else 0
            smooth_lines.append(
                SmoothRevealLine(
                    text=line,
                    run_index=run_index if line else None,
                    first_strong_rtl=first in {"R", "AL", "AN"},
                    bounds=TextRevealBounds(
                        left=left - bleed[0],
                        top=top + index * block["line_step"] - bleed[1],
                        right=left + block["widths"][index] + bleed[2],
                        bottom=top + (index + 1) * block["line_step"] + bleed[3],
                    )
                    if line
                    else None,
                )
            )
            if line:
                run_index += 1
        smooth_reveal = SmoothRevealContent(text=original_text, lines=smooth_lines)
    discrete_reveal = None
    if effect in {"typewriter", "stream-in"}:
        cursor_style = motion.cursor_style if motion else "bar"
        cursor_text = (
            " ▮" if cursor_style == "block" else " _" if cursor_style == "underscore" else " |"
        )
        reveal_lines = []
        run_index = 0
        for index, line in enumerate(lines):
            cursor_run = PositionedTextRun.model_validate(
                {
                    **runs[0].model_dump(),
                    "text": cursor_text,
                    "x": cloud._anchored_left_x(anchor, cx, block["widths"][index] if line else 0),
                    "baseline_y": top + block["ascent_offset"] + index * block["line_step"],
                    "glyphs": None if shaped else resolve_legacy_glyphs(font, cursor_text, spacing),
                }
            )
            reveal_lines.append(
                DiscreteRevealLine(
                    text=line,
                    run_index=run_index if line else None,
                    cursor_offsets=[
                        (
                            cloud._measure_line(font, line[:count], spacing, shape_text=shaped)
                            + spacing
                        )
                        if count
                        else 0
                        for count in range(len(line) + 1)
                    ],
                    cursor_run=cursor_run,
                )
            )
            if line:
                run_index += 1
        raw_schedule = overlay.get("reveal_schedule_s")
        discrete_reveal = DiscreteRevealContent(
            text=original_text,
            schedule=[cloud._finite_float(value, overlay["start_s"]) for value in raw_schedule]
            if isinstance(raw_schedule, list)
            else None,
            lines=reveal_lines,
        )
    if effect == "ink-reveal":
        shadow_left, shadow_top, shadow_right, shadow_bottom = cloud._text_shadow_bleed_px(
            cloud._text_shadow_style(overlay)
        )
        glow_bleed = 62.0 if cloud._finite_float(overlay.get("glow_strength"), 0.0) > 0 else 0.0
        stroke_bleed = float(stroke + 2)
        left = min(cloud._anchored_left_x(anchor, cx, width) for width in block["widths"])
        right = max(cloud._anchored_left_x(anchor, cx, width) + width for width in block["widths"])
        reveal_bounds = TextRevealBounds(
            left=left - max(stroke_bleed, shadow_left, glow_bleed),
            top=top - max(stroke_bleed, shadow_top, glow_bleed),
            right=right + max(stroke_bleed, shadow_right, glow_bleed),
            bottom=top + block["block_h"] + max(stroke_bleed, shadow_bottom, glow_bleed),
        )
    background = None
    if overlay.get("background_color"):
        from app.kria.portable_text import TextBackground, TextInk

        width = max(block["widths"])
        color = cloud._skia_color_from_hex(overlay["background_color"], 255)
        background = TextBackground(
            color=TextInk(
                red=skia.ColorGetR(color) / 255,
                green=skia.ColorGetG(color) / 255,
                blue=skia.ColorGetB(color) / 255,
                alpha=1,
            ),
            left=cloud._anchored_left_x(anchor, cx, width) - 8,
            top=top - 4,
            width=width + 16,
            height=block["block_h"] + 8,
            radius=4,
        )
    return PortableTextLayer(
        id=layer_id,
        start=overlay["start_s"],
        end=overlay["end_s"],
        background=background,
        anchor_x=cx,
        anchor_y=cy,
        rotation_degrees=cloud._finite_float(overlay.get("rotation_deg"), 0),
        runs=runs,
        effect=effect,
        animation_phases=overlay.get("animation_phases"),
        dissolve_seed=dissolve_seed if effect == "dissolve-out" else None,
        motion=motion,
        fade=fade,
        reveal_bounds=reveal_bounds,
        discrete_reveal=discrete_reveal,
        smooth_reveal=smooth_reveal,
        staggered=_compile_staggered_content(
            overlay,
            text=text,
            canvas=canvas,
            font=font,
            size=size,
            font_asset=font_asset,
            spacing=spacing,
        )
        if effect == "staggered-slice"
        else None,
    ), font_asset


def _compile_handwriting_overlay(overlay: dict, *, layer_id: str, canvas, motion):
    from app.kria.portable_text import (
        HandwritingContent,
        PortableTextLayer,
        TextPenStroke,
        TextStrokePoint,
    )
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.handwriting_strokes import layout_handwriting_text

    text = cloud._overlay_text(overlay)
    size = cloud._resolve_font_size_px(overlay)
    layout = layout_handwriting_text(
        text,
        max_width_em=cloud._overlay_max_width_px(overlay, canvas) / max(size, 1),
        letter_spacing_em=cloud.resolve_letter_spacing_em(overlay.get("letter_spacing")),
        wrap_lines=overlay.get("wrap_lines", True),
        line_spacing=cloud.resolve_line_spacing(overlay.get("line_spacing")),
    )
    if not layout.strokes:
        raise UnsupportedPortableText("handwriting has no drawable pen paths")
    cx, cy = cloud._resolve_anchor(overlay, canvas)
    anchor = cloud._resolve_text_anchor(overlay)
    top = cloud._vertical_block_top(
        cloud._resolve_vertical_anchor(overlay), cy, layout.height_em * size
    )
    fill, blurs, gradient = _resolve_paints(
        overlay,
        width=max(layout.width_em * size, 1),
        height=max(layout.height_em * size, 1),
        left=cloud._anchored_left_x(anchor, cx, layout.width_em * size),
        top=top,
    )
    strokes = []
    for stroke in layout.strokes:
        left = cloud._anchored_left_x(anchor, cx, layout.line_widths_em[stroke.line_index] * size)
        strokes.append(
            TextPenStroke(
                points=[
                    TextStrokePoint(x=left + x * size, y=top + y * size) for x, y in stroke.points
                ],
                start_progress=stroke.start_progress,
                end_progress=stroke.end_progress,
            )
        )
    return PortableTextLayer(
        id=layer_id,
        start=overlay["start_s"],
        end=overlay["end_s"],
        anchor_x=cx,
        anchor_y=cy,
        rotation_degrees=cloud._finite_float(overlay.get("rotation_deg"), 0),
        effect="handwriting",
        motion=motion,
        fade=_compile_fade_envelope(overlay),
        handwriting=HandwritingContent(
            text=text,
            strokes=strokes,
            ink_width=max(1, layout.stroke_width_em * size),
            fill=fill,
            outline=_outline_ink(overlay),
            outline_width=max(
                0, float(overlay.get("outline_px") or overlay.get("stroke_width") or 0) * 2
            ),
            blur_layers=blurs,
            gradient=gradient,
        ),
    )


def _compile_staggered_content(overlay, *, text, canvas, font, size, font_asset, spacing):
    from app.kria.portable_text import PositionedTextRun, StaggeredContent, StaggeredGlyph
    from app.pipeline import text_overlay_skia as cloud

    max_width = cloud._overlay_max_width_px(overlay, canvas)
    rows = [
        (index, row)
        for index, line in enumerate(text.split("\n"))
        for row in (
            [line]
            if overlay.get("wrap_lines") is False
            else cloud._wrap_text_to_lines(line, font, max_width, spacing)
        )
    ]
    block = cloud._measure_block(
        font,
        [row for _, row in rows],
        line_spacing=cloud.resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=spacing,
    )
    cx, cy = cloud._resolve_anchor(overlay, canvas)
    anchor = cloud._resolve_text_anchor(overlay)
    top = cloud._vertical_block_top(cloud._resolve_vertical_anchor(overlay), cy, block["block_h"])
    fill, blurs, gradient = _resolve_paints(
        overlay,
        width=max(block["widths"]),
        height=block["block_h"],
        left=cloud._anchored_left_x(anchor, cx, max(block["widths"])),
        top=top,
    )
    logical_glyphs = [cloud._segment_graphemes(line) for line in text.split("\n")]
    cursors = [0] * len(logical_glyphs)
    seen = set()
    glyphs = []
    for row_index, (logical_index, row) in enumerate(rows):
        source = logical_glyphs[logical_index]
        cursor = cursors[logical_index]
        if logical_index in seen:
            while cursor < len(source) and source[cursor].isspace():
                cursor += 1
        seen.add(logical_index)
        x = cloud._anchored_left_x(anchor, cx, block["widths"][row_index])
        baseline = top + block["ascent_offset"] + row_index * block["line_step"]
        for grapheme in cloud._segment_graphemes(row):
            if cursor >= len(source):
                break
            if source[cursor] != grapheme:
                cursor = next(
                    (i for i in range(cursor, len(source)) if source[i] == grapheme), cursor
                )
            width = font.measureText(grapheme)
            glyphs.append(
                StaggeredGlyph(
                    logical_line=logical_index,
                    glyph_index=cursor,
                    pivot_x=x + width / 2,
                    pivot_y=baseline - size * 0.4,
                    run=PositionedTextRun(
                        text=grapheme,
                        font_asset_id=font_asset.id,
                        font_variations=resolved_font_variations(font),
                        font_size=size,
                        x=x,
                        baseline_y=baseline,
                        letter_spacing=spacing,
                        shaped=False,
                        glyphs=resolve_legacy_glyphs(font, grapheme, spacing),
                        fill=fill,
                        stroke=_outline_ink(overlay),
                        stroke_width=max(
                            0,
                            int(overlay.get("outline_px") or overlay.get("stroke_width") or 0) * 2,
                        ),
                        blur_layers=blurs,
                        gradient=gradient,
                    ),
                )
            )
            x += width + spacing
            cursor += 1
        cursors[logical_index] = cursor
    return StaggeredContent(text=text, glyphs=glyphs)


def _compile_karaoke_overlay(overlay: dict, *, layer_id: str, canvas):
    from app.kria.portable_text import KaraokeContent, PortableTextLayer, PositionedTextRun
    from app.pipeline import text_overlay_skia as cloud
    from app.services.render_library import bundled_font_asset

    words, starts = [], []
    acc = 0.0
    for entry in overlay.get("word_timings") or []:
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        start = cloud._finite_float(entry.get("start_s"), acc)
        fallback = max(0.05, cloud._finite_float(entry.get("duration_cs"), 5.0) / 100)
        end = cloud._finite_float(entry.get("end_s"), start + fallback)
        duration = end - start if end > start else fallback
        words.append(text)
        starts.append(max(0.0, start))
        acc = max(acc, max(0.0, start) + duration)
    if not words:
        # This handler ignores display_text, including in its static fallback.
        return compile_text_overlay(
            {**overlay, "effect": "static", "display_text": None}, layer_id=layer_id, canvas=canvas
        )
    resolved = cloud._resolve_typeface_for_overlay(overlay)
    cloud.assert_lyric_glyphs(resolved.typeface, " ".join(words))
    asset = bundled_font_asset(resolved.file, asset_id="font-" + resolved.file)
    size = cloud._resolve_font_size_px(overlay)
    font = skia.Font(resolved.typeface, size)
    font.setSubpixel(True)
    spacing = cloud._overlay_letter_spacing_px(overlay, size)
    rows = cloud._wrap_word_indices(
        words, font, cloud._overlay_max_width_px(overlay, canvas), spacing
    )
    widths = [cloud._measure_line(font, word, spacing) for word in words]
    gap = font.measureText(" ") + 2 * spacing
    block = cloud._measure_block(
        font,
        [" ".join(words[i] for i in row) for row in rows],
        line_spacing=cloud.resolve_line_spacing(overlay.get("line_spacing")),
        letter_spacing_px=spacing,
    )
    cx, cy = cloud._resolve_anchor(overlay, canvas)
    top = cloud._vertical_block_top(cloud._resolve_vertical_anchor(overlay), cy, block["block_h"])
    fill, blurs, _ = _resolve_paints(
        {**overlay, "text_gradient": None}, width=1, height=1, left=0, top=0
    )
    highlight, _, _ = _resolve_paints(
        {"text_color": overlay.get("highlight_color") or "#FFD24A"},
        width=1,
        height=1,
        left=0,
        top=0,
    )
    runs, ordered_starts = [], []
    for row_index, row in enumerate(rows):
        width = sum(widths[i] for i in row) + gap * max(0, len(row) - 1)
        x = cloud._anchored_left_x(cloud._resolve_text_anchor(overlay), cx, width)
        for i in row:
            runs.append(
                PositionedTextRun(
                    text=words[i],
                    font_asset_id=asset.id,
                    font_variations=resolved_font_variations(font),
                    font_size=size,
                    x=x,
                    baseline_y=top + block["ascent_offset"] + row_index * block["line_step"],
                    letter_spacing=spacing,
                    shaped=False,
                    glyphs=resolve_legacy_glyphs(font, words[i], spacing),
                    fill=fill,
                    stroke=_outline_ink(overlay),
                    stroke_width=max(
                        0, int(overlay.get("outline_px") or overlay.get("stroke_width") or 0) * 2
                    ),
                    blur_layers=blurs,
                )
            )
            ordered_starts.append(starts[i])
            x += widths[i] + gap
    return PortableTextLayer(
        id=layer_id,
        start=overlay["start_s"],
        end=overlay["end_s"],
        anchor_x=cx,
        anchor_y=cy,
        rotation_degrees=cloud._finite_float(overlay.get("rotation_deg"), 0),
        runs=runs,
        effect="karaoke-line",
        karaoke=KaraokeContent(starts=ordered_starts, highlight=highlight),
    ), asset


def _compile_fade_envelope(overlay: dict):
    from app.kria.portable_text import TextFadeEnvelope
    from app.pipeline import text_overlay_skia as cloud

    curve = "sqrt" if overlay.get("fade_out_curve") == "sqrt" else "square"
    if overlay.get("effect") == "lyric-line":
        return TextFadeEnvelope(
            kind="lyric",
            in_ms=max(
                0, int(overlay.get("fade_in_ms") if overlay.get("fade_in_ms") is not None else 150)
            ),
            out_ms=max(
                0,
                int(overlay.get("fade_out_ms") if overlay.get("fade_out_ms") is not None else 250),
            ),
            curve=curve,
        )
    if cloud._is_sequence_overlay(overlay) and cloud._sequence_fade_out_ms(overlay) > 0:
        return TextFadeEnvelope(
            kind="sequence", in_ms=0, out_ms=cloud._sequence_fade_out_ms(overlay), curve=curve
        )
    # Other effects ignore these fields in the production dispatcher.
    return None


def _compile_pop_suffix_overlay(overlay: dict, *, layer_id: str, canvas):
    """The cloud suffix handler is a settled split line, with no entrance motion."""
    from app.kria.portable_text import PortableTextLayer, PositionedTextRun
    from app.pipeline import text_overlay_skia as cloud
    from app.services.render_library import bundled_font_asset

    text = cloud._overlay_text(overlay)
    suffix = overlay.get("pop_animated_suffix") or ""
    if not suffix or not text.endswith(suffix):
        return None
    resolved = cloud._resolve_typeface_for_overlay(overlay)
    cloud.assert_lyric_glyphs(resolved.typeface, text)
    size = cloud._resolve_font_size_px(overlay)
    width = cloud._overlay_max_width_px(overlay, canvas)
    anchor = cloud._resolve_text_anchor(overlay)
    args = (text, resolved.typeface, size, width)
    if overlay.get("wrap_lines") is False:
        font, size, lines = cloud._authored_lines(*args)
    elif overlay.get("preserve_font_size"):
        font, size, lines = cloud._wrap_at_fixed_size(*args)
    elif anchor == "left":
        font, size, lines = cloud._shrink_to_fit(*args)
    else:
        font, size, lines = cloud._shrink_to_fit_max_lines(*args, cloud._POP_SUFFIX_MAX_LINES)
    if not lines or not lines[-1].rstrip().endswith(suffix):
        return None
    asset = bundled_font_asset(resolved.file, asset_id="font-" + resolved.file)
    block = cloud._measure_block(font, lines)
    cx, cy = cloud._resolve_anchor(overlay, canvas)
    top = cloud._vertical_block_top(cloud._resolve_vertical_anchor(overlay), cy, block["block_h"])
    fill, blurs, _ = _resolve_paints(
        {**overlay, "text_gradient": None}, width=1, height=1, left=0, top=0
    )
    prefix = lines[-1].rstrip()[: -len(suffix)].rstrip()
    full_width = font.measureText(lines[-1])
    draws = []
    for index, line in enumerate([*lines[:-1], prefix]):
        if line:
            anchor_width = full_width if index == len(lines) - 1 else font.measureText(line)
            draws.append((line, cloud._anchored_left_x(anchor, cx, anchor_width), index))
    suffix_x = cloud._anchored_left_x(anchor, cx, full_width) + font.measureText(
        prefix + " " if prefix else ""
    )
    draws.append((suffix, suffix_x, len(lines) - 1))
    runs = [
        PositionedTextRun(
            text=line,
            font_asset_id=asset.id,
            font_variations=resolved_font_variations(font),
            font_size=size,
            x=x,
            baseline_y=top + block["ascent_offset"] + index * block["line_step"],
            letter_spacing=0,
            shaped=False,
            glyphs=resolve_legacy_glyphs(font, line, 0),
            fill=fill,
            stroke=_outline_ink(overlay),
            stroke_width=max(
                0, int(overlay.get("outline_px") or overlay.get("stroke_width") or 0) * 2
            ),
            blur_layers=blurs,
        )
        for line, x, index in draws
    ]
    return PortableTextLayer(
        id=layer_id,
        start=overlay["start_s"],
        end=overlay["end_s"],
        anchor_x=cx,
        anchor_y=cy,
        rotation_degrees=cloud._finite_float(overlay.get("rotation_deg"), 0),
        runs=runs,
        effect="static",
    ), asset
