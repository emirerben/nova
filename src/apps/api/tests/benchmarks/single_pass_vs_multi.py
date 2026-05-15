"""Wall-clock benchmark for single-pass vs multi-pass encode.

Pairs with ``tests/quality/single_pass_parity.py``. Where parity asks "is
the output the same?", this asks "is it faster?". Both must clear their
gates before any ``single_pass_encode_enabled`` flag flip; quality without
speedup is wasted milestones, speedup without quality is a regression.

History: ships as the empirical foundation for the multi-clip template
speedup plan (May 2026). The single-pass docstring claims "the multi-pass
path runs the final-output bytes through libx264 fast 3-4 times" and
projects a 30-50% wall-clock reduction. Empirically verified that the
encode count varies from 0 to ~12 depending on template features, so the
speedup claim is template-conditional. This harness measures it per template.

Why not in PR CI: each trial encodes real H.264 twice; multiplied by N
templates and K repeats this runs for many minutes. Workflow_dispatch only
(see ``.github/workflows/single-pass-parity.yml``).

PARITY_CLIPS_DIR
    Same reference clip set used by the parity gate. See that file's
    docstring for the suggested mix.

BENCH_REPEATS
    How many trials per template per render path. Default 3 (good
    median/spread tradeoff for slow runs). Set ``BENCH_REPEATS=1`` for
    smoke runs.

BENCH_REPORT_DIR
    Where to dump the markdown report. Defaults to a tmp dir.

Gates (Plan section "Phase 0.6"):
    - No regression on M2-compatible simple templates (single-pass ≤ 1.10×
      multi-pass median; stream-copy concat means multi-pass is FAST for
      hard-cut, so single-pass losing here is expected up to ~10%).
    - Positive delta on templates we expect to win on (curtain-close, xfade,
      absolute overlays). Single-pass < 0.85× multi-pass median required to
      unlock a milestone PR.

The benchmark does NOT compare different concurrency settings. That's a
separate Phase 2 single-machine Fly canary, not a pytest run.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

PARITY_CLIPS_DIR = Path(os.environ.get("PARITY_CLIPS_DIR", "")).expanduser()
BENCH_REPORT_DIR = Path(
    os.environ.get("BENCH_REPORT_DIR", tempfile.gettempdir())
).expanduser()
BENCH_REPEATS = int(os.environ.get("BENCH_REPEATS", "3"))
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "parity_templates"

# Reuse the same fixtures as the parity gate. Don't duplicate.
BENCH_TEMPLATE_FIXTURES = [
    "impressing-myself",
    "just-fine",
    # "morocco",  # 24 slots; expensive
]


@dataclass
class BenchResult:
    template: str
    repeats: int
    multi_seconds: list[float]
    single_seconds: list[float]
    multi_median: float
    single_median: float
    ratio: float
    output_size_multi: int
    output_size_single: int


def _load_clips(min_count: int = 2) -> list[Path]:
    if not PARITY_CLIPS_DIR or not PARITY_CLIPS_DIR.is_dir():
        return []
    clips = sorted(PARITY_CLIPS_DIR.glob("*.mp4"))
    return clips if len(clips) >= min_count else []


def _load_fixture(name: str) -> dict | None:
    path = FIXTURES_DIR / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _build_steps_and_probes(fixture: dict, clips: list[Path]):
    """Same plumbing as the parity script; kept inline to avoid cross-module
    coupling between the quality and benchmark harnesses."""
    from app.pipeline.agents.gemini_analyzer import AssemblyStep
    from app.pipeline.probe import probe_video

    slots = fixture["slots"]
    chosen = clips[: len(slots)]
    steps = []
    clip_id_to_local: dict[str, str] = {}
    clip_probe_map: dict = {}
    for slot, clip in zip(slots, chosen):
        clip_id = clip.stem
        target = float(slot.get("target_duration_s", 3.0))
        steps.append(
            AssemblyStep(
                slot=slot,
                clip_id=clip_id,
                moment={"start_s": 0.0, "end_s": target, "energy": 0.5, "description": ""},
            )
        )
        clip_id_to_local[clip_id] = str(clip)
        clip_probe_map[str(clip)] = probe_video(str(clip))
    return steps, clip_id_to_local, clip_probe_map


def _render_once(fixture: dict, clips: list[Path], tmp_path: Path, force_single_pass: bool) -> tuple[Path, float]:
    from app.tasks.template_orchestrate import _assemble_clips

    suffix = "single" if force_single_pass else "multi"
    output = tmp_path / f"out_{suffix}_{time.monotonic_ns()}.mp4"
    work = tmp_path / f"work_{suffix}_{time.monotonic_ns()}"
    work.mkdir()

    steps, clip_id_to_local, clip_probe_map = _build_steps_and_probes(fixture, clips)
    interstitials = fixture.get("interstitials", [])

    t0 = time.monotonic()
    _assemble_clips(
        steps=steps,
        clip_id_to_local=clip_id_to_local,
        clip_probe_map=clip_probe_map,
        output_path=str(output),
        tmpdir=str(work),
        interstitials=interstitials,
        force_single_pass=force_single_pass,
    )
    return output, time.monotonic() - t0


def _classify_speedup(template: str, ratio: float) -> tuple[str, bool]:
    """Decide pass/fail given the template kind. Returns (verdict, passed).

    Simple templates (hard-cut only): tolerated up to 1.10x slower since
    multi-pass has the stream-copy concat fast-path.
    Heavy templates (curtain-close, xfade, absolute overlays): require
    single-pass < 0.85x multi-pass to justify the milestone.

    The fixture loader doesn't currently distinguish kinds; this is hooked
    to the fixture name today. Move to a fixture metadata field once heavy
    templates land.
    """
    heavy = {"dimples-passport", "saygimdan", "rule-of-thirds", "football-face-hook"}
    if template in heavy:
        return f"heavy template; needs ratio < 0.85 (got {ratio:.2f}x)", ratio < 0.85
    return f"simple template; tolerated up to 1.10x (got {ratio:.2f}x)", ratio <= 1.10


@pytest.mark.skipif(
    not _load_clips(),
    reason="PARITY_CLIPS_DIR must point to a dir with ≥2 mp4 clips.",
)
@pytest.mark.parametrize("template_name", BENCH_TEMPLATE_FIXTURES)
def test_single_pass_perf_vs_multi(template_name: str, tmp_path: Path) -> None:
    """Median wall-clock comparison per template. Asserts the per-template
    speedup gate. A failure here means the milestone PR for this template
    is not ready to ship.
    """
    fixture = _load_fixture(template_name)
    if fixture is None:
        pytest.skip(f"Fixture {template_name}.json not in {FIXTURES_DIR}.")

    clips = _load_clips()

    multi_times: list[float] = []
    single_times: list[float] = []
    multi_size = 0
    single_size = 0

    for trial in range(BENCH_REPEATS):
        multi_out, multi_t = _render_once(fixture, clips, tmp_path, force_single_pass=False)
        multi_times.append(multi_t)
        multi_size = multi_out.stat().st_size
    for trial in range(BENCH_REPEATS):
        single_out, single_t = _render_once(fixture, clips, tmp_path, force_single_pass=True)
        single_times.append(single_t)
        single_size = single_out.stat().st_size

    multi_median = statistics.median(multi_times)
    single_median = statistics.median(single_times)
    ratio = single_median / multi_median if multi_median > 0 else float("inf")

    result = BenchResult(
        template=template_name,
        repeats=BENCH_REPEATS,
        multi_seconds=multi_times,
        single_seconds=single_times,
        multi_median=multi_median,
        single_median=single_median,
        ratio=ratio,
        output_size_multi=multi_size,
        output_size_single=single_size,
    )
    _append_report(result)

    verdict, passed = _classify_speedup(template_name, ratio)
    if not passed:
        pytest.fail(
            f"Benchmark failed for {template_name}: {verdict}. "
            f"Report: {BENCH_REPORT_DIR}/single-pass-bench.md"
        )


def _append_report(result: BenchResult) -> None:
    BENCH_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = BENCH_REPORT_DIR / "single-pass-bench.md"
    if not report.exists():
        report.write_text(
            "# Single-pass vs multi-pass benchmark report\n\n"
            "Generated by tests/benchmarks/single_pass_vs_multi.py.\n\n"
            "| Template | Repeats | Multi (median) | Single (median) | "
            "Single / Multi | Multi size | Single size |\n"
            "|----------|---------|----------------|-----------------|"
            "----------------|------------|-------------|\n"
        )
    with report.open("a") as f:
        f.write(
            f"| {result.template} | {result.repeats} | "
            f"{result.multi_median:.2f}s | {result.single_median:.2f}s | "
            f"{result.ratio:.2f}x | "
            f"{result.output_size_multi/1024:.0f} KB | "
            f"{result.output_size_single/1024:.0f} KB |\n"
        )


def test_ffmpeg_is_available() -> None:
    """Harness depends on ffmpeg/ffprobe on PATH."""
    import shutil
    assert shutil.which("ffmpeg"), "ffmpeg not on PATH"
    assert shutil.which("ffprobe"), "ffprobe not on PATH"
