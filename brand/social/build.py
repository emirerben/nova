#!/usr/bin/env python3
"""Build the Kria social kit: watermark, outro, and reusable clip templates.

    python brand/social/build.py              # everything, into dist/
    python brand/social/build.py --prores     # also the big ProRes 4444 overlay
    python brand/social/build.py card --kind hook --text "I stopped editing"

Every export is regenerated from the in-repo wordmark and brand fonts, and every
placement is measured against the platform chrome map before it is written, so
`dist/` is a build artifact and never a thing to hand-edit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import skia

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # noqa: E402
from kria_brand import (  # noqa: E402
    REPO_ROOT, BUTTER, alpha_bbox, FPS, H, INK, PAPER, SKY, TEXT_SAFE, W,
    draw_rgba, ease_out_cubic, font, hex_to_color, save_png, shadow_filter,
    to_image,
    soft_shadow,
    wordmark, wrap,
)

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"

# The iOS renderer needs these at runtime and `brand/` is not part of the app
# bundle, so the build copies them into the Swift package's Resources. Rather
# than keep a second hand-maintained copy, the build writes both and
# `BrandingTests` fails if they drift.
RUNTIME_ASSET_DIRS = (
    REPO_ROOT / "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Resources",
)
RUNTIME_ASSET_FILES = (
    "watermark/kria-watermark-mist-standard.png",
    "watermark/kria-watermark-graphite-standard.png",
    "outro/kria-outro-paper.mp4",
)

# --- placements (all verified against the chrome map before export) -----------
WATERMARK_SIZES = {"compact": 140, "standard": 168, "demo": 210}
# Bottom-left. The hard floor is 1530, where Reels starts drawing the username
# block, but the signed-off position is one notch above it: the standard mark's
# top edge on 1400, i.e. its bottom edge on 1475. That leaves 55px of clearance
# above the caption block rather than sitting flush against it.
#
# The slot is anchored by the mark's BOTTOM edge, not its top: the three sizes
# have different heights, and pinning the top would push the tallest one into
# the caption block (which is exactly what the placement gate caught).
WATERMARK_BOTTOM = 1475
WATERMARK_LEFT = 60
WATERMARK_TOP_Y = 210             # the top-left alternate is top-anchored


def watermark_slot(size: str, slot: str = "bottom-left") -> tuple[int, int]:
    """Placement origin of the MARK (not the padded tile) for a size and slot."""
    if slot == "top-left":
        return (WATERMARK_LEFT, WATERMARK_TOP_Y)
    mark_h = wordmark(WATERMARK_SIZES[size]).size[1]
    return (WATERMARK_LEFT, WATERMARK_BOTTOM - mark_h)


# Kept for the standard size, which is what the exploration tools default to.
WATERMARK_HOME = watermark_slot("standard")
WATERMARK_ALT = (WATERMARK_LEFT, WATERMARK_TOP_Y)

OUTRO_FRAMES = 48                 # 1.6s at 30fps
MARK_W_OUTRO = 560


# =============================================================================
# watermark
# =============================================================================

# The watermark is a quiet grey mark with a diffuse shadow and nothing else --
# no chip, no halo, no box. Two tones cover the range: one light, one dark.
# name:   (ink colour, baked opacity, shadow opacity or None)
# APPROVED WEIGHTS. Signed off 2026-09-21 after three rounds of review on real
# footage. Deliberately near-subliminal: at this weight the mark is a quiet
# attribution mark, not something a viewer reads. It WILL disappear into bright
# footage, and that is the choice, not a defect -- see README section 2.
#
# `test_shipped_weights_match_the_approved_values` pins these so a later change
# has to be deliberate.
VARIANTS = {
    "mist":     ("#CAD2DB", 0.42, 0.10),  # light grey: dark / mid / warm / busy
    "graphite": ("#526071", 0.62, 0.08),  # dark grey: bright / pale footage
    "sky":      (SKY,       1.00, None),  # logotype, white product screens only
}

# Which footage each variant is intended for. The report measures the full
# cross-product so the out-of-policy combinations stay visible as evidence
# rather than folklore.
RECOMMENDED = {
    "mist": ("dark", "mid", "warm", "busy"),
    "graphite": ("bright",),
    # `sky` is the brand logotype on white product surfaces, not a mark over
    # footage. WCAG 1.4.11 exempts logotypes from the contrast minimum.
    "sky": (),
}

SHADOW_PAD = 30
PLATE_PAD_X, PLATE_PAD_Y, PLATE_RADIUS, PLATE_FEATHER = 26, 17, 20, 7


def build_watermark(name: str, width: int) -> np.ndarray:
    """A padded RGBA tile: mark, shadow, opacity already baked in.

    Opacity is baked rather than left to the editor because the one thing that
    reliably goes wrong in a hurry is somebody typing a different number into
    CapCut every week.
    """
    fill, opacity, shadow = VARIANTS[name]
    mark = wordmark(width, fill=fill)
    mw, mh = mark.size
    pad = watermark_pad(name)
    surface = skia.Surface(mw + pad * 2, mh + pad * 2)
    with surface as canvas:
        canvas.clear(skia.ColorTRANSPARENT)
        paint = (soft_shadow(dy=2, sigma=7, opacity=shadow)
                 if shadow is not None else skia.Paint(AntiAlias=True))
        draw_rgba(canvas, mark.full, pad, pad, paint, opacity)
    return np.array(surface.makeImageSnapshot().toarray())


def watermark_pad(name: str) -> int:
    """Transparent margin the tile carries, so masks can be aligned to it."""
    return SHADOW_PAD if VARIANTS[name][2] is not None else 0


def export_watermarks() -> dict:
    out = DIST / "watermark"
    manifest = {}
    for variant in VARIANTS:
        for size_name, width in WATERMARK_SIZES.items():
            tile = build_watermark(variant, width)
            path = out / f"kria-watermark-{variant}-{size_name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            skia.Image.fromarray(
                tile, colorType=skia.kRGBA_8888_ColorType
            ).save(str(path), skia.kPNG)
            pad = watermark_pad(variant)
            manifest[path.name] = {
                "variant": variant,
                "for_footage": list(RECOMMENDED[variant]) or "logotype only",
                "mark_width_px": width,
                "tile_px": [tile.shape[1], tile.shape[0]],
                "transparent_pad_px": pad,
                # Tiles carry a transparent margin for the shadow, so the
                # overlay origin is not the placement origin. These are the
                # numbers to paste into ffmpeg; no arithmetic required.
                "overlay_xy": {
                    name: [watermark_slot(size_name, name)[0] - pad,
                           watermark_slot(size_name, name)[1] - pad]
                    for name in ("bottom-left", "top-left")
                },
            }
    return manifest


# =============================================================================
# outro
# =============================================================================

def _outro_frame(i: int, overlay: bool) -> skia.Surface:
    """One outro frame. Letters land in the approved order, then the URL fades."""
    surface = skia.Surface(W, H)
    ink = PAPER if overlay else SKY
    mark = wordmark(MARK_W_OUTRO, fill=ink)
    mw, mh = mark.size
    x0 = (W - mw) / 2
    # Optically centred: the mark carries the card alone, and dead-centre
    # reads low on a 9:16 frame.
    y0 = 900 - mh / 2

    with surface as canvas:
        if overlay:
            canvas.clear(skia.ColorTRANSPARENT)
            # Wash fades in over the first 8 frames so the last shot stays
            # readable underneath instead of slamming to a card.
            wash = ease_out_cubic(i / 8.0) * 0.46
            canvas.drawRect(skia.Rect.MakeWH(W, H),
                            skia.Paint(Color=skia.Color(0, 0, 0,
                                                        int(round(wash * 255)))))
        else:
            canvas.clear(hex_to_color(PAPER))

        # Letters are composited into one layer before the shadow is applied.
        # Shadowing them individually makes each letter cast onto the next, and
        # the approved mark overlaps enough that the seams are visible.
        letters = skia.Surface(W, H)
        with letters as lc:
            lc.clear(skia.ColorTRANSPARENT)
            for idx, letter in enumerate(mark.letters):
                t = ease_out_cubic((i - (3 + idx * 3)) / 9.0)
                if t <= 0:
                    continue
                draw_rgba(lc, letter, x0, y0 + (1.0 - t) * 26.0, opacity=t)
        canvas.drawImage(
            letters.makeImageSnapshot(), 0, 0,
            skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kNone),
            soft_shadow(dy=4, sigma=14, opacity=0.38) if overlay
            else skia.Paint(AntiAlias=True))
    return surface


def export_outro(prores: bool = False) -> dict:
    out = DIST / "outro"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {}

    for overlay in (False, True):
        label = "overlay" if overlay else "paper"
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for i in range(OUTRO_FRAMES):
                save_png(_outro_frame(i, overlay), tmpdir / f"f{i:04d}.png")

            if not overlay:
                # Silent stereo AAC so this concatenates onto a clip that has
                # audio without ffmpeg dropping the whole audio stream.
                target = out / "kria-outro-paper.mp4"
                _run([
                    "ffmpeg", "-y", "-framerate", str(FPS),
                    "-i", str(tmpdir / "f%04d.png"),
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-shortest",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "17",
                    "-pix_fmt", "yuv420p", "-profile:v", "high",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(target),
                ])
                manifest[target.name] = _probe(target)
            else:
                target = out / "kria-outro-overlay.webm"
                _run([
                    "ffmpeg", "-y", "-framerate", str(FPS),
                    "-i", str(tmpdir / "f%04d.png"),
                    "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                    "-b:v", "0", "-crf", "28", "-row-mt", "1",
                    str(target),
                ])
                manifest[target.name] = _probe(target)

                if prores:
                    mov = out / "kria-outro-overlay.mov"
                    _run([
                        "ffmpeg", "-y", "-framerate", str(FPS),
                        "-i", str(tmpdir / "f%04d.png"),
                        "-c:v", "prores_ks", "-profile:v", "4444",
                        "-pix_fmt", "yuva444p10le", str(mov),
                    ])
                    manifest[mov.name] = _probe(mov)

        # Static end frame, for thumbnails and for editors who want a still.
        still = out / f"kria-endcard-{label}.png"
        save_png(_outro_frame(OUTRO_FRAMES - 1, overlay), still)
        manifest[still.name] = {"px": [W, H]}

    return manifest


# =============================================================================
# reusable clip templates
# =============================================================================

def card_hook(text: str, kicker: str | None = None) -> skia.Surface:
    """Full-bleed hook title for viral clips: white Fraunces over footage.

    Sits at the top of the shared safe rectangle so it clears every app's
    chrome and leaves the lower two thirds of the frame for the footage.
    """
    x0, y0, x1, _ = TEXT_SAFE
    surface = skia.Surface(W, H)
    with surface as canvas:
        canvas.clear(skia.ColorTRANSPARENT)
        y = y0 + 90
        if kicker:
            kf = font("Inter-Bold", 38)
            kp = skia.Paint(AntiAlias=True, Color=hex_to_color(BUTTER))
            kp.setImageFilter(shadow_filter(dy=2, sigma=7, opacity=0.5))
            canvas.drawString(kicker.upper(), x0, y, kf, kp)
            y += 74

        f = font("Fraunces-Bold", 104)
        paint = skia.Paint(AntiAlias=True, Color=hex_to_color(PAPER))
        paint.setImageFilter(shadow_filter(dy=4, sigma=13, opacity=0.5))
        for line in wrap(text, f, x1 - x0)[:3]:
            y += 104
            canvas.drawString(line, x0, y, f, paint)
    return surface


def card_step(number: int, label: str) -> skia.Surface:
    """Numbered step chip for product/demo clips: Sky pill, warm ink text."""
    x0, _, x1, y1 = TEXT_SAFE
    surface = skia.Surface(W, H)
    with surface as canvas:
        canvas.clear(skia.ColorTRANSPARENT)
        nf, lf = font("Inter-Bold", 44), font("Inter-Medium", 46)
        num = str(number)
        pad_x, pad_y, gap, dia = 30, 22, 22, 62
        text_w = lf.measureText(label)
        pill_w = min(pad_x * 2 + dia + gap + text_w, x1 - x0)
        pill_h = dia + pad_y * 2
        top = y1 - 120 - pill_h

        canvas.drawRoundRect(
            skia.Rect.MakeXYWH(x0, top, pill_w, pill_h), pill_h / 2, pill_h / 2,
            skia.Paint(AntiAlias=True, Color=hex_to_color(PAPER, 0.94)))
        canvas.drawCircle(x0 + pad_x + dia / 2, top + pill_h / 2, dia / 2,
                          skia.Paint(AntiAlias=True, Color=hex_to_color(SKY)))
        canvas.drawString(num, x0 + pad_x + dia / 2 - nf.measureText(num) / 2,
                          top + pill_h / 2 + 16, nf,
                          skia.Paint(AntiAlias=True, Color=hex_to_color(INK)))
        canvas.drawString(label, x0 + pad_x + dia + gap,
                          top + pill_h / 2 + 17, lf,
                          skia.Paint(AntiAlias=True, Color=hex_to_color(INK)))
    return surface


def export_templates() -> dict:
    out = DIST / "templates"
    manifest: dict = {}
    examples = [
        ("hook-example-1.png", card_hook("I stopped editing my own videos",
                                         kicker="day 14")),
        ("hook-example-2.png", card_hook("This took 40 seconds")),
        ("step-example-1.png", card_step(1, "Drop in your raw clips")),
        ("step-example-2.png", card_step(2, "Kria cuts it to the beat")),
    ]
    for name, surface in examples:
        save_png(surface, out / name)
        # Measure where the ink actually landed rather than trusting the
        # layout maths, so the documented rectangles cannot go stale.
        rgba = np.array(surface.makeImageSnapshot().toarray())
        manifest[name] = {"px": [W, H], "ink_rect": list(alpha_bbox(rgba))}
    return manifest


# =============================================================================
# proofs
# =============================================================================

def _backgrounds() -> dict[str, np.ndarray]:
    """Synthetic stand-ins for the footage classes that break watermarks."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    u, v = xx / W, yy / H
    rng = np.random.default_rng(52)

    def stack(*ch): return np.clip(np.dstack(ch), 0, 255)

    busy = rng.normal(128, 58, (H // 8, W // 8, 3))
    busy = np.repeat(np.repeat(busy, 8, axis=0), 8, axis=1)[:H, :W]
    busy += (np.sin(xx / 9.0) * 34 + np.cos(yy / 7.0) * 34)[..., None]

    return {
        # blown-out sky / snow / a white app screen
        "bright": stack(250 - v * 14, 251 - v * 10, 253 - v * 6),
        # night, interior, concert
        "dark": stack(16 + v * 20, 18 + v * 22, 26 + v * 28),
        # the classic mid-grey that defeats both white and black ink
        "mid": stack(np.full_like(u, 128.0), np.full_like(u, 128.0),
                     np.full_like(u, 128.0)),
        # skin tones, golden hour
        "warm": stack(228 - v * 60, 176 - v * 56, 128 - v * 44),
        # high-frequency detail: foliage, crowds, city at night
        "busy": np.clip(busy, 0, 255),
    }


def _overlay_rgba(tile: np.ndarray, x: int, y: int) -> np.ndarray:
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    th, tw = tile.shape[:2]
    frame[y:y + th, x:x + tw] = tile
    return frame


def _place(tile: np.ndarray, slot: tuple[int, int], pad: int) -> np.ndarray:
    """Composite a padded tile so that the MARK lands on `slot`.

    Tiles carry a transparent margin for the shadow, so the tile origin and the
    placement origin differ by `pad`. Getting this wrong shifts the mark by 30px
    -- invisible on a top-anchored slot, and enough to push a bottom-anchored
    one under the caption block.
    """
    return _overlay_rgba(tile, slot[0] - pad, slot[1] - pad)


def _annotate(canvas: skia.Canvas) -> None:
    """Draw the union of all three platforms' chrome, plus the safe rectangle."""
    from kria_brand import OCCLUSION
    hatch = skia.Paint(Color=skia.Color(220, 30, 30, 46))
    for regions in OCCLUSION.values():
        for (rx0, ry0, rx1, ry1) in regions:
            canvas.drawRect(skia.Rect.MakeLTRB(rx0, ry0, rx1, ry1), hatch)
    edge = skia.Paint(AntiAlias=True, Color=hex_to_color(BUTTER),
                      Style=skia.Paint.kStroke_Style, StrokeWidth=4)
    canvas.drawRect(skia.Rect.MakeLTRB(*TEXT_SAFE), edge)


_TEMPLATE_RECTS: dict | None = None


def export_proofs() -> dict:
    out = DIST / "proofs"
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"placements": {}, "legibility": {}}

    # 1. geometry: every placement this kit ships, checked against the map.
    checks: dict = {}
    for size_name, width in WATERMARK_SIZES.items():
        tile = build_watermark("mist", width)
        pad = watermark_pad("mist")
        th, tw = tile.shape[0] - pad * 2, tile.shape[1] - pad * 2
        for slot in ("bottom-left", "top-left"):
            px, py = watermark_slot(size_name, slot)
            rect = (px, py, px + tw, py + th)
            checks[f"watermark/{size_name}/{slot}"] = {
                "rect": list(rect),
                "collisions": verify.check_rect(rect),
                "clear": verify.is_clear(rect),
            }
    for name, meta in (_TEMPLATE_RECTS or {}).items():
        rect = tuple(meta["ink_rect"])
        checks[f"template/{name}"] = {
            "rect": list(rect),
            "collisions": verify.check_rect(rect),
            "clear": verify.is_clear(rect),
        }
    checks["safe-rectangle"] = {
        "rect": list(TEXT_SAFE),
        "collisions": verify.check_rect(TEXT_SAFE),
        "clear": verify.is_clear(TEXT_SAFE),
    }
    report["placements"] = checks

    # 2. the map itself, as a picture.
    surface = skia.Surface(W, H)
    with surface as canvas:
        canvas.clear(hex_to_color("#F2F4F7"))
        _annotate(canvas)
        std = build_watermark("graphite", WATERMARK_SIZES["standard"])
        pad_std = watermark_pad("graphite")
        for name in ("bottom-left", "top-left"):
            sx, sy = watermark_slot("standard", name)
            draw_rgba(canvas, std, sx - pad_std, sy - pad_std)
        f = font("Inter-Medium", 34)
        p = skia.Paint(AntiAlias=True, Color=hex_to_color(INK))
        canvas.drawString("safe rectangle 60,200 - 890,1520", 70, 1660, f, p)
        canvas.drawString("red = TikTok / Reels / Shorts chrome", 70, 1710, f, p)
    save_png(surface, out / "safezone-map.png")

    # 3. photometry: each variant over each background class.
    for bg_name, bg in _backgrounds().items():
        for variant in VARIANTS:
            width = WATERMARK_SIZES["standard"]
            tile = build_watermark(variant, width)
            pad = watermark_pad(variant)
            composite = verify.over(_place(tile, WATERMARK_HOME, pad), bg)

            # Mask from the ink alone (no shadow/scrim) so the glyph is the
            # subject. It must be padded exactly as `build_watermark` padded
            # the tile, or the mask lands beside the ink and every ratio
            # reads 1.00.
            fill = VARIANTS[variant][0]
            bare = wordmark(width, fill=fill)
            ink_layer = _place(_pad(bare.full, pad), WATERMARK_HOME, pad)

            res = verify.contrast(ink_layer, composite)
            entry = res.as_dict()
            entry["recommended_pairing"] = bg_name in RECOMMENDED[variant]
            # Reported, never gated. See verify.WATERMARK_REFERENCE.
            entry["meets_reference"] = res.worst_tile >= verify.WATERMARK_REFERENCE
            report["legibility"][f"{variant}/{bg_name}"] = entry

            _save_proof(_sheet(composite, variant, bg_name, res),
                        out / f"legibility-{variant}-{bg_name}.jpg")

    return report


def _save_proof(surface: skia.Surface, path: Path) -> None:
    """Proof sheets are evidence, not deliverables: half-res JPEG is plenty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = surface.makeImageSnapshot().resize(
        W // 2, H // 2,
        skia.SamplingOptions(skia.CubicResampler.Mitchell()))
    img.save(str(path), skia.kJPEG, 90)


def _pad(rgba: np.ndarray, pad: int) -> np.ndarray:
    out = np.zeros((rgba.shape[0] + pad * 2, rgba.shape[1] + pad * 2, 4),
                   dtype=rgba.dtype)
    out[pad:pad + rgba.shape[0], pad:pad + rgba.shape[1]] = rgba
    return out


def _sheet(composite: np.ndarray, variant: str, bg: str,
           res: verify.ContrastResult) -> skia.Surface:
    rgba = np.dstack([composite.astype(np.uint8),
                      np.full((H, W, 1), 255, np.uint8)])
    surface = skia.Surface(W, H)
    with surface as canvas:
        draw_rgba(canvas, rgba, 0, 0)
        _annotate(canvas)
        verdict = "PASS" if res.passes_floor else "FAIL"
        colour = "#17633B" if res.passes_floor else "#B42318"
        f = font("Inter-Bold", 40)
        canvas.drawRect(skia.Rect.MakeXYWH(48, 1620, 984, 180),
                        skia.Paint(Color=hex_to_color(PAPER, 0.92)))
        p = skia.Paint(AntiAlias=True, Color=hex_to_color(INK))
        canvas.drawString(f"{variant} on {bg}", 76, 1682, f, p)
        canvas.drawString(
            f"median {res.median:.1f}:1   worst tile {res.worst_tile:.1f}:1",
            76, 1742, font("Inter-Medium", 36), p)
        canvas.drawString(verdict, 830, 1742, f,
                          skia.Paint(AntiAlias=True, Color=hex_to_color(colour)))
    return surface


# =============================================================================
# gray watermark exploration
# =============================================================================

# A tonal ladder for the "subtle gray" treatment, from near-white to slate.
# Tone is the variable; every candidate carries the same faint shadow so the
# comparison isn't confounded, except `bare`, which is the control that shows
# what dropping the shadow costs. Greys are taken from the warm-ink scale in
# DESIGN.md where one exists, so this stays inside the palette.
@dataclass(frozen=True)
class GrayCandidate:
    """One grey watermark treatment.

    Three ways to stay legible without shouting, in increasing footage cost:
    a `shadow` (cheapest, helps only on light backgrounds), a `halo` that hugs
    the letterforms, or a `scrim` chip behind the whole mark. `pad` controls
    how much footage the chip covers -- worth tightening before reaching for
    more opacity.
    """

    fill: str
    opacity: float
    shadow: bool = False
    shadow_opacity: float = 0.30
    scrim_hex: str | None = None
    scrim: float = 0.0
    halo_hex: str | None = None
    halo: float = 0.0
    halo_sigma: float = 9.0
    pad: tuple[int, int, int] = (PLATE_PAD_X, PLATE_PAD_Y, PLATE_RADIUS)

    def label(self) -> str:
        bits = [f"{self.fill} @{self.opacity:.0%}"]
        if self.shadow:
            bits.append(f"shadow {self.shadow_opacity:.0%}")
        if self.halo_hex:
            bits.append(f"halo {self.halo:.0%}")
        if self.scrim_hex:
            bits.append(f"chip {self.scrim:.0%}"
                        + ("" if self.pad[0] == PLATE_PAD_X else " tight"))
        return "  ".join(bits)


# Round 4: "cok koyu" -- the shipped mark reads too heavy on footage. The main
# culprit is the shadow rather than the ink: at 45% it leaves a dark halo that
# makes a light grey mark look weighted. This ladder walks BOTH down together,
# from the shipped weight to barely-there.
GRAY_CANDIDATES: dict[str, GrayCandidate] = {
    "shipped":   GrayCandidate("#CAD2DB", 0.85, shadow=True, shadow_opacity=0.45),
    "lighter":   GrayCandidate("#CAD2DB", 0.72, shadow=True, shadow_opacity=0.28),
    "soft":      GrayCandidate("#CAD2DB", 0.62, shadow=True, shadow_opacity=0.20),
    "faint":     GrayCandidate("#CAD2DB", 0.52, shadow=True, shadow_opacity=0.14),
    "ghost":     GrayCandidate("#CAD2DB", 0.42, shadow=True, shadow_opacity=0.10),
    "no-shadow": GrayCandidate("#CAD2DB", 0.62, shadow=False),
}


GRAY_SHADOW = dict(dy=2, sigma=7, opacity=0.30)


def build_gray(name: str, width: int) -> np.ndarray:
    c = GRAY_CANDIDATES[name]
    mark = wordmark(width, fill=c.fill)
    mw, mh = mark.size
    pad = SHADOW_PAD
    surface = skia.Surface(mw + pad * 2, mh + pad * 2)
    with surface as canvas:
        canvas.clear(skia.ColorTRANSPARENT)
        if c.scrim_hex is not None:
            px, py, radius = c.pad
            sp = skia.Paint(AntiAlias=True,
                            Color=hex_to_color(c.scrim_hex, c.scrim))
            sp.setImageFilter(skia.ImageFilters.Blur(PLATE_FEATHER, PLATE_FEATHER))
            canvas.drawRoundRect(
                skia.Rect.MakeLTRB(pad - px, pad - py,
                                   pad + mw + px, pad + mh + py),
                radius, radius, sp)
        if c.halo_hex is not None:
            # A glow, not an outline: the same diffuse device as the shadow,
            # inverted, so it lifts the mark off dark AND busy footage without
            # boxing it. Drawn twice to build up density.
            hp = skia.Paint(AntiAlias=True)
            hp.setImageFilter(skia.ImageFilters.DropShadowOnly(
                0.0, 0.0, c.halo_sigma, c.halo_sigma,
                hex_to_color(c.halo_hex, c.halo)))
            for _ in range(2):
                draw_rgba(canvas, mark.full, pad, pad, hp)
        paint = (soft_shadow(dy=2, sigma=7, opacity=c.shadow_opacity) if c.shadow
                 else skia.Paint(AntiAlias=True))
        draw_rgba(canvas, mark.full, pad, pad, paint, c.opacity)
    return np.array(surface.makeImageSnapshot().toarray())


def context_sheet(video: Path, at: float, names: list[str], size: str,
                  slot: tuple[int, int] = WATERMARK_HOME) -> skia.Surface:
    """Full frames, side by side. A 1:1 crop makes every mark look shouty."""
    width = WATERMARK_SIZES[size]
    bg = load_frame(video, at)
    scale = 0.34
    fw, fh = int(W * scale), int(H * scale)
    surface = skia.Surface(fw * len(names), fh + 56)
    with surface as canvas:
        canvas.clear(hex_to_color("#14171A"))
        for col, name in enumerate(names):
            composite = verify.over(
                _place(build_gray(name, width), slot, SHADOW_PAD), bg)
            rgba = np.dstack([composite.astype(np.uint8),
                              np.full((H, W, 1), 255, np.uint8)])
            img = to_image(rgba).resize(
                fw, fh, skia.SamplingOptions(skia.CubicResampler.Mitchell()))
            canvas.drawImage(img, col * fw, 0)
            canvas.drawString(name, col * fw + 16, fh + 38, font("Inter-Bold", 28),
                              skia.Paint(AntiAlias=True, Color=hex_to_color(PAPER)))
    return surface


def place_sheet(video: Path, at: float, name: str, ys: list[int],
                size: str) -> tuple[skia.Surface, dict]:
    """The same mark at several heights, with the platform chrome drawn over it.

    Position is a whole-frame question, and the binding constraint at the
    bottom-left is the username/caption block -- which is exactly where a
    bottom-left watermark wants to live. Drawing the chrome makes the floor
    visible instead of asking anyone to trust a number.
    """
    from kria_brand import OCCLUSION

    width = WATERMARK_SIZES[size]
    bg = load_frame(video, at)
    tile = build_gray(name, width)
    pad = SHADOW_PAD
    mh = tile.shape[0] - pad * 2
    mw = tile.shape[1] - pad * 2

    scale = 0.30
    fw, fh = int(W * scale), int(H * scale)
    surface = skia.Surface(fw * len(ys), fh + 92)
    report: dict = {}

    with surface as canvas:
        canvas.clear(hex_to_color("#14171A"))
        for col, y in enumerate(ys):
            slot = (WATERMARK_HOME[0], y)
            composite = verify.over(_place(tile, slot, pad), bg)
            rgba = np.dstack([composite.astype(np.uint8),
                              np.full((H, W, 1), 255, np.uint8)])
            frame = skia.Surface(W, H)
            with frame as fc:
                draw_rgba(fc, rgba, 0, 0)
                hatch = skia.Paint(Color=skia.Color(220, 30, 30, 62))
                for regions in OCCLUSION.values():
                    for (rx0, ry0, rx1, ry1) in regions:
                        fc.drawRect(skia.Rect.MakeLTRB(rx0, ry0, rx1, ry1), hatch)
            img = frame.makeImageSnapshot().resize(
                fw, fh, skia.SamplingOptions(skia.CubicResampler.Mitchell()))
            canvas.drawImage(img, col * fw, 0)

            rect = (slot[0], y, slot[0] + mw, y + mh)
            hits = verify.check_rect(rect)
            clear = verify.is_clear(rect)
            report[str(y)] = {"rect": list(rect), "clear": clear,
                              "collisions": hits}
            note = "clear" if clear else "  ".join(
                f"{k}:{'+'.join(v)}" for k, v in hits.items() if v)
            canvas.drawString(f"y = {y}", col * fw + 16, fh + 38,
                              font("Inter-Bold", 28),
                              skia.Paint(AntiAlias=True, Color=hex_to_color(PAPER)))
            canvas.drawString(note, col * fw + 16, fh + 72,
                              font("Inter-Medium", 21),
                              skia.Paint(AntiAlias=True,
                                         Color=hex_to_color(PAPER if clear
                                                            else "#E8846F")))
    return surface, report


def _cell_for(slot: tuple[int, int]) -> tuple[int, int, int, int]:
    """The crop shown per cell: the mark plus enough footage around it to judge
    whether it is subtle or shouting."""
    return (0, slot[1] - 80, 620, slot[1] + 180)


def compare_grays(video: Path, times: list[float], size: str,
                  slot: tuple[int, int] = WATERMARK_HOME
                  ) -> tuple[skia.Surface, dict]:
    width = WATERMARK_SIZES[size]
    names = list(GRAY_CANDIDATES)
    cx0, cy0, cx1, cy1 = _cell_for(slot)
    cw, ch = cx1 - cx0, cy1 - cy0
    label_h, head_h = 76, 64

    frames = [(t, load_frame(video, t)) for t in times]
    scores: dict = {}

    surface = skia.Surface(cw * len(frames), head_h + len(names) * (ch + label_h))
    with surface as canvas:
        canvas.clear(hex_to_color("#14171A"))
        head = font("Inter-Bold", 30)
        for col, (t, bg) in enumerate(frames):
            lum = float(np.median(verify.luminance(bg[cy0:cy1, 30:270])))
            canvas.drawString(f"t={t:g}s   corner luminance {lum:.2f}",
                              col * cw + 18, 42, head,
                              skia.Paint(AntiAlias=True, Color=hex_to_color(PAPER)))

        for row, name in enumerate(names):
            tile = build_gray(name, width)
            bare = wordmark(width, fill=GRAY_CANDIDATES[name].fill)
            ink = _place(_pad(bare.full, SHADOW_PAD), slot, SHADOW_PAD)
            y = head_h + row * (ch + label_h)

            for col, (t, bg) in enumerate(frames):
                composite = verify.over(_place(tile, slot, SHADOW_PAD), bg)
                res = verify.contrast(ink, composite)
                scores[f"{name}@{t:g}s"] = res.as_dict()

                rgba = np.dstack([composite.astype(np.uint8),
                                  np.full((H, W, 1), 255, np.uint8)])
                cell = np.ascontiguousarray(rgba[cy0:cy1, cx0:cx1])
                draw_rgba(canvas, cell, col * cw, y)

                paint = skia.Paint(
                    AntiAlias=True,
                    Color=hex_to_color(PAPER if res.worst_tile >= 2.5
                                       else "#E8846F"))
                canvas.drawString(f"{name}   {res.worst_tile:.1f}:1",
                                  col * cw + 18, y + ch + 30,
                                  font("Inter-Bold", 26), paint)
                canvas.drawString(GRAY_CANDIDATES[name].label(),
                                  col * cw + 18, y + ch + 60,
                                  font("Inter-Medium", 21), paint)
    return surface, scores


# =============================================================================
# pick: which variant does this actual shot need?
# =============================================================================

def load_frame(path: Path, at: float = 0.0) -> np.ndarray:
    """Grab one frame as a 1080x1920 RGB array, cover-cropped like an upload."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "frame.png"
        _run([
            "ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", str(path),
            "-frames:v", "1",
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H}",
            "-update", "1", str(out),
        ])
        data = skia.Image.open(str(out)).convert(
            colorType=skia.kRGBA_8888_ColorType,
            alphaType=skia.kUnpremul_AlphaType)
        return np.array(data.toarray())[..., :3].astype(np.float64)


