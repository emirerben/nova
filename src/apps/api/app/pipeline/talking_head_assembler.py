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

Scope (Lane C): pure pipeline module. It is NOT wired into the generative
orchestrator dispatch — that is Lane D. Lane D resolves `edit_format` against the
footage, calls `assemble_talking_head`, and on `SpineExtractionError` degrades to
the montage archetype (+ `record_pipeline_event("assembly","archetype_fallback")`).
This module RAISES on a corrupt spine; the *degrade* lives in the caller.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import structlog

from app.pipeline.probe import probe_video
from app.pipeline.reframe import _encoding_args, reframe_and_export
from app.services.clip_speech import speech_coverage
from app.services.pipeline_trace import record_pipeline_event

log = structlog.get_logger()

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


def schedule_broll(usable_s: float, broll: list[BrollSource]) -> list[BrollWindow]:
    """Lay B-roll cutaways across the spine at a fixed even cadence.

    Each B-roll clip gets one evenly-spaced window after a lead-in. Window length
    is clamped to the clip's own source duration, to its segment (no overlap), and
    to the remaining timeline. Windows that would start at/after `usable_s` are
    dropped. Returns [] for no B-roll or a non-positive timeline (→ spine-only).
    """
    if not broll or usable_s <= 0:
        return []
    span = usable_s - _LEAD_IN_S
    if span <= _MIN_WINDOW_S:
        return []  # spine too short to cut away into — show the speaker alone

    n = len(broll)
    seg = span / n
    windows: list[BrollWindow] = []
    for i, b in enumerate(broll):
        start = _LEAD_IN_S + i * seg
        if start >= usable_s:
            break
        length = min(_MAX_WINDOW_S, b.source_dur_s, seg, usable_s - start)
        if length < _MIN_WINDOW_S:
            continue
        windows.append(
            BrollWindow(
                clip_id=b.clip_id,
                reframed_path=b.reframed_path,
                start_s=round(start, 3),
                end_s=round(start + length, 3),
            )
        )
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

    spine_reframed = f"{tmpdir}/spine_{selection.spine_clip_id}.mp4"
    try:
        reframe_and_export(spine_path, 0.0, spine_dur, _ASPECT, None, spine_reframed)
    except Exception as exc:  # noqa: BLE001 — ReframeError or any probe/IO failure degrades
        raise SpineExtractionError(
            f"failed to reframe spine clip {selection.spine_clip_id}: {exc}"
        ) from exc

    # ── B-roll: reframe each; a failure drops that clip (best-effort). ──
    broll_sources: list[BrollSource] = []
    for cid in selection.broll_clip_ids:
        path = clip_paths[cid]
        dur = _duration(path, probe_map, cid)
        if dur <= 0:
            log.warning("talking_head_broll_no_duration", job_id=job_id, clip_id=cid)
            continue
        reframed = f"{tmpdir}/broll_{cid}.mp4"
        try:
            reframe_and_export(path, 0.0, dur, _ASPECT, None, reframed)
        except Exception as exc:  # noqa: BLE001 — drop this window, keep the job
            log.warning(
                "talking_head_broll_reframe_failed", job_id=job_id, clip_id=cid, error=str(exc)
            )
            continue
        broll_sources.append(BrollSource(clip_id=cid, reframed_path=reframed, source_dur_s=dur))

    windows = schedule_broll(usable_s, broll_sources)
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
