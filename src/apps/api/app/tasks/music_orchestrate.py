"""Celery tasks for music beat-sync jobs.

analyze_music_track_task(track_id)
  → download audio from GCS (already uploaded by audio_download.py)
  → _detect_audio_beats()           — FFmpeg energy onset detection
  → auto_best_section()             — 45s peak-density window
  → store beat_timestamps_s + track_config in MusicTrack

orchestrate_music_job(job_id)
  → load MusicTrack (must be ready + audio_gcs_path present)
  → generate_music_recipe()         — beat-based slots
  → parallel Gemini analyze_clip() × N
  → template_matcher.match()        — unchanged
  → _assemble_clips()               — unchanged (receives beat_timestamps_s)
  → _mix_template_audio()           — music track replaces template audio
  → set job.status = music_ready

Both tasks follow the "never raises" invariant:
  all errors caught → job/track status = 'failed' with error_detail
"""

import math
import os
import re
import subprocess
import tempfile
import uuid
from typing import Any

import structlog

from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.pipeline.music_recipe import (
    DEFAULT_WINDOW_S,
    auto_best_section,
    generate_music_recipe,
    merge_audio_recipe,
)
from app.storage import download_to_file
from app.tasks.template_orchestrate import (
    _analyze_clips_parallel,
    _assemble_clips,
    _download_clips_parallel,
    _enrich_slots_with_energy,
    _mix_template_audio,
    _probe_clips,
    _upload_clips_parallel,
)
from app.worker import celery_app

try:
    from app.pipeline.agents.gemini_analyzer import (
        GeminiAnalysisError,
        analyze_audio_template,
        gemini_upload_and_wait,
    )
    from app.pipeline.template_matcher import TemplateMismatchError, consolidate_slots, match
except ImportError:
    GeminiAnalysisError = Exception  # type: ignore[assignment, misc]
    TemplateMismatchError = Exception  # type: ignore[assignment, misc]
    analyze_audio_template = None  # type: ignore[assignment]
    gemini_upload_and_wait = None  # type: ignore[assignment]

log = structlog.get_logger()

MAX_ERROR_DETAIL_LEN = 2000


# ── Music-specific beat detection ────────────────────────────────────────────


