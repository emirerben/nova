"""orchestrate_generative_job — the "generative edit" pipeline.

A generative edit has NO reference template and NO pre-selected song. The user
uploads clips; we analyze them, auto-match a song, write our own intro overlay text,
and render THREE variants for the user to choose from:

  - variant 1 "song_lyrics"   : auto-matched song + the song's lyrics as overlays
  - variant 2 "song_text"     : auto-matched song + an AI-written hero-intro overlay
  - variant 3 "original_text" : the clips' ORIGINAL audio + the AI-written intro

This re-anchors on the existing auto-music engine (plan Decision 1): it reuses
`generate_music_recipe` (beats→slots), `music_matcher`, `inject_lyric_overlays`,
`_assemble_clips`, `_mix_template_audio`, and the JobClip variant pattern. The only
net-new render behavior is the no-music branch (variant 3 skips `_mix_template_audio`
to keep source audio) and injecting the agent-authored intro overlay.

Resilience: the song variants are best-effort. If no labeled track matches (or the
matcher fails), variants 1 & 2 are skipped and variant 3 (original audio) still
renders — a generative edit never hard-fails just because the library had no match.
Likewise the agent text is best-effort: if the writer refuses/returns empty, the text
variants render footage without an intro overlay rather than crashing.

The authoritative per-variant state lives in `Job.assembly_plan["variants"]` (this
task owns it), so the API needs no new DB column to distinguish text modes.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError

from app.agents._schemas.edit_format import coerce_edit_format
from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.services.job_phases import mark_failed_phase, mark_finished, mark_started, record_phase
from app.worker import celery_app

log = structlog.get_logger()

MAX_ERROR_DETAIL_LEN = 2000
# Caps the hero intro's reveal/animation window (NOT its display time — the intro is
# held statically for the whole video). A long beat-1 slot shouldn't stretch the
# word-by-word reveal across the entire first clip; it finishes revealing within this
# window, then the full text holds.
MAX_INTRO_S = 3.0
HERO_SLOT_INDEX = 0
# Variant 3 (original audio) arrangement: one slot per clip, capped so a 20-clip
# upload doesn't produce 20 micro-cuts with no song to justify them.
_MAX_NO_MUSIC_SLOTS = 6
# Minimum speech_coverage (0-1) for ANY clip to qualify a talking_head edit. Below
# this the footage carries no usable spoken spine, so the job degrades to montage.
# Deliberately low — silencedetect undercounts quiet/lapel speech; we only want to
# reject footage that is essentially silent (b-roll, ambience, music-over).
_MIN_SPINE_COVERAGE = 0.15

# Voiceover edits: the user's recorded/uploaded voice is the audio bed. `mix` is the
# voice-prominence slider (1.0 = bed fully ducked, voice only; 0.0 = bed full).
# Defaults differ per variant: voice-over-footage starts with footage muted, while
# voice+music starts with the music audibly under the voice. Output is capped so a
# long voiceover can never run past the footage OR the sub-60s short-form ceiling.
_VOICEOVER_ONLY_DEFAULT_MIX = 1.0
_VOICEOVER_MUSIC_DEFAULT_MIX = 0.7
_VOICEOVER_MAX_DURATION_S = 60.0

# Bounded concurrency for the HDR→SDR pre-tonemap. Each conversion is a CPU-bound
# zscale linear-light tonemap + crf16 x264 encode; the prod worker is
# shared-cpu-4x / 6144MB, so 2 concurrent tonemaps is the safe ceiling — more
# risks CPU thrash and the OOM class noted in fly.toml (2026-05-17). Running the
# clips strictly serially was the cause of the >30min "analyzing your clips"
# freeze on heavy 4K/HDR uploads (prod job d30c61fe): 7 clips × 4-8min each blew
# past the task soft_time_limit before any variant rendered.
_PRETONEMAP_MAX_WORKERS = 2

# Terminal statuses a redelivered task (Celery acks_late) must NOT re-run. A job
# killed mid-render is left at "rendering" (not terminal) so the resume path can
# reuse persisted variants; but a job that already failed/finished/cancelled must
# no-op on redelivery rather than repeat the full (expensive) pre-tonemap and
# overwrite a finished result.
_NO_RERUN_STATUSES = frozenset(
    {
        "processing_failed",
        "variants_ready",
        "variants_ready_partial",
        "variants_failed",
        "cancelled",
    }
)


@celery_app.task(
    name="orchestrate_generative_job",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    # time_limit MUST stay under the broker visibility_timeout=1900 (worker.py).
    # With acks_late, a job still in-flight past visibility_timeout is redelivered
    # to a SECOND worker while the first runs — two concurrent HDR pre-tonemap
    # passes fill the RAM-backed /tmp (tmpfs) → "No space left on device"
    # (prod 08532ba3). At 1740/1800 the soft limit fails the job terminal BEFORE
    # 1900, so #419's _NO_RERUN_STATUSES guard no-ops the redelivery. Matches
    # orchestrate_music_job / orchestrate_template_job.
    soft_time_limit=1740,
    time_limit=1800,
)
def orchestrate_generative_job(self, job_id: str) -> None:
    """Entry point. Never raises — any exception becomes processing_failed."""
    log.info("generative_job_start", job_id=job_id)

    from celery.exceptions import SoftTimeLimitExceeded  # noqa: PLC0415

    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        mark_started(job_id)
        try:
            _run_generative_job(job_id)
            mark_finished(job_id)
        except OperationalError:
            raise  # transient DB → Celery autoretry
        except SoftTimeLimitExceeded:
            # The 30-min soft limit fired (heavy 4K/HDR footage). Fail VISIBLY with a
            # user-actionable message instead of letting the hard time_limit SIGKILL
            # freeze the row at status="processing" forever. Not in autoretry_for, so
            # this does not loop back into the same wall.
            log.warning("generative_job_timeout", job_id=job_id)
            mark_failed_phase(job_id)
            _fail_job(
                job_id,
                "Processing timed out — your clips are heavy (likely 4K/HDR). "
                "Try fewer or shorter clips.",
                failure_reason="processing_timeout",
            )
        except Exception as exc:
            log.error("generative_job_failed", job_id=job_id, error=str(exc), exc_info=True)
            mark_failed_phase(job_id)
            _fail_job(job_id, str(exc))


# ── Pipeline ──────────────────────────────────────────────────────────────────


def _run_generative_job(job_id: str) -> None:
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    # Skia kill-switch guard. Agent-text + karaoke reveals have NO Pillow equivalent;
    # if the renderer falls back to Pillow+libass the overlays render wrong or drop.
    # Fail loudly rather than ship garbage. (See CLAUDE.md TEXT_RENDERER_SKIA_ENABLED.)
    if not settings.text_renderer_skia_enabled:
        _fail_job(
            job_id,
            "Generative edits require the Skia text renderer (TEXT_RENDERER_SKIA_ENABLED). "
            "It is disabled in this environment — refusing to render with the Pillow "
            "fallback, which cannot draw the agent-text / karaoke overlays.",
            failure_reason="skia_disabled",
        )
        return

    # Phase: analyze_clips — covers download + probe + Gemini + clip_metadata.
    record_phase(job_id, "queued", next_phase="analyze_clips")

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("generative_job_not_found", job_id=job_id)
            return
        # Redelivery guard (acks_late). If this job already reached a terminal state,
        # a redelivered message must not re-run the whole pipeline (it would repeat
        # the expensive pre-tonemap and clobber a finished result). Mid-render jobs
        # are left at "rendering" — not terminal — so the resume path still works.
        if job.status in _NO_RERUN_STATUSES:
            log.info("generative_job_skip_terminal", job_id=job_id, status=job.status)
            return
        job.status = "processing"
        if job.mode is None:
            job.mode = "generative"
        db.commit()
        all_candidates = job.all_candidates or {}
        clip_paths_gcs: list[str] = all_candidates.get("clip_paths", []) or []
        # Closed allowlist enforced at the API edge; legacy rows default to "en".
        language: str = all_candidates.get("language") or "en"
        # Persona/series context for persona-coherent hooks (content-plan jobs
        # only — public generative jobs omit the key). Forwarded to intro_writer.
        persona: dict = all_candidates.get("persona") or {}
        # Per-user style (Creator Agent M1). Absent on legacy/public jobs →
        # all render branches fall through to today's byte-identical behavior.
        user_style: dict = all_candidates.get("user_style") or {}
        # Plan-declared edit format (Lane A). Coerced defensively — a drifted token
        # falls back to montage rather than failing the job. Resolved against the
        # footage after ingest (see _resolve_archetype).
        edit_format = coerce_edit_format(all_candidates.get("edit_format"))
        # Optional user-supplied voiceover (audio-only). When present it becomes the
        # narration bed and the job renders voiceover variants instead of song/original
        # — resolved in _resolve_archetype below, ahead of the footage-speech logic.
        voiceover_gcs_path: str | None = all_candidates.get("voiceover_gcs_path") or None

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths in all_candidates")

    # ignore_cleanup_errors: on a soft-time-limit abort, an orphaned pre-tonemap
    # ffmpeg thread (outer pool shutdown wait=False) may still be writing sdr_*
    # files into tmpdir as this block unwinds. Without this flag a racing rmtree
    # could raise and MASK the SoftTimeLimitExceeded, routing to the generic
    # handler and losing the actionable "timed out" failure_reason. A temp-dir
    # cleanup error must never shadow the real exception.
    with tempfile.TemporaryDirectory(
        prefix="nova_generative_", ignore_cleanup_errors=True
    ) as tmpdir:
        ingest = _ingest_clips(clip_paths_gcs, tmpdir, job_id=job_id)
        clip_metas = ingest["clip_metas"]
        clip_id_to_gcs = ingest["clip_id_to_gcs"]
        clip_id_to_local = ingest["clip_id_to_local"]
        probe_map = ingest["probe_map"]
        hero = ingest["hero"]
        # The edit can never be longer than the footage the user actually uploaded.
        # This hard ceiling flows into every variant: it shrinks the song's
        # best-section window (music variants) and sizes the no-music arrangement.
        available_footage_s = _available_footage_s(probe_map)
        record_pipeline_event(
            "assembly",
            "clip_metadata_done",
            {"clips": len(clip_metas), "available_footage_s": round(available_footage_s, 3)},
        )

        # The pre-render phase has three independent workstreams that used to run
        # strictly serially (~tonemap + ~20s text/style + up to ~64s matcher).
        # They share no inputs beyond clip_metas/clip_id_to_local, so run them
        # concurrently — the HDR tonemap is an ffmpeg subprocess and the agents
        # are network-bound LLM calls, both of which release the GIL, so threads
        # genuinely overlap. Critical path collapses to the SLOWEST stream.
        #
        #   A) HDR pre-tonemap     — ffmpeg, mutates clip_id_to_local/probe_map.
        #   B) text agents → style — style depends on the intro text, so chained.
        #   C) music matcher       — top-1 track for the song variants.
        #
        # Tonemap (A) ONLY up front, before rendering any variant: the HLG/HDR10 →
        # SDR tonemap is the single most expensive per-slot op (70-123s/slot vs
        # ~16s for SDR — prod job f91ebe67), and a generative job reframes every
        # clip in all three variants, so the same HDR frames were tonemapped up
        # to 3×. Pre-converting to an SDR intermediate (and repointing
        # clip_id_to_local) means every per-slot reframe sees bt709 and skips the
        # tonemap. Generative-only; SDR clips untouched.
        #
        # pipeline_trace contextvar caveat: `record_pipeline_event` reads a
        # contextvar set by `pipeline_trace_for`, which worker threads do NOT
        # inherit — so the trace events are emitted HERE on the main thread after
        # join, never from inside the threads. The agents persist their own
        # agent_run rows via the explicit `job_id` on RunContext, unaffected.
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        def _text_then_style():
            text, form = _run_text_agents(
                clip_metas, hero, job_id=job_id, language=language, persona=persona
            )
            # Creator Agent M1: if the user has a pinned style_set_id, bypass the
            # per-render AgenticStyleSelectorAgent and use it directly. This ensures
            # a consistent visual identity across all of a creator's edits. When
            # absent/disabled, the selector runs as before (byte-identical baseline).
            pinned_set_id = str(user_style.get("style_set_id") or "").strip()
            if pinned_set_id and pinned_set_id != "default":
                from app.pipeline.style_sets import style_set_ids  # noqa: PLC0415

                if pinned_set_id in style_set_ids(applies_to="generative"):
                    log.info(
                        "generative_style_set.user_pinned",
                        job_id=job_id,
                        style_set_id=pinned_set_id,
                    )
                    style = pinned_set_id
                else:
                    # Pinned set no longer in catalog (drift) → fall back to selector.
                    log.info(
                        "generative_style_set.pinned_not_in_catalog",
                        job_id=job_id,
                        pinned=pinned_set_id,
                    )
                    style = _select_generative_style_set(clip_metas, text, job_id=job_id)
            else:
                style = _select_generative_style_set(clip_metas, text, job_id=job_id)
            return text, form, style

        # NOT a `with` block: ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
        # which would BLOCK on the in-flight tonemap thread if the soft time limit
        # fires mid-join — so SoftTimeLimitExceeded can't reach the orchestrator's
        # handler before the hard time_limit SIGKILL freezes the job at
        # status="processing". On the error path we instead shutdown(wait=False,
        # cancel_futures=True) so the exception propagates immediately. Python can't
        # kill a running ffmpeg thread, but each tonemap holds its own 600s timeout
        # and the failing task's worker is recycled, so the orphan is bounded.
        pool = ThreadPoolExecutor(max_workers=3)
        try:
            fut_tonemap = pool.submit(
                _pretonemap_hdr_clips, clip_id_to_local, probe_map, tmpdir, job_id=job_id
            )
            fut_text = pool.submit(_text_then_style)
            fut_match = pool.submit(_match_best_track, clip_metas, job_id=job_id)
            n_tonemapped = fut_tonemap.result()
            agent_text, agent_form, style_set_id = fut_text.result()
            best_track = fut_match.result()
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

        record_pipeline_event("reframe", "hdr_pretonemap_done", {"clips_converted": n_tonemapped})
        record_pipeline_event("overlay", "agent_text_done", {"has_text": bool(agent_text)})
        record_pipeline_event("overlay", "style_set_selected", {"style_set_id": style_set_id})
        record_pipeline_event(
            "assembly", "song_match_done", {"track_id": best_track.id if best_track else None}
        )

        # Compute per-user knob overrides once (cheap, CPU-only) for use across all
        # variants. Empty dict when no style is present → no overrides → baseline.
        user_style_knobs: dict = {}
        if user_style:
            try:
                from app.agents._schemas.user_style import (  # noqa: PLC0415
                    coerce_user_style,
                    user_style_knobs_dict,
                )

                user_style_knobs = user_style_knobs_dict(coerce_user_style(user_style))
            except Exception:  # noqa: BLE001 — defensive; bad blob → no overrides
                pass

        # Phase transition: clip analysis + song match are both complete.
        record_phase(job_id, "analyze_clips", next_phase="match_song")
        record_phase(job_id, "match_song", next_phase="render_variants")

        # [Phase 4/5] Resolve the archetype against the footage, then render its
        # variant set. Default-safe: montage (today's path) unless the plan declares
        # talking_head AND the flag is on AND a clip actually carries speech.
        from app.pipeline.talking_head_assembler import SpineExtractionError  # noqa: PLC0415

        archetype, spine_clip_id = _resolve_archetype(
            edit_format,
            clip_metas,
            clip_id_to_local,
            job_id=job_id,
            voiceover_gcs_path=voiceover_gcs_path,
        )
        _set_status(job_id, "rendering")

        # Resume support. A prior run of THIS job (killed mid-render by a CI
        # deploy / OOM, then redelivered via Celery acks_late) may have already
        # rendered some variants and persisted them below. Reuse any persisted
        # variant whose id AND matched track still apply — only render the rest.
        # The track-id guard matters: _match_best_track re-runs on retry and
        # Gemini may pick a different song, in which case the old song variant is
        # stale and must re-render. The original-audio variant (no track) is
        # always reusable. Net: a deploy that kills the job after two variants
        # costs only the third on retry, not all three.
        prior = {
            v.get("variant_id"): v
            for v in _existing_variants(job_id)
            if v.get("ok") and v.get("output_url")
        }

        def _render_spec_set(
            specs: list[dict[str, Any]], spine: str | None
        ) -> list[dict[str, Any]]:
            """Render every spec, with resume reuse + immediate persist. Lets a
            talking_head SpineExtractionError propagate so the caller can degrade the
            WHOLE job to montage; per-variant non-spine errors become failure records
            inside the render functions."""
            out: list[dict[str, Any]] = []
            for rank, spec in enumerate(specs, start=1):
                variant_id = spec["variant_id"]
                spec_track_id = spec["track"].id if spec["track"] else None
                reusable = prior.get(variant_id)
                if reusable is not None and reusable.get("music_track_id") == spec_track_id:
                    record_pipeline_event(
                        "assembly", "variant_resumed", {"variant_id": variant_id, "rank": rank}
                    )
                    out.append({**reusable, "rank": rank})
                    continue

                # Per-variant render_started_at timestamp (D6 tile clock).
                _update_variant_entry(
                    job_id,
                    variant_id,
                    {
                        "render_status": "rendering",
                        "render_started_at": datetime.utcnow().isoformat() + "Z",
                    },
                )

                variant_dir = os.path.join(tmpdir, f"variant_{rank}")
                os.makedirs(variant_dir, exist_ok=True)
                if spec.get("archetype") == "talking_head":
                    result = _render_talking_head_variant(
                        job_id=job_id,
                        rank=rank,
                        spine_clip_id=spine,
                        clip_metas=clip_metas,
                        clip_id_to_local=clip_id_to_local,
                        probe_map=probe_map,
                        available_footage_s=available_footage_s,
                        agent_text=agent_text,
                        agent_form=agent_form,
                        variant_dir=variant_dir,
                        style_set_id=style_set_id,
                        user_style_knobs=user_style_knobs,
                    )
                else:
                    result = _render_generative_variant(
                        job_id=job_id,
                        rank=rank,
                        spec=spec,
                        clip_metas=clip_metas,
                        clip_id_to_local=clip_id_to_local,
                        clip_id_to_gcs=clip_id_to_gcs,
                        probe_map=probe_map,
                        available_footage_s=available_footage_s,
                        agent_text=agent_text,
                        agent_form=agent_form,
                        variant_dir=variant_dir,
                        style_set_id=style_set_id,
                        user_style_knobs=user_style_knobs,
                    )

                # Per-variant render_finished_at on success (D6 tile clock).
                if result.get("ok"):
                    result["render_finished_at"] = datetime.utcnow().isoformat() + "Z"

                # Persist immediately so a deploy/OOM after this point can't lose it,
                # and so the status endpoint reveals variants as they finish rather
                # than all-at-once at _finalize_job.
                _upsert_variant_entry(job_id, result)
                out.append(result)
            return out

        # Upfront pending-variant upsert: announce all variant IDs the moment the spec
        # set is known — before any render starts. The frontend sees a stable N-tile grid
        # from this point, never from a growing list that pops in mid-render (D7).
        initial_specs = _specs_for_archetype(
            archetype, best_track, voiceover_gcs_path=voiceover_gcs_path
        )
        for spec in initial_specs:
            _upsert_variant_entry(
                job_id,
                {
                    "variant_id": spec["variant_id"],
                    "rank": initial_specs.index(spec) + 1,
                    "text_mode": spec.get("text_mode", "agent_text"),
                    "music_track_id": spec["track"].id if spec.get("track") else None,
                    "track_title": spec["track"].title if spec.get("track") else None,
                    "render_status": "pending",
                    "ok": False,
                },
            )

        try:
            results = _render_spec_set(initial_specs, spine_clip_id)
        except SpineExtractionError as exc:
            # Critical failure mode: a corrupt/unreadable spine clip degrades the whole
            # job to montage rather than hard-failing (best-effort invariant). Any
            # talking_head partials are discarded — _render_spec_set starts montage fresh.
            record_pipeline_event(
                "assembly",
                "archetype_fallback",
                {"declared": edit_format, "reason": "spine_extraction_failed"},
            )
            log.warning("generative_talking_head_degrade_montage", job_id=job_id, error=str(exc))
            # Re-upsert the montage fallback specs so the tile set stays consistent.
            fallback_specs = _variant_specs(best_track)
            for spec in fallback_specs:
                _upsert_variant_entry(
                    job_id,
                    {
                        "variant_id": spec["variant_id"],
                        "rank": fallback_specs.index(spec) + 1,
                        "text_mode": spec.get("text_mode", "agent_text"),
                        "music_track_id": spec["track"].id if spec.get("track") else None,
                        "track_title": spec["track"].title if spec.get("track") else None,
                        "render_status": "pending",
                        "ok": False,
                    },
                )
            results = _render_spec_set(fallback_specs, None)

    record_phase(job_id, "render_variants", next_phase="finalize")
    record_phase(job_id, "finalize")
    _finalize_job(job_id, results)


# ── Ingest (shared by the full job + the single-variant re-render) ──────────────


def _ingest_clips(clip_paths_gcs: list[str], tmpdir: str, *, job_id: str) -> dict[str, Any]:
    """Download → probe → Gemini upload → clip_metadata. Reuses the proven helpers.

    Returns clip_metas, clip_id↔gcs/local maps, the probe map, and the hero clip.
    Raises if more than half the clips fail metadata extraction.
    """
    from app.tasks.template_orchestrate import (  # noqa: PLC0415
        _analyze_clips_parallel,
        _download_clips_parallel,
        _probe_clips,
        _upload_clips_parallel,
    )

    # Mirror the clip_id convention both music orchestrators use: a successful Gemini
    # upload's ref.name, else the `clip_{idx}` synthetic id the Whisper-fallback
    # ClipMeta uses. (Defined locally — the music orchestrators nest this helper.)
    def _clip_id_for(ref: object | None, idx: int) -> str:
        return ref.name if ref is not None else f"clip_{idx}"

    local_clip_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)
    probe_map = _probe_clips(local_clip_paths)
    file_refs = _upload_clips_parallel(local_clip_paths)
    clip_metas, failed_count = _analyze_clips_parallel(
        file_refs, local_clip_paths, probe_map, job_id=job_id
    )
    total = len(clip_metas) + failed_count
    if total == 0 or failed_count > total * 0.5:
        raise ValueError(
            f"{failed_count}/{total} clips failed clip_metadata — aborting generative job"
        )
    return {
        "clip_metas": clip_metas,
        "probe_map": probe_map,
        "clip_id_to_gcs": {
            _clip_id_for(ref, i): gcs for i, (ref, gcs) in enumerate(zip(file_refs, clip_paths_gcs))
        },
        "clip_id_to_local": {
            _clip_id_for(ref, i): path
            for i, (ref, path) in enumerate(zip(file_refs, local_clip_paths))
        },
        "hero": max(clip_metas, key=lambda m: float(getattr(m, "hook_score", 0.0) or 0.0)),
    }


def _pretonemap_hdr_clips(
    clip_id_to_local: dict[str, str],
    probe_map: dict,
    tmpdir: str,
    *,
    job_id: str | None = None,
) -> int:
    """Convert each HLG/HDR10 source to an SDR intermediate ONCE, in place.

    Mutates `clip_id_to_local` (repoints HDR clips at their SDR intermediate)
    and `probe_map` (adds a bt709 probe entry for each intermediate). Returns
    the number of clips converted.

    Why: the HDR→SDR tonemap is by far the most expensive per-slot reframe step
    (zscale linear-light + float upconvert + tonemap). A generative job reframes
    every clip independently in all three variants, so without this the same HDR
    frames are tonemapped up to 3×. Running the tonemap once per clip and feeding
    every variant the resulting bt709 file collapses that to a single pass.

    Concurrency: the per-clip conversions run on a bounded pool
    (`_PRETONEMAP_MAX_WORKERS`) rather than strictly serially — on heavy 4K/HDR
    footage each clip costs 4-8min, and a serial 7-clip loop blew past the task
    soft_time_limit before any variant rendered (prod job d30c61fe). Each ffmpeg
    is CPU-bound and releases the GIL, so threads genuinely overlap. The maps are
    mutated on the calling thread AFTER join (dict mutation isn't thread-safe, and
    the per-slot reframe must see fully-populated maps).

    Parity: reuses `reframe._ZSCALE_SDR_PIPELINE` verbatim (the v0.4.45.7 sky-
    banding fix: linear-light lanczos downscale + mobius tonemap + error-diffusion
    dither). The intermediate is already ~`output_height` tall and tagged bt709,
    so each per-slot reframe then takes the cheap SDR branch (`colorspace` +
    `scale=-2:H` becomes a near-identity resample) — identical geometry to today,
    minus the repeated tonemap. The only added cost is one high-quality
    (crf 16) encode generation per HDR clip. Audio is stream-copied so the
    original-audio variant keeps faithful source audio.
    """
    import subprocess  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415
    from contextlib import nullcontext  # noqa: PLC0415

    from app.pipeline.reframe import (  # noqa: PLC0415
        _HDR10_TRANSFER,
        _HDR_FALLBACK_PIPELINE,
        _HLG_TRANSFER,
        _ZSCALE_SDR_PIPELINE,
        _zscale_available,
    )
    from app.services.pipeline_trace import (  # noqa: PLC0415
        pipeline_trace_for,
        record_pipeline_event,
    )
    from app.tasks.template_orchestrate import _probe_clips  # noqa: PLC0415

    hdr_transfers = {_HLG_TRANSFER, _HDR10_TRANSFER}
    vf = _ZSCALE_SDR_PIPELINE if _zscale_available() else _HDR_FALLBACK_PIPELINE
    # Guard against odd output dimensions (libx264 + yuv420p require even W/H);
    # the linear-light downscale can land on an odd minor axis for non-16:9
    # sources. trunc-to-even crops at most 1px — imperceptible, and only fires
    # on pathological aspect ratios.
    vf = f"{vf},crop=trunc(iw/2)*2:trunc(ih/2)*2"

    # Snapshot the HDR clips up front: stable enumeration → stable `sdr_{idx}`
    # naming (no shared mutable counter across threads) and no mutating
    # clip_id_to_local mid-iteration from worker threads.
    hdr_clips = [
        (idx, clip_id, local_path)
        for idx, (clip_id, local_path) in enumerate(clip_id_to_local.items())
        if probe_map.get(local_path) is not None
        and getattr(probe_map.get(local_path), "color_trc", "bt709") in hdr_transfers
    ]
    if not hdr_clips:
        return 0

    def _convert_one(idx: int, clip_id: str, local_path: str):
        sdr_path = os.path.join(tmpdir, f"sdr_{idx}_{os.path.basename(local_path)}")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            local_path,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            # Intermediate is re-encoded downstream by the per-slot reframe, but
            # this is the one generation that carries the dithered HDR gradient —
            # crf 16/fast (not ultrafast) so x264 doesn't reintroduce the very
            # banding the tonemap's error-diffusion just removed.
            "-crf",
            "16",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-c:a",
            "copy",  # keep source audio for the original-audio variant
            "-movflags",
            "+faststart",
            sdr_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Best-effort: a failed pre-tonemap leaves the HDR clip in place so
            # the per-slot path still tonemaps it (slow but correct). Never abort
            # the job over an optimization.
            stderr = getattr(exc, "stderr", b"")
            log.warning(
                "generative_pretonemap_failed",
                clip_id=clip_id,
                error=str(exc),
                stderr=(stderr[-500:].decode("utf-8", "replace") if stderr else ""),
            )
            return None
        try:
            probe = _probe_clips([sdr_path])[sdr_path]
        except Exception as exc:  # noqa: BLE001
            log.warning("generative_pretonemap_reprobe_failed", clip_id=clip_id, error=str(exc))
            return None
        return (clip_id, sdr_path, probe)

    # `record_pipeline_event` reads a contextvar set by `pipeline_trace_for`, which
    # this thread (Stream A) does NOT inherit from the orchestrator — so we
    # re-establish it here. Forward-progress events keep a slow-but-alive job from
    # looking identical to a hang in the admin job-debug view.
    results: list[tuple[str, str, Any]] = []
    total = len(hdr_clips)
    trace_ctx = pipeline_trace_for(job_id) if job_id is not None else nullcontext()
    with trace_ctx:
        with ThreadPoolExecutor(max_workers=min(_PRETONEMAP_MAX_WORKERS, total)) as pool:
            futs = [pool.submit(_convert_one, *hc) for hc in hdr_clips]
            done = 0
            for fut in as_completed(futs):
                res = fut.result()
                done += 1
                if res is not None:
                    results.append(res)
                record_pipeline_event(
                    "reframe", "pretonemap_progress", {"done": done, "total": total}
                )

    # Mutate the shared maps on the calling thread, after all conversions joined.
    for clip_id, sdr_path, probe in results:
        probe_map[sdr_path] = probe
        clip_id_to_local[clip_id] = sdr_path

    converted = len(results)
    if converted:
        log.info("generative_pretonemap_done", clips_converted=converted)
    return converted


@celery_app.task(
    name="regenerate_generative_variant",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    # Keep under broker visibility_timeout=1900 (worker.py) — see
    # orchestrate_generative_job above for the acks_late double-run rationale.
    soft_time_limit=1740,
    time_limit=1800,
)
def regenerate_generative_variant(
    self,
    job_id: str,
    variant_id: str,
    new_track_id: str | None = None,
    override_text: str | None = None,
    remove_text: bool = False,
    style_set_id: str | None = None,
    size_override_px: int | None = None,
    mix_override: float | None = None,
) -> None:
    """Re-render ONE variant of a generative job (swap-song / retext / restyle / resize / mix).

    Async by design (plan Decision 4): a re-slot against a new song is a full pipeline
    re-run, not an instant preview. Re-runs clip ingest, renders just the target
    variant, and updates that entry in `Job.assembly_plan["variants"]` in place.
    """
    log.info(
        "generative_regenerate_start",
        job_id=job_id,
        variant_id=variant_id,
        new_track_id=new_track_id,
        remove_text=remove_text,
        has_override=bool(override_text),
        style_set_id=style_set_id,
    )
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        try:
            _run_regenerate_variant(
                job_id,
                variant_id,
                new_track_id,
                override_text,
                remove_text,
                style_set_id,
                size_override_px,
                mix_override,
            )
        except OperationalError:
            raise
        except Exception as exc:
            log.error(
                "generative_regenerate_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc),
                exc_info=True,
            )
            _update_variant_entry(
                job_id,
                variant_id,
                {
                    "render_status": "failed",
                    "ok": False,
                    "error": str(exc),
                    "error_class": _classify_error(exc),
                },
            )


def _resolve_regen_text(
    override_text: str | None,
    remove_text: bool,
    existing_text_mode: str | None,
    persisted_text: str | None,
    persisted_highlight: str | None,
    *,
    run_text_agents_fn,  # callable: () -> (agent_text, agent_form)
) -> tuple:
    """Return (agent_text, agent_form, text_mode) without running the LLM when possible.

    Priority:
    1. remove_text → text_mode="none", no overlay.
    2. override_text → sanitised SimpleNamespace, mode unchanged.
    3. persisted intro_text present + mode agent_text → reuse (no LLM).
    4. else → run intro_writer (first render or legacy variant).
    """
    import types as _types  # noqa: PLC0415

    if remove_text:
        return None, None, "none"

    if override_text:
        # Reuse existing sanitise chain (same as _run_regenerate_variant original path).
        # Strip ASS tags / control chars / URLs / handles and clamp length.
        from app.agents.intro_writer import _clamp, _strip_unsafe_tokens  # noqa: PLC0415
        from app.agents.text_alignment import _sanitize_aligned_line  # noqa: PLC0415

        cleaned = _clamp(_strip_unsafe_tokens(_sanitize_aligned_line(override_text)))
        if cleaned:
            agent_text = _types.SimpleNamespace(text=cleaned, highlight_word=None)
            agent_form = {"effect": "karaoke-line"}
            return agent_text, agent_form, existing_text_mode or "agent_text"
        # Nothing renderable after sanitization → no overlay (footage only).
        return None, None, existing_text_mode or "agent_text"

    if persisted_text and existing_text_mode == "agent_text":
        # Reuse persisted text — NO LLM call.
        agent_text = _types.SimpleNamespace(text=persisted_text, highlight_word=persisted_highlight)
        agent_form = {"effect": "karaoke-line"}
        return agent_text, agent_form, "agent_text"

    # Fall through: run intro_writer (first render or legacy variant without persisted text).
    agent_text, agent_form = run_text_agents_fn()
    # Preserve existing_text_mode when the LLM returns None (e.g. text-removed variant
    # that somehow reaches here); avoids corrupting text_mode from "none" to "agent_text".
    mode = "agent_text" if agent_text is not None else (existing_text_mode or "none")
    return agent_text, agent_form, mode


def _is_fast_reburn_eligible(
    existing: dict,
    new_track_id: str | None,
    mix_override,
    settings,
) -> bool:
    """True iff this edit can use the cached-base fast-reburn path."""
    if not getattr(settings, "GENERATIVE_FAST_REBURN_ENABLED", True):
        return False
    if new_track_id is not None or mix_override is not None:
        return False  # audio changes → must full re-render
    if not existing.get("base_video_path"):
        return False  # no cached base (legacy or lyrics variant)
    text_mode = existing.get("text_mode")
    if text_mode not in ("agent_text", "none"):
        return False  # lyrics variants: full path in v1
    return True


def _reburn_text_on_base(
    *,
    job_id: str,
    variant_id: str,
    existing: dict,
    agent_text,
    agent_form: dict | None,
    text_mode: str,
    resolved_style_set_id: str | None,
    size_override_px: int | None,
    settings,
) -> dict:
    """Fast reburn: download base → rebuild overlay → burn → upload.

    Returns a partial variant patch dict (same keys as _update_variant_entry expects).
    Raises on unrecoverable failure (base download error handled by caller via fallback).
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from app.pipeline.generative_overlays import build_persistent_intro_overlays  # noqa: PLC0415
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415

    base_gcs_path = existing["base_video_path"]

    with tempfile.TemporaryDirectory(prefix="nova_reburn_") as tmpdir:
        local_base = os.path.join(tmpdir, "base.mp4")

        # Download the cached base
        download_to_file(base_gcs_path, local_base)

        # Resolve size (pixel-stability rule)
        existing_size_px = existing.get("intro_text_size_px")
        existing_size_source = existing.get("intro_size_source", "computed")
        if size_override_px is not None:
            final_size_px = size_override_px
            final_size_source = "user"
        elif existing_size_source == "user" and existing_size_px:
            final_size_px = existing_size_px
            final_size_source = "user"
        else:
            # computed_fallback_px: if set has no px, fall back to persisted px
            # instead of hero-less recompute.
            final_size_px = existing_size_px  # may be None; resolver handles it
            final_size_source = "computed"

        # Probe base duration for reveal window
        try:
            base_dur = float(probe_video(local_base).duration_s)
        except Exception:  # noqa: BLE001
            base_dur = MAX_INTRO_S
        reveal_window_s = min(base_dur, MAX_INTRO_S) if base_dur > 0 else MAX_INTRO_S

        final_path = os.path.join(tmpdir, "final.mp4")
        intro_px = existing_size_px
        intro_source = existing_size_source

        if agent_text is not None and text_mode == "agent_text":
            # Always pass final_size_px as size_override_px so we never try to
            # recompute via compute_overlay_size (which needs hero-clip metadata
            # we don't have on the fast-reburn path). The "user" vs "computed"
            # distinction is preserved via final_size_source returned below.
            params, intro_px, intro_source = _resolve_intro_overlay_params(
                agent_text,
                agent_form or {"effect": "karaoke-line"},
                resolved_style_set_id,
                size_override_px=final_size_px,
            )
            # Preserve the original source label — size_override_px always wins
            # inside the resolver, which would label it "user"; restore the real
            # source so pixel-stability logic downstream remains correct.
            intro_source = final_size_source
            overlays = build_persistent_intro_overlays(
                reveal_window_s=reveal_window_s,
                beats=[],  # even-split reveal; talking-head precedent
                **params,
            )
            burn_text_overlays_skia(local_base, overlays, final_path, tmpdir)

            # Detect silent copy-through (burn failed with non-empty overlays)
            if (
                overlays
                and os.path.exists(final_path)
                and os.path.getsize(final_path) == os.path.getsize(local_base)
            ):
                raise RuntimeError(
                    f"burn_text_overlays_skia copy-through detected on {base_gcs_path}; "
                    "marking reburn as failed"
                )
        else:
            # remove_text or none mode
            shutil.copy2(local_base, final_path)

        # Upload final to same variant key
        variant_gcs_key = (existing.get("video_path") or "").lstrip("/")
        output_url = upload_public_read(final_path, variant_gcs_key)

        return {
            "intro_text": agent_text.text if agent_text else None,
            "intro_highlight_word": (
                getattr(agent_text, "highlight_word", None) if agent_text else None
            ),
            "intro_text_size_px": intro_px if agent_text else existing_size_px,
            "intro_size_source": intro_source if agent_text else existing_size_source,
            "style_set_id": resolved_style_set_id,
            "text_mode": text_mode,
            "render_status": "ready",
            "ok": True,
            "render_finished_at": datetime.utcnow().isoformat() + "Z",
            "video_path": variant_gcs_key,
            "output_url": output_url,
            # base_video_path unchanged (still valid for next edit)
        }


