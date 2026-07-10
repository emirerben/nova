"""Dedicated `talking_head` assembler for the format-aware edit engine (Lane C).

A talking-head edit has a different *shape* than the beat-synced montage that
`_assemble_clips` produces: ONE clip's full audio track (the "spine") carries the
whole video, while the OTHER clips' video is cut in as B-roll over the spine's
video at intervals. The spine speaker keeps talking underneath; the visuals
change.

This is a SEPARATE path on purpose (plan decision D2): it is NOT an extension of
the ~3000-line `_assemble_clips`. It owns its own one-shot timeline — reframe
every clip with the shared `reframe_and_export`, then a single FFmpeg pass lays
B-roll video over the spine video and muxes the spine audio. It reuses the
encoder policy (`_encoding_args`, preset="fast"), the CFR-normalised reframe
output, and the loudnorm/aresample tail from `intro_voiceover_mix`. The montage
and music beat-snap paths are untouched.

Assembly flow (plans/010 T6 added the silence-cut stage; SPINE ONLY — b-roll is
never cut, and every cut-stage failure falls OPEN to the uncut flow below):

    clip_paths + clip_metas ─▶ select_spine (speech_coverage + Lane A labels)
                                    │ spine + ordered b-roll
                                    ▼
    [silence_cut_fn set] spine pre-cap (≤ min(max(2×target, 120s), 300s))
                       → transcribe + silencedetect → CutPlan     (plans/010)
                                    │ keep_segments in the capped spine window
                                    ▼
    reframe spine (keep_segments + alternating punch-in when a plan applies)
    reframe each b-roll (unchanged — never cut)
                                    │ re-probe CUT spine → usable_s
                                    ▼
    schedule_broll(usable_s, broll, anchors=cut points in the CUT timeline)
                                    │ windows bias onto cuts to conceal them
                                    ▼
    build_talking_head_command → ONE FFmpeg composite + spine-audio loudnorm mux

Scope (Lane C): pure pipeline module. It is NOT wired into the generative
orchestrator dispatch — that is Lane D. Lane D resolves `edit_format` against the
footage, calls `assemble_talking_head`, and on `SpineExtractionError` degrades to
the montage archetype (+ `record_pipeline_event("assembly","archetype_fallback")`).
This module RAISES on a corrupt spine; the *degrade* lives in the caller.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from app.pipeline.probe import probe_video
from app.pipeline.reframe import _encoding_args, reframe_and_export, resolve_output_fit
from app.pipeline.silence_cut import KEEP_SEGMENTS_PUNCH_IN
from app.services.clip_speech import speech_coverage
from app.services.pipeline_trace import record_pipeline_event

log = structlog.get_logger()


def _safe_stem(clip_id: str) -> str:
    """A filesystem-safe filename stem from a clip_id.

    Generative clip_ids are Gemini upload ref names like ``files/t1eq2y5u9km4`` —
    the ``/`` would make ``f"{tmpdir}/spine_{clip_id}.mp4"`` point into a nonexistent
    subdirectory and ffmpeg's output open fails (the spine reframe then raises
    SpineExtractionError and the whole job degrades to montage). Collapse anything
    that isn't alphanumeric to ``_`` so the intermediate path is always writable.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", str(clip_id)).strip("_") or "clip"


# 9:16 vertical output (all generative output is portrait).
_ASPECT = "9:16"

# B-roll scheduling (fixed-cadence v1 — explicit over clever). Transcript-keyword
# / energy-driven placement is the eventual goal (Phase 2 spice); fixed cadence
# is correct and predictable until then.
_LEAD_IN_S = 1.5  # show the speaker alone before the first cutaway
_MAX_WINDOW_S = 3.0  # cap a single cutaway so the speaker keeps reappearing
_MIN_WINDOW_S = 0.5  # drop sub-0.5s windows (perceived as a flicker, not a cut)

# loudnorm target matches intro_voiceover_mix / TikTok-YouTube convention.
_OUTPUT_LUFS = -14.0
_OUTPUT_SAMPLE_RATE = 48000  # loudnorm upsamples to 192k; without this AAC picks
#                              96k → wrong-pitch playback on some browsers.

