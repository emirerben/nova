#!/usr/bin/env python3
"""Per-frame font fingerprinting of the BRAZIL/PERU title animation.

For every sampled frame in a Dimples Passport render, this tool:

  1. Locates the yellow title bbox (color mask, geometry-filtered).
  2. Binarizes the yellow region into a 1-bit glyph mask.
  3. Compares that mask against rendered templates of each cycle font
     (PlayfairDisplay Bold, Montserrat ExtraBold, Instrument Serif,
     Bodoni Moda Bold, Fraunces Bold, Permanent Marker, Pacifico) using
     normalized intersection-over-union (IoU).
  4. Picks the best-matching font per frame.

Then summarizes the animation:

  - Font phase timeline:    t=3.55s → Pacifico, t=3.62s → Bodoni Moda, ...
  - Phase durations:        run-length of each contiguous same-font window
  - Cycle interval:         median of inter-transition gaps
  - Font frequency:         how often each font appears across the cycle
  - Settle detection:       a final stretch ≥ FONT_CYCLE_SETTLE_RATIO of
                            the cycle held on one font signals the settle
                            phase fired (the renderer's expected behavior
                            when font_cycle_accel_at_s is absent).
  - Accel detection:        compares median interval in first half vs
                            second half — a drop ≥ 30% = accel ramp fired.
  - Visual strip:           writes a PNG strip showing every sampled
                            BRAZIL crop with its identified font label,
                            so you can eyeball the timeline.

In diff mode (REF=… OURS=…), the two timelines render side by side and
the tool emits a table of per-font deltas (frequency, total visible
time) plus a transition-rate comparison.

Usage:
    python analyze_brazil_animation.py path/to/video.mp4
    python analyze_brazil_animation.py REF=brazil.mp4 OURS=output.mp4
    python analyze_brazil_animation.py REF=ref.mp4 OURS=output.mp4 \\
        --sample-step 0.033 --strip /tmp/brazil_anim.png

Sampling at 0.033s (per-frame at 30fps) catches every cycle change at
the FONT_CYCLE_FAST_INTERVAL_S=0.07 tempo. Coarser sampling will miss
some transitions but is faster.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --- Font registry (must match what the renderer cycles through) ---
# Resolved from src/apps/api/app/pipeline/text_overlay.py:_CYCLE_CONTRAST_NAMES
# and OVERLAY_FONT_PATH. Index 0 is the settle font.

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

CYCLE_FONTS = [
    # (display_name, file, category)
    ("Playfair Bold",      "PlayfairDisplay-Bold.ttf",     "serif-display"),  # settle
    ("Montserrat",         "Montserrat-ExtraBold.ttf",     "sans"),
    ("Instrument Serif",   "InstrumentSerif-Regular.ttf",  "serif-thin"),
    ("Bodoni Moda",        "BodoniModa-Bold.ttf",          "serif-fat"),
    ("Fraunces",           "Fraunces-Bold.ttf",            "serif-mod"),
    ("Permanent Marker",   "PermanentMarker-Regular.ttf",  "brush"),
    ("Pacifico",           "Pacifico-Regular.ttf",         "script"),
]

# --- Color targets (match seed_dimples_passport_brazil.py) ---
YELLOW_RGB = (244, 208, 63)
YELLOW_CH_TOL = 35
YELLOW_MIN_PIXELS = 800
TITLE_Y_FRAC = 0.45
TITLE_Y_BAND_PX = 250
TITLE_MAX_HEIGHT_PX = 400
TITLE_CENTROID_X_MIN_FRAC = 0.15
TITLE_CENTROID_X_MAX_FRAC = 0.85

FRAME_HEIGHT = 1920
FRAME_WIDTH = 1080
# Normalize all template masks and frame crops to this size for comparison
NORM_W = 600
NORM_H = 180
# Templates rendered at this font size — larger gives better fingerprint quality
TEMPLATE_FONT_SIZE = 200


# ---------------- template generation ----------------

@dataclass
class FontTemplate:
    name: str
    category: str
    mask: np.ndarray  # bool, shape (NORM_H, NORM_W)


def _build_templates(text: str = "BRAZIL") -> list[FontTemplate]:
    templates = []
    for name, file, category in CYCLE_FONTS:
        font_path = FONTS_DIR / file
        if not font_path.exists():
            print(f"warning: font missing: {font_path}", file=sys.stderr)
            continue
        font = ImageFont.truetype(str(font_path), TEMPLATE_FONT_SIZE)
        # Render to oversized canvas, then tight-crop to text bbox
        canvas = Image.new("L", (TEMPLATE_FONT_SIZE * 8, TEMPLATE_FONT_SIZE * 3), 0)
        d = ImageDraw.Draw(canvas)
        d.text((20, 20), text, fill=255, font=font)
        arr = np.array(canvas)
        ys, xs = np.where(arr > 64)
        if len(xs) == 0:
            continue
        tight = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        # Normalize to (NORM_H, NORM_W) — pad if aspect doesn't match
        tight_img = Image.fromarray(tight, "L")
        norm = _aspect_pad_resize(tight_img, NORM_W, NORM_H)
        mask = np.array(norm) > 64
        templates.append(FontTemplate(name=name, category=category, mask=mask))
    return templates


def _aspect_pad_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize preserving aspect ratio, then pad with black to target dims.

    Critical for fair font comparison: a stretched-without-padding resize
    would erase aspect-ratio differences between fonts (Pacifico's swooshes
    vs Montserrat's geometric blocks become indistinguishable).
    """
    iw, ih = img.size
    scale = min(target_w / iw, target_h / ih)
    new_w, new_h = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    out = Image.new("L", (target_w, target_h), 0)
    out.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return out


