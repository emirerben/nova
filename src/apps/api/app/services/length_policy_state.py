"""Generation-bound duration policy beside the unchanged stored v1 contract."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.contract_check_rollout import GenerationCheckRollout, run_contract_check
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.length_check_observation import emit_length_check_observation
from app.services.length_policy import (
    DurationBound,
    LengthOwner,
    ceiling_miss_message,
    creator_duration_satisfied,
    length_owner,
    natural_length_owner,
)

LENGTH_POLICY_FIELD = "creator_length_policy"
LENGTH_POLICIES_FIELD = "creator_length_policies"
# KRI-847: contract digest -> "at_most", written only for a ceiling ("at most 10 seconds").
# A sibling key, not a field of the stored state or of the contract: both forbid extra keys,
# so an older worker could not read a job that carried one there.
LENGTH_BOUND_FIELD = "creator_length_bounds"
DURATION_CHECK = "creator_duration"


class LengthPolicyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal[1] = 1
    generation_id: str
    contract_digest: str
    owner: LengthOwner
    natural_owner: LengthOwner
    requested_duration_s: float | None
    check_mode: Literal["log_only", "enforce"] = "log_only"
    # KRI-847: excluded from the dump (see ``LENGTH_BOUND_FIELD``); set by ``read_length_policy``.
    requested_duration_bound: DurationBound = Field(default="exact", exclude=True)

    def verify_binding(self, contract: Any) -> None:
        if (
            self.generation_id != contract.generation_id
            or self.contract_digest != contract.digest
            or self.requested_duration_s != contract.duration_s
            or (self.owner == "creator") != (contract.duration_s is not None)
        ):
            raise CreatorRenderContractError(
                "This edit's length policy belongs to a different approved revision."
            )


def stamp_length_policy(
    assembly: Mapping[str, Any],
    strategy: Mapping[str, Any],
    contract: Any,
    *,
    mode: str,
    duration_bound: DurationBound = "exact",
) -> dict[str, Any]:
    state = LengthPolicyState(
        generation_id=contract.generation_id,
        contract_digest=contract.digest,
        owner=length_owner(strategy, creator_duration_proven=contract.duration_s is not None),
        natural_owner=natural_length_owner(strategy),
        requested_duration_s=contract.duration_s,
        check_mode=mode,
    )
    payload = state.model_dump(mode="json")
    policies = dict(assembly.get(LENGTH_POLICIES_FIELD) or {})
    policies[contract.digest] = payload
    stamped = {**assembly, LENGTH_POLICY_FIELD: payload, LENGTH_POLICIES_FIELD: policies}
    if duration_bound == "at_most":
        bounds = dict(assembly.get(LENGTH_BOUND_FIELD) or {})
        bounds[contract.digest] = duration_bound
        stamped[LENGTH_BOUND_FIELD] = bounds
    return stamped


def read_length_policy(assembly: Mapping[str, Any], contract: Any) -> LengthPolicyState | None:
    raw = assembly.get(LENGTH_POLICY_FIELD)
    if raw is None:
        return None  # Stored contracts keep their existing validation behavior.
    if isinstance(raw, Mapping) and raw.get("contract_digest") != contract.digest:
        raw = (assembly.get(LENGTH_POLICIES_FIELD) or {}).get(contract.digest)
    try:
        state = LengthPolicyState.model_validate(raw)
    except ValueError as exc:
        raise CreatorRenderContractError(
            "This edit's approved length policy is unreadable."
        ) from exc
    state.verify_binding(contract)
    bounds = assembly.get(LENGTH_BOUND_FIELD)
    if isinstance(bounds, Mapping) and bounds.get(contract.digest) == "at_most":
        state = state.model_copy(update={"requested_duration_bound": "at_most"})
    return state


def check_creator_duration(
    contract: Any,
    actual_duration_s: float,
    *,
    state: LengthPolicyState | None,
    decline_type: type[Exception] = CreatorRenderContractError,
) -> dict[str, Any] | None:
    """Only creator-proven timing is a promise; derived timing is a receipt note."""
    if state is not None:
        state.verify_binding(contract)
    if contract.duration_s is None:
        return None
    requested = contract.duration_s
    # KRI-847: without a state (an older job) the length is exact, as it always was.
    bound = state.requested_duration_bound if state is not None else "exact"

    def verify() -> None:
        if not math.isfinite(actual_duration_s) or actual_duration_s <= 0:
            raise decline_type("This export has no valid measured duration.")
        if not creator_duration_satisfied(actual_duration_s, requested, bound):
            if bound == "at_most":
                raise decline_type(ceiling_miss_message(requested, actual_duration_s))
            raise decline_type(
                f"You asked for {requested:g} seconds; this edit is {actual_duration_s:g} seconds."
            )

    if state is None:
        verify()
        return None
    result = run_contract_check(
        DURATION_CHECK,
        verify,
        rollout=GenerationCheckRollout(
            generation_id=state.generation_id,
            approved=True,
            modes={DURATION_CHECK: state.check_mode},
        ),
        decline_exceptions=(decline_type,),
    )
    observation = {
        "check": DURATION_CHECK,
        "generation_id": state.generation_id,
        "contract_digest": state.contract_digest,
        "length_owner": state.owner,
        "requested_duration_s": requested,
        "actual_duration_s": actual_duration_s,
        "mode": result.mode,
        "outcome": result.outcome,
        "blocking": result.blocking,
        **({"requested_duration_bound": bound} if bound == "at_most" else {}),
    }
    emit_length_check_observation(check_mode=result.mode, outcome=result.outcome)
    if result.blocking:
        raise decline_type(result.decline_message)
    return observation
