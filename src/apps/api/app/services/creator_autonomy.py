"""Deterministic, fail-open policy for one Main Creator auto-revision.

This module is intentionally renderer- and storage-free.  It converts a
bounded objective review receipt into an existing typed craft command only
when every server threshold and identity fence is satisfied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.agents._schemas.creator_agent import (
    ApplySpeechCutCommand,
    CreatorAutomationDecision,
    CreatorCraftBundle,
    CreatorCraftCommand,
    CreatorRevisionProposal,
    CreatorTargetPin,
    RemoveOptionalTreatmentCommand,
    SetCaptionStyleCommand,
    SetTransitionCommand,
)

MIN_CONFIDENCE = 0.85
MAX_QUALITY = 4.0
MIN_EXPECTED_IMPROVEMENT = 0.5
MAX_AUTOMATIC_REVISIONS = 1
OBJECTIVE_TAG = "objective_quality"
ALLOWLIST = frozenset(
    {"transition_fallback", "caption_legibility", "remove_optional_treatment", "speech_cut"}
)


def _skip(review: Mapping[str, Any], reason: str, *, opted_in: bool, remaining: int = 0):
    mode = str(review.get("review_mode") or review.get("mode") or "objective")
    if mode not in {"objective", "taste", "mixed"}:
        mode = "objective"
    return CreatorAutomationDecision(
        decision="skip",
        reason_code=reason,
        review_generation_id=str(
            review.get("render_generation_id") or review.get("generation_id") or "unknown"
        ),
        opted_in=opted_in,
        review_mode=mode,
        confidence=float(review.get("confidence") or 0.0),
        current_quality=review.get("quality_score"),
        expected_improvement=review.get("expected_improvement"),
        render_budget_remaining=max(0, remaining),
        automatic_revision_count=int(review.get("automatic_revision_count") or 0),
        allowlist_action=(
            review.get("allowlist_action")
            if isinstance(review.get("allowlist_action"), str)
            and review.get("allowlist_action") in ALLOWLIST
            else None
        ),
    )


def evaluate_auto_iteration(
    review: Mapping[str, Any] | None,
    *,
    opted_in: bool,
    render_budget_remaining: int,
    automatic_revision_count: int,
) -> CreatorAutomationDecision:
    """Evaluate policy gates without guessing at missing review evidence."""

    receipt = review if isinstance(review, Mapping) else {}
    if not opted_in:
        return _skip(
            receipt, "session_opt_in_required", opted_in=False, remaining=render_budget_remaining
        )
    if receipt.get("status") != "complete":
        return _skip(
            receipt, "review_not_complete", opted_in=True, remaining=render_budget_remaining
        )
    if str(receipt.get("review_mode") or "") != "objective":
        return _skip(
            receipt, "objective_review_required", opted_in=True, remaining=render_budget_remaining
        )
    confidence = receipt.get("confidence")
    if confidence is None or float(confidence) < MIN_CONFIDENCE:
        return _skip(
            receipt,
            "confidence_below_threshold",
            opted_in=True,
            remaining=render_budget_remaining,
        )
    quality = receipt.get("quality_score")
    if quality is None or float(quality) >= MAX_QUALITY:
        return _skip(
            receipt,
            "quality_already_sufficient",
            opted_in=True,
            remaining=render_budget_remaining,
        )
    expected = receipt.get("expected_improvement")
    if expected is None or float(expected) < MIN_EXPECTED_IMPROVEMENT:
        return _skip(
            receipt,
            "expected_improvement_below_threshold",
            opted_in=True,
            remaining=render_budget_remaining,
        )
    if render_budget_remaining <= 0:
        return _skip(receipt, "render_budget_exhausted", opted_in=True, remaining=0)
    if automatic_revision_count >= MAX_AUTOMATIC_REVISIONS:
        return _skip(
            receipt,
            "automatic_revision_cap_reached",
            opted_in=True,
            remaining=render_budget_remaining,
        )
    if receipt.get("objective_tag") != OBJECTIVE_TAG:
        return _skip(
            receipt, "objective_tag_required", opted_in=True, remaining=render_budget_remaining
        )
    action = str(receipt.get("allowlist_action") or "")
    if action not in ALLOWLIST:
        return _skip(
            receipt, "treatment_not_allowlisted", opted_in=True, remaining=render_budget_remaining
        )
    return CreatorAutomationDecision(
        decision="eligible",
        reason_code="bounded_objective_revision",
        review_generation_id=str(
            receipt.get("render_generation_id") or receipt.get("generation_id")
        ),
        opted_in=True,
        review_mode="objective",
        confidence=float(confidence),
        current_quality=float(quality),
        expected_improvement=float(expected),
        render_budget_remaining=render_budget_remaining,
        automatic_revision_count=automatic_revision_count,
        allowlist_action=action,
        proposed_revision=CreatorRevisionProposal(
            revision_id=str(
                receipt.get("proposed_revision", {}).get("revision_id") or "auto-revision"
            ),
            summary=str(
                receipt.get("proposed_revision", {}).get("summary")
                or "Apply one bounded objective correction."
            ),
            rationale=str(
                receipt.get("proposed_revision", {}).get("rationale")
                or "Objective review identified a correctable quality issue."
            ),
            evidence_ids=list(receipt.get("proposed_revision", {}).get("evidence_ids") or []),
        ),
        # The concrete command is built only after the route has loaded the
        # exact current variant and can prove the command is applicable.
        command=None,
    )


def build_auto_command(
    action: str,
    *,
    pin: Mapping[str, Any],
    review: Mapping[str, Any],
    variant: Mapping[str, Any],
) -> CreatorCraftCommand:
    """Build one existing craft command from review evidence and current state."""

    target = CreatorTargetPin.model_validate(pin).model_dump()
    if action == "caption_legibility":
        return SetCaptionStyleCommand(
            **target, command="set_caption_style", caption_style="sentence"
        )
    if action == "transition_fallback":
        slots = (
            variant.get("user_timeline", {}).get("slots")
            or variant.get("ai_timeline", {}).get("slots")
            or []
        )
        boundary = review.get("boundary_index")
        if not isinstance(boundary, int) or isinstance(boundary, bool):
            raise ValueError("reviewed_boundary_required")
        if boundary < 0 or boundary >= len(slots) - 1:
            raise ValueError("reviewed_boundary_not_present")
        return SetTransitionCommand(
            **target,
            command="set_transition",
            boundary_index=int(boundary),
            transition="none",
            duration_s=0.0,
        )
    if action == "remove_optional_treatment":
        treatment = str(review.get("treatment") or review.get("optional_treatment") or "")
        if treatment not in {"media_overlay", "sfx"}:
            raise ValueError("optional_treatment_not_allowlisted")
        source = (
            variant.get("media_overlays")
            if treatment == "media_overlay"
            else variant.get("sound_effects")
        )
        source = [value for value in (source or []) if isinstance(value, Mapping)]
        treatment_id = review.get("treatment_id")
        if not source or not treatment_id:
            raise ValueError("reviewed_treatment_required")
        if not any(
            str(value.get("id") or value.get("asset_id") or value.get("sound_effect_id") or "")
            == str(treatment_id)
            for value in source
        ):
            raise ValueError("reviewed_treatment_not_present")
        return RemoveOptionalTreatmentCommand(
            **target,
            command="remove_optional_treatment",
            treatment=treatment,
            treatment_id=str(treatment_id),
        )
    if action == "speech_cut":
        candidate_id = review.get("candidate_id")
        candidates = [
            value
            for value in (variant.get("speech_cut_candidates") or [])
            if isinstance(value, Mapping)
        ]
        candidate = next(
            (value for value in candidates if value.get("candidate_id") == candidate_id), None
        )
        if candidate is None or candidate.get("status") != "pending":
            raise ValueError("speech_cut_candidate_not_validated")
        source = str(candidate.get("source") or "")
        if source not in {"retake_review", "silence_review", "filler_review"}:
            raise ValueError("speech_cut_candidate_not_allowlisted")
        revision = (
            candidate.get("revision")
            or candidate.get("candidate_revision")
            or review.get("expected_cut_revision")
        )
        if not revision:
            raise ValueError("speech_cut_revision_missing")
        return ApplySpeechCutCommand(
            **target,
            command="apply_speech_cut",
            candidate_id=str(candidate_id),
            expected_cut_revision=str(revision),
        )
    raise ValueError("treatment_not_allowlisted")


def build_auto_bundle(
    *,
    session_id: str,
    idempotency_key: str,
    pin: Mapping[str, Any],
    action: str,
    review: Mapping[str, Any],
    variant: Mapping[str, Any],
) -> CreatorCraftBundle:
    """Build the exact bounded request persisted before publishing work."""

    command = build_auto_command(action, pin=pin, review=review, variant=variant)
    return CreatorCraftBundle(
        session_id=session_id,
        idempotency_key=idempotency_key,
        commands=[command],
        **pin,
    )


def recover_auto_bundle(
    raw_bundle: Any,
    *,
    session_id: str,
    idempotency_key: str,
    job_id: str,
    variant_id: str,
    generation_id: str,
    ownership_epoch: int,
) -> CreatorCraftBundle:
    """Validate a persisted request without rebuilding against mutable state."""

    try:
        bundle = CreatorCraftBundle.model_validate(raw_bundle)
    except ValueError as exc:
        raise ValueError("automatic_revision_receipt_invalid") from exc
    if (
        bundle.session_id != session_id
        or bundle.idempotency_key != idempotency_key
        or bundle.expected_job_id != job_id
        or bundle.expected_variant_id != variant_id
        or bundle.expected_generation_id != generation_id
        or bundle.expected_ownership_epoch != ownership_epoch
    ):
        raise ValueError("automatic_revision_receipt_stale")
    return bundle


__all__ = [
    "ALLOWLIST",
    "MAX_AUTOMATIC_REVISIONS",
    "MIN_CONFIDENCE",
    "MIN_EXPECTED_IMPROVEMENT",
    "OBJECTIVE_TAG",
    "build_auto_command",
    "build_auto_bundle",
    "evaluate_auto_iteration",
    "recover_auto_bundle",
]
