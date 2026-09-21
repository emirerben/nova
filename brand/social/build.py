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
from pathlib import Path

import numpy as np
import skia

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # noqa: E402
from kria_brand import (  # noqa: E402
    BUTTER, alpha_bbox, FPS, H, INK, PAPER, SKY, TEXT_SAFE, W,
    draw_rgba, ease_out_cubic, font, hex_to_color, save_png, shadow_filter,
    soft_shadow,
    wordmark, wrap,
)

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"

# --- placements (all verified against the chrome map before export) -----------
WATERMARK_SIZES = {"compact": 140, "standard": 168, "demo": 210}
WATERMARK_HOME = (60, 210)        # top-left, primary
WATERMARK_ALT = (60, 1400)        # bottom-left, when the top-left is busy

OUTRO_FRAMES = 48                 # 1.6s at 30fps
MARK_W_OUTRO = 560


# =============================================================================
# watermark
# =============================================================================

VARIANTS = {
    # name:   (ink colour, baked opacity, shadow?, scrim opacity or None)
    "plate":  (PAPER, 0.95, True, 0.48),   # universal: works on any footage
    "white":  (PAPER, 0.88, True, None),   # dark / mid / busy footage
    "ink":    (INK,   0.85, False, None),  # bright / pale / warm footage
    "sky":    (SKY,   1.00, False, None),  # white product screens, UI demos
}

# Which footage each variant is signed off for. The build gates on exactly
# these pairings; the report still measures the full cross-product so the
# out-of-policy combinations stay visible as evidence rather than folklore.
RECOMMENDED = {
    "plate": ("bright", "dark", "mid", "warm", "busy"),
    "white": ("dark", "mid", "busy"),
    "ink": ("bright", "warm"),
    # `sky` is the brand logotype on white product surfaces, not text over
    # footage. WCAG 1.4.11 exempts logotypes from the contrast minimum, so it
    # is measured and reported but never gated.
    "sky": (),
}

SHADOW_PAD = 30
PLATE_PAD_X, PLATE_PAD_Y, PLATE_RADIUS, PLATE_FEATHER = 26, 17, 20, 7


def build_watermark(name: str, width: int) -> np.ndarray:
    """A padded RGBA tile: scrim, mark, shadow, opacity already baked in.

    Opacity is baked rather than left to the editor because the one thing that
    reliably goes wrong in a hurry is somebody typing a different number into
    CapCut every week.
    """
    fill, opacity, shadowed, scrim = VARIANTS[name]
    mark = wordmark(width, fill=fill)
    mw, mh = mark.size
    pad = watermark_pad(name)
    surface = skia.Surface(mw + pad * 2, mh + pad * 2)
    with surface as canvas:
        canvas.clear(skia.ColorTRANSPARENT)
        if scrim is not None:
            # Feathered, so it reads as a soft chip rather than a box.
            sp = skia.Paint(AntiAlias=True,
                            Color=skia.Color(0, 0, 0, int(round(scrim * 255))))
            sp.setImageFilter(skia.ImageFilters.Blur(PLATE_FEATHER, PLATE_FEATHER))
            canvas.drawRoundRect(
                skia.Rect.MakeLTRB(pad - PLATE_PAD_X, pad - PLATE_PAD_Y,
                                   pad + mw + PLATE_PAD_X, pad + mh + PLATE_PAD_Y),
                PLATE_RADIUS, PLATE_RADIUS, sp)
        paint = (soft_shadow(dy=2, sigma=6, opacity=0.35) if scrim is not None
                 else soft_shadow() if shadowed else skia.Paint(AntiAlias=True))
        draw_rgba(canvas, mark.full, pad, pad, paint, opacity)
    return np.array(surface.makeImageSnapshot().toarray())


