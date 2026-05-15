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

Milestone status (May 2026):
  M2 shipped — clips + color holds + hard-cut concat
  M3 shipped — xfade transitions in filter_complex
  M6 shipped — absolute-timestamp PNG and ASS overlays after concat/xfade
  M4 OPEN  — curtain-close PNG-bar overlay. Templates using
             curtain-close still raise SinglePassUnsupportedError at the
             spec-build gate and fall through to multi-pass.
  M5 OPEN  — slot-relative pre-burn PNGs. Same gate.

Per-template savings (vs current multi-pass), once env flag + per-template
allow-list both flip:
  Hard-cut + no overlays   → ~0 saved (multi-pass already stream-copy concats)
  Hard-cut + abs overlays  → ~1 final encode saved (M6)
  Xfade + no overlays      → ~1 final encode saved (M3)
  Xfade + abs overlays     → ~2 final encodes saved (M3 + M6)
  Curtain-close (any combo) → 0 saved until M4 lands

The 30-50% wall-clock projection in early plan docs assumed M4+M3+M6
together. Real savings on this branch are the M3 + M6 share only.
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
from app.pipeline.transitions import (
    DEFAULT_TRANSITION_DURATION_S,
    XFADE_MAP,
)

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
    # Normalize timebase + PTS so concat and xfade nodes downstream see
    # matching streams. Without this, raw clip inputs land in xfade with
    # the source mp4's timebase (e.g. 1/15360) while concat outputs use
    # AVTB (1/1000000) — xfade rejects the mismatch with
    #   "First input link main timebase do not match the corresponding
    #    second input link xfade timebase"
    # caught empirically by tests/quality/single_pass_parity.py against
    # dimples-passport on 2026-05-15. settb=AVTB rewrites the stream
    # timebase to the canonical 1/1000000; setpts=PTS-STARTPTS zeroes the
    # start so xfade offset math doesn't drift on clips that were seeked
    # mid-file via -ss.
    vf_parts.append("setpts=PTS-STARTPTS")
    vf_parts.append("settb=AVTB")
    chain = ",".join(vf_parts)
    return f"[{input_idx}:v]{chain}[{output_label}]"


def _per_hold_filter_chain(input_idx: int, output_label: str) -> str:
    """Build the filter graph fragment for one color hold input.

    lavfi `color=` already emits the target resolution and fps. Force yuv420p
    so it joins the concat with the same pixel format as the clip chains.
    Normalize timebase + PTS for the same reason `_per_clip_filter_chain`
    does — concat and xfade downstream require matching timebases across
    every input stream.
    """
    return (
        f"[{input_idx}:v]format=yuv420p,setpts=PTS-STARTPTS,settb=AVTB"
        f"[{output_label}]"
    )


def _compute_output_durations(spec: SinglePassSpec) -> list[float]:
    """Per-input output duration after speed factor / hold time.

    For xfade offset math we need the OUTPUT timeline duration of each
    segment, not the source duration. Clips at speed_factor=2.0 occupy
    half their source duration in the output; color holds occupy their
    hold_s verbatim.
    """
    durations: list[float] = []
    for inp in spec.inputs:
        if inp.kind == "clip":
            assert inp.start_s is not None and inp.end_s is not None  # noqa: S101
            durations.append((inp.end_s - inp.start_s) / inp.speed_factor)
        else:
            durations.append(inp.hold_s)
    return durations


def _group_by_concat_runs(transitions: list[str]) -> list[list[int]]:
    """Group adjacent slot indices into concat sub-batches.

    Each group is a maximal run of slots joined by ``"none"`` transitions
    (hard-cut). Groups are separated by visual transitions, which become
    xfade boundaries between the groups in :func:`_build_xfade_chain`.

    Example: ``transitions=["none", "crossfade", "none"]`` (4 slots) →
    ``[[0, 1], [2, 3]]`` — slots 0+1 concat into the first group, slots 2+3
    concat into the second, and the crossfade boundary bridges them.

    Why mirror the multi-pass grouping: ``transitions.py:join_with_transitions``
    rejects ``"none"`` precisely because chaining many low-duration xfade
    filters caused FFmpeg to drop frames and truncate long outputs to 3-4s
    on the 17-slot Dimples Passport recipe. Concat-then-xfade keeps the
    xfade-filter count bounded by the number of *visual* transitions,
    independent of slot count.
    """
    groups: list[list[int]] = [[0]]
    for i, trans in enumerate(transitions):
        if trans == "none":
            groups[-1].append(i + 1)
        else:
            groups.append([i + 1])
    return groups


