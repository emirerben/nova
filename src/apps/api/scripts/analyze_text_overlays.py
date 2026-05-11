#!/usr/bin/env python3
"""Frame-by-frame text-overlay forensics for Dimples Passport renders.

Compares 'Welcome to' (small white serif) and the location title (jumbo
yellow display font, cycling) between two video files — typically the
reference recipe `brazil.mp4` and one of our renders. For each video and
each overlay, measures presence, position, size, and font-cycle behavior
using color masking on numpy arrays (no OCR — too unreliable at small
sizes and on noisy clip backgrounds).

What the tool reports per overlay per video:

  - First appearance frame / timestamp
  - Last appearance frame / timestamp
  - Total visible duration
  - Bounding-box width range (min, median, max, std)
    → width changes ≈ font cycling. Higher std = more cycle variety.
  - Bounding-box centroid position (median x, median y)
    → drift from intended position_y_frac
  - Estimated font cycle interval (seconds between distinct width clusters)
  - Estimated number of distinct font phases

For comparison mode (REF=… OURS=…), each metric is shown side-by-side
with a delta column so timing/size/position drift is obvious.

Usage:
    python analyze_text_overlays.py path/to/video.mp4
    python analyze_text_overlays.py REF=brazil.mp4 OURS=output.mp4
    python analyze_text_overlays.py REF=brazil.mp4 OURS=output.mp4 \\
        --sample-step 0.05 --json /tmp/diff.json

Defaults sample at 0.1s (every 3rd frame at 30fps) — adequate for detecting
font cycles which run at ~0.07s intervals. Use --sample-step 0.033 for
per-frame analysis (slower, more accurate cycle counting).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median, stdev

import numpy as np
from PIL import Image

# --- Color targets (must match seed_dimples_passport_brazil.py) ---
# PERU_COLOR = "#F4D03F"  → yellow (HSV: H≈48°, S≈85%, V≈96%)
# WELCOME_COLOR = "#FFFFFF" → white
YELLOW_RGB = (244, 208, 63)
YELLOW_CH_TOL = 35           # per-channel tolerance, captures cycle-driven hue drift
YELLOW_MIN_PIXELS = 800      # raised: filter out small yellow b-roll content
                             # (signs, jerseys, flags). Text at 170-265px font
                             # has thousands of yellow pixels.

# Title is positioned around PERU_Y_FRAC=0.45 → y≈864 in 1920px frame.
# Constrain detection to a Y-band around there so b-roll yellow content
# (yellow umbrella tops, yellow jerseys in the football clip, yellow signs)
# doesn't pollute the BRAZIL geometry stats.
TITLE_Y_FRAC = 0.45
TITLE_Y_BAND_PX = 250        # ±250px → catches BRAZIL whether at 170 or 265px size
TITLE_MAX_HEIGHT_PX = 400    # actual rendered text never exceeds ~350px; >400 = clip blob
TITLE_CENTROID_X_MIN_FRAC = 0.15  # text is roughly centered; reject left-edge clip blobs
TITLE_CENTROID_X_MAX_FRAC = 0.85

# White-text detection is harder (sky, t-shirts, walls all white).
# Crop tighter than the title because WELCOME_SIZE_PX=48 produces ~28px-tall
# rendered text, far smaller than any natural white blob in the clips.
WELCOME_Y_FRAC = 0.4779
WELCOME_Y_BAND_PX = 60       # ±60px around expected Y (tighter than title)
WHITE_MIN_LUMA = 235         # pixel min in all 3 channels (raised from 230 to
                             # exclude beach-sky off-white that passes 230)
WHITE_MIN_PIXELS = 100
WELCOME_MAX_HEIGHT_PX = 50   # rendered welcome at 48px font ≈ 28-32px tall;
                             # >50 means caught a cloud / shirt / sand glint
WELCOME_MAX_WIDTH_PX = 400   # 'Welcome to' at 48px is ~250px wide max; >400 = noise
WELCOME_MIN_WIDTH_PX = 100   # rendered text ≥ ~150px; <100 = stray pixel cluster
WELCOME_CENTROID_X_MIN_FRAC = 0.35  # text is centered horizontally; reject off-center blobs
WELCOME_CENTROID_X_MAX_FRAC = 0.65

FRAME_HEIGHT = 1920
FRAME_WIDTH = 1080


def _check_dependencies() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("error: ffmpeg not found on PATH")


def _video_duration_s(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
    )
    return float(out.strip())


def _extract_frames(video_path: Path, sample_step_s: float, output_dir: Path) -> list[Path]:
    """Extract frames at sample_step_s intervals into output_dir."""
    fps = 1.0 / sample_step_s
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video_path), "-vf", f"fps={fps}",
        "-y", str(output_dir / "frame_%04d.png"),
    ]
    subprocess.run(cmd, check=True)
    return sorted(output_dir.glob("frame_*.png"))


def _analyze_yellow(frame: np.ndarray) -> dict | None:
    """Return geometry of yellow title text, or None if not the title.

    Detection is constrained to a Y-band around the expected title position
    (TITLE_Y_FRAC). Yellow content in b-roll (signs, jerseys, umbrella
    fabric) outside this band is ignored. Within the band, the region must
    pass aspect/size/centroid checks consistent with a text overlay.
    """
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
    pixel_count = int(mask.sum())
    if pixel_count < YELLOW_MIN_PIXELS:
        return None
    ys, xs = np.where(mask)
    bbox_h = int(ys.max() - ys.min())
    centroid_x = float(xs.mean())
    if bbox_h > TITLE_MAX_HEIGHT_PX:
        return None  # one big yellow blob — not text
    if centroid_x < TITLE_CENTROID_X_MIN_FRAC * FRAME_WIDTH:
        return None
    if centroid_x > TITLE_CENTROID_X_MAX_FRAC * FRAME_WIDTH:
        return None
    return {
        "pixel_count": pixel_count,
        "bbox_x_min": int(xs.min()),
        "bbox_x_max": int(xs.max()),
        "bbox_y_min": int(ys.min() + y_lo),
        "bbox_y_max": int(ys.max() + y_lo),
        "width_px": int(xs.max() - xs.min()),
        "height_px": bbox_h,
        "centroid_x": centroid_x,
        "centroid_y": float(ys.mean() + y_lo),
        "frame_width_pct": float((xs.max() - xs.min()) / FRAME_WIDTH * 100),
        "clipped_right_edge": bool(xs.max() >= FRAME_WIDTH - 5),
        "clipped_left_edge": bool(xs.min() <= 5),
    }


def _analyze_welcome(frame: np.ndarray) -> dict | None:
    """Detect small white 'Welcome to' text near WELCOME_Y_FRAC.

    Crops a Y-band around the expected position, masks high-luma pixels,
    rejects clusters that don't match the welcome's geometry (too tall,
    too short, too narrow).
    """
    expected_y = int(WELCOME_Y_FRAC * FRAME_HEIGHT)
    y_lo = max(0, expected_y - WELCOME_Y_BAND_PX)
    y_hi = min(FRAME_HEIGHT, expected_y + WELCOME_Y_BAND_PX)
    band = frame[y_lo:y_hi]
    r, g, b = band[..., 0], band[..., 1], band[..., 2]
    # White: all 3 channels high AND roughly balanced (rules out yellow-bright pixels)
    mask = (r >= WHITE_MIN_LUMA) & (g >= WHITE_MIN_LUMA) & (b >= WHITE_MIN_LUMA)
    # Reject yellow pixels in the strip (BRAZIL letters might pass the LUMA check
    # in some renders) — yellow has R≈G high, B low; white has all three high.
    mask = mask & (b >= 200)
    pixel_count = int(mask.sum())
    if pixel_count < WHITE_MIN_PIXELS:
        return None
    ys, xs = np.where(mask)
    bbox_w = int(xs.max() - xs.min())
    bbox_h = int(ys.max() - ys.min())
    centroid_x = float(xs.mean())
    # Geometry: rendered 'Welcome to' at 48px font is ~250px wide, ~28px tall.
    # Reject anything outside that envelope — those are clouds, t-shirts, glints.
    if bbox_h > WELCOME_MAX_HEIGHT_PX:
        return None
    if bbox_w < WELCOME_MIN_WIDTH_PX or bbox_w > WELCOME_MAX_WIDTH_PX:
        return None
    if centroid_x < WELCOME_CENTROID_X_MIN_FRAC * FRAME_WIDTH:
        return None
    if centroid_x > WELCOME_CENTROID_X_MAX_FRAC * FRAME_WIDTH:
        return None
    return {
        "pixel_count": pixel_count,
        "bbox_x_min": int(xs.min()),
        "bbox_x_max": int(xs.max()),
        "bbox_y_min_in_frame": int(ys.min() + y_lo),
        "bbox_y_max_in_frame": int(ys.max() + y_lo),
        "width_px": bbox_w,
        "height_px": bbox_h,
        "centroid_x": centroid_x,
        "centroid_y_in_frame": float(ys.mean() + y_lo),
    }


def analyze_video(video_path: Path, sample_step_s: float, label: str) -> dict:
    """Run the full analysis pipeline on one video; return a structured report."""
    print(f"\n[{label}] analyzing {video_path.name} ...", file=sys.stderr)
    duration = _video_duration_s(video_path)
    with tempfile.TemporaryDirectory(prefix=f"text_analyze_{label}_") as td:
        td_path = Path(td)
        frame_paths = _extract_frames(video_path, sample_step_s, td_path)
        n_frames = len(frame_paths)
        print(f"[{label}] extracted {n_frames} frames at {sample_step_s}s step", file=sys.stderr)

        yellow_obs: list[tuple[float, dict]] = []
        welcome_obs: list[tuple[float, dict]] = []

        for i, fp in enumerate(frame_paths):
            ts = i * sample_step_s
            frame = np.array(Image.open(fp).convert("RGB"))
            y = _analyze_yellow(frame)
            if y is not None:
                yellow_obs.append((ts, y))
            w = _analyze_welcome(frame)
            if w is not None:
                welcome_obs.append((ts, w))

    return {
        "label": label,
        "video": str(video_path),
        "duration_s": duration,
        "sample_step_s": sample_step_s,
        "frames_analyzed": n_frames,
        "yellow_title": _summarize_overlay(yellow_obs, sample_step_s, kind="yellow"),
        "welcome_subtitle": _summarize_overlay(welcome_obs, sample_step_s, kind="welcome"),
    }


def _summarize_overlay(obs: list[tuple[float, dict]], sample_step_s: float, kind: str) -> dict:
    if not obs:
        return {"present": False}

    timestamps = [t for t, _ in obs]
    widths = [o["width_px"] for _, o in obs]
    heights = [o["height_px"] for _, o in obs]
    centroid_xs = [o["centroid_x"] for _, o in obs]
    centroid_ys = [o.get("centroid_y", o.get("centroid_y_in_frame")) for _, o in obs]

    summary = {
        "present": True,
        "first_appearance_s": min(timestamps),
        "last_appearance_s": max(timestamps),
        "visible_duration_s": max(timestamps) - min(timestamps) + sample_step_s,
        "frames_present": len(obs),
        "width_px": {
            "min": min(widths),
            "max": max(widths),
            "median": median(widths),
            "stdev": stdev(widths) if len(widths) > 1 else 0.0,
            "range": max(widths) - min(widths),
        },
        "height_px": {
            "min": min(heights),
            "max": max(heights),
            "median": median(heights),
        },
        "centroid_x_median": median(centroid_xs),
        "centroid_y_median": median(centroid_ys),
        "centroid_x_frac": median(centroid_xs) / FRAME_WIDTH,
        "centroid_y_frac": median(centroid_ys) / FRAME_HEIGHT,
        "frame_width_pct_range": [
            min(o["width_px"] for _, o in obs) / FRAME_WIDTH * 100,
            max(o["width_px"] for _, o in obs) / FRAME_WIDTH * 100,
        ],
    }

    if kind == "yellow":
        clipped_frames = [
            t for t, o in obs
            if o.get("clipped_right_edge") or o.get("clipped_left_edge")
        ]
        summary["clipped_off_frame"] = {
            "frames": len(clipped_frames),
            "pct_of_visible_frames": len(clipped_frames) / len(obs) * 100,
            "first_clip_at_s": clipped_frames[0] if clipped_frames else None,
        }

    # Font cycle estimation: bucket widths into ~20px clusters,
    # count distinct buckets visited = # distinct fonts in the cycle.
    if summary["width_px"]["range"] > 30:
        bucket_size = 20
        buckets = set(int(w // bucket_size) for w in widths)
        summary["font_cycle"] = {
            "estimated_distinct_phases": len(buckets),
            "width_buckets_visited": sorted(buckets),
            "tempo_evidence": _estimate_cycle_tempo(obs, sample_step_s),
        }
    else:
        summary["font_cycle"] = {
            "estimated_distinct_phases": 1,
            "note": "width range <30px — overlay appears static (no font cycle)",
        }

    return summary


def _estimate_cycle_tempo(obs: list[tuple[float, dict]], sample_step_s: float) -> dict:
    """Walk the width series and count transitions between width buckets.

    A 'transition' happens when consecutive frames differ in width bucket.
    Total transitions / visible duration ≈ cycle frequency.
    """
    if len(obs) < 2:
        return {"note": "insufficient frames for tempo estimate"}
    bucket_size = 20
    buckets = [int(o["width_px"] // bucket_size) for _, o in obs]
    transitions = sum(1 for a, b in zip(buckets, buckets[1:]) if a != b)
    visible_s = obs[-1][0] - obs[0][0]
    if visible_s <= 0:
        return {"note": "visible duration is zero"}
    return {
        "transitions": transitions,
        "transitions_per_second": transitions / visible_s,
        "implied_cycle_interval_s": visible_s / transitions if transitions > 0 else None,
    }


def _fmt_num(v, decimals=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{decimals}f}"
    return str(v)


def render_single_report(report: dict) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append(f"TEXT-OVERLAY ANALYSIS — {report['label']}")
    lines.append("=" * 78)
    lines.append(f"video:    {report['video']}")
    lines.append(f"duration: {report['duration_s']:.2f}s")
    lines.append(f"sampled:  {report['frames_analyzed']} frames at "
                 f"{report['sample_step_s']}s step")
    for overlay_name, label_name in [
        ("yellow_title", "YELLOW TITLE (BRAZIL/PERU)"),
        ("welcome_subtitle", "WHITE 'Welcome to' SUBTITLE"),
    ]:
        ov = report[overlay_name]
        lines.append("")
        lines.append(f"--- {label_name} ---")
        if not ov.get("present"):
            lines.append("  NOT DETECTED in any sampled frame")
            continue
        lines.append(f"  first appearance:   {_fmt_num(ov['first_appearance_s'])}s")
        lines.append(f"  last appearance:    {_fmt_num(ov['last_appearance_s'])}s")
        lines.append(f"  visible duration:   {_fmt_num(ov['visible_duration_s'])}s "
                     f"({ov['frames_present']} sampled frames)")
        lines.append(f"  width range:        {ov['width_px']['min']}–"
                     f"{ov['width_px']['max']}px "
                     f"(median {ov['width_px']['median']}px, "
                     f"stdev {_fmt_num(ov['width_px']['stdev'], 1)})")
        lines.append(f"  frame-width %:      {_fmt_num(ov['frame_width_pct_range'][0], 1)}%–"
                     f"{_fmt_num(ov['frame_width_pct_range'][1], 1)}%")
        lines.append(f"  height range:       {ov['height_px']['min']}–"
                     f"{ov['height_px']['max']}px (median {ov['height_px']['median']})")
        lines.append(f"  centroid (median):  x={_fmt_num(ov['centroid_x_median'], 0)}px "
                     f"(x_frac={_fmt_num(ov['centroid_x_frac'], 3)}), "
                     f"y={_fmt_num(ov['centroid_y_median'], 0)}px "
                     f"(y_frac={_fmt_num(ov['centroid_y_frac'], 3)})")
        if overlay_name == "yellow_title":
            clip = ov.get("clipped_off_frame") or {}
            lines.append(f"  off-frame clipping: {clip.get('frames', 0)} frames "
                         f"({_fmt_num(clip.get('pct_of_visible_frames', 0), 1)}% of visible)"
                         + (f"; first clip at {_fmt_num(clip.get('first_clip_at_s'))}s"
                            if clip.get('first_clip_at_s') is not None else ""))
        cycle = ov.get("font_cycle") or {}
        lines.append(f"  font cycle phases:  {cycle.get('estimated_distinct_phases', '—')}"
                     + (f" (buckets visited: {cycle.get('width_buckets_visited')})"
                        if 'width_buckets_visited' in cycle else f" — {cycle.get('note', '')}"))
        tempo = (cycle.get("tempo_evidence") or {})
        if "transitions" in tempo:
            implied = _fmt_num(tempo.get("implied_cycle_interval_s"), 3)
            lines.append(
                f"  cycle tempo:        {tempo['transitions']} width-bucket "
                f"transitions over visible window "
                f"= {_fmt_num(tempo['transitions_per_second'], 2)} per sec "
                f"(implied interval {implied}s)"
            )
    return "\n".join(lines)


def render_diff_report(ref: dict, ours: dict) -> str:
    """Side-by-side diff between two reports — REF vs OURS."""
    lines = []
    lines.append("=" * 96)
    lines.append("DIFFERENCE REPORT — REF (brazil.mp4) vs OURS")
    lines.append("=" * 96)
    lines.append(f"ref:  {ref['video']}  ({ref['duration_s']:.2f}s, "
                 f"{ref['frames_analyzed']} sampled)")
    lines.append(f"ours: {ours['video']}  ({ours['duration_s']:.2f}s, "
                 f"{ours['frames_analyzed']} sampled)")

    for overlay_name, label_name in [
        ("yellow_title", "YELLOW TITLE (BRAZIL/PERU)"),
        ("welcome_subtitle", "WHITE 'Welcome to' SUBTITLE"),
    ]:
        r = ref[overlay_name]
        o = ours[overlay_name]
        lines.append("")
        lines.append(f"--- {label_name} ---")
        if not r.get("present") and not o.get("present"):
            lines.append("  NOT DETECTED in either video")
            continue
        if not r.get("present"):
            lines.append("  NOT DETECTED in ref")
            continue
        if not o.get("present"):
            lines.append("  NOT DETECTED in ours")
            continue
        lines.append(f"  {'metric':28s} {'REF':>14s} {'OURS':>14s} {'DELTA':>14s}  notes")
        lines.append(f"  {'-'*28} {'-'*14} {'-'*14} {'-'*14}  {'-'*40}")

        def row(metric, ref_v, ours_v, delta_fmt=None, notes=""):
            d = ""
            if isinstance(ref_v, (int, float)) and isinstance(ours_v, (int, float)):
                delta = ours_v - ref_v
                if delta_fmt is None:
                    d = f"{delta:+.2f}"
                else:
                    d = delta_fmt(delta)
            lines.append(f"  {metric:28s} {_fmt_num(ref_v):>14s} {_fmt_num(ours_v):>14s} "
                         f"{d:>14s}  {notes}")

        row("first appearance (s)", r["first_appearance_s"], o["first_appearance_s"],
            notes="negative = ours appears earlier" )
        row("last appearance (s)", r["last_appearance_s"], o["last_appearance_s"],
            notes="negative = ours disappears earlier")
        row("visible duration (s)", r["visible_duration_s"], o["visible_duration_s"],
            notes="negative = ours stays visible for less time")
        row("width median (px)", r["width_px"]["median"], o["width_px"]["median"],
            notes="positive = ours wider (closer to clipping)")
        row("width range (px)", r["width_px"]["range"], o["width_px"]["range"],
            notes="larger = more font variety in cycle")
        row("width % of frame (med)", r["width_px"]["median"] / FRAME_WIDTH * 100,
            o["width_px"]["median"] / FRAME_WIDTH * 100,
            notes="100% means filling whole frame — likely clipped")
        row("height median (px)", r["height_px"]["median"], o["height_px"]["median"])
        row("centroid x_frac", r["centroid_x_frac"], o["centroid_x_frac"])
        row("centroid y_frac", r["centroid_y_frac"], o["centroid_y_frac"],
            notes="lower = text higher in frame")
        if overlay_name == "yellow_title":
            row("off-frame-clip frames",
                (r.get("clipped_off_frame") or {}).get("frames", 0),
                (o.get("clipped_off_frame") or {}).get("frames", 0),
                notes="should be 0 — any value > 0 = text overflows frame")
        rcycle = (r.get("font_cycle") or {})
        ocycle = (o.get("font_cycle") or {})
        row("font cycle phases",
            rcycle.get("estimated_distinct_phases", 0),
            ocycle.get("estimated_distinct_phases", 0))
        rt = (rcycle.get("tempo_evidence") or {})
        ot = (ocycle.get("tempo_evidence") or {})
        if "transitions_per_second" in rt and "transitions_per_second" in ot:
            row("cycle transitions/sec",
                rt["transitions_per_second"], ot["transitions_per_second"],
                notes="similar values = matching tempo")

    lines.append("")
    lines.append("VERDICT:")
    issues = []
    # Yellow title checks
    rt = ref["yellow_title"]
    ot = ours["yellow_title"]
    if rt.get("present") and ot.get("present"):
        ref_med_w = rt["width_px"]["median"]
        our_med_w = ot["width_px"]["median"]
        if our_med_w > ref_med_w * 1.3:
            ratio = our_med_w / ref_med_w
            issues.append(f"BRAZIL too wide: ours median {our_med_w}px vs ref {ref_med_w}px "
                          f"(ratio {ratio:.2f}× — clipping likely)")
        if (ot.get("clipped_off_frame") or {}).get("frames", 0) > 0:
            issues.append(f"BRAZIL clips off frame in {ot['clipped_off_frame']['frames']} "
                          f"of {ot['frames_present']} visible frames")
        if abs(rt["first_appearance_s"] - ot["first_appearance_s"]) > 0.5:
            d = ot["first_appearance_s"] - rt["first_appearance_s"]
            issues.append(f"BRAZIL appears {d:+.2f}s vs ref (negative = too early)")
    # Welcome checks
    rw = ref["welcome_subtitle"]
    ow = ours["welcome_subtitle"]
    if rw.get("present") and ow.get("present"):
        if ow["visible_duration_s"] < rw["visible_duration_s"] * 0.7:
            issues.append(f"Welcome to visible for {ow['visible_duration_s']:.2f}s "
                          f"vs ref {rw['visible_duration_s']:.2f}s "
                          f"(ours fades early)")
    if not issues:
        lines.append("  No major deltas — overlays match reference within tolerance.")
    else:
        for i, issue in enumerate(issues, 1):
            lines.append(f"  {i}. {issue}")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("videos", nargs="+",
                    help="One or more video paths. Use LABEL=path to label "
                         "(e.g. REF=brazil.mp4 OURS=output.mp4). Two videos "
                         "with labels triggers diff mode.")
    ap.add_argument("--sample-step", type=float, default=0.1,
                    help="Seconds between sampled frames (default 0.1)")
    ap.add_argument("--json", type=Path,
                    help="Write structured JSON report to this path")
    args = ap.parse_args()

    _check_dependencies()

    parsed: list[tuple[str, Path]] = []
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

    reports = [analyze_video(p, args.sample_step, label) for label, p in parsed]

    # Console output: single report or diff
    print()
    for r in reports:
        print(render_single_report(r))
        print()
    if len(reports) == 2:
        # Heuristic: first one with REF in label wins; otherwise first arg = ref
        ref = next((r for r in reports if "REF" in r["label"].upper()), reports[0])
        ours = next((r for r in reports if r is not ref), reports[1])
        print(render_diff_report(ref, ours))
        print()

    if args.json:
        args.json.write_text(json.dumps(reports, indent=2))
        print(f"JSON report written to {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
