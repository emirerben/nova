"""Celery task: orchestrate_auto_music_job — auto-music multi-variant pipeline.

This is Phase 3 of the auto-music feature (see
``plans/our-current-agentic-template-scalable-gem.md``). It's an
ADDITIVE, SEPARATE orchestrator. The existing
``orchestrate_music_job`` (manual song pick) and
``orchestrate_template_job`` (template mode) are NOT modified — they
stay byte-identical to their pre-Phase-3 behavior. Pinned by
``tests/test_music_mode_manual_unchanged.py``,
``tests/test_template_mode_unchanged.py``, and
``tests/test_reroll_endpoint_unchanged.py``.

Why a separate task module (not a ``mode='auto'`` branch inside
``music_orchestrate.py``)? Two reasons:

1.  ``_run_music_job`` already has a long, beat-snap-specific control
    flow. Adding a top-of-function branch for "if auto: do N variants"
    means every future change to the music path has to consider both
    callers. Splitting keeps the existing path frozen — the only
    regression surface is the orchestrator's *call* into shared
    helpers (``_download_clips_parallel``, ``_analyze_clips_parallel``,
    ``_assemble_clips``, ``_mix_template_audio``), which we deliberately
    REUSE here without forking.

2.  The N-variants fan-out + matcher integration is unique to this
    path. Bundling it next to the single-variant pipeline obscures
    which behavior belongs to which mode.

Flow (per the plan):

  1. Load job + clip GCS paths. Set status=processing.
  2. Download clips + probe (shared helpers).
  3. Upload clips to Gemini + run ``clip_metadata`` ONCE per clip total
     — never per variant. This is the dominant cost; multiplying it
     by K is a non-starter.
  4. Load all ``MusicTrack`` rows where:
       - ``published_at IS NOT NULL``
       - ``ai_labels IS NOT NULL``
       - ``label_version = CURRENT_LABEL_VERSION``
     If zero rows match → status=no_labeled_tracks, fail loudly.
  5. Pre-match filter: drop tracks whose ``slot_count`` after
     ``consolidate_slots(slot_count, n_clips)`` would be degenerate
     (track wants 12 slots, user gave 4 clips → filtered out).
  6. Call ``MusicMatcherAgent.run()`` ONCE with the full filtered
     library + clip summaries → ranked list. Empty ranked list →
     status=matching_failed.
  7. Take the top-K (default K=3, configurable). Fan out IN PARALLEL
     across N threads — each thread:
       a. ``generate_music_recipe(track, clip_count)``
       b. Build the recipe + run ``consolidate_slots`` + ``match()``
       c. ``_assemble_clips`` + ``_mix_template_audio`` (shared)
       d. Upload final → JobClip row with rank, music_track_id,
          match_score, match_rationale.
  8. If all variants failed → status=variants_failed.
     If some failed → status=variants_ready_partial.
     Else → status=variants_ready.

Feature flag: ``settings.enable_auto_music_mode``. When False, this
task early-exits with ``processing_failed`` and a clear error_detail.
The (yet-to-land Phase 4) API route checks the flag at request time;
this server-side check is defense-in-depth so a stale Celery message
can't bypass the gate.

Cost guard (NON-NEGOTIABLE):
  - ``clip_metadata`` runs exactly N times (N = clip count). NOT N×K.
  - ``music_matcher`` runs exactly once.
  - Per-variant LLM calls (transition_picker / text_designer / etc.)
    run K times — that's the *point* of the variant fan-out and is
    acceptable because they're text-only.

Status taxonomy (new in Phase 3, all lowercase-with-underscores per
``Job.status`` convention):

  - ``matching``           — between clip_metadata and matcher call
  - ``rendering``          — between matcher and variant fan-out
  - ``variants_ready``     — all K variants rendered successfully
  - ``variants_ready_partial`` — at least one variant succeeded, at least one failed
  - ``variants_failed``    — every variant attempted failed
  - ``matching_failed``    — matcher returned empty ranked list
  - ``no_labeled_tracks``  — no published tracks have current-version labels
"""

from __future__ import annotations

import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import structlog
from sqlalchemy import select

from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION, MusicLabels
from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, JobClip, MusicTrack
from app.pipeline.music_recipe import generate_music_recipe

