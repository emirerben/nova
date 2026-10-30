"""Readers for an explicit creator replacement of one global order requirement.

These records are server-owned. Parsing proves shape, not creator authority or
named-action meaning. Producers must use checked moment evidence and authoritative
question/answer events. Consumers verify the full compiled timeline independently.
No brief, planner, persistence or provider imports belong here.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from app.schemas.order_replacement import (
    CheckedOrderMoment as CheckedOrderMoment,
)
from app.schemas.order_replacement import (
    OrderReplacementProof as OrderReplacementProof,
)
from app.schemas.order_replacement import (
    OrderReplacementProposal as OrderReplacementProposal,
)

if TYPE_CHECKING:
    from app.kria.prepared_execution import SourceIdentity
    from app.services.phone_sources import PhoneSourceBinding


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def source_fence_digest(source: SourceIdentity) -> str:
    """Reuse prepared execution's fence, including role, trim and storage generation."""
    return _digest(source.fence())


def phone_binding_digest(binding: PhoneSourceBinding) -> str:
    """Pin bind_phone_sources' verified original identity and v1 original-clock contract.

    Call only on a binding returned by bind_phone_sources from server-owned
    assignments. Constructing or parsing a PhoneSourceBinding is not verification.
    """
    return _digest(
        {
            "timing_version": 1,
            "binding": binding.model_dump(mode="json"),
            "render_asset": binding.render_asset().model_dump(mode="json"),
        }
    )


def _validate_sources(
    moments: Sequence[CheckedOrderMoment],
    sources: Sequence[SourceIdentity],
    phone_bindings: Sequence[PhoneSourceBinding] = (),
) -> None:
    by_slot = {source.slot: source for source in sources}
    if len(by_slot) != len(sources):
        raise ValueError("Order source slots must be unique")
    for moment in moments:
        source = by_slot.get(moment.source_slot)
        if (
            source is None
            or source.media_id != moment.media_id
            or source.path != moment.gcs_path
            or source.generation != moment.generation
            or source_fence_digest(source) != moment.source_fence_digest
        ):
            raise ValueError("Order replacement source identity changed")
        duration = source.duration_s
        if source.role == "analysis_proxy":
            from app.services.phone_sources import require_bound_moment

            if not phone_bindings or moment.phone_binding_digest is None:
                raise ValueError("Order replacement requires a verified phone binding")
            binding = require_bound_moment(
                tuple(phone_bindings),
                media_id=moment.media_id,
                path=moment.gcs_path,
                generation=moment.generation,
            )
            if (
                phone_binding_digest(binding) != moment.phone_binding_digest
                or binding.original.kind != moment.kind
            ):
                raise ValueError("Order replacement original identity or timing changed")
            duration = min(duration, binding.original.duration_s) if duration is not None else None
        elif source.role != "original" or moment.phone_binding_digest is not None:
            raise ValueError("Order replacement source role disagrees with its binding")
        if moment.kind == "video":
            start, end = moment.source_start_s, moment.source_end_s
            if (
                duration is None
                or not math.isfinite(duration)
                or start < (source.trim_start_s or 0)
                or end > duration
                or (source.trim_end_s is not None and end > source.trim_end_s)
            ):
                raise ValueError("Order replacement window exceeds the current source")


def validate_proposal(
    proposal: OrderReplacementProposal,
    *,
    predecessor_requirement_id: str,
    predecessor_brief_version: int,
    predecessor_requirement_digest: str,
    checked_moments: Sequence[CheckedOrderMoment],
    sources: Sequence[SourceIdentity],
    rejection_question_id: str,
    rejection_category_key: str,
    rejection_selection_sequence: int,
    rejected_intent_id: str,
    phone_bindings: Sequence[PhoneSourceBinding] = (),
) -> None:
    """Compare an offer against current server evidence, never a model's completeness flag."""
    current = (
        predecessor_requirement_id,
        predecessor_brief_version,
        predecessor_requirement_digest,
        rejection_question_id,
        rejection_category_key,
        rejection_selection_sequence,
        rejected_intent_id,
    )
    expected = (
        proposal.predecessor_requirement_id,
        proposal.predecessor_brief_version,
        proposal.predecessor_requirement_digest,
        proposal.rejection_question_id,
        proposal.rejection_category_key,
        proposal.rejection_selection_sequence,
        proposal.rejected_intent_id,
    )
    if current != expected:
        raise ValueError("Order replacement predecessor or rejection changed")
    if tuple(checked_moments) != proposal.moments:
        raise ValueError("Order replacement full checked sequence changed")
    _validate_sources(proposal.moments, sources, phone_bindings)