# Spine silence-cut pre-cap bounds (plans/010 14A / outside voice #9): detection
# + cutting operate on at most min(max(2×target, MIN), MAX) seconds of spine.
# MAX mirrors subtitled's 300s clip budget and keeps the 16 kHz mono analysis
# WAV (~9.6 MB at 300s) safely under whisper-1's 25 MB upload limit.
_SPINE_CUT_CAP_MIN_S = 120.0
_SPINE_CUT_CAP_MAX_S = 300.0


class TalkingHeadAssemblyError(Exception):
    """A talking-head render could not be produced."""


class SpineExtractionError(TalkingHeadAssemblyError):
    """The chosen spine clip is corrupt/unreadable — caller should degrade to montage."""


@dataclass(frozen=True)
class SpineSelection:
    spine_clip_id: str
    spine_score: float
    broll_clip_ids: list[str]  # ordered (input upload order, spine removed)


@dataclass(frozen=True)
class BrollSource:
    clip_id: str
    reframed_path: str
    source_dur_s: float


@dataclass(frozen=True)
class BrollWindow:
    clip_id: str
    reframed_path: str
    start_s: float
    end_s: float


def _content_type(meta: object) -> str:
    # getattr default keeps this forward-compatible with the ClipMeta dataclass
    # (orchestration carrier) and tolerant of a drifted/missing Lane A label.
    return str(getattr(meta, "content_type", "broll") or "broll")


def _audio_type(meta: object) -> str:
    return str(getattr(meta, "audio_type", "ambient") or "ambient")


def select_spine(
    clip_metas: list,
    clip_paths: dict[str, str],
    *,
    spine_clip_id: str | None = None,
    coverage_fn=speech_coverage,
) -> SpineSelection:
    """Pick the spine clip (whose audio carries the video) + ordered B-roll.

    Ranking: raw `speech_coverage` is the primary signal, nudged up by the Lane A
    `content_type=="talking_head"` / `audio_type∈{dialogue,voiceover}` labels so a
    clearly-labelled talking clip wins a near-tie against an incidentally-noisy
    one. An explicit `spine_clip_id` (user override) always wins.

    `coverage_fn` is injectable so unit tests need no ffmpeg.
    """
    ordered_ids = [str(m.clip_id) for m in clip_metas if str(m.clip_id) in clip_paths]
    if not ordered_ids:
        raise TalkingHeadAssemblyError("no clips with resolvable paths to assemble")

    meta_by_id = {str(m.clip_id): m for m in clip_metas}

    # Explicit override path — trust the caller, still record its coverage.
    if spine_clip_id is not None and spine_clip_id in clip_paths:
        chosen = spine_clip_id
        score = coverage_fn(clip_paths[chosen])
    else:

        def _score(cid: str) -> float:
            meta = meta_by_id[cid]
            s = coverage_fn(clip_paths[cid])
            if _content_type(meta) == "talking_head":
                s += 0.5
            if _audio_type(meta) in ("dialogue", "voiceover"):
                s += 0.25
            return s

        scored = {cid: _score(cid) for cid in ordered_ids}
        chosen = max(ordered_ids, key=lambda cid: scored[cid])
        score = scored[chosen]

    broll = [cid for cid in ordered_ids if cid != chosen]
    return SpineSelection(spine_clip_id=chosen, spine_score=score, broll_clip_ids=broll)


def spine_cut_cap_s(target_duration_s: float | None) -> float:
    """Spine silence-cut working-window cap (plans/010 14A / outside voice #9).

    ``min(max(target × 2, 120s), 300s)``. No target (None/0 → full spine today)
    falls back to the 300s maximum — subtitled's clip budget precedent. The cap
    bounds DETECTION + CUTTING only; when the cut stage does not apply, the
    spine renders full-length exactly as today (fail-open).
    """
    if not target_duration_s or float(target_duration_s) <= 0:
        return _SPINE_CUT_CAP_MAX_S
    return min(max(float(target_duration_s) * 2.0, _SPINE_CUT_CAP_MIN_S), _SPINE_CUT_CAP_MAX_S)