# Shared render-path helpers live in template_orchestrate. We REUSE these
# verbatim — no forks. If the shared helper changes, both orchestrators
# move together.
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
        TemplateRecipe,
    )
    from app.pipeline.template_matcher import (
        TemplateMismatchError,
        consolidate_slots,
        match,
    )
except ImportError:  # pragma: no cover — only hit on broken installs
    GeminiAnalysisError = Exception  # type: ignore[assignment, misc]
    TemplateMismatchError = Exception  # type: ignore[assignment, misc]
    TemplateRecipe = None  # type: ignore[assignment]
    consolidate_slots = None  # type: ignore[assignment]
    match = None  # type: ignore[assignment]

log = structlog.get_logger()

MAX_ERROR_DETAIL_LEN = 2000
# Plan default. Configurable per-job via the `n_variants` field on the
# (yet-to-land) POST /auto-music-jobs request; the orchestrator reads it
# off ``job.all_candidates`` so the API doesn't need a new column.
DEFAULT_N_VARIANTS = 3

# Render fan-out concurrency. The plan picks "N variants in parallel,
# each max_workers=1 internally". This is the outer worker count; the
# inner ffmpeg work in _assemble_clips is single-threaded per call.
_RENDER_PARALLELISM = DEFAULT_N_VARIANTS


# ── Public Celery task ────────────────────────────────────────────────────────


@celery_app.task(
    name="tasks.orchestrate_auto_music_job",
    bind=True,
    max_retries=0,
    soft_time_limit=1800,
    time_limit=2000,
)
def orchestrate_auto_music_job(self, job_id: str) -> None:
    """Top-level entry point — wraps ``_run_auto_music_job`` in failure handling.

    Mirrors the existing ``orchestrate_music_job`` shape: never raises;
    any exception becomes ``status=processing_failed`` with
    ``error_detail`` set.
    """
    log.info("auto_music_job_start", job_id=job_id)

    if not settings.enable_auto_music_mode:
        log.warning(
            "auto_music_job_flag_off",
            job_id=job_id,
            reason="ENABLE_AUTO_MUSIC_MODE feature flag is off",
        )
        _fail_job(
            job_id,
            "Auto-music mode is not enabled in this environment.",
            failure_reason="auto_music_disabled",
        )
        return

    # `pipeline_trace_for` binds job_id into a contextvar so every
    # `record_pipeline_event` call inside the assembly pipeline attributes
    # to this job. Cleared on exit (including on exception).
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        try:
            _run_auto_music_job(job_id)
        except Exception as exc:
            log.error(
                "auto_music_job_failed",
                job_id=job_id,
                error=str(exc),
                exc_info=True,
            )
            _fail_job(job_id, str(exc))


# ── Pipeline ──────────────────────────────────────────────────────────────────


