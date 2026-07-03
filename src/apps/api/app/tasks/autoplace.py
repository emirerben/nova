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

import os
import tempfile
import uuid

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


# ── asset analysis ────────────────────────────────────────────────────────────


# Bump when the analysis payload gains fields the matcher depends on. The
# matcher backfills REAL analyses older than this (plan 006, decision C);
# stubs carry the current version too, so they NEVER trigger the backfill loop.
# v3 (plan 009 E1): pixel width/height persisted into the analysis JSONB —
# rule (g)'s fail-closed resolution gate and the FE low-res warning both need
# real dims, and the self-healing backfill now covers IMAGE assets too.
ANALYSIS_VERSION = 3


def _stub_analysis(asset: PlanItemAsset) -> dict:
    """Keyless fallback: a filename-derived subject so the heuristic matcher
    still has tokens to work with (finding 10 — flow completes without keys)."""
    stem = (asset.source_filename or "").rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
    return {
        "subject": stem.strip()[:80],
        "description": "",
        "on_screen_text": "",
        "kind_hint": "other",
        "source": "stub",
        "analysis_version": ANALYSIS_VERSION,
    }


def analysis_is_stale(analysis: dict | None) -> bool:
    """True for pre-006 REAL analyses (no best_moments persisted). Stubs are
    never stale — re-analyzing them on a keyless machine yields another stub
    (the infinite-loop class the outside voice flagged, plan 006 finding 2)."""
    a = analysis or {}
    if a.get("source") == "stub":
        return False
    try:
        return int(a.get("analysis_version") or 1) < ANALYSIS_VERSION
    except (TypeError, ValueError):
        return True


def _analyze_image(
    local_path: str, job_scope: str
) -> tuple[dict | None, float | None, tuple[int, int] | None]:
    """(analysis, aspect, (width, height)) for a still image."""
    aspect: float | None = None
    dims: tuple[int, int] | None = None
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(local_path) as im:
            if im.height:
                aspect = round(im.width / im.height, 4)
                dims = (int(im.width), int(im.height))
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace.image_size_failed", error=str(exc)[:160])

    from app.config import settings  # noqa: PLC0415

    if not settings.gemini_api_key:
        return None, aspect, dims
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
            },
            aspect,
            dims,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("autoplace.image_analysis_failed", error=str(exc)[:200])
        return None, aspect, dims


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
                    analysis, aspect, dims = _analyze_image(local, scope)
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
        {"id": r.id, "name": r.name, "audio_gcs_path": r.audio_gcs_path, "duration_s": r.duration_s}
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
    job_id: str, variant_id: str, user_id: str, auto_apply: bool = False
) -> None:
    """Match pool assets to this variant's speech.

    `auto_apply=True` (plan 007, D2-B zero-click path): after persisting the
    suggestion set, re-read it under the row lock and burn it in through the
    SHARED apply helper — the same unit the route uses. Busy render / flag-off
    conditions degrade to suggest-only with a trace, never an error.
    """
    from app.config import settings  # noqa: PLC0415
    from app.services.overlay_autoplace import build_suggestions, heuristic_match  # noqa: PLC0415
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

            # Self-healing backfill (006 decision C, widened by 009 E1): stale
            # REAL analyses — videos missing best_moments AND any asset missing
            # v3 pixel dims — re-analyze in the background (refresh keeps the
            # asset ready); THIS run suggests without trim/dims for them. Stubs
            # never trigger (analysis_is_stale excludes them — keyless-loop guard).
            for a in assets:
                if analysis_is_stale(a["analysis"]):
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
                                    duration_s=a["duration_s"],
                                    aspect=a["aspect"],
                                    width=(a["analysis"] or {}).get("width"),
                                    height=(a["analysis"] or {}).get("height"),
                                )
                                for a in assets
                            ],
                            occupied=[list(t) for t in _occupied_intervals(variant)],
                            duration_s=duration_s,
                        ),
                        ctx=RunContext(job_id=job_id),
                    )
                    raw, wishlist, matcher = agent_out.placements, agent_out.wishlist, "agent"
                except Exception as exc:  # noqa: BLE001
                    log.warning("autoplace.agent_failed", job_id=job_id, error=str(exc)[:200])
            if matcher == "heuristic":
                raw = heuristic_match(words, assets, duration_s=duration_s)

            # 4. Persist under the row lock, validating against FRESH occupied
            #    intervals (finding 8 — the user may have dragged cards mid-match).
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
                    trace=lambda event, **f: _record(event, variant_id=variant_id, **f),
                    fullscreen_enabled=settings.fullscreen_cutaways_enabled,
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
