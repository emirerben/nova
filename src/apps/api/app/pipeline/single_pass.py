"""Single-pass FFmpeg encode for template assembly.

Collapses stages 1-6 of the multi-pass pipeline (per-slot reframe, pre-curtain
text burn, curtain animation, concat/xfade join, final text-overlay burn) into
ONE filter_complex invocation with ONE final libx264 encode. Audio mix (stage
7) stays separate.

Why: the multi-pass path runs the final-output bytes through libx264 fast 3-4
times. Quality compounds (macroblocking on smooth gradients — the brazil.mp4
bug class) and latency adds up. One encode is structurally better.

Secondary unlock: with one ffmpeg process per job instead of 3 parallel
(reframe phase) + 3-4 sequential (final-output stages), two concurrent jobs
share the worker's 2 shared CPUs cleanly. After single-pass is stable in prod
this directly enables flipping `worker --concurrency=1` → `--concurrency=2` in
fly.toml — a ~2x throughput win at the same VM cost. The original constraint
(PR #103, job d018d1c3: --concurrency=2 ballooned a curtain-close encode from
14s to >600s) was a geq-pixel-filter contention issue that PR #105 already
fixed at the filter level and single-pass retires at the process level.

Gated by `settings.single_pass_encode_enabled` (env-default OFF) and the
per-job `force_single_pass` kwarg on `orchestrate_template_job`. Both paths
coexist; the multi-pass branch in _assemble_clips is untouched until the env
default flips in a follow-up PR.

Public API:
    SinglePassInput        per-segment record (kind="clip"|"color_hold")
    SinglePassSpec         flat list of inputs + transitions + absolute overlays
    build_single_pass_command(spec, output_path) -> list[str]   pure, testable
    run_single_pass(spec, output_path) -> None                  subprocess

Milestone 2 scope: clips + color holds + hard-cut concat. xfade transitions,
curtain-close PNG overlay, pre-burn PNGs, and the absolute-overlay layer land
in milestones 3-6. The runner (`run_single_pass`) is still stubbed.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from typing import Literal

import structlog

from app.config import settings
from app.pipeline.audio_layout import SILENT_AUDIO_INPUT_ARGS
from app.pipeline.reframe import _build_video_filter, _encoding_args

log = structlog.get_logger()

# Timeout matches the multi-pass orchestrator's outer Celery soft_time_limit
# of 1740s. A single ffmpeg invocation should complete well under that, but a
# pathological 24-slot Morocco template with heavy font-cycle is the relevant
# stress case.
_SUBPROCESS_TIMEOUT_S = 1500


class SinglePassError(Exception):
    """FFmpeg failure during single-pass encode (mirrors ReframeError)."""


class SinglePassUnsupportedError(NotImplementedError):
    """The current SinglePassSpec uses a feature single-pass doesn't yet
    implement (xfade, curtain-close, pre-burn PNGs, absolute overlays).

    Subclasses NotImplementedError so existing `except NotImplementedError`
    handlers keep working, but callers that want to be specific can catch
    this directly. The orchestrator's gate-point catches it and falls back
    to multi-pass with a structured warning.
    """


@dataclasses.dataclass
class SinglePassInput:
    """One segment of the assembled video (clip or color hold).

    For ``kind="clip"``:
      - clip_path, start_s, end_s, aspect_ratio, output_fit are required
      - speed_factor, color_trc, color_hint, has_audio, curtain_animate_s
        carry the same semantics as SlotPlan fields
      - pre_burn_pngs are slot-relative-timestamp PNGs overlaid BEFORE bars

    For ``kind="color_hold"``:
      - hold_color ("black" | "white" | hex) and hold_s required
      - all clip-only fields are None / default
    """

    kind: Literal["clip", "color_hold"]
    # clip-only (None for color_hold)
    clip_path: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    speed_factor: float = 1.0
    aspect_ratio: str = "16:9"
    output_fit: str = "crop"
    color_trc: str = "bt709"
    color_hint: str = "none"
    darkening_windows: list[tuple[float, float]] = dataclasses.field(default_factory=list)
    narrowing_windows: list[tuple[float, float]] = dataclasses.field(default_factory=list)
    has_grid: bool = False
    grid_params: dict = dataclasses.field(default_factory=dict)
    has_audio: bool = False
    curtain_animate_s: float = 0.0
    pre_burn_pngs: list[dict] = dataclasses.field(default_factory=list)
    # color_hold-only
    hold_color: str = "black"
    hold_s: float = 0.0


@dataclasses.dataclass
class SinglePassSpec:
    """Flat assembly spec consumed by build_single_pass_command.

    Fields:
      inputs        ordered list of clips + color holds in render order
      transitions   per-pair transition between adjacent inputs;
                    len(transitions) == len(inputs) - 1.
                    "none" = hard cut (concat), else xfade transition name
                    matching app.pipeline.transitions vocabulary.
                    Milestone 2: only "none" is implemented; non-"none"
                    raises NotImplementedError at build time.
      abs_pngs      absolute-timestamp PNGs for the post-concat overlay layer.
                    Each dict matches the existing _collect_absolute_overlays
                    shape (start_s, end_s, x, y, png_path). M6 wires these in.
      abs_ass_paths absolute-timestamp ASS files for the post-concat layer.
                    Applied via subtitles=...:fontsdir=... last (after PNGs).
      fonts_dir     fontsdir for the subtitles= filter; matches reframe.py.
      output_duration_s   target output duration (for validation /
                          tpad bridge in the parity script).
    """

    inputs: list[SinglePassInput]
    transitions: list[str] = dataclasses.field(default_factory=list)
    abs_pngs: list[dict] = dataclasses.field(default_factory=list)
    abs_ass_paths: list[str] = dataclasses.field(default_factory=list)
    fonts_dir: str = ""
    output_duration_s: float = 0.0

    def __post_init__(self) -> None:
        if len(self.inputs) < 1:
            raise ValueError("SinglePassSpec requires at least one input")
        if self.transitions and len(self.transitions) != len(self.inputs) - 1:
            raise ValueError(
                f"transitions length ({len(self.transitions)}) must be "
                f"inputs length ({len(self.inputs)}) - 1"
            )


def _color_lavfi_arg(inp: SinglePassInput) -> str:
    """Build the `-i color=...` filter source string for a color hold.

    No decoder — the source synthesizes solid-color frames inline. Mirrors
    `render_color_hold` in interstitials.py:113-117 (same width/height/fps/dur
    contract) but emitted as a lavfi `-i` input rather than a pre-rendered mp4.
    """
    return (
        f"color=c={inp.hold_color}"
        f":s={settings.output_width}x{settings.output_height}"
        f":d={inp.hold_s}"
        f":r={settings.output_fps}"
    )


def _per_clip_filter_chain(inp: SinglePassInput, input_idx: int, output_label: str) -> str:
    """Build the filter graph fragment for one clip input.

    Reuses `_build_video_filter` (reframe.py:477) verbatim so the per-slot
    visual treatment in single-pass is byte-identical to multi-pass: HDR
    tonemap → setpts → scale/crop → color grade → darkening → narrowing →
    grid. Returns the `[N:v]<chain>[v_N]` segment, no trailing `;`.

    The `subtitles=` filter at the end of `_build_video_filter` is suppressed
    here (ass_path=None) — single-pass burns ASS at the post-concat absolute
    overlay layer instead (milestone 6).
    """
    vf_parts = _build_video_filter(
        aspect_ratio=inp.aspect_ratio,
        ass_path=None,
        color_hint=inp.color_hint,
        speed_factor=inp.speed_factor,
        darkening_windows=inp.darkening_windows or None,
        narrowing_windows=inp.narrowing_windows or None,
        color_trc=inp.color_trc or "bt709",
        output_fit=inp.output_fit,
        has_grid=inp.has_grid,
        **(inp.grid_params or {}),
    )
    # Force yuv420p at the end of every per-slot chain. _build_video_filter
    # emits format=yuv420p only on the HDR/HLG branches (via _ZSCALE_SDR_PIPELINE
    # and _HDR_FALLBACK_PIPELINE); SDR sources skip it because _encoding_args
    # forces -pix_fmt yuv420p on the output in the multi-pass path. In single-
    # pass the concat filter runs BEFORE the encoder, so mixed pixel formats
    # across slots silently corrupt the output. Idempotent — adding format=
    # yuv420p when the chain already ends in it is a no-op.
    vf_parts.append("format=yuv420p")
    chain = ",".join(vf_parts)
    return f"[{input_idx}:v]{chain}[{output_label}]"


def _per_hold_filter_chain(input_idx: int, output_label: str) -> str:
    """Build the filter graph fragment for one color hold input.

    lavfi `color=` already emits the target resolution and fps. Force yuv420p
    so it joins the concat with the same pixel format as the clip chains.
    """
    return f"[{input_idx}:v]format=yuv420p[{output_label}]"


def build_single_pass_command(
    spec: SinglePassSpec,
    output_path: str,
) -> list[str]:
    """Construct the single-pass ffmpeg command from a SinglePassSpec.

    Pure function — no I/O, no subprocess. Returns the argv list for
    subprocess.run. Unit-testable.

    Milestone 2 scope: hard-cut concat only. Non-"none" transitions raise
    NotImplementedError (milestone 3 wires xfade). Absolute overlays are
    silently ignored (milestone 6).

    The single FINAL_OUTPUT call site of _encoding_args in this module —
    locked by tests/test_encoder_policy.py.
    """
    # ── Input args (one -i per clip or hold, then silent audio at the end) ──
    input_args: list[str] = []
    filter_chains: list[str] = []
    slot_labels: list[str] = []

    for idx, inp in enumerate(spec.inputs):
        label = f"v{idx}"
        slot_labels.append(label)
        if inp.kind == "clip":
            if inp.clip_path is None or inp.start_s is None or inp.end_s is None:
                raise ValueError(
                    f"clip input {idx} missing required clip_path/start_s/end_s"
                )
            duration = inp.end_s - inp.start_s
            if duration <= 0:
                raise ValueError(
                    f"clip input {idx} has non-positive duration: {duration}s"
                )
            input_args += [
                "-ss", str(inp.start_s),
                "-t", str(duration),
                "-i", inp.clip_path,
            ]
            filter_chains.append(_per_clip_filter_chain(inp, idx, label))
        elif inp.kind == "color_hold":
            if inp.hold_s <= 0:
                raise ValueError(
                    f"color_hold input {idx} has non-positive duration: {inp.hold_s}s"
                )
            input_args += ["-f", "lavfi", "-i", _color_lavfi_arg(inp)]
            filter_chains.append(_per_hold_filter_chain(idx, label))
        else:
            raise ValueError(f"unknown SinglePassInput kind: {inp.kind!r}")

    # ── Concat all slots (hard-cut only — milestone 3 adds xfade) ──────────
    for trans in spec.transitions:
        if trans != "none":
            raise SinglePassUnsupportedError(
                f"xfade transition {trans!r} lands in milestone 3 — "
                f"only 'none' (hard-cut) is supported in milestone 2"
            )
    concat_inputs = "".join(f"[{lbl}]" for lbl in slot_labels)
    filter_chains.append(
        f"{concat_inputs}concat=n={len(slot_labels)}:v=1:a=0[vout]"
    )

    # ── Silent audio input (post-video so -ss on clips doesn't apply) ──────
    # _mix_template_audio (stage 7) stream-copies the video and re-mixes audio
    # from the template's music track; the silent body track satisfies the
    # AAC/44.1k/stereo contract in audio_layout.BODY_SLOT_AUDIO_OUT_ARGS so
    # the stream-copy is safe.
    silent_audio_idx = len(spec.inputs)
    input_args += SILENT_AUDIO_INPUT_ARGS

    # ── Final command ──────────────────────────────────────────────────────
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += input_args
    cmd += ["-filter_complex", ";".join(filter_chains)]
    cmd += [
        "-map", "[vout]",
        "-map", f"{silent_audio_idx}:a:0",
        "-shortest",
    ]
    # _encoding_args is the LOCKED final-output encoder contract. The single
    # call site in this module is tracked by tests/test_encoder_policy.py
    # (FINAL_OUTPUT_REQUIRED). preset="fast" + crf="18" matches every other
    # final-output encode in the codebase.
    cmd += _encoding_args(output_path, preset="fast", crf="18")
    return cmd


def run_single_pass(spec: SinglePassSpec, output_path: str) -> None:
    """Render the single-pass spec to output_path via FFmpeg subprocess.

    Milestone 2 scope: basic subprocess invocation + structured error path.
    The libass-missing retry lands in M8 (mirrors reframe_and_export:271);
    the xfade-fallback retry lands in M3 (mirrors _join_or_concat:2152).

    Raises SinglePassError on FFmpeg failure (mirrors ReframeError contract
    so callers can keep their existing except clauses).
    """
    cmd = build_single_pass_command(spec, output_path)
    log.info(
        "single_pass_start",
        inputs=len(spec.inputs),
        transitions=len([t for t in spec.transitions if t != "none"]),
        abs_pngs=len(spec.abs_pngs),
        abs_ass=len(spec.abs_ass_paths),
        output=output_path,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SinglePassError(
            f"single-pass ffmpeg timed out after {_SUBPROCESS_TIMEOUT_S}s "
            f"(output={output_path})"
        ) from exc

    if result.returncode != 0:
        full_stderr = result.stderr.decode(errors="replace")
        # FFmpeg prints a long version+config banner to stderr; the real error
        # lives in the lines that DON'T match those headers. Mirror the
        # extraction logic from reframe_and_export to surface the meaningful
        # part even under buffer truncation.
        meaningful = [
            ln for ln in full_stderr.splitlines()
            if ln.strip()
            and not ln.startswith(("ffmpeg version", "  built with", "  configuration:", "  lib"))
        ]
        tail = "\n".join(meaningful[-20:]) if meaningful else full_stderr[-4000:]
        raise SinglePassError(
            f"single-pass ffmpeg failed (rc={result.returncode}, "
            f"output={output_path}):\n{tail}"
        )

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise SinglePassError(
            f"single-pass ffmpeg returned success but output missing/empty: "
            f"{output_path}"
        )

    log.info(
        "single_pass_done",
        output=output_path,
        size_bytes=os.path.getsize(output_path),
    )