def _run_auto_music_job(job_id: str) -> None:
    """Core auto-music pipeline. May raise — caller wraps in try/except."""

    # [1] Load job + clip paths
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("auto_music_job_not_found", job_id=job_id)
            return

        job.status = "processing"
        if job.mode is None:
            job.mode = "auto_music"
        db.commit()

        all_candidates = job.all_candidates or {}
        clip_paths_gcs: list[str] = all_candidates.get("clip_paths", []) or []
        n_variants = int(
            all_candidates.get("n_variants", DEFAULT_N_VARIANTS) or DEFAULT_N_VARIANTS
        )
        n_variants = max(1, min(n_variants, 10))

    if not clip_paths_gcs:
        raise ValueError("Auto-music job has no clip paths in all_candidates")

    log.info(
        "auto_music_job_loaded",
        job_id=job_id,
        n_clips=len(clip_paths_gcs),
        n_variants=n_variants,
    )

    with tempfile.TemporaryDirectory(prefix="nova_auto_music_") as tmpdir:
        # [2] Download + probe + Gemini upload + clip_metadata (ONCE per clip)
        local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)
        probe_map = _probe_clips(local_clip_paths)

        log.info("auto_music_gemini_upload_start", job_id=job_id, n=len(local_clip_paths))
        file_refs = _upload_clips_parallel(local_clip_paths)

        log.info("auto_music_clip_metadata_start", job_id=job_id)
        clip_metas, failed_count = _analyze_clips_parallel(
            file_refs,
            local_clip_paths,
            probe_map,
            job_id=job_id,
        )
        total = len(clip_metas) + failed_count
        if total == 0 or failed_count > total * 0.5:
            raise GeminiAnalysisError(
                f"{failed_count}/{total} clips failed clip_metadata — aborting auto-music job"
            )
        log.info(
            "auto_music_clip_metadata_done",
            job_id=job_id,
            success=len(clip_metas),
            failed=failed_count,
        )

        clip_id_to_gcs = {ref.name: gcs for ref, gcs in zip(file_refs, clip_paths_gcs)}
        clip_id_to_local = {ref.name: path for ref, path in zip(file_refs, local_clip_paths)}

        # [3] Status: matching
        _set_status(job_id, "matching")

        # [4] Load labeled, published tracks + filter
        candidate_tracks = _load_matcher_candidates(len(clip_metas))
        if not candidate_tracks:
            _fail_job(
                job_id,
                (
                    "No music tracks have current-version AI labels. "
                    "Run the song_classifier backfill before retrying."
                ),
                failure_reason="no_labeled_tracks",
                status="no_labeled_tracks",
            )
            return

        log.info(
            "auto_music_matcher_candidates",
            job_id=job_id,
            n_candidates=len(candidate_tracks),
        )

        # [5] Run the matcher (text-only, one call per job)
        ranked = _run_music_matcher(
            clip_metas=clip_metas,
            candidate_tracks=candidate_tracks,
            n_variants=n_variants,
            job_id=job_id,
        )
        if not ranked:
            _fail_job(
                job_id,
                (
                    "music_matcher returned an empty ranked list — no song "
                    "in the library was a confident enough match for these clips."
                ),
                failure_reason="matching_failed",
                status="matching_failed",
            )
            return

        # [6] Resolve picks: take top-K, skipping any track_id that's
        # somehow not in the candidate set (defense-in-depth; the
        # matcher's parse() already filters hallucinations).
        track_by_id: dict[str, MusicTrack] = {t.id: t for t in candidate_tracks}
        picks: list[tuple[MusicTrack, float, str]] = []
        for r in ranked:
            track = track_by_id.get(r["track_id"])
            if track is None:
                log.warning(
                    "auto_music_matcher_skipping_unknown_track",
                    job_id=job_id,
                    track_id=r["track_id"],
                )
                continue
            picks.append((track, r["score"], r["rationale"]))
            if len(picks) >= n_variants:
                break

        if not picks:
            _fail_job(
                job_id,
                "All ranked tracks were filtered out as hallucinated — matcher failed.",
                failure_reason="matching_failed",
                status="matching_failed",
            )
            return

        log.info(
            "auto_music_picks_chosen",
            job_id=job_id,
            n_picks=len(picks),
            track_ids=[p[0].id for p in picks],
        )

        # [7] Status: rendering
        _set_status(job_id, "rendering")

        # [8] Fan out variants in parallel.
        # Each variant runs in its own thread, builds its own recipe and
        # assembly plan, and writes its own JobClip row.
        results = _render_variants_parallel(
            job_id=job_id,
            picks=picks,
            clip_metas=clip_metas,
            clip_id_to_local=clip_id_to_local,
            clip_id_to_gcs=clip_id_to_gcs,
            probe_map=probe_map,
            tmpdir=tmpdir,
        )

    # [9] Terminal status — based on per-variant success
    successes = [r for r in results if r.get("ok")]
    failures = [r for r in results if not r.get("ok")]
    if successes and failures:
        terminal = "variants_ready_partial"
    elif successes:
        terminal = "variants_ready"
    else:
        terminal = "variants_failed"

    _set_status(
        job_id,
        terminal,
        # Capture per-variant outcome in the job's assembly_plan so the
        # UI can show "1 of 3 variants failed" without joining JobClip.
        extra_plan={
            "variants": [
                {
                    "rank": r["rank"],
                    "track_id": r["track_id"],
                    "score": r.get("score"),
                    "output_url": r.get("output_url"),
                    "ok": bool(r.get("ok")),
                    "error": r.get("error"),
                }
                for r in results
            ],
        },
    )
    log.info(
        "auto_music_job_done",
        job_id=job_id,
        terminal=terminal,
        successes=len(successes),
        failures=len(failures),
    )


# ── Candidate loading + matcher invocation ────────────────────────────────────