def _build_xfade_chain(
    slot_labels: list[str],
    durations: list[float],
    transitions: list[str],
    final_label: str = "vout",
) -> tuple[list[str], str]:
    """Build the concat-then-xfade filter graph fragments.

    Returns ``(fragments, final_output_label)``. The fragments are joined
    by ``;`` and appended to ``filter_complex``. ``final_output_label`` is
    the value passed in ``final_label`` (default ``"vout"``) — M6 callers
    pass ``"base"`` so the absolute-overlay chain can consume the joined
    video and produce the actual ``[vout]``.

    Algorithm mirrors ``transitions._build_xfade_filter`` but operates on
    per-group durations (each group is a concat of consecutive ``"none"``
    boundaries) rather than raw per-slot durations.
    """
    groups = _group_by_concat_runs(transitions)
    fragments: list[str] = []
    group_labels: list[str] = []
    group_durations: list[float] = []

    # Phase 1: concat each group → labelled stream.
    for gi, group in enumerate(groups):
        if len(group) == 1:
            # No concat needed; reuse the slot label directly.
            group_labels.append(slot_labels[group[0]])
            group_durations.append(durations[group[0]])
        else:
            inputs_str = "".join(f"[{slot_labels[idx]}]" for idx in group)
            out_lbl = f"g{gi}"
            fragments.append(
                f"{inputs_str}concat=n={len(group)}:v=1:a=0[{out_lbl}]"
            )
            group_labels.append(out_lbl)
            group_durations.append(sum(durations[idx] for idx in group))

    # Phase 2: xfade between groups. The non-"none" transitions in the
    # caller's list are exactly the visual boundaries between groups; their
    # order in `transitions` matches the order of the gaps between
    # consecutive groups in `groups`.
    visual_transitions = [t for t in transitions if t != "none"]
    if not visual_transitions:
        # Pure-"none" inputs route through the M2 concat path; this helper
        # should not have been called. Raise rather than silently emit a
        # degenerate filter that callers can't map.
        raise ValueError(
            "_build_xfade_chain called with all-'none' transitions; "
            "use the M2 concat path instead"
        )

    cumulative_dur = group_durations[0]
    cumulative_trans = 0.0
    current_label = group_labels[0]

    for i, trans in enumerate(visual_transitions):
        # xfade overlap duration is bounded by 30% of the shorter adjacent
        # group; clip to the default 0.3s. Mirrors
        # transitions._build_xfade_filter so visual results match multi-pass.
        max_dur = min(group_durations[i], group_durations[i + 1]) * 0.3
        trans_dur = min(DEFAULT_TRANSITION_DURATION_S, max_dur)
        offset = max(0.0, cumulative_dur - cumulative_trans - trans_dur)

        xfade_type = XFADE_MAP.get(trans, "fade")
        next_label = group_labels[i + 1]
        out_lbl = "vout" if i == len(visual_transitions) - 1 else f"xf{i}"
        fragments.append(
            f"[{current_label}][{next_label}]"
            f"xfade=transition={xfade_type}"
            f":duration={trans_dur:.3f}:offset={offset:.3f}"
            f"[{out_lbl}]"
        )

        current_label = out_lbl
        cumulative_dur += group_durations[i + 1]
        cumulative_trans += trans_dur

    return fragments, final_label


