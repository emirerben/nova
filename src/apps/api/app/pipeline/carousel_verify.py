"""Parity gate: our Python/Skia carousel render vs. the browser reference.

Lives next to `overlay_verify.py` (NOT inside `app/pipeline/carousel/`) because
this module is verification tooling, not the render pipeline itself — same
split as `overlay_verify.py` vs. `text_overlay_skia.py`.

Two things are compared against `tools/carousel_reference/`'s captured
browser trace (see that dir's README for the capture contract):

  1. **Motion parity** — per-frame, per-card bounding-box + opacity deltas
     between the browser's `getBoundingClientRect()`/computed-style trace
     and our `project_card_corners()`-derived equivalent (`compare_motion_traces`).
  2. **Pixel parity** — SSIM between the browser's `reference.mp4` and our
     re-encoded frame sequence (`compute_ssim`), using the exact same ffmpeg
     encode settings `capture.sh` uses so the comparison isn't skewed by
     codec/preset differences.

`generate_test_cards` recreates the four reference HTML pages' card visuals
(flat color + white-bordered inset marker) as PNGs so `render_our_side` can
drive the real renderer (`app.pipeline.carousel.renderer`) against content
that matches the browser pages pixel-for-pixel, before any per-effect
transform is applied.

No module-level `import skia` — `render_carousel_frames` (imported by name
here) only pulls skia in when it's actually called, matching the lazy-import
discipline documented in `renderer.py`'s module docstring.
"""

from __future__ import annotations

import colorsys
import glob
import os
import re
import subprocess

from PIL import Image, ImageChops, ImageDraw, ImageOps

from .carousel import effects, spring
from .carousel.cards import CardAsset
from .carousel.gesture import CANONICAL_FLICK
from .carousel.renderer import lagged_virtual_scroll, project_card_corners, render_carousel_frames
from .carousel.segment import DEFAULT_GEOMETRY, _fit_duration

# -- Card visuals, replicated from the reference HTML pages -------------------
# Read from scale-sweep.html / cover-flow.html / cards.html / flipbook.html
# (tools/carousel_reference/): all four pages share IDENTICAL card markup —
#   <div class="card" style="background: hsl(i*67, 70%, 55%);">
#     <div class="marker" style="top: {40 + 60*i}px;"></div>
#   </div>
# .card is 540x720 (== DEFAULT_GEOMETRY.card_w/card_h — imported, not
# duplicated, so the two can't drift). .marker is `position: absolute; left:
# 12px; right: 12px; height: 32px; border: 3px solid #fff; box-sizing:
# border-box; background: transparent` — i.e. an unfilled white-bordered
# rectangle inset 12px from each side, 32px tall, whose vertical position is
# the only thing that varies per card (top = 40, 100, 160, 220, 280 for cards
# 0-4 — an arithmetic sequence, step 60px).
HUE_STEP_DEG = 67.0
SATURATION_PCT = 70.0
LIGHTNESS_PCT = 55.0

MARKER_INSET_PX = 12.0
MARKER_HEIGHT_PX = 32.0
MARKER_BORDER_PX = 3
MARKER_TOP_BASE_PX = 40.0
MARKER_TOP_STEP_PX = 60.0
MARKER_COLOR = (255, 255, 255)

# #0a0a0c — the <body> background every reference HTML page sets.
REFERENCE_BACKGROUND_RGB = (0x0A, 0x0A, 0x0C)

CAROUSEL_VIEWPORT_W = 1080
N_CARDS = 5

_SSIM_LINE_RE = re.compile(r"All:([\d.]+)")
_FRAME_NAME_RE = re.compile(r"frame_(\d+)\.png$")


def _hsl_to_rgb(
    hue_deg: float, saturation_pct: float, lightness_pct: float
) -> tuple[int, int, int]:
    """CSS `hsl(hue_deg, saturation_pct%, lightness_pct%)` -> (r, g, b) 0-255.

    `colorsys.hls_to_rgb` implements the same HSL color model under a
    differently-ordered name (Hue, Lightness, Saturation) — CSS's `hsl()` and
    Python's `hls_to_rgb` are the same formula with h/l/s reordered, not two
    different color spaces.
    """
    r, g, b = colorsys.hls_to_rgb(
        (hue_deg % 360.0) / 360.0, lightness_pct / 100.0, saturation_pct / 100.0
    )
    return (round(r * 255), round(g * 255), round(b * 255))