# ---------------- frame extraction + analysis ----------------

def _check_deps() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("error: ffmpeg not found on PATH")


def _video_duration_s(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)], text=True)
    return float(out.strip())


def _extract_frames(video: Path, sample_step_s: float, out_dir: Path) -> list[Path]:
    fps = 1.0 / sample_step_s
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video), "-vf", f"fps={fps}",
        "-y", str(out_dir / "f_%04d.png"),
    ], check=True)
    return sorted(out_dir.glob("f_*.png"))


def _yellow_mask_in_band(frame: np.ndarray) -> tuple[np.ndarray, int, int] | None:
    """Return (mask, y_offset_in_full_frame, x_offset_in_full_frame) for the
    yellow title region, or None if no title text is present."""
    expected_y = int(TITLE_Y_FRAC * FRAME_HEIGHT)
    y_lo = max(0, expected_y - TITLE_Y_BAND_PX)
    y_hi = min(FRAME_HEIGHT, expected_y + TITLE_Y_BAND_PX)
    band = frame[y_lo:y_hi]
    r = band[..., 0].astype(int)
    g = band[..., 1].astype(int)
    b = band[..., 2].astype(int)
    mask = (
        (np.abs(r - YELLOW_RGB[0]) < YELLOW_CH_TOL) &
        (np.abs(g - YELLOW_RGB[1]) < YELLOW_CH_TOL) &
        (np.abs(b - YELLOW_RGB[2]) < YELLOW_CH_TOL)
    )
    if int(mask.sum()) < YELLOW_MIN_PIXELS:
        return None
    ys, xs = np.where(mask)
    h = int(ys.max() - ys.min())
    cx = float(xs.mean())
    if h > TITLE_MAX_HEIGHT_PX:
        return None
    if cx < TITLE_CENTROID_X_MIN_FRAC * FRAME_WIDTH:
        return None
    if cx > TITLE_CENTROID_X_MAX_FRAC * FRAME_WIDTH:
        return None
    # Tight-crop the yellow region
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return crop, int(ys.min() + y_lo), int(xs.min())


def _normalize_crop(crop: np.ndarray) -> np.ndarray:
    """Aspect-preserving resize of crop to (NORM_H, NORM_W) bool mask."""
    img = Image.fromarray((crop.astype(np.uint8) * 255), "L")
    norm = _aspect_pad_resize(img, NORM_W, NORM_H)
    return np.array(norm) > 64


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union > 0 else 0.0


@dataclass
class FrameObs:
    t: float
    crop: np.ndarray | None
    font_name: str | None
    iou_scores: dict = field(default_factory=dict)  # font_name → iou


def _identify_font(crop: np.ndarray, templates: list[FontTemplate]) -> tuple[str, dict]:
    norm = _normalize_crop(crop)
    scores = {t.name: _iou(norm, t.mask) for t in templates}
    best = max(scores, key=scores.get)
    return best, scores


