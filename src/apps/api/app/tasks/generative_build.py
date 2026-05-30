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
from typing import Any

import structlog
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
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


@celery_app.task(
    name="orchestrate_generative_job",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    soft_time_limit=1800,
    time_limit=2000,
)
def orchestrate_generative_job(self, job_id: str) -> None:
    """Entry point. Never raises — any exception becomes processing_failed."""
    log.info("generative_job_start", job_id=job_id)

    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        try:
            _run_generative_job(job_id)
        except OperationalError:
            raise  # transient DB → Celery autoretry
        except Exception as exc:
            log.error("generative_job_failed", job_id=job_id, error=str(exc), exc_info=True)
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

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("generative_job_not_found", job_id=job_id)
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

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths in all_candidates")

    with tempfile.TemporaryDirectory(prefix="nova_generative_") as tmpdir:
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
            style = _select_generative_style_set(clip_metas, text, job_id=job_id)
            return text, form, style

        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_tonemap = pool.submit(_pretonemap_hdr_clips, clip_id_to_local, probe_map, tmpdir)
            fut_text = pool.submit(_text_then_style)
            fut_match = pool.submit(_match_best_track, clip_metas, job_id=job_id)
            n_tonemapped = fut_tonemap.result()
            agent_text, agent_form, style_set_id = fut_text.result()
            best_track = fut_match.result()

        record_pipeline_event("reframe", "hdr_pretonemap_done", {"clips_converted": n_tonemapped})
        record_pipeline_event("overlay", "agent_text_done", {"has_text": bool(agent_text)})
        record_pipeline_event("overlay", "style_set_selected", {"style_set_id": style_set_id})
        record_pipeline_event(
            "assembly", "song_match_done", {"track_id": best_track.id if best_track else None}
        )

        # [Phase 4/5] Build the variant spec list and render each.
        _set_status(job_id, "rendering")
        specs = _variant_specs(best_track)

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
        results: list[dict[str, Any]] = []
        for rank, spec in enumerate(specs, start=1):
            variant_id = spec["variant_id"]
            spec_track_id = spec["track"].id if spec["track"] else None
            reusable = prior.get(variant_id)
            if reusable is not None and reusable.get("music_track_id") == spec_track_id:
                record_pipeline_event(
                    "assembly", "variant_resumed", {"variant_id": variant_id, "rank": rank}
                )
                results.append({**reusable, "rank": rank})
                continue

            variant_dir = os.path.join(tmpdir, f"variant_{rank}")
            os.makedirs(variant_dir, exist_ok=True)
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
            )
            # Persist immediately so a deploy/OOM after this point can't lose it,
            # and so the status endpoint reveals variants as they finish rather
            # than all-at-once at _finalize_job.
            _upsert_variant_entry(job_id, result)
            results.append(result)

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

    from app.pipeline.reframe import (  # noqa: PLC0415
        _HDR10_TRANSFER,
        _HDR_FALLBACK_PIPELINE,
        _HLG_TRANSFER,
        _ZSCALE_SDR_PIPELINE,
        _zscale_available,
    )
    from app.tasks.template_orchestrate import _probe_clips  # noqa: PLC0415

    hdr_transfers = {_HLG_TRANSFER, _HDR10_TRANSFER}
    vf = _ZSCALE_SDR_PIPELINE if _zscale_available() else _HDR_FALLBACK_PIPELINE
    # Guard against odd output dimensions (libx264 + yuv420p require even W/H);
    # the linear-light downscale can land on an odd minor axis for non-16:9
    # sources. trunc-to-even crops at most 1px — imperceptible, and only fires
    # on pathological aspect ratios.
    vf = f"{vf},crop=trunc(iw/2)*2:trunc(ih/2)*2"

    converted = 0
    for clip_id, local_path in list(clip_id_to_local.items()):
        probe = probe_map.get(local_path)
        if probe is None or getattr(probe, "color_trc", "bt709") not in hdr_transfers:
            continue

        sdr_path = os.path.join(tmpdir, f"sdr_{converted}_{os.path.basename(local_path)}")
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
            continue

        try:
            probe_map[sdr_path] = _probe_clips([sdr_path])[sdr_path]
        except Exception as exc:  # noqa: BLE001
            log.warning("generative_pretonemap_reprobe_failed", clip_id=clip_id, error=str(exc))
            continue
        clip_id_to_local[clip_id] = sdr_path
        converted += 1

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
    soft_time_limit=1800,
    time_limit=2000,
)
def regenerate_generative_variant(
    self,
    job_id: str,
    variant_id: str,
    new_track_id: str | None = None,
    override_text: str | None = None,
    remove_text: bool = False,
    style_set_id: str | None = None,
) -> None:
    """Re-render ONE variant of an existing generative job (swap-song / retext / restyle).

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
                job_id, variant_id, new_track_id, override_text, remove_text, style_set_id
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
                job_id, variant_id, {"render_status": "failed", "ok": False, "error": str(exc)}
            )


def _run_regenerate_variant(
    job_id: str,
    variant_id: str,
    new_track_id: str | None,
    override_text: str | None,
    remove_text: bool,
    style_set_id: str | None = None,
) -> None:
    import types  # noqa: PLC0415

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
        variants = ((job.assembly_plan or {}).get("variants")) or []
        existing = next((v for v in variants if v.get("variant_id") == variant_id), None)
        if existing is None:
            log.error("generative_regenerate_variant_unknown", job_id=job_id, variant_id=variant_id)
            return
        rank = int(existing.get("rank", 1))
        existing_track_id = existing.get("music_track_id")
        existing_text_mode = existing.get("text_mode", "agent_text")
        # Style precedence: explicit restyle request → the variant's persisted set →
        # any sibling variant's set (the job-level default) → "default".
        existing_style_set_id = existing.get("style_set_id")
        if existing_style_set_id is None:
            existing_style_set_id = next(
                (v.get("style_set_id") for v in variants if v.get("style_set_id")), None
            )
    resolved_style_set_id = style_set_id or existing_style_set_id or "default"

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths to re-render from")

    # Mark this variant as re-rendering so the UI can show a spinner immediately.
    _update_variant_entry(
        job_id, variant_id, {"render_status": "rendering", "ok": False, "error": None}
    )

    # Resolve the track for the new spec.
    track: MusicTrack | None = None
    track_id = new_track_id or existing_track_id
    if track_id:
        with _sync_session() as db:
            track = db.get(MusicTrack, track_id)
        if track is None or track.analysis_status != "ready" or not track.audio_gcs_path:
            raise ValueError(f"Track {track_id} is not available for re-render")

    # Resolve text mode + text.
    if remove_text:
        text_mode = "none"
    elif override_text:
        text_mode = "agent_text"
    else:
        text_mode = existing_text_mode

    spec = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": text_mode,
        "track": track,
    }

    with tempfile.TemporaryDirectory(prefix="nova_generative_re_") as tmpdir:
        # PERF/TODO: this re-runs the full clip ingest (re-download + re-Gemini
        # clip_metadata) on every swap/retext, even remove_text. Acceptable for v1
        # (async re-render, reject-if-rendering guard caps spam), but the Gemini
        # analysis is cacheable — persist clip_metas after the first orchestrate run
        # and skip re-analysis here (only re-download + re-probe are truly needed for
        # the render). Follow-up once the feature is real-render-verified.
        ingest = _ingest_clips(clip_paths_gcs, tmpdir, job_id=job_id)
        available_footage_s = _available_footage_s(ingest["probe_map"])
        agent_text: Any = None
        agent_form: dict = {}
        if text_mode == "agent_text":
            if override_text:
                # User-supplied text bypasses intro_writer's LLM, so it must go through
                # the SAME output sanitizers the agent path applies (the Skia renderer
                # does NOT sanitize — it draws overlay["text"] verbatim). Strip ASS
                # tags / control chars / URLs / handles and clamp length.
                from app.agents.intro_writer import _clamp, _strip_unsafe_tokens  # noqa: PLC0415
                from app.agents.text_alignment import _sanitize_aligned_line  # noqa: PLC0415

                cleaned = _clamp(_strip_unsafe_tokens(_sanitize_aligned_line(override_text)))
                if cleaned:
                    agent_text = types.SimpleNamespace(text=cleaned, highlight_word=None)
                    agent_form = {"effect": "karaoke-line"}
                # else: nothing renderable after sanitization → no overlay (footage only).
            else:
                agent_text, agent_form = _run_text_agents(
                    ingest["clip_metas"],
                    ingest["hero"],
                    job_id=job_id,
                    language=language,
                    persona=persona,
                )

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
) -> dict[str, Any]:
    """Render one variant. Never raises — failures become a failure record."""
    from app.pipeline.agents.gemini_analyzer import build_recipe  # noqa: PLC0415
    from app.pipeline.music_recipe import generate_music_recipe  # noqa: PLC0415
    from app.pipeline.template_matcher import (  # noqa: PLC0415
        TemplateMismatchError,
        consolidate_slots,
        match,
    )
    from app.storage import upload_public_read  # noqa: PLC0415
    from app.tasks.template_orchestrate import (  # noqa: PLC0415
        _assemble_clips,
        _enrich_slots_with_energy,
        _mix_template_audio,
    )

    variant_id = spec["variant_id"]
    text_mode = spec["text_mode"]
    track: MusicTrack | None = spec["track"]
    track_id = track.id if track else None
    track_title = track.title if track else None

    base = {
        "variant_id": variant_id,
        "rank": rank,
        "text_mode": text_mode,
        "music_track_id": track_id,
        "track_title": track_title,
        "style_set_id": style_set_id,
    }
    try:
        beats: list[float] = []
        if track is not None:
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
        if text_mode == "lyrics" and track is not None:
            recipe_dict = _inject_lyrics(recipe_dict, track, style_set_id=style_set_id)
        elif text_mode == "agent_text" and agent_text is not None:
            recipe_dict = _inject_agent_intro(
                recipe_dict, agent_text, agent_form, beats, style_set_id=style_set_id
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

        final_path = os.path.join(variant_dir, "final.mp4")
        if track is not None:
            # Song variants: replace source audio with the matched track.
            cfg = track.track_config or {}
            _mix_template_audio(
                assembled_path,
                track.audio_gcs_path,
                final_path,
                variant_dir,
                audio_start_offset_s=float(cfg.get("best_start_s", 0.0)),
            )
        else:
            # Original-audio variant: KEEP the clips' source audio — skip the mix.
            # `_assemble_clips` already muxed source audio into assembled.mp4.
            final_path = assembled_path

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
        return {**base, "ok": False, "render_status": "failed", "error": err}


def _inject_lyrics(recipe_dict: dict, track: MusicTrack, style_set_id: str | None = None) -> dict:
    from app.pipeline.lyric_injector import inject_lyric_overlays  # noqa: PLC0415
    from app.services.lyrics_config_effective import effective_lyrics_config  # noqa: PLC0415

    cfg = track.track_config or {}
    if style_set_id:
        # The chosen curated set drives the lyric look for a generative edit and is
        # authoritative over the track's saved (music-job) lyric tuning, so we do NOT
        # inherit `cfg["lyrics_config"]`. The set's lyric role implies the injector
        # style (line/karaoke/word-pop) via `lyric_style_for_set`. style_set_id is
        # consumed by `inject_lyric_overlays` directly (not a validated config key),
        # so set it on the dict rather than routing it through effective_lyrics_config.
        lyrics_config = {"enabled": True, "style_set_id": style_set_id}
    else:
        # Force lyrics on for this variant (the user explicitly chose the lyrics edit).
        lyrics_config = effective_lyrics_config(cfg, {"enabled": True, "style": "karaoke"})
    return inject_lyric_overlays(
        recipe_dict,
        track.lyrics_cached,
        best_start_s=float(cfg.get("best_start_s", 0.0)),
        best_end_s=float(cfg.get("best_end_s", 0.0)),
        lyrics_config=lyrics_config,
    )


def _inject_agent_intro(
    recipe_dict: dict,
    agent_text,
    agent_form: dict,
    beats: list[float],
    style_set_id: str | None = None,
) -> dict:
    from app.pipeline.generative_overlays import (  # noqa: PLC0415
        inject_persistent_intro,
    )

    slots = recipe_dict.get("slots") or []
    if not slots:
        return recipe_dict
    slot0_dur = float(slots[HERO_SLOT_INDEX].get("target_duration_s", 0.0) or 0.0)
    # The intro now persists for the whole video (held statically after the reveal), so
    # MAX_INTRO_S caps only the reveal/animation window, not how long the text shows.
    reveal_window_s = min(slot0_dur, MAX_INTRO_S) if slot0_dur > 0 else MAX_INTRO_S

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

    return inject_persistent_intro(
        recipe_dict,
        HERO_SLOT_INDEX,
        text=agent_text.text,
        effect=style.get("effect") or agent_form.get("effect", "karaoke-line"),
        reveal_window_s=reveal_window_s,
        beats=beats,  # slot-0 / section-relative; empty for the no-music variant
        position=style.get("position") or agent_form.get("position", "center"),
        size_class=agent_form.get("size_class", "jumbo"),
        text_color=style.get("text_color") or agent_form.get("text_color", "#FFFFFF"),
        highlight_color=style.get("highlight_color")
        or agent_form.get("highlight_color", "#FFD24A"),
        text_anchor=style.get("text_anchor") or agent_form.get("text_anchor", "center"),
        highlight_word=getattr(agent_text, "highlight_word", None),
        font_family=style.get("font_family"),
        stroke_width=style.get("stroke_width"),
        text_size_px=style.get("text_size_px"),
        position_x_frac=style.get("position_x_frac"),
        position_y_frac=style.get("position_y_frac"),
    )


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