def _run_regenerate_variant(
    job_id: str,
    variant_id: str,
    new_track_id: str | None,
    override_text: str | None,
    remove_text: bool,
    style_set_id: str | None = None,
    size_override_px: int | None = None,
    mix_override: float | None = None,
) -> None:
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("generative_regenerate_job_not_found", job_id=job_id)
            return
        clip_paths_gcs = (job.all_candidates or {}).get("clip_paths", []) or []
        # Re-renders inherit the language the user chose at job creation. Legacy
        # jobs (pre-language-field) default to "en". Frontend NEVER passes language
        # on retext/swap_song/change_style — single source of truth is the Job row.
        language: str = (job.all_candidates or {}).get("language") or "en"
        # Re-renders inherit the persona context too (content-plan jobs only), so a
        # retext/swap_song hook stays persona-coherent. Same Job-row source of truth.
        persona: dict = (job.all_candidates or {}).get("persona") or {}
        # Voiceover jobs re-render the same voice bed; the mix slider is the only knob
        # that changes here. Both come from the Job row (single source of truth).
        voiceover_gcs_path: str | None = (job.all_candidates or {}).get(
            "voiceover_gcs_path"
        ) or None
        variants = ((job.assembly_plan or {}).get("variants")) or []
        existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
        if existing is None:
            log.error("generative_regenerate_variant_unknown", job_id=job_id, variant_id=variant_id)
            return
        rank = int(existing.get("rank", 1))
        existing_track_id = existing.get("music_track_id")
        existing_text_mode = existing.get("text_mode", "agent_text")
        existing_mix = existing.get("mix")
        existing_size_source = existing.get("intro_size_source")
        existing_size_px = existing.get("intro_text_size_px")
        # User-style knobs persisted on the variant entry (Creator Agent M1). Reading
        # from the variant (not the persona row) so re-renders are hermetic — the
        # persona style could have changed between first-render and re-render.
        existing_user_style_knobs: dict | None = existing.get("user_style_knobs") or None
        # Persisted intro text — used by _resolve_regen_text to skip re-running
        # intro_writer on font/size/style edits where the user's text hasn't changed.
        persisted_text: str | None = existing.get("intro_text") or None
        persisted_highlight: str | None = existing.get("intro_highlight_word") or None
        # Style precedence: explicit restyle request → the variant's persisted set →
        # any sibling variant's set (the job-level default) → "default".
        existing_style_set_id = existing.get("style_set_id")
        if existing_style_set_id is None:
            existing_style_set_id = next(
                (v.get("style_set_id") for v in variants if v.get("style_set_id")), None
            )
    resolved_style_set_id = style_set_id or existing_style_set_id or "default"

    # Intro-size precedence on a re-render:
    #   explicit resize request       → new user pin
    #   prior pin was the user's      → preserve it (swap-song/retext must not recompute
    #                                  over a size the user set by hand)
    #   prior source was "user_style" → re-apply via user_style_knobs (M1; the knobs
    #                                  path handles this; don't double-pin in override)
    #   otherwise                     → None → recompute from the hero clip's composition
    if size_override_px is not None:
        resolved_size_override_px = int(size_override_px)
    elif existing_size_source == "user" and existing_size_px is not None:
        resolved_size_override_px = int(existing_size_px)
    else:
        resolved_size_override_px = None

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths to re-render from")

    # Mark this variant as re-rendering so the UI can show a spinner immediately.
    _update_variant_entry(
        job_id, variant_id, {"render_status": "rendering", "ok": False, "error": None}
    )

    # ── Fast-reburn path ──────────────────────────────────────────────────────
    # When the edit is a pure text/style/size change (no audio change, base cached),
    # skip the full clip ingest + re-assemble. Download the base → reburn text → done.
    if _is_fast_reburn_eligible(existing, new_track_id, mix_override, settings):
        # Resolve text WITHOUT ingest (no LLM needed: persisted text or override).
        # The run_text_agents_fn is never called on the fast path because eligibility
        # already guarantees base_video_path is set, which only happens after a
        # successful full render that persists intro_text.
        fast_agent_text, fast_agent_form, fast_text_mode = _resolve_regen_text(
            override_text=override_text,
            remove_text=remove_text,
            existing_text_mode=existing_text_mode,
            persisted_text=persisted_text,
            persisted_highlight=persisted_highlight,
            run_text_agents_fn=lambda: (None, None),  # unreachable: persisted text exists
        )
        _used_fast_path = False
        try:
            result = _reburn_text_on_base(
                job_id=job_id,
                variant_id=variant_id,
                existing=existing,
                agent_text=fast_agent_text,
                agent_form=fast_agent_form,
                text_mode=fast_text_mode,
                resolved_style_set_id=resolved_style_set_id,
                size_override_px=resolved_size_override_px,
                settings=settings,
            )
            _used_fast_path = True
        except Exception as _fast_exc:  # noqa: BLE001
            # If the base blob is gone (GCS lifecycle, deleted base, etc.), fall
            # through to the full re-render path rather than surfacing an error.
            # Detect by checking for Google/GCS NotFound or generic download signals.
            _exc_type = type(_fast_exc).__name__
            _is_missing = (
                "NotFound" in _exc_type
                or "not found" in str(_fast_exc).lower()
                or "does not exist" in str(_fast_exc).lower()
                or "no such object" in str(_fast_exc).lower()
            )
            if _is_missing:
                log.warning(
                    "generative_fast_reburn_base_missing",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(_fast_exc),
                )
                # Fall through to full path below.
            else:
                raise
        if _used_fast_path:
            _update_variant_entry(job_id, variant_id, result)
            return
    # ── /Fast-reburn path ─────────────────────────────────────────────────────

    # Resolve the track for the new spec.
    track: MusicTrack | None = None
    track_id = new_track_id or existing_track_id
    if track_id:
        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
        if track is None or track.analysis_status != "ready" or not track.audio_gcs_path:
            raise ValueError(f"Track {track_id} is not available for re-render")

    with tempfile.TemporaryDirectory(prefix="nova_generative_re_") as tmpdir:
        # PERF/TODO: this re-runs the full clip ingest (re-download + re-Gemini
        # clip_metadata) on every swap/retext, even remove_text. Acceptable for v1
        # (async re-render, reject-if-rendering guard caps spam), but the Gemini
        # analysis is cacheable — persist clip_metas after the first orchestrate run
        # and skip re-analysis here (only re-download + re-probe are truly needed for
        # the render). Follow-up once the feature is real-render-verified.
        ingest = _ingest_clips(clip_paths_gcs, tmpdir, job_id=job_id)
        available_footage_s = _available_footage_s(ingest["probe_map"])

        # Resolve text mode + agent_text in one step.  _resolve_regen_text re-uses
        # persisted intro_text when the user is only changing font/size/style — no LLM.
        agent_text, agent_form, text_mode = _resolve_regen_text(
            override_text=override_text,
            remove_text=remove_text,
            existing_text_mode=existing_text_mode,
            persisted_text=persisted_text,
            persisted_highlight=persisted_highlight,
            run_text_agents_fn=lambda: _run_text_agents(
                ingest["clip_metas"],
                ingest["hero"],
                job_id=job_id,
                language=language,
                persona=persona,
            ),
        )

        spec: dict[str, Any] = {
            "variant_id": variant_id,
            "rank": rank,
            "text_mode": text_mode,
            "track": track,
        }
        # Voiceover variant re-render (e.g. the mix slider): re-attach the voice bed and
        # the resolved mix. Precedence: explicit slider value → the variant's persisted
        # mix → the per-variant default. The track (voiceover_music's bed) is already
        # resolved above via existing_track_id.
        if voiceover_gcs_path and variant_id in ("voiceover_only", "voiceover_music"):
            if mix_override is not None:
                resolved_mix = max(0.0, min(1.0, float(mix_override)))
            elif existing_mix is not None:
                resolved_mix = max(0.0, min(1.0, float(existing_mix)))
            elif variant_id == "voiceover_music":
                resolved_mix = _VOICEOVER_MUSIC_DEFAULT_MIX
            else:
                resolved_mix = _VOICEOVER_ONLY_DEFAULT_MIX
            spec["voiceover_gcs_path"] = voiceover_gcs_path
            spec["mix"] = resolved_mix

        variant_dir = os.path.join(tmpdir, f"variant_{rank}")
        os.makedirs(variant_dir, exist_ok=True)
        result = _render_generative_variant(
            job_id=job_id,
            rank=rank,
            spec=spec,
            clip_metas=ingest["clip_metas"],
            clip_id_to_local=ingest["clip_id_to_local"],
            clip_id_to_gcs=ingest["clip_id_to_gcs"],
            probe_map=ingest["probe_map"],
            available_footage_s=available_footage_s,
            agent_text=agent_text,
            agent_form=agent_form,
            variant_dir=variant_dir,
            style_set_id=resolved_style_set_id,
            intro_size_override_px=resolved_size_override_px,
            user_style_knobs=existing_user_style_knobs,
        )

    _update_variant_entry(job_id, variant_id, result)


