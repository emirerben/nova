"""Atomically project validated editor state into the next immutable phone recipe."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from app.config import settings
from app.kria.device_render import make_device_request
from app.pipeline.guided_story import (
    GuidedStoryError,
    GuidedStoryExecutionPlan,
    compile_guided_runtime_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.device_render import device_status, pin_device_request
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding


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
        plan = copy.deepcopy(assembly["guided_story_execution_plan"])
        revision = prep.get("guided_revision")
        if revision is not None:
            plan = compile_guided_runtime_plan(plan, assembly["guided_edit"], revision)
        else:
            # Legacy guided editors permit text-only saves. Preserve all other
            # approved lanes, including contextual and narration label lanes.
            render_sections = {key for key, value in prep["sections"].items() if value}
            if render_sections - {"text_elements"}:
                raise ValueError("phone editor requires a canonical guided revision")
            plan["text_elements"] = variant.get("text_elements") or []
        bindings = tuple(
            PhoneSourceBinding.model_validate(row) for row in assembly[PHONE_SOURCES_FIELD]
        )
        recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(plan), bindings)
        validate_phone_pilot_recipe(recipe)
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
        staged.status = "awaiting_device"
    except (KeyError, StopIteration, TypeError, ValueError, GuidedStoryError) as exc:
        raise HTTPException(422, detail={"code": "unsupported_phone_edit"}) from exc
    job.assembly_plan = staged.assembly_plan
    job.status = staged.status
    if "started_at" in vars(staged):
        job.started_at = staged.started_at
    return {**prep, "render_destination": "device", "render_task_id": None}