def _load_matcher_candidates(n_clips: int) -> list[MusicTrack]:
    """Return published, current-label-version tracks the matcher may pick from.

    Two filters:
      - DB-side: ``published_at IS NOT NULL`` AND ``ai_labels IS NOT NULL``
        AND ``label_version = CURRENT_LABEL_VERSION``.
      - Python-side: drop tracks whose slot count is degenerate after
        ``consolidate_slots(slot_count, n_clips)`` — see Critical risk #4
        in the plan. We approximate this by checking that the track's
        recipe's slot count is feasible against the user's clip count:
        if ``slot_count > 2 * n_clips`` we consider it "too many slots"
        and skip. The exact gate uses the matcher's slot_count hint, not
        the post-consolidation count, because consolidate_slots needs the
        clip_metas (which we have) but we want a cheap pre-filter here.
    """
    with _sync_session() as db:
        stmt = select(MusicTrack).where(
            MusicTrack.published_at.is_not(None),
            MusicTrack.ai_labels.is_not(None),
            MusicTrack.label_version == CURRENT_LABEL_VERSION,
            MusicTrack.analysis_status == "ready",
        )
        tracks: list[MusicTrack] = list(db.execute(stmt).scalars().all())
        # Detach: the session closes before the orchestrator finishes,
        # so we eagerly resolve the few attributes we'll need later.
        # SQLAlchemy ORM objects re-fetch on attribute access by default,
        # which raises after the session closes. Loading them inside the
        # with-block side-steps that without an .expunge_all() dance.
        for t in tracks:
            _ = (
                t.id,
                t.title,
                t.duration_s,
                t.audio_gcs_path,
                t.beat_timestamps_s,
                t.track_config,
                t.ai_labels,
                t.label_version,
                t.recipe_cached,
            )
        # Pre-match filter: drop tracks whose slot count is wildly too
        # large for the user's clip count.
        out: list[MusicTrack] = []
        for t in tracks:
            slot_count = _track_slot_count(t)
            if slot_count <= 0:
                continue
            # The conservative-but-cheap heuristic: a track that wants
            # >2x the user's clip count won't survive consolidate_slots
            # (which clamps at the unique-clip floor). Better to drop
            # it from the matcher input than spend an LLM call on a
            # track the renderer will reject.
            if slot_count > max(2 * n_clips, n_clips + 4):
                continue
            out.append(t)
        return out


def _track_slot_count(track: MusicTrack) -> int:
    """Best-effort slot count derived from cached recipe or track_config."""
    recipe = track.recipe_cached or {}
    slots = recipe.get("slots") or []
    if slots:
        return len(slots)
    cfg = track.track_config or {}
    # Fallback: if the analysis stored required_clips_max, use that as a
    # proxy. Beat-detection sets it to the slot count.
    val = cfg.get("required_clips_max") or cfg.get("slot_count") or 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _run_music_matcher(
    *,
    clip_metas: list,
    candidate_tracks: list[MusicTrack],
    n_variants: int,
    job_id: str,
) -> list[dict[str, Any]]:
    """Call the matcher exactly once. Returns the ranked list as plain dicts.

    Returns empty list on refusal / failure. The caller decides whether
    that's terminal.
    """
    # Lazy imports — keeps test-time monkey-patching at the orchestrator
    # boundary clean.
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import AgentError, RunContext  # noqa: PLC0415
    from app.agents.music_matcher import (  # noqa: PLC0415
        ClipSummary,
        MusicMatcherAgent,
        MusicMatcherInput,
        TrackSummary,
    )

    clip_summaries = [_clip_meta_to_summary(ClipSummary, m) for m in clip_metas]
    track_summaries: list[TrackSummary] = []
    for t in candidate_tracks:
        labels_dict = (t.ai_labels or {}).get("labels") or t.ai_labels or {}
        try:
            labels = MusicLabels(**labels_dict)
        except Exception as exc:
            log.warning(
                "auto_music_matcher_track_label_invalid",
                track_id=t.id,
                error=str(exc),
            )
            continue
        track_summaries.append(
            TrackSummary(
                track_id=t.id,
                title=t.title or "",
                duration_s=float(t.duration_s or 0.0),
                slot_count=_track_slot_count(t),
                labels=labels,
            )
        )

    if not track_summaries:
        log.warning("auto_music_matcher_no_valid_track_summaries", job_id=job_id)
        return []

    matcher_input = MusicMatcherInput(
        clip_summaries=clip_summaries,
        available_tracks=track_summaries,
        n_variants=n_variants,
    )

    try:
        result = MusicMatcherAgent(default_client()).run(
            matcher_input, ctx=RunContext(job_id=job_id)
        )
    except AgentError as exc:
        log.warning(
            "auto_music_matcher_failed",
            job_id=job_id,
            error=str(exc),
        )
        return []
    except Exception as exc:
        log.warning(
            "auto_music_matcher_unexpected_failure",
            job_id=job_id,
            error=str(exc),
        )
        return []

    ranked = [
        {
            "track_id": r.track_id,
            "score": float(r.score),
            "rationale": r.rationale,
            "predicted_strengths": list(r.predicted_strengths or []),
        }
        for r in result.ranked
    ]
    log.info(
        "auto_music_matcher_done",
        job_id=job_id,
        n_ranked=len(ranked),
        top_track=ranked[0]["track_id"] if ranked else None,
    )
    return ranked