def validate_confirmation(
    proposal: OrderReplacementProposal,
    *,
    question: Mapping[str, Any],
    selection: Mapping[str, Any],
    question_sequence: int,
    selection_sequence: int,
    **current: Any,
) -> OrderReplacementProof:
    """Use saved assistant question and explicit user selection from authoritative events.

    The caller must select the current answer, reject superseding events and supply
    actual event sequence numbers. This pure reader does not fetch conversation history.
    """
    validate_proposal(proposal, **current)
    if (
        question.get("kind") != "order_replacement"
        or question.get("input_digest") != proposal.input_digest
        or selection.get("question_id") != question.get("question_id")
        or selection.get("option_key") != "accept_sequence"
        or selection.get("delegated") is True
        or not any(
            isinstance(option, Mapping) and option.get("key") == "accept_sequence"
            for option in question.get("options") or ()
        )
    ):
        raise ValueError("Order replacement needs the exact explicit offered choice")
    return OrderReplacementProof(
        proposal=proposal,
        question_id=question.get("question_id"),
        question_input_digest=question.get("input_digest"),
        question_sequence=question_sequence,
        selection_sequence=selection_sequence,
    )


def _compiled_media_id(moment: CheckedOrderMoment, row: Mapping[str, Any]) -> str:
    if not moment.source_slot.startswith("visual:"):
        return moment.media_id
    asset_id = uuid.UUID(moment.source_slot.removeprefix("visual:"))
    if (
        moment.source_slot != f"visual:{asset_id}"
        or moment.media_id != f"asset-{asset_id}"
        or row.get("lane") != "asset"
    ):
        raise ValueError("Compiled Visuals source does not match its canonical asset")
    return str(asset_id)


def verify_compiled_order(
    proof: OrderReplacementProof,
    execution: Mapping[str, Any],
    *,
    sources: Sequence[SourceIdentity],
    phone_bindings: Sequence[PhoneSourceBinding] = (),
) -> None:
    """Verify the complete real compiler story_timeline, without filtering or row pruning.

    occurrence_id is the compiler's moment_id, bound before offering the question.
    A photo's synthetic source clock represents its hold, not a video interval.
    Output intervals, rate and freeze holds are independently pinned. They need
    not equal source duration because the compiler supports retiming/transitions.
    Proxy sources additionally require current bind_phone_sources output from
    authoritative stored assignments. SourceIdentity alone cannot authorize them.
    The binding's original clock bounds windows and its native asset fingerprint
    must still match the one the creator confirmed.
    """
    proof = OrderReplacementProof.model_validate(proof.model_dump(mode="json"))
    _validate_sources(proof.proposal.moments, sources, phone_bindings)
    rows = execution.get("story_timeline")
    if not isinstance(rows, list) or len(rows) != len(proof.proposal.moments):
        raise ValueError("Compiled order does not contain the full confirmed sequence")
    previous_start = -1.0
    for moment, row in zip(proof.proposal.moments, rows, strict=True):
        if not isinstance(row, Mapping):
            raise ValueError("Compiled order row is invalid")
        expected = (
            moment.occurrence_id,
            _compiled_media_id(moment, row),
            moment.kind,
            moment.gcs_path,
            moment.generation,
        )
        actual = tuple(
            row.get(key) for key in ("moment_id", "media_id", "kind", "gcs_path", "generation")
        )
        if actual != expected:
            raise ValueError("Compiled order occurrence or source changed")
        start, end = row.get("output_start_s"), row.get("output_end_s")
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not (0 <= start < end < float("inf"))
            or start <= previous_start
        ):
            raise ValueError("Compiled output sequence is invalid")
        previous_start = start
        timing_keys = ("output_start_s", "output_end_s", "duration_s")
        actual_timing = tuple(row.get(key) for key in timing_keys)
        expected_timing = (moment.output_start_s, moment.output_end_s, moment.duration_s)
        rate = row.get("playback_rate")
        rate = 1.0 if rate is None else rate
        hold = row.get("freeze_hold_s")
        hold = 0.0 if hold is None else hold
        if (
            any(type(value) not in (int, float) for value in (*actual_timing, rate, hold))
            or actual_timing != expected_timing
            or rate != moment.playback_rate
            or hold != moment.freeze_hold_s
        ):
            raise ValueError("Compiled order output timing changed")
        expected_window = (
            (moment.source_start_s, moment.source_end_s)
            if moment.kind == "video"
            else (0.0, moment.hold_duration_s)
        )
        actual_window = (row.get("source_start_s"), row.get("source_end_s"))
        if any(type(value) not in (int, float) for value in actual_window):
            raise ValueError("Compiled source window is invalid")
        if actual_window != expected_window:
            raise ValueError("Compiled order source window changed")
        if moment.kind == "image" and row.get("duration_s") != moment.hold_duration_s:
            raise ValueError("Compiled photo hold changed")