def _build_abs_overlay_chain(
    abs_pngs: list[dict],
    abs_ass_paths: list[str],
    fonts_dir: str,
    base_label: str,
    png_input_start_idx: int,
) -> tuple[list[str], str]:
    """Chain absolute-timestamp PNG overlays + ASS subtitle layers.

    Mirrors ``_burn_text_overlays`` in template_orchestrate.py:3155 so the
    pixels at every overlay timestamp match multi-pass byte-for-byte (same
    overlay filter, same ``enable='between(t,...)'`` clamps, same
    ``subtitles=`` + ``fontsdir=`` for libass).

    Returns ``(fragments, final_label)``. ``final_label`` is the label
    consumed by the ``-map`` in the caller — ``"vout"`` if any overlays
    exist, otherwise the unchanged ``base_label`` (caller short-circuits
    on the empty case so it can route ``[base_label]`` straight to map).

    Args:
        abs_pngs: list of dicts with ``png_path``, ``start_s``, ``end_s``.
            Order is preserved; later PNGs paint OVER earlier ones (FFmpeg
            ``overlay`` filter is destructive on the previous label).
        abs_ass_paths: ASS file paths applied AFTER the PNG layer. libass
            absolute timings live inside each Dialogue line; no enable=
            clamp needed on the filter.
        fonts_dir: directory containing the font bundle for ``fontsdir=``.
        base_label: filter graph label flowing into the chain.
        png_input_start_idx: ffmpeg input index of the first PNG ``-i``
            argument. PNGs are appended to argv AFTER the clip/hold inputs
            and BEFORE the silent audio, so the caller computes this as
            ``len(spec.inputs)`` and the silent audio shifts to
            ``len(spec.inputs) + len(spec.abs_pngs)``.

    Filter graph shape (2 PNGs + 1 ASS):
        [base_label][N:v]overlay=0:0:enable=...[png0];
        [png0][N+1:v]overlay=0:0:enable=...[png1];
        [png1]subtitles='...':fontsdir='...'[vout]

    The escape on ``:`` and ``'`` mirrors _burn_text_overlays
    (template_orchestrate.py:3231-3236) — required on macOS/Linux because
    the subtitles filter parses these as option separators.
    """
    if not abs_pngs and not abs_ass_paths:
        return [], base_label

    total_nodes = len(abs_pngs) + len(abs_ass_paths)
    fragments: list[str] = []
    prev = base_label
    node_count = 0

    for i, cfg in enumerate(abs_pngs):
        node_count += 1
        out_lbl = "vout" if node_count == total_nodes else f"png{i}"
        input_idx = png_input_start_idx + i
        fragments.append(
            f"[{prev}][{input_idx}:v]overlay=0:0"
            f":enable='between(t,{cfg['start_s']:.3f},{cfg['end_s']:.3f})'"
            f"[{out_lbl}]"
        )
        prev = out_lbl

    fonts_escaped = fonts_dir.replace(":", "\\:").replace("'", "\\'")
    for i, ass_path in enumerate(abs_ass_paths):
        node_count += 1
        out_lbl = "vout" if node_count == total_nodes else f"ass{i}"
        ass_filter_path = ass_path.replace(":", "\\:").replace("'", "\\'")
        fragments.append(
            f"[{prev}]subtitles='{ass_filter_path}':fontsdir='{fonts_escaped}'[{out_lbl}]"
        )
        prev = out_lbl

    return fragments, "vout"


