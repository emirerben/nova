"""Pixel + motion parity gate for the Blossom-carousel Python/Skia render vs.
the captured browser reference (`tools/carousel_reference/`).

Structure mirrors `single_pass_parity.py`: gated on an env var pointing at a
directory of captured fixtures (raw browser captures are never committed —
see root CLAUDE.md), parametrized so one effect regressing doesn't block the
others, and each effect independently skips if its own fixture is missing
(capture happens effect-by-effect via `capture.sh`, so a partial capture set
is expected mid-loop, not an error).

CAROUSEL_PARITY_REFERENCE_ROOT
    Directory containing one subdirectory per effect (Python effect names:
    scale_sweep, cover_flow, cards_stack, flipbook), each holding the
    `capture.sh` output: frame_%04d.png, trace.json, reference.mp4.

    Capture one with (note: capture.sh's own EFFECT vocabulary is the HTML
    page slugs — scale-sweep / cover-flow / cards / flipbook, NOT the same as
    the Python names above; `make carousel-capture EFFECT=<python_name>`
    handles the translation):

        make carousel-capture EFFECT=scale_sweep
        make carousel-capture EFFECT=cover_flow
        make carousel-capture EFFECT=cards_stack
        make carousel-capture EFFECT=flipbook

    which by default writes to tools/carousel_reference/out/<python_name>/ —
    point CAROUSEL_PARITY_REFERENCE_ROOT at tools/carousel_reference/out to
    pick those up directly.

Thresholds:
    SSIM (global)        >= CAROUSEL_SSIM_MIN (default 0.95, matches
                             single_pass_parity's grain-heavy-content rule
                             of thumb — see that module's calibration note)
    Motion trace          max per-frame/per-card px delta <= CAROUSEL_TRACE_TOL_PX
                             (default 2.0px)
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

REFERENCE_ROOT = Path(os.environ.get("CAROUSEL_PARITY_REFERENCE_ROOT", "")).expanduser()
SSIM_MIN = float(os.environ.get("CAROUSEL_SSIM_MIN", "0.95"))
TRACE_TOL_PX = float(os.environ.get("CAROUSEL_TRACE_TOL_PX", "2.0"))

EFFECTS = ("scale_sweep", "cover_flow", "cards_stack", "flipbook")


def _report_dir() -> Path:
    return Path(os.environ.get("PARITY_REPORT_DIR", os.environ.get("TMPDIR", "/tmp"))).expanduser()


@pytest.mark.skipif(
    not REFERENCE_ROOT or not REFERENCE_ROOT.is_dir(),
    reason=(
        "CAROUSEL_PARITY_REFERENCE_ROOT must point to a dir with one subdirectory "
        "per effect (scale_sweep/cover_flow/cards_stack/flipbook), each holding "
        "capture.sh output (trace.json, reference.mp4, frame_*.png). Capture one "
        "with `make carousel-capture EFFECT=<effect>` — raw browser captures are "
        "never committed (see root CLAUDE.md)."
    ),
)
@pytest.mark.parametrize("effect", EFFECTS)
def test_carousel_parity(effect: str, tmp_path: Path) -> None:
    """Our Skia render must match the captured browser reference within the
    SSIM and motion-trace tolerances for every effect. Failures append a row
    to the parity report; one effect regressing does not block the others.
    """
    from app.pipeline import carousel_verify as cv

    effect_dir = REFERENCE_ROOT / effect
    trace_path = effect_dir / "trace.json"
    reference_mp4 = effect_dir / "reference.mp4"

    if not trace_path.exists():
        pytest.skip(
            f"no trace.json at {trace_path} — capture it with "
            f"`make carousel-capture EFFECT={effect}` before running this gate."
        )
    if not reference_mp4.exists():
        pytest.skip(f"no reference.mp4 at {reference_mp4} — re-run capture.sh for {effect}.")

    browser_trace = json.loads(trace_path.read_text())

    work_dir = tmp_path / effect
    work_dir.mkdir(parents=True, exist_ok=True)

    _frame_paths, our_trace = cv.render_our_side(effect, len(browser_trace), str(work_dir))
    frames_dir = work_dir / "frames"

    ours_mp4 = work_dir / "ours.mp4"
    cv.encode_frames_to_mp4(str(frames_dir), str(ours_mp4), fps=30)

    ssim = cv.compute_ssim(str(reference_mp4), str(ours_mp4), str(work_dir))
    trace_cmp = cv.compare_motion_traces(browser_trace, our_trace, tol_px=TRACE_TOL_PX)

    montage_path = work_dir / "montage.png"
    cv.build_side_by_side_montage(str(effect_dir), str(frames_dir), str(montage_path))

    reasons: list[str] = []
    if ssim["global"] < SSIM_MIN:
        reasons.append(f"SSIM global {ssim['global']:.4f} < {SSIM_MIN}")
    if not trace_cmp["pass"]:
        worst = trace_cmp["worst"]
        reasons.append(
            f"motion trace max_delta_px {trace_cmp['max_delta_px']:.2f} > {TRACE_TOL_PX} "
            f"(worst: frame {worst['frame']} card {worst['card']} {worst['field']}: "
            f"browser={worst['browser']} ours={worst['ours']})"
        )
    if trace_cmp["frame_count_mismatch"]:
        reasons.append(
            f"frame count mismatch: browser={trace_cmp['browser_frame_count']} "
            f"ours={trace_cmp['our_frame_count']}"
        )

    _append_report(effect, ssim, trace_cmp, montage_path, passed=not reasons)

    if reasons:
        pytest.fail(
            f"Carousel parity failed for {effect}:\n  - "
            + "\n  - ".join(reasons)
            + f"\nMontage: {montage_path}"
        )


def _append_report(
    effect: str, ssim: dict, trace_cmp: dict, montage_path: Path, passed: bool
) -> None:
    report_dir = _report_dir()
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "carousel-parity.md"
    if not report.exists():
        report.write_text(
            "# Carousel parity report\n\n"
            "Generated by tests/quality/carousel_parity.py.\n\n"
            "| Effect | SSIM (global) | SSIM (min frame) | Max trace delta (px) | "
            "Mean trace delta (px) | Result |\n"
            "|--------|----------------|-------------------|------------------------|"
            "------------------------|--------|\n"
        )
    status = "PASS" if passed else "FAIL"
    with report.open("a") as f:
        f.write(
            f"| {effect} | {ssim['global']:.4f} | {ssim['min_frame']:.4f} | "
            f"{trace_cmp['max_delta_px']:.2f} | {trace_cmp['mean_delta_px']:.2f} | {status} |\n"
        )


def test_ffmpeg_is_available() -> None:
    """Sanity: this gate depends on ffmpeg being on PATH. Catch a
    misconfigured environment before the parity tests do."""
    assert shutil.which("ffmpeg"), "ffmpeg not on PATH"
