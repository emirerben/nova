"""Celery tasks for template-mode jobs.

orchestrate_template_job(job_id)
  → parallel Gemini upload × N clips
  → parallel analyze_clip × N
  → threshold check (>50% fail → fatal)
  → template_match (greedy + variety penalty)
  → ffmpeg_assemble (concat + reframe each slot)
  → generate_copy (with template tone)
  → set job.status = template_ready

analyze_template_task(template_id)
  → download template video from GCS
  → Gemini upload + analyze_template
  → extract template audio → GCS (idempotent)
  → cache recipe in VideoTemplate.recipe_cached

Both tasks follow the render_clip "never raises" invariant:
all errors caught → job.status = 'processing_failed'
"""

import bisect
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, VideoTemplate
from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    GeminiRefusalError,
    TemplateRecipe,
    analyze_clip,
    analyze_template,
    gemini_upload_and_wait,
)
from app.pipeline.template_matcher import TemplateMismatchError, match
from app.storage import download_to_file, upload_public_read
from app.worker import celery_app

log = structlog.get_logger()

# Sync engine (same pattern as orchestrate.py)
_sync_db_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
_sync_engine = create_engine(_sync_db_url)


def _sync_session() -> Session:
    return Session(_sync_engine, expire_on_commit=False)


# ── analyze_template_task ─────────────────────────────────────────────────────


@celery_app.task(name="tasks.analyze_template_task", bind=True, max_retries=0, time_limit=600)
def analyze_template_task(self, template_id: str) -> None:
    """Download template video, analyze with Gemini, cache recipe in DB."""
    log.info("analyze_template_start", template_id=template_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, "template.mp4")
        try:
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template is None:
                    log.error("template_not_found", template_id=template_id)
                    return
                gcs_path = template.gcs_path
                existing_audio_gcs = template.audio_gcs_path

            download_to_file(gcs_path, local_path)

            file_ref = gemini_upload_and_wait(local_path)
            recipe = analyze_template(file_ref)

            # Extract template audio (idempotent — skip if already stored)
            audio_gcs: str | None = existing_audio_gcs
            audio_local = os.path.join(tmpdir, "audio.m4a")
            if not existing_audio_gcs:
                if _extract_template_audio(local_path, audio_local):
                    audio_gcs = f"templates/{template_id}/audio.m4a"
                    upload_public_read(audio_local, audio_gcs)
                    log.info("template_audio_extracted", template_id=template_id)

            # Beat detection: merge Gemini beats with FFmpeg energy onsets
            ffmpeg_beats = (
                _detect_audio_beats(audio_local)
                if os.path.exists(audio_local)
                else []
            )
            merged_beats = _merge_beat_sources(recipe.beat_timestamps_s, ffmpeg_beats)

            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    from datetime import datetime  # noqa: PLC0415
                    template.recipe_cached = {
                        "shot_count": recipe.shot_count,
                        "total_duration_s": recipe.total_duration_s,
                        "hook_duration_s": recipe.hook_duration_s,
                        "slots": recipe.slots,
                        "copy_tone": recipe.copy_tone,
                        "caption_style": recipe.caption_style,
                        "beat_timestamps_s": merged_beats,
                    }
                    template.recipe_cached_at = datetime.now(UTC)
                    template.analysis_status = "ready"
                    if audio_gcs and not template.audio_gcs_path:
                        template.audio_gcs_path = audio_gcs
                    db.commit()

            log.info("analyze_template_done", template_id=template_id, slots=len(recipe.slots))

        except Exception as exc:
            log.error("analyze_template_failed", template_id=template_id, error=str(exc))
            with _sync_session() as db:
                template = db.get(VideoTemplate, template_id)
                if template:
                    template.analysis_status = "failed"
                    db.commit()


# ── orchestrate_template_job ──────────────────────────────────────────────────