def analyze_video(video: Path, sample_step_s: float, label: str,
                  templates: list[FontTemplate]) -> dict:
    print(f"\n[{label}] analyzing {video.name} ...", file=sys.stderr)
    duration = _video_duration_s(video)
    obs: list[FrameObs] = []
    with tempfile.TemporaryDirectory(prefix=f"brazil_anim_{label}_") as td:
        td_path = Path(td)
        frames = _extract_frames(video, sample_step_s, td_path)
        print(f"[{label}] {len(frames)} frames at {sample_step_s}s step",
              file=sys.stderr)
        for i, fp in enumerate(frames):
            t = i * sample_step_s
            arr = np.array(Image.open(fp).convert("RGB"))
            res = _yellow_mask_in_band(arr)
            if res is None:
                obs.append(FrameObs(t=t, crop=None, font_name=None))
                continue
            crop, y0, x0 = res
            font_name, scores = _identify_font(crop, templates)
            obs.append(FrameObs(t=t, crop=crop, font_name=font_name,
                                 iou_scores=scores))
    return _summarize(obs, sample_step_s, duration, str(video), label)


# ---------------- summary stats ----------------

def _runs(seq: list[str | None]) -> list[tuple[str | None, int]]:
    """Run-length encode a sequence."""
    out = []
    cur, n = None, 0
    for x in seq:
        if x == cur:
            n += 1
        else:
            if cur is not None or n > 0:
                out.append((cur, n))
            cur, n = x, 1
    if n > 0:
        out.append((cur, n))
    return out


def _summarize(obs: list[FrameObs], step_s: float, duration: float,
               video: str, label: str) -> dict:
    visible = [o for o in obs if o.font_name is not None]
    if not visible:
        return {
            "label": label, "video": video, "duration_s": duration,
            "sample_step_s": step_s, "frames_total": len(obs),
            "frames_with_title": 0,
            "summary": "no title detected in any frame",
        }

    # Find contiguous "title visible" windows
    visible_indices = [i for i, o in enumerate(obs) if o.font_name is not None]
    first_t = obs[visible_indices[0]].t
    last_t = obs[visible_indices[-1]].t

    # Restrict animation analysis to the longest contiguous visible window
    # (the BRAZIL phase, not stray yellow b-roll). Use the largest run of
    # not-None values.
    spans = []
    cur_start = None
    for i, o in enumerate(obs):
        if o.font_name is not None:
            if cur_start is None:
                cur_start = i
        else:
            if cur_start is not None:
                spans.append((cur_start, i - 1))
                cur_start = None
    if cur_start is not None:
        spans.append((cur_start, len(obs) - 1))
    # Longest span = the BRAZIL window
    longest = max(spans, key=lambda s: s[1] - s[0])
    primary = obs[longest[0]:longest[1] + 1]
    primary_fonts = [o.font_name for o in primary]
    primary_runs = _runs(primary_fonts)

    # Cycle interval = median run length × step_s for runs of length ≥ 1
    run_durations_s = [n * step_s for _, n in primary_runs]
    cycle_interval_s = median(run_durations_s)

    # Settle phase = last run if its duration > FONT_CYCLE_SETTLE_RATIO of total
    total_dur = (longest[1] - longest[0] + 1) * step_s
    settle_run_n = primary_runs[-1][1]
    settle_run_s = settle_run_n * step_s
    settle_fired = settle_run_s >= 0.20 * total_dur and settle_run_n >= 3
    settle_font = primary_runs[-1][0] if settle_fired else None

    # Accel detection: compare median run length in first half vs second half
    mid = len(primary_runs) // 2
    first_half = primary_runs[:mid] if mid else primary_runs
    second_half = primary_runs[mid:]
    if len(first_half) >= 2 and len(second_half) >= 2:
        m1 = median([n * step_s for _, n in first_half])
        m2 = median([n * step_s for _, n in second_half])
        accel_fired = m2 < m1 * 0.7 or m1 < m2 * 0.7
        accel_dir = "speedup" if m2 < m1 else ("slowdown" if m2 > m1 else "flat")
        accel_first_half_interval = m1
        accel_second_half_interval = m2
    else:
        accel_fired = False
        accel_dir = "n/a"
        accel_first_half_interval = None
        accel_second_half_interval = None

    # Font frequency: total frame count + total time per font
    freq = Counter(primary_fonts)
    freq_pct = {
        f: {"frames": n, "time_s": n * step_s, "pct_of_window": n / len(primary) * 100}
        for f, n in freq.items()
    }

    transitions = sum(1 for a, b in zip(primary_fonts, primary_fonts[1:]) if a != b)
    transitions_per_sec = transitions / total_dur if total_dur > 0 else 0

    # Confidence: mean IoU of the chosen font across the BRAZIL window
    chosen_ious = [o.iou_scores.get(o.font_name, 0.0) for o in primary if o.font_name]
    mean_chosen_iou = sum(chosen_ious) / len(chosen_ious)

    return {
        "label": label,
        "video": video,
        "duration_s": duration,
        "sample_step_s": step_s,
        "frames_total": len(obs),
        "frames_with_title": len(visible),
        "title_window": {
            "first_appearance_s": first_t,
            "last_appearance_s": last_t,
        },
        "primary_brazil_window": {
            "start_s": primary[0].t,
            "end_s": primary[-1].t,
            "duration_s": total_dur,
            "frames": len(primary),
        },
        "font_runs": [
            {"font": f, "frames": n, "duration_s": n * step_s,
             "start_s": primary[i_start].t}
            for (f, n), i_start in _annotate_runs(primary_runs, primary)
        ],
        "font_frequency": freq_pct,
        "cycle": {
            "transitions": transitions,
            "transitions_per_sec": transitions_per_sec,
            "median_run_duration_s": cycle_interval_s,
            "implied_cycle_interval_s": cycle_interval_s,
            "distinct_fonts_seen": len(freq),
        },
        "settle": {
            "fired": settle_fired,
            "font": settle_font,
            "duration_s": settle_run_s if settle_fired else 0.0,
        },
        "accel": {
            "fired": accel_fired,
            "direction": accel_dir,
            "first_half_median_interval_s": accel_first_half_interval,
            "second_half_median_interval_s": accel_second_half_interval,
        },
        "matching_confidence_mean_iou": mean_chosen_iou,
        "_obs": obs,  # carry through for visual strip rendering
    }