def pick_variant(frame: Path, at: float, slot: tuple[int, int],
                 size: str) -> tuple[str, dict]:
    """Measure every watermark variant against a real shot and rank them.

    Removes the one judgement call the kit would otherwise leave to a person in
    a hurry: instead of eyeballing whether a corner is "bright", composite the
    mark onto the frame and read the contrast off it.
    """
    bg = load_frame(frame, at)
    width = WATERMARK_SIZES[size]
    scores: dict[str, float] = {}
    for variant in VARIANTS:
        pad = watermark_pad(variant)
        tile = build_watermark(variant, width)
        composite = verify.over(_place(tile, slot, pad), bg)
        bare = wordmark(width, fill=VARIANTS[variant][0])
        ink = _place(_pad(bare.full, pad), slot, pad)
        scores[variant] = verify.contrast(ink, composite).worst_tile

    # Prefer the least obtrusive mark that clears the design target; fall back
    # to the plate, which is the variant that always works.
    usable = [v for v in ("mist", "graphite")
              if scores[v] >= verify.WATERMARK_FLOOR]
    best = (max(usable, key=lambda v: scores[v]) if usable
            else max(("mist", "graphite"), key=lambda v: scores[v]))
    return best, scores


# =============================================================================
# plumbing
# =============================================================================