def _existing_variants(job_id: str) -> list[dict[str, Any]]:
    """Return the variants already persisted on this job (empty on first run)."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return []
        return list((job.assembly_plan or {}).get("variants") or [])


def _upsert_variant_entry(job_id: str, result: dict[str, Any]) -> None:
    """Insert or replace `result` in Job.assembly_plan['variants'] by variant_id.

    Like `_update_variant_entry` but appends when the variant isn't present yet —
    the full-job render starts with an empty variants list and adds entries as
    each variant completes. Row-locked RMW (the worker runs --concurrency>1 and a
    `regenerate_generative_variant` task may touch the same row). Does NOT change
    job.status — the job stays `rendering` until `_finalize_job`.
    """
    variant_id = result.get("variant_id")
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        plan = dict(job.assembly_plan or {})
        variants = list(plan.get("variants") or [])
        for i, v in enumerate(variants):
            if v.get("variant_id") == variant_id:
                variants[i] = result
                break
        else:
            variants.append(result)
        plan["variants"] = variants
        job.assembly_plan = plan
        db.commit()


def _update_variant_entry(job_id: str, variant_id: str, patch: dict[str, Any]) -> None:
    """Merge `patch` into the matching entry of Job.assembly_plan['variants'].

    Row-locked (SELECT ... FOR UPDATE): concurrent `regenerate_generative_variant`
    tasks each do a read-modify-write of the whole `assembly_plan` JSONB, so without
    the lock one task's variant update silently clobbers another's (worker runs
    --concurrency=4). The lock serializes the RMW per Job row.
    """
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        plan = dict(job.assembly_plan or {})
        variants = list(plan.get("variants") or [])
        for i, v in enumerate(variants):
            if v.get("variant_id") == variant_id:
                variants[i] = {**v, **{k: val for k, val in patch.items() if k != "variant_id"}}
                break
        plan["variants"] = variants
        job.assembly_plan = plan
        db.commit()


# ── Variant spec ──────────────────────────────────────────────────────────────


def _variant_specs(best_track: MusicTrack | None) -> list[dict[str, Any]]:
    """The variants to render. Song variants only when a track matched; the lyrics
    variant only when that track actually has cached lyrics (otherwise it would render
    identically to song_text with no lyrics — a wasted render + a confusing "Lyrics"
    card). Always emit the original-audio variant."""
    specs: list[dict[str, Any]] = []
    if best_track is not None:
        if best_track.lyrics_cached:
            specs.append({"variant_id": "song_lyrics", "text_mode": "lyrics", "track": best_track})
        specs.append({"variant_id": "song_text", "text_mode": "agent_text", "track": best_track})
    specs.append({"variant_id": "original_text", "text_mode": "agent_text", "track": None})
    return specs


# ── Archetype dispatch (Lane D) ─────────────────────────────────────────────────


def _resolve_archetype(
    edit_format: str,
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    *,
    job_id: str,
    voiceover_gcs_path: str | None = None,
) -> tuple[str, str | None]:
    """Resolve the plan-declared edit_format against the footage → (archetype, spine).

    Default-safe: returns `("montage", None)` for every case except a talking_head edit
    that is enabled AND backed by footage with usable speech. Emits an
    `archetype_fallback` / `archetype_selected` trace event so the admin job-debug view
    explains why a declared format did or didn't take. The returned `spine_clip_id` is
    fed straight to `assemble_talking_head`, whose override path then only re-scores
    that one clip.

    Reasons for montage fallback: `archetype_not_implemented` (day_vlog/single_hero —
    no assembler yet), `flag_disabled` (kill switch off), `no_speech` (no clip clears
    `_MIN_SPINE_COVERAGE`).
    """
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    def _fallback(reason: str) -> tuple[str, None]:
        record_pipeline_event(
            "assembly", "archetype_fallback", {"declared": edit_format, "reason": reason}
        )
        log.info(
            "generative_archetype_fallback", job_id=job_id, declared=edit_format, reason=reason
        )
        return "montage", None

    # A user-supplied voiceover wins over any footage-derived archetype: the voice is
    # the spine. Resolved BEFORE the speech-coverage logic because it's driven by an
    # uploaded asset, not by what the footage happens to contain.
    if voiceover_gcs_path:
        record_pipeline_event("assembly", "archetype_selected", {"archetype": "voiceover"})
        log.info("generative_archetype_selected", job_id=job_id, archetype="voiceover")
        return "voiceover", None

    if edit_format == "montage":
        return "montage", None
    if edit_format != "talking_head":
        # day_vlog / single_hero declared but no assembler exists yet.
        return _fallback("archetype_not_implemented")
    if not settings.edit_format_talking_head_enabled:
        return _fallback("flag_disabled")

    # Pick the highest-speech clip; reject the format if none carries real speech.
    from app.services.clip_speech import speech_coverage  # noqa: PLC0415

    best_id: str | None = None
    best_cov = -1.0
    for m in clip_metas:
        cid = str(getattr(m, "clip_id", "") or "")
        path = clip_id_to_local.get(cid)
        if not path:
            continue
        try:
            cov = float(speech_coverage(path))
        except Exception as exc:  # noqa: BLE001 — best-effort; a probe failure scores 0
            log.warning(
                "generative_speech_coverage_failed", job_id=job_id, clip_id=cid, error=str(exc)
            )
            cov = 0.0
        if cov > best_cov:
            best_cov, best_id = cov, cid

    if best_id is None or best_cov < _MIN_SPINE_COVERAGE:
        return _fallback("no_speech")

    record_pipeline_event(
        "assembly",
        "archetype_selected",
        {
            "archetype": "talking_head",
            "spine_clip_id": best_id,
            "speech_coverage": round(best_cov, 3),
        },
    )
    log.info(
        "generative_archetype_selected",
        job_id=job_id,
        archetype="talking_head",
        spine_clip_id=best_id,
        speech_coverage=round(best_cov, 3),
    )
    return "talking_head", best_id


def _specs_for_archetype(
    archetype: str,
    best_track: MusicTrack | None,
    *,
    voiceover_gcs_path: str | None = None,
) -> list[dict[str, Any]]:
    """The variant set to render for a resolved archetype (single source of truth).

    montage → today's song/original variants. talking_head → ONE variant (the spine's
    own audio + the AI intro overlay); the music-bed variant is a follow-up. voiceover →
    the user's recorded voice over a footage montage: `voiceover_only` (footage ducked
    under the voice) plus, when a track matched, `voiceover_music` (matched track as a
    low bed under the voice). Both render through the montage path (`_render_generative_variant`)
    — the voiceover specs carry no `talking_head` archetype, just the voiceover params.
    Each spec carries its `archetype` so the render loop dispatches correctly; specs from
    `_variant_specs` default to montage (no `archetype` key).
    """
    if archetype == "voiceover":
        specs: list[dict[str, Any]] = [
            {
                "variant_id": "voiceover_only",
                "text_mode": "agent_text",
                "track": None,
                "archetype": "voiceover",
                "voiceover_gcs_path": voiceover_gcs_path,
                "mix": _VOICEOVER_ONLY_DEFAULT_MIX,
            }
        ]
        if best_track is not None:
            specs.append(
                {
                    "variant_id": "voiceover_music",
                    "text_mode": "agent_text",
                    "track": best_track,
                    "archetype": "voiceover",
                    "voiceover_gcs_path": voiceover_gcs_path,
                    "mix": _VOICEOVER_MUSIC_DEFAULT_MIX,
                }
            )
        return specs
    if archetype == "talking_head":
        return [
            {
                "variant_id": "talking_head",
                "text_mode": "agent_text",
                "track": None,
                "archetype": "talking_head",
            }
        ]
    return _variant_specs(best_track)


# ── Agents (best-effort) ────────────────────────────────────────────────────────


def _run_text_agents(
    clip_metas: list, hero, *, job_id: str, language: str = "en", persona: dict | None = None
) -> tuple[Any, dict]:
    """Run overlay_format_matcher → intro_writer. Returns (IntroWriterOutput|None, form dict).

    `language` is the target render language (closed allowlist enforced at the API
    edge). Forwarded to both agents so the intro is written in the right language
    and the form matcher considers form-fit per language.

    `persona` is the optional content-plan creator context
    (`{tone, content_pillars, theme, idea}`, stashed on `all_candidates["persona"]`).
    When present it steers the hook's voice toward the creator's pillars + the
    day's theme; empty/absent for public generative jobs → footage-only voice
    (identical to pre-persona behavior). intro_writer re-sanitizes every field.

    Best-effort: any failure yields (None, {}) so the text variants render footage
    without an intro rather than failing the job.
    """
    persona = persona or {}
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.intro_writer import IntroTextWriterAgent, IntroWriterInput  # noqa: PLC0415
        from app.agents.overlay_examples import examples_by_id  # noqa: PLC0415
        from app.agents.overlay_format_matcher import (  # noqa: PLC0415
            OverlayFormatMatcherAgent,
            OverlayFormatMatcherInput,
        )

        hero_summary = _meta_to_summary(hero)
        clip_set_summary = _clip_set_summary(clip_metas)
        ctx = RunContext(job_id=job_id)
        client = default_client()

        form = OverlayFormatMatcherAgent(client).run(
            OverlayFormatMatcherInput(
                clip_set_summary=clip_set_summary,
                hero_clip=hero_summary,
                language=language,
            ),
            ctx=ctx,
        )
        by_id = examples_by_id()
        exemplars = [by_id[i] for i in form.matched_example_ids if i in by_id]

        text = IntroTextWriterAgent(client).run(
            IntroWriterInput(
                hero_clip=hero_summary,
                hero_transcript=str(getattr(hero, "transcript", "") or ""),
                tone=str(persona.get("tone", "") or ""),
                content_pillars=list(persona.get("content_pillars", []) or []),
                theme=str(persona.get("theme", "") or ""),
                idea=str(persona.get("idea", "") or ""),
                preference_summary=str(persona.get("preference_summary", "") or ""),
                # Deep TikTok analysis — the creator's proven style informs the hook
                # voice. Empty for public jobs and when analysis hasn't landed yet
                # → prompt byte-identical to baseline (_persona_context handles this).
                tiktok_analysis=str(persona.get("tiktok_summary", "") or ""),
                form=form.model_dump(),
                exemplars=exemplars,
                language=language,
            ),
            ctx=ctx,
        )
        return text, form.model_dump()
    except Exception as exc:
        log.warning("generative_text_agents_failed", job_id=job_id, error=str(exc))
        return None, {}


def _select_generative_style_set(clip_metas: list, agent_text, *, job_id: str) -> str:
    """Pick a curated style set for this generative edit. Returns a set id.

    Reuses `AgenticStyleSelectorAgent` (text-only) but feeds it the
    generative-eligible catalog so a music-only set can never be chosen. The
    clip-set summary stands in for the "template theme"; the AI intro text (when
    present) is the on-screen text sample. Best-effort: any failure → "default"
    so a job is never blocked on style selection.
    """
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.agentic_style_selector import (  # noqa: PLC0415
            AgenticStyleSelectorAgent,
            AgenticStyleSelectorInput,
            StyleSetCandidate,
        )
        from app.pipeline.style_sets import list_style_sets  # noqa: PLC0415

        candidates = [StyleSetCandidate(**s) for s in list_style_sets(applies_to="generative")]
        overlay_texts = [agent_text.text] if agent_text is not None else []
        out = AgenticStyleSelectorAgent(default_client()).run(
            AgenticStyleSelectorInput(
                overlay_texts=overlay_texts,
                template_theme=_clip_set_summary(clip_metas),
                available_sets=candidates,
            ),
            ctx=RunContext(job_id=job_id),
        )
        return out.style_set_id or "default"
    except Exception as exc:  # noqa: BLE001 — selection is best-effort
        log.warning("generative_style_set_select_failed", job_id=job_id, error=str(exc))
        return "default"


def _match_best_track(clip_metas: list, *, job_id: str) -> MusicTrack | None:
    """Top-1 matched track, or None if the library has no confident match."""
    try:
        from app.tasks.auto_music_orchestrate import (  # noqa: PLC0415
            _load_matcher_candidates,
            _run_music_matcher,
        )

        # Generative auto-picks a song; the user never browses the gallery, so
        # match against the whole analyzed library, not just published tracks.
        candidates = _load_matcher_candidates(len(clip_metas), require_published=False)
        if not candidates:
            log.info("generative_no_labeled_tracks", job_id=job_id)
            return None
        ranked = _run_music_matcher(
            clip_metas=clip_metas, candidate_tracks=candidates, n_variants=1, job_id=job_id
        )
        if not ranked:
            return None
        by_id = {t.id: t for t in candidates}
        for r in ranked:
            track = by_id.get(r["track_id"])
            if track is not None:
                return track
        return None
    except Exception as exc:
        log.warning("generative_song_match_failed", job_id=job_id, error=str(exc))
        return None


# ── Footage-derived sizing ───────────────────────────────────────────────────────


def _available_footage_s(probe_map: dict) -> float:
    """Total seconds of uploaded footage — the hard ceiling on every variant.

    Summed across all probed clips. The output edit is sized against this so it
    can never run longer than the content the user actually uploaded (a clip used
    in more than one slot can't manufacture extra runtime — `allow_slowdown_fill=
    False` forbids stretching, and the matcher prefers spreading clips across
    slots). A probe failure contributes 0 for that clip rather than a fabricated
    fallback, keeping the ceiling conservative.
    """
    total = 0.0
    for probe in probe_map.values():
        dur = float(getattr(probe, "duration_s", 0.0) or 0.0)
        if dur > 0:
            total += dur
    return round(total, 3)


def _fit_section_to_footage(track_config: dict, available_footage_s: float) -> dict:
    """Shrink a song best-section window so it is no longer than the footage.

    Returns a copy with `best_end_s` pulled in to `best_start_s + min(window,
    available_footage_s)`. Never extends the window (a section shorter than the
    footage is left alone — the song's own structure stays the ceiling there).
    `best_start_s` is untouched so the audio offset in `_mix_template_audio`
    stays aligned with the original best section.
    """
    cfg = dict(track_config or {})
    if available_footage_s <= 0:
        return cfg
    start_s = float(cfg.get("best_start_s", 0.0) or 0.0)
    end_s = float(cfg.get("best_end_s", 0.0) or 0.0)
    window = end_s - start_s
    if window <= 0:
        return cfg
    if window > available_footage_s:
        cfg["best_end_s"] = round(start_s + available_footage_s, 3)
    return cfg


# ── Variant render ──────────────────────────────────────────────────────────────


def _classify_error(exc: BaseException) -> str:
    """Map an exception to a machine-readable error_class for the frontend.

    Keeps the raw `error` field (admin-only) separate from the public taxonomy.
    The frontend maps these to user-facing copy; `unknown` gets the generic fallback.
    """
    from celery.exceptions import SoftTimeLimitExceeded  # noqa: PLC0415

    if isinstance(exc, SoftTimeLimitExceeded):
        return "timeout"
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ffmpeg" in name or "encoder" in name or "ffmpeg" in msg or "codec" in msg:
        return "encoder_error"
    if "storage" in name or "gcs" in name or "upload" in name or "download" in name:
        return "storage_error"
    if "clip" in name and ("read" in name or "read" in msg):
        return "clip_read_error"
    return "unknown"


def _render_generative_variant(
    *,
    job_id: str,
    rank: int,
    spec: dict[str, Any],
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    clip_id_to_gcs: dict[str, str],
    probe_map: dict,
    available_footage_s: float,
    agent_text,
    agent_form: dict,
    variant_dir: str,
    style_set_id: str | None = None,
    intro_size_override_px: int | None = None,
    user_style_knobs: dict | None = None,
) -> dict[str, Any]:
    """Render one variant. Never raises — failures become a failure record.

    `intro_size_override_px` carries a user-pinned intro size (the public ±size
    nudge). When None the size is computed from the hero clip's composition; when
    set it wins and the variant records `intro_size_source="user"` so later
    re-renders (swap-song/retext/restyle) preserve it instead of recomputing.

    `user_style_knobs` are per-user parity-safe overrides (Creator Agent M1):
    font, position, colors, etc. They win over the curated set's values inside
    `_resolve_intro_overlay_params`. Persisted on the variant entry so re-renders
    (swap-song/retext/restyle) re-apply them without re-reading the persona row.
    """
    from app.pipeline.agents.gemini_analyzer import build_recipe  # noqa: PLC0415
    from app.pipeline.music_recipe import generate_music_recipe  # noqa: PLC0415
    from app.pipeline.template_matcher import (  # noqa: PLC0415
        TemplateMismatchError,
        consolidate_slots,
        match,
    )
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415
    from app.tasks.template_orchestrate import (  # noqa: PLC0415
        _assemble_clips,
        _enrich_slots_with_energy,
        _mix_template_audio,
        _mix_user_voiceover,
        _probe_duration,
    )

    variant_id = spec["variant_id"]
    text_mode = spec["text_mode"]
    track: MusicTrack | None = spec["track"]
    track_id = track.id if track else None
    track_title = track.title if track else None
    # Voiceover variants: the user's audio is the narration bed, footage tiles as
    # visuals. `mix` is the voice-prominence slider (persisted so the UI slider and
    # re-renders can read it back). Absent on song/original/talking_head specs.
    voiceover_gcs_path: str | None = spec.get("voiceover_gcs_path")
    mix: float = float(spec.get("mix", _VOICEOVER_ONLY_DEFAULT_MIX))

    base = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": text_mode,
        "music_track_id": track_id,
        "track_title": track_title,
        "style_set_id": style_set_id,
        # Agent-decided (or user-pinned) intro size. None for non-text variants.
        "intro_text_size_px": None,
        "intro_size_source": None,  # "computed" | "user" | "user_style" | None
        # Persisted intro text so re-renders can reuse it without re-running intro_writer.
        "intro_text": None,
        "intro_highlight_word": None,
        # Voice-prominence slider for voiceover variants; None otherwise.
        "mix": mix if voiceover_gcs_path else None,
        # Per-user parity-safe knob overrides (Creator Agent M1). Persisted so
        # re-renders (swap-song/retext/restyle) re-apply them without re-reading
        # the persona row. None/empty = no overrides = baseline.
        "user_style_knobs": user_style_knobs or None,
        # Cached text-free, audio-mixed base for fast-reburn on style/font/size edits.
        # None for lyrics variants (full path in v1) and voiceover variants.
        "base_video_path": None,
    }
    try:
        beats: list[float] = []
        voiceover_local: str | None = None
        voiceover_target_s = available_footage_s
        if voiceover_gcs_path:
            # Voiceover edit: download the voice, then size the footage montage to
            # min(footage, voice, 60) — never stretch footage past what was uploaded
            # (D5), never exceed the short-form ceiling. The matched track (if this is
            # the voiceover_music variant) is layered as a low bed afterwards, NOT
            # beat-synced into slots, so the visuals are a plain footage montage.
            voiceover_local = os.path.join(variant_dir, "voiceover_src")
            download_to_file(voiceover_gcs_path, voiceover_local)
            voice_dur = _probe_duration(voiceover_local)
            _cands = [available_footage_s, _VOICEOVER_MAX_DURATION_S]
            if voice_dur > 0:
                _cands.append(voice_dur)
            voiceover_target_s = min(_cands)
            recipe_dict = _build_no_music_recipe(clip_metas, voiceover_target_s)
        elif track is not None:
            from app.services.music_sections import (  # noqa: PLC0415
                track_config_with_rank_one,
            )

            if not track.audio_gcs_path:
                raise ValueError(f"Track {track_id} has no audio_gcs_path")
            # Clamp the song's best-section window to the uploaded footage BEFORE
            # generating slots. The recipe slices [best_start, best_end] into
            # beat-snapped slots, so capping the window here is what keeps a
            # music variant from ever running longer than the content exists for.
            track_config = _fit_section_to_footage(
                track_config_with_rank_one(track), available_footage_s
            )
            track_data = {
                "beat_timestamps_s": track.beat_timestamps_s or [],
                "track_config": track_config,
                "duration_s": track.duration_s,
            }
            recipe_dict = generate_music_recipe(track_data)
            beats = list(recipe_dict.get("beat_timestamps_s") or [])
            recipe_dict["slots"] = _enrich_slots_with_energy(
                recipe_dict["slots"], track_data["beat_timestamps_s"]
            )
        else:
            recipe_dict = _build_no_music_recipe(clip_metas, available_footage_s)

        # Text injection per mode. The chosen style set styles BOTH the lyric
        # overlays (lyrics variant) and the AI hero-intro (text variants).
        # For agent_text variants we do NOT inject into the recipe here —
        # instead we assemble text-free, cache the base, then burn text in a
        # separate step so fast-reburn can skip re-assembly on future edits.
        if text_mode == "lyrics" and track is not None:
            recipe_dict = _inject_lyrics(recipe_dict, track, style_set_id=style_set_id)

        # Resolve agent_text overlay params early (before assembly) so we have
        # intro_px / intro_source for base dict even if the burn fails below.
        _agent_text_overlays = None  # built after audio mix, if needed
        _agent_text_intro_px = None
        _agent_text_intro_source = None
        if text_mode == "agent_text" and agent_text is not None:
            hero_safe_zone, hero_density = _hero_composition(clip_metas)
            _at_params, _agent_text_intro_px, _agent_text_intro_source = (
                _resolve_intro_overlay_params(
                    agent_text,
                    agent_form,
                    style_set_id,
                    hero_safe_zone=hero_safe_zone,
                    hero_density=hero_density,
                    size_override_px=intro_size_override_px,
                    user_style_knobs=user_style_knobs,
                )
            )
            base["intro_text_size_px"] = _agent_text_intro_px
            base["intro_size_source"] = _agent_text_intro_source
            # Persist the intro text so re-renders (font/size/style edits) can reuse
            # it without re-running intro_writer.
            base["intro_text"] = agent_text.text if agent_text is not None else None
            base["intro_highlight_word"] = (
                getattr(agent_text, "highlight_word", None) if agent_text is not None else None
            )

        recipe = build_recipe(recipe_dict)
        try:
            recipe = consolidate_slots(recipe, clip_metas)
            assembly_plan = match(recipe, clip_metas)
        except TemplateMismatchError as exc:
            raise ValueError(f"{exc.code}: {exc.message}") from exc

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
            is_agentic=True,  # route overlays through the Skia renderer
            # Generative edits must never stretch footage to fill a slot. When a
            # clip is shorter than its slot, shrink the slot instead of slowing
            # the clip down — the output stays bounded by real footage length.
            allow_slowdown_fill=False,
        )

        # audio_mixed_path: the assembled+audio-mixed video before text burn.
        # For agent_text variants this becomes the cached base.
        audio_mixed_path = os.path.join(variant_dir, "audio_mixed.mp4")
        final_path = os.path.join(variant_dir, "final.mp4")
        if voiceover_gcs_path:
            # Voiceover variants: the user's voice is the bed. voiceover_only ducks the
            # footage audio under the voice; voiceover_music drops a matched track low
            # under the voice instead. `mix` is the voice-prominence slider.
            cfg = (track.track_config or {}) if track is not None else {}
            _mix_user_voiceover(
                assembled_path,
                voiceover_local,
                audio_mixed_path,
                variant_dir,
                mix=mix,
                target_duration_s=voiceover_target_s,
                music_gcs_path=track.audio_gcs_path if track is not None else None,
                music_start_offset_s=float(cfg.get("best_start_s", 0.0)),
            )
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            record_pipeline_event(
                "audio_mix",
                "voiceover_mixed",
                {
                    "variant_id": variant_id,
                    "mix": round(mix, 3),
                    "bed": "music" if track is not None else "footage",
                    "target_s": round(voiceover_target_s, 3),
                },
            )
        elif track is not None:
            # Song variants: replace source audio with the matched track.
            cfg = track.track_config or {}
            _mix_template_audio(
                assembled_path,
                track.audio_gcs_path,
                audio_mixed_path,
                variant_dir,
                audio_start_offset_s=float(cfg.get("best_start_s", 0.0)),
            )
        else:
            # Original-audio variant: KEEP the clips' source audio — skip the mix.
            # `_assemble_clips` already muxed source audio into assembled.mp4.
            audio_mixed_path = assembled_path

        if not os.path.exists(audio_mixed_path) or os.path.getsize(audio_mixed_path) == 0:
            raise RuntimeError(f"variant {variant_id} produced empty audio-mixed output")

        # For agent_text variants: upload the text-free base for fast-reburn, then
        # burn text on top to produce the final output. For all other modes the
        # audio-mixed video IS the final (no separate burn step).
        if text_mode == "agent_text" and agent_text is not None:
            from app.pipeline.generative_overlays import (  # noqa: PLC0415
                build_persistent_intro_overlays,
            )
            from app.pipeline.probe import probe_video  # noqa: PLC0415
            from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415

            # Upload the text-free base first.
            base_gcs = f"generative-jobs/{job_id}/base_{rank}_{variant_id}.mp4"
            base_url_unused = upload_public_read(audio_mixed_path, base_gcs)  # noqa: F841
            base["base_video_path"] = base_gcs
            log.info(
                "generative_base_uploaded",
                job_id=job_id,
                variant_id=variant_id,
                base_gcs=base_gcs,
            )

            # Burn the agent intro overlay on top of the base.
            try:
                base_dur = float(probe_video(audio_mixed_path).duration_s)
            except Exception:  # noqa: BLE001
                base_dur = MAX_INTRO_S
            reveal_window_s = min(base_dur, MAX_INTRO_S) if base_dur > 0 else MAX_INTRO_S
            overlays = build_persistent_intro_overlays(
                reveal_window_s=reveal_window_s,
                beats=beats,
                **_at_params,
            )
            burn_text_overlays_skia(audio_mixed_path, overlays, final_path, variant_dir)
        else:
            # lyrics / none / voiceover: no text burn; base caching not supported in v1.
            final_path = audio_mixed_path

        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError(f"variant {variant_id} produced empty output")

        output_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}.mp4"
        output_url = upload_public_read(final_path, output_gcs)
        log.info("generative_variant_uploaded", job_id=job_id, variant_id=variant_id)
        return {
            **base,
            "ok": True,
            "render_status": "ready",
            "video_path": output_gcs,
            "output_url": output_url,
        }
    except Exception as exc:
        err = str(exc)[:MAX_ERROR_DETAIL_LEN]
        log.error(
            "generative_variant_failed",
            job_id=job_id,
            variant_id=variant_id,
            error=err,
            exc_info=True,
        )
        return {
            **base,
            "ok": False,
            "render_status": "failed",
            "error": err,
            "error_class": _classify_error(exc),
        }


def _render_talking_head_variant(
    *,
    job_id: str,
    rank: int,
    spine_clip_id: str | None,
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    probe_map: dict,
    available_footage_s: float,
    agent_text,
    agent_form: dict,
    variant_dir: str,
    style_set_id: str | None = None,
    intro_size_override_px: int | None = None,
    user_style_knobs: dict | None = None,
) -> dict[str, Any]:
    """Render the talking_head variant: spine audio + B-roll, then burn the AI intro.

    `assemble_talking_head` produces the composite (one clip's full audio under the
    other clips' video). It RAISES `SpineExtractionError` on a corrupt spine — that
    propagates to the caller (`_run_generative_job`) to degrade the WHOLE job to
    montage. Every other failure becomes a per-variant failure record (matching
    `_render_generative_variant`'s never-raise contract). The AI intro is burned onto
    the composite via the standalone Skia path (`burn_text_overlays_skia`) — the
    assembler itself draws no text. Shape-compatible with `_render_generative_variant`
    plus a `resolved_archetype` field.
    """
    from app.pipeline.generative_overlays import build_persistent_intro_overlays  # noqa: PLC0415
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.talking_head_assembler import (  # noqa: PLC0415
        SpineExtractionError,
        assemble_talking_head,
    )
    from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415
    from app.storage import upload_public_read  # noqa: PLC0415

    variant_id = "talking_head"
    base = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": "agent_text" if agent_text is not None else "none",
        "music_track_id": None,
        "track_title": None,
        "style_set_id": style_set_id,
        "intro_text_size_px": None,
        "intro_size_source": None,
        # Persisted intro text so re-renders can reuse it without re-running intro_writer.
        "intro_text": None,
        "intro_highlight_word": None,
        "resolved_archetype": "talking_head",
        # Per-user parity-safe knob overrides (Creator Agent M1). Persisted for
        # re-renders (same as _render_generative_variant).
        "user_style_knobs": user_style_knobs or None,
        # No text-free base cached for talking_head in v1 (the assembler's spine
        # extraction is the expensive step, not text burn; fast-reburn not yet applied).
        "base_video_path": None,
    }

    try:
        # SpineExtractionError (corrupt spine) is re-raised below for the job-level
        # montage degrade; every OTHER failure — a composite ffmpeg error, a burn or
        # upload failure — becomes a per-variant failure record (the never-raise
        # contract `_render_generative_variant` also honors).
        base_path = os.path.join(variant_dir, "base.mp4")
        assemble_talking_head(
            clip_paths=clip_id_to_local,
            clip_metas=clip_metas,
            probe_map=probe_map,
            target_duration_s=available_footage_s or None,
            output_path=base_path,
            tmpdir=variant_dir,
            job_id=job_id,
            spine_clip_id=spine_clip_id,
        )

        final_path = base_path
        if agent_text is not None:
            try:
                base_dur = float(probe_video(base_path).duration_s)
            except Exception:  # noqa: BLE001 — reveal window falls back to the cap
                base_dur = MAX_INTRO_S
            reveal_window_s = min(base_dur, MAX_INTRO_S) if base_dur > 0 else MAX_INTRO_S
            hero_safe_zone, hero_density = _hero_composition(clip_metas)
            params, intro_px, intro_source = _resolve_intro_overlay_params(
                agent_text,
                agent_form,
                style_set_id,
                hero_safe_zone=hero_safe_zone,
                hero_density=hero_density,
                size_override_px=intro_size_override_px,
                user_style_knobs=user_style_knobs,
            )
            # No song → no beats; the intro reveals on an even split. Slot-0-relative
            # timestamps (from 0) are already absolute on the composite, which is what
            # burn_text_overlays_skia expects.
            overlays = build_persistent_intro_overlays(
                reveal_window_s=reveal_window_s, beats=[], **params
            )
            if overlays:
                burned = os.path.join(variant_dir, "final.mp4")
                burn_text_overlays_skia(base_path, overlays, burned, variant_dir)
                final_path = burned
                base["intro_text_size_px"] = intro_px
                base["intro_size_source"] = intro_source
            # Persist intro text regardless of whether overlays were non-empty —
            # the text itself is what re-renders need to reuse.
            base["intro_text"] = agent_text.text if agent_text is not None else None
            base["intro_highlight_word"] = (
                getattr(agent_text, "highlight_word", None) if agent_text is not None else None
            )

        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError(f"variant {variant_id} produced empty output")

        output_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}.mp4"
        output_url = upload_public_read(final_path, output_gcs)
        log.info("generative_variant_uploaded", job_id=job_id, variant_id=variant_id)
        return {
            **base,
            "ok": True,
            "render_status": "ready",
            "video_path": output_gcs,
            "output_url": output_url,
        }
    except SpineExtractionError:
        raise  # job-level degrade to montage (handled by the caller)
    except Exception as exc:
        err = str(exc)[:MAX_ERROR_DETAIL_LEN]
        log.error(
            "generative_variant_failed",
            job_id=job_id,
            variant_id=variant_id,
            error=err,
            exc_info=True,
        )
        return {
            **base,
            "ok": False,
            "render_status": "failed",
            "error": err,
            "error_class": _classify_error(exc),
        }


def _inject_lyrics(recipe_dict: dict, track: MusicTrack, style_set_id: str | None = None) -> dict:
    from app.pipeline.lyric_injector import inject_lyric_overlays  # noqa: PLC0415
    from app.services.lyrics_cache_refresh import (  # noqa: PLC0415
        ensure_fresh_lyrics_cached_for_render,
    )
    from app.services.lyrics_config_effective import effective_lyrics_config  # noqa: PLC0415

    cfg = track.track_config or {}
    if style_set_id:
        # The chosen curated set drives the lyric look for a generative edit and is
        # authoritative over the track's saved (music-job) lyric tuning, so we do NOT
        # inherit visual fields from `cfg["lyrics_config"]`. The set's lyric role
        # implies the injector style (line/karaoke/word-pop) via `lyric_style_for_set`.
        # style_set_id is consumed by `inject_lyric_overlays` directly (not a
        # validated config key), so set it on the dict rather than routing it
        # through effective_lyrics_config.
        lyrics_config = {"enabled": True, "style_set_id": style_set_id}
        saved_lyrics_config = cfg.get("lyrics_config") if isinstance(cfg, dict) else None
        if (
            isinstance(saved_lyrics_config, dict)
            and saved_lyrics_config.get("sync_offset_s") is not None
        ):
            lyrics_config["sync_offset_s"] = saved_lyrics_config["sync_offset_s"]
    else:
        # Force lyrics on for this variant (the user explicitly chose the lyrics edit).
        lyrics_config = effective_lyrics_config(cfg, {"enabled": True, "style": "karaoke"})

    lyrics_cached = ensure_fresh_lyrics_cached_for_render(
        track_id=str(track.id),
        lyrics_cached=track.lyrics_cached,
        lyrics_config=lyrics_config,
        reason="generative_lyrics_variant",
    )
    track.lyrics_cached = lyrics_cached
    return inject_lyric_overlays(
        recipe_dict,
        lyrics_cached,
        best_start_s=float(cfg.get("best_start_s", 0.0)),
        best_end_s=float(cfg.get("best_end_s", 0.0)),
        lyrics_config=lyrics_config,
    )


def _safe_density(m) -> float:
    """visual_density of a clip meta, clamped to [0, 10]; 5.0 on junk/missing."""
    try:
        return max(0.0, min(10.0, float(getattr(m, "visual_density", 5.0))))
    except (TypeError, ValueError):
        return 5.0


def _hero_composition(clip_metas: list) -> tuple[dict | None, float]:
    """Composition signal for intro SIZING: the most text-friendly clip — the
    largest CALM safe zone — not the highest-hook clip.

    The intro overlay persists across the whole video, so its size should track
    the clip with the most room for text, letting it breathe when the footage
    allows. This previously used the highest-`hook_score` clip, but hook strength
    is uncorrelated with open space (a punchy clip is often the busiest), which
    forced almost every intro to the small end. The openness score is safe-zone
    AREA discounted by visual density, so a big-but-cluttered box can't beat a
    slightly smaller open one; `_shrink_to_fit` + the overlay's drop shadow keep
    the text legible over the busier clips it also overlaps.

    Returns `(None, 5.0)` when no clip reported a usable safe zone (degraded /
    pre-bump cache) — `compute_overlay_size` handles that as a computed full-width
    fallback, never a hardcoded size, never a crash."""
    best = None
    best_score = -1.0
    for m in clip_metas or []:
        sz = getattr(m, "text_safe_zone", None)
        if not isinstance(sz, dict):
            continue
        try:
            w, h = float(sz.get("w")), float(sz.get("h"))
        except (TypeError, ValueError):
            continue
        if not (0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            continue
        # Bigger AND calmer wins: halve the area weight as density climbs 0 → 10.
        score = (w * h) * (1.0 - 0.5 * _safe_density(m) / 10.0)
        if score > best_score:
            best_score, best = score, m
    if best is None:
        return None, 5.0
    return best.text_safe_zone, _safe_density(best)


def _inject_agent_intro(
    recipe_dict: dict,
    agent_text,
    agent_form: dict,
    beats: list[float],
    style_set_id: str | None = None,
    *,
    hero_safe_zone: dict | None = None,
    hero_density: float = 5.0,
    size_override_px: int | None = None,
    user_style_knobs: dict | None = None,
) -> tuple[dict, int | None, str | None]:
    """Inject the hero intro and return (recipe, intro_text_size_px, size_source).

    Size precedence (the user's "no default size" rule — never a constant):
      1. `size_override_px` — the public ±nudge → source "user" (preserved on
         later re-renders so swap-song/retext don't recompute over a manual pin).
      2. user_style_knobs `text_size_px` — source "user_style" (per-user pin).
      3. curated style-set `text_size_px` — source "computed" (set-driven; safe to
         re-resolve from the set on re-render).
      4. `compute_overlay_size(...)` from the hero clip's safe-zone + density —
         source "computed".
    """
    from app.pipeline.generative_overlays import (  # noqa: PLC0415
        inject_persistent_intro,
    )

    slots = recipe_dict.get("slots") or []
    if not slots:
        return recipe_dict, None, None
    slot0_dur = float(slots[HERO_SLOT_INDEX].get("target_duration_s", 0.0) or 0.0)
    # The intro now persists for the whole video (held statically after the reveal), so
    # MAX_INTRO_S caps only the reveal/animation window, not how long the text shows.
    reveal_window_s = min(slot0_dur, MAX_INTRO_S) if slot0_dur > 0 else MAX_INTRO_S

    params, intro_px, intro_source = _resolve_intro_overlay_params(
        agent_text,
        agent_form,
        style_set_id,
        hero_safe_zone=hero_safe_zone,
        hero_density=hero_density,
        size_override_px=size_override_px,
        user_style_knobs=user_style_knobs,
    )
    recipe_dict = inject_persistent_intro(
        recipe_dict,
        HERO_SLOT_INDEX,
        reveal_window_s=reveal_window_s,
        beats=beats,  # slot-0 / section-relative; empty for the no-music variant
        **params,
    )
    return recipe_dict, intro_px, intro_source


def _resolve_intro_overlay_params(
    agent_text,
    agent_form: dict,
    style_set_id: str | None,
    *,
    hero_safe_zone: dict | None = None,
    hero_density: float = 5.0,
    size_override_px: int | None = None,
    user_style_knobs: dict | None = None,
) -> tuple[dict, int | None, str | None]:
    """Resolve the hero-intro look + size into kwargs for the overlay builders.

    The SINGLE source of truth for intro styling/sizing, shared by the montage path
    (`_inject_agent_intro` → `inject_persistent_intro`) and the talking-head path
    (`_render_talking_head_variant` → `build_persistent_intro_overlays`) so the two
    can never drift on font/size/color/effect/position.

    Returns `(params, intro_text_size_px, size_source)` where `params` is a kwargs dict
    accepted by both `inject_persistent_intro` and `build_persistent_intro_overlays`
    (everything except `recipe`/`hero_slot_index`/`reveal_window_s`/`beats`).

    Size precedence (the user's "no default size" rule — never a constant):
      1. `size_override_px` — the public ±nudge → source "user" (preserved on later
         re-renders so swap-song/retext don't recompute over a manual pin).
      2. `user_style_knobs["text_size_px"]` — per-user style pin → source "user_style".
      3. curated style-set `text_size_px` — source "computed" (set-driven).
      4. `compute_overlay_size(...)` from the hero clip's safe-zone + density.

    Knob precedence (most-specific wins):
      user_style_knobs > curated-set value > agent advisory > hardcoded default.
    """
    # Curated style set owns the intro look (font, size, color, effect, position).
    # The agent_form fields drop to ADVISORY: `resolve_overlay_style` lets the set
    # win and only fills from `advisory` what the set leaves null. Resolving here
    # (not inside build_intro_overlay) keeps that module import-light (no PIL/skia).
    style: dict = {}
    if style_set_id:
        from app.pipeline.style_sets import resolve_overlay_style  # noqa: PLC0415

        advisory = {
            "effect": agent_form.get("effect"),
            "position": agent_form.get("position"),
            "text_color": agent_form.get("text_color"),
            "highlight_color": agent_form.get("highlight_color"),
            "text_anchor": agent_form.get("text_anchor"),
        }
        style = resolve_overlay_style(style_set_id, "intro", advisory=advisory)

    # User-style knobs win over curated-set values (Creator Agent M1).
    # None when USER_STYLE_ENABLED=false or user has no derived style → baseline.
    knobs: dict = user_style_knobs or {}

    # Font: user-style knob > set > None (renderer picks a fallback)
    font_family = knobs.get("font_family") or style.get("font_family")

    set_px = style.get("text_size_px")
    user_style_px = knobs.get("text_size_px") if knobs else None
    if size_override_px is not None:
        intro_px, intro_source = int(size_override_px), "user"
    elif user_style_px is not None:
        intro_px, intro_source = int(user_style_px), "user_style"
    elif set_px is not None:
        intro_px, intro_source = int(set_px), "computed"
    else:
        from app.pipeline.overlay_sizing import compute_overlay_size  # noqa: PLC0415

        intro_px = compute_overlay_size(
            agent_text.text,
            font_family=font_family,
            safe_zone=hero_safe_zone,
            visual_density=hero_density,
        )
        intro_source = "computed"

    params = {
        "text": agent_text.text,
        # effect: NOT in StyleKnobs (parity unconfirmed — #296); set/agent-advisory only
        "effect": style.get("effect") or agent_form.get("effect", "karaoke-line"),
        # knobs win over set, set wins over agent advisory, agent advisory wins over default
        "position": (
            knobs.get("position") or style.get("position") or agent_form.get("position", "center")
        ),
        "text_color": (
            knobs.get("text_color")
            or style.get("text_color")
            or agent_form.get("text_color", "#FFFFFF")
        ),
        "highlight_color": (
            knobs.get("highlight_color")
            or style.get("highlight_color")
            or agent_form.get("highlight_color", "#FFD24A")
        ),
        "text_anchor": (
            knobs.get("text_anchor")
            or style.get("text_anchor")
            or agent_form.get("text_anchor", "center")
        ),
        "highlight_word": getattr(agent_text, "highlight_word", None),
        "font_family": font_family,
        # stroke_width: None-safe — knob wins when set (0 is a valid value)
        "stroke_width": (
            knobs["stroke_width"]
            if knobs.get("stroke_width") is not None
            else style.get("stroke_width")
        ),
        "text_size_px": intro_px,  # computed/user/user_style/set px — no hardcoded jumbo default
        # position_x_frac / position_y_frac: None-safe
        "position_x_frac": (
            knobs["position_x_frac"]
            if knobs.get("position_x_frac") is not None
            else style.get("position_x_frac")
        ),
        "position_y_frac": (
            knobs["position_y_frac"]
            if knobs.get("position_y_frac") is not None
            else style.get("position_y_frac")
        ),
    }
    return params, intro_px, intro_source


def _build_no_music_recipe(clip_metas: list, available_footage_s: float) -> dict:
    """A song-free recipe: one slot per clip (capped), even-split of the footage.

    Variant 3 keeps the clips' original audio, so there are no song beats to slice
    against — we arrange the available clips evenly across the uploaded footage's
    total length. `consolidate_slots` + `match` handle the actual clip assignment
    downstream, and `allow_slowdown_fill=False` trims any slot whose assigned clip
    is shorter than its share, so the output never exceeds the real footage.
    """
    n = max(1, min(len(clip_metas), _MAX_NO_MUSIC_SLOTS))
    per = max(0.5, round(float(available_footage_s) / n, 3))
    slots = [
        {
            "position": i + 1,
            "target_duration_s": per,
            "slot_type": "broll",
            "energy": 5.0,
            "priority": 5,
            "text_overlays": [],
            "transition_in": "cut",
            "speed_factor": 1.0,
        }
        for i in range(n)
    ]
    return {
        "shot_count": n,
        "total_duration_s": round(per * n, 3),
        "hook_duration_s": per,
        "slots": slots,
        "beat_timestamps_s": [],
        "sync_style": "freeform",
        "pacing_style": "medium",
        "color_grade": "none",
        "transition_style": "cut",
        "copy_tone": "energetic",
        "caption_style": "none",
        "creative_direction": "original-audio generative edit",
        "interstitials": [],
        "required_clips_min": 1,
        "required_clips_max": n,
    }


# ── Summaries (energy derived from best_moments — eng fix) ──────────────────────


def _meta_to_summary(meta):
    """ClipMeta → matcher ClipSummary, with clip_energy DERIVED from best_moments.

    ClipMetadataOutput/ClipMeta has NO top-level `energy` — only per-moment energy.
    The auto-music helper defaults missing energy to 5.0; here we compute the real
    signal so hero/form selection isn't flattened to a constant.
    """
    from app.agents.music_matcher import ClipSummary  # noqa: PLC0415
    from app.tasks.auto_music_orchestrate import _clip_meta_to_summary  # noqa: PLC0415

    summary = _clip_meta_to_summary(ClipSummary, meta)
    moments = getattr(meta, "best_moments", None) or []
    energies = [float(m.get("energy", 5.0)) for m in moments if isinstance(m, dict)]
    if energies:
        summary = summary.model_copy(update={"energy": max(0.0, min(10.0, max(energies)))})
    return summary


def _clip_set_summary(clip_metas: list) -> str:
    n = len(clip_metas)
    if n == 0:
        return "(no clips)"
    avg_hook = sum(float(getattr(m, "hook_score", 0.0) or 0.0) for m in clip_metas) / n
    return f"n_clips={n} | avg_hook_score={avg_hook:.1f}"


# ── Status helpers ──────────────────────────────────────────────────────────────


def _finalize_job(job_id: str, results: list[dict[str, Any]]) -> None:
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
        extra_plan={
            "variants": [
                {
                    "variant_id": r["variant_id"],
                    "rank": r["rank"],
                    "text_mode": r["text_mode"],
                    "music_track_id": r.get("music_track_id"),
                    "track_title": r.get("track_title"),
                    "style_set_id": r.get("style_set_id"),
                    "output_url": r.get("output_url"),
                    "video_path": r.get("video_path"),
                    "render_status": r.get("render_status"),
                    "ok": bool(r.get("ok")),
                    "error": r.get("error"),
                    "intro_text_size_px": r.get("intro_text_size_px"),
                    "intro_size_source": r.get("intro_size_source"),
                    "resolved_archetype": r.get("resolved_archetype"),
                    # fast-reburn fields — MUST survive finalization or the cached
                    # base is permanently unreachable after the first completed render
                    "intro_text": r.get("intro_text"),
                    "intro_highlight_word": r.get("intro_highlight_word"),
                    "base_video_path": r.get("base_video_path"),
                }
                for r in results
            ],
        },
    )
    log.info(
        "generative_job_done",
        job_id=job_id,
        terminal=terminal,
        successes=len(successes),
        failures=len(failures),
    )


def _set_status(job_id: str, status: str, extra_plan: dict[str, Any] | None = None) -> None:
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        job.status = status
        if extra_plan is not None:
            existing = job.assembly_plan or {}
            job.assembly_plan = {**existing, **extra_plan}
        db.commit()


def _fail_job(job_id: str, error_detail: str, failure_reason: str | None = None) -> None:
    try:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "processing_failed"
                job.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                if failure_reason:
                    job.failure_reason = failure_reason
                db.commit()
    except Exception as exc:
        log.error("generative_fail_job_db_error", job_id=job_id, error=str(exc))