def generate_test_cards(n: int, work_dir: str) -> list[CardAsset]:
    """Recreate the reference HTML pages' `n` card visuals as flat 540x720
    PNGs: `hsl(i*67, 70%, 55%)` background + a white-bordered inset marker
    rect, per the module docstring above. Card `i`'s marker top is
    `40 + 60*i`. Returns one `CardAsset` per card, written to
    `{work_dir}/card_{i:02d}.png`.

    Deliberately does NOT apply the CSS `border-radius: 24px` rounding —
    `render_carousel_frames`'s `_load_card_face` already rounds the corners
    at render time (see `DEFAULT_GEOMETRY.corner_radius`), so baking it in
    here would double-clip.
    """
    os.makedirs(work_dir, exist_ok=True)
    card_w = round(DEFAULT_GEOMETRY.card_w)
    card_h = round(DEFAULT_GEOMETRY.card_h)

    cards: list[CardAsset] = []
    for i in range(n):
        bg = _hsl_to_rgb(i * HUE_STEP_DEG, SATURATION_PCT, LIGHTNESS_PCT)
        img = Image.new("RGB", (card_w, card_h), bg)
        draw = ImageDraw.Draw(img)

        top = MARKER_TOP_BASE_PX + MARKER_TOP_STEP_PX * i
        x0 = MARKER_INSET_PX
        x1 = card_w - MARKER_INSET_PX - 1
        y0 = top
        y1 = top + MARKER_HEIGHT_PX - 1
        draw.rectangle([x0, y0, x1, y1], outline=MARKER_COLOR, width=MARKER_BORDER_PX)

        path = os.path.join(work_dir, f"card_{i:02d}.png")
        img.save(path, "PNG")
        cards.append(CardAsset(index=i, image_path=path))
    return cards


def render_our_side(effect: str, n_frames: int, work_dir: str) -> tuple[list[str], list[dict]]:
    """Render our side of the parity comparison for one effect.

    Simulates the canonical flick gesture over the 5-card snap layout
    (`DEFAULT_GEOMETRY`, matching `segment.py`'s production defaults),
    fits the resulting spring trace to exactly `n_frames` (truncate/pad-
    with-settled, via `segment._fit_duration` — see that function's
    docstring), renders through the real Skia path
    (`render_carousel_frames`) into `{work_dir}/frames/frame_%04d.png`
    against cards generated by `generate_test_cards`, and ALSO computes our
    predicted per-frame motion trace in the browser trace schema (see
    `tools/carousel_reference/README.md`'s `trace.json` schema) by
    projecting each card's `CardTransform` through `project_card_corners`
    and taking its axis-aligned bounding box.

    Returns `(frame_paths, our_trace)`.
    """
    geo = DEFAULT_GEOMETRY
    snaps = effects.snap_positions(effect, N_CARDS, geo, CAROUSEL_VIEWPORT_W)
    bounds = effects.snap_bounds(N_CARDS, geo, CAROUSEL_VIEWPORT_W)
    frames = spring.simulate(
        CANONICAL_FLICK, snaps, snapport_width=CAROUSEL_VIEWPORT_W, bounds=bounds
    )
    frames = _fit_duration(frames, n_frames)

    cards = generate_test_cards(N_CARDS, os.path.join(work_dir, "cards"))

    frames_dir = os.path.join(work_dir, "frames")
    frame_paths = render_carousel_frames(
        effect, frames, cards, geo, frames_dir, background_rgb=REFERENCE_BACKGROUND_RGB
    )

    our_trace: list[dict] = []
    for idx, spring_frame in enumerate(frames):
        # The rendered transform (scale/rotation/opacity) lags scrollLeft by
        # one frame (see `renderer.lagged_virtual_scroll`'s docstring); the
        # card's native-scroll LAYOUT position does not. `effects.transform_for`'s
        # `position_scroll_x` threads the un-lagged value through for that.
        scroll_x = lagged_virtual_scroll(frames, idx)
        position_scroll_x = spring_frame.virtual_scroll
        cards_out = []
        for card in cards:
            t = effects.transform_for(
                effect,
                scroll_x,
                card.index,
                geo,
                CAROUSEL_VIEWPORT_W,
                position_scroll_x=position_scroll_x,
            )
            corners = project_card_corners(t, geo)
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            left, top = min(xs), min(ys)
            width, height = max(xs) - left, max(ys) - top
            cards_out.append(
                {
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "opacity": t.opacity,
                }
            )
        our_trace.append({"i": idx, "scrollLeft": spring_frame.virtual_scroll, "cards": cards_out})

    return frame_paths, our_trace


