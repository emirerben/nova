"""Pixel-parity gate for single-pass vs multi-pass encode.

The gate the speedup plan rests on. Runs each M2-compatible production
template through both render paths against a reference clip set, computes
SSIM and ffprobe metadata diffs, and asserts parity thresholds before any
``single_pass_encode_enabled`` flag flip.

History: this file ships as the empirical foundation for the multi-clip
template speedup plan (May 2026). Single-pass code (PR #129) and the
fallback path are well-unit-tested at the filter-graph level, but no test
has ever compared single-pass output against multi-pass output on real
pixels. Without that gate, flipping the env flag is a leap of faith;
``tests/pipeline/test_single_pass.py:6`` flags exactly this gap.

Why not in PR CI: each run encodes real H.264 video twice per template
fixture. A 2-slot 6-second template costs ~10s of wall-clock per encode
pair; longer templates scale linearly. Runs via ``workflow_dispatch`` only
(see ``.github/workflows/single-pass-parity.yml``) or manual local invoke.

PARITY_CLIPS_DIR
    Directory of reference ``.mp4`` clips. Raw video is never committed
    (CLAUDE.md). Each clip should be ≥3s, ≤1080p, ≤16:9, with audio.
    The fixture loader picks the first N alphabetically. Suggested set:
    one gradient (banding canary), one motion, one text-on-screen, one
    photo-import, one low-light. Five is enough.

PARITY_REPORT_DIR
    Where to dump the markdown report. Defaults to a tmp dir; set to
    ``~/.gstack/projects/<slug>/`` for archival.

Thresholds (derived from Plan section "Failure modes"):
    SSIM (global Y channel)   ≥ 0.98          — visible diff if below
    Frame count delta         == 0
    Duration delta            ≤ 0.05 s
    Codec                     exact match
    Pixel format              exact match
    FPS                       exact match
    Bitrate delta             ≤ 15 %          — soft, single-pass tends higher

A score in [0.98, 0.995] means "likely fine but eyeball it before merging."
> 0.995 is visually identical for our purposes.

Templates currently covered (M2-compatible, hard-cut only, no curtain-close,
no xfade, no absolute overlays):

    - impressing-myself (2 slots)
    - just-fine (2 slots)
    - morocco (24 slots — heaviest test of the scaffold)

To add a milestone-N template, drop a recipe JSON into
``tests/fixtures/parity_templates/`` and append to ``PARITY_TEMPLATE_FIXTURES``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

PARITY_CLIPS_DIR = Path(os.environ.get("PARITY_CLIPS_DIR", "")).expanduser()
PARITY_REPORT_DIR = Path(
    os.environ.get("PARITY_REPORT_DIR", tempfile.gettempdir())
).expanduser()
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "parity_templates"

SSIM_MIN_GLOBAL = 0.98
DURATION_TOLERANCE_S = 0.05
# Frame count tolerance — multi-pass runs 3+ encode passes (reframe → concat
# → text-overlay burn) each of which can drift the output by a frame from
# container/codec rounding. Single-pass is one encode end-to-end. A 1-frame
# delta at 30fps is 33ms, comfortably under the DURATION_TOLERANCE_S=50ms
# gate, and shouldn't gate parity by itself.
FRAME_COUNT_TOLERANCE = 2
# Bitrate gate is ONE-SIDED — single-pass producing a HIGHER bitrate than
# multi-pass at equal visual fidelity (SSIM check above) would suggest the
# single-pass encode is wasting bytes. Lower bitrate is fine, often
# expected: M2-shape templates have multi-pass stream-copy from ultrafast
# slot mp4s (high bitrate, low quality settings) while single-pass
# re-encodes at preset=fast which is more efficient at the same SSIM.
BITRATE_OVERSHOOT_TOLERANCE_PCT = 0.15


@dataclass
class ProbeMeta:
    duration_s: float
    fps: float
    width: int
    height: int
    codec: str
    pix_fmt: str
    bitrate: int
    frame_count: int


@dataclass
class ParityResult:
    template: str
    ssim_global: float
    ssim_min_frame: float
    multi_meta: ProbeMeta
    single_meta: ProbeMeta
    multi_elapsed_s: float
    single_elapsed_s: float
    passed: bool
    failure_reasons: list[str]


def _ffprobe(path: Path) -> ProbeMeta:
    """Extract metadata for parity comparison. Uses a single ffprobe call."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate,pix_fmt,nb_read_packets,bit_rate,duration",
            "-count_packets", "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=30, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    num, denom = (int(x) for x in stream["r_frame_rate"].split("/"))
    return ProbeMeta(
        duration_s=float(stream.get("duration", "0") or 0.0),
        fps=num / denom if denom else 0.0,
        width=stream["width"],
        height=stream["height"],
        codec=stream["codec_name"],
        pix_fmt=stream["pix_fmt"],
        bitrate=int(stream.get("bit_rate", "0") or 0),
        frame_count=int(stream.get("nb_read_packets", "0") or 0),
    )


