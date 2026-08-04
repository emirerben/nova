"""CLI: verify the Python/Skia carousel render against a captured browser
reference (see `tools/carousel_reference/`).

Flow: load `<reference>/trace.json` (from `capture.sh`) -> render our side
with a matching frame count (`carousel_verify.render_our_side`) -> encode our
frames to mp4 with the SAME ffmpeg settings `capture.sh` used for
`reference.mp4` -> compute SSIM (`carousel_verify.compute_ssim`) and compare
motion traces (`carousel_verify.compare_motion_traces`) -> write
`<out>/report.json`, `<out>/montage.png`, `<out>/ours.mp4`.

Two independent gates, both must pass:
  - SSIM (pixel parity)   : ssim.global >= --ssim-min
  - trace (motion parity) : max per-frame/per-card px delta <= --trace-tol-px

Exit code: 0 iff both gates pass, 1 otherwise — mirrors `verify_overlays.py`'s
convention so it composes in shells and CI.

Usage:
    cd src/apps/api
    python -m app.cli.verify_carousel --effect scale_sweep \
        --reference ../../../tools/carousel_reference/out/scale-sweep
"""

from __future__ import annotations

import argparse
import json
import os

from app.pipeline import carousel_verify as cv
from app.pipeline.carousel.effects import EFFECTS


def _load_trace(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def _print_summary(report: dict, out_dir: str) -> None:
    ssim = report["ssim"]
    trace = report["trace"]
    overall = "PASS" if report["overall_pass"] else "FAIL"
    print(f"\nverify-carousel: effect={report['effect']}  overall={overall}")
    ssim_verdict = "PASS" if report["gates"]["ssim_pass"] else "FAIL"
    print(
        f"  ssim:  global={ssim['global']:.4f} (min frame {ssim['min_frame']:.4f})  "
        f"threshold>={report['ssim_min']}  {ssim_verdict}"
    )
    trace_verdict = "PASS" if report["gates"]["trace_pass"] else "FAIL"
    print(
        f"  trace: max_delta_px={trace['max_delta_px']:.2f}  "
        f"mean_delta_px={trace['mean_delta_px']:.2f}  "
        f"max_opacity_delta={trace['max_opacity_delta']:.3f}  "
        f"tol<={report['trace_tol_px']}  {trace_verdict}"
    )
    if trace.get("frame_count_mismatch"):
        print(
            f"  WARNING: frame count mismatch — "
            f"browser={trace['browser_frame_count']} ours={trace['our_frame_count']} "
            f"(compared common prefix of {trace['compared_frames']})"
        )
    if trace.get("worst"):
        w = trace["worst"]
        print(
            f"  worst delta: frame {w['frame']} card {w['card']} {w['field']}: "
            f"browser={w['browser']} ours={w['ours']} (delta={w['delta']:.2f}px)"
        )
    print(f"  report:  {os.path.join(out_dir, 'report.json')}")
    print(f"  montage: {os.path.join(out_dir, 'montage.png')}")
    print(f"  ours.mp4: {os.path.join(out_dir, 'ours.mp4')}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verify_carousel", description=__doc__)
    p.add_argument("--effect", required=True, choices=EFFECTS)
    p.add_argument(
        "--reference",
        required=True,
        help="dir containing frame_%%04d.png, trace.json, reference.mp4 (from capture.sh)",
    )
    p.add_argument("--out", default=None, help="output dir (default: <reference>/verify)")
    p.add_argument("--ssim-min", type=float, default=0.95)
    p.add_argument("--trace-tol-px", type=float, default=2.0)
    args = p.parse_args(argv)

    ref_dir = os.path.abspath(args.reference)
    trace_path = os.path.join(ref_dir, "trace.json")
    reference_mp4 = os.path.join(ref_dir, "reference.mp4")

    if not os.path.exists(trace_path):
        raise SystemExit(
            f"no trace.json at {trace_path!r} — capture a reference first: "
            f"tools/carousel_reference/capture.sh <effect> {ref_dir}"
        )
    if not os.path.exists(reference_mp4):
        raise SystemExit(f"no reference.mp4 at {reference_mp4!r} — re-run capture.sh")

    out_dir = os.path.abspath(args.out) if args.out else os.path.join(ref_dir, "verify")
    os.makedirs(out_dir, exist_ok=True)

    browser_trace = _load_trace(trace_path)

    frame_paths, our_trace = cv.render_our_side(args.effect, len(browser_trace), out_dir)
    frames_dir = os.path.join(out_dir, "frames")

    ours_mp4 = os.path.join(out_dir, "ours.mp4")
    cv.encode_frames_to_mp4(frames_dir, ours_mp4, fps=30)

    ssim = cv.compute_ssim(reference_mp4, ours_mp4, out_dir)
    trace_cmp = cv.compare_motion_traces(browser_trace, our_trace, tol_px=args.trace_tol_px)

    montage_path = os.path.join(out_dir, "montage.png")
    cv.build_side_by_side_montage(ref_dir, frames_dir, montage_path)

    ssim_pass = ssim["global"] >= args.ssim_min
    trace_pass = bool(trace_cmp["pass"])

    report = {
        "effect": args.effect,
        "reference_dir": ref_dir,
        "frame_counts": {
            "browser": len(browser_trace),
            "ours": len(frame_paths),
        },
        "ssim": ssim,
        "ssim_min": args.ssim_min,
        "trace": trace_cmp,
        "trace_tol_px": args.trace_tol_px,
        "gates": {"ssim_pass": ssim_pass, "trace_pass": trace_pass},
        "overall_pass": ssim_pass and trace_pass,
    }
    report_path = os.path.join(out_dir, "report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    _print_summary(report, out_dir)
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