def _clip_meta_to_summary(ClipSummary, meta):  # type: ignore[no-untyped-def]  # noqa: N803
    """Translate a ClipMeta dataclass into the matcher's ClipSummary shape.

    The matcher only needs a flat summary — subject, hook text, energy,
    duration, media type. Anything more is wasted prompt tokens.
    """
    clip_id = getattr(meta, "clip_id", "") or getattr(meta, "name", "") or "unknown"
    duration = float(getattr(meta, "duration_s", 0.0) or 0.0)
    subject = getattr(meta, "detected_subject", "") or ""
    hook_text = getattr(meta, "hook_text", "") or ""
    # ClipMeta exposes a 0-10 hook_score; energy may be missing on older
    # shapes — default to 5.
    hook_score = float(getattr(meta, "hook_score", 0.0) or 0.0)
    energy = float(getattr(meta, "energy", 5.0) or 5.0)
    # The matcher uses media_type to weight "this is a photo set" vs
    # "this is video". ClipMeta uses ``is_image`` on the recent path.
    is_image = bool(getattr(meta, "is_image", False))
    media_type = "image" if is_image else "video"
    description = getattr(meta, "summary", "") or getattr(meta, "description", "") or ""
    return ClipSummary(
        clip_id=str(clip_id),
        media_type=media_type,
        duration_s=duration,
        subject=str(subject),
        hook_text=str(hook_text),
        hook_score=max(0.0, min(10.0, hook_score)),
        energy=max(0.0, min(10.0, energy)),
        description=str(description),
    )


# ── Variant fan-out ───────────────────────────────────────────────────────────


def _render_variants_parallel(
    *,
    job_id: str,
    picks: list[tuple[MusicTrack, float, str]],
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    clip_id_to_gcs: dict[str, str],
    probe_map: dict,
    tmpdir: str,
) -> list[dict[str, Any]]:
    """Render each picked track as a separate variant in parallel.

    Each variant gets its own subdir under ``tmpdir`` so concurrent
    ffmpeg writes don't collide.
    """
    results: list[dict[str, Any]] = []
    futures = {}
    with ThreadPoolExecutor(max_workers=_RENDER_PARALLELISM) as pool:
        for idx, (track, score, rationale) in enumerate(picks):
            rank = idx + 1
            variant_dir = os.path.join(tmpdir, f"variant_{rank}")
            os.makedirs(variant_dir, exist_ok=True)
            fut = pool.submit(
                _render_one_variant,
                job_id=job_id,
                rank=rank,
                track=track,
                match_score=score,
                match_rationale=rationale,
                clip_metas=clip_metas,
                clip_id_to_local=clip_id_to_local,
                clip_id_to_gcs=clip_id_to_gcs,
                probe_map=probe_map,
                variant_dir=variant_dir,
            )
            futures[fut] = (rank, track.id)
        for fut in as_completed(futures):
            rank, track_id = futures[fut]
            try:
                res = fut.result()
                results.append(res)
            except Exception as exc:
                log.error(
                    "auto_music_variant_thread_failed",
                    job_id=job_id,
                    rank=rank,
                    track_id=track_id,
                    error=str(exc),
                    exc_info=True,
                )
                results.append(
                    {
                        "ok": False,
                        "rank": rank,
                        "track_id": track_id,
                        "error": str(exc)[:MAX_ERROR_DETAIL_LEN],
                    }
                )
    # Order by rank for stable downstream consumption.
    results.sort(key=lambda r: r["rank"])
    return results


