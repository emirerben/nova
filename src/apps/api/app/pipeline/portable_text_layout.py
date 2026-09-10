"""Compile text layout decisions for the phone; never generate raster media."""

from __future__ import annotations

import skia

from app.kria.portable_text import PositionedGlyph


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


def compile_text_overlay(overlay: dict, *, layer_id: str, canvas):
    """Resolve a supported cloud overlay into geometry plus its exact font asset.

    Reuses the production layout helpers. No image, frame, or encoded media is
    generated. Specialized reveal/lyric dispatch remains explicitly unsupported.
    """
    import math
    from dataclasses import asdict

    from app.kria.portable_text import (
        PortableTextLayer,
        PositionedTextRun,
        ResolvedTextMotion,
        TextBlurLayer,
        TextGradient,
        TextGradientStop,
        TextInk,
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
        "pop-in",
        "bounce",
    }:
        raise UnsupportedPortableText(f"unsupported text effect: {effect}")
    # These fields invoke specialized drawing or timing outside the base line
    # painter. Accepting their base text would silently lose creator intent.
    for key in (
        "emoji",
        "emoji_prefix",
        "pop_animated_suffix",
        "highlight_word",
        "fade_out_ms",
        "fade_in_ms",
        "karaoke_words",
        "theme_transition",
        "behind_subject",
        "spans",
        "masonry_layer_origin_x_px",
    ):
        if overlay.get(key):
            raise UnsupportedPortableText(f"unsupported text treatment: {key}")
    raw_motion = overlay.get("motion")
    motion = None
    if (
        effect not in {"static", "none"}
        and isinstance(raw_motion, dict)
        and raw_motion.get("version") == 2
    ):
        motion = ResolvedTextMotion(**asdict(normalize_text_motion(effect, raw_motion)))
    text = cloud._overlay_text(overlay)
    if not text.strip():
        raise UnsupportedPortableText("empty text has no layer")
    shaped = bool(overlay.get("shape_text"))
    resolved = cloud._resolve_typeface_for_overlay(overlay)
    font_asset = bundled_font_asset(resolved.file, asset_id="font-" + resolved.file)
    spacing_em = cloud.resolve_letter_spacing_em(overlay.get("letter_spacing"))
    wrap = cloud._wrap_at_fixed_size if overlay.get("preserve_font_size") else cloud._shrink_to_fit
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
                    dx=0,
                    dy=0,
                )
            )
    for shadow in cloud._text_shadow_layers(cloud._text_shadow_style(overlay)):
        blurs.append(
            TextBlurLayer(
                color=TextInk(red=0, green=0, blue=0, alpha=shadow.alpha / 255),
                sigma=shadow.sigma,
                dx=shadow.dx,
                dy=shadow.dy,
            )
        )
    gradient = None
    parsed = cloud._parse_gradient_spec(overlay.get("text_gradient"))
    if parsed:
        width = max(block["widths"])
        left = cloud._anchored_left_x(anchor, cx, width)
        angle = math.radians(parsed["angle_deg"] % 360)
        cosine, sine = math.cos(angle), math.sin(angle)
        half = (abs(width * cosine) + abs(block["block_h"] * sine)) / 2
        center_x, center_y = left + width / 2, top + block["block_h"] / 2
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
    stroke = int(overlay.get("outline_px") or overlay.get("stroke_width") or 0)
    runs = [
        PositionedTextRun(
            text=line,
            font_asset_id=font_asset.id,
            font_size=size,
            x=cloud._anchored_left_x(anchor, cx, block["widths"][index]),
            baseline_y=top + block["ascent_offset"] + index * block["line_step"],
            letter_spacing=spacing,
            shaped=shaped,
            glyphs=None if shaped else resolve_legacy_glyphs(font, line, spacing),
            fill=fill,
            stroke=TextInk(red=0, green=0, blue=0, alpha=230 / 255),
            stroke_width=max(0, stroke * 2),
            blur_layers=blurs,
            gradient=gradient,
        )
        for index, line in enumerate(lines)
        if line
    ]
    return PortableTextLayer(
        id=layer_id,
        start=overlay["start_s"],
        end=overlay["end_s"],
        anchor_x=cx,
        anchor_y=cy,
        rotation_degrees=cloud._finite_float(overlay.get("rotation_deg"), 0),
        runs=runs,
        effect=effect,
        motion=motion,
    ), font_asset
