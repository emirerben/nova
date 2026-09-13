"""Narrow runtime-1 editor execution, using the canonical atomic Save validators."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.models import Job, PlanItem, PlanItemAsset
from app.routes._copilot import CopilotTurnBody, run_copilot_turn
from app.routes.generative_jobs import (
    EditorCommitRequest,
    enqueue_editor_commit_render,
    prepare_editor_commit,
    variant_render_baseline,
)
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops


def is_visual_removal_request(message: str) -> bool:
    """Admission only; the model must still resolve an explicit safe operation."""
    return bool(
        re.search(r"\b(remove|delete|clear|erase|strip|take out|get rid of)\b", message, re.I)
        and re.search(
            r"\b(visuals?|overlays?|images?|pictures?|photos?|video layers?)\b", message, re.I
        )
    )


def selected_variant(thread: Any, job: Any) -> dict | None:
    variants = [v for v in (job.assembly_plan or {}).get("variants", []) if isinstance(v, dict)]
    selected = (thread.state or {}).get("selected_variant_id")
    if selected:
        return next((v for v in variants if v.get("variant_id") == selected), None)
    return variants[0] if len(variants) == 1 else None


async def execute_visual_removal(db: Any, thread: Any, body: Any, user: Any) -> Any:
    """Resolve outside locks, then persist user event, mutation and receipt together."""
    # Lazy import avoids the route/service module cycle.
    from app.routes import creation_threads as routes

    _, _, job, _ = await routes._load_authorized_projection_rows(db, thread)
    variant = selected_variant(thread, job) if job is not None else None
    if variant is None or len(body.message) > 2000:
        reply = (
            "Please shorten this Visuals request to 2,000 characters so I can apply it safely."
            if len(body.message) > 2000
            else (
                "Open the video version you want to edit, "
                "then ask me to remove its uploaded Visuals."
            )
        )
        await routes._append(
            db,
            thread,
            event_type="user_message",
            role="user",
            content=body.message,
            client_event_id=body.client_event_id,
        )
        await routes._append(
            db, thread, event_type="assistant_response", role="assistant", content=reply
        )
        await db.commit()
        return thread

    user_id, user_type = user.id, type(user)
    job_id, variant_id = job.id, variant["variant_id"]
    selection = (thread.state or {}).get("selected_variant_id")
    baseline = variant_render_baseline(variant)
    snapshot = build_editor_snapshot(job, copy.deepcopy(variant))
    # This first release executes only uploaded media removals. Other editor
    # families retain their existing client Save / planner workflows.
    snapshot["component_context_version"] = 1
    snapshot["allowed_op_families"] = [
        family for family in snapshot["allowed_op_families"] if family == "visual_media"
    ]
    thread_id = str(thread.id)
    await db.rollback()
    response = await run_copilot_turn(
        CopilotTurnBody(
            message=body.message,
            snapshot=snapshot,
            client_contract_version=2,
            client_request_id=hashlib.sha256(body.client_event_id.encode("utf-8")).hexdigest(),
        ),
        job_id=job_id,
    )
    # Preserve the global mutation lock order: account -> thread -> item -> job.
    user = await db.get(user_type, user_id, with_for_update=True, populate_existing=True)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    thread = await routes._load(thread_id, user, db, lock=True)
    routes._require_runtime_v1_mutation(thread)
    duplicate = await routes._duplicate(db, thread.id, routes._client_id(body.client_event_id))
    if duplicate:
        if duplicate.event_type != "user_message" or duplicate.content != body.message:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        await db.rollback()
        return await routes._load(thread_id, user, db)
    if thread.status != "active" or thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    # Refresh and hold the item before validating its current-job pointer, as
    # canonical manual Save does. A job replacement cannot pass this fence.
    locked_item = await db.get(
        PlanItem, thread.active_plan_item_id, populate_existing=True, with_for_update=True
    )
    if locked_item is None or locked_item.current_job_id != job_id:
        raise HTTPException(status_code=409, detail="Editor target changed")
    item, _, current_job, _ = await routes._load_authorized_projection_rows(db, thread)
    if (
        current_job is None
        or current_job.id != job_id
        or (thread.state or {}).get("selected_variant_id") != selection
    ):
        raise HTTPException(status_code=409, detail="Editor target changed")
    job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
    if job.status == "cancelled":
        raise HTTPException(status_code=409, detail="Cancelled videos cannot be edited.")
    variant = selected_variant(thread, job)
    if (
        variant is None
        or variant.get("variant_id") != variant_id
        or variant_render_baseline(variant) != baseline
    ):
        raise HTTPException(status_code=409, detail="baseline_conflict")

    prep = None
    receipt: dict[str, Any] = {
        "job_id": str(job_id),
        "variant_id": variant_id,
        "outcome": response.outcome,
    }
    if response.ops and all(op.get("op") == "remove_visual_media" for op in response.ops):
        original_plan = copy.deepcopy(job.assembly_plan)
        try:
            compiled = compile_editor_ops(job, variant, response.ops)
            payload = EditorCommitRequest.model_validate(compiled.payload)
            assets = (
                (
                    await db.execute(
                        select(PlanItemAsset).where(
                            PlanItemAsset.plan_item_id == thread.active_plan_item_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            visual_assets = {
                str(asset.id): {
                    "status": asset.status,
                    "gcs_path": asset.gcs_path,
                    "kind": asset.kind,
                    "duration_s": getattr(asset, "duration_s", None),
                    "preview_gcs_path": getattr(asset, "preview_gcs_path", None),
                    "user_context": getattr(asset, "user_context", None),
                }
                for asset in assets
            }
            if variant.get("resolved_archetype") == "guided_story":
                from app.pipeline.guided_story import (  # noqa: PLC0415
                    GuidedStoryError,
                    validate_guided_snapshot,
                )
                from app.routes.plan_items import _proposal_media_is_current  # noqa: PLC0415

                try:
                    _, _, guided_snapshot = validate_guided_snapshot(
                        (job.assembly_plan or {}).get("guided_edit")
                    )
                    media_current = await _proposal_media_is_current(
                        item, guided_snapshot, db, user_id=user.id
                    )
                except GuidedStoryError as exc:
                    raise ValueError("The guided source is stale.") from exc
                if not media_current:
                    raise ValueError("The guided source is stale.")
            prep = prepare_editor_commit(
                job,
                variant_id,
                payload,
                user_id=str(user.id),
                plan_item_id=str(thread.active_plan_item_id),
                visual_assets=visual_assets,
            )
            if not prep["sections"].get("visual_blocks"):
                raise ValueError("This video cannot currently save uploaded Visuals changes.")
            receipt.update(
                outcome="saved", generation=prep["generation"], sections=prep["sections"]
            )
            reply = (
                "Removed the requested uploaded Visuals from the saved edit. "
                "Text, captions, footage and audio are unchanged."
            )
        except (ValueError, HTTPException) as exc:
            # Validation stages no writes on failure. Do not convert a moved
            # baseline into a misleading assistant response.
            if isinstance(exc, HTTPException) and exc.status_code == 409:
                raise
            job.assembly_plan = original_plan
            prep = None
            receipt["outcome"] = "unsupported"
            reply = "I couldn’t safely save that Visuals removal. The edit is unchanged."
    else:
        # Model prose is not evidence of persistence, even for clarification
        # or no_effect (a negated unrelated clause can mask a success claim).
        receipt["outcome"] = response.outcome if not response.ops else "unsupported"
        reply = (
            "Which uploaded image or video Visuals should I remove? "
            "Please identify them or say all uploaded Visuals. Nothing was changed."
            if response.outcome == "clarification" and not response.ops
            else (
                "I couldn’t apply that uploaded Visuals removal. Nothing was changed. "
                "I can remove supported uploaded image and video Visuals; "
                "please identify the ones you want removed."
            )
        )
    await routes._append(
        db,
        thread,
        event_type="user_message",
        role="user",
        content=body.message,
        client_event_id=body.client_event_id,
    )
    await routes._append(
        db,
        thread,
        event_type="assistant_response",
        role="assistant",
        content=reply,
        payload=receipt,
    )
    await db.commit()
    if prep:
        try:
            enqueue_editor_commit_render(str(job_id), variant_id, prep)
        except Exception:
            # Desired state is durable; terminalize only this exact attempt.
            thread = await routes._load(thread_id, user, db, lock=True)
            job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan or {})
            terminalized = False
            for row in plan.get("variants", []):
                if (
                    row.get("variant_id") == variant_id
                    and row.get("render_generation_id") == prep["generation"]
                    and row.get("render_status") == "rendering"
                ):
                    row.update(
                        render_status="failed",
                        ok=False,
                        error="The saved render could not be queued.",
                        error_class="render_enqueue_failed",
                    )
                    terminalized = True
                    job.assembly_plan = plan
                    if row.get("video_path") or row.get("output_url"):
                        job.status = "variants_ready_partial"
                    break
            if terminalized:
                await routes._append(
                    db,
                    thread,
                    event_type="assistant_render_failed",
                    role="assistant",
                    content=(
                        "Your Visuals change was saved, but rendering could not start. "
                        "Retry the render to update the exported video."
                    ),
                    payload={**receipt, "outcome": "render_enqueue_failed"},
                )
            await db.commit()
    return thread