def watermark_pad(name: str) -> int:
    """Transparent margin the tile carries, so masks can be aligned to it."""
    _fill, _opacity, shadowed, scrim = VARIANTS[name]
    return SHADOW_PAD if (shadowed or scrim is not None) else 0


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
                    "top-left": [WATERMARK_HOME[0] - pad, WATERMARK_HOME[1] - pad],
                    "bottom-left": [WATERMARK_ALT[0] - pad, WATERMARK_ALT[1] - pad],
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
        tile = build_watermark("plate", width)
        pad = watermark_pad("plate")
        th, tw = tile.shape[0] - pad * 2, tile.shape[1] - pad * 2
        for slot, (px, py) in (("top-left", WATERMARK_HOME),
                               ("bottom-left", WATERMARK_ALT)):
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
        std = build_watermark("plate", WATERMARK_SIZES["standard"])
        draw_rgba(canvas, std, *WATERMARK_HOME)
        draw_rgba(canvas, std, *WATERMARK_ALT)
        f = font("Inter-Medium", 34)
        p = skia.Paint(AntiAlias=True, Color=hex_to_color(INK))
        canvas.drawString("safe rectangle 60,200 - 890,1520", 70, 1560, f, p)
        canvas.drawString("red = TikTok / Reels / Shorts chrome", 70, 1610, f, p)
    save_png(surface, out / "safezone-map.png")

    # 3. photometry: each variant over each background class.
    for bg_name, bg in _backgrounds().items():
        for variant in VARIANTS:
            width = WATERMARK_SIZES["standard"]
            tile = build_watermark(variant, width)
            layer = _overlay_rgba(tile, *WATERMARK_HOME)
            composite = verify.over(layer, bg)

            # Mask from the ink alone (no shadow/scrim) so the glyph is the
            # subject. It must be padded exactly as `build_watermark` padded
            # the tile, or the mask lands beside the ink and every ratio
            # reads 1.00.
            fill = VARIANTS[variant][0]
            bare = wordmark(width, fill=fill)
            ink_layer = _overlay_rgba(
                _pad(bare.full, watermark_pad(variant)), *WATERMARK_HOME)

            res = verify.contrast(ink_layer, composite)
            entry = res.as_dict()
            entry["recommended_pairing"] = bg_name in RECOMMENDED[variant]
            entry["gated"] = entry["recommended_pairing"]
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
        tile = build_watermark(variant, width)
        composite = verify.over(_overlay_rgba(tile, *slot), bg)
        bare = wordmark(width, fill=VARIANTS[variant][0])
        ink = _overlay_rgba(_pad(bare.full, watermark_pad(variant)), *slot)
        scores[variant] = verify.contrast(ink, composite).worst_tile

    # Prefer the least obtrusive mark that clears the design target; fall back
    # to the plate, which is the variant that always works.
    plain = [v for v in ("ink", "white")
             if scores[v] >= verify.CONTRAST_TARGET]
    best = max(plain, key=lambda v: scores[v]) if plain else "plate"
    return best, scores


# =============================================================================
# plumbing
# =============================================================================

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
    pick.add_argument("--slot", choices=["top-left", "bottom-left"], default="top-left")
    pick.add_argument("--size", choices=list(WATERMARK_SIZES), default="standard")

    card = sub.add_parser("card", help="render one template card")
    card.add_argument("--kind", choices=["hook", "step"], required=True)
    card.add_argument("--text", required=True)
    card.add_argument("--kicker")
    card.add_argument("--number", type=int, default=1)
    card.add_argument("--out", type=Path, default=Path("card.png"))
    args = ap.parse_args()

    if args.cmd == "pick":
        slot = WATERMARK_HOME if args.slot == "top-left" else WATERMARK_ALT
        best, scores = pick_variant(args.frame, args.at, slot, args.size)
        for name, score in sorted(scores.items(), key=lambda kv: -kv[1]):
            note = "" if name in ("plate", "white", "ink") else "  (logotype, not gated)"
            print(f"  {name:6} {score:6.2f}:1"
                  f"  {'ok' if score >= verify.CONTRAST_FLOOR else 'too low'}{note}")
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
    (DIST / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (DIST / "proofs" / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    failures = [k for k, v in report["legibility"].items()
                if v["gated"] and not v["passes_floor"]]
    bad_geom = [k for k, v in report["placements"].items() if not v["clear"]]
    print(json.dumps({"legibility_failures": failures,
                      "placement_failures": bad_geom}, indent=2))
    return 1 if (failures or bad_geom) else 0


if __name__ == "__main__":
    raise SystemExit(main())
