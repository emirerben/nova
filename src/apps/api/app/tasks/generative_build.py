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
import hashlib
import json
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from itertools import cycle
from typing import Any, NamedTuple

import structlog
from sqlalchemy.exc import OperationalError

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS, coerce_edit_format
from app.config import settings
from app.database import sync_session as _sync_session
from app.models import Job, MusicTrack
from app.pipeline.canvas import PORTRAIT, Canvas, canvas_for_orientation
from app.pipeline.look_presets import (
    LookPreset,
    normalize_look_adjustments,
    normalize_look_preset,
)
from app.schemas.montage_preset import (
    DEFAULT_MONTAGE_PRESET,
    MASONRY_MONTAGE_PRESET,
    coerce_montage_preset,
    is_collage_montage_preset,
)
from app.services.generative_jobs import CONTENT_PLAN_PRIMARY_VARIANT_POLICY
from app.services.job_phases import (
    job_heartbeat,
    mark_failed_phase,
    mark_finished,
    mark_started,
    record_phase,
    record_sub_phase,
)
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
_CLIP_METADATA_CACHE_VERSION = 1
_PREPROCESSED_SOURCE_CACHE_VERSION = 1
_HDR_PRETONEMAP_CACHE_VERSION = 1
# Caps the hero intro's reveal/animation window (NOT its display time — the intro is
# held statically for the whole video). A long beat-1 slot shouldn't stretch the
# word-by-word reveal across the entire first clip; it finishes revealing within this
# window, then the full text holds.
MAX_INTRO_S = 3.0
HERO_SLOT_INDEX = 0


class _ResolvedTimelineSlot(NamedTuple):
    clip_index: int
    in_s: float
    duration_s: float
    moment_energy: Any
    moment_description: Any
    transition_after: str
    transition_duration_s: float | None
    look_preset: LookPreset
    look_adjustments: dict | None


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
# The music matcher prompt and worker memory must not grow with the full admin
# catalog. Eligibility is filtered in SQL and this deterministic newest-first
# cap bounds the ORM/JSONB payload materialized by one Smart render.
_SMART_MUSIC_CANDIDATE_LIMIT = 80


class CachedBaseUnusableError(RuntimeError):
    """The cached fast-reburn substrate cannot be safely reused."""


class CachedBaseProbeError(CachedBaseUnusableError):
    """The cached fast-reburn substrate could not be inspected."""


class CachedBaseCanvasMismatchError(CachedBaseUnusableError):
    """A fast-reburn base belongs to a different output canvas."""

    def __init__(
        self,
        *,
        base_path: str,
        expected: tuple[int, int],
        actual: tuple[int, int],
    ) -> None:
        self.base_path = base_path
        self.expected = expected
        self.actual = actual
        super().__init__(
            "Cached fast-reburn base canvas mismatch: "
            f"expected {expected[0]}x{expected[1]}, got {actual[0]}x{actual[1]}"
        )


# Voiceover edits: the user's recorded/uploaded voice is the audio bed. `mix` is the
# voice-prominence slider (1.0 = bed fully ducked, voice only; 0.0 = bed full).
# Defaults differ per variant: voice-over-footage starts with footage muted, while
# voice+music starts with the music audibly under the voice. Output is capped so a
# long voiceover can never run past the footage OR the sub-60s short-form ceiling.
_VOICEOVER_ONLY_DEFAULT_MIX = 1.0
_VOICEOVER_MUSIC_DEFAULT_MIX = 0.7
_VOICEOVER_MAX_DURATION_S = 60.0

# Celery-safe tri-state sentinel for `regenerate_generative_variant`'s
# `carousel_moment_override` kwarg (the editable-carousel dispatch path).
# A plain string so it survives task serialization unmolested; distinguishes
# "no carousel edit requested this render" (carry the persisted moment
# forward unchanged) from the two real values `None` (explicit removal) and
# `dict` (partial edit) — see `_merge_carousel_moment_override` below.
# Defined this early (well before `regenerate_generative_variant`'s def) so
# it's available as a default-argument value at function-definition time —
# Python evaluates defaults when the `def` executes, not lazily.
CAROUSEL_MOMENT_UNSET = "__carousel_moment_unset__"

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


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _record_render_subphase(
    job_id: str | uuid.UUID | None,
    parent: str,
    name: str,
    t0: float,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    if job_id is None:
        return
    record_sub_phase(job_id, parent, name, elapsed_ms=_elapsed_ms(t0), detail=detail)


def _cache_fingerprint(clip_paths: list[str]) -> dict[str, Any]:
    return {
        "clip_paths": list(clip_paths),
        "source_downscale_guard_enabled": bool(settings.source_downscale_guard_enabled),
        "source_downscale_short_edge_max": int(settings.source_downscale_short_edge_max),
        "orientation_normalize_enabled": bool(
            getattr(settings, "orientation_normalize_enabled", True)
        ),
    }


def _read_all_candidates(job_id: str | uuid.UUID | None) -> dict[str, Any]:
    if job_id is None:
        return {}
    try:
        job_uuid = uuid.UUID(str(job_id))
    except (ValueError, TypeError):
        return {}
    try:
        with _sync_session() as db:
            job = db.get(Job, job_uuid)
            return dict(job.all_candidates or {}) if job is not None else {}
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log.warning("generative_cache_read_failed", job_id=str(job_id), error=str(exc))
        return {}


def _merge_all_candidates(job_id: str | uuid.UUID | None, patch: dict[str, Any]) -> None:
    if job_id is None:
        return
    try:
        job_uuid = uuid.UUID(str(job_id))
    except (ValueError, TypeError):
        return
    try:
        with _sync_session() as db:
            job = db.get(Job, job_uuid, with_for_update=True)
            if job is None:
                return
            job.all_candidates = {**(job.all_candidates or {}), **patch}
            db.commit()
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log.warning("generative_cache_write_failed", job_id=str(job_id), error=str(exc))


def _clip_meta_to_cache(meta: Any) -> dict[str, Any]:
    if is_dataclass(meta):
        return asdict(meta)
    if hasattr(meta, "model_dump"):
        return meta.model_dump()
    return {
        "clip_id": getattr(meta, "clip_id", ""),
        "transcript": getattr(meta, "transcript", ""),
        "hook_text": getattr(meta, "hook_text", ""),
        "hook_score": float(getattr(meta, "hook_score", 0.0) or 0.0),
        "best_moments": list(getattr(meta, "best_moments", []) or []),
        "detected_subject": getattr(meta, "detected_subject", ""),
        "analysis_degraded": bool(getattr(meta, "analysis_degraded", False)),
        "failed": bool(getattr(meta, "failed", False)),
        "clip_path": getattr(meta, "clip_path", ""),
        "text_safe_zone": getattr(meta, "text_safe_zone", None),
        "visual_density": float(getattr(meta, "visual_density", 5.0) or 5.0),
    }


def _clip_meta_from_cache(raw: dict[str, Any]) -> Any:
    from app.pipeline.agents.gemini_analyzer import ClipMeta  # noqa: PLC0415

    allowed = {
        "clip_id",
        "transcript",
        "hook_text",
        "hook_score",
        "best_moments",
        "detected_subject",
        "analysis_degraded",
        "failed",
        "clip_path",
        "text_safe_zone",
        "visual_density",
    }
    payload = {k: raw.get(k) for k in allowed if k in raw}
    return ClipMeta(**payload)


def _load_clip_metadata_cache(job_id: str, clip_paths_gcs: list[str]) -> list[Any] | None:
    cache = _read_all_candidates(job_id).get("clip_metadata_cache")
    if not isinstance(cache, dict):
        return None
    if cache.get("version") != _CLIP_METADATA_CACHE_VERSION:
        return None
    if cache.get("fingerprint") != _cache_fingerprint(clip_paths_gcs):
        return None
    raw_metas = cache.get("clip_metas")
    if not isinstance(raw_metas, list) or len(raw_metas) != len(clip_paths_gcs):
        return None
    try:
        return [_clip_meta_from_cache(m) for m in raw_metas if isinstance(m, dict)]
    except Exception as exc:  # noqa: BLE001 - cache hit may never break render
        log.warning("clip_metadata_cache_decode_failed", job_id=job_id, error=str(exc))
        return None


def _store_clip_metadata_cache(
    job_id: str, clip_paths_gcs: list[str], clip_metas: list[Any]
) -> None:
    _merge_all_candidates(
        job_id,
        {
            "clip_metadata_cache": {
                "version": _CLIP_METADATA_CACHE_VERSION,
                "fingerprint": _cache_fingerprint(clip_paths_gcs),
                "clip_metas": [_clip_meta_to_cache(meta) for meta in clip_metas],
            }
        },
    )


def _load_preprocessed_source_cache(job_id: str, clip_paths_gcs: list[str]) -> list[str] | None:
    cache = _read_all_candidates(job_id).get("preprocessed_source_cache")
    if not isinstance(cache, dict):
        return None
    if cache.get("version") != _PREPROCESSED_SOURCE_CACHE_VERSION:
        return None
    if cache.get("fingerprint") != _cache_fingerprint(clip_paths_gcs):
        return None
    paths = cache.get("processed_clip_paths")
    if not isinstance(paths, list) or len(paths) != len(clip_paths_gcs):
        return None
    return [str(path) for path in paths]


def _store_preprocessed_source_cache(
    job_id: str, clip_paths_gcs: list[str], local_clip_paths: list[str]
) -> None:
    try:
        from app.storage import upload_public_read  # noqa: PLC0415

        processed_paths: list[str] = []
        for i, local_path in enumerate(local_clip_paths):
            ext = os.path.splitext(local_path)[1] or ".mp4"
            dst = f"generative-jobs/{job_id}/preprocessed/{i:03d}{ext}"
            upload_public_read(local_path, dst)
            processed_paths.append(dst)
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log.warning("preprocessed_source_cache_store_failed", job_id=job_id, error=str(exc))
        return
    _merge_all_candidates(
        job_id,
        {
            "preprocessed_source_cache": {
                "version": _PREPROCESSED_SOURCE_CACHE_VERSION,
                "fingerprint": _cache_fingerprint(clip_paths_gcs),
                "processed_clip_paths": processed_paths,
            }
        },
    )


def _pretonemap_fingerprint(clip_id_to_local: dict[str, str], probe_map: dict) -> dict[str, Any]:
    clips: list[dict[str, Any]] = []
    for clip_id, local_path in clip_id_to_local.items():
        probe = probe_map.get(local_path)
        clips.append(
            {
                "clip_id": clip_id,
                "duration_s": round(float(getattr(probe, "duration_s", 0.0) or 0.0), 3),
                "width": int(getattr(probe, "width", 0) or 0),
                "height": int(getattr(probe, "height", 0) or 0),
                "color_trc": getattr(probe, "color_trc", None),
            }
        )
    return {
        "clips": clips,
        "zscale_pipeline": _ZSCALE_SDR_PIPELINE_CACHE_KEY,
    }


def _safe_cache_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in str(value))
    return token[:80] or "clip"


_ZSCALE_SDR_PIPELINE_CACHE_KEY = "zscale-sdr-v1-crf16-fast-bt709"


def _load_hdr_pretonemap_cache(
    job_id: str | None,
    clip_id_to_local: dict[str, str],
    probe_map: dict,
    tmpdir: str,
    *,
    signature: dict[str, Any],
    hdr_clip_ids: set[str],
) -> int:
    if job_id is None or not hdr_clip_ids:
        return 0
    cache = _read_all_candidates(job_id).get("hdr_pretonemap_cache")
    if not isinstance(cache, dict):
        return 0
    if cache.get("version") != _HDR_PRETONEMAP_CACHE_VERSION:
        return 0
    if cache.get("fingerprint") != signature:
        return 0
    paths_by_clip = cache.get("processed_by_clip_id")
    if not isinstance(paths_by_clip, dict) or not hdr_clip_ids.issubset(paths_by_clip):
        return 0
    try:
        from app.storage import download_to_file  # noqa: PLC0415
        from app.tasks.template_orchestrate import _probe_clips  # noqa: PLC0415

        downloaded: dict[str, str] = {}
        for clip_id in sorted(hdr_clip_ids):
            local_path = os.path.join(tmpdir, f"cached_sdr_{_safe_cache_token(clip_id)}.mp4")
            download_to_file(str(paths_by_clip[clip_id]), local_path)
            downloaded[clip_id] = local_path
        reprobed = _probe_clips(list(downloaded.values()))
    except Exception as exc:  # noqa: BLE001 - cache hit must never break render
        log.warning("hdr_pretonemap_cache_load_failed", job_id=job_id, error=str(exc))
        return 0

    for clip_id, local_path in downloaded.items():
        probe = reprobed.get(local_path)
        if probe is None:
            return 0
        probe_map[local_path] = probe
        clip_id_to_local[clip_id] = local_path
    return len(downloaded)


def _store_hdr_pretonemap_cache(
    job_id: str | None,
    *,
    signature: dict[str, Any],
    converted: list[tuple[str, str, Any]],
) -> None:
    if job_id is None or not converted:
        return
    try:
        from app.storage import upload_public_read  # noqa: PLC0415

        processed_by_clip_id: dict[str, str] = {}
        for i, (clip_id, local_path, _probe) in enumerate(converted):
            dst = (
                f"generative-jobs/{job_id}/preprocessed/"
                f"hdr_{i:03d}_{_safe_cache_token(clip_id)}.mp4"
            )
            upload_public_read(local_path, dst)
            processed_by_clip_id[clip_id] = dst
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log.warning("hdr_pretonemap_cache_store_failed", job_id=job_id, error=str(exc))
        return
    _merge_all_candidates(
        job_id,
        {
            "hdr_pretonemap_cache": {
                "version": _HDR_PRETONEMAP_CACHE_VERSION,
                "fingerprint": signature,
                "processed_by_clip_id": processed_by_clip_id,
            }
        },
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

    # job_heartbeat: liveness beacon for the status route's `retrying` flag —
    # a silently killed attempt stops beating, and the redelivered attempt's
    # first beat clears the stale state (2026-07-21 OOM, job e8173a25).
    with pipeline_trace_for(job_id), job_heartbeat(job_id):
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


@celery_app.task(
    name="rerender_speech_timing",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    soft_time_limit=1740,
    time_limit=1800,
)
def rerender_speech_timing(self, job_id: str, operation_id: str) -> None:
    """Dedicated full speech rebuild with last-good transactional semantics."""
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id), job_heartbeat(job_id):
        task_id = str(self.request.id or operation_id)
        retry_number = int(self.request.retries or 0)
        attempt_id = f"{task_id}:{retry_number}:{uuid.uuid4().hex}"
        if not _claim_speech_cut_finalize(
            job_id,
            operation_id,
            attempt_id,
            task_id=task_id,
            retry_number=retry_number,
        ):
            log.info(
                "speech_timing_rerender_duplicate_skipped",
                job_id=job_id,
                operation_id=operation_id,
            )
            return
        mark_started(job_id)
        try:
            _run_generative_job(
                job_id,
                speech_cut_operation_id=operation_id,
                speech_cut_attempt_id=attempt_id,
            )
            mark_finished(job_id)
        except OperationalError as exc:
            # Preserve the normal transient retry path, but do not leave the
            # accepted request stuck forever when the final retry is exhausted.
            if self.request.retries >= self.max_retries:
                _restore_failed_speech_cut_rerender(
                    job_id,
                    str(exc),
                    expected_operation_id=operation_id,
                    expected_attempt_id=attempt_id,
                )
                mark_finished(job_id)
                return
            _release_speech_cut_finalize_claim(job_id, operation_id, attempt_id)
            raise
        except Exception as exc:  # noqa: BLE001 — restore the exact last-good variant
            log.error(
                "speech_timing_rerender_failed",
                job_id=job_id,
                error=str(exc)[:MAX_ERROR_DETAIL_LEN],
                exc_info=True,
            )
            _restore_failed_speech_cut_rerender(
                job_id,
                str(exc),
                expected_operation_id=operation_id,
                expected_attempt_id=attempt_id,
            )
            mark_finished(job_id)


# ── Pipeline ──────────────────────────────────────────────────────────────────