_GEOMETRY_FIELDS = ("left", "top", "width", "height")


def compare_motion_traces(
    browser_trace: list[dict], our_trace: list[dict], tol_px: float = 2.0
) -> dict:
    """Per-frame, per-card deltas between a browser `trace.json` and our
    predicted trace (both in the schema documented in
    `tools/carousel_reference/README.md`).

    Compares `left`/`top`/`width`/`height` (absolute px delta) and `opacity`
    (absolute delta) for every card present on both sides. If the two traces
    have different frame counts (or a frame has a different card count —
    shouldn't happen, but don't crash on it), only the common prefix is
    compared; the mismatch is reported, not silently ignored.
    """
    browser_n = len(browser_trace)
    our_n = len(our_trace)
    common_n = min(browser_n, our_n)

    all_deltas: list[float] = []
    opacity_deltas: list[float] = []
    per_frame_max: list[float] = []
    worst: dict | None = None

    for frame_idx in range(common_n):
        b_cards = browser_trace[frame_idx].get("cards", [])
        o_cards = our_trace[frame_idx].get("cards", [])
        common_cards = min(len(b_cards), len(o_cards))
        frame_max = 0.0

        for card_idx in range(common_cards):
            b_card = b_cards[card_idx]
            o_card = o_cards[card_idx]

            for field in _GEOMETRY_FIELDS:
                delta = abs(float(b_card.get(field, 0.0)) - float(o_card.get(field, 0.0)))
                all_deltas.append(delta)
                frame_max = max(frame_max, delta)
                if worst is None or delta > worst["delta"]:
                    worst = {
                        "frame": frame_idx,
                        "card": card_idx,
                        "field": field,
                        "browser": b_card.get(field),
                        "ours": o_card.get(field),
                        "delta": delta,
                    }

            opacity_deltas.append(
                abs(float(b_card.get("opacity", 1.0)) - float(o_card.get("opacity", 1.0)))
            )

        per_frame_max.append(frame_max)

    max_delta_px = max(all_deltas) if all_deltas else 0.0
    mean_delta_px = (sum(all_deltas) / len(all_deltas)) if all_deltas else 0.0
    max_opacity_delta = max(opacity_deltas) if opacity_deltas else 0.0

    return {
        "max_delta_px": max_delta_px,
        "mean_delta_px": mean_delta_px,
        "max_opacity_delta": max_opacity_delta,
        "worst": worst,
        "per_frame_max": per_frame_max,
        "compared_frames": common_n,
        "browser_frame_count": browser_n,
        "our_frame_count": our_n,
        "frame_count_mismatch": browser_n != our_n,
        "pass": max_delta_px <= tol_px,
    }


def compute_ssim(reference_mp4: str, ours_mp4: str, work_dir: str) -> dict:
    """Global + worst-frame SSIM between two mp4s via ffmpeg's `ssim` filter.

    Pattern (parse the "All:" summary line ffmpeg prints to stderr, plus the
    per-frame stats file for the worst frame) is replicated — not imported —
    from `tests/quality/single_pass_parity.py::_compute_ssim`: that module
    lives under `tests/`, which isn't a package the `app` code depends on, so
    importing it from here would be a layering violation. Credit: the
    parsing logic below is a direct port of that function.
    """
    os.makedirs(work_dir, exist_ok=True)
    stats_path = os.path.join(work_dir, "ssim_stats.log")
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "info",
            "-y",
            "-i",
            reference_mp4,
            "-i",
            ours_mp4,
            "-lavfi",
            f"ssim=stats_file={stats_path}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"carousel_verify.compute_ssim: ffmpeg ssim failed: {result.stderr[-500:]}"
        )

    global_ssim = 0.0
    for line in reversed(result.stderr.splitlines()):
        match = _SSIM_LINE_RE.search(line)
        if match:
            global_ssim = float(match.group(1))
            break

    min_frame = 1.0
    if os.path.exists(stats_path):
        with open(stats_path) as f:
            for stats_line in f:
                match = _SSIM_LINE_RE.search(stats_line)
                if match:
                    min_frame = min(min_frame, float(match.group(1)))

    return {"global": global_ssim, "min_frame": min_frame, "stats_path": stats_path}