def _extract_capped_audio(src_path: str, cap_s: float, wav_path: str) -> None:
    """First ``cap_s`` seconds of audio as 16 kHz mono WAV (whisper-native).

    A precise ``-t`` re-encode, NOT a stream copy — keyframe-snapped copies can
    overshoot the cap and push keep_segments past the reframe window's bounds.
    Timestamps in the WAV are identical to the source clip's first ``cap_s``
    seconds, so the CutPlan applies directly to the original spine.
    """
    cmd = [
        "ffmpeg",
        "-i",
        src_path,
        "-t",
        f"{cap_s:.3f}",
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-y",
        wav_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-300:]
        raise TalkingHeadAssemblyError(f"spine pre-cap audio extraction failed: {stderr}")


def _spine_silence_cut_plan(
    *,
    spine_path: str,
    spine_dur: float,
    spine_probe: object | None,
    target_duration_s: float | None,
    tmpdir: str,
    silence_cut_fn: Callable[[str, float], dict[str, Any]],
    job_id: str | None,
) -> tuple[Any, float, int]:
    """Spine silence-cut ANALYSIS (plans/010 T6): gates → pre-cap → shared analysis.

    Returns ``(plan | None, analysis_duration_s, retake_span_count)``. Every
    gate and failure returns ``(None, …)`` — the caller renders today's uncut
    flow (fail-open; the stage may shorten the video, never fail the job).
    Same gates + events as the subtitled path: the ``has_audio`` probe gate
    fires BEFORE any ASR (eng review 3A — whisper on injected digital silence
    hallucinates plausible words).
    """
    has_audio = getattr(spine_probe, "has_audio", None)
    if has_audio is None:
        try:
            has_audio = bool(probe_video(spine_path).has_audio)
        except Exception as exc:  # noqa: BLE001 — probe failure ⇒ skip the stage, render uncut
            log.warning("talking_head_spine_cut_probe_failed", job_id=job_id, error=str(exc))
            return None, spine_dur, 0
    if not has_audio:
        record_pipeline_event(
            "silence_cut", "silence_cut_skipped_no_audio", {"variant_id": "talking_head"}
        )
        return None, spine_dur, 0

    # Spine pre-cap (14A): bound the working window BEFORE detection so the
    # analysis WAV stays under whisper-1's 25 MB limit. A spine shorter than
    # the cap analyzes the original clip directly — nothing changes (and the
    # per-job cache entry stays keyed by the clip's own path).
    cap = spine_cut_cap_s(target_duration_s)
    analysis_dur = min(float(spine_dur), cap)
    analysis_path = spine_path
    if float(spine_dur) > cap:
        analysis_path = f"{tmpdir}/spine_cut_analysis_{_safe_stem(spine_path)}.wav"
        try:
            _extract_capped_audio(spine_path, cap, analysis_path)
        except Exception as exc:  # noqa: BLE001 — pre-cap failure ⇒ uncut flow
            log.warning("talking_head_spine_precap_failed", job_id=job_id, error=str(exc)[:200])
            record_pipeline_event(
                "silence_cut", "silence_cut_analysis_failed", {"error": str(exc)[:200]}
            )
            return None, spine_dur, 0

    entry = silence_cut_fn(analysis_path, analysis_dur)
    if not isinstance(entry, dict) or entry.get("failed") or entry.get("plan") is None:
        return None, analysis_dur, 0
    return entry["plan"], analysis_dur, int(entry.get("retake_span_count") or 0)