def build_single_pass_command(
    spec: SinglePassSpec,
    output_path: str,
    base_output_path: str | None = None,
) -> list[str]:
    """Construct the single-pass ffmpeg command from a SinglePassSpec.

    Pure function — no I/O, no subprocess. Returns the argv list for
    subprocess.run. Unit-testable.

    Through M6: hard-cut concat, xfade transitions, and absolute overlays
    all supported. Curtain-close, barn-door-open, and pre-burn PNGs still
    raise SinglePassUnsupportedError at spec-build time.

    When ``base_output_path`` is set AND ``spec`` carries absolute
    overlays, the command emits TWO outputs sharing the same filter graph
    (overlay-free at [base], overlay-burned at [vout]). When set but no
    overlays exist, [base] doesn't exist in the filter graph — callers
    should ``shutil.copy2`` post-process to materialize base_output_path.

    The FINAL_OUTPUT call sites of _encoding_args in this module are
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

    # ── Join all slots ─────────────────────────────────────────────────────
    # Three paths now:
    #   M2 path  (all "none", no overlays)    → single concat=n=N → [vout]
    #   M3 path  (any non-"none" transition)  → concat-then-xfade chain
    #   M6 path  (absolute overlays exist)    → join emits [base], then
    #                                           overlay chain produces [vout]
    #
    # The concat-grouping rule (consecutive "none" → concat sub-batch)
    # protects against the multi-pass bug class where long chains of
    # duration=0.001 xfade filters truncate output (transitions.py:74
    # spells out the 17-slot Dimples Passport case).
    has_visual_transition = any(t != "none" for t in spec.transitions)
    has_overlays = bool(spec.abs_pngs or spec.abs_ass_paths)
    join_out_label = "base" if has_overlays else "vout"

    if has_visual_transition:
        durations = _compute_output_durations(spec)
        xfade_fragments, _ = _build_xfade_chain(
            slot_labels, durations, spec.transitions, final_label=join_out_label
        )
        filter_chains.extend(xfade_fragments)
    else:
        concat_inputs = "".join(f"[{lbl}]" for lbl in slot_labels)
        filter_chains.append(
            f"{concat_inputs}concat=n={len(slot_labels)}:v=1:a=0[{join_out_label}]"
        )

    # ── Absolute overlays (M6) ─────────────────────────────────────────────
    # PNG inputs are appended to argv between the video inputs and the
    # silent audio so the silent audio index shifts by len(abs_pngs). The
    # filter graph consumes PNG inputs starting at index len(spec.inputs).
    png_input_start_idx = len(spec.inputs)
    for cfg in spec.abs_pngs:
        input_args += ["-i", cfg["png_path"]]

    if has_overlays:
        overlay_fragments, _ = _build_abs_overlay_chain(
            spec.abs_pngs,
            spec.abs_ass_paths,
            spec.fonts_dir,
            base_label=join_out_label,
            png_input_start_idx=png_input_start_idx,
        )
        filter_chains.extend(overlay_fragments)

    # ── Silent audio input (post-video so -ss on clips doesn't apply) ──────
    # _mix_template_audio (stage 7) stream-copies the video and re-mixes audio
    # from the template's music track; the silent body track satisfies the
    # AAC/44.1k/stereo contract in audio_layout.BODY_SLOT_AUDIO_OUT_ARGS so
    # the stream-copy is safe.
    silent_audio_idx = len(spec.inputs) + len(spec.abs_pngs)
    input_args += SILENT_AUDIO_INPUT_ARGS

    # ── Final command ──────────────────────────────────────────────────────
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += input_args
    cmd += ["-filter_complex", ";".join(filter_chains)]
    # _encoding_args is the LOCKED final-output encoder contract. The
    # call sites in this module are tracked by tests/test_encoder_policy.py
    # (FINAL_OUTPUT_REQUIRED). preset="fast" + crf="18" matches every
    # other final-output encode in the codebase.
    #
    # base_output_path branch: when set AND overlays exist, emit a SECOND
    # output mapped from [base] sharing the same ffmpeg process. The
    # filter graph is evaluated once up to the split; libx264 encodes
    # twice (once per output). Cheaper than running two `run_single_pass`
    # calls (one decode instead of two; one ffmpeg process startup
    # instead of two). When no overlays exist, [base] doesn't exist —
    # callers fall back to a post-process file copy.
    if base_output_path and has_overlays:
        cmd += [
            "-map", "[base]",
            "-map", f"{silent_audio_idx}:a:0",
            "-shortest",
        ]
        cmd += _encoding_args(base_output_path, preset="fast", crf="18")
    cmd += [
        "-map", "[vout]",
        "-map", f"{silent_audio_idx}:a:0",
        "-shortest",
    ]
    cmd += _encoding_args(output_path, preset="fast", crf="18")
    return cmd


def run_single_pass(
    spec: SinglePassSpec,
    output_path: str,
    base_output_path: str | None = None,
) -> None:
    """Render the single-pass spec to output_path via FFmpeg subprocess.

    When ``base_output_path`` is set AND the spec has absolute overlays,
    the ffmpeg invocation emits TWO outputs in a single process — the
    overlay-free render at ``[base]`` and the overlay-burned render at
    ``[vout]``. Shares the decode + filter graph evaluation, encodes
    twice. Avoids the regression where M6 doubled encode cost for
    overlay templates by calling run_single_pass twice.

    When ``base_output_path`` is set but the spec has no overlays,
    ``base_output_path == output_path`` semantically; the caller is
    expected to ``shutil.copy2`` after this returns.

    Raises SinglePassError on FFmpeg failure (mirrors ReframeError contract
    so callers can keep their existing except clauses).
    """
    cmd = build_single_pass_command(spec, output_path, base_output_path=base_output_path)
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
