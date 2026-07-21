"""Celery tasks for overlay auto-placement (plans/005 PR1a/PR1b).

Two light LLM tasks — NO ffmpeg here (renders stay on overlay-jobs):

  analyze_pool_asset(asset_id)
      upload-time analysis of one pool asset. Images → ImageMetadataAgent
      (+ PIL size for aspect); video → probe_video (server-side duration/aspect,
      never client-trusted) + best-effort clip analysis. Keyless machines get a
      filename-derived stub analysis so the flow still completes (finding 10).

  match_overlay_suggestions(job_id, variant_id, user_id)
      the matcher. transcript_source (Whisper fallback, persisted run-once) →
      OverlayPlacementAgent (or the deterministic heuristic when Gemini is
      unavailable/fails) → build_suggestions validates against FRESH occupied
      intervals under the same row lock that persists (finding 8).

Both: soft_time_limit=240 / time_limit=300 (< broker visibility_timeout=1900,
worker.py invariant) and wrapped in pipeline_trace_for (mandatory orchestrator
contract — agent I/O must reach /admin/jobs).

Queue: `settings.autoplace_queue` (default "celery"; local dev sets a dedicated
queue so sibling worktree workers on the shared redis never grab unregistered tasks).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import structlog

from app.database import sync_session as _sync_session
from app.models import Job, PlanItemAsset, SoundEffect
from app.worker import celery_app

log = structlog.get_logger()

_AUTOPLACE_TASK_LIMITS = {"soft_time_limit": 240, "time_limit": 300}


def _record(event: str, **fields) -> None:
    try:
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        # record_pipeline_event signature is (stage, event, data=None) — pass the
        # autoplace stage + the fields dict, NOT **fields (which bound the event
        # name to `stage` and raised TypeError that the bare except swallowed,
        # silently dropping every trace this feature's failure design depends on).
        record_pipeline_event("autoplace", event, fields)
    except Exception:  # noqa: BLE001 — tracing must never break the task
        pass


def _materialize_extracted_frames(job_id: str, *, minimum_ready: int = 3) -> None:
    """Persist source-footage frames in the normal replaceable asset pool."""
    from sqlalchemy import func, select  # noqa: PLC0415

    from app import storage  # noqa: PLC0415

    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.content_plan_item_id is None:
            return
        ready = (
            db.execute(
                select(PlanItemAsset).where(
                    PlanItemAsset.plan_item_id == job.content_plan_item_id,
                    PlanItemAsset.status == "ready",
                )
            )
            .scalars()
            .all()
        )
        if len(ready) >= minimum_ready:
            return
        total = int(
            db.execute(
                select(func.count())
                .select_from(PlanItemAsset)
                .where(PlanItemAsset.plan_item_id == job.content_plan_item_id)
            ).scalar_one()
        )
        needed = min(minimum_ready - len(ready), max(0, 20 - total))
        if needed <= 0:
            return
        existing_sources = {
            (
                str((asset.analysis or {}).get("source_clip_gcs_path") or ""),
                round(float((asset.analysis or {}).get("source_timestamp_s") or 0.0), 2),
            )
            for asset in ready
            if (asset.analysis or {}).get("source") == "extracted_frame"
        }
        source_paths = list((job.all_candidates or {}).get("clip_paths") or [])
        user_id = job.user_id
        item_id = job.content_plan_item_id

    if not source_paths:
        return
    created: list[PlanItemAsset] = []
    with tempfile.TemporaryDirectory(prefix="nova_extracted_frames_") as tmpdir:
        for clip_index, source_gcs_path in enumerate(source_paths):
            if len(created) >= needed:
                break
            local_video = os.path.join(tmpdir, f"source_{clip_index}.mp4")
            try:
                storage.download_to_file(source_gcs_path, local_video)
                from app.pipeline.probe import probe_video  # noqa: PLC0415

                duration_s = max(0.1, float(probe_video(local_video).duration_s))
            except Exception as exc:  # noqa: BLE001 - source extraction is best effort
                log.warning(
                    "visual_blocks.frame_source_failed",
                    job_id=job_id,
                    source=source_gcs_path,
                    error=str(exc)[:180],
                )
                continue
            for fraction in (0.18, 0.5, 0.82):
                if len(created) >= needed:
                    break
                timestamp_s = round(min(max(0.0, duration_s - 0.05), duration_s * fraction), 3)
                provenance = (source_gcs_path, round(timestamp_s, 2))
                if provenance in existing_sources:
                    continue
                frame_path = os.path.join(tmpdir, f"frame_{clip_index}_{fraction}.jpg")
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-ss",
                        f"{timestamp_s:.3f}",
                        "-i",
                        local_video,
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        "-y",
                        frame_path,
                    ],
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
                if result.returncode != 0 or not os.path.exists(frame_path):
                    continue
                frame_bytes = Path(frame_path).read_bytes()
                digest = hashlib.sha256(frame_bytes).hexdigest()
                object_path = (
                    f"users/{user_id}/plan/{item_id}/pool/"
                    f"extracted_{clip_index}_{int(timestamp_s * 1000)}_{digest[:10]}.jpg"
                )
                try:
                    storage.upload_public_read(frame_path, object_path, content_type="image/jpeg")
                    from PIL import Image  # noqa: PLC0415

                    with Image.open(frame_path) as image:
                        width, height = image.size
                    created.append(
                        PlanItemAsset(
                            plan_item_id=item_id,
                            user_id=user_id,
                            gcs_path=object_path,
                            kind="image",
                            content_hash=digest,
                            source_filename=f"Extracted frame · clip {clip_index + 1}",
                            aspect=round(width / height, 4) if height else None,
                            analysis={
                                "subject": "Extracted frame",
                                "description": (
                                    f"Frame from source clip {clip_index + 1} at {timestamp_s:.2f}s"
                                ),
                                "on_screen_text": "",
                                "brands": [],
                                "source": "extracted_frame",
                                "analysis_version": ANALYSIS_VERSION,
                                "source_clip_gcs_path": source_gcs_path,
                                "source_clip_index": clip_index,
                                "source_timestamp_s": timestamp_s,
                                "width": width,
                                "height": height,
                                "has_alpha": False,
                            },
                            status="ready",
                        )
                    )
                    existing_sources.add(provenance)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "visual_blocks.frame_materialize_failed",
                        job_id=job_id,
                        error=str(exc)[:180],
                    )
    if created:
        with _sync_session() as db:
            db.add_all(created)
            db.commit()
        _record("visual_blocks_frames_extracted", count=len(created))


def _clear_visual_block_attempts(job_id: str, variant_ids: list[str]) -> None:
    """Release run-once claims that never reached a terminal planner result."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    wanted = set(variant_ids)
    with _sync_session() as db:
        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
        if job is None:
            return
        variants = list((job.assembly_plan or {}).get("variants") or [])
        changed = False
        for variant in variants:
            if (
                variant.get("variant_id") in wanted
                and not variant.get("visual_blocks")
                and variant.get("visual_blocks_autoplan_attempted")
            ):
                variant["visual_blocks_autoplan_attempted"] = False
                changed = True
        if changed:
            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            flag_modified(job, "assembly_plan")
            db.commit()


