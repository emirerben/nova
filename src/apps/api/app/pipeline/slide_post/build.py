"""FFmpeg-backed slide normalization, preview stitching, and export-bundle
assembly for mixed-media "slide posts" (ordered images + videos).

Pure filesystem functions — no GCS, no DB. The caller (the dispatch task in
`app/tasks/generative_build.py`) owns download/upload; every function here
takes and returns local paths. Mirrors the separation already established by
`app/pipeline/image_clip.py`.

Encoder policy: the normalized per-slide derivatives and the stitched preview
are the bytes that ship in the export bundle and the hero preview — FINAL
output, `preset="fast"` or stricter (see CLAUDE.md "Encoder policy"). This
module does NOT call the shared `app.pipeline.reframe._encoding_args` helper
— that helper is coupled to the main reframe pipeline's HDR/canvas handling,
which a slide post (a still-image-heavy carousel, not a continuous shot) does
not need. Same quality policy, a simpler direct implementation; the guard at
`tests/test_encoder_policy.py` audits `_encoding_args` call sites specifically
and does not need to know about this module.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from app.config import settings
from app.kria.framing import LANDSCAPE_CANVAS, ffmpeg_crop_filter
from app.pipeline.look_presets import look_preset_filter
from app.pipeline.slide_post.stickers import (
    StickerGeometry,
    prepare_sticker_png,
    sticker_filter_chain,
    sticker_geometry,
    sticker_overlay,
)

if TYPE_CHECKING:
    from app.schemas.slide_post import SlideEdits, SlideTilt

log = structlog.get_logger()

Canvas = tuple[int, int]

# The only font a v1 slide-text overlay uses — matches the product's own UI
# font (DESIGN.md). No font picker in v1 (plans/024 follow-up eng-review).
_SLIDE_TEXT_FONT = str(Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Inter-Bold.ttf")

# Default per-image hold in the stitched PREVIEW (the export bundle's own
# image files are full-resolution stills with no duration concept — this
# only paces the scrubbable preview MP4).
DEFAULT_IMAGE_HOLD_S = 2.5
# A video slide's full length always ships in the export bundle; the preview
# only plays a capped slice of it so the preview stays short regardless of
# how many/how long the video slides are.
MAX_PREVIEW_VIDEO_SLICE_S = 4.0

_FINAL_PRESET = "fast"
_FINAL_CRF = "20"

# Every slide carries the Kria watermark (KRI-472): the same mark, tone and
# anchor the iOS engine burns into phone-made videos (`KriaBranding` in
# KriaMediaEngine/Branding.swift; placement rationale in
# brand/social/README.md). The PNG is a copy that brand/social/build.py writes
# from brand/social/dist, and a test fails if it drifts. Opacity and shadow
# are baked into the file, so it is composited as-is.
_WATERMARK_PNG = str(
    Path(__file__).resolve().parents[3] / "assets" / "branding" / "kria-watermark-mist-standard.png"
)
# Authored on 1080x1920. The PNG carries a 30px transparent pad for its
# shadow, so the tile's origin sits 30px left of and below the mark's: mark
# left edge at x=60, mark bottom edge 445px above the bottom of the frame.
_WATERMARK_REF_CANVAS: Canvas = (1080, 1920)
_WATERMARK_TILE_LEFT = 30
_WATERMARK_TILE_BOTTOM_INSET = 415
# Part of every normalized derivative's cache key and stamped on the rendered
# variant. Bump it when the mark, its placement, or how it is composited
# changes, so cached derivatives rebuild and older exports are re-rendered
# before they can be downloaded.
SLIDE_WATERMARK_VERSION = 1
# Part of the normalized key of a slide that carries a drawing (and only of those,
# so every existing key is unchanged). Bump it when `drawing_png` paints differently.
SLIDE_DRAWING_PAINTER_VERSION = 1
# Part of the normalized key of a slide that carries stickers (and only of those). Bump it
# when the sticker decode/scale/rotate/overlay recipe changes.
SLIDE_STICKER_COMPOSITOR_VERSION = 1
# Part of the normalized key of a tilted slide (and only of those). Bump it when the turn,
# its backdrop or how they are composited changes.
SLIDE_TILT_COMPOSITOR_VERSION = 1


class SlideBuildError(Exception):
    """Raised when FFmpeg fails to normalize, stitch, or extract a slide."""


def _run_ffmpeg(cmd: list[str], *, context: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        non_empty = [ln for ln in stderr.splitlines() if ln.strip()]
        tail = " | ".join(non_empty[-3:]) if non_empty else f"ffmpeg exited {proc.returncode}"
        log.error(
            "slide_post_ffmpeg_failed",
            context=context,
            returncode=proc.returncode,
            stderr=stderr,
        )
        raise SlideBuildError(f"{context}: {tail}")


def probe_dimensions(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=,",
            path,
        ],
        text=True,
    ).strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def probe_duration_s(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        text=True,
    ).strip()
    return float(out)


# Gaussian sigma of the Fit "blur" bars, in full-resolution pixels. The filter
# runs on a quarter-size copy (sigma 20); the phone mirrors this as a CIGaussianBlur
# radius of this value x its output scale. Pinned by the parity fixture.
FIT_BLUR_SIGMA_PX = 80
_FIT_BLUR_DOWNSCALE = 4


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def _scale_crop_filter(canvas: Canvas, source_crop: dict[str, float] | None = None) -> str:
    """Fill: cover-fit the (cropped) photo into the canvas."""
    return _frame_filter(canvas, source_crop, None)


def _frame_filter(canvas: Canvas, source_crop: dict[str, float] | None, fit: str | None) -> str:
    """Frame the photo in the canvas: fill (cover-crop) or fit with bars (KRI-606).

    A framing `source_crop` (upright space) is cut out first, so both the cover
    fit and the Fit are of that region -- the same order the device and `reframe`
    use. ``fit=None`` is the historical cover-fit, byte for byte.

    The blur variant is one graph whose first and last chains are unlabelled, so
    `_overlay_filter_complex`'s ``[0:v]{vf}[b0]`` wrap and the ``,``-joined look
    fragment still work; its ``k*`` labels never collide with ``b0..bN``/``wm``.
    ``format=auto`` matches every other overlay here (the default would convert
    Fit slides to limited-range 4:2:0 and shift their colour against Fill ones).
    """
    cw, ch = canvas
    pre = ffmpeg_crop_filter(source_crop)
    if fit is None:
        # Cover-fit: scale so the shorter axis fills the target, then center-crop
        # the overflow. Same fit policy as image_to_video._normalize_to_9x16,
        # expressed as one ffmpeg filter instead of a PIL resize+crop pass.
        cover = f"scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch}"
        return f"{pre},{cover}" if pre else cover
    head = f"{pre}," if pre else ""
    if fit in ("black", "white"):
        return (
            f"{head}scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color={fit},setsar=1"
        )
    if fit == "blur":
        bw, bh = _even(cw / _FIT_BLUR_DOWNSCALE), _even(ch / _FIT_BLUR_DOWNSCALE)
        sigma = FIT_BLUR_SIGMA_PX // _FIT_BLUR_DOWNSCALE
        return (
            f"{head}split=2[kfg][kbg];"
            f"[kbg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"gblur=sigma={sigma},scale={cw}:{ch}[kbgo];"
            f"[kfg]scale={cw}:{ch}:force_original_aspect_ratio=decrease[kfgo];"
            f"[kbgo][kfgo]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1"
        )
    raise SlideBuildError(f"unknown fit {fit!r}")


def _tilt_frame_filter(
    canvas: Canvas, source_crop: dict[str, float] | None, fit: str | None, tilt: SlideTilt
) -> str:
    """Frame the media, turn it about the canvas centre, lay it over the tilt's backdrop (KRI-1138).

    The media is the (cropped) region scaled like the slide frames it: cover for Fill, whole
    for Fit (``tilt.py``). It is NOT trimmed to the canvas before it turns, so the pixels a
    Fill photo has beyond the frame rotate into view. The Fit bars do not draw: the tilt's
    backdrop is what shows behind a turned photo. ``rotate`` is clockwise for a positive angle
    and ``c=none`` leaves the corners transparent for the overlay.

    Same graph convention as ``_frame_filter``: unlabelled first and last chains, so
    ``_overlay_filter_complex``'s ``[0:v]{vf}[b0]`` wrap and the ``,``-joined look still work.
    ``format=auto`` matches every other overlay here (see ``_frame_filter``).
    """
    cw, ch = canvas
    head = f"{ffmpeg_crop_filter(source_crop)}," if source_crop else ""
    rad = f"{math.radians(tilt.rotation_deg):.9f}"
    mode = "decrease" if fit is not None else "increase"
    media = (
        f"scale={cw}:{ch}:force_original_aspect_ratio={mode},format=rgba,"
        f"rotate={rad}:ow='rotw({rad})':oh='roth({rad})':c=none"
    )
    backdrop = tilt.backdrop
    if backdrop.kind == "blur":
        bw, bh = _even(cw / _FIT_BLUR_DOWNSCALE), _even(ch / _FIT_BLUR_DOWNSCALE)
        sigma = FIT_BLUR_SIGMA_PX // _FIT_BLUR_DOWNSCALE
        return (
            f"{head}split=2[tfg][tbg];"
            f"[tbg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"gblur=sigma={sigma},scale={cw}:{ch}[tbgo];"
            f"[tfg]{media}[tfgo];"
            f"[tbgo][tfgo]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1"
        )
    colour = "black" if backdrop.kind == "blank" else f"0x{(backdrop.color or '#000000')[1:]}"
    return (
        f"{head}{media}[tfgo];"
        f"color=c={colour}:s={cw}x{ch}:r=30,format=rgb24[tbgo];"
        f"[tbgo][tfgo]overlay=(W-w)/2:(H-h)/2:format=auto:shortest=1,setsar=1"
    )


def tilt_key_suffix(edits: SlideEdits | None) -> str:
    """`_tl{N}` for a tilted slide, "" otherwise (existing keys stay put)."""
    if edits is None or edits.tilt is None:
        return ""
    return f"_tl{SLIDE_TILT_COMPOSITOR_VERSION}"


def slide_canvas(profile: str, edits: SlideEdits | None, aspect: str | None = None) -> Canvas:
    """The canvas this slide exports at: the post's size, or 1920x1080 when the
    slide is landscape and the profile allows mixing shapes (KRI-44)."""
    from app.pipeline.slide_post.profiles import PLATFORM_PROFILES, post_canvas  # noqa: PLC0415

    spec = PLATFORM_PROFILES[profile]  # type: ignore[index]
    framing = edits.framing if edits is not None else None
    if framing is not None and framing.orientation == "landscape" and spec.allows_landscape:
        return LANDSCAPE_CANVAS
    return post_canvas(profile, aspect)  # type: ignore[arg-type]


def _framing_crop(edits: SlideEdits | None) -> dict[str, float] | None:
    framing = edits.framing if edits is not None else None
    if framing is None or framing.source_crop is None:
        return None
    return framing.source_crop.model_dump()


def _drawtext_y_expr(position: str) -> str:
    if position == "top":
        return "h*0.08"
    if position == "bottom":
        return "h*0.85-text_h"
    return "(h-text_h)/2"


def _escape_drawtext_path(path: str) -> str:
    """Escape a filesystem path for use as a drawtext option VALUE
    (`fontfile=`/`textfile=`). Only the filtergraph's own delimiters need
    escaping here — unlike `text=`, there is no separate FFmpeg-internal
    quoting layer for these options."""
    return path.replace("\\", "\\\\").replace(":", "\\:")


@contextmanager
def _edits_filter_fragment(
    edits: SlideEdits | None, *, canvas: Canvas, out_path: str, include_text: bool = True
) -> Iterator[str | None]:
    """Build the FFmpeg `-vf` fragment for one slide's edits, or None.

    Appended after the crop/scale filter and before any fps filter — same
    ordering `look_presets.py`'s own docstring specifies for every other
    render path (crop/HDR/recipe hint, then look, then text/overlays).

    Text goes through `textfile=`, not an inline escaped `text=` value.
    FFmpeg's drawtext has two escaping layers (the filtergraph parser, and
    drawtext's own single-quote grouping around the text value) that
    interact badly with a literal apostrophe in the text — `textfile=` reads
    the file's raw bytes as the message with neither layer applied, which
    is the standard escape hatch for exactly this class of dynamic text.
    """
    if edits is None:
        yield None
        return
    fragments: list[str] = []
    cw, ch = canvas
    look_fragment = look_preset_filter(edits.look_preset, width=cw, height=ch)
    if look_fragment:
        fragments.append(look_fragment)
    draw_legacy = include_text and edits.text is not None
    text_file = Path(f"{out_path}.drawtext.txt") if draw_legacy else None
    try:
        if draw_legacy and edits.text is not None and text_file is not None:
            text_file.write_text(edits.text.content, encoding="utf-8")
            fontsize = max(24, round(ch * 0.045))
            box_border = max(12, round(fontsize * 0.3))
            fragments.append(
                "drawtext="
                f"fontfile={_escape_drawtext_path(_SLIDE_TEXT_FONT)}:"
                f"textfile={_escape_drawtext_path(str(text_file))}:"
                # drawtext expands `%{...}`/strftime-style sequences even
                # when reading from textfile= — a literal "%" in ordinary
                # user text (e.g. "50% off") logs "Stray %" and drops the
                # ENTIRE overlay silently (exit code 0, no exception, no
                # visible text). expansion=none turns this off; there is no
                # legitimate use for frame-number/timestamp expansion in a
                # static per-slide caption. Found via manual local
                # verification — the automated tests below only asserted
                # the render didn't raise, not that the text was visible.
                "expansion=none:"
                f"fontsize={fontsize}:fontcolor=white:"
                f"box=1:boxcolor=black@0.45:boxborderw={box_border}:"
                f"x=(w-text_w)/2:y={_drawtext_y_expr(edits.text.position)}"
            )
        yield ",".join(fragments) if fragments else None
    finally:
        if text_file is not None and text_file.exists():
            text_file.unlink()


# ---- Rich per-slide text (KRI-298): Pillow PNG overlays -------------------------------

_RICH_Y_FRAC = {"top": 0.12, "center": 0.5, "bottom": 0.82}
_RICH_X_FRAC = {"left": 0.08, "center": 0.5, "right": 0.92}
_RICH_BOX_RGBA = (0, 0, 0, 115)  # black @ 0.45, same as the legacy drawtext box
_FULL_CANVAS = (1080, 1920)  # text_overlay._draw_text_png's fixed raster


def rich_text_active(edits: SlideEdits | None) -> bool:
    """True when this slide renders through the PNG-overlay path."""
    return edits is not None and edits.texts is not None and settings.slide_post_rich_text_enabled


def edits_cache_digest(
    edits: SlideEdits | None, sticker_fingerprints: dict[str, str | None] | None = None
) -> str:
    """Content-address component for a slide's normalized derivative.

    Legacy-only edits hash exactly as before `texts` existed (the field is
    excluded when None), so existing cached derivatives stay valid. A slide
    with `texts` gets a path-specific digest so flipping
    `slide_post_rich_text_enabled` never reuses the other path's pixels.

    A slide with stickers also hashes each sticker's asset content fingerprint
    (``sticker_fingerprints``: sticker id -> fingerprint, ``None`` = the asset could not be
    resolved and the sticker is dropped), so replacing a sticker's image re-renders; the
    geometry is already part of the dumped edits. Slides without stickers are unchanged.
    """
    if edits is None:
        return "noedits"
    if edits.texts is None:
        payload = edits.model_dump_json(exclude={"texts"})
    else:
        payload = edits.model_dump_json() + ("|rich" if rich_text_active(edits) else "|legacy")
    if edits.stickers:
        fingerprints = sticker_fingerprints or {}
        payload += "|st:" + ",".join(
            f"{st.id}={fingerprints.get(st.id) or 'missing'}" for st in edits.stickers
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _hex_rgba(color: str) -> tuple[int, int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), 255)


def render_text_element_png(element, png_path: str, *, canvas: Canvas) -> None:
    """Render one `SlideTextElement` to a canvas-sized transparent PNG.

    `_draw_text_png` always rasters 1080x1920. For a shorter canvas (4:5) the
    element's canvas-relative y is remapped into the centered band of that
    raster, `y' = (band_top + y * canvas_h) / 1920`, then the band is cropped
    out, so placement matches the client preview on both profiles.
    """
    from PIL import Image  # noqa: PLC0415

    from app.pipeline import text_overlay  # noqa: PLC0415

    cw, ch = canvas
    full_w, full_h = _FULL_CANVAS
    if element.position == "custom":
        y = 0.5 if element.y_frac is None else element.y_frac
    else:
        y = _RICH_Y_FRAC[element.position]
    x = _RICH_X_FRAC[element.alignment] if element.x_frac is None else element.x_frac
    # The band keeps the destination's aspect at the raster's width, so the final
    # resize is uniform: 1080x1920 -> the whole raster, 1080x1350 -> a 1350-row
    # band (unchanged), 1920x1080 -> a 608-row band scaled up 1.78x both ways.
    band_h = min(full_h, round(full_w * ch / cw))
    band_top = (full_h - band_h) // 2
    y_full = (band_top + y * band_h) / full_h
    from app.agents._schemas.text_element import apply_text_case  # noqa: PLC0415

    # Same overlay-dict -> Pillow paint mapping the video editor uses, so the
    # parity fields (rotation, stroke/shadow color, highlight, spacing) render
    # identically. Unset fields are omitted => legacy pixels.
    overlay = {
        key: getattr(element, key)
        for key in (
            "stroke_color",
            "shadow_color",
            "shadow_opacity",
            "background_color",
            "rotation_deg",
            "max_width_frac",
            "wrap_lines",
        )
        if getattr(element, key) is not None
    }
    paint = text_overlay._authored_pillow_paint(overlay)
    if element.background == "box":
        # Legacy box keeps its look; an explicit background_color wins.
        paint.setdefault("background_color", _RICH_BOX_RGBA)
    # Rotation is applied by _draw_text_png on the full 1080x1920 raster
    # (pivot = the text anchor), BEFORE the 4:5 band crop below, so rotated
    # text near the band edge is cropped like any other pixels, never clipped
    # early.
    text_overlay._draw_text_png(
        apply_text_case(element.text, element.text_case),
        "center",
        png_path,
        font_family=element.font_family,
        text_size_px=round(element.size_px),
        text_color=_hex_rgba(element.color),
        position_x_frac=x,
        position_y_frac=y_full,
        text_anchor=element.alignment,
        vertical_anchor="center",
        stroke_width=round(element.stroke_width),
        shadow_enabled=element.shadow_enabled,
        letter_spacing=element.letter_spacing,
        line_spacing=element.line_spacing,
        **paint,
    )
    if (cw, ch) != _FULL_CANVAS:
        with Image.open(png_path) as raster:
            img = raster.convert("RGBA")
        if band_h < full_h:
            img = img.crop((0, band_top, full_w, band_top + band_h))
        if img.size != (cw, ch):
            img = img.resize((cw, ch), Image.LANCZOS)
        img.save(png_path)


@contextmanager
def _text_pngs(edits: SlideEdits | None, *, canvas: Canvas, out_path: str) -> Iterator[list[str]]:
    """Yield the PNG overlay paths for a rich slide ([] when not applicable);
    always cleans up."""
    paths: list[str] = []
    try:
        if rich_text_active(edits) and edits is not None and edits.texts:
            for i, element in enumerate(edits.texts):
                png = f"{out_path}.text{i}.png"
                paths.append(png)
                render_text_element_png(element, png, canvas=canvas)
        yield paths
    finally:
        for png in paths:
            Path(png).unlink(missing_ok=True)


def drawing_key_suffix(edits: SlideEdits | None) -> str:
    """`_dr{N}` for a slide with a drawing, "" otherwise (existing keys stay put)."""
    if edits is None or edits.drawing is None:
        return ""
    return f"_dr{SLIDE_DRAWING_PAINTER_VERSION}"


@contextmanager
def _drawing_pngs(
    edits: SlideEdits | None, *, canvas: Canvas, out_path: str
) -> Iterator[list[str]]:
    """Yield ``[png]`` for a slide with a drawing, ``[]`` otherwise; always cleans up.

    Independent of `rich_text_active`: ink is painted from strokes whatever path the
    text takes. Skia is imported only here, so drawing-free renders never need it.
    """
    paths: list[str] = []
    try:
        if edits is not None and edits.drawing is not None:
            from app.pipeline.slide_post.drawing_png import render_drawing_png  # noqa: PLC0415

            png = f"{out_path}.drawing.png"
            paths.append(png)
            render_drawing_png(edits.drawing, png, canvas=canvas)
        yield paths
    finally:
        for png in paths:
            Path(png).unlink(missing_ok=True)


def sticker_key_suffix(edits: SlideEdits | None) -> str:
    """`_st{N}` for a slide with stickers, "" otherwise (existing keys stay put)."""
    if edits is None or not edits.stickers:
        return ""
    return f"_st{SLIDE_STICKER_COMPOSITOR_VERSION}"


@contextmanager
def _sticker_inputs(
    edits: SlideEdits | None,
    sticker_sources: dict[str, str] | None,
    *,
    canvas: Canvas,
    out_path: str,
) -> Iterator[list[tuple[str, StickerGeometry]]]:
    """Yield ``[(rgba_png, geometry)]`` for the slide's stickers in z-order; always cleans up.

    ``sticker_sources`` maps a sticker id to the local original the caller downloaded. A
    sticker with no source, or whose file cannot be decoded, is DROPPED (logged), never
    fatal: the same best-effort posture as a missing slide asset.
    """
    resolved: list[tuple[str, StickerGeometry]] = []
    try:
        if edits is not None and edits.stickers:
            for i, sticker in enumerate(edits.stickers):
                source = (sticker_sources or {}).get(sticker.id)
                if not source:
                    log.warning("slide_sticker_dropped", sticker_id=sticker.id, reason="no_source")
                    continue
                png = f"{out_path}.sticker{i}.png"
                try:
                    size = prepare_sticker_png(source, png)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "slide_sticker_dropped",
                        sticker_id=sticker.id,
                        reason="unreadable",
                        error=str(exc),
                    )
                    Path(png).unlink(missing_ok=True)
                    continue
                resolved.append((png, sticker_geometry(canvas, size, sticker)))
        yield resolved
    finally:
        for png, _ in resolved:
            Path(png).unlink(missing_ok=True)
        # A decode that raised after writing a partial file.
        if edits is not None and edits.stickers:
            for i in range(len(edits.stickers)):
                Path(f"{out_path}.sticker{i}.png").unlink(missing_ok=True)


def _watermark_filter(canvas: Canvas, *, base: str, mark: str, out: str) -> str:
    """Composite the watermark input `mark` over `base` into `out`.

    One scale for both the mark and its insets, taken from whichever axis is
    more constrained -- the rule `KriaBranding.tileTransform` uses -- so a 4:5
    carousel slide gets a proportionally smaller mark in the same corner and a
    9:16 slide gets exactly the reference placement.
    """
    cw, ch = canvas
    ref_w, ref_h = _WATERMARK_REF_CANVAS
    scale = min(cw / ref_w, ch / ref_h)
    x = round(_WATERMARK_TILE_LEFT * scale)
    bottom = round(_WATERMARK_TILE_BOTTOM_INSET * scale)
    sized, prefix = mark, ""
    if scale != 1:
        sized = "wm"
        prefix = f"[{mark}]scale=iw*{scale:.6f}:ih*{scale:.6f}:flags=lanczos[{sized}];"
    return f"{prefix}[{base}][{sized}]overlay={x}:main_h-overlay_h-{bottom}:format=auto[{out}]"


def _overlay_filter_complex(
    base_vf: str,
    n_overlays: int,
    *,
    canvas: Canvas,
    stickers: Sequence[StickerGeometry] = (),
) -> str:
    """`[0:v]<base>[b0]; [b0][1:v]overlay[b1]; ...` then the watermark, ending on `[vout]`.

    Inputs 1..n are the canvas-sized overlay PNGs (the text PNGs, then the slide's
    drawing when it has one), inputs n+1..n+m are the sticker rasters (each scaled and
    rotated here, z-order = input order) and the last input is the watermark. The
    watermark goes on last so no text, ink or sticker can bury it, the same stacking the
    iOS engine uses.
    """
    parts = [f"[0:v]{base_vf}[b0]"]
    for i in range(n_overlays):
        parts.append(f"[b{i}][{i + 1}:v]overlay=0:0:format=auto[b{i + 1}]")
    top = f"b{n_overlays}"
    for j, geometry in enumerate(stickers):
        index = n_overlays + 1 + j
        parts.append(sticker_filter_chain(geometry, src=f"{index}:v", out=f"st{j}"))
        out = f"bs{j}"
        parts.append(sticker_overlay(geometry, base=top, sticker=f"st{j}", out=out))
        top = out
    mark = f"{n_overlays + len(stickers) + 1}:v"
    parts.append(_watermark_filter(canvas, base=top, mark=mark, out="vout"))
    return ";".join(parts)


# Bump when the image decode/normalize recipe changes so cached normalized
# derivatives (content-addressed in GCS) are rebuilt. Image slides only.
SLIDE_IMAGE_NORMALIZER_VERSION = 2


def _decode_image_for_ffmpeg(src_path: str) -> str:
    """Decode with Pillow into an sRGB, upright, opaque JPEG beside the source.

    FFmpeg alone mishandles iPhone HEIC (grid tiles, ignored ICC/orientation),
    producing B&W / tiled crops. Falls back to the original path if Pillow
    cannot open the file.
    """
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415

        try:
            import pillow_heif  # type: ignore[import-not-found]  # noqa: PLC0415

            pillow_heif.register_heif_opener()
        except Exception:  # noqa: BLE001
            pass
        with Image.open(src_path) as opened:
            icc = opened.info.get("icc_profile")
            image = ImageOps.exif_transpose(opened)
            image.load()
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                matte = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
                image = Image.alpha_composite(matte, rgba).convert("RGB")
            else:
                image = image.convert("RGB")
            if icc:
                try:
                    from PIL import ImageCms  # noqa: PLC0415

                    src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                    dst_profile = ImageCms.createProfile("sRGB")
                    image = ImageCms.profileToProfile(image, src_profile, dst_profile)
                except Exception:  # noqa: BLE001
                    pass
            out = f"{os.path.splitext(src_path)[0]}_decoded.jpg"
            image.save(out, format="JPEG", quality=95, subsampling=0, optimize=False)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("slide_image_decode_fallback", error=str(exc))
        return src_path


def normalize_image_slide(
    src_path: str,
    out_path: str,
    *,
    canvas: Canvas,
    edits: SlideEdits | None = None,
    sticker_sources: dict[str, str] | None = None,
) -> None:
    """Normalize any image format (incl. HEIC/WebP) to a cover-fit JPEG.

    ``sticker_sources`` (sticker id -> local original) feeds ``edits.stickers``; see
    ``_sticker_inputs``.
    """
    if not Path(src_path).exists():
        raise SlideBuildError(f"image slide not found: {src_path}")
    rich = rich_text_active(edits)
    decoded_path = _decode_image_for_ffmpeg(src_path)
    with (
        _edits_filter_fragment(
            edits, canvas=canvas, out_path=out_path, include_text=not rich
        ) as edit_fragment,
        _text_pngs(edits, canvas=canvas, out_path=out_path) as text_pngs,
        _drawing_pngs(edits, canvas=canvas, out_path=out_path) as ink_pngs,
        _sticker_inputs(edits, sticker_sources, canvas=canvas, out_path=out_path) as sticker_inputs,
    ):
        pngs = [*text_pngs, *ink_pngs]
        fit = edits.fit if edits is not None else None
        if edits is not None and edits.tilt is not None:
            vf = _tilt_frame_filter(canvas, _framing_crop(edits), fit, edits.tilt)
        else:
            vf = _frame_filter(canvas, _framing_crop(edits), fit)
        if edit_fragment:
            vf = f"{vf},{edit_fragment}"
        cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-nostats", "-i", decoded_path]
        for png in [*pngs, *(path for path, _ in sticker_inputs), _WATERMARK_PNG]:
            cmd += ["-i", png]
        cmd += [
            "-filter_complex",
            _overlay_filter_complex(
                vf,
                len(pngs),
                canvas=canvas,
                stickers=[geometry for _, geometry in sticker_inputs],
            ),
            "-map",
            "[vout]",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            out_path,
        ]
        _run_ffmpeg(cmd, context="normalize_image_slide")


def normalize_video_slide(
    src_path: str,
    out_path: str,
    *,
    canvas: Canvas,
    edits: SlideEdits | None = None,
    sticker_sources: dict[str, str] | None = None,
) -> None:
    """Normalize a video slide to the platform canvas, keeping its own audio.

    This is the derivative that ships in the export bundle at full length —
    unlike the preview segment below, it is never trimmed.
    """
    if not Path(src_path).exists():
        raise SlideBuildError(f"video slide not found: {src_path}")
    rich = rich_text_active(edits)
    with (
        _edits_filter_fragment(
            edits, canvas=canvas, out_path=out_path, include_text=not rich
        ) as edit_fragment,
        _text_pngs(edits, canvas=canvas, out_path=out_path) as text_pngs,
        _drawing_pngs(edits, canvas=canvas, out_path=out_path) as ink_pngs,
        _sticker_inputs(edits, sticker_sources, canvas=canvas, out_path=out_path) as sticker_inputs,
    ):
        pngs = [*text_pngs, *ink_pngs]
        # Video slides are never cropped (KRI-44) or fitted; a tilt turns the cover-fit frame.
        if edits is not None and edits.tilt is not None:
            vf = f"{_tilt_frame_filter(canvas, None, None, edits.tilt)},fps=30"
        else:
            vf = f"{_scale_crop_filter(canvas)},fps=30"
        if edit_fragment:
            vf = f"{vf},{edit_fragment}"
        cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-nostats", "-i", src_path]
        for png in [*pngs, *(path for path, _ in sticker_inputs), _WATERMARK_PNG]:
            cmd += ["-i", png]
        cmd += [
            "-filter_complex",
            _overlay_filter_complex(
                vf,
                len(pngs),
                canvas=canvas,
                stickers=[geometry for _, geometry in sticker_inputs],
            ),
            "-map",
            "[vout]",
            # One audio track, as ffmpeg's own stream selection kept before
            # every slide went through a filtergraph; mapping all of them
            # would carry every extra track a phone recorded into the export.
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            _FINAL_PRESET,
            "-crf",
            _FINAL_CRF,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_path,
        ]
        _run_ffmpeg(cmd, context="normalize_video_slide")


def render_preview_segment(
    normalized_path: str,
    out_path: str,
    *,
    kind: Literal["image", "video"],
    canvas: Canvas,
    hold_s: float = DEFAULT_IMAGE_HOLD_S,
    source_canvas: Canvas | None = None,
) -> None:
    """Render one slide's silent, capped-duration segment for the stitched preview.

    `canvas` is the post's preview canvas. A slide exported on a different
    canvas (a landscape slide in a portrait post) is fitted whole and padded with
    black bars so the stitched preview keeps one shape."""
    cw, ch = canvas
    if source_canvas is not None and source_canvas != canvas:
        fit_filter = (
            f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    else:
        fit_filter = f"scale={cw}:{ch},setsar=1"
    # setsar=1 is required, not cosmetic: the image and video encode paths
    # can each independently pick up a non-square Sample Aspect Ratio (a PNG
    # decoder's default vs. an already-1:1 H.264 stream), and the concat
    # FILTER (unlike the demuxer) hard-rejects joining segments whose SAR
    # differs, even when the pixel dimensions already match exactly.
    if kind == "image":
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "warning",
            "-nostats",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            normalized_path,
            "-t",
            f"{max(0.5, hold_s):.3f}",
            "-vf",
            fit_filter,
            "-c:v",
            "libx264",
            "-preset",
            _FINAL_PRESET,
            "-crf",
            _FINAL_CRF,
            "-pix_fmt",
            "yuv420p",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "warning",
            "-nostats",
            "-i",
            normalized_path,
            "-t",
            f"{max(0.5, hold_s):.3f}",
            "-an",
            "-vf",
            fit_filter,
            "-c:v",
            "libx264",
            "-preset",
            _FINAL_PRESET,
            "-crf",
            _FINAL_CRF,
            "-pix_fmt",
            "yuv420p",
            out_path,
        ]
    _run_ffmpeg(cmd, context="render_preview_segment")


def concat_preview_segments(segment_paths: list[str], out_path: str, *, canvas: Canvas) -> None:
    """Concatenate pre-rendered (silent, same-canvas) segments into one preview mp4.

    Uses the concat FILTER (re-encoding), not the concat demuxer, so
    independently-encoded segments always join on a clean keyframe boundary
    regardless of how each was produced.
    """
    if not segment_paths:
        raise SlideBuildError("concat_preview_segments requires at least one segment")
    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-nostats"]
    for path in segment_paths:
        cmd += ["-i", path]
    n = len(segment_paths)
    filter_inputs = "".join(f"[{i}:v]" for i in range(n))
    filter_complex = f"{filter_inputs}concat=n={n}:v=1:a=0[outv]"
    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-preset",
        _FINAL_PRESET,
        "-crf",
        _FINAL_CRF,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        out_path,
    ]
    _run_ffmpeg(cmd, context="concat_preview_segments")


def extract_cover(
    normalized_slide_path: str, kind: Literal["image", "video"], out_path: str
) -> None:
    """Produce the cover JPEG for a slide already normalized to the canvas."""
    if kind == "image":
        shutil.copy2(normalized_slide_path, out_path)
        return
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "warning",
        "-nostats",
        "-i",
        normalized_slide_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out_path,
    ]
    _run_ffmpeg(cmd, context="extract_cover")


@dataclass(frozen=True)
class BundleSlideFile:
    local_path: str
    kind: Literal["image", "video"]
    index: int
    alt: str | None = None
    width: int | None = None
    height: int | None = None

    @property
    def arcname(self) -> str:
        ext = "jpg" if self.kind == "image" else "mp4"
        return f"slides/{self.index:02d}.{ext}"


def build_post_manifest(
    *,
    platform_profile: str,
    caption: str,
    cover_index: int,
    slides: list[BundleSlideFile],
    aspect: str | None = None,
) -> dict:
    """The `post.json` manifest — the platform-agnostic, order-authoritative
    description of the export bundle's contents."""
    return {
        "schema_version": 1,
        "platform_profile": platform_profile,
        # Only when the creator chose a non-default size, so default bundles
        # keep their historical manifest.
        **({"aspect": aspect} if aspect else {}),
        "caption": caption,
        "cover_index": cover_index,
        "slides": [
            {
                "index": s.index,
                "kind": s.kind,
                "filename": s.arcname.removeprefix("slides/"),
                "alt": s.alt,
                **({"width": s.width, "height": s.height} if s.width and s.height else {}),
            }
            for s in slides
        ],
    }


def build_bundle_zip(
    out_zip_path: str, *, slides: list[BundleSlideFile], manifest: dict, caption: str
) -> None:
    """Assemble the downloadable `bundle.zip`: ordered slide files + post.json + caption.txt."""
    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for slide in slides:
            zf.write(slide.local_path, arcname=slide.arcname)
        zf.writestr("post.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr("caption.txt", caption or "")
