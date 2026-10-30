"""Receipt evidence minted only by verification of a complete compiled order."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.kria.order_replacement import OrderReplacementProof, verify_compiled_order

if TYPE_CHECKING:
    from app.kria.brief import CreativeBrief
    from app.kria.brief_checks import PlanFacts
    from app.kria.order_replacement_sources import OrderReplacementSourceContext
    from app.kria.prepared_execution import SourceIdentity
    from app.services.phone_sources import PhoneSourceBinding


@dataclass(frozen=True, init=False)
class VerifiedOrderExecution:
    _proof_json: str = field(repr=False)

    def __init__(self) -> None:
        raise TypeError("Use verify_order_execution with current trusted source context")

    def matches(self, proof: OrderReplacementProof) -> bool:
        return self._proof_json == proof.model_dump_json()


def verify_order_execution(
    proof: OrderReplacementProof,
    execution: Mapping[str, Any],
    *,
    sources: Sequence["SourceIdentity"],
    phone_bindings: Sequence["PhoneSourceBinding"] = (),
) -> VerifiedOrderExecution:
    """Caller supplies current trusted sources and bind_phone_sources output, never model data.

    The caller must use this result only with facts from this same execution artifact.
    Parsing a proof or providing a strategy field alone cannot mint this evidence.
    """
    proof = OrderReplacementProof.model_validate(proof.model_dump(mode="json"))
    verify_compiled_order(proof, execution, sources=sources, phone_bindings=phone_bindings)
    evidence = object.__new__(VerifiedOrderExecution)
    object.__setattr__(evidence, "_proof_json", proof.model_dump_json())
    return evidence


def with_verified_order_execution(
    facts: "PlanFacts",
    *,
    brief: "CreativeBrief | None",
    strategy: Mapping[str, Any] | None,
    execution: Mapping[str, Any] | None,
    source_context: "OrderReplacementSourceContext | None",
    creator_id: object,
) -> "PlanFacts":
    """Attach evidence only for matching canonical/strategy proofs and the current actor.

    Callers load source_context from the actor-owned item and pass the actual compiled
    artifact used for these facts. Missing or invalid context leaves a typed request partial.
    """
    from dataclasses import replace

    from app.kria.order_replacement_sources import OrderReplacementSourceContext

    proofs = (
        [req.order_replacement for req in brief.live() if req.order_replacement] if brief else []
    )
    if not proofs:
        return facts
    unverified = replace(facts, verified_order_execution=None)
    if (
        len(proofs) != 1
        or not isinstance(source_context, OrderReplacementSourceContext)
        or creator_id is None
        or source_context.creator_id != str(creator_id)
        or not isinstance(execution, Mapping)
        or not isinstance(strategy, Mapping)
    ):
        return unverified
    try:
        strategy_proof = OrderReplacementProof.model_validate(
            strategy.get("confirmed_order_replacement")
        )
        if strategy_proof != proofs[0]:
            return unverified
        verified = verify_order_execution(
            proofs[0],
            execution,
            sources=source_context.sources,
            phone_bindings=source_context.phone_bindings,
        )
    except ValueError:
        return unverified
    return replace(facts, verified_order_execution=verified)


def compiled_order_preflight_sync(
    db: Any,
    *,
    item: Any,
    creator_id: object,
    brief: "CreativeBrief | None",
    strategy: Mapping[str, Any] | None,
    media_snapshot: Mapping[str, Any] | None,
    client_protocol: int | None,
) -> dict[str, Any] | None:
    """Compile the actual phone worker plan and check it against the canonical replacement.

    Only typed replacements run this reader. No provider, storage call or write is used.
    Expected unsupported-plan errors return no artifact; programming/DB errors propagate.
    """
    proofs = (
        [req.order_replacement for req in brief.live() if req.order_replacement] if brief else []
    )
    if len(proofs) != 1 or not isinstance(strategy, Mapping):
        return None
    try:
        strategy_proof = OrderReplacementProof.model_validate(
            strategy.get("confirmed_order_replacement")
        )
    except ValueError:
        return None
    if strategy_proof != proofs[0]:
        return None

    from app.kria.order_replacement_sources import load_brief_order_source_context_sync
    from app.pipeline.guided_story import GuidedStoryError, compile_execution_plan
    from app.services.phone_montage_preflight import (
        MontagePreflightSkip,
        _ready_visuals,
        preflight_montage_plan,
    )

    context = load_brief_order_source_context_sync(
        db,
        brief=brief,
        item_id=item.id,
        creator_id=creator_id,
    )
    if context is None:
        return None
    try:
        planned = preflight_montage_plan(
            creator_id=creator_id,
            edit_format=item.edit_format,
            brief=brief,
            strategy=strategy,
            media_snapshot=media_snapshot,
            visuals=lambda: _ready_visuals(db, item=item, user_id=creator_id, strategy=strategy),
            client_protocol=client_protocol,
        )
        if planned is None or planned is MontagePreflightSkip.OTHER_WRITER:
            return None
        plan, _aliases = planned
        execution = compile_execution_plan(plan.guided_edit(), track=None)
    except (ValueError, GuidedStoryError):
        return None
    try:
        verify_compiled_order(
            proofs[0],
            execution,
            sources=context.sources,
            phone_bindings=context.phone_bindings,
        )
    except ValueError:
        return None
    return execution