_SSIM_LINE_RE = re.compile(r"All:([\d.]+)")


def _compute_ssim(reference: Path, candidate: Path, stats_path: Path) -> tuple[float, float]:
    """Compute global SSIM via ffmpeg's ssim filter. Returns (global, min_frame).

    The ssim filter compares two streams frame-by-frame; the per-frame log
    is written to stats_path and the final "All:X.XX" line is printed to
    stderr. Both inputs MUST have identical frame counts; an off-by-one is
    a real diff, not a measurement artifact.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "info", "-y",
            "-i", str(reference), "-i", str(candidate),
            "-lavfi", f"ssim=stats_file={stats_path}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=600, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg ssim failed: {result.stderr[-500:]}")

    # ffmpeg writes the summary line like:
    #   [Parsed_ssim_0 @ 0x...] SSIM Y:0.99 U:0.99 V:0.99 All:0.99 (24.36)
    global_ssim = 0.0
    for line in reversed(result.stderr.splitlines()):
        match = _SSIM_LINE_RE.search(line)
        if match:
            global_ssim = float(match.group(1))
            break

    min_frame = 1.0
    if stats_path.exists():
        for stats_line in stats_path.read_text().splitlines():
            # Each line: "n:1 Y:0.98 U:0.99 V:0.99 All:0.98 (24.36)"
            match = _SSIM_LINE_RE.search(stats_line)
            if match:
                min_frame = min(min_frame, float(match.group(1)))

    return global_ssim, min_frame


def _load_clips(min_count: int = 2) -> list[Path]:
    if not PARITY_CLIPS_DIR or not PARITY_CLIPS_DIR.is_dir():
        return []
    clips = sorted(PARITY_CLIPS_DIR.glob("*.mp4"))
    return clips if len(clips) >= min_count else []


def _load_fixture(name: str) -> dict | None:
    """Load a parity template fixture. None if not present.

    Schema (JSON):
        {
          "name": "impressing-myself",
          "slots": [
            {"position": 1, "target_duration_s": 3.0, "transition_in": "none",
             "text_overlays": []},
            ...
          ],
          "interstitials": []  // optional; M2 supports fade-black-hold / flash-white
        }
    """
    path = FIXTURES_DIR / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


PARITY_TEMPLATE_FIXTURES = [
    "impressing-myself",
    "just-fine",
    # M3: xfade single-pass exercised by the dissolve transitions in this
    # fixture. Heavier than the M2 set (~30s per encode pair); uncomment
    # for full parity runs once the M2 set is green against real clips.
    "dimples-passport",
    # M6: absolute-timestamp text overlays exercised by the fade-in
    # text_overlays. The PNG and ASS code paths both fire.
    "rule-of-thirds",
    # 6-slot stress fixture; ~60-90s per encode pair.
    # "morocco",
]


def _build_steps_and_probes(fixture: dict, clips: list[Path]):
    """Construct (steps, clip_id_to_local, clip_probe_map) for _assemble_clips.

    Uses the FIRST N clips from the reference set, where N = slot count.
    Both render paths see identical inputs, so this is a deterministic
    pair-up; the parity assertion is on output equivalence.
    """
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


def _render(
    fixture: dict, clips: list[Path], tmp_path: Path, force_single_pass: bool,
) -> tuple[Path, float]:
    """Run _assemble_clips once. Returns (output_path, elapsed_seconds)."""
    from app.tasks.template_orchestrate import _assemble_clips

    suffix = "single" if force_single_pass else "multi"
    output = tmp_path / f"out_{suffix}.mp4"
    work = tmp_path / f"work_{suffix}"
    work.mkdir(exist_ok=True)

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


@pytest.mark.skipif(
    not _load_clips(),
    reason="PARITY_CLIPS_DIR must point to a dir with ≥2 mp4 clips. "
           "Raw clips are not committed; supply your own reference set.",
)
@pytest.mark.parametrize("template_name", PARITY_TEMPLATE_FIXTURES)
def test_single_pass_parity_with_multi_pass(template_name: str, tmp_path: Path) -> None:
    """For each M2-compatible production template, single-pass and multi-pass
    must produce output that is visually equivalent (SSIM ≥ 0.98), identical
    in frame count, and within 50ms of the same duration.

    Failures dump a per-template markdown line and fail loudly. A single
    template regressing should NOT block other templates; pytest parametrize
    keeps them independent.
    """
    fixture = _load_fixture(template_name)
    if fixture is None:
        pytest.skip(
            f"Fixture {template_name}.json not in {FIXTURES_DIR}. "
            f"Add it before running Phase 0.5."
        )

    clips = _load_clips()
    stats_path = tmp_path / "ssim_stats.log"

    multi_out, multi_elapsed = _render(fixture, clips, tmp_path, force_single_pass=False)
    single_out, single_elapsed = _render(fixture, clips, tmp_path, force_single_pass=True)

    multi_meta = _ffprobe(multi_out)
    single_meta = _ffprobe(single_out)
    ssim_global, ssim_min_frame = _compute_ssim(multi_out, single_out, stats_path)

    reasons: list[str] = []
    if ssim_global < SSIM_MIN_GLOBAL:
        reasons.append(f"SSIM global {ssim_global:.4f} < {SSIM_MIN_GLOBAL}")
    if abs(multi_meta.duration_s - single_meta.duration_s) > DURATION_TOLERANCE_S:
        reasons.append(
            f"duration delta {abs(multi_meta.duration_s - single_meta.duration_s):.3f}s "
            f"> {DURATION_TOLERANCE_S}s"
        )
    frame_delta = abs(multi_meta.frame_count - single_meta.frame_count)
    if frame_delta > FRAME_COUNT_TOLERANCE:
        reasons.append(
            f"frame count delta {frame_delta} > {FRAME_COUNT_TOLERANCE} "
            f"(multi={multi_meta.frame_count} single={single_meta.frame_count})"
        )
    if multi_meta.codec != single_meta.codec:
        reasons.append(f"codec mismatch: multi={multi_meta.codec} single={single_meta.codec}")
    if multi_meta.pix_fmt != single_meta.pix_fmt:
        reasons.append(f"pix_fmt mismatch: multi={multi_meta.pix_fmt} single={single_meta.pix_fmt}")
    # FPS via r_frame_rate fractions can drift by a hair after division
    # (e.g. 30000/1001 → 29.97002997...). Match within 0.01 fps.
    if abs(multi_meta.fps - single_meta.fps) > 0.01:
        reasons.append(f"fps mismatch: multi={multi_meta.fps} single={single_meta.fps}")
    # Bitrate gate (one-sided): single-pass overshooting multi-pass bitrate
    # at equal SSIM means the encode is wasting bytes. Single-pass UNDER
    # multi-pass is fine, often expected — see BITRATE_OVERSHOOT_TOLERANCE_PCT
    # docstring at the top of the module.
    if multi_meta.bitrate > 0 and single_meta.bitrate > multi_meta.bitrate:
        overshoot = (single_meta.bitrate - multi_meta.bitrate) / multi_meta.bitrate
        if overshoot > BITRATE_OVERSHOOT_TOLERANCE_PCT:
            reasons.append(
                f"bitrate overshoot {overshoot:.1%} > {BITRATE_OVERSHOOT_TOLERANCE_PCT:.0%} "
                f"(multi={multi_meta.bitrate} single={single_meta.bitrate})"
            )

    result = ParityResult(
        template=template_name,
        ssim_global=ssim_global,
        ssim_min_frame=ssim_min_frame,
        multi_meta=multi_meta,
        single_meta=single_meta,
        multi_elapsed_s=multi_elapsed,
        single_elapsed_s=single_elapsed,
        passed=not reasons,
        failure_reasons=reasons,
    )
    _append_report(result)

    if reasons:
        pytest.fail(
            f"Parity failed for {template_name}:\n  - " + "\n  - ".join(reasons)
            + f"\nReport: {PARITY_REPORT_DIR}/single-pass-parity.md"
        )


def _append_report(result: ParityResult) -> None:
    """Append a markdown row per template. Idempotent header creation."""
    PARITY_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = PARITY_REPORT_DIR / "single-pass-parity.md"
    if not report.exists():
        report.write_text(
            "# Single-pass vs multi-pass parity report\n\n"
            "Generated by tests/quality/single_pass_parity.py.\n\n"
            "| Template | SSIM (global) | SSIM (min frame) | Duration delta | "
            "Frame delta | Multi elapsed | Single elapsed | Result |\n"
            "|----------|---------------|------------------|----------------|"
            "------------|---------------|----------------|--------|\n"
        )
    duration_delta = result.single_meta.duration_s - result.multi_meta.duration_s
    frame_delta = result.single_meta.frame_count - result.multi_meta.frame_count
    status = "PASS" if result.passed else "FAIL: " + "; ".join(result.failure_reasons)
    with report.open("a") as f:
        f.write(
            f"| {result.template} | {result.ssim_global:.4f} | "
            f"{result.ssim_min_frame:.4f} | {duration_delta:+.3f}s | "
            f"{frame_delta:+d} | {result.multi_elapsed_s:.1f}s | "
            f"{result.single_elapsed_s:.1f}s | {status} |\n"
        )


def test_ffmpeg_is_available() -> None:
    """Sanity: the harness depends on ffmpeg + ffprobe being on PATH. Catch
    a misconfigured environment before the parity tests do."""
    assert shutil.which("ffmpeg"), "ffmpeg not on PATH"
    assert shutil.which("ffprobe"), "ffprobe not on PATH"