def _render_one_variant(
    *,
    job_id: str,
    rank: int,
    track: MusicTrack,
    match_score: float,
    match_rationale: str,
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    clip_id_to_gcs: dict[str, str],
    probe_map: dict,
    variant_dir: str,
) -> dict[str, Any]:
    """Render a single variant. Writes a JobClip row regardless of outcome.

    Returns a dict shaped {ok, rank, track_id, output_url?, error?}.
    Never raises — exceptions are caught and converted into a failure
    record so other variants in the fan-out are unaffected.
    """
    track_id = track.id
    log.info(
        "auto_music_variant_start",
        job_id=job_id,
        rank=rank,
        track_id=track_id,
    )

    try:
        if track.audio_gcs_path is None:
            raise ValueError(f"Track {track_id} has no audio_gcs_path")
        if not track.beat_timestamps_s:
            raise ValueError(f"Track {track_id} has no beat_timestamps_s")

        track_data = {
            "beat_timestamps_s": track.beat_timestamps_s or [],
            "track_config": track.track_config or {},
            "duration_s": track.duration_s,
        }

        # [a] Recipe
        recipe_dict = generate_music_recipe(track_data)

        # [b] Inject the track's MusicLabels copy_tone / transition_style
        # / color_grade so the downstream agents (text_designer,
        # transition_picker, platform_copy) consume the SONG's creative
        # direction, not the beat-only defaults. See plan Phase 3
        # "Inject the track's MusicLabels.copy_tone..." bullet. No agent
        # rewrites — we're just putting the right values in the recipe
        # shape the existing agents already read.
        labels_blob = (track.ai_labels or {}).get("labels") or track.ai_labels or {}
        if isinstance(labels_blob, dict):
            for k in ("copy_tone", "transition_style", "color_grade"):
                v = labels_blob.get(k)
                if v:
                    recipe_dict[k] = v

        if TemplateRecipe is None:
            raise RuntimeError("TemplateRecipe import unavailable — broken install")
        try:
            recipe = TemplateRecipe(**recipe_dict)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(
                f"Failed to build TemplateRecipe from auto-music recipe: {exc}"
            ) from exc

        # [c] Slot energy + match
        enriched_slots = _enrich_slots_with_energy(
            recipe_dict["slots"],
            track_data["beat_timestamps_s"],
        )
        recipe_dict = {**recipe_dict, "slots": enriched_slots}
        recipe = TemplateRecipe(**recipe_dict)

        if consolidate_slots is None or match is None:
            raise RuntimeError("template_matcher import unavailable — broken install")
        try:
            recipe = consolidate_slots(recipe, clip_metas)
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

        # [d] Assemble + mix audio
        assembled_path = os.path.join(variant_dir, "assembled.mp4")
        _assemble_clips(
            assembly_plan.steps,
            clip_id_to_local,
            probe_map,
            assembled_path,
            variant_dir,
            beat_timestamps_s=recipe.beat_timestamps_s,
            clip_metas=clip_metas,
            global_color_grade=recipe.color_grade,
            job_id=f"{job_id}#v{rank}",
            user_subject="",
            interstitials=[],
            force_single_pass=False,
        )
        final_path = os.path.join(variant_dir, "final.mp4")
        _mix_template_audio(assembled_path, track.audio_gcs_path, final_path, variant_dir)
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError(
                f"_mix_template_audio produced empty output for variant {rank}"
            )

        # [e] Upload + JobClip row
        from app.storage import upload_public_read  # noqa: PLC0415

        output_gcs = f"auto-music-jobs/{job_id}/variant_{rank}.mp4"
        upload_public_read(final_path, output_gcs)
        log.info(
            "auto_music_variant_uploaded",
            job_id=job_id,
            rank=rank,
            track_id=track_id,
            gcs_path=output_gcs,
        )

        _write_variant_jobclip(
            job_id=job_id,
            rank=rank,
            track_id=track_id,
            match_score=match_score,
            match_rationale=match_rationale,
            video_path=output_gcs,
            assembly_plan=_assembly_plan_to_dict(assembly_plan, clip_id_to_gcs),
            render_status="ready",
        )
        return {
            "ok": True,
            "rank": rank,
            "track_id": track_id,
            "score": match_score,
            "output_url": output_gcs,
        }
    except Exception as exc:
        err = str(exc)[:MAX_ERROR_DETAIL_LEN]
        log.error(
            "auto_music_variant_failed",
            job_id=job_id,
            rank=rank,
            track_id=track_id,
            error=err,
            exc_info=True,
        )
        # Still record a failed JobClip so the UI can show the tile.
        try:
            _write_variant_jobclip(
                job_id=job_id,
                rank=rank,
                track_id=track_id,
                match_score=match_score,
                match_rationale=match_rationale,
                video_path=None,
                assembly_plan=None,
                render_status="failed",
                error_detail=err,
            )
        except Exception as inner:
            log.error(
                "auto_music_variant_jobclip_write_failed",
                job_id=job_id,
                rank=rank,
                error=str(inner),
            )
        return {
            "ok": False,
            "rank": rank,
            "track_id": track_id,
            "score": match_score,
            "error": err,
        }