def schedule_broll(
    usable_s: float, broll: list[BrollSource], anchors: list[float] | None = None
) -> list[BrollWindow]:
    """Lay B-roll cutaways across the spine at a fixed even cadence.

    Each B-roll clip gets one evenly-spaced window after a lead-in. Window length
    is clamped to the clip's own source duration, to its segment (no overlap), and
    to the remaining timeline. Windows that would start at/after `usable_s` are
    dropped. Returns [] for no B-roll or a non-positive timeline (→ spine-only).

    ``anchors`` (plans/010 T6, 12A / outside voice #7): optional silence-cut
    positions in the CUT spine timeline. Each window keeps today's slot and
    length but SLIDES within its slot to cover the nearest uncovered anchor —
    a cutaway playing over a jump cut conceals it. Cadence, lead-in, min/max
    window and window count are unchanged; anchors no window can legally cover
    are simply left uncovered (no forcing). ``None``/``[]`` ⇒ byte-identical
    to the anchor-less schedule.
    """
    if not broll or usable_s <= 0:
        return []
    span = usable_s - _LEAD_IN_S
    if span <= _MIN_WINDOW_S:
        return []  # spine too short to cut away into — show the speaker alone

    # Coverable anchors only: a cut inside the lead-in (windows can't start
    # earlier) or at/after the trim boundary has no legal covering window.
    pending = sorted(float(a) for a in (anchors or []) if _LEAD_IN_S <= float(a) < usable_s)

    n = len(broll)
    seg = span / n
    windows: list[BrollWindow] = []
    for i, b in enumerate(broll):
        slot_lo = _LEAD_IN_S + i * seg
        if slot_lo >= usable_s:
            break
        length = min(_MAX_WINDOW_S, b.source_dur_s, seg, usable_s - slot_lo)
        if length < _MIN_WINDOW_S:
            continue
        start = slot_lo  # anchor-less cadence position (regression-pinned)
        if pending:
            # The window may slide within its own slot: [slot_lo, max_start].
            # length ≤ seg and ≤ usable_s − slot_lo, so max_start ≥ slot_lo.
            max_start = min(slot_lo + seg, usable_s) - length
            coverable = [a for a in pending if slot_lo <= a <= max_start + length]
            if coverable:
                target = coverable[0]  # nearest uncovered anchor (all sit ≥ slot_lo)
                # Center the anchor inside the window when the slot allows;
                # clamp into [max(slot_lo, target−length), min(max_start, target)]
                # which is never empty and always keeps start ≤ target ≤ end.
                lo_f = max(slot_lo, target - length)
                hi_f = min(max_start, target)
                start = min(max(target - length / 2.0, lo_f), hi_f)
        start_r = round(start, 3)
        end_r = round(start + length, 3)
        windows.append(
            BrollWindow(
                clip_id=b.clip_id,
                reframed_path=b.reframed_path,
                start_s=start_r,
                end_s=end_r,
            )
        )
        if pending:
            # Anything this window covers (the target + incidentals) is done.
            pending = [a for a in pending if not (start_r - 1e-6 <= a <= end_r + 1e-6)]
    return windows


def build_talking_head_command(
    spine_reframed_path: str,
    windows: list[BrollWindow],
    output_path: str,
    *,
    usable_s: float,
) -> list[str]:
    """Build the single FFmpeg composite+mux command (pure — no I/O).

    Base = spine video trimmed to `usable_s`. Each B-roll window is overlaid with
    its PTS shifted to the window start (`enable=` only gates *visibility*; without
    the shift an early window would show nothing). `eof_action=pass` means a B-roll
    shorter than its window lets the spine video show through rather than freezing
    the last frame. Audio is the spine's own track, loudnorm'd + resampled to 48k.

    The `_encoding_args(..., preset="fast")` call sits directly in this function so
    the encoder-policy AST gate (tests/test_encoder_policy.py) attributes it here.
    """
    inputs = ["-i", spine_reframed_path]
    for w in windows:
        inputs += ["-i", w.reframed_path]

    filter_parts: list[str] = [f"[0:v]trim=0:{usable_s:.3f},setpts=PTS-STARTPTS,settb=AVTB[base]"]
    prev = "[base]"
    for k, w in enumerate(windows):
        in_idx = k + 1  # input 0 is the spine
        shifted = f"b{k}"
        out = f"ov{k}"
        # PTS shift so the B-roll plays from its own start during the window.
        filter_parts.append(
            f"[{in_idx}:v]setpts=PTS-STARTPTS+{w.start_s:.3f}/TB,settb=AVTB[{shifted}]"
        )
        filter_parts.append(
            f"{prev}[{shifted}]overlay=0:0:"
            f"enable='between(t,{w.start_s:.3f},{w.end_s:.3f})':eof_action=pass[{out}]"
        )
        prev = f"[{out}]"

    # Spine audio: trim to the same usable window, normalise loudness, force 48k.
    filter_parts.append(
        f"[0:a]atrim=0:{usable_s:.3f},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={_OUTPUT_LUFS}:TP=-1.5:LRA=11,"
        f"aresample={_OUTPUT_SAMPLE_RATE}[outa]"
    )

    cmd = [
        "ffmpeg",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        prev,
        "-map",
        "[outa]",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        # Final-output encode → preset="fast" (banding policy). include_audio=False
        # because the audio is built by the filtergraph above, not the slot layout.
        *_encoding_args(output_path, preset="fast", include_audio=False),
    ]
    return cmd


