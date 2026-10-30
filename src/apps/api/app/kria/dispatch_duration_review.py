"""Read-only duration audit after dispatch stamping and after native compilation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from app.kria.brief_binding import BriefBinding
from app.kria.device_render import DeviceRecipe
from app.kria.freeze_ask import times_one_moment
from app.services.creator_render_contract import read_render_contract
from app.services.length_policy import (
    _DURATION_CONFLICTS,
    creator_duration,
    creator_duration_satisfied,
    creator_max_export_duration_s,
    strict_duration_matches,
)


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


@dataclass(frozen=True)
class DispatchDurationSnapshot:
    job_id: str
    generation_id: str
    binding_json: str
    approved_plan_json: str
    final_strategy_json: str
    contract_json: str
    invalid_choice_value: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(_json(vars(self)).encode()).hexdigest()


@dataclass(frozen=True)
class RestoreApprovedDuration:
    target_s: float
    binding_digest: str
    source_snapshot_digest: str
    operation: Literal["restore_approved_total_duration"] = "restore_approved_total_duration"
    scope: Literal["global"] = "global"


@dataclass(frozen=True)
class DurationReview:
    status: Literal[
        "consistent", "correction_required", "no_explicit_global", "clarification_needed"
    ]
    mismatches: tuple[str, ...] = ()
    candidate: RestoreApprovedDuration | None = None
    recipe_checked: bool = False


def capture_dispatch_duration(
    job: Any, *, binding: BriefBinding, approved_plan: dict[str, Any]
) -> DispatchDurationSnapshot:
    binding.resolve(binding.thread_id)
    assembly = job.assembly_plan or {}
    recorded = BriefBinding.model_validate(assembly.get("creator_brief_binding"))
    recorded.resolve(binding.thread_id)
    if recorded.digest != binding.digest:
        raise ValueError("Dispatch binding differs from approved binding")
    contract = read_render_contract(assembly)
    generation = assembly.get("creator_generation_id")
    if not generation or contract is None or contract.generation_id != generation:
        raise ValueError("Dispatch contract generation is unavailable or stale")
    return DispatchDurationSnapshot(
        job_id=str(job.id),
        generation_id=generation,
        binding_json=binding.model_dump_json(),
        approved_plan_json=_json(approved_plan),
        final_strategy_json=_json((job.all_candidates or {}).get("creator_strategy") or {}),
        contract_json=contract.model_dump_json(),
        invalid_choice_value=any(not _positive_number(value) for value in _choice_values(binding)),
    )


def _positive_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _choice_values(binding: BriefBinding) -> list[Any]:
    return [
        answer.get("duration_s")
        for answer in binding.choice_answers
        if answer.get("conflict") in _DURATION_CONFLICTS
    ]


def review_dispatch_duration(
    snapshot: DispatchDurationSnapshot, *, compiled_recipe: DeviceRecipe | None = None
) -> DurationReview:
    """Consistency evidence only; callers still own locks, freshness and execution admission."""
    if snapshot.invalid_choice_value:
        return DurationReview("clarification_needed")
    binding = BriefBinding.model_validate_json(snapshot.binding_json)
    brief = binding.resolve(binding.thread_id)
    globals_ = (
        [
            row
            for row in brief.live()
            if row.kind == "timing"
            and row.scope == "global"
            and not times_one_moment(row)
            and row.facts.get("duration_s") is not None
        ]
        if brief is not None
        else []
    )
    values = [row.facts["duration_s"] for row in globals_] + _choice_values(binding)
    if any(not _positive_number(value) for value in values) or len(set(values)) > 1:
        return DurationReview("clarification_needed")
    global_brief = (
        brief.model_copy(update={"requirements": globals_}) if brief is not None else None
    )
    authority = creator_duration(brief=global_brief, authoritative_answers=binding.choice_answers)
    if authority is None:
        return DurationReview("no_explicit_global")
    if not _positive_number(authority.seconds):
        return DurationReview("clarification_needed")
    target = authority.seconds
    strategy = json.loads(snapshot.final_strategy_json)
    contract = json.loads(snapshot.contract_json)
    approved = json.loads(snapshot.approved_plan_json)
    approved_target = approved.get("target_duration_s")
    approved_matches = _positive_number(approved_target) and (
        approved_target <= target
        if authority.bound == "at_most"
        else math.isclose(approved_target, target, abs_tol=0.001, rel_tol=0)
    )
    if not approved_matches:
        return DurationReview("clarification_needed", ("approved_plan.target_duration_s",))
    observed = {
        "strategy.target_duration_s": strategy.get("target_duration_s"),
        "strategy.creator_duration_s": strategy.get("creator_duration_s"),
        "contract.duration_s": contract.get("duration_s"),
    }
    mismatches = [
        name
        for name, value in observed.items()
        if not _positive_number(value)
        or not math.isclose(
            value,
            approved_target if name == "strategy.target_duration_s" else target,
            abs_tol=0.001,
            rel_tol=0,
        )
    ]
    if strategy.get("target_duration_requested") is not True:
        mismatches.append("strategy.target_duration_requested")
    if compiled_recipe is not None:
        duration = compiled_recipe.duration
        recipe_matches = (
            math.isclose(duration, target, abs_tol=1 / 30, rel_tol=0)
            if authority.bound == "exact"
            else creator_duration_satisfied(duration, target, authority.bound)
        )
        strict_maximum = creator_max_export_duration_s(brief=global_brief)
        if strict_maximum is not None:
            recipe_matches = recipe_matches and strict_duration_matches(duration, strict_maximum)
        if not _positive_number(duration) or not recipe_matches:
            mismatches.append("compiled_recipe.duration")
    if mismatches and authority.bound != "exact":
        return DurationReview(
            "clarification_needed", tuple(mismatches), recipe_checked=compiled_recipe is not None
        )
    if mismatches:
        return DurationReview(
            "correction_required",
            tuple(mismatches),
            RestoreApprovedDuration(target, binding.digest, snapshot.digest),
            recipe_checked=compiled_recipe is not None,
        )
    return DurationReview("consistent", recipe_checked=compiled_recipe is not None)
