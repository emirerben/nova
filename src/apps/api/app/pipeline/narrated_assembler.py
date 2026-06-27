"""Assemble narrated walkthrough clips against aligned voiceover timings.

Pipeline
--------

    step_timings (voiceover-time)        clip_assignments
            │                                   │
            ▼                                   ▼
    ┌──────────────────────────────────────────────────┐
    │  per step: _fit_clip_segment(clip, step_duration) │
    │    clip >= step  → trim   [-ss/-t, speed 1.0]      │
    │    clip <  step  → SLOW   [speed_factor < 1.0]     │  ← never freeze:
    │                            output == step duration │    reflow to fill
    └──────────────────────────────────────────────────┘
            │                                   transcript.words
            ▼                                   │
    run_single_pass(inputs, transitions="none",│
                    abs_ass=[captions.ass]) ◄───┘ _rebase_words_to_assembled
            │                                     (voiceover-time → concatenated
            │                                      visual-time, drops gap words)
            ▼
    silent visuals + burned plain captions  (single_pass, preset=fast)
            │
            ▼
    _mix_user_voiceover(voice + footage bed)  → final audio
            │   voice at full level; the original clip audio plays underneath
            │   at `bed_level`, side-chain DUCKED by the voice (dips while you
            │   speak, rises in your pauses). single_pass strips clip audio, so
            ▼   the bed is reassembled here from the same per-step windows.
    final.mp4
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

import structlog

from app.pipeline.narrated_alignment import StepTiming
from app.pipeline.probe import probe_video
from app.pipeline.reframe import resolve_output_fit
from app.pipeline.single_pass import SinglePassInput, SinglePassSpec, run_single_pass
from app.tasks.template_orchestrate import _mix_user_voiceover, _probe_duration

log = structlog.get_logger()

# Plain caption font: a clean, platform-native sans bundled in assets/fonts
# (family name as libass resolves it via fontsdir).
CAPTION_FONT = "TikTok Sans"  # libass family name; shared by render + reburn


def _caption_font_entry(font: str | None) -> dict | None:
    """Font-registry entry for a KNOWN, non-deprecated caption font, else None.

    The single registry lookup shared by :func:`resolve_caption_font` and
    :func:`is_valid_caption_font` so the two can never disagree on what counts as a
    usable caption font. A registry hiccup degrades to None (treated as unknown).
    """
    if not font:
        return None
    try:
        from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

        entry = _FONT_REGISTRY.get("fonts", {}).get(font)
        if entry and not entry.get("deprecated"):
            return entry
    except Exception:  # noqa: BLE001 — a registry hiccup must never fail the burn
        pass
    return None


def resolve_caption_font(font: str | None) -> str:
    """Map a UI font choice (a font-registry key, e.g. ``"Montserrat Bold"``) to the
    libass family name (the registry ``ass_name``, e.g. ``"Montserrat"``) used as the
    ASS ``Fontname`` for the caption burn.

    Only KNOWN, non-deprecated registry fonts resolve; anything else (None, unknown,
    deprecated) falls back to :data:`CAPTION_FONT`. That fallback is also the security
    gate — raw user text never reaches the ASS ``Fontname`` field, so a font choice
    can't inject ASS markup. The font family is changed; the bold caption weight is
    kept (the ASS style stays Bold) so any picked family reads as a bold caption.
    """
    entry = _caption_font_entry(font)
    if entry:
        ass_name = entry.get("ass_name")
        if ass_name:
            return str(ass_name)
    return CAPTION_FONT


def is_valid_caption_font(font: str | None) -> bool:
    """True if ``font`` is a known, non-deprecated font-registry key (or ``None`` =
    reset to default). The caption-font endpoint uses this to reject unknown fonts."""
    return font is None or _caption_font_entry(font) is not None


# Reflow tuning for clips shorter than their narration step.
_MIN_USABLE_S = 0.05  # below this a clip is treated as unusable / unprobeable
_EOF_GUARD_S = 0.05  # read a hair under EOF so -t never overshoots the source
_SLOW_WARN_FACTOR = 0.4  # warn when a clip must slow more than ~2.5x to fill

# Nova's default original-audio bed level (0 = voice only, 1 = loudest). A modest
# ambient bed under the voice unless the creator dials it elsewhere. The voice is
# always side-chain dominant, so this is the resting level in speech pauses.
_DEFAULT_BED_LEVEL = 0.25
_BED_SAMPLE_RATE = 44100


@dataclass(frozen=True, slots=True)
class NarratedClip:
    step_id: str
    clip_path: str
    source_start_s: float = 0.0


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _coerce_clip(value: NarratedClip | dict | Any) -> NarratedClip:
    if isinstance(value, NarratedClip):
        return value
    step_id = str(_field(value, "step_id", _field(value, "shot_id", "")) or "")
    clip_path = str(_field(value, "clip_path", _field(value, "local_path", "")) or "")
    source_start = float(_field(value, "source_start_s", _field(value, "start_s", 0.0)) or 0.0)
    if not step_id:
        raise ValueError("narrated clip assignment missing step_id/shot_id")
    if not clip_path:
        raise ValueError(f"narrated clip assignment for {step_id} missing clip_path")
    return NarratedClip(step_id=step_id, clip_path=clip_path, source_start_s=source_start)


def _fit_clip_segment(
    clip_path: str,
    source_start_s: float,
    target_dur_s: float,
    probe,
    *,
    output_fit: str = "crop",
) -> SinglePassInput:
    """Return a SinglePassInput that renders to EXACTLY ``target_dur_s``.

    The narrated stall ("video froze at 0:17 while the voice kept going") is a
    clip shorter than the narration step it was assigned to: single_pass cuts
    with ``-ss/-t`` and stops at clip EOF, but the timeline assumes the full
    step length, so the visuals run out before the voice. We never freeze-hold
    (that is perceptually the same bug); instead, when a clip is too short we
    SLOW it (``speed_factor`` < 1.0) so its output fills the step. Motion keeps
    going; the voice and visuals end together.

    ``output_fit`` ("crop" | "fill" | …) is the frame fit chosen upstream by
    ``resolve_output_fit`` (the landscape Fit/Fill control); it applies whether
    the clip is trimmed or reflow-slowed.
    """
    try:
        src_total = max(0.0, float(probe(clip_path) or 0.0))
    except Exception:  # noqa: BLE001 — unprobeable clip → zero-length, falls through to -ss/-t
        src_total = 0.0
    start = max(0.0, source_start_s)
    available = src_total - start
    if available <= _MIN_USABLE_S:
        # source_start sits past EOF (or the clip was unprobeable) — restart at 0.
        start = 0.0
        available = src_total

    common = dict(
        kind="clip",
        clip_path=clip_path,
        aspect_ratio="16:9",
        output_fit=output_fit,
        has_audio=False,
    )

    if available >= target_dur_s or available <= _MIN_USABLE_S:
        # Enough footage to trim, or unprobeable → let -ss/-t do its best.
        return SinglePassInput(start_s=start, end_s=start + target_dur_s, **common)

    # Too short: slow the available footage to fill the whole step.
    usable = max(_MIN_USABLE_S, available - _EOF_GUARD_S)
    speed = usable / target_dur_s  # < 1.0 → slower playback; output == target
    if speed < _SLOW_WARN_FACTOR:
        log.warning(
            "narrated_clip_heavy_slow",
            clip=os.path.basename(clip_path),
            available_s=round(available, 2),
            target_s=round(target_dur_s, 2),
            speed_factor=round(speed, 3),
        )
    return SinglePassInput(
        start_s=start,
        end_s=start + usable,
        speed_factor=speed,
        **common,
    )


def _rebase_words_to_assembled(words: list, step_timings: list[StepTiming]) -> list:
    """Map word timestamps from voiceover-time onto the concatenated visual time.

    The assembled video is the step DURATIONS laid back-to-back from 0. Word
    timestamps are in absolute voiceover time. When a step starts after leading
    silence (narrated_ready) or steps have gaps between them, the two clocks
    diverge and captions burned in voiceover-time drift off the visuals. Remap
    each word through its containing step into assembled-time; words that fall
    in a gap (no containing step) are dropped. For the contiguous-from-0
    scripted path this is the identity.
    """
    from app.pipeline.transcribe import Word  # noqa: PLC0415

    spans: list[tuple[float, float, float]] = []  # (vo_start, vo_end, assembled_start)
    cum = 0.0
    for t in step_timings:
        dur = max(0.0, float(t.end_s) - float(t.start_s))
        spans.append((float(t.start_s), float(t.end_s), cum))
        cum += dur

    rebased: list = []
    for w in words:
        ws = float(w.start_s)
        we = float(w.end_s)
        for vo_start, vo_end, asm_start in spans:
            if vo_start <= ws < vo_end:
                a_start = asm_start + (max(ws, vo_start) - vo_start)
                a_end = asm_start + (min(we, vo_end) - vo_start)
                if a_end <= a_start:
                    a_end = a_start + 0.01
                rebased.append(
                    Word(
                        text=w.text,
                        start_s=round(a_start, 3),
                        end_s=round(a_end, 3),
                        confidence=float(getattr(w, "confidence", 1.0)),
                    )
                )
                break

    rebased.sort(key=lambda x: (x.start_s, x.end_s))
    return rebased


def _build_caption_overlay(
    transcript: Any,
    step_timings: list[StepTiming],
    total_duration_s: float,
    tmpdir: str,
    *,
    caption_style: str = "sentence",
    caption_font: str | None = None,
) -> tuple[list[str], str, list[dict]]:
    """Generate the caption ASS for the narration + its editable cues.

    Returns ``(abs_ass_paths, fonts_dir, cues)``. ``cues`` is the
    ``[{text, start_s, end_s}]`` list (assembled-time) the caller persists so the
    on-video editor and any later reburn render exactly what was burned here. A
    caption failure must never fail the render — fall back to no captions.

    ``caption_style`` selects the cue grouping + burned look:
      - ``"sentence"`` (default) — sentence-block plain captions (``build_plain_cues``).
      - ``"word"`` — one big word at a time, the word-by-word ("qbuilder") style
        (``build_word_cues`` + the ``"word"`` ASS style). The cues are still plain
        ``{text, start_s, end_s}`` (one per word), so the editor + reburn are unchanged.

    ``caption_font`` is a UI font choice (registry key) for BOTH styles; it resolves
    to a libass family via :func:`resolve_caption_font` (None/unknown → the default).
    """
    if transcript is None or not getattr(transcript, "words", None):
        return [], "", []
    try:
        from app.pipeline.captions import (  # noqa: PLC0415
            build_plain_cues,
            build_word_cues,
            generate_ass_from_cues,
        )
        from app.pipeline.text_overlay import FONTS_DIR  # noqa: PLC0415

        rebased = _rebase_words_to_assembled(transcript.words, step_timings)
        word_mode = caption_style == "word"
        cues = (
            build_word_cues(rebased, offset_s=0.0)
            if word_mode
            else build_plain_cues(rebased, offset_s=0.0)
        )
        if not cues:
            return [], "", []
        ass_path = os.path.join(tmpdir, "narrated_captions.ass")
        # Burn FROM the cues (single source of truth shared with persist + reburn).
        generate_ass_from_cues(
            cues,
            ass_path,
            font_name=resolve_caption_font(caption_font),
            style="word" if word_mode else "plain",
        )
        return [ass_path], FONTS_DIR, cues
    except Exception as exc:  # noqa: BLE001 — captions are best-effort
        log.warning("narrated_captions_failed", error=str(exc))
        return [], "", []


def _clip_has_audio(clip_path: str) -> bool:
    """True when the clip carries at least one audio stream."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                clip_path,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return bool(result.stdout.strip())
    except Exception:  # noqa: BLE001 — best-effort probe
        return False


