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
from sqlalchemy.exc import DBAPIError, OperationalError

from app.agents._runtime import RefusalError
from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.music_recipe import (
    DEFAULT_WINDOW_S,
    auto_best_section,
    count_slots,
    generate_music_recipe,
    merge_audio_recipe,
)
from app.services.lyrics_cache_refresh import ensure_fresh_lyrics_cached_for_render
from app.services.lyrics_config_effective import effective_lyrics_config
from app.services.music_sections import (
    reconcile_track_config_to_rank_one,
    refresh_recipe_cached_for_bounds,
)
from app.storage import download_to_file
from app.tasks.template_orchestrate import (
    _analyze_clips_parallel,
    _assemble_clips,
    _burn_text_overlays,
    _collect_absolute_overlays,
    _download_clips_parallel,
    _enrich_slots_with_energy,
    _mix_template_audio,
    _probe_clips,
    _upload_clips_parallel,
    compute_snapped_slot_durations,
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


def _coerce_best_start_s(track_config: dict | None) -> float:
    """Parse `best_start_s` from a track_config dict, treating None / invalid
    as 0.0. The DB column allows NULL (admins who never set the section
    leave it unset), and pydantic validation upstream may pass NaN/string
    on partial config writes. Crashing the music orchestrator on
    `float(None)` would brick the entire job — this helper keeps the
    pipeline going with the sensible default.
    """
    if not track_config:
        return 0.0
    raw = track_config.get("best_start_s")
    if raw is None:
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return v


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
        "-hide_banner",
        "-v",
        "error",
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
    # Retry on transient Postgres outages (incident 2026-05-18 07:45:57Z).
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,  # deterministic exp backoff — jitter halves avg budget
    # 7 retries × deterministic exp backoff (1+2+4+8+16+32+60 = 123s).
    # retry_jitter=False so the budget is predictable, not half on average.
    # Covers the documented 65s nova-db VM stall (2026-05-18 07:45:57Z)
    # with ~2× safety margin. retry_backoff_max=60 caps any single delay.
    max_retries=7,
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
        # Cleared at the start of every run so a successful re-analyze cannot
        # leave a stale reason from a prior silent-fail attempt. _run_song_sections
        # repopulates this on the broad-Exception branch (best-effort fail-open).
        track.section_error_detail = None
        db.commit()

    try:
        with tempfile.TemporaryDirectory(prefix="nova_music_analyze_") as tmpdir:
            local_audio = os.path.join(tmpdir, "audio.m4a")
            download_to_file(audio_gcs, local_audio)

            beats = _detect_music_beats(local_audio)
            log.info("music_beats_detected", track_id=track_id, count=len(beats))

            # ── Lyric extraction (fail-open, moved before section pick) ──
            # Layer 3: feed full-track lyrics into auto_best_section so the
            # picked window favors vocal-rich passages over beat-only peaks.
            # `best_start_s`/`best_end_s` on LyricsInput are advisory (the agent
            # transcribes the whole track regardless — see
            # `app/agents/lyrics.py`), so passing full-track bounds here costs
            # nothing extra and gives us the lines we need at scoring time.
            # Reuses the already-downloaded audio file; any failure here is
            # surfaced as `lyrics_status` but never blocks `analysis_status=ready`.
            lyrics_result = _run_lyrics_extraction(
                local_audio,
                track_id,
                best_start_s=0.0,
                best_end_s=float(duration_s or 0.0),
                duration_s=float(duration_s or 0.0),
            )
            # `_run_lyrics_extraction` is documented to never raise + always
            # return a status dict, but tests mock it to return None and
            # other call sites have done the same historically — guard so a
            # None doesn't crash the analyze flow before Gemini gets a chance.
            lyric_lines: list[dict] | None = None
            if lyrics_result and lyrics_result.get("status") == "ready":
                lyric_lines = (lyrics_result.get("output") or {}).get("lines") or None

            # Auto-select best section only if not already admin-configured.
            # Pass lyric_lines so the score becomes
            # `beats + 0.5 * overlapping_lyric_lines` (see music_recipe.py:
            # _LYRIC_LINE_WEIGHT). Backward-compat: None falls back to beat-only.
            best_start: float = float(existing_config.get("best_start_s", 0.0))
            best_end: float = float(existing_config.get("best_end_s", 0.0))
            if best_end <= best_start:
                best_start, best_end = auto_best_section(
                    beats,
                    window_s=DEFAULT_WINDOW_S,
                    track_duration_s=float(duration_s or 0.0),
                    lyric_lines=lyric_lines,
                )

            # Slot count from best section
            n = int(existing_config.get("slot_every_n_beats", 8))
            n_slots = count_slots(beats, best_start, best_end, n)

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
            # Populated by _run_song_sections' broad-Exception branch only;
            # see its docstring for the (None, str) vs (None, None) contract.
            sections_error: str | None = None
            if gemini_upload_and_wait is not None and analyze_audio_template is not None:
                (
                    recipe_cached,
                    ai_labels_dict,
                    sections_dict,
                    sections_error,
                ) = _run_gemini_audio_analysis(
                    local_audio,
                    beats,
                    new_config,
                    float(duration_s or 0.0),
                    track_id,
                )

            # ── Section-1 reconciliation (canonical best_section) ────────
            # `auto_best_section()` above wrote a legacy 45s window into
            # new_config. If song_sections returned a usable rank-1, promote
            # its bounds — every downstream consumer (manual job, templated
            # job, admin merge endpoints) reads track_config.best_start_s /
            # best_end_s, so writing rank-1 here fixes them all at once.
            # See app/services/music_sections.py for the gating logic.
            best_section_source = "auto_best_section"
            if sections_dict is not None:
                new_config, best_section_source = reconcile_track_config_to_rank_one(
                    track_config=new_config,
                    beats=beats,
                    sections=sections_dict.get("sections"),
                    section_version=sections_dict.get("section_version"),
                )
                if best_section_source == "song_sections":
                    # Refresh log fields so the structured log at end of
                    # task reflects the canonical bounds, not the legacy
                    # auto_best_section window we computed before.
                    best_start = float(new_config["best_start_s"])
                    best_end = float(new_config["best_end_s"])
                    n_slots = max(1, int(new_config.get("required_clips_max", n_slots)))
                    # Templated music jobs render `recipe_cached` verbatim
                    # (see _run_templated_music_job). If we don't refresh
                    # it against the new bounds, those jobs keep using the
                    # legacy 45s slot timing forever. Manual jobs are safe
                    # because they regenerate from track_config at job
                    # time, but the cache must match for templated paths.
                    if recipe_cached is not None:
                        try:
                            recipe_cached = refresh_recipe_cached_for_bounds(
                                recipe_cached=recipe_cached,
                                beats=beats,
                                track_config=new_config,
                                duration_s=float(duration_s or 0.0),
                            )
                        except Exception as exc:
                            log.warning(
                                "recipe_cached_refresh_failed",
                                track_id=track_id,
                                error=str(exc),
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
                _apply_lyrics_result(track, lyrics_result)
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
                elif sections_error is not None:
                    # Silent-fail branch: persist the truncated reason so admin
                    # can see WHY song_sections returned None for this track
                    # (was already cleared above when analyze started, so a
                    # success-path on a re-analyze cannot leave stale text).
                    track.section_error_detail = sections_error[:MAX_ERROR_DETAIL_LEN]
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
            lyrics_status=lyrics_result.get("status") if lyrics_result else "skipped",
            has_ai_labels=ai_labels_dict is not None,
            has_best_sections=sections_dict is not None,
            best_section_source=best_section_source,
        )

    except OperationalError as db_exc:
        # Transient Postgres outage — re-raise for Celery autoretry. Do NOT
        # mark the track failed; that itself would hit the still-down DB
        # and lock the track at status=failed permanently.
        log.warning(
            "analyze_music_track_transient_db_error_retry",
            track_id=track_id,
            error=str(db_exc),
            retry_count=self.request.retries,
        )
        raise
    except RefusalError as exc:
        retry_count = int(getattr(getattr(self, "request", None), "retries", 0) or 0)
        max_retries = getattr(self, "max_retries", None)
        log.warning(
            "analyze_music_track_song_sections_refusal_retry",
            track_id=track_id,
            error=str(exc),
            retry_count=retry_count,
            max_retries=max_retries,
        )
        if max_retries is not None and retry_count >= int(max_retries):
            log.error(
                "analyze_music_track_song_sections_refusal_exhausted",
                track_id=track_id,
                error=str(exc),
            )
            _fail_track(track_id, str(exc))
            return
        raise self.retry(exc=exc)
    except Exception as exc:
        log.error("analyze_music_track_failed", track_id=track_id, error=str(exc))
        _fail_track(track_id, str(exc))


# ── orchestrate_music_job ─────────────────────────────────────────────────────


@celery_app.task(
    name="tasks.extract_track_lyrics_task",
    bind=True,
    max_retries=0,
    soft_time_limit=180,
    time_limit=240,
)
def extract_track_lyrics_task(self, track_id: str) -> None:
    """Re-run lyric extraction on an already-analyzed track.

    Distinct from `analyze_music_track_task` so the admin can re-extract
    lyrics without re-running beat detection (which is the expensive part
    and rarely needs to change). The audio file is downloaded from GCS into
    a temp dir, exactly the same shape the analyze task uses.
    """
    log.info("extract_track_lyrics_start", track_id=track_id)

    with _sync_session() as db:
        track = db.get(MusicTrack, track_id)
        if track is None:
            log.error("track_not_found_for_lyrics", track_id=track_id)
            return
        if not track.audio_gcs_path:
            with _sync_session() as db2:
                t2 = db2.get(MusicTrack, track_id)
                if t2:
                    t2.lyrics_status = "failed"
                    t2.lyrics_error_detail = "no audio file"
                    db2.commit()
            return
        audio_gcs = track.audio_gcs_path
        cfg = track.track_config or {}
        track_duration_s = float(track.duration_s or 0.0)
        track.lyrics_status = "extracting"
        track.lyrics_error_detail = None
        db.commit()

    try:
        with tempfile.TemporaryDirectory(prefix="nova_lyrics_extract_") as tmpdir:
            local_audio = os.path.join(tmpdir, "audio.m4a")
            download_to_file(audio_gcs, local_audio)
            result = _run_lyrics_extraction(
                local_audio,
                track_id,
                best_start_s=float(cfg.get("best_start_s", 0.0)),
                best_end_s=float(cfg.get("best_end_s", 0.0)),
                duration_s=track_duration_s,
            )
        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
            if track:
                _apply_lyrics_result(track, result)
                db.commit()
        log.info(
            "extract_track_lyrics_done",
            track_id=track_id,
            status=result.get("status") if result else "unknown",
        )
    except Exception as exc:
        log.error("extract_track_lyrics_failed", track_id=track_id, error=str(exc))
        with _sync_session() as db:
            t = db.get(MusicTrack, track_id)
            if t:
                t.lyrics_status = "failed"
                t.lyrics_error_detail = str(exc)[:MAX_ERROR_DETAIL_LEN]
                db.commit()


@celery_app.task(
    name="tasks.orchestrate_music_job",
    bind=True,
    # Retry on transient Postgres outages (incident 2026-05-18 07:45:57Z).
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,  # deterministic exp backoff — jitter halves avg budget
    # 7 retries × deterministic exp backoff (1+2+4+8+16+32+60 = 123s).
    # retry_jitter=False so the budget is predictable, not half on average.
    # Covers the documented 65s nova-db VM stall (2026-05-18 07:45:57Z)
    # with ~2× safety margin. retry_backoff_max=60 caps any single delay.
    max_retries=7,
    # Matched to orchestrate_template_job (1740/1800). Music jobs go through
    # the same heavy `_assemble_clips` → per-slot reframe → ASS-burn → mix
    # pipeline that template jobs do, but historically had a tighter budget
    # because their burn step was lighter. After #258 shipped lyrics, music
    # renders now stage per-word-pop overlays + run the same ASS burn pass,
    # and the legacy 1080/1200 budget no longer covers the same 10-clip
    # iPhone-HEVC-HDR input that templates render comfortably (incident: job
    # ceaed607 hit SoftTimeLimitExceeded at 1080s with lyric burn ~90%
    # complete; empirical phase breakdown:
    #   - Gemini analyze (10 clips parallel, ~57s each):  568s
    #   - Per-slot reframe (10 slots, max 2 workers):     445s
    #   - Overlay collect (Bug B cumulative-pop staging): ~1s
    #   - ASS burn + mix attempt (truncated by SIGKILL):  ~90s
    # Worker's broker_transport_options.visibility_timeout=1900 still has
    # 100s headroom above the new 1800s ceiling.
    soft_time_limit=1740,
    time_limit=1800,
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
        except OperationalError as db_exc:
            # Transient Postgres outage — re-raise for Celery autoretry.
            # See incident 2026-05-18 07:45:57Z (job c13db8a3).
            log.warning(
                "music_job_transient_db_error_retry",
                job_id=job_id,
                error=str(db_exc),
                retry_count=self.request.retries,
            )
            raise
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
        lyrics_config_override = all_candidates.get("lyrics_config_override") or None

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
        lyrics_cached = track.lyrics_cached
        # Lyrics config lives next to the rest of the per-track admin tuning
        # (best_start_s, slot_every_n_beats, etc) so admins can edit
        # everything in one place. See app.routes.admin_music.update_music_track.
        lyrics_config = effective_lyrics_config(track.track_config or {}, lyrics_config_override)
        # Pick a lyric style set from the track's MusicLabels (best-effort).
        lyrics_config = _maybe_select_lyric_style_set(
            lyrics_config, track.ai_labels, track.title or ""
        )

    lyrics_cached = ensure_fresh_lyrics_cached_for_render(
        track_id=music_track_id,
        lyrics_cached=lyrics_cached,
        lyrics_config=lyrics_config,
        reason="music_job",
    )

    # [3] Generate recipe from beats
    recipe_dict = generate_music_recipe(track_data)

    # [3a] Inject lyric overlays (no-op when lyrics_config.enabled=False or
    # track has no cached lyrics). Done BEFORE TemplateRecipe is built so the
    # overlays flow through `_assemble_clips` like any other text overlay.
    #
    # Overwrite target_duration_s with post-beat-snap values FIRST so the
    # injector clamps lyric windows to the slot boundaries _assemble_clips
    # will actually produce (karaoke word-highlight drift fix).
    cfg = track_data["track_config"]
    _beats = list(track_data.get("beat_timestamps_s") or [])
    _snapped = compute_snapped_slot_durations(
        recipe_dict.get("slots") or [],
        _beats,
        is_agentic=False,
        user_total_dur_s=None,
    )
    for _i, _s in enumerate(_snapped):
        recipe_dict["slots"][_i]["target_duration_s"] = _s
    recipe_dict = inject_lyric_overlays(
        recipe_dict,
        lyrics_cached,
        best_start_s=float(cfg.get("best_start_s", 0.0)),
        best_end_s=float(cfg.get("best_end_s", 0.0)),
        lyrics_config=lyrics_config,
    )

    # Build a TemplateRecipe-compatible object (use the existing dataclass).
    # `build_recipe` strips routing/validation-only keys (e.g. required_clips_min)
    # that live on the recipe dict but are not TemplateRecipe constructor kwargs.
    from app.pipeline.agents.gemini_analyzer import build_recipe  # noqa: PLC0415

    try:
        recipe = build_recipe(recipe_dict)
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
        recipe = build_recipe(recipe_dict)

        # [8] Template match (slot consolidation + greedy assignment)
        log.info("template_match_start", job_id=job_id)
        try:
            recipe = consolidate_slots(recipe, clip_metas)
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        # Failed uploads come back as None refs; their fallback ClipMeta uses
        # `clip_{idx}` as clip_id (see _analyze_clips_parallel's Whisper-fallback
        # branch in template_orchestrate.py). Mirror that key here so the
        # matcher's assigned clip_id resolves to a real GCS path / local file
        # for both Gemini-successful and Whisper-fallback clips.
        def _clip_id_for(ref: object | None, idx: int) -> str:
            return ref.name if ref is not None else f"clip_{idx}"

        clip_id_to_gcs = {
            _clip_id_for(ref, i): gcs for i, (ref, gcs) in enumerate(zip(file_refs, clip_paths_gcs))
        }
        plan_data = {
            "steps": [
                {
                    "slot": step.slot,
                    "clip_id": step.clip_id,
                    "clip_gcs_path": clip_id_to_gcs.get(step.clip_id) or "",
                    "moment": step.moment,
                }
                for step in assembly_plan.steps
            ],
            "lyrics_config_effective": lyrics_config,
        }
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.assembly_plan = plan_data
                db.commit()

        # [9] FFmpeg assemble
        log.info("ffmpeg_assemble_start", job_id=job_id)
        assembled_path = os.path.join(tmpdir, "assembled.mp4")
        clip_id_to_local = {
            _clip_id_for(ref, i): path
            for i, (ref, path) in enumerate(zip(file_refs, local_clip_paths))
        }
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
            # Lyric overlays render via Skia (HarfBuzz shaping, paint shadows),
            # matching the templated-music path + the renderer-split intent.
            # Gated globally by settings.text_renderer_skia_enabled inside
            # _burn_text_overlays.
            use_skia=True,
            # Anchor for the line-style lyric audible-window finalizer. The
            # audio mix runs from `best_start_s` for the full rendered video
            # duration; `_collect_absolute_overlays` derives the audible end
            # from this start + the post-snap cumulative durations it already
            # uses for abs timestamps. Same `best_start_s` MUST be passed to
            # `_mix_template_audio` below — drift here equals lyric drift.
            # `_coerce_best_start_s` handles None / NaN / non-numeric DB
            # values without crashing the worker.
            lyric_audio_mix_song_start_s=_coerce_best_start_s(cfg),
        )

        # [10] Mix in music track audio
        # `audio_start_offset_s` MUST match the `best_start_s` passed to
        # `inject_lyric_overlays` above (line 509). Lyric overlays are placed in
        # section-relative time (song-absolute timestamps minus best_start_s);
        # if the audio is mixed without the same offset, FFmpeg plays the song
        # from t=0 while the lyrics describe what's sung at song-time
        # best_start_s — drift equals best_start_s seconds. The latent bug was
        # invisible for music-only output (beat-snap clip cuts feel rhythmic
        # regardless of audio offset) and surfaced the moment lyrics shipped.
        log.info("mix_audio_start", job_id=job_id)
        final_path = os.path.join(tmpdir, "final.mp4")
        _mix_template_audio(
            assembled_path,
            audio_gcs_path,
            final_path,
            tmpdir,
            audio_start_offset_s=float(cfg.get("best_start_s", 0.0)),
        )
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError(
                "_mix_template_audio produced empty or missing output — "
                "check FFmpeg audio filter logs above"
            )

        # [11] Upload final video
        from app.storage import upload_public_read  # noqa: PLC0415

        output_gcs = f"music-jobs/{job_id}/output.mp4"
        # Capture the signed URL — template_orchestrate stores the URL,
        # so the public/admin viewers can use assembly_plan.output_url
        # directly as a <video src>. Was previously discarding this and
        # storing the relative GCS path, which broke direct playback.
        output_url = upload_public_read(final_path, output_gcs)
        log.info("music_job_uploaded", job_id=job_id, gcs_path=output_gcs)

    # [12] Mark done
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job:
            job.status = "music_ready"
            existing_plan = job.assembly_plan or {}
            job.assembly_plan = {**existing_plan, "output_url": output_url}
            db.commit()

    log.info("music_job_done", job_id=job_id)


# ── Lyric extraction helper ───────────────────────────────────────────────────


def _run_lyrics_extraction(
    local_audio: str,
    track_id: str,
    *,
    best_start_s: float,
    best_end_s: float,
    duration_s: float = 0.0,
) -> dict:
    """Call LyricsExtractionAgent and return a status-tagged result.

    Returns one of:
      {"status": "ready",               "output": <dump>, "source": "lrclib_*",
                                        "diagnostic": <dump>, "version_snapshot": int}
      {"status": "needs_manual_lyrics", "whisper_draft": <dump>, "source": "whisper_only",
                                        "diagnostic": <dump>, "version_snapshot": int}
      {"status": "unavailable",         "error": "...", "diagnostic": <dump>?,
                                        "version_snapshot": int}
      {"status": "failed",              "error": "...", "version_snapshot": int}
      {"status": "skipped",             "reason": "..."}        # no version_snapshot

    `version_snapshot` is the `lyrics_extraction_version` read off the track
    at the time the agent was kicked off. `_apply_lyrics_result` uses it for
    conditional UPDATE — if another task has bumped the version in the
    meantime, our mutation is discarded (stale-task protection).

    Never raises — caller is expected to slot whatever is returned into the
    DB via `_apply_lyrics_result`.
    """
    from app.agents._runtime import RunContext, TerminalError  # noqa: PLC0415
    from app.agents.lyrics import (  # noqa: PLC0415
        PUBLISHABLE_LYRICS_SOURCES,
        LyricsExtractionAgent,
        LyricsInput,
    )
    from app.config import settings  # noqa: PLC0415

    # Whisper is required; without it we can't produce timing data. Bail fast.
    if not settings.openai_api_key:
        log.info("lyrics_extract_skipped", reason="OPENAI_API_KEY missing")
        return {"status": "skipped", "reason": "openai_api_key_missing"}

    # Look up the most recent track metadata for the agent input (title/artist
    # may have been edited since dispatch) AND snapshot the extraction version
    # so we can detect stale-task races at commit time.
    forced_lrclib_id: int | None = None
    version_snapshot: int = 0
    try:
        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
            if track is None:
                return {"status": "failed", "error": "track_not_found"}
            title = (track.title or "").strip()
            artist = (track.artist or "").strip()
            version_snapshot = int(track.lyrics_extraction_version or 0)
            # Forced override (admin paste-ID) lives on track_config.lyrics_config.
            cfg = (track.track_config or {}) or {}
            lyrics_cfg = (cfg.get("lyrics_config") or {}) if isinstance(cfg, dict) else {}
            forced_raw = (
                lyrics_cfg.get("forced_lrclib_id") if isinstance(lyrics_cfg, dict) else None
            )
            if forced_raw is not None:
                try:
                    forced_lrclib_id = int(forced_raw)
                    if forced_lrclib_id <= 0:
                        forced_lrclib_id = None
                except (TypeError, ValueError):
                    log.warning(
                        "lyrics_forced_id_invalid_in_config",
                        track_id=track_id,
                        raw=forced_raw,
                    )
                    forced_lrclib_id = None
    except Exception as exc:
        return {"status": "failed", "error": f"db_lookup_failed: {exc}"}

    # Rule-based agents bypass the model client; passing None is safe because
    # Agent.__init__ stores it without calling any methods on it.
    agent = LyricsExtractionAgent(model_client=None)  # type: ignore[arg-type]

    try:
        output = agent.run(
            LyricsInput(
                audio_path=local_audio,
                track_title=title,
                artist=artist,
                best_start_s=float(best_start_s or 0.0),
                best_end_s=float(best_end_s or 0.0),
                duration_s=float(duration_s or 0.0),
                forced_lrclib_id=forced_lrclib_id,
            ),
            ctx=RunContext(job_id=f"track:{track_id}"),
        )
    except TerminalError as exc:
        msg = str(exc)
        # "whisper returned zero words" → instrumental; surface as unavailable
        # so the admin UI can show "no lyrics" instead of "extraction failed".
        if "zero words" in msg or "instrumental" in msg:
            return {
                "status": "unavailable",
                "error": msg[:500],
                "version_snapshot": version_snapshot,
            }
        return {"status": "failed", "error": msg[:500], "version_snapshot": version_snapshot}
    except Exception as exc:  # safety net — agent runtime should already wrap
        return {"status": "failed", "error": str(exc)[:500], "version_snapshot": version_snapshot}

    if output.is_empty:
        return {
            "status": "unavailable",
            "error": "no_lines_after_alignment",
            "diagnostic": output.lyrics_diagnostic,
            "version_snapshot": version_snapshot,
        }

    output_dump = output.model_dump()

    # Publishability decision: only LRCLIB-derived sources count as ready.
    # Whisper-only goes to the draft column with status=needs_manual_lyrics.
    if output.source in PUBLISHABLE_LYRICS_SOURCES:
        return {
            "status": "ready",
            "source": output.source,
            "output": output_dump,
            "diagnostic": output.lyrics_diagnostic,
            "version_snapshot": version_snapshot,
        }
    # Anything else (whisper_only, or any non-LRCLIB source) is non-publishable.
    return {
        "status": "needs_manual_lyrics",
        "source": output.source,
        "whisper_draft": output_dump,
        "diagnostic": output.lyrics_diagnostic,
        "version_snapshot": version_snapshot,
    }


def _apply_lyrics_result(track: MusicTrack, result: dict | None) -> None:
    """Persist the result of `_run_lyrics_extraction` onto the track row.

    Stale-task protection: when the result carries a `version_snapshot`
    that doesn't match the row's current `lyrics_extraction_version`, the
    result is discarded — a newer extraction task has already raced ahead
    and we'd otherwise overwrite its correct output with our stale one.
    """
    if not result:
        return
    from datetime import UTC, datetime  # noqa: PLC0415

    status = result.get("status") or "failed"

    # Stale-task discard. Skipped runs don't have a snapshot (never read a
    # version), so they bypass the check.
    if "version_snapshot" in result:
        current_version = int(track.lyrics_extraction_version or 0)
        snapshot = int(result.get("version_snapshot") or 0)
        if snapshot != current_version:
            log.warning(
                "lyric_extraction_stale_task_discarded",
                track_id=track.id,
                version_snapshot=snapshot,
                current_version=current_version,
                status=status,
            )
            return

    now = datetime.now(UTC)
    if status == "ready":
        track.lyrics_status = "ready"
        track.lyrics_cached = result.get("output")
        track.lyrics_whisper_draft = None  # publishable wins; clear any prior draft
        track.lyrics_source = result.get("source") or "lrclib_synced+whisper"
        track.lyrics_error_detail = None
        track.lyrics_diagnostic = result.get("diagnostic")
        track.lyrics_extracted_at = now
    elif status == "needs_manual_lyrics":
        # Non-publishable extraction (whisper_only). Keep the draft for admin
        # reference but do NOT populate lyrics_cached — production consumers
        # only ever read that column and must not see Whisper hallucinations.
        track.lyrics_status = "needs_manual_lyrics"
        track.lyrics_cached = None
        track.lyrics_whisper_draft = result.get("whisper_draft")
        track.lyrics_source = result.get("source") or "whisper_only"
        track.lyrics_error_detail = "LRCLIB lookup failed; paste a row ID to recover"
        track.lyrics_diagnostic = result.get("diagnostic")
        track.lyrics_extracted_at = now
    elif status == "unavailable":
        track.lyrics_status = "unavailable"
        track.lyrics_cached = None
        track.lyrics_whisper_draft = None
        track.lyrics_error_detail = (result.get("error") or "")[:MAX_ERROR_DETAIL_LEN]
        track.lyrics_diagnostic = result.get("diagnostic") or track.lyrics_diagnostic
        track.lyrics_extracted_at = now
    elif status == "skipped":
        # Don't overwrite a previous successful extraction just because the
        # current run was skipped (e.g. OPENAI_API_KEY temporarily unset).
        if track.lyrics_status not in ("ready", "unavailable", "needs_manual_lyrics"):
            track.lyrics_status = "pending"
    else:  # failed
        track.lyrics_status = "failed"
        track.lyrics_error_detail = (result.get("error") or "")[:MAX_ERROR_DETAIL_LEN]


# ── Gemini audio analysis helper ──────────────────────────────────────────────


def _run_gemini_audio_analysis(
    local_audio: str,
    beats: list[float],
    track_config: dict,
    duration_s: float,
    track_id: str,
) -> tuple[dict | None, dict | None, dict | None, str | None]:
    """Upload audio to Gemini, run structural analysis + song classifier + song sections.

    Returns ``(recipe_cached, ai_labels, best_sections, sections_error)``.
    Each result side can be ``None`` independently — a Gemini-recipe failure
    does not block label generation, a classifier failure does not block
    recipe caching. A song_sections refusal (all proposed sections invalid)
    propagates so the track is visibly failed instead of carrying no
    usable current-version section. Other Gemini failures still fall back
    to a beat-only recipe where possible.

    ``sections_error`` is the truthful reason ``_run_song_sections`` returned
    ``None`` on its broad-Exception branch (or ``None`` when the agent
    succeeded, was skipped for duration ≤ 0, or never ran because the
    outer Gemini upload itself failed — those cases aren't actionable
    "song_sections failed because X" signals).
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
        sections, sections_error = _run_song_sections(
            file_ref, gemini_recipe, beats, duration_s, track_id
        )

        log.info(
            "gemini_audio_analysis_done",
            track_id=track_id,
            gemini_slots=len(gemini_recipe.get("slots", [])),
            merged_slots=len(merged.get("slots", [])),
            has_ai_labels=ai_labels is not None,
            has_best_sections=sections is not None,
        )
        return merged, ai_labels, sections, sections_error

    except RefusalError:
        log.warning(
            "gemini_audio_analysis_song_sections_refused",
            track_id=track_id,
        )
        raise
    except Exception as exc:
        # Gemini failure is non-fatal: fall back to beat-only recipe
        log.warning(
            "gemini_audio_analysis_failed",
            track_id=track_id,
            error=str(exc),
        )
        # Return beat-only recipe as fallback. Labels and sections
        # can't be produced without the file_ref, so they stay None.
        # sections_error stays None here — the failure happened before
        # song_sections could be attempted, so "song_sections failed"
        # would be misleading; the outer log line above carries the
        # actual upload/analyze error.
        try:
            track_data = {
                "beat_timestamps_s": beats,
                "track_config": track_config,
                "duration_s": duration_s,
            }
            return generate_music_recipe(track_data), None, None, None
        except Exception:
            return None, None, None, None


def _run_song_sections(
    file_ref: Any,
    audio_template_output: dict,
    beats: list[float],
    duration_s: float,
    track_id: str,
) -> tuple[dict | None, str | None]:
    """Best-effort: pick top-3 edit-worthy sections.

    Returns ``(sections_dict, error_message)``:
      - ``(dict, None)`` on success — caller writes sections to the track.
      - ``(None, str)`` on a non-Refusal Exception — caller persists the
        message to ``MusicTrack.section_error_detail`` so admin can see
        WHY this track ended up unsectioned without grepping worker logs.
      - ``(None, None)`` when skipped because ``duration_s ≤ 0`` (a track
        with no known duration isn't actionable, so "agent failed" would
        be misleading text on the row).

    ``RefusalError`` (every proposed section violated the hard constraints)
    still propagates and ``analyze_music_track_task`` marks the track failed.
    """
    if duration_s <= 0.0:
        # SongSectionsInput requires duration_s > 0. A track with no
        # known duration cannot be sectioned — skip silently.
        log.debug("song_sections_skip_no_duration", track_id=track_id)
        return None, None
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
        return out.to_dict(), None
    except RefusalError as exc:
        log.warning(
            "song_sections_refused",
            track_id=track_id,
            error=str(exc),
        )
        raise
    except Exception as exc:
        log.warning(
            "song_sections_failed",
            track_id=track_id,
            error=str(exc),
        )
        return None, str(exc)


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


def _maybe_select_lyric_style_set(
    lyrics_config: dict | None,
    ai_labels: dict | None,
    title: str = "",
) -> dict | None:
    """Best-effort: pick a lyric style set via LyricStyleSelectorAgent.

    No-op when lyrics are disabled, a set is already pinned in lyrics_config,
    or the track has no `ai_labels`. Never raises — on any failure the config
    is returned unchanged so lyrics still render with their existing defaults.
    """
    if not lyrics_config or not lyrics_config.get("enabled"):
        return lyrics_config
    if lyrics_config.get("style_set_id"):
        return lyrics_config
    labels_dict = (ai_labels or {}).get("labels")
    if not labels_dict:
        return lyrics_config
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents._schemas.music_labels import MusicLabels  # noqa: PLC0415
        from app.agents.lyric_style_selector import (  # noqa: PLC0415
            LyricStyleSelectorAgent,
            LyricStyleSelectorInput,
        )

        out = LyricStyleSelectorAgent(default_client()).run(
            LyricStyleSelectorInput(labels=MusicLabels(**labels_dict), title=title or ""),
            ctx=RunContext(job_id=None),
        )
        cfg = dict(lyrics_config)
        cfg["style_set_id"] = out.style_set_id
        log.info("lyric_style_set_selected", style_set_id=out.style_set_id)
        return cfg
    except Exception as exc:  # noqa: BLE001 — selection is best-effort
        log.warning("lyric_style_set_select_failed", error=str(exc))
        return lyrics_config


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
        music_track_id_tmpl = job.music_track_id
        all_candidates = job.all_candidates or {}
        clip_paths_gcs: list[str] = all_candidates.get("clip_paths", [])
        lyrics_config_override = all_candidates.get("lyrics_config_override") or None

        track = db.get(MusicTrack, music_track_id_tmpl)
        if track is None:
            raise ValueError(f"MusicTrack {music_track_id_tmpl} not found")
        if not track.audio_gcs_path:
            raise ValueError("MusicTrack has no audio_gcs_path")
        if not track.recipe_cached:
            raise ValueError(
                "Templated music job requires track.recipe_cached (slot_type per slot)"
            )
        recipe_dict: dict = dict(track.recipe_cached)
        audio_gcs_path = track.audio_gcs_path
        lyrics_cached_tmpl = track.lyrics_cached
        track_cfg_tmpl = track.track_config or {}
        lyrics_config_tmpl = effective_lyrics_config(track_cfg_tmpl, lyrics_config_override)
        lyrics_config_tmpl = _maybe_select_lyric_style_set(
            lyrics_config_tmpl, track.ai_labels, track.title or ""
        )

    lyrics_cached_tmpl = ensure_fresh_lyrics_cached_for_render(
        track_id=music_track_id_tmpl,
        lyrics_cached=lyrics_cached_tmpl,
        lyrics_config=lyrics_config_tmpl,
        reason="templated_music_job",
    )

    # Inject lyric overlays for templated tracks. `best_start_s` defaults to 0
    # for templated tracks (slot times already start at the beginning of the
    # audio file).
    #
    # No beat-snap pre-seeding here: _run_templated_music_job bypasses
    # _assemble_clips entirely (_concat_pre_rendered_clips is used instead),
    # so slot durations are never snapped — the recipe's target_duration_s
    # values are already the final assembly boundaries.
    recipe_dict = inject_lyric_overlays(
        recipe_dict,
        lyrics_cached_tmpl,
        best_start_s=float(track_cfg_tmpl.get("best_start_s", 0.0)),
        best_end_s=float(track_cfg_tmpl.get("best_end_s", 0.0))
        or sum(float(s.get("target_duration_s", 0.0)) for s in recipe_dict.get("slots", [])),
        lyrics_config=lyrics_config_tmpl,
    )

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
            "lyrics_config_effective": lyrics_config_tmpl,
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

        # [6a] Burn lyric overlays (templated jobs don't otherwise hit
        # _burn_text_overlays — only lyric overlays survive the templated
        # path; admin-authored text on templated tracks is rare and is
        # tracked as v2). Skipped when injection produced nothing.
        slot_durations = [float(step.slot.get("target_duration_s", 0.0)) for step in steps]
        abs_overlays = _collect_absolute_overlays(
            [{"slot": step.slot, "clip_id": step.clip_id} for step in steps],
            slot_durations,
            clip_metas=None,
            subject="",
            # Anchor for the line-style lyric audible-window finalizer; see
            # the matching call in `_run_music_job`. Templated path uses
            # `audio_start_offset_s = best_start_s` (or 0.0 when admin
            # leaves the section unset) for `_mix_template_audio` below.
            # `_coerce_best_start_s` handles None / NaN safely.
            lyric_audio_mix_song_start_s=_coerce_best_start_s(track_cfg_tmpl),
        )
        if abs_overlays:
            burned_path = os.path.join(tmpdir, "burned.mp4")
            # Music jobs use Skia for lyrics rendering (karaoke-line, per-word-pop).
            # Gated globally by settings.text_renderer_skia_enabled inside _burn_text_overlays.
            _burn_text_overlays(assembled_path, abs_overlays, burned_path, tmpdir, use_skia=True)
            if os.path.exists(burned_path) and os.path.getsize(burned_path) > 0:
                assembled_path = burned_path
            else:
                log.warning(
                    "templated_lyric_burn_empty",
                    job_id=job_id,
                    overlays=len(abs_overlays),
                )

        # [7] Mix in template audio
        # Mirror of the audio-offset fix in `_run_music_job` (see the long
        # comment there). Templated tracks usually have best_start_s == 0
        # (slot times start at the beginning of the audio file per the
        # docstring at the inject_lyric_overlays call above), so this is a
        # latent guard: it only changes behavior if someone configures a
        # non-zero best_start_s on a templated track. Cheap defense.
        log.info("templated_mix_audio_start", job_id=job_id)
        final_path = os.path.join(tmpdir, "final.mp4")
        _mix_template_audio(
            assembled_path,
            audio_gcs_path,
            final_path,
            tmpdir,
            audio_start_offset_s=float(track_cfg_tmpl.get("best_start_s", 0.0)),
        )
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError("_mix_template_audio produced empty output")

        # [8] Upload result
        output_gcs = f"music-jobs/{job_id}/output.mp4"
        output_url = upload_public_read(final_path, output_gcs)
        log.info("templated_music_job_uploaded", job_id=job_id, gcs_path=output_gcs)

    # [9] Mark done
    with _sync_session() as db:
        j = db.get(Job, uuid.UUID(job_id))
        if j:
            j.status = "music_ready"
            existing = j.assembly_plan or {}
            j.assembly_plan = {**existing, "output_url": output_url}
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
    # Hardened against transient DB outages: retry the mark if Postgres is
    # briefly unreachable, so we don't leave the row at status=processing
    # (zombie). Incident 2026-05-18 07:45:57Z.
    for attempt in range(3):
        try:
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job:
                    job.status = "processing_failed"
                    job.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                    db.commit()
            return
        except (OperationalError, DBAPIError) as exc:
            if attempt == 2:
                log.error("fail_job_db_unreachable", job_id=job_id, error=str(exc))
                return
            import time as _time  # noqa: PLC0415

            _time.sleep(1 + attempt)
        except Exception as exc:
            log.error("fail_job_db_error", job_id=job_id, error=str(exc))
            return


def _fail_track(track_id: str, error_detail: str) -> None:
    # Hardened against transient DB outages — see _fail_job.
    for attempt in range(3):
        try:
            with _sync_session() as db:
                track = db.get(MusicTrack, track_id)
                if track:
                    track.analysis_status = "failed"
                    track.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                    track.best_sections = None
                    track.section_version = None
                    db.commit()
            return
        except (OperationalError, DBAPIError) as exc:
            if attempt == 2:
                log.error("fail_track_db_unreachable", track_id=track_id, error=str(exc))
                return
            import time as _time  # noqa: PLC0415

            _time.sleep(1 + attempt)
        except Exception as exc:
            log.error("fail_track_db_error", track_id=track_id, error=str(exc))
            return
