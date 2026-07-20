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

import copy
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import is_dataclass, replace
from datetime import datetime
from itertools import cycle
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS, coerce_edit_format
from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.pipeline.canvas import PORTRAIT, Canvas, canvas_for_orientation
from app.schemas.montage_preset import (
    DEFAULT_MONTAGE_PRESET,
    MASONRY_MONTAGE_PRESET,
    coerce_montage_preset,
    is_collage_montage_preset,
)
from app.services.generative_jobs import CONTENT_PLAN_PRIMARY_VARIANT_POLICY
from app.services.job_phases import mark_failed_phase, mark_finished, mark_started, record_phase
from app.worker import celery_app


def _rendered_duration_s(path: str) -> float | None:
    """Best-effort persisted duration for editor-side structural validation."""
    try:
        from app.pipeline.probe import probe_video  # noqa: PLC0415

        duration = float(probe_video(path).duration_s)
        return round(duration, 3) if duration > 0 else None
    except Exception:  # noqa: BLE001 - render success must not depend on metadata
        return None


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
# Soft-warn threshold: >24 shots is unusual (≈8 shots × 3 clips/shot) and risks
# approaching the Celery soft_time_limit=1740s on long-footage jobs.
_NARRATIVE_FLOOR_WARN_THRESHOLD = 24
# Minimum speech_coverage (0-1) for ANY clip to qualify a talking_head edit. Below
# this the footage carries no usable spoken spine, so the job degrades to montage.
# Deliberately low — silencedetect undercounts quiet/lapel speech; we only want to
# reject footage that is essentially silent (b-roll, ambience, music-over).
_MIN_SPINE_COVERAGE = 0.15
# A self-narration multi-clip talking_head needs enough spine timeline to show
# the speaker, then place at least one B-roll cutaway. The assembler's fixed
# cadence uses a 1.5s lead-in and drops windows shorter than 0.5s, so a <=2s
# spine would render as a single clip even when many clips were uploaded.
_MIN_TALKING_HEAD_SPINE_WITH_BROLL_S = 2.0

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

# Kill switch for the TextElement authoring layer (T3/T4 — plan-item-timeline).
# When False: no text_elements snapshot is written on render, and the
# _reburn_text_on_base early branch (user-authored text_elements) is bypassed.
# Apply: fly secrets set TEXT_ELEMENTS_ENABLED=false --app nova-video + worker restart.
_TEXT_ELEMENTS_ENABLED = os.getenv("TEXT_ELEMENTS_ENABLED", "true").lower() != "false"


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
        # Smart Captions creator preset is resolved from the server-owned
        # assignment at dispatch time and pinned into this job. Re-check the
        # master/base-renderer gates here so a mid-rollout kill switch wins.
        _raw_smart = all_candidates.get("smart_captions")
        smart_captions: dict[str, str] | None = None
        if (
            settings.smart_captions_enabled
            and settings.subtitled_archetype_enabled
            and isinstance(_raw_smart, dict)
            and str(_raw_smart.get("preset_id") or "").strip()
            and str(_raw_smart.get("preset_version") or "").strip()
        ):
            smart_captions = {
                "preset_id": str(_raw_smart["preset_id"]),
                "preset_version": str(_raw_smart["preset_version"]),
                "sound_design": ("off" if _raw_smart.get("sound_design") == "off" else "auto"),
            }
        # Plan-declared edit format (Lane A). Coerced defensively — a drifted token
        # falls back to montage rather than failing the job. Resolved against the
        # footage after ingest (see _resolve_archetype).
        edit_format = coerce_edit_format(all_candidates.get("edit_format"))
        # Optional user-supplied voiceover (audio-only). When present it becomes the
        # narration bed and the job renders voiceover variants instead of song/original
        # — resolved in _resolve_archetype below, ahead of the footage-speech logic.
        voiceover_gcs_path: str | None = all_candidates.get("voiceover_gcs_path") or None
        # Original-audio bed level for the narrated archetype (0..1; None → Kria's
        # default). Plumbed into the narrated spec; ignored by other archetypes.
        _raw_bed = all_candidates.get("voiceover_bed_level")
        voiceover_bed_level: float | None = float(_raw_bed) if _raw_bed is not None else None
        # Caption style for the narrated archetype ("sentence" | "word"; None →
        # "sentence", today's sentence-block captions). "word" renders one big word
        # at a time (qbuilder style). Plumbed into the narrated spec; ignored elsewhere.
        voiceover_caption_style: str | None = all_candidates.get("voiceover_caption_style") or None
        # Filming guide (Creator Agent M3 / B2). Shot-list context forwarded to
        # intro_writer for hook voice. Absent on public/legacy jobs → empty list →
        # byte-identical to pre-M3 behavior.
        filming_guide_candidates: list[dict] = list(all_candidates.get("filming_guide") or [])
        # Creator clip notes (WS5 / dogfood feedback #3). gcs_path → note_text,
        # only populated on plan-item jobs where the creator typed a note. Absent
        # on public/legacy jobs → empty dict → byte-identical baseline. Forwarded
        # to intro_writer for hook grounding.
        clip_notes_candidates: dict = dict(all_candidates.get("clip_notes") or {})
        # Narrative clip order (filming-guide alignment): the first N entries of
        # clip_paths are the guide's shot clips, in guide order (derived at
        # dispatch by _dispatch_item_render). 0/absent on public/legacy jobs.
        narrative_shot_count: int = int(all_candidates.get("narrative_shot_count") or 0)
        # Landscape-clip fit preference (plan-item editor, 0057+). "fit" = letterbox
        # landscape clips (black bars, never enlarged). "fill" = crop to fill (legacy
        # default). Absent on public/legacy jobs → defaults to "fill" → byte-identical
        # crop behavior everywhere those jobs previously ran.
        # "fit" stored in all_candidates by build_generative_job when the user
        # chose letterbox; absent = fill (the legacy crop default). Use (or {})
        # to guard the rare in-flight job where all_candidates is None.
        landscape_fit: str = (all_candidates or {}).get("landscape_fit") or "fill"
        # Content-plan item renders are intentionally single-output for now. Public
        # generative jobs omit this key and keep the full multi-variant behavior.
        variant_policy: str | None = (all_candidates or {}).get("variant_policy") or None
        # Per-item silence-cut opt-out (plans/010 10A — support's per-item remedy).
        # Surface: Job.assembly_plan["silence_cut_disabled"] = true, set via
        # POST /admin/jobs/{id}/silence-cut-disable (admin_jobs.py). Read HERE at
        # render time, so only a FULL re-render (or a retried/redelivered render
        # of THIS job) picks the flag up — a caption reburn re-encodes the
        # already-cut base and keeps its cuts. Skips the whole cut stage
        # (retakes included). Job-scoped by design — top-level assembly_plan
        # keys survive every variant upsert/finalize merge (_set_status merges,
        # _finalize_job only replaces "variants").
        silence_cut_disabled: bool = (job.assembly_plan or {}).get("silence_cut_disabled") is True
        # Montage visual preset. Absent means classic so public/legacy jobs keep
        # byte-identical render behavior.
        montage_preset = coerce_montage_preset((all_candidates or {}).get("montage_preset"))

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths in all_candidates")

    # Durable per-job source copies (clip timeline editor). User uploads under
    # the 24h-lifecycle prefixes are snapshot to `generative-jobs/{job_id}/sources/`
    # BEFORE ingest, so a timeline edit days later can still re-render from the
    # original bytes. Strictly order-preserving 1:1 — _resolve_narrative_order
    # slices the first N keys of clip_id_to_gcs, so clip_paths order is
    # load-bearing. Best-effort: on any copy failure ALL original paths are kept.
    clip_paths_gcs = _persist_durable_sources(job_id, clip_paths_gcs)

    # ignore_cleanup_errors: on a soft-time-limit abort, an orphaned pre-tonemap
    # ffmpeg thread (outer pool shutdown wait=False) may still be writing sdr_*
    # files into tmpdir as this block unwinds. Without this flag a racing rmtree
    # could raise and MASK the SoftTimeLimitExceeded, routing to the generic
    # handler and losing the actionable "timed out" failure_reason. A temp-dir
    # cleanup error must never shadow the real exception.
    with tempfile.TemporaryDirectory(
        prefix="nova_generative_", ignore_cleanup_errors=True
    ) as tmpdir:
        # A narrated job (flag on + narrated format + voiceover) renders from the
        # voiceover + raw clips and never reads clip_metadata — skip the Gemini
        # clip analysis so it's faster, cheaper, and survives Gemini outages. This
        # condition mirrors the narrated branch of _resolve_archetype exactly, so we
        # only skip when narrated WILL be selected (a montage fallback still needs metas).
        _skip_clip_analysis = (
            settings.narrated_archetype_enabled
            and edit_format in NARRATED_EDIT_FORMATS
            and bool(voiceover_gcs_path)
        )
        ingest = _ingest_clips(
            clip_paths_gcs, tmpdir, job_id=job_id, skip_analysis=_skip_clip_analysis
        )
        clip_metas = ingest["clip_metas"]
        clip_id_to_gcs = ingest["clip_id_to_gcs"]
        clip_id_to_local = ingest["clip_id_to_local"]
        probe_map = ingest["probe_map"]
        clip_durations_s = {
            cid: float(getattr(probe_map.get(path), "duration_s", 0.0) or 0.0)
            for cid, path in clip_id_to_local.items()
            if probe_map.get(path) is not None
        }
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

        # Narrative order: clip_id_to_gcs preserves clip_paths order (insertion
        # order = upload index), so the first N keys ARE the guide clips in
        # guide order. The matcher tolerates ids missing from clip_metas
        # (degraded analysis) — it drops them from the spine with a warning.
        narrative_order = _resolve_narrative_order(
            narrative_shot_count, clip_id_to_gcs, job_id=job_id
        )
        if narrative_order:
            # Ground the hook text in the clip that actually OPENS the edit
            # (the guide's first shot), not the max-hook_score clip. Intro
            # SIZING (_hero_composition) is intentionally untouched — it picks
            # the most text-friendly clip because the overlay persists across
            # the whole video.
            hero = next((m for m in clip_metas if m.clip_id == narrative_order[0]), hero)

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
                clip_metas,
                hero,
                job_id=job_id,
                language=language,
                persona=persona,
                filming_guide=filming_guide_candidates,
                clip_notes=clip_notes_candidates,
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

        # B3: extract footage_type_bias from user_style as a soft tiebreaker.
        # When user_style is absent or empty (public/legacy jobs) → empty list →
        # byte-identical to pre-B3 behavior.
        _footage_type_bias: list[str] = list(
            (user_style.get("footage_type_bias") or []) if user_style else []
        )
        archetype, spine_clip_id, archetype_fallback_reason = _resolve_archetype(
            edit_format,
            clip_metas,
            clip_id_to_local,
            job_id=job_id,
            voiceover_gcs_path=voiceover_gcs_path,
            filming_guide=filming_guide_candidates,
            footage_type_bias=_footage_type_bias,
            clip_durations_s=clip_durations_s,
        )
        _set_status(job_id, "rendering")
        # Persist the style-downgrade reason so the item page can explain a montage
        # fallback to the user (trace events are admin-only). A retry that now
        # resolves cleanly clears the stale reason from the previous attempt.
        _persist_archetype_fallback(job_id, edit_format, archetype_fallback_reason)

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

        # Rhythm-mode quote authoring (editorial sequence without eligible
        # speech). Same grounding _run_text_agents feeds intro_writer; called
        # lazily per variant because the target sentence count tracks THAT
        # variant's rendered duration.
        def _author_quote(video_duration_s: float) -> str | None:
            return _author_sequence_quote(
                hero,
                job_id=job_id,
                video_duration_s=video_duration_s,
                language=language,
                persona=persona,
                filming_guide=filming_guide_candidates,
            )

        # Once-per-clip silence-cut artifacts (plans/010 7A): verbatim transcript,
        # CutPlan, and the cut base render are computed on first need and cached
        # here for the whole job, so every cut-capable variant shares ONE whisper
        # call and ONE cut encode (subtitled + the talking_head spine). Lives
        # under the job tmpdir — per-variant scratch cleanup
        # (shutil.rmtree(variant_dir)) never touches it.
        silence_cut_cache = _SilenceCutCache(os.path.join(tmpdir, "silence_cut"))

        def _render_one_spec(rank: int, spec: dict[str, Any], spine: str | None) -> dict[str, Any]:
            """Render a single (already non-resumable) spec: mark rendering →
            render → stamp finished → persist → free scratch. A talking_head
            SpineExtractionError propagates so the caller degrades the whole job
            to montage; other per-variant errors become failure records inside the
            render functions. Persists are row-locked (with_for_update), so this is
            safe to call concurrently across variants."""
            variant_id = spec["variant_id"]
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
            try:
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
                        language=language,
                        landscape_fit=landscape_fit,
                        silence_cut_disabled=silence_cut_disabled,
                        silence_cut_cache=silence_cut_cache,
                    )
                elif spec.get("archetype") == "narrated":
                    result = _render_narrated_variant(
                        job_id=job_id,
                        rank=rank,
                        spec=spec,
                        filming_guide=filming_guide_candidates,
                        narrative_order=narrative_order,
                        clip_id_to_local=clip_id_to_local,
                        variant_dir=variant_dir,
                        landscape_fit=landscape_fit,
                    )
                elif spec.get("archetype") == "subtitled":
                    result = _render_subtitled_variant(
                        job_id=job_id,
                        rank=rank,
                        spec=spec,
                        clip_id_to_local=clip_id_to_local,
                        variant_dir=variant_dir,
                        language=language,
                        landscape_fit=landscape_fit,
                        silence_cut_disabled=silence_cut_disabled,
                        silence_cut_cache=silence_cut_cache,
                        smart_captions=smart_captions,
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
                        narrative_order=narrative_order,
                        filming_guide=(
                            filming_guide_candidates if narrative_shot_count > 0 else None
                        ),
                        author_quote_fn=_author_quote,
                        language=language,
                        landscape_fit=landscape_fit,
                        montage_preset=montage_preset,
                    )

                # Per-variant render_finished_at on success (D6 tile clock).
                if result.get("ok"):
                    result["render_finished_at"] = datetime.utcnow().isoformat() + "Z"

                # Non-authoritative TextElement snapshot (T3 — plan-item-timeline).
                # Render path still reads legacy fields; text_elements is informational
                # until Phase 1 (T4) when the user first edits the overlay.
                _maybe_add_text_elements_snapshot(result)

                # Persist immediately so a deploy/OOM after this point can't lose it,
                # and so the status endpoint reveals variants as they finish rather
                # than all-at-once at _finalize_job.
                _upsert_variant_entry(job_id, result)
                return result
            finally:
                # Free this variant's scratch (assembled/audio_mixed/final mp4s +
                # the large Skia PNG sequences) the moment it's done. The output +
                # fast-reburn base are already uploaded to GCS by here, so nothing
                # local is still needed. Without this, all variants' scratch
                # coexisted under the job tmpdir until job end — a prime cause of
                # the worker /tmp exhaustion ("No space left on device").
                shutil.rmtree(variant_dir, ignore_errors=True)

        def _render_spec_set(
            specs: list[dict[str, Any]], spine: str | None
        ) -> list[dict[str, Any]]:
            """Render every spec, with resume reuse + immediate persist. Lets a
            talking_head SpineExtractionError propagate so the caller can degrade the
            WHOLE job to montage; per-variant non-spine errors become failure records
            inside the render functions.

            Renders the to-render specs serially by default, or concurrently when
            GENERATIVE_PARALLEL_VARIANTS_ENABLED is on (bounded by
            GENERATIVE_PARALLEL_VARIANTS_MAX). Concurrency is ONLY a win on a
            dedicated-CPU worker — see the flag docs + the fly.toml worker VM note."""
            results_by_rank: dict[int, dict[str, Any]] = {}
            to_render: list[tuple[int, dict[str, Any]]] = []
            for rank, spec in enumerate(specs, start=1):
                variant_id = spec["variant_id"]
                spec_track_id = spec["track"].id if spec["track"] else None
                reusable = prior.get(variant_id)
                if reusable is not None and reusable.get("music_track_id") == spec_track_id:
                    record_pipeline_event(
                        "assembly", "variant_resumed", {"variant_id": variant_id, "rank": rank}
                    )
                    results_by_rank[rank] = {**reusable, "rank": rank}
                    continue
                to_render.append((rank, spec))

            max_parallel = max(1, int(settings.GENERATIVE_PARALLEL_VARIANTS_MAX))
            if (
                settings.GENERATIVE_PARALLEL_VARIANTS_ENABLED
                and len(to_render) > 1
                and max_parallel > 1
            ):
                from concurrent.futures import (  # noqa: PLC0415
                    ThreadPoolExecutor,
                    as_completed,
                )

                workers = min(max_parallel, len(to_render))
                log.info(
                    "generative_variants_parallel",
                    job_id=job_id,
                    variant_count=len(to_render),
                    workers=workers,
                )
                pool = ThreadPoolExecutor(max_workers=workers)
                try:
                    futs = {
                        pool.submit(_render_one_spec, rank, spec, spine): rank
                        for rank, spec in to_render
                    }
                    for fut in as_completed(futs):
                        # A talking_head SpineExtractionError re-raises here and
                        # propagates so the caller degrades the whole job to montage.
                        results_by_rank[futs[fut]] = fut.result()
                finally:
                    pool.shutdown(wait=True)
            else:
                for rank, spec in to_render:
                    results_by_rank[rank] = _render_one_spec(rank, spec, spine)

            return [results_by_rank[rank] for rank in sorted(results_by_rank)]

        # Upfront pending-variant upsert: announce all variant IDs the moment the spec
        # set is known — before any render starts. The frontend sees a stable N-tile grid
        # from this point, never from a growing list that pops in mid-render (D7).
        initial_specs = _specs_for_archetype(
            archetype,
            best_track,
            voiceover_gcs_path=voiceover_gcs_path,
            voiceover_bed_level=voiceover_bed_level,
            voiceover_caption_style=voiceover_caption_style,
            variant_policy=variant_policy,
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
            # Persist the downgrade reason for the item-page banner (same contract as
            # the resolution-time stash above).
            _persist_archetype_fallback(job_id, edit_format, "spine_extraction_failed")
            # Re-upsert the montage fallback specs so the tile set stays consistent.
            fallback_specs = _specs_for_archetype(
                "montage",
                best_track,
                variant_policy=variant_policy,
            )
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
    # Overlay autoplace chain (plan 007, D2-B). MUST run AFTER _finalize_job —
    # finalize rebuilds every variant entry from the in-memory results whitelist,
    # so anything the match/apply tasks wrote mid-render would be stripped
    # (007 CRITICAL-1). Best-effort: render success never depends on it.
    try:
        _maybe_autoplace_after_finalize(job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace_chain_dispatch_failed", job_id=job_id, error=str(exc)[:200])
    try:
        _maybe_visual_blocks_after_finalize(job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("visual_blocks_chain_dispatch_failed", job_id=job_id, error=str(exc)[:200])


def _maybe_visual_blocks_after_finalize(job_id: str) -> None:
    """Dispatch first-edit block planning once for compatible ready variants."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.config import settings as _settings  # noqa: PLC0415
    from app.tasks.autoplace import (  # noqa: PLC0415
        _clear_visual_block_attempts,
        prepare_visual_block_assets,
    )

    if not (_settings.visual_blocks_enabled and _settings.visual_block_autoplan_enabled):
        return
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None or job.content_plan_item_id is None:
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        eligible: list[str] = []
        for variant in variants:
            if (
                variant.get("render_status") == "ready"
                and variant.get("base_video_path")
                and variant.get("text_mode") != "lyrics"
                and not variant.get("visual_blocks_autoplan_attempted")
            ):
                variant["visual_blocks_autoplan_attempted"] = True
                eligible.append(str(variant.get("variant_id")))
        if not eligible:
            return
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
        db.commit()
    try:
        prepare_visual_block_assets.apply_async(
            args=[job_id, eligible], queue=_settings.autoplace_queue
        )
    except Exception:
        _clear_visual_block_attempts(job_id, eligible)
        raise


def _maybe_autoplace_after_finalize(job_id: str) -> None:
    """Zero-click visual placement after a plan-item generate (plan 007, D2-B).

    Guards, in order (each traced/skipped, never raised to the caller):
      - OVERLAY_AUTOPLACE_ENABLED off ⇒ no-op (byte-identical behavior).
      - Public generative jobs (no content_plan_item_id) ⇒ no-op — the pool is
        a plan-item concept (finalize also serves /generative, 007 CRITICAL-1).
      - Pool has zero READY assets ⇒ no-op.
      - Per variant: only speech-bearing (music_track_id is None — Whisper on a
        song track yields garbage anchors, 007 G2-A), only rendered
        (video_path + ready), and only ONCE per render generation
        (`autoplace_attempted` marker, 007 CRITICAL-3: the overlay burn's own
        completion and acks_late re-deliveries never re-fire the chain).

    `auto_apply` follows OVERLAY_AUTOAPPLY_ENABLED (G3-A kill switch): off ⇒
    the chain still matches and the suggestions await review (suggest-only).
    """
    from sqlalchemy import func as _sql_func  # noqa: PLC0415
    from sqlalchemy import select as _select  # noqa: PLC0415
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.config import settings as _settings  # noqa: PLC0415
    from app.models import PlanItemAsset  # noqa: PLC0415
    from app.tasks.autoplace import match_overlay_suggestions  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None or job.content_plan_item_id is None:
            return
        # The persisted context pins the preset/version for determinism, but the
        # master switch remains an emergency kill switch throughout the job. A
        # rollout disabled while the render is in flight must not force the
        # zero-click matcher/apply chain afterward.
        smart_mode = bool(
            _settings.smart_captions_enabled and (job.all_candidates or {}).get("smart_captions")
        )
        # Smart Captions already consumed the analyzed pool inside its single
        # transcript-anchored plan. A second matcher would duplicate visuals,
        # change timing after SFX resolution, and violate spatial ownership.
        if smart_mode:
            return
        if not _settings.overlay_autoplace_enabled:
            return
        ready_assets = int(
            db.execute(
                _select(_sql_func.count())
                .select_from(PlanItemAsset)
                .where(
                    PlanItemAsset.plan_item_id == job.content_plan_item_id,
                    PlanItemAsset.status == "ready",
                )
            ).scalar_one()
        )
        if ready_assets == 0:
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        eligible: list[str] = []
        for v in variants:
            if (
                v.get("render_status") == "ready"
                and v.get("video_path")
                and v.get("music_track_id") is None
                and not v.get("autoplace_attempted")
            ):
                v["autoplace_attempted"] = True
                eligible.append(str(v.get("variant_id")))
        if not eligible:
            return
        user_id = str(job.user_id)
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
        db.commit()

    # Smart Captions is explicitly a zero-click cinematic render: selecting it
    # opts this item into auto-apply even when generic autoplace remains in
    # suggest-only rollout. Lane-level media/SFX kill switches still win inside
    # the shared apply helper.
    auto_apply = bool(_settings.overlay_autoapply_enabled or smart_mode)
    for variant_id in eligible:
        match_overlay_suggestions.apply_async(
            args=[job_id, variant_id, user_id],
            kwargs={"auto_apply": auto_apply, "smart_mode": smart_mode},
            queue=_settings.autoplace_queue,
        )
    log.info(
        "autoplace_chain_dispatched",
        job_id=job_id,
        variants=eligible,
        auto_apply=auto_apply,
        smart_mode=smart_mode,
    )


# ── Ingest (shared by the full job + the single-variant re-render) ──────────────


def _ingest_clips(
    clip_paths_gcs: list[str],
    tmpdir: str,
    *,
    job_id: str,
    min_success_fraction: float = 0.5,
    skip_analysis: bool = False,
) -> dict[str, Any]:
    """Download → probe → Gemini upload → clip_metadata. Reuses the proven helpers.

    Returns clip_metas, clip_id↔gcs/local maps, the probe map, and the hero clip.
    Raises when the analyzed fraction drops below ``min_success_fraction``
    (default: more than half failing aborts — right for renders, where cutting
    with half-garbage footage is worse than failing). Pool MATCHING passes 0.0:
    any analyzed subset is worth matching; only total failure raises.

    ``skip_analysis``: download + probe ONLY, no Gemini upload/clip_metadata. The
    narrated archetype renders from the voiceover + raw clips and never reads clip
    metadata, so analysis is wasted work there — and skipping it keeps narrated
    renders working when Gemini is rate-limited/unavailable. clip_metas is empty,
    hero is None; clip ids use the `clip_{idx}` synthetic convention.
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
    if skip_analysis:
        return {
            "clip_metas": [],
            "probe_map": probe_map,
            "clip_id_to_gcs": {_clip_id_for(None, i): gcs for i, gcs in enumerate(clip_paths_gcs)},
            "clip_id_to_local": {
                _clip_id_for(None, i): path for i, path in enumerate(local_clip_paths)
            },
            "hero": None,
        }
    file_refs = _upload_clips_parallel(local_clip_paths)
    clip_metas, failed_count = _analyze_clips_parallel(
        file_refs, local_clip_paths, probe_map, job_id=job_id
    )
    total = len(clip_metas) + failed_count
    if total == 0 or len(clip_metas) == 0 or failed_count > total * (1.0 - min_success_fraction):
        raise ValueError(
            f"{failed_count}/{total} clips failed clip_metadata — aborting "
            f"(min_success_fraction={min_success_fraction})"
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


def _resolve_narrative_order(
    narrative_shot_count: int,
    clip_id_to_gcs: dict[str, str],
    *,
    job_id: str,
) -> list[str] | None:
    """clip_ids of the filming guide's shot clips, in guide order — or None.

    `clip_id_to_gcs` preserves clip_paths order (built by index enumeration),
    so the first `narrative_shot_count` keys are the guide clips in guide
    order (dispatch contract: _dispatch_item_render reorders clip_paths).

    Render-time kill switch: NARRATIVE_CLIP_ORDER_ENABLED=false makes every
    render (queued jobs and re-renders alike) fall back to pure greedy
    matching — worker restart, no deploy. Both outcomes emit a pipeline event
    so /admin/jobs shows why an edit was or wasn't guide-ordered.
    """
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    if narrative_shot_count <= 0:
        return None
    if not settings.NARRATIVE_CLIP_ORDER_ENABLED:
        record_pipeline_event(
            "assembly",
            "narrative_order_skipped",
            {"reason": "kill_switch", "shot_count": narrative_shot_count},
        )
        log.info("narrative_order_kill_switch", job_id=job_id)
        return None
    ordered_ids = list(clip_id_to_gcs)[:narrative_shot_count]
    record_pipeline_event(
        "assembly",
        "narrative_order_applied",
        {"shot_count": narrative_shot_count, "clip_ids": ordered_ids},
    )
    return ordered_ids


def _durable_sources_prefix(job_id: str) -> str:
    """GCS prefix for a job's durable source-clip snapshots (clip timeline editor)."""
    return f"generative-jobs/{job_id}/sources/"


def _persist_durable_sources(job_id: str, clip_paths: list[str]) -> list[str]:
    """Snapshot each uploaded clip to a durable per-job key and rewrite clip_paths.

    `generative-jobs/*` is exempt from the 24h GCS lifecycle rule, so timeline
    edits can re-render long after the original `dev-user/*` uploads expire.
    Copies are server-side (`storage.copy_object`) — no egress.

    Contract:
      - STRICTLY order-preserving 1:1 rewrite (`{i:03d}_{basename}` keys) —
        downstream `clip_index` identity and narrative ordering both key off
        clip_paths positions.
      - All-or-nothing: ANY failure (copy or DB persist) logs a warning and
        returns ALL original paths — never a mixed list, never a failed job.
      - Idempotent: paths already under the durable prefix are not re-copied;
        an entirely-durable list short-circuits (acks_late re-runs).
    """
    if not settings.GENERATIVE_TIMELINE_EDITOR_ENABLED:
        return clip_paths
    from app.storage import copy_object  # noqa: PLC0415

    prefix = _durable_sources_prefix(job_id)
    if all(p.startswith(prefix) for p in clip_paths):
        return clip_paths  # already durable — idempotent re-run
    try:
        durable: list[str] = []
        for i, src in enumerate(clip_paths):
            if src.startswith(prefix):
                durable.append(src)
                continue
            dst = f"{prefix}{i:03d}_{os.path.basename(src)}"
            copy_object(src, dst)
            durable.append(dst)
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if job is not None:
                all_candidates = dict(job.all_candidates or {})
                all_candidates["clip_paths"] = list(durable)
                job.all_candidates = all_candidates
                db.commit()
    except Exception as exc:  # noqa: BLE001 — durability is best-effort, never job-fatal
        log.warning(
            "generative_durable_sources_failed",
            job_id=job_id,
            error=str(exc),
        )
        return clip_paths
    log.info("generative_durable_sources_persisted", job_id=job_id, clips=len(durable))
    return durable


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
    layout_override: str | None = None,
    timeline_override: list[dict] | None = None,
    font_family_override: str | None = None,
    effect_override: str | None = None,
    text_color_override: str | None = None,
    cluster_hero_font_override: str | None = None,
    cluster_body_font_override: str | None = None,
    cluster_accent_font_override: str | None = None,
    cluster_hero_size_px_override: int | None = None,
    cluster_body_size_px_override: int | None = None,
    cluster_accent_size_px_override: int | None = None,
    media_overlays_override: list[dict] | None = None,
    sfx_override: list[dict] | None = None,
    render_gen_id: str | None = None,
    intro_start_s_override: float | None = None,
    intro_end_s_override: float | None = None,
    text_behind_subject: bool | None = None,
    orientation_override: str | None = None,
    force_full_render: bool = False,
) -> None:
    """Re-render ONE variant of a generative job (swap-song / retext / restyle / resize / mix).

    Async by design (plan Decision 4): a re-slot against a new song is a full pipeline
    re-run, not an instant preview. Re-runs clip ingest, renders just the target
    variant, and updates that entry in `Job.assembly_plan["variants"]` in place.

    `timeline_override` (clip timeline editor): user-edited slot list — takes
    precedence over the variant's persisted `user_timeline`. On montage
    song_text/original_text variants, plus the internal preserve-cuts path for
    song_lyrics, an active timeline skips the whole ingest+Gemini+match leg
    (see `_run_regenerate_variant`).

    `text_behind_subject`: explicit text-behind-subject toggle (None = leave the
    persisted `intro_behind_subject` decision alone). Kwarg name is a frozen
    contract with the route layer's `.delay(...)` call.
    """
    log.info(
        "generative_regenerate_start",
        job_id=job_id,
        variant_id=variant_id,
        new_track_id=new_track_id,
        remove_text=remove_text,
        has_override=bool(override_text),
        style_set_id=style_set_id,
        layout_override=layout_override,
        font_override=bool(font_family_override),
        effect_override=bool(effect_override),
        color_override=bool(text_color_override),
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
                layout_override,
                timeline_override=timeline_override,
                font_family_override=font_family_override,
                effect_override=effect_override,
                text_color_override=text_color_override,
                cluster_hero_font_override=cluster_hero_font_override,
                cluster_body_font_override=cluster_body_font_override,
                cluster_accent_font_override=cluster_accent_font_override,
                cluster_hero_size_px_override=cluster_hero_size_px_override,
                cluster_body_size_px_override=cluster_body_size_px_override,
                cluster_accent_size_px_override=cluster_accent_size_px_override,
                media_overlays_override=media_overlays_override,
                sfx_override=sfx_override,
                render_gen_id=render_gen_id,
                intro_start_s_override=intro_start_s_override,
                intro_end_s_override=intro_end_s_override,
                text_behind_subject=text_behind_subject,
                orientation_override=orientation_override,
                force_full_render=force_full_render,
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
            # E1: a superseded task must not flip render_status to "failed" —
            # the newer commit's task owns the variant's terminal state now.
            _update_variant_entry(
                job_id,
                variant_id,
                {
                    "render_status": "failed",
                    "ok": False,
                    "error": str(exc),
                    "error_class": _classify_error(exc),
                },
                expected_render_gen_id=render_gen_id,
                outcome="failed",
            )


def _resolve_regen_text(
    override_text: str | None,
    remove_text: bool,
    existing_text_mode: str | None,
    persisted_text: str | None,
    persisted_highlight: str | None,
    *,
    run_text_agents_fn,  # callable: () -> (agent_text, agent_form)
    persisted_layout: str | None = None,
    persisted_word_roles: list[str] | None = None,
    persisted_behind_subject: bool | None = None,
) -> tuple:
    """Return (agent_text, agent_form, text_mode) without running the LLM when possible.

    Priority:
    1. remove_text → text_mode="none", no overlay.
    2. override_text → sanitised SimpleNamespace; a "none"-mode variant flips back
       to "agent_text" (re-adding text after removal), lyrics mode is preserved.
    3. persisted intro_text present + mode agent_text → reuse (no LLM).
    4. else → run intro_writer (first render or legacy variant).

    `persisted_layout` / `persisted_word_roles` keep a cluster intro a cluster
    across no-LLM re-renders. On override_text the roles are STALE (they aligned
    to the old words) so they are dropped — the layout engine re-derives roles
    heuristically, and falls back to linear when the new text doesn't suit a
    cluster. Absent fields (legacy variants) → "linear"/None, byte-identical.

    `persisted_behind_subject` mirrors `persisted_layout`: folded into the
    reconstructed `agent_form["behind_subject"]` on the no-LLM branches (2 and 3)
    so `_resolve_intro_overlay_params`'s agent-form fallback tier sees it. The
    LLM fall-through branch (4) is NOT folded — a fresh `OverlayFormatMatcherAgent`
    run makes its own behind_subject decision, which must not be clobbered.
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
            # word_roles deliberately absent: stale roles must never be applied
            # to user-typed words (the engine re-derives them).
            agent_text = _types.SimpleNamespace(text=cleaned, highlight_word=None)
            agent_form = {
                "effect": "karaoke-line",
                "layout": persisted_layout or "linear",
                "behind_subject": bool(persisted_behind_subject),
            }
            # A text-removed variant ("none" — truthy!) must flip back to
            # "agent_text" when the user supplies new text, or _reburn_text_on_base
            # skips the burn and the edit silently no-ops. Lyrics keep their mode.
            mode = (
                "agent_text"
                if existing_text_mode in (None, "none", "agent_text")
                else existing_text_mode
            )
            return agent_text, agent_form, mode
        # Nothing renderable after sanitization → no overlay (footage only).
        # Mode unchanged: a "none" variant stays text-free rather than becoming a
        # text-less "agent_text" that would re-trigger intro_writer later.
        return None, None, existing_text_mode or "agent_text"

    if existing_text_mode == "lyrics":
        # Lyrics variants have no AI intro. Falling through to intro_writer here
        # would fabricate one AND flip text_mode to "agent_text" — which then
        # makes the variant fast-reburn eligible, so later lyric-override
        # dispatches silently skip lyric re-injection (2026-07-18 E2E bug).
        return None, None, "lyrics"

    if persisted_text and existing_text_mode == "agent_text":
        # Reuse persisted text — NO LLM call.
        agent_text = _types.SimpleNamespace(
            text=persisted_text,
            highlight_word=persisted_highlight,
            word_roles=persisted_word_roles,
        )
        agent_form = {
            "effect": "karaoke-line",
            "layout": persisted_layout or "linear",
            "behind_subject": bool(persisted_behind_subject),
        }
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
    orientation_override: str | None = None,
) -> bool:
    """True iff this edit can use the cached-base fast-reburn path."""
    if not getattr(settings, "GENERATIVE_FAST_REBURN_ENABLED", True):
        return False
    if new_track_id is not None or mix_override is not None:
        return False  # audio changes → must full re-render
    # Orientation routes persist the requested value before the worker starts.
    # Comparing the override with ``existing["orientation"]`` therefore cannot
    # detect a real change: both already contain the new value. Any explicit
    # orientation request must rebuild from source clips so a cached portrait
    # base can never be stretched into a landscape encode (or vice versa).
    if orientation_override is not None:
        return False
    if not existing.get("base_video_path"):
        return False  # no cached base (legacy or lyrics variant)
    text_mode = existing.get("text_mode")
    if text_mode not in ("agent_text", "none"):
        # Lyrics-as-optional-elements: a `lyrics_baked=False` song_lyrics
        # variant's base is genuinely lyrics-free (same upload-before-burn
        # shape as agent_text's base), and `_reburn_text_on_base`'s
        # `text_mode == "lyrics"` branch already burns ONLY `text_elements`
        # (now including `role=lyric_line` ones) onto it — so it's eligible
        # for the real fast path too. Legacy lyrics variants (lyrics_baked
        # True/absent) keep the full-render-only v1 behavior.
        if not (text_mode == "lyrics" and existing.get("lyrics_baked") is False):
            return False
    return True


def _resolve_variant_orientation(
    existing: dict | None,
    orientation_override: str | None = None,
) -> str:
    candidate = orientation_override
    if candidate is None and existing is not None:
        candidate = existing.get("orientation")
    return "landscape" if candidate == "landscape" else "portrait"


def _canvas_kwargs(canvas: Canvas) -> dict[str, Canvas]:
    return {"canvas": canvas} if canvas != PORTRAIT else {}


def _lyrics_active(text_mode: str | None, lyrics_enabled) -> bool:
    if isinstance(lyrics_enabled, bool):
        return lyrics_enabled
    return text_mode == "lyrics"


def _is_collage_audio_only_swap_eligible(existing: dict, new_track_id: str | None) -> bool:
    """True when a song swap can preserve rendered collage visuals exactly."""
    if new_track_id is None:
        return False
    if not is_collage_montage_preset(existing.get("montage_preset_rendered")):
        return False
    if not existing.get("video_path"):
        return False
    if existing.get("variant_id") == "song_lyrics" or existing.get("text_mode") == "lyrics":
        return False
    return existing.get("music_track_id") is not None


def _mux_track_audio_preserve_video(
    *,
    video_gcs_path: str,
    track: MusicTrack,
    output_gcs_path: str,
    tmpdir: str,
    label: str,
) -> str:
    """Replace a finished video's audio with ``track`` while stream-copying video."""
    import subprocess  # noqa: PLC0415

    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415
    from app.tasks.template_orchestrate import _probe_duration  # noqa: PLC0415

    if not track.audio_gcs_path:
        raise ValueError(f"Track {track.id} has no audio_gcs_path")

    video_local = os.path.join(tmpdir, f"{label}_video.mp4")
    audio_local = os.path.join(tmpdir, f"{label}_audio.m4a")
    out_local = os.path.join(tmpdir, f"{label}_out.mp4")
    download_to_file(video_gcs_path, video_local)
    download_to_file(track.audio_gcs_path, audio_local)

    video_dur = _probe_duration(video_local)
    if video_dur <= 0:
        raise ValueError(f"Cannot audio-swap {video_gcs_path}: duration probe failed")

    audio_dur = _probe_duration(audio_local)
    cfg = track.track_config or {}
    safe_offset = max(0.0, float(cfg.get("best_start_s", 0.0) or 0.0))
    if audio_dur > 0 and safe_offset > 0:
        safe_offset = min(safe_offset, max(0.0, audio_dur - 5.0))

    fade_start = max(0.0, video_dur - 0.5)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_local,
        "-stream_loop",
        "-1",
        *(["-ss", f"{safe_offset:.3f}"] if safe_offset > 0 else []),
        "-i",
        audio_local,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-af",
        f"afade=t=out:st={fade_start:.3f}:d=0.5,loudnorm=I={settings.output_target_lufs}:TP=-1.5:LRA=11",  # noqa: E501
        "-t",
        f"{video_dur:.3f}",
        "-movflags",
        "+faststart",
        out_local,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-800:]
        raise RuntimeError(
            f"masonry audio-only song swap failed (rc={result.returncode}): {stderr}"
        )
    if not os.path.exists(out_local) or os.path.getsize(out_local) == 0:
        raise RuntimeError("masonry audio-only song swap produced empty output")
    return upload_public_read(out_local, output_gcs_path, content_type="video/mp4")


def _run_masonry_audio_only_song_swap(
    *,
    job_id: str,
    variant_id: str,
    existing: dict,
    track: MusicTrack,
    expected_render_gen_id: str | None = None,
) -> bool:
    """Fast song swap for masonry variants: video bytes are stream-copied."""
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    token = uuid.uuid4().hex
    path_fields = (
        "video_path",
        "base_video_path",
        "pre_media_overlay_video_path",
        "pre_sfx_video_path",
    )
    patch: dict[str, Any] = {
        "music_track_id": track.id,
        "track_title": track.title,
        "ok": True,
        "error": None,
        "render_error": None,
        "render_status": "ready",
        "render_finished_at": datetime.utcnow().isoformat() + "Z",
    }
    with tempfile.TemporaryDirectory(prefix="nova_masonry_audio_swap_") as tmpdir:
        for field in path_fields:
            source_gcs = existing.get(field)
            if not source_gcs:
                continue
            out_gcs = f"generative-jobs/{job_id}/audio-swap/{variant_id}_{token}_{field}.mp4"
            signed_url = _mux_track_audio_preserve_video(
                video_gcs_path=source_gcs,
                track=track,
                output_gcs_path=out_gcs,
                tmpdir=tmpdir,
                label=field,
            )
            patch[field] = out_gcs
            if field == "video_path":
                patch["output_url"] = signed_url

    if "video_path" not in patch or "output_url" not in patch:
        raise ValueError("masonry audio-only song swap missing current video_path")

    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=expected_render_gen_id,
        outcome="masonry_audio_swap",
    ):
        return False

    record_pipeline_event(
        "audio_mix",
        "masonry_audio_only_swap",
        {"variant_id": variant_id, "track_id": track.id},
    )
    _reapply_persisted_sfx_if_any(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=expected_render_gen_id,
    )
    return True


def _run_media_overlay_pass(
    *,
    job_id: str,
    variant_id: str,
    overlays_raw: list[dict],
    expected_render_gen_id: str | None = None,
    deadline_monotonic: float | None = None,
) -> None:
    """Apply (or clear) media-overlay cards on a finished variant (fast path).

    `deadline_monotonic` (R4-2): wall-clock ceiling threaded from callers that
    enter this pass mid-task (the caption reburn terminals) — clamps the
    fullscreen encode budget inside apply_media_overlays. None (default) keeps
    the standalone-task behavior byte-identical.

    Steps:
    1. Load the variant entry from the DB.
    2. Parse + validate the overlay list (coerce_media_overlays).
    3. If overlays is empty/None → restore from pre_media_overlay_video_path (clear).
    4. Otherwise → ensure a clean base copy exists (pre_media_overlay_video_path),
       then apply_media_overlays on top of it → overwrite video_path.
    5. Persist updated variant keys + re-sign output_url.

    Uses `storage.copy_object` (server-side, no egress) to durable-copy the
    original variant before the first apply-pass; subsequent card edits work
    from the same clean copy.
    """
    from app.agents._schemas.media_overlay import coerce_media_overlays  # noqa: PLC0415
    from app.pipeline.media_overlay import apply_media_overlays  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415
    from app.storage import copy_object  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=expected_render_gen_id is not None)
        if job is None:
            log.error("media_overlay_job_not_found", job_id=job_id)
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
        if existing is None:
            log.error("media_overlay_variant_not_found", job_id=job_id, variant_id=variant_id)
            return

        current_video_path = existing.get("video_path")
        if not current_video_path:
            log.error("media_overlay_no_video_path", job_id=job_id, variant_id=variant_id)
            return

        cards = coerce_media_overlays(overlays_raw)

        # Stale-bake detection baseline (plan 009 E5): snapshot the PERSISTED
        # card list before the long ffmpeg run. If the user autosaves an edit
        # (e.g. a PiP↔fullscreen toggle) while the bake runs, the write-back
        # below must NOT clobber it with this task's older list.
        import json as _json_e5  # noqa: PLC0415

        def _canon_cards(raw: object) -> str:
            try:
                return _json_e5.dumps(raw or [], sort_keys=True)
            except (TypeError, ValueError):
                return "[]"

        overlays_at_start = _canon_cards(existing.get("media_overlays"))

        # When SFX are persisted, the terminal SFX reapply pass (the hook at the
        # end of both branches below) owns the final render_status. Keep THIS
        # overlay pass non-terminal ("rendering") so the frontend download poll
        # never observes the intermediate overlay-only "ready" before SFX are
        # remixed on top — otherwise it can download a file missing its SFX
        # (two-pass observability). When no SFX are persisted the reapply is a
        # no-op, so this pass stays terminal as before.
        from app.config import settings as _settings_ov  # noqa: PLC0415

        will_reapply_sfx = bool(existing.get("smart_music_treatment")) or (
            bool(existing.get("sound_effects")) and _settings_ov.sound_effects_enabled
        )

        # ── Clear path: remove all cards ──────────────────────────────────────
        if not cards:
            clean_path = existing.get("pre_media_overlay_video_path")
            if clean_path and clean_path != current_video_path:
                # Restore the clean variant by overwriting current with the clean copy.
                copy_object(clean_path, current_video_path)
                # Re-sign for 1 day (matches PLAYBACK_URL_TTL_MIN in generative_jobs.py).
                from app.storage import signed_get_url  # noqa: PLC0415

                signed_url = signed_get_url(current_video_path, expiration_minutes=60 * 24)
            else:
                signed_url = existing.get("output_url", "")

            # Patch the variant.
            for v in variants:
                if v.get("variant_id") == variant_id:
                    current = v.get("render_generation_id")
                    if (
                        expected_render_gen_id is not None
                        and current is not None
                        and current != expected_render_gen_id
                    ):
                        log.warning(
                            "stale_render_write_discarded",
                            job_id=job_id,
                            variant_id=variant_id,
                            outcome="media_overlay_clear",
                            expected_gen_id=expected_render_gen_id,
                            actual_gen_id=current,
                        )
                        return
                    v["media_overlays"] = None
                    v["output_url"] = signed_url
                    if will_reapply_sfx:
                        v["render_status"] = "rendering"  # SFX reapply sets terminal "ready"
                    else:
                        v["render_status"] = "ready"
                        v["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
            record_pipeline_event("media_overlay", "cards_cleared", {"variant_id": variant_id})
            # Terminal hook: re-apply persisted SFX on top of the restored clean variant.
            sfx_owned = _reapply_persisted_sfx_if_any(
                job_id=job_id,
                variant_id=variant_id,
                expected_render_gen_id=expected_render_gen_id,
            )
            if will_reapply_sfx and not sfx_owned:
                _finalize_overlay_deferred_terminal(
                    job_id=job_id,
                    variant_id=variant_id,
                    expected_render_gen_id=expected_render_gen_id,
                )
            return

        # ── Apply path: composite cards onto the clean base ──────────────────
        pre_clean = existing.get("pre_media_overlay_video_path")
        if not pre_clean:
            # First apply-pass: durable-copy the base.
            # Source from pre_sfx_video_path when present so the overlay clean copy
            # is never SFX-contaminated (SFX is the outermost layer).
            base_for_clean = existing.get("pre_sfx_video_path") or current_video_path
            pre_clean = current_video_path + "_pre_overlay"
            try:
                copy_object(base_for_clean, pre_clean)
                log.info("media_overlay_clean_copy_created", job_id=job_id, dst=pre_clean)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "media_overlay_clean_copy_failed",
                    job_id=job_id,
                    error=str(exc),
                )
                # Continue with current_video_path as the base (no restore on clear).
                pre_clean = current_video_path

        try:
            overlay_canvas = canvas_for_orientation(existing.get("orientation"))
            new_url = apply_media_overlays(
                base_gcs_path=pre_clean,
                cards=cards,
                output_gcs_path=current_video_path,
                job_id=job_id,
                deadline_monotonic=deadline_monotonic,
                **_canvas_kwargs(overlay_canvas),
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "media_overlay_apply_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc),
                exc_info=True,
            )
            for v in variants:
                if v.get("variant_id") == variant_id:
                    current = v.get("render_generation_id")
                    if (
                        expected_render_gen_id is not None
                        and current is not None
                        and current != expected_render_gen_id
                    ):
                        log.warning(
                            "stale_render_write_discarded",
                            job_id=job_id,
                            variant_id=variant_id,
                            outcome="media_overlay_failed",
                            expected_gen_id=expected_render_gen_id,
                            actual_gen_id=current,
                        )
                        return
                    v["render_status"] = "failed"
                    v["render_error"] = str(exc)[:500]
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
            record_pipeline_event("media_overlay", "apply_failed", {"error": str(exc)[:200]})
            return

        # Re-read FRESH under a row lock before writing back (plan 009 E5): the
        # ffmpeg run above took minutes; autosaves may have landed since. The
        # variants list loaded at task start is stale — never write it back.
        if hasattr(db, "expire_all"):
            db.expire_all()
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        stale_write_skipped = False
        for v in variants:
            if v.get("variant_id") == variant_id:
                current = v.get("render_generation_id")
                if (
                    expected_render_gen_id is not None
                    and current is not None
                    and current != expected_render_gen_id
                ):
                    log.warning(
                        "stale_render_write_discarded",
                        job_id=job_id,
                        variant_id=variant_id,
                        outcome="media_overlay_apply",
                        expected_gen_id=expected_render_gen_id,
                        actual_gen_id=current,
                    )
                    return
                if _canon_cards(v.get("media_overlays")) == overlays_at_start:
                    v["media_overlays"] = [c.model_dump() for c in cards]
                else:
                    # User edited during the bake — their metadata wins; the
                    # video shows this bake's cards until the next Download.
                    stale_write_skipped = True
                v["pre_media_overlay_video_path"] = pre_clean
                v["output_url"] = new_url
                if will_reapply_sfx:
                    v["render_status"] = "rendering"  # SFX reapply sets terminal "ready"
                else:
                    v["render_status"] = "ready"
                    v["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
                break
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        flag_modified(job, "assembly_plan")
        db.commit()
        record_pipeline_event(
            "media_overlay",
            "cards_applied",
            {
                "variant_id": variant_id,
                "card_count": len(cards),
                "stale_write_skipped": stale_write_skipped,
            },
        )
        # Terminal hook: re-apply persisted SFX on top of the newly composited video.
        sfx_owned = _reapply_persisted_sfx_if_any(
            job_id=job_id,
            variant_id=variant_id,
            expected_render_gen_id=expected_render_gen_id,
        )
        if will_reapply_sfx and not sfx_owned:
            _finalize_overlay_deferred_terminal(
                job_id=job_id,
                variant_id=variant_id,
                expected_render_gen_id=expected_render_gen_id,
            )


def _finalize_overlay_deferred_terminal(
    *,
    job_id: str,
    variant_id: str,
    expected_render_gen_id: str | None,
) -> None:
    """R1-3 sibling for the overlay pass: it deferred its terminal render_status
    to the SFX reapply hook (will_reapply_sfx → wrote "rendering"), but the hook
    reported no ownership — e.g. SFX cleared in the DB mid-bake via the
    persist-only save, or the flag flipped off mid-run. Finalize "ready" exactly
    like the caption terminals do (token-gated), or the variant strands in
    "rendering" behind the 409 gate forever."""
    _update_variant_entry(
        job_id,
        variant_id,
        {
            "render_status": "ready",
            "render_finished_at": datetime.utcnow().isoformat() + "Z",
        },
        expected_render_gen_id=expected_render_gen_id,
        outcome="media_overlay_sfx_reapply_noop",
    )


# Variant keys that snapshot a "clean" (pre-effect) video for the fast lanes.
_MEDIA_SNAPSHOT_FIELDS = ("pre_media_overlay_video_path", "pre_sfx_video_path")

# Caption tasks enter the overlay reapply pass mid-task, AFTER a burn that may have
# consumed minutes — apply_media_overlays' fullscreen budget was sized for a task
# that STARTS with the pass (R4-2). The soft limit mirrors the caption tasks'
# decorators (literal there — test_task_time_limits.py pins the decorator source);
# the margin leaves room to persist the failed/ready terminal before SIGKILL.
_CAPTION_TASK_SOFT_TIME_LIMIT_S = 1740
_REAPPLY_DEADLINE_MARGIN_S = 120


def _stage_media_snapshot_nulls(
    patch: dict[str, Any],
    current: dict[str, Any],
    *,
    fields: tuple[str, ...] = _MEDIA_SNAPSHOT_FIELDS,
) -> list[str]:
    """Null retired pre_* snapshot fields in `patch`; return the retired keys.

    NO deletes here — callers free the returned keys AFTER their gen-gated write
    is accepted / their transaction commits (R1-2: a superseded write must never
    delete the winning render's snapshots; F4: no network I/O inside an open
    transaction). Keep-set guard: the fast passes alias the snapshot to
    `video_path` itself when their durable copy fails, so a key equal to ANY
    live reference (`video_path` / `base_video_path` on the patch or the current
    variant) is nulled but never returned for deletion.
    """
    keep = {
        current.get("video_path"),
        current.get("base_video_path"),
        patch.get("video_path"),
        patch.get("base_video_path"),
    }
    keep.discard(None)
    retired: list[str] = []
    for field in fields:
        snapshot = current.get(field)
        if snapshot and snapshot not in keep:
            retired.append(snapshot)
        patch[field] = None
    return retired


def _free_media_snapshot_keys(keys: list[str]) -> None:
    """Best-effort delete of retired snapshot blobs (D16-C).

    `generative-jobs/*` never expires and has no sweeper, so a snapshot key that
    is simply nulled strands its blob forever. Prefix-confined: the shared bucket
    holds curated forever-assets (music/*, templates/*), so anything outside
    generative-jobs/* is skipped, never deleted. delete_object_best_effort never
    raises — it returns False on failure, which only strands a blob.
    """
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    for key in keys:
        if not key.startswith("generative-jobs/"):
            log.warning("media_snapshot_free_skipped_foreign_prefix", key=key)
            continue
        if not delete_object_best_effort(key):
            log.warning("media_snapshot_free_failed", key=key)


def _free_retired_media_snapshots(
    current: dict[str, Any],
    keep_paths: tuple[str | None, ...] = (),
    *,
    fields: tuple[str, ...] = _MEDIA_SNAPSHOT_FIELDS,
) -> None:
    """Free-only path for post-write sites (R1-2): the caption terminals stage the
    None fields in their patch dict literally, so after the accepted terminal
    write only the freeing remains. `keep_paths` carries the patch's new
    video/base keys; `current`'s own live references join the keep set."""
    keep = {current.get("video_path"), current.get("base_video_path"), *keep_paths}
    keep.discard(None)
    _free_media_snapshot_keys(
        [current[f] for f in fields if current.get(f) and current[f] not in keep]
    )


def _null_and_free_media_snapshots(
    patch: dict[str, Any],
    current: dict[str, Any],
    *,
    fields: tuple[str, ...] = _MEDIA_SNAPSHOT_FIELDS,
) -> None:
    """One-shot null+free (stage + free composed). Hot paths use the split forms
    so deletes land only after an accepted write / committed transaction —
    reach for this only where no gen-gated write or open transaction is in play."""
    _free_media_snapshot_keys(_stage_media_snapshot_nulls(patch, current, fields=fields))


def _will_reapply_media_layers(variant: dict) -> bool:
    """True when _reapply_user_media_layers will run at least one pass for this
    variant — the OV-7 deferred-terminal condition: the caption terminals keep
    render_status="rendering" and let the reapply chain own the final
    ready/failed, so a poll never observes an effect-less "ready"."""
    return (
        (bool(variant.get("media_overlays")) and settings.media_overlays_enabled)
        or bool(variant.get("smart_music_treatment"))
        or (bool(variant.get("sound_effects")) and settings.sound_effects_enabled)
    )


def _reapply_user_media_layers(
    *,
    job_id: str,
    variant_id: str,
    expected_render_gen_id: str | None = None,
    deadline_monotonic: float | None = None,
) -> bool:
    """Rebuild the user's persisted media lanes on a freshly rendered base (3A).

    Overlays first — that pass owns the follow-up SFX hook because SFX is the
    outermost audio layer. If no overlays are persisted (or the overlay feature
    is disabled), fall through to the SFX-only hook. Shared by the montage
    full-render/fast-reburn terminals and the three caption re-render terminals.

    Returns True when the chain took ownership of the terminal render_status
    (ran a pass, marked the variant failed, or found itself superseded by a
    newer generation). False = the chain no-oped: a caller that deferred its
    terminal status (OV-7) must finalize itself or the variant strands (R1-3).
    """
    if _reapply_persisted_media_overlays_if_any(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=expected_render_gen_id,
        deadline_monotonic=deadline_monotonic,
    ):
        return True
    return _reapply_persisted_sfx_if_any(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=expected_render_gen_id,
    )


def _reapply_prep_superseded(existing: dict, expected_render_gen_id: str | None) -> bool:
    """F4: same stale-generation rule as `_update_variant_entry`, applied to the
    reapply preps' row-locked read — a superseded chain must not touch snapshots
    or run a pass (the newer generation owns the variant's state now)."""
    current = existing.get("render_generation_id")
    return (
        expected_render_gen_id is not None
        and current is not None
        and current != expected_render_gen_id
    )


def _reapply_persisted_media_overlays_if_any(
    *,
    job_id: str,
    variant_id: str,
    expected_render_gen_id: str | None = None,
    deadline_monotonic: float | None = None,
) -> bool:
    """Re-apply media overlays after a fresh full re-render, if any are persisted.

    Full re-renders produce a fresh base video without media-overlay cards. Reset
    stale clean-copy snapshots, then route through _run_media_overlay_pass so its
    normal terminal behavior and SFX reapply hook stay single-sourced.

    Returns True iff this helper took ownership of the terminal render_status
    (pass ran / marked failed / superseded by a newer generation); False = no-op.
    """
    from app.config import settings as _settings_overlay  # noqa: PLC0415

    if not _settings_overlay.media_overlays_enabled:
        return False

    try:
        with _sync_session() as db:
            # F4: row-locked RMW — the gen compare, the snapshot-null staging,
            # and the commit happen under ONE lock; blob deletes run after
            # (mirrors _upsert_variant_entry, same clobber class as PR #595).
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if job is None:
                return False
            variants = list((job.assembly_plan or {}).get("variants") or [])
            existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
            if existing is None:
                return False
            if _reapply_prep_superseded(existing, expected_render_gen_id):
                log.warning(
                    "stale_render_write_discarded",
                    job_id=job_id,
                    variant_id=variant_id,
                    outcome="media_overlay_reapply_prep",
                    expected_gen_id=expected_render_gen_id,
                    actual_gen_id=existing.get("render_generation_id"),
                )
                return True  # the newer generation owns the terminal status
            overlays_raw = existing.get("media_overlays")
            if not overlays_raw:
                return False
            retired_keys: list[str] = []
            for v in variants:
                if v.get("variant_id") == variant_id:
                    # SFX is re-applied after overlays. Any old SFX clean copy
                    # points at the pre-full-render video, so force a fresh one.
                    # Collect the orphaned keys only (D16-C) — the best-effort
                    # GCS deletes run AFTER the commit (no I/O in the txn, F4).
                    retired_keys = _stage_media_snapshot_nulls(v, v)
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
        _free_media_snapshot_keys(retired_keys)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_overlay_reapply_prep_failed", job_id=job_id, error=str(exc))
        _mark_variant_failed(
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            expected_render_gen_id=expected_render_gen_id,
        )
        return True

    try:
        _run_media_overlay_pass(
            job_id=job_id,
            variant_id=variant_id,
            overlays_raw=overlays_raw,
            expected_render_gen_id=expected_render_gen_id,
            deadline_monotonic=deadline_monotonic,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("media_overlay_reapply_failed", job_id=job_id, error=str(exc))
        _mark_variant_failed(
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            expected_render_gen_id=expected_render_gen_id,
        )
    return True


def _reapply_persisted_sfx_if_any(
    *,
    job_id: str,
    variant_id: str,
    expected_render_gen_id: str | None = None,
) -> bool:
    """Re-apply sound effects after an overlay edit, if any SFX are persisted.

    SFX is the outermost layer: an overlay edit replaces video_path from the
    overlay-clean base, so any SFX mixed on top are wiped. This terminal hook
    fires at the end of both branches of _run_media_overlay_pass to restore
    the SFX layer.

    No-op when SOUND_EFFECTS_ENABLED is False or the variant has no persisted SFX.
    Never raises — best-effort, overlay success must not be gated on SFX reapply.

    Returns True iff this helper took ownership of the terminal render_status
    (pass ran / marked failed / superseded by a newer generation); False = no-op.
    """
    from app.config import settings as _settings_sfx  # noqa: PLC0415

    try:
        with _sync_session() as db:
            # F4: row-locked RMW — see _reapply_persisted_media_overlays_if_any.
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if job is None:
                return False
            variants = list((job.assembly_plan or {}).get("variants") or [])
            existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
            if existing is None:
                return False
            if _reapply_prep_superseded(existing, expected_render_gen_id):
                log.warning(
                    "stale_render_write_discarded",
                    job_id=job_id,
                    variant_id=variant_id,
                    outcome="sfx_reapply_prep",
                    expected_gen_id=expected_render_gen_id,
                    actual_gen_id=existing.get("render_generation_id"),
                )
                return True  # the newer generation owns the terminal status
            sfx_raw = (
                existing.get("sound_effects") or [] if _settings_sfx.sound_effects_enabled else []
            )
            if not sfx_raw and not existing.get("smart_music_treatment"):
                return False
            # pre_sfx_video_path must now point to the newly composited overlay video
            # (the current video_path post-overlay), so SFX are re-applied on top.
            # Clear the stale pre_sfx_video_path so _run_sfx_pass takes a fresh
            # copy; the orphaned blob is freed AFTER the commit (D16-C + F4).
            retired_keys: list[str] = []
            for v in variants:
                if v.get("variant_id") == variant_id:
                    retired_keys = _stage_media_snapshot_nulls(v, v, fields=("pre_sfx_video_path",))
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
        _free_media_snapshot_keys(retired_keys)
    except Exception as exc:  # noqa: BLE001
        log.warning("sfx_reapply_prep_failed", job_id=job_id, error=str(exc))
        # The overlay pass deferred its terminal state to this reapply (it left
        # render_status="rendering"). A prep failure means _run_sfx_pass never
        # runs, so surface it as a failed render rather than stranding the
        # variant in "rendering" forever.
        _mark_variant_failed(
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            expected_render_gen_id=expected_render_gen_id,
        )
        return True

    try:
        _run_sfx_pass(
            job_id=job_id,
            variant_id=variant_id,
            sfx_raw=sfx_raw,
            expected_render_gen_id=expected_render_gen_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sfx_reapply_failed", job_id=job_id, error=str(exc))
        # _run_sfx_pass sets render_status="failed" itself on a HANDLED apply
        # error; this catch covers UNHANDLED errors, which would otherwise leave
        # the overlay-deferred "rendering" state stuck. Surface it.
        _mark_variant_failed(
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            expected_render_gen_id=expected_render_gen_id,
        )
    return True


def _mark_variant_failed(
    *,
    job_id: str,
    variant_id: str,
    error: str,
    expected_render_gen_id: str | None = None,
) -> None:
    """Best-effort flip of a variant to render_status="failed".

    Used when a pass that DEFERRED its terminal render_status (e.g. an overlay
    pass that handed off to a failing SFX reapply) would otherwise strand the
    variant in "rendering". Never raises.
    """
    try:
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if job is None:
                return
            variants = list((job.assembly_plan or {}).get("variants") or [])
            for v in variants:
                if v.get("variant_id") == variant_id:
                    current = v.get("render_generation_id")
                    if (
                        expected_render_gen_id is not None
                        and current is not None
                        and current != expected_render_gen_id
                    ):
                        log.warning(
                            "stale_render_write_discarded",
                            job_id=job_id,
                            variant_id=variant_id,
                            outcome="sfx_failed",
                            expected_gen_id=expected_render_gen_id,
                            actual_gen_id=current,
                        )
                        return
                    v["render_status"] = "failed"
                    v["render_error"] = error[:500]
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("mark_variant_failed_error", job_id=job_id, error=str(exc))


def _run_sfx_pass(
    *,
    job_id: str,
    variant_id: str,
    sfx_raw: list[dict],
    expected_render_gen_id: str | None = None,
) -> None:
    """Apply (or clear) sound-effect placements on a finished variant (fast path).

    Steps:
    1. Load the variant entry from the DB.
    2. Parse + validate the SFX list (coerce_sound_effects).
    3. If sfx_raw is empty/None → restore from pre_sfx_video_path (clear).
    4. Otherwise → ensure a clean base copy exists (pre_sfx_video_path),
       then apply_sound_effects on top of it → overwrite video_path.
    5. Persist updated variant keys + re-sign output_url.

    SFX is the OUTERMOST audio layer: pre_sfx_video_path = final variant
    with overlays applied but without SFX. When overlays are edited, they
    must re-apply SFX on top (handled by the terminal hook in
    _run_media_overlay_pass). The clean-copy for overlays is sourced from
    pre_sfx_video_path when present so the overlay base is never SFX-contaminated.
    """
    from app.agents._schemas.sound_effect import coerce_sound_effects  # noqa: PLC0415
    from app.pipeline.sound_effects import (  # noqa: PLC0415
        apply_smart_audio_treatment,
        apply_sound_effects,
    )
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415
    from app.storage import copy_object  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=expected_render_gen_id is not None)
        if job is None:
            log.error("sfx_job_not_found", job_id=job_id)
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
        if existing is None:
            log.error("sfx_variant_not_found", job_id=job_id, variant_id=variant_id)
            return

        current_video_path = existing.get("video_path")
        if not current_video_path:
            log.error("sfx_no_video_path", job_id=job_id, variant_id=variant_id)
            return

        placements = coerce_sound_effects(sfx_raw) or []
        music_treatment = existing.get("smart_music_treatment")

        # ── Clear path: remove all effects ───────────────────────────────────
        if not placements and not music_treatment:
            clean_path = existing.get("pre_sfx_video_path")
            if clean_path and clean_path != current_video_path:
                copy_object(clean_path, current_video_path)
                from app.storage import signed_get_url  # noqa: PLC0415

                signed_url = signed_get_url(current_video_path, expiration_minutes=60 * 24)
            else:
                signed_url = existing.get("output_url", "")

            for v in variants:
                if v.get("variant_id") == variant_id:
                    current = v.get("render_generation_id")
                    if (
                        expected_render_gen_id is not None
                        and current is not None
                        and current != expected_render_gen_id
                    ):
                        log.warning(
                            "stale_render_write_discarded",
                            job_id=job_id,
                            variant_id=variant_id,
                            outcome="sfx_clear",
                            expected_gen_id=expected_render_gen_id,
                            actual_gen_id=current,
                        )
                        return
                    v["sound_effects"] = None
                    v["output_url"] = signed_url
                    v["render_status"] = "ready"
                    v["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
            record_pipeline_event("sound_effects", "effects_cleared", {"variant_id": variant_id})
            return

        # ── Apply path: mix effects onto the clean base ───────────────────────
        # pre_sfx_video_path is the variant with overlays but WITHOUT SFX.
        pre_clean = existing.get("pre_sfx_video_path")
        if not pre_clean:
            # First apply-pass: durable-copy the current variant.
            pre_clean = current_video_path + "_pre_sfx"
            try:
                copy_object(current_video_path, pre_clean)
                log.info("sfx_clean_copy_created", job_id=job_id, dst=pre_clean)
            except Exception as exc:  # noqa: BLE001
                log.warning("sfx_clean_copy_failed", job_id=job_id, error=str(exc))
                pre_clean = current_video_path

        try:
            from app.agents._schemas.visual_block import coerce_visual_blocks  # noqa: PLC0415

            muted_sfx_intervals = (
                [
                    (block.start_s, block.end_s)
                    for block in coerce_visual_blocks(existing.get("visual_blocks") or [])
                    if block.audio_policy.sfx == "mute"
                ]
                if settings.visual_blocks_enabled
                else []
            )
            if music_treatment:
                new_url, audio_receipt = apply_smart_audio_treatment(
                    base_gcs_path=pre_clean,
                    effects=placements,
                    output_gcs_path=current_video_path,
                    music_bed=music_treatment,
                    job_id=job_id,
                )
            else:
                new_url = apply_sound_effects(
                    base_gcs_path=pre_clean,
                    effects=placements,
                    output_gcs_path=current_video_path,
                    job_id=job_id,
                    mute_intervals=muted_sfx_intervals,
                )
                audio_receipt = None
        except Exception as exc:  # noqa: BLE001
            log.error(
                "sfx_apply_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc),
                exc_info=True,
            )
            for v in variants:
                if v.get("variant_id") == variant_id:
                    current = v.get("render_generation_id")
                    if (
                        expected_render_gen_id is not None
                        and current is not None
                        and current != expected_render_gen_id
                    ):
                        log.warning(
                            "stale_render_write_discarded",
                            job_id=job_id,
                            variant_id=variant_id,
                            outcome="sfx_failed",
                            expected_gen_id=expected_render_gen_id,
                            actual_gen_id=current,
                        )
                        return
                    v["render_status"] = "failed"
                    v["render_error"] = str(exc)[:500]
                    break
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            flag_modified(job, "assembly_plan")
            db.commit()
            record_pipeline_event("sound_effects", "apply_failed", {"error": str(exc)[:200]})
            return

        for v in variants:
            if v.get("variant_id") == variant_id:
                current = v.get("render_generation_id")
                if (
                    expected_render_gen_id is not None
                    and current is not None
                    and current != expected_render_gen_id
                ):
                    log.warning(
                        "stale_render_write_discarded",
                        job_id=job_id,
                        variant_id=variant_id,
                        outcome="sfx_apply",
                        expected_gen_id=expected_render_gen_id,
                        actual_gen_id=current,
                    )
                    return
                v["sound_effects"] = [p.model_dump() for p in placements]
                v["pre_sfx_video_path"] = pre_clean
                v["output_url"] = new_url
                if audio_receipt is not None:
                    v["smart_audio_receipt"] = audio_receipt
                v["render_status"] = "ready"
                v["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
                break
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        flag_modified(job, "assembly_plan")
        db.commit()
        record_pipeline_event(
            "sound_effects",
            "effects_applied",
            {"variant_id": variant_id, "effect_count": len(placements)},
        )


def _maybe_add_text_elements_snapshot(result: dict) -> None:
    """Attach a non-authoritative TextElement snapshot to a freshly-rendered variant.

    Called immediately after each variant render succeeds (before
    `_upsert_variant_entry`) so the snapshot is persisted alongside the other
    variant fields.  The render path still reads the legacy ``intro_text`` /
    ``scenes`` / etc. fields; ``text_elements`` is purely informational until
    Phase 1 (T4) when the user first edits and sets ``text_elements_user_edited=True``.

    No-op when:
      - ``_TEXT_ELEMENTS_ENABLED`` is False (kill switch)
      - the render failed (``ok`` is falsy)
      - ``text_elements_user_edited`` is true (the user's saved list is authoritative)
      - ``text_elements_for_variant`` raises or returns an empty list
    Never raises — a snapshot failure must never block a completed render.
    """
    if not _TEXT_ELEMENTS_ENABLED or not result.get("ok"):
        return
    if result.get("text_elements_user_edited"):
        return
    try:
        from app.agents._schemas.text_element import text_elements_for_variant  # noqa: PLC0415

        te_list = text_elements_for_variant(result)
        result["text_elements"] = [e.model_dump() for e in te_list]
    except Exception:  # noqa: BLE001 — snapshot is informational; never block the render
        pass


# ── Text-behind-subject matte resolution (shared: first render + reburn) ────

# Padding applied to each behind_subject overlay's [start_s, end_s] window before
# computing the matte, so the occlusion mask covers a hair before/after the text
# is actually on screen (reveal/hold crossfade edges, frame-rounding at burn time).
_SUBJECT_MATTE_WINDOW_PAD_S = 0.25


def _behind_subject_windows(overlays: list[dict], duration_s: float) -> list:
    """Union of padded, duration-clamped windows for every `behind_subject: True`
    overlay. Adjacent/overlapping windows are merged so `compute_subject_matte`
    never re-computes the same span twice. `duration_s` <= 0 skips the upper
    clamp (caller couldn't probe the video — matte compute will still bound
    itself against the actual decoded frame count)."""
    from app.pipeline.subject_matte import MatteWindow  # noqa: PLC0415

    raw: list[tuple[float, float]] = []
    for ov in overlays:
        if not ov.get("behind_subject"):
            continue
        start_s = max(0.0, float(ov.get("start_s", 0.0)) - _SUBJECT_MATTE_WINDOW_PAD_S)
        end_s = float(ov.get("end_s", 0.0)) + _SUBJECT_MATTE_WINDOW_PAD_S
        if duration_s > 0:
            end_s = min(end_s, duration_s)
        if end_s > start_s:
            raw.append((start_s, end_s))
    if not raw:
        return []
    raw.sort()
    merged = [list(raw[0])]
    for start_s, end_s in raw[1:]:
        if start_s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end_s)
        else:
            merged.append([start_s, end_s])
    return [MatteWindow(start_s=s, end_s=e) for s, e in merged]


def _resolve_subject_matte_for_burn(
    *,
    video_path: str,
    overlays: list[dict],
    tmpdir: str,
    cached_matte_path: str | None,
    upload_key_base: str,
    duration_s: float,
    job_id: str,
    variant_id: str,
) -> tuple[Any, str | None, list[dict]]:
    """Best-effort matte resolution for a burn about to happen.

    Returns ``(provider_or_None, matte_gcs_path_or_None, overlays)``. When the
    flag is off, no overlay requests occlusion, or ANY step fails (download,
    compute, sanity check, upload, provider open), ``overlays`` comes back as a
    COPY with every `behind_subject` key stripped — the caller burns plain text
    instead of failing the render — and a `text_behind_subject_fallback` warning
    is logged with the reason. `matte_gcs_path` is `cached_matte_path` unchanged
    on failure (a bad recompute must never clobber a previously-good cache).

    Cache contract: `cached_matte_path` set → downloaded and opened, never
    recomputed (the "steady state" fast-reburn path). `None` → a fresh matte is
    computed over the union of `behind_subject` windows (padded, duration-
    clamped), sanity-gated, uploaded next to `upload_key_base`, and returned as
    the new `matte_gcs_path` for the caller to persist.
    """
    if not getattr(settings, "text_behind_subject_enabled", False):
        return None, cached_matte_path, overlays
    behind = [ov for ov in overlays if ov.get("behind_subject")]
    if not behind:
        return None, cached_matte_path, overlays

    from app.pipeline.subject_matte import (  # noqa: PLC0415
        SubjectMatteProvider,
        compute_subject_matte,
        matte_is_sane,
    )
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415

    provider = None
    matte_gcs_path = cached_matte_path
    try:
        if cached_matte_path:
            local_matte = os.path.join(tmpdir, "cached_subject_matte.mp4")
            download_to_file(cached_matte_path, local_matte)
            download_to_file(f"{cached_matte_path}.json", f"{local_matte}.json")
            provider = SubjectMatteProvider.open(local_matte)
            if provider is None:
                raise RuntimeError("cached matte failed to open")
        else:
            windows = _behind_subject_windows(behind, duration_s)
            if not windows:
                raise RuntimeError("no renderable behind_subject windows")
            local_matte = os.path.join(tmpdir, "computed_subject_matte.mp4")
            stats = compute_subject_matte(video_path, windows, local_matte)
            if stats is None or not matte_is_sane(stats):
                raise RuntimeError(f"matte compute failed or insane: {stats}")
            upload_key = f"{upload_key_base}.matte.mp4"
            upload_public_read(local_matte, upload_key)
            upload_public_read(f"{local_matte}.json", f"{upload_key}.json")
            provider = SubjectMatteProvider.open(local_matte)
            if provider is None:
                raise RuntimeError("freshly computed matte failed to open")
            matte_gcs_path = upload_key
    except Exception as exc:  # noqa: BLE001 — best-effort, never fails the burn
        log.warning(
            "text_behind_subject_fallback",
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
        )
        stripped = [{k: v for k, v in ov.items() if k != "behind_subject"} for ov in overlays]
        return None, cached_matte_path, stripped

    return provider, matte_gcs_path, overlays


def _ensure_visual_blocks_base(
    *,
    job_id: str,
    variant_id: str,
    variant: dict,
    base_gcs_path: str,
) -> tuple[str, str | None]:
    """Return the text-free picture base, composing persisted blocks once.

    The original ``base_video_path`` stays immutable.  A successful block
    composite is cached under ``visual_blocks_base_path`` and reused by later
    text/caption reburns.  With the flag off or no blocks, this is a literal
    no-op so existing renders remain byte-identical.
    """
    from app.agents._schemas.visual_block import coerce_visual_blocks  # noqa: PLC0415
    from app.config import settings as _visual_settings  # noqa: PLC0415

    if not _visual_settings.visual_blocks_enabled:
        return base_gcs_path, None
    blocks = coerce_visual_blocks(variant.get("visual_blocks") or [])
    if not blocks:
        return base_gcs_path, None
    cached = variant.get("visual_blocks_base_path")
    if cached and not variant.get("visual_blocks_cache_stale"):
        log.info(
            "visual_blocks_cache_hit",
            job_id=job_id,
            variant_id=variant_id,
            cache_path=str(cached),
        )
        return str(cached), str(cached)

    from app.pipeline.visual_blocks import apply_visual_blocks  # noqa: PLC0415

    cache_path = f"generative-jobs/{job_id}/visual-blocks/{variant_id}_{uuid.uuid4().hex[:10]}.mp4"
    log.info(
        "visual_blocks_cache_miss",
        job_id=job_id,
        variant_id=variant_id,
        block_count=len(blocks),
    )
    apply_visual_blocks(
        base_gcs_path=base_gcs_path,
        blocks=blocks,
        output_gcs_path=cache_path,
        job_id=job_id,
    )
    return cache_path, cache_path


def _free_retired_visual_blocks_base(previous: dict, replacement: str | None) -> None:
    """Delete an invalidated block cache only after its replacement wins."""
    old_path = previous.get("visual_blocks_base_path")
    if old_path and old_path != replacement:
        from app.storage import delete_object_best_effort  # noqa: PLC0415

        delete_object_best_effort(old_path)


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
    sequence_allowed: bool = True,
    language: str = "en",
    font_family_override: str | None = None,
    effect_override: str | None = None,
    text_color_override: str | None = None,
    cluster_hero_font_override: str | None = None,
    cluster_body_font_override: str | None = None,
    cluster_accent_font_override: str | None = None,
    cluster_hero_size_px_override: int | None = None,
    cluster_body_size_px_override: int | None = None,
    cluster_accent_size_px_override: int | None = None,
    intro_start_s_override: float | None = None,
    intro_end_s_override: float | None = None,
    text_behind_subject: bool | None = None,
) -> dict:
    """Fast reburn: download base → rebuild overlay → burn → upload.

    Returns a partial variant patch dict (same keys as _update_variant_entry expects).
    Raises on unrecoverable failure (base download error handled by caller via fallback).

    Sequence variants (`intro_mode == "sequence"`): when `sequence_allowed` (the
    edit is a pure font/size/style change), the sequence is rebuilt
    DETERMINISTICALLY from the persisted scenes — no transcription, no LLM; a size
    nudge re-scales `sequence_base_size_px` and rebuilds (D19). When the edit opts
    out (`sequence_allowed=False` — an explicit layout pick or text change), the
    variant renders the static intro from the persisted text and the persisted
    transcript/scenes are CLEARED (the variant is no longer synced; the route-side
    cluster word gate applies to it again from then on).

    `text_behind_subject`: explicit task kwarg for the text-behind-subject toggle
    (frozen contract with `regenerate_generative_variant`). None falls back to
    the persisted `existing["intro_behind_subject"]`. When resolved True and the
    flag is on, a cached `existing["subject_matte_path"]` is reused; absent, a
    fresh matte is computed on the downloaded base and cached for next time. Any
    matte failure strips `behind_subject` from every overlay about to burn and
    falls back to plain text — never fails the reburn.
    """
    import tempfile  # noqa: PLC0415

    from app.pipeline.generative_overlays import (  # noqa: PLC0415
        build_persistent_intro_overlays,
        build_sequence_overlays,
    )
    from app.pipeline.intro_cluster import EDITORIAL_STYLE  # noqa: PLC0415
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415

    base_gcs_path = existing["base_video_path"]
    render_base_gcs_path, visual_blocks_cache_path = _ensure_visual_blocks_base(
        job_id=job_id,
        variant_id=variant_id,
        variant=existing,
        base_gcs_path=base_gcs_path,
    )
    visual_blocks_patch = {
        "visual_blocks_base_path": visual_blocks_cache_path,
        "visual_blocks_cache_stale": False,
    }
    orientation = _resolve_variant_orientation(existing)
    canvas = canvas_for_orientation(orientation)

    with tempfile.TemporaryDirectory(prefix="nova_reburn_") as tmpdir:
        local_base = os.path.join(tmpdir, "base.mp4")

        # Download the cached base
        download_to_file(render_base_gcs_path, local_base)

        def _burn_text_for_variant(
            input_path: str,
            overlay_dicts: list[dict],
            output_path: str,
            *,
            matte=None,
        ) -> None:
            if is_collage_montage_preset(existing.get("montage_preset_rendered")):
                from app.pipeline.masonry_montage import (  # noqa: PLC0415
                    burn_masonry_text_overlays,
                    masonry_board_width_for_preset,
                )

                # Masonry's board-motion burn has no matte-occlusion support
                # (Lane B scoped it to the standard burn path only) — any
                # behind_subject key reaching here would just render as normal
                # text, so strip it defensively rather than ship a half-effect.
                overlay_dicts = [
                    {k: v for k, v in ov.items() if k != "behind_subject"} for ov in overlay_dicts
                ]
                try:
                    burn_duration_s = float(probe_video(input_path).duration_s)
                except Exception:  # noqa: BLE001
                    burn_duration_s = MAX_INTRO_S
                burn_masonry_text_overlays(
                    input_path,
                    overlay_dicts,
                    output_path,
                    tmpdir,
                    duration_s=burn_duration_s,
                    board_width=masonry_board_width_for_preset(
                        existing.get("montage_preset_rendered")
                    ),
                )
                return
            burn_text_overlays_skia(
                input_path,
                overlay_dicts,
                output_path,
                tmpdir,
                matte=matte,
                **_canvas_kwargs(canvas),
            )

        if text_mode == "lyrics":
            _lyrics_burn_dicts = _text_element_burn_dicts(existing)
            final_path = os.path.join(tmpdir, "final.mp4")
            _lyrics_matte_path = existing.get("subject_matte_path")
            if _lyrics_burn_dicts:
                try:
                    _lyrics_dur = float(probe_video(local_base).duration_s)
                except Exception:  # noqa: BLE001
                    _lyrics_dur = MAX_INTRO_S
                _lyrics_matte, _lyrics_matte_path, _lyrics_burn_dicts = (
                    _resolve_subject_matte_for_burn(
                        video_path=local_base,
                        overlays=_lyrics_burn_dicts,
                        tmpdir=tmpdir,
                        cached_matte_path=existing.get("subject_matte_path"),
                        upload_key_base=base_gcs_path,
                        duration_s=_lyrics_dur,
                        job_id=job_id,
                        variant_id=variant_id,
                    )
                )
                _burn_text_for_variant(
                    local_base, _lyrics_burn_dicts, final_path, matte=_lyrics_matte
                )
            else:
                shutil.copy2(local_base, final_path)
            variant_gcs_key = (existing.get("video_path") or "").lstrip("/")
            output_url = upload_public_read(final_path, variant_gcs_key)
            return {
                "render_status": "ready",
                "ok": True,
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
                "video_path": variant_gcs_key,
                "output_url": output_url,
                "text_mode": text_mode,
                "style_set_id": resolved_style_set_id,
                "orientation": orientation,
                "text_elements_user_edited": bool(existing.get("text_elements_user_edited")),
                "subject_matte_path": _lyrics_matte_path,
            }

        # ── TextElement early branch (T3 — plan-item-timeline) ──────────────
        # When the user has edited text via the TextElement API (T4),
        # their explicitly-authored elements own the overlay completely.
        # Skip the legacy size / style / intro-writer resolution path and
        # compile the elements directly to burn dicts.
        if _TEXT_ELEMENTS_ENABLED and existing.get("text_elements_user_edited"):
            if _should_compose_subtitled_final(existing):
                _fresh_existing = _fresh_variant_snapshot(job_id, variant_id) or existing
                _te_final_path = _compose_subtitled_final(local_base, _fresh_existing, tmpdir)
                # Subtitled text edits must not overwrite the current key. Signed URLs and
                # CDN layers may keep serving that object, so mint a new key and delete the old.
                _rank = int(_fresh_existing.get("rank") or existing.get("rank") or 1)
                _te_gcs_key = (
                    f"generative-jobs/{job_id}/variant_{_rank}_{variant_id}_text_"
                    f"{uuid.uuid4().hex[:8]}.mp4"
                )
                _te_output_url = upload_public_read(_te_final_path, _te_gcs_key)
                _old_video_path = _fresh_existing.get("video_path") or existing.get("video_path")
                return {
                    **visual_blocks_patch,
                    "render_status": "ready",
                    "ok": True,
                    "render_finished_at": datetime.utcnow().isoformat() + "Z",
                    "video_path": _te_gcs_key,
                    "output_url": _te_output_url,
                    "text_mode": text_mode,
                    "style_set_id": resolved_style_set_id,
                    "orientation": orientation,
                    "intro_text_size_px": existing.get("intro_text_size_px"),
                    "intro_size_source": existing.get("intro_size_source"),
                    "text_elements": _fresh_existing.get("text_elements") or [],
                    "text_elements_user_edited": True,
                    "_old_video_path_for_delete": (
                        _old_video_path
                        if _old_video_path and _old_video_path != _te_gcs_key
                        else None
                    ),
                }

            _te_burn_dicts = _text_element_burn_dicts(existing)
            _te_final_path = os.path.join(tmpdir, "final.mp4")
            try:
                _te_dur = float(probe_video(local_base).duration_s)
            except Exception:  # noqa: BLE001
                _te_dur = MAX_INTRO_S
            _te_provider, _te_matte_path, _te_burn_dicts = _resolve_subject_matte_for_burn(
                video_path=local_base,
                overlays=_te_burn_dicts,
                tmpdir=tmpdir,
                cached_matte_path=existing.get("subject_matte_path"),
                upload_key_base=base_gcs_path,
                duration_s=_te_dur,
                job_id=job_id,
                variant_id=variant_id,
            )
            _burn_text_for_variant(local_base, _te_burn_dicts, _te_final_path, matte=_te_provider)
            _te_gcs_key = (existing.get("video_path") or "").lstrip("/")
            _te_output_url = upload_public_read(_te_final_path, _te_gcs_key)
            return {
                **visual_blocks_patch,
                "render_status": "ready",
                "ok": True,
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
                "video_path": _te_gcs_key,
                "output_url": _te_output_url,
                "text_mode": text_mode,
                "style_set_id": resolved_style_set_id,
                "orientation": orientation,
                "intro_text_size_px": existing.get("intro_text_size_px"),
                "intro_size_source": existing.get("intro_size_source"),
                "text_elements_user_edited": True,
                "text_placement_candidates": existing.get("text_placement_candidates"),
                "subject_matte_path": _te_matte_path,
            }

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
        reburn_layout = existing.get("intro_layout")
        reburn_word_roles = existing.get("intro_word_roles")
        reburn_mode = existing.get("intro_mode") or reburn_layout  # legacy inference (D19)
        editorial_enabled = bool(getattr(settings, "editorial_sequence_enabled", True))
        was_sequence = existing.get("intro_mode") == "sequence"
        sequence_patch: dict = {}
        reburn_behind_subject = False
        reburn_matte_path = existing.get("subject_matte_path")

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
                language=language,
                font_family_override=font_family_override,
                effect_override=effect_override,
                text_color_override=text_color_override,
                placement_candidates=existing.get("text_placement_candidates") or None,
                # Precedence mirrors `layout`: task kwarg > persisted (present-key
                # check via .get, absent → None) > agent_form's behind_subject.
                behind_subject_override=(
                    text_behind_subject
                    if text_behind_subject is not None
                    else existing.get("intro_behind_subject")
                ),
                canvas=canvas,
            )
            # Preserve the original source label — size_override_px always wins
            # inside the resolver, which would label it "user"; restore the real
            # source so pixel-stability logic downstream remains correct.
            intro_source = final_size_source
            reburn_word_roles = params.get("word_roles")
            # Sticky (pre-gate) decision — must be popped before `params` is ever
            # spread into build_persistent_intro_overlays (not a builder kwarg).
            reburn_behind_subject = params.pop("_bs_pregate", False)

            overlays: list[dict] | None = None
            persisted_scenes = existing.get("scenes") or None
            if sequence_allowed and editorial_enabled and was_sequence and persisted_scenes:
                # Deterministic sequence rebuild (D15/D19): the persisted scenes
                # already carry word timings + roles — no transcribe, no LLM. A
                # size nudge re-scales the sequence base px and rebuilds.
                seq_px = int(
                    final_size_px or existing.get("sequence_base_size_px") or intro_px or 60
                )
                overlays = build_sequence_overlays(
                    persisted_scenes,
                    base_size_px=seq_px,
                    text_color=str(params.get("text_color") or "#FFFFFF"),
                    **_canvas_kwargs(canvas),
                )
                if overlays:
                    reburn_mode = "sequence"
                    reburn_layout = "cluster"
                    intro_px = seq_px
                    sequence_patch = {"sequence_base_size_px": seq_px}
                else:
                    record_pipeline_event(
                        "overlay",
                        "sequence_fallback",
                        {
                            "variant_id": variant_id,
                            "reason": "reburn_rebuild_failed",
                            "mode": existing.get("sequence_mode"),
                        },
                    )
                    log.warning(
                        "generative_sequence_reburn_rebuild_failed",
                        job_id=job_id,
                        variant_id=variant_id,
                    )
                    # Force the static-intro rebuild below (an empty list would
                    # skip it AND dodge copy-through detection — textless ship).
                    overlays = None
            if overlays is None:
                _has_cluster_overrides = any(
                    [
                        cluster_hero_font_override,
                        cluster_body_font_override,
                        cluster_accent_font_override,
                        cluster_hero_size_px_override,
                        cluster_body_size_px_override,
                        cluster_accent_size_px_override,
                    ]
                )
                if editorial_enabled and sequence_allowed and _has_cluster_overrides:
                    _reburn_cs: dict | None = dict(EDITORIAL_STYLE)
                    if cluster_hero_font_override:
                        _reburn_cs["hero_font"] = cluster_hero_font_override
                    if cluster_body_font_override:
                        _reburn_cs["body_font"] = cluster_body_font_override
                    if cluster_accent_font_override:
                        _reburn_cs["accent_font"] = cluster_accent_font_override
                    if cluster_hero_size_px_override:
                        _reburn_cs["hero_size_px_override"] = cluster_hero_size_px_override
                    if cluster_body_size_px_override:
                        _reburn_cs["connector_size_px_override"] = cluster_body_size_px_override
                    if cluster_accent_size_px_override:
                        _reburn_cs["closer_size_px_override"] = cluster_accent_size_px_override
                elif editorial_enabled and sequence_allowed:
                    _reburn_cs = EDITORIAL_STYLE  # type: ignore[assignment]
                else:
                    _reburn_cs = None
                overlays = build_persistent_intro_overlays(
                    reveal_window_s=reveal_window_s,
                    beats=[],  # even-split reveal; talking-head precedent
                    # Sequence-eligible fallback keeps PR #508's editorial restyle.
                    # Explicit opt-outs (layout/text edits) use the legacy static
                    # cluster path so Slice 3a's registry pairing owns the faces.
                    cluster_style=_reburn_cs,
                    start_s=intro_start_s_override,
                    end_s=intro_end_s_override,
                    **params,
                    **_canvas_kwargs(canvas),
                )
                # EFFECTIVE layout (cluster = 2 overlays per block; linear = one
                # pair). Legacy inference (D19) — sets intro_mode for this render.
                reburn_layout = "cluster" if len(overlays) > 2 else "linear"
                reburn_mode = reburn_layout
                if was_sequence and not sequence_allowed:
                    # Explicit opt-out (layout pick / text change): the variant is
                    # no longer synced — clear the sequence persistence (rhythm
                    # quote included) so the route-side cluster word gate applies
                    # to it again.
                    sequence_patch = {
                        "transcript": None,
                        "scenes": None,
                        "sequence_base_size_px": None,
                        "sequence_mode": None,
                        "sequence_quote": None,
                    }
            _matte_provider, reburn_matte_path, overlays = _resolve_subject_matte_for_burn(
                video_path=local_base,
                overlays=overlays,
                tmpdir=tmpdir,
                cached_matte_path=existing.get("subject_matte_path"),
                upload_key_base=base_gcs_path,
                duration_s=base_dur,
                job_id=job_id,
                variant_id=variant_id,
            )
            _burn_text_for_variant(local_base, overlays, final_path, matte=_matte_provider)

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
            reburn_mode = None
            if was_sequence:
                sequence_patch = {
                    "transcript": None,
                    "scenes": None,
                    "sequence_base_size_px": None,
                    "sequence_mode": None,
                    "sequence_quote": None,
                }

        # Upload final to same variant key
        variant_gcs_key = (existing.get("video_path") or "").lstrip("/")
        output_url = upload_public_read(final_path, variant_gcs_key)

        return {
            **visual_blocks_patch,
            "intro_text": agent_text.text if agent_text else None,
            "intro_highlight_word": (
                getattr(agent_text, "highlight_word", None) if agent_text else None
            ),
            "intro_layout": (reburn_layout if agent_text else None),
            "intro_word_roles": (reburn_word_roles if agent_text else None),
            "intro_mode": (reburn_mode if agent_text else None),
            "intro_text_size_px": intro_px if agent_text else existing_size_px,
            "intro_size_source": intro_source if agent_text else existing_size_source,
            # Sticky (pre-gate) decision + cached matte key. remove_text/none mode
            # carries the persisted matte forward unchanged (removing text doesn't
            # invalidate a still-valid cache; behind_subject just resets to False).
            "intro_behind_subject": (reburn_behind_subject if agent_text else False),
            "subject_matte_path": reburn_matte_path,
            "style_set_id": resolved_style_set_id,
            "orientation": orientation,
            "text_mode": text_mode,
            "render_status": "ready",
            "ok": True,
            "render_finished_at": datetime.utcnow().isoformat() + "Z",
            "video_path": variant_gcs_key,
            "output_url": output_url,
            # User-pinned independent overrides — persist across re-renders.
            # _reburn_text_on_base receives the RESOLVED sticky values (caller
            # already merged explicit request > existing pin), so writing them
            # back here keeps the pin alive for the next re-render.
            "intro_font_family": font_family_override,
            "intro_effect": effect_override,
            "intro_text_color": text_color_override,
            "text_placement_candidates": existing.get("text_placement_candidates"),
            "intro_cluster_hero_font": cluster_hero_font_override,
            "intro_cluster_body_font": cluster_body_font_override,
            "intro_cluster_accent_font": cluster_accent_font_override,
            "intro_cluster_hero_size_px": cluster_hero_size_px_override,
            "intro_cluster_body_size_px": cluster_body_size_px_override,
            "intro_cluster_accent_size_px": cluster_accent_size_px_override,
            # base_video_path unchanged (still valid for next edit);
            # transcript/scenes only appear here when they must change (merge
            # semantics: absent keys keep the persisted values).
            **sequence_patch,
        }


# ── Clip timeline editor (durable sources + ai_timeline + override render) ──────

# Beat-span match tolerance for `duration_beats` derivation: a slot duration is
# only labeled with a whole-beat count when some consecutive beat-grid span
# matches it within this window. Wider would mislabel footage-trimmed slots;
# tighter would miss beat-snapped slots after float rounding.
_BEAT_SPAN_TOLERANCE_S = 0.05

# Variants whose layout is a beat-driven montage — the only ones the timeline
# override path may re-assemble. song_lyrics is included for the internal
# preserve-cuts flow; public lyric clip editing remains route-gated. Voiceover
# and talking_head layouts follow a spine, not a slot grid.
_TIMELINE_OVERRIDE_VARIANTS = frozenset({"song_text", "song_lyrics", "original_text"})


def _derive_duration_beats(durations: list[float], beat_grid: list[float]) -> list[int | None]:
    """Per-slot whole-beat counts: for each slot duration, the consecutive
    beat-grid span (walked left-to-right with a cursor) whose length matches
    within `_BEAT_SPAN_TOLERANCE_S`. None when no span matches — e.g. a slot
    trimmed to real footage length rather than snapped to the grid. An off-grid
    slot re-anchors the cursor at the beat nearest its end so it doesn't
    misalign every later span."""
    if len(beat_grid) < 2:
        return [None for _ in durations]
    out: list[int | None] = []
    cursor = 0
    for duration in durations:
        best_n: int | None = None
        best_err = float("inf")
        for n in range(1, len(beat_grid) - cursor):
            err = abs((beat_grid[cursor + n] - beat_grid[cursor]) - duration)
            if err < best_err:
                best_err, best_n = err, n
        if best_n is not None and best_err <= _BEAT_SPAN_TOLERANCE_S:
            out.append(best_n)
            cursor += best_n
        else:
            out.append(None)
            anchor = beat_grid[min(cursor, len(beat_grid) - 1)] + duration
            cursor = min(range(len(beat_grid)), key=lambda k: abs(beat_grid[k] - anchor))
    return out


def _build_ai_timeline(
    *,
    steps: list,
    resolved_plans: list[dict],
    clip_id_to_gcs: dict[str, str],
    clip_id_to_local: dict[str, str],
    probe_map: dict,
    beat_grid: list[float],
) -> dict[str, Any] | None:
    """Build the persisted `ai_timeline` blob from a completed montage assembly.

    `clip_index` — THE stable slot identity — is each step's source position in
    `all_candidates["clip_paths"]` (matcher clip_ids are Gemini-ref-derived and
    unstable across re-renders). `clip_id_to_gcs` preserves clip_paths order 1:1
    (index enumeration in both the ingest and timeline-override paths), so
    position-in-values == clip_index. Windows come from the POST-resolution
    plans `_assemble_clips` actually rendered (`resolved_plans_out` sink), not
    the matcher's requested moments. `beat_grid` is the section-relative beat
    list the assembly snapped against (`generate_music_recipe` already shifts
    window beats by best_start_s); empty for no-music variants.

    Best-effort: returns None when the assembly can't be mapped — a timeline is
    a UX nicety, never a render blocker.
    """
    try:
        if len(resolved_plans) != len(steps):
            return None
        gcs_to_index = {gcs: i for i, gcs in enumerate(clip_id_to_gcs.values())}
        durations = [float(p.get("duration_s") or 0.0) for p in resolved_plans]
        beats_per_slot = _derive_duration_beats(durations, [float(b) for b in beat_grid or []])
        slots: list[dict[str, Any]] = []
        for order, (step, plan) in enumerate(zip(steps, resolved_plans)):
            gcs = clip_id_to_gcs.get(step.clip_id)
            if gcs is None or gcs not in gcs_to_index:
                return None
            probe = probe_map.get(clip_id_to_local.get(step.clip_id))
            moment = getattr(step, "moment", None) or {}
            slots.append(
                {
                    "slot_id": uuid.uuid4().hex,
                    "clip_index": gcs_to_index[gcs],
                    "source_gcs_path": gcs,
                    "source_duration_s": round(float(getattr(probe, "duration_s", 0.0) or 0.0), 3),
                    "in_s": round(float(plan["start_s"]), 3),
                    "duration_s": round(float(plan["duration_s"]), 3),
                    "duration_beats": beats_per_slot[order],
                    "order": order,
                    "moment_energy": moment.get("energy"),
                    "moment_description": moment.get("description"),
                }
            )
        return {
            "beat_grid": [round(float(b), 3) for b in beat_grid or []],
            "slots": slots,
        }
    except Exception as exc:  # noqa: BLE001 — never block a render on timeline mapping
        log.warning("generative_ai_timeline_build_failed", error=str(exc))
        return None


def _prepare_timeline_assembly(
    timeline_slots: list[dict],
    clip_paths_gcs: list[str],
    tmpdir: str,
    *,
    job_id: str,
) -> dict[str, Any] | None:
    """Resolve user-timeline slots into ready-to-assemble exact-window steps.

    Download + probe ONLY: no Gemini upload, no clip_metadata, no clip_cache,
    no narrative order — the user's slots ARE the plan. `clip_id_to_gcs` is
    rebuilt over the FULL clip_paths list (clip_{i} → path, index enumeration)
    so `_build_ai_timeline`'s position-in-values == clip_index invariant holds;
    only the clips the timeline actually uses are downloaded.

    Returns None when the timeline is corrupt/unresolvable (bad clip_index,
    missing/invalid fields, nothing left after removals) so the caller falls
    back to a fresh match.
    """
    from app.tasks.template_orchestrate import (  # noqa: PLC0415
        _download_clips_parallel,
        _probe_clips,
    )

    resolved: list[tuple[int, float, float, Any, Any]] = []
    for slot in timeline_slots:
        try:
            clip_index = int(slot["clip_index"])
            in_s = float(slot["in_s"])
            duration_s = float(slot["duration_s"])
        except (KeyError, TypeError, ValueError):
            log.warning("generative_timeline_corrupt_slot", job_id=job_id, slot=slot)
            return None
        if not (0 <= clip_index < len(clip_paths_gcs)) or duration_s <= 0 or in_s < 0:
            log.warning(
                "generative_timeline_unresolvable_slot",
                job_id=job_id,
                clip_index=clip_index,
                in_s=in_s,
                duration_s=duration_s,
            )
            return None
        resolved.append(
            (
                clip_index,
                in_s,
                duration_s,
                slot.get("moment_energy"),
                slot.get("moment_description"),
            )
        )
    if not resolved:
        return None

    used_indices = sorted({r[0] for r in resolved})
    local_paths = _download_clips_parallel([clip_paths_gcs[i] for i in used_indices], tmpdir)
    probe_map = _probe_clips(local_paths)
    clip_id_to_local = {f"clip_{i}": path for i, path in zip(used_indices, local_paths)}
    clip_id_to_gcs = {f"clip_{i}": gcs for i, gcs in enumerate(clip_paths_gcs)}

    # Clamp every window against the REAL probed duration — the route skips
    # bounds checks on clips the AI never probed (its comment promises "the
    # worker's probe will clamp"; this is that clamp). Slots that collapse
    # below 0.1s after clamping are dropped with a warning.
    clamped: list[tuple[int, float, float, Any, Any]] = []
    for clip_index, in_s, duration_s, moment_energy, moment_description in resolved:
        probe = probe_map.get(clip_id_to_local[f"clip_{clip_index}"])
        probe_duration = float(getattr(probe, "duration_s", 0.0) or 0.0)
        if probe_duration > 0:
            in_s = min(in_s, max(0.0, probe_duration - 0.1))
            end = min(in_s + duration_s, probe_duration)
            duration_s = end - in_s
        if duration_s < 0.1:
            log.warning(
                "generative_timeline_slot_clamped_away",
                job_id=job_id,
                clip_index=clip_index,
                in_s=in_s,
                probe_duration_s=probe_duration,
            )
            continue
        clamped.append((clip_index, in_s, duration_s, moment_energy, moment_description))
    if not clamped:
        return None

    from app.pipeline.agents.gemini_analyzer import AssemblyStep  # noqa: PLC0415

    steps = [
        AssemblyStep(
            slot={
                "position": i + 1,
                "slot_type": "broll",
                "target_duration_s": duration_s,
                # exact_window: _plan_slots renders this verbatim source range —
                # no beat-snap, no cursor sharing, no speed ramp.
                "exact_window": True,
            },
            clip_id=f"clip_{clip_index}",
            moment={
                "start_s": in_s,
                "end_s": in_s + duration_s,
                "energy": moment_energy or 5.0,
                "description": moment_description or "",
            },
        )
        for i, (clip_index, in_s, duration_s, moment_energy, moment_description) in enumerate(
            clamped
        )
    ]
    return {
        "steps": steps,
        "clip_id_to_local": clip_id_to_local,
        "clip_id_to_gcs": clip_id_to_gcs,
        "probe_map": probe_map,
    }


def _project_recipe_overlays_to_steps(recipe_dict: dict, steps: list) -> None:
    """Project recipe-relative overlays onto exact user-timeline steps.

    Preserve-cuts bypasses the matcher and uses prebuilt AssemblySteps, so the
    recipe's slot dictionaries are otherwise never consulted by assembly. Copy
    every injected overlay through absolute video time and clip it back into
    each exact step; this keeps lyric burns aligned without exposing lyric
    variants to the general clip editor.
    """
    overlays: list[tuple[float, float, dict]] = []
    cursor = 0.0
    for slot in recipe_dict.get("slots") or []:
        try:
            duration_s = max(0.0, float(slot.get("target_duration_s") or 0.0))
        except (AttributeError, TypeError, ValueError):
            continue
        for overlay in slot.get("text_overlays") or []:
            if not isinstance(overlay, dict):
                continue
            try:
                start_s = cursor + float(overlay.get("start_s") or 0.0)
                end_s = cursor + float(overlay.get("end_s") or 0.0)
            except (TypeError, ValueError):
                continue
            if end_s > start_s:
                overlays.append((start_s, end_s, overlay))
        cursor += duration_s

    step_cursor = 0.0
    for step in steps:
        slot = getattr(step, "slot", None)
        if not isinstance(slot, dict):
            continue
        try:
            duration_s = max(0.0, float(slot.get("target_duration_s") or 0.0))
        except (TypeError, ValueError):
            duration_s = 0.0
        step_end = step_cursor + duration_s
        projected: list[dict] = []
        for overlay_start, overlay_end, overlay in overlays:
            clipped_start = max(step_cursor, overlay_start)
            clipped_end = min(step_end, overlay_end)
            if clipped_end <= clipped_start:
                continue
            copied = copy.deepcopy(overlay)
            copied["start_s"] = round(clipped_start - step_cursor, 6)
            copied["end_s"] = round(clipped_end - step_cursor, 6)
            projected.append(copied)
        slot["text_overlays"] = projected
        step_cursor = step_end


def _run_regenerate_variant(
    job_id: str,
    variant_id: str,
    new_track_id: str | None,
    override_text: str | None,
    remove_text: bool,
    style_set_id: str | None = None,
    size_override_px: int | None = None,
    mix_override: float | None = None,
    layout_override: str | None = None,
    timeline_override: list[dict] | None = None,
    font_family_override: str | None = None,
    effect_override: str | None = None,
    text_color_override: str | None = None,
    cluster_hero_font_override: str | None = None,
    cluster_body_font_override: str | None = None,
    cluster_accent_font_override: str | None = None,
    cluster_hero_size_px_override: int | None = None,
    cluster_body_size_px_override: int | None = None,
    cluster_accent_size_px_override: int | None = None,
    media_overlays_override: list[dict] | None = None,
    sfx_override: list[dict] | None = None,
    render_gen_id: str | None = None,
    intro_start_s_override: float | None = None,
    intro_end_s_override: float | None = None,
    text_behind_subject: bool | None = None,
    orientation_override: str | None = None,
    force_full_render: bool = False,
) -> None:
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    # ── Media-overlay fast path ─────────────────────────────────────────────────
    # When the only change is adding/removing media-overlay cards (no song/text/
    # style/timeline change), take this lightweight path:
    #   1. Download the clean base (pre_media_overlay or current video_path).
    #   2. Composite cards on top via apply_media_overlays.
    #   3. Upload, patch the variant entry, done.
    # No clip ingest, no Gemini, no music — just one ffmpeg encode.
    # Guarded by the kill switch (settings.media_overlays_enabled).
    _is_overlay_only = (
        media_overlays_override is not None
        and sfx_override is None
        and new_track_id is None
        and override_text is None
        and not remove_text
        and style_set_id is None
        and size_override_px is None
        and mix_override is None
        and layout_override is None
        and timeline_override is None
        and font_family_override is None
        and effect_override is None
        and text_color_override is None
        and cluster_hero_font_override is None
        and cluster_body_font_override is None
        and cluster_accent_font_override is None
        and cluster_hero_size_px_override is None
        and cluster_body_size_px_override is None
        and cluster_accent_size_px_override is None
        and text_behind_subject is None
        and orientation_override is None
    )
    if _is_overlay_only and settings.media_overlays_enabled:
        _run_media_overlay_pass(
            job_id=job_id,
            variant_id=variant_id,
            overlays_raw=media_overlays_override or [],
            expected_render_gen_id=render_gen_id,
        )
        return

    # ── Sound-effects fast path ─────────────────────────────────────────────────
    # When the only change is adding/removing sound-effect placements (no song/text/
    # style/overlay/timeline change), take this lightweight audio-only path:
    #   1. Download the base (pre_sfx or current video_path).
    #   2. Mix effects via apply_sound_effects (adelay+amix, -c:v copy).
    #   3. Upload, patch the variant entry, done.
    # Guarded by the kill switch (settings.sound_effects_enabled).
    from app.config import settings as _settings_sfx  # noqa: PLC0415

    _is_sfx_only = (
        sfx_override is not None
        and media_overlays_override is None
        and new_track_id is None
        and override_text is None
        and not remove_text
        and style_set_id is None
        and size_override_px is None
        and mix_override is None
        and layout_override is None
        and timeline_override is None
        and font_family_override is None
        and effect_override is None
        and text_color_override is None
        and cluster_hero_font_override is None
        and cluster_body_font_override is None
        and cluster_accent_font_override is None
        and cluster_hero_size_px_override is None
        and cluster_body_size_px_override is None
        and cluster_accent_size_px_override is None
        and text_behind_subject is None
        and orientation_override is None
    )
    if _is_sfx_only and _settings_sfx.sound_effects_enabled:
        _run_sfx_pass(
            job_id=job_id,
            variant_id=variant_id,
            sfx_raw=sfx_override or [],
            expected_render_gen_id=render_gen_id,
        )
        return

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
        # Re-renders inherit the filming guide too (Creator Agent M3 / B2), so a
        # retext/swap_song hook still reflects the intended shots.
        filming_guide_regen: list[dict] = list(
            (job.all_candidates or {}).get("filming_guide") or []
        )
        # Re-renders inherit the creator clip notes too (WS5), so a retext/swap_song
        # hook still has the per-clip context the creator provided at job creation.
        clip_notes_regen: dict = dict((job.all_candidates or {}).get("clip_notes") or {})
        # Re-renders inherit the narrative clip order too — otherwise the first
        # song swap would silently reshuffle a guide-ordered edit back to random.
        narrative_shot_count_regen: int = int(
            (job.all_candidates or {}).get("narrative_shot_count") or 0
        )
        # Voiceover jobs re-render the same voice bed; the mix slider is the only knob
        # that changes here. Both come from the Job row (single source of truth).
        voiceover_gcs_path: str | None = (job.all_candidates or {}).get(
            "voiceover_gcs_path"
        ) or None
        # Re-renders inherit the landscape-fit preference too — otherwise the
        # toggle would silently revert to crop on the first song-swap / retext.
        landscape_fit_regen: str = (job.all_candidates or {}).get("landscape_fit") or "fill"
        # Re-renders inherit the montage visual preset too — otherwise a
        # song-swap/retext would silently snap a masonry item back to classic.
        montage_preset_regen = coerce_montage_preset(
            (job.all_candidates or {}).get("montage_preset")
        )
        variants = ((job.assembly_plan or {}).get("variants")) or []
        existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
        if existing is None:
            log.error("generative_regenerate_variant_unknown", job_id=job_id, variant_id=variant_id)
            return
        effective_orientation = _resolve_variant_orientation(existing, orientation_override)
        _is_subtitled_text_reburn = (
            existing.get("resolved_archetype") == "subtitled"
            and (
                getattr(settings, "subtitled_text_lane_enabled", False)
                or (
                    getattr(settings, "visual_blocks_enabled", False)
                    and bool(existing.get("visual_blocks"))
                )
            )
            and _TEXT_ELEMENTS_ENABLED
            and existing.get("text_elements_user_edited")
            and new_track_id is None
            and mix_override is None
            and timeline_override is None
            and media_overlays_override is None
            and sfx_override is None
            and override_text is None
            and not remove_text
            and style_set_id is None
            and size_override_px is None
            and layout_override is None
            and font_family_override is None
            and effect_override is None
            and text_color_override is None
            and cluster_hero_font_override is None
            and cluster_body_font_override is None
            and cluster_accent_font_override is None
            and cluster_hero_size_px_override is None
            and cluster_body_size_px_override is None
            and cluster_accent_size_px_override is None
            and intro_start_s_override is None
            and intro_end_s_override is None
        )
        if existing.get("resolved_archetype") in _CAPTION_REBURN_ARCHETYPES and not (
            _is_subtitled_text_reburn
        ):
            # Defense-in-depth (mirrors the reburn guard): the generic re-render funnels
            # into the MONTAGE path — running it on a narrated/subtitled variant would
            # overwrite resolved_archetype/video_path and orphan the captions the user
            # may have hand-edited. Caption variants only change via their own tasks
            # (reburn / re-transcribe); reject anything else loudly.
            log.error(
                "generative_regenerate_rejected_caption_variant",
                job_id=job_id,
                variant_id=variant_id,
                archetype=existing.get("resolved_archetype"),
            )
            # E1: terminal write ("ready") — token-checked like every other outcome.
            _update_variant_entry(
                job_id,
                variant_id,
                {"render_status": "ready"},
                expected_render_gen_id=render_gen_id,
                outcome="caption_reject",
            )
            return
        rank = int(existing.get("rank", 1))
        existing_track_id = existing.get("music_track_id")
        existing_music_start_s = existing.get("music_start_s")
        existing_music_window_duration_s = existing.get("music_window_video_duration_s")
        music_window_alignment = None
        if existing_music_window_duration_s is not None:
            music_window_alignment = (
                "preserve_cuts"
                if (existing.get("user_timeline") or {}).get("slots")
                else "resync_beats"
            )
        existing_text_mode = existing.get("text_mode", "agent_text")
        inherited_lyrics_enabled = existing.get("lyrics_enabled")
        inherited_lyric_line_overrides = (
            None if new_track_id is not None else existing.get("lyric_line_overrides")
        )
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
        # Cluster persistence — keeps a cluster intro a cluster on no-LLM re-renders
        # (font/size/style/swap-song). Absent on legacy variants → linear/None.
        # A user layout pick (the post-render layout option) overrides what's
        # persisted; the persisted text is kept. When switching INTO cluster on a
        # variant that never had word roles, the engine derives them heuristically.
        persisted_layout: str | None = existing.get("intro_layout") or None
        if layout_override in ("linear", "cluster"):
            persisted_layout = layout_override
        persisted_word_roles: list[str] | None = existing.get("intro_word_roles") or None
        # Rhythm-mode quote (sequence_mode == "rhythm"). A re-assembling
        # re-render re-times the SAME quote on the new duration — deterministic
        # synthesis, zero LLM calls. Nulled below on opt-out edits.
        persisted_sequence_quote: str | None = existing.get("sequence_quote") or None
        # User-pinned scene timing overrides (PR-D). Applied inside the sequence
        # overlay functions so the render honors the user's drag edits.
        persisted_scene_timing_overrides: list[dict] | None = (
            existing.get("scene_timing_overrides") or None
        )
        # Style precedence: explicit restyle request → the variant's persisted set →
        # any sibling variant's set (the job-level default) → "default".
        existing_style_set_id = existing.get("style_set_id")
        if existing_style_set_id is None:
            existing_style_set_id = next(
                (v.get("style_set_id") for v in variants if v.get("style_set_id")), None
            )
    resolved_style_set_id = style_set_id or existing_style_set_id or "default"

    # Style override precedence: explicit request > previously pinned > nothing.
    # These persist across later swap-song/retext re-renders (same lifecycle as
    # size_override_px / "user" source).
    existing_font_override: str | None = existing.get("intro_font_family") or None
    existing_effect_override: str | None = existing.get("intro_effect") or None
    existing_color_override: str | None = existing.get("intro_text_color") or None
    resolved_font_override = font_family_override or existing_font_override
    resolved_effect_override = effect_override or existing_effect_override
    resolved_color_override = text_color_override or existing_color_override
    # Cluster per-role font overrides — sticky across re-renders (same lifecycle as
    # intro_font_family: explicit request wins; otherwise carry the persisted pin).
    existing_cluster_hero_override: str | None = existing.get("intro_cluster_hero_font") or None
    existing_cluster_body_override: str | None = existing.get("intro_cluster_body_font") or None
    existing_cluster_accent_override: str | None = existing.get("intro_cluster_accent_font") or None
    resolved_cluster_hero_override = cluster_hero_font_override or existing_cluster_hero_override
    resolved_cluster_body_override = cluster_body_font_override or existing_cluster_body_override
    resolved_cluster_accent_override = (
        cluster_accent_font_override or existing_cluster_accent_override
    )
    existing_cluster_hero_size_override: int | None = (
        existing.get("intro_cluster_hero_size_px") or None
    )
    existing_cluster_body_size_override: int | None = (
        existing.get("intro_cluster_body_size_px") or None
    )
    existing_cluster_accent_size_override: int | None = (
        existing.get("intro_cluster_accent_size_px") or None
    )
    resolved_cluster_hero_size_override = (
        cluster_hero_size_px_override or existing_cluster_hero_size_override
    )
    resolved_cluster_body_size_override = (
        cluster_body_size_px_override or existing_cluster_body_size_override
    )
    resolved_cluster_accent_size_override = (
        cluster_accent_size_px_override or existing_cluster_accent_size_override
    )

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

    audio_only_song_swap = (
        _is_collage_audio_only_swap_eligible(existing, new_track_id)
        and not force_full_render
        and music_window_alignment is None
        and override_text is None
        and not remove_text
        and style_set_id is None
        and size_override_px is None
        and mix_override is None
        and layout_override is None
        and timeline_override is None
        and font_family_override is None
        and effect_override is None
        and text_color_override is None
        and cluster_hero_font_override is None
        and cluster_body_font_override is None
        and cluster_accent_font_override is None
        and cluster_hero_size_px_override is None
        and cluster_body_size_px_override is None
        and cluster_accent_size_px_override is None
        and media_overlays_override is None
        and sfx_override is None
        and intro_start_s_override is None
        and intro_end_s_override is None
        and text_behind_subject is None
        and orientation_override is None
    )
    if audio_only_song_swap and new_track_id is not None:
        with _sync_session() as db:
            track = db.get(MusicTrack, new_track_id)
        if track is None or track.analysis_status != "ready" or not track.audio_gcs_path:
            raise ValueError(f"Track {new_track_id} is not available for audio-only swap")
        if not _update_variant_entry(
            job_id,
            variant_id,
            {"render_status": "rendering", "ok": False, "error": None},
            expected_render_gen_id=render_gen_id,
            outcome="masonry_audio_swap_start",
        ):
            return
        try:
            completed = _run_masonry_audio_only_song_swap(
                job_id=job_id,
                variant_id=variant_id,
                existing=existing,
                track=track,
                expected_render_gen_id=render_gen_id,
            )
            if completed:
                return
            return
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "masonry_audio_only_swap_fallback_full_render",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc),
                exc_info=True,
            )

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths to re-render from")

    # Sequence opt-out (D19): an explicit layout pick or text change on a synced
    # variant means "stop syncing — render the static intro from intro_text".
    # Everything else (size nudge, style set, swap-song, mix, timeline edits)
    # keeps the sequence auto-pick: fast-reburns rebuild deterministically from
    # the persisted scenes; re-assembling renders re-transcribe (D15).
    allow_sequence = layout_override is None and override_text is None and not remove_text
    if not allow_sequence:
        # Opt-out edits stop syncing: don't carry the rhythm quote forward —
        # the merge clears it alongside transcript/scenes (D19 opt-out).
        persisted_sequence_quote = None

    # ── Timeline resolution (clip timeline editor) ────────────────────────────
    # Precedence: explicit timeline_override kwarg → the variant's persisted
    # user_timeline → None (fresh match, today's behavior). Slots marked
    # `removed` are dropped; the rest render in list order.
    #
    # Swap-song exception: a new track means a new beat grid, so the user's cut
    # no longer lines up with the music. Clear the persisted user_timeline and
    # force a fresh match — this is the contract the frontend ConfirmDialog
    # states ("your clip edits will be reset") and docs/pipelines/generative.md
    # documents.
    if new_track_id is not None and music_window_alignment != "preserve_cuts":
        timeline_override = None
        _clear_user_timeline(job_id, variant_id)
    active_timeline_slots: list[dict] | None = None
    if settings.GENERATIVE_TIMELINE_EDITOR_ENABLED and (
        new_track_id is None or music_window_alignment == "preserve_cuts"
    ):
        raw_timeline_slots = timeline_override
        if raw_timeline_slots is None:
            raw_timeline_slots = (existing.get("user_timeline") or {}).get("slots") or None
        if raw_timeline_slots:
            kept_slots = [s for s in raw_timeline_slots if not s.get("removed")]
            record_pipeline_event(
                "assembly",
                "timeline_edit_received",
                {
                    "slot_count": len(raw_timeline_slots),
                    "removed_count": len(raw_timeline_slots) - len(kept_slots),
                    "has_override": timeline_override is not None,
                },
            )
            # Everything removed → nothing to assemble → fresh match below.
            active_timeline_slots = kept_slots or None

    # Mark this variant as re-rendering so the UI can show a spinner immediately.
    _update_variant_entry(
        job_id, variant_id, {"render_status": "rendering", "ok": False, "error": None}
    )

    # ── Fast-reburn path ──────────────────────────────────────────────────────
    # When the edit is a pure text/style/size change (no audio change, base cached),
    # skip the full clip ingest + re-assemble. Download the base → reburn text → done.
    # An EXPLICIT timeline_override always forces a re-assembly — the cached base
    # was rendered from the previous slot layout. (A merely-persisted user_timeline
    # is fine: the base was re-cached by the override render that persisted it.)
    if (
        timeline_override is None
        and not force_full_render
        and _is_fast_reburn_eligible(
            existing,
            new_track_id,
            mix_override,
            settings,
            orientation_override=orientation_override,
        )
    ):
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
            persisted_layout=persisted_layout,
            persisted_word_roles=persisted_word_roles,
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
                language=language,
                settings=settings,
                sequence_allowed=allow_sequence,
                font_family_override=resolved_font_override,
                effect_override=resolved_effect_override,
                text_color_override=resolved_color_override,
                cluster_hero_font_override=resolved_cluster_hero_override,
                cluster_body_font_override=resolved_cluster_body_override,
                cluster_accent_font_override=resolved_cluster_accent_override,
                cluster_hero_size_px_override=resolved_cluster_hero_size_override,
                cluster_body_size_px_override=resolved_cluster_body_size_override,
                cluster_accent_size_px_override=resolved_cluster_accent_size_override,
                intro_start_s_override=intro_start_s_override,
                intro_end_s_override=intro_end_s_override,
                text_behind_subject=text_behind_subject,
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
            _old_video_path_for_delete = result.pop("_old_video_path_for_delete", None)
            # #626: decide the lane reapply from the FRESH persisted state (the
            # burn took wall-clock time; a render=False lane autosave may have
            # landed since `existing` was read). When a lane exists, defer the
            # terminal "ready" to the reapply chain (OV-7, mirrors the caption
            # terminals) — without the deferral a poll observes an effect-less
            # "ready" between burn and reapply and can dispatch an edit that
            # races the hook (prod job 4bee92f8: sfx_applying with no
            # media_overlay_applying; cards persisted but absent from the video).
            fast_will_reapply = _will_reapply_media_layers(
                _fresh_variant_snapshot(job_id, variant_id) or existing
            )
            if fast_will_reapply:
                result["render_status"] = "rendering"
                result.pop("render_finished_at", None)
            # A20/E1: stale-write guard for reburns.  A subsequent editor commit
            # (or PUT /text-elements) overwrites `render_generation_id` in the DB;
            # if it differs from the token we were launched with, our result is
            # stale and the newer task will write its own — discard, don't clobber.
            if not _update_variant_entry(
                job_id,
                variant_id,
                result,
                expected_render_gen_id=render_gen_id,
                outcome="reburn",
            ):
                return
            _free_retired_visual_blocks_base(existing, result.get("visual_blocks_base_path"))
            if _old_video_path_for_delete:
                from app.storage import delete_object_best_effort  # noqa: PLC0415

                delete_object_best_effort(_old_video_path_for_delete)
            # A text/style reburn overwrites video_path from the cached text-free
            # base, so any persisted user media layers must be rebuilt. The chain
            # re-reads persisted state under a row lock before each pass.
            reapply_owned = _reapply_user_media_layers(
                job_id=job_id,
                variant_id=variant_id,
                expected_render_gen_id=render_gen_id,
            )
            if fast_will_reapply and not reapply_owned:
                # R1-3: the terminal write above deferred status (OV-7) but the
                # chain no-oped (e.g. lanes cleared mid-run via the render=False
                # autosave) — finalize so the variant never strands in
                # "rendering". Token-gated like every terminal write.
                _update_variant_entry(
                    job_id,
                    variant_id,
                    {
                        "render_status": "ready",
                        "render_finished_at": datetime.utcnow().isoformat() + "Z",
                    },
                    expected_render_gen_id=render_gen_id,
                    outcome="reburn_reapply_noop",
                )
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
        # ── Timeline-override assembly (clip timeline editor) ─────────────────
        # Montage variants with an active timeline skip the ENTIRE
        # ingest+Gemini+match leg: the user's slots ARE the plan (download +
        # probe only). Corrupt/unresolvable timelines fall back to fresh match.
        timeline_assembly: dict[str, Any] | None = None
        if active_timeline_slots is not None and variant_id in _TIMELINE_OVERRIDE_VARIANTS:
            timeline_assembly = _prepare_timeline_assembly(
                active_timeline_slots, clip_paths_gcs, tmpdir, job_id=job_id
            )
            if timeline_assembly is None:
                log.warning(
                    "generative_timeline_fallback_fresh_match",
                    job_id=job_id,
                    variant_id=variant_id,
                )

        assembly_steps_override: list | None = None
        if timeline_assembly is not None:
            assembly_steps_override = timeline_assembly["steps"]
            record_pipeline_event(
                "assembly",
                "timeline_override_path",
                {"variant_id": variant_id, "slot_count": len(assembly_steps_override)},
            )
            render_clip_metas: list = []  # no Gemini analysis on this path
            render_clip_id_to_local = timeline_assembly["clip_id_to_local"]
            render_clip_id_to_gcs = timeline_assembly["clip_id_to_gcs"]
            render_probe_map = timeline_assembly["probe_map"]
            available_footage_s = _available_footage_s(render_probe_map)
            narrative_order_regen = None
            # No clip analysis on this path → a fresh rhythm quote can't be
            # grounded; rhythm only re-times the persisted quote (if any).
            regen_author_quote_fn = None
            # Persisted intro_text is reused verbatim — the timeline path never
            # runs intro_writer (no clip_metas to ground a fresh hook in).
            agent_text, agent_form, text_mode = _resolve_regen_text(
                override_text=override_text,
                remove_text=remove_text,
                existing_text_mode=existing_text_mode,
                persisted_text=persisted_text,
                persisted_highlight=persisted_highlight,
                run_text_agents_fn=lambda: (None, None),
                persisted_layout=persisted_layout,
                persisted_word_roles=persisted_word_roles,
                persisted_behind_subject=existing.get("intro_behind_subject"),
            )
        else:
            # PERF/TODO: this re-runs the full clip ingest (re-download + re-Gemini
            # clip_metadata) on every swap/retext, even remove_text. Acceptable for v1
            # (async re-render, reject-if-rendering guard caps spam), but the Gemini
            # analysis is cacheable — persist clip_metas after the first orchestrate run
            # and skip re-analysis here (only re-download + re-probe are truly needed for
            # the render). Follow-up once the feature is real-render-verified.
            ingest = _ingest_clips(clip_paths_gcs, tmpdir, job_id=job_id)
            render_clip_metas = ingest["clip_metas"]
            render_clip_id_to_local = ingest["clip_id_to_local"]
            render_clip_id_to_gcs = ingest["clip_id_to_gcs"]
            render_probe_map = ingest["probe_map"]
            available_footage_s = _available_footage_s(render_probe_map)

            # Narrative order survives re-renders (same dispatch contract as the
            # first render). Hook text grounds in the clip that opens the edit.
            narrative_order_regen = _resolve_narrative_order(
                narrative_shot_count_regen, ingest["clip_id_to_gcs"], job_id=job_id
            )
            regen_hero = ingest["hero"]
            if active_timeline_slots:
                # Hook regrounding: a timeline-edited job opens with the
                # timeline's slot-0 clip — ground the hook there, not in the
                # guide's first shot. (This branch runs for timeline-active
                # variants that still take the full leg, e.g. song_lyrics.)
                slot0_gcs: str | None = None
                try:
                    slot0_index = int(active_timeline_slots[0]["clip_index"])
                    if 0 <= slot0_index < len(clip_paths_gcs):
                        slot0_gcs = clip_paths_gcs[slot0_index]
                except (KeyError, TypeError, ValueError):
                    slot0_gcs = None
                if slot0_gcs is not None:
                    slot0_clip_id = next(
                        (cid for cid, g in ingest["clip_id_to_gcs"].items() if g == slot0_gcs),
                        None,
                    )
                    regen_hero = next(
                        (m for m in ingest["clip_metas"] if m.clip_id == slot0_clip_id),
                        regen_hero,
                    )
            elif narrative_order_regen:
                regen_hero = next(
                    (m for m in ingest["clip_metas"] if m.clip_id == narrative_order_regen[0]),
                    regen_hero,
                )

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
                    regen_hero,
                    job_id=job_id,
                    language=language,
                    persona=persona,
                    filming_guide=filming_guide_regen,
                    clip_notes=clip_notes_regen,
                ),
                persisted_layout=persisted_layout,
                persisted_word_roles=persisted_word_roles,
                persisted_behind_subject=existing.get("intro_behind_subject"),
            )

            # Rhythm-mode quote authoring on a full re-render (same grounding
            # as the first render; only reached when no quote is persisted).
            def regen_author_quote_fn(video_duration_s: float) -> str | None:
                return _author_sequence_quote(
                    regen_hero,
                    job_id=job_id,
                    video_duration_s=video_duration_s,
                    language=language,
                    persona=persona,
                    filming_guide=filming_guide_regen,
                )

        spec: dict[str, Any] = {
            "variant_id": variant_id,
            "rank": rank,
            "text_mode": text_mode,
            "track": track,
            "music_start_s": existing_music_start_s,
            "music_window_video_duration_s": existing_music_window_duration_s,
            "storage_generation": render_gen_id,
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
            clip_metas=render_clip_metas,
            clip_id_to_local=render_clip_id_to_local,
            clip_id_to_gcs=render_clip_id_to_gcs,
            probe_map=render_probe_map,
            available_footage_s=available_footage_s,
            agent_text=agent_text,
            agent_form=agent_form,
            variant_dir=variant_dir,
            style_set_id=resolved_style_set_id,
            intro_size_override_px=resolved_size_override_px,
            user_style_knobs=existing_user_style_knobs,
            narrative_order=narrative_order_regen,
            filming_guide=(filming_guide_regen if narrative_shot_count_regen > 0 else None),
            assembly_steps_override=assembly_steps_override,
            allow_sequence=allow_sequence,
            author_quote_fn=regen_author_quote_fn,
            existing_sequence_quote=persisted_sequence_quote,
            scene_timing_overrides=persisted_scene_timing_overrides,
            language=language,
            font_family_override=resolved_font_override,
            effect_override=resolved_effect_override,
            text_color_override=resolved_color_override,
            cluster_hero_font_override=resolved_cluster_hero_override,
            cluster_body_font_override=resolved_cluster_body_override,
            cluster_accent_font_override=resolved_cluster_accent_override,
            cluster_hero_size_px_override=resolved_cluster_hero_size_override,
            cluster_body_size_px_override=resolved_cluster_body_size_override,
            cluster_accent_size_px_override=resolved_cluster_accent_size_override,
            landscape_fit=landscape_fit_regen,
            montage_preset=montage_preset_regen,
            behind_subject_override=text_behind_subject,
            lyrics_enabled=inherited_lyrics_enabled,
            lyric_line_overrides=inherited_lyric_line_overrides,
            orientation=effective_orientation,
        )

    # E1: the token check covers BOTH terminal branches (ready and failed) —
    # a superseded task's output/status must never clobber the newer commit's.
    if result.get("ok"):
        latest_variant = _fresh_variant_snapshot(job_id, variant_id) or existing
        has_visual_blocks = settings.visual_blocks_enabled and bool(
            latest_variant.get("visual_blocks")
        )
        if settings.visual_blocks_enabled and not has_visual_blocks:
            # Removing every block must publish the newly assembled clean base
            # and retire any previous block composite rather than preserving it
            # through the variant merge.
            result["visual_blocks_base_path"] = None
            result["visual_blocks_cache_stale"] = False
        if (
            (_TEXT_ELEMENTS_ENABLED and latest_variant.get("text_elements_user_edited"))
            or has_visual_blocks
        ) and result.get("base_video_path"):
            result = {
                **result,
                **_reburn_text_on_base(
                    job_id=job_id,
                    variant_id=variant_id,
                    existing={
                        **latest_variant,
                        **result,
                        "text_elements": latest_variant.get("text_elements") or [],
                        "text_elements_user_edited": bool(
                            latest_variant.get("text_elements_user_edited")
                        ),
                        # A full assembly minted a new clean base; never reuse a
                        # block composite whose audio/picture came from the old one.
                        "visual_blocks_base_path": None,
                    },
                    agent_text=agent_text,
                    agent_form=agent_form,
                    text_mode=text_mode,
                    resolved_style_set_id=resolved_style_set_id,
                    size_override_px=resolved_size_override_px,
                    settings=settings,
                    sequence_allowed=allow_sequence,
                    language=language,
                    font_family_override=resolved_font_override,
                    effect_override=resolved_effect_override,
                    text_color_override=resolved_color_override,
                    cluster_hero_font_override=resolved_cluster_hero_override,
                    cluster_body_font_override=resolved_cluster_body_override,
                    cluster_accent_font_override=resolved_cluster_accent_override,
                    cluster_hero_size_px_override=resolved_cluster_hero_size_override,
                    cluster_body_size_px_override=resolved_cluster_body_size_override,
                    cluster_accent_size_px_override=resolved_cluster_accent_size_override,
                ),
            }
        retired_snapshot_keys: list[str] = []
        # #626: `existing` predates the minutes-long re-render — re-read so a
        # lane edit persisted mid-render (render=False autosave) is neither
        # dropped by the merge below (the base result dict carries an explicit
        # media_overlays=None) nor resurrected from the stale task-start list.
        persisted_media_overlays = (_fresh_variant_snapshot(job_id, variant_id) or existing).get(
            "media_overlays"
        ) or None
        if persisted_media_overlays:
            # A fresh full render produces the clean base without user media cards.
            # Preserve the just-persisted cards through the result merge and STAGE
            # the snapshot nulls (R1-2: no deletes yet — a superseded write below
            # must never delete the winning render's snapshot blobs).
            result["media_overlays"] = persisted_media_overlays
            retired_snapshot_keys = _stage_media_snapshot_nulls(result, existing)
        # Regenerate hygiene (A1): this full re-render runs the MONTAGE path,
        # which never runs the silence-cut stage, so `result` carries no
        # summary key — without an explicit None the entry merge would keep
        # the previous render's blob and the admin cut-plan strip would
        # describe cuts that don't exist in the new video. (Caption archetypes
        # are rejected above, so no fresh subtitled summary is clobbered.)
        result["silence_cut"] = None
        if not _update_variant_entry(
            job_id,
            variant_id,
            result,
            expected_render_gen_id=render_gen_id,
            outcome="full_render",
        ):
            _discard_generation_storage(result, job_id=job_id, generation=render_gen_id)
            return
        # Write accepted — the retired snapshot blobs are unreachable; free them
        # (D16-C) before the reapply pass mints fresh ones.
        _free_media_snapshot_keys(retired_snapshot_keys)
        # Text-behind-subject: a full re-render's matte upload key is deterministic
        # (base_video_path + ".matte.mp4", same as `base_gcs` itself), so a fresh
        # recompute overwrites the SAME blob — no orphan there. The orphan case is
        # the OTHER direction: the previous render had a matte and this one didn't
        # recompute one (behind_subject/flag now off, or no overlay needs it), so
        # the old blob at that deterministic key is unreferenced from here on.
        old_matte_path = existing.get("subject_matte_path")
        new_matte_path = result.get("subject_matte_path")
        if old_matte_path and old_matte_path != new_matte_path:
            from app.storage import delete_object_best_effort  # noqa: PLC0415

            delete_object_best_effort(old_matte_path)
            delete_object_best_effort(f"{old_matte_path}.json")
        _free_retired_visual_blocks_base(existing, result.get("visual_blocks_base_path"))
        # A full re-render re-assembles video_path without user media layers.
        _reapply_user_media_layers(
            job_id=job_id,
            variant_id=variant_id,
            expected_render_gen_id=render_gen_id,
        )
    else:
        # Failure-patch hygiene: the failure record spreads the fresh `base`
        # dict, whose None values (base_video_path, intro_text, ...) would NULL
        # the persisted fields through _update_variant_entry's merge — and
        # video_path/output_url must never be overwritten on failure, so the
        # last good render survives a failed edit.
        failure_patch = {
            k: v
            for k, v in result.items()
            if v is not None and k not in ("video_path", "output_url")
        }
        _update_variant_entry(
            job_id,
            variant_id,
            failure_patch,
            expected_render_gen_id=render_gen_id,
            outcome="full_render_failed",
        )


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


def _clear_user_timeline(job_id: str, variant_id: str) -> None:
    """Remove the persisted `user_timeline` key from one variant entry (row-locked).

    `_update_variant_entry` can only merge keys, never drop them — swap-song
    needs a true removal so the variant reads as "no user edits" afterwards
    (mirrors `persist_user_timeline(..., None)` on the route side).
    """
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        plan = dict(job.assembly_plan or {})
        variants = list(plan.get("variants") or [])
        for i, v in enumerate(variants):
            if v.get("variant_id") == variant_id and "user_timeline" in v:
                updated = dict(v)
                updated.pop("user_timeline", None)
                variants[i] = updated
                break
        plan["variants"] = variants
        job.assembly_plan = plan
        db.commit()


def _stale_render_discarded(
    job_id: str, variant_id: str, render_gen_id: str | None, *, outcome: str
) -> bool:
    """Render-intent guard (E1): True → this task's terminal DB write must be skipped.

    A render task launched with a `render_gen_id` token owns the variant's
    DB-visible outcome only while the variant's persisted `render_generation_id`
    still equals that token. Every editor commit bumps the token BEFORE
    enqueueing its own render, so an older task that finishes late must finish
    its compute but DISCARD its patch — the newest committed state is the one
    whose render lands (D8 queue/supersede). Tasks launched without a token
    (legacy per-field dispatchers) always write — flag-off surfaces unchanged.
    """
    if render_gen_id is None:
        return False
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return False
        variants = (job.assembly_plan or {}).get("variants") or []
        variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if variant is None:
        return False
    current = variant.get("render_generation_id")
    if current is not None and current != render_gen_id:
        log.warning(
            "stale_render_write_discarded",
            job_id=job_id,
            variant_id=variant_id,
            outcome=outcome,
            expected_gen_id=render_gen_id,
            actual_gen_id=current,
        )
        return True
    return False


def _update_variant_entry(
    job_id: str,
    variant_id: str,
    patch: dict[str, Any],
    *,
    expected_render_gen_id: str | None = None,
    outcome: str | None = None,
) -> bool:
    """Merge `patch` into the matching entry of Job.assembly_plan['variants'].

    Row-locked (SELECT ... FOR UPDATE): concurrent `regenerate_generative_variant`
    tasks each do a read-modify-write of the whole `assembly_plan` JSONB, so without
    the lock one task's variant update silently clobbers another's (worker runs
    --concurrency=4). The lock serializes the RMW per Job row.

    When `expected_render_gen_id` is provided, the stale-generation comparison is
    performed under the same row lock as the merge. This makes terminal render
    writes check-and-write atomic; legacy tokenless tasks pass None and always
    write as before. Returns False when the patch was discarded or the row was
    missing.
    """
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return False
        plan = dict(job.assembly_plan or {})
        variants = list(plan.get("variants") or [])
        for i, v in enumerate(variants):
            if v.get("variant_id") == variant_id:
                current = v.get("render_generation_id")
                if (
                    expected_render_gen_id is not None
                    and current is not None
                    and current != expected_render_gen_id
                ):
                    log.warning(
                        "stale_render_write_discarded",
                        job_id=job_id,
                        variant_id=variant_id,
                        outcome=outcome or "variant_update",
                        expected_gen_id=expected_render_gen_id,
                        actual_gen_id=current,
                    )
                    return False
                variants[i] = {**v, **{k: val for k, val in patch.items() if k != "variant_id"}}
                break
        else:
            return False
        plan["variants"] = variants
        job.assembly_plan = plan
        db.commit()
    return True


# ── Variant spec ──────────────────────────────────────────────────────────────


def _variant_specs(best_track: MusicTrack | None) -> list[dict[str, Any]]:
    """The variants to render. Song variants only when a track matched; the lyrics
    variant only when that track actually has cached lyrics (otherwise it would render
    identically to song_text with no lyrics — a wasted render + a confusing "Lyrics"
    card) AND the lyric language is renderable by the bundled Latin-script fonts
    (a CJK track would tofu-render or break word alignment — skip cleanly instead
    of shipping one broken card). Always emit the original-audio variant."""
    from app.pipeline.lyric_support import (  # noqa: PLC0415
        lyric_language,
        lyrics_variant_renderable,
    )

    specs: list[dict[str, Any]] = []
    if best_track is not None:
        if best_track.lyrics_cached:
            if lyrics_variant_renderable(best_track.lyrics_cached):
                specs.append(
                    {"variant_id": "song_lyrics", "text_mode": "lyrics", "track": best_track}
                )
            else:
                log.info(
                    "generative_lyrics_variant_skipped_language",
                    track_id=str(getattr(best_track, "id", "")),
                    lyric_language=lyric_language(best_track.lyrics_cached),
                )
        specs.append({"variant_id": "song_text", "text_mode": "agent_text", "track": best_track})
    specs.append({"variant_id": "original_text", "text_mode": "agent_text", "track": None})
    return specs


def _content_plan_primary_montage_spec(best_track: MusicTrack | None) -> dict[str, Any]:
    return _variant_specs(best_track)[0]


# ── Archetype dispatch (Lane D) ─────────────────────────────────────────────────


def _resolve_archetype(
    edit_format: str,
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    *,
    job_id: str,
    voiceover_gcs_path: str | None = None,
    filming_guide: list[dict] | None = None,
    footage_type_bias: list[str] | None = None,
    clip_durations_s: dict[str, float] | None = None,
) -> tuple[str, str | None, str | None]:
    """Resolve the declared edit_format against footage → (archetype, spine, fallback_reason).

    Default-safe: returns `("montage", None, reason)` for every case except a talking_head
    edit that is enabled AND backed by footage with usable speech, and the narrated
    self-narration branch (no voiceover + flag on) which can select subtitled or
    talking_head from the footage's own speech. Emits an
    `archetype_fallback` / `archetype_selected` trace event so the admin job-debug view
    explains why a declared format did or didn't take. The returned `spine_clip_id` is
    fed straight to `assemble_talking_head`, whose override path then only re-scores
    that one clip.

    `footage_type_bias` (Creator Agent M3 / B3): the user's declared preference for
    footage type (from UserStyle.footage_type_bias). Used as a SOFT TIE-BREAKER only —
    it NEVER overrides the voiceover fast path, the speech-coverage gate, or the
    edit_format_talking_head_enabled kill switch. When the declared edit_format is
    "montage" AND the bias contains "talking_head" AND the flag is on AND a clip carries
    sufficient speech, the archetype is promoted to talking_head. When bias is absent or
    empty, behavior is byte-identical to pre-B3. Any failure in the bias path falls back
    silently to montage (best-effort).

    Reasons for montage fallback: `archetype_not_implemented` (day_vlog/single_hero —
    no assembler yet), `flag_disabled` (kill switch off), `no_speech` (no clip clears
    `_MIN_SPINE_COVERAGE`), `spine_too_short` (self-narration picked a multi-clip
    talking_head spine too short to show B-roll), `archetype_bias_no_speech`
    (bias suggested talking_head but no speech found).

    The third tuple element is the montage-fallback reason (None when a non-montage
    archetype was selected or montage was the declared format). The orchestrator
    persists it on `assembly_plan["archetype_fallback"]` so the plan-item page can
    explain a style downgrade to the user — the trace event alone is admin-only.
    """
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    def _fallback(reason: str) -> tuple[str, None, str]:
        record_pipeline_event(
            "assembly", "archetype_fallback", {"declared": edit_format, "reason": reason}
        )
        log.info(
            "generative_archetype_fallback", job_id=job_id, declared=edit_format, reason=reason
        )
        return "montage", None, reason

    # Any narrated format with a voiceover renders the narrated archetype. A written
    # script (the filming guide) drives force-alignment; without a usable one
    # (narrated_ready, or a "Narrated walkthrough" item whose guide is empty) the
    # renderer auto-segments the narration across the clips. Either way the voiceover
    # spines the edit and the words become captions — so an empty guide must NOT drop
    # a narrated item to the voiceover-montage path (which loses captions).
    if edit_format in NARRATED_EDIT_FORMATS and voiceover_gcs_path:
        if settings.narrated_archetype_enabled:
            record_pipeline_event("assembly", "archetype_selected", {"archetype": "narrated"})
            log.info("generative_archetype_selected", job_id=job_id, archetype="narrated")
            return "narrated", None, None
        record_pipeline_event(
            "assembly",
            "archetype_fallback",
            {"declared": edit_format, "reason": "flag_disabled"},
        )
        log.info(
            "generative_archetype_fallback",
            job_id=job_id,
            declared=edit_format,
            reason="flag_disabled",
        )

    # Self-narration: a narrated format with NO recorded voiceover can still be
    # narration-driven when the footage itself carries the voice (the user filmed a
    # walkthrough narrating over it). One clip with speech → subtitled (own audio +
    # editable captions, the purpose-built renderer for "the clip's audio IS the
    # narration"); several clips → talking_head (highest-speech clip spines the edit,
    # the rest cut in as B-roll). No audible speech anywhere → montage fallback with
    # the reason persisted for the item-page banner. NARRATED_SELF_NARRATION_ENABLED
    # is the SOLE gate here — the declared-format kill switches
    # (subtitled_archetype_enabled / edit_format_talking_head_enabled) gate the style
    # picker, not this resolution outcome (see config.py).
    if (
        edit_format in NARRATED_EDIT_FORMATS
        and not voiceover_gcs_path
        and settings.narrated_self_narration_enabled
    ):
        spine_id, coverage = _pick_speech_spine(clip_metas, clip_id_to_local, job_id=job_id)
        if spine_id is None or coverage < _MIN_SPINE_COVERAGE:
            return _fallback("no_speech")
        selected = "subtitled" if len(clip_id_to_local) == 1 else "talking_head"
        spine_duration_s = (clip_durations_s or {}).get(spine_id)
        if (
            selected == "talking_head"
            and spine_duration_s is not None
            and spine_duration_s <= _MIN_TALKING_HEAD_SPINE_WITH_BROLL_S
        ):
            return _fallback("spine_too_short")
        record_pipeline_event(
            "assembly",
            "archetype_selected",
            {
                "archetype": selected,
                "via": "narrated_self_narration",
                "spine_clip_id": spine_id,
                "speech_coverage": round(coverage, 3),
            },
        )
        log.info(
            "generative_archetype_selected",
            job_id=job_id,
            archetype=selected,
            via="narrated_self_narration",
            spine_clip_id=spine_id,
            speech_coverage=round(coverage, 3),
        )
        if selected == "subtitled":
            return "subtitled", None, None
        return "talking_head", spine_id, None

    # A user-supplied voiceover wins over any footage-derived archetype: the voice is
    # the spine. Resolved BEFORE the speech-coverage logic because it's driven by an
    # uploaded asset, not by what the footage happens to contain.
    # MUST NOT be overridden by footage_type_bias (bias is a soft signal only).
    if voiceover_gcs_path:
        record_pipeline_event("assembly", "archetype_selected", {"archetype": "voiceover"})
        log.info("generative_archetype_selected", job_id=job_id, archetype="voiceover")
        return "voiceover", None, None

    # Subtitled single-clip talking-head: no voiceover, no spine selection — the whole
    # clip keeps its own audio and is captioned. Gated by the kill switch; a
    # no-speech clip still resolves to `subtitled` (the caption layer shows the empty
    # state) rather than silently dropping to montage. Flag OFF ⇒ montage fallback (the
    # frontend picker also hides the card when the flag is off, so this is only reached
    # via a stale/forced token).
    if edit_format == "subtitled":
        if not settings.subtitled_archetype_enabled:
            return _fallback("flag_disabled")
        record_pipeline_event("assembly", "archetype_selected", {"archetype": "subtitled"})
        log.info("generative_archetype_selected", job_id=job_id, archetype="subtitled")
        return "subtitled", None, None

    if edit_format == "montage":
        # B3 soft bias: when the user's style says "talking_head", attempt the
        # talking_head path only when the flag is on AND speech actually exists.
        # Hard signals already handled above (voiceover) or below (explicit format).
        # This is a tie-breaker only — it NEVER runs if the flag is off.
        bias = list(footage_type_bias or [])
        if bias:
            record_pipeline_event(
                "assembly",
                "archetype_bias",
                {
                    "edit_format": edit_format,
                    "footage_type_bias": bias,
                    "bias_active": True,
                },
            )
        if bias and "talking_head" in bias and settings.edit_format_talking_head_enabled:
            # Attempt the bias: check speech coverage.
            try:
                best_id_bias, best_cov_bias = _pick_speech_spine(
                    clip_metas, clip_id_to_local, job_id=job_id
                )

                if best_id_bias is not None and best_cov_bias >= _MIN_SPINE_COVERAGE:
                    record_pipeline_event(
                        "assembly",
                        "archetype_selected",
                        {
                            "archetype": "talking_head",
                            "via": "footage_type_bias",
                            "spine_clip_id": best_id_bias,
                            "speech_coverage": round(best_cov_bias, 3),
                        },
                    )
                    log.info(
                        "generative_archetype_selected",
                        job_id=job_id,
                        archetype="talking_head",
                        via="footage_type_bias",
                        spine_clip_id=best_id_bias,
                        speech_coverage=round(best_cov_bias, 3),
                    )
                    return "talking_head", best_id_bias, None
                else:
                    # Bias suggested talking_head but no speech — fall through to montage.
                    record_pipeline_event(
                        "assembly",
                        "archetype_fallback",
                        {
                            "declared": edit_format,
                            "reason": "archetype_bias_no_speech",
                            "bias": bias,
                        },
                    )
                    log.info(
                        "generative_archetype_fallback",
                        job_id=job_id,
                        declared=edit_format,
                        reason="archetype_bias_no_speech",
                        bias=bias,
                    )
            except Exception as exc:  # noqa: BLE001 — bias is best-effort; never fail the job
                log.warning(
                    "generative_archetype_bias_failed",
                    job_id=job_id,
                    error=str(exc),
                )
        return "montage", None, None

    if edit_format != "talking_head":
        # day_vlog / single_hero declared but no assembler exists yet.
        return _fallback("archetype_not_implemented")
    if not settings.edit_format_talking_head_enabled:
        return _fallback("flag_disabled")

    # Pick the highest-speech clip; reject the format if none carries real speech.
    best_id, best_cov = _pick_speech_spine(clip_metas, clip_id_to_local, job_id=job_id)

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
    return "talking_head", best_id, None


def _pick_speech_spine(
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    *,
    job_id: str | None = None,
) -> tuple[str | None, float]:
    """Scan every clip's speech coverage → (best clip_id, best coverage).

    The single spine-selection loop shared by the footage-type-bias branch, the
    declared-talking_head branch, and the narrated self-narration branch of
    `_resolve_archetype` — one source of truth for how "which clip carries the
    voice" is decided. A probe failure logs and scores that clip 0 rather than
    failing resolution (best-effort). Returns (None, -1.0) when no clip has a
    local path. Callers compare the coverage against `_MIN_SPINE_COVERAGE`.
    """
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
    return best_id, best_cov


def _specs_for_archetype(
    archetype: str,
    best_track: MusicTrack | None,
    *,
    voiceover_gcs_path: str | None = None,
    voiceover_bed_level: float | None = None,
    voiceover_caption_style: str | None = None,
    variant_policy: str | None = None,
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
        if variant_policy == CONTENT_PLAN_PRIMARY_VARIANT_POLICY and best_track is not None:
            return [
                {
                    "variant_id": "voiceover_music",
                    "text_mode": "agent_text",
                    "track": best_track,
                    "archetype": "voiceover",
                    "voiceover_gcs_path": voiceover_gcs_path,
                    "mix": _VOICEOVER_MUSIC_DEFAULT_MIX,
                }
            ]
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
    if archetype == "narrated":
        return [
            {
                "variant_id": "narrated",
                "text_mode": "none",
                "track": None,
                "archetype": "narrated",
                "voiceover_gcs_path": voiceover_gcs_path,
                "mix": 1.0,
                "voiceover_bed_level": voiceover_bed_level,
                "voiceover_caption_style": voiceover_caption_style,
            }
        ]
    if archetype == "talking_head":
        return [
            {
                "variant_id": "talking_head",
                "text_mode": "agent_text",
                "track": None,
                "archetype": "talking_head",
            }
        ]
    if archetype == "subtitled":
        # ONE variant: the clip's own audio (no track, no voiceover) + editable
        # captions transcribed from that audio. text_mode="none" — captions are burned
        # via the caption path, not the agent-intro overlay. caption_style follows the
        # item toggle: "word" → word-by-word lime pop, anything else → sentence blocks
        # (the safe default). Reuses the narrated voiceover_caption_style key.
        return [
            {
                "variant_id": "subtitled",
                "text_mode": "none",
                "track": None,
                "archetype": "subtitled",
                "caption_style": "word" if voiceover_caption_style == "word" else "sentence",
            }
        ]
    if variant_policy == CONTENT_PLAN_PRIMARY_VARIANT_POLICY:
        return [_content_plan_primary_montage_spec(best_track)]
    return _variant_specs(best_track)


# ── Agents (best-effort) ────────────────────────────────────────────────────────


def _run_text_agents(
    clip_metas: list,
    hero,
    *,
    job_id: str,
    language: str = "en",
    persona: dict | None = None,
    filming_guide: list[dict] | None = None,
    clip_notes: dict | None = None,
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

    `filming_guide` is the optional per-item shot list from the content plan (Creator
    Agent M3 / B2). When present, it provides the hook writer with DATA context about
    the intended shots so the hook can reflect the shooting intent. Empty for public
    jobs → byte-identical to pre-M3 behavior.

    `clip_notes` is the optional per-clip creator notes (WS5 / dogfood feedback #3):
    gcs_path → note_text. Only populated on plan-item jobs where the creator typed a
    note. Empty for public jobs → byte-identical baseline.

    Best-effort: any failure yields (None, {}) so the text variants render footage
    without an intro rather than failing the job.
    """
    persona = persona or {}
    filming_guide = filming_guide or []
    clip_notes = clip_notes or {}
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RefusalError, RunContext, TerminalError  # noqa: PLC0415
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

        writer_input = IntroWriterInput(
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
            # Filming guide (Creator Agent M3 / B2). Shot-list context for the
            # hook writer — DATA only, never a command. Empty → byte-identical.
            filming_guide=filming_guide,
            # Creator clip notes (WS5). Per-clip context the creator typed before
            # submitting. DATA only, re-sanitized in intro_writer. Empty → byte-identical.
            clip_notes=clip_notes,
            form=form.model_dump(),
            exemplars=exemplars,
            language=language,
        )
        try:
            text = IntroTextWriterAgent(client).run(writer_input, ctx=ctx)
        except (RefusalError, TerminalError) as exc:
            if isinstance(exc, TerminalError) and not isinstance(exc.__cause__, RefusalError):
                raise
            text = _fallback_intro_text(hero_summary)
            form_dict = form.model_dump()
            form_dict.update({"effect": "fade-in", "layout": "linear"})
            log.warning(
                "generative_intro_writer_refusal_fallback",
                job_id=job_id,
                error=str(exc),
                fallback_text=text.text,
            )
            return text, form_dict
        return text, form.model_dump()
    except Exception as exc:
        log.warning("generative_text_agents_failed", job_id=job_id, error=str(exc))
        return None, {}


def _author_sequence_quote(
    hero,
    *,
    job_id: str,
    video_duration_s: float,
    language: str = "en",
    persona: dict | None = None,
    filming_guide: list[dict] | None = None,
) -> str | None:
    """Run SequenceQuoteWriterAgent for a rhythm-mode sequence. Returns the
    sanitized quote, or None on ANY failure.

    Grounding mirrors `_run_text_agents`' IntroWriterInput (same hero summary,
    persona, language, filming guide — minus the intro-only form/exemplars)
    plus the rendered duration, which drives the target sentence count. NO
    heuristic fallback BY DESIGN: a failed/terminal agent run means the caller
    falls back to the static styled cluster — never a made-up quote.
    """
    persona = persona or {}
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.sequence_quote_writer import (  # noqa: PLC0415
            SequenceQuoteInput,
            SequenceQuoteWriterAgent,
        )

        out = SequenceQuoteWriterAgent(default_client()).run(
            SequenceQuoteInput(
                hero_clip=_meta_to_summary(hero),
                hero_transcript=str(getattr(hero, "transcript", "") or ""),
                tone=str(persona.get("tone", "") or ""),
                language=language,
                content_pillars=list(persona.get("content_pillars", []) or []),
                theme=str(persona.get("theme", "") or ""),
                idea=str(persona.get("idea", "") or ""),
                preference_summary=str(persona.get("preference_summary", "") or ""),
                tiktok_analysis=str(persona.get("tiktok_summary", "") or ""),
                filming_guide=filming_guide or [],
                video_duration_s=max(float(video_duration_s), 0.1),
            ),
            ctx=RunContext(job_id=job_id),
        )
        return out.quote
    except Exception as exc:  # noqa: BLE001 — quote authoring can never fail a render
        log.warning("generative_sequence_quote_failed", job_id=job_id, error=str(exc))
        return None


def _fallback_intro_text(hero_summary):
    import types as _types  # noqa: PLC0415

    subject = str(getattr(hero_summary, "subject", "") or "").strip()
    description = str(getattr(hero_summary, "description", "") or "").strip()
    base = subject or description or "the moment"
    words = [w.strip(".,!?;:\"'()[]{}").lower() for w in base.split() if w.strip(".,!?;:\"'()[]{}")]
    core = " ".join(words[:3]) or "the moment"
    return _types.SimpleNamespace(
        text=f"watch {core} unfold",
        highlight_word=None,
        word_roles=None,
    )


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


def _effective_music_window(
    track: MusicTrack,
    *,
    requested_start_s: float | None,
    requested_duration_s: float | None,
    fallback_footage_s: float,
) -> dict[str, Any]:
    """Resolve one song window for recipe, lyrics, preview persistence, and mix."""
    from app.services.music_sections import track_config_with_rank_one  # noqa: PLC0415

    cfg = _fit_section_to_footage(track_config_with_rank_one(track), fallback_footage_s)
    if requested_start_s is None or requested_duration_s is None:
        start_s = float(cfg.get("best_start_s", 0.0) or 0.0)
        end_s = float(cfg.get("best_end_s", start_s) or start_s)
        return {
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": max(0.0, end_s - start_s),
            "track_config": cfg,
            "validated": False,
        }

    track_duration_s = float(track.duration_s or 0.0)
    duration_s = float(requested_duration_s)
    if track_duration_s <= 0 or duration_s <= 0 or track_duration_s + 0.02 < duration_s:
        raise ValueError("Selected song window is no longer available")
    max_start = max(0.0, track_duration_s - duration_s)
    requested = max(0.0, min(float(requested_start_s), max_start))
    beats: list[float] = []
    for raw_beat in track.beat_timestamps_s or []:
        try:
            beat = float(raw_beat)
        except (TypeError, ValueError):
            continue
        if math.isfinite(beat) and beat >= 0:
            beats.append(beat)
    beats = sorted(set(beats))
    candidates = [beat for beat in beats if beat <= max_start]
    if not candidates:
        raise ValueError("Selected song has no usable beat timing")
    start_s = min(candidates, key=lambda beat: (abs(beat - requested), beat))
    end_s = start_s + duration_s
    cfg.update(
        {
            "best_start_s": round(start_s, 3),
            "best_end_s": round(end_s, 3),
            "exact_window": True,
        }
    )
    return {
        "start_s": round(start_s, 3),
        "end_s": round(end_s, 3),
        "duration_s": round(duration_s, 3),
        "track_config": cfg,
        "validated": True,
    }


# ── Transcript-synced typographic sequence (editorial auto-upgrade, D6/D16) ─────

# Hard wall-clock guard on the in-task Whisper call (D18 "API-or-static"): the
# render task runs at soft_time_limit=1740 and the sequence is an enhancement —
# it must never eat the render budget. The OpenAI client has its own timeouts,
# but a wedged connection must not stall the burn step, so the call runs on a
# helper thread and is abandoned past this deadline (the thread's ffmpeg audio
# extract holds its own 300s timeout, so the orphan is bounded).
_SEQUENCE_TRANSCRIBE_TIMEOUT_S = 90.0


def _sequence_gate(
    *,
    layout: str | None,
    track: MusicTrack | None,
    voiceover_gcs_path: str | None,
) -> tuple[bool, str]:
    """Sequence eligibility predicate (D16), evaluated BEFORE transcription.

    The sequence syncs on-screen text to the words in `assembled_path`'s ORIGINAL
    montage audio (that is what D11 transcribes), so it is only eligible when that
    audio is what the viewer actually hears in the variant's FINAL mix:

    - `original_text` (no track, no voiceover): `_render_generative_variant` skips
      the mix entirely (`audio_mixed_path = assembled_path`) → the assembled audio
      IS the final audio → eligible.
    - song variants (track set, no voiceover): `_mix_template_audio` REPLACES the
      source audio (`-map 1:a` — the song is the only audio stream) → the original
      speech is never audible → ineligible.
    - voiceover variants (voiceover_gcs_path set): the audible speech is the
      user's VOICEOVER, not the assembled montage audio. `_mix_user_voiceover`
      with a music bed drops `[0:a]` (footage audio) from the filter graph
      entirely; voice-only mode ducks it by `1 - mix` under the always-full voice.
      Either way, syncing typography to the assembled-path transcript would fight
      the narration the viewer hears → ineligible (sequence-over-voiceover would
      need to transcribe the voice bed instead — a follow-up, not v1).

    Also requires the variant's intro layout to RESOLVE to "cluster" (the
    Editorial pick — post kill-switch / position-pin forcing in
    `_resolve_intro_overlay_params`); the sequence is the auto-upgraded editorial
    mode (D6), never a linear-intro upgrade.
    """
    if layout != "cluster":
        return False, "layout_not_cluster"
    if voiceover_gcs_path:
        return False, "voiceover_bed"
    if track is not None:
        return False, "song_replaced_audio"
    return True, "ok"


def _transcribe_for_sequence(assembled_path: str):
    """Whisper transcription of the pre-mix montage (D11) with a hard wall-clock
    guard (D18). Returns a `Transcript`; raises on failure/timeout — the caller
    maps ANY exception to the static styled-cluster fallback."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from app.pipeline import transcribe as transcribe_mod  # noqa: PLC0415

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(transcribe_mod.transcribe_whisper, assembled_path)
        return future.result(timeout=_SEQUENCE_TRANSCRIBE_TIMEOUT_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _compact_transcript_words(words: list) -> list[dict]:
    """Compact word records for `variants[i]["transcript"]` persistence
    (`{"word","start_s","end_s"}` — the shape `phrase_sequence` accepts back on
    deterministic re-renders). Reuses the phrase engine's normalizer so accessor
    quirks (dataclass vs dict, `start` vs `start_s`) stay single-sourced."""
    from app.pipeline.phrase_sequence import _normalize_words  # noqa: PLC0415

    return [
        {"word": w.text, "start_s": round(w.start, 3), "end_s": round(w.end, 3)}
        for w in _normalize_words(words)
    ]


def _record_sequence_fallback(reason: str, *, job_id: str, variant_id: str, mode: str) -> None:
    """Trace + log one sequence fallback (`mode` = "transcript" | "rhythm")."""
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    record_pipeline_event(
        "overlay",
        "sequence_fallback",
        {"variant_id": variant_id, "reason": reason, "mode": mode},
    )
    log.info(
        "generative_sequence_fallback",
        job_id=job_id,
        variant_id=variant_id,
        reason=reason,
        mode=mode,
    )


def _annotate_scene_roles(scenes: list[dict], *, job_id: str, variant_id: str, mode: str) -> None:
    """Fill `scene["word_roles"]` in place via the emphasis agent.

    ONE agent call annotating every phrase; terminal failure falls back per
    phrase to the deterministic heuristic (the agent contract). Shared by the
    transcript-synced and rhythm sequence paths so the two can never drift on
    role semantics. Never raises.
    """
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    emphasis_source = "heuristic"
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.sequence_emphasis import (  # noqa: PLC0415
            SequenceEmphasisAgent,
            SequenceEmphasisInput,
        )

        annotated = SequenceEmphasisAgent(default_client()).run(
            SequenceEmphasisInput(phrases=[list(s["words"]) for s in scenes]),
            ctx=RunContext(job_id=job_id),
        )
        for scene, phrase in zip(scenes, annotated.phrases, strict=True):
            scene["word_roles"] = list(phrase.word_roles)
        emphasis_source = "agent"
    except Exception as exc:  # noqa: BLE001 — emphasis is taste, never a blocker
        log.warning("generative_sequence_emphasis_failed", job_id=job_id, error=str(exc))
        from app.pipeline.intro_cluster import derive_word_roles  # noqa: PLC0415

        for scene in scenes:
            scene["word_roles"] = derive_word_roles(list(scene["words"]))
    record_pipeline_event(
        "overlay",
        "emphasis_source",
        {"variant_id": variant_id, "source": emphasis_source, "mode": mode},
    )


def _apply_scene_timing_overrides(scenes: list[dict], overrides: list[dict]) -> list[dict]:
    """Apply user-pinned start_s/end_s onto re-derived scenes.

    Silently drops overrides whose scene_index is out of bounds (handles transcript
    drift where re-derivation produces a different scene count).
    """
    patched = [dict(s) for s in scenes]
    for ov in overrides:
        idx = ov.get("scene_index", -1)
        if 0 <= idx < len(patched):
            patched[idx]["start_s"] = ov["start_s"]
            patched[idx]["end_s"] = ov["end_s"]
    return patched


def _attempt_sequence_overlays(
    *,
    job_id: str,
    variant_id: str,
    assembled_path: str,
    video_duration_s: float,
    base_size_px: int,
    text_color: str,
    scene_timing_overrides: list[dict] | None = None,
    canvas: Canvas = PORTRAIT,
) -> tuple[list[dict], dict[str, Any]] | None:
    """Build the transcript-synced sequence for one eligible variant.

    transcribe(assembled_path) → speech_eligibility → split_phrases → emphasis
    agent (per-phrase heuristic fallback) → build_sequence_overlays. Returns
    `(overlays, persist_patch)` on success; None on ANY failure/ineligibility —
    every fallback is traced (`sequence_fallback` + reason, mode="transcript")
    and the caller attempts the rhythm sequence / static cluster instead.
    Never raises.
    """
    from app.pipeline.phrase_sequence import speech_eligibility, split_phrases  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    def _fallback(reason: str) -> None:
        _record_sequence_fallback(reason, job_id=job_id, variant_id=variant_id, mode="transcript")
        return None

    try:
        transcript = _transcribe_for_sequence(assembled_path)
    except Exception as exc:  # noqa: BLE001 — D18: ASR can never fail a render
        log.warning("generative_sequence_transcribe_failed", job_id=job_id, error=str(exc))
        return _fallback("transcribe_failed")

    words = list(getattr(transcript, "words", None) or [])
    eligibility = speech_eligibility(words, video_duration_s=video_duration_s)
    record_pipeline_event(
        "overlay",
        "sequence_transcribed",
        {
            "variant_id": variant_id,
            "word_count": eligibility["word_count"],
            "coverage_frac": eligibility["coverage_frac"],
        },
    )
    if getattr(transcript, "low_confidence", False):
        return _fallback("low_confidence_transcript")
    if not eligibility["eligible"]:
        return _fallback(eligibility["reason"])

    scenes = split_phrases(words, video_duration_s=video_duration_s)
    if not scenes:
        return _fallback("no_scenes")
    record_pipeline_event(
        "overlay",
        "sequence_scenes",
        {"variant_id": variant_id, "count": len(scenes), "mode": "transcript"},
    )

    _annotate_scene_roles(scenes, job_id=job_id, variant_id=variant_id, mode="transcript")

    if scene_timing_overrides:
        scenes = _apply_scene_timing_overrides(scenes, scene_timing_overrides)

    from app.pipeline.generative_overlays import build_sequence_overlays  # noqa: PLC0415

    overlays = build_sequence_overlays(
        scenes,
        base_size_px=base_size_px,
        text_color=text_color,
        **_canvas_kwargs(canvas),
    )
    if not overlays:
        return _fallback("no_renderable_scenes")

    persist: dict[str, Any] = {
        # Compact transcript + annotated scenes: a fast-reburn edit rebuilds the
        # sequence DETERMINISTICALLY from these (no ASR, no LLM — D15/D19); a
        # re-assembling re-render overwrites them (transcript invalidation).
        "transcript": _compact_transcript_words(words),
        "scenes": scenes,
        "sequence_base_size_px": int(base_size_px),
        # Observability + FE-future field: which engine synced this sequence.
        # Real speech is authoritative — any persisted rhythm quote is cleared.
        "sequence_mode": "transcript",
        "sequence_quote": None,
    }
    return overlays, persist


def _attempt_rhythm_overlays(
    *,
    job_id: str,
    variant_id: str,
    video_duration_s: float,
    base_size_px: int,
    text_color: str,
    author_quote_fn: Any | None = None,
    persisted_quote: str | None = None,
    scene_timing_overrides: list[dict] | None = None,
    canvas: Canvas = PORTRAIT,
) -> tuple[list[dict], dict[str, Any]] | None:
    """Build the rhythm-mode sequence (authored quote, synthesized timings).

    Editorial variants WITHOUT eligible speech (no/too-little speech, low
    coverage, ASR failure, song-replaced audio, voiceover bed) still get the
    sequence treatment: an authored multi-phrase quote is paced rhythmically
    across the video by `rhythm_scenes` (deterministic — no ASR), then flows
    through the SAME emphasis + overlay machinery as the transcript path.

    Quote precedence: `persisted_quote` (cut-edit re-assembly re-times the SAME
    quote on the new duration — zero LLM calls) → `author_quote_fn(duration)`
    (SequenceQuoteWriterAgent, first render). There is NO heuristic quote
    fallback BY DESIGN — no agent quote means the static styled cluster, never
    a made-up quote. Returns `(overlays, persist_patch)` on success; None on
    ANY failure (traced `sequence_fallback`, mode="rhythm"). Never raises.
    """
    from app.agents.sequence_quote_writer import split_quote_sentences  # noqa: PLC0415
    from app.pipeline.phrase_sequence import rhythm_scenes  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    def _fallback(reason: str) -> None:
        _record_sequence_fallback(reason, job_id=job_id, variant_id=variant_id, mode="rhythm")
        return None

    quote = (persisted_quote or "").strip() or None
    if quote is not None:
        record_pipeline_event("overlay", "sequence_quote_reused", {"variant_id": variant_id})
    else:
        if author_quote_fn is None:
            return _fallback("no_quote_author")
        try:
            quote = author_quote_fn(video_duration_s)
        except Exception as exc:  # noqa: BLE001 — authoring can never fail a render
            log.warning("generative_sequence_quote_error", job_id=job_id, error=str(exc))
            quote = None
        quote = (quote or "").strip() or None
        if quote is None:
            return _fallback("quote_agent_failed")
        sentences = split_quote_sentences(quote)
        record_pipeline_event(
            "overlay",
            "sequence_quote_authored",
            {
                "variant_id": variant_id,
                "sentence_count": len(sentences),
                "word_count": sum(len(s.split()) for s in sentences),
            },
        )

    scenes = rhythm_scenes(quote, video_duration_s=video_duration_s)
    if not scenes:
        return _fallback("no_rhythm_scenes")
    record_pipeline_event(
        "overlay",
        "sequence_scenes",
        {"variant_id": variant_id, "count": len(scenes), "mode": "rhythm"},
    )

    _annotate_scene_roles(scenes, job_id=job_id, variant_id=variant_id, mode="rhythm")

    if scene_timing_overrides:
        scenes = _apply_scene_timing_overrides(scenes, scene_timing_overrides)

    from app.pipeline.generative_overlays import build_sequence_overlays  # noqa: PLC0415

    overlays = build_sequence_overlays(
        scenes,
        base_size_px=base_size_px,
        text_color=text_color,
        **_canvas_kwargs(canvas),
    )
    if not overlays:
        return _fallback("no_renderable_scenes")

    persist: dict[str, Any] = {
        # No ASR transcript in rhythm mode — explicit None clears any stale one.
        # The quote survives re-assembly (D15 clears scenes, NOT the quote) so a
        # cut-edit re-render re-times the same words deterministically, LLM-free.
        "transcript": None,
        "scenes": scenes,
        "sequence_base_size_px": int(base_size_px),
        "sequence_mode": "rhythm",
        "sequence_quote": quote,
    }
    return overlays, persist


def _burn_copy_through(final_path: str, source_path: str) -> bool:
    """Heuristic ported from the fast-reburn path (D20): a burn whose output is
    byte-size-identical to its input almost certainly copied input → output (the
    renderer's internal failure fallback) — i.e. a silent textless video."""
    return (
        os.path.exists(final_path)
        and os.path.exists(source_path)
        and os.path.getsize(final_path) == os.path.getsize(source_path)
    )


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
    if "missingglyphs" in name:
        return "lyrics_unsupported_language"
    if "lyric" in name or "karaoke" in name:
        return "lyric_alignment_error"
    return "unknown"


def _replace_step_clip(step: Any, clip_id: str) -> Any:
    """Return a copy of an AssemblyStep-like object pointed at another clip."""
    target_s = float((getattr(step, "slot", {}) or {}).get("target_duration_s", 0.5) or 0.5)
    moment = {"start_s": 0.0, "end_s": max(0.5, target_s)}
    if is_dataclass(step):
        return replace(step, clip_id=clip_id, moment=moment)
    copied = type("AssemblyStepLike", (), {})()
    copied.slot = dict(getattr(step, "slot", {}) or {})
    copied.clip_id = clip_id
    copied.moment = moment
    return copied


def _masonry_classic_safe_inputs(
    *,
    steps: list,
    clip_id_to_local: dict[str, str],
    clip_id_to_gcs: dict[str, str],
    probe_map: dict,
    clip_metas: list,
) -> tuple[list, dict[str, str], dict[str, str], dict, list, int]:
    """Build video-only inputs for classic fallback/audio-bed assembly.

    Masonry accepts still photos as visual tiles. The classic montage assembler
    does not: raw JPG/HEIC inputs can hit the video reframe path and fail before
    the collage has a chance to render. For fallback and original-audio beds we
    keep the slot structure but substitute photo slots with available videos.
    """
    from app.pipeline.image_clip import is_image_file  # noqa: PLC0415

    video_ids = [cid for cid, path in clip_id_to_local.items() if not is_image_file(path)]
    image_ids = {cid for cid, path in clip_id_to_local.items() if is_image_file(path)}
    if not image_ids:
        return steps, clip_id_to_local, clip_id_to_gcs, probe_map, clip_metas, 0
    if not video_ids:
        return [], {}, {}, {}, [], len(image_ids)

    replacements = cycle(video_ids)
    safe_steps = [
        step
        if getattr(step, "clip_id", None) not in image_ids
        else _replace_step_clip(step, next(replacements))
        for step in steps
    ]
    safe_local = {cid: clip_id_to_local[cid] for cid in video_ids}
    safe_gcs = {cid: path for cid, path in clip_id_to_gcs.items() if cid in video_ids}
    safe_paths = set(safe_local.values())
    safe_probe_map = {path: probe for path, probe in probe_map.items() if path in safe_paths}
    safe_video_ids = set(video_ids)
    safe_metas = [meta for meta in clip_metas if getattr(meta, "clip_id", None) in safe_video_ids]
    return safe_steps, safe_local, safe_gcs, safe_probe_map, safe_metas, len(image_ids)


def _variant_storage_key(job_id: str, filename: str, generation: object = None) -> str:
    """Return an immutable generation-scoped key for re-render outputs."""
    token = "".join(character for character in str(generation or "") if character.isalnum())[:32]
    if token:
        stem, extension = os.path.splitext(filename)
        filename = f"{stem}_{token}{extension}"
    return f"generative-jobs/{job_id}/{filename}"


def _discard_generation_storage(result: dict, *, job_id: str, generation: object) -> None:
    """Best-effort cleanup for outputs rejected by the generation guard."""
    token = "".join(character for character in str(generation or "") if character.isalnum())[:32]
    if not token:
        return
    prefix = f"generative-jobs/{job_id}/"
    keys = {
        value
        for field in ("video_path", "base_video_path", "subject_matte_path")
        if isinstance((value := result.get(field)), str)
        and value.startswith(prefix)
        and token in value
    }
    if not keys:
        return
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    for key in keys:
        delete_object_best_effort(key)
        if key == result.get("subject_matte_path"):
            delete_object_best_effort(f"{key}.json")


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
    narrative_order: list[str] | None = None,
    filming_guide: list[dict] | None = None,
    assembly_steps_override: list | None = None,
    allow_sequence: bool = True,
    author_quote_fn: Any | None = None,
    existing_sequence_quote: str | None = None,
    scene_timing_overrides: list[dict] | None = None,
    language: str = "en",
    font_family_override: str | None = None,
    effect_override: str | None = None,
    text_color_override: str | None = None,
    cluster_hero_font_override: str | None = None,
    cluster_body_font_override: str | None = None,
    cluster_accent_font_override: str | None = None,
    cluster_hero_size_px_override: int | None = None,
    cluster_body_size_px_override: int | None = None,
    cluster_accent_size_px_override: int | None = None,
    landscape_fit: str = "fill",
    montage_preset: str = "classic",
    behind_subject_override: bool | None = None,
    lyrics_enabled: bool | None = None,
    lyric_line_overrides: dict | None = None,
    orientation: str | None = None,
) -> dict[str, Any]:
    """Render one variant. Never raises — failures become a failure record.

    `allow_sequence` gates the editorial sequence — transcript-synced AND
    rhythm (D16/D19): True on first renders and pure re-assembly re-renders
    (the auto-pick re-runs, re-transcribing per D15); False when the edit
    explicitly opts out — a layout pick or a text change
    (`_run_regenerate_variant` computes this) — so the variant renders the
    static cluster/linear intro instead.

    `author_quote_fn` (rhythm mode): `(video_duration_s) -> str | None`, runs
    SequenceQuoteWriterAgent with the orchestrator's grounding. None means this
    render cannot author a fresh quote (e.g. the timeline-override path has no
    clip analysis) — rhythm then only runs off `existing_sequence_quote`.

    `existing_sequence_quote` is the variant's persisted rhythm quote: a
    re-assembling re-render (cut edit) re-times the SAME quote on the new
    duration deterministically — zero LLM calls — and carries it forward even
    when the sequence falls back to static (so a later eligible render can
    still reuse it).

    `narrative_order` (filming-guide alignment) is the guide clips' ids in
    shot order; forwarded to template_matcher.match so the edit's sequence
    follows the guide. None = classic greedy (public/legacy jobs).

    `assembly_steps_override` (clip timeline editor): pre-built exact-window
    AssemblySteps from a user timeline. When set, `consolidate_slots` and
    `match()` are skipped entirely — the steps go straight to `_assemble_clips`
    — and `clip_metas` may be empty (the override path runs no Gemini analysis).

    `intro_size_override_px` carries a user-pinned intro size (the public ±size
    nudge). When None the size is computed from the hero clip's composition; when
    set it wins and the variant records `intro_size_source="user"` so later
    re-renders (swap-song/retext/restyle) preserve it instead of recomputing.

    `user_style_knobs` are per-user parity-safe overrides (Creator Agent M1):
    font, position, colors, etc. They win over the curated set's values inside
    `_resolve_intro_overlay_params`. Persisted on the variant entry so re-renders
    (swap-song/retext/restyle) re-apply them without re-reading the persona row.

    `behind_subject_override` (text-behind-subject): explicit sticky decision
    from a re-render's task kwarg. None on a first render (the resolver falls
    back to the AI form's `behind_subject` decision). Gated against
    `settings.text_behind_subject_enabled` inside `_resolve_intro_overlay_params`
    — off means no matte is ever computed here regardless of this value.
    """
    from app.pipeline.agents.gemini_analyzer import build_recipe  # noqa: PLC0415
    from app.pipeline.lyric_support import lyrics_variant_renderable  # noqa: PLC0415
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
    resolved_montage_preset = coerce_montage_preset(montage_preset)
    resolved_orientation = _resolve_variant_orientation(None, orientation)
    canvas = canvas_for_orientation(resolved_orientation)
    # Voiceover variants: the user's audio is the narration bed, footage tiles as
    # visuals. `mix` is the voice-prominence slider (persisted so the UI slider and
    # re-renders can read it back). Absent on song/original/talking_head specs.
    voiceover_gcs_path: str | None = spec.get("voiceover_gcs_path")
    mix: float = float(spec.get("mix", _VOICEOVER_ONLY_DEFAULT_MIX))
    effective_lyrics_enabled = _lyrics_active(text_mode, lyrics_enabled)
    lyrics_available = (
        lyrics_variant_renderable(track.lyrics_cached) if track is not None else False
    )
    # Lyrics-as-optional-elements (LYRICS_OPTIONAL_ENABLED): every render pass
    # for a song_lyrics variant made while the flag is on skips baking lyrics
    # into pixels entirely, regardless of the `lyrics_enabled` kwarg — the
    # variant always renders lyrics-free (clean base, like song_text) and the
    # editor's Lyrics toggle instead materializes beat-synced lyric lines as
    # ordinary `role=lyric_line` TextElements (GET .../lyric-seeds) that burn
    # through the normal fast-text-reburn path on save. This is intentionally
    # unconditional on `lyrics_enabled` — a variant becomes "new model" the
    # moment it's (re-)rendered under the flag; flag-off (or a non-lyrics
    # variant) leaves `effective_lyrics_enabled` untouched, byte-identical.
    lyrics_optional_active = bool(settings.lyrics_optional_enabled) and text_mode == "lyrics"
    if lyrics_optional_active:
        effective_lyrics_enabled = False

    base = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": text_mode,
        "music_track_id": track_id,
        # Seed from the committed selection so a renderer failure cannot erase
        # the user's saved song window. Successful resolution overwrites this
        # with the same snapped effective value below.
        "music_start_s": spec.get("music_start_s"),
        "track_title": track_title,
        "style_set_id": style_set_id,
        # Agent-decided (or user-pinned) intro size. None for non-text variants.
        "intro_text_size_px": None,
        "intro_size_source": None,  # "computed" | "user" | "user_style" | None
        # Persisted intro text so re-renders can reuse it without re-running intro_writer.
        "intro_text": None,
        "intro_highlight_word": None,
        # Effective intro layout ("linear" | "cluster") + the word-role annotation
        # that rebuilds a cluster deterministically on re-render (no LLM).
        "intro_layout": None,
        "intro_word_roles": None,
        # Authoritative intro mode (D19): "sequence" | "cluster" | "linear" | None.
        # Replaces the len(overlays)>2 layout inference for readers; the inference
        # survives below ONLY to derive the value for non-sequence renders.
        "intro_mode": None,
        # Transcript-synced sequence persistence (D11/D15). None whenever this
        # render did NOT produce a sequence — a re-assembling re-render thereby
        # CLEARS stale words/scenes on success (merge semantics set them to None),
        # so the next editorial render re-transcribes against the new montage.
        "transcript": None,
        "scenes": None,
        "sequence_base_size_px": None,
        # Which engine synced the sequence: "transcript" | "rhythm" | None.
        "sequence_mode": None,
        # Rhythm-mode quote. Unlike transcript/scenes, the persisted quote
        # SURVIVES re-assembly (carried via existing_sequence_quote) so a cut
        # edit re-times the same words deterministically without an LLM call.
        "sequence_quote": existing_sequence_quote or None,
        # Voice-prominence slider for voiceover variants; None otherwise.
        "mix": mix if voiceover_gcs_path else None,
        # Per-user parity-safe knob overrides (Creator Agent M1). Persisted so
        # re-renders (swap-song/retext/restyle) re-apply them without re-reading
        # the persona row. None/empty = no overrides = baseline.
        "user_style_knobs": user_style_knobs or None,
        # Cached text-free, audio-mixed base for fast-reburn on style/font/size edits.
        # None for lyrics variants (full path in v1) and voiceover variants.
        "base_video_path": None,
        # Text-behind-subject (occlusion). intro_behind_subject is the sticky
        # pre-gate decision (task kwarg > persisted > agent form); overwritten
        # below once resolved. subject_matte_path is the cached matte's GCS key
        # (set once compute succeeds; None when the flag is off, no overlay
        # requested occlusion, or the compute/upload failed).
        "intro_behind_subject": False,
        "subject_matte_path": None,
        # Post-assembly clip timeline (clip timeline editor). Rewritten on EVERY
        # full montage assembly; None for voiceover/spine variants.
        "ai_timeline": None,
        "orientation": resolved_orientation,
        # Media-overlay cards (slice 1): list of MediaOverlay dicts set by the
        # apply-media-overlays variant-edit action; None when no cards have been
        # applied. The render path branches on truthiness (if media_overlays:)
        # so None is byte-identical to the pre-feature baseline.
        "media_overlays": None,
        # Durable clean copy of the variant before the first card apply-pass.
        # Enables "remove all cards" to restore the unmodified variant.
        # Stored as a GCS object key; None until the first apply-pass fires.
        "pre_media_overlay_video_path": None,
        # User-pinned independent style overrides (decoupled from style_set_id).
        # Resolved by the caller (_run_regenerate_variant) via sticky-override merge;
        # persisted here so the next re-render can carry them forward.
        "intro_font_family": font_family_override,
        "intro_effect": effect_override,
        "intro_text_color": text_color_override,
        "intro_cluster_hero_font": cluster_hero_font_override,
        "intro_cluster_body_font": cluster_body_font_override,
        "intro_cluster_accent_font": cluster_accent_font_override,
        "intro_cluster_hero_size_px": cluster_hero_size_px_override,
        "intro_cluster_body_size_px": cluster_body_size_px_override,
        "intro_cluster_accent_size_px": cluster_accent_size_px_override,
        "lyrics_enabled": effective_lyrics_enabled,
        "lyrics_available": lyrics_available,
        "lyric_line_overrides": lyric_line_overrides or None,
        "lyric_overlay_snapshot": None,
    }
    if spec.get("music_window_video_duration_s") is not None:
        base["music_window_video_duration_s"] = spec["music_window_video_duration_s"]
    if resolved_montage_preset != DEFAULT_MONTAGE_PRESET:
        # User-selected preset + actual renderer outcome. Only present when the
        # user opted in so classic jobs keep their historical variant shape.
        base.update(
            {
                "montage_preset": resolved_montage_preset,
                "montage_preset_rendered": None,
                "montage_preset_fallback": None,
            }
        )
    if lyrics_optional_active:
        # Only stamped when the flag actually skipped baking — absent (None
        # via .get()) for every flag-off render and every non-lyrics variant,
        # which is what keeps "flag off = byte-identical" true at the dict-
        # shape level too (same pattern as montage_preset above). Every
        # reader treats `variant.get("lyrics_baked") is False` as "new model"
        # and anything else (True/None/absent) as "legacy baked".
        base["lyrics_baked"] = False
    try:
        beats: list[float] = []
        effective_music_window: dict[str, Any] | None = None
        voiceover_local: str | None = None
        voiceover_target_s = available_footage_s

        # Slot-count floor: shot-assigned clips each get a guaranteed slot; for
        # pool-only jobs (no narrative_order) use total analyzed clip count so
        # that all uploaded footage is represented in the edit.
        min_slots = len(narrative_order) if narrative_order else len(clip_metas)
        if min_slots > _NARRATIVE_FLOOR_WARN_THRESHOLD:
            log.warning(
                "narrative_floor_high",
                min_slots=min_slots,
                job_id=job_id,
                variant_id=variant_id,
            )

        masonry_requested = (
            is_collage_montage_preset(resolved_montage_preset) and not voiceover_gcs_path
        )
        if masonry_requested and resolved_orientation == "landscape":
            log.warning(
                "landscape_orientation_unsupported_masonry_fallback_portrait",
                job_id=job_id,
                variant_id=variant_id,
                montage_preset=resolved_montage_preset,
            )
            resolved_orientation = "portrait"
            canvas = PORTRAIT
            base["orientation"] = resolved_orientation
        # ``landscape_fit`` is a portrait-canvas preference: it controls whether
        # a wide source is preserved inside 9:16. A landscape output follows the
        # editor's cover preview and always center-crops to fill 16:9. Resolve it
        # after the masonry fallback above because that fallback changes canvas.
        assembly_landscape_fit = "fill" if resolved_orientation == "landscape" else landscape_fit
        effective_available_footage_s = available_footage_s
        if masonry_requested:
            from app.pipeline.masonry_montage import clamp_masonry_duration  # noqa: PLC0415

            effective_available_footage_s = clamp_masonry_duration(available_footage_s)

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
            recipe_dict = _build_no_music_recipe(
                clip_metas, voiceover_target_s, min_slots=min_slots
            )
        elif track is not None:
            if not track.audio_gcs_path:
                raise ValueError(f"Track {track_id} has no audio_gcs_path")
            # Clamp the song's best-section window to the uploaded footage BEFORE
            # generating slots. The recipe slices [best_start, best_end] into
            # beat-snapped slots, so capping the window here is what keeps a
            # music variant from ever running longer than the content exists for.
            effective_music_window = _effective_music_window(
                track,
                requested_start_s=spec.get("music_start_s"),
                requested_duration_s=spec.get("music_window_video_duration_s"),
                fallback_footage_s=effective_available_footage_s,
            )
            track_config = effective_music_window["track_config"]
            base["music_start_s"] = effective_music_window["start_s"]
            if effective_music_window["validated"]:
                base["music_window_video_duration_s"] = effective_music_window["duration_s"]
            track_data = {
                "beat_timestamps_s": track.beat_timestamps_s or [],
                "track_config": track_config,
                "duration_s": track.duration_s,
            }
            recipe_dict = generate_music_recipe(
                track_data, filming_guide=filming_guide, min_slots=min_slots
            )
            beats = list(recipe_dict.get("beat_timestamps_s") or [])
            recipe_dict["slots"] = _enrich_slots_with_energy(
                recipe_dict["slots"], track_data["beat_timestamps_s"]
            )
        else:
            recipe_dict = _build_no_music_recipe(
                clip_metas,
                effective_available_footage_s,
                filming_guide=filming_guide,
                min_slots=min_slots,
            )

        # Text injection per mode. The chosen style set styles BOTH the lyric
        # overlays (lyrics variant) and the AI hero-intro (text variants).
        # For agent_text variants we do NOT inject into the recipe here —
        # instead we assemble text-free, cache the base, then burn text in a
        # separate step so fast-reburn can skip re-assembly on future edits.
        lyrics_rendered = effective_lyrics_enabled and track is not None
        if lyrics_rendered:
            lyric_kwargs = {
                "style_set_id": style_set_id,
                "line_overrides": lyric_line_overrides,
            }
            if effective_music_window and effective_music_window["validated"]:
                lyric_kwargs.update(
                    music_start_s=effective_music_window["start_s"],
                    music_end_s=effective_music_window["end_s"],
                )
            lyric_result = _inject_lyrics(recipe_dict, track, **lyric_kwargs)
            if isinstance(lyric_result, tuple):
                recipe_dict, lyric_snapshot = lyric_result
            else:
                recipe_dict, lyric_snapshot = lyric_result, []
            base["lyric_overlay_snapshot"] = lyric_snapshot
            from app.pipeline.lyric_injector import (  # noqa: PLC0415
                rematerialize_lyric_line_overrides,
            )

            active_lyric_keys = {
                str(entry.get("line_key"))
                for entry in lyric_snapshot
                if isinstance(entry, dict) and entry.get("line_key")
            }
            base["lyric_line_overrides"] = rematerialize_lyric_line_overrides(
                track.lyrics_cached,
                lyric_line_overrides,
                active_lyric_keys,
            )
            if assembly_steps_override is not None:
                _project_recipe_overlays_to_steps(recipe_dict, assembly_steps_override)

        # Resolve agent_text overlay params early (before assembly) so we have
        # intro_px / intro_source for base dict even if the burn fails below.
        _agent_text_overlays = None  # built after audio mix, if needed
        _agent_text_intro_px = None
        _agent_text_intro_source = None
        text_placement_candidates: list[dict] = []
        if text_mode == "agent_text" and agent_text is not None:
            hero_safe_zone, hero_density = _hero_composition(clip_metas)
            text_placement_candidates = _placement_candidates_for_intro(
                hero_safe_zone=hero_safe_zone,
                hero_density=hero_density,
                masonry_requested=masonry_requested,
                montage_preset=resolved_montage_preset if masonry_requested else None,
                duration_s=effective_available_footage_s,
            )
            _at_params, _agent_text_intro_px, _agent_text_intro_source = (
                _resolve_intro_overlay_params(
                    agent_text,
                    agent_form,
                    style_set_id,
                    hero_safe_zone=hero_safe_zone,
                    hero_density=hero_density,
                    size_override_px=intro_size_override_px,
                    user_style_knobs=user_style_knobs,
                    language=language,
                    font_family_override=font_family_override,
                    effect_override=effect_override,
                    text_color_override=text_color_override,
                    placement_candidates=text_placement_candidates,
                    behind_subject_override=behind_subject_override,
                    canvas=canvas,
                )
            )
            # Sticky (pre-gate) decision — must be popped before `_at_params` is
            # ever spread into build_persistent_intro_overlays (not a builder kwarg).
            base["intro_behind_subject"] = _at_params.pop("_bs_pregate", False)
            base["text_placement_candidates"] = text_placement_candidates or None
            base["intro_text_size_px"] = _agent_text_intro_px
            base["intro_size_source"] = _agent_text_intro_source
            # Persist the intro text so re-renders (font/size/style edits) can reuse
            # it without re-running intro_writer.
            base["intro_text"] = agent_text.text if agent_text is not None else None
            base["intro_highlight_word"] = (
                getattr(agent_text, "highlight_word", None) if agent_text is not None else None
            )
            # EFFECTIVE layout (post kill-switch / position-pin forcing), not the
            # agent's raw choice — the FE gates instant text preview on this.
            base["intro_layout"] = _at_params.get("layout", "linear")
            # Provisional mode (refined in the burn step: "sequence" when the
            # editorial auto-upgrade lands, else the effective static layout).
            base["intro_mode"] = base["intro_layout"]
            base["intro_word_roles"] = _at_params.get("word_roles")

        # Propagate the shot-count floor into the recipe so consolidate_slots
        # (template_matcher.py:231-242) doesn't collapse below it. The builders
        # already applied the floor to slot count; this ensures consolidation
        # doesn't undo that work even when the user uploads fewer total clips.
        if min_slots > 0:
            recipe_dict["min_slots"] = max(int(recipe_dict.get("min_slots", 0) or 0), min_slots)

        recipe = build_recipe(recipe_dict)
        if assembly_steps_override is not None:
            # Timeline-override path: the user's exact-window slots ARE the
            # plan — skip consolidate_slots and match() entirely.
            steps = list(assembly_steps_override)
        else:
            try:
                recipe = consolidate_slots(recipe, clip_metas)
                assembly_plan = match(recipe, clip_metas, narrative_order=narrative_order)
            except TemplateMismatchError as exc:
                raise ValueError(f"{exc.code}: {exc.message}") from exc
            steps = assembly_plan.steps

        # After the matcher runs, check whether any assigned clips were left
        # unplaced (residual: song window physically too short, or footage that
        # failed analysis and never entered clip_metas). Surface per-variant so
        # the UI can explain the gap without the user digging into /admin/jobs.
        # Timeline-override path has no narrative_order → skip.
        if narrative_order and assembly_steps_override is None:
            placed_ids = {step.clip_id for step in steps}
            unplaced_ids = [cid for cid in narrative_order if cid not in placed_ids]
            if unplaced_ids:
                base["unplaced_shots"] = _build_unplaced_shots(
                    unplaced_ids,
                    narrative_order=narrative_order,
                    clip_id_to_gcs=clip_id_to_gcs,
                    clip_metas=clip_metas,
                    is_music_variant=(track is not None and not voiceover_gcs_path),
                )

        assembled_path = os.path.join(variant_dir, "assembled.mp4")
        resolved_plans: list[dict] = []
        classic_steps = steps
        classic_clip_id_to_local = clip_id_to_local
        classic_clip_id_to_gcs = clip_id_to_gcs
        classic_probe_map = probe_map
        classic_clip_metas = clip_metas
        classic_image_substitutions = 0
        if masonry_requested:
            (
                classic_steps,
                classic_clip_id_to_local,
                classic_clip_id_to_gcs,
                classic_probe_map,
                classic_clip_metas,
                classic_image_substitutions,
            ) = _masonry_classic_safe_inputs(
                steps=steps,
                clip_id_to_local=clip_id_to_local,
                clip_id_to_gcs=clip_id_to_gcs,
                probe_map=probe_map,
                clip_metas=clip_metas,
            )

        # Masonry song variants replace footage audio with the matched track later
        # in the normal audio-mix branch. Rendering a full classic montage first is
        # therefore pure waste and can consume the whole Celery budget on heavy
        # uploads before the collage compositor even starts. Original-audio masonry
        # still needs this pass because it derives its audio bed from source clips.
        skip_classic_assembly_for_masonry_song = masonry_requested and track is not None
        classic_assembly_done = False

        def _assemble_classic_montage() -> None:
            nonlocal classic_assembly_done
            if masonry_requested and (not classic_steps or not classic_clip_id_to_local):
                raise RuntimeError("classic montage fallback unavailable: no video clips")
            _assemble_clips(
                classic_steps,
                classic_clip_id_to_local,
                classic_probe_map,
                assembled_path,
                variant_dir,
                beat_timestamps_s=recipe.beat_timestamps_s,
                clip_metas=classic_clip_metas,
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
                # Post-resolution source windows per slot — the clip editor's
                # ground truth for what each slot actually rendered.
                resolved_plans_out=resolved_plans,
                landscape_fit=assembly_landscape_fit,
                canvas=canvas,
            )
            classic_assembly_done = True

        if not skip_classic_assembly_for_masonry_song:
            if masonry_requested and not classic_steps:
                log.info(
                    "masonry_original_audio_bed_skipped_no_video",
                    job_id=job_id,
                    variant_id=variant_id,
                    image_clips=classic_image_substitutions,
                )
            else:
                _assemble_classic_montage()

        masonry_applied = False
        if masonry_requested:
            from app.pipeline.masonry_montage import assemble_masonry_montage  # noqa: PLC0415
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            if classic_image_substitutions:
                record_pipeline_event(
                    "assembly",
                    "masonry_classic_inputs_sanitized",
                    {
                        "variant_id": variant_id,
                        "image_clips": classic_image_substitutions,
                        "video_clips": len(classic_clip_id_to_local),
                    },
                )

            masonry_path = os.path.join(variant_dir, "masonry.mp4")
            try:
                assemble_masonry_montage(
                    steps=steps,
                    clip_id_to_local=clip_id_to_local,
                    output_path=masonry_path,
                    tmpdir=variant_dir,
                    duration_s=effective_available_footage_s,
                    audio_source_path=assembled_path if classic_assembly_done else None,
                    job_id=job_id,
                    preset=resolved_montage_preset,
                )
                assembled_path = masonry_path
                masonry_applied = True
                base["montage_preset_rendered"] = resolved_montage_preset
                record_pipeline_event(
                    "assembly",
                    "masonry_preset_applied",
                    {"variant_id": variant_id, "duration_s": effective_available_footage_s},
                )
            except Exception as exc:  # noqa: BLE001
                base["montage_preset_fallback"] = "classic_render_failed"
                record_pipeline_event(
                    "assembly",
                    "masonry_preset_fallback",
                    {"variant_id": variant_id, "error": str(exc)[:300]},
                )
                log.warning(
                    "masonry_preset_fallback_classic",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc),
                )
                if not classic_assembly_done:
                    _assemble_classic_montage()

        # ai_timeline persistence (clip timeline editor): rewritten on every
        # FRESH montage assembly (first render, swap-song, mix re-render), so
        # the stored AI cut tracks what the matcher actually produced.
        # Voiceover variants are skipped — the voice, not a slot grid, drives
        # their layout. `beats` is the section-relative grid this assembly
        # snapped against (empty for the no-music variant).
        #
        # Override path (user timeline render): the steps ARE the user's cut,
        # not an AI cut — rebuilding here would make "Reset to AI cut" re-render
        # the user's own edit. Pop the key so `_update_variant_entry`'s
        # {**v, **patch} merge carries the variant's persisted ai_timeline
        # forward untouched (writing None instead would null the stored
        # timeline and flip the variant uneditable).
        if (
            settings.GENERATIVE_TIMELINE_EDITOR_ENABLED
            and not voiceover_gcs_path
            and not masonry_applied
        ):
            if assembly_steps_override is not None:
                base.pop("ai_timeline", None)
            else:
                base["ai_timeline"] = _build_ai_timeline(
                    steps=classic_steps if masonry_requested else steps,
                    resolved_plans=resolved_plans,
                    clip_id_to_gcs=classic_clip_id_to_gcs if masonry_requested else clip_id_to_gcs,
                    clip_id_to_local=(
                        classic_clip_id_to_local if masonry_requested else clip_id_to_local
                    ),
                    probe_map=classic_probe_map if masonry_requested else probe_map,
                    beat_grid=beats,
                )
        elif masonry_applied:
            base.pop("ai_timeline", None)

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
            if effective_music_window is None:
                raise RuntimeError("Music window was not resolved")
            _mix_template_audio(
                assembled_path,
                track.audio_gcs_path,
                audio_mixed_path,
                variant_dir,
                audio_start_offset_s=float(effective_music_window["start_s"]),
                validated_window_duration_s=(
                    float(effective_music_window["duration_s"])
                    if effective_music_window["validated"]
                    else None
                ),
                require_audio=True,
            )
        else:
            # Original-audio variant: KEEP the clips' source audio — skip the mix.
            # `_assemble_clips` already muxed source audio into assembled.mp4.
            audio_mixed_path = assembled_path

        if not os.path.exists(audio_mixed_path) or os.path.getsize(audio_mixed_path) == 0:
            raise RuntimeError(f"variant {variant_id} produced empty audio-mixed output")

        # For agent_text variants: upload the text-free base for fast-reburn, then
        # burn text on top to produce the final output. Lyrics variants cache the
        # lyric-burned, user-text-free base so user TextElements can layer above it.
        if text_mode == "agent_text" and agent_text is not None:
            from app.pipeline.generative_overlays import (  # noqa: PLC0415
                build_persistent_intro_overlays,
            )
            from app.pipeline.intro_cluster import EDITORIAL_STYLE  # noqa: PLC0415
            from app.pipeline.probe import probe_video  # noqa: PLC0415
            from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            # Upload the text-free base first.
            base_gcs = _variant_storage_key(
                job_id,
                f"base_{rank}_{variant_id}.mp4",
                spec.get("storage_generation"),
            )
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

            def _burn_agent_text_overlays(
                overlay_dicts: list[dict], output_path: str, *, matte=None
            ) -> None:
                if masonry_applied:
                    from app.pipeline.masonry_montage import (  # noqa: PLC0415
                        burn_masonry_text_overlays,
                        masonry_board_width_for_preset,
                    )

                    # Masonry's board-motion burn has no matte-occlusion support
                    # (Lane B scoped it to the standard burn path only).
                    overlay_dicts = [
                        {k: v for k, v in ov.items() if k != "behind_subject"}
                        for ov in overlay_dicts
                    ]
                    burn_masonry_text_overlays(
                        audio_mixed_path,
                        overlay_dicts,
                        output_path,
                        variant_dir,
                        duration_s=base_dur,
                        board_width=masonry_board_width_for_preset(resolved_montage_preset),
                    )
                    return
                burn_text_overlays_skia(
                    audio_mixed_path,
                    overlay_dicts,
                    output_path,
                    variant_dir,
                    matte=matte,
                    **_canvas_kwargs(canvas),
                )

            def _burn_agent_text_overlays_with_matte(
                overlay_dicts: list[dict], output_path: str
            ) -> None:
                # Text-behind-subject: resolve (or compute-and-cache) the matte for
                # THIS overlay set, then burn. `base["subject_matte_path"]` doubles
                # as the resolution cache key — a copy-through retry below re-burns
                # a fresh overlay set on the SAME base, so the second call reuses
                # whatever the first call just computed instead of recomputing.
                provider, matte_path, overlay_dicts = _resolve_subject_matte_for_burn(
                    video_path=audio_mixed_path,
                    overlays=overlay_dicts,
                    tmpdir=variant_dir,
                    cached_matte_path=base.get("subject_matte_path"),
                    upload_key_base=base_gcs,
                    duration_s=base_dur,
                    job_id=job_id,
                    variant_id=variant_id,
                )
                base["subject_matte_path"] = matte_path
                _burn_agent_text_overlays(overlay_dicts, output_path, matte=provider)

            # Editorial sequence auto-upgrade (D6/D16): when the kill switch is
            # ON and the layout resolved to "cluster", the variant gets the
            # typographic sequence. Source precedence:
            #   1. TRANSCRIPT sync — only when the final mix keeps the montage's
            #      original speech audible (_sequence_gate) AND the speech is
            #      eligible: transcribe the PRE-mix montage (assembled_path —
            #      D11) and sync the typography to the spoken words.
            #   2. RHYTHM — speech ineligible for ANY reason (no/too-little
            #      speech, low coverage, ASR failure, song-replaced audio,
            #      voiceover bed): pace an authored quote across the video
            #      (rhythm mode works on ANY audio — it needs no audible
            #      speech).
            # Any rhythm failure falls through to the static styled cluster
            # (the `cluster_style` arg below) — never a failed variant. A
            # linear layout stays static (the sequence is the editorial
            # auto-upgrade, never a linear-intro upgrade — D6).
            editorial_enabled = bool(getattr(settings, "editorial_sequence_enabled", True))
            sequence_result = None
            if editorial_enabled and allow_sequence:
                sequence_ok, gate_reason = _sequence_gate(
                    layout=_at_params.get("layout"),
                    track=track,
                    voiceover_gcs_path=voiceover_gcs_path,
                )
                record_pipeline_event(
                    "overlay",
                    "sequence_eligibility",
                    {"variant_id": variant_id, "eligible": sequence_ok, "reason": gate_reason},
                )
                if sequence_ok:
                    sequence_result = _attempt_sequence_overlays(
                        job_id=job_id,
                        variant_id=variant_id,
                        assembled_path=assembled_path,  # PRE-mix montage audio (D11)
                        video_duration_s=base_dur,
                        base_size_px=int(_agent_text_intro_px or 60),
                        text_color=str(_at_params.get("text_color") or "#FFFFFF"),
                        scene_timing_overrides=scene_timing_overrides or None,
                        **_canvas_kwargs(canvas),
                    )
                if sequence_result is None and gate_reason != "layout_not_cluster":
                    sequence_result = _attempt_rhythm_overlays(
                        job_id=job_id,
                        variant_id=variant_id,
                        video_duration_s=base_dur,
                        base_size_px=int(_agent_text_intro_px or 60),
                        text_color=str(_at_params.get("text_color") or "#FFFFFF"),
                        author_quote_fn=author_quote_fn,
                        persisted_quote=existing_sequence_quote,
                        scene_timing_overrides=scene_timing_overrides or None,
                        **_canvas_kwargs(canvas),
                    )

            def _static_intro_overlays() -> list[dict]:
                # Static intro (cluster or linear). Sequence-eligible fallback
                # keeps PR #508's editorial restyle; explicit opt-outs
                # (layout/text edits) use the legacy static cluster path so
                # Slice 3a's registry pairing owns the faces.
                _has_sio_overrides = any(
                    [
                        cluster_hero_font_override,
                        cluster_body_font_override,
                        cluster_accent_font_override,
                        cluster_hero_size_px_override,
                        cluster_body_size_px_override,
                        cluster_accent_size_px_override,
                    ]
                )
                if editorial_enabled and allow_sequence and _has_sio_overrides:
                    _sio_cs: dict | None = dict(EDITORIAL_STYLE)
                    if cluster_hero_font_override:
                        _sio_cs["hero_font"] = cluster_hero_font_override
                    if cluster_body_font_override:
                        _sio_cs["body_font"] = cluster_body_font_override
                    if cluster_accent_font_override:
                        _sio_cs["accent_font"] = cluster_accent_font_override
                    if cluster_hero_size_px_override:
                        _sio_cs["hero_size_px_override"] = cluster_hero_size_px_override
                    if cluster_body_size_px_override:
                        _sio_cs["connector_size_px_override"] = cluster_body_size_px_override
                    if cluster_accent_size_px_override:
                        _sio_cs["closer_size_px_override"] = cluster_accent_size_px_override
                elif editorial_enabled and allow_sequence:
                    _sio_cs = EDITORIAL_STYLE  # type: ignore[assignment]
                else:
                    _sio_cs = None
                return build_persistent_intro_overlays(
                    reveal_window_s=reveal_window_s,
                    beats=beats,
                    cluster_style=_sio_cs,
                    **_at_params,
                    **_canvas_kwargs(canvas),
                )

            def _apply_static_layout(static_overlays: list[dict]) -> None:
                # EFFECTIVE layout: the engine can decline at build time (word
                # count, fit) and fall back to linear — a linear pair is exactly
                # 2 overlays, a cluster is 2 per block. Legacy inference (D19) —
                # kept ONLY to derive intro_mode/intro_layout for static renders.
                effective = "cluster" if len(static_overlays) > 2 else "linear"
                base["intro_layout"] = effective
                base["intro_mode"] = effective
                base["transcript"] = None
                base["scenes"] = None
                base["sequence_base_size_px"] = None
                base["sequence_mode"] = None
                # base["sequence_quote"] is deliberately NOT cleared: a known
                # quote (persisted carry or just authored) survives a static
                # fallback so a later eligible render re-times it LLM-free.

            if sequence_result is not None:
                overlays, sequence_persist = sequence_result
                base.update(sequence_persist)
                base["intro_layout"] = "cluster"
                base["intro_mode"] = "sequence"
            else:
                overlays = _static_intro_overlays()
                _apply_static_layout(overlays)
            _burn_agent_text_overlays_with_matte(overlays, final_path)

            # D20: copy-through detection ported from the fast-reburn path. A
            # silent textless output must never ship as a "ready" variant.
            if overlays and _burn_copy_through(final_path, audio_mixed_path):
                if base["intro_mode"] == "sequence":
                    # Loud static fallback: re-burn the static styled cluster.
                    record_pipeline_event(
                        "overlay",
                        "sequence_fallback",
                        {
                            "variant_id": variant_id,
                            "reason": "burn_copy_through",
                            "mode": base.get("sequence_mode"),
                        },
                    )
                    log.warning(
                        "generative_sequence_burn_copy_through",
                        job_id=job_id,
                        variant_id=variant_id,
                    )
                    overlays = _static_intro_overlays()
                    _apply_static_layout(overlays)
                    _burn_agent_text_overlays_with_matte(overlays, final_path)
                if overlays and _burn_copy_through(final_path, audio_mixed_path):
                    raise RuntimeError(
                        f"burn_text_overlays_skia copy-through detected on variant "
                        f"{variant_id}; failing the render instead of shipping a "
                        "textless video"
                    )
        else:
            if text_mode == "lyrics" or lyrics_rendered:
                base_gcs = _variant_storage_key(
                    job_id,
                    f"base_{rank}_{variant_id}.mp4",
                    spec.get("storage_generation"),
                )
                base_url_unused = upload_public_read(audio_mixed_path, base_gcs)  # noqa: F841
                base["base_video_path"] = base_gcs
                log.info(
                    "generative_base_uploaded",
                    job_id=job_id,
                    variant_id=variant_id,
                    base_gcs=base_gcs,
                )
            final_path = audio_mixed_path

        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError(f"variant {variant_id} produced empty output")
        if resolved_orientation == "landscape":
            from app.pipeline.validator import validate_output  # noqa: PLC0415

            validation = validate_output(
                final_path,
                expected_resolution=(canvas.width, canvas.height),
                # The validator's default 45–59s contract belongs to the
                # template pipeline. Generative montages are intentionally
                # shorter; retain the universal sub-60s ceiling while still
                # rejecting empty/truncated output.
                expected_duration_range=(0.1, settings.output_max_duration_s),
            )
            if not validation.passed:
                raise RuntimeError("; ".join(validation.errors))

        output_gcs = _variant_storage_key(
            job_id,
            f"variant_{rank}_{variant_id}.mp4",
            spec.get("storage_generation"),
        )
        output_url = upload_public_read(final_path, output_gcs)
        log.info("generative_variant_uploaded", job_id=job_id, variant_id=variant_id)
        return {
            **base,
            "ok": True,
            "render_status": "ready",
            "video_path": output_gcs,
            "output_url": output_url,
            **(
                {"duration_s": _rendered_duration_s(final_path)}
                if settings.visual_blocks_enabled
                else {}
            ),
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
    language: str = "en",
    landscape_fit: str = "fill",
    silence_cut_disabled: bool = False,
    silence_cut_cache: _SilenceCutCache | None = None,
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

    Silence/filler/retake cut (plans/010 T6, behind SILENCE_CUT_ENABLED): the SPINE
    clip gets the same cut stage as subtitled — the flag/per-item gates live here,
    the mechanics (pre-cap, has_audio gate, keep_segments reframe, b-roll cut-point
    anchors) live in the assembler, and the analysis routes through the shared
    `_silence_cut_analysis` + per-job cache so a clip is never re-analyzed. Every
    gate/failure falls OPEN to the uncut flow; flag off is byte-identical
    (kill-switch pinned).
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
        # Effective intro layout ("linear" | "cluster") + the word-role annotation
        # that rebuilds a cluster deterministically on re-render (no LLM).
        "intro_layout": None,
        "intro_word_roles": None,
        # Authoritative intro mode (D19). talking_head never renders the
        # transcript-synced sequence in v1, so this is always the static layout.
        "intro_mode": None,
        "resolved_archetype": "talking_head",
        # Per-user parity-safe knob overrides (Creator Agent M1). Persisted for
        # re-renders (same as _render_generative_variant).
        "user_style_knobs": user_style_knobs or None,
        # No text-free base cached for talking_head in v1 (the assembler's spine
        # extraction is the expensive step, not text burn; fast-reburn not yet applied).
        "base_video_path": None,
        # Text-behind-subject: resolved (overwritten below) but never rendered —
        # talking_head has no fast-reburn path in v1, so no matte is ever computed
        # here; a behind_subject overlay burns as plain text (matte=None is a
        # safe, logged fallback per the Skia renderer's contract).
        "intro_behind_subject": False,
        "subject_matte_path": None,
        # Media-overlay cards (slice 1) — see montage finalize dict for docs.
        "media_overlays": None,
        "pre_media_overlay_video_path": None,
        # Silence-cut summary {removed, time_saved_s, version} (plans/010 T6) — set
        # only when the stage ran to a plan; drives the admin cut-plan viewer.
        "silence_cut": None,
    }

    try:
        # SpineExtractionError (corrupt spine) is re-raised below for the job-level
        # montage degrade; every OTHER failure — a composite ffmpeg error, a burn or
        # upload failure — becomes a per-variant failure record (the never-raise
        # contract `_render_generative_variant` also honors).
        base_path = os.path.join(variant_dir, "base.mp4")

        # ── Silence/filler/retake cut gates (plans/010 T6) ──────────────────
        # Flag off / per-item disable ⇒ silence_cut_fn stays None and the
        # assembler runs its pre-T6 flow byte-identically. The has_audio gate
        # needs the SPINE probe, so it lives inside the assembler (same event).
        silence_cut_fn = None
        silence_cut_out: dict[str, Any] = {}
        if settings.silence_cut_enabled:
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            if silence_cut_disabled:
                # Per-item opt-out (10A) — skips the WHOLE stage, retakes included.
                record_pipeline_event(
                    "silence_cut", "silence_cut_skipped_disabled", {"variant_id": variant_id}
                )
            else:

                def silence_cut_fn(
                    analysis_path: str, duration_s: float, *, cache_key: str | None = None
                ) -> dict[str, Any]:
                    # Shared analysis (7A): per-job cache ⇒ a clip analyzed for
                    # one variant is never re-analyzed for another. `cache_key`
                    # lets the assembler key a pre-capped analysis WAV by its
                    # SOURCE spine (+cap), so the entry stays clip-addressed.
                    return _silence_cut_analysis(
                        analysis_path,
                        duration_s,
                        job_id=job_id,
                        cache=silence_cut_cache,
                        cache_key=cache_key,
                    )

        assemble_talking_head(
            clip_paths=clip_id_to_local,
            clip_metas=clip_metas,
            probe_map=probe_map,
            target_duration_s=available_footage_s or None,
            output_path=base_path,
            tmpdir=variant_dir,
            job_id=job_id,
            spine_clip_id=spine_clip_id,
            landscape_fit=landscape_fit,
            silence_cut_fn=silence_cut_fn,
            silence_cut_out=silence_cut_out,
        )
        base["silence_cut"] = silence_cut_out.get("summary")

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
                language=language,
            )
            # Sticky (pre-gate) decision — no override kwarg here: talking_head has
            # no fast-reburn / re-render path (v1), so there is no task kwarg to
            # thread. Popped so `params` stays a valid builder-kwargs dict below.
            base["intro_behind_subject"] = params.pop("_bs_pregate", False)
            # No song → no beats; the intro reveals on an even split. Slot-0-relative
            # timestamps (from 0) are already absolute on the composite, which is what
            # burn_text_overlays_skia expects.
            # DELIBERATE (PR #508 review): no cluster_style here — the editorial
            # cascade restyle + transcript/rhythm sequence are scoped to montage
            # variants this release. Talking-head intros stay the legacy stacked
            # cluster on purpose; unifying the look is a separate aesthetic change
            # that should be judged on a talking-head render first, not slipped in.
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
            # EFFECTIVE layout (cluster = 2 overlays per block; linear = one pair).
            base["intro_layout"] = "cluster" if len(overlays) > 2 else "linear"
            base["intro_mode"] = base["intro_layout"]
            base["intro_word_roles"] = params.get("word_roles")

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
            **(
                {"duration_s": _rendered_duration_s(final_path)}
                if settings.visual_blocks_enabled
                else {}
            ),
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


def _narrated_script_steps(filming_guide: list[dict]) -> list:
    from app.pipeline.narrated_alignment import StepScript  # noqa: PLC0415

    steps: list[StepScript] = []
    for idx, shot in enumerate(filming_guide):
        if not isinstance(shot, dict):
            continue
        text = str(shot.get("what") or "").strip()
        if not text:
            continue
        step_id = str(shot.get("shot_id") or f"step_{idx + 1}")
        steps.append(StepScript(step_id=step_id, text=text))
    return steps


def _narrated_clip_assignments(
    filming_guide: list[dict],
    narrative_order: list[str] | None,
    clip_id_to_local: dict[str, str],
) -> list:
    from app.pipeline.narrated_assembler import NarratedClip  # noqa: PLC0415

    ordered_clip_ids = list(narrative_order or list(clip_id_to_local))
    assignments: list[NarratedClip] = []
    clip_idx = 0
    for idx, shot in enumerate(filming_guide):
        if not isinstance(shot, dict) or not str(shot.get("what") or "").strip():
            continue
        if clip_idx >= len(ordered_clip_ids):
            break
        clip_id = ordered_clip_ids[clip_idx]
        clip_idx += 1
        clip_path = clip_id_to_local.get(clip_id)
        if not clip_path:
            continue
        step_id = str(shot.get("shot_id") or f"step_{idx + 1}")
        assignments.append(NarratedClip(step_id=step_id, clip_path=clip_path))
    return assignments


def _render_narrated_variant(
    *,
    job_id: str,
    rank: int,
    spec: dict[str, Any],
    filming_guide: list[dict],
    narrative_order: list[str] | None,
    clip_id_to_local: dict[str, str],
    variant_dir: str,
    landscape_fit: str = "fill",
) -> dict[str, Any]:
    """Render one narrated walkthrough variant."""
    from app.pipeline.narrated_assembler import assemble_narrated  # noqa: PLC0415
    from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415

    variant_id = spec["variant_id"]
    voiceover_gcs_path = str(spec.get("voiceover_gcs_path") or "")
    bed_level = spec.get("voiceover_bed_level")
    # Caption style: "word" → one big word at a time (qbuilder); anything else →
    # "sentence" (today's sentence-block captions). Persisted on the variant so the
    # reburn re-burns edited cues in the same style.
    caption_style = (
        "word" if str(spec.get("voiceover_caption_style") or "") == "word" else "sentence"
    )
    # Caption font (a font-registry key; None → the default TikTok Sans). Applies to
    # both caption styles. Persisted on the variant so the editor shows the choice and
    # the reburn re-burns in the same font. The render resolves it to a libass family.
    caption_font = spec.get("voiceover_caption_font") or None
    base = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": "none",
        "music_track_id": None,
        "track_title": None,
        "style_set_id": None,
        "intro_text_size_px": None,
        "intro_size_source": None,
        "intro_text": None,
        "intro_highlight_word": None,
        "intro_layout": None,
        "intro_word_roles": None,
        "intro_mode": None,
        "transcript": None,
        "scenes": None,
        "sequence_base_size_px": None,
        "sequence_mode": None,
        "sequence_quote": None,
        "mix": 1.0,
        "user_style_knobs": None,
        "base_video_path": None,
        # Editable caption cues [{text, start_s, end_s}] (assembled-time). Drives the
        # on-video caption editor; reburned onto base_video_path when the creator edits.
        "caption_cues": None,
        # On/off toggle, independent of cue count — toggling off must never destroy
        # the transcript-derived cues (so toggling back on needs no re-transcription).
        # Defaults true; the editor's Captions tab visibility gates on archetype, not
        # this flag or cue count (see _is_editable_caption_variant).
        "captions_enabled": True,
        # Caption style this variant renders with ("sentence" | "word"). The reburn
        # reads it so edited cues re-burn in the SAME style as the first render.
        "voiceover_caption_style": caption_style,
        # Caption font (font-registry key; None → default). Editable in the on-video
        # caption editor; the reburn resolves it to a libass family.
        "voiceover_caption_font": caption_font,
        "ai_timeline": None,
        "resolved_archetype": "narrated",
        # Background-sound level this variant rendered with (None → Kria's
        # default). Editable post-gen via the BackgroundSoundControl reburn —
        # persisted here so the editor shows the TRUE current value, not a guess.
        "voiceover_bed_level": bed_level,
        # Media-overlay cards (slice 1) — see montage finalize dict for docs.
        "media_overlays": None,
        "pre_media_overlay_video_path": None,
    }
    try:
        if not voiceover_gcs_path:
            raise ValueError("narrated variant missing voiceover_gcs_path")
        voiceover_local = os.path.join(variant_dir, "voiceover_src")
        download_to_file(voiceover_gcs_path, voiceover_local)
        # Caption accuracy: the narration becomes burned + editable captions, so use
        # the larger narrated model (local backend; flag-gated, kill-switch in config).
        transcript = transcribe_whisper(voiceover_local, model=settings.narrated_whisper_model)

        script_steps = _narrated_script_steps(filming_guide)
        if len(script_steps) >= 2:
            # narrated / narrated_planned with a written script: force-align it to the
            # voiceover. Fewer than two scripted steps falls through to auto-segmentation.
            from app.pipeline.narrated_alignment import align_script_to_voiceover  # noqa: PLC0415

            clip_assignments = _narrated_clip_assignments(
                filming_guide, narrative_order, clip_id_to_local
            )
            if len(clip_assignments) < len(script_steps):
                raise ValueError(
                    f"narrated variant has {len(clip_assignments)} clips for "
                    f"{len(script_steps)} scripted steps"
                )
            step_timings = align_script_to_voiceover(script_steps, transcript.words)
        else:
            # narrated_ready: no pre-written script — auto-segment the voiceover transcript
            # into at most n_clips duration-proportional buckets so each uploaded clip
            # appears at most once and segments stay at sentence granularity.
            from app.pipeline.narrated_alignment import (  # noqa: PLC0415
                contiguous_step_timings,
            )
            from app.pipeline.narrated_assembler import NarratedClip  # noqa: PLC0415
            from app.pipeline.phrase_sequence import split_phrases  # noqa: PLC0415
            from app.tasks.template_orchestrate import _probe_duration  # noqa: PLC0415

            words = transcript.words
            total_s = max((w.end_s for w in words), default=60.0)
            phrases = split_phrases(words, video_duration_s=total_s)
            if len(phrases) < 2:
                raise ValueError(
                    "narrated_ready auto-segmentation produced fewer than two segments"
                )

            # Bucket micro-phrases into at most n_clips segments so each clip shows once.
            # Strategy: fill each bucket until its accumulated duration covers its share
            # of the total speech duration, then start a new bucket.
            ordered_ids = list(narrative_order or list(clip_id_to_local))
            if not ordered_ids:
                raise ValueError("narrated_ready variant has no clips")
            n_clips = len(ordered_ids)
            target_count = max(2, min(n_clips, len(phrases)))

            if len(phrases) > target_count:
                speech_start = phrases[0]["speech_start_s"]
                speech_end = phrases[-1]["speech_end_s"]
                total_speech = max(speech_end - speech_start, 0.1)
                bucket_dur = total_speech / target_count
                buckets: list[dict] = []
                bucket_open = phrases[0].copy()
                for p in phrases[1:]:
                    if (p["speech_end_s"] - bucket_open["speech_start_s"]) >= bucket_dur and len(
                        buckets
                    ) < target_count - 1:
                        buckets.append({**bucket_open, "speech_end_s": bucket_open["speech_end_s"]})
                        bucket_open = p.copy()
                    else:
                        bucket_open = {**bucket_open, "speech_end_s": p["speech_end_s"]}
                buckets.append(bucket_open)
                phrases = buckets

            # Tile the FULL voiceover with contiguous segments (boundaries at each
            # next phrase's speech onset) so the assembled visual timeline matches
            # the voiceover audio's natural timing — otherwise the compressed visual
            # makes the burned captions lead the spoken words. Use the probed audio
            # length as the end so nothing the user said gets cut.
            vo_dur = _probe_duration(voiceover_local) or 0.0
            timeline_end = max(total_s, vo_dur)
            step_timings = contiguous_step_timings(
                [float(p["speech_start_s"]) for p in phrases], timeline_end
            )
            log.info(
                "narrated_ready_segments",
                job_id=job_id,
                n_clips=n_clips,
                n_segments=len(step_timings),
                timeline_end_s=round(timeline_end, 2),
                durations=[round(t.end_s - t.start_s, 2) for t in step_timings],
            )

            # Assign clips 1-to-1 in upload order; cycle only if segments > clips.
            clip_assignments = []
            for i, timing in enumerate(step_timings):
                clip_id = ordered_ids[i % len(ordered_ids)]
                clip_path = clip_id_to_local.get(clip_id)
                if clip_path:
                    clip_assignments.append(
                        NarratedClip(step_id=timing.step_id, clip_path=clip_path)
                    )
            if not clip_assignments:
                raise ValueError("narrated_ready variant: no valid clip paths found")

        final_path = os.path.join(variant_dir, "final.mp4")
        # Caption-free twin (same clips + voice + bed, no burned text) so the
        # creator can edit captions live on the video and reburn just the text.
        base_path = os.path.join(variant_dir, "final_base.mp4")
        caption_cues = assemble_narrated(
            step_timings,
            clip_assignments,
            voiceover_local,
            final_path,
            variant_dir,
            landscape_fit=landscape_fit,
            # Burn the transcribed narration as synced captions (the on-screen
            # text IS the spoken voiceover). Reuses the transcript already
            # computed above — no second Whisper pass.
            transcript=transcript,
            # Original-audio bed under the voice (None → Kria's default level).
            bed_level=bed_level,
            base_output_path=base_path,
            # "sentence" (default) or "word" (qbuilder one-word-at-a-time).
            caption_style=caption_style,
            # Font for the burned captions (registry key; None → default).
            caption_font=caption_font,
        )
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError("narrated variant produced empty output")

        output_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}.mp4"
        output_url = upload_public_read(final_path, output_gcs)
        # Persist the caption-free base + editable cues so the on-video editor +
        # reburn work. Best-effort: a missing base just disables editing (the
        # burned video still plays/downloads). Only when captions actually exist.
        base_gcs: str | None = None
        if caption_cues and os.path.exists(base_path) and os.path.getsize(base_path) > 0:
            base_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_base.mp4"
            upload_public_read(base_path, base_gcs)
        return {
            **base,
            "ok": True,
            "render_status": "ready",
            "video_path": output_gcs,
            "output_url": output_url,
            **(
                {"duration_s": _rendered_duration_s(final_path)}
                if settings.visual_blocks_enabled
                else {}
            ),
            "base_video_path": base_gcs,
            "caption_cues": caption_cues or None,
            # Narrated renders no media-overlay cards (v1), but the finalize whitelist
            # must round-trip these keys so a montage variant can never lose overlays
            # through a shared path. `**base` already carries them (None for narrated);
            # naming them explicitly keeps the byte-identity guard honest.
            "media_overlays": base["media_overlays"],
            "pre_media_overlay_video_path": base["pre_media_overlay_video_path"],
            "narrated_timings": [
                {
                    "step_id": t.step_id,
                    "start_s": t.start_s,
                    "end_s": t.end_s,
                    "confidence": t.confidence,
                }
                for t in step_timings
            ],
        }
    except Exception as exc:
        err = str(exc)[:MAX_ERROR_DETAIL_LEN]
        log.error(
            "generative_narrated_variant_failed",
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


# ── Silence/filler/retake cut (plans/010 T5) ─────────────────────────────────────


class _SilenceCutCache:
    """Per-job once-per-clip cache for the silence-cut stage (plans/010 7A).

    Mirrors `_pretonemap_hdr_clips`' compute-once-per-clip intent, lazily: the
    first variant that needs a clip's cut pays for whisper + silencedetect +
    the CutPlan (and stashes the cut base render under `dir`); every later
    variant reads the same entry, so variants can never disagree on the cut
    timeline. Entries are keyed by the clip's LOCAL path (post pre-tonemap
    repoint) and shaped by `_silence_cut_analysis` — talking_head (T6) shares
    this cache via the same helper.

    Locking is PER KEY (review R3c): the global lock is held only long enough
    to get-or-insert a key's in-flight event; the expensive compute (whisper +
    silencedetect + retake detection) runs OUTSIDE it, so two DIFFERENT clips
    analyzed concurrently never serialize behind each other's network calls.
    Same-key arrivals wait on the key's event and read the shared entry.
    """

    def __init__(self, cache_dir: str) -> None:
        self.dir = cache_dir  # under the job tmpdir; created only on first cut
        self.lock = threading.Lock()
        self.clips: dict[str, dict[str, Any]] = {}
        # key → Event, present only while that key's first compute is in
        # flight; set (and removed) once the entry is published to `clips`.
        self.pending: dict[str, threading.Event] = {}


def _silence_cut_retake_spans(transcript, *, job_id: str) -> list[tuple[int, int]]:  # noqa: ANN001
    """Retake spans for the CutPlan (plans/010 T7 wiring), failure-isolated.

    Behind RETAKE_CUT_ENABLED (own kill switch, independent of
    SILENCE_CUT_ENABLED). Invoked sync — same celery-context pattern as the
    sibling agents in this file (`run_*` + RunContext(job_id=…)). ANY detector
    failure — TerminalError after retries, ValidationError on malformed words,
    anything else — degrades to ZERO retake spans with the
    `retake_detector_failed` event; silence/filler cutting proceeds unharmed
    (plans/010 failure isolation).
    """
    if not settings.retake_cut_enabled:
        return []
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    try:
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.retake_detector import (  # noqa: PLC0415
            RetakeDetectorInput,
            run_retake_detector,
        )

        indexed = [
            {"i": i, "text": w.text, "start_s": w.start_s, "end_s": w.end_s}
            for i, w in enumerate(transcript.words)
        ]
        # Too-short-transcript short-circuit lives INSIDE run_retake_detector
        # (the single floor shared by every entrypoint) — no duplicate here.
        out = run_retake_detector(
            RetakeDetectorInput.model_validate(
                {"words": indexed, "language": transcript.language or ""}
            ),
            ctx=RunContext(job_id=job_id),
        )
        return [(span.start_word, span.end_word) for span in out.retakes]
    except Exception as exc:  # noqa: BLE001 — retakes can never block the base feature
        log.warning("retake_detector_failed", job_id=job_id, error=str(exc)[:200])
        record_pipeline_event("silence_cut", "retake_detector_failed", {"error": str(exc)[:200]})
        return []


def _silence_cut_analysis(
    clip_path: str,
    duration_s: float,
    *,
    job_id: str,
    cache: _SilenceCutCache | None,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """Detection inputs + CutPlan for one clip, computed once per job (7A).

    Returns the per-clip cache entry::

        {"failed": bool, "words": list[Word], "language": str,
         "plan": CutPlan | None, "retake_span_count": int,
         "cut_video_path": str | None}

    ``failed`` True ⇒ transcription/detection blew up — the caller renders
    today's uncut flow (fail-open; the failure is cached too, so sibling
    variants don't re-spend a failing whisper call and can never disagree).
    ``cut_video_path`` is filled by the render path after its first cut encode
    so later variants copy the file instead of re-running ffmpeg.

    ``cache_key`` overrides the cache key (default: ``clip_path``) — the
    talking_head assembler analyzes a pre-capped WAV derived from the spine and
    keys the entry by the SOURCE spine (+cap) so it stays clip-addressed.

    Callers gate has_audio BEFORE calling (eng review 3A — whisper on injected
    digital silence hallucinates); this helper assumes a real audio stream.
    Analysis-scoped pipeline events (rule-2 calibration gate, bailout, retake
    failure) fire here exactly once per clip; per-variant events stay with the
    render functions.
    """
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    def _compute() -> dict[str, Any]:
        entry: dict[str, Any] = {
            "failed": False,
            "words": [],
            "language": "",
            "plan": None,
            "retake_span_count": 0,
            "cut_video_path": None,
        }
        try:
            from app.pipeline.silence_cut import (  # noqa: PLC0415
                BAILOUT_CLIP_TOO_SHORT,
                MIN_CLIP_S,
                SILENCE_CUT_VERBATIM_PROMPT,
                build_cut_plan,
                no_op_plan,
            )
            from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
            from app.services.clip_speech import detect_silences  # noqa: PLC0415

            # P3: below the cutting floor build_cut_plan would bail anyway —
            # return the no-op plan BEFORE spending whisper + silencedetect +
            # retake detection on a clip that can never be cut. `words` stays
            # empty, so consumers MUST caption from their own fallback path
            # (never from this entry).
            if duration_s < MIN_CLIP_S:
                plan = no_op_plan(duration_s, bailout_reason=BAILOUT_CLIP_TOO_SHORT)
                record_pipeline_event(
                    "silence_cut", "silence_cut_bailout", {"reason": plan.bailout_reason}
                )
                entry["plan"] = plan
                return entry

            # Detection runs on the ORIGINAL clip, never the rendered base — the
            # verbatim bias prompt keeps fillers as tokens so rule 1 can see them.
            transcript = transcribe_whisper(
                clip_path, language=None, verbatim_prompt=SILENCE_CUT_VERBATIM_PROMPT
            )
            # d=0.1 (NOT speech_coverage's 0.3 default): the cut path needs short
            # real silences visible to the intersection rule (round 2 / 9A).
            silences = detect_silences(clip_path, min_silence_s=0.1)
            if not silences:
                # Calibration gate visibility: zero silencedetect ranges means
                # rule 2 self-disables inside build_cut_plan (noisy footage —
                # aggressiveness must never scale WITH background noise).
                record_pipeline_event(
                    "silence_cut",
                    "silence_cut_rule2_disabled",
                    {"clip": os.path.basename(clip_path)},
                )
            retake_spans = _silence_cut_retake_spans(transcript, job_id=job_id)
            plan = build_cut_plan(transcript.words, silences, duration_s, retake_spans=retake_spans)
            if plan.bailout_reason:
                # Safety rail tripped → the plan is a no-op; callers render uncut.
                record_pipeline_event(
                    "silence_cut", "silence_cut_bailout", {"reason": plan.bailout_reason}
                )
            entry.update(
                words=list(transcript.words),
                language=transcript.language or "",
                plan=plan,
                retake_span_count=len(retake_spans),
            )
        except Exception as exc:  # noqa: BLE001 — fail-open: worst case is today's uncut render
            log.warning(
                "silence_cut_analysis_failed",
                job_id=job_id,
                clip=os.path.basename(clip_path),
                error=str(exc)[:200],
            )
            record_pipeline_event(
                "silence_cut", "silence_cut_analysis_failed", {"error": str(exc)[:200]}
            )
            entry["failed"] = True
        return entry

    if cache is None:
        return _compute()
    key = cache_key or clip_path
    # Per-key locking (R3c): hold the global lock only to get-or-insert the
    # key's slot. The first arrival computes OUTSIDE the lock (whisper +
    # silencedetect + retakes are seconds of network/CPU) and then publishes;
    # later arrivals for the SAME key wait on its event and read the shared
    # entry — still 1× per clip regardless of variant count (7A), failures
    # cached too. Different keys never block each other.
    with cache.lock:
        hit = cache.clips.get(key)
        if hit is not None:
            return hit
        event = cache.pending.get(key)
        is_owner = event is None
        if is_owner:
            event = threading.Event()
            cache.pending[key] = event
    if not is_owner:
        event.wait()
        with cache.lock:
            # Always present: the owner's finally publishes an entry (a failed
            # one at worst) before setting the event.
            return cache.clips[key]
    entry: dict[str, Any] | None = None
    try:
        entry = _compute()
        return entry
    finally:
        # _compute fail-opens internally and never raises an Exception, but a
        # BaseException (task abort/kill) must still unblock waiters with a
        # failed entry rather than deadlock them on the event.
        if entry is None:
            entry = {
                "failed": True,
                "words": [],
                "language": "",
                "plan": None,
                "retake_span_count": 0,
                "cut_video_path": None,
            }
        with cache.lock:
            cache.clips[key] = entry
            cache.pending.pop(key, None)
        event.set()


def _is_smart_captions_v2(smart_captions: dict[str, str] | None) -> bool:
    if not smart_captions:
        return False
    version = str(smart_captions.get("preset_version") or "").strip().lower()
    preset_id = str(smart_captions.get("preset_id") or "").strip().lower()
    return version in {"v2", f"{preset_id}-v2"}


def _compile_smart_caption_render_plan(
    *,
    cues: list[dict[str, Any]],
    smart_captions: dict[str, str],
    detected_lang: str,
    job_id: str,
) -> tuple[Any | None, dict[str, Any]]:
    """Load the visual pool once and compile a closed-token Smart plan."""

    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.models import Job as _Job  # noqa: PLC0415
    from app.models import PlanItemAsset  # noqa: PLC0415
    from app.smart_edit.compiler import compile_smart_plan  # noqa: PLC0415
    from app.smart_edit.planner import plan_smart_captions  # noqa: PLC0415

    smart_assets: list[dict[str, Any]] = []
    with _sync_session() as db:
        smart_job = db.get(_Job, uuid.UUID(job_id))
        if smart_job is not None and smart_job.content_plan_item_id is not None:
            rows = (
                db.execute(
                    _select(PlanItemAsset).where(
                        PlanItemAsset.plan_item_id == smart_job.content_plan_item_id,
                        PlanItemAsset.status == "ready",
                    )
                )
                .scalars()
                .all()
            )
            smart_assets = [
                {
                    "id": str(row.id),
                    "gcs_path": row.gcs_path,
                    "kind": row.kind,
                    "source_filename": row.source_filename,
                    "duration_s": row.duration_s,
                    "aspect": row.aspect,
                    "analysis": row.analysis or {},
                }
                for row in rows
            ]
    assets_by_id = {str(asset["id"]): asset for asset in smart_assets}
    smart_plan = plan_smart_captions(
        cues,
        preset_id=smart_captions["preset_id"],
        preset_version=smart_captions["preset_version"],
        language=detected_lang,
        assets=smart_assets,
        job_id=job_id,
    )
    if smart_plan is None:
        return None, {}
    compiled = compile_smart_plan(
        smart_plan.document,
        smart_plan.caption_cues,
        assets_by_id=assets_by_id,
    )
    return compiled, {
        "smart_captions_applied": True,
        "smart_edit_document": smart_plan.document.model_dump(mode="json"),
        "smart_compiled_patch": compiled.compiled_patch,
        "smart_planner_versions": {
            **smart_plan.planner_versions,
            "compiler": compiled.compiled_patch["compiler_version"],
        },
        "smart_validation_receipts": {
            "planner": smart_plan.validation_receipt,
            "compiler": compiled.validation_receipt,
        },
        "media_overlays": compiled.media_overlays or None,
        "boundary_effects": compiled.boundary_effects or None,
        "text_elements": compiled.text_elements,
        "text_elements_user_edited": True,
        "text_elements_materialized_from": "smart_captions",
    }


def _smart_music_track_eligible(track: Any) -> bool:
    """Closed production eligibility predicate for the v2 background bed."""

    from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION  # noqa: PLC0415
    from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION  # noqa: PLC0415
    from app.services.music_sections import current_best_section_for_track  # noqa: PLC0415

    config = getattr(track, "track_config", None) or {}
    return bool(
        getattr(track, "analysis_status", None) == "ready"
        and getattr(track, "published_at", None) is not None
        and getattr(track, "archived_at", None) is None
        and str(getattr(track, "audio_gcs_path", "") or "").startswith("music/")
        and getattr(track, "ai_labels", None)
        and getattr(track, "label_version", None) == CURRENT_LABEL_VERSION
        and getattr(track, "best_sections", None)
        and getattr(track, "section_version", None) == CURRENT_SECTION_VERSION
        and current_best_section_for_track(track) is not None
        and config.get("smart_captions_licensed") is True
    )


def _resolve_smart_music_treatment(
    *,
    cues: list[dict[str, Any]],
    audio_intents: list[dict[str, Any]],
    job_id: str,
    variant_id: str,
    duration_s: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve one immutable v2 music treatment under a short Redis lock."""

    import json  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    receipt: dict[str, Any] = {
        "status": "no_bed",
        "eligible_tracks": 0,
        "matcher_invocations": 0,
    }
    if not audio_intents:
        receipt["reason"] = "no_audio_intent"
        return None, receipt
    floor = max(float(intent.get("music_match_min_score") or 7.0) for intent in audio_intents)
    cache_key = f"smart-captions:music-treatment:{job_id}:{variant_id}"
    try:
        import redis as redis_lib  # noqa: PLC0415

        client = redis_lib.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
        lock = client.lock(f"{cache_key}:lock", timeout=60, blocking_timeout=2)
        if not lock.acquire(blocking=True):
            receipt["reason"] = "lock_timeout"
            return None, receipt
    except Exception as exc:  # noqa: BLE001
        receipt["reason"] = f"lock_unavailable:{type(exc).__name__}"
        return None, receipt

    try:
        cached = client.get(cache_key)
        if cached:
            treatment = json.loads(cached)
            receipt.update({"status": "reused", "track_id": treatment.get("track_id")})
            return treatment, receipt

        with _sync_session() as db:
            tracks = list(db.query(MusicTrack).all())
            for track in tracks:
                _ = (
                    track.id,
                    track.title,
                    track.audio_gcs_path,
                    track.duration_s,
                    track.analysis_status,
                    track.published_at,
                    track.archived_at,
                    track.track_config,
                    track.ai_labels,
                    track.label_version,
                    track.best_sections,
                    track.section_version,
                    track.recipe_cached,
                    track.beat_timestamps_s,
                    track.lyrics_cached,
                )
            eligible = [track for track in tracks if _smart_music_track_eligible(track)]
        receipt["eligible_tracks"] = len(eligible)
        if not eligible:
            receipt["reason"] = "empty_eligible_library"
            return None, receipt

        from app.services.music_sections import current_best_section_for_track  # noqa: PLC0415
        from app.tasks.auto_music_orchestrate import _run_music_matcher  # noqa: PLC0415

        spoken_text = " ".join(str(cue.get("text") or "") for cue in cues).strip()
        clip_meta = SimpleNamespace(
            clip_id=variant_id,
            duration_s=duration_s,
            detected_subject=spoken_text[:160],
            hook_text=spoken_text[:160],
            hook_score=7.0,
            energy=5.0,
            summary=spoken_text[:400],
            is_image=False,
        )
        receipt["matcher_invocations"] = 1
        ranked = _run_music_matcher(
            clip_metas=[clip_meta],
            candidate_tracks=eligible,
            n_variants=1,
            job_id=job_id,
        )
        by_id = {track.id: track for track in eligible}
        winner = next(
            (
                (by_id[item["track_id"]], item)
                for item in ranked
                if item.get("track_id") in by_id and float(item.get("score") or 0.0) >= floor
            ),
            None,
        )
        if winner is None:
            receipt["reason"] = "below_match_floor"
            return None, receipt
        track, match = winner
        section = current_best_section_for_track(track)
        if section is None:
            receipt["reason"] = "section_missing_after_match"
            return None, receipt
        intent = audio_intents[0]
        treatment = {
            "track_id": str(track.id),
            "src_gcs_path": str(track.audio_gcs_path),
            "section_start_s": round(float(section[0]), 3),
            "section_end_s": round(float(section[1]), 3),
            "gain_db": float(intent.get("bed_gain_db") or -18.0),
            "matcher_score": float(match["score"]),
            "matcher_rationale": str(match.get("rationale") or ""),
            "minimum_score": floor,
        }
        client.setex(cache_key, 86400, json.dumps(treatment, sort_keys=True))
        receipt.update({"status": "selected", "track_id": track.id})
        return treatment, receipt
    except Exception as exc:  # noqa: BLE001
        receipt["reason"] = f"resolution_failed:{type(exc).__name__}"
        return None, receipt
    finally:
        try:
            lock.release()
        except Exception:  # noqa: BLE001
            pass


def _render_subtitled_variant(
    *,
    job_id: str,
    rank: int,
    spec: dict[str, Any],
    clip_id_to_local: dict[str, str],
    variant_dir: str,
    language: str = "en",
    landscape_fit: str = "fill",
    silence_cut_disabled: bool = False,
    silence_cut_cache: _SilenceCutCache | None = None,
    smart_captions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render the subtitled single-clip variant.

    Lean path (NOT the narrated assembler — no voiceover, no reflow): reframe the ONE
    uploaded clip to 9:16 keeping its OWN audio (LUFS-normalized), transcribe that
    audio (whisper-1 + `language` hint → reliable Turkish + English), and burn editable
    sentence-block captions at the platform-safe MarginV over a cached caption-free
    base. The clip renders 1:1 (no trim/speed) so word/cue times need no clip→assembled
    rebasing. Never raises for a per-variant failure (matches the other render fns) —
    a corrupt clip / empty transcript becomes a failure record, and a no-speech clip
    still ships the clean video (the UI shows the empty-caption state).

    Reuses the narrated caption keys (`voiceover_caption_style` / `voiceover_caption_font`)
    so the finalize whitelist, the on-video CaptionEditor, and the reburn all work
    unchanged. Both caption styles ship: "sentence" (default; pop-in blocks) and "word"
    (line-visible lime word-pop), selected via the item's caption-style toggle.

    Silence/filler/retake cut (plans/010, behind SILENCE_CUT_ENABLED): the ORIGINAL
    clip is transcribed verbatim + silence-scanned, the CutPlan executes inside the
    reframe (`keep_segments` + alternating punch-in), and captions come from the
    remapped transcript minus filler tokens — no second whisper call on the base.
    Every gate/failure falls OPEN to the flag-off flow above; flag off is
    byte-identical to pre-feature behavior (kill-switch pinned).
    """
    from app.pipeline.caption_correct import correct_caption_cues  # noqa: PLC0415
    from app.pipeline.captions import (  # noqa: PLC0415
        build_plain_cues,
        generate_ass_from_cues,
        generate_word_pop_ass,
        resplit_cues_into_sentences,
    )
    from app.pipeline.narrated_assembler import (  # noqa: PLC0415
        burn_captions_on_video,
        resolve_caption_font,
    )
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.reframe import reframe_and_export, resolve_output_fit  # noqa: PLC0415
    from app.pipeline.text_overlay import FONTS_DIR  # noqa: PLC0415
    from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
    from app.storage import upload_public_read  # noqa: PLC0415

    variant_id = spec["variant_id"]
    # Caption style: "word" → word-by-word lime pop (line visible, active word popped);
    # anything else → sentence blocks (the safe default). Reuses the narrated key.
    caption_style = "word" if spec.get("caption_style") == "word" else "sentence"
    # v1: no font chosen at render time — the editor sets it later (None → default
    # TikTok Sans). Reuses the narrated caption-font key so the editor/reburn work.
    caption_font = spec.get("caption_font") or None
    caption_margin_v = _resolve_caption_margin_v(spec)
    smart_v2 = _is_smart_captions_v2(smart_captions)
    smart_render_started = time.monotonic()
    smart_caption_policy: dict[str, Any] | None = None
    if smart_v2 and smart_captions is not None:
        from app.smart_edit.presets import load_preset  # noqa: PLC0415

        smart_caption_policy = load_preset(
            smart_captions["preset_id"], smart_captions["preset_version"]
        ).caption.model_dump(mode="json")
    base = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": "none",
        "music_track_id": None,
        "track_title": None,
        "style_set_id": None,
        "intro_text_size_px": None,
        "intro_size_source": None,
        "intro_text": None,
        "intro_highlight_word": None,
        "intro_layout": None,
        "intro_word_roles": None,
        "intro_mode": None,
        "transcript": None,
        "scenes": None,
        "sequence_base_size_px": None,
        "sequence_mode": None,
        "sequence_quote": None,
        "mix": 1.0,
        "user_style_knobs": None,
        "base_video_path": None,
        # Editable caption cues [{text, start_s, end_s}] (base/clip time). Drives the
        # on-video caption editor; reburned onto base_video_path when the creator edits.
        "caption_cues": None,
        # On/off toggle, independent of cue count — see the narrated base dict above
        # for the full rationale (same field, same semantics for subtitled).
        "captions_enabled": True,
        # The caption style this variant rendered with ("sentence" | "word"). The reburn
        # reads it so edited cues re-burn in the SAME style (word-pop stays word-pop).
        "voiceover_caption_style": caption_style,
        "voiceover_caption_font": caption_font,
        "caption_margin_v": caption_margin_v,
        # Language the captions were transcribed in (ISO "en"/"tr"). Shown as the editor
        # chip; the re-transcribe override reads + rewrites it.
        "caption_language": (language or "en"),
        "ai_timeline": None,
        "resolved_archetype": "subtitled",
        "media_overlays": None,
        "pre_media_overlay_video_path": None,
        "sound_effects": None,
        "pre_sfx_video_path": None,
        "smart_captions_applied": False,
        "smart_edit_document": None,
        "smart_compiled_patch": None,
        "smart_planner_versions": None,
        "smart_validation_receipts": None,
        "smart_caption_policy": smart_caption_policy,
        "smart_music_treatment": None,
        "smart_audio_receipt": None,
        "boundary_effects": None,
        "text_elements_materialized_from": None,
        # Silence-cut summary {removed, time_saved_s, version} (plans/010) — set
        # only when the stage ran to a plan; drives the admin cut-plan viewer.
        "silence_cut": None,
    }
    if getattr(settings, "subtitled_text_lane_enabled", False) or smart_captions is not None:
        base["text_elements"] = []
        base["text_elements_user_edited"] = False
    try:
        # Subtitled is single-clip: the first uploaded clip (order-preserving). The
        # uploader caps new subtitled items at ONE clip, but an item switched from
        # montage can carry more — record the drop so admin job-debug explains why
        # only one clip appears (never a silent mystery).
        clip_path = next(iter(clip_id_to_local.values()), None)
        if not clip_path:
            raise ValueError("subtitled variant has no clip")
        if len(clip_id_to_local) > 1:
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            record_pipeline_event(
                "assembly",
                "subtitled_extra_clips_ignored",
                {"clip_count": len(clip_id_to_local), "used_clip": next(iter(clip_id_to_local))},
            )
            log.info(
                "subtitled_extra_clips_ignored",
                job_id=job_id,
                clip_count=len(clip_id_to_local),
            )

        probe = probe_video(clip_path)
        # Cap sized to the CAPTION-TASK budget, not just whisper's 25MB (~13 min):
        # every Apply/re-transcribe re-encodes the full clip at preset=fast inside a
        # soft_time_limit=600 task (~realtime on the 4-vCPU worker). A longer clip
        # renders once, then every caption edit times out forever. 5 min also stays
        # aligned with the sub-60s product target. Fail fast with an actionable reason.
        if float(probe.duration_s) > 300.0:
            raise ValueError(
                "subtitled clips are capped at 5 minutes — trim the clip and re-upload"
            )
        aspect = "16:9" if getattr(probe, "aspect_ratio", "") == "16:9" else "9:16"
        fit = resolve_output_fit(probe, landscape_fit=landscape_fit)

        # ── Silence/filler/retake cut (plans/010 T5) ────────────────────────────
        # Detection runs on the ORIGINAL clip BEFORE the reframe. The base renders
        # start=0 / full duration / speed 1.0, so clip timeline == base timeline:
        # the plan's keep_segments apply directly inside the reframe and the
        # remapped word times are natively base-relative. Every gate below fails
        # OPEN to the flag-off flow — this stage may shorten the video, never
        # fail the job.
        sc_entry: dict[str, Any] | None = None  # per-clip cache entry (7A)
        sc_words: list | None = None  # verbatim original-clip words for captions
        sc_language = ""
        sc_plan = None  # CutPlan captions remap against (no-op when nothing cut)
        sc_apply = False  # True ⇒ pass keep_segments into the reframe
        if settings.silence_cut_enabled:
            from app.pipeline.silence_cut import (  # noqa: PLC0415
                KEEP_SEGMENTS_PUNCH_IN,
                is_filler_token,
                plan_event_payload,
                plan_summary,
                remap_words,
            )
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            if silence_cut_disabled:
                # Per-item opt-out (10A) — skips the WHOLE stage, retakes included.
                record_pipeline_event(
                    "silence_cut", "silence_cut_skipped_disabled", {"variant_id": variant_id}
                )
            elif not probe.has_audio:
                # No real audio stream: the reframe injects silent AAC, and whisper
                # on digital silence hallucinates plausible words — skip BEFORE any
                # ASR call (eng review 3A).
                record_pipeline_event(
                    "silence_cut", "silence_cut_skipped_no_audio", {"variant_id": variant_id}
                )
            else:
                sc_entry = _silence_cut_analysis(
                    clip_path, float(probe.duration_s), job_id=job_id, cache=silence_cut_cache
                )
                # `words` must be non-empty to adopt the verbatim transcript:
                # an empty-words bailout (e.g. clip_too_short — P3 returns
                # before whisper runs) must caption from the base-transcription
                # fallback below, NOT produce zero cues.
                if not sc_entry["failed"] and sc_entry["words"]:
                    sc_words = sc_entry["words"]
                    sc_language = sc_entry["language"]
                    sc_plan = sc_entry["plan"]
                    # A bailed-out plan is a no-op (render uncut); a clean plan
                    # with zero removals skips the segmented encode too. Captions
                    # still come from the already-paid-for verbatim transcript.
                    sc_apply = sc_plan.bailout_reason is None and bool(sc_plan.removed)

        # V2 prerequisites are deliberately resolved before the only reframe
        # encode. The original clip and the reframe share a 1:1 timeline unless
        # silence-cut is active; that path already supplies exactly remapped words.
        detected_lang = language or "en"
        cues: list[dict[str, Any]] = []
        smart_compiled = None
        if smart_v2:
            if sc_words is not None:
                from app.pipeline.transcribe import Word  # noqa: PLC0415

                caption_words = [
                    Word(
                        text=word["text"],
                        start_s=word["start_s"],
                        end_s=word["end_s"],
                        confidence=1.0,
                    )
                    for word in remap_words(sc_words, sc_plan)
                    if not is_filler_token(word["text"])
                ]
                detected_lang = sc_language or detected_lang
                cues = build_plain_cues(caption_words, attach_words=True)
            else:
                transcript = transcribe_whisper(clip_path, language=None)
                detected_lang = transcript.language or detected_lang
                cues = build_plain_cues(transcript.words, attach_words=True)
            cues = correct_caption_cues(
                cues,
                detected_lang,
                model=settings.caption_correction_model,
                enabled=settings.subtitled_caption_correction_enabled,
            )
            cues = resplit_cues_into_sentences(cues)
            if smart_captions is not None and cues:
                try:
                    smart_compiled, smart_state = _compile_smart_caption_render_plan(
                        cues=cues,
                        smart_captions=smart_captions,
                        detected_lang=detected_lang,
                        job_id=job_id,
                    )
                    if smart_compiled is not None:
                        cues = smart_compiled.caption_cues
                        base.update(smart_state)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "smart_captions_v2_prerequisites_failed_open",
                        job_id=job_id,
                        variant_id=variant_id,
                        error=str(exc)[:300],
                    )

        # Caption-free 9:16 base with the clip's own audio (LUFS-normalized). start=0,
        # end=duration, speed=1.0 → base timeline == clip timeline, so transcript word
        # times map directly onto the base (no rebasing needed).
        base_path = os.path.join(variant_dir, "final_base.mp4")
        reframe_kwargs: dict[str, Any] = {}
        if sc_apply:
            # The punch factor comes from silence_cut's constant (user-approved
            # jump-cut idiom) — NEVER a literal here, so every render path cuts
            # in the same style from one source of truth.
            reframe_kwargs = {
                "keep_segments": sc_plan.keep_segments,
                "keep_segments_punch_in": KEEP_SEGMENTS_PUNCH_IN,
            }
        if smart_compiled is not None and smart_compiled.camera_intents:
            reframe_kwargs["semantic_crop_pulses"] = smart_compiled.camera_intents
        # Cut-output reuse (7A): a sibling variant may have already paid for the
        # cut encode of this exact clip — copy it instead of re-running ffmpeg.
        cut_reused = False
        if (
            sc_apply
            and silence_cut_cache is not None
            and "semantic_crop_pulses" not in reframe_kwargs
        ):
            cached_cut = sc_entry.get("cut_video_path")
            if cached_cut and os.path.exists(cached_cut):
                shutil.copy2(cached_cut, base_path)
                cut_reused = True
        if not cut_reused:
            try:
                reframe_and_export(
                    clip_path,
                    0.0,
                    float(probe.duration_s),
                    aspect,
                    None,  # no ASS at this stage — captions are burned in a second pass
                    base_path,
                    output_fit=fit,
                    has_audio=probe.has_audio,
                    **reframe_kwargs,
                )
            except Exception as exc:
                if reframe_kwargs.pop("semantic_crop_pulses", None) is not None:
                    if os.path.exists(base_path):
                        os.remove(base_path)
                    try:
                        reframe_and_export(
                            clip_path,
                            0.0,
                            float(probe.duration_s),
                            aspect,
                            None,
                            base_path,
                            output_fit=fit,
                            has_audio=probe.has_audio,
                            **reframe_kwargs,
                        )
                        base["smart_validation_receipts"]["camera_render"] = {
                            "requested": len(smart_compiled.camera_intents),
                            "applied": 0,
                            "status": "retry_without_camera",
                            "error": str(exc)[:160],
                            "full_video_encode_count": 1,
                        }
                        exc = None
                    except Exception as retry_exc:  # noqa: BLE001
                        exc = retry_exc
                    if exc is None:
                        pass
                    elif not sc_apply:
                        raise exc
                if exc is None:
                    pass
                elif not sc_apply:
                    raise
                # Fail-open on the CUT apply (R3a, mirroring talking_head's
                # uncut retry): a segment-filter failure must cost the cuts,
                # never the variant. Clear every plan-derived state so captions
                # fall back to the base-transcription path below (the remapped
                # verbatim words describe a cut timeline that no longer
                # exists), drop the partial output, and re-run the reframe
                # WITHOUT keep_segments. No summary is persisted on this path —
                # a removed[] blob on an uncut video lies to the admin viewer.
                if exc is not None:
                    log.warning(
                        "silence_cut_apply_failed",
                        job_id=job_id,
                        variant_id=variant_id,
                        error=str(exc)[:200],
                    )
                    record_pipeline_event(
                        "silence_cut",
                        "silence_cut_apply_failed",
                        {"variant_id": variant_id, "error": str(exc)[:200]},
                    )
                    sc_apply = False
                    sc_plan = None
                    sc_words = None
                    sc_language = ""
                    sc_entry = None
                    if os.path.exists(base_path):
                        os.remove(base_path)
                    reframe_and_export(
                        clip_path,
                        0.0,
                        float(probe.duration_s),
                        aspect,
                        None,
                        base_path,
                        output_fit=fit,
                        has_audio=probe.has_audio,
                    )
            else:
                if smart_compiled is not None and smart_compiled.camera_intents:
                    base["smart_validation_receipts"]["camera_render"] = {
                        "requested": len(smart_compiled.camera_intents),
                        "applied": len(smart_compiled.camera_intents),
                        "status": "applied_in_reframe",
                        "full_video_encode_count": 1,
                    }
        if not os.path.exists(base_path) or os.path.getsize(base_path) == 0:
            raise RuntimeError("subtitled base render produced empty output")
        if sc_apply and not cut_reused and silence_cut_cache is not None:
            # Stash the cut base for sibling variants (best-effort — a copy
            # failure only costs the next variant a re-encode, never the job).
            try:
                os.makedirs(silence_cut_cache.dir, exist_ok=True)
                cached_cut = os.path.join(
                    silence_cut_cache.dir, f"{os.path.basename(clip_path)}.cut.mp4"
                )
                # Same filesystem (both live under the job tmpdir): a hardlink
                # is free — no byte copy. Fall back to a real copy if the FS
                # refuses the link (P2).
                try:
                    os.link(base_path, cached_cut)
                except OSError:
                    shutil.copy2(base_path, cached_cut)
                sc_entry["cut_video_path"] = cached_cut
            except OSError as exc:
                log.warning("silence_cut_cache_store_failed", job_id=job_id, error=str(exc))

        if not smart_v2 and sc_words is not None:
            # NO second transcription (plans/010): cues come from the verbatim
            # original-clip transcript remapped into the cut timeline (exact
            # arithmetic — see silence_cut.remap_words), MINUS every lexicon
            # filler token. Caption hygiene (15A): fillers never reach captions
            # even when they were NOT cut from the video (e.g. blocked by the
            # segment-signal guard or below MIN_CUT_S).
            from app.pipeline.transcribe import Word  # noqa: PLC0415

            caption_words = [
                Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
                for w in remap_words(sc_words, sc_plan)
                if not is_filler_token(w["text"])
            ]
            detected_lang = sc_language or (language or "en")
            cues = build_plain_cues(caption_words, attach_words=True)
        elif not smart_v2:
            # Flag-off / gated / analysis-failed path — today's flow, unchanged.
            # Subtitled captions the SPOKEN language of the clip: auto-detect
            # (language=None), NOT the plan's content language — a Turkish clip must
            # get Turkish captions even in an English plan. The user can still
            # override the detected language via the D5 chip (re-transcribe).
            # Persist the detected language so the chip shows it.
            transcript = transcribe_whisper(base_path, language=None)
            detected_lang = transcript.language or (language or "en")
            # Word mode attaches each cue's real per-word timings so the highlight
            # (and any reburn of an UNedited cue) stays locked to the audio.
            # Always attach real word timings (both styles): the sentence re-split
            # lands boundaries on word times when the correction leaves a cue
            # untouched, and the word-pop burn keeps its highlight audio-locked
            # from the carried words.
            cues = build_plain_cues(transcript.words, attach_words=True)
        # Fix whisper's spelling/grammar mishearings (esp. Turkish morphology) while
        # keeping cue timing. Best-effort — a failure leaves the raw cues.
        if not smart_v2:
            cues = correct_caption_cues(
                cues,
                detected_lang,
                model=settings.caption_correction_model,
                enabled=settings.subtitled_caption_correction_enabled,
            )
        # One SENTENCE per caption: whisper emits no punctuation (14-word chunk cues);
        # the correction adds it — re-split so captions show one sentence at a time
        # instead of a stacked 4-line block.
        if not smart_v2:
            cues = resplit_cues_into_sentences(cues)

        # Smart Captions semantic pass. It operates on the corrected, final cue
        # text and its word timings, then compiles only closed tokens into Nova's
        # existing caption/text/SFX lanes. Any planner/compiler bug fails open to
        # the normal subtitled render — the feature may add polish, never block
        # the creator's base video.
        if not smart_v2:
            smart_compiled = None
        if not smart_v2 and smart_captions is not None and cues:
            try:
                smart_compiled, smart_state = _compile_smart_caption_render_plan(
                    cues=cues,
                    smart_captions=smart_captions,
                    detected_lang=detected_lang,
                    job_id=job_id,
                )
                if smart_compiled is not None:
                    cues = smart_compiled.caption_cues
                    base.update(smart_state)
            except Exception as exc:  # noqa: BLE001 — Smart polish fails open
                log.warning(
                    "smart_captions_plan_failed",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )

        if smart_compiled is not None:
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            record_pipeline_event(
                "smart_captions",
                "plan_compiled",
                {
                    "events": len((base.get("smart_edit_document") or {}).get("events") or []),
                    "styled_captions": smart_compiled.validation_receipt["styled_caption_count"],
                    "titles": len(smart_compiled.text_elements),
                    "sfx_intents": len(smart_compiled.sfx_intents),
                    "boundary_effects": len(smart_compiled.boundary_effects),
                    "visuals": len(smart_compiled.media_overlays),
                    "camera_intents": len(getattr(smart_compiled, "camera_intents", [])),
                },
            )

        if smart_compiled is not None and smart_compiled.boundary_effects:
            try:
                from app.pipeline.boundary_effects import apply_boundary_effects  # noqa: PLC0415

                boundary_base = os.path.join(variant_dir, "base_with_boundaries.mp4")
                apply_boundary_effects(
                    base_path,
                    smart_compiled.boundary_effects,
                    boundary_base,
                )
                base_path = boundary_base
                base["smart_validation_receipts"]["boundary_render"] = {
                    "requested": len(smart_compiled.boundary_effects),
                    "applied": len(smart_compiled.boundary_effects),
                    "status": "applied",
                }
            except Exception as exc:  # noqa: BLE001 — captions still ship
                log.warning(
                    "smart_boundary_effects_failed_open",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )
                base["boundary_effects"] = None
                base["smart_validation_receipts"]["boundary_render"] = {
                    "requested": len(smart_compiled.boundary_effects),
                    "applied": 0,
                    "status": "failed_open",
                    "error": str(exc)[:160],
                }

        final_path = os.path.join(variant_dir, "final.mp4")
        if getattr(settings, "subtitled_text_lane_enabled", False) or smart_compiled is not None:
            final_path = _compose_subtitled_final(
                base_path,
                {
                    **base,
                    "caption_cues": cues or None,
                    "caption_language": detected_lang,
                },
                variant_dir,
            )
        elif cues:
            ass_path = os.path.join(variant_dir, "captions.ass")
            ass_font = resolve_caption_font(caption_font)
            if caption_style == "word":
                generate_word_pop_ass(cues, ass_path, font_name=ass_font, margin_v=caption_margin_v)
            else:
                generate_ass_from_cues(
                    cues,
                    ass_path,
                    font_name=ass_font,
                    style="plain",
                    margin_v=caption_margin_v,
                    pop_in=True,
                    **(
                        {"smart_policy": smart_caption_policy}
                        if smart_caption_policy is not None
                        else {}
                    ),
                )
            burn_captions_on_video(base_path, ass_path, FONTS_DIR, final_path)
        else:
            # No detectable speech → ship the clean clip; the UI shows the empty-caption
            # state. NOT a failure — a caption-less talking clip is still valid output.
            shutil.copy2(base_path, final_path)
        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError("subtitled variant produced empty output")

        output_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}.mp4"
        output_url: str | None = None
        rendered_gcs: str | None = None
        sound_effects: list[dict[str, Any]] = []
        pre_sfx_gcs: str | None = None
        pre_media_gcs: str | None = None
        sound_design_auto = bool(
            smart_captions is not None and smart_captions.get("sound_design", "auto") == "auto"
        )
        if smart_compiled is not None and settings.sound_effects_enabled and sound_design_auto:
            try:
                from sqlalchemy import select as _select  # noqa: PLC0415

                from app.models import SoundEffect  # noqa: PLC0415
                from app.smart_edit.compiler import resolve_sfx_placements  # noqa: PLC0415

                with _sync_session() as db:
                    glossary = [
                        {
                            "id": row.id,
                            "name": row.name,
                            "audio_gcs_path": row.audio_gcs_path,
                            "duration_s": row.duration_s,
                            "role_tags": row.role_tags or [],
                            "contains_voice": row.contains_voice,
                            "vocal_probability": row.vocal_probability,
                            "manual_audit_status": row.manual_audit_status,
                            "quality_tier": row.quality_tier,
                        }
                        for row in db.execute(
                            _select(SoundEffect).where(
                                SoundEffect.status == "ready",
                                SoundEffect.audio_gcs_path.is_not(None),
                                SoundEffect.published_at.is_not(None),
                                SoundEffect.archived_at.is_(None),
                            )
                        )
                        .scalars()
                        .all()
                    ]
                sound_effects = resolve_sfx_placements(
                    smart_compiled.sfx_intents,
                    glossary,
                    preset_id=smart_captions["preset_id"],
                    preset_version=smart_captions["preset_version"],
                )
                base["smart_validation_receipts"]["sfx_resolution"] = {
                    "requested": len(smart_compiled.sfx_intents),
                    "resolved": len(sound_effects),
                    "unresolved_roles": sorted(
                        {str(intent.get("role")) for intent in smart_compiled.sfx_intents}
                        - {str(placement.get("smart_role")) for placement in sound_effects}
                    ),
                }
            except Exception as exc:  # noqa: BLE001 — visuals/captions still ship
                log.warning(
                    "smart_captions_sfx_resolution_failed_open",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )
                sound_effects = []
                base["smart_validation_receipts"]["sfx_resolution"] = {
                    "requested": len(smart_compiled.sfx_intents),
                    "resolved": 0,
                    "status": "failed_open",
                    "error": str(exc)[:160],
                }
        elif smart_compiled is not None:
            base["smart_validation_receipts"]["sfx_resolution"] = {
                "requested": len(smart_compiled.sfx_intents),
                "resolved": 0,
                "status": "disabled",
            }

        music_treatment: dict[str, Any] | None = None
        if smart_v2 and smart_compiled is not None and sound_design_auto:
            music_treatment, music_receipt = _resolve_smart_music_treatment(
                cues=cues,
                audio_intents=smart_compiled.audio_treatment_intents,
                job_id=job_id,
                variant_id=variant_id,
                duration_s=float(probe.duration_s),
            )
            base["smart_music_treatment"] = music_treatment
            base["smart_validation_receipts"]["music_resolution"] = music_receipt
        elif smart_v2 and smart_compiled is not None:
            base["smart_validation_receipts"]["music_resolution"] = {"status": "disabled"}

        protected_boxes: list[dict[str, float]] = []
        if smart_v2 and smart_compiled is not None:
            from app.pipeline.render_geometry import sample_face_boxes  # noqa: PLC0415
            from app.pipeline.text_overlay_skia import measure_text_overlay_box  # noqa: PLC0415

            protected_boxes.extend(
                dict(box) for cue in cues if isinstance((box := cue.get("smart_render_box")), dict)
            )
            for overlay in _text_element_burn_dicts(base):
                protected_boxes.append(measure_text_overlay_box(overlay))
            anchor_times = [
                float(intent.get("at_s") or 0.0) for intent in smart_compiled.camera_intents
            ]
            anchor_times.extend(
                float(event.get("start_s") or 0.0) for event in smart_compiled.media_overlays
            )
            face_boxes, face_receipt = sample_face_boxes(base_path, anchor_times)
            protected_boxes.extend(box.as_dict() for box in face_boxes)
            base["smart_validation_receipts"]["geometry_prepare"] = {
                **face_receipt,
                "caption_boxes": sum(isinstance(cue.get("smart_render_box"), dict) for cue in cues),
                "title_boxes": len(base.get("text_elements") or []),
                "protected_boxes": len(protected_boxes),
            }

        # Visuals and sound share the semantic plan. Render them in a fixed
        # order: captions/text -> visual pool -> SFX. Each lane fails open to
        # the last good artifact, never to a foreign/source video's audio.
        media_cards = None
        if smart_compiled is not None and smart_compiled.media_overlays:
            try:
                from app.agents._schemas.media_overlay import coerce_media_overlays  # noqa: PLC0415

                media_cards = coerce_media_overlays(smart_compiled.media_overlays)
            except Exception:  # noqa: BLE001
                media_cards = None
        if media_cards and settings.media_overlays_enabled:
            try:
                from app.pipeline.media_overlay import apply_media_overlays  # noqa: PLC0415

                pre_media_gcs = (
                    f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_pre_media.mp4"
                )
                upload_public_read(final_path, pre_media_gcs)
                media_target = (
                    f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_pre_sfx.mp4"
                    if sound_effects or music_treatment
                    else output_gcs
                )
                layout_receipts: list[dict[str, Any]] = []
                output_url = apply_media_overlays(
                    pre_media_gcs,
                    media_cards,
                    media_target,
                    job_id=job_id,
                    protected_boxes=protected_boxes or None,
                    layout_receipt_out=layout_receipts,
                )
                rendered_gcs = media_target
                base["pre_media_overlay_video_path"] = pre_media_gcs
                base["media_overlays"] = [
                    card.model_dump(exclude_none=True) for card in media_cards
                ]
                base["smart_validation_receipts"]["visual_render"] = {
                    "requested": len(media_cards),
                    "applied": len(media_cards),
                    "status": "applied",
                    "layout": layout_receipts,
                }
            except Exception as exc:  # noqa: BLE001 — continue from captioned video
                log.warning(
                    "smart_captions_media_failed_open",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )
                base["media_overlays"] = None
                base["pre_media_overlay_video_path"] = None
                base["smart_validation_receipts"]["visual_render"] = {
                    "requested": len(media_cards),
                    "applied": 0,
                    "status": "failed_open",
                    "error": str(exc)[:160],
                }
        elif media_cards:
            base["smart_validation_receipts"]["visual_render"] = {
                "requested": len(media_cards),
                "applied": 0,
                "status": "disabled",
            }

        if sound_effects or music_treatment:
            try:
                from app.agents._schemas.sound_effect import coerce_sound_effects  # noqa: PLC0415
                from app.pipeline.sound_effects import (  # noqa: PLC0415
                    apply_smart_audio_treatment,
                    apply_sound_effects,
                )

                if rendered_gcs is None:
                    pre_sfx_gcs = (
                        f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_pre_sfx.mp4"
                    )
                    upload_public_read(final_path, pre_sfx_gcs)
                else:
                    pre_sfx_gcs = rendered_gcs
                placements = coerce_sound_effects(sound_effects) or []
                if smart_v2:
                    output_url, audio_receipt = apply_smart_audio_treatment(
                        pre_sfx_gcs,
                        placements,
                        output_gcs,
                        music_bed=music_treatment,
                        job_id=job_id,
                    )
                    base["smart_audio_receipt"] = audio_receipt
                else:
                    output_url = apply_sound_effects(
                        pre_sfx_gcs,
                        placements,
                        output_gcs,
                        job_id=job_id,
                    )
                    audio_receipt = {
                        "final_tier": "sfx_only",
                        "video_codec": "copy",
                    }
                rendered_gcs = output_gcs
                base["smart_validation_receipts"]["sfx_render"] = {
                    "requested": len(sound_effects),
                    "applied": len(sound_effects),
                    "status": "applied",
                    "audio": audio_receipt,
                }
            except Exception as exc:  # noqa: BLE001 — keep last good visual artifact
                failed_sfx_count = len(sound_effects)
                log.warning(
                    "smart_captions_sfx_failed_open",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )
                sound_effects = []
                base["smart_validation_receipts"]["sfx_render"] = {
                    "requested": failed_sfx_count,
                    "applied": 0,
                    "status": "failed_open",
                    "error": str(exc)[:160],
                }

        if rendered_gcs is None or output_url is None:
            rendered_gcs = output_gcs
            output_url = upload_public_read(final_path, output_gcs)
        # Persist the caption-free base when captions exist. With the text lane on,
        # persist it even for cue-less clips so later user-authored text can fast-reburn.
        base_gcs: str | None = None
        should_upload_base = bool(cues) or getattr(settings, "subtitled_text_lane_enabled", False)
        if should_upload_base and os.path.exists(base_path) and os.path.getsize(base_path) > 0:
            base_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_base.mp4"
            upload_public_read(base_path, base_gcs)

        # Silence-cut summary (plans/010): persisted whenever the stage ran to a
        # non-bailout plan (zero-removal plans included — "nothing to cut" is
        # information). `plan_summary`/`plan_event_payload` are the single
        # source of truth for both shapes (shared with talking_head — M2/M6);
        # plain dicts land in Job.assembly_plan JSON and the admin cut-plan
        # viewer (T9) renders removed[] directly. Bailouts are event-only (the
        # silence_cut_bailout event carries the reason).
        silence_cut_summary: dict[str, Any] | None = None
        if sc_plan is not None and sc_plan.bailout_reason is None:
            silence_cut_summary = plan_summary(sc_plan, original_duration_s=float(probe.duration_s))
            record_pipeline_event(
                "silence_cut",
                "silence_cut_plan",
                plan_event_payload(
                    sc_plan,
                    variant_id=variant_id,
                    retake_spans=sc_entry["retake_span_count"] if sc_entry else 0,
                    applied=sc_apply,
                    cut_reused=cut_reused,
                ),
            )
        if smart_v2 and base.get("smart_validation_receipts") is not None:
            try:
                import resource  # noqa: PLC0415

                peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            except Exception:  # noqa: BLE001
                peak_rss = None
            base["smart_validation_receipts"]["performance"] = {
                "wall_time_ms": round((time.monotonic() - smart_render_started) * 1000),
                "reframe_full_video_encode_count": 1,
                "camera_additional_full_video_encodes": 0,
                "audio_video_codec": "copy" if sound_effects or music_treatment else None,
                "peak_rss_platform_units": peak_rss,
            }
        return {
            **base,
            "ok": True,
            "render_status": "ready",
            "video_path": rendered_gcs,
            "output_url": output_url,
            **(
                {"duration_s": _rendered_duration_s(final_path)}
                if settings.visual_blocks_enabled
                else {}
            ),
            "base_video_path": base_gcs,
            "caption_cues": cues or None,
            # The language actually spoken/detected (not the plan language).
            "caption_language": detected_lang,
            "media_overlays": base["media_overlays"],
            "pre_media_overlay_video_path": base["pre_media_overlay_video_path"],
            "sound_effects": sound_effects or None,
            "pre_sfx_video_path": pre_sfx_gcs,
            "smart_captions_applied": base["smart_captions_applied"],
            "smart_edit_document": base["smart_edit_document"],
            "smart_compiled_patch": base["smart_compiled_patch"],
            "smart_planner_versions": base["smart_planner_versions"],
            "smart_validation_receipts": base["smart_validation_receipts"],
            "smart_caption_policy": base["smart_caption_policy"],
            "smart_music_treatment": base["smart_music_treatment"],
            "smart_audio_receipt": base["smart_audio_receipt"],
            "boundary_effects": base["boundary_effects"],
            "text_elements": base.get("text_elements"),
            "text_elements_user_edited": base.get("text_elements_user_edited"),
            "text_elements_materialized_from": base.get("text_elements_materialized_from"),
            "silence_cut": silence_cut_summary,
        }
    except Exception as exc:
        err = str(exc)[:MAX_ERROR_DETAIL_LEN]
        log.error(
            "generative_subtitled_variant_failed",
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


@celery_app.task(
    name="reburn_narrated_captions",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    # Caption reburn re-encodes the (already-mixed) base, then may inline-run the
    # overlay+SFX reapply chain (plan 010) — the overlay pass alone budgets a 600s
    # subprocess timeout, so this rides the standard render ceiling (5A). Bounded
    # under the broker visibility_timeout (1900s) like the other render tasks.
    soft_time_limit=1740,
    time_limit=1800,
)
def reburn_narrated_captions(
    self, job_id: str, variant_id: str, render_gen_id: str | None = None
) -> None:
    """Reburn a narrated variant's (hand-edited) caption cues onto its caption-free base.

    Apply step for the on-video caption editor: reads the variant's persisted
    `caption_cues` + `base_video_path`, burns the cues with libass onto the base
    (clips + voice + bed, no old text), uploads to a NEW key (so caches/old links
    don't serve stale captions), swaps in `video_path`, and re-applies any
    persisted SFX/overlay lanes on top (plan 010). A failure reverts the variant
    to `ready` keeping its last-good video.
    """
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        terminal_state = {"accepted": False}
        try:
            _run_reburn_narrated_captions(
                job_id, variant_id, render_gen_id=render_gen_id, terminal_state=terminal_state
            )
        except OperationalError:
            raise
        except Exception as exc:
            log.error(
                "narrated_caption_reburn_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc)[:MAX_ERROR_DETAIL_LEN],
                exc_info=True,
            )
            if terminal_state["accepted"]:
                # F5: the video swap already landed — an exception past that point
                # means the persisted lanes are missing from the new video, so
                # "ready" would lie. Token-gated like every terminal write.
                _update_variant_entry(
                    job_id,
                    variant_id,
                    {"render_status": "failed", "render_error": str(exc)[:500]},
                    expected_render_gen_id=render_gen_id,
                    outcome="caption_reburn_failed_post_swap",
                )
            else:
                # Keep the last-good burned video; just clear the in-flight state.
                _update_variant_entry(
                    job_id,
                    variant_id,
                    {"render_status": "ready"},
                    expected_render_gen_id=render_gen_id,
                    outcome="caption_reburn_failed",
                )


# Archetypes whose editable caption cues may be reburned onto a caption-free base.
# Defense-in-depth: any OTHER archetype (e.g. an agent_text montage) also carries a
# base_video_path, and burning subtitles over it is corruption — so the reburn hard-
# rejects anything not in this set.
_CAPTION_REBURN_ARCHETYPES = frozenset({"narrated", "subtitled"})

# Languages the subtitled caption override accepts (whisper-1 handles both well with an
# explicit hint). Keep in lockstep with the route + the request Literal.
_SUBTITLED_CAPTION_LANGUAGES = frozenset({"en", "tr"})


def _resolve_caption_margin_v(variant: dict) -> int:
    """Resolve a subtitled variant's ASS MarginV.

    Absent/null/invalid values fall back to the legacy subtitled safe-zone margin so
    old variants keep byte-identical ASS geometry. Valid persisted values are bounded
    to the UI/API's 0.30-0.90 y_frac range: round((1 - y_frac) * 1920).
    """
    from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V  # noqa: PLC0415

    raw = variant.get("caption_margin_v")
    if raw is None:
        return SUBTITLED_CAPTION_MARGIN_V
    try:
        margin_v = int(raw)
    except (TypeError, ValueError):
        return SUBTITLED_CAPTION_MARGIN_V
    min_margin = round((1.0 - 0.90) * 1920)
    max_margin = round((1.0 - 0.30) * 1920)
    if min_margin <= margin_v <= max_margin:
        return margin_v
    return SUBTITLED_CAPTION_MARGIN_V


def _fresh_variant_snapshot(job_id: str, variant_id: str) -> dict | None:
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return None
        variants = (job.assembly_plan or {}).get("variants") or []
        fresh = next((v for v in variants if v.get("variant_id") == variant_id), None)
        return dict(fresh) if isinstance(fresh, dict) else None


def _text_element_burn_dicts(variant: dict) -> list[dict]:
    from app.agents._schemas.text_element import coerce_text_elements  # noqa: PLC0415
    from app.pipeline.generative_overlays import build_overlays_from_text_elements  # noqa: PLC0415

    elements = coerce_text_elements(variant.get("text_elements") or []) or []
    # Lyrics-as-optional-elements: on a `lyrics_baked=False` variant, saved
    # `role=lyric_line` elements are ordinary burnable elements (they were
    # accepted at write time by `validate_text_elements_payload`), so burn
    # them like any other element. Legacy (lyrics_baked True/absent) variants
    # keep the historical exclusion — lyric_line there is only ever a
    # read-only projection, never a real persisted element.
    include_lyric_line = variant.get("lyrics_baked") is False
    if not include_lyric_line:
        elements = [elem for elem in elements if getattr(elem, "role", None) != "lyric_line"]
    if not elements:
        return []
    overlays = build_overlays_from_text_elements(
        elements,
        video_duration_s=float(variant.get("duration_s") or 10.0),
        include_lyric_line=include_lyric_line,
        independent_box_alignment=True,
    )
    schedules = {
        (element.text, round(float(element.start_s), 3)): params["reveal_schedule_s"]
        for element in elements
        if isinstance((params := element.source_params), dict)
        and isinstance(params.get("reveal_schedule_s"), list)
    }
    for overlay in overlays:
        key = (
            str(overlay.get("text") or ""),
            round(float(overlay.get("start_s") or 0.0), 3),
        )
        if key in schedules and overlay.get("effect") == "typewriter":
            overlay["reveal_schedule_s"] = schedules[key]
    return overlays


def _compose_subtitled_final(base_local: str, variant: dict, tmpdir: str) -> str:
    """Compose a subtitled final: authored text first, persisted captions last."""
    from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415

    text_overlays = _text_element_burn_dicts(variant)
    captions_input = base_local
    if text_overlays:
        text_burned = os.path.join(tmpdir, "subtitled_text_underlay.mp4")
        burn_text_overlays_skia(base_local, text_overlays, text_burned, tmpdir)
        captions_input = text_burned

    final_path = os.path.join(tmpdir, "subtitled_final.mp4")
    _burn_persisted_captions_onto_base(captions_input, final_path, variant, tmpdir)
    return final_path


def _should_compose_subtitled_final(variant: dict) -> bool:
    """Keep captions when authored text is added by visual-block autoplan.

    The public subtitled text lane has its own rollout flag. AI text cards are
    authored internally while visual blocks are enabled, so they must still use
    the text-then-caption compositor or regeneration silently drops captions.
    """
    return variant.get("resolved_archetype") == "subtitled" and (
        getattr(settings, "subtitled_text_lane_enabled", False)
        or (
            getattr(settings, "visual_blocks_enabled", False)
            and _TEXT_ELEMENTS_ENABLED
            and bool(variant.get("visual_blocks"))
            and bool(variant.get("text_elements_user_edited"))
        )
    )


def _burn_persisted_captions_onto_base(
    base_local: str, out_local: str, variant: dict, tmpdir: str
) -> None:
    """Burn (or skip) a variant's persisted caption cues onto its caption-free base.

    Shared by `_run_reburn_narrated_captions` (Apply after a hand-edit) and
    `_run_reburn_narrated_bed_level` (background-sound change — the base changes,
    the cues don't, so the SAME burn-or-copy logic re-applies onto the new base).
    Gates on BOTH `captions_enabled` (the on/off toggle — off always yields the
    caption-free copy regardless of stored cue count, so toggling back on needs no
    re-transcription) AND the presence of cues.
    """
    from app.pipeline.captions import generate_ass_from_cues, generate_word_pop_ass  # noqa: PLC0415
    from app.pipeline.narrated_assembler import (  # noqa: PLC0415
        burn_captions_on_video,
        resolve_caption_font,
    )
    from app.pipeline.text_overlay import FONTS_DIR  # noqa: PLC0415

    archetype = variant.get("resolved_archetype")
    cues = list(variant.get("caption_cues") or [])
    captions_enabled = variant.get("captions_enabled", True) is not False
    if not (captions_enabled and cues):
        # Off (regardless of stored cue count) or genuinely no cues → caption-free.
        shutil.copy2(base_local, out_local)
        return
    # Subtitled word-by-word (lime pop) is rendered by generate_word_pop_ass, not the
    # plain/word ASS styles — flagged here and branched at burn time below.
    subtitled_word_pop = (
        archetype == "subtitled" and variant.get("voiceover_caption_style") == "word"
    )
    if archetype == "subtitled":
        # Subtitled captions sit at the platform-safe MarginV. The first burn and this
        # reburn MUST agree on the margin (and style) or edited captions jump. Word-pop
        # uses the same safe margin via generate_word_pop_ass.
        ass_style = "plain"
        margin_v: int | None = _resolve_caption_margin_v(variant)
    else:
        # narrated: re-burn edited cues in the SAME caption style the variant first
        # rendered with, so word-by-word stays word-by-word after an edit ("word" → the
        # big centered one-word style; anything else → the plain sentence style).
        ass_style = "word" if variant.get("voiceover_caption_style") == "word" else "plain"
        # Legacy narrated default is MarginV=180 (captions._ass_header_for default).
        # Only pass an explicit margin after the new position control has stored one.
        margin_v = (
            _resolve_caption_margin_v(variant)
            if variant.get("caption_margin_v") is not None
            else None
        )
    # Re-burn in the variant's chosen caption font (registry key → libass family;
    # None/unknown → the default). Both narrated AND subtitled persist the font under
    # `voiceover_caption_font` (render + caption-font route + finalize whitelist).
    ass_font = resolve_caption_font(variant.get("voiceover_caption_font"))
    ass_path = os.path.join(tmpdir, "captions.ass")
    if subtitled_word_pop:
        # Real per-word times for cues left untouched; edited cues re-synthesize
        # inside generate_word_pop_ass (E3). Same safe margin as the first burn.
        generate_word_pop_ass(cues, ass_path, font_name=ass_font, margin_v=margin_v)
    else:
        generate_ass_from_cues(
            cues,
            ass_path,
            font_name=ass_font,
            style=ass_style,
            margin_v=margin_v,
            # Subtitled sentence captions keep their pop-in through edits; the
            # user's cue set is authoritative (never re-split here). Narrated
            # stays un-animated (margin_v is None only for narrated).
            pop_in=(archetype == "subtitled"),
            **(
                {"smart_policy": variant["smart_caption_policy"]}
                if variant.get("smart_caption_policy") is not None
                else {}
            ),
        )
    burn_captions_on_video(base_local, ass_path, FONTS_DIR, out_local)


def _run_reburn_narrated_captions(
    job_id: str,
    variant_id: str,
    render_gen_id: str | None = None,
    terminal_state: dict | None = None,
) -> None:
    from app.storage import (  # noqa: PLC0415
        delete_object_best_effort,
        download_to_file,
        upload_public_read,
    )

    # R4-2: the reapply chain below may run the overlay pass mid-task — clamp its
    # fullscreen budget to the wall clock actually left under the soft ceiling.
    reapply_deadline = (
        time.monotonic() + _CAPTION_TASK_SOFT_TIME_LIMIT_S - _REAPPLY_DEADLINE_MARGIN_S
    )
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("narrated_caption_reburn_job_not_found", job_id=job_id)
            return
        variants = (job.assembly_plan or {}).get("variants") or []
        variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if variant is None:
        raise ValueError(f"variant {variant_id} not found on job {job_id}")
    archetype = variant.get("resolved_archetype")
    if archetype not in _CAPTION_REBURN_ARCHETYPES:
        raise ValueError(f"variant {variant_id} is not a caption variant")
    base_path = variant.get("base_video_path")
    rank = variant.get("rank")
    if not base_path:
        raise ValueError("variant has no caption-free base — cannot reburn captions")
    render_base_path, visual_blocks_cache_path = _ensure_visual_blocks_base(
        job_id=job_id,
        variant_id=variant_id,
        variant=variant,
        base_gcs_path=base_path,
    )

    if not _update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=render_gen_id,
        outcome="caption_reburn_start",
    ):
        return
    with tempfile.TemporaryDirectory(prefix="nova_caption_reburn_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        download_to_file(render_base_path, base_local)
        if _should_compose_subtitled_final(variant):
            variant = _fresh_variant_snapshot(job_id, variant_id) or variant
            out_local = _compose_subtitled_final(base_local, variant, tmpdir)
        else:
            out_local = os.path.join(tmpdir, "out.mp4")
            _burn_persisted_captions_onto_base(base_local, out_local, variant, tmpdir)
        # New key so CDN / signed-URL caches never serve the pre-edit captions.
        new_gcs = (
            f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_cap_{uuid.uuid4().hex[:8]}.mp4"
        )
        output_url = upload_public_read(out_local, new_gcs)

    # #626: the burn above took wall-clock minutes — a lane save (the render=False
    # overlay autosave writes media_overlays with no render_status gate and no gen
    # bump) may have landed since the task-start read. Decide the reapply from the
    # FRESH persisted lane state, not the stale snapshot, so a mid-burn save is
    # still re-applied and a mid-burn clear isn't resurrected. The reapply preps
    # re-read again under a row lock before running their pass.
    will_reapply = _will_reapply_media_layers(
        _fresh_variant_snapshot(job_id, variant_id) or variant
    )
    # Local name `patch` is load-bearing: tests/test_media_overlay_byteidentity.py's
    # AST guard exempts merge-patch dicts assigned to locals named `patch`.
    patch: dict[str, Any] = {
        "video_path": new_gcs,
        "output_url": output_url,
        # Deliberate reset, never a stale round-trip: the old snapshots point at
        # the pre-reburn video (deleted below), so a stale key is a download-404.
        "pre_media_overlay_video_path": None,
        "pre_sfx_video_path": None,
        "visual_blocks_base_path": visual_blocks_cache_path,
        "visual_blocks_cache_stale": False,
    }
    if will_reapply:
        # OV-7: the reapply chain owns the final ready/failed — no effect-less
        # "ready" observable between burn and reapply.
        patch["render_status"] = "rendering"
    else:
        patch["render_status"] = "ready"
        # Advance the render fingerprint so the hero player (keyed off
        # render_finished_at) swaps to the reburned video instead of showing
        # the pre-edit one until reload.
        patch["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=render_gen_id,
        outcome="caption_reburn",
    ):
        # F3: superseded — the just-uploaded burn was never referenced; free it.
        delete_object_best_effort(new_gcs)
        return
    if terminal_state is not None:
        terminal_state["accepted"] = True  # F5: video swap landed
    _free_retired_visual_blocks_base(variant, visual_blocks_cache_path)
    # OV-4: deletes run only after the accepted terminal write above — a
    # discarded stale task must never delete the winning task's live blobs.
    _free_retired_media_snapshots(variant, (patch.get("video_path"), patch.get("base_video_path")))
    # The old burn is unreachable now — free it (generative-jobs/* never expires).
    old_video_path = variant.get("video_path")
    if old_video_path and old_video_path != new_gcs:
        delete_object_best_effort(old_video_path)
    if will_reapply and not _reapply_user_media_layers(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=render_gen_id,
        deadline_monotonic=reapply_deadline,
    ):
        # R1-3: the terminal write above deferred status (OV-7) but the chain
        # no-oped — e.g. lanes cleared mid-run via the render=False autosave, or
        # flags off in a tokenless legacy run. Finalize so no path leaves the
        # variant stranded in "rendering". Token-gated like every terminal write.
        _update_variant_entry(
            job_id,
            variant_id,
            {
                "render_status": "ready",
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
            },
            expected_render_gen_id=render_gen_id,
            outcome="caption_reburn_reapply_noop",
        )
    log.info(
        "narrated_caption_reburn_done",
        job_id=job_id,
        variant_id=variant_id,
        cues=len(variant.get("caption_cues") or []),
    )


# Archetypes with a footage audio-bed concept (background sound under a voice).
# Subtitled has no separate voice track (it keeps the ONE clip's own audio) — no
# bed to mix, so it is deliberately excluded here even though it shares the
# caption-reburn archetype set above.
_BED_LEVEL_ARCHETYPES = frozenset({"narrated"})


@celery_app.task(
    name="reburn_narrated_bed_level",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    # Re-mixes the audio bed and re-runs the clip assembly (no Whisper, no LLM) —
    # heavier than the caption reburn's re-encode-only path but far cheaper than a
    # first render. Same ceiling as the montage regenerate path.
    soft_time_limit=1740,
    time_limit=1800,
)
def reburn_narrated_bed_level(
    self, job_id: str, variant_id: str, bed_level: float, render_gen_id: str | None = None
) -> None:
    """Re-render a narrated variant's background-sound (voice/bed) balance.

    Post-gen editor Apply step for the Background Sound slider. Unlike the caption
    reburn (which only re-encodes the already-mixed base), this rebuilds the clip
    assembly from the persisted, deterministic render inputs — `narrated_timings`
    (the final step boundaries, so no re-alignment/re-transcription), `filming_guide`
    + `narrative_order` (so the SAME clip-to-step assignment recurs), and the
    persisted `caption_cues` (reburned onto the new base via the shared helper, so
    hand-edited captions survive a background-sound change). A failure reverts the
    variant to `ready` keeping its last-good video.
    """
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        terminal_state = {"accepted": False}
        try:
            _run_reburn_narrated_bed_level(
                job_id,
                variant_id,
                bed_level,
                render_gen_id=render_gen_id,
                terminal_state=terminal_state,
            )
        except OperationalError:
            raise
        except Exception as exc:
            log.error(
                "narrated_bed_level_reburn_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc)[:MAX_ERROR_DETAIL_LEN],
                exc_info=True,
            )
            if terminal_state["accepted"]:
                # F5: the video swap already landed — an exception past that point
                # means the persisted lanes are missing; "ready" would lie.
                _update_variant_entry(
                    job_id,
                    variant_id,
                    {"render_status": "failed", "render_error": str(exc)[:500]},
                    expected_render_gen_id=render_gen_id,
                    outcome="bed_level_reburn_failed_post_swap",
                )
            else:
                # Keep the last-good burned video; just clear the in-flight state.
                _update_variant_entry(
                    job_id,
                    variant_id,
                    {"render_status": "ready"},
                    expected_render_gen_id=render_gen_id,
                    outcome="bed_level_reburn_failed",
                )


def _run_reburn_narrated_bed_level(
    job_id: str,
    variant_id: str,
    bed_level: float,
    render_gen_id: str | None = None,
    terminal_state: dict | None = None,
) -> None:
    from app.pipeline.narrated_alignment import StepTiming  # noqa: PLC0415
    from app.pipeline.narrated_assembler import (  # noqa: PLC0415
        NarratedClip,
        assemble_narrated,
    )
    from app.storage import delete_object_best_effort, upload_public_read  # noqa: PLC0415
    from app.tasks.template_orchestrate import _download_clips_parallel  # noqa: PLC0415

    # R4-2: clamp the mid-task overlay reapply to the wall clock actually left.
    reapply_deadline = (
        time.monotonic() + _CAPTION_TASK_SOFT_TIME_LIMIT_S - _REAPPLY_DEADLINE_MARGIN_S
    )
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("narrated_bed_level_reburn_job_not_found", job_id=job_id)
            return
        variants = (job.assembly_plan or {}).get("variants") or []
        variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
        all_candidates = job.all_candidates or {}
    if variant is None:
        raise ValueError(f"variant {variant_id} not found on job {job_id}")
    if variant.get("resolved_archetype") not in _BED_LEVEL_ARCHETYPES:
        raise ValueError(f"variant {variant_id} has no background-sound bed to mix")
    rank = variant.get("rank")
    narrated_timings = list(variant.get("narrated_timings") or [])
    if not narrated_timings:
        raise ValueError(f"variant {variant_id} has no persisted narrated_timings")
    voiceover_gcs_path = all_candidates.get("voiceover_gcs_path")
    if not voiceover_gcs_path:
        raise ValueError(f"variant {variant_id} has no voiceover to re-mix")
    filming_guide = list(all_candidates.get("filming_guide") or [])
    clip_paths_gcs = list(all_candidates.get("clip_paths") or [])
    if not clip_paths_gcs:
        raise ValueError(f"job {job_id} has no persisted clip_paths — cannot rebuild the bed")
    narrative_shot_count = int(all_candidates.get("narrative_shot_count") or 0)
    landscape_fit = all_candidates.get("landscape_fit") or "fill"
    caption_style = (
        "word" if str(variant.get("voiceover_caption_style") or "") == "word" else "sentence"
    )
    caption_font = variant.get("voiceover_caption_font") or None

    clip_id_to_gcs = {f"clip_{i}": gcs for i, gcs in enumerate(clip_paths_gcs)}
    narrative_order = _resolve_narrative_order(narrative_shot_count, clip_id_to_gcs, job_id=job_id)

    if not _update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=render_gen_id,
        outcome="bed_level_reburn_start",
    ):
        return
    with tempfile.TemporaryDirectory(prefix="nova_bed_level_reburn_") as tmpdir:
        from app.storage import download_to_file  # noqa: PLC0415

        local_paths = _download_clips_parallel(clip_paths_gcs, tmpdir)
        clip_id_to_local = {f"clip_{i}": path for i, path in enumerate(local_paths)}
        voiceover_local = os.path.join(tmpdir, "voiceover_src")
        download_to_file(voiceover_gcs_path, voiceover_local)

        # Same clip-to-step assignment rule the first render used — deterministic,
        # transcript-independent (mirrors _render_narrated_variant's branch).
        script_steps = _narrated_script_steps(filming_guide)
        if len(script_steps) >= 2:
            clip_assignments = _narrated_clip_assignments(
                filming_guide, narrative_order, clip_id_to_local
            )
        else:
            ordered_ids = list(narrative_order or list(clip_id_to_local))
            if not ordered_ids:
                raise ValueError(f"variant {variant_id} has no clips to rebuild the bed from")
            clip_assignments = [
                NarratedClip(
                    step_id=str(t["step_id"]),
                    clip_path=clip_id_to_local[ordered_ids[i % len(ordered_ids)]],
                )
                for i, t in enumerate(narrated_timings)
                if ordered_ids[i % len(ordered_ids)] in clip_id_to_local
            ]
        if not clip_assignments:
            raise ValueError(f"variant {variant_id}: could not rebuild clip assignments")

        step_timings = [
            StepTiming(
                step_id=str(t["step_id"]),
                start_s=float(t["start_s"]),
                end_s=float(t["end_s"]),
                confidence=float(t.get("confidence", 1.0)),
            )
            for t in narrated_timings
        ]

        # transcript=None: the burned-visuals output is a throwaway (identical to the
        # base when no captions are requested) — only base_output_path is used below.
        # This reuses the exact, already-tested clip-assembly + audio-mix pipeline
        # (no new low-level ffmpeg plumbing) at the cost of a real re-assembly rather
        # than a lossless copy — acceptable given the debounce/commit-on-release
        # requirement already bounds how often this runs.
        throwaway_path = os.path.join(tmpdir, "throwaway.mp4")
        new_base_path = os.path.join(tmpdir, "new_base.mp4")
        assemble_narrated(
            step_timings,
            clip_assignments,
            voiceover_local,
            throwaway_path,
            tmpdir,
            landscape_fit=landscape_fit,
            transcript=None,
            bed_level=bed_level,
            base_output_path=new_base_path,
            caption_style=caption_style,
            caption_font=caption_font,
        )
        if not os.path.exists(new_base_path) or os.path.getsize(new_base_path) == 0:
            raise RuntimeError("bed-level reburn produced an empty base")

        out_local = os.path.join(tmpdir, "out.mp4")
        _burn_persisted_captions_onto_base(new_base_path, out_local, variant, tmpdir)

        # Shared suffix so the burned + base pair are traceable to the same reburn.
        reburn_token = uuid.uuid4().hex[:8]
        new_video_gcs = (
            f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_bed_{reburn_token}.mp4"
        )
        new_base_gcs = (
            f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_bed_{reburn_token}_base.mp4"
        )
        output_url = upload_public_read(out_local, new_video_gcs)
        upload_public_read(new_base_path, new_base_gcs)

    # #626: decide the reapply from the FRESH persisted lane state — the rebuild
    # above took wall-clock minutes and the render=False overlay autosave writes
    # media_overlays with no render_status gate (see _run_reburn_narrated_captions).
    will_reapply = _will_reapply_media_layers(
        _fresh_variant_snapshot(job_id, variant_id) or variant
    )
    # Local name `patch` is load-bearing: tests/test_media_overlay_byteidentity.py's
    # AST guard exempts merge-patch dicts assigned to locals named `patch`.
    patch: dict[str, Any] = {
        "video_path": new_video_gcs,
        "base_video_path": new_base_gcs,
        "output_url": output_url,
        "voiceover_bed_level": bed_level,
        # OV-2: media_overlays is deliberately ABSENT from this patch — the
        # _update_variant_entry merge preserves the DB's current cards, so one
        # saved during this minutes-long rebuild survives. The snapshots ARE
        # reset explicitly: they point at the pre-reburn video deleted below.
        "pre_media_overlay_video_path": None,
        "pre_sfx_video_path": None,
    }
    if will_reapply:
        # OV-7: the reapply chain owns the final ready/failed.
        patch["render_status"] = "rendering"
    else:
        patch["render_status"] = "ready"
        patch["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=render_gen_id,
        outcome="bed_level_reburn",
    ):
        # F3: superseded — the just-uploaded pair was never referenced; free both.
        delete_object_best_effort(new_video_gcs)
        delete_object_best_effort(new_base_gcs)
        return
    if terminal_state is not None:
        terminal_state["accepted"] = True  # F5: video swap landed
    # OV-4: deletes run only after the accepted terminal write above.
    _free_retired_media_snapshots(variant, (patch.get("video_path"), patch.get("base_video_path")))
    # Old burns are unreachable now — free them (generative-jobs/* never expires).
    for old_path in (variant.get("video_path"), variant.get("base_video_path")):
        if old_path and old_path not in (new_video_gcs, new_base_gcs):
            delete_object_best_effort(old_path)
    if will_reapply and not _reapply_user_media_layers(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=render_gen_id,
        deadline_monotonic=reapply_deadline,
    ):
        # R1-3: deferred terminal status (OV-7) but the chain no-oped — finalize
        # so no path leaves the variant stranded in "rendering". Token-gated.
        _update_variant_entry(
            job_id,
            variant_id,
            {
                "render_status": "ready",
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
            },
            expected_render_gen_id=render_gen_id,
            outcome="bed_level_reburn_reapply_noop",
        )
    log.info(
        "narrated_bed_level_reburn_done", job_id=job_id, variant_id=variant_id, bed_level=bed_level
    )


@celery_app.task(
    name="retranscribe_subtitled_captions",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    # Re-transcribe + reburn from the cached base (no clip re-assembly), then may
    # inline-run the overlay+SFX reapply chain (plan 010) — the overlay pass alone
    # budgets a 600s subprocess timeout, so this rides the standard render ceiling
    # (5A), under the broker visibility_timeout (1900s).
    soft_time_limit=1740,
    time_limit=1800,
)
def retranscribe_subtitled_captions(
    self, job_id: str, variant_id: str, language: str, render_gen_id: str | None = None
) -> None:
    """Re-transcribe a subtitled variant's own audio in a new language, then reburn.

    The D5 language override: the creator changes the caption language (e.g. auto-detect
    guessed wrong on a code-switched clip), and we re-run whisper-1 on the cached base's
    audio with the new hint, rebuild the cues, and reburn — REPLACING any hand-edits
    (the frontend confirms this first)."""
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    terminal_state = {"accepted": False}
    try:
        # Trace scope: the correction LLM runs inside — admin job-debug must see it.
        with pipeline_trace_for(job_id):
            _run_retranscribe_subtitled(
                job_id,
                variant_id,
                language,
                render_gen_id=render_gen_id,
                terminal_state=terminal_state,
            )
    except OperationalError:
        raise
    except Exception as exc:
        log.error(
            "subtitled_retranscribe_failed",
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc)[:MAX_ERROR_DETAIL_LEN],
            exc_info=True,
        )
        if terminal_state["accepted"]:
            # F5: the video swap already landed — an exception past that point
            # means the persisted lanes are missing; "ready" would lie.
            _update_variant_entry(
                job_id,
                variant_id,
                {"render_status": "failed", "render_error": str(exc)[:500]},
                expected_render_gen_id=render_gen_id,
                outcome="subtitled_retranscribe_failed_post_swap",
            )
        else:
            # Keep the last-good burned video; just clear the in-flight state.
            _update_variant_entry(
                job_id,
                variant_id,
                {"render_status": "ready"},
                expected_render_gen_id=render_gen_id,
                outcome="subtitled_retranscribe_failed",
            )


def _run_retranscribe_subtitled(
    job_id: str,
    variant_id: str,
    language: str,
    render_gen_id: str | None = None,
    terminal_state: dict | None = None,
) -> None:
    from app.pipeline.caption_correct import correct_caption_cues  # noqa: PLC0415
    from app.pipeline.captions import (  # noqa: PLC0415
        build_plain_cues,
        generate_ass_from_cues,
        generate_word_pop_ass,
        resplit_cues_into_sentences,
    )
    from app.pipeline.narrated_assembler import (  # noqa: PLC0415
        burn_captions_on_video,
        resolve_caption_font,
    )
    from app.pipeline.text_overlay import FONTS_DIR  # noqa: PLC0415
    from app.pipeline.transcribe import transcribe_whisper  # noqa: PLC0415
    from app.storage import (  # noqa: PLC0415
        delete_object_best_effort,
        download_to_file,
        upload_public_read,
    )

    # R4-2: clamp the mid-task overlay reapply to the wall clock actually left.
    reapply_deadline = (
        time.monotonic() + _CAPTION_TASK_SOFT_TIME_LIMIT_S - _REAPPLY_DEADLINE_MARGIN_S
    )
    lang = (language or "").strip().lower()
    if lang not in _SUBTITLED_CAPTION_LANGUAGES:
        raise ValueError(f"unsupported caption language: {language!r}")

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("subtitled_retranscribe_job_not_found", job_id=job_id)
            return
        variants = (job.assembly_plan or {}).get("variants") or []
        variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if variant is None:
        raise ValueError(f"variant {variant_id} not found on job {job_id}")
    if variant.get("resolved_archetype") != "subtitled":
        raise ValueError(f"variant {variant_id} is not a subtitled variant")
    base_path = variant.get("base_video_path")
    if not base_path:
        raise ValueError("variant has no caption-free base — cannot re-transcribe")
    rank = variant.get("rank")
    word_pop = variant.get("voiceover_caption_style") == "word"
    ass_font = resolve_caption_font(variant.get("voiceover_caption_font"))
    caption_margin_v = _resolve_caption_margin_v(variant)

    if not _update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=render_gen_id,
        outcome="subtitled_retranscribe_start",
    ):
        return
    with tempfile.TemporaryDirectory(prefix="nova_subtitled_retx_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        download_to_file(base_path, base_local)
        # Re-transcribe the base's OWN audio with the new language hint. base timeline ==
        # cue timeline (the clip renders 1:1), so no rebasing.
        transcript = transcribe_whisper(base_local, language=lang)
        cues = build_plain_cues(transcript.words, attach_words=True)
        cues = correct_caption_cues(
            cues,
            lang,
            model=settings.caption_correction_model,
            enabled=settings.subtitled_caption_correction_enabled,
        )
        # One sentence per caption (same as the first render) — see _render_subtitled.
        cues = resplit_cues_into_sentences(cues)
        if not cues:
            # The new-language pass heard nothing (e.g. a wrong hint on a quiet clip).
            # KEEP the existing captions + video — replacing them would destroy the
            # user's (possibly hand-edited) cues AND remove the Captions tab, taking
            # the re-transcribe control with it (no recovery path). Surfacing an
            # unchanged variant beats silently deleting work.
            log.warning(
                "subtitled_retranscribe_empty_kept_existing",
                job_id=job_id,
                variant_id=variant_id,
                language=lang,
            )
            # Video untouched → any baked-in SFX/overlay lanes are still on it;
            # no reapply, no snapshot reset.
            _update_variant_entry(
                job_id,
                variant_id,
                {"render_status": "ready"},
                expected_render_gen_id=render_gen_id,
                outcome="subtitled_retranscribe_empty",
            )
            return
        if getattr(settings, "subtitled_text_lane_enabled", False):
            variant = {
                **variant,
                "caption_cues": cues or None,
                "caption_language": lang,
            }
            out_local = _compose_subtitled_final(base_local, variant, tmpdir)
        else:
            out_local = os.path.join(tmpdir, "out.mp4")
            ass_path = os.path.join(tmpdir, "captions.ass")
            if word_pop:
                generate_word_pop_ass(cues, ass_path, font_name=ass_font, margin_v=caption_margin_v)
            else:
                generate_ass_from_cues(
                    cues,
                    ass_path,
                    font_name=ass_font,
                    style="plain",
                    margin_v=caption_margin_v,
                    pop_in=True,
                )
            burn_captions_on_video(base_local, ass_path, FONTS_DIR, out_local)
        new_gcs = (
            f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_lang_{uuid.uuid4().hex[:8]}.mp4"
        )
        output_url = upload_public_read(out_local, new_gcs)

    # #626: decide the reapply from the FRESH persisted lane state — the
    # transcription + burn above took wall-clock minutes and the render=False
    # overlay autosave writes media_overlays with no render_status gate
    # (see _run_reburn_narrated_captions).
    will_reapply = _will_reapply_media_layers(
        _fresh_variant_snapshot(job_id, variant_id) or variant
    )
    # Local name `patch` is load-bearing: tests/test_media_overlay_byteidentity.py's
    # AST guard exempts merge-patch dicts assigned to locals named `patch`.
    patch: dict[str, Any] = {
        "video_path": new_gcs,
        "output_url": output_url,
        "caption_cues": cues or None,
        "caption_language": lang,
        # Deliberate reset, never a stale round-trip: the old snapshots point at
        # the pre-reburn video (deleted below), so a stale key is a download-404.
        "pre_media_overlay_video_path": None,
        "pre_sfx_video_path": None,
    }
    if will_reapply:
        # OV-7: the reapply chain owns the final ready/failed.
        patch["render_status"] = "rendering"
    else:
        patch["render_status"] = "ready"
        patch["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=render_gen_id,
        outcome="subtitled_retranscribe",
    ):
        # F3: superseded — the just-uploaded burn was never referenced; free it.
        delete_object_best_effort(new_gcs)
        return
    if terminal_state is not None:
        terminal_state["accepted"] = True  # F5: video swap landed
    # OV-4: deletes run only after the accepted terminal write above.
    _free_retired_media_snapshots(variant, (patch.get("video_path"), patch.get("base_video_path")))
    # The old burn is unreachable now — free it (generative-jobs/* never expires).
    old_video_path = variant.get("video_path")
    if old_video_path and old_video_path != new_gcs:
        delete_object_best_effort(old_video_path)
    if will_reapply and not _reapply_user_media_layers(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=render_gen_id,
        deadline_monotonic=reapply_deadline,
    ):
        # R1-3: deferred terminal status (OV-7) but the chain no-oped — finalize
        # so no path leaves the variant stranded in "rendering". Token-gated.
        _update_variant_entry(
            job_id,
            variant_id,
            {
                "render_status": "ready",
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
            },
            expected_render_gen_id=render_gen_id,
            outcome="subtitled_retranscribe_reapply_noop",
        )
    log.info(
        "subtitled_retranscribe_done",
        job_id=job_id,
        variant_id=variant_id,
        language=lang,
        cues=len(cues),
    )


def _inject_lyrics(
    recipe_dict: dict,
    track: MusicTrack,
    style_set_id: str | None = None,
    line_overrides: dict | None = None,
    music_start_s: float | None = None,
    music_end_s: float | None = None,
) -> tuple[dict, list[dict]]:
    from app.pipeline.lyric_injector import (  # noqa: PLC0415
        _apply_lyric_style_overrides,
        apply_lyric_line_overrides,
        build_lyric_overlay_snapshot,
        inject_lyric_overlays,
    )
    from app.services.lyrics_cache_refresh import (  # noqa: PLC0415
        ensure_fresh_lyrics_cached_for_render,
    )
    from app.services.lyrics_config_effective import effective_lyrics_config  # noqa: PLC0415
    from app.tasks.template_orchestrate import (  # noqa: PLC0415
        compute_snapped_slot_durations,
    )

    # Overwrite target_duration_s with post-beat-snap values before injection
    # so lyric windows are clamped to the slot boundaries _assemble_clips will
    # actually produce (karaoke word-highlight drift fix).
    # The recipe grid is already relative to the effective song-window start.
    # Using the track's absolute beat timestamps here would re-snap a 55s
    # selection against video time 0 and desynchronize lyric projection from
    # the recipe and final audio seek.
    _beats = list(recipe_dict.get("beat_timestamps_s") or [])
    _snapped = compute_snapped_slot_durations(
        recipe_dict.get("slots") or [],
        _beats,
        is_agentic=False,
        user_total_dur_s=None,
    )
    for _i, _s in enumerate(_snapped):
        recipe_dict["slots"][_i]["target_duration_s"] = _s

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
    lyrics_cached_for_render = apply_lyric_line_overrides(lyrics_cached, line_overrides)
    recipe_dict = inject_lyric_overlays(
        recipe_dict,
        lyrics_cached_for_render,
        best_start_s=float(
            music_start_s if music_start_s is not None else cfg.get("best_start_s", 0.0)
        ),
        best_end_s=float(music_end_s if music_end_s is not None else cfg.get("best_end_s", 0.0)),
        lyrics_config=lyrics_config,
    )
    _apply_lyric_style_overrides(recipe_dict, line_overrides)
    return recipe_dict, build_lyric_overlay_snapshot(recipe_dict, list(_snapped))


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


def _placement_candidates_for_intro(
    *,
    hero_safe_zone: dict | None,
    hero_density: float,
    masonry_requested: bool,
    montage_preset: str | None = None,
    duration_s: float,
) -> list[dict]:
    """Variant-level smart text placement candidates for editor/render use."""
    if masonry_requested:
        from app.pipeline.masonry_montage import masonry_text_placement_candidates  # noqa: PLC0415

        return masonry_text_placement_candidates(
            duration_s=duration_s,
            preset=montage_preset or MASONRY_MONTAGE_PRESET,
        )

    from app.pipeline.overlay_sizing import text_placement_candidate_from_safe_zone  # noqa: PLC0415

    candidate = text_placement_candidate_from_safe_zone(
        hero_safe_zone,
        visual_density=hero_density,
    )
    return [candidate] if candidate else []


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
    language: str = "en",
    placement_candidates: list[dict] | None = None,
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
        language=language,
        placement_candidates=placement_candidates,
    )
    params.pop("_bs_pregate", None)  # private resolver key; not a builder kwarg
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
    language: str = "en",
    font_family_override: str | None = None,
    effect_override: str | None = None,
    text_color_override: str | None = None,
    placement_candidates: list[dict] | None = None,
    behind_subject_override: bool | None = None,
    canvas: Canvas = PORTRAIT,
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

    `behind_subject` precedence (text-behind-subject occlusion), mirroring the
    `layout` pattern above — the persisted variant value is folded into
    `agent_form["behind_subject"]` by the CALLER (same convention as
    `agent_form["layout"]`), so this resolver only sees two inputs:
      1. `behind_subject_override` (task kwarg) — wins when not None.
      2. `agent_form.get("behind_subject")` — the AI decision (first render) or
         the caller-folded persisted value (re-render).
    The resolved (pre-gate) decision is stashed under the private
    `params["_bs_pregate"]` key for the caller to persist onto
    `variant["intro_behind_subject"]` — callers MUST `pop()` it before
    spreading `params` into `inject_persistent_intro`/`build_persistent_intro_overlays`
    (neither accepts that key). `params["behind_subject"]` itself is the
    GATED value actually used for rendering: `resolved AND
    settings.text_behind_subject_enabled` — the single chokepoint where the
    kill switch forces every source to False.
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

    # Font: user-override (independent picker) > user-style knob > set > None
    font_family = font_family_override or knobs.get("font_family") or style.get("font_family")

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
            **_canvas_kwargs(canvas),
        )
        intro_source = "computed"

    params = {
        "text": agent_text.text,
        # effect: user_override > set > agent advisory; honored by both renderers
        "effect": (
            effect_override or style.get("effect") or agent_form.get("effect", "karaoke-line")
        ),
        # knobs win over set, set wins over agent advisory, agent advisory wins over default
        "position": (
            knobs.get("position") or style.get("position") or agent_form.get("position", "center")
        ),
        "text_color": (
            text_color_override
            or knobs.get("text_color")
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
        "shadow_enabled": False,
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
        "rotation_deg": (
            knobs["rotation_deg"]
            if knobs.get("rotation_deg") is not None
            else style.get("rotation_deg")
        ),
    }

    first_candidate = (placement_candidates or [None])[0]
    has_explicit_position = any(
        value is not None
        for value in (
            knobs.get("position"),
            knobs.get("position_x_frac"),
            knobs.get("position_y_frac"),
            style.get("position"),
            style.get("position_x_frac"),
            style.get("position_y_frac"),
        )
    )
    if isinstance(first_candidate, dict) and not has_explicit_position:
        params["position"] = "center"
        params["position_x_frac"] = first_candidate.get("x_frac")
        params["position_y_frac"] = first_candidate.get("y_frac")
        params["max_width_frac"] = first_candidate.get("max_width_frac")
        params["rotation_deg"] = first_candidate.get("rotation_deg")
        params["text_anchor"] = "center"

    # Layout (linear | cluster). Linear is forced when the kill switch is off or
    # when ANY explicit position exists (knob/set fracs, or a RESOLVED named
    # position other than center — knob, curated set, or agent advisory alike):
    # the cluster's geometry is engine-owned and always centers, so honoring a
    # requested "top"/"bottom" means rendering it as a linear block there.
    requested_layout = str(agent_form.get("layout") or "linear")
    layout = requested_layout
    position_pinned = any(
        params[k] is not None for k in ("position_x_frac", "position_y_frac")
    ) or params["position"] not in (None, "", "center")
    layout_reason: str | None = None
    if requested_layout == "cluster" and not getattr(
        settings, "GENERATIVE_CLUSTER_INTRO_ENABLED", True
    ):
        layout = "linear"
        layout_reason = "disabled"
    elif requested_layout == "cluster" and position_pinned:
        layout = "linear"
        layout_reason = "position_pinned"
    params["layout"] = layout
    params["requested_layout"] = requested_layout
    params["layout_source"] = str(agent_form.get("layout_source") or "model")
    params["layout_reason"] = layout_reason
    params["word_roles"] = getattr(agent_text, "word_roles", None)
    params["language"] = language

    # behind_subject: task kwarg > agent_form (AI decision, or the persisted
    # value the caller folded in — same convention as agent_form["layout"]).
    _bs_resolved = (
        behind_subject_override
        if behind_subject_override is not None
        else bool(agent_form.get("behind_subject", False))
    )
    params["behind_subject"] = _bs_resolved and bool(
        getattr(settings, "text_behind_subject_enabled", False)
    )
    # Private: pre-gate decision for sticky persistence. Callers MUST pop this
    # before spreading params into a builder function (see docstring above).
    params["_bs_pregate"] = _bs_resolved
    return params, intro_px, intro_source


def _build_unplaced_shots(
    unplaced_ids: list[str],
    *,
    narrative_order: list[str],
    clip_id_to_gcs: dict[str, str],
    clip_metas: list,
    is_music_variant: bool,
) -> list[dict]:
    """Build per-variant unplaced-shot records for assigned clips that didn't land.

    Called after match() with the set of narrative_order clip_ids NOT found in
    plan.steps.  Funnels all drop reasons through one place:
      "unusable_footage"  — clip_id absent from clip_metas (analysis failed / missing)
      "song_too_short"    — analyzed but not placed in a music variant (residual
                             when the song window is physically too short for the floor)

    Returns a list of dicts: [{clip_id, gcs_path, shot_index, reason}].
    shot_index is 1-based ordinal in narrative_order (the only shot pointer
    recoverable at render time — shot_id is stripped before the job; see
    _build_filming_guide_context).
    """
    analyzed_ids = {getattr(m, "clip_id", None) for m in clip_metas}
    narrative_index = {cid: i for i, cid in enumerate(narrative_order)}
    records = []
    for cid in unplaced_ids:
        shot_index = narrative_index.get(cid, -1) + 1
        gcs_path = clip_id_to_gcs.get(cid)
        if cid not in analyzed_ids:
            reason = "unusable_footage"
        elif is_music_variant:
            reason = "song_too_short"
        else:
            reason = "unusable_footage"
        records.append(
            {
                "clip_id": cid,
                "gcs_path": gcs_path,
                "shot_index": shot_index,
                "reason": reason,
            }
        )
    return records


def _build_no_music_recipe(
    clip_metas: list,
    available_footage_s: float,
    *,
    filming_guide: list[dict] | None = None,
    min_slots: int = 0,
) -> dict:
    """A song-free recipe: one slot per clip (capped), even-split of the footage.

    Variant 3 keeps the clips' original audio, so there are no song beats to slice
    against — we arrange the available clips evenly across the uploaded footage's
    total length. `consolidate_slots` + `match` handle the actual clip assignment
    downstream, and `allow_slowdown_fill=False` trims any slot whose assigned clip
    is shorter than its share, so the output never exceeds the real footage.

    When `filming_guide` is provided and has at least `n` entries, slot durations
    are proportional to the guide's ``duration_s`` hints rather than equal. Payoff
    shots declared longer in the guide receive a larger share of the total footage,
    biasing both clip selection and the actual rendered duration toward the intended
    setup:payoff ratio.
    """
    from app.pipeline.music_recipe import _extract_guide_durations  # noqa: PLC0415

    n = max(1, min(len(clip_metas), _MAX_NO_MUSIC_SLOTS))
    # Slot-count floor: assigned shot clips are owed a slot each. Raise n to
    # min_slots, but never beyond the clips actually available (extra slots
    # would just get gap-filled by the matcher's clip-rotation logic anyway).
    if min_slots > n:
        n = min(min_slots, len(clip_metas))

    # Narrative payoff weighting: proportional slot durations from the guide.
    guide_durs = _extract_guide_durations(filming_guide or [], n)
    if guide_durs is not None:
        total_guide = sum(guide_durs)
        slot_durs = [
            max(0.5, round(float(available_footage_s) * (d / total_guide), 3)) for d in guide_durs
        ]
    else:
        per = max(0.5, round(float(available_footage_s) / n, 3))
        slot_durs = [per] * n

    slots = [
        {
            "position": i + 1,
            "target_duration_s": slot_durs[i],
            "slot_type": "broll",
            "energy": 5.0,
            "priority": 5,
            "text_overlays": [],
            "transition_in": "cut",
            "speed_factor": 1.0,
        }
        for i in range(n)
    ]
    total_duration = round(sum(slot_durs), 3)
    return {
        "shot_count": n,
        "total_duration_s": total_duration,
        "hook_duration_s": slot_durs[0],
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
                    # Visual replacement blocks render below text/captions. The
                    # original clean base remains immutable; the derived cache
                    # is reused by text-only reburns.
                    "visual_blocks": r.get("visual_blocks"),
                    "visual_blocks_base_path": r.get("visual_blocks_base_path"),
                    "visual_blocks_cache_stale": r.get("visual_blocks_cache_stale", False),
                    "visual_blocks_autoplan_attempted": r.get("visual_blocks_autoplan_attempted"),
                    # media-overlay cards (slice 1) — MUST survive finalization
                    # or "clear all" loses the pre-overlay clean copy reference.
                    "media_overlays": r.get("media_overlays"),
                    "pre_media_overlay_video_path": r.get("pre_media_overlay_video_path"),
                    # sound-effect placements (SFX lane) — MUST survive finalization
                    # or any later full re-render (text/song/clip edit) strips the
                    # effects with no error: the render-sfx pass reads sound_effects
                    # from the variant, and pre_sfx_video_path is the clean (no-SFX)
                    # base it re-applies onto. Pinned by
                    # test_finalize_job_preserves_sound_effects.
                    "sound_effects": r.get("sound_effects"),
                    "pre_sfx_video_path": r.get("pre_sfx_video_path"),
                    # narrated caption editor — MUST survive or the cues are stripped
                    # and the on-video editor has nothing to load (base survives above,
                    # but the editor needs the cues too).
                    "caption_cues": r.get("caption_cues"),
                    # narrated caption style ("sentence" | "word") — MUST survive or a
                    # caption edit reburns in the wrong style (the reburn reads it).
                    "voiceover_caption_style": r.get("voiceover_caption_style"),
                    # narrated caption font (registry key) — MUST survive or a caption
                    # edit reburns in the wrong font (the reburn reads it).
                    "voiceover_caption_font": r.get("voiceover_caption_font"),
                    # subtitled caption position — MUST survive or a caption edit /
                    # language re-transcribe reburns at the legacy safe-zone instead
                    # of the creator's chosen y position.
                    "caption_margin_v": r.get("caption_margin_v"),
                    # subtitled caption language ("en"/"tr") — MUST survive so the editor
                    # chip shows it and the re-transcribe override reads the current one.
                    "caption_language": r.get("caption_language"),
                    # Smart Captions initial semantic plan + deterministic lane
                    # compilation. These are admin/debug evidence and the future
                    # immutable-revision seed; the public response model may omit
                    # them, but finalization must not destroy worker output.
                    "smart_captions_applied": r.get("smart_captions_applied", False),
                    "smart_edit_document": r.get("smart_edit_document"),
                    "smart_compiled_patch": r.get("smart_compiled_patch"),
                    "smart_planner_versions": r.get("smart_planner_versions"),
                    "smart_validation_receipts": r.get("smart_validation_receipts"),
                    "smart_caption_policy": r.get("smart_caption_policy"),
                    "smart_music_treatment": r.get("smart_music_treatment"),
                    "smart_audio_receipt": r.get("smart_audio_receipt"),
                    "boundary_effects": r.get("boundary_effects"),
                    # TextElement lane state — MUST survive finalization for any variant
                    # whose authored text is edited through PUT /text-elements.
                    "text_elements": r.get("text_elements"),
                    "text_elements_user_edited": r.get("text_elements_user_edited"),
                    # Lyrics editor state — MUST survive finalization or the editor
                    # sees no lyric projections and capabilities report
                    # no_renderable_lyrics until the first re-render. Pinned by
                    # test_finalize_job_preserves_lyric_fields.
                    "lyrics_enabled": r.get("lyrics_enabled"),
                    "lyrics_available": r.get("lyrics_available"),
                    "lyric_line_overrides": r.get("lyric_line_overrides"),
                    "lyric_overlay_snapshot": r.get("lyric_overlay_snapshot"),
                    # Lyrics-as-optional-elements: False iff this render skipped
                    # baking lyrics under LYRICS_OPTIONAL_ENABLED. MUST survive
                    # finalization or the "new model" carve-outs (timeline
                    # editability, text-element write acceptance, fast reburn)
                    # silently revert to legacy-baked behavior after every job.
                    "lyrics_baked": r.get("lyrics_baked"),
                    # Output orientation — initial renders are portrait today, but
                    # keep the persisted value authoritative rather than implied.
                    "orientation": r.get("orientation"),
                    "text_elements_materialized_from": r.get("text_elements_materialized_from"),
                    # render fingerprint — the caption editor's remount key reads it, so
                    # stripping it here would silently degrade re-seeding after reburns.
                    "render_finished_at": r.get("render_finished_at"),
                    # sequence + cluster persistence (D15/D19) — MUST survive
                    # finalization or every re-render loses the synced typography:
                    # intro_mode gates route behavior (sequence_synced, retext
                    # block), scenes/transcript drive the deterministic reburn
                    # rebuild, sequence_quote drives LLM-free rhythm re-timing,
                    # intro_layout/intro_word_roles keep clusters clusters.
                    "intro_layout": r.get("intro_layout"),
                    "intro_word_roles": r.get("intro_word_roles"),
                    "intro_mode": r.get("intro_mode"),
                    "transcript": r.get("transcript"),
                    "scenes": r.get("scenes"),
                    "sequence_base_size_px": r.get("sequence_base_size_px"),
                    "sequence_mode": r.get("sequence_mode"),
                    "sequence_quote": r.get("sequence_quote"),
                    # per-user style knobs (M1) — re-renders re-apply them from the
                    # variant entry, never the persona row (same strip class).
                    "user_style_knobs": r.get("user_style_knobs"),
                    # clip-editor timeline — MUST survive finalization or every
                    # variant reports `no_timeline` and the editor never appears.
                    # This whitelist silently strips anything the render persisted
                    # that isn't re-listed here; pinned by
                    # test_finalize_job_preserves_ai_timeline.
                    "ai_timeline": r.get("ai_timeline"),
                    # Montage visual preset/capability gating. MUST survive
                    # finalization or masonry variants look like classic variants
                    # to the editor and expose a misleading clip timeline.
                    **(
                        {
                            "montage_preset": r.get("montage_preset"),
                            "montage_preset_rendered": r.get("montage_preset_rendered"),
                            "montage_preset_fallback": r.get("montage_preset_fallback"),
                        }
                        if r.get("montage_preset")
                        else {}
                    ),
                    # voiceover variants: mix must survive or the voice/bed slider
                    # resets after the first completed render (same strip class).
                    "mix": r.get("mix"),
                    # narrated step alignment (diagnostic): keep it through finalize
                    # so the admin job-debug view + re-render diagnostics can read it.
                    "narrated_timings": r.get("narrated_timings"),
                    # silence/filler cut summary (plans/010) — MUST survive
                    # finalization or the admin cut-plan viewer (T9) and the
                    # per-variant time-saved stat silently lose their data the
                    # moment the job completes. Pinned by
                    # test_finalize_job_preserves_silence_cut.
                    "silence_cut": r.get("silence_cut"),
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


def _persist_archetype_fallback(job_id: str, declared: str, reason: str | None) -> None:
    """Persist (or clear) the style-downgrade reason on assembly_plan["archetype_fallback"].

    The single writer for the key both call sites share (resolution-time stash and the
    mid-render spine-degrade path). `reason` set → {declared, reason} lands on the plan
    and the item page shows the downgrade banner. `reason` None → clear a stale value
    from a prior attempt, but ONLY when the key already exists — never touching a
    clean job keeps flag-off assembly_plan byte-identical to pre-feature output.
    """
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        plan = job.assembly_plan or {}
        if reason:
            job.assembly_plan = {
                **plan,
                "archetype_fallback": {"declared": declared, "reason": reason},
            }
        elif "archetype_fallback" in plan:
            job.assembly_plan = {**plan, "archetype_fallback": None}
        else:
            return
        db.commit()


def _set_status(job_id: str, status: str, extra_plan: dict[str, Any] | None = None) -> None:
    # Row-locked RMW (mirrors _upsert_variant_entry / _update_variant_entry).
    # `extra_plan` merges into assembly_plan — _finalize_job writes the WHOLE
    # variants list here. Sibling regenerate/reapply tasks and the status route's
    # lazy overlay-preview backfill read-modify-write the same JSONB concurrently,
    # so without SELECT ... FOR UPDATE a stale read silently clobbers their state.
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
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
            # Row-locked: reconciling variant render_status below is a
            # read-modify-write of assembly_plan. Without SELECT ... FOR UPDATE a
            # concurrent variant/finalize write can be clobbered by this stale read
            # (mirrors _upsert_variant_entry).
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if job:
                job.status = "processing_failed"
                job.error_detail = error_detail[:MAX_ERROR_DETAIL_LEN]
                if failure_reason:
                    job.failure_reason = failure_reason

                # Reconcile per-variant render_status.  Any variant still at
                # "rendering" or "pending" when the task fails will freeze the
                # frontend poll loop forever (the anyRendering predicate keeps
                # polling while any variant claims to be rendering).  Flip those
                # to "failed" now so the UI reaches a terminal state immediately.
                ap = job.assembly_plan or {}
                if isinstance(ap, dict):
                    variants = ap.get("variants") or []
                    new_variants = [
                        {
                            **v,
                            "render_status": "failed",
                            "error": v.get("error") or error_detail[:200],
                        }
                        if v.get("render_status") in ("rendering", "pending")
                        else v
                        for v in variants
                    ]
                    if new_variants != variants:
                        job.assembly_plan = {**ap, "variants": new_variants}

                db.commit()
    except Exception as exc:
        log.error("generative_fail_job_db_error", job_id=job_id, error=str(exc))