def _atempo_chain(speed_factor: float) -> str:
    """Audio atempo filter(s) matching a video ``speed_factor`` (<1.0 = slower).

    atempo accepts [0.5, 100]; chain factors so their product == speed_factor,
    keeping the bed audio in sync with a reflow-slowed clip.
    """
    # >=1.0 needs no slowdown; <=0 is degenerate (would loop forever halving) — both
    # leave the bed untouched. Real callers floor speed at _MIN_USABLE_S/duration > 0.
    if speed_factor >= 1.0 or speed_factor <= 0.0:
        return ""
    factors: list[float] = []
    remaining = float(speed_factor)
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining = remaining / 0.5
    factors.append(remaining)
    return ",".join(f"atempo={f:.4f}" for f in factors)


def _assemble_footage_bed(
    inputs: list[SinglePassInput],
    out_path: str,
    tmpdir: str,
) -> str | None:
    """Concatenate the original clip audio for each step into one bed track.

    Each step contributes its clip's audio for the SAME source window the visual
    used, time-stretched by the same ``speed_factor`` so the bed stays in sync,
    and padded/trimmed to exactly the step's output duration. Clips with no audio
    contribute matching silence. Returns the bed path, or None on any failure
    (the caller then renders voice-only — best-effort).
    """
    bed_dir = os.path.join(tmpdir, "bed_segments")
    os.makedirs(bed_dir, exist_ok=True)
    seg_paths: list[str] = []
    try:
        for idx, inp in enumerate(inputs):
            used = max(_MIN_USABLE_S, float(inp.end_s) - float(inp.start_s))
            step_dur = used / max(1e-6, float(inp.speed_factor))
            seg = os.path.join(bed_dir, f"seg_{idx}.wav")
            if inp.clip_path and _clip_has_audio(inp.clip_path):
                af = "aresample=" + str(_BED_SAMPLE_RATE)
                tempo = _atempo_chain(float(inp.speed_factor))
                if tempo:
                    af = f"{tempo},{af}"
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{float(inp.start_s):.3f}",
                    "-t",
                    f"{used:.3f}",
                    "-i",
                    inp.clip_path,
                    "-vn",
                    "-af",
                    f"{af},apad",
                    "-ac",
                    "2",
                    "-ar",
                    str(_BED_SAMPLE_RATE),
                    "-t",
                    f"{step_dur:.3f}",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    seg,
                ]
            else:
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={_BED_SAMPLE_RATE}:cl=stereo",
                    "-t",
                    f"{step_dur:.3f}",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    seg,
                ]
            res = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
            if res.returncode != 0 or not os.path.exists(seg) or os.path.getsize(seg) == 0:
                raise RuntimeError(f"bed segment {idx} failed: {res.stderr.decode()[:200]}")
            seg_paths.append(seg)

        if not seg_paths:
            return None
        if len(seg_paths) == 1:
            os.replace(seg_paths[0], out_path)
            return out_path

        concat_inputs: list[str] = []
        for seg in seg_paths:
            concat_inputs += ["-i", seg]
        graph = "".join(f"[{i}:a]" for i in range(len(seg_paths)))
        graph += f"concat=n={len(seg_paths)}:v=0:a=1[a]"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            *concat_inputs,
            "-filter_complex",
            graph,
            "-map",
            "[a]",
            "-ac",
            "2",
            "-ar",
            str(_BED_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-y",
            out_path,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError(f"bed concat failed: {res.stderr.decode()[:200]}")
        return out_path
    except Exception as exc:  # noqa: BLE001 — bed is best-effort; fall back to voice-only
        log.warning("narrated_footage_bed_failed", error=str(exc))
        return None


def burn_captions_on_video(
    video_path: str,
    ass_path: str,
    fonts_dir: str,
    out_path: str,
) -> None:
    """Burn an ASS caption file onto an already-mixed video (audio stream-copied).

    The caption-reburn path: take the caption-FREE base (clips + voice + bed) and
    re-render only the video with libass so the creator's edited cues appear. Audio
    is `-c:a copy` (untouched). Final-output encode → preset "fast" + crf 18, matching
    the encoder policy (libx264 fast, like the fixed-intro renderer).
    """
    ass_esc = ass_path.replace(":", "\\:").replace("'", "\\'")
    fonts_esc = fonts_dir.replace(":", "\\:").replace("'", "\\'")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-vf",
        f"subtitles='{ass_esc}':fontsdir='{fonts_esc}'",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        out_path,
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"caption reburn failed: {res.stderr.decode(errors='replace')[:300]}")