@celery_app.task(name="tasks.orchestrate_template_job", bind=True, max_retries=0, time_limit=1800)
def orchestrate_template_job(self, job_id: str) -> None:
    """Full template-mode pipeline. Never raises — all errors go to processing_failed."""
    log.info("template_job_start", job_id=job_id)

    try:
        _run_template_job(job_id)
    except Exception as exc:
        log.error("template_job_fatal", job_id=job_id, error=str(exc))
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "processing_failed"
                job.error_detail = str(exc)[:1000]
                db.commit()


def _run_template_job(job_id: str) -> None:
    """Core template job logic. May raise — caller wraps in try/except."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("template_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        db.commit()

        # Snapshot fields before session closes
        template_id = job.template_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs = all_candidates.get("clip_paths", [])
        selected_platforms = job.selected_platforms or ["tiktok", "instagram", "youtube"]

    if not clip_paths_gcs:
        raise ValueError("No clip paths found in job")

    # [1] Load template recipe from DB cache
    with _sync_session() as db:
        template = db.get(VideoTemplate, template_id)
        if template is None:
            raise ValueError(f"Template {template_id} not found")
        if template.analysis_status != "ready" or not template.recipe_cached:
            raise ValueError(f"Template {template_id} analysis not ready")

        recipe_data = template.recipe_cached
        audio_gcs_path = template.audio_gcs_path  # may be None

    try:
        recipe = TemplateRecipe(**recipe_data)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Template recipe in DB is malformed: {exc}") from exc

    with tempfile.TemporaryDirectory() as tmpdir:
        # [2] Download all clip files in parallel
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)

        # [2b] Probe all clips — get aspect_ratio + duration in a single FFprobe pass
        probe_map = _probe_clips(local_clip_paths)

        # [3] Upload all clips to Gemini in parallel
        log.info("gemini_upload_clips_start", job_id=job_id, count=len(local_clip_paths))
        file_refs = _upload_clips_parallel(local_clip_paths)
        log.info("gemini_upload_clips_done", job_id=job_id)

        # [4] Analyze all clips in parallel
        log.info("gemini_analyze_clips_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(file_refs, local_clip_paths, probe_map)

        # Threshold check: >50% failure → fatal
        total = len(clip_metas) + failed_count
        if failed_count > total * 0.5:
            raise GeminiAnalysisError(
                f"{failed_count}/{total} clips failed Gemini analysis — "
                "too many failures to produce quality output"
            )
        log.info(
            "gemini_analyze_clips_done",
            job_id=job_id,
            success=len(clip_metas),
            failed=failed_count,
        )

        # [5] Template match
        log.info("template_match_start", job_id=job_id)
        try:
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        # Persist assembly plan
        plan_data = {
            "steps": [
                {
                    "slot": step.slot,
                    "clip_id": step.clip_id,
                    "moment": step.moment,
                }
                for step in assembly_plan.steps
            ]
        }
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.assembly_plan = plan_data
                db.commit()

        # [6] FFmpeg assemble
        log.info("ffmpeg_assemble_start", job_id=job_id)
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        clip_id_to_local = {
            ref.name: path for ref, path in zip(file_refs, local_clip_paths)
        }
        _assemble_clips(
            assembly_plan.steps, clip_id_to_local, probe_map,
            assembled_path, tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
        )

        # [6b] Mix template audio if available
        if audio_gcs_path:
            final_path = os.path.join(tmpdir, "final.mp4")
            _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)
            output_path = final_path
        else:
            output_path = assembled_path

        # [7] Generate copy (with template tone)
        hook_text = _extract_hook_text(clip_metas, assembly_plan.steps)
        transcript_excerpt = _extract_transcript(clip_metas, assembly_plan.steps)
        from app.pipeline.agents.copy_writer import generate_copy  # noqa: PLC0415
        platform_copy, copy_status = generate_copy(
            hook_text=hook_text,
            transcript_excerpt=transcript_excerpt,
            platforms=selected_platforms,
            has_transcript=bool(transcript_excerpt),
            template_tone=recipe.copy_tone,
        )

        # [8] Upload assembled video to GCS
        gcs_output_path = f"jobs/{job_id}/template_output.mp4"
        video_url = upload_public_read(output_path, gcs_output_path)

        # [9] Finalize
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "template_ready"
                job.assembly_plan = {
                    **plan_data,
                    "output_url": video_url,
                    "platform_copy": platform_copy.model_dump(),
                    "copy_status": copy_status,
                }
                db.commit()

        log.info("template_job_done", job_id=job_id, output_url=video_url)


# ── Parallelism helpers ────────────────────────────────────────────────────────


def _download_clips_parallel(gcs_paths: list[str], tmpdir: str) -> list[str]:
    """Download all clips from GCS in parallel. Returns local file paths."""
    local_paths = [os.path.join(tmpdir, f"clip_{i}.mp4") for i in range(len(gcs_paths))]

    def _download_one(args: tuple[str, str]) -> str:
        gcs_path, local_path = args
        download_to_file(gcs_path, local_path)
        return local_path

    with ThreadPoolExecutor(max_workers=min(len(gcs_paths), 8)) as pool:
        list(pool.map(_download_one, zip(gcs_paths, local_paths)))

    return local_paths


def _probe_clips(local_paths: list[str]) -> dict:
    """Return {local_path: VideoProbe} for each clip. Falls back on error.

    Replaces _get_clip_duration() — probe_video() returns both aspect_ratio
    and duration_s in a single FFprobe call.
    """
    from app.pipeline.probe import VideoProbe, probe_video  # noqa: PLC0415

    _FALLBACK_DURATION = 30.0

    result: dict = {}
    for path in local_paths:
        try:
            result[path] = probe_video(path)
        except Exception as exc:
            log.warning("clip_probe_failed_using_default", path=path, error=str(exc))
            result[path] = VideoProbe(
                duration_s=_FALLBACK_DURATION,
                fps=30.0,
                width=1920,
                height=1080,
                has_audio=True,
                codec="h264",
                aspect_ratio="16:9",
                file_size_bytes=0,
            )
    return result


def _upload_clips_parallel(local_paths: list[str]) -> list:
    """Upload all clips to Gemini File API in parallel. Returns file refs in order."""
    results: dict[int, object] = {}

    def _upload_one(idx_path: tuple[int, str]) -> tuple[int, object]:
        idx, path = idx_path
        ref = gemini_upload_and_wait(path)
        return idx, ref

    with ThreadPoolExecutor(max_workers=min(len(local_paths), 8)) as pool:
        futures = {pool.submit(_upload_one, (i, p)): i for i, p in enumerate(local_paths)}
        for future in as_completed(futures):
            idx, ref = future.result()
            results[idx] = ref

    return [results[i] for i in range(len(local_paths))]


def _analyze_clips_parallel(
    file_refs: list, local_paths: list[str], probe_map: dict | None = None
) -> tuple[list[ClipMeta], int]:
    """Analyze all clips with Gemini in parallel.

    Returns (successful_metas, failed_count).
    Per-clip failures are logged but don't raise — caller applies threshold.
    probe_map is used in the Whisper fallback path to get clip duration without
    a second FFprobe call.
    """
    clip_metas: list[ClipMeta] = []
    failed_count = 0

    def _analyze_one(args: tuple[int, object, str]) -> tuple[ClipMeta | None, str | None]:
        idx, ref, path = args
        try:
            meta = analyze_clip(ref)
            meta.clip_path = path
            return meta, None
        except (GeminiRefusalError, GeminiAnalysisError, Exception) as exc:
            log.warning("clip_analysis_failed", clip_idx=idx, error=str(exc))
            # Whisper heuristic fallback for failed clips
            from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
            try:
                transcript = transcribe_whisper(path)
                clip_dur = (
                    probe_map[path].duration_s
                    if probe_map and path in probe_map
                    else 30.0
                )
                fallback_meta = ClipMeta(
                    clip_id=getattr(ref, "name", f"clip_{idx}"),
                    transcript=transcript.full_text,
                    hook_text=transcript.full_text[:100] if transcript.full_text else "",
                    hook_score=5.0,
                    best_moments=_fallback_moments(clip_dur),
                    analysis_degraded=True,
                    clip_path=path,
                )
                return fallback_meta, None
            except Exception as whisper_exc:
                log.warning("whisper_fallback_failed", clip_idx=idx, error=str(whisper_exc))
                return None, str(exc)

    with ThreadPoolExecutor(max_workers=min(len(file_refs), 8)) as pool:
        futures = [
            pool.submit(_analyze_one, (i, ref, path))
            for i, (ref, path) in enumerate(zip(file_refs, local_paths))
        ]
        for future in as_completed(futures):
            meta, error = future.result()
            if meta is not None:
                clip_metas.append(meta)
            else:
                failed_count += 1

    return clip_metas, failed_count


# ── Fallback helpers ──────────────────────────────────────────────────────────


def _fallback_moments(clip_dur: float) -> list[dict]:
    """Generate overlapping moments at multiple durations for Whisper-fallback clips.

    Covers short (3–5s), medium (8–12s), and long (15s+) slot ranges so that
    a fallback clip can satisfy template slots of any target_duration_s.
    """
    moments = [
        {"start_s": 0.0, "end_s": min(clip_dur, 5.0), "energy": 5.0, "description": "fallback"},
        {"start_s": 0.0, "end_s": min(clip_dur, 10.0), "energy": 5.0, "description": "fallback"},
        {"start_s": 0.0, "end_s": min(clip_dur, 15.0), "energy": 5.0, "description": "fallback"},
    ]
    # Add a full-clip moment only if it meaningfully extends beyond 15s
    if clip_dur > 21.0:
        moments.append(
            {"start_s": 0.0, "end_s": clip_dur, "energy": 5.0, "description": "fallback"}
        )
    return moments


# ── FFmpeg assembly ────────────────────────────────────────────────────────────


def _assemble_clips(
    steps: list,
    clip_id_to_local: dict[str, str],
    clip_probe_map: dict,
    output_path: str,
    tmpdir: str,
    beat_timestamps_s: list[float] | None = None,
) -> None:
    """Assemble clips in slot order using FFmpeg concat.

    Each step is trimmed to slot's target_duration_s and reframed to the clip's
    native aspect ratio (derived from probe, not hardcoded).

    Time-cursor: when a clip appears in multiple slots, each use advances start_s
    by slot_target_dur so the viewer sees a different section of the clip rather
    than the same footage repeated. Wraps to the beginning if the clip is exhausted.

    Beat-snap: when beat_timestamps_s is provided, each slot boundary is snapped
    to the nearest beat within BEAT_SNAP_TOLERANCE_S, adjusting slot duration
    so transitions align with musical beats.
    """
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

    beats = beat_timestamps_s or []

    # Per-clip time cursors: track next available start_s to avoid reusing footage
    clip_cursors: dict[str, float] = {}

    # Cumulative time offset for beat-snap (tracks assembled video position)
    cumulative_s = 0.0

    # Step 1: reframe each slot to a temp file
    reframed_paths: list[str] = []
    for i, step in enumerate(steps):
        clip_id = step.clip_id
        local_path = clip_id_to_local.get(clip_id)
        if not local_path or not os.path.exists(local_path):
            raise ValueError(f"Local path not found for clip_id {clip_id}")

        moment = step.moment
        slot_target_dur = float(step.slot.get("target_duration_s", 5.0))
        slot_target_dur = max(slot_target_dur, 0.5)  # guard against zero/negative

        # Beat-snap: adjust slot duration so the cut lands on a musical beat
        if beats:
            expected_end = cumulative_s + slot_target_dur
            snapped_end = _snap_to_beat(expected_end, beats)
            slot_target_dur = max(0.5, snapped_end - cumulative_s)
            cumulative_s = snapped_end
        else:
            cumulative_s += slot_target_dur

        # Time-cursor logic: first use of a clip follows Gemini's moment start_s;
        # subsequent uses advance forward so footage doesn't repeat.
        if clip_id not in clip_cursors:
            start_s = float(moment.get("start_s", 0.0))
        else:
            start_s = clip_cursors[clip_id]

        # Clamp if cursor has gone past the clip duration.
        # Never wrap to 0.0 — that replays footage the viewer already saw.
        # Instead, show the last available segment of the clip.
        probe = clip_probe_map.get(local_path)
        clip_dur = probe.duration_s if probe else 30.0
        if start_s + slot_target_dur > clip_dur:
            start_s = max(0.0, clip_dur - slot_target_dur)
            log.warning(
                "clip_footage_exhausted",
                clip_id=clip_id,
                position=step.slot.get("position"),
                clip_dur=clip_dur,
                cursor=clip_cursors.get(clip_id, 0.0),
            )

        clip_cursors[clip_id] = start_s + slot_target_dur

        # End time: start + slot target, bounded by clip duration.
        # Do NOT use moment.end_s — with cursor-based assembly, the slot's
        # target_duration_s controls the cut length, not the Gemini moment range.
        end_s = min(start_s + slot_target_dur, clip_dur)

        # Use per-clip aspect ratio from probe; fall back to 16:9
        probe = clip_probe_map.get(local_path)
        aspect_ratio = probe.aspect_ratio if probe else "16:9"

        log.debug(
            "slot_timed",
            position=step.slot.get("position"),
            target_s=slot_target_dur,
            actual_s=round(end_s - start_s, 2),
        )

        reframed = os.path.join(tmpdir, f"slot_{i}.mp4")
        reframe_and_export(
            input_path=local_path,
            start_s=start_s,
            end_s=end_s,
            aspect_ratio=aspect_ratio,
            ass_subtitle_path=None,
            output_path=reframed,
        )
        reframed_paths.append(reframed)

    if not reframed_paths:
        raise ValueError("No slots to assemble")

    if len(reframed_paths) == 1:
        # Single slot — just copy
        shutil.copy2(reframed_paths[0], output_path)
        return

    # Step 2: write concat list
    concat_list = os.path.join(tmpdir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in reframed_paths:
            f.write(f"file '{p}'\n")

    # Step 3: FFmpeg concat (demuxer — fast, no re-encode)
    cmd = [
        "ffmpeg",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        "-y",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg concat failed: {result.stderr.decode()[:300]}")


# ── Beat detection helpers ─────────────────────────────────────────────────────

BEAT_SNAP_TOLERANCE_S = 0.4


def _detect_audio_beats(audio_path: str, threshold_db: float = -20.0) -> list[float]:
    """Detect energy onset timestamps in audio using FFmpeg silencedetect.

    Parses silence_end markers from FFmpeg stderr — each silence_end marks a
    transition from quiet→loud, which corresponds to a beat or energy onset.

    Non-fatal: returns [] on any error.
    """
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=noise={threshold_db}dB:d=0.1",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            log.warning("beat_detect_ffmpeg_failed", stderr=result.stderr.decode()[:200])
            return []

        # Parse silence_end timestamps from stderr
        # Format: [silencedetect @ 0x...] silence_end: 1.234 | silence_duration: 0.567
        stderr_text = result.stderr.decode()
        beats = sorted(
            float(m.group(1))
            for m in re.finditer(r"silence_end:\s*([\d.]+)", stderr_text)
        )
        log.info("beat_detect_done", count=len(beats))
        return beats

    except Exception as exc:
        log.warning("beat_detect_failed", error=str(exc))
        return []


def _merge_beat_sources(
    gemini_beats: list[float],
    ffmpeg_beats: list[float],
    dedup_threshold_s: float = 0.15,
) -> list[float]:
    """Merge two beat timestamp lists, deduplicating within threshold.

    When Gemini and FFmpeg beats are within dedup_threshold_s of each other,
    the FFmpeg timestamp is kept (frame-accurate) and the Gemini one is dropped.
    """
    if not gemini_beats and not ffmpeg_beats:
        return []
    if not gemini_beats:
        return sorted(ffmpeg_beats)
    if not ffmpeg_beats:
        return sorted(gemini_beats)

    # Start with FFmpeg beats (higher accuracy)
    merged = sorted(ffmpeg_beats)

    # Add Gemini beats that aren't near any FFmpeg beat
    for gb in gemini_beats:
        idx = bisect.bisect_left(merged, gb)
        too_close = False
        for check_idx in (idx - 1, idx):
            if 0 <= check_idx < len(merged):
                if abs(merged[check_idx] - gb) <= dedup_threshold_s:
                    too_close = True
                    break
        if not too_close:
            bisect.insort(merged, gb)

    return merged


def _snap_to_beat(target_end_s: float, beats: list[float]) -> float:
    """Snap target timestamp to nearest beat if within tolerance.

    Uses bisect for O(log n) lookup. No-op if beats is empty.
    """
    if not beats:
        return target_end_s

    idx = bisect.bisect_left(beats, target_end_s)
    candidates = []
    if idx < len(beats):
        candidates.append(beats[idx])
    if idx > 0:
        candidates.append(beats[idx - 1])

    nearest = min(candidates, key=lambda b: abs(b - target_end_s))
    if abs(nearest - target_end_s) <= BEAT_SNAP_TOLERANCE_S:
        return nearest
    return target_end_s


# ── Template audio helpers ─────────────────────────────────────────────────────


def _extract_template_audio(local_video_path: str, output_path: str) -> bool:
    """Extract audio track from template video. Returns True on success.

    Non-fatal: some templates may be silent.
    Uses .m4a container (AAC in MP4) for broad FFmpeg compatibility.
    """
    cmd = [
        "ffmpeg", "-i", local_video_path,
        "-vn",          # no video
        "-acodec", "aac",
        "-b:a", "192k",
        "-f", "mp4",    # force mp4 container (more compatible than raw ADTS .aac)
        "-y", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        log.warning(
            "template_audio_extract_failed",
            stderr=result.stderr.decode()[:200],
        )
        return False
    return os.path.exists(output_path) and os.path.getsize(output_path) > 1000


def _mix_template_audio(
    video_path: str, audio_gcs_path: str, output_path: str, tmpdir: str
) -> None:
    """Replace assembled video's audio with template music track.

    Non-fatal if anything fails — falls back to copying video_path → output_path.
    """
    audio_local = os.path.join(tmpdir, "template_audio.m4a")
    try:
        download_to_file(audio_gcs_path, audio_local)
    except Exception as exc:
        log.warning("template_audio_download_failed", error=str(exc))
        shutil.copy2(video_path, output_path)
        return

    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-i", audio_local,
        "-map", "0:v",  # video from assembled
        "-map", "1:a",  # audio from template
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",    # cut to shorter of video/audio
        "-y", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        log.warning("template_audio_mix_failed", stderr=result.stderr.decode()[:200])
        shutil.copy2(video_path, output_path)


# ── Copy helpers ───────────────────────────────────────────────────────────────


def _extract_hook_text(clip_metas: list[ClipMeta], steps: list) -> str:
    """Get hook text from the first step's clip."""
    if not steps:
        return ""
    first_clip_id = steps[0].clip_id
    for meta in clip_metas:
        if meta.clip_id == first_clip_id:
            return meta.hook_text or ""
    return ""


def _extract_transcript(clip_metas: list[ClipMeta], steps: list) -> str:
    """Concatenate transcripts from clips used in the assembly."""
    used_clip_ids = {step.clip_id for step in steps}
    parts = [
        meta.transcript
        for meta in clip_metas
        if meta.clip_id in used_clip_ids and meta.transcript
    ]
    return " ".join(parts)[:500]