def _planner_revision_matches(target: dict, revision: dict) -> bool:
    from app.services.transcript_source import words_from_variant  # noqa: PLC0415

    fresh_words = words_from_variant(target)
    if fresh_words is None and (target.get("transcript") or target.get("overlay_transcript")):
        return False
    return (
        target.get("base_video_path") == revision["base_video_path"]
        and target.get("duration_s") == revision["duration_s"]
        and target.get("output_duration_s") == revision["output_duration_s"]
        and target.get("render_generation_id") == revision["render_generation_id"]
        and (fresh_words or []) == revision["words"]
    )


@celery_app.task(name="app.tasks.autoplace.prepare_visual_block_assets", **_AUTOPLACE_TASK_LIMITS)
def prepare_visual_block_assets(job_id: str, variant_ids: list[str]) -> None:
    """Extract missing source frames once, then fan out variant planners."""
    from app.config import settings  # noqa: PLC0415

    if not (settings.visual_blocks_enabled and settings.visual_block_autoplan_enabled):
        _clear_visual_block_attempts(job_id, variant_ids)
        return
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        try:
            _materialize_extracted_frames(job_id)
        except Exception:
            _clear_visual_block_attempts(job_id, variant_ids)
            raise
        for index, variant_id in enumerate(variant_ids):
            try:
                plan_visual_blocks.apply_async(
                    args=[job_id, variant_id], queue=settings.autoplace_queue
                )
            except Exception:
                _clear_visual_block_attempts(job_id, variant_ids[index:])
                raise