def assemble_narrated(
    step_timings: list[StepTiming],
    clip_assignments: list[NarratedClip | dict | Any],
    voiceover_local_path: str,
    output_path: str,
    tmpdir: str,
    *,
    landscape_fit: str = "fill",
    transcript: Any = None,
    bed_level: float | None = None,
    base_output_path: str | None = None,
    caption_style: str = "sentence",
    caption_font: str | None = None,
) -> list[dict]:
    """Hard-cut one visual clip per narrated step, burn captions, lay voice on top.

    ``landscape_fit`` ("fill" | "fit") picks how a landscape clip fills the 9:16
    frame (resolve_output_fit per probe); a too-short clip is still reflow-slowed.

    ``transcript`` is the Whisper transcription of the voiceover (already
    computed by the caller for alignment). When present, its words are burned
    as plain synced captions so the on-screen text IS the spoken narration.

    ``bed_level`` controls the original clip audio under the voice (0 = voice
    only, 1 = loudest; None = Nova's default). When > 0 the footage audio is
    reassembled and side-chain ducked beneath the narration.

    ``base_output_path``: when set, ALSO writes a caption-FREE cut (same clips +
    voice + bed, no burned text) here — the source the on-video caption editor
    overlays and the reburn re-burns edited cues onto. single_pass emits both the
    burned and the caption-free visuals from ONE decode (``base_output_path``),
    so the extra cut is nearly free.

    Returns the caption ``cues`` ([{text, start_s, end_s}], assembled-time) for
    persistence — empty when there are no captions.
    """
    os.makedirs(tmpdir, exist_ok=True)
    coerced_clips = [_coerce_clip(c) for c in clip_assignments]
    clips_by_step = {c.step_id: c for c in coerced_clips}

    # Pre-build probe map so each unique path is probed only once, even when
    # the same clip appears in multiple narrated steps.
    probe_map: dict[str, object] = {}
    for c in coerced_clips:
        if c.clip_path not in probe_map:
            try:
                probe_map[c.clip_path] = probe_video(c.clip_path)
            except Exception:  # noqa: BLE001 — probe failure → fall back to crop
                probe_map[c.clip_path] = None

    inputs: list[SinglePassInput] = []
    total_duration_s = 0.0
    for timing in step_timings:
        duration_s = max(0.001, float(timing.end_s) - float(timing.start_s))
        clip = clips_by_step.get(timing.step_id)
        if clip is None:
            raise ValueError(f"no narrated clip assignment for step_id={timing.step_id}")
        probe = probe_map.get(clip.clip_path)
        inputs.append(
            _fit_clip_segment(
                clip.clip_path,
                clip.source_start_s,
                duration_s,
                _probe_duration,
                # Landscape Fit/Fill (main) layered onto the short-clip reflow (mine):
                # a clip that's both landscape AND too short gets the right fit AND slow.
                output_fit=resolve_output_fit(probe, landscape_fit=landscape_fit),
            )
        )
        total_duration_s += duration_s

    abs_ass_paths, fonts_dir, cues = _build_caption_overlay(
        transcript,
        step_timings,
        total_duration_s,
        tmpdir,
        caption_style=caption_style,
        caption_font=caption_font,
    )

    burned_visuals = os.path.join(tmpdir, "narrated_visuals.mp4")
    # Dual-output: burned [vout] + caption-free [base] in one ffmpeg when captions
    # exist (and the caller wants the base). No captions → no overlay → single_pass
    # emits only [vout]; the base is then a copy (already caption-free).
    base_visuals: str | None = None
    want_base = base_output_path is not None
    spec_base_arg: str | None = None
    if want_base:
        base_visuals = os.path.join(tmpdir, "narrated_visuals_base.mp4")
        spec_base_arg = base_visuals if abs_ass_paths else None
    run_single_pass(
        SinglePassSpec(
            inputs=inputs,
            transitions=["none"] * max(0, len(inputs) - 1),
            output_duration_s=total_duration_s,
            abs_ass_paths=abs_ass_paths,
            fonts_dir=fonts_dir,
        ),
        burned_visuals,
        base_output_path=spec_base_arg,
    )
    if want_base and base_visuals and not os.path.exists(base_visuals):
        # No overlays (or single-output) → the burned visuals ARE caption-free.
        shutil.copy2(burned_visuals, base_visuals)

    resolved_bed = _DEFAULT_BED_LEVEL if bed_level is None else max(0.0, min(1.0, float(bed_level)))
    footage_bed_path: str | None = None
    if resolved_bed > 0.0:
        footage_bed_path = _assemble_footage_bed(
            inputs, os.path.join(tmpdir, "footage_bed.wav"), tmpdir
        )
    mix_bed_level = resolved_bed if footage_bed_path else 0.0

    _mix_user_voiceover(
        burned_visuals,
        voiceover_local_path,
        output_path,
        tmpdir,
        mix=1.0,
        target_duration_s=total_duration_s,
        footage_bed_path=footage_bed_path,
        bed_level=mix_bed_level,
    )
    # Same voice + bed over the caption-free visuals → the editor/reburn source.
    if base_output_path and base_visuals:
        _mix_user_voiceover(
            base_visuals,
            voiceover_local_path,
            base_output_path,
            tmpdir,
            mix=1.0,
            target_duration_s=total_duration_s,
            footage_bed_path=footage_bed_path,
            bed_level=mix_bed_level,
        )

    return cues