def _annotate_runs(runs: list[tuple[str | None, int]],
                   primary: list[FrameObs]) -> list[tuple[tuple, int]]:
    out = []
    i = 0
    for run in runs:
        out.append((run, i))
        i += run[1]
    return out


# ---------------- rendering ----------------

def _fmt(v, dp=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{dp}f}"
    return str(v)


def render_single_report(rep: dict) -> str:
    lines = []
    lines.append("=" * 84)
    lines.append(f"BRAZIL FONT-CYCLE ANIMATION — {rep['label']}")
    lines.append("=" * 84)
    lines.append(f"video:             {rep['video']}")
    lines.append(f"duration:          {rep['duration_s']:.2f}s")
    lines.append(f"sampled:           {rep['frames_total']} frames "
                 f"at {rep['sample_step_s']}s step")
    if rep['frames_with_title'] == 0:
        lines.append("\n  NO TITLE DETECTED — yellow text not found in any frame")
        return "\n".join(lines)

    pw = rep["primary_brazil_window"]
    lines.append(f"BRAZIL window:     {pw['start_s']:.2f}s → {pw['end_s']:.2f}s "
                 f"(duration {pw['duration_s']:.2f}s, {pw['frames']} frames)")
    lines.append(f"match confidence:  mean IoU {rep['matching_confidence_mean_iou']:.3f} "
                 f"({rep['frames_with_title']} title frames analyzed)")

    cyc = rep["cycle"]
    lines.append("")
    lines.append("--- CYCLE STATS ---")
    lines.append(f"  transitions:               {cyc['transitions']} "
                 f"({cyc['transitions_per_sec']:.2f}/sec)")
    lines.append(f"  median run duration:       {cyc['median_run_duration_s']:.3f}s")
    lines.append(f"  implied cycle interval:    {cyc['implied_cycle_interval_s']:.3f}s")
    lines.append(f"  distinct fonts seen:       {cyc['distinct_fonts_seen']} of "
                 f"{len(CYCLE_FONTS)}")

    settle = rep["settle"]
    lines.append("")
    lines.append("--- SETTLE PHASE ---")
    if settle["fired"]:
        lines.append(f"  DETECTED — last run held {settle['duration_s']:.2f}s "
                     f"on '{settle['font']}'")
    else:
        lines.append("  not detected — cycle ran through to the end without "
                     "settling on one font (matches font_cycle_accel_at_s=0.0)")

    accel = rep["accel"]
    lines.append("")
    lines.append("--- ACCEL / RAMP DETECTION ---")
    if accel["fired"]:
        d = accel["direction"]
        lines.append(f"  {d.upper()} DETECTED: median interval shifted from "
                     f"{accel['first_half_median_interval_s']:.3f}s in first half "
                     f"to {accel['second_half_median_interval_s']:.3f}s in second")
    else:
        m1 = accel.get("first_half_median_interval_s")
        m2 = accel.get("second_half_median_interval_s")
        lines.append(f"  tempo flat across the cycle "
                     f"(first half {_fmt(m1)}s vs second half {_fmt(m2)}s)")

    lines.append("")
    lines.append("--- FONT FREQUENCY ---")
    lines.append(f"  {'font':22s}  {'frames':>7s}  {'visible':>10s}  {'% of window':>12s}")
    lines.append(f"  {'-'*22}  {'-'*7}  {'-'*10}  {'-'*12}")
    rows = sorted(rep["font_frequency"].items(),
                  key=lambda kv: -kv[1]["frames"])
    for fname, stats in rows:
        lines.append(f"  {fname:22s}  {stats['frames']:>7d}  "
                     f"{stats['time_s']:>8.2f}s  {stats['pct_of_window']:>10.1f}%")

    lines.append("")
    lines.append("--- PHASE TIMELINE (first 30 runs) ---")
    lines.append(f"  {'t_start':>8s}  {'duration':>10s}  font")
    lines.append(f"  {'-'*8}  {'-'*10}  {'-'*22}")
    for r in rep["font_runs"][:30]:
        lines.append(f"  {r['start_s']:>7.3f}s  {r['duration_s']:>8.3f}s  "
                     f"{r['font']}")
    if len(rep["font_runs"]) > 30:
        lines.append(f"  ... ({len(rep['font_runs']) - 30} more runs)")

    return "\n".join(lines)