def _detect_music_beats(audio_path: str, min_gap_s: float = 0.15) -> list[float]:
    """Detect beats in a music track via FFmpeg RMS energy peak detection.

    Unlike silencedetect (which looks for silence→loud transitions and fails on
    continuous music), this uses per-frame RMS energy from astats and finds local
    peaks above the median energy level. Works for any music with rhythmic content.

    Returns sorted list of beat timestamps in seconds.
    """
    cmd = [
        "ffmpeg",
        "-i",
        audio_path,
        "-af",
        "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            log.warning("music_beat_detect_ffmpeg_failed", stderr=result.stderr[-500:])
            return []

        # Parse timestamps and RMS levels from stdout (ametadata file=- writes there)
        lines = result.stdout.strip().split("\n")
        frames: list[tuple[float, float]] = []
        ts = None
        for line in lines:
            line = line.strip()
            if line.startswith("frame:"):
                m = re.search(r"pts_time:([\d.]+)", line)
                if m:
                    ts = float(m.group(1))
            elif "RMS_level" in line and ts is not None:
                m = re.search(r"=(-?\d+\.?\d*)", line)
                if m:
                    try:
                        frames.append((ts, float(m.group(1))))
                    except ValueError:
                        pass  # skip unparseable values like bare "-"

        if len(frames) < 10:
            log.warning("music_beat_detect_too_few_frames", count=len(frames))
            return []

        # Find energy peaks: above median + 3dB, higher than both neighbors
        energies = [e for _, e in frames]
        median_e = sorted(energies)[len(energies) // 2]
        threshold = median_e + 3.0  # 3dB above median

        beats: list[float] = []
        for i in range(1, len(frames) - 1):
            t, e = frames[i]
            _, e_prev = frames[i - 1]
            _, e_next = frames[i + 1]
            if e > e_prev and e > e_next and e > threshold:
                if not beats or (t - beats[-1]) > min_gap_s:
                    beats.append(t)

        log.info("music_beat_detect_done", count=len(beats), threshold=round(threshold, 1))
        return beats

    except Exception as exc:
        log.warning("music_beat_detect_failed", error=str(exc))
        return []


# ── analyze_music_track_task ──────────────────────────────────────────────────


@celery_app.task(
    name="tasks.analyze_music_track_task",
    bind=True,
    max_retries=0,
    soft_time_limit=300,
    time_limit=360,
)
def analyze_music_track_task(self, track_id: str) -> None:
    """Detect beats + auto-select best section for a music track."""
    log.info("analyze_music_track_start", track_id=track_id)

    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            log.error("music_track_not_found", track_id=track_id)
            return
        if not track.audio_gcs_path:
            log.error("music_track_no_audio_gcs_path", track_id=track_id)
            _fail_track(track_id, "Audio not yet uploaded to GCS — cannot analyze.")
            return
        audio_gcs = track.audio_gcs_path
        duration_s = track.duration_s
        existing_config = track.track_config or {}

        track.analysis_status = "analyzing"
        track.error_detail = None
        db.commit()

    try:
        with tempfile.TemporaryDirectory(prefix="nova_music_analyze_") as tmpdir:
            local_audio = os.path.join(tmpdir, "audio.m4a")
            download_to_file(audio_gcs, local_audio)

            beats = _detect_music_beats(local_audio)
            log.info("music_beats_detected", track_id=track_id, count=len(beats))

            # Auto-select best section only if not already admin-configured
            best_start: float = float(existing_config.get("best_start_s", 0.0))
            best_end: float = float(existing_config.get("best_end_s", 0.0))
            if best_end <= best_start:
                best_start, best_end = auto_best_section(
                    beats,
                    window_s=DEFAULT_WINDOW_S,
                    track_duration_s=float(duration_s or 0.0),
                )

            # Slot count from best section
            n = int(existing_config.get("slot_every_n_beats", 8))
            window_beats = [b for b in beats if best_start <= b <= best_end]
            n_slots = len(range(0, max(0, len(window_beats) - n), n))

            if n_slots == 0:
                _fail_track(
                    track_id,
                    "Beat detection produced 0 slots — audio may be "
                    "silent or use an unsupported format.",
                )
                return

            new_config = {
                **existing_config,
                "best_start_s": round(best_start, 3),
                "best_end_s": round(best_end, 3),
                "slot_every_n_beats": n,
                "required_clips_min": max(1, math.floor(n_slots / 2)),
                "required_clips_max": max(1, n_slots),
            }

            # ── Gemini audio analysis (new) ──────────────────────────────
            recipe_cached = None
            ai_labels_dict: dict | None = None
            sections_dict: dict | None = None
            if gemini_upload_and_wait is not None and analyze_audio_template is not None:
                recipe_cached, ai_labels_dict, sections_dict = _run_gemini_audio_analysis(
                    local_audio,
                    beats,
                    new_config,
                    float(duration_s or 0.0),
                    track_id,
                )

        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
            if track:
                track.beat_timestamps_s = beats
                track.track_config = new_config
                if recipe_cached is not None:
                    from datetime import UTC, datetime  # noqa: PLC0415

                    track.recipe_cached = recipe_cached
                    track.recipe_cached_at = datetime.now(UTC)
                if ai_labels_dict is not None:
                    # Phase 1 (auto-music): song_classifier labels feed the
                    # matcher's stale-row filter via label_version. See
                    # app/agents/_schemas/music_labels.py.
                    track.ai_labels = ai_labels_dict
                    track.label_version = (
                        str(ai_labels_dict.get("labels", {}).get("label_version", "")) or None
                    )
                if sections_dict is not None:
                    # Phase 2 (auto-music, library side): song_sections picks
                    # top-3 edit-worthy sections. NULL means the agent failed —
                    # matcher excludes the track from auto-mode until backfill
                    # re-runs. See app/agents/_schemas/song_sections.py.
                    track.best_sections = sections_dict.get("sections")
                    track.section_version = sections_dict.get("section_version") or None
                track.analysis_status = "ready"
                db.commit()

        log.info(
            "analyze_music_track_done",
            track_id=track_id,
            beats=len(beats),
            best_start=best_start,
            best_end=best_end,
            n_slots=n_slots,
            has_gemini_recipe=recipe_cached is not None,
            has_ai_labels=ai_labels_dict is not None,
            has_best_sections=sections_dict is not None,
        )

    except Exception as exc:
        log.error("analyze_music_track_failed", track_id=track_id, error=str(exc))
        _fail_track(track_id, str(exc))


# ── orchestrate_music_job ─────────────────────────────────────────────────────


@celery_app.task(
    name="tasks.orchestrate_music_job",
    bind=True,
    max_retries=0,
    soft_time_limit=1080,
    time_limit=1200,
)
def orchestrate_music_job(self, job_id: str) -> None:
    """Full beat-sync pipeline for a music job.

    Dispatches to the templated path (fixed-asset + user-upload slots) when the
    track's cached recipe declares typed slots; otherwise runs the legacy
    beat-sync pipeline that fills every slot from user clips.
    """
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    log.info("music_job_start", job_id=job_id)
    # `pipeline_trace_for` binds job_id into a contextvar so every
    # `record_pipeline_event` call downstream in app/pipeline/* attributes
    # to this job. The finally-block in the context manager clears it on
    # exit (including on exception) so the next Celery task on this worker
    # doesn't inherit a stale job_id.
    with pipeline_trace_for(job_id):
        try:
            if _job_uses_templated_recipe(job_id):
                _run_templated_music_job(job_id)
            else:
                _run_music_job(job_id)
        except Exception as exc:
            log.error("music_job_failed", job_id=job_id, error=str(exc), exc_info=True)
            _fail_job(job_id, str(exc))


def _job_uses_templated_recipe(job_id: str) -> bool:
    """Return True when the job's track has a recipe with typed slots.

    A typed recipe declares per-slot `slot_type` of `fixed_asset` or
    `user_upload`. Legacy recipes either omit `slot_type` or set it to
    `broll` and are routed through `_run_music_job` unchanged.
    """
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or not job.music_track_id:
            return False
        track = db.get(MusicTrack, job.music_track_id)
        if track is None:
            return False
        recipe = track.recipe_cached or {}
    for slot in recipe.get("slots", []):
        if slot.get("slot_type") in ("fixed_asset", "user_upload"):
            return True
    return False


def _run_music_job(job_id: str) -> None:
    """Core music job logic. May raise — caller wraps in try/except."""

    # [1] Load job + track
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("music_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        db.commit()

        music_track_id = job.music_track_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs: list[str] = all_candidates.get("clip_paths", [])

    if not clip_paths_gcs:
        raise ValueError("No clip paths found in job")
    if not music_track_id:
        raise ValueError("Job has no music_track_id")

    # [2] Load music track — must be ready with audio
    with _sync_session() as db:
        track = db.get(MusicTrack, music_track_id)
        if track is None:
            raise ValueError(f"MusicTrack {music_track_id} not found")
        if track.analysis_status != "ready":
            raise ValueError(
                f"MusicTrack {music_track_id} is not ready (status: {track.analysis_status})"
            )
        if not track.audio_gcs_path:
            raise ValueError(
                f"MusicTrack {music_track_id} has no audio_gcs_path — "
                "analysis must have failed silently. Re-analyze the track."
            )

        track_data = {
            "beat_timestamps_s": track.beat_timestamps_s or [],
            "track_config": track.track_config or {},
            "duration_s": track.duration_s,
        }
        audio_gcs_path = track.audio_gcs_path

    # [3] Generate recipe from beats
    recipe_dict = generate_music_recipe(track_data)

    # Build a TemplateRecipe-compatible object (use the existing dataclass)
    from app.pipeline.agents.gemini_analyzer import TemplateRecipe  # noqa: PLC0415

    try:
        recipe = TemplateRecipe(**recipe_dict)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Failed to build TemplateRecipe from music recipe: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="nova_music_job_") as tmpdir:
        # [4] Download clips in parallel
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)

        # [5] Probe clips
        probe_map = _probe_clips(local_clip_paths)

        # [6] Upload to Gemini + analyze in parallel
        log.info("gemini_upload_clips_start", job_id=job_id, count=len(local_clip_paths))
        file_refs = _upload_clips_parallel(local_clip_paths)
        log.info("gemini_upload_clips_done", job_id=job_id)

        log.info("gemini_analyze_clips_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(
            file_refs,
            local_clip_paths,
            probe_map,
            job_id=job_id,
        )

        total = len(clip_metas) + failed_count
        if failed_count > total * 0.5:
            raise GeminiAnalysisError(f"{failed_count}/{total} clips failed Gemini analysis")
        log.info(
            "gemini_analyze_clips_done",
            job_id=job_id,
            success=len(clip_metas),
            failed=failed_count,
        )

        # [7] Enrich slots with energy from beats
        enriched_slots = _enrich_slots_with_energy(
            recipe_dict["slots"],
            track_data["beat_timestamps_s"],
        )
        recipe_dict = {**recipe_dict, "slots": enriched_slots}
        recipe = TemplateRecipe(**recipe_dict)

        # [8] Template match (slot consolidation + greedy assignment)
        log.info("template_match_start", job_id=job_id)
        try:
            recipe = consolidate_slots(recipe, clip_metas)
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        clip_id_to_gcs = {ref.name: gcs for ref, gcs in zip(file_refs, clip_paths_gcs)}
        plan_data = {
            "steps": [
                {
                    "slot": step.slot,
                    "clip_id": step.clip_id,
                    "clip_gcs_path": clip_id_to_gcs.get(step.clip_id) or "",
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

        # [9] FFmpeg assemble
        log.info("ffmpeg_assemble_start", job_id=job_id)
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        clip_id_to_local = {ref.name: path for ref, path in zip(file_refs, local_clip_paths)}
        # Music jobs always take the multi-pass path for Phase 1 of the
        # single-pass rollout (env flag + video_templates.single_pass_enabled).
        # MusicTrack has no equivalent per-track allow-list flag, and music
        # recipes have a different shape (beat-snapped slots, no
        # text_overlays / interstitials) that hasn't been parity-tested
        # against single-pass. The explicit force_single_pass=False here is
        # documentation, not a behavior change — _assemble_clips's gate is
        # now force-only and defaults to False, so omitting the kwarg
        # produces the same routing. Add a MusicTrack.single_pass_enabled
        # flag and lift this explicit-False if/when music joins the
        # single-pass rollout.
        _assemble_clips(
            assembly_plan.steps,
            clip_id_to_local,
            probe_map,
            assembled_path,
            tmpdir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=clip_metas,
            global_color_grade=recipe.color_grade,
            job_id=job_id,
            user_subject="",
            interstitials=[],
            force_single_pass=False,
        )

        # [10] Mix in music track audio
        log.info("mix_audio_start", job_id=job_id)
        final_path = os.path.join(tmpdir, "final.mp4")
        _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError(
                "_mix_template_audio produced empty or missing output — "
                "check FFmpeg audio filter logs above"
            )

        # [11] Upload final video
        from app.storage import upload_public_read  # noqa: PLC0415

        output_gcs = f"music-jobs/{job_id}/output.mp4"
        upload_public_read(final_path, output_gcs)
        log.info("music_job_uploaded", job_id=job_id, gcs_path=output_gcs)

    # [12] Mark done
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job:
            job.status = "music_ready"
            existing_plan = job.assembly_plan or {}
            job.assembly_plan = {**existing_plan, "output_url": output_gcs}
            db.commit()

    log.info("music_job_done", job_id=job_id)


# ── Gemini audio analysis helper ──────────────────────────────────────────────


def _run_gemini_audio_analysis(
    local_audio: str,
    beats: list[float],
    track_config: dict,
    duration_s: float,
    track_id: str,
) -> tuple[dict | None, dict | None, dict | None]:
    """Upload audio to Gemini, run structural analysis + song classifier + song sections.

    Returns ``(recipe_cached, ai_labels, best_sections)``. Each side can
    be ``None`` independently — a Gemini-recipe failure does not block
    label generation, a classifier failure does not block recipe
    caching, and a sections failure does not block either. On a total
    upload failure all three are ``None`` (falls back to beat-only
    recipe, no labels, no sections).
    """
    try:
        log.info("gemini_audio_analysis_start", track_id=track_id)
        file_ref = gemini_upload_and_wait(local_audio, timeout=120)

        gemini_recipe = analyze_audio_template(
            file_ref=file_ref,
            beat_timestamps_s=beats,
            track_config=track_config,
            duration_s=duration_s,
            job_id=f"track:{track_id}",
        )

        # Merge: beat timing (exact) + Gemini visuals (creative)
        track_data = {
            "beat_timestamps_s": beats,
            "track_config": track_config,
            "duration_s": duration_s,
        }
        beat_recipe = generate_music_recipe(track_data)
        merged = merge_audio_recipe(beat_recipe, gemini_recipe)

        # Phase 1 (auto-music): classify the song while we still have
        # the file_ref + audio_template output. Best-effort — the
        # recipe path doesn't depend on labels.
        ai_labels = _run_song_classifier(file_ref, gemini_recipe, track_id)

        # Phase 2 (auto-music, library side): pick top-3 edit-worthy
        # sections. Reuses the same file_ref — no second Gemini upload.
        # Best-effort: matcher's NULL-section filter handles failure.
        sections = _run_song_sections(file_ref, gemini_recipe, beats, duration_s, track_id)

        log.info(
            "gemini_audio_analysis_done",
            track_id=track_id,
            gemini_slots=len(gemini_recipe.get("slots", [])),
            merged_slots=len(merged.get("slots", [])),
            has_ai_labels=ai_labels is not None,
            has_best_sections=sections is not None,
        )
        return merged, ai_labels, sections

    except Exception as exc:
        # Gemini failure is non-fatal: fall back to beat-only recipe
        log.warning(
            "gemini_audio_analysis_failed",
            track_id=track_id,
            error=str(exc),
        )
        # Return beat-only recipe as fallback. Labels and sections
        # can't be produced without the file_ref, so they stay None.
        try:
            track_data = {
                "beat_timestamps_s": beats,
                "track_config": track_config,
                "duration_s": duration_s,
            }
            return generate_music_recipe(track_data), None, None
        except Exception:
            return None, None, None


def _run_song_sections(
    file_ref: Any,
    audio_template_output: dict,
    beats: list[float],
    duration_s: float,
    track_id: str,
) -> dict | None:
    """Best-effort: pick top-3 edit-worthy sections. Never raises.

    Failure is logged and returns ``None``; the matcher will treat the
    track as unsectioned (filtered out of auto-music selection until
    the backfill script reruns).
    """
    if duration_s <= 0.0:
        # SongSectionsInput requires duration_s > 0. A track with no
        # known duration cannot be sectioned — skip silently.
        log.debug("song_sections_skip_no_duration", track_id=track_id)
        return None
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.song_sections import (  # noqa: PLC0415
            SongSectionsAgent,
            SongSectionsInput,
        )

        inp = SongSectionsInput(
            file_uri=file_ref.uri,
            file_mime=getattr(file_ref, "mime_type", None) or "audio/mp4",
            duration_s=float(duration_s),
            beat_timestamps_s=beats,
            audio_template_output=audio_template_output or {},
        )
        out = SongSectionsAgent(default_client()).run(
            inp, ctx=RunContext(job_id=f"track:{track_id}")
        )
        log.info(
            "song_sections_done",
            track_id=track_id,
            section_count=len(out.sections),
            ranks=[s.rank for s in out.sections],
        )
        return out.to_dict()
    except Exception as exc:
        log.warning(
            "song_sections_failed",
            track_id=track_id,
            error=str(exc),
        )
        return None


def _run_song_classifier(
    file_ref: Any,
    audio_template_output: dict,
    track_id: str,
) -> dict | None:
    """Best-effort: produce MusicLabels for the track. Never raises.

    Failure is logged and returns ``None``; the matcher will treat the
    track as unlabeled (filtered out of auto-music selection until the
    backfill script reruns).
    """
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.song_classifier import (  # noqa: PLC0415
            SongClassifierAgent,
            SongClassifierInput,
        )

        inp = SongClassifierInput(
            file_uri=file_ref.uri,
            file_mime=getattr(file_ref, "mime_type", None) or "audio/mp4",
            audio_template_output=audio_template_output or {},
        )
        out = SongClassifierAgent(default_client()).run(
            inp, ctx=RunContext(job_id=f"track:{track_id}")
        )
        log.info(
            "song_classifier_done",
            track_id=track_id,
            genre=out.labels.genre,
            energy=out.labels.energy,
            vibe_tags=out.labels.vibe_tags,
        )
        return out.to_dict()
    except Exception as exc:
        log.warning(
            "song_classifier_failed",
            track_id=track_id,
            error=str(exc),
        )
        return None


# ── Templated music jobs (fixed-asset + user-upload slots) ────────────────────


def _run_templated_music_job(job_id: str) -> None:
    """Pipeline for typed-slot music jobs (e.g. Love From Moon).

    Differences from `_run_music_job`:
      - Recipe is loaded from `track.recipe_cached` verbatim — no beat-snap.
      - Slots split into `fixed_asset` (rendered from a stored image) and
        `user_upload` (filled by the caller's upload). Validates the user
        clip count matches the number of `user_upload` slots.
      - Image inputs (template asset OR user upload) are rendered to mp4
        via `image_clip.render_image_to_clip` before assembly.
      - Skips Gemini / matcher entirely when only one user slot exists or
        when all user inputs are images — there is nothing to match.
    """
    from app.pipeline.agents.gemini_analyzer import (  # noqa: PLC0415
        AssemblyStep,
    )
    from app.pipeline.image_clip import (  # noqa: PLC0415
        SUBTLE_ZOOM_IN,
        ImageClipError,
        is_image_file,
        normalize_to_jpeg,
        render_image_to_clip,
        render_video_to_clip,
    )
    from app.storage import upload_public_read  # noqa: PLC0415

    # [1] Load job + track + recipe
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("music_job_not_found", job_id=job_id)
            return
        job.status = "processing"
        db.commit()

        if not job.music_track_id:
            raise ValueError("Job has no music_track_id")
        all_candidates = job.all_candidates or {}
        clip_paths_gcs: list[str] = all_candidates.get("clip_paths", [])

        track = db.get(MusicTrack, job.music_track_id)
        if track is None:
            raise ValueError(f"MusicTrack {job.music_track_id} not found")
        if not track.audio_gcs_path:
            raise ValueError("MusicTrack has no audio_gcs_path")
        if not track.recipe_cached:
            raise ValueError(
                "Templated music job requires track.recipe_cached (slot_type per slot)"
            )
        recipe_dict: dict = dict(track.recipe_cached)
        audio_gcs_path = track.audio_gcs_path

    slots: list[dict] = recipe_dict.get("slots", []) or []
    if not slots:
        raise ValueError("Recipe has no slots")
    fixed_slots = [s for s in slots if s.get("slot_type") == "fixed_asset"]
    user_slots = [s for s in slots if s.get("slot_type") == "user_upload"]

    if len(user_slots) != len(clip_paths_gcs):
        raise ValueError(
            f"Recipe expects {len(user_slots)} user upload(s) but job "
            f"provided {len(clip_paths_gcs)}"
        )

    log.info(
        "templated_music_job_start",
        job_id=job_id,
        slots=len(slots),
        fixed=len(fixed_slots),
        user=len(user_slots),
    )

    with tempfile.TemporaryDirectory(prefix="nova_lfm_job_") as tmpdir:
        # [2] Render fixed-asset slots
        # Maps slot.position → (clip_id, local_mp4_path)
        slot_position_to_clip: dict[int, tuple[str, str]] = {}
        for slot in fixed_slots:
            position = int(slot.get("position", 0))
            asset_gcs = slot.get("asset_gcs_path")
            if not asset_gcs:
                raise ValueError(f"fixed_asset slot {position} missing asset_gcs_path")
            asset_local = os.path.join(tmpdir, f"asset_{position}_src")
            try:
                download_to_file(asset_gcs, asset_local)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to download fixed asset for slot {position}: {exc}"
                ) from exc

            target_dur = float(slot.get("target_duration_s", 5.0))
            asset_kind = slot.get("asset_kind", "image")
            slot_clip = os.path.join(tmpdir, f"asset_{position}.mp4")

            if asset_kind == "image":
                # Normalize to JPEG (handles HEIC/WEBP) then render to mp4
                norm = os.path.join(tmpdir, f"asset_{position}.jpg")
                try:
                    normalize_to_jpeg(asset_local, norm)
                except ImageClipError:
                    norm = asset_local  # FFmpeg can usually read the original
                motion = _ken_burns_from_slot(slot)
                render_image_to_clip(
                    image_path=norm,
                    output_path=slot_clip,
                    duration_s=target_dur,
                    ken_burns=motion,
                )
            elif asset_kind == "video":
                # Re-encode to output spec by passing through reframe.
                # (Not used for LFM but kept for future templates.)
                from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

                reframe_and_export(
                    input_path=asset_local,
                    start_s=0.0,
                    end_s=target_dur,
                    aspect_ratio="9:16",
                    ass_subtitle_path=None,
                    output_path=slot_clip,
                )
            else:
                raise ValueError(f"Unknown asset_kind {asset_kind!r} for fixed slot {position}")

            clip_id = f"fixed:slot{position}"
            slot_position_to_clip[position] = (clip_id, slot_clip)

        # [3] Download user uploads + decide each slot's effective duration
        # User clips arrive in the order their corresponding user_slots appear
        # in the recipe (slot.position ascending after sort).
        user_slots_sorted = sorted(user_slots, key=lambda s: int(s.get("position", 0)))
        user_local_paths: list[str] = []
        for i, gcs in enumerate(clip_paths_gcs):
            ext = os.path.splitext(gcs)[1].lower() or ".bin"
            local = os.path.join(tmpdir, f"user_{i}{ext}")
            try:
                download_to_file(gcs, local)
            except Exception as exc:
                raise RuntimeError(f"Failed to download user upload {gcs}: {exc}") from exc
            user_local_paths.append(local)

        for slot, user_path in zip(user_slots_sorted, user_local_paths):
            position = int(slot.get("position", 0))
            target_dur = float(slot.get("target_duration_s", 5.0))
            user_clip_id = f"user:slot{position}"

            if is_image_file(user_path):
                # Static image upload → render with default Ken Burns.
                # Use slot.image_duration_s (per-template image default) when
                # set, otherwise fall back to target_duration_s. Photos held
                # at a video's full ceiling tend to drag — templates that
                # accept both kinds usually want a shorter image hold.
                photo_dur = float(slot.get("image_duration_s", target_dur))
                photo_dur = max(0.5, photo_dur)
                norm = os.path.join(tmpdir, f"user_{position}.jpg")
                try:
                    normalize_to_jpeg(user_path, norm)
                except ImageClipError:
                    norm = user_path
                slot_clip = os.path.join(tmpdir, f"user_{position}.mp4")
                render_image_to_clip(
                    image_path=norm,
                    output_path=slot_clip,
                    duration_s=photo_dur,
                    ken_burns=_ken_burns_from_slot(slot, default=SUBTLE_ZOOM_IN),
                )
                # Update slot duration so the audio mix trims to the visual.
                slot["target_duration_s"] = round(photo_dur, 3)
                slot_position_to_clip[position] = (user_clip_id, slot_clip)
            else:
                # Video upload — reframe to 1080x1920, picking the best window.
                # If video shorter than target, use full duration (per user
                # spec: "video ne kadar uzunsa o kadar oynat").
                # Uses image_clip.render_video_to_clip rather than reframe.py
                # because reframe's colorspace filter rejects "unknown" or
                # arib-std-b67 inputs that are common from iPhones / web exports.
                from app.pipeline.probe import probe_video  # noqa: PLC0415

                try:
                    probe = probe_video(user_path)
                    video_dur = float(probe.duration_s or 0.0)
                    aspect_ratio = probe.aspect_ratio or "9:16"
                except Exception as exc:
                    log.warning(
                        "user_clip_probe_failed",
                        path=os.path.basename(user_path),
                        error=str(exc),
                    )
                    video_dur = 0.0
                    aspect_ratio = "9:16"

                if video_dur <= 0:
                    # Probe failed — try the slot duration; if reframe blows
                    # up the orchestrator surfaces a clear error.
                    effective_dur = target_dur
                    start_s = 0.0
                elif video_dur <= target_dur:
                    effective_dur = video_dur
                    start_s = 0.0
                    # Update slot duration so audio mix sees the shorter total.
                    slot["target_duration_s"] = round(video_dur, 3)
                else:
                    # Video longer than slot — use first target_dur seconds.
                    # NOTE: Gemini-driven best-moment selection can be added
                    # later by analyzing this single clip.
                    effective_dur = target_dur
                    start_s = 0.0

                slot_clip = os.path.join(tmpdir, f"user_{position}.mp4")
                render_video_to_clip(
                    input_path=user_path,
                    output_path=slot_clip,
                    start_s=start_s,
                    duration_s=effective_dur,
                    aspect_ratio=aspect_ratio,
                )
                slot_position_to_clip[position] = (user_clip_id, slot_clip)

        # [4] Build assembly directly — no matcher, no beat snap
        slots_sorted = sorted(slots, key=lambda s: int(s.get("position", 0)))
        steps: list[AssemblyStep] = []
        clip_id_to_local: dict[str, str] = {}
        probe_map: dict = {}
        from app.pipeline.probe import VideoProbe  # noqa: PLC0415

        for slot in slots_sorted:
            position = int(slot.get("position", 0))
            entry = slot_position_to_clip.get(position)
            if entry is None:
                raise RuntimeError(f"Slot {position} not bound to any clip — assembly aborted")
            clip_id, local_path = entry
            clip_id_to_local[clip_id] = local_path
            slot_dur = float(slot.get("target_duration_s", 5.0))
            steps.append(
                AssemblyStep(
                    slot=dict(slot),
                    clip_id=clip_id,
                    moment={"start_s": 0.0, "end_s": slot_dur},
                )
            )
            # All pre-rendered slot clips are at output spec (1080x1920, 9:16).
            probe_map[local_path] = VideoProbe(
                duration_s=slot_dur,
                fps=30.0,
                width=1080,
                height=1920,
                has_audio=False,
                codec="h264",
                aspect_ratio="9:16",
                file_size_bytes=os.path.getsize(local_path),
            )

        # [5] Persist plan for the editor preview
        plan_data = {
            "steps": [
                {
                    "slot": step.slot,
                    "clip_id": step.clip_id,
                    "clip_gcs_path": "",  # synthetic clips have no source GCS
                    "moment": step.moment,
                }
                for step in steps
            ],
            "templated": True,
        }
        with _sync_session() as db:
            j = db.get(Job, uuid.UUID(job_id))
            if j:
                j.assembly_plan = plan_data
                db.commit()

        # [6] FFmpeg concat — clips are already at output spec, no reframe.
        # _assemble_clips is intentionally bypassed: its phase-2 re-runs every
        # clip through reframe.py, which fails on inputs with unknown color
        # primaries or arib-std-b67 transfer (common iPhone HLG / web exports).
        log.info("templated_assemble_start", job_id=job_id, steps=len(steps))
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        _concat_pre_rendered_clips(
            [clip_id_to_local[step.clip_id] for step in steps],
            assembled_path,
            tmpdir,
        )

        # [7] Mix in template audio
        log.info("templated_mix_audio_start", job_id=job_id)
        final_path = os.path.join(tmpdir, "final.mp4")
        _mix_template_audio(assembled_path, audio_gcs_path, final_path, tmpdir)
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError("_mix_template_audio produced empty output")

        # [8] Upload result
        output_gcs = f"music-jobs/{job_id}/output.mp4"
        upload_public_read(final_path, output_gcs)
        log.info("templated_music_job_uploaded", job_id=job_id, gcs_path=output_gcs)

    # [9] Mark done
    with _sync_session() as db:
        j = db.get(Job, uuid.UUID(job_id))
        if j:
            j.status = "music_ready"
            existing = j.assembly_plan or {}
            j.assembly_plan = {**existing, "output_url": output_gcs}
            db.commit()
    log.info("templated_music_job_done", job_id=job_id)


def _concat_pre_rendered_clips(
    clip_paths: list[str],
    output_path: str,
    tmpdir: str,
) -> None:
    """Concatenate clips that are already at the output spec into one mp4.

    Uses FFmpeg's concat demuxer (`-f concat`) which copies streams without
    re-encoding when codecs match — every clip we pass here was rendered with
    the same `ultrafast / yuv420p / bt709` settings, so stream copy is safe
    and very fast.

    Templated jobs use this in place of `_assemble_clips`'s join because they
    don't need transitions, interstitials, or per-slot reframing.
    """
    import shutil  # noqa: PLC0415

    if not clip_paths:
        raise ValueError("Cannot concat zero clips")
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], output_path)
        return

    list_file = os.path.join(tmpdir, "concat_list.txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
        # Stream-copy can fail when minor stream metadata differs across clips
        # (e.g. one comes from `image_clip` with no audio, another has audio).
        # Re-encode as a fallback.
        log.warning(
            "concat_demuxer_copy_failed_reencoding",
            stderr=stderr_tail[:300],
        )
        cmd = [
            "ffmpeg",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            "-y",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
        if result.returncode != 0:
            stderr_tail2 = result.stderr.decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"concat re-encode failed (rc={result.returncode}): {stderr_tail2}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"concat produced empty output: {output_path}")


def _ken_burns_from_slot(slot: dict, default=None):  # type: ignore[no-untyped-def]
    """Extract Ken Burns parameters from a recipe slot.

    Recipe shape (all optional):
        slot["ken_burns"] = "zoom_in" | "none"
        slot["zoom_from"] = float, slot["zoom_to"] = float
    """
    from app.pipeline.image_clip import (  # noqa: PLC0415
        NO_MOTION,
        SUBTLE_ZOOM_IN,
        KenBurns,
    )

    motion_name = (slot.get("ken_burns") or "").lower()
    if motion_name == "zoom_in":
        return SUBTLE_ZOOM_IN
    if motion_name == "none":
        return NO_MOTION

    if "zoom_from" in slot and "zoom_to" in slot:
        return KenBurns(
            zoom_from=float(slot["zoom_from"]),
            zoom_to=float(slot["zoom_to"]),
        )

    if default is not None:
        return default
    return NO_MOTION


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fail_job(job_id: str, error_detail: str) -> None:
    try:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "processing_failed"
                job.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                db.commit()
    except Exception as exc:
        log.error("fail_job_db_error", job_id=job_id, error=str(exc))


def _fail_track(track_id: str, error_detail: str) -> None:
    try:
        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
            if track:
                track.analysis_status = "failed"
                track.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                db.commit()
    except Exception as exc:
        log.error("fail_track_db_error", track_id=track_id, error=str(exc))