@celery_app.task(name="app.tasks.autoplace.plan_visual_blocks", **_AUTOPLACE_TASK_LIMITS)
def plan_visual_blocks(job_id: str, variant_id: str) -> None:
    """Plan, persist, and render first-edit visual blocks for one variant."""
    from app.config import settings  # noqa: PLC0415

    if not (settings.visual_blocks_enabled and settings.visual_block_autoplan_enabled):
        _clear_visual_block_attempts(job_id, [variant_id])
        return

    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415

    with pipeline_trace_for(job_id):
        from sqlalchemy import select  # noqa: PLC0415

        from app.services.transcript_source import (  # noqa: PLC0415
            transcript_source,
            words_from_variant,
        )

        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None or job.content_plan_item_id is None:
                return
            variant = next(
                (
                    row
                    for row in (job.assembly_plan or {}).get("variants") or []
                    if row.get("variant_id") == variant_id
                ),
                None,
            )
            if (
                variant is None
                or variant.get("text_mode") == "lyrics"
                or not variant.get("base_video_path")
                or variant.get("visual_blocks")
            ):
                return
            assets = [
                {
                    "id": str(asset.id),
                    "gcs_path": asset.gcs_path,
                    "kind": asset.kind,
                    "analysis": asset.analysis or {},
                }
                for asset in db.execute(
                    select(PlanItemAsset).where(
                        PlanItemAsset.plan_item_id == job.content_plan_item_id,
                        PlanItemAsset.status == "ready",
                    )
                )
                .scalars()
                .all()
            ]
            duration_s = float(variant.get("duration_s") or variant.get("output_duration_s") or 0)
            persisted_words = words_from_variant(variant)
            if persisted_words is None and (
                variant.get("transcript") or variant.get("overlay_transcript")
            ):
                # An authoritative but malformed transcript must not fall
                # through to Whisper forever. Fail closed and keep the run-once
                # marker terminal until the source data is corrected.
                _record("visual_blocks_transcript_invalid", variant_id=variant_id)
                return
            had_persisted_words = persisted_words is not None
            source_revision = {
                "base_video_path": variant.get("base_video_path"),
                "duration_s": variant.get("duration_s"),
                "output_duration_s": variant.get("output_duration_s"),
                "render_generation_id": variant.get("render_generation_id"),
            }
            transcript = transcript_source(variant, allow_whisper=True)
            words = transcript[0] if transcript else []
            beats = list(variant.get("beat_grid") or variant.get("beat_timestamps_s") or [])
            timeline = variant.get("user_timeline") or variant.get("ai_timeline") or {}
            clip_plan = list(timeline.get("slots") or variant.get("assembly_steps") or [])
            visual_signals = {
                "resolved_archetype": variant.get("resolved_archetype"),
                "montage_preset": variant.get("montage_preset_rendered"),
                "shot_quality": variant.get("shot_quality"),
                "repetition_signals": variant.get("repetition_signals"),
                "speech_pace": variant.get("speech_pace"),
                "music_energy": variant.get("music_energy"),
            }

        from app.agents.visual_treatment_planner import (  # noqa: PLC0415
            RawVisualTreatment,
            VisualTreatmentAsset,
            VisualTreatmentPlannerAgent,
            VisualTreatmentPlannerInput,
        )

        proposals: list[RawVisualTreatment] = []
        planner_outcome = "skipped"
        if words:
            # Reconcile the transcript under the row lock before calling the
            # model. Never overwrite a correction or a transcript produced by
            # a concurrent task after this planner's unlocked Whisper read.
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
                variants = list((job.assembly_plan or {}).get("variants") or []) if job else []
                fresh = next((row for row in variants if row.get("variant_id") == variant_id), None)
                source_changed = fresh is None or any(
                    fresh.get(key) != value for key, value in source_revision.items()
                )
                if source_changed:
                    if fresh is not None:
                        fresh["visual_blocks_autoplan_attempted"] = False
                        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                        flag_modified(job, "assembly_plan")
                        db.commit()
                    _record("visual_blocks_plan_stale", variant_id=variant_id)
                    return
                fresh_words = words_from_variant(fresh)
                if fresh_words is not None:
                    words = fresh_words
                elif had_persisted_words:
                    fresh["visual_blocks_autoplan_attempted"] = False
                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                    flag_modified(job, "assembly_plan")
                    db.commit()
                    _record("visual_blocks_plan_stale", variant_id=variant_id)
                    return
                else:
                    fresh["overlay_transcript"] = words
                    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                    flag_modified(job, "assembly_plan")
                    db.commit()
        if duration_s <= 0:
            duration_s = float(words[-1].get("end_s", 0.0)) + 1.0 if words else 60.0
        planning_revision = {**source_revision, "words": words}
        if settings.gemini_api_key and (words or beats):
            try:
                from app.agents._model_client import default_client  # noqa: PLC0415
                from app.agents._runtime import RunContext  # noqa: PLC0415

                output = VisualTreatmentPlannerAgent(default_client()).run(
                    VisualTreatmentPlannerInput(
                        words=words,
                        assets=[
                            VisualTreatmentAsset(
                                asset_id=asset["id"],
                                kind=asset["kind"],
                                subject=str(asset["analysis"].get("subject", "")),
                                description=str(asset["analysis"].get("description", "")),
                                on_screen_text=str(asset["analysis"].get("on_screen_text", "")),
                            )
                            for asset in assets
                        ],
                        duration_s=duration_s,
                        beat_timestamps_s=beats,
                        clip_plan=clip_plan,
                        visual_signals=visual_signals,
                        recent_treatments=list(variant.get("visual_treatment_history") or []),
                    ),
                    ctx=RunContext(job_id=job_id),
                )
                proposals = output.treatments
                planner_outcome = "succeeded"
            except Exception as exc:  # noqa: BLE001 - deterministic fallback below
                planner_outcome = "failed"
                log.warning(
                    "visual_blocks.planner_failed",
                    job_id=job_id,
                    variant_id=variant_id,
                    error=str(exc)[:240],
                )
                _record(
                    "visual_blocks_planner_failed",
                    variant_id=variant_id,
                    error=str(exc)[:160],
                )

        # Keyless/agent-failure fallback is deliberately conservative: a short
        # opening montage is grounded by real assets; semantic cards require the
        # transcript-aware agent and are never hallucinated heuristically.
        if (
            planner_outcome != "succeeded"
            and not proposals
            and len(assets) >= 3
            and duration_s >= 1.2
        ):
            proposals = [
                RawVisualTreatment(
                    kind="montage",
                    purpose="hook",
                    start_s=0.0,
                    end_s=min(3.0, duration_s),
                    asset_ids=[asset["id"] for asset in assets[:5]],
                    rationale="Rapid opening context montage from the creator's asset pool.",
                    confidence="medium",
                )
            ]

        from app.agents._schemas.visual_block import (  # noqa: PLC0415
            validate_visual_block_text_links,
            validate_visual_blocks,
        )
        from app.services.visual_treatment_planner import (  # noqa: PLC0415
            build_visual_treatments,
        )

        blocks, new_text = build_visual_treatments(
            proposals,
            assets_by_id={asset["id"]: asset for asset in assets},
            duration_s=duration_s,
            transcript_text=" ".join(
                str(word.get("word") or word.get("text") or "")
                if isinstance(word, dict)
                else str(word)
                for word in words
            ),
            transcript_words=words,
        )
        # Zero and failure outcomes are still tied to the source revision the
        # model saw. Validate before consuming the newer render's run-once
        # claim as a terminal no-op.
        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            variants = list((job.assembly_plan or {}).get("variants") or []) if job else []
            target = next((row for row in variants if row.get("variant_id") == variant_id), None)
            if target is None:
                return
            if not _planner_revision_matches(target, planning_revision):
                target["visual_blocks_autoplan_attempted"] = False
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                flag_modified(job, "assembly_plan")
                db.commit()
                _record("visual_blocks_plan_stale", variant_id=variant_id)
                return
        if not blocks:
            if planner_outcome == "failed":
                _clear_visual_block_attempts(job_id, [variant_id])
            else:
                _record("visual_blocks_plan_zero", variant_id=variant_id)
            return
        blocks = validate_visual_blocks(blocks, duration_s=duration_s)

        with _sync_session() as db:
            job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
            if job is None:
                return
            variants = list((job.assembly_plan or {}).get("variants") or [])
            target = next((row for row in variants if row.get("variant_id") == variant_id), None)
            if target is None or target.get("visual_blocks"):
                return
            if not _planner_revision_matches(target, planning_revision):
                target["visual_blocks_autoplan_attempted"] = False
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                flag_modified(job, "assembly_plan")
                db.commit()
                _record("visual_blocks_plan_stale", variant_id=variant_id)
                return
            previous_render_status = target.get("render_status")
            previous_render_generation_id = target.get("render_generation_id")
            previous_render_generation_present = "render_generation_id" in target
            previous_text_elements = list(target.get("text_elements") or [])
            previous_text_elements_present = "text_elements" in target
            previous_text_edited = target.get("text_elements_user_edited")
            previous_text_edited_present = "text_elements_user_edited" in target
            elements = list(target.get("text_elements") or []) + new_text
            validate_visual_block_text_links(blocks, elements)
            render_gen_id = uuid.uuid4().hex
            target["visual_blocks"] = blocks
            target["visual_blocks_base_path"] = None
            if new_text:
                target["text_elements"] = elements
                target["text_elements_user_edited"] = True
            target["render_generation_id"] = render_gen_id
            target["render_status"] = "rendering"
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

            job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
            flag_modified(job, "assembly_plan")
            db.commit()

        from app.tasks.generative_build import regenerate_generative_variant  # noqa: PLC0415

        try:
            regenerate_generative_variant.apply_async(
                args=[job_id, variant_id],
                kwargs={"render_gen_id": render_gen_id},
                queue="overlay-jobs",
            )
        except Exception as exc:
            # The DB commit must precede enqueue so the worker can see it. If
            # the broker rejects that enqueue, roll back only this generation's
            # authored state so the last good video stays ready and autoplan can
            # retry instead of stranding the variant in `rendering` forever.
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
                if job is not None:
                    variants = list((job.assembly_plan or {}).get("variants") or [])
                    target = next(
                        (row for row in variants if row.get("variant_id") == variant_id),
                        None,
                    )
                    if target is not None and target.get("render_generation_id") == render_gen_id:
                        target.pop("visual_blocks", None)
                        target.pop("visual_blocks_base_path", None)
                        target["visual_blocks_autoplan_attempted"] = False
                        if previous_text_elements_present:
                            target["text_elements"] = previous_text_elements
                        else:
                            target.pop("text_elements", None)
                        if previous_text_edited_present:
                            target["text_elements_user_edited"] = previous_text_edited
                        else:
                            target.pop("text_elements_user_edited", None)
                        if previous_render_generation_present:
                            target["render_generation_id"] = previous_render_generation_id
                        else:
                            target.pop("render_generation_id", None)
                        target["render_status"] = previous_render_status or "ready"
                        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                        job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                        flag_modified(job, "assembly_plan")
                        db.commit()
            _record(
                "visual_blocks_render_dispatch_failed",
                variant_id=variant_id,
                error=str(exc)[:160],
            )
            raise
        _record(
            "visual_blocks_planned",
            variant_id=variant_id,
            blocks=len(blocks),
            linked_text=len(new_text),
        )