def render_diff_report(ref: dict, ours: dict) -> str:
    lines = []
    lines.append("=" * 100)
    lines.append("BRAZIL FONT-CYCLE DIFF — REF vs OURS")
    lines.append("=" * 100)
    if ref["frames_with_title"] == 0 or ours["frames_with_title"] == 0:
        lines.append("  one side has no detected title — cannot diff")
        return "\n".join(lines)

    def row(metric, rv, ov, dfmt=lambda d: f"{d:+.3f}", notes=""):
        if isinstance(rv, (int, float)) and isinstance(ov, (int, float)):
            d = dfmt(ov - rv)
        else:
            d = ""
        lines.append(f"  {metric:34s} {_fmt(rv):>12s} {_fmt(ov):>12s} {d:>12s}  {notes}")

    lines.append(f"  {'metric':34s} {'REF':>12s} {'OURS':>12s} {'DELTA':>12s}  notes")
    lines.append(f"  {'-'*34} {'-'*12} {'-'*12} {'-'*12}  {'-'*40}")

    rw = ref["primary_brazil_window"]
    ow = ours["primary_brazil_window"]
    row("BRAZIL window start (s)", rw["start_s"], ow["start_s"])
    row("BRAZIL window end (s)", rw["end_s"], ow["end_s"])
    row("BRAZIL window duration (s)", rw["duration_s"], ow["duration_s"])

    rc, oc = ref["cycle"], ours["cycle"]
    row("transitions in window", rc["transitions"], oc["transitions"],
        dfmt=lambda d: f"{d:+.0f}")
    row("transitions / sec", rc["transitions_per_sec"], oc["transitions_per_sec"])
    row("median run duration (s)", rc["median_run_duration_s"],
        oc["median_run_duration_s"], notes="cycle interval")
    row("distinct fonts seen", rc["distinct_fonts_seen"],
        oc["distinct_fonts_seen"], dfmt=lambda d: f"{d:+.0f}",
        notes=f"of {len(CYCLE_FONTS)}")

    rs, os_ = ref["settle"], ours["settle"]
    lines.append("")
    lines.append("--- SETTLE PHASE ---")
    lines.append(f"  REF:  fired={rs['fired']}, font={rs['font']}, "
                 f"duration={rs['duration_s']:.2f}s")
    lines.append(f"  OURS: fired={os_['fired']}, font={os_['font']}, "
                 f"duration={os_['duration_s']:.2f}s")

    ra, oa = ref["accel"], ours["accel"]
    lines.append("")
    lines.append("--- ACCEL / RAMP ---")
    lines.append(f"  REF:  fired={ra['fired']}, dir={ra['direction']}, "
                 f"first-half {_fmt(ra.get('first_half_median_interval_s'))}s "
                 f"→ second-half {_fmt(ra.get('second_half_median_interval_s'))}s")
    lines.append(f"  OURS: fired={oa['fired']}, dir={oa['direction']}, "
                 f"first-half {_fmt(oa.get('first_half_median_interval_s'))}s "
                 f"→ second-half {_fmt(oa.get('second_half_median_interval_s'))}s")

    lines.append("")
    lines.append("--- FONT FREQUENCY (frames per font in the BRAZIL window) ---")
    fonts_all = sorted(set(list(ref["font_frequency"].keys()) +
                            list(ours["font_frequency"].keys())))
    lines.append(f"  {'font':22s}  {'REF frames':>11s}  {'REF time':>10s}  "
                 f"{'OURS frames':>12s}  {'OURS time':>10s}  delta time")
    lines.append(f"  {'-'*22}  {'-'*11}  {'-'*10}  {'-'*12}  {'-'*10}  {'-'*10}")
    for f in fonts_all:
        rstats = ref["font_frequency"].get(f, {"frames": 0, "time_s": 0.0})
        ostats = ours["font_frequency"].get(f, {"frames": 0, "time_s": 0.0})
        dt = ostats["time_s"] - rstats["time_s"]
        lines.append(f"  {f:22s}  {rstats['frames']:>11d}  "
                     f"{rstats['time_s']:>8.2f}s  {ostats['frames']:>12d}  "
                     f"{ostats['time_s']:>8.2f}s  {dt:+.2f}s")

    lines.append("")
    lines.append("VERDICT:")
    issues = []
    # Tempo
    if oc["transitions_per_sec"] > rc["transitions_per_sec"] * 1.25:
        issues.append(f"OURS cycles {oc['transitions_per_sec']/rc['transitions_per_sec']:.2f}× "
                      f"faster than REF ({oc['transitions_per_sec']:.2f} vs "
                      f"{rc['transitions_per_sec']:.2f} transitions/sec)")
    elif rc["transitions_per_sec"] > oc["transitions_per_sec"] * 1.25:
        issues.append(f"REF cycles {rc['transitions_per_sec']/oc['transitions_per_sec']:.2f}× "
                      f"faster than OURS")
    # Settle
    if rs["fired"] and not os_["fired"]:
        issues.append("REF has a SETTLE phase (ends on one font); OURS keeps cycling "
                      "through (matches our font_cycle_accel_at_s=0.0)")
    if os_["fired"] and not rs["fired"]:
        issues.append("OURS settles; REF does not — investigate")
    # Accel
    if ra["fired"] and not oa["fired"]:
        issues.append(f"REF has an accel ramp ({ra['direction']}); OURS tempo is flat")
    if oa["fired"] and not ra["fired"]:
        issues.append(f"OURS has an accel ramp ({oa['direction']}); REF tempo is flat")
    # Font diversity
    if abs(oc["distinct_fonts_seen"] - rc["distinct_fonts_seen"]) > 1:
        issues.append(f"Font diversity differs: REF used {rc['distinct_fonts_seen']} "
                      f"distinct fonts, OURS {oc['distinct_fonts_seen']}")
    if not issues:
        lines.append("  No major animation deltas — cycles look comparable.")
    else:
        for i, issue in enumerate(issues, 1):
            lines.append(f"  {i}. {issue}")
    return "\n".join(lines)


