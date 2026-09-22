"""Atomically project validated editor state into the next immutable phone recipe."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import HTTPException

from app.config import settings
from app.kria.device_render import make_device_request
from app.pipeline.guided_story import (
    GuidedStoryError,
    GuidedStoryExecutionPlan,
    compile_guided_runtime_plan,
    song_reference_variant_fields,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor_sources import (
    editor_source_bindings,
    editor_sources_for_variant,
    editor_visual_bindings,
)
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import (
    PHONE_SOURCES_FIELD,
    PHONE_VISUALS_FIELD,
    PhoneSourceBinding,
    PhoneVisualBinding,
)

log = structlog.get_logger()
PHONE_EDITOR_PLAN_FIELD = "_phone_editor_plan_v1"


class _StagedJob:
    """Keep validator mutations off the ORM until the full native program validates."""

    def __init__(self, job: Any):
        self._job = job
        self.assembly_plan = copy.deepcopy(job.assembly_plan or {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._job, name)


def prepare_phone_editor_commit(
    job: Any, variant_id: str, *, prepare: Callable[[Any], dict]
) -> dict:
    staged = _StagedJob(job)
    prep = prepare(staged)
    if not prep["has_render_section"]:
        return {**prep, "render_destination": "device"}
    if not settings.phone_rendering_for(job.user_id):
        raise HTTPException(422, detail={"code": "phone_rendering_unavailable"})
    try:
        previous = device_status(job, variant_id).request
        assembly = staged.assembly_plan
        variant = next(v for v in assembly["variants"] if v.get("variant_id") == variant_id)
        plan = copy.deepcopy(
            variant.get(PHONE_EDITOR_PLAN_FIELD) or assembly["guided_story_execution_plan"]
        )
        revision = prep.get("guided_revision")
        render_sections = {key for key, value in prep["sections"].items() if value}
        if not (render_sections - {"text_elements"}):
            # A text-only save keeps the approved plan's device-verified timing
            # program and swaps just the text lane, whether it came from a
            # legacy guided editor or the v2 revision contract. Re-projecting
            # through `compile_guided_runtime_plan` re-clocks every moment and
            # transition onto 1/30 s frames, which `compile_phone_guided_plan`
            # rejects as a different program from the millisecond timing the
            # phone was pinned against -- so under guided editor v2 every phone
            # text edit was a 422 `unsupported_phone_edit` (2026-09-19 chat-edit
            # incident, job d9a965b0). Other approved lanes, including the
            # contextual and narration label lanes, are preserved untouched.
            plan["text_elements"] = variant.get("text_elements") or []
        elif revision is not None:
            plan = compile_guided_runtime_plan(
                assembly["guided_story_execution_plan"],
                assembly["guided_edit"],
                revision,
                admitted_sources=editor_sources_for_variant(variant),
            )
        else:
            raise ValueError("phone editor requires a canonical guided revision")
        bindings = tuple(
            PhoneSourceBinding.model_validate(row) for row in assembly[PHONE_SOURCES_FIELD]
        ) + editor_source_bindings(variant)
        # Reuse approved and asynchronously admitted receipts; Save never
        # downloads or hashes media while holding its database locks.
        visuals = tuple(
            PhoneVisualBinding.model_validate(row)
            for row in assembly.get(PHONE_VISUALS_FIELD) or []
        ) + editor_visual_bindings(variant)
        # Narration is already pinned in the previous immutable recipe. Never
        # re-download/hash on Save, nor silently drop its audio when adding media.
        narration = None
        if plan.get("narration") is not None:
            voice = next(
                asset
                for asset in previous.recipe.asset_manifest.assets
                if asset.kind == "voiceover"
            )
            media = next(asset for asset in previous.recipe.assets if asset.id == voice.id)
            narration = PhoneNarrationBed(
                plan_item_id=voice.plan_item_id,
                generation=voice.generation,
                fingerprint=voice.fingerprint,
                duration_s=media.duration,
            )
        allow_editor_media = bool(plan.get("editor_visual_blocks"))
        recipe = compile_phone_guided_plan(
            GuidedStoryExecutionPlan.model_validate(plan),
            bindings,
            visuals=visuals,
            narration=narration,
            allow_editor_media=allow_editor_media,
        )
        validate_phone_pilot_recipe(recipe, allow_editor_media=allow_editor_media)
        request = make_device_request(
            job_id=job.id,
            variant_id=variant_id,
            revision=previous.identity.recipe_revision + 1,
            recipe=recipe,
        )
        pin_device_request(staged, request, base_generation=prep["generation"])
        for variant in staged.assembly_plan["variants"]:
            if variant.get("variant_id") == variant_id:
                variant["render_status"] = "awaiting_device"
                variant["render_destination"] = "device"
                variant["duration_s"] = plan["resolved_duration_s"]
                variant[PHONE_EDITOR_PLAN_FIELD] = plan
                variant.update(song_reference_variant_fields(plan))
        staged.status = "awaiting_device"
    except (KeyError, StopIteration, TypeError, ValueError, GuidedStoryError) as exc:
        # The cause was invisible: a bare code reached the creator and nothing
        # reached the logs (2026-09-19 phone chat-edit incident). Keep the
        # wire code stable; name the failing step for operators.
        reason = f"{type(exc).__name__}: {exc}"[:300]
        log.warning(
            "phone_editor_commit_unsupported",
            job_id=str(job.id),
            variant_id=variant_id,
            reason=reason,
            exc_info=True,
        )
        raise HTTPException(
            422, detail={"code": "unsupported_phone_edit", "reason": reason}
        ) from exc
    job.assembly_plan = staged.assembly_plan
    job.status = staged.status
    if "started_at" in vars(staged):
        job.started_at = staged.started_at
    return {**prep, "render_destination": "device", "render_task_id": None}