def assemble_talking_head(
    *,
    clip_paths: dict[str, str],
    clip_metas: list,
    probe_map: dict | None = None,
    target_duration_s: float | None = None,
    output_path: str,
    tmpdir: str,
    job_id: str | None = None,
    spine_clip_id: str | None = None,
    landscape_fit: str = "fill",
    silence_cut_fn: Callable[[str, float], dict[str, Any]] | None = None,
    silence_cut_out: dict[str, Any] | None = None,
) -> str:
    """Render a talking-head edit: spine audio under B-roll cutaways.

    Args:
        clip_paths: clip_id → local file path for every uploaded clip.
        clip_metas: per-clip metadata (needs `.clip_id`; reads optional Lane A
            `.content_type` / `.audio_type` labels via getattr).
        probe_map: optional clip_id → VideoProbe to avoid re-probing.
        target_duration_s: cap the output to this length (None → full spine).
        output_path: where to write the muxed mp4.
        tmpdir: scratch dir for reframed intermediates.
        job_id: for log context; pipeline_trace events are bound by the caller's
            `pipeline_trace_for(job_id)` block (Lane D), no-op without one.
        spine_clip_id: explicit spine override (user "set as spine" action).
        silence_cut_fn: optional ``(analysis_path, duration_s) → analysis entry``
            hook (the caller wraps ``generative_build._silence_cut_analysis`` so
            the per-job cache + retake wiring stay in the tasks layer). When set,
            the SPINE gets the plans/010 silence/filler/retake cut: pre-cap →
            analyze → ``reframe(keep_segments + punch-in)`` → re-probed
            ``usable_s`` → b-roll windows anchored onto the cut points. B-roll
            is never cut. None (the kill-switch/gated path) ⇒ byte-identical to
            the pre-T6 flow; every cut-stage failure also falls open to it.
        silence_cut_out: optional dict the assembler fills with
            ``{"summary": {removed, time_saved_s, version}}`` when a non-bailout
            plan was produced — the caller persists it on the variant.

    Returns:
        output_path on success.

    Raises:
        SpineExtractionError: spine clip corrupt/unreadable — caller degrades to montage.
        TalkingHeadAssemblyError: the composite render failed.
    """
    selection = select_spine(clip_metas, clip_paths, spine_clip_id=spine_clip_id)
    record_pipeline_event("assembly", "archetype_selected", {"archetype": "talking_head"})
    record_pipeline_event(
        "assembly",
        "spine_selected",
        {"clip_id": selection.spine_clip_id, "speech_coverage": round(selection.spine_score, 3)},
    )
    log.info(
        "talking_head_spine",
        job_id=job_id,
        spine=selection.spine_clip_id,
        score=round(selection.spine_score, 3),
        broll_count=len(selection.broll_clip_ids),
    )

    # ── Spine: probe + reframe. Failure here is fatal (degrade signal). ──
    spine_path = clip_paths[selection.spine_clip_id]
    spine_dur = _duration(spine_path, probe_map, selection.spine_clip_id)
    if spine_dur <= 0:
        raise SpineExtractionError(f"spine clip {selection.spine_clip_id} has no usable duration")
    usable_s = min(spine_dur, target_duration_s) if target_duration_s else spine_dur

    # ── Spine silence-cut analysis (plans/010 T6) — SPINE ONLY, fail-open. ──
    sc_plan = None
    sc_analysis_dur = spine_dur
    sc_retake_count = 0
    sc_apply_error: str | None = None
    if silence_cut_fn is not None:
        sc_plan, sc_analysis_dur, sc_retake_count = _spine_silence_cut_plan(
            spine_path=spine_path,
            spine_dur=spine_dur,
            # probe_map is path-keyed in prod (_probe_clips); the clip_id lookup
            # is a defensive mirror of _duration's keying.
            spine_probe=(probe_map or {}).get(spine_path)
            or (probe_map or {}).get(selection.spine_clip_id),
            target_duration_s=target_duration_s,
            tmpdir=tmpdir,
            silence_cut_fn=silence_cut_fn,
            job_id=job_id,
        )
    sc_apply = (
        sc_plan is not None
        and sc_plan.bailout_reason is None
        and bool(sc_plan.removed)
        and bool(sc_plan.keep_segments)
    )

    spine_reframed = f"{tmpdir}/spine_{_safe_stem(selection.spine_clip_id)}.mp4"
    # probe_map is keyed by local file path (from _probe_clips), not by clip_id.
    spine_probe = (probe_map or {}).get(spine_path)

    def _reframe_spine(end_s: float, cut_kwargs: dict[str, Any]) -> None:
        reframe_and_export(
            spine_path,
            0.0,
            end_s,
            _ASPECT,
            None,
            spine_reframed,
            output_fit=resolve_output_fit(spine_probe, landscape_fit=landscape_fit),
            **cut_kwargs,
        )

    try:
        if sc_apply:
            # The cut executes INSIDE the reframe (13A): keep_segments live in
            # the pre-capped window [0, sc_analysis_dur]; the punch factor is
            # silence_cut's user-approved constant — never a literal here.
            _reframe_spine(
                sc_analysis_dur,
                {
                    "keep_segments": sc_plan.keep_segments,
                    "keep_segments_punch_in": KEEP_SEGMENTS_PUNCH_IN,
                },
            )
        else:
            _reframe_spine(spine_dur, {})
    except Exception as exc:  # noqa: BLE001 — ReframeError or any probe/IO failure degrades
        if not sc_apply:
            raise SpineExtractionError(
                f"failed to reframe spine clip {selection.spine_clip_id}: {exc}"
            ) from exc
        # ffmpeg CUT failure falls back to the uncut spine (plans/010 failure
        # table) — the cut stage may never turn a renderable job into a montage
        # degrade. Only an UNCUT failure is a real spine-extraction signal.
        sc_apply = False
        sc_apply_error = str(exc)[:200]
        log.warning("talking_head_spine_cut_apply_failed", job_id=job_id, error=sc_apply_error)
        try:
            _reframe_spine(spine_dur, {})
        except Exception as exc2:  # noqa: BLE001
            raise SpineExtractionError(
                f"failed to reframe spine clip {selection.spine_clip_id}: {exc2}"
            ) from exc2

    # ── Cut applied: usable_s derives from the CUT spine (re-probe; plan
    # arithmetic is the fallback), and each removal becomes a b-roll anchor at
    # its position in the cut timeline (start minus removed-time-before-it). ──
    anchors: list[float] | None = None
    if sc_apply:
        kept_s = sum(b - a for a, b in sc_plan.keep_segments)
        try:
            cut_dur = float(probe_video(spine_reframed).duration_s) or kept_s
        except Exception as exc:  # noqa: BLE001 — fall back to exact plan arithmetic
            log.warning("talking_head_cut_spine_probe_failed", job_id=job_id, error=str(exc))
            cut_dur = kept_s
        usable_s = min(cut_dur, target_duration_s) if target_duration_s else cut_dur
        anchors = []
        removed_before = 0.0
        for r in sc_plan.removed:
            anchors.append(round(r.start_s - removed_before, 3))
            removed_before += r.end_s - r.start_s

    if sc_plan is not None and sc_plan.bailout_reason is None:
        # Same persistence contract as the subtitled path: non-bailout plans
        # (zero-removal included — "nothing to cut" is information) emit the
        # plan event; bailouts are event-only upstream in the analysis. A plan
        # whose APPLY failed is event-only too (the video shipped uncut, so a
        # persisted removed[] would lie to the admin cut-plan viewer).
        reasons: dict[str, int] = {}
        for r in sc_plan.removed:
            reasons[r.reason] = reasons.get(r.reason, 0) + 1
        payload: dict[str, Any] = {
            "variant_id": "talking_head",
            "removed_count": len(sc_plan.removed),
            "time_saved_s": round(sc_plan.time_saved_s, 3),
            "reasons": reasons,
            "retake_spans": sc_retake_count,
            "applied": sc_apply,
            "cut_reused": False,
            "broll_anchors": len(anchors or []),
        }
        if sc_apply_error is not None:
            payload["apply_error"] = sc_apply_error
        record_pipeline_event("silence_cut", "silence_cut_plan", payload)
        if sc_apply_error is None and silence_cut_out is not None:
            silence_cut_out["summary"] = {
                "removed": [
                    {"start_s": round(r.start_s, 3), "end_s": round(r.end_s, 3), "reason": r.reason}
                    for r in sc_plan.removed
                ],
                "time_saved_s": round(sc_plan.time_saved_s, 3),
                "version": sc_plan.version,
            }

    # ── B-roll: reframe each; a failure drops that clip (best-effort). ──
    broll_sources: list[BrollSource] = []
    for i, cid in enumerate(selection.broll_clip_ids):
        path = clip_paths[cid]
        dur = _duration(path, probe_map, cid)
        if dur <= 0:
            log.warning("talking_head_broll_no_duration", job_id=job_id, clip_id=cid)
            continue
        # Index-prefixed so two clip_ids that sanitize to the same stem can't collide.
        reframed = f"{tmpdir}/broll_{i}_{_safe_stem(cid)}.mp4"
        broll_probe = (probe_map or {}).get(path)  # probe_map keyed by path, not clip_id
        try:
            reframe_and_export(
                path,
                0.0,
                dur,
                _ASPECT,
                None,
                reframed,
                output_fit=resolve_output_fit(broll_probe, landscape_fit=landscape_fit),
            )
        except Exception as exc:  # noqa: BLE001 — drop this window, keep the job
            log.warning(
                "talking_head_broll_reframe_failed", job_id=job_id, clip_id=cid, error=str(exc)
            )
            continue
        broll_sources.append(BrollSource(clip_id=cid, reframed_path=reframed, source_dur_s=dur))

    windows = schedule_broll(usable_s, broll_sources, anchors=anchors)
    record_pipeline_event(
        "assembly",
        "broll_scheduled",
        {
            "count": len(windows),
            "windows": [
                {"clip_id": w.clip_id, "start_s": w.start_s, "end_s": w.end_s} for w in windows
            ],
        },
    )

    cmd = build_talking_head_command(spine_reframed, windows, output_path, usable_s=usable_s)
    result = subprocess.run(cmd, capture_output=True, timeout=1200, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-800:]
        log.warning("talking_head_ffmpeg_failed", job_id=job_id, stderr=stderr)
        raise TalkingHeadAssemblyError(f"talking-head composite failed: {stderr}")

    log.info("talking_head_done", job_id=job_id, output=output_path, broll_windows=len(windows))
    return output_path


def _duration(path: str, probe_map: dict | None, clip_id: str) -> float:
    """Clip duration in seconds from probe_map, else a fresh probe. 0.0 on failure."""
    if probe_map and clip_id in probe_map:
        probe = probe_map[clip_id]
        dur = getattr(probe, "duration_s", 0.0)
        if dur:
            return float(dur)
    try:
        return float(probe_video(path).duration_s)
    except Exception as exc:  # noqa: BLE001
        log.warning("talking_head_probe_failed", clip_id=clip_id, error=str(exc))
        return 0.0