# ── asset analysis ────────────────────────────────────────────────────────────


# Bump when the analysis payload gains fields the matcher depends on. The
# matcher backfills REAL analyses older than this (plan 006, decision C);
# stubs carry the current version too, so they NEVER trigger the backfill loop.
# v3 (plan 009 E1): pixel width/height persisted into the analysis JSONB —
# rule (g)'s fail-closed resolution gate and the FE low-res warning both need
# real dims, and the self-healing backfill now covers IMAGE assets too.
# v4 (G1): image alpha presence persisted for UI/debug visibility.
# v5 (TikTok E2E follow-up): image + video analyses persist brand/mascot
# identities in `brands`; v3/v4 real analyses are stale for both kinds so the
# matcher can suppress already-satisfied brand wishlist rows.
ANALYSIS_VERSION = 5


def _stub_analysis(asset: PlanItemAsset) -> dict:
    """Keyless fallback: a filename-derived subject so the heuristic matcher
    still has tokens to work with (finding 10 — flow completes without keys)."""
    stem = (asset.source_filename or "").rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
    return {
        "subject": stem.strip()[:80],
        "description": "",
        "on_screen_text": "",
        "kind_hint": "other",
        "brands": [],
        "source": "stub",
        "analysis_version": ANALYSIS_VERSION,
    }


def analysis_is_stale(analysis: dict | None, *, kind: str | None = None) -> bool:
    """True for pre-006 REAL analyses (no best_moments persisted). Stubs are
    never stale — re-analyzing them on a keyless machine yields another stub
    (the infinite-loop class the outside voice flagged, plan 006 finding 2)."""
    a = analysis or {}
    if a.get("source") == "stub":
        return False
    try:
        version = int(a.get("analysis_version") or 1)
    except (TypeError, ValueError):
        return True
    if version >= ANALYSIS_VERSION:
        return False
    return True