def _run_generative_job(
    job_id: str,
    *,
    speech_cut_operation_id: str | None = None,
    speech_cut_attempt_id: str | None = None,
) -> None:
    from app.services.pipeline_trace import (  # noqa: PLC0415
        record_pipeline_event,
        record_render_stage,
        render_stage_timer,
    )

    # Skia kill-switch guard. Agent-text + karaoke reveals have NO Pillow equivalent;
    # if the renderer falls back to Pillow+libass the overlays render wrong or drop.
    # Fail loudly rather than ship garbage. (See CLAUDE.md TEXT_RENDERER_SKIA_ENABLED.)
    if not settings.text_renderer_skia_enabled:
        if speech_cut_operation_id:
            raise RuntimeError("speech timing rerender requires TEXT_RENDERER_SKIA_ENABLED")
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
    render_trace_id = uuid.uuid4().hex

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("generative_job_not_found", job_id=job_id)
            return
        # Redelivery guard (acks_late). If this job already reached a terminal state,
        # a redelivered message must not re-run the whole pipeline (it would repeat
        # the expensive pre-tonemap and clobber a finished result). Mid-render jobs
        # are left at "rendering" — not terminal — so the resume path still works.
        control = (job.assembly_plan or {}).get("speech_cut_control") or {}
        matching_speech_cut = bool(
            speech_cut_operation_id
            and control.get("operation_id") == speech_cut_operation_id
            and (control.get("finalizer_claim") or {}).get("attempt_id") == speech_cut_attempt_id
        )
        if speech_cut_operation_id and not matching_speech_cut:
            raise RuntimeError("speech cut operation was superseded before render")
        if job.status in _NO_RERUN_STATUSES and not matching_speech_cut:
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
            # Re-validate every preset token at the worker boundary: assembly_plan
            # is JSONB any writer can touch, and load_preset builds a filesystem
            # path from these — match the service-layer charset gate exactly.
            from app.services.generative_jobs import _SMART_PRESET_TOKEN_RE  # noqa: PLC0415

            _pid = str(_raw_smart["preset_id"])
            _pver = str(_raw_smart["preset_version"])
            if not (
                _SMART_PRESET_TOKEN_RE.fullmatch(_pid) and _SMART_PRESET_TOKEN_RE.fullmatch(_pver)
            ):
                # Fail closed to a non-smart render, but never silently — the
                # admin debug trail must show WHY smart captions vanished.
                log.warning(
                    "smart_preset_token_rejected",
                    job_id=job_id,
                    preset_id=_pid[:80],
                    preset_version=_pver[:80],
                )
            else:
                smart_captions = {
                    "preset_id": _pid,
                    "preset_version": _pver,
                    "sound_design": ("off" if _raw_smart.get("sound_design") == "off" else "auto"),
                }
                _sid = str(_raw_smart.get("shadow_preset_id") or "").strip()
                _sver = str(_raw_smart.get("shadow_preset_version") or "").strip()
                if (
                    _sid
                    and _sver
                    and _SMART_PRESET_TOKEN_RE.fullmatch(_sid)
                    and _SMART_PRESET_TOKEN_RE.fullmatch(_sver)
                ):
                    smart_captions["shadow_preset_id"] = _sid
                    smart_captions["shadow_preset_version"] = _sver
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
        _speech_cut_prior = (job.assembly_plan or {}).get("speech_cut_previous_variant")
        speech_cut_pinned_spine = (
            str(_speech_cut_prior.get("spine_clip_id"))
            if isinstance(_speech_cut_prior, dict) and _speech_cut_prior.get("spine_clip_id")
            else None
        )
        # Montage visual preset. Absent means classic so public/legacy jobs keep
        # byte-identical render behavior.
        montage_preset = coerce_montage_preset((all_candidates or {}).get("montage_preset"))

    if not clip_paths_gcs:
        raise ValueError("Generative job has no clip paths in all_candidates")

    analyze_t0 = time.monotonic()

    # Durable per-job source copies (clip timeline editor). User uploads under
    # the 24h-lifecycle prefixes are snapshot to `generative-jobs/{job_id}/sources/`
    # BEFORE ingest, so a timeline edit days later can still re-render from the
    # original bytes. Strictly order-preserving 1:1 — _resolve_narrative_order
    # slices the first N keys of clip_id_to_gcs, so clip_paths order is
    # load-bearing. Best-effort: on any copy failure ALL original paths are kept.
    with render_stage_timer(
        "asset_persist_durable_sources",
        trace_id=render_trace_id,
        counts={"clip_count": len(clip_paths_gcs)},
    ):
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
        with render_stage_timer(
            "asset_loading_and_preprocess",
            trace_id=render_trace_id,
            counts={
                "clip_count": len(clip_paths_gcs),
                "skip_analysis": _skip_clip_analysis,
            },
        ):
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
        match_phase_t0 = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=3)
        try:
            prework_started = time.monotonic()
            fut_tonemap = pool.submit(
                _pretonemap_hdr_clips, clip_id_to_local, probe_map, tmpdir, job_id=job_id
            )
            fut_text = pool.submit(_text_then_style)
            fut_match = pool.submit(_match_best_track, clip_metas, job_id=job_id)
            tonemap_t0 = time.monotonic()
            n_tonemapped = fut_tonemap.result()
            record_render_stage(
                "preprocessing_hdr_tonemap",
                elapsed_ms=int((time.monotonic() - prework_started) * 1000),
                trace_id=render_trace_id,
                counts={"clips_converted": n_tonemapped},
            )
            _record_render_subphase(
                job_id,
                "match_song",
                "hdr_pretonemap",
                tonemap_t0,
                detail={"clips_converted": n_tonemapped},
            )
            text_t0 = time.monotonic()
            agent_text, agent_form, style_set_id = fut_text.result()
            record_render_stage(
                "ai_text_and_style",
                elapsed_ms=int((time.monotonic() - prework_started) * 1000),
                trace_id=render_trace_id,
                counts={"has_text": bool(agent_text)},
            )
            _record_render_subphase(
                job_id,
                "match_song",
                "text_and_style",
                text_t0,
                detail={"has_text": bool(agent_text), "style_set_id": style_set_id},
            )
            match_t0 = time.monotonic()
            best_track = fut_match.result()
            record_render_stage(
                "audio_match",
                elapsed_ms=int((time.monotonic() - prework_started) * 1000),
                trace_id=render_trace_id,
                counts={"matched": best_track is not None},
            )
            _record_render_subphase(
                job_id,
                "match_song",
                "music_matcher",
                match_t0,
                detail={"track_id": best_track.id if best_track else None},
            )
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
        match_done_t = time.monotonic()
        record_phase(
            job_id,
            "analyze_clips",
            elapsed_ms=int((match_phase_t0 - analyze_t0) * 1000),
            next_phase="match_song",
        )
        record_phase(
            job_id,
            "match_song",
            elapsed_ms=int((match_done_t - match_phase_t0) * 1000),
            next_phase="render_variants",
        )
        render_variants_t0 = time.monotonic()

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
        if (
            archetype == "talking_head"
            and speech_cut_pinned_spine
            and speech_cut_pinned_spine in clip_id_to_local
        ):
            spine_clip_id = speech_cut_pinned_spine
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
            # First render only. Re-renders stamp this at DISPATCH instead
            # (`stamp_variant_attempt` in services/job_phases.py) so the tile
            # clock restarts on the Save press rather than inheriting this value.
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
            variant_render_t0 = time.monotonic()
            archetype_for_trace = str(spec.get("archetype") or "montage")
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
                        render_trace_id=render_trace_id,
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
                record_render_stage(
                    "variant_render",
                    elapsed_ms=int((time.monotonic() - variant_render_t0) * 1000),
                    status="ok" if result.get("ok") else "failed",
                    trace_id=render_trace_id,
                    variant_id=variant_id,
                    render_generation_id=spec.get("storage_generation"),
                    counts={"archetype": archetype_for_trace, "rank": rank},
                )
                result = _merge_speech_cut_prior_state(
                    job_id,
                    result,
                    expected_operation_id=speech_cut_operation_id,
                    expected_attempt_id=speech_cut_attempt_id,
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
            except BaseException as exc:
                record_render_stage(
                    "variant_render",
                    elapsed_ms=int((time.monotonic() - variant_render_t0) * 1000),
                    status="failed",
                    trace_id=render_trace_id,
                    variant_id=variant_id,
                    render_generation_id=spec.get("storage_generation"),
                    counts={"archetype": archetype_for_trace, "rank": rank},
                    error_class=type(exc).__name__,
                )
                raise
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
        # Carousel-moment authoring policy (kill-switched, additive): attaches
        # spec["carousel_moment"] to one eligible montage spec so the render
        # hook in _render_generative_variant actually fires on real jobs. See
        # _author_carousel_moments's docstring for the eligibility/selection
        # rules. No-op — mutates nothing — unless both carousel flags are on.
        _author_carousel_moments(initial_specs, job_id=job_id, n_clips=len(clip_metas))
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
                    # Seed the authored moment onto the row BEFORE the render even
                    # starts (not just once `base` persists it below): a crash/OOM
                    # between here and the first `_upsert_variant_entry(result)`
                    # would otherwise leave this pending row as the only persisted
                    # state, and _run_regenerate_variant's `existing.get(...)`
                    # reads THIS row — so a carousel_moment authored but never
                    # rendered must still be visible to a re-render.
                    "carousel_moment": spec.get("carousel_moment"),
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

    record_phase(
        job_id,
        "render_variants",
        elapsed_ms=_elapsed_ms(render_variants_t0),
        next_phase="finalize",
    )
    finalize_t0 = time.monotonic()
    with render_stage_timer(
        "finalize",
        trace_id=render_trace_id,
        counts={"variant_count": len(results)},
    ):
        _finalize_job(
            job_id,
            results,
            expected_operation_id=speech_cut_operation_id,
            expected_attempt_id=speech_cut_attempt_id,
        )
        if speech_cut_operation_id and speech_cut_attempt_id:
            _compose_speech_cut_rerender(
                job_id,
                expected_operation_id=speech_cut_operation_id,
                expected_attempt_id=speech_cut_attempt_id,
            )
            _publish_speech_cut_rerender(
                job_id,
                expected_operation_id=speech_cut_operation_id,
                expected_attempt_id=speech_cut_attempt_id,
            )
    record_phase(job_id, "finalize", elapsed_ms=_elapsed_ms(finalize_t0))
    _dispatch_post_finalize_suggestion_chains(
        job_id,
        speech_cut_rerender=bool(speech_cut_operation_id),
    )


def _dispatch_post_finalize_suggestion_chains(job_id: str, *, speech_cut_rerender: bool) -> None:
    """Run first-generation suggestion chains, never timing-only rebuilds.

    Speech-cut rerenders already reproject the creator's existing media/SFX
    lanes and invalidate stale Director state. Re-running first-generation
    placement after the exact cut receipt would mutate the accepted output
    behind that receipt and could add duplicate treatments.
    """
    if speech_cut_rerender:
        return
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
    # SFX suggestion chain (word-level sound design). Independent of the overlay
    # chain's flag/asset guards — it needs speech + the SFX glossary, not the
    # asset pool. Same AFTER-finalize ordering constraint. Best-effort.
    try:
        _maybe_sfx_autoplace_after_finalize(job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("sfx_autoplace_chain_dispatch_failed", job_id=job_id, error=str(exc)[:200])


def _maybe_visual_blocks_after_finalize(job_id: str) -> None:
    """Dispatch first-edit block planning once for compatible ready variants."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.config import settings as _settings  # noqa: PLC0415
    from app.tasks.autoplace import (  # noqa: PLC0415
        VISUAL_BLOCK_AUTOPLAN_ARCHETYPES,
        _clear_visual_block_attempts,
        prepare_visual_block_assets,
    )

    if not (_settings.visual_blocks_enabled and _settings.visual_block_autoplan_enabled):
        return
    # Buffered, flushed AFTER the row lock releases (record_pipeline_event's
    # own-connection UPDATE deadlocks against a held FOR UPDATE on jobs).
    skipped_archetypes: list[tuple[str, str]] = []
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
                # Autoplan targets speech-spined archetypes only; unset
                # resolved_archetype means montage-by-default. No claim is
                # taken, so a later re-render that changes the archetype can
                # still autoplan.
                archetype = str(variant.get("resolved_archetype") or "montage")
                if archetype not in VISUAL_BLOCK_AUTOPLAN_ARCHETYPES:
                    skipped_archetypes.append((str(variant.get("variant_id")), archetype))
                    continue
                variant["visual_blocks_autoplan_attempted"] = True
                eligible.append(str(variant.get("variant_id")))
        if eligible:
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            flag_modified(job, "assembly_plan")
            db.commit()
    if skipped_archetypes:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        for variant_id, archetype in skipped_archetypes:
            record_pipeline_event(
                "autoplace",
                "visual_blocks_skipped_archetype",
                {"variant_id": variant_id, "archetype": archetype},
            )
        log.info(
            "visual_blocks_autoplan_skipped_archetype",
            job_id=job_id,
            skipped=skipped_archetypes,
        )
    if not eligible:
        return
    try:
        prepare_visual_block_assets.apply_async(
            args=[job_id, eligible], queue=_settings.autoplace_queue
        )
    except Exception:
        _clear_visual_block_attempts(job_id, eligible)
        raise


def _maybe_sfx_autoplace_after_finalize(job_id: str) -> None:
    """Dispatch advisory SFX suggestions per eligible variant (dark-flagged).

    Guards, in order (each a silent no-op, never raised to the caller):
      - SFX_AUTOPLACE_ENABLED off ⇒ byte-identical behavior.
      - Public generative jobs (no content_plan_item_id) ⇒ no-op — the editor
        surface that realizes suggestions is the plan-item editor.
      - Per variant: rendered (video_path + ready), only ONCE per render
        generation (`sfx_autoplace_attempted` marker, same acks_late guard as
        the overlay chain), and speech-plausible: a persisted word source
        (sequence transcript / caption cue words / overlay_transcript) OR no
        matched music track (bounded Whisper on speech is sane; on a song it
        yields garbage anchors — same rule as the overlay chain).
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.config import settings as _settings  # noqa: PLC0415
    from app.services.transcript_source import speech_words_for_variant  # noqa: PLC0415
    from app.tasks.autoplace import autoplace_sfx_suggestions  # noqa: PLC0415

    # Dual-flag guard: suggestions are realized through the SOUND_EFFECTS
    # write routes — with that lane off they'd be unrealizable ghosts (and
    # would burn Whisper+Gemini per variant for nothing).
    if not (_settings.sfx_autoplace_enabled and _settings.sound_effects_enabled):
        return
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None or job.content_plan_item_id is None:
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        eligible: list[str] = []
        for v in variants:
            if (
                v.get("render_status") == "ready"
                and v.get("video_path")
                and not v.get("sfx_autoplace_attempted")
                and (speech_words_for_variant(v) is not None or v.get("music_track_id") is None)
            ):
                v["sfx_autoplace_attempted"] = True
                eligible.append(str(v.get("variant_id")))
        if not eligible:
            return
        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
        flag_modified(job, "assembly_plan")
        db.commit()
    for variant_id in eligible:
        autoplace_sfx_suggestions.apply_async(
            args=[job_id, variant_id],
            queue=_settings.autoplace_queue,
        )
    log.info("sfx_autoplace_chain_dispatched", job_id=job_id, variants=eligible)


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

    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    download_t0 = time.monotonic()
    cached_sources = _load_preprocessed_source_cache(job_id, clip_paths_gcs)
    source_paths_to_download = cached_sources or clip_paths_gcs
    local_clip_paths = _download_clips_parallel(source_paths_to_download, tmpdir)
    _record_render_subphase(
        job_id,
        "analyze_clips",
        "ingest_download",
        download_t0,
        detail={"clips": len(local_clip_paths), "preprocessed_cache_hit": bool(cached_sources)},
    )
    if cached_sources:
        record_pipeline_event(
            "ingest",
            "preprocessed_sources_reused",
            {"clips": len(cached_sources)},
        )

    probe_t0 = time.monotonic()
    probe_map = _probe_clips(local_clip_paths)
    _record_render_subphase(
        job_id,
        "analyze_clips",
        "ingest_probe",
        probe_t0,
        detail={"clips": len(probe_map)},
    )
    if not cached_sources:
        # Heavy-source guard (2026-07-21 OOM): oversized SDR clips are downscaled
        # ONCE here — before Gemini upload (smaller upload) and before any variant
        # reframe (bounded decode). Mutates local_clip_paths/probe_map in place so
        # the clip_id maps below point at the intermediates. HDR clips pass through
        # untouched (the pre-tonemap pass owns those). Best-effort: a failed
        # conversion keeps the original.
        from app.pipeline.source_guard import downscale_oversized_sources  # noqa: PLC0415

        before_guard = list(local_clip_paths)
        guard_t0 = time.monotonic()
        downscale_oversized_sources(local_clip_paths, probe_map, tmpdir, job_id=job_id)
        changed = before_guard != local_clip_paths
        _record_render_subphase(
            job_id,
            "analyze_clips",
            "source_guard",
            guard_t0,
            detail={"changed": changed},
        )
        if changed:
            _store_preprocessed_source_cache(job_id, clip_paths_gcs, local_clip_paths)
            record_pipeline_event(
                "ingest",
                "preprocessed_sources_stored",
                {"clips": len(local_clip_paths)},
            )
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
    cached_metas = _load_clip_metadata_cache(job_id, clip_paths_gcs)
    if cached_metas:
        record_pipeline_event(
            "ingest",
            "clip_metadata_cache_hit",
            {"clips": len(cached_metas)},
        )
        for i, meta in enumerate(cached_metas):
            if i < len(local_clip_paths):
                meta.clip_path = local_clip_paths[i]
        local_by_id = {
            str(getattr(meta, "clip_id", f"clip_{i}")): local_clip_paths[i]
            for i, meta in enumerate(cached_metas)
            if i < len(local_clip_paths)
        }
        gcs_by_id = {
            str(getattr(meta, "clip_id", f"clip_{i}")): clip_paths_gcs[i]
            for i, meta in enumerate(cached_metas)
            if i < len(clip_paths_gcs)
        }
        return {
            "clip_metas": cached_metas,
            "probe_map": probe_map,
            "clip_id_to_gcs": gcs_by_id,
            "clip_id_to_local": local_by_id,
            "hero": max(cached_metas, key=lambda m: float(getattr(m, "hook_score", 0.0) or 0.0)),
        }
    record_pipeline_event("ingest", "clip_metadata_cache_miss", {"clips": len(clip_paths_gcs)})
    analysis_t0 = time.monotonic()
    file_refs = _upload_clips_parallel(local_clip_paths)
    clip_metas, failed_count = _analyze_clips_parallel(
        file_refs, local_clip_paths, probe_map, job_id=job_id
    )
    _record_render_subphase(
        job_id,
        "analyze_clips",
        "clip_metadata",
        analysis_t0,
        detail={"clips": len(clip_metas), "failed": failed_count},
    )
    total = len(clip_metas) + failed_count
    if total == 0 or len(clip_metas) == 0 or failed_count > total * (1.0 - min_success_fraction):
        raise ValueError(
            f"{failed_count}/{total} clips failed clip_metadata — aborting "
            f"(min_success_fraction={min_success_fraction})"
        )
    _store_clip_metadata_cache(job_id, clip_paths_gcs, clip_metas)
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
    signature = _pretonemap_fingerprint(clip_id_to_local, probe_map)
    cached_count = _load_hdr_pretonemap_cache(
        job_id,
        clip_id_to_local,
        probe_map,
        tmpdir,
        signature=signature,
        hdr_clip_ids={clip_id for _idx, clip_id, _path in hdr_clips},
    )
    if cached_count:
        record_pipeline_event(
            "reframe",
            "hdr_pretonemap_cache_hit",
            {"clips": cached_count},
        )
        return cached_count

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
    _store_hdr_pretonemap_cache(job_id, signature=signature, converted=results)
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
    carousel_moment_override: Any = CAROUSEL_MOMENT_UNSET,
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

    `carousel_moment_override`: tri-state carousel-editor edit — the
    `CAROUSEL_MOMENT_UNSET` sentinel default (leave the persisted moment
    alone), `None` (explicit removal), or a validated partial-edit dict.
    Merged onto the persisted `carousel_moment` by
    `_merge_carousel_moment_override` inside `_run_regenerate_variant`.

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
                carousel_moment_override=carousel_moment_override,
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
    persisted_position: str | None = None,
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

    `persisted_position` (from `variant["intro_placement"]["position"]`) follows the
    same rule. The agent's original position advisory is not otherwise recoverable
    on a no-LLM branch, so without this fold an intro the matcher placed at "bottom"
    silently re-centered on the first text edit. Folding it at the ADVISORY tier —
    not as an override — keeps knobs and a curated set winning, exactly as they did
    on the first render. Callers pass None for legacy variants AND for the centered
    default (see `_persisted_intro_position`): `agent_form` then gets no `position`
    key and resolution is byte-identical.
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
            if persisted_position:
                agent_form["position"] = persisted_position
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
        if persisted_position:
            agent_form["position"] = persisted_position
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
    if existing.get("base_video_stale"):
        return False  # a superseded base-affecting render has not landed yet
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
    audio_start_offset_s: float | None = None,
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
    requested_offset = (
        audio_start_offset_s if audio_start_offset_s is not None else cfg.get("best_start_s", 0.0)
    )
    safe_offset = max(0.0, float(requested_offset or 0.0))
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
        raise RuntimeError(f"audio-only song swap failed (rc={result.returncode}): {stderr}")
    if not os.path.exists(out_local) or os.path.getsize(out_local) == 0:
        raise RuntimeError("audio-only song swap produced empty output")
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
        "base_video_stale": False,
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


def _slot_signature(slot: dict) -> dict[str, Any]:
    return {
        key: slot.get(key)
        for key in (
            "slot_id",
            "clip_index",
            "source_gcs_path",
            "in_s",
            "duration_s",
            "duration_beats",
            "removed",
        )
    }


def _same_rendered_slots(left: list[dict] | None, right: list[dict] | None) -> bool:
    if left is None or right is None or len(left) != len(right):
        return False
    return [_slot_signature(s) for s in left] == [_slot_signature(s) for s in right]


def _is_music_window_audio_only_swap_eligible(
    *,
    existing: dict,
    track: MusicTrack | None,
    music_window_alignment: str | None,
    timeline_override: list[dict] | None,
) -> bool:
    """True when a song-window edit can preserve already-rendered visuals."""
    if music_window_alignment != "preserve_cuts":
        return False
    if existing.get("variant_id") != "song_text":
        return False
    if existing.get("text_mode") == "lyrics":
        return False
    if track is None or track.analysis_status != "ready" or not track.audio_gcs_path:
        return False
    if not existing.get("video_path") or not existing.get("base_video_path"):
        return False
    rendered_slots = (existing.get("user_timeline") or {}).get("slots") or (
        existing.get("ai_timeline") or {}
    ).get("slots")
    if timeline_override is not None and not _same_rendered_slots(
        timeline_override, rendered_slots
    ):
        return False
    return True


def _run_music_window_audio_only_swap(
    *,
    job_id: str,
    variant_id: str,
    existing: dict,
    track: MusicTrack,
    expected_render_gen_id: str | None = None,
) -> bool:
    """Fast music-window edit: keep video frames/cuts, replace only the audio bed."""
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    token = uuid.uuid4().hex
    music_start_s = max(0.0, float(existing.get("music_start_s", 0.0) or 0.0))
    path_fields = (
        "video_path",
        "base_video_path",
        "pre_media_overlay_video_path",
        "pre_sfx_video_path",
    )
    patch: dict[str, Any] = {
        "music_track_id": track.id,
        "track_title": track.title,
        "base_video_stale": False,
        "ok": True,
        "error": None,
        "render_error": None,
        "render_status": "ready",
        "render_finished_at": datetime.utcnow().isoformat() + "Z",
    }
    with tempfile.TemporaryDirectory(prefix="nova_music_window_audio_swap_") as tmpdir:
        for field in path_fields:
            source_gcs = existing.get(field)
            if not source_gcs:
                continue
            out_gcs = (
                f"generative-jobs/{job_id}/music-window-audio/{variant_id}_{token}_{field}.mp4"
            )
            signed_url = _mux_track_audio_preserve_video(
                video_gcs_path=source_gcs,
                track=track,
                output_gcs_path=out_gcs,
                tmpdir=tmpdir,
                label=field,
                audio_start_offset_s=music_start_s,
            )
            patch[field] = out_gcs
            if field == "video_path":
                patch["output_url"] = signed_url

    if "video_path" not in patch or "output_url" not in patch:
        raise ValueError("music-window audio-only swap missing current video_path")

    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=expected_render_gen_id,
        outcome="music_window_audio_swap",
    ):
        return False

    record_pipeline_event(
        "audio_mix",
        "music_window_audio_only_swap",
        {"variant_id": variant_id, "track_id": track.id, "music_start_s": music_start_s},
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
    from app.storage import copy_object, object_exists  # noqa: PLC0415

    pass_t0 = time.monotonic()

    # Snapshot only. Never hold a checked-out connection or row lock across GCS
    # copies / FFmpeg: a database restart during a long encode otherwise poisons
    # the session before the terminal write (prod incident 2026-07-27).
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("media_overlay_job_not_found", job_id=job_id)
            return
        persisted = (job.assembly_plan or {}).get("variants") or []
        found = next((v for v in persisted if v.get("variant_id") == variant_id), None)
        existing = dict(found) if found is not None else None
    if existing is None:
        log.error("media_overlay_variant_not_found", job_id=job_id, variant_id=variant_id)
        return

    current_video_path = existing.get("video_path")
    if not current_video_path:
        log.error("media_overlay_no_video_path", job_id=job_id, variant_id=variant_id)
        return

    cards = coerce_media_overlays(overlays_raw)

    # Stale-bake detection baseline (plan 009 E5): if the user edits cards while
    # FFmpeg runs, preserve their newer metadata during the terminal commit.
    import json as _json_e5  # noqa: PLC0415

    def _canon_cards(raw: object) -> str:
        try:
            return _json_e5.dumps(raw or [], sort_keys=True)
        except (TypeError, ValueError):
            return "[]"

    overlays_at_start = _canon_cards(existing.get("media_overlays"))

    def _persist_result(
        *,
        output_url: str,
        pre_clean: str | None,
        clear: bool,
    ) -> tuple[bool, bool, bool]:
        """Return (accepted, will_reapply_sfx, stale_card_metadata_skipped)."""
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        from app.config import settings as _settings_ov  # noqa: PLC0415

        with _sync_session() as db:
            locked_job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if locked_job is None:
                return False, False, False
            variants = list((locked_job.assembly_plan or {}).get("variants") or [])
            stale_write_skipped = False
            for variant in variants:
                if variant.get("variant_id") != variant_id:
                    continue
                current = variant.get("render_generation_id")
                if (
                    expected_render_gen_id is not None
                    and current is not None
                    and current != expected_render_gen_id
                ):
                    log.warning(
                        "stale_render_write_discarded",
                        job_id=job_id,
                        variant_id=variant_id,
                        outcome="media_overlay_clear" if clear else "media_overlay_apply",
                        expected_gen_id=expected_render_gen_id,
                        actual_gen_id=current,
                    )
                    return False, False, False

                will_reapply_sfx = (
                    (
                        bool(variant.get("background_music_treatment"))
                        or bool(variant.get("smart_music_treatment"))
                    )
                    and _settings_ov.smart_music_bed_enabled
                ) or (bool(variant.get("sound_effects")) and _settings_ov.sound_effects_enabled)
                if _canon_cards(variant.get("media_overlays")) == overlays_at_start:
                    if clear:
                        variant["media_overlays"] = None
                    else:
                        variant["media_overlays"] = [card.model_dump() for card in cards or []]
                        variant["media_overlays_applied_ids"] = None
                else:
                    stale_write_skipped = True
                if pre_clean is not None:
                    variant["pre_media_overlay_video_path"] = pre_clean
                if not stale_write_skipped:
                    variant["media_overlays_render_dirty"] = False
                variant["output_url"] = output_url
                if will_reapply_sfx:
                    variant["render_status"] = "rendering"
                else:
                    variant["render_status"] = "ready"
                    variant["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
                break
            else:
                return False, False, False

            locked_job.assembly_plan = {
                **(locked_job.assembly_plan or {}),
                "variants": variants,
            }
            flag_modified(locked_job, "assembly_plan")
            db.commit()
            return True, will_reapply_sfx, stale_write_skipped

    # ── Clear path: remove all cards ──────────────────────────────────────────
    if not cards:
        clean_path = existing.get("pre_media_overlay_video_path")
        if clean_path and clean_path != current_video_path:
            copy_object(clean_path, current_video_path)
            from app.storage import signed_get_url  # noqa: PLC0415

            signed_url = signed_get_url(current_video_path, expiration_minutes=60 * 24)
        else:
            signed_url = existing.get("output_url", "")

        accepted, will_reapply_sfx, _ = _persist_result(
            output_url=signed_url,
            pre_clean=None,
            clear=True,
        )
        if not accepted:
            return
        record_pipeline_event(
            "media_overlay",
            "cards_cleared",
            {"variant_id": variant_id, "elapsed_ms": _elapsed_ms(pass_t0)},
        )
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

    # ── Apply path: composite cards onto the clean base ───────────────────────
    pre_clean = existing.get("pre_media_overlay_video_path")
    if not pre_clean:
        base_for_clean = existing.get("pre_sfx_video_path") or current_video_path
        pre_clean = current_video_path + "_pre_overlay"
        try:
            if object_exists(pre_clean):
                log.info("media_overlay_clean_copy_reused", job_id=job_id, dst=pre_clean)
            else:
                copy_object(base_for_clean, pre_clean)
                log.info("media_overlay_clean_copy_created", job_id=job_id, dst=pre_clean)
        except Exception as exc:  # noqa: BLE001
            log.warning("media_overlay_clean_copy_failed", job_id=job_id, error=str(exc))
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
    except OperationalError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error(
            "media_overlay_apply_failed",
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            exc_info=True,
        )
        _mark_variant_failed(
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            expected_render_gen_id=expected_render_gen_id,
        )
        record_pipeline_event(
            "media_overlay",
            "apply_failed",
            {"error": str(exc)[:200], "elapsed_ms": _elapsed_ms(pass_t0)},
        )
        return

    accepted, will_reapply_sfx, stale_write_skipped = _persist_result(
        output_url=new_url,
        pre_clean=pre_clean,
        clear=False,
    )
    if not accepted:
        return
    record_pipeline_event(
        "media_overlay",
        "cards_applied",
        {
            "variant_id": variant_id,
            "card_count": len(cards),
            "stale_write_skipped": stale_write_skipped,
            "elapsed_ms": _elapsed_ms(pass_t0),
        },
    )
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
        or (
            (
                bool(variant.get("background_music_treatment"))
                or bool(variant.get("smart_music_treatment"))
            )
            and settings.smart_music_bed_enabled
        )
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
            overlays_raw = _project_carousel_timed_lanes(existing).get("media_overlays")
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
    except OperationalError:
        raise
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
    except OperationalError:
        raise
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
                _project_carousel_timed_lanes(existing).get("sound_effects") or []
                if _settings_sfx.sound_effects_enabled
                else []
            )
            music_active = (
                bool(existing.get("background_music_treatment"))
                or bool(existing.get("smart_music_treatment"))
            ) and getattr(_settings_sfx, "smart_music_bed_enabled", True)
            if not sfx_raw and not music_active:
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
    except OperationalError:
        raise
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
    except OperationalError:
        raise
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
    """Flip a variant to render_status="failed", retrying transient DB loss.

    Used when a pass that DEFERRED its terminal render_status (e.g. an overlay
    pass that handed off to a failing SFX reapply) would otherwise strand the
    variant in "rendering". OperationalError propagates after the local retry
    budget so the owning Celery task's autoretry policy can take over.
    """
    retry_delays_s = (1, 2, 4)
    for attempt in range(len(retry_delays_s) + 1):
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
                                outcome="variant_failed",
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
            return
        except OperationalError as exc:
            if attempt == len(retry_delays_s):
                log.error(
                    "variant_db_write_retry_exhausted",
                    job_id=job_id,
                    variant_id=variant_id,
                    operation="mark_failed",
                    attempts=attempt + 1,
                    error=str(exc),
                )
                raise
            delay_s = retry_delays_s[attempt]
            log.warning(
                "variant_db_write_retry",
                job_id=job_id,
                variant_id=variant_id,
                operation="mark_failed",
                attempt=attempt + 1,
                delay_s=delay_s,
                error=str(exc),
            )
            time.sleep(delay_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("mark_variant_failed_error", job_id=job_id, error=str(exc))
            return


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
    from app.storage import copy_object, object_exists  # noqa: PLC0415

    pass_t0 = time.monotonic()

    # Snapshot only. The expensive audio encode runs after this context exits;
    # the terminal write uses _update_variant_entry's fresh row-locked session.
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("sfx_job_not_found", job_id=job_id)
            return
        persisted = (job.assembly_plan or {}).get("variants") or []
        found = next((v for v in persisted if v.get("variant_id") == variant_id), None)
        existing = dict(found) if found is not None else None
    if existing is None:
        log.error("sfx_variant_not_found", job_id=job_id, variant_id=variant_id)
        return

    current_video_path = existing.get("video_path")
    if not current_video_path:
        log.error("sfx_no_video_path", job_id=job_id, variant_id=variant_id)
        return

    placements = coerce_sound_effects(sfx_raw) or []
    music_treatment = (
        existing.get("background_music_treatment") or existing.get("smart_music_treatment")
        if settings.smart_music_bed_enabled
        else None
    )

    # ── Clear path: remove all effects ────────────────────────────────────────
    if not placements and not music_treatment:
        clean_path = existing.get("pre_sfx_video_path")
        if clean_path and clean_path != current_video_path:
            copy_object(clean_path, current_video_path)
            from app.storage import signed_get_url  # noqa: PLC0415

            signed_url = signed_get_url(current_video_path, expiration_minutes=60 * 24)
        else:
            signed_url = existing.get("output_url", "")

        if not _update_variant_entry(
            job_id,
            variant_id,
            {
                "sound_effects": None,
                "output_url": signed_url,
                "render_status": "ready",
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
            },
            expected_render_gen_id=expected_render_gen_id,
            outcome="sfx_clear",
        ):
            return
        record_pipeline_event(
            "sound_effects",
            "effects_cleared",
            {"variant_id": variant_id, "elapsed_ms": _elapsed_ms(pass_t0)},
        )
        return

    # ── Apply path: mix effects onto the clean base ───────────────────────────
    pre_clean = existing.get("pre_sfx_video_path")
    if not pre_clean:
        pre_clean = current_video_path + "_pre_sfx"
        try:
            if object_exists(pre_clean):
                log.info("sfx_clean_copy_reused", job_id=job_id, dst=pre_clean)
            else:
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
                mute_intervals=muted_sfx_intervals,
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
    except OperationalError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error(
            "sfx_apply_failed",
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            exc_info=True,
        )
        _mark_variant_failed(
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
            expected_render_gen_id=expected_render_gen_id,
        )
        record_pipeline_event(
            "sound_effects",
            "apply_failed",
            {"error": str(exc)[:200], "elapsed_ms": _elapsed_ms(pass_t0)},
        )
        return

    patch: dict[str, Any] = {
        "pre_sfx_video_path": pre_clean,
        "output_url": new_url,
        "render_status": "ready",
        "render_finished_at": datetime.utcnow().isoformat() + "Z",
    }
    if placements or settings.sound_effects_enabled:
        # Empty placements with the lane ON is an explicit clear. With the lane
        # OFF, placements were emptied by the kill switch and persisted SFX stay.
        patch["sound_effects"] = [placement.model_dump() for placement in placements]
    if audio_receipt is not None:
        patch["smart_audio_receipt"] = audio_receipt
    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=expected_render_gen_id,
        outcome="sfx_apply",
    ):
        return
    record_pipeline_event(
        "sound_effects",
        "effects_applied",
        {
            "variant_id": variant_id,
            "effect_count": len(placements),
            "elapsed_ms": _elapsed_ms(pass_t0),
        },
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

# Matte cache-key suffix. v2: RVM backbone + boundary-aware temporal reset +
# oscillation gate + frame-aligned windows (the beach-glitch fix, prod job
# add80a9c). A persisted subject_matte_path WITHOUT this suffix predates the
# fix and may be a glitching matte the old gate accepted — treated as a cache
# miss so the next matte-needing burn recomputes under the v2 key.
_MATTE_CACHE_SUFFIX = ".matte.v2.mp4"

# Persisted marker (a path-shaped sentinel, no GCS object behind it) recorded
# when a freshly computed matte DEFINITIVELY fails the sanity gate (stats
# computed, matte_is_sane False — a property of the footage, not a transient
# error). Reburns reuse the same base video, so recomputing on every text
# edit would fail the same way while burning the full matte budget each time;
# the sentinel short-circuits straight to plain text. Full re-renders reset
# subject_matte_path to None, so new footage retries naturally. Transient
# failures (download/upload/budget/compute error) never mint the sentinel.
_MATTE_UNSTABLE_SUFFIX = ".matte.v2.unstable"


def _matte_delete_allowed(path: str) -> bool:
    """Only job-scoped matte blobs may be deleted by the migration cleanup.

    Prefix guard: `subject_matte_path` comes from persisted variant state,
    and curated `music/*` / `templates/*` assets live in the same bucket —
    a corrupted or maliciously-planted path must never turn this best-effort
    delete into an arbitrary-object delete."""
    return path.startswith("generative-jobs/") and ".matte." in path


# Coverage tolerance when checking a cached matte against freshly derived
# windows: grid snapping + 3dp rounding keep unchanged timings well inside
# this; a genuinely moved overlay lands frames outside it.
_MATTE_COVERAGE_TOLERANCE_S = 0.1


def _matte_covers_windows(provider: Any, windows: list) -> bool:
    """True when every requested window fits inside a stored matte span.

    Duck-typed: providers without ``window_spans`` (test fakes) are assumed
    covered — the check exists to catch text-timing edits after a matte was
    cached, where ``mask_at`` would return None mid-overlay."""
    spans_fn = getattr(provider, "window_spans", None)
    if spans_fn is None:
        return True
    try:
        spans = spans_fn()
    except Exception:  # noqa: BLE001 — best-effort check, never blocks a burn
        return True
    tol = _MATTE_COVERAGE_TOLERANCE_S
    return all(any(s - tol <= w.start_s and w.end_s <= e + tol for s, e in spans) for w in windows)


def _behind_subject_windows(overlays: list[dict], duration_s: float) -> list:
    """Union of padded, duration-clamped windows for every `behind_subject: True`
    overlay. Adjacent/overlapping windows are merged so `compute_subject_matte`
    never re-computes the same span twice. `duration_s` <= 0 skips the upper
    clamp (caller couldn't probe the video — matte compute will still bound
    itself against the actual decoded frame count).

    Window starts are snapped DOWN to the 1/MATTE_FPS frame grid: the 0.25s
    pad is 7.5 frames at 30fps, so an un-snapped start hands `mask_at` a
    constant half-frame offset whose rounding repeats/skips mask indices
    every ~3 frames — a visible judder of the occlusion edge against smooth
    video. Flooring keeps the effective pad >= _SUBJECT_MATTE_WINDOW_PAD_S.
    """
    from app.pipeline.subject_matte import MATTE_FPS, MatteWindow  # noqa: PLC0415

    raw: list[tuple[float, float]] = []
    for ov in overlays:
        if not ov.get("behind_subject"):
            continue
        start_s = max(0.0, float(ov.get("start_s", 0.0)) - _SUBJECT_MATTE_WINDOW_PAD_S)
        start_s = math.floor(start_s * MATTE_FPS + 1e-6) / MATTE_FPS
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


def _cut_boundaries_from_durations(durations: list[float]) -> list[float] | None:
    """Interior hard-cut times on the output timeline of a cut-only montage
    (cumulative slot durations, last slot's end excluded — it isn't a cut)."""
    out: list[float] = []
    t = 0.0
    for d in durations[:-1]:
        t += float(d or 0.0)
        if t > 0:
            out.append(round(t, 3))
    return out or None


def _variant_slot_boundaries(existing: dict) -> list[float] | None:
    """Cut times for an already-rendered montage variant, from its persisted
    timeline (user_timeline wins over ai_timeline, same precedence as the
    cut-preserving reburn path). ``None`` for collage/legacy variants —
    boundary hints are best-effort by design. Slot durations are 3dp-rounded
    and CFR normalization can shift the encoded cut by ±1 frame; a matte
    reset one frame off still removes the cross-clip ghost."""
    try:
        if is_collage_montage_preset(existing.get("montage_preset_rendered")):
            return None
        slots = ((existing.get("user_timeline") or {}).get("slots")) or (
            (existing.get("ai_timeline") or {}).get("slots")
        )
        if not slots:
            return None
        ordered = sorted(slots, key=lambda s: s.get("order", 0))
        durations = [float(s.get("duration_s") or 0.0) for s in ordered if not s.get("removed")]
        return _cut_boundaries_from_durations(durations)
    except Exception:  # noqa: BLE001 — a boundary hint must never break a burn
        return None


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
    cut_boundaries_s: list[float] | None = None,
) -> tuple[Any, str | None, list[dict]]:
    """Best-effort matte resolution for a burn about to happen.

    Returns ``(provider_or_None, matte_gcs_path_or_None, overlays)``. When the
    flag is off, no overlay requests occlusion, or ANY step fails (download,
    compute, sanity check, upload, provider open), ``overlays`` comes back as a
    COPY with every `behind_subject` key stripped — the caller burns plain text
    instead of failing the render — and a `text_behind_subject_fallback` warning
    is logged with the reason. `matte_gcs_path` is `cached_matte_path` unchanged
    on failure (a bad recompute must never clobber a previously-good cache).

    Cache contract: `cached_matte_path` set AND carrying the current
    `_MATTE_CACHE_SUFFIX` → downloaded and opened, never recomputed (the
    "steady state" fast-reburn path). A path WITHOUT the suffix is a v1
    matte from before the beach-glitch fix — possibly a glitching matte the
    old sanity gate accepted — so it is treated as a cache miss: a fresh
    matte is computed under the v2 key and the v1 blob is deleted
    best-effort after a successful upload. On recompute failure the ORIGINAL
    (possibly v1) path is returned unchanged so the next matte-needing burn
    retries the migration. `None` → a fresh matte is computed over the union
    of `behind_subject` windows (padded, duration-clamped), sanity-gated,
    uploaded next to `upload_key_base`, and returned as the new
    `matte_gcs_path` for the caller to persist.

    `cut_boundaries_s` (output-timeline hard-cut times, best-effort) is
    forwarded to `compute_subject_matte` so the segmenter's temporal state
    resets at montage slot joins instead of ghosting the previous clip's
    silhouette across the cut.
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
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415

    original_matte_path = cached_matte_path

    # Known-unstable footage (a prior compute's stats definitively failed the
    # sanity gate): burn plain text immediately — no download, no recompute.
    if cached_matte_path and cached_matte_path.endswith(_MATTE_UNSTABLE_SUFFIX):
        record_pipeline_event(
            "overlay",
            "subject_matte_resolved",
            {
                "variant_id": variant_id,
                "outcome": "cached_unstable",
                "matte_path": cached_matte_path,
            },
        )
        stripped = [{k: v for k, v in ov.items() if k != "behind_subject"} for ov in overlays]
        return None, cached_matte_path, stripped

    stale_matte_path: str | None = None
    if cached_matte_path and not cached_matte_path.endswith(_MATTE_CACHE_SUFFIX):
        stale_matte_path = cached_matte_path
        cached_matte_path = None  # v1 matte — force a recompute under the v2 key

    provider = None
    matte_gcs_path = original_matte_path
    try:
        windows = _behind_subject_windows(behind, duration_s)
        if not windows:
            raise RuntimeError("no renderable behind_subject windows")
        if cached_matte_path:
            try:
                local_matte = os.path.join(tmpdir, "cached_subject_matte.mp4")
                download_to_file(cached_matte_path, local_matte)
                download_to_file(f"{cached_matte_path}.json", f"{local_matte}.json")
                provider = SubjectMatteProvider.open(local_matte)
                if provider is None:
                    raise RuntimeError("cached matte failed to open")
                if not _matte_covers_windows(provider, windows):
                    # Text timing moved since the matte was computed — a
                    # cached matte that doesn't span the requested windows
                    # makes mask_at return None mid-overlay (occlusion
                    # silently drops out). Recompute for the new windows.
                    raise RuntimeError("cached matte does not cover requested windows")
            except Exception as cache_exc:  # noqa: BLE001 — treat as a miss
                # A broken blob/sidecar (or stale coverage) must not poison
                # the cache forever: recompute under the same v2 key
                # (overwrites in place) instead of failing this and every
                # future burn the same way.
                log.warning(
                    "text_behind_subject_cache_miss_recompute",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(cache_exc),
                )
                provider = None
                cached_matte_path = None
        if provider is None:
            local_matte = os.path.join(tmpdir, "computed_subject_matte.mp4")
            stats = compute_subject_matte(
                video_path, windows, local_matte, cut_boundaries_s=cut_boundaries_s
            )
            if stats is None:
                raise RuntimeError("matte compute failed")
            had_v2_cache = bool(
                original_matte_path and original_matte_path.endswith(_MATTE_CACHE_SUFFIX)
            )
            if not matte_is_sane(stats) and had_v2_cache:
                # A v2 cache existed and only this recompute (typically a
                # coverage-miss after a text-timing move) failed the gate —
                # the failure is window-local, not a property of the whole
                # base. Keep the old cache: text moved back into its span
                # works instantly, and the moved window stays retryable.
                raise RuntimeError(f"matte insane for new windows (keeping v2 cache): {stats}")
            if not matte_is_sane(stats) and not cut_boundaries_s:
                # Gate rejection WITHOUT cut hints is ambiguous: on legacy
                # variants (no persisted timeline) and subtitled silence-cut
                # joins, real cuts count as jumps/flips, so the rejection may
                # be the hints' absence, not the footage. Fall back for this
                # burn only — never mint the permanent sentinel from
                # known-incomplete inputs.
                raise RuntimeError(f"matte insane (no cut hints): {stats}")
            if not matte_is_sane(stats):
                # Definitive footage-level rejection — persist the sentinel so
                # every later reburn of this base skips straight to plain text
                # instead of recomputing (and failing) the matte each time.
                sentinel = f"{upload_key_base}{_MATTE_UNSTABLE_SUFFIX}"
                if stale_matte_path and _matte_delete_allowed(stale_matte_path):
                    from app.storage import delete_object_best_effort  # noqa: PLC0415

                    delete_object_best_effort(stale_matte_path)
                    delete_object_best_effort(f"{stale_matte_path}.json")
                log.warning(
                    "text_behind_subject_unstable_footage",
                    job_id=job_id,
                    variant_id=variant_id,
                    stats=str(stats)[:300],
                )
                record_pipeline_event(
                    "overlay",
                    "subject_matte_resolved",
                    {
                        "variant_id": variant_id,
                        "outcome": "unstable_rejected",
                        "matte_path": sentinel,
                        "stats": str(stats)[:200],
                    },
                )
                stripped = [
                    {k: v for k, v in ov.items() if k != "behind_subject"} for ov in overlays
                ]
                return None, sentinel, stripped
            upload_key = f"{upload_key_base}{_MATTE_CACHE_SUFFIX}"
            upload_public_read(local_matte, upload_key)
            upload_public_read(f"{local_matte}.json", f"{upload_key}.json")
            provider = SubjectMatteProvider.open(local_matte)
            if provider is None:
                raise RuntimeError("freshly computed matte failed to open")
            matte_gcs_path = upload_key
            if (
                stale_matte_path
                and stale_matte_path != upload_key
                and _matte_delete_allowed(stale_matte_path)
            ):
                from app.storage import delete_object_best_effort  # noqa: PLC0415

                delete_object_best_effort(stale_matte_path)
                delete_object_best_effort(f"{stale_matte_path}.json")
    except Exception as exc:  # noqa: BLE001 — best-effort, never fails the burn
        log.warning(
            "text_behind_subject_fallback",
            job_id=job_id,
            variant_id=variant_id,
            error=str(exc),
        )
        record_pipeline_event(
            "overlay",
            "subject_matte_resolved",
            {
                "variant_id": variant_id,
                "outcome": "fallback_stripped",
                "error": str(exc)[:200],
            },
        )
        stripped = [{k: v for k, v in ov.items() if k != "behind_subject"} for ov in overlays]
        return None, original_matte_path, stripped

    record_pipeline_event(
        "overlay",
        "subject_matte_resolved",
        {
            "variant_id": variant_id,
            "source": "cache" if cached_matte_path else "computed",
            "matte_path": matte_gcs_path,
        },
    )
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


def _motion_cache_identity(
    *,
    source_path: str,
    runtime_hash: str | None,
    scenes: list[dict],
    asset_identities: list[dict] | None = None,
    source_identity: dict | None = None,
) -> str:
    """Content address a trusted motion cache, including its media references."""
    return hashlib.sha256(
        json.dumps(
            {
                "source": source_identity or {"path": source_path},
                "runtime_hash": runtime_hash,
                "scenes": scenes,
                "assets": asset_identities or [],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _motion_object_identity(object_path: str, *, image: bool = False) -> dict:
    """Resolve the live storage generation used by a motion render."""
    from app.storage import object_metadata  # noqa: PLC0415

    try:
        metadata = object_metadata(object_path)
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError("motion source resource is missing") from exc
    max_bytes = 25 * 1024 * 1024 if image else 4 * 1024 * 1024 * 1024
    if metadata.size <= 0 or metadata.size > max_bytes:
        raise RuntimeError("motion source resource has an invalid size")
    if image and not metadata.content_type.startswith("image/"):
        raise RuntimeError("motion asset object is not an image")
    return {
        "path": metadata.path,
        "generation": metadata.generation,
        "etag": metadata.etag,
        "size": metadata.size,
        "content_type": metadata.content_type,
    }


def _motion_asset_identities(*, job_id: str, scenes: list[dict]) -> list[dict]:
    """Revalidate persisted media refs against the live plan-item asset pool."""
    refs = {
        str(ref["asset_id"]): ref
        for scene in scenes
        if scene.get("preset_id") in {"card_stack", "film_strip"}
        for ref in scene.get("params", {}).get("assets", [])
    }
    if not refs:
        return []

    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.models import PlanItemAsset  # noqa: PLC0415

    try:
        asset_uuids = [uuid.UUID(asset_id) for asset_id in refs]
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise RuntimeError("motion asset reference is not a valid pool id") from exc

    with _sync_session() as db:
        job = db.get(Job, job_uuid)
        if job is None or job.content_plan_item_id is None:
            raise RuntimeError("motion media requires a plan-item asset pool")
        rows = (
            db.execute(
                _select(PlanItemAsset).where(
                    PlanItemAsset.plan_item_id == job.content_plan_item_id,
                    PlanItemAsset.id.in_(asset_uuids),
                )
            )
            .scalars()
            .all()
        )
        by_id = {str(row.id): row for row in rows}
        identities: list[dict] = []
        allowed_prefix = f"users/{job.user_id}/plan/{job.content_plan_item_id}/pool/"
        for asset_id, ref in sorted(refs.items()):
            row = by_id.get(asset_id)
            if (
                row is None
                or row.user_id != job.user_id
                or row.status != "ready"
                or row.kind != "image"
                or row.gcs_path != ref.get("gcs_path")
                or not row.gcs_path.startswith(allowed_prefix)
            ):
                raise RuntimeError("motion asset is no longer an owned ready image")
            identities.append(
                {
                    "asset_id": asset_id,
                    "gcs_path": row.gcs_path,
                    "content_hash": row.content_hash,
                }
            )
    return [
        {**identity, "object": _motion_object_identity(identity["gcs_path"], image=True)}
        for identity in identities
    ]


def _ensure_motion_base(
    *,
    job_id: str,
    variant_id: str,
    variant: dict,
    base_gcs_path: str,
    identity_out: dict | None = None,
) -> tuple[str, str | None]:
    """Return the picture base with the shared motion layer below authored text."""
    raw_scenes = variant.get("motion_scenes")
    if raw_scenes is None or raw_scenes == []:
        return base_gcs_path, None

    from app.config import settings as _motion_settings  # noqa: PLC0415
    from app.pipeline.motion_scene import (  # noqa: PLC0415
        LEGACY_MOTION_RUNTIME_HASH,
        MOTION_RUNTIME_HASH,
        PREVIOUS_MOTION_RUNTIME_HASH,
        apply_motion_scenes,
        validate_motion_instances,
    )

    scenes = validate_motion_instances(raw_scenes)
    required_hash = variant.get("motion_runtime_hash")
    legacy_route_only = required_hash == LEGACY_MOTION_RUNTIME_HASH and all(
        scene.get("preset_id") == "route_trace" for scene in scenes
    )
    compatible_hash = required_hash in {MOTION_RUNTIME_HASH, PREVIOUS_MOTION_RUNTIME_HASH}
    if not compatible_hash and not legacy_route_only:
        raise RuntimeError(
            f"motion runtime mismatch: variant requires {required_hash!r}, "
            f"worker has {MOTION_RUNTIME_HASH!r}"
        )
    cached = variant.get("motion_base_path")
    asset_identities = _motion_asset_identities(job_id=job_id, scenes=scenes)
    source_identity = _motion_object_identity(base_gcs_path)
    renderer_hash = MOTION_RUNTIME_HASH
    cache_identity = _motion_cache_identity(
        source_path=base_gcs_path,
        runtime_hash=renderer_hash,
        scenes=scenes,
        asset_identities=asset_identities,
        source_identity=source_identity,
    )
    if identity_out is not None:
        identity_out["cache_identity"] = cache_identity
        identity_out["renderer_hash"] = renderer_hash
    cache_matches_source = variant.get("motion_base_source_path") == base_gcs_path
    applied_hash = variant.get("motion_applied_runtime_hash")
    cache_matches_runtime = applied_hash == renderer_hash
    cache_is_fresh = bool(
        cached
        and not variant.get("motion_cache_stale")
        and cache_matches_source
        and cache_matches_runtime
        and variant.get("motion_cache_identity") == cache_identity
    )
    if not _motion_settings.motion_scenes_enabled:
        if cache_is_fresh:
            return str(cached), str(cached)
        raise RuntimeError(
            "motion scenes are disabled and this persisted variant needs a cache rebuild"
        )
    if cache_is_fresh:
        log.info(
            "motion_scene_cache_hit",
            job_id=job_id,
            variant_id=variant_id,
            cache_path=str(cached),
            runtime_hash=MOTION_RUNTIME_HASH,
        )
        return str(cached), str(cached)

    cache_path = f"generative-jobs/{job_id}/motion/{variant_id}_{uuid.uuid4().hex[:10]}.mp4"
    apply_motion_scenes(
        base_gcs_path=base_gcs_path,
        instances=scenes,
        output_gcs_path=cache_path,
        job_id=job_id,
        source_generation=source_identity["generation"],
        asset_generations={
            identity["asset_id"]: identity["object"]["generation"] for identity in asset_identities
        },
    )
    return cache_path, cache_path


def _free_retired_motion_base(previous: dict, replacement: str | None) -> None:
    old_path = previous.get("motion_base_path")
    if old_path and old_path != replacement:
        from app.storage import delete_object_best_effort  # noqa: PLC0415

        delete_object_best_effort(old_path)


def _ensure_creator_layer_base(
    *,
    job_id: str,
    variant_id: str,
    variant: dict,
    base_gcs_path: str,
) -> tuple[str, str | None, str | None, str | None, dict]:
    """Compose visual blocks then Creator Blocks on a caption/text-free base.

    Caption reburn and retranscription paths use this shared ordering so neither
    lane can accidentally bypass motion. Newly-created partial caches are
    removed if a later layer fails before the caller can persist them.
    """
    variant = _project_carousel_timed_lanes(variant)
    visual_blocks_cache_path: str | None = None
    motion_cache_path: str | None = None
    motion_base_source_path: str | None = None
    motion_identity: dict = {}
    try:
        render_base_path, visual_blocks_cache_path = _ensure_visual_blocks_base(
            job_id=job_id,
            variant_id=variant_id,
            variant=variant,
            base_gcs_path=base_gcs_path,
        )
        motion_base_source_path = render_base_path
        render_base_path, motion_cache_path = _ensure_motion_base(
            job_id=job_id,
            variant_id=variant_id,
            variant=variant,
            base_gcs_path=render_base_path,
            identity_out=motion_identity,
        )
        return (
            render_base_path,
            visual_blocks_cache_path,
            motion_cache_path,
            motion_base_source_path,
            motion_identity,
        )
    except Exception:
        from app.storage import delete_object_best_effort  # noqa: PLC0415

        for new_path, old_path in (
            (visual_blocks_cache_path, variant.get("visual_blocks_base_path")),
            (motion_cache_path, variant.get("motion_base_path")),
        ):
            if new_path and new_path != old_path:
                delete_object_best_effort(new_path)
        raise


def _creator_layer_cache_patch(
    *,
    visual_blocks_cache_path: str | None,
    motion_cache_path: str | None,
    motion_base_source_path: str | None,
    motion_identity: dict,
) -> dict[str, Any]:
    """Build the persisted cache metadata shared by all terminal render paths."""
    return {
        "visual_blocks_base_path": visual_blocks_cache_path,
        "visual_blocks_cache_stale": False,
        "motion_base_path": motion_cache_path,
        "motion_base_source_path": motion_base_source_path if motion_cache_path else None,
        "motion_cache_stale": False,
        "motion_applied_runtime_hash": (
            motion_identity.get("renderer_hash") if motion_cache_path else None
        ),
        "motion_cache_identity": (
            motion_identity.get("cache_identity") if motion_cache_path else None
        ),
    }


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
    storage_generation: str | None = None,
    created_storage_paths: list[str] | None = None,
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
    from app.pipeline.intro_cluster import (  # noqa: PLC0415
        cluster_style_marker,
        resolve_cluster_style,
    )
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415
    from app.storage import download_to_file, upload_public_read  # noqa: PLC0415

    # All timed lanes persist in pre-insertion time. Every reburn consumes an
    # idempotent render-only projection so a later text/style edit cannot move
    # downstream content back across an already-rendered Carousel.
    existing = _project_carousel_timed_lanes(existing)
    base_gcs_path = existing["base_video_path"]
    reburn_generation = storage_generation or uuid.uuid4().hex
    rank = int(existing.get("rank") or 1)
    reburn_output_key = _variant_storage_key(
        job_id,
        f"variant_{rank}_{variant_id}_text_reburn.mp4",
        reburn_generation,
    )
    if created_storage_paths is not None:
        created_storage_paths.append(reburn_output_key)
    previous_video_path = (existing.get("video_path") or "").lstrip("/") or None
    orientation = _resolve_variant_orientation(existing)
    canvas = canvas_for_orientation(orientation)

    with tempfile.TemporaryDirectory(prefix="nova_reburn_") as tmpdir:
        local_base = os.path.join(tmpdir, "base.mp4")

        def _download_and_validate_base(base_path: str):
            download_to_file(base_path, local_base)
            try:
                probe = probe_video(local_base)
            except Exception as exc:  # noqa: BLE001
                record_pipeline_event(
                    "render",
                    "fast_reburn_base_probe_failed",
                    {
                        "variant_id": variant_id,
                        "orientation": orientation,
                        "base_path": base_path,
                        "error_type": type(exc).__name__,
                    },
                )
                raise CachedBaseProbeError(
                    f"Cached fast-reburn base could not be probed: {type(exc).__name__}"
                ) from exc
            expected_canvas = (canvas.width, canvas.height)
            actual_canvas = (int(probe.width), int(probe.height))
            if actual_canvas != expected_canvas:
                mismatch_data = {
                    "variant_id": variant_id,
                    "orientation": orientation,
                    "base_path": base_path,
                    "expected_width": expected_canvas[0],
                    "expected_height": expected_canvas[1],
                    "actual_width": actual_canvas[0],
                    "actual_height": actual_canvas[1],
                }
                record_pipeline_event(
                    "render",
                    "fast_reburn_base_canvas_mismatch",
                    mismatch_data,
                )
                raise CachedBaseCanvasMismatchError(
                    base_path=base_path,
                    expected=expected_canvas,
                    actual=actual_canvas,
                )
            return probe

        # Validate the immutable source before any visual-block or motion cache
        # can be generated from it. A superseded orientation render may leave
        # this path stale even though the variant's desired orientation changed.
        base_probe = _download_and_validate_base(base_gcs_path)

        visual_blocks_cache_path = None
        motion_cache_path = None
        motion_identity: dict = {}
        try:
            render_base_gcs_path, visual_blocks_cache_path = _ensure_visual_blocks_base(
                job_id=job_id,
                variant_id=variant_id,
                variant=existing,
                base_gcs_path=base_gcs_path,
            )
            if (
                created_storage_paths is not None
                and visual_blocks_cache_path
                and visual_blocks_cache_path != existing.get("visual_blocks_base_path")
            ):
                created_storage_paths.append(visual_blocks_cache_path)
            motion_base_source_path = render_base_gcs_path
            render_base_gcs_path, motion_cache_path = _ensure_motion_base(
                job_id=job_id,
                variant_id=variant_id,
                variant=existing,
                base_gcs_path=render_base_gcs_path,
                identity_out=motion_identity,
            )
            if (
                created_storage_paths is not None
                and motion_cache_path
                and motion_cache_path != existing.get("motion_base_path")
            ):
                created_storage_paths.append(motion_cache_path)
            if render_base_gcs_path != base_gcs_path:
                base_probe = _download_and_validate_base(render_base_gcs_path)
        except Exception:
            from app.storage import delete_object_best_effort  # noqa: PLC0415

            for new_path, old_path in (
                (visual_blocks_cache_path, existing.get("visual_blocks_base_path")),
                (motion_cache_path, existing.get("motion_base_path")),
            ):
                if new_path and new_path != old_path:
                    delete_object_best_effort(new_path)
            raise
        visual_blocks_patch = {
            "visual_blocks_base_path": visual_blocks_cache_path,
            "visual_blocks_cache_stale": False,
            "motion_base_path": motion_cache_path,
            "motion_base_source_path": (motion_base_source_path if motion_cache_path else None),
            "motion_cache_stale": False,
            "motion_applied_runtime_hash": (
                motion_identity.get("renderer_hash") if motion_cache_path else None
            ),
            "motion_cache_identity": (
                motion_identity.get("cache_identity") if motion_cache_path else None
            ),
        }
        base_duration_s = float(base_probe.duration_s)

        # Camera effects are authored in pre-insertion time just like captions
        # and creator lanes. Apply the render-only projected copy to the fresh
        # base before text/captions, while leaving persisted timestamps alone.
        if existing.get("resolved_archetype") == "subtitled" and existing.get("camera_effects"):
            from app.pipeline.camera_effects import normalize_camera_effects  # noqa: PLC0415
            from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415

            projected_camera_effects = normalize_camera_effects(
                existing.get("camera_effects") or [], duration_s=base_duration_s
            )
            if projected_camera_effects:
                camera_base = os.path.join(tmpdir, "projected_camera_base.mp4")
                reframe_and_export(
                    local_base,
                    0.0,
                    base_duration_s,
                    "16:9" if orientation == "landscape" else "9:16",
                    None,
                    camera_base,
                    output_fit="crop",
                    has_audio=base_probe.has_audio,
                    semantic_crop_pulses=projected_camera_effects,
                    **_canvas_kwargs(canvas),
                )
                local_base = camera_base
                base_probe = probe_video(local_base)
                base_duration_s = float(base_probe.duration_s)
                existing = {**existing, "camera_effects": projected_camera_effects}

        # REAPPLY-ON-REBURN, same contract as the persisted SFX/media-overlay
        # lanes (see _reapply_user_media_layers, called by every caller of this
        # shared fast-reburn function) — a custom effect renders UNDER text, so
        # it burns onto local_base BEFORE any of the text-burn branches below,
        # all of which read local_base. Fails open on rejection/render failure.
        from app.tasks.custom_effects_render import (  # noqa: PLC0415
            reapply_persisted_custom_effect,
        )

        custom_effect_cleared = False
        if existing.get("custom_effects"):
            local_base, custom_effect_cleared = reapply_persisted_custom_effect(
                local_base, existing, tmpdir
            )
            if custom_effect_cleared:
                existing = {**existing, "custom_effects": []}

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
                burn_masonry_text_overlays(
                    input_path,
                    overlay_dicts,
                    output_path,
                    tmpdir,
                    duration_s=base_duration_s,
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
                input_probe=base_probe,
                **_canvas_kwargs(canvas),
            )

        if existing.get("resolved_archetype") == "subtitled":
            # A Carousel full rebuild creates a fresh caption-free base. Burn
            # the projected authored text and persisted captions back on top,
            # even when those are the only downstream lanes that exist.
            persisted_snapshot = _fresh_variant_snapshot(job_id, variant_id) or existing
            render_snapshot = _project_carousel_timed_lanes(persisted_snapshot)
            final_path, subtitled_matte_path = _compose_subtitled_final(
                local_base,
                render_snapshot,
                tmpdir,
                job_id=job_id,
                variant_id=variant_id,
                upload_key_base=base_gcs_path,
            )
            output_url = upload_public_read(final_path, reburn_output_key)
            return {
                **visual_blocks_patch,
                "render_status": "ready",
                "ok": True,
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
                "video_path": reburn_output_key,
                "output_url": output_url,
                "text_mode": text_mode,
                "style_set_id": resolved_style_set_id,
                "orientation": orientation,
                # Persist the original base-time lane, never the projected
                # render copy used above.
                "text_elements": persisted_snapshot.get("text_elements") or [],
                "text_elements_user_edited": bool(
                    persisted_snapshot.get("text_elements_user_edited")
                ),
                "subject_matte_path": subtitled_matte_path,
                "_old_video_path_for_delete": previous_video_path,
                **({"custom_effects": []} if custom_effect_cleared else {}),
            }

        if text_mode == "lyrics":
            _lyrics_burn_dicts = _text_element_burn_dicts(existing)
            final_path = os.path.join(tmpdir, "final.mp4")
            _lyrics_matte_path = existing.get("subject_matte_path")
            if _lyrics_burn_dicts:
                _lyrics_dur = base_duration_s or MAX_INTRO_S
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
                        cut_boundaries_s=_variant_slot_boundaries(existing),
                    )
                )
                _burn_text_for_variant(
                    local_base, _lyrics_burn_dicts, final_path, matte=_lyrics_matte
                )
            else:
                shutil.copy2(local_base, final_path)
            output_url = upload_public_read(final_path, reburn_output_key)
            return {
                **visual_blocks_patch,
                "render_status": "ready",
                "ok": True,
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
                "video_path": reburn_output_key,
                "output_url": output_url,
                "text_mode": text_mode,
                "style_set_id": resolved_style_set_id,
                "orientation": orientation,
                "text_elements_user_edited": bool(existing.get("text_elements_user_edited")),
                "subject_matte_path": _lyrics_matte_path,
                "_old_video_path_for_delete": previous_video_path,
                **({"custom_effects": []} if custom_effect_cleared else {}),
            }

        # ── TextElement early branch (T3 — plan-item-timeline) ──────────────
        # When the user has edited text via the TextElement API (T4),
        # their explicitly-authored elements own the overlay completely.
        # Skip the legacy size / style / intro-writer resolution path and
        # compile the elements directly to burn dicts.
        if _TEXT_ELEMENTS_ENABLED and existing.get("text_elements_user_edited"):
            if _should_compose_subtitled_final(existing):
                _fresh_existing = _fresh_variant_snapshot(job_id, variant_id) or existing
                _te_final_path, _te_subtitled_matte_path = _compose_subtitled_final(
                    local_base,
                    _fresh_existing,
                    tmpdir,
                    job_id=job_id,
                    variant_id=variant_id,
                    upload_key_base=base_gcs_path,
                )
                # Subtitled text edits must not overwrite the current key. Signed URLs and
                # CDN layers may keep serving that object, so mint a new key and delete the old.
                _te_gcs_key = reburn_output_key
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
                    "subject_matte_path": _te_subtitled_matte_path,
                    "_old_video_path_for_delete": (
                        _old_video_path
                        if _old_video_path and _old_video_path != _te_gcs_key
                        else None
                    ),
                    **({"custom_effects": []} if custom_effect_cleared else {}),
                }

            _te_burn_dicts = _text_element_burn_dicts(existing)
            _te_final_path = os.path.join(tmpdir, "final.mp4")
            _te_dur = base_duration_s
            _te_provider, _te_matte_path, _te_burn_dicts = _resolve_subject_matte_for_burn(
                video_path=local_base,
                overlays=_te_burn_dicts,
                tmpdir=tmpdir,
                cached_matte_path=existing.get("subject_matte_path"),
                upload_key_base=base_gcs_path,
                duration_s=_te_dur,
                job_id=job_id,
                variant_id=variant_id,
                cut_boundaries_s=_variant_slot_boundaries(existing),
            )
            _burn_text_for_variant(local_base, _te_burn_dicts, _te_final_path, matte=_te_provider)
            _te_gcs_key = reburn_output_key
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
                "_old_video_path_for_delete": previous_video_path,
                **({"custom_effects": []} if custom_effect_cleared else {}),
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

        # Reuse the preflight probe for the reveal window.
        base_dur = base_duration_s
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
        # Set only when this reburn rebuilds the STATIC intro (see below). Empty
        # on the sequence-rebuild and remove_text paths so the persisted marker
        # survives untouched — same merge semantics as `sequence_patch`.
        cluster_style_patch: dict = {}
        reburn_behind_subject = False
        # Carried forward unless this reburn resolves its own placement below —
        # a non-agent_text mode (e.g. an override on a song_text variant) never
        # reaches the resolver, so the persisted snapshot must survive.
        reburn_placement = existing.get("intro_placement")
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
            reburn_placement = _intro_placement_from_params(
                params, has_candidates=bool(existing.get("text_placement_candidates"))
            )

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
                # Sequence-eligible fallback keeps PR #508's editorial restyle;
                # explicit opt-outs (layout/text edits) use the legacy static
                # cluster path so Slice 3a's registry pairing owns the faces.
                _reburn_cs = resolve_cluster_style(
                    editorial=editorial_enabled and sequence_allowed,
                    hero_font=cluster_hero_font_override,
                    body_font=cluster_body_font_override,
                    accent_font=cluster_accent_font_override,
                    hero_size_px=cluster_hero_size_px_override,
                    body_size_px=cluster_body_size_px_override,
                    accent_size_px=cluster_accent_size_px_override,
                )
                # Snapshot for the read adapter (merge-patch: only stamped when
                # this reburn actually rebuilt the static intro).
                cluster_style_patch = {"intro_cluster_style": cluster_style_marker(_reburn_cs)}
                overlays = build_persistent_intro_overlays(
                    reveal_window_s=reveal_window_s,
                    beats=[],  # even-split reveal; talking-head precedent
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
                cut_boundaries_s=_variant_slot_boundaries(existing),
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

        # Every reburn uses a generation-scoped immutable key. The DB generation
        # guard runs after upload, so a superseded worker must never be able to
        # overwrite the bytes referenced by the winning variant row.
        output_url = upload_public_read(final_path, reburn_output_key)

        return {
            **visual_blocks_patch,
            "intro_text": agent_text.text if agent_text else None,
            "intro_highlight_word": (
                getattr(agent_text, "highlight_word", None) if agent_text else None
            ),
            "intro_layout": (reburn_layout if agent_text else None),
            "intro_word_roles": (reburn_word_roles if agent_text else None),
            "intro_mode": (reburn_mode if agent_text else None),
            # Always emitted (merge semantics: an absent key keeps the persisted
            # value) so a placement that just changed can't go stale on the variant.
            "intro_placement": (reburn_placement if agent_text else None),
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
            "video_path": reburn_output_key,
            "output_url": output_url,
            "_old_video_path_for_delete": previous_video_path,
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
            **cluster_style_patch,
            **({"custom_effects": []} if custom_effect_cleared else {}),
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


# Contiguity tolerance for merging adjacent same-source slots: float noise on
# projected windows, never a real footage gap (a genuine cut is >= one beat).
_CONTIGUOUS_SOURCE_EPSILON_S = 0.05


def _merge_contiguous_same_source_steps(
    steps: list,
    *,
    clip_id_to_local: dict[str, str],
    probe_map: dict,
    epsilon_s: float = _CONTIGUOUS_SOURCE_EPSILON_S,
) -> list:
    """Collapse adjacent matcher steps that will render contiguous windows of
    the SAME source clip into one step.

    `_plan_slots`' per-clip cursor makes every repeat use of a clip start
    exactly where the previous slot ended, so a beat-driven recipe matched
    against a single uploaded clip emits back-to-back slots whose seam is an
    invisible cut — renders identically to no cut but shows as two clips in
    the timeline editor (prod job 96771038). Runs on the FRESH-match generative
    path only, before both `_assemble_clips` and `_build_ai_timeline`, so the
    rendered cut structure and the editor timeline agree.

    Windows are projected with the same cursor + footage-trim arithmetic the
    planner applies under `allow_slowdown_fill=False`; adjacent same-clip steps
    whose windows are contiguous within `epsilon_s` merge (first step's
    slot/moment identity kept, target durations summed, moment end extended).
    Pinned steps (`locked`/`exact_window` — user-timeline windows) and steps
    carrying their own text overlays never merge.
    """
    if len(steps) < 2:
        return list(steps)

    def _project(step: Any, cursors: dict[str, float]) -> tuple[bool, float, float]:
        slot = getattr(step, "slot", None) or {}
        moment = getattr(step, "moment", None) or {}
        pinned = bool(slot.get("locked") or slot.get("exact_window"))
        duration = max(float(slot.get("target_duration_s") or 0.0), 0.5) * float(
            slot.get("speed_factor") or 1.0
        )
        if pinned:
            start = float(moment.get("start_s") or 0.0)
            duration = max(0.5, float(moment.get("end_s", start + duration)) - start)
        elif step.clip_id in cursors:
            start = cursors[step.clip_id]
        else:
            start = float(moment.get("start_s") or 0.0)
        probe = probe_map.get(clip_id_to_local.get(step.clip_id))
        clip_dur = float(getattr(probe, "duration_s", 0.0) or 0.0)
        if not pinned and clip_dur > 0.0 and start + duration > clip_dur:
            available = max(0.0, clip_dur - start)
            if available > 0.0:
                duration = available  # footage-exhausted trim (no slowdown fill)
            else:
                start = max(0.0, clip_dur - duration)  # clamp-and-warn branch
        return pinned, start, duration

    merged: list = []
    windows: list[tuple[bool, float, float]] = []  # (pinned, start_s, duration_s)
    cursors: dict[str, float] = {}
    for step in steps:
        pinned, start, duration = _project(step, cursors)
        if not pinned:
            cursors[step.clip_id] = start + duration
        slot = getattr(step, "slot", None) or {}
        prev_win = windows[-1] if windows else None
        if (
            merged
            and not pinned
            and prev_win is not None
            and not prev_win[0]
            and merged[-1].clip_id == step.clip_id
            and not slot.get("text_overlays")
            and abs((prev_win[1] + prev_win[2]) - start) <= epsilon_s
        ):
            prev = merged[-1]
            prev_slot = dict(getattr(prev, "slot", None) or {})
            prev_slot["target_duration_s"] = round(
                float(prev_slot.get("target_duration_s") or 0.0)
                + float(slot.get("target_duration_s") or 0.0),
                3,
            )
            if "target_duration_pct" in prev_slot or "target_duration_pct" in slot:
                pct_a = prev_slot.get("target_duration_pct")
                pct_b = slot.get("target_duration_pct")
                prev_slot["target_duration_pct"] = (
                    round(float(pct_a) + float(pct_b), 6)
                    if pct_a is not None and pct_b is not None
                    else None
                )
            merged_end = prev_win[1] + prev_win[2] + duration
            prev_moment = dict(getattr(prev, "moment", None) or {})
            prev_moment["end_s"] = round(merged_end, 3)
            merged[-1] = type(prev)(slot=prev_slot, clip_id=prev.clip_id, moment=prev_moment)
            windows[-1] = (False, prev_win[1], prev_win[2] + duration)
            continue
        merged.append(step)
        windows.append((pinned, start, duration))
    return merged


_AUTO_CAROUSEL_FALLBACK_DURATION_S = 3.0


def _stable_seed_from_variant(variant_id: str | None) -> int:
    """Deterministic seed derived from a variant identifier — stable across
    processes/restarts (unlike Python's per-process-randomized `hash()` for
    strings), so a re-render of the same variant (no explicit
    `moment_cfg["seed"]`) still lands on the same director choice."""
    import zlib  # noqa: PLC0415

    return zlib.crc32((variant_id or "carousel-moment").encode("utf-8"))


_CAROUSEL_POSITION_WEIGHTS: dict[str, float] = {"intro": 0.5, "middle": 0.3, "outro": 0.2}

# `CAROUSEL_MOMENT_UNSET` (the tri-state sentinel for
# `regenerate_generative_variant`'s `carousel_moment_override` kwarg) is
# defined near the top of this module (before `regenerate_generative_variant`
# itself), NOT here — it's used as a default-argument value, which Python
# evaluates at `def`-time, so it must exist before that function is defined.

# Boundary crossfade applied at a carousel moment's edge(s) when the editor
# requests `carousel_moment.transition == "crossfade"` — see
# `_insert_carousel_moment_step`. "crossfade" is already the INTERNAL
# transition vocabulary value (`transitions.translate_transition("crossfade")
# == "crossfade"`; XFADE_MAP maps it to ffmpeg's `fade`), matching the same
# literal `slot["transition_in"]` value `_prepare_timeline_assembly` writes
# for the identical user-facing "crossfade" choice on the timeline editor.
_CAROUSEL_MOMENT_TRANSITION_DURATION_S = 0.4


def _project_carousel_timed_lanes(variant: dict[str, Any]) -> dict[str, Any]:
    """Build a render-only ripple projection; persisted base times stay stable."""
    if variant.get("_carousel_lanes_projected"):
        return variant
    cfg = variant.get("carousel_moment") or {}
    insertion_s = variant.get("carousel_insertion_base_s")
    ripple_s = variant.get(
        "carousel_ripple_duration_s", variant.get("carousel_inserted_duration_s")
    )
    if cfg.get("timing_model") != "ripple_v1" or insertion_s is None or not ripple_s:
        return variant
    insertion_s = float(insertion_s)
    ripple_s = float(ripple_s)

    def point(value: Any) -> float:
        seconds = float(value or 0.0)
        return seconds + ripple_s if seconds >= insertion_s - 1e-6 else seconds

    projected = {**variant, "_carousel_lanes_projected": True}
    for field in (
        "caption_cues",
        "text_elements",
        "visual_blocks",
        "media_overlays",
        "camera_effects",
    ):
        if not isinstance(variant.get(field), list):
            continue
        projected[field] = [
            {
                **item,
                **({"start_s": point(item["start_s"])} if "start_s" in item else {}),
                **({"end_s": point(item["end_s"])} if "end_s" in item else {}),
            }
            for item in variant[field]
            if isinstance(item, dict)
        ]
    if isinstance(variant.get("sound_effects"), list):
        projected["sound_effects"] = [
            {
                **item,
                **({"at_s": point(item["at_s"])} if "at_s" in item else {}),
                **({"end_s": point(item["end_s"])} if item.get("end_s") is not None else {}),
            }
            for item in variant["sound_effects"]
            if isinstance(item, dict)
        ]
    if isinstance(variant.get("motion_scenes"), list):
        insertion_frame = round(insertion_s * 30)
        duration_frames = round(ripple_s * 30)
        projected["motion_scenes"] = [
            {
                **item,
                "start_frame": int(item.get("start_frame", 0))
                + (duration_frames if int(item.get("start_frame", 0)) >= insertion_frame else 0),
                "end_frame_exclusive": int(item.get("end_frame_exclusive", 0))
                + (
                    duration_frames
                    if int(item.get("end_frame_exclusive", 0)) >= insertion_frame
                    else 0
                ),
            }
            for item in variant["motion_scenes"]
            if isinstance(item, dict)
        ]
    return projected


def _carousel_boundary_duration(
    requested_s: Any,
    before_duration_s: float,
    after_duration_s: float,
) -> float:
    """Render-safe boundary overlap: 0.1..1s, capped to 30% of both sides."""
    requested = (
        _CAROUSEL_MOMENT_TRANSITION_DURATION_S if requested_s is None else float(requested_s)
    )
    return round(
        max(
            0.0,
            min(1.0, max(0.1, requested), before_duration_s * 0.3, after_duration_s * 0.3),
        ),
        3,
    )


def _carousel_step_duration_s(step: Any) -> float:
    moment = getattr(step, "moment", None) or {}
    return max(0.0, float(moment.get("end_s", 0.0)) - float(moment.get("start_s", 0.0)))


def _carousel_step_overlap_s(previous_step: Any, step: Any) -> float:
    """Actual output overlap into `step`, using the renderer's 30% cap."""
    slot = getattr(step, "slot", None) or {}
    transition = str(slot.get("transition_in") or "none")
    if transition in {"none", "hard-cut", "cut"}:
        return 0.0
    requested = slot.get("transition_duration_s")
    requested_s = 0.3 if requested is None else float(requested)
    return max(
        0.0,
        min(
            requested_s,
            _carousel_step_duration_s(previous_step) * 0.3,
            _carousel_step_duration_s(step) * 0.3,
        ),
    )


def _carousel_base_insertion_s(steps: list[Any], insertion_index: int) -> float:
    """Output-clock start of the replaced base boundary, overlaps included."""
    cursor_s = 0.0
    for index, step in enumerate(steps):
        overlap_s = _carousel_step_overlap_s(steps[index - 1], step) if index > 0 else 0.0
        start_s = cursor_s - overlap_s
        if index == insertion_index:
            return max(0.0, start_s)
        cursor_s = start_s + _carousel_step_duration_s(step)
    return max(0.0, cursor_s)


def _merge_carousel_moment_override(
    existing_cfg: dict[str, Any] | None,
    override: Any,
) -> dict[str, Any] | None:
    """Merge a carousel-editor edit onto a variant's persisted `carousel_moment`.

    `override` is one of three states (see `CAROUSEL_MOMENT_UNSET`'s docstring):
      - `CAROUSEL_MOMENT_UNSET`: no edit requested this render — `existing_cfg`
        carries forward unchanged (same lifecycle as `music_start_s` /
        `user_style_knobs` on the spec dict this feeds into).
      - `None`: explicit removal — the variant loses its carousel moment.
      - `dict`: partial edit, already field-validated by
        `dispatch_edit_variant`. Present keys win over `existing_cfg`; absent
        keys keep whatever `existing_cfg` already had (or nothing, for a
        variant with no prior moment). `focus_clip_index` (an int, or `None`
        to clear) is translated to the `focus` list-of-dicts shape
        `_apply_moment_overrides`/`_parse_focus_override` already consume:
        `[{"card_index": n}]` — AND kept verbatim as `focus_clip_index`
        alongside it: the render pipeline only ever reads `focus`, but the
        editor-contract/UI/copilot-snapshot side reads `focus_clip_index`
        (flat int, per the API contract on `CarouselMoment`/
        `CarouselMomentEditRequest`). Persisting only the translated shape
        made the editor panel prefill "Let Nova pick" even when a specific
        clip was chosen — the panel's `current?.focus_clip_index` never
        existed on the persisted dict. Both keys now carry the same value so
        neither reader needs to know about the other's shape.

    ANY key present in `override` sets `auto=False` on the merged result —
    once the user has touched the moment, the auto-director must stop
    re-rolling mode/effect on their behalf (product decision, see the API
    contract in the carousel-editor plan). `seed` is not touched here; it
    stays whatever `existing_cfg` already carried (or absent, for a brand
    new manual moment), keeping any pinned focus/rolling choreography
    deterministic across re-renders.
    """
    if override == CAROUSEL_MOMENT_UNSET:
        return existing_cfg
    if override is None:
        return None
    merged: dict[str, Any] = dict(existing_cfg or {})
    if override:
        merged["auto"] = False
        if "focus_clip_index" in override:
            idx = override["focus_clip_index"]
            if idx is None:
                merged.pop("focus", None)
                merged.pop("focus_clip_index", None)
            else:
                merged["focus"] = [{"card_index": int(idx)}]
                merged["focus_clip_index"] = int(idx)
        for key in (
            "position",
            "mode",
            "effect",
            "duration_s",
            "transition",
            "sequence",
            "move_duration_s",
            "zoom_duration_s",
            "transition_in",
            "transition_in_duration_s",
            "transition_out",
            "transition_out_duration_s",
            "timing_model",
        ):
            if key in override:
                merged[key] = override[key]
        if merged.get("timing_model") == "ripple_v1":
            # Upgrade sparse legacy boundaries exactly once at the API merge
            # boundary. Explicit new fields above win; the old single
            # transition remains a supported read/write input.
            legacy_transition = merged.get("transition", "crossfade")
            merged.setdefault("transition_in", legacy_transition)
            merged.setdefault("transition_in_duration_s", 0.4)
            merged.setdefault("transition_out", legacy_transition)
            merged.setdefault("transition_out_duration_s", 0.4)
    return merged or None


def _author_carousel_moments(
    initial_specs: list[dict[str, Any]], *, job_id: str, n_clips: int
) -> None:
    """Authoring policy: attach `spec["carousel_moment"]` to ONE eligible spec
    in `initial_specs`, in place, so the already-merged render hook
    (`_insert_carousel_moment_step`) actually fires on real generative jobs
    instead of only on manually-crafted specs.

    Rules:
      - No-op unless BOTH `settings.carousel_effects_enabled` (the render
        flag) AND `settings.carousel_auto_author_enabled` (this policy) are
        on. Authoring without the render flag would silently do nothing
        anyway — checked separately from the render flag for clarity at this
        call site.
      - No-op if `n_clips < 3` — a carousel moment needs enough footage to
        feel like a deck, not a rehash of the only 1-2 clips already in the
        montage (mirrors `director.FOCUS_QUALIFY_MIN_CLIPS`'s spirit, applied
        at the whole-job level here).
      - Eligible specs are montage-path specs: no `archetype` key (excludes
        talking_head/narrated/subtitled AND voiceover — voiceover renders
        through the montage path too but narration timing shouldn't get a
        carousel splice in v1) and no pre-existing `carousel_moment` key
        (respects an already-authored or explicitly-configured spec).
      - Exactly one eligible spec is chosen via
        `random.Random(zlib.crc32(f"carousel-author:{job_id}".encode()))` —
        the same crc32-seeding idiom as `_stable_seed_from_variant`, so a
        re-run of the same job (no new job_id) always picks the same variant.
      - Position is a seeded weighted choice off the same RNG:
        `_CAROUSEL_POSITION_WEIGHTS` (intro 0.5 / middle 0.3 / outro 0.2).
      - The moment's own seed is `_stable_seed_from_variant(job_id + variant
        identifier)` — deterministic per (job, variant), independent of which
        OTHER variant the job-level draw above picked.
      - Emits `record_pipeline_event("assembly", "carousel_moment_authored",
        ...)` when (and only when) it authors a spec — the orchestrator
        already runs inside `pipeline_trace_for`, so no extra context binding
        is needed here.
    """
    if not (settings.carousel_effects_enabled and settings.carousel_auto_author_enabled):
        return
    if n_clips < 3:
        return

    eligible = [
        spec for spec in initial_specs if "archetype" not in spec and "carousel_moment" not in spec
    ]
    if not eligible:
        return

    import random  # noqa: PLC0415
    import zlib  # noqa: PLC0415

    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    rng = random.Random(zlib.crc32(f"carousel-author:{job_id}".encode()))
    chosen = rng.choice(eligible)
    position = rng.choices(
        list(_CAROUSEL_POSITION_WEIGHTS),
        weights=list(_CAROUSEL_POSITION_WEIGHTS.values()),
        k=1,
    )[0]

    variant_ident = str(chosen.get("variant_id") or chosen.get("text_mode") or "variant")
    moment_seed = _stable_seed_from_variant(f"{job_id}:{variant_ident}")

    chosen["carousel_moment"] = {"auto": True, "seed": moment_seed, "position": position}

    record_pipeline_event(
        "assembly",
        "carousel_moment_authored",
        {"variant": variant_ident, "position": position},
    )
    log.info(
        "generative_carousel_moment_authored",
        job_id=job_id,
        variant=variant_ident,
        position=position,
    )


def _parse_focus_override(raw: Any) -> tuple | None:
    """Best-effort: converts a `moment_cfg["focus"]` list of
    `{"card_index", "hold_s"?, "zoom_s"?}` dicts into a tuple of
    `choreography.FocusMoment`. Returns `None` (caller treats that as "no
    override applied") on any shape mismatch or if `FocusMoment` hasn't
    landed yet — never raises, matching the never-raise contract this whole
    region operates under."""
    try:
        from app.pipeline.carousel.choreography import FocusMoment  # noqa: PLC0415

        return tuple(
            FocusMoment(
                card_index=int(item["card_index"]),
                **{k: v for k, v in item.items() if k in ("hold_s", "zoom_s")},
            )
            for item in raw
        )
    except Exception:  # noqa: BLE001 — override parsing is best-effort, never raise
        log.warning("carousel_moment_focus_override_unparseable", exc_info=True)
        return None


def _apply_moment_overrides(spec: Any, moment_cfg: dict[str, Any]) -> Any:
    """Explicit `moment_cfg` fields win over whatever produced `spec` — the
    director's auto-picked choices, or the plain defaults the non-auto path
    below already applied. Introspects `spec`'s own dataclass fields
    (instead of assuming they exist) so this degrades gracefully if
    `mode`/`focus_moments`/`seed` haven't landed on `CarouselMomentSpec` yet:
    an override for a field that doesn't exist is a logged no-op, never a
    crash. Factored out so override precedence is independently unit
    testable without going through a full render."""
    override_keys = {
        "effect",
        "duration_s",
        "mode",
        "focus",
        "sequence",
        "seed",
        "move_duration_s",
        "zoom_duration_s",
        "timing_model",
    }
    if not override_keys & moment_cfg.keys():
        return spec  # nothing to override; skip the dataclass introspection below

    from dataclasses import fields  # noqa: PLC0415

    field_names = {f.name for f in fields(spec)}
    overrides: dict[str, Any] = {}

    if "effect" in moment_cfg and "effect" in field_names:
        overrides["effect"] = moment_cfg["effect"]
    if "duration_s" in moment_cfg and "duration_s" in field_names:
        overrides["duration_s"] = float(moment_cfg["duration_s"])
        # An explicit duration_s also caps a mode="focus" moment's natural
        # choreography length (ignored otherwise — see CarouselMomentSpec's
        # docstring on this field). Auto-authored moments never set
        # "duration_s" in moment_cfg, so this never fires for them.
        if "focus_duration_cap_s" in field_names:
            overrides["focus_duration_cap_s"] = float(moment_cfg["duration_s"])
    if "mode" in moment_cfg:
        if "mode" in field_names:
            overrides["mode"] = moment_cfg["mode"]
        else:
            log.warning("carousel_moment_mode_override_unsupported_schema")
    if "focus" in moment_cfg:
        if "focus_moments" in field_names:
            focus_moments = _parse_focus_override(moment_cfg["focus"])
            if focus_moments is not None:
                overrides["focus_moments"] = focus_moments
        else:
            log.warning("carousel_moment_focus_override_unsupported_schema")
    if moment_cfg.get("timing_model") == "ripple_v1" and "manual_timing" in field_names:
        overrides["manual_timing"] = True
    if "move_duration_s" in moment_cfg and "move_duration_s" in field_names:
        overrides["move_duration_s"] = float(moment_cfg["move_duration_s"])
    if "sequence" in moment_cfg and moment_cfg["sequence"] is not None:
        if "focus_moments" in field_names:
            zoom_s = float(moment_cfg.get("zoom_duration_s", 0.6))
            focus_moments = _parse_focus_override(
                [
                    {
                        "card_index": item["clip_index"],
                        "hold_s": item["hold_s"],
                        "zoom_s": zoom_s,
                    }
                    for item in moment_cfg["sequence"]
                ]
            )
            if focus_moments is not None:
                overrides["focus_moments"] = focus_moments
    if "seed" in moment_cfg and "seed" in field_names:
        overrides["seed"] = moment_cfg["seed"]

    if not overrides:
        return spec
    return replace(spec, **overrides)


def _carousel_clip_signal(meta: Any | None) -> tuple[float, tuple[str, ...]]:
    """Map one clip's Gemini analysis (`ClipMeta`) to `director.ClipInfo`'s
    `(interest, labels)` — best-effort: any missing/malformed field falls
    back to `ClipInfo`'s own defaults (`interest=0.5`, `labels=()`), never
    raises.

    Normalization: `ClipMeta.hook_score` is 0..10 (`app/agents/clip_metadata.py`
    `hook_score: float = Field(..., ge=0, le=10)`; same range documented in the
    `analyze_clip` prompt template in `app/pipeline/prompt_loader.py`:
    `"hook_score": float 0–10`) — divide by 10 for `ClipInfo.interest`'s 0..1
    range. When `hook_score` is absent/zero, fall back to the clip's own peak
    `best_moments[].energy` (same 0–10 scale per the same prompt/schema) as a
    weaker but still-real interest signal. `detected_subject` (free-text) maps
    1:1 to `labels` when non-empty.
    """
    if meta is None:
        return 0.5, ()

    interest = 0.5
    hook_score = getattr(meta, "hook_score", None)
    if isinstance(hook_score, (int, float)) and hook_score > 0:
        interest = max(0.0, min(1.0, float(hook_score) / 10.0))
    else:
        best_moments = getattr(meta, "best_moments", None) or []
        energies = [float(m.get("energy", 0.0) or 0.0) for m in best_moments if isinstance(m, dict)]
        if energies:
            interest = max(0.0, min(1.0, max(energies) / 10.0))

    subject = str(getattr(meta, "detected_subject", "") or "").strip()
    labels = (subject,) if subject else ()
    return interest, labels


def _direct_auto_carousel_spec(
    moment_cfg: dict[str, Any],
    *,
    clip_paths: list[str],
    probe_map: dict | None,
    variant_id: str | None,
    clip_metas: list | None = None,
) -> Any:
    """`moment_cfg["auto"]` path: build `director.ClipInfo`s from the local
    clip paths + whatever duration the probe map already has for them (the
    probe map is computed once per variant upstream — see
    `_insert_carousel_moment_step`'s caller in `_render_generative_variant`),
    ask the DIRECTOR for a spec, then let any explicit `moment_cfg` field
    override its choice via `_apply_moment_overrides`.

    `clip_metas` (optional, the job's Gemini `ClipMeta` list) is matched back
    to each `clip_paths` entry via `ClipMeta.clip_path` — populated by every
    branch of `_analyze_clips_parallel` (`app/tasks/template_orchestrate.py`)
    with the SAME local path string used to build `clip_id_to_local`, so a
    plain dict lookup is exact, no clip_id plumbing needed here. Each matched
    meta's `(interest, labels)` come from `_carousel_clip_signal`. Omitting
    `clip_metas` (the default) leaves `ClipInfo` at its neutral defaults —
    byte-identical to before this parameter existed.
    """
    from app.pipeline.carousel import director as director_mod  # noqa: PLC0415

    probe_map = probe_map or {}
    meta_by_path: dict[str, Any] = {}
    for meta in clip_metas or []:
        meta_path = str(getattr(meta, "clip_path", "") or "")
        if meta_path:
            meta_by_path[meta_path] = meta

    clips = []
    for path in clip_paths:
        probe = probe_map.get(path)
        duration_s = float(getattr(probe, "duration_s", 0.0) or 0.0)
        if duration_s <= 0:
            duration_s = _AUTO_CAROUSEL_FALLBACK_DURATION_S
        interest, labels = _carousel_clip_signal(meta_by_path.get(path))
        clips.append(
            director_mod.ClipInfo(
                path=path, duration_s=duration_s, interest=interest, labels=labels
            )
        )

    seed = moment_cfg.get("seed")
    if not isinstance(seed, int):
        seed = _stable_seed_from_variant(variant_id)

    target_duration_s = (
        float(moment_cfg["duration_s"])
        if "duration_s" in moment_cfg
        else director_mod.DEFAULT_TARGET_DURATION_S
    )

    spec = director_mod.direct_carousel_moment(
        clips,
        seed=seed,
        target_duration_s=target_duration_s,
        # Stills excluded from AUTO authoring per product decision
        # 2026-08-06: static cards (no video playback, no tile-focus-expand)
        # read as a broken moment in real edits. Auto-authored moments must
        # always be dynamic — either every tile playing live video
        # ("rolling") or the center-tile-plays-then-expands-to-fullscreen
        # choreography ("focus", the flagship). An explicit (non-auto)
        # moment_cfg may still request mode="stills" via
        # `_apply_moment_overrides` below — this restriction only applies
        # to the director's own auto-pick.
        allowed_modes=("focus", "rolling"),
    )
    return _apply_moment_overrides(spec, moment_cfg)


def _maybe_render_carousel_moment(
    moment_cfg: dict[str, Any],
    *,
    clip_id_to_local: dict[str, str],
    steps: list,
    variant_dir: str,
    probe_map: dict | None = None,
    variant_id: str | None = None,
    clip_metas: list | None = None,
    render_meta: dict[str, Any] | None = None,
) -> str | None:
    """Render one Blossom-carousel moment segment for this variant.

    Belt-and-braces around `render_carousel_moment`'s never-raise contract: any
    exception — including an import-time failure of the carousel/skia-adjacent
    modules themselves (e.g. a missing native lib on a given machine/arch) and
    an unexpected bug in that never-supposed-to-raise function — is caught
    here too, logged, and treated as "skip the moment": this helper itself
    never raises. Callers only reach this when `settings.carousel_effects_enabled`
    is True AND the spec actually requests a moment, so the carousel/skia-
    adjacent modules are imported LAZILY here — the flag-off path imports none
    of this.

    `moment_cfg["auto"]` truthy hands mode/effect/focus selection to the
    DIRECTOR (`app.pipeline.carousel.director.direct_carousel_moment`,
    imported lazily so non-auto/flag-off callers never touch it either);
    explicit `effect`/`mode`/`focus`/`duration_s`/`seed` keys in `moment_cfg`
    still override the director's picks (`_apply_moment_overrides`). Without
    `"auto"`, behavior is unchanged from before the director existed —
    `mode`/`focus`/`seed` now pass through to `CarouselMomentSpec` too if
    present in `moment_cfg`, but a `moment_cfg` that only ever set
    `effect`/`duration_s`/`position` (every caller as of this writing) is
    byte-identical.

    `clip_metas` (optional) is forwarded to `_direct_auto_carousel_spec` for
    the auto path's per-clip interest/labels signal; omitted (the default)
    keeps `ClipInfo` at its neutral defaults, same as before this param
    existed.

    `render_meta` (optional): when a caller passes an (empty) dict, it is
    populated with the built spec's `{"effect", "mode"}` right before the
    render call — a side-channel so `_insert_carousel_moment_step` can attach
    those fields to its `carousel_moment_inserted` trace event without
    changing this function's `str | None` return contract (every existing
    caller/test that doesn't pass `render_meta` is unaffected).
    """
    try:
        from app.pipeline.carousel.segment import (  # noqa: PLC0415
            CarouselMomentSpec,
            render_carousel_moment,
        )

        clip_paths: list[str] = []
        card_index_by_clip_index: dict[int, int] = {}
        for step in steps:
            step_clip_id = getattr(step, "clip_id", None)
            # Belt-and-braces (carousel-inside-carousel guard): a rendered
            # carousel-moment segment is a synthetic, locally-rendered
            # composite (see `_insert_carousel_moment_step`'s
            # `synthetic_id = f"__carousel_{variant_id}"`), never a real
            # source clip — it must never become a CARD SOURCE for a new
            # moment. `steps` should already be clean on every traced call
            # path (fresh match / `_prepare_timeline_assembly` both derive
            # strictly from the job's real `clip_paths`), but this filter
            # makes that invariant hold even if a future code path lets a
            # stale synthetic step slip through.
            if isinstance(step_clip_id, str) and step_clip_id.startswith("__carousel_"):
                continue
            local_path = clip_id_to_local.get(step_clip_id)
            if local_path and local_path not in clip_paths:
                card_index = len(clip_paths)
                clip_paths.append(local_path)
                if isinstance(step_clip_id, str) and step_clip_id.startswith("clip_"):
                    try:
                        card_index_by_clip_index[int(step_clip_id.removeprefix("clip_"))] = (
                            card_index
                        )
                    except ValueError:
                        pass
            if len(clip_paths) >= 5:
                break
        if not clip_paths:
            return None

        render_moment_cfg = moment_cfg
        if moment_cfg.get("timing_model") == "ripple_v1" and moment_cfg.get("sequence"):
            mapped_sequence: list[dict[str, Any]] = []
            for item in moment_cfg["sequence"]:
                card_index = card_index_by_clip_index.get(int(item["clip_index"]))
                if card_index is None:
                    log.warning(
                        "carousel_manual_sequence_clip_unavailable",
                        clip_index=item["clip_index"],
                    )
                    return None
                mapped_sequence.append({**item, "clip_index": card_index})
            render_moment_cfg = {**moment_cfg, "sequence": mapped_sequence}

        if render_moment_cfg.get("auto"):
            spec = _direct_auto_carousel_spec(
                render_moment_cfg,
                clip_paths=clip_paths,
                probe_map=probe_map,
                variant_id=variant_id,
                clip_metas=clip_metas,
            )
        else:
            spec = CarouselMomentSpec(
                effect=render_moment_cfg.get("effect", "scale_sweep"),
                clip_paths=tuple(clip_paths),
                duration_s=float(render_moment_cfg.get("duration_s", 4.0)),
            )
            spec = _apply_moment_overrides(spec, render_moment_cfg)

        if render_meta is not None:
            render_meta["effect"] = getattr(spec, "effect", None)
            render_meta["mode"] = getattr(spec, "mode", None)

        return render_carousel_moment(spec, variant_dir)
    except Exception:  # noqa: BLE001 — the contract says never-raise; don't trust it blindly
        log.warning("carousel_moment_render_failed", exc_info=True)
        return None


def _insert_carousel_moment_step(
    steps: list,
    spec: dict[str, Any],
    *,
    clip_id_to_local: dict[str, str],
    clip_id_to_gcs: dict[str, str],
    probe_map: dict,
    variant_dir: str,
    clip_metas: list | None = None,
    inserted_duration_out: dict[str, float] | None = None,
) -> list:
    """Splice a rendered Blossom-carousel moment into the montage `steps` list.

    No-op — zero carousel imports, `steps` returned unchanged — unless
    `settings.carousel_effects_enabled` is True AND `spec["carousel_moment"]`
    is present. This is the entire kill switch: additive only, so the
    flag-off (or spec-absent) output is byte-identical to pre-feature.

    On success the rendered segment is registered as a synthetic clip (mirrors
    the clip_id -> local path / clip_id -> probe maps the rest of this module
    uses) and spliced in as an exact-window AssemblyStep, matching the shape
    `_prepare_timeline_assembly` builds for user-pinned timeline windows.
    `position` ("intro" | "middle" | "outro", default "intro") controls where.

    `clip_metas` (optional, the job's Gemini `ClipMeta` list — a parameter of
    `_render_generative_variant`, in scope at its call site) is forwarded to
    `_maybe_render_carousel_moment` for the auto-director's interest/labels
    signal; omitted it behaves exactly as before this param existed.

    Trace events (admin job-debug view, not the never-raise contract — a
    `record_pipeline_event` failure is itself swallowed, see that function):
    `carousel_moment_inserted` on a successful splice (effect/mode come from
    `_maybe_render_carousel_moment`'s `render_meta` side-channel, duration_s
    is the PROBED rendered duration, not the requested one);
    `carousel_moment_skipped` when the render came back empty (caught
    exception or a legitimate `None` from `render_carousel_moment` — this
    function can't tell those apart, both surface as a falsy `moment_path`)
    or when the probe of the rendered segment itself fails or reports a
    non-positive duration — each alongside the pre-existing `log.warning`
    (probe failure) or logged inside `_maybe_render_carousel_moment` (render
    failure).

    Note: the synthetic clip has no GCS-backed source (it's a locally rendered
    composite, not one of the user's uploaded clips), so `_build_ai_timeline`'s
    clip_paths-index lookup can't map it — that function is documented
    best-effort and returns None gracefully rather than raising, so a carousel
    moment simply means no post-render clip-timeline editor for that variant.

    `moment_cfg["transition"] == "crossfade"` (the carousel editor's boundary
    option) sets `transition_in`/`transition_duration_s` on BOTH edges of the
    splice: the moment step's own slot (the incoming edge, from whatever
    precedes it) and the step immediately after the insertion point (the
    outgoing edge, into whatever follows). `_assemble_clips`/`_plan_slots`
    only ever consult a step's `transition_in` to describe the cut INTO that
    step — i.e. it never reads the very first step's own value — so for
    `position="intro"` (insertion index 0) only the outgoing edge actually
    renders; for `position="outro"` there is no next step, so only the
    incoming edge applies; `"middle"` gets both. `moment_cfg["transition"]`
    absent/"none"/anything else is a no-op — same hard-cut boundary as before
    this option existed.

    `inserted_duration_out` (optional): when provided (an empty dict), the
    successfully-rendered moment's PROBED duration is written to
    `inserted_duration_out["duration_s"]` — the side-channel
    `_render_generative_variant` uses to extend a voiceover variant's
    `-t` mix-truncation target by the spliced moment's length (otherwise the
    voiceover mix, sized to the voice BEFORE the splice, chops the moment
    off the tail). Never populated when no moment was inserted.
    """
    if not settings.carousel_effects_enabled:
        return steps
    moment_cfg = spec.get("carousel_moment")
    if not moment_cfg:
        return steps

    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    variant_id = spec.get("variant_id")
    render_meta: dict[str, Any] = {}
    moment_path = _maybe_render_carousel_moment(
        moment_cfg,
        clip_id_to_local=clip_id_to_local,
        steps=steps,
        variant_dir=variant_dir,
        probe_map=probe_map,
        variant_id=variant_id,
        clip_metas=clip_metas,
        render_meta=render_meta,
    )
    if not moment_path:
        record_pipeline_event(
            "assembly",
            "carousel_moment_skipped",
            {"variant_id": variant_id, "reason": "render_unavailable"},
        )
        return steps

    from app.pipeline.agents.gemini_analyzer import AssemblyStep  # noqa: PLC0415
    from app.pipeline.probe import probe_video  # noqa: PLC0415

    try:
        probe = probe_video(moment_path)
        duration_s = float(probe.duration_s)
    except Exception:  # noqa: BLE001 — a bad probe should skip, not fail the variant
        log.warning("carousel_moment_probe_failed", exc_info=True)
        record_pipeline_event(
            "assembly",
            "carousel_moment_skipped",
            {"variant_id": variant_id, "reason": "probe_failed"},
        )
        return steps
    if duration_s <= 0:
        record_pipeline_event(
            "assembly",
            "carousel_moment_skipped",
            {"variant_id": variant_id, "reason": "non_positive_duration"},
        )
        return steps

    synthetic_id = f"__carousel_{variant_id or 'moment'}"
    clip_id_to_local[synthetic_id] = moment_path
    clip_id_to_gcs[synthetic_id] = moment_path
    probe_map[moment_path] = probe

    moment_step = AssemblyStep(
        slot={"exact_window": True},
        clip_id=synthetic_id,
        moment={"start_s": 0.0, "end_s": round(duration_s, 3)},
    )

    position = moment_cfg.get("position", "intro")
    new_steps = list(steps)
    if position == "middle":
        insertion_index = len(new_steps) // 2
        new_steps.insert(insertion_index, moment_step)
    elif position == "outro":
        insertion_index = len(new_steps)
        new_steps.append(moment_step)
    else:  # "intro" (default) and any unrecognized value
        insertion_index = 0
        new_steps.insert(0, moment_step)

    transition_in = moment_cfg.get("transition_in", moment_cfg.get("transition", "none"))
    transition_out = moment_cfg.get("transition_out", moment_cfg.get("transition", "none"))
    explicit_boundary_model = (
        moment_cfg.get("timing_model") == "ripple_v1"
        or "transition_in" in moment_cfg
        or "transition_out" in moment_cfg
    )
    previous_step = new_steps[insertion_index - 1] if insertion_index > 0 else None
    next_index = insertion_index + 1
    next_step = new_steps[next_index] if next_index < len(new_steps) else None
    old_boundary_overlap_s = (
        _carousel_step_overlap_s(steps[insertion_index - 1], steps[insertion_index])
        if 0 < insertion_index < len(steps)
        else 0.0
    )
    insertion_base_s = _carousel_base_insertion_s(steps, insertion_index)
    incoming_overlap_s = 0.0
    outgoing_overlap_s = 0.0
    legacy_boundary_model = not explicit_boundary_model
    if transition_in == "crossfade" and (previous_step is not None or legacy_boundary_model):
        # Incoming edge: transition INTO the moment step, from whatever
        # precedes it.
        moment_step.slot["transition_in"] = "crossfade"
        if legacy_boundary_model:
            moment_step.slot["transition_duration_s"] = _CAROUSEL_MOMENT_TRANSITION_DURATION_S
            incoming_overlap_s = (
                _CAROUSEL_MOMENT_TRANSITION_DURATION_S if previous_step is not None else 0.0
            )
        else:
            incoming_overlap_s = _carousel_boundary_duration(
                moment_cfg.get("transition_in_duration_s"),
                _carousel_step_duration_s(previous_step),
                duration_s,
            )
            moment_step.slot["transition_duration_s"] = incoming_overlap_s
    elif explicit_boundary_model:
        moment_step.slot["transition_in"] = "none"
        moment_step.slot.pop("transition_duration_s", None)
    # Outgoing edge: transition INTO the step immediately after the moment,
    # from the moment. Absent for "outro" (nothing follows it).
    if transition_out == "crossfade" and next_step is not None:
        next_step.slot["transition_in"] = "crossfade"
        outgoing_overlap_s = (
            _CAROUSEL_MOMENT_TRANSITION_DURATION_S
            if legacy_boundary_model
            else _carousel_boundary_duration(
                moment_cfg.get("transition_out_duration_s"),
                duration_s,
                _carousel_step_duration_s(next_step),
            )
        )
        next_step.slot["transition_duration_s"] = outgoing_overlap_s
    elif next_step is not None and explicit_boundary_model:
        # The Carousel replaced this boundary. Do not leak the old clip-to-clip
        # transition into an explicitly hard-cut Carousel exit.
        next_step.slot["transition_in"] = "none"
        next_step.slot.pop("transition_duration_s", None)

    if inserted_duration_out is not None:
        inserted_duration_out["duration_s"] = duration_s
        if moment_cfg.get("timing_model") == "ripple_v1":
            inserted_duration_out["insertion_base_s"] = insertion_base_s
            inserted_duration_out["ripple_duration_s"] = max(
                0.0,
                duration_s + old_boundary_overlap_s - incoming_overlap_s - outgoing_overlap_s,
            )

    record_pipeline_event(
        "assembly",
        "carousel_moment_inserted",
        {
            "variant_id": variant_id,
            "position": position,
            "effect": render_meta.get("effect"),
            "mode": render_meta.get("mode"),
            "duration_s": round(duration_s, 3),
        },
    )
    return new_steps


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
        for zip_index, (step, plan) in enumerate(zip(steps, resolved_plans)):
            # A spliced carousel-moment step (`_insert_carousel_moment_step`'s
            # synthetic `__carousel_{variant_id}` clip_id) is a locally
            # rendered composite, never a real source clip — it must never
            # occupy a `clip_index` slot in the persisted timeline. Without
            # this skip, `clip_id_to_gcs[synthetic_id] = moment_path` (the
            # segment's LOCAL file path, appended after every real clip) gets
            # its own trailing index, growing the timeline's distinct
            # clip_index/clip count by one on every carousel'd variant and
            # shifting focus-tile indices on the next edit.
            if isinstance(step.clip_id, str) and step.clip_id.startswith("__carousel_"):
                continue
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
                    "duration_beats": beats_per_slot[zip_index],
                    "order": len(slots),
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

    resolved: list[_ResolvedTimelineSlot] = []
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
        look_preset = normalize_look_preset(slot.get("look_preset"))
        look_adjustments_model = normalize_look_adjustments(
            look_preset,
            slot.get("look_adjustments"),
        )
        resolved.append(
            _ResolvedTimelineSlot(
                clip_index,
                in_s,
                duration_s,
                slot.get("moment_energy"),
                slot.get("moment_description"),
                (
                    str(slot.get("transition_after") or "cut")
                    if settings.edit_transitions_enabled
                    else "cut"
                ),
                (
                    float(slot["transition_duration_s"])
                    if settings.edit_transitions_enabled
                    and slot.get("transition_duration_s") is not None
                    else None
                ),
                look_preset,
                (
                    look_adjustments_model.model_dump()
                    if look_adjustments_model is not None
                    else None
                ),
            )
        )
    if not resolved:
        return None

    used_indices = sorted({slot.clip_index for slot in resolved})
    local_paths = _download_clips_parallel([clip_paths_gcs[i] for i in used_indices], tmpdir)
    probe_map = _probe_clips(local_paths)
    # Heavy-source guard (2026-07-21 OOM): timeline re-renders decode the
    # DURABLE ORIGINALS — an un-guarded re-render of a 4K job reproduces the
    # exact incident. Same in-place mutation contract as _ingest_clips.
    from app.pipeline.source_guard import downscale_oversized_sources  # noqa: PLC0415

    downscale_oversized_sources(local_paths, probe_map, tmpdir, job_id=job_id)
    clip_id_to_local = {f"clip_{i}": path for i, path in zip(used_indices, local_paths)}
    clip_id_to_gcs = {f"clip_{i}": gcs for i, gcs in enumerate(clip_paths_gcs)}

    # Clamp every window against the REAL probed duration — the route skips
    # bounds checks on clips the AI never probed (its comment promises "the
    # worker's probe will clamp"; this is that clamp). Slots that collapse
    # below 0.1s after clamping are dropped with a warning.
    clamped: list[_ResolvedTimelineSlot] = []
    for slot in resolved:
        clip_index = slot.clip_index
        in_s = slot.in_s
        duration_s = slot.duration_s
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
        clamped.append(
            _ResolvedTimelineSlot(
                clip_index,
                in_s,
                duration_s,
                slot.moment_energy,
                slot.moment_description,
                slot.transition_after,
                slot.transition_duration_s,
                slot.look_preset,
                slot.look_adjustments,
            )
        )
    if not clamped:
        return None

    boundary_transitions: list[tuple[str, float | None]] = [("hard-cut", None)]
    transition_names = {
        "crossfade": "crossfade",
        "dip_to_black": "dip-to-black",
        "flash": "flash",
    }
    for index in range(1, len(clamped)):
        transition_name = transition_names.get(clamped[index - 1].transition_after, "hard-cut")
        if transition_name == "hard-cut":
            boundary_transitions.append(("hard-cut", None))
            continue
        max_duration_s = min(
            0.3,
            clamped[index - 1].duration_s * 0.3,
            clamped[index].duration_s * 0.3,
        )
        if max_duration_s < 0.1:
            boundary_transitions.append(("hard-cut", None))
            continue
        requested_duration_s = clamped[index - 1].transition_duration_s or 0.3
        boundary_transitions.append(
            (transition_name, round(min(requested_duration_s, max_duration_s), 3))
        )

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
                # The renderer expresses a boundary on the destination slot.
                # User state stores it after the source slot, so read the prior
                # row while materializing AssemblySteps.
                "transition_in": boundary_transitions[i][0],
                "transition_duration_s": boundary_transitions[i][1],
                "look_preset": look_preset,
                "look_adjustments": look_adjustments,
            },
            clip_id=f"clip_{clip_index}",
            moment={
                "start_s": in_s,
                "end_s": in_s + duration_s,
                "energy": moment_energy or 5.0,
                "description": moment_description or "",
            },
        )
        for i, (
            clip_index,
            in_s,
            duration_s,
            moment_energy,
            moment_description,
            _transition_after,
            _transition_duration_s,
            look_preset,
            look_adjustments,
        ) in enumerate(clamped)
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
    carousel_moment_override: Any = CAROUSEL_MOMENT_UNSET,
) -> None:
    from app.services.pipeline_trace import (  # noqa: PLC0415
        record_pipeline_event,
        render_stage_timer,
    )

    render_trace_id = render_gen_id or uuid.uuid4().hex

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
    if _is_sfx_only and (
        _settings_sfx.sound_effects_enabled or _settings_sfx.smart_music_bed_enabled
    ):
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
        _clear_user_timeline(
            job_id,
            variant_id,
            expected_render_gen_id=render_gen_id,
        )
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
            if active_timeline_slots and (new_track_id or existing_track_id):
                # A user timeline can end after the song's last natural beat.
                # Size the exact music window from the cut we are about to render,
                # not a stale duration persisted by the previous render.
                active_timeline_duration_s = round(
                    sum(float(slot.get("duration_s") or 0.0) for slot in active_timeline_slots),
                    3,
                )
                if active_timeline_duration_s > 0:
                    existing_music_window_duration_s = active_timeline_duration_s

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
    # A carousel-moment edit (add/update/remove) ALSO always forces a full
    # re-assembly: `_reburn_text_on_base` only re-burns the text overlay onto
    # the already-flattened base video, it has no way to splice a multi-clip
    # carousel segment into that base. `_is_fast_reburn_eligible` has no
    # carousel awareness (it only checks track/mix/orientation/base/text_mode),
    # so without this guard a carousel-only edit (no text/style field set)
    # silently takes the fast path and the carousel_moment_override is dropped
    # on the floor — the render "succeeds" but the moment never lands.
    if (
        timeline_override is None
        and not force_full_render
        and carousel_moment_override == CAROUSEL_MOMENT_UNSET
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
            persisted_position=_persisted_intro_position(existing),
        )
        _used_fast_path = False
        fast_created_storage: list[str] = []
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
                storage_generation=render_gen_id,
                created_storage_paths=fast_created_storage,
            )
            _used_fast_path = True
        except Exception as _fast_exc:  # noqa: BLE001
            _free_uncommitted_storage_paths(fast_created_storage, job_id=job_id)
            # If the base blob is gone (GCS lifecycle, deleted base, etc.), fall
            # through to the full re-render path rather than surfacing an error.
            # A canvas mismatch means a superseded orientation render left the
            # desired orientation paired with the previous render's base; that
            # state also requires a source rebuild, never a resize of the stale base.
            _exc_type = type(_fast_exc).__name__
            _is_missing = (
                "NotFound" in _exc_type
                or "not found" in str(_fast_exc).lower()
                or "does not exist" in str(_fast_exc).lower()
                or "no such object" in str(_fast_exc).lower()
            )
            _is_unusable = isinstance(_fast_exc, CachedBaseUnusableError)
            if _is_missing or _is_unusable:
                if isinstance(_fast_exc, CachedBaseCanvasMismatchError):
                    event_name = "generative_fast_reburn_base_canvas_mismatch"
                elif isinstance(_fast_exc, CachedBaseProbeError):
                    event_name = "generative_fast_reburn_base_probe_failed"
                else:
                    event_name = "generative_fast_reburn_base_missing"
                log.warning(
                    event_name,
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
                _discard_uncommitted_reburn_storage(
                    existing,
                    result,
                    job_id=job_id,
                )
                _free_uncommitted_storage_paths(
                    [
                        path
                        for path in fast_created_storage
                        if path
                        not in {
                            result.get("video_path"),
                            result.get("visual_blocks_base_path"),
                            result.get("motion_base_path"),
                        }
                    ],
                    job_id=job_id,
                )
                return
            _free_retired_visual_blocks_base(existing, result.get("visual_blocks_base_path"))
            _free_retired_motion_base(existing, result.get("motion_base_path"))
            if (
                _old_video_path_for_delete
                and _old_video_path_for_delete != result.get("video_path")
                and _old_video_path_for_delete != existing.get("base_video_path")
            ):
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

    music_window_audio_only = (
        music_window_alignment == "preserve_cuts"
        and override_text is None
        and not remove_text
        and style_set_id is None
        and size_override_px is None
        and mix_override is None
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
        and media_overlays_override is None
        and sfx_override is None
        and intro_start_s_override is None
        and intro_end_s_override is None
        and text_behind_subject is None
        and orientation_override is None
    )
    if music_window_audio_only and _is_music_window_audio_only_swap_eligible(
        existing=existing,
        track=track,
        music_window_alignment=music_window_alignment,
        timeline_override=timeline_override,
    ):
        try:
            completed = _run_music_window_audio_only_swap(
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
                "music_window_audio_only_swap_fallback_full_render",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc),
                exc_info=True,
            )

    with tempfile.TemporaryDirectory(prefix="nova_generative_re_") as tmpdir:
        # ── Timeline-override assembly (clip timeline editor) ─────────────────
        # Montage variants with an active timeline skip the ENTIRE
        # ingest+Gemini+match leg: the user's slots ARE the plan (download +
        # probe only). Corrupt/unresolvable timelines fall back to fresh match.
        timeline_assembly: dict[str, Any] | None = None
        if active_timeline_slots is not None and variant_id in _TIMELINE_OVERRIDE_VARIANTS:
            with render_stage_timer(
                "asset_loading_and_preprocess",
                trace_id=render_trace_id,
                variant_id=variant_id,
                render_generation_id=render_gen_id,
                counts={"timeline_override": True, "clip_count": len(clip_paths_gcs)},
            ):
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
            if track is not None:
                rendered_timeline_duration_s = round(
                    sum(
                        float(step.slot.get("target_duration_s") or 0.0)
                        for step in assembly_steps_override
                    ),
                    3,
                )
                if rendered_timeline_duration_s > 0:
                    # Unknown source durations are clamped only after probing.
                    # Recipe, lyrics, mix, and persisted state must follow the
                    # exact windows that survived that clamp.
                    existing_music_window_duration_s = rendered_timeline_duration_s
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
                persisted_position=_persisted_intro_position(existing),
            )
        else:
            # PERF/TODO: this re-runs the full clip ingest (re-download + re-Gemini
            # clip_metadata) on every swap/retext, even remove_text. Acceptable for v1
            # (async re-render, reject-if-rendering guard caps spam), but the Gemini
            # analysis is cacheable — persist clip_metas after the first orchestrate run
            # and skip re-analysis here (only re-download + re-probe are truly needed for
            # the render). Follow-up once the feature is real-render-verified.
            with render_stage_timer(
                "asset_loading_and_preprocess",
                trace_id=render_trace_id,
                variant_id=variant_id,
                render_generation_id=render_gen_id,
                counts={"timeline_override": False, "clip_count": len(clip_paths_gcs)},
            ):
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
                persisted_position=_persisted_intro_position(existing),
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
            # Carry an authored carousel moment forward across retext/swap-song/
            # restyle re-renders — same lifecycle as music_start_s /
            # user_style_knobs above. The persisted cfg's own "seed" keeps the
            # director's mode/effect choice deterministic on re-render; if the
            # clip set changed enough that eligibility now fails, that's handled
            # downstream by _insert_carousel_moment_step's existing never-raise +
            # `carousel_moment_skipped` trace event, not here.
            #
            # `carousel_moment_override` (the carousel editor's dispatch path)
            # merges onto that persisted value: UNSET carries it forward
            # unchanged (the line above, byte-identical to before this param
            # existed), `None` removes it, a dict partial-merges. See
            # `_merge_carousel_moment_override`.
            "carousel_moment": _merge_carousel_moment_override(
                existing.get("carousel_moment"), carousel_moment_override
            ),
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
        with render_stage_timer(
            "variant_render",
            trace_id=render_trace_id,
            variant_id=variant_id,
            render_generation_id=render_gen_id,
            counts={"archetype": existing.get("resolved_archetype") or "montage", "rank": rank},
        ):
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
        reburn_intermediate_path: str | None = None
        latest_variant = _fresh_variant_snapshot(job_id, variant_id) or existing
        render_variant = _project_carousel_timed_lanes({**latest_variant, **result})
        has_visual_blocks = settings.visual_blocks_enabled and bool(
            render_variant.get("visual_blocks")
        )
        # Persisted desired state, not the live flag, decides whether the
        # result must pass through the motion base. With the flag off,
        # _ensure_motion_base may reuse a source-bound cache but otherwise
        # fails closed; it must never publish a silently motionless rebuild.
        has_motion_scenes = bool(render_variant.get("motion_scenes"))
        if settings.visual_blocks_enabled and not has_visual_blocks:
            # Removing every block must publish the newly assembled clean base
            # and retire any previous block composite rather than preserving it
            # through the variant merge.
            result["visual_blocks_base_path"] = None
            result["visual_blocks_cache_stale"] = False
        if settings.motion_scenes_enabled and not has_motion_scenes:
            result["motion_base_path"] = None
            result["motion_base_source_path"] = None
            result["motion_cache_stale"] = False
            result["motion_applied_runtime_hash"] = None
            result["motion_cache_identity"] = None
        full_reburn_created_storage: list[str] = []
        has_projected_subtitled_lanes = render_variant.get(
            "resolved_archetype"
        ) == "subtitled" and bool(
            render_variant.get("caption_cues")
            or render_variant.get("camera_effects")
            or render_variant.get("text_elements")
        )
        if (
            (_TEXT_ELEMENTS_ENABLED and render_variant.get("text_elements_user_edited"))
            or has_visual_blocks
            or has_motion_scenes
            or has_projected_subtitled_lanes
        ) and result.get("base_video_path"):
            try:
                result = {
                    **result,
                    **_reburn_text_on_base(
                        job_id=job_id,
                        variant_id=variant_id,
                        existing={
                            **render_variant,
                            **result,
                            "text_elements": render_variant.get("text_elements") or [],
                            "text_elements_user_edited": bool(
                                render_variant.get("text_elements_user_edited")
                            ),
                            # A full assembly minted a new clean base; never reuse a
                            # block composite whose audio/picture came from the old one.
                            "visual_blocks_base_path": None,
                            "motion_base_path": None,
                            "motion_base_source_path": None,
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
                        storage_generation=render_gen_id,
                        created_storage_paths=full_reburn_created_storage,
                    ),
                }
            except Exception:
                _discard_generation_storage(
                    result,
                    job_id=job_id,
                    generation=render_gen_id,
                )
                _free_uncommitted_storage_paths(
                    full_reburn_created_storage,
                    job_id=job_id,
                )
                raise
            reburn_intermediate_path = result.pop("_old_video_path_for_delete", None)
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
        result["base_video_stale"] = False
        if force_full_render:
            # A token-winning full rebuild is the only safe place to consume
            # the sticky camera-removal marker. If user cards remain, their
            # reapply pass clears the overlay-dirty bit after it lands.
            result["overlay_camera_rebuild_pending"] = False
            if not persisted_media_overlays:
                result["media_overlays_render_dirty"] = False
        if not _update_variant_entry(
            job_id,
            variant_id,
            result,
            expected_render_gen_id=render_gen_id,
            outcome="full_render",
        ):
            _discard_generation_storage(result, job_id=job_id, generation=render_gen_id)
            _free_uncommitted_storage_paths(
                full_reburn_created_storage,
                job_id=job_id,
            )
            if reburn_intermediate_path and reburn_intermediate_path != result.get(
                "base_video_path"
            ):
                from app.storage import delete_object_best_effort  # noqa: PLC0415

                delete_object_best_effort(reburn_intermediate_path)
            return
        # Write accepted — the retired snapshot blobs are unreachable; free them
        # (D16-C) before the reapply pass mints fresh ones.
        _free_media_snapshot_keys(retired_snapshot_keys)
        # Text-behind-subject: a full re-render's matte upload key is deterministic
        # (base_video_path + _MATTE_CACHE_SUFFIX), so a fresh recompute overwrites
        # the SAME blob — no orphan there. The orphan cases the old!=new delete
        # below covers: the previous render had a matte and this one didn't
        # recompute one (behind_subject/flag now off, or no overlay needs it),
        # and the v1→v2 suffix migration (old ".matte.mp4" key retired when the
        # recompute lands under ".matte.v2.mp4").
        old_matte_path = existing.get("subject_matte_path")
        new_matte_path = result.get("subject_matte_path")
        if (
            old_matte_path
            and old_matte_path != new_matte_path
            and _matte_delete_allowed(old_matte_path)
        ):
            from app.storage import delete_object_best_effort  # noqa: PLC0415

            delete_object_best_effort(old_matte_path)
            delete_object_best_effort(f"{old_matte_path}.json")
        _free_retired_visual_blocks_base(existing, result.get("visual_blocks_base_path"))
        _free_retired_motion_base(existing, result.get("motion_base_path"))
        _free_retired_generation_outputs(existing, result, job_id=job_id)
        if (
            reburn_intermediate_path
            and reburn_intermediate_path != result.get("video_path")
            and reburn_intermediate_path != result.get("base_video_path")
        ):
            from app.storage import delete_object_best_effort  # noqa: PLC0415

            delete_object_best_effort(reburn_intermediate_path)
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
        failure_accepted = _update_variant_entry(
            job_id,
            variant_id,
            failure_patch,
            expected_render_gen_id=render_gen_id,
            outcome="full_render_failed",
        )
        if not failure_accepted:
            _discard_generation_storage(result, job_id=job_id, generation=render_gen_id)
            return
        # A failed render deliberately preserves the last good video/output URL,
        # but may persist a newly-created clean base for a later reburn. Retire
        # only fields that actually landed, and remove the unpublished video.
        _free_retired_generation_outputs(existing, failure_patch, job_id=job_id)
        _discard_generation_storage(
            result,
            job_id=job_id,
            generation=render_gen_id,
            fields=("video_path",),
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


def _clear_user_timeline(
    job_id: str,
    variant_id: str,
    *,
    expected_render_gen_id: str | None = None,
) -> bool:
    """Remove the persisted `user_timeline` key from one variant entry (row-locked).

    `_update_variant_entry` can only merge keys, never drop them — swap-song
    needs a true removal so the variant reads as "no user edits" afterwards
    (mirrors `persist_user_timeline(..., None)` on the route side).

    The generation comparison happens under the same row lock as the removal:
    a superseded swap worker must never erase a newer editor commit's timeline.
    """
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return False
        plan = dict(job.assembly_plan or {})
        variants = list(plan.get("variants") or [])
        for i, v in enumerate(variants):
            if v.get("variant_id") != variant_id:
                continue
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
                    outcome="clear_user_timeline",
                    expected_gen_id=expected_render_gen_id,
                    actual_gen_id=current,
                )
                return False
            if "user_timeline" in v:
                updated = dict(v)
                updated.pop("user_timeline", None)
                variants[i] = updated
            else:
                return True
            break
        else:
            return False
        plan["variants"] = variants
        job.assembly_plan = plan
        db.commit()
    return True


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
    if requested_duration_s is None:
        start_s = float(cfg.get("best_start_s", 0.0) or 0.0)
        end_s = float(cfg.get("best_end_s", start_s) or start_s)
        return {
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": max(0.0, end_s - start_s),
            "track_config": cfg,
            "validated": False,
        }
    if requested_start_s is None:
        # Legacy variants may predate persisted music_start_s. An edited
        # timeline still supplies an exact duration, so seed its start from the
        # ranked section and run the same legal beat-snap/clamp path.
        requested_start_s = float(cfg.get("best_start_s", 0.0) or 0.0)

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


def _discard_generation_storage(
    result: dict,
    *,
    job_id: str,
    generation: object,
    fields: tuple[str, ...] = (
        "video_path",
        "base_video_path",
        "subject_matte_path",
        "visual_blocks_base_path",
        "motion_base_path",
    ),
) -> None:
    """Best-effort cleanup for outputs rejected by the generation guard."""
    token = "".join(character for character in str(generation or "") if character.isalnum())[:32]
    if not token:
        return
    prefix = f"generative-jobs/{job_id}/"
    keys = {
        value
        for field in fields
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


def _free_uncommitted_storage_paths(paths: list[str], *, job_id: str) -> None:
    """Best-effort cleanup for task-owned keys that never won a DB write."""
    prefix = f"generative-jobs/{job_id}/"
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    for path in set(paths):
        if path.startswith(prefix):
            delete_object_best_effort(path)


def _free_retired_generation_outputs(previous: dict, result: dict, *, job_id: str) -> None:
    """Retire the last generation's video/base only after its replacement lands."""
    prefix = f"generative-jobs/{job_id}/"
    keep = {
        result.get("video_path"),
        result.get("base_video_path"),
        result.get("visual_blocks_base_path"),
        result.get("motion_base_path"),
        previous.get("pre_media_overlay_video_path"),
        previous.get("pre_sfx_video_path"),
    }
    keep.discard(None)
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    for field in ("video_path", "base_video_path"):
        if not result.get(field):
            continue
        path = previous.get(field)
        if isinstance(path, str) and path.startswith(prefix) and path not in keep:
            delete_object_best_effort(path)


def _discard_uncommitted_reburn_storage(
    previous: dict,
    result: dict,
    *,
    job_id: str,
) -> None:
    """Delete immutable reburn outputs rejected by the DB generation guard."""
    prefix = f"generative-jobs/{job_id}/"
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    for field, previous_field in (
        ("video_path", "video_path"),
        ("visual_blocks_base_path", "visual_blocks_base_path"),
        ("motion_base_path", "motion_base_path"),
    ):
        path = result.get(field)
        if (
            isinstance(path, str)
            and path.startswith(prefix)
            and path != previous.get(previous_field)
        ):
            delete_object_best_effort(path)


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
        # Seeded straight from `spec` (already fully decided by the authoring
        # policy / carried forward by regen — see _run_regenerate_variant)
        # into the persisted row on BOTH the success and failure return paths
        # (both are `{**base, ...}`), so a renderer failure cannot silently
        # drop an authored moment the same way a renderer failure cannot drop
        # music_start_s above.
        "carousel_moment": spec.get("carousel_moment"),
        # Render receipt for projecting stable pre-insertion creator lanes.
        "carousel_insertion_base_s": None,
        "carousel_inserted_duration_s": None,
        "carousel_ripple_duration_s": None,
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
        # RESOLVED placement snapshot (see `_intro_placement_from_params`). None for
        # the plain centered intro — the editor's read adapter treats absent/None as
        # "legacy variant" and keeps its pre-snapshot behavior.
        "intro_placement": None,
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
        # Which style profile the static intro was actually BURNED with —
        # "editorial" | "legacy". Overwritten by `_apply_static_layout` once
        # the intro is built; stays None for renders that build no static
        # intro (sequence, lyrics, footage-only). The read adapter needs this
        # because the decision also depends on `allow_sequence`, a render-time
        # kwarg no other persisted field records. None/absent == legacy.
        "intro_cluster_style": None,
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
    variant_t0 = time.monotonic()
    try:
        plan_t0 = time.monotonic()
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
            # RESOLVED placement — the editor projects the intro element from this
            # (absent → it would re-guess "center" and draw a "bottom" hook mid-frame,
            # or read the masonry candidate fracs the resolver actually declined).
            base["intro_placement"] = _intro_placement_from_params(
                _at_params, has_candidates=bool(text_placement_candidates)
            )

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
            # Fresh-match montage only (masonry keeps tiles; the override path
            # above must honor the user's slots verbatim): collapse invisible
            # same-source seams so render and editor timeline agree.
            if not masonry_requested:
                steps = _merge_contiguous_same_source_steps(
                    steps, clip_id_to_local=clip_id_to_local, probe_map=probe_map
                )
                if len(steps) < len(assembly_plan.steps):
                    from app.services.pipeline_trace import (  # noqa: PLC0415
                        record_pipeline_event,
                    )

                    record_pipeline_event(
                        "assembly",
                        "contiguous_slots_merged",
                        {
                            "variant_id": variant_id,
                            "matched_slots": len(assembly_plan.steps),
                            "merged_slots": len(steps),
                        },
                    )
        _record_render_subphase(
            job_id,
            "render_variants",
            "variant_plan",
            plan_t0,
            detail={"variant_id": variant_id, "track_id": track_id},
        )

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

        # Blossom-carousel moment hook (Lane G, kill-switched): additive splice
        # into the finalized montage `steps`, before assembly. No-op — zero
        # carousel imports, `steps` unchanged — unless the flag is on AND the
        # spec requests a moment. See `_insert_carousel_moment_step`.
        _carousel_insert_sink: dict[str, float] = {}
        moment_cfg = spec.get("carousel_moment") or {}
        steps = _insert_carousel_moment_step(
            steps,
            spec,
            clip_id_to_local=clip_id_to_local,
            clip_id_to_gcs=clip_id_to_gcs,
            probe_map=probe_map,
            variant_dir=variant_dir,
            clip_metas=clip_metas,
            inserted_duration_out=_carousel_insert_sink,
        )
        if "duration_s" in _carousel_insert_sink:
            base["carousel_inserted_duration_s"] = _carousel_insert_sink["duration_s"]
        if moment_cfg.get("timing_model") == "ripple_v1":
            if "insertion_base_s" in _carousel_insert_sink:
                base["carousel_insertion_base_s"] = round(
                    _carousel_insert_sink["insertion_base_s"], 3
                )
            if "ripple_duration_s" in _carousel_insert_sink:
                base["carousel_ripple_duration_s"] = round(
                    _carousel_insert_sink["ripple_duration_s"], 3
                )
        if voiceover_gcs_path and "duration_s" in _carousel_insert_sink:
            # `voiceover_target_s` was sized to the voice BEFORE this splice
            # (min(footage, voice, cap), above) — extend it by the spliced
            # moment's real rendered length so `_mix_user_voiceover`'s final
            # `-t` truncation doesn't chop the moment off the tail. Only the
            # mix-stage target changes; the recipe/slot layout built earlier
            # from the pre-splice value is untouched.
            voiceover_target_s += _carousel_insert_sink["duration_s"]

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
        assembly_t0 = time.monotonic()

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
        _record_render_subphase(
            job_id,
            "render_variants",
            "variant_assembly",
            assembly_t0,
            detail={
                "variant_id": variant_id,
                "masonry": masonry_applied,
                "classic": classic_assembly_done,
            },
        )

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
        audio_t0 = time.monotonic()
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
        _record_render_subphase(
            job_id,
            "render_variants",
            "variant_audio_mix",
            audio_t0,
            detail={
                "variant_id": variant_id,
                "mode": (
                    "voiceover"
                    if voiceover_gcs_path
                    else ("music" if track is not None else "original")
                ),
            },
        )

        if not os.path.exists(audio_mixed_path) or os.path.getsize(audio_mixed_path) == 0:
            raise RuntimeError(f"variant {variant_id} produced empty audio-mixed output")

        # For agent_text variants: upload the text-free base for fast-reburn, then
        # burn text on top to produce the final output. Lyrics variants cache the
        # lyric-burned, user-text-free base so user TextElements can layer above it.
        if text_mode == "agent_text" and agent_text is not None:
            from app.pipeline.generative_overlays import (  # noqa: PLC0415
                build_persistent_intro_overlays,
            )
            from app.pipeline.intro_cluster import (  # noqa: PLC0415
                cluster_style_marker,
                resolve_cluster_style,
            )
            from app.pipeline.probe import probe_video  # noqa: PLC0415
            from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            # Upload the text-free base first.
            base_gcs = _variant_storage_key(
                job_id,
                f"base_{rank}_{variant_id}.mp4",
                spec.get("storage_generation"),
            )
            base_upload_t0 = time.monotonic()
            base_url_unused = upload_public_read(audio_mixed_path, base_gcs)  # noqa: F841
            _record_render_subphase(
                job_id,
                "render_variants",
                "variant_base_upload",
                base_upload_t0,
                detail={"variant_id": variant_id},
            )
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
                    # Slot joins in a classic cut-only assembly are exact cut
                    # times; masonry/collage boards have no timeline cuts.
                    cut_boundaries_s=(
                        _cut_boundaries_from_durations(
                            [float(p.get("duration_s") or 0.0) for p in resolved_plans]
                        )
                        if classic_assembly_done and not masonry_applied
                        else None
                    ),
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

            # Static intro (cluster or linear) style. Sequence-eligible fallback
            # keeps PR #508's editorial restyle; explicit opt-outs (layout/text
            # edits) use the legacy static cluster path so Slice 3a's registry
            # pairing owns the faces. Resolved ONCE here — `_apply_static_layout`
            # stamps the matching marker so the read adapter can rebuild it.
            _sio_cs = resolve_cluster_style(
                editorial=editorial_enabled and allow_sequence,
                hero_font=cluster_hero_font_override,
                body_font=cluster_body_font_override,
                accent_font=cluster_accent_font_override,
                hero_size_px=cluster_hero_size_px_override,
                body_size_px=cluster_body_size_px_override,
                accent_size_px=cluster_accent_size_px_override,
            )

            def _static_intro_overlays() -> list[dict]:
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
                # Snapshot of the style these overlays were built with, so the
                # read adapter projects the same blocks/faces/sizes/positions.
                # Stamped for linear too — harmless (the linear path ignores
                # cluster_style) and it keeps a later cluster edit honest.
                base["intro_cluster_style"] = cluster_style_marker(_sio_cs)
                base["transcript"] = None
                base["scenes"] = None
                base["sequence_base_size_px"] = None
                base["sequence_mode"] = None
                # base["sequence_quote"] is deliberately NOT cleared: a known
                # quote (persisted carry or just authored) survives a static
                # fallback so a later eligible render re-times it LLM-free.

            text_burn_t0 = time.monotonic()
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
            _record_render_subphase(
                job_id,
                "render_variants",
                "variant_text_burn",
                text_burn_t0,
                detail={"variant_id": variant_id, "mode": base.get("intro_mode")},
            )
        else:
            if text_mode == "lyrics" or lyrics_rendered:
                base_gcs = _variant_storage_key(
                    job_id,
                    f"base_{rank}_{variant_id}.mp4",
                    spec.get("storage_generation"),
                )
                base_upload_t0 = time.monotonic()
                base_url_unused = upload_public_read(audio_mixed_path, base_gcs)  # noqa: F841
                _record_render_subphase(
                    job_id,
                    "render_variants",
                    "variant_base_upload",
                    base_upload_t0,
                    detail={"variant_id": variant_id},
                )
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
        output_upload_t0 = time.monotonic()
        output_url = upload_public_read(final_path, output_gcs)
        _record_render_subphase(
            job_id,
            "render_variants",
            "variant_output_upload",
            output_upload_t0,
            detail={"variant_id": variant_id},
        )
        log.info("generative_variant_uploaded", job_id=job_id, variant_id=variant_id)
        _record_render_subphase(
            job_id,
            "render_variants",
            "variant_total",
            variant_t0,
            detail={"variant_id": variant_id, "ok": True},
        )
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
        _record_render_subphase(
            job_id,
            "render_variants",
            "variant_total",
            variant_t0,
            detail={"variant_id": variant_id, "ok": False, "error": err},
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
        # RESOLVED placement snapshot — see `_intro_placement_from_params`.
        "intro_placement": None,
        # Authoritative intro mode (D19). talking_head never renders the
        # transcript-synced sequence in v1, so this is always the static layout.
        "intro_mode": None,
        "resolved_archetype": "talking_head",
        # Per-user parity-safe knob overrides (Creator Agent M1). Persisted for
        # re-renders (same as _render_generative_variant).
        "user_style_knobs": user_style_knobs or None,
        # Text-free base: uploaded best-effort right after assembly, before the
        # intro burn (filled in below). Without it the API emits no
        # `base_video_url`, and the editor falls back to playing the TEXT-BURNED
        # output while still drawing its own DOM text layer — the intro then
        # renders twice, once in the pixels and once in the DOM.
        "base_video_path": None,
        # Text-behind-subject: resolved (overwritten below) but never rendered on
        # THIS pass — the first render always burns the intro straight onto the
        # composite, so no matte is computed here and a behind_subject overlay
        # burns as plain text (matte=None is a safe, logged fallback per the Skia
        # renderer's contract). A later text edit reaches `_reburn_text_on_base`
        # via the cached base above, which resolves its own matte.
        "intro_behind_subject": False,
        "subject_matte_path": None,
        # Media-overlay cards (slice 1) — see montage finalize dict for docs.
        "media_overlays": None,
        "pre_media_overlay_video_path": None,
        # Silence-cut summary {removed, time_saved_s, version} (plans/010 T6) — set
        # only when the stage ran to a plan; drives the admin cut-plan viewer.
        "silence_cut": None,
        "speech_cut_candidates": None,
        "speech_cut_forced_removals": None,
        "speech_cuts_disabled": False,
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
        if settings.silence_cut_enabled or settings.retake_cut_enabled:
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            if silence_cut_disabled:
                # Per-item opt-out (10A) — skips the WHOLE stage, retakes included.
                record_pipeline_event(
                    "silence_cut", "silence_cut_skipped_disabled", {"variant_id": variant_id}
                )
            else:

                def silence_cut_fn(
                    analysis_path: str,
                    duration_s: float,
                    *,
                    cache_key: str | None = None,
                    source_fingerprint: str | None = None,
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
                        source_fingerprint=source_fingerprint,
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
        base["speech_cut_candidates"] = silence_cut_out.get("review_candidates") or None
        base["spine_clip_id"] = silence_cut_out.get("spine_clip_id")

        # Cache the text-free composite BEFORE the intro burn. The editor plays
        # this base and draws its text elements as a DOM layer on top; with no
        # base it falls back to the burned output and the intro double-displays
        # (once in the pixels, once in the DOM). Best-effort, matching the
        # narrated path below: a failed base upload only costs fast-reburn +
        # WYSIWYG editing, so it must never fail an otherwise-good render.
        if os.path.exists(base_path) and os.path.getsize(base_path) > 0:
            base_gcs = f"generative-jobs/{job_id}/base_{rank}_{variant_id}.mp4"
            try:
                upload_public_read(base_path, base_gcs)
                base["base_video_path"] = base_gcs
                log.info(
                    "generative_base_uploaded",
                    job_id=job_id,
                    variant_id=variant_id,
                    base_gcs=base_gcs,
                )
            except Exception as exc:  # noqa: BLE001 — editing is optional, the render is not
                log.warning(
                    "generative_base_upload_failed",
                    job_id=job_id,
                    variant_id=variant_id,
                    base_gcs=base_gcs,
                    error=str(exc)[:MAX_ERROR_DETAIL_LEN],
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
            # RESOLVED placement — the talking_head intro is the likeliest to sit
            # off-center (curated sets / the format matcher both return "bottom").
            base["intro_placement"] = _intro_placement_from_params(params)

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


def _silence_cut_retake_spans(  # noqa: ANN001
    transcript, *, job_id: str, source_fingerprint: str
) -> tuple[list[tuple[int, int]], list[dict[str, Any]]]:
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
        return [], []
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
        from app.pipeline.speech_cut_state import make_candidate  # noqa: PLC0415
        from app.services.transcript_source import compute_transcript_hash  # noqa: PLC0415

        transcript_words = [
            {
                "word": str(word.text),
                "start_s": float(word.start_s),
                "end_s": float(word.end_s),
            }
            for word in transcript.words
        ]
        transcript_hash = compute_transcript_hash(transcript_words, None)

        candidates = []
        for span in getattr(out, "review_candidates", []) or []:
            words = transcript.words[span.start_word : span.end_word + 1]
            if not words:
                continue
            candidates.append(
                make_candidate(
                    start_s=float(words[0].start_s),
                    end_s=float(words[-1].end_s),
                    reason=span.reason,
                    source="retake_review",
                    preview=" ".join(str(word.text) for word in words),
                    source_fingerprint=hashlib.sha256(
                        source_fingerprint.encode("utf-8")
                    ).hexdigest()[:24],
                    transcript_hash=transcript_hash,
                )
            )
        return [(span.start_word, span.end_word) for span in out.retakes], candidates
    except Exception as exc:  # noqa: BLE001 — retakes can never block the base feature
        log.warning("retake_detector_failed", job_id=job_id, error=str(exc)[:200])
        record_pipeline_event("silence_cut", "retake_detector_failed", {"error": str(exc)[:200]})
        return [], []


def _silence_cut_analysis(
    clip_path: str,
    duration_s: float,
    *,
    job_id: str,
    cache: _SilenceCutCache | None,
    cache_key: str | None = None,
    source_fingerprint: str | None = None,
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

    forced_removals: list[dict[str, Any]] = []
    try:
        with _sync_session() as db:
            cut_job = db.get(Job, uuid.UUID(job_id))
            forced_removals = list(
                ((cut_job.assembly_plan or {}).get("speech_cut_control") or {}).get(
                    "forced_removals"
                )
                or []
            )
    except Exception as exc:  # noqa: BLE001 — optional review state fails open
        log.warning("speech_cut_control_read_failed", job_id=job_id, error=str(exc)[:160])

    def _compute() -> dict[str, Any]:
        entry: dict[str, Any] = {
            "failed": False,
            "words": [],
            "language": "",
            "plan": None,
            "retake_span_count": 0,
            "review_candidates": [],
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
            silences = (
                detect_silences(clip_path, min_silence_s=0.1)
                if settings.silence_cut_enabled
                else []
            )
            if not silences:
                # Calibration gate visibility: zero silencedetect ranges means
                # rule 2 self-disables inside build_cut_plan (noisy footage —
                # aggressiveness must never scale WITH background noise).
                record_pipeline_event(
                    "silence_cut",
                    "silence_cut_rule2_disabled",
                    {"clip": os.path.basename(clip_path)},
                )
            retake_spans, review_candidates = _silence_cut_retake_spans(
                transcript,
                job_id=job_id,
                source_fingerprint=source_fingerprint or cache_key or clip_path,
            )
            plan = build_cut_plan(
                transcript.words,
                silences,
                duration_s,
                retake_spans=retake_spans,
                forced_removals=forced_removals,
                include_silence_and_fillers=settings.silence_cut_enabled,
            )
            review_candidates = [
                candidate
                for candidate in review_candidates
                if not any(
                    min(float(candidate["end_s"]), removal.end_s)
                    > max(float(candidate["start_s"]), removal.start_s)
                    for removal in plan.removed
                )
            ]
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
                review_candidates=review_candidates,
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
                "review_candidates": [],
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


def _load_smart_caption_assets(job_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.models import Job as _Job  # noqa: PLC0415
    from app.models import PlanItemAsset  # noqa: PLC0415

    with _sync_session() as db:
        smart_job = db.get(_Job, uuid.UUID(job_id))
        if smart_job is None or smart_job.content_plan_item_id is None:
            return []
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
        return [
            {
                "id": str(row.id),
                "gcs_path": row.gcs_path,
                "kind": row.kind,
                "source_filename": row.source_filename,
                "duration_s": row.duration_s,
                "aspect": row.aspect,
                "user_context": getattr(row, "user_context", None),
                "analysis": row.analysis or {},
            }
            for row in rows
        ]


def _load_smart_caption_assets_fail_open(
    job_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the creator pool without allowing a context outage to fail video."""

    try:
        assets = _load_smart_caption_assets(job_id)
        return assets, {"status": "loaded", "asset_count": len(assets)}
    except Exception as exc:  # noqa: BLE001 — Smart context is optional polish
        log.warning(
            "smart_caption_asset_context_failed_open",
            job_id=job_id,
            error_class=type(exc).__name__,
        )
        try:
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            record_pipeline_event(
                "smart_captions",
                "asset_context_failed_open",
                {"error_class": type(exc).__name__},
            )
        except Exception:  # noqa: BLE001 — observability cannot fail the render
            pass
        return [], {"status": "failed_open", "error_class": type(exc).__name__}


def _smart_caption_trusted_aliases(
    smart_captions: dict[str, str] | None,
    smart_assets: list[dict[str, Any]],
) -> list[str]:
    if not smart_captions:
        return []
    from app.pipeline.caption_correct import build_trusted_caption_hints  # noqa: PLC0415
    from app.smart_edit.presets import load_preset  # noqa: PLC0415

    try:
        visual_aliases: list[Any] = list(
            load_preset(
                smart_captions["preset_id"],
                smart_captions["preset_version"],
            ).visual_aliases
        )
    except Exception:
        visual_aliases = []
    return build_trusted_caption_hints(
        visual_aliases=visual_aliases,
        asset_names=[str(asset.get("source_filename") or "") for asset in smart_assets],
    )


def _compile_smart_caption_render_plan(
    *,
    cues: list[dict[str, Any]],
    smart_captions: dict[str, str],
    detected_lang: str,
    job_id: str,
    smart_assets: list[dict[str, Any]],
) -> tuple[Any | None, dict[str, Any]]:
    """Compile a closed-token Smart plan from the caller's one pool snapshot."""

    from app.smart_edit.compiler import compile_smart_plan  # noqa: PLC0415
    from app.smart_edit.planner import plan_smart_captions  # noqa: PLC0415

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
    from app.pipeline.camera_effects import camera_effects_from_intents  # noqa: PLC0415

    camera_effects = camera_effects_from_intents(getattr(compiled, "camera_intents", []))
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
        "camera_effects": camera_effects or None,
        "text_elements": compiled.text_elements,
        "text_elements_user_edited": True,
        "text_elements_materialized_from": "smart_captions",
    }


def _smart_shadow_comparison(
    *,
    primary_state: dict[str, Any],
    shadow_state: dict[str, Any],
    shadow_compiled: Any,
) -> dict[str, Any]:
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    def fingerprint(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    document = shadow_state.get("smart_edit_document") or {}
    patch = shadow_state.get("smart_compiled_patch") or {}
    return {
        "status": "compiled",
        "materialized": False,
        "primary_document_fingerprint": fingerprint(primary_state.get("smart_edit_document") or {}),
        "shadow_document_fingerprint": fingerprint(document),
        "shadow_patch_fingerprint": fingerprint(patch),
        "shadow_preset_id": document.get("preset_id"),
        "shadow_preset_version": document.get("preset_version"),
        "events": len(document.get("events") or []),
        "captions": len(shadow_compiled.caption_cues),
        "titles": len(shadow_compiled.text_elements),
        "visuals": len(shadow_compiled.media_overlays),
        "sfx_intents": len(shadow_compiled.sfx_intents),
        "camera_intents": len(getattr(shadow_compiled, "camera_intents", [])),
        "audio_treatment_intents": len(getattr(shadow_compiled, "audio_treatment_intents", [])),
    }


def _smart_music_track_eligible(track: Any) -> bool:
    """Closed production eligibility predicate for the v2 background bed.

    Eligibility is purely a function of analysis freshness + curation state
    (archiving in /admin/music is the only lever left). There is no license
    or publish-state requirement: any `ready`, labeled, sectioned, non-archived
    `music/%` track is eligible for the bed, published or not.
    """

    from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION  # noqa: PLC0415
    from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION  # noqa: PLC0415
    from app.services.music_sections import current_best_section_for_track  # noqa: PLC0415

    return bool(
        getattr(track, "analysis_status", None) == "ready"
        and getattr(track, "archived_at", None) is None
        and str(getattr(track, "audio_gcs_path", "") or "").startswith("music/")
        and getattr(track, "ai_labels", None)
        and getattr(track, "label_version", None) == CURRENT_LABEL_VERSION
        and getattr(track, "best_sections", None)
        and getattr(track, "section_version", None) == CURRENT_SECTION_VERSION
        and current_best_section_for_track(track) is not None
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
    if not settings.smart_music_bed_enabled:
        receipt["reason"] = "disabled_by_flag"
        return None, receipt
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
            # The lock holder is resolving right now — reuse its result if the
            # write already landed instead of silently rendering without music.
            try:
                cached = client.get(cache_key)
                if cached:
                    treatment = json.loads(cached)
                    receipt.update({"status": "reused", "track_id": treatment.get("track_id")})
                    return treatment, receipt
            except Exception:  # noqa: BLE001 — fail open to no-bed
                pass
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

        from sqlalchemy import func as _func  # noqa: PLC0415
        from sqlalchemy import select as _select  # noqa: PLC0415
        from sqlalchemy.orm import load_only  # noqa: PLC0415

        from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION  # noqa: PLC0415
        from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION  # noqa: PLC0415

        with _sync_session() as db:
            library_base = (
                MusicTrack.archived_at.is_(None),
                MusicTrack.audio_gcs_path.like("music/%"),
            )
            ready_clause = MusicTrack.analysis_status == "ready"
            labeled_clause = (
                MusicTrack.ai_labels.is_not(None),
                MusicTrack.label_version == CURRENT_LABEL_VERSION,
            )
            sectioned_clause = (
                MusicTrack.best_sections.is_not(None),
                MusicTrack.section_version == CURRENT_SECTION_VERSION,
            )
            eligibility_counts = db.execute(
                _select(
                    _func.count().label("total"),
                    _func.count().filter(ready_clause).label("ready"),
                    _func.count().filter(ready_clause, *labeled_clause).label("labeled_current"),
                    _func.count()
                    .filter(ready_clause, *labeled_clause, *sectioned_clause)
                    .label("sectioned_current"),
                )
                .select_from(MusicTrack)
                .where(*library_base)
            ).one()
            receipt["eligible_reasons"] = {
                "total": eligibility_counts.total,
                "ready": eligibility_counts.ready,
                "labeled_current": eligibility_counts.labeled_current,
                "sectioned_current": eligibility_counts.sectioned_current,
                "eligible": 0,
            }
            tracks = list(
                db.execute(
                    _select(MusicTrack)
                    .options(
                        load_only(
                            MusicTrack.id,
                            MusicTrack.title,
                            MusicTrack.audio_gcs_path,
                            MusicTrack.duration_s,
                            MusicTrack.analysis_status,
                            MusicTrack.published_at,
                            MusicTrack.archived_at,
                            MusicTrack.track_config,
                            MusicTrack.ai_labels,
                            MusicTrack.label_version,
                            MusicTrack.best_sections,
                            MusicTrack.section_version,
                            MusicTrack.recipe_cached,
                            MusicTrack.beat_timestamps_s,
                            MusicTrack.lyrics_cached,
                        )
                    )
                    .where(
                        MusicTrack.analysis_status == "ready",
                        MusicTrack.archived_at.is_(None),
                        MusicTrack.audio_gcs_path.like("music/%"),
                        MusicTrack.ai_labels.is_not(None),
                        MusicTrack.label_version == CURRENT_LABEL_VERSION,
                        MusicTrack.best_sections.is_not(None),
                        MusicTrack.section_version == CURRENT_SECTION_VERSION,
                    )
                    .order_by(MusicTrack.created_at.desc(), MusicTrack.id)
                    .limit(_SMART_MUSIC_CANDIDATE_LIMIT)
                )
                .scalars()
                .all()
            )
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
        receipt["eligible_reasons"]["eligible"] = len(eligible)
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
            "speech_duck_db": float(
                intent["speech_duck_db"] if intent.get("speech_duck_db") is not None else -12.0
            ),
            "final_lufs": float(
                intent["final_lufs"] if intent.get("final_lufs") is not None else -14.0
            ),
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


# Feature C candidate ladder (plan 011 §Feature C). #0 is ALWAYS the preset y so a
# well-framed video changes nothing; the rest are the discrete zones the caption
# may move to. ONE static y per video — no per-scene motion (design discrete-zone
# rule). #0 is prepended at call time from the resolved preset.
_FACE_PLACEMENT_EXTRA_CANDIDATES: tuple[float, ...] = (0.62, 0.78, 0.55, 0.86)

# Anchor budget. Both the sampler's frame-seek count and its timeout budget scale
# with the anchor count, and the plan-derived anchors are bounded only by
# MAX_SMART_EDIT_EVENTS (120) — without a cap a saturated plan would hand one
# subprocess a ~45s budget. The two lanes are capped SEPARATELY so neither can
# starve the other:
#   * intent anchors (camera + media starts) cap at 12 — exactly the sampler's
#     historical max_samples default, so card face-protection keeps parity with
#     the pre-feature path even on a saturated plan;
#   * the 8 evenly-spaced anchors ALWAYS survive, so the one permanent placement
#     decision is never made from a front-loaded slice of the video.
# Dedupe can only shrink the union, so 12 + 8 is a hard ceiling (timeout <= 8s).
_FACE_PLACEMENT_MAX_INTENT_ANCHORS = 12
_FACE_PLACEMENT_SPACED_ANCHORS = 8
# The sampler is a COLD-START subprocess: the base term has to cover interpreter
# boot plus `import cv2` before a single frame is decoded. Prod stderr stamps put
# that at ~1.7s in the prod image, so the original 1.0s was already spent by the
# time real work began — on a 4-shared-vCPU worker also running FFmpeg, healthy
# runs (130-185ms/anchor) and killed ones sat on either side of the same line.
_FACE_PLACEMENT_TIMEOUT_BASE_S = 2.5
_FACE_PLACEMENT_TIMEOUT_PER_ANCHOR_S = 0.35
_FACE_PLACEMENT_MAX_ANCHORS = _FACE_PLACEMENT_MAX_INTENT_ANCHORS + _FACE_PLACEMENT_SPACED_ANCHORS


def _evenly_spaced_anchors(duration_s: float, n: int = 8) -> list[float]:
    """``n`` sample times centered in each 1/n bucket (avoids frame 0 and EOF)."""

    if duration_s <= 0 or n <= 0:
        return []
    return [round(duration_s * (index + 0.5) / n, 3) for index in range(n)]


def _dedupe_anchor_times(times: list[float], *, min_gap_s: float = 0.25) -> list[float]:
    """Sorted, non-negative anchors with any within ``min_gap_s`` of a kept one dropped."""

    kept: list[float] = []
    for value in sorted(max(0.0, float(item)) for item in times):
        if not kept or value - kept[-1] >= min_gap_s:
            kept.append(value)
    return kept


def _smart_caption_protected_regions(
    base: dict[str, Any], cues: list[dict[str, Any]]
) -> tuple[list, list]:
    """Caption + authored-title regions the media compositor must not cover.

    Single owner of this construction: the face-placement pass (which needs the
    title boxes to choose a y) and BOTH protected-box assembly branches read it,
    so the three copies can no longer drift and `measure_text_overlay_box` — a
    real Skia layout pass — runs once per render instead of twice.
    """

    from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import measure_text_overlay_box  # noqa: PLC0415

    caption_regions = [
        ProtectedRegion(
            start_s=float(cue.get("start_s") or 0.0),
            end_s=float(cue.get("end_s") or 0.0),
            box=NormalizedBox(**cue["smart_render_box"]),
            kind="caption",
        )
        for cue in cues
        if isinstance(cue.get("smart_render_box"), dict)
    ]
    title_regions = [
        ProtectedRegion(
            start_s=float(overlay.get("start_s") or 0.0),
            end_s=float(overlay.get("end_s") or 0.0),
            box=NormalizedBox(**measure_text_overlay_box(overlay)),
            kind="title",
        )
        for overlay in _text_element_burn_dicts(base)
    ]
    return caption_regions, title_regions


def _distinct_caption_probe_boxes(cues: list[dict[str, Any]]) -> list:
    """Every DISTINCT measured cue box — the placement probe set.

    NOT just the tallest: the overlap gate divides by the probe box's OWN area, so
    no single cue is the universal worst case. Against a face band near the
    caption's bottom edge a SHORT one-line cue reports far more coverage than a
    tall two-line one (same intersection, smaller denominator), while a band
    higher up only reaches the tall box. Deduped by measured size, and cheap —
    ``max_lines`` is clamped to 1-2, so a video yields only a handful of shapes.
    """

    from app.pipeline.render_geometry import NormalizedBox  # noqa: PLC0415

    seen: set[tuple[float, float]] = set()
    boxes: list[NormalizedBox] = []
    for cue in cues:
        raw = cue.get("smart_render_box")
        if not isinstance(raw, dict):
            continue
        box = NormalizedBox(**raw)
        key = (round(box.height, 5), round(box.width, 5))
        if key in seen:
            continue
        seen.add(key)
        boxes.append(box)
    # No cue carries a measured box (shouldn't happen post-compile) → a centered
    # lower-third probe so the chooser still has a finite box to translate.
    return boxes or [NormalizedBox(0.3, 0.58, 0.7, 0.705)]


def _apply_face_aware_caption_placement(
    *,
    base: dict[str, Any],
    cues: list[dict[str, Any]],
    base_path: str,
    smart_compiled: Any,
    job_id: str,
    variant_id: str,
) -> tuple[list, dict[str, Any], list[dict[str, Any]]]:
    """Choose ONE static caption y from sampled faces, persist it, re-measure cues.

    Runs POST-reframe (faces can only be located on final geometry). Mutates
    ``base``: ``smart_caption_policy['y_frac']`` and ``caption_margin_v`` follow the
    chosen y (``caption_position_user_edited`` is deliberately NOT set — a first-
    render placement, not a user edit), and ``smart_validation_receipts
    ['caption_placement']`` records the decision. Returns the sampled faces (REUSED
    for card arbitration so the subprocess never runs twice), the sampler receipt,
    and the cues re-measured at the chosen y so protected boxes match the burned
    captions (finding TEST-1). ``choose_caption_y_frac`` itself never raises; the
    caller wraps this orchestration (ffprobe / sample / re-measure) fail-open.
    """
    from app.pipeline.captions import (  # noqa: PLC0415
        prepare_smart_caption_cues,
        y_frac_to_margin_v,
    )
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.render_geometry import (  # noqa: PLC0415
        choose_caption_y_frac,
        sample_face_regions,
    )
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    policy = base["smart_caption_policy"]
    preset_y = float(policy["y_frac"])
    candidates = (preset_y, *_FACE_PLACEMENT_EXTRA_CANDIDATES)

    # Anchor UNION on the RENDERED base duration (never the original clip — a
    # silence-cut base is shorter, and seeking past its EOF yields undecodable
    # frames): evenly-spaced ∪ camera-intent ∪ media-overlay starts, deduped.
    base_duration = float(probe_video(base_path).duration_s)
    intent_times = [
        float(intent.get("at_s") or 0.0) for intent in getattr(smart_compiled, "camera_intents", [])
    ]
    intent_times.extend(
        float(event.get("start_s") or 0.0) for event in smart_compiled.media_overlays
    )
    # Each lane is capped on its OWN budget (see the constants): the intent lane
    # can never grow past the sampler's historical 12, and the evenly-spaced lane
    # always survives in full so a front-loaded plan can't reduce this permanent
    # placement decision to a sample of the video's first few seconds.
    intent_anchors = _dedupe_anchor_times(intent_times)[:_FACE_PLACEMENT_MAX_INTENT_ANCHORS]
    anchors = _dedupe_anchor_times(
        intent_anchors + _evenly_spaced_anchors(base_duration, n=_FACE_PLACEMENT_SPACED_ANCHORS)
    )

    # Timeout scales with the (now bounded) anchor count on top of a base term
    # sized for the subprocess's own cold start. max_samples matches the union so
    # the cap above is the only limit. A kill no longer discards the anchors the
    # worker already streamed (T-CAP011-2, shipped).
    face_regions, face_receipt = sample_face_regions(
        base_path,
        anchors,
        max_samples=max(len(anchors), 1),
        timeout_s=(
            _FACE_PLACEMENT_TIMEOUT_BASE_S + _FACE_PLACEMENT_TIMEOUT_PER_ANCHOR_S * len(anchors)
        ),
        count_decoded=True,
    )

    _, title_boxes = _smart_caption_protected_regions(base, cues)

    chosen_y, placement_receipt = choose_caption_y_frac(
        face_regions,
        face_receipt,
        _distinct_caption_probe_boxes(cues),
        title_boxes,
        candidates,
    )

    # Re-measure ONLY so protected boxes match reality — the burn-time ASS geometry
    # already follows the persisted policy y automatically. Done against a COPY of
    # the policy, before anything on `base` is touched.
    remeasured = prepare_smart_caption_cues(cues, {**policy, "y_frac": chosen_y})

    record_pipeline_event(
        "smart_captions",
        "caption_placement_chosen",
        {
            "variant_id": variant_id,
            "chosen_y_frac": chosen_y,
            "preset_y_frac": preset_y,
            "status": placement_receipt.get("status"),
            "reason": placement_receipt.get("reason"),
            "anchors": len(anchors),
        },
    )

    # ── Commit. Every fallible step is already done, so `base` is mutated only on
    # the success path and nothing below can raise. Mutating earlier would break
    # the caller's fail-open contract: an exception after a partial write would
    # burn captions at the chosen y while the protected boxes still described the
    # preset-y band — precisely the phantom-band mismatch this feature prevents.
    policy["y_frac"] = chosen_y
    base["caption_margin_v"] = y_frac_to_margin_v(chosen_y)
    if not isinstance(base.get("smart_validation_receipts"), dict):
        base["smart_validation_receipts"] = {}
    base["smart_validation_receipts"]["caption_placement"] = placement_receipt
    return face_regions, face_receipt, remeasured


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
    render_trace_id: str | None = None,
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
    from app.pipeline.transcribe import (  # noqa: PLC0415
        transcribe_whisper,
        transcribe_whisper_cached,
    )
    from app.services.pipeline_trace import record_render_stage, render_stage_timer  # noqa: PLC0415
    from app.storage import upload_public_read  # noqa: PLC0415

    variant_id = spec["variant_id"]

    def _stage_timer(
        stage: str,
        *,
        cache: dict[str, Any] | None = None,
        retry: dict[str, Any] | None = None,
        counts: dict[str, Any] | None = None,
    ):
        return render_stage_timer(
            stage,
            trace_id=render_trace_id,
            variant_id=variant_id,
            render_generation_id=spec.get("storage_generation"),
            cache=cache,
            retry=retry,
            counts=counts,
        )

    def _record_stage(
        stage: str,
        *,
        elapsed_ms: int = 0,
        status: str = "ok",
        cache: dict[str, Any] | None = None,
        retry: dict[str, Any] | None = None,
        counts: dict[str, Any] | None = None,
        error_class: str | None = None,
    ) -> None:
        record_render_stage(
            stage,
            elapsed_ms=elapsed_ms,
            status=status,
            trace_id=render_trace_id,
            variant_id=variant_id,
            render_generation_id=spec.get("storage_generation"),
            cache=cache,
            retry=retry,
            counts=counts,
            error_class=error_class,
        )

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
    smart_preset_receipt: dict[str, Any] | None = None
    if smart_captions is not None:
        from app.smart_edit.presets import load_preset  # noqa: PLC0415

        try:
            resolved_preset = load_preset(
                smart_captions["preset_id"], smart_captions["preset_version"]
            )
            if smart_v2:
                smart_caption_policy = resolved_preset.caption.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — stale assignment must fail open
            log.warning(
                "smart_caption_primary_preset_failed_open",
                job_id=job_id,
                variant_id=variant_id,
                error_class=type(exc).__name__,
            )
            smart_preset_receipt = {
                "status": "failed_open",
                "error_class": type(exc).__name__,
            }
            smart_captions = None
            smart_v2 = False
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
        # GCS key of the cached subject matte ({base_key}.matte.mp4) once a
        # behind_subject text element has rendered — reburns reuse it instead
        # of recomputing (same field/semantics as the montage lane).
        "subject_matte_path": None,
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
        "caption_size_px": spec.get("caption_size_px"),
        "caption_text_color": spec.get("caption_text_color"),
        "caption_highlight_color": spec.get("caption_highlight_color"),
        "caption_stroke_width": spec.get("caption_stroke_width"),
        "caption_shadow_enabled": spec.get("caption_shadow_enabled"),
        "caption_font_user_edited": spec.get("caption_font_user_edited"),
        "caption_position_user_edited": spec.get("caption_position_user_edited"),
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
        "smart_validation_receipts": (
            {"preset_resolution": smart_preset_receipt} if smart_preset_receipt else None
        ),
        "smart_caption_policy": smart_caption_policy,
        "smart_music_treatment": None,
        "smart_audio_receipt": None,
        "smart_shadow_comparison": None,
        "boundary_effects": None,
        "camera_effects": None,
        "text_elements_materialized_from": None,
        # Silence-cut summary {removed, time_saved_s, version} (plans/010) — set
        # only when the stage ran to a plan; drives the admin cut-plan viewer.
        "silence_cut": None,
        "speech_cut_candidates": None,
        "speech_cut_forced_removals": None,
        "speech_cuts_disabled": False,
    }
    if base.get("smart_caption_policy") is not None:
        base["smart_caption_policy"] = _effective_smart_caption_policy(
            base,
            ass_font=resolve_caption_font(caption_font),
            margin_v=caption_margin_v,
        )
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

        with _stage_timer("asset_probe"):
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
        if smart_captions is not None:
            with _stage_timer("asset_context_loading"):
                smart_assets, asset_context_receipt = _load_smart_caption_assets_fail_open(job_id)
        else:
            smart_assets, asset_context_receipt = [], None
        trusted_caption_aliases = _smart_caption_trusted_aliases(
            smart_captions,
            smart_assets,
        )

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
        sc_apply_failed = False
        if settings.silence_cut_enabled or settings.retake_cut_enabled:
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
                with _stage_timer("silence_cut_analysis"):
                    sc_entry = _silence_cut_analysis(
                        clip_path,
                        float(probe.duration_s),
                        job_id=job_id,
                        cache=silence_cut_cache,
                        source_fingerprint=next(iter(clip_id_to_local)),
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
                # Content-addressed cache: re-renders of the same clip reuse the
                # identical transcript (plan 012 P1-4), so captions stop drifting
                # run-to-run. Fail-open to a live transcribe on any cache error.
                transcript_t0 = time.monotonic()
                transcript = transcribe_whisper_cached(clip_path, language=None)
                _record_stage(
                    "transcription",
                    elapsed_ms=int((time.monotonic() - transcript_t0) * 1000),
                    cache={
                        "name": "transcript",
                        "status": getattr(transcript, "cache_status", "unknown"),
                    },
                    counts={"word_count": len(transcript.words)},
                )
                detected_lang = transcript.language or detected_lang
                cues = build_plain_cues(transcript.words, attach_words=True)
            with _stage_timer("caption_correction", counts={"cue_count": len(cues)}):
                cues = correct_caption_cues(
                    cues,
                    detected_lang,
                    model=settings.caption_correction_model,
                    enabled=settings.subtitled_caption_correction_enabled,
                    trusted_aliases=trusted_caption_aliases,
                )
            with _stage_timer("caption_preparation", counts={"cue_count": len(cues)}):
                cues = resplit_cues_into_sentences(cues)
            canonical_cues = copy.deepcopy(cues)
            if smart_captions is not None and cues:
                try:
                    with _stage_timer("smart_caption_compile", counts={"cue_count": len(cues)}):
                        smart_compiled, smart_state = _compile_smart_caption_render_plan(
                            cues=cues,
                            smart_captions=smart_captions,
                            detected_lang=detected_lang,
                            job_id=job_id,
                            smart_assets=smart_assets,
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
        camera_intents = (
            getattr(smart_compiled, "camera_intents", []) if smart_compiled is not None else []
        )
        if camera_intents and not sc_apply:
            from app.pipeline.camera_effects import camera_effects_from_intents  # noqa: PLC0415

            base["camera_effects"] = (
                camera_effects_from_intents(
                    camera_intents,
                    existing_effects=base.get("camera_effects"),
                    duration_s=float(probe.duration_s),
                )
                or None
            )
        elif camera_intents:
            base["smart_validation_receipts"]["camera_render"] = {
                "requested": len(camera_intents),
                "applied": 0,
                "status": "omitted_silence_cut_timeline_unmapped",
                "full_video_encode_count": 1,
            }
        # Cut-output reuse (7A): a sibling variant may have already paid for the
        # cut encode of this exact clip — copy it instead of re-running ffmpeg.
        cut_reused = False
        if sc_apply and silence_cut_cache is not None:
            cached_cut = sc_entry.get("cut_video_path")
            if cached_cut and os.path.exists(cached_cut):
                shutil.copy2(cached_cut, base_path)
                cut_reused = True
        if not cut_reused:
            try:
                with _stage_timer(
                    "base_reframe_encode",
                    counts={"has_audio": probe.has_audio, "silence_cut": sc_apply},
                ):
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
                # exc now holds the still-unrecovered failure: the original when
                # no camera retry ran, the retry error when it also failed, or
                # None when the camera-less retry succeeded.
                if exc is not None and not sc_apply:
                    raise exc
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
                    sc_apply_failed = True
                    sc_plan = None
                    sc_words = None
                    sc_language = ""
                    sc_entry = None
                    if os.path.exists(base_path):
                        os.remove(base_path)
                    with _stage_timer(
                        "base_reframe_encode",
                        retry={"reason": "silence_cut_apply_failed_uncut_retry"},
                        counts={"has_audio": probe.has_audio, "silence_cut": False},
                    ):
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
            _record_stage(
                "base_reframe_encode",
                cache={"name": "silence_cut_video", "status": "hit"},
                counts={"has_audio": probe.has_audio, "silence_cut": True},
            )
        if not os.path.exists(base_path) or os.path.getsize(base_path) == 0:
            raise RuntimeError("subtitled base render produced empty output")
        if smart_v2 and sc_apply_failed:
            # The first semantic plan used the cut timeline. If that cut encode
            # failed open, rebuild from the successfully rendered uncut base so
            # captions and every later overlay stay audio-locked. Camera pulses
            # cannot be folded into the already-complete reframe and are recorded
            # as omitted; all non-camera Smart lanes can still ship.
            with _stage_timer("transcription", cache={"name": "transcript", "status": "live"}):
                transcript = transcribe_whisper(base_path, language=None)
            detected_lang = transcript.language or (language or "en")
            cues = build_plain_cues(transcript.words, attach_words=True)
            cues = correct_caption_cues(
                cues,
                detected_lang,
                model=settings.caption_correction_model,
                enabled=settings.subtitled_caption_correction_enabled,
                trusted_aliases=trusted_caption_aliases,
            )
            cues = resplit_cues_into_sentences(cues)
            canonical_cues = copy.deepcopy(cues)
            smart_compiled = None
            if smart_captions is not None and cues:
                try:
                    with _stage_timer("smart_caption_compile", counts={"cue_count": len(cues)}):
                        smart_compiled, smart_state = _compile_smart_caption_render_plan(
                            cues=cues,
                            smart_captions=smart_captions,
                            detected_lang=detected_lang,
                            job_id=job_id,
                            smart_assets=smart_assets,
                        )
                    if smart_compiled is not None:
                        cues = smart_compiled.caption_cues
                        base.update(smart_state)
                        replanned_camera_intents = getattr(smart_compiled, "camera_intents", [])
                        if replanned_camera_intents:
                            base["smart_validation_receipts"]["camera_render"] = {
                                "requested": len(replanned_camera_intents),
                                "applied": 0,
                                "status": "omitted_after_silence_cut_retry",
                                "full_video_encode_count": 1,
                            }
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "smart_captions_replan_after_cut_failure_failed_open",
                        job_id=job_id,
                        variant_id=variant_id,
                        error=str(exc)[:300],
                    )
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
            with _stage_timer("transcription", cache={"name": "transcript", "status": "live"}):
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
            with _stage_timer("caption_correction", counts={"cue_count": len(cues)}):
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
            with _stage_timer("caption_preparation", counts={"cue_count": len(cues)}):
                cues = resplit_cues_into_sentences(cues)
            canonical_cues = copy.deepcopy(cues)

        # Smart Captions semantic pass. It operates on the corrected, final cue
        # text and its word timings, then compiles only closed tokens into Nova's
        # existing caption/text/SFX lanes. Any planner/compiler bug fails open to
        # the normal subtitled render — the feature may add polish, never block
        # the creator's base video.
        if not smart_v2:
            smart_compiled = None
        if not smart_v2 and smart_captions is not None and cues:
            try:
                with _stage_timer("smart_caption_compile", counts={"cue_count": len(cues)}):
                    smart_compiled, smart_state = _compile_smart_caption_render_plan(
                        cues=cues,
                        smart_captions=smart_captions,
                        detected_lang=detected_lang,
                        job_id=job_id,
                        smart_assets=smart_assets,
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

        shadow_id = smart_captions.get("shadow_preset_id") if smart_captions else None
        shadow_version = smart_captions.get("shadow_preset_version") if smart_captions else None
        if shadow_id and shadow_version and canonical_cues:
            try:
                with _stage_timer(
                    "smart_caption_shadow_compile",
                    counts={"cue_count": len(canonical_cues)},
                ):
                    shadow_compiled, shadow_state = _compile_smart_caption_render_plan(
                        cues=canonical_cues,
                        smart_captions={
                            "preset_id": shadow_id,
                            "preset_version": shadow_version,
                            "sound_design": "off",
                        },
                        detected_lang=detected_lang,
                        job_id=job_id,
                        smart_assets=smart_assets,
                    )
                if shadow_compiled is not None:
                    base["smart_shadow_comparison"] = _smart_shadow_comparison(
                        primary_state=base,
                        shadow_state=shadow_state,
                        shadow_compiled=shadow_compiled,
                    )
            except Exception as exc:  # noqa: BLE001
                base["smart_shadow_comparison"] = {
                    "status": "failed_open",
                    "materialized": False,
                    "error_class": type(exc).__name__,
                }

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

        if asset_context_receipt is not None:
            if not isinstance(base.get("smart_validation_receipts"), dict):
                base["smart_validation_receipts"] = {}
            base["smart_validation_receipts"]["asset_context"] = asset_context_receipt

        if smart_compiled is not None and smart_compiled.boundary_effects:
            try:
                from app.pipeline.boundary_effects import apply_boundary_effects  # noqa: PLC0415

                boundary_base = os.path.join(variant_dir, "base_with_boundaries.mp4")
                with _stage_timer(
                    "effect_preparation",
                    counts={"boundary_effect_count": len(smart_compiled.boundary_effects)},
                ):
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

        caption_base_path = base_path
        camera_render_applied = False
        if base.get("camera_effects"):
            try:
                camera_base_path = os.path.join(variant_dir, "base_with_camera.mp4")
                reframe_and_export(
                    base_path,
                    0.0,
                    float(probe.duration_s),
                    aspect,
                    None,
                    camera_base_path,
                    output_fit="crop",
                    has_audio=probe.has_audio,
                    semantic_crop_pulses=base["camera_effects"],
                    **_canvas_kwargs(canvas_for_orientation(base.get("orientation"))),
                )
                caption_base_path = camera_base_path
                camera_render_applied = True
                base["smart_validation_receipts"]["camera_render"] = {
                    "requested": len(base["camera_effects"]),
                    "applied": len(base["camera_effects"]),
                    "status": "applied",
                    "full_video_encode_count": 1,
                }
            except Exception as exc:  # noqa: BLE001 — camera emphasis fails open
                log.warning(
                    "smart_camera_effects_failed_open",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )
                base["smart_validation_receipts"]["camera_render"] = {
                    "requested": len(base.get("camera_effects") or []),
                    "applied": 0,
                    "status": "failed_open",
                    "error": str(exc)[:160],
                    "full_video_encode_count": 0,
                }

        # ── Feature C: face-aware caption placement (plan 011 §Feature C) ─────
        # BEFORE the caption burn (the ordering surgery): sample faces on the
        # rendered base, move the caption band off the speaker's face, persist the
        # chosen y, and re-measure cues so the protected boxes match the burn. The
        # samples are reused for card arbitration below (never sampled twice).
        # Fail-open — any error keeps the preset geometry. Flag off ⇒ this block is
        # inert and the anchor set / receipts stay byte-identical.
        face_regions_placed: list | None = None
        face_receipt_placed: dict[str, Any] | None = None
        if (
            settings.smart_caption_face_placement_enabled
            and smart_v2
            and smart_compiled is not None
            and isinstance(base.get("smart_caption_policy"), dict)
            and cues
            # Documented precedence (docs/pipelines/smart-captions.md):
            # caption_position_user_edited > face-chosen > preset. The gate never
            # enforced it, so a re-render silently overrode a position the creator
            # had pinned by hand — and with the safe fallback below it could
            # overwrite that pin on a sampler failure too.
            and not base.get("caption_position_user_edited")
        ):
            try:
                with _stage_timer("caption_effect_preparation", counts={"cue_count": len(cues)}):
                    face_regions_placed, face_receipt_placed, cues = (
                        _apply_face_aware_caption_placement(
                            base=base,
                            cues=cues,
                            base_path=caption_base_path,
                            smart_compiled=smart_compiled,
                            job_id=job_id,
                            variant_id=variant_id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 — fail open to preset geometry
                face_regions_placed = None
                face_receipt_placed = None
                log.warning(
                    "smart_caption_face_placement_failed_open",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:300],
                )

        final_path = os.path.join(variant_dir, "final.mp4")
        # Deterministic caption-free-base key — also the matte cache anchor
        # (`{key}.matte.mp4`), so build it once here and reuse it for the
        # conditional base upload below.
        base_gcs_key = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_base.mp4"
        subtitled_matte_path = base.get("subject_matte_path")
        if getattr(settings, "subtitled_text_lane_enabled", False) or smart_compiled is not None:
            with _stage_timer("composition", counts={"cue_count": len(cues)}):
                # A camera-warped substrate needs its own matte (the mask must
                # register to the warped pixels) under a camera-scoped key, and
                # it must NOT be persisted — reburns run on the clean base and
                # would inherit a misaligned occlusion.
                composed_final, composed_matte_path = _compose_subtitled_final(
                    caption_base_path,
                    {
                        **base,
                        "caption_cues": cues or None,
                        "caption_language": detected_lang,
                        **({"subject_matte_path": None} if camera_render_applied else {}),
                    },
                    variant_dir,
                    job_id=job_id,
                    variant_id=variant_id,
                    upload_key_base=(
                        f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_camera_base.mp4"
                        if camera_render_applied
                        else base_gcs_key
                    ),
                )
                final_path = composed_final
                if not camera_render_applied:
                    subtitled_matte_path = composed_matte_path
        elif cues:
            ass_path = os.path.join(variant_dir, "captions.ass")
            ass_font = resolve_caption_font(caption_font)
            caption_appearance = _caption_style_overrides(base)
            caption_appearance_kwargs = (
                {"appearance": caption_appearance} if caption_appearance is not None else {}
            )
            effective_smart_policy = _effective_smart_caption_policy(
                base,
                ass_font=ass_font,
                margin_v=caption_margin_v,
            )
            with _stage_timer("caption_burn", counts={"cue_count": len(cues)}):
                if caption_style == "word":
                    generate_word_pop_ass(
                        cues,
                        ass_path,
                        font_name=ass_font,
                        margin_v=caption_margin_v,
                        **caption_appearance_kwargs,
                    )
                else:
                    generate_ass_from_cues(
                        cues,
                        ass_path,
                        font_name=ass_font,
                        style="plain",
                        margin_v=caption_margin_v,
                        pop_in=True,
                        **caption_appearance_kwargs,
                        **(
                            {"smart_policy": effective_smart_policy}
                            if effective_smart_policy is not None
                            else {}
                        ),
                    )
                burn_captions_on_video(caption_base_path, ass_path, FONTS_DIR, final_path)
        else:
            # No detectable speech → ship the clean clip; the UI shows the empty-caption
            # state. NOT a failure — a caption-less talking clip is still valid output.
            shutil.copy2(caption_base_path, final_path)
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

                with _stage_timer(
                    "audio_preparation",
                    counts={"sfx_intent_count": len(smart_compiled.sfx_intents)},
                ):
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
            with _stage_timer(
                "audio_preparation",
                counts={"audio_intent_count": len(smart_compiled.audio_treatment_intents)},
            ):
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

        protected_boxes: list[dict[str, object]] = []
        if face_regions_placed is not None:
            # Feature C already sampled faces on the anchor UNION (a superset of the
            # camera/media times) BEFORE the caption burn and re-measured the cues
            # at the chosen y. Reuse those faces for card arbitration — never re-run
            # the subprocess — so card face-protection is coverage-identical-or-
            # better (ARCH-2/TEST-2) and matches the burned caption band (TEST-1).
            caption_regions, title_regions = _smart_caption_protected_regions(base, cues)
            protected_boxes.extend(region.as_dict() for region in caption_regions)
            protected_boxes.extend(region.as_dict() for region in title_regions)
            protected_boxes.extend(region.as_dict() for region in face_regions_placed)
            base["smart_validation_receipts"]["geometry_prepare"] = {
                **(face_receipt_placed or {}),
                "caption_boxes": len(caption_regions),
                "title_boxes": len(base.get("text_elements") or []),
                "protected_boxes": len(protected_boxes),
                "source": "face_placement",
            }
        elif smart_v2 and smart_compiled is not None and not smart_compiled.media_overlays:
            # Nothing to arbitrate against — skip the face-sampler subprocess
            # (~2s of cv2 startup) and the box measurements entirely.
            base["smart_validation_receipts"]["geometry_prepare"] = {"status": "skipped_no_media"}
        elif smart_v2 and smart_compiled is not None:
            from app.pipeline.render_geometry import sample_face_regions  # noqa: PLC0415

            caption_regions, title_regions = _smart_caption_protected_regions(base, cues)
            protected_boxes.extend(region.as_dict() for region in caption_regions)
            protected_boxes.extend(region.as_dict() for region in title_regions)
            anchor_times = [
                float(intent.get("at_s") or 0.0)
                for intent in getattr(smart_compiled, "camera_intents", [])
            ]
            anchor_times.extend(
                float(event.get("start_s") or 0.0) for event in smart_compiled.media_overlays
            )
            with _stage_timer(
                "caption_effect_preparation",
                counts={"anchor_count": len(anchor_times)},
            ):
                face_regions, face_receipt = sample_face_regions(caption_base_path, anchor_times)
            protected_boxes.extend(region.as_dict() for region in face_regions)
            base["smart_validation_receipts"]["geometry_prepare"] = {
                **face_receipt,
                "caption_boxes": len(caption_regions),
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
                with _stage_timer("upload", counts={"artifact": "pre_media"}):
                    upload_public_read(final_path, pre_media_gcs)
                media_target = (
                    f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_pre_sfx.mp4"
                    if sound_effects or music_treatment
                    else output_gcs
                )
                layout_receipts: list[dict[str, Any]] = []
                applied_media_cards: list[dict[str, Any]] = []
                # v2 always opts into geometry arbitration (an empty protection
                # list still enables simultaneous-card dedup); v1 must pass None
                # so its layout stays byte-stable pre-arbitration.
                with _stage_timer(
                    "composition_media_overlay",
                    counts={"card_count": len(media_cards)},
                ):
                    output_url = apply_media_overlays(
                        pre_media_gcs,
                        media_cards,
                        media_target,
                        job_id=job_id,
                        protected_boxes=(
                            protected_boxes if smart_v2 and smart_compiled is not None else None
                        ),
                        layout_receipt_out=layout_receipts,
                        applied_cards_out=applied_media_cards,
                    )
                rendered_gcs = media_target
                base["pre_media_overlay_video_path"] = pre_media_gcs
                # Persist compiled cards (review D4): survivors carry their
                # arbitration-resolved geometry, download-failed cards keep
                # their original payload so the next reburn retries them.
                # Arbitration-OMITTED cards are dropped — the reburn path has
                # no arbitration, so persisting them would resurrect the
                # occlusion the omission prevented. The applied manifest
                # records which subset actually reached the burned video.
                base["media_overlays"] = _merged_media_overlay_persistence(
                    media_cards, applied_media_cards, layout_receipts
                )
                base["media_overlays_applied_ids"] = [
                    str(card.get("id")) for card in applied_media_cards
                ]
                base["smart_validation_receipts"]["visual_render"] = {
                    "requested": len(media_cards),
                    "applied": len(applied_media_cards),
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
                base["media_overlays_applied_ids"] = None
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
                    with _stage_timer("upload", counts={"artifact": "pre_sfx"}):
                        upload_public_read(final_path, pre_sfx_gcs)
                else:
                    pre_sfx_gcs = rendered_gcs
                placements = coerce_sound_effects(sound_effects) or []
                if smart_v2:
                    with _stage_timer(
                        "audio_render",
                        counts={"sfx_count": len(placements), "music_bed": bool(music_treatment)},
                    ):
                        output_url, audio_receipt = apply_smart_audio_treatment(
                            pre_sfx_gcs,
                            placements,
                            output_gcs,
                            music_bed=music_treatment,
                            job_id=job_id,
                        )
                    base["smart_audio_receipt"] = audio_receipt
                else:
                    with _stage_timer("audio_render", counts={"sfx_count": len(placements)}):
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
            with _stage_timer("upload", counts={"artifact": "final"}):
                output_url = upload_public_read(final_path, output_gcs)
        # Persist the caption-free base when captions exist. With the text lane on,
        # persist it even for cue-less clips so later user-authored text can fast-reburn.
        base_gcs: str | None = None
        should_upload_base = bool(cues) or getattr(settings, "subtitled_text_lane_enabled", False)
        if should_upload_base and os.path.exists(base_path) and os.path.getsize(base_path) > 0:
            base_gcs = base_gcs_key
            with _stage_timer("upload", counts={"artifact": "base"}):
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
        if sc_entry is not None:
            base["speech_cut_candidates"] = sc_entry.get("review_candidates") or None
        if smart_v2 and base.get("smart_validation_receipts") is not None:
            try:
                import resource  # noqa: PLC0415

                peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            except Exception:  # noqa: BLE001
                peak_rss = None
            base["smart_validation_receipts"]["performance"] = {
                "wall_time_ms": round((time.monotonic() - smart_render_started) * 1000),
                "reframe_full_video_encode_count": 1,
                "camera_additional_full_video_encodes": 1 if camera_render_applied else 0,
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
            "subject_matte_path": subtitled_matte_path,
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
            "smart_shadow_comparison": base["smart_shadow_comparison"],
            "boundary_effects": base["boundary_effects"],
            "camera_effects": base["camera_effects"],
            "text_elements": base.get("text_elements"),
            "text_elements_user_edited": base.get("text_elements_user_edited"),
            "text_elements_materialized_from": base.get("text_elements_materialized_from"),
            "silence_cut": silence_cut_summary,
            "speech_cut_candidates": base.get("speech_cut_candidates"),
            "speech_cut_forced_removals": base.get("speech_cut_forced_removals"),
            "speech_cuts_disabled": base.get("speech_cuts_disabled", False),
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


@celery_app.task(
    name="rerender_caption_camera_effects",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=False,
    max_retries=7,
    soft_time_limit=1740,
    time_limit=1800,
)
def rerender_caption_camera_effects(
    self, job_id: str, variant_id: str, render_gen_id: str | None = None
) -> None:
    """Rebuild a caption variant's clean base after editable camera-effect changes."""
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    terminal_state = {"accepted": False}
    with pipeline_trace_for(job_id):
        try:
            _run_rerender_caption_camera_effects(
                job_id,
                variant_id,
                render_gen_id=render_gen_id,
                terminal_state=terminal_state,
            )
        except OperationalError:
            raise
        except Exception as exc:
            log.error(
                "caption_camera_rerender_failed",
                job_id=job_id,
                variant_id=variant_id,
                error=str(exc)[:MAX_ERROR_DETAIL_LEN],
                exc_info=True,
            )
            _update_variant_entry(
                job_id,
                variant_id,
                {
                    "render_status": "failed" if terminal_state["accepted"] else "ready",
                    "render_error": str(exc)[:500],
                },
                expected_render_gen_id=render_gen_id,
                outcome=(
                    "caption_camera_rerender_failed_post_swap"
                    if terminal_state["accepted"]
                    else "caption_camera_rerender_failed"
                ),
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
    from app.pipeline.captions import (  # noqa: PLC0415
        CAPTION_Y_FRAC_MAX,
        CAPTION_Y_FRAC_MIN,
        SUBTITLED_CAPTION_MARGIN_V,
        y_frac_to_margin_v,
    )

    raw = variant.get("caption_margin_v")
    if raw is None:
        return SUBTITLED_CAPTION_MARGIN_V
    try:
        margin_v = int(raw)
    except (TypeError, ValueError):
        return SUBTITLED_CAPTION_MARGIN_V
    # A higher y_frac sits lower on screen → a smaller MarginV, so the max y_frac
    # bounds the min margin and vice versa.
    min_margin = y_frac_to_margin_v(CAPTION_Y_FRAC_MAX)
    max_margin = y_frac_to_margin_v(CAPTION_Y_FRAC_MIN)
    if min_margin <= margin_v <= max_margin:
        return margin_v
    return SUBTITLED_CAPTION_MARGIN_V


def _merged_media_overlay_persistence(
    requested: list[Any],
    applied: list[dict[str, Any]],
    layout_receipts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Persist compiled cards so TRANSIENT failures self-heal on reburn.

    Survivors of the apply pass carry their arbitration-resolved geometry
    (persisted state matches the burned video for what IS in it); cards that
    failed download keep their original compiled payload so a later reburn
    retries them. Cards the arbitration DELIBERATELY omitted (no collision-free
    layout) are dropped from persistence — the reburn path applies persisted
    cards without arbitration, so persisting them would resurrect the exact
    face/caption occlusion the omission prevented. The companion
    `media_overlays_applied_ids` manifest records the burned subset.
    """
    applied_by_id = {str(card.get("id")): card for card in applied}
    omitted_ids = {
        str(receipt.get("id"))
        for receipt in layout_receipts or []
        if str(receipt.get("decision") or "").startswith("omitted")
    }
    merged: list[dict[str, Any]] = []
    for card in requested:
        card_id = str(card.id)
        if card_id in applied_by_id:
            merged.append(applied_by_id[card_id])
        elif card_id not in omitted_ids:
            merged.append(card.model_dump(exclude_none=True))
    return merged


def _effective_smart_caption_policy(
    variant: dict,
    *,
    ass_font: str,
    margin_v: int | None,
) -> dict[str, Any] | None:
    """Merge explicit creator caption overrides into the pinned Smart policy."""

    raw = variant.get("smart_caption_policy")
    if not isinstance(raw, dict):
        return None
    policy = dict(raw)
    if variant.get("caption_font_user_edited"):
        policy["font_family"] = ass_font
    if variant.get("caption_position_user_edited") and margin_v is not None:
        from app.pipeline.captions import margin_v_to_y_frac  # noqa: PLC0415

        policy["y_frac"] = margin_v_to_y_frac(margin_v)
    if variant.get("caption_size_px") is not None:
        policy["font_size_px"] = variant.get("caption_size_px")
    if variant.get("caption_text_color"):
        policy["color"] = variant.get("caption_text_color")
        policy["color_user_edited"] = True
    if variant.get("caption_stroke_width") is not None:
        policy["stroke_width"] = variant.get("caption_stroke_width")
    if variant.get("caption_shadow_enabled") is not None:
        policy["shadow_enabled"] = bool(variant.get("caption_shadow_enabled"))
    return policy


def _caption_style_overrides(variant: dict) -> dict[str, Any] | None:
    appearance = {
        "font_size_px": variant.get("caption_size_px"),
        "color": variant.get("caption_text_color"),
        "highlight_color": variant.get("caption_highlight_color"),
        "stroke_width": variant.get("caption_stroke_width"),
        "shadow_enabled": variant.get("caption_shadow_enabled"),
    }
    return appearance if any(value is not None for value in appearance.values()) else None


def _resolve_cue_font_overrides(cues: list[dict]) -> list[dict]:
    """Resolve each cue's per-cue `font_family` override (plan PR-A) from its
    persisted registry key to the burn-ready libass family name.

    Mirrors the variant-level resolution one line above every call site
    (``ass_font = resolve_caption_font(variant.get("voiceover_caption_font"))``):
    the PATCH validates/stores a font-registry KEY (``CaptionCue.font_family`` via
    ``is_valid_caption_font``); `app.pipeline.captions` treats a cue's
    `font_family` as already burn-ready, so the key → ass_name mapping happens
    exactly once, here, right before burning — never persisted back. Cues with no
    override pass through untouched (dict identity preserved where possible).
    """
    from app.pipeline.narrated_assembler import resolve_caption_font  # noqa: PLC0415

    resolved: list[dict] = []
    for cue in cues:
        if cue.get("font_family"):
            cue = {**cue, "font_family": resolve_caption_font(cue["font_family"])}
        resolved.append(cue)
    return resolved


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
        # Karaoke settle-color contract: user-edited variants hold the user's
        # element color after the sweep (see build_overlays_from_text_elements).
        user_edited=bool(variant.get("text_elements_user_edited")),
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


def _compose_subtitled_final(
    base_local: str,
    variant: dict,
    tmpdir: str,
    *,
    job_id: str,
    variant_id: str,
    upload_key_base: str,
) -> tuple[str, str | None]:
    """Compose a subtitled final: authored text first, persisted captions last.

    Authored text elements burn through the Skia renderer, so behind_subject
    occlusion works here exactly like the montage lane: when any element wants
    it, `_resolve_subject_matte_for_burn` runs (cache reuse via the variant's
    `subject_matte_path`, compute + sanity gate + upload next to
    `upload_key_base` otherwise; any failure strips the flags and burns plain
    text). Captions burn through libass afterwards and are never occluded.

    Returns ``(final_path, subject_matte_path)``. The matte path is the
    variant's cached path when no element wants occlusion (or on failure —
    the resolver never clobbers a good cache), or the freshly-uploaded key on
    first compute; callers MUST persist it into their variant patch so
    reburns stay cache-fast. The keyword-only plumbing args are required so a
    future call site cannot silently reintroduce the no-matte no-op this
    function shipped with (prod job 1e768d5b).
    """
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.text_overlay_skia import burn_text_overlays_skia  # noqa: PLC0415

    variant = _project_carousel_timed_lanes(variant)
    text_overlays = _text_element_burn_dicts(variant)
    matte_gcs_path = variant.get("subject_matte_path")
    captions_input = base_local
    if text_overlays:
        provider = None
        if any(ov.get("behind_subject") for ov in text_overlays):
            try:
                duration_s = float(probe_video(base_local).duration_s)
            except Exception:  # noqa: BLE001
                duration_s = float(variant.get("duration_s") or 0.0)
            provider, matte_gcs_path, text_overlays = _resolve_subject_matte_for_burn(
                video_path=base_local,
                overlays=text_overlays,
                tmpdir=tmpdir,
                cached_matte_path=matte_gcs_path,
                upload_key_base=upload_key_base,
                duration_s=duration_s,
                job_id=job_id,
                variant_id=variant_id,
                # Subtitled variants are single-clip — no interior hard cuts.
                # (Silence-cut keep-segment joins are a known unmodeled
                # discontinuity; acceptable, boundary hints are best-effort.)
                cut_boundaries_s=None,
            )
        text_burned = os.path.join(tmpdir, "subtitled_text_underlay.mp4")
        burn_text_overlays_skia(
            base_local,
            text_overlays,
            text_burned,
            tmpdir,
            matte=provider,
            canvas=canvas_for_orientation(variant.get("orientation")),
        )
        captions_input = text_burned

    final_path = os.path.join(tmpdir, "subtitled_final.mp4")
    _burn_persisted_captions_onto_base(captions_input, final_path, variant, tmpdir)
    return final_path, matte_gcs_path


def _should_compose_subtitled_final(variant: dict) -> bool:
    """Keep authored text and captions together on every subtitled reburn.

    The public text lane, visual-block autoplan, and Smart Captions can each
    author text independently. Persisted Smart titles must therefore use the
    compositor even while the public text-lane rollout flag is off.
    """
    return variant.get("resolved_archetype") == "subtitled" and (
        getattr(settings, "subtitled_text_lane_enabled", False)
        or (
            variant.get("text_elements_materialized_from") == "smart_captions"
            and bool(variant.get("text_elements"))
        )
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
    variant = _project_carousel_timed_lanes(variant)
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
    # Per-cue font_family overrides (plan PR-A) store a registry key, same
    # contract as `voiceover_caption_font` below — resolve to the libass family
    # name once, here, before either burn path reads it. A no-op for cues with
    # no override (the common case) and for the word-pop path, which doesn't
    # read per-cue style fields at all.
    cues = _resolve_cue_font_overrides(cues)
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
    smart_policy = _effective_smart_caption_policy(
        variant,
        ass_font=ass_font,
        margin_v=margin_v,
    )
    appearance = _caption_style_overrides(variant)
    appearance_kwargs = {"appearance": appearance} if appearance is not None else {}
    ass_path = os.path.join(tmpdir, "captions.ass")
    if subtitled_word_pop:
        # Real per-word times for cues left untouched; edited cues re-synthesize
        # inside generate_word_pop_ass (E3). Same safe margin as the first burn.
        generate_word_pop_ass(
            cues, ass_path, font_name=ass_font, margin_v=margin_v, **appearance_kwargs
        )
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
            **appearance_kwargs,
            **({"smart_policy": smart_policy} if smart_policy is not None else {}),
        )
    burn_captions_on_video(base_local, ass_path, FONTS_DIR, out_local)


def _run_rerender_caption_camera_effects(
    job_id: str,
    variant_id: str,
    render_gen_id: str | None = None,
    terminal_state: dict | None = None,
) -> None:
    from app.pipeline.camera_effects import normalize_camera_effects  # noqa: PLC0415
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.pipeline.reframe import reframe_and_export  # noqa: PLC0415
    from app.storage import (  # noqa: PLC0415
        delete_object_best_effort,
        download_to_file,
        upload_public_read,
    )
    from app.tasks.custom_effects_render import reapply_persisted_custom_effect  # noqa: PLC0415

    reapply_deadline = (
        time.monotonic() + _CAPTION_TASK_SOFT_TIME_LIMIT_S - _REAPPLY_DEADLINE_MARGIN_S
    )
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            log.error("caption_camera_rerender_job_not_found", job_id=job_id)
            return
        variants = (job.assembly_plan or {}).get("variants") or []
        variant = next((v for v in variants if v.get("variant_id") == variant_id), None)
    if variant is None:
        raise ValueError(f"variant {variant_id} not found on job {job_id}")
    if variant.get("resolved_archetype") != "subtitled":
        raise ValueError(f"variant {variant_id} is not a subtitled variant")
    render_variant = _project_carousel_timed_lanes(variant)
    rank = variant.get("rank") or 1
    old_video_path = variant.get("video_path")
    old_base_path = variant.get("base_video_path")
    if not old_base_path:
        raise ValueError(f"variant {variant_id} has no clean base video")

    if not _update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=render_gen_id,
        outcome="caption_camera_rerender_start",
    ):
        return

    (
        creator_base_path,
        visual_blocks_cache_path,
        motion_cache_path,
        motion_base_source_path,
        motion_identity,
    ) = _ensure_creator_layer_base(
        job_id=job_id,
        variant_id=variant_id,
        variant=render_variant,
        base_gcs_path=str(old_base_path),
    )

    with tempfile.TemporaryDirectory(prefix="nova_caption_camera_") as tmpdir:
        new_base_local = os.path.join(tmpdir, "clean_base.mp4")
        download_to_file(creator_base_path, new_base_local)
        probe = probe_video(new_base_local)
        aspect = "16:9" if variant.get("orientation") == "landscape" else "9:16"
        effects = normalize_camera_effects(
            render_variant.get("camera_effects") or [],
            duration_s=float(probe.duration_s),
        )

        caption_base_local = new_base_local
        if effects:
            caption_base_local = os.path.join(tmpdir, "camera_base.mp4")
            reframe_and_export(
                new_base_local,
                0.0,
                float(probe.duration_s),
                aspect,
                None,
                caption_base_local,
                output_fit="crop",
                has_audio=probe.has_audio,
                semantic_crop_pulses=effects,
                **_canvas_kwargs(canvas_for_orientation(variant.get("orientation"))),
            )
        # REAPPLY-ON-REBURN (not one-shot), same contract as persisted SFX/media-
        # overlay lanes below: a custom effect renders UNDER captions, so it must
        # burn onto caption_base_local BEFORE any caption/text compose step.
        # Never trusts the stored spec — reapply_persisted_custom_effect
        # re-validates it and fails open (unmodified video + cleared entry) on
        # any rejection or render failure.
        custom_effect_cleared = False
        custom_effect_applied = False
        if render_variant.get("custom_effects"):
            caption_base_local, custom_effect_cleared = reapply_persisted_custom_effect(
                caption_base_local, render_variant, tmpdir
            )
            custom_effect_applied = not custom_effect_cleared
        pixels_modified = bool(effects) or custom_effect_applied
        fresh_variant = {
            **render_variant,
            "camera_effects": effects or None,
            "base_video_path": None,
            "media_overlays": render_variant.get("media_overlays"),
            "pre_media_overlay_video_path": render_variant.get("pre_media_overlay_video_path"),
        }
        suffix = uuid.uuid4().hex[:8]
        camera_matte_path: str | None = None
        camera_matte_persist = False
        if _should_compose_subtitled_final(fresh_variant):
            # With camera effects (or a reapplied custom effect) the substrate is
            # warped/regraded — the matte must be recomputed against the changed
            # pixels under a scoped key, and NOT persisted (later reburns run on
            # the clean base, where the variant's cached matte stays valid).
            final_local, camera_matte_path = _compose_subtitled_final(
                caption_base_local,
                {**fresh_variant, "subject_matte_path": None} if pixels_modified else fresh_variant,
                tmpdir,
                job_id=job_id,
                variant_id=variant_id,
                upload_key_base=(
                    f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_camera_{suffix}.mp4"
                    if pixels_modified
                    else str(old_base_path)
                ),
            )
            camera_matte_persist = not pixels_modified
        else:
            final_local = os.path.join(tmpdir, "out.mp4")
            _burn_persisted_captions_onto_base(
                caption_base_local, final_local, fresh_variant, tmpdir
            )

        new_video_gcs = f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_camera_{suffix}.mp4"
        output_url = upload_public_read(final_local, new_video_gcs)
        duration_s = _rendered_duration_s(final_local)

    fresh_for_reapply = _fresh_variant_snapshot(job_id, variant_id) or variant
    will_reapply = _will_reapply_media_layers(fresh_for_reapply)
    patch: dict[str, Any] = {
        "video_path": new_video_gcs,
        "output_url": output_url,
        "base_video_path": old_base_path,
        # Store the authored pre-insertion lane, never the projected render copy.
        "camera_effects": variant.get("camera_effects") or None,
        "pre_media_overlay_video_path": None,
        "pre_sfx_video_path": None,
        **_creator_layer_cache_patch(
            visual_blocks_cache_path=visual_blocks_cache_path,
            motion_cache_path=motion_cache_path,
            motion_base_source_path=motion_base_source_path,
            motion_identity=motion_identity,
        ),
        "overlay_camera_rebuild_pending": False,
        **({"subject_matte_path": camera_matte_path} if camera_matte_persist else {}),
        **({"custom_effects": []} if custom_effect_cleared else {}),
    }
    if not fresh_for_reapply.get("media_overlays"):
        patch["media_overlays_render_dirty"] = False
    if duration_s is not None:
        patch["duration_s"] = duration_s
    if will_reapply:
        patch["render_status"] = "rendering"
    else:
        patch["render_status"] = "ready"
        patch["render_finished_at"] = datetime.utcnow().isoformat() + "Z"
    if not _update_variant_entry(
        job_id,
        variant_id,
        patch,
        expected_render_gen_id=render_gen_id,
        outcome="caption_camera_rerender",
    ):
        delete_object_best_effort(new_video_gcs)
        for new_path, old_path in (
            (visual_blocks_cache_path, variant.get("visual_blocks_base_path")),
            (motion_cache_path, variant.get("motion_base_path")),
        ):
            if new_path and new_path != old_path:
                delete_object_best_effort(new_path)
        return
    if terminal_state is not None:
        terminal_state["accepted"] = True
    _free_retired_visual_blocks_base(variant, visual_blocks_cache_path)
    _free_retired_motion_base(variant, motion_cache_path)
    _free_retired_media_snapshots(variant, (patch.get("video_path"), patch.get("base_video_path")))
    if old_video_path and old_video_path != new_video_gcs:
        delete_object_best_effort(old_video_path)
    if will_reapply and not _reapply_user_media_layers(
        job_id=job_id,
        variant_id=variant_id,
        expected_render_gen_id=render_gen_id,
        deadline_monotonic=reapply_deadline,
    ):
        _update_variant_entry(
            job_id,
            variant_id,
            {
                "render_status": "ready",
                "render_finished_at": datetime.utcnow().isoformat() + "Z",
            },
            expected_render_gen_id=render_gen_id,
            outcome="caption_camera_rerender_reapply_noop",
        )
    log.info(
        "caption_camera_rerender_done",
        job_id=job_id,
        variant_id=variant_id,
        effects=len(effects),
    )


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
    from app.tasks.custom_effects_render import reapply_persisted_custom_effect  # noqa: PLC0415

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
    if not _update_variant_entry(
        job_id,
        variant_id,
        {"render_status": "rendering"},
        expected_render_gen_id=render_gen_id,
        outcome="caption_reburn_start",
    ):
        return
    (
        render_base_path,
        visual_blocks_cache_path,
        motion_cache_path,
        motion_base_source_path,
        motion_identity,
    ) = _ensure_creator_layer_base(
        job_id=job_id,
        variant_id=variant_id,
        variant=variant,
        base_gcs_path=base_path,
    )
    custom_effect_cleared = False
    with tempfile.TemporaryDirectory(prefix="nova_caption_reburn_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        download_to_file(render_base_path, base_local)
        # REAPPLY-ON-REBURN, same contract as the persisted SFX/media-overlay
        # lanes further below — a custom effect renders UNDER captions, so it
        # burns onto base_local BEFORE any caption compose step. Fails open
        # (unmodified video + cleared entry) on any rejection/render failure.
        custom_effect_applied = False
        if variant.get("custom_effects"):
            base_local, custom_effect_cleared = reapply_persisted_custom_effect(
                base_local, variant, tmpdir
            )
            custom_effect_applied = not custom_effect_cleared
        reburn_matte_path: str | None = None
        reburn_matte_persist = False
        if _should_compose_subtitled_final(variant):
            variant = _fresh_variant_snapshot(job_id, variant_id) or variant
            out_local, reburn_matte_path = _compose_subtitled_final(
                base_local,
                {**variant, "subject_matte_path": None} if custom_effect_applied else variant,
                tmpdir,
                job_id=job_id,
                variant_id=variant_id,
                upload_key_base=(
                    f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_cap_effect_"
                    f"{uuid.uuid4().hex[:8]}"
                    if custom_effect_applied
                    else str(base_path)
                ),
            )
            reburn_matte_persist = not custom_effect_applied
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
    fresh_for_reapply = _fresh_variant_snapshot(job_id, variant_id) or variant
    will_reapply = _will_reapply_media_layers(fresh_for_reapply)
    # Local name `patch` is load-bearing: tests/test_media_overlay_byteidentity.py's
    # AST guard exempts merge-patch dicts assigned to locals named `patch`.
    patch: dict[str, Any] = {
        "video_path": new_gcs,
        "output_url": output_url,
        # Deliberate reset, never a stale round-trip: the old snapshots point at
        # the pre-reburn video (deleted below), so a stale key is a download-404.
        "pre_media_overlay_video_path": None,
        "pre_sfx_video_path": None,
        **_creator_layer_cache_patch(
            visual_blocks_cache_path=visual_blocks_cache_path,
            motion_cache_path=motion_cache_path,
            motion_base_source_path=motion_base_source_path,
            motion_identity=motion_identity,
        ),
        **({"subject_matte_path": reburn_matte_path} if reburn_matte_persist else {}),
        **({"custom_effects": []} if custom_effect_cleared else {}),
    }
    if will_reapply:
        # OV-7: the reapply chain owns the final ready/failed — no effect-less
        # "ready" observable between burn and reapply.
        patch["render_status"] = "rendering"
    if not fresh_for_reapply.get("media_overlays"):
        patch["media_overlays_render_dirty"] = False
    if not will_reapply:
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
        for new_path, old_path in (
            (visual_blocks_cache_path, variant.get("visual_blocks_base_path")),
            (motion_cache_path, variant.get("motion_base_path")),
        ):
            if new_path and new_path != old_path:
                delete_object_best_effort(new_path)
        return
    if terminal_state is not None:
        terminal_state["accepted"] = True  # F5: video swap landed
    _free_retired_visual_blocks_base(variant, visual_blocks_cache_path)
    _free_retired_motion_base(variant, motion_cache_path)
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
        # Heavy-source guard (2026-07-21 OOM): the bed-level reburn re-decodes
        # the durable originals through the narrated spine render — probe +
        # downscale here so a 4K job's reburn can't reproduce the incident.
        # Best-effort as a UNIT: this path never probed before, so a probe
        # failure must skip the guard (originals kept), not newly fail the task.
        try:
            from app.pipeline.source_guard import downscale_oversized_sources  # noqa: PLC0415
            from app.tasks.template_orchestrate import _probe_clips  # noqa: PLC0415

            downscale_oversized_sources(
                local_paths, _probe_clips(local_paths), tmpdir, job_id=job_id
            )
        except Exception as exc:  # noqa: BLE001 — guard is an optimization here
            log.warning(
                "bed_level_reburn_source_guard_skipped", job_id=job_id, error=str(exc)[:160]
            )
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

        # REAPPLY-ON-REBURN, same contract as the persisted SFX/media-overlay
        # lanes below — burns onto a COPY of new_base_path (never new_base_path
        # itself, which is uploaded below as the fresh CLEAN base) since a
        # custom effect renders UNDER captions, before this compose step.
        # Fails open on any rejection/render failure.
        from app.tasks.custom_effects_render import (  # noqa: PLC0415
            reapply_persisted_custom_effect,
        )

        caption_base_path = new_base_path
        custom_effect_cleared = False
        if variant.get("custom_effects"):
            caption_base_path, custom_effect_cleared = reapply_persisted_custom_effect(
                new_base_path, variant, tmpdir
            )

        out_local = os.path.join(tmpdir, "out.mp4")
        _burn_persisted_captions_onto_base(caption_base_path, out_local, variant, tmpdir)

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
        **({"custom_effects": []} if custom_effect_cleared else {}),
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
    if variant.get("smart_captions_applied"):
        raise ValueError("Smart Caption language changes require a new render")
    base_path = variant.get("base_video_path")
    if not base_path:
        raise ValueError("variant has no caption-free base — cannot re-transcribe")
    rank = variant.get("rank")
    word_pop = variant.get("voiceover_caption_style") == "word"
    ass_font = resolve_caption_font(variant.get("voiceover_caption_font"))
    caption_margin_v = _resolve_caption_margin_v(variant)
    caption_appearance = _caption_style_overrides(variant)

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
        (
            render_base_path,
            visual_blocks_cache_path,
            motion_cache_path,
            motion_base_source_path,
            motion_identity,
        ) = _ensure_creator_layer_base(
            job_id=job_id,
            variant_id=variant_id,
            variant=variant,
            base_gcs_path=base_path,
        )
        caption_base_local = base_local
        if render_base_path != base_path:
            caption_base_local = os.path.join(tmpdir, "caption_base.mp4")
            download_to_file(render_base_path, caption_base_local)
        # REAPPLY-ON-REBURN, same contract as the persisted SFX/media-overlay
        # lanes below — a custom effect renders UNDER captions, so it burns
        # onto caption_base_local BEFORE the caption compose step (fails open
        # on any rejection/render failure; the transcription pass above reads
        # only audio, so effect order relative to it doesn't matter).
        from app.tasks.custom_effects_render import (  # noqa: PLC0415
            reapply_persisted_custom_effect,
        )

        custom_effect_cleared = False
        custom_effect_applied = False
        if variant.get("custom_effects"):
            caption_base_local, custom_effect_cleared = reapply_persisted_custom_effect(
                caption_base_local, variant, tmpdir
            )
            custom_effect_applied = not custom_effect_cleared
        retx_matte_path: str | None = None
        retx_matte_persist = False
        if getattr(settings, "subtitled_text_lane_enabled", False):
            variant = {
                **variant,
                "caption_cues": cues or None,
                "caption_language": lang,
            }
            out_local, retx_matte_path = _compose_subtitled_final(
                caption_base_local,
                {**variant, "subject_matte_path": None} if custom_effect_applied else variant,
                tmpdir,
                job_id=job_id,
                variant_id=variant_id,
                upload_key_base=(
                    f"generative-jobs/{job_id}/variant_{rank}_{variant_id}_lang_effect_"
                    f"{uuid.uuid4().hex[:8]}"
                    if custom_effect_applied
                    else str(base_path)
                ),
            )
            retx_matte_persist = not custom_effect_applied
        else:
            out_local = os.path.join(tmpdir, "out.mp4")
            ass_path = os.path.join(tmpdir, "captions.ass")
            caption_appearance_kwargs = (
                {"appearance": caption_appearance} if caption_appearance is not None else {}
            )
            if word_pop:
                generate_word_pop_ass(
                    cues,
                    ass_path,
                    font_name=ass_font,
                    margin_v=caption_margin_v,
                    **caption_appearance_kwargs,
                )
            else:
                generate_ass_from_cues(
                    cues,
                    ass_path,
                    font_name=ass_font,
                    style="plain",
                    margin_v=caption_margin_v,
                    pop_in=True,
                    **caption_appearance_kwargs,
                )
            burn_captions_on_video(caption_base_local, ass_path, FONTS_DIR, out_local)
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
        **_creator_layer_cache_patch(
            visual_blocks_cache_path=visual_blocks_cache_path,
            motion_cache_path=motion_cache_path,
            motion_base_source_path=motion_base_source_path,
            motion_identity=motion_identity,
        ),
        **({"subject_matte_path": retx_matte_path} if retx_matte_persist else {}),
        **({"custom_effects": []} if custom_effect_cleared else {}),
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
        for new_path, old_path in (
            (visual_blocks_cache_path, variant.get("visual_blocks_base_path")),
            (motion_cache_path, variant.get("motion_base_path")),
        ):
            if new_path and new_path != old_path:
                delete_object_best_effort(new_path)
        return
    if terminal_state is not None:
        terminal_state["accepted"] = True  # F5: video swap landed
    _free_retired_visual_blocks_base(variant, visual_blocks_cache_path)
    _free_retired_motion_base(variant, motion_cache_path)
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


# Placement fields resolved by `_resolve_intro_overlay_params` and snapshotted onto
# the variant under `intro_placement` (see `_intro_placement_from_params`). MUST stay
# in sync with `text_element._INTRO_PLACEMENT_ADAPTER_KEYS` (the reader) — pinned by
# test_adapter_reads_every_persisted_placement_key.
_INTRO_PLACEMENT_KEYS = (
    "position",
    "position_x_frac",
    "position_y_frac",
    "max_width_frac",
    "text_anchor",
    "rotation_deg",
)

# The plain centered intro — the overwhelming majority of variants. Placement that
# equals this is persisted as None so those variants keep the exact dict they store
# today and the read adapter keeps its legacy (pre-`intro_placement`) path.
# Derived from the key tuple so a new placement field can only be added in one place
# (a key-set drift would make the equality below unsatisfiable and every centered
# variant would start persisting a dict).
_DEFAULT_INTRO_PLACEMENT: dict = {
    **dict.fromkeys(_INTRO_PLACEMENT_KEYS),
    "position": "center",
    "text_anchor": "center",
}


def _persisted_intro_position(existing: dict) -> str | None:
    """Named NON-CENTER position from a variant's `intro_placement` snapshot.

    None for legacy variants (no snapshot) AND for "center", so the re-render
    resolution stays byte-identical for both.

    Returning "center" here would NOT be a harmless no-op: the caller folds this
    into `agent_form["position"]`, which becomes the `advisory` that
    `resolve_overlay_style` uses to fill any key the curated set leaves null. A set
    with a null `intro.position` would then resolve `style["position"] == "center"`,
    which flips `has_explicit_position` True in `_resolve_intro_overlay_params` and
    SKIPS the placement-candidate branch — silently dropping a masonry intro's
    whitespace-pocket fracs on re-render.
    """
    placement = existing.get("intro_placement")
    if not isinstance(placement, dict):
        return None
    position = placement.get("position")
    if not isinstance(position, str) or position == _DEFAULT_INTRO_PLACEMENT["position"]:
        return None
    return position or None


def _intro_placement_from_params(params: dict, *, has_candidates: bool = False) -> dict | None:
    """Snapshot the RESOLVED intro placement for persistence on the variant.

    `_resolve_intro_overlay_params` folds knobs > curated set > agent advisory into
    one placement, but nothing ever wrote it back — so the editor's read adapter
    (`_base_text_elements_for_variant`) had to re-guess it and always landed on
    `build_intro_overlay`'s `_DEFAULT_POSITION`. A curated set or an
    `overlay_format_matcher` run that picked "bottom" burned at the bottom while
    the editor drew the element at mid-frame.

    Persisting it also lets a no-LLM re-render restore a non-default position:
    `_resolve_regen_text` reconstructs `agent_form` WITHOUT the agent's original
    position advisory, so without this snapshot the first text edit silently
    re-centered a "bottom" intro.

    Returns None for the plain centered placement — see `_DEFAULT_INTRO_PLACEMENT`.

    EXCEPT when `has_candidates`: a variant carrying `text_placement_candidates`
    has something for the adapter's legacy fallback to disagree with, so even a
    plain centered resolution must be recorded. Every shipped style set pins
    `intro.position="center"`, which makes `has_explicit_position` True and the
    resolver SKIP the candidate branch — so a masonry variant burns dead-center
    while the adapter, seeing no snapshot, reads the candidate's whitespace-pocket
    fracs and draws the hook somewhere else entirely. Finalization hides this on
    the first render (it strips `text_placement_candidates`), but a re-render
    re-adds them and nothing strips them again.
    """
    placement = {key: params.get(key) for key in _INTRO_PLACEMENT_KEYS}
    if not placement["position"]:
        placement["position"] = "center"
    if not placement["text_anchor"]:
        placement["text_anchor"] = "center"
    if placement == _DEFAULT_INTRO_PLACEMENT and not has_candidates:
        return None
    return placement


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


# ── Speech-cut rerender publication ─────────────────────────────────────────────


_SPEECH_CUT_FINALIZER_CLAIM_TTL_S = 1810.0


def _speech_cut_claim_matches(control: dict[str, Any], operation_id: str, attempt_id: str) -> bool:
    claim = control.get("finalizer_claim") or {}
    return bool(
        control.get("operation_id") == operation_id
        and claim.get("operation_id") == operation_id
        and claim.get("attempt_id") == attempt_id
    )


def _claim_speech_cut_finalize(
    job_id: str,
    operation_id: str,
    attempt_id: str,
    *,
    task_id: str | None = None,
    retry_number: int = 0,
) -> bool:
    """Claim one operation attempt; fresh duplicate deliveries become no-ops.

    The lease is slightly longer than this task's hard time limit. A worker that
    is still alive cannot be stolen from, while a broker redelivery after a
    hard-killed worker can recover instead of stranding the request.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    now_s = time.time()
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return False
        plan = job.assembly_plan or {}
        control = dict(plan.get("speech_cut_control") or {})
        if control.get("operation_id") != operation_id:
            return False
        claim = control.get("finalizer_claim") or {}
        try:
            claim_age_s = now_s - float(claim.get("claimed_at_epoch_s"))
        except (TypeError, ValueError):
            claim_age_s = _SPEECH_CUT_FINALIZER_CLAIM_TTL_S
        try:
            claimed_retry_number = int(claim.get("retry_number") or 0)
        except (TypeError, ValueError):
            claimed_retry_number = 0
        same_task_newer_retry = bool(
            task_id and claim.get("task_id") == task_id and retry_number > claimed_retry_number
        )
        if (
            claim.get("attempt_id")
            and not same_task_newer_retry
            and claim_age_s < _SPEECH_CUT_FINALIZER_CLAIM_TTL_S
        ):
            return False
        control["finalizer_claim"] = {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "retry_number": retry_number,
            "claimed_at_epoch_s": now_s,
        }
        job.assembly_plan = {**plan, "speech_cut_control": control}
        flag_modified(job, "assembly_plan")
        db.commit()
        return True


def _assert_speech_cut_finalize_claim(job_id: str, operation_id: str, attempt_id: str) -> None:
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        control = ((job.assembly_plan or {}).get("speech_cut_control") or {}) if job else {}
    if not _speech_cut_claim_matches(control, operation_id, attempt_id):
        raise RuntimeError("speech cut operation was superseded")


def _release_speech_cut_finalize_claim(job_id: str, operation_id: str, attempt_id: str) -> bool:
    """Release only this attempt's claim so an OperationalError retry can run."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return False
        plan = job.assembly_plan or {}
        control = dict(plan.get("speech_cut_control") or {})
        if not _speech_cut_claim_matches(control, operation_id, attempt_id):
            return False
        control["finalizer_claim"] = None
        job.assembly_plan = {**plan, "speech_cut_control": control}
        flag_modified(job, "assembly_plan")
        db.commit()
        return True


def _removals_from_summary(summary: dict | None) -> list[Any]:
    from app.pipeline.silence_cut import Removal  # noqa: PLC0415

    out = []
    for raw in (summary or {}).get("removed") or []:
        try:
            out.append(
                Removal(
                    start_s=float(raw["start_s"]),
                    end_s=float(raw["end_s"]),
                    reason=str(raw.get("reason") or "speech_cut"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _merge_speech_cut_prior_state(
    job_id: str,
    result: dict[str, Any],
    *,
    expected_operation_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Carry creator-authored lanes through old-output → source → new-output."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return result
        plan = job.assembly_plan or {}
        control = plan.get("speech_cut_control") or {}
        prior = plan.get("speech_cut_previous_variant")
    if (
        expected_operation_id
        and expected_attempt_id
        and not _speech_cut_claim_matches(control, expected_operation_id, expected_attempt_id)
    ):
        raise RuntimeError("speech cut operation was superseded during render")
    if not isinstance(prior, dict) or control.get("variant_id") != result.get("variant_id"):
        return result

    from app.pipeline.speech_cut_state import reproject_variant_timing  # noqa: PLC0415

    projected = reproject_variant_timing(
        prior,
        old_removals=_removals_from_summary(prior.get("silence_cut")),
        new_removals=_removals_from_summary(result.get("silence_cut")),
    )
    merged = dict(result)
    # New speech/caption/Smart analysis stays authoritative. Creator-authored
    # timing lanes are projected exactly; appearance toggles are timing-free.
    for field in (
        "media_overlays",
        "sound_effects",
        "camera_effects",
        "boundary_effects",
        "motion_scenes",
        "visual_blocks",
    ):
        if isinstance(prior.get(field), list):
            merged[field] = projected.get(field) or []
    if prior.get("text_elements_user_edited"):
        merged["text_elements"] = projected.get("text_elements") or []
        merged["text_elements_user_edited"] = True
        merged["text_elements_materialized_from"] = prior.get("text_elements_materialized_from")
    for field in (
        "text_mode",
        "style_set_id",
        "intro_text",
        "intro_highlight_word",
        "intro_text_color",
        "intro_behind_subject",
        "intro_text_size_px",
        "intro_size_source",
        "intro_layout",
        "intro_word_roles",
        "intro_mode",
        "intro_placement",
        "intro_cluster_style",
        "intro_cluster_hero_font",
        "intro_cluster_body_font",
        "intro_cluster_accent_font",
        "intro_cluster_hero_size_px",
        "intro_cluster_body_size_px",
        "intro_cluster_accent_size_px",
        "sequence_base_size_px",
        "sequence_mode",
        "sequence_quote",
        "user_style_knobs",
        "captions_enabled",
        "voiceover_caption_style",
        "voiceover_caption_font",
        "caption_margin_v",
        "caption_size_px",
        "caption_text_color",
        "caption_highlight_color",
        "caption_stroke_width",
        "caption_shadow_enabled",
        "caption_font_user_edited",
        "caption_position_user_edited",
    ):
        if field in prior:
            merged[field] = prior.get(field)
    merged["speech_cut_candidates"] = prior.get("speech_cut_candidates") or result.get(
        "speech_cut_candidates"
    )
    merged["spine_clip_id"] = prior.get("spine_clip_id") or result.get("spine_clip_id")
    merged["speech_cut_forced_removals"] = list(control.get("forced_removals") or [])
    merged["speech_cuts_disabled"] = bool(control.get("desired_disabled"))
    merged["speech_cut_in_flight"] = control.get("in_flight")
    return merged


def _speech_cut_intervals(raw_items: Any) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            raise RuntimeError("speech cut receipt contained an invalid removal")
        try:
            start_s = float(raw["start_s"])
            end_s = float(raw["end_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("speech cut receipt contained an invalid removal") from exc
        if not (math.isfinite(start_s) and math.isfinite(end_s) and end_s > start_s):
            raise RuntimeError("speech cut receipt contained an invalid removal")
        intervals.append((start_s, end_s))
    return intervals


def _speech_cut_interval_is_covered(
    requested: tuple[float, float], actual: list[tuple[float, float]]
) -> bool:
    epsilon_s = 0.04  # slightly over one 30fps frame
    cursor = requested[0]
    for start_s, end_s in sorted(actual):
        if end_s < cursor - epsilon_s:
            continue
        if start_s > cursor + epsilon_s:
            return False
        cursor = max(cursor, end_s)
        if cursor >= requested[1] - epsilon_s:
            return True
    return False


def _validate_speech_cut_publication(control: dict[str, Any], variant: dict[str, Any]) -> None:
    """Prove the persisted render matches the server-owned requested cut."""
    actual = _speech_cut_intervals((variant.get("silence_cut") or {}).get("removed") or [])
    if control.get("desired_disabled") is True:
        if actual:
            raise RuntimeError("speech timing restore rendered residual cuts")
        return
    forced = _speech_cut_intervals(control.get("forced_removals") or [])
    if not forced or not all(_speech_cut_interval_is_covered(item, actual) for item in forced):
        raise RuntimeError("speech cut render did not apply every requested removal")


def _restore_failed_speech_cut_rerender(
    job_id: str,
    error: str,
    *,
    expected_operation_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> bool:
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return False
        plan = job.assembly_plan or {}
        prior = plan.get("speech_cut_previous_variant")
        control = plan.get("speech_cut_control") or {}
        if (
            expected_operation_id
            and expected_attempt_id
            and not _speech_cut_claim_matches(control, expected_operation_id, expected_attempt_id)
        ):
            return False
        if not isinstance(prior, dict):
            return False
        previous_variants = plan.get("speech_cut_previous_variants")
        variants = (
            list(previous_variants)
            if isinstance(previous_variants, list)
            else [
                prior if v.get("variant_id") == control.get("variant_id") else v
                for v in plan.get("variants") or []
            ]
        )
        failure = {
            "operation_id": control.get("operation_id"),
            "message": str(error)[:300],
        }
        variants = [
            {**variant, "speech_cut_last_error": failure}
            if variant.get("variant_id") == control.get("variant_id")
            else variant
            for variant in variants
        ]
        job.assembly_plan = {
            **plan,
            "silence_cut_disabled": bool(control.get("prior_disabled")),
            "speech_cut_control": None,
            "speech_cut_previous_variant": None,
            "speech_cut_previous_variants": None,
            "speech_cut_last_error": str(error)[:300],
            "variants": variants,
        }
        job.status = "variants_ready"
        flag_modified(job, "assembly_plan")
        db.commit()
        return True


def _publish_speech_cut_rerender(
    job_id: str, *, expected_operation_id: str, expected_attempt_id: str
) -> None:
    """Token-winning publication after the full render and lane composition."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    from app.pipeline.speech_cut_state import cut_revision  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        plan = job.assembly_plan or {}
        control = plan.get("speech_cut_control") or {}
        if not _speech_cut_claim_matches(control, expected_operation_id, expected_attempt_id):
            raise RuntimeError("speech cut publication was superseded")
        variant_id = control.get("variant_id")
        if not variant_id:
            raise RuntimeError("speech cut publication target disappeared")
        variants = list(plan.get("variants") or [])
        published = False
        for variant in variants:
            if variant.get("variant_id") != variant_id:
                continue
            _validate_speech_cut_publication(control, variant)
            operation = dict(control.get("operation") or {})
            if operation.get("operation") == "apply_speech_cut_candidate":
                for candidate in variant.get("speech_cut_candidates") or []:
                    if candidate.get("candidate_id") == operation.get("candidate_id"):
                        candidate["status"] = "accepted"
            variant["speech_cut_forced_removals"] = list(control.get("forced_removals") or [])
            variant["speech_cuts_disabled"] = bool(control.get("desired_disabled"))
            variant["speech_cut_in_flight"] = None
            operation["render_generation_id"] = variant.get("render_generation_id")
            operation["status"] = "applied"
            variant["speech_cut_revision"] = cut_revision(variant)
            operation["revision"] = variant["speech_cut_revision"]
            variant["speech_cut_last_receipt"] = operation
            variant["speech_cut_last_error"] = None
            published = True
            break
        if not published:
            raise RuntimeError("speech cut publication variant disappeared")
        job.assembly_plan = {
            **plan,
            "silence_cut_disabled": bool(control.get("desired_disabled")),
            "speech_cut_control": None,
            "speech_cut_previous_variant": None,
            "speech_cut_previous_variants": None,
            "speech_cut_last_error": None,
            "variants": variants,
        }
        flag_modified(job, "assembly_plan")
        db.commit()


def _compose_speech_cut_rerender(
    job_id: str, *, expected_operation_id: str, expected_attempt_id: str
) -> None:
    """Burn projected creator text/captions and reapply media before publish."""
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        control = (job.assembly_plan or {}).get("speech_cut_control") or {}
        if not _speech_cut_claim_matches(control, expected_operation_id, expected_attempt_id):
            raise RuntimeError("speech cut composition was superseded")
        variant_id = control.get("variant_id")
        variant = next(
            (
                v
                for v in (job.assembly_plan or {}).get("variants") or []
                if v.get("variant_id") == variant_id
            ),
            None,
        )
    if not isinstance(variant, dict):
        raise RuntimeError("speech cut target variant disappeared")
    render_gen_id = uuid.uuid4().hex
    if not _update_variant_entry(
        job_id,
        str(variant_id),
        {"render_generation_id": render_gen_id, "render_status": "rendering"},
        outcome="speech_cut_recompose_start",
    ):
        raise RuntimeError("speech cut recomposition was superseded")

    archetype = variant.get("resolved_archetype")
    if archetype == "subtitled":
        terminal = {"accepted": False}
        _run_reburn_narrated_captions(
            job_id,
            str(variant_id),
            render_gen_id=render_gen_id,
            terminal_state=terminal,
        )
        if not terminal["accepted"]:
            raise RuntimeError("speech cut caption recomposition did not publish")
        _assert_speech_cut_finalize_claim(job_id, expected_operation_id, expected_attempt_id)
        return
    if archetype != "talking_head":
        raise RuntimeError(f"unsupported speech cut archetype: {archetype}")

    patch = _reburn_text_on_base(
        job_id=job_id,
        variant_id=str(variant_id),
        existing=variant,
        agent_text=variant.get("intro_text"),
        agent_form=None,
        text_mode=str(variant.get("text_mode") or "none"),
        resolved_style_set_id=variant.get("style_set_id"),
        size_override_px=None,
        settings=settings,
        language="en",
        storage_generation=render_gen_id,
    )
    patch.pop("_old_video_path_for_delete", None)
    will_reapply = _will_reapply_media_layers({**variant, **patch})
    patch["render_status"] = "rendering" if will_reapply else "ready"
    if not _update_variant_entry(
        job_id,
        str(variant_id),
        patch,
        expected_render_gen_id=render_gen_id,
        outcome="speech_cut_recompose_talking_head",
    ):
        raise RuntimeError("speech cut text recomposition was superseded")
    if will_reapply and not _reapply_user_media_layers(
        job_id=job_id,
        variant_id=str(variant_id),
        expected_render_gen_id=render_gen_id,
    ):
        raise RuntimeError("speech cut media reapply did not publish")
    _assert_speech_cut_finalize_claim(job_id, expected_operation_id, expected_attempt_id)


# ── Status helpers ──────────────────────────────────────────────────────────────


def _finalize_job(
    job_id: str,
    results: list[dict[str, Any]],
    *,
    expected_operation_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> None:
    successes = [r for r in results if r.get("ok")]
    failures = [r for r in results if not r.get("ok")]
    if successes and failures:
        terminal = "variants_ready_partial"
    elif successes:
        terminal = "variants_ready"
    else:
        terminal = "variants_failed"
    speech_cut_status_kwargs = (
        {
            "expected_speech_cut_operation_id": expected_operation_id,
            "expected_speech_cut_attempt_id": expected_attempt_id,
        }
        if expected_operation_id and expected_attempt_id
        else {}
    )

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
                    "intro_text_color": r.get("intro_text_color"),
                    "intro_behind_subject": r.get("intro_behind_subject"),
                    "base_video_path": r.get("base_video_path"),
                    # Visual replacement blocks render below text/captions. The
                    # original clean base remains immutable; the derived cache
                    # is reused by text-only reburns.
                    "visual_blocks": r.get("visual_blocks"),
                    "visual_blocks_base_path": r.get("visual_blocks_base_path"),
                    "visual_blocks_cache_stale": r.get("visual_blocks_cache_stale", False),
                    "visual_blocks_autoplan_attempted": r.get("visual_blocks_autoplan_attempted"),
                    "motion_scenes": r.get("motion_scenes"),
                    "motion_runtime_hash": r.get("motion_runtime_hash"),
                    "motion_base_path": r.get("motion_base_path"),
                    "motion_base_source_path": r.get("motion_base_source_path"),
                    "motion_cache_stale": r.get("motion_cache_stale", False),
                    "motion_applied_runtime_hash": r.get("motion_applied_runtime_hash"),
                    "motion_cache_identity": r.get("motion_cache_identity"),
                    # media-overlay cards (slice 1) — MUST survive finalization
                    # or "clear all" loses the pre-overlay clean copy reference.
                    "media_overlays": r.get("media_overlays"),
                    "media_overlays_applied_ids": r.get("media_overlays_applied_ids"),
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
                    # of the chosen y position (set by the creator's position edit OR
                    # by face-aware placement on the first render).
                    "caption_margin_v": r.get("caption_margin_v"),
                    # caption appearance overrides — MUST survive or caption style
                    # edits appear to save once, then vanish after the next full render.
                    "caption_size_px": r.get("caption_size_px"),
                    "caption_text_color": r.get("caption_text_color"),
                    "caption_highlight_color": r.get("caption_highlight_color"),
                    "caption_stroke_width": r.get("caption_stroke_width"),
                    "caption_shadow_enabled": r.get("caption_shadow_enabled"),
                    "caption_font_user_edited": r.get("caption_font_user_edited"),
                    "caption_position_user_edited": r.get("caption_position_user_edited"),
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
                    "smart_shadow_comparison": r.get("smart_shadow_comparison"),
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
                    # RESOLVED intro placement — MUST survive finalization or the
                    # editor re-guesses "center" and draws an off-center hook at
                    # mid-frame on the FIRST render (the exact bug the snapshot
                    # exists to fix). Guard: test_finalize_job_preserves_intro_placement.
                    "intro_placement": r.get("intro_placement"),
                    # Which style profile the static intro was BURNED with, plus
                    # the per-role pins that patched it. MUST survive or the read
                    # adapter rebuilds the LEGACY cluster for an editorially-
                    # rendered variant — wrong block count, sizes, faces and y
                    # positions in the editor, on every FIRST render. Pinned by
                    # test_finalize_job_preserves_cluster_style.
                    "intro_cluster_style": r.get("intro_cluster_style"),
                    "intro_cluster_hero_font": r.get("intro_cluster_hero_font"),
                    "intro_cluster_body_font": r.get("intro_cluster_body_font"),
                    "intro_cluster_accent_font": r.get("intro_cluster_accent_font"),
                    "intro_cluster_hero_size_px": r.get("intro_cluster_hero_size_px"),
                    "intro_cluster_body_size_px": r.get("intro_cluster_body_size_px"),
                    "intro_cluster_accent_size_px": r.get("intro_cluster_accent_size_px"),
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
                    "spine_clip_id": r.get("spine_clip_id"),
                    "speech_cut_candidates": r.get("speech_cut_candidates"),
                    "speech_cut_forced_removals": r.get("speech_cut_forced_removals"),
                    "speech_cuts_disabled": r.get("speech_cuts_disabled", False),
                    "speech_cut_in_flight": r.get("speech_cut_in_flight"),
                    "speech_cut_last_receipt": r.get("speech_cut_last_receipt"),
                    "speech_cut_last_error": r.get("speech_cut_last_error"),
                }
                for r in results
            ],
        },
        **speech_cut_status_kwargs,
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


def _set_status(
    job_id: str,
    status: str,
    extra_plan: dict[str, Any] | None = None,
    *,
    expected_speech_cut_operation_id: str | None = None,
    expected_speech_cut_attempt_id: str | None = None,
) -> None:
    # Row-locked RMW (mirrors _upsert_variant_entry / _update_variant_entry).
    # `extra_plan` merges into assembly_plan — _finalize_job writes the WHOLE
    # variants list here. Sibling regenerate/reapply tasks and the status route's
    # lazy overlay-preview backfill read-modify-write the same JSONB concurrently,
    # so without SELECT ... FOR UPDATE a stale read silently clobbers their state.
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        if expected_speech_cut_operation_id and expected_speech_cut_attempt_id:
            control = (job.assembly_plan or {}).get("speech_cut_control") or {}
            if not _speech_cut_claim_matches(
                control,
                expected_speech_cut_operation_id,
                expected_speech_cut_attempt_id,
            ):
                raise RuntimeError("speech cut finalization was superseded")
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