def encode_frames_to_mp4(frames_dir: str, output_path: str, fps: int = 30) -> None:
    """Mux `{frames_dir}/frame_%04d.png` into `output_path` with the EXACT
    ffmpeg settings `tools/carousel_reference/capture.sh` uses to produce
    `reference.mp4` (`libx264`, `preset fast`, `crf 18`, `yuv420p`) — so
    `compute_ssim` compares like-for-like encodes, not an artifact of
    mismatched codec settings. See `capture.sh`'s final `ffmpeg` call.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        os.path.join(frames_dir, "frame_%04d.png"),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(
            f"carousel_verify.encode_frames_to_mp4: ffmpeg failed "
            f"(rc={result.returncode}): {result.stderr[-2000:]}"
        )


def build_side_by_side_montage(
    ref_frames_dir: str, our_frames_dir: str, out_png: str, sample_every: int = 15
) -> None:
    """PIL contact sheet: one row per sampled frame, three columns
    `[reference | ours | abs-diff heatmap]`, each downscaled to ~270px wide.

    Frames are matched by filename — both sides use the `frame_%04d.png`
    convention (`capture.sh`'s screenshots on the reference side,
    `render_carousel_frames`'s output on ours), sampled every
    `sample_every`-th reference frame. The diff is computed at native
    resolution (`ImageChops.difference` + `ImageOps.autocontrast`) before
    downscaling, so small deltas aren't lost to the thumbnail resize. Frame
    index / column-header labels are drawn in the padding outside each
    thumbnail — they are not part of the compared imagery.
    """
    ref_paths = sorted(glob.glob(os.path.join(ref_frames_dir, "frame_*.png")))
    if not ref_paths:
        raise RuntimeError(
            f"build_side_by_side_montage: no reference frames (frame_*.png) in {ref_frames_dir!r}"
        )
    sampled = ref_paths[:: max(1, sample_every)]

    thumb_w = 270
    pad = 8
    label_h = 18
    header_h = 22
    headers = ("reference", "ours", "abs diff")

    with Image.open(sampled[0]) as im0:
        aspect = im0.height / im0.width
    thumb_h = round(thumb_w * aspect)

    cell_w = thumb_w + pad * 2
    cell_h = thumb_h + pad * 2 + label_h
    sheet = Image.new(
        "RGB", (cell_w * len(headers), header_h + cell_h * len(sampled)), (16, 16, 18)
    )
    draw = ImageDraw.Draw(sheet)
    for col_idx, header in enumerate(headers):
        draw.text((col_idx * cell_w + pad, 4), header, fill=(200, 200, 205))

    for row_idx, ref_path in enumerate(sampled):
        basename = os.path.basename(ref_path)
        our_path = os.path.join(our_frames_dir, basename)
        name_match = _FRAME_NAME_RE.search(basename)
        frame_label = name_match.group(1) if name_match else "?"

        with Image.open(ref_path).convert("RGB") as ref_im:
            ref_full = ref_im.copy()

        if os.path.exists(our_path):
            with Image.open(our_path).convert("RGB") as our_im:
                our_full = our_im.copy()
            if our_full.size != ref_full.size:
                our_full = our_full.resize(ref_full.size)
            diff_full = ImageOps.autocontrast(ImageChops.difference(ref_full, our_full))
        else:
            # No matching frame on our side (frame-count mismatch) — render a
            # visibly-flagged placeholder rather than crashing the montage.
            our_full = Image.new("RGB", ref_full.size, (60, 0, 0))
            diff_full = Image.new("RGB", ref_full.size, (60, 0, 0))

        y0 = header_h + row_idx * cell_h
        for col_idx, img in enumerate((ref_full, our_full, diff_full)):
            thumb = img.resize((thumb_w, thumb_h))
            x0 = col_idx * cell_w + pad
            sheet.paste(thumb, (x0, y0 + pad))
        draw.text((pad, y0 + pad + thumb_h + 2), f"frame {frame_label}", fill=(180, 180, 185))

    out_dir = os.path.dirname(out_png)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    sheet.save(out_png, "PNG")