def _analyze_image(
    local_path: str, job_scope: str
) -> tuple[dict | None, float | None, tuple[int, int] | None, bool]:
    """(analysis, aspect, (width, height), has_alpha) for a still image."""
    aspect: float | None = None
    dims: tuple[int, int] | None = None
    has_alpha = False
    try:
        from PIL import Image  # noqa: PLC0415

        from app.pipeline.image_clip import image_has_alpha  # noqa: PLC0415

        with Image.open(local_path) as im:
            if im.height:
                aspect = round(im.width / im.height, 4)
                dims = (int(im.width), int(im.height))
        has_alpha = image_has_alpha(local_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace.image_size_failed", error=str(exc)[:160])

    from app.config import settings  # noqa: PLC0415

    if not settings.gemini_api_key:
        return None, aspect, dims, has_alpha
    try:
        # INLINE bytes, not the Gemini File API: still images are small, and the
        # File API's processing step intermittently 500s on PNGs (observed
        # locally: ~170s of retries before falling back to the stub). Inline
        # parts skip processing entirely — one fast generate_content call.
        import json as _json  # noqa: PLC0415

        from google.genai import types as genai_types  # noqa: PLC0415

        from app.agents.image_metadata import ImageMetadataOutput  # noqa: PLC0415
        from app.pipeline.agents.gemini_analyzer import _get_client  # noqa: PLC0415
        from app.pipeline.prompt_loader import load_prompt  # noqa: PLC0415

        mime = "image/png"
        lower = local_path.lower()
        if lower.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif lower.endswith(".webp"):
            mime = "image/webp"
        elif lower.endswith(".heic"):
            mime = "image/heic"
        with open(local_path, "rb") as fh:
            data = fh.read()
        resp = _get_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                genai_types.Part.from_bytes(data=data, mime_type=mime),
                load_prompt("image_metadata"),
            ],
            config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = _json.loads(resp.text or "{}")
        out = ImageMetadataOutput.model_validate(parsed)
        _record("image_metadata_analyzed", scope=job_scope, subject=out.subject[:60])
        return (
            {
                **out.model_dump(),
                "source": "image_metadata",
                "analysis_version": ANALYSIS_VERSION,
                "has_alpha": has_alpha,
            },
            aspect,
            dims,
            has_alpha,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace.image_analysis_failed", error=str(exc)[:200])
        return None, aspect, dims, has_alpha


def _analyze_video(
    local_path: str, job_scope: str
) -> tuple[dict | None, float | None, float | None, tuple[int, int] | None]:
    """(analysis, aspect, duration_s, (width, height)) for a video asset."""
    aspect: float | None = None
    duration: float | None = None
    dims: tuple[int, int] | None = None
    try:
        from app.pipeline.probe import probe_video  # noqa: PLC0415

        probe = probe_video(local_path)
        duration = float(probe.duration_s or 0) or None
        if probe.height:
            aspect = round(probe.width / probe.height, 4)
            dims = (int(probe.width), int(probe.height))
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace.video_probe_failed", error=str(exc)[:160])

    from app.config import settings  # noqa: PLC0415

    if not settings.gemini_api_key:
        return None, aspect, duration, dims
    try:
        from app.pipeline.agents.gemini_analyzer import (  # noqa: PLC0415
            analyze_clip,
            gemini_upload_and_wait,
        )

        file_ref = gemini_upload_and_wait(local_path)
        meta = analyze_clip(file_ref, job_id=job_scope)
        if getattr(meta, "failed", False):
            return None, aspect, duration, dims
        # Persist the content map the trim rule needs (plan 006 §1): every
        # best_moment, clamped later at USE time (pick_trim_window) against the
        # PROBED duration — Gemini timing is never trusted raw.
        best_moments = []
        for m in getattr(meta, "best_moments", None) or []:
            try:
                best_moments.append(
                    {
                        "start_s": round(float(getattr(m, "start_s", 0.0)), 3),
                        "end_s": round(float(getattr(m, "end_s", 0.0)), 3),
                        "energy": float(getattr(m, "energy", 0.0)),
                        "description": str(getattr(m, "description", "") or "")[:160],
                    }
                )
            except (TypeError, ValueError):
                continue
        analysis = {
            "subject": str(getattr(meta, "detected_subject", "") or "")[:200],
            "description": str(getattr(meta, "description", "") or "")[:400],
            "on_screen_text": str(getattr(meta, "transcript", "") or "")[:400],
            "kind_hint": "screenshot",
            "source": "clip_metadata",
            "best_moments": best_moments,
            "brands": list(getattr(meta, "brands", None) or [])[:10],
            "duration_s": duration,
            "analysis_version": ANALYSIS_VERSION,
        }
        return analysis, aspect, duration, dims
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace.video_analysis_failed", error=str(exc)[:200])
        return None, aspect, duration, dims


@celery_app.task(name="app.tasks.autoplace.analyze_pool_asset", **_AUTOPLACE_TASK_LIMITS)
def analyze_pool_asset(asset_id: str, refresh: bool = False) -> None:
    """Analyze one pool asset.

    `refresh=True` (006 decision C backfill): the asset stays `status="ready"`
    the whole time — it never leaves the matcher pool and the rail button never
    flickers off; only the analysis payload is swapped on success. A failed
    refresh keeps the previous working analysis (never degrades a ready asset).
    """
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415
    from app.storage import download_to_file  # noqa: PLC0415

    with _sync_session() as db:
        asset = db.get(PlanItemAsset, uuid.UUID(asset_id))
        if asset is None:
            return
        scope = str(asset.plan_item_id)
        if not refresh:
            asset.status = "analyzing"
            db.commit()

    with pipeline_trace_for(scope):
        analysis: dict | None = None
        aspect: float | None = None
        duration: float | None = None
        dims: tuple[int, int] | None = None
        has_alpha: bool | None = None
        failed = False
        try:
            with _sync_session() as db:
                asset = db.get(PlanItemAsset, uuid.UUID(asset_id))
                if asset is None:
                    return
                gcs_path, kind = asset.gcs_path, asset.kind
                filename = asset.source_filename or "asset"
            with tempfile.TemporaryDirectory() as tmpdir:
                local = os.path.join(tmpdir, filename.split("/")[-1] or "asset")
                download_to_file(gcs_path, local)
                if kind == "video":
                    analysis, aspect, duration, dims = _analyze_video(local, scope)
                else:
                    analysis, aspect, dims, has_alpha = _analyze_image(local, scope)
        except Exception as exc:  # noqa: BLE001
            log.warning("autoplace.analysis_failed", asset_id=asset_id, error=str(exc)[:200])
            failed = True

        with _sync_session() as db:
            asset = db.get(PlanItemAsset, uuid.UUID(asset_id), with_for_update=True)
            if asset is None:
                return
            if failed and analysis is None and aspect is None and duration is None:
                if refresh:
                    # Refresh must never degrade a working asset (decision C):
                    # keep the previous analysis + ready status, just trace.
                    pass
                else:
                    # Couldn't even read the file — the honest "failed" tile (2A).
                    asset.status = "failed"
            else:
                if refresh and analysis is None:
                    # Refresh produced no better data (keyless / Gemini down):
                    # keep the existing payload rather than downgrading to a stub.
                    pass
                else:
                    final = analysis or _stub_analysis(asset)
                    # Plan 009 E1: pixel dims ride the analysis JSONB (no
                    # migration) — rule (g) + the FE low-res warning read them.
                    if dims:
                        final["width"], final["height"] = dims
                    if has_alpha is not None:
                        final["has_alpha"] = has_alpha
                    asset.analysis = final
                if aspect:
                    asset.aspect = aspect
                if duration:
                    asset.duration_s = duration
                asset.status = "ready"
            db.commit()
        _record(
            "pool_asset_analyzed",
            asset_id=asset_id,
            status="failed" if failed else "ready",
            has_llm_analysis=bool(analysis),
        )


# ── the matcher ───────────────────────────────────────────────────────────────


def _load_glossary(db) -> list[dict]:
    from sqlalchemy import select  # noqa: PLC0415

    rows = (
        db.execute(
            select(SoundEffect).where(
                SoundEffect.status == "ready",
                SoundEffect.audio_gcs_path.is_not(None),
                SoundEffect.archived_at.is_(None),
                # published_at gate mirrors the public picker (routes/sound_effects.py):
                # without it the AI matcher + zero-click auto-apply could bake an
                # unpublished/draft/admin-test effect into a user's downloaded video.
                SoundEffect.published_at.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "audio_gcs_path": r.audio_gcs_path,
            "duration_s": r.duration_s,
            # Sound-design metadata (migration 0065) — consumed by the SFX
            # suggestion path; the overlay intent mapper ignores them.
            "role_tags": list(r.role_tags or []),
            "contains_voice": bool(r.contains_voice),
        }
        for r in rows
    ]


def _occupied_intervals(variant: dict) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for card in variant.get("media_overlays") or []:
        try:
            out.append((float(card.get("start_s", 0.0)), float(card.get("end_s", 0.0))))
        except (TypeError, ValueError):
            continue
    return out


def _find_variant(job: Job, variant_id: str) -> dict | None:
    for v in (job.assembly_plan or {}).get("variants") or []:
        if v.get("variant_id") == variant_id:
            return v
    return None


def _persist_variant_fields(db, job_id: str, variant_id: str, fields: dict) -> dict | None:
    """Row-locked read-modify-write of one variant's keys (decision 4A pattern).
    Returns the FRESH variant dict (pre-update) for occupied-interval reads."""
    job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
    if job is None:
        return None
    variants = list((job.assembly_plan or {}).get("variants") or [])
    fresh = None
    for v in variants:
        if v.get("variant_id") == variant_id:
            fresh = dict(v)
            v.update(fields)
            break
    if fresh is None:
        return None
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
    flag_modified(job, "assembly_plan")
    db.commit()
    return fresh


@celery_app.task(name="app.tasks.autoplace.match_overlay_suggestions", **_AUTOPLACE_TASK_LIMITS)
def match_overlay_suggestions(
    job_id: str,
    variant_id: str,
    user_id: str,
    auto_apply: bool = False,
    smart_mode: bool = False,
) -> None:
    """Match pool assets to this variant's speech.

    `auto_apply=True` (plan 007, D2-B zero-click path): after persisting the
    suggestion set, re-read it under the row lock and burn it in through the
    SHARED apply helper — the same unit the route uses. Busy render / flag-off
    conditions degrade to suggest-only with a trace, never an error.
    """
    from app.config import settings  # noqa: PLC0415
    from app.services.overlay_autoplace import (  # noqa: PLC0415
        build_suggestions,
        filter_wishlist_against_assets,
        heuristic_match,
    )
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415
    from app.services.transcript_source import (  # noqa: PLC0415
        transcript_source,
        words_from_variant,
    )

    with pipeline_trace_for(job_id):
        try:
            # Visibility (007 CRITICAL-2): the TASK persists "matching" — the
            # route also sets it on the manual path, but the auto path has no
            # route, and the page's poll continuation keys off this status.
            with _sync_session() as db:
                _persist_variant_fields(
                    db, job_id, variant_id, {"overlay_suggest_status": "matching"}
                )
            # 1. Unlocked read: variant + assets (LLM call must not hold a row lock).
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job is None:
                    return
                variant = _find_variant(job, variant_id)
                if variant is None:
                    return
                item_id = job.content_plan_item_id
                duration_s = None
                for key in ("duration_s", "output_duration_s"):
                    if variant.get(key):
                        duration_s = float(variant[key])
                        break
                from sqlalchemy import select  # noqa: PLC0415

                assets = [
                    {
                        "id": str(a.id),
                        "gcs_path": a.gcs_path,
                        "kind": a.kind,
                        "source_filename": a.source_filename,
                        "duration_s": a.duration_s,
                        "aspect": a.aspect,
                        "analysis": a.analysis or {},
                    }
                    for a in db.execute(
                        select(PlanItemAsset).where(
                            PlanItemAsset.plan_item_id == item_id,
                            PlanItemAsset.status == "ready",
                        )
                    )
                    .scalars()
                    .all()
                ]
                glossary = _load_glossary(db)

            if not assets:
                # Route gates on ≥1 ready asset, but assets can vanish mid-flight.
                with _sync_session() as db:
                    _persist_variant_fields(
                        db,
                        job_id,
                        variant_id,
                        {"overlay_suggest_status": "zero", "overlay_suggestions": None},
                    )
                return

            # Self-healing backfill (006 decision C, widened by 009 E1/G1/v5):
            # stale REAL analyses — v1/v2 missing trim/dims signals, plus v3/v4
            # analyses missing brands — re-analyze in the background (refresh
            # keeps the asset ready); THIS run suggests without the newer
            # signals for them. Stubs never trigger
            # (analysis_is_stale excludes them — keyless-loop guard).
            for a in assets:
                if analysis_is_stale(a["analysis"], kind=a.get("kind")):
                    _record("autoplace_stale_analysis", asset_id=a["id"])
                    try:
                        analyze_pool_asset.apply_async(
                            args=[a["id"]],
                            kwargs={"refresh": True},
                            queue=settings.autoplace_queue,
                        )
                    except Exception as exc:  # noqa: BLE001 — best-effort, like register
                        log.warning(
                            "autoplace.backfill_dispatch_failed",
                            asset_id=a["id"],
                            error=str(exc)[:160],
                        )

            # 2. Transcript (Whisper fallback allowed here — task context).
            had_persisted_words = words_from_variant(variant) is not None
            src = transcript_source(variant, allow_whisper=True)
            if src is None:
                with _sync_session() as db:
                    _persist_variant_fields(
                        db, job_id, variant_id, {"overlay_suggest_status": "failed"}
                    )
                _record("autoplace_match_failed", reason="no_transcript", variant_id=variant_id)
                return
            words, transcript_hash = src
            # Main-duration provenance (006 decision D): persisted variant keys →
            # transcript end + 1s WITH a trace. The silent 60.0 fiction is gone —
            # transcript_source guarantees non-empty words here.
            if duration_s is None:
                duration_s = float(words[-1].get("end_s", 0.0)) + 1.0
                _record(
                    "autoplace_duration_fallback",
                    variant_id=variant_id,
                    duration_s=duration_s,
                    source="transcript_end",
                )

            # 3. Match: agent first, deterministic heuristic as fallback.
            raw, wishlist, matcher = [], [], "heuristic"
            if settings.gemini_api_key:
                try:
                    from app.agents._model_client import default_client  # noqa: PLC0415
                    from app.agents._runtime import RunContext  # noqa: PLC0415
                    from app.agents.overlay_placement import (  # noqa: PLC0415
                        OverlayPlacementAgent,
                        OverlayPlacementInput,
                        PlacementAsset,
                    )

                    agent_out = OverlayPlacementAgent(default_client()).run(
                        OverlayPlacementInput(
                            words=words,
                            assets=[
                                PlacementAsset(
                                    asset_id=a["id"],
                                    kind=a["kind"],
                                    subject=str((a["analysis"] or {}).get("subject", "")),
                                    description=str((a["analysis"] or {}).get("description", "")),
                                    on_screen_text=str(
                                        (a["analysis"] or {}).get("on_screen_text", "")
                                    ),
                                    brands=list((a["analysis"] or {}).get("brands") or []),
                                    duration_s=a["duration_s"],
                                    aspect=a["aspect"],
                                    width=(a["analysis"] or {}).get("width"),
                                    height=(a["analysis"] or {}).get("height"),
                                )
                                for a in assets
                            ],
                            occupied=[list(t) for t in _occupied_intervals(variant)],
                            duration_s=duration_s,
                            archetype=(
                                "smart_captions"
                                if smart_mode
                                else str(variant.get("resolved_archetype") or "talking_head")
                            ),
                        ),
                        ctx=RunContext(job_id=job_id),
                    )
                    raw, wishlist, matcher = agent_out.placements, agent_out.wishlist, "agent"
                except Exception as exc:  # noqa: BLE001
                    log.warning("autoplace.agent_failed", job_id=job_id, error=str(exc)[:200])
            if matcher == "heuristic":
                raw = heuristic_match(words, assets, duration_s=duration_s)
            wishlist = filter_wishlist_against_assets(wishlist, assets)

            # 4. Persist under the row lock, validating against FRESH occupied
            #    intervals (finding 8 — the user may have dragged cards mid-match).
            # Trace events from build_suggestions are DEFERRED until the lock is
            # released: record_pipeline_event opens its own connection and
            # UPDATEs the same jobs row this session holds FOR UPDATE — calling
            # it mid-lock self-deadlocks the worker (observed 2026-07-07: every
            # matcher run with ≥1 placement hung until pg_terminate_backend).
            deferred_trace: list[tuple[str, dict]] = []
            assets_by_id = {a["id"]: a for a in assets}
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
                if job is None:
                    return
                variants = list((job.assembly_plan or {}).get("variants") or [])
                target = next((v for v in variants if v.get("variant_id") == variant_id), None)
                if target is None:
                    return
                # Plan 009: fullscreen data sources, named (eng review E11).
                # intro windows ← the variant's text_elements timings (montage
                # intro text); caption cues ← persisted narrated caption_cues.
                # Variants without either simply skip rules (a-text)/(h).
                intro_windows: list[tuple[float, float]] = []
                for el in target.get("text_elements") or []:
                    try:
                        intro_windows.append(
                            (float(el.get("start_s", 0.0)), float(el.get("end_s", 0.0)))
                        )
                    except (TypeError, ValueError):
                        continue
                fs_stats: dict = {}
                suggestions = build_suggestions(
                    raw,
                    assets_by_id=assets_by_id,
                    words=words,
                    duration_s=duration_s,
                    occupied=_occupied_intervals(target),
                    glossary=glossary,
                    trace=lambda event, **f: deferred_trace.append(
                        (event, {"variant_id": variant_id, **f})
                    ),
                    fullscreen_enabled=(settings.fullscreen_cutaways_enabled or smart_mode),
                    intro_windows=intro_windows,
                    caption_cues=list(target.get("caption_cues") or []),
                    stats=fs_stats,
                )
                target["overlay_suggestions"] = suggestions or None
                target["overlay_suggest_status"] = "ready" if suggestions else "zero"
                target["overlay_suggest_hash"] = transcript_hash
                target["overlay_suggest_wishlist"] = list(wishlist) or None
                if not had_persisted_words:
                    # Whisper ran — persist run-once so re-matches and staleness
                    # checks read the same words (tension-1 contract). Under a
                    # DEDICATED key (review C19): writing to "transcript" would
                    # flip the variant sequence-capable in generative_jobs.py
                    # (cross-feature collision). transcript_source reads both.
                    target["overlay_transcript"] = words
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

                job.assembly_plan = {**(job.assembly_plan or {}), "variants": variants}
                flag_modified(job, "assembly_plan")
                db.commit()
            for _ev, _fields in deferred_trace:
                _record(_ev, **_fields)
            _record(
                "autoplace_match_done",
                variant_id=variant_id,
                matcher=matcher,
                suggestions=len(suggestions),
                wishlist=len(wishlist),
                transcript_hash=transcript_hash,
            )

            # Zero-click apply (007 D2-B). Re-read under a FRESH row lock — a
            # concurrent dismiss/asset-delete between persist and apply must win
            # (the 005-finding-8 class). Flag matrix (G3-A): overlays flag off ⇒
            # the dispatch 404s and we degrade to suggest-only; busy render ⇒
            # 409 ⇒ suggest-only. Both traced, never raised.
            if auto_apply and suggestions:
                from app.services.overlay_apply import (  # noqa: PLC0415
                    apply_suggestions_to_variant,
                )

                try:
                    with _sync_session() as db:
                        job = db.get(Job, uuid.UUID(job_id), with_for_update=True)
                        if job is None:
                            return
                        fresh_variant = _find_variant(job, variant_id)
                        fresh = list((fresh_variant or {}).get("overlay_suggestions") or [])
                        if not fresh:
                            _record(
                                "autoplace_auto_apply_skipped",
                                variant_id=variant_id,
                                reason="suggestions_gone",
                            )
                            return
                        result = apply_suggestions_to_variant(
                            job, variant_id, fresh, user_id=user_id
                        )
                        # Demote receipt (plan 009 ARCH-4): the zero-click path
                        # must NEVER silently ship fewer/smaller visuals than
                        # matched. Combine suggestion-time fullscreen demotions
                        # with apply-time drops; the helper already wrote the
                        # dropped-half under this same row lock.
                        demoted = int(fs_stats.get("fullscreen_demoted", 0) or 0)
                        if demoted:
                            fv = _find_variant(job, variant_id)
                            if fv is not None:
                                receipt = dict(fv.get("overlay_apply_receipt") or {})
                                receipt["demoted"] = demoted
                                receipt.setdefault("dropped", int(result["dropped"]))
                                receipt["reason"] = (fs_stats.get("demote_reasons") or ["cap"])[0]
                                from datetime import datetime as _dt  # noqa: PLC0415

                                receipt.setdefault("at", _dt.utcnow().isoformat() + "Z")
                                fv["overlay_apply_receipt"] = receipt
                                from sqlalchemy.orm.attributes import (  # noqa: PLC0415
                                    flag_modified,
                                )

                                flag_modified(job, "assembly_plan")
                        db.commit()
                    _record(
                        "autoplace_auto_applied",
                        variant_id=variant_id,
                        applied=result["applied"],
                        dropped=result["dropped"],
                        sfx=result["sfx"],
                        fullscreen=fs_stats.get("fullscreen_emitted", 0),
                        fullscreen_demoted=fs_stats.get("fullscreen_demoted", 0),
                    )
                except Exception as exc:  # noqa: BLE001 — degrade, never fail the match
                    # HTTPException(404 flag gate / 409 busy) or anything else:
                    # suggestions stay persisted for manual review.
                    _record(
                        "autoplace_auto_apply_degraded",
                        variant_id=variant_id,
                        reason=str(getattr(exc, "detail", exc))[:160],
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("autoplace.match_failed", job_id=job_id, error=str(exc)[:300])
            try:
                with _sync_session() as db:
                    _persist_variant_fields(
                        db, job_id, variant_id, {"overlay_suggest_status": "failed"}
                    )
            except Exception:  # noqa: BLE001
                pass
            _record("autoplace_match_failed", reason=str(exc)[:160], variant_id=variant_id)


@celery_app.task(name="app.tasks.autoplace.autoplace_sfx_suggestions", **_AUTOPLACE_TASK_LIMITS)
def autoplace_sfx_suggestions(job_id: str, variant_id: str) -> None:
    """Propose advisory SFX placements for one rendered variant (dark-flagged).

    Best-effort end to end: any failure logs + leaves the variant untouched —
    a generative job never degrades because sound-design suggestions failed.
    Speech source precedence mirrors the read path (persisted words → caption
    cue words), then bounded Whisper as the task-context fallback; Whisper
    words are persisted under `overlay_transcript` (run-once semantics, same
    contract as the overlay matcher — NEVER `transcript`, review C19).
    """
    from app.config import settings  # noqa: PLC0415
    from app.services.pipeline_trace import pipeline_trace_for  # noqa: PLC0415
    from app.services.sfx_autoplace import resolve_sfx_suggestions  # noqa: PLC0415
    from app.services.speech_map import build_speech_map  # noqa: PLC0415
    from app.services.transcript_source import (  # noqa: PLC0415
        compute_transcript_hash,
        speech_words_for_variant,
        transcribe_variant_video,
        variant_duration_s,
    )

    if not settings.sfx_autoplace_enabled:
        return
    with pipeline_trace_for(job_id):
        try:
            # 1. Unlocked read (the LLM call must not hold a row lock).
            with _sync_session() as db:
                job = db.get(Job, uuid.UUID(job_id))
                if job is None:
                    return
                variant = _find_variant(job, variant_id)
                if variant is None:
                    return
                glossary = _load_glossary(db)
            if not glossary:
                _record("sfx_autoplace_skipped", reason="empty_glossary", variant_id=variant_id)
                return

            # 2. Speech source: read-path precedence first, Whisper fallback.
            whisper_words: list[dict] | None = None
            speech = speech_words_for_variant(variant)
            if speech is not None:
                words, _source = speech
            else:
                whisper_words = transcribe_variant_video(variant)
                if not whisper_words:
                    _record("sfx_autoplace_skipped", reason="no_transcript", variant_id=variant_id)
                    return
                words = whisper_words
            duration_s = variant_duration_s(variant)
            if duration_s is None:
                duration_s = float(words[-1].get("end_s", 0.0)) + 1.0
            speech_map = build_speech_map(words, duration_s, "task")
            if speech_map is None:
                _record("sfx_autoplace_skipped", reason="no_usable_words", variant_id=variant_id)
                return

            # 3. Clip-moment windows from the persisted AI timeline (cumulative
            # output windows in slot order); best-effort — empty when absent.
            moments: list[dict] = []
            timeline = variant.get("ai_timeline") or {}
            slots = timeline.get("slots") if isinstance(timeline, dict) else None
            if isinstance(slots, list):
                cursor = 0.0
                for slot in sorted(
                    (s for s in slots if isinstance(s, dict)),
                    key=lambda s: s.get("order", 0),
                ):
                    try:
                        slot_dur = float(slot.get("duration_s", 0.0))
                    except (TypeError, ValueError):
                        continue
                    if slot_dur <= 0:
                        continue
                    description = str(slot.get("moment_description") or "")
                    if description:
                        moments.append(
                            {
                                "start_s": round(cursor, 2),
                                "end_s": round(cursor + slot_dur, 2),
                                "description": description,
                            }
                        )
                    cursor += slot_dur

            # 4. One agent call; validation is the server's job.
            from app.agents._model_client import default_client  # noqa: PLC0415
            from app.agents._runtime import RunContext  # noqa: PLC0415
            from app.agents.sfx_placement import (  # noqa: PLC0415
                SfxCatalogEffect,
                SfxPlacementAgent,
                SfxPlacementInput,
            )

            if not settings.gemini_api_key:
                _record("sfx_autoplace_skipped", reason="no_gemini_key", variant_id=variant_id)
                return
            agent_out = SfxPlacementAgent(default_client()).run(
                SfxPlacementInput(
                    words=words,
                    pauses=speech_map["pauses"],
                    moments=moments,
                    effects=[
                        SfxCatalogEffect(
                            effect_id=str(g["id"]),
                            name=str(g.get("name") or ""),
                            role_tags=list(g.get("role_tags") or []),
                            duration_s=g.get("duration_s"),
                        )
                        for g in glossary
                        if not g.get("contains_voice")
                    ],
                    duration_s=duration_s,
                ),
                ctx=RunContext(job_id=job_id),
            )
            transcript_hash = compute_transcript_hash(words, duration_s)
            suggestions = resolve_sfx_suggestions(
                agent_out.placements, glossary, duration_s, transcript_hash
            )

            # 5. Persist under the row lock; trace AFTER the lock releases
            # (record_pipeline_event opens its own connection and UPDATEs the
            # same jobs row — firing it mid-lock self-deadlocks the worker).
            with _sync_session() as db:
                fields: dict = {"pending_sfx_suggestions": suggestions}
                if whisper_words:
                    fields["overlay_transcript"] = whisper_words
                _persist_variant_fields(db, job_id, variant_id, fields)
            _record(
                "sfx_autoplace_suggested",
                variant_id=variant_id,
                proposed=len(agent_out.placements),
                accepted=len(suggestions),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("sfx_autoplace.failed", job_id=job_id, error=str(exc)[:300])
            _record("sfx_autoplace_failed", reason=str(exc)[:160], variant_id=variant_id)