def _assembly_plan_to_dict(plan, clip_id_to_gcs: dict[str, str]) -> dict[str, Any]:
    """Mirror the shape ``_run_music_job`` writes to ``job.assembly_plan``."""
    return {
        "steps": [
            {
                "slot": step.slot,
                "clip_id": step.clip_id,
                "clip_gcs_path": clip_id_to_gcs.get(step.clip_id, ""),
                "moment": step.moment,
            }
            for step in plan.steps
        ],
    }


# ── DB helpers ────────────────────────────────────────────────────────────────


def _write_variant_jobclip(
    *,
    job_id: str,
    rank: int,
    track_id: str,
    match_score: float,
    match_rationale: str,
    video_path: str | None,
    assembly_plan: dict[str, Any] | None,
    render_status: str,
    error_detail: str | None = None,
) -> None:
    """Persist a JobClip row for a variant.

    Score fields are populated with the matcher score so the existing
    JobClip non-null guards (hook_score/engagement_score/combined_score
    are all NOT NULL) are satisfied. start_s/end_s mirror the variant's
    duration when we have it; otherwise 0.0 placeholders. The fields
    that genuinely matter for the variant view are ``rank``,
    ``music_track_id``, ``match_score``, ``match_rationale``, and
    ``video_path``.
    """
    with _sync_session() as db:
        clip = JobClip(
            job_id=uuid.UUID(job_id),
            rank=rank,
            # Matcher 0-10 score doubles as a proxy quality signal. We
            # don't have per-clip hook scores at the variant level; the
            # variant is a track, not a clip. These columns are NOT NULL
            # on the model so they get the matcher's score.
            hook_score=float(match_score),
            engagement_score=float(match_score),
            combined_score=float(match_score),
            start_s=0.0,
            end_s=0.0,
            hook_text=None,
            video_path=video_path,
            render_status=render_status,
            music_track_id=track_id,
            match_score=float(match_score),
            match_rationale=(match_rationale or "")[:MAX_ERROR_DETAIL_LEN],
            error_detail=error_detail,
        )
        db.add(clip)
        # Mirror the variant's assembly plan onto the Job row only when
        # the variant succeeded. Multiple successes overwrite each other
        # — the job-level plan is informational; per-variant detail
        # lives on the JobClip + the assembly_plan.variants summary.
        if assembly_plan is not None:
            job = db.get(Job, uuid.UUID(job_id))
            if job is not None and job.assembly_plan is None:
                # Only set if no plan yet — avoids the parallel-write race
                # where two variants overwrite each other's plan with no
                # winner.
                job.assembly_plan = assembly_plan
        db.commit()


def _set_status(
    job_id: str,
    status: str,
    extra_plan: dict[str, Any] | None = None,
) -> None:
    """Update ``Job.status`` (and optionally merge into ``assembly_plan``)."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        job.status = status
        if extra_plan is not None:
            existing = job.assembly_plan or {}
            job.assembly_plan = {**existing, **extra_plan}
        db.commit()


def _fail_job(
    job_id: str,
    error_detail: str,
    failure_reason: str | None = None,
    status: str = "processing_failed",
) -> None:
    """Mark the job as failed with a structured reason.

    Mirrors the existing music_orchestrate._fail_job shape — same
    truncation, same swallow-DB-errors discipline — but lets the
    auto-music path supply a richer ``failure_reason`` (e.g.
    ``no_labeled_tracks`` or ``matching_failed``) for the future UI.
    """
    try:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = status
                job.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                if failure_reason:
                    job.failure_reason = failure_reason
                db.commit()
    except Exception as exc:
        log.error("auto_music_fail_job_db_error", job_id=job_id, error=str(exc))