def sync_runtime_assets() -> None:
    """Copy the files the renderers load into each runtime that bundles them."""
    for directory in RUNTIME_ASSET_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
        for rel in RUNTIME_ASSET_FILES:
            shutil.copyfile(DIST / rel, directory / Path(rel).name)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{proc.stderr[-2500:]}")


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_name,width,height,pix_fmt,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return {
        "bytes": int(data["format"]["size"]),
        "duration_s": round(float(data["format"]["duration"]), 3),
        "streams": [{k: s.get(k) for k in
                     ("codec_name", "width", "height", "pix_fmt", "r_frame_rate")}
                    for s in data["streams"]],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--prores", action="store_true",
                    help="also write the ProRes 4444 overlay outro (large, gitignored)")
    pick = sub.add_parser(
        "pick", help="measure the watermark variants against a real frame")
    pick.add_argument("frame", type=Path, help="image or video to sample")
    pick.add_argument("--at", type=float, default=0.0, help="seek seconds, video only")
    pick.add_argument("--slot", choices=["bottom-left", "top-left"],
                      default="bottom-left")
    pick.add_argument("--size", choices=list(WATERMARK_SIZES), default="standard")

    cmp_ = sub.add_parser(
        "compare", help="render the grey watermark ladder over a real clip")
    cmp_.add_argument("video", type=Path)
    cmp_.add_argument("--at", default="1,6,10",
                      help="comma-separated seek times")
    cmp_.add_argument("--size", choices=list(WATERMARK_SIZES), default="standard")
    cmp_.add_argument("--slot", choices=["bottom-left", "top-left"],
                      default="bottom-left")
    cmp_.add_argument("--context", default="mist,slate,veil",
                      help="candidates to show full-frame, comma-separated")
    cmp_.add_argument("--out", type=Path, default=Path("gray-compare.png"))

    place = sub.add_parser(
        "place", help="show one treatment at several heights, over the chrome map")
    place.add_argument("video", type=Path)
    place.add_argument("--at", type=float, default=4.0)
    place.add_argument("--name", default="mist", help="candidate from GRAY_CANDIDATES")
    place.add_argument("--ys", default="1400,1455,1520,1600")
    place.add_argument("--size", choices=list(WATERMARK_SIZES), default="standard")
    place.add_argument("--out", type=Path, default=Path("placement.png"))

    card = sub.add_parser("card", help="render one template card")
    card.add_argument("--kind", choices=["hook", "step"], required=True)
    card.add_argument("--text", required=True)
    card.add_argument("--kicker")
    card.add_argument("--number", type=int, default=1)
    card.add_argument("--out", type=Path, default=Path("card.png"))
    args = ap.parse_args()

    if args.cmd == "compare":
        times = [float(x) for x in args.at.split(",")]
        slot = WATERMARK_ALT if args.slot == "top-left" else WATERMARK_HOME
        surface, scores = compare_grays(args.video, times, args.size, slot)
        save_png(surface, args.out)
        args.out.with_suffix(".json").write_text(json.dumps(scores, indent=2) + "\n")
        ctx = args.out.with_name(args.out.stem + "-in-context.png")
        save_png(context_sheet(args.video, times[0],
                               args.context.split(","), args.size, slot), ctx)
        print(f"wrote {args.out}\nwrote {ctx}")
        return 0

    if args.cmd == "place":
        ys = [int(v) for v in args.ys.split(",")]
        surface, report = place_sheet(args.video, args.at, args.name, ys, args.size)
        save_png(surface, args.out)
        print(json.dumps(report, indent=2))
        return 0

    if args.cmd == "pick":
        slot = WATERMARK_HOME if args.slot == "bottom-left" else WATERMARK_ALT
        best, scores = pick_variant(args.frame, args.at, slot, args.size)
        for name, score in sorted(scores.items(), key=lambda kv: -kv[1]):
            note = "" if name != "sky" else "  (logotype, not gated)"
            print(f"  {name:9} {score:6.2f}:1"
                  f"  {'ok' if score >= verify.WATERMARK_FLOOR else 'too low'}{note}")
        print(f"\nuse: kria-watermark-{best}-{args.size}.png at {args.slot}")
        return 0

    if args.cmd == "card":
        surface = (card_hook(args.text, args.kicker) if args.kind == "hook"
                   else card_step(args.number, args.text))
        save_png(surface, args.out)
        print(f"wrote {args.out}")
        return 0

    if DIST.exists():
        shutil.rmtree(DIST)
    global _TEMPLATE_RECTS
    manifest = {
        "watermark": export_watermarks(),
        "outro": export_outro(prores=args.prores),
        "templates": export_templates(),
    }
    _TEMPLATE_RECTS = manifest["templates"]
    report = export_proofs()
    sync_runtime_assets()
    (DIST / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (DIST / "proofs" / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    below = {k: round(v["worst_tile"], 2)
             for k, v in report["legibility"].items()
             if v["recommended_pairing"] and not v["meets_reference"]}
    bad_geom = [k for k, v in report["placements"].items() if not v["clear"]]
    print(json.dumps({"placement_failures": bad_geom,
                      "watermark_below_reference": below}, indent=2))
    return 1 if bad_geom else 0


if __name__ == "__main__":
    raise SystemExit(main())