# ---------------- visual strip ----------------

def write_strip(reports: list[dict], out_path: Path,
                max_cells: int = 40, cell_w: int = 130, cell_h: int = 160) -> None:
    """Render a side-by-side strip showing each video's BRAZIL window
    as a sequence of (crop, font label) cells."""
    rows = []
    for rep in reports:
        if "_obs" not in rep:
            continue
        obs = rep["_obs"]
        win = rep.get("primary_brazil_window")
        if not win:
            continue
        # Find the obs subset matching the primary BRAZIL window
        in_win = [o for o in obs if win["start_s"] <= o.t <= win["end_s"]
                  and o.crop is not None]
        # Subsample if too many
        if len(in_win) > max_cells:
            step = len(in_win) / max_cells
            in_win = [in_win[int(i * step)] for i in range(max_cells)]
        rows.append((rep["label"], in_win))

    if not rows:
        return

    # Each row: label column (120px) + N cells × cell_w wide
    label_col_w = 120
    max_cells_row = max(len(cells) for _, cells in rows)
    strip_w = label_col_w + max_cells_row * cell_w
    strip_h = len(rows) * cell_h + 40  # header padding
    img = Image.new("RGB", (strip_w, strip_h), (24, 24, 24))
    d = ImageDraw.Draw(img)

    # Header
    try:
        header_font = ImageFont.truetype(
            str(FONTS_DIR / "Inter-Bold.ttf"), 22)
        cell_font = ImageFont.truetype(
            str(FONTS_DIR / "Inter-Regular.ttf"), 13)
    except Exception:
        header_font = ImageFont.load_default()
        cell_font = ImageFont.load_default()
    d.text((10, 10), f"BRAZIL font-cycle timeline — {max_cells_row} cells per row",
           fill=(220, 220, 220), font=header_font)

    for row_i, (label, cells) in enumerate(rows):
        y0 = 40 + row_i * cell_h
        d.text((10, y0 + cell_h // 2 - 8), label, fill=(244, 208, 63),
               font=header_font)
        for ci, fobs in enumerate(cells):
            x0 = label_col_w + ci * cell_w
            crop = fobs.crop
            crop_img = Image.fromarray((crop.astype(np.uint8) * 255), "L")
            crop_norm = _aspect_pad_resize(crop_img, cell_w - 6, cell_h - 50)
            yellow_layer = Image.new("RGB", crop_norm.size, (244, 208, 63))
            black_layer = Image.new("RGB", crop_norm.size, (0, 0, 0))
            yellow_on_black = Image.composite(yellow_layer, black_layer, crop_norm)
            img.paste(yellow_on_black, (x0 + 3, y0 + 3))
            # Font label
            label_text = (fobs.font_name or "?")
            d.text((x0 + 3, y0 + cell_h - 38), label_text,
                   fill=(220, 220, 220), font=cell_font)
            d.text((x0 + 3, y0 + cell_h - 22), f"{fobs.t:.2f}s",
                   fill=(120, 200, 120), font=cell_font)

    img.save(out_path)
    print(f"strip written: {out_path}", file=sys.stderr)


# ---------------- CLI ----------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("videos", nargs="+",
                    help="One or more video paths. Use LABEL=path to label. "
                         "Two with labels triggers diff mode.")
    ap.add_argument("--sample-step", type=float, default=0.033,
                    help="Seconds between sampled frames (default 0.033 = "
                         "30fps per-frame)")
    ap.add_argument("--strip", type=Path,
                    help="Write a visual cycle-strip PNG to this path")
    ap.add_argument("--json", type=Path,
                    help="Write structured JSON report to this path")
    args = ap.parse_args()

    _check_deps()
    print("building font templates ...", file=sys.stderr)
    templates = _build_templates(text="BRAZIL")
    print(f"  {len(templates)} templates ready: "
          f"{[t.name for t in templates]}", file=sys.stderr)

    parsed = []
    for v in args.videos:
        if "=" in v:
            label, path = v.split("=", 1)
        else:
            label = Path(v).stem.upper()
            path = v
        p = Path(path)
        if not p.exists():
            sys.exit(f"error: video not found: {p}")
        parsed.append((label, p))

    reports = [analyze_video(p, args.sample_step, label, templates)
               for label, p in parsed]

    print()
    for r in reports:
        print(render_single_report(r))
        print()
    if len(reports) == 2:
        ref = next((r for r in reports if "REF" in r["label"].upper()),
                   reports[0])
        ours = next((r for r in reports if r is not ref), reports[1])
        print(render_diff_report(ref, ours))
        print()

    if args.strip:
        write_strip(reports, args.strip)

    if args.json:
        slim = []
        for r in reports:
            r_copy = {k: v for k, v in r.items() if k != "_obs"}
            slim.append(r_copy)
        args.json.write_text(json.dumps(slim, indent=2))
        print(f"JSON report written to {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
