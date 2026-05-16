#!/usr/bin/env python3
"""Waka Waka text-overlay diff analyzer.

Compares a Nova-pipeline output video against the original recipe video it
was meant to match. Focuses on the first 4 seconds and the three known
intro overlays: "This" / "is" / country-name. Detects per-overlay text
identity, position, size, color, animation, and audio-cue alignment.

The tool is offline, deterministic, file-in → JSON-out. It does NOT
modify the pipeline.

Usage:
    python -m scripts.analyze_waka_waka_diff \\
        REF=/path/to/recipe.mp4 \\
        OURS=/path/to/output.mp4 \\
        --json /tmp/diff.json [--window 4.0] [--sample-step 0.05]

Setup (one-time):
    brew install tesseract        # macOS
    pip install pytesseract       # any platform

Without tesseract installed, the analyzer still runs but reports text as
"<unknown>" with text_source="color-region-fallback" — text content
mismatches will still be detected via region+color but not by string.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

# Make this script runnable either as `python -m scripts.analyze_waka_waka_diff` or directly.
if __package__ in (None, ""):
    _SCRIPT_DIR = Path(__file__).resolve().parent
    if str(_SCRIPT_DIR.parent) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR.parent))

from scripts.overlay_forensics import diff as diff_mod  # noqa: E402
from scripts.overlay_forensics import events as events_mod  # noqa: E402
from scripts.overlay_forensics import frames as frames_mod  # noqa: E402
from scripts.overlay_forensics import masking as masking_mod  # noqa: E402
from scripts.overlay_forensics import ocr as ocr_mod  # noqa: E402
from scripts.overlay_forensics import safe_crop as safe_crop_mod  # noqa: E402

# Waka Waka palette — per src/apps/api/scripts/add_waka_waka_intro_overlays.py.
# WHITE handles "This" and "is"; MAIZE handles "AFRICA" / location names.
# Min-pixel thresholds are expressed as a FRACTION of frame area so the same
# config works for the 1024x576 recipe and the 1080x1920 output. Real text
# overlays at 60-250px font size cover 0.05-2% of the frame; pure-color noise
# in b-roll (sand glints, sky pixels) is much smaller in relative terms.
WHITE_MIN_PIXELS_FRAC = 0.00015   # ~88 px at 1024x576, ~311 px at 1080x1920
MAIZE_MIN_PIXELS_FRAC = 0.00030   # ~177 px / ~622 px
WHITE_MIN_LUMA = 220

# Filter spurious tiny detections post-clustering: real text events have
# an on-screen median height of at least 1.5% of the frame height.
EVENT_MIN_HEIGHT_FRAC = 0.018

COLOR_TARGETS: dict[str, dict] = {
    "white": {
        "kind": "white",
        "min_luma": WHITE_MIN_LUMA,
        "min_pixels_frac": WHITE_MIN_PIXELS_FRAC,
    },
    "maize": {
        "kind": "rgb",
        "rgb": (244, 208, 63),
        "per_channel_tol": 35,
        "min_pixels_frac": MAIZE_MIN_PIXELS_FRAC,
    },
}

# Expected vocabulary, for fuzzy fallback when OCR returns garbage.
EXPECTED_TEXTS_BY_COLOR: dict[str, list[str]] = {
    "white": ["This", "is"],
    # AFRICA may be substituted by location at slot-2 — accept either.
    "maize": ["Africa", "AFRICA", "Morocco", "MOROCCO"],
}

DEFAULT_WINDOW_S = 4.0
DEFAULT_STEP_S = 0.05


# ---------------------------------------------------------------------------
# Audio beat detection (inlined from template_orchestrate._detect_audio_beats
# to avoid importing Celery + DB deps).
# ---------------------------------------------------------------------------


def detect_audio_beats(audio_path: Path, threshold_db: float = -35.0) -> list[float]:
    """Parse silence_end timestamps from FFmpeg silencedetect — each marks an
    energy onset. Non-fatal: returns [] on any error.
    """
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d=0.1",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            return []
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        return sorted(
            float(m.group(1))
            for m in re.finditer(r"silence_end:\s*([\d.]+)", stderr_text)
        )
    except Exception:
        return []


def nearest_beat_offset(event_t: float, beats: list[float]) -> float | None:
    """Signed offset from `event_t` to the nearest beat; None if no beats."""
    if not beats:
        return None
    nearest = min(beats, key=lambda b: abs(b - event_t))
    return event_t - nearest


# ---------------------------------------------------------------------------
# Per-video pipeline.
# ---------------------------------------------------------------------------


@dataclass
class VideoAnalysis:
    label: str
    path: Path
    width: int
    height: int
    fps: float
    duration_s: float
    aspect_ratio: float
    events: list[events_mod.TextEvent]
    beats_in_window: list[float]
    audio_status: Literal["ok", "no-audio-stream", "extraction-error"]

    def aspect_label(self) -> str:
        if abs(self.aspect_ratio - 16 / 9) < 0.05:
            return "16:9"
        if abs(self.aspect_ratio - 9 / 16) < 0.05:
            return "9:16"
        if abs(self.aspect_ratio - 4 / 3) < 0.05:
            return "4:3"
        return f"{self.aspect_ratio:.3f}"


def _detect_color_observations_per_frame(
    frame_t: float,
    frame: np.ndarray,
    frame_w: int,
    frame_h: int,
) -> list[events_mod.FrameObservation]:
    out: list[events_mod.FrameObservation] = []
    frame_area = frame_w * frame_h
    for color_key, cfg in COLOR_TARGETS.items():
        if cfg["kind"] == "white":
            mask = masking_mod.mask_white(frame, min_luma=cfg["min_luma"])
        else:
            mask = masking_mod.mask_by_color(
                frame, cfg["rgb"], per_channel_tol=cfg["per_channel_tol"]
            )
        min_pixels = max(40, int(cfg["min_pixels_frac"] * frame_area))
        pixel_count = int(mask.sum())
        if pixel_count < min_pixels:
            continue
        bbox = masking_mod.largest_blob_bbox(mask, min_pixels=min_pixels)
        if bbox is None:
            continue
        # Compute mean brightness over the masked region — used by animation
        # classifier to spot fade-in.
        text_pixels = frame[
            bbox.y_min:bbox.y_max + 1, bbox.x_min:bbox.x_max + 1
        ].mean() if (bbox.height > 0 and bbox.width > 0) else 0.0
        out.append(events_mod.FrameObservation(
            t_s=frame_t,
            color_key=color_key,
            bbox=bbox,
            mask_pixel_count=pixel_count,
            mean_brightness=float(text_pixels) / 255.0,
            frame_array=frame,
            mask=mask,
        ))
    return out


def _resolve_event_text(
    ev: events_mod.TextEvent,
    *,
    ocr_available: bool,
) -> None:
    """Populate ev.text + ev.ocr_confidence + ev.text_source in place."""
    peak = ev.peak_observation()
    if not ocr_available or peak.frame_array is None or peak.mask is None:
        ev.text = _fallback_text_for_color(ev.color_key)
        ev.text_source = "color-region-fallback"
        ev.ocr_confidence = None
        return
    result = ocr_mod.ocr_mask_crop(peak.frame_array, peak.mask, psm=8)
    expected = EXPECTED_TEXTS_BY_COLOR.get(ev.color_key, [])
    # Snap to an expected word if OCR is close to it — small distance saves
    # OCR confusions like "Ths" → "This" or "Africa" with stylized glyphs.
    snapped = _snap_to_expected(result.text, expected)
    if snapped is not None and result.confidence >= 0.3:
        ev.text = snapped
        ev.text_source = "ocr"
        ev.ocr_confidence = result.confidence
        return
    if result.confidence >= 0.5 and result.text:
        ev.text = result.text
        ev.text_source = "ocr"
        ev.ocr_confidence = result.confidence
        return
    # OCR failed or was low-confidence — fall back to expected vocabulary.
    ev.text = _fallback_text_for_color(ev.color_key)
    ev.text_source = "color-region-fallback"
    ev.ocr_confidence = result.confidence


def _fallback_text_for_color(color_key: str) -> str:
    pool = EXPECTED_TEXTS_BY_COLOR.get(color_key, [])
    return pool[0] if pool else f"<{color_key}>"


def _snap_to_expected(ocr_text: str, expected: list[str]) -> str | None:
    if not ocr_text or not expected:
        return None
    norm = re.sub(r"[^A-Za-z]", "", ocr_text)
    if not norm:
        return None
    best = None
    best_dist = 99
    for cand in expected:
        cand_norm = re.sub(r"[^A-Za-z]", "", cand)
        dist = diff_mod.levenshtein(norm, cand_norm)
        if dist < best_dist:
            best_dist = dist
            best = cand
    if best is None:
        return None
    # Allow snap if distance ≤ 30% of candidate length, min 1.
    if best_dist <= max(1, int(len(best) * 0.4)):
        return best
    return None


def analyze_video(
    label: str,
    path: Path,
    window_s: float,
    step_s: float,
    ocr_available: bool,
) -> VideoAnalysis:
    print(f"[{label}] probing {path.name}...", file=sys.stderr)
    info = frames_mod.probe_video(path)
    width = info["width"]
    height = info["height"]

    print(
        f"[{label}] sampling {window_s:.2f}s at {step_s:.3f}s step "
        f"= ~{int(window_s / step_s)} frames",
        file=sys.stderr,
    )
    observations: list[events_mod.FrameObservation] = []
    for ts, frame in frames_mod.sample_frames(path, 0.0, window_s, step_s):
        observations.extend(_detect_color_observations_per_frame(ts, frame, width, height))

    # region_grid=2 keeps "This" (left) and "is" (right) distinguishable in
    # the recipe (which has them at x_frac ~0.25 and ~0.70 — different halves)
    # but is coarse enough that a slide-up animation in the OUTPUT does NOT
    # fragment into multiple sub-events as the y centroid sweeps the frame.
    # gap_tolerance_steps=4 (=0.20s @ 0.05s step) absorbs font-cycle flickers
    # where a frame transiently dips below min_pixels.
    events = events_mod.cluster_observations_to_events(
        observations, width, height,
        step_s=step_s,
        gap_tolerance_steps=4,
        min_duration_s=0.10,
        region_grid=2,
    )

    # Drop tiny stray detections — real text covers > EVENT_MIN_HEIGHT_FRAC.
    events = [
        ev for ev in events
        if ev.median_relative_bbox(width, height).h_frac >= EVENT_MIN_HEIGHT_FRAC
    ]

    # Annotate each event with text + animation entrance.
    for ev in events:
        _resolve_event_text(ev, ocr_available=ocr_available)
        ent = events_mod.classify_entrance(ev, width, height)
        ev._entrance = ent  # type: ignore[attr-defined]

    # Audio beats in window.
    with tempfile.TemporaryDirectory(prefix="wakawaka_audio_") as td:
        audio_path = Path(td) / "audio.m4a"
        try:
            has_audio = frames_mod.extract_audio(path, audio_path)
        except frames_mod.VideoReadError:
            has_audio = False
            audio_status: Literal["ok", "no-audio-stream", "extraction-error"] = "extraction-error"
        else:
            audio_status = "ok" if has_audio else "no-audio-stream"
        beats: list[float] = []
        if has_audio and audio_path.exists() and audio_path.stat().st_size > 0:
            all_beats = detect_audio_beats(audio_path)
            beats = [b for b in all_beats if 0.0 <= b <= window_s]

    return VideoAnalysis(
        label=label,
        path=path,
        width=width,
        height=height,
        fps=info["fps"],
        duration_s=info["duration_s"],
        aspect_ratio=width / max(height, 1),
        events=events,
        beats_in_window=beats,
        audio_status=audio_status,
    )


# ---------------------------------------------------------------------------
# Diff + report
# ---------------------------------------------------------------------------


def _check_cooccurrence(
    events: list[events_mod.TextEvent],
    required_texts: list[str],
    trigger_text: str,
) -> bool | None:
    """Returns True if at the moment `trigger_text` first appears, every text
    in `required_texts` is also visible. Returns None if the trigger event
    cannot be located.
    """
    trigger = next(
        (e for e in events if (e.text or "").lower() == trigger_text.lower()),
        None,
    )
    if trigger is None:
        return None
    t = trigger.t_start
    visible = {
        (e.text or "").lower() for e in events
        if e.t_start <= t <= e.t_end and e is not trigger
    }
    return all(rt.lower() in visible for rt in required_texts)


def build_report(
    recipe: VideoAnalysis,
    output: VideoAnalysis,
    window_s: float,
    step_s: float,
) -> dict:
    findings: list[diff_mod.DiffFinding] = []

    pairs = diff_mod.pair_events(recipe.events, output.events)
    for pair in pairs:
        findings.extend(diff_mod.diff_paired_event(
            pair, recipe.width, recipe.height, output.width, output.height,
        ))

    # Co-occurrence rule: in recipe, when "Africa" (or location) starts, both
    # "This" and "is" must still be visible. Same check on output. Flag if
    # recipe satisfies but output does not.
    recipe_cooc = _check_cooccurrence(recipe.events, ["This", "is"], "Africa")
    if recipe_cooc is None:
        # Try the actual location word that may have appeared in the recipe.
        for trig in ["AFRICA", "Morocco", "MOROCCO"]:
            recipe_cooc = _check_cooccurrence(recipe.events, ["This", "is"], trig)
            if recipe_cooc is not None:
                break
    output_cooc = _check_cooccurrence(output.events, ["This", "is"], "Africa")
    if output_cooc is None:
        for trig in ["AFRICA", "Morocco", "MOROCCO"]:
            output_cooc = _check_cooccurrence(output.events, ["This", "is"], trig)
            if output_cooc is not None:
                break
    cooc_finding = diff_mod.diff_cooccurrence_rule(
        rule_name="africa_starts_while_this_and_is_visible",
        recipe_satisfied=recipe_cooc,
        output_satisfied=output_cooc,
        description=(
            "In the recipe, when the subject (Africa/location) word appears, "
            "both 'This' and 'is' are still on-screen. In the output this "
            "co-occurrence is not satisfied — the intro sequence is broken."
        ),
    )
    if cooc_finding is not None:
        findings.append(cooc_finding)

    # Beat alignment for "is" (the audio-cued word per the user's spec).
    r_is = next((e for e in recipe.events if (e.text or "").lower() == "is"), None)
    o_is = next((e for e in output.events if (e.text or "").lower() == "is"), None)
    if r_is is not None and o_is is not None:
        r_offset = nearest_beat_offset(r_is.t_start, recipe.beats_in_window)
        o_offset = nearest_beat_offset(o_is.t_start, output.beats_in_window)
        beat_finding = diff_mod.diff_beat_alignment("is", r_offset, o_offset)
        if beat_finding is not None:
            findings.append(beat_finding)

    # Safe-crop projection per recipe event (only meaningful for 16:9 → 9:16).
    safe_crop_summaries: dict[str, dict] = {}
    if recipe.aspect_ratio > 9 / 16 + 0.05 and output.aspect_ratio < 1.0:
        for ev in recipe.events:
            label = (ev.text or ev.color_key).strip()
            proj = safe_crop_mod.project_to_9x16(
                ev.median_bbox(), recipe.width, recipe.height,
            )
            safe_crop_summaries[label] = {
                "survives": proj.survives,
                "projected_x_frac": (
                    None if proj.projected_x_frac is None else round(proj.projected_x_frac, 3)
                ),
                "projected_cy_frac": (
                    None if proj.projected_cy_frac is None else round(proj.projected_cy_frac, 3)
                ),
                "note": proj.note,
            }
            sc_finding = diff_mod.diff_safe_crop(label, proj.note, proj.survives)
            if sc_finding is not None:
                findings.append(sc_finding)

    findings = diff_mod.sort_findings(findings)
    summary_lines = diff_mod.plain_english_summary(findings, top_n=5)

    return {
        "videos": {
            "recipe": _video_meta(recipe),
            "output": _video_meta(output),
        },
        "window_s": window_s,
        "sample_step_s": step_s,
        "events": {
            "recipe": [_event_json(ev, recipe.width, recipe.height) for ev in recipe.events],
            "output": [_event_json(ev, output.width, output.height) for ev in output.events],
        },
        "audio": {
            "recipe": {
                "status": recipe.audio_status,
                "beats_in_window": [round(b, 3) for b in recipe.beats_in_window],
            },
            "output": {
                "status": output.audio_status,
                "beats_in_window": [round(b, 3) for b in output.beats_in_window],
            },
        },
        "cooccurrence": {
            "africa_starts_while_this_and_is_visible": {
                "recipe": recipe_cooc,
                "output": output_cooc,
            },
        },
        "safe_crop_9x16": safe_crop_summaries,
        "diff": [f.to_dict() for f in findings],
        "summary": summary_lines,
    }


def _video_meta(va: VideoAnalysis) -> dict:
    return {
        "label": va.label,
        "path": str(va.path),
        "width": va.width,
        "height": va.height,
        "aspect": va.aspect_label(),
        "fps": round(va.fps, 2),
        "duration_s": round(va.duration_s, 3),
    }


def _event_json(ev: events_mod.TextEvent, frame_w: int, frame_h: int) -> dict:
    rel = ev.median_relative_bbox(frame_w, frame_h)
    bbox = ev.median_bbox()
    entrance = getattr(ev, "_entrance", ("unknown", 0.0))
    return {
        "text": ev.text,
        "text_source": ev.text_source,
        "ocr_confidence": (
            None if ev.ocr_confidence is None else round(ev.ocr_confidence, 3)
        ),
        "color_key": ev.color_key,
        "t_start": round(ev.t_start, 3),
        "t_end": round(ev.t_end, 3),
        "duration_s": round(ev.duration_s, 3),
        "frames_present": len(ev.observations),
        "position": {
            "cx_frac": round(rel.cx_frac, 3),
            "cy_frac": round(rel.cy_frac, 3),
            "x_frac": round(rel.x_frac, 3),
            "y_frac": round(rel.y_frac, 3),
        },
        "size": {
            "h_frac": round(rel.h_frac, 3),
            "w_frac": round(rel.w_frac, 3),
            "h_px": bbox.height,
            "w_px": bbox.width,
        },
        "animation": {
            "entrance": entrance[0],
            "entrance_duration_s": round(entrance[1], 3),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_labeled_video(arg: str) -> tuple[str, Path]:
    if "=" in arg:
        label, path = arg.split("=", 1)
    else:
        label = Path(arg).stem.upper()
        path = arg
    p = Path(path)
    if not p.exists():
        sys.exit(f"error: video not found: {p}")
    return label.strip(), p


def _print_stdout_report(report: dict) -> None:
    summary = report.get("summary") or []
    diff = report.get("diff") or []
    crit = [f for f in diff if f["severity"] == "critical"]
    major = [f for f in diff if f["severity"] == "major"]
    minor = [f for f in diff if f["severity"] == "minor"]

    print()
    print("=" * 78)
    print(f"WAKA WAKA TEXT-OVERLAY DIFF (first {report['window_s']:.1f}s)")
    print("=" * 78)
    print(
        f"recipe : {report['videos']['recipe']['path']}  "
        f"({report['videos']['recipe']['aspect']}, "
        f"{report['videos']['recipe']['width']}x{report['videos']['recipe']['height']})"
    )
    print(
        f"output : {report['videos']['output']['path']}  "
        f"({report['videos']['output']['aspect']}, "
        f"{report['videos']['output']['width']}x{report['videos']['output']['height']})"
    )

    print()
    print("--- DETECTED EVENTS ---")
    for label, evs in (("recipe", report["events"]["recipe"]),
                      ("output", report["events"]["output"])):
        print(f"\n  [{label}] {len(evs)} event(s):")
        for ev in evs:
            print(
                f"    {ev['text']!r:>12s}  color={ev['color_key']:6s}  "
                f"t=[{ev['t_start']:5.2f},{ev['t_end']:5.2f}]s  "
                f"pos=({ev['position']['cx_frac']:.2f},{ev['position']['cy_frac']:.2f})  "
                f"size_h={ev['size']['h_frac']:.3f}  "
                f"entrance={ev['animation']['entrance']}  "
                f"text_src={ev['text_source']}"
            )

    print()
    if crit:
        print(f"--- CRITICAL ({len(crit)}) ---")
        for i, f in enumerate(crit, 1):
            print(f"  {i}. {f['reason']}")
    if major:
        print(f"--- MAJOR ({len(major)}) ---")
        for i, f in enumerate(major, 1):
            print(f"  {i}. {f['reason']}")
    if minor:
        print(f"--- MINOR ({len(minor)}) ---")
        for i, f in enumerate(minor, 1):
            print(f"  {i}. {f['reason']}")
    if not (crit or major or minor):
        print("No divergences found within tolerance.")

    print()
    print("--- TOP-5 PLAIN-ENGLISH ---")
    for line in summary:
        print(f"  • {line}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "videos", nargs=2,
        help="Two videos. Use LABEL=path (REF=recipe.mp4 OURS=output.mp4).",
    )
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_S,
                        help=f"Analysis window in seconds (default {DEFAULT_WINDOW_S})")
    parser.add_argument("--sample-step", type=float, default=DEFAULT_STEP_S,
                        help=f"Seconds between sampled frames (default {DEFAULT_STEP_S})")
    parser.add_argument("--json", type=Path,
                        help="Write structured JSON report to this path")
    parser.add_argument("--no-ocr", action="store_true",
                        help="Skip OCR entirely (use color-region fallback for all text)")
    args = parser.parse_args(argv)

    frames_mod.check_ffmpeg_installed()

    ocr_available = (not args.no_ocr) and ocr_mod.is_ocr_available()
    if not args.no_ocr and not ocr_available:
        print(
            "[warn] pytesseract / tesseract not available — text content will "
            "use color-region fallback. Install with: "
            "brew install tesseract && pip install pytesseract",
            file=sys.stderr,
        )

    parsed = [_parse_labeled_video(v) for v in args.videos]
    # Disambiguate which one is the recipe and which the output.
    label_map = {label.upper(): (label, p) for label, p in parsed}
    if "REF" in label_map:
        recipe_label, recipe_path = label_map["REF"]
        ours_label, ours_path = next(
            (lp for k, lp in label_map.items() if k != "REF")
        )
    elif "OURS" in label_map:
        ours_label, ours_path = label_map["OURS"]
        recipe_label, recipe_path = next(
            (lp for k, lp in label_map.items() if k != "OURS")
        )
    else:
        recipe_label, recipe_path = parsed[0]
        ours_label, ours_path = parsed[1]

    recipe = analyze_video(recipe_label, recipe_path, args.window, args.sample_step, ocr_available)
    output = analyze_video(ours_label, ours_path, args.window, args.sample_step, ocr_available)

    report = build_report(recipe, output, args.window, args.sample_step)

    _print_stdout_report(report)

    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"JSON report written to {args.json}", file=sys.stderr)

    # Exit code: 1 if any critical findings; 0 otherwise. Useful in CI.
    has_critical = any(f["severity"] == "critical" for f in report["diff"])
    return 1 if has_critical else 0


if __name__ == "__main__":
    sys.exit(main())
