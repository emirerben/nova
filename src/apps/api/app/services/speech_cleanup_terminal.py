"""Private staging and ownership fences for required speech-cleanup renders."""

from __future__ import annotations

import copy
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.services.durable_attempt_cleanup import (
    RENDER_GENERATION_CLEANUP_FIELD,
    CleanupReceiptLocator,
    mark_cleanup_receipt_closed,
    remove_cleanup_receipt,
    render_generation_prefix,
    reserve_render_generation_cleanup,
)
from app.services.variant_generation_guard import (
    PRIVATE_SPEECH_CLEANUP_KEY,
    REQUIRED_SPEECH_LOCKS_KEY,
)

STAGED_RENDER_RESULTS_KEY = "staged_render_results"
WORKING_RENDER_VARIANTS_KEY = "working_render_variants"
TERMINAL_PENDING_KEY = "terminal_pending"
STAGED_RENDER_RESULTS_CAP = 16
TERMINAL_PENDING_CAP = 64
REQUIRED_SPEECH_CLAIM_TTL_S = 1810.0

_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_CELERY_UUID_TASK_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CREATOR_SPEECH_TASK_ID_RE = re.compile(
    r"^creator-craft-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}-(?P<generation>[0-9a-f]{32})$"
)
_RESUME_ANALYSIS_VIEWS = frozenset({"full_clip", "talking_head_spine_capped"})
_RESUME_ARTIFACT_FIELDS = frozenset(
    {
        "video_path",
        "base_video_path",
        "poster_path",
        "base_poster_path",
        "pre_media_overlay_video_path",
        "pre_overlay_poster_path",
        "pre_sfx_video_path",
        "subject_matte_path",
        "visual_blocks_base_path",
        "motion_base_path",
        "motion_base_source_path",
        "camera_base_path",
    }
)

TerminalizationStatus = Literal["unchanged", "terminalized", "blocked"]
RequiredSpeechResumeStatus = Literal["absent", "resumable", "rotate", "blocked"]
RequiredSpeechClaimStatus = Literal[
    "absent",
    "fresh",
    "released",
    "expired",
    "same_task_retry",
    "malformed",
]
RouteSpeechCutRollbackDisposition = Literal[
    "eligible",
    "not_owned",
    "enqueue_uncertain",
]


@dataclass(frozen=True)
class RequiredSpeechTerminalization:
    """Pure recovery result for abandoned required-speech generations.

    ``blocked`` deliberately carries the original plan.  Recovery must never
    partially clear an ownership fence when its cleanup receipt, rollback
    snapshot, or generation correlation cannot be proved.
    """

    status: TerminalizationStatus
    plan: dict[str, Any]
    terminalized_count: int = 0
    restored_last_good: bool = False
    terminal_contexts: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class RequiredSpeechResume:
    """Exact, typed decision for one initial required-speech retry."""

    status: RequiredSpeechResumeStatus
    generation: str | None = None
    staged_result: dict[str, Any] | None = None
    reason: str | None = None
    retry_after_s: float | None = None


@dataclass(frozen=True)
class RequiredSpeechClaimDisposition:
    """Shared finalizer-claim lease interpretation used by claimers and reapers."""

    status: RequiredSpeechClaimStatus

    @property
    def recoverable(self) -> bool:
        return self.status in {"absent", "released", "expired", "same_task_retry"}


class RequiredSpeechOwnershipError(RuntimeError):
    """A generation no longer owns the private stage/public pending row."""


class RequiredSpeechStageBackpressure(RequiredSpeechOwnershipError):
    """The bounded private stage has no safe capacity."""


def classify_required_speech_claim(
    claim: object,
    *,
    now_epoch_s: float | None = None,
    task_id: str | None = None,
    retry_number: int = 0,
    ttl_s: float = REQUIRED_SPEECH_CLAIM_TTL_S,
) -> RequiredSpeechClaimDisposition:
    """Interpret a finalizer claim without treating malformed time as expiry.

    A normal recovery may take over only an explicitly released or provably
    expired claim.  Acquisition additionally has the broker's same-task/newer-
    retry proof, which cannot be fabricated by a stale-job sweeper.
    """

    if claim in (None, {}):
        return RequiredSpeechClaimDisposition("absent")
    if not isinstance(claim, dict):
        return RequiredSpeechClaimDisposition("malformed")
    attempt_id = claim.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return RequiredSpeechClaimDisposition("malformed")
    if claim.get("released") is True:
        return RequiredSpeechClaimDisposition("released")
    try:
        claimed_at = float(claim["claimed_at_epoch_s"])
        now = time.time() if now_epoch_s is None else float(now_epoch_s)
        ttl = float(ttl_s)
    except (KeyError, TypeError, ValueError, OverflowError):
        return RequiredSpeechClaimDisposition("malformed")
    if not (claimed_at >= 0.0 and now >= 0.0 and ttl > 0.0):
        return RequiredSpeechClaimDisposition("malformed")
    try:
        claimed_retry_number = int(claim.get("retry_number") or 0)
    except (TypeError, ValueError, OverflowError):
        return RequiredSpeechClaimDisposition("malformed")
    if task_id and claim.get("task_id") == task_id and retry_number > claimed_retry_number:
        return RequiredSpeechClaimDisposition("same_task_retry")
    if now - claimed_at >= ttl:
        return RequiredSpeechClaimDisposition("expired")
    return RequiredSpeechClaimDisposition("fresh")


def active_speech_claim_task_id(
    plan: object,
    *,
    now_epoch_s: float | None = None,
) -> str | None:
    """Return only a structurally proven live speech-worker task identity.

    The private assembly-plan namespace is durable but is not itself authority
    to send Celery control messages. Correlate every claim field with the
    route-owned control and the worker's deterministic attempt shape before a
    cancellation or erasure workflow may revoke it.
    """

    if not isinstance(plan, dict):
        return None
    control = plan.get("speech_cut_control")
    if not isinstance(control, dict):
        return None
    operation_id = control.get("operation_id")
    generation = control.get("render_generation_id")
    variant_id = control.get("variant_id")
    if (
        not isinstance(operation_id, str)
        or not _GENERATION_RE.fullmatch(operation_id)
        or not isinstance(generation, str)
        or not _GENERATION_RE.fullmatch(generation)
        or not isinstance(variant_id, str)
        or not variant_id
        or len(variant_id) > 128
    ):
        return None
    claim = control.get("finalizer_claim")
    if not isinstance(claim, dict):
        return None
    task_id = claim.get("task_id")
    retry_number = claim.get("retry_number")
    attempt_id = claim.get("attempt_id")
    try:
        claimed_at = float(claim.get("claimed_at_epoch_s"))
        now = time.time() if now_epoch_s is None else float(now_epoch_s)
    except (TypeError, ValueError, OverflowError):
        return None
    creator_match = (
        _CREATOR_SPEECH_TASK_ID_RE.fullmatch(task_id) if isinstance(task_id, str) else None
    )
    task_id_matches = bool(
        isinstance(task_id, str)
        and (
            _CELERY_UUID_TASK_ID_RE.fullmatch(task_id)
            or (creator_match and creator_match.group("generation") == generation)
        )
    )
    if (
        claim.get("operation_id") != operation_id
        or claim.get("render_generation_id") != generation
        or not task_id_matches
        or isinstance(retry_number, bool)
        or not isinstance(retry_number, int)
        or retry_number < 0
        or not isinstance(attempt_id, str)
        or len(attempt_id) > 512
        or not attempt_id.startswith(f"{task_id}:{retry_number}:")
        or not _GENERATION_RE.fullmatch(attempt_id.rsplit(":", 1)[-1])
        or not math.isfinite(claimed_at)
        or not math.isfinite(now)
        or claimed_at < 0.0
        or now < 0.0
        or claimed_at > now + 60.0
        or classify_required_speech_claim(claim, now_epoch_s=now).status != "fresh"
    ):
        return None
    return task_id


def classify_route_speech_cut_rollback(
    plan: object,
    *,
    variant_id: str,
    operation_id: str,
    generation: str,
) -> RouteSpeechCutRollbackDisposition:
    """Prove a failed enqueue still owns an entirely unclaimed route commit.

    Broker publication is not an atomic request/response boundary: a task may
    be delivered, claim the control, and reserve private upload state before
    ``apply_async`` reports an error.  A route may therefore restore its
    pre-dispatch snapshot only while the freshly locked row still contains its
    exact operation/generation and has no evidence of worker adoption.

    ``not_owned`` is reserved for a missing or superseding control.  An exact
    control with any non-null (including malformed) claim or matching private
    owner is ``enqueue_uncertain`` and must be left for the worker/terminalizer.
    Malformed private containers also fail closed.
    """

    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 128
        for value in (variant_id, operation_id, generation)
    ):
        return "enqueue_uncertain"
    if not isinstance(plan, dict):
        return "enqueue_uncertain"
    control = plan.get("speech_cut_control")
    if control in (None, {}):
        # The broker may have delivered and the worker may have completed the
        # entire private-generation publication before ``apply_async`` reports
        # its lost acknowledgement.  Required-v1 keeps the public row on the
        # old generation until that final transaction, so a unique target row
        # carrying our freshly minted generation is positive delivery evidence.
        # There is no route-owned control left to roll back; keep creator
        # receipts resumable and surface the enqueue outcome as uncertain.
        variants = plan.get("variants")
        completed_matches = (
            [
                value
                for value in variants
                if isinstance(value, dict)
                and value.get("variant_id") == variant_id
                and value.get("render_generation_id") == generation
            ]
            if isinstance(variants, list)
            else []
        )
        if len(completed_matches) == 1:
            return "enqueue_uncertain"
        return "not_owned"
    if not isinstance(control, dict):
        return "enqueue_uncertain"
    if (
        control.get("variant_id") != variant_id
        or control.get("operation_id") != operation_id
        or control.get("render_generation_id") != generation
    ):
        return "not_owned"
    # Only JSON null/missing represents the route-owned pre-adoption state.
    # Empty dictionaries and partial claims are ambiguous worker evidence.
    if control.get("finalizer_claim") is not None:
        return "enqueue_uncertain"

    internal = plan.get(PRIVATE_SPEECH_CLEANUP_KEY)
    if internal is None:
        return "eligible"
    if not isinstance(internal, dict):
        return "enqueue_uncertain"

    locks = internal.get(REQUIRED_SPEECH_LOCKS_KEY)
    if locks is not None:
        if not isinstance(locks, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in locks.items()
        ):
            return "enqueue_uncertain"
        if variant_id in locks or generation in locks.values():
            return "enqueue_uncertain"

    stage_key = f"{variant_id}:{generation}"
    for field in (
        STAGED_RENDER_RESULTS_KEY,
        WORKING_RENDER_VARIANTS_KEY,
        TERMINAL_PENDING_KEY,
    ):
        container = internal.get(field)
        if container is None:
            continue
        if not isinstance(container, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in container.items()
        ):
            return "enqueue_uncertain"
        if stage_key in container or any(
            value.get("variant_id") == variant_id or value.get("render_generation_id") == generation
            for value in container.values()
        ):
            return "enqueue_uncertain"

    receipts = internal.get(RENDER_GENERATION_CLEANUP_FIELD)
    if receipts is not None:
        if not isinstance(receipts, list) or any(
            not isinstance(receipt, dict) for receipt in receipts
        ):
            return "enqueue_uncertain"
        if any(receipt.get("generation") == generation for receipt in receipts):
            return "enqueue_uncertain"
    return "eligible"


def _internal(plan: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    if not isinstance(plan, dict):
        raise RequiredSpeechOwnershipError("assembly_plan_not_object")
    if PRIVATE_SPEECH_CLEANUP_KEY not in plan:
        if not create:
            return None
        plan[PRIVATE_SPEECH_CLEANUP_KEY] = {}
    internal = plan.get(PRIVATE_SPEECH_CLEANUP_KEY)
    if not isinstance(internal, dict):
        raise RequiredSpeechOwnershipError("private_container_malformed")
    return internal


def _locks(plan: dict[str, Any], *, create: bool) -> dict[str, str] | None:
    internal = _internal(plan, create=create)
    if internal is None:
        return None
    if REQUIRED_SPEECH_LOCKS_KEY not in internal:
        if not create:
            return None
        internal[REQUIRED_SPEECH_LOCKS_KEY] = {}
    locks = internal.get(REQUIRED_SPEECH_LOCKS_KEY)
    if not isinstance(locks, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in locks.items()
    ):
        raise RequiredSpeechOwnershipError("generation_locks_malformed")
    return locks


def _stages(plan: dict[str, Any], *, create: bool) -> dict[str, dict[str, Any]] | None:
    internal = _internal(plan, create=create)
    if internal is None:
        return None
    if STAGED_RENDER_RESULTS_KEY not in internal:
        if not create:
            return None
        internal[STAGED_RENDER_RESULTS_KEY] = {}
    stages = internal.get(STAGED_RENDER_RESULTS_KEY)
    if not isinstance(stages, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict) for key, value in stages.items()
    ):
        raise RequiredSpeechOwnershipError("staged_results_malformed")
    return stages


def _terminal_contexts(plan: dict[str, Any], *, create: bool) -> dict[str, dict[str, Any]] | None:
    internal = _internal(plan, create=create)
    if internal is None:
        return None
    if TERMINAL_PENDING_KEY not in internal:
        if not create:
            return None
        internal[TERMINAL_PENDING_KEY] = {}
    contexts = internal.get(TERMINAL_PENDING_KEY)
    if not isinstance(contexts, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict) for key, value in contexts.items()
    ):
        raise RequiredSpeechOwnershipError("terminal_context_malformed")
    return contexts


def _terminal_context_capsule(
    result: dict[str, Any],
    *,
    variant_id: str,
    generation: str,
) -> dict[str, Any] | None:
    """Copy only bounded scalar evidence needed to prove a retry context."""

    context = result.get("_speech_cleanup_outcome_context")
    if context is None:
        return None
    if not isinstance(context, dict):
        return None
    analysis_attempt_id = context.get("analysis_attempt_id")
    analysis_view = context.get("analysis_view")
    detector_version = context.get("detector_version")
    if (
        not isinstance(analysis_attempt_id, str)
        or not analysis_attempt_id
        or len(analysis_attempt_id) > 128
        or analysis_view not in _RESUME_ANALYSIS_VIEWS
        or not isinstance(detector_version, str)
        or not detector_version
        or len(detector_version) > 80
    ):
        return None
    capsule: dict[str, Any] = {
        "variant_id": variant_id,
        "render_generation_id": generation,
        "analysis_attempt_id": analysis_attempt_id,
        "analysis_view": analysis_view,
        "detector_version": detector_version,
    }
    for field in (
        "source_tag",
        "selected_plan",
        "candidate_status",
        "output_removal_count",
        "output_removed_ms",
    ):
        value = context.get(field)
        if isinstance(value, str):
            if len(value) > 128:
                return None
            capsule[field] = value
        elif isinstance(value, int) and 0 <= value <= 86_400_000:
            capsule[field] = value
        elif value is None:
            capsule[field] = None
    return capsule


def _working_variants(plan: dict[str, Any], *, create: bool) -> dict[str, dict[str, Any]] | None:
    """Private in-progress variants for publication-atomic rerenders.

    Initial renders continue to use the public pending row because no last-good
    output exists.  A speech-cut rerender keeps the accepted public row intact
    and advances this generation-owned working copy through render/compose.
    """

    internal = _internal(plan, create=create)
    if internal is None:
        return None
    if WORKING_RENDER_VARIANTS_KEY not in internal:
        if not create:
            return None
        internal[WORKING_RENDER_VARIANTS_KEY] = {}
    variants = internal.get(WORKING_RENDER_VARIANTS_KEY)
    if not isinstance(variants, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict) for key, value in variants.items()
    ):
        raise RequiredSpeechOwnershipError("working_variants_malformed")
    return variants


def _stage_key(variant_id: str, generation: str) -> str:
    if not variant_id or not generation:
        raise RequiredSpeechOwnershipError("missing_generation_owner")
    return f"{variant_id}:{generation}"


def _public_variant(plan: dict[str, Any], variant_id: str) -> tuple[list[Any], int, dict[str, Any]]:
    variants = plan.get("variants")
    if not isinstance(variants, list):
        raise RequiredSpeechOwnershipError("variants_not_list")
    indexes = [
        index
        for index, value in enumerate(variants)
        if isinstance(value, dict) and value.get("variant_id") == variant_id
    ]
    if len(indexes) != 1:
        raise RequiredSpeechOwnershipError("variant_missing_or_ambiguous")
    index = indexes[0]
    return variants, index, dict(variants[index])


def reserve_required_speech_generation(
    plan: dict[str, Any],
    *,
    job_id: str,
    pending_variant: dict[str, Any],
    generation: str,
    lease_expires_at: datetime,
) -> None:
    """Install pending row, private edit lock, and durable upload receipt together."""

    variant_id = str(pending_variant.get("variant_id") or "")
    if pending_variant.get("render_generation_id") != generation:
        raise RequiredSpeechOwnershipError("pending_generation_mismatch")
    variants = plan.setdefault("variants", [])
    if not isinstance(variants, list):
        raise RequiredSpeechOwnershipError("variants_not_list")
    matches = [
        index
        for index, value in enumerate(variants)
        if isinstance(value, dict) and value.get("variant_id") == variant_id
    ]
    if len(matches) > 1:
        raise RequiredSpeechOwnershipError("variant_ambiguous")
    control = plan.get("speech_cut_control")
    required_atomic = plan.get("speech_cleanup_contract") == "required_v1"
    if required_atomic and control not in (None, {}):
        if not isinstance(control, dict):
            raise RequiredSpeechOwnershipError("speech_cut_control_malformed")
        operation_id = control.get("operation_id")
        if (
            control.get("variant_id") != variant_id
            or control.get("render_generation_id") != generation
            or not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 128
        ):
            raise RequiredSpeechOwnershipError("speech_cut_pre_reservation_owner_mismatch")
        if (
            _safe_last_good_snapshot(
                plan,
                variant_id=variant_id,
                generation_prefix=render_generation_prefix(job_id, generation),
            )
            is None
        ):
            raise RequiredSpeechOwnershipError("last_good_snapshot_unavailable")
        rerender = True
    else:
        rerender = bool(
            isinstance(control, dict)
            and control.get("variant_id") == variant_id
            and control.get("render_generation_id") == generation
        )
    locks = _locks(plan, create=True)
    assert locks is not None
    existing_lock = locks.get(variant_id)
    if existing_lock is not None and existing_lock != generation:
        raise RequiredSpeechOwnershipError("variant_generation_locked")
    if matches:
        previous = dict(variants[matches[0]])
        if rerender:
            working = _working_variants(plan, create=True)
            assert working is not None
            key = _stage_key(variant_id, generation)
            if key not in working and len(working) >= STAGED_RENDER_RESULTS_CAP:
                raise RequiredSpeechStageBackpressure("working_variant_backpressure")
            working[key] = {**copy.deepcopy(previous), **copy.deepcopy(pending_variant)}
        else:
            variants[matches[0]] = {**previous, **copy.deepcopy(pending_variant)}
    else:
        if rerender:
            raise RequiredSpeechOwnershipError("rerender_public_variant_missing")
        variants.append(copy.deepcopy(pending_variant))
    locks[variant_id] = generation
    reserve_render_generation_cleanup(
        plan,
        job_id=job_id,
        generation=generation,
        lease_expires_at=lease_expires_at,
    )


def mark_required_speech_rendering(
    plan: dict[str, Any],
    *,
    variant_id: str,
    generation: str,
    render_started_at: str,
) -> None:
    locks = _locks(plan, create=False)
    if locks is None or locks.get(variant_id) != generation:
        raise RequiredSpeechOwnershipError("generation_lock_mismatch")
    key = _stage_key(variant_id, generation)
    working = _working_variants(plan, create=False)
    if working is not None and key in working:
        current = dict(working[key])
        if current.get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("working_generation_mismatch")
        current.update(render_status="rendering", render_started_at=render_started_at)
        working[key] = current
    else:
        variants, index, current = _public_variant(plan, variant_id)
        if current.get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("public_generation_mismatch")
        current.update(render_status="rendering", render_started_at=render_started_at)
        variants[index] = current


def stage_required_speech_generation(
    plan: dict[str, Any],
    *,
    result: dict[str, Any],
    generation: str,
) -> None:
    variant_id = str(result.get("variant_id") or "")
    if result.get("render_generation_id") != generation:
        raise RequiredSpeechOwnershipError("result_generation_mismatch")
    locks = _locks(plan, create=False)
    if locks is None or locks.get(variant_id) != generation:
        raise RequiredSpeechOwnershipError("generation_lock_mismatch")
    key = _stage_key(variant_id, generation)
    working = _working_variants(plan, create=False)
    if working is not None and key in working:
        if working[key].get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("working_generation_mismatch")
    else:
        _variants, _index, current = _public_variant(plan, variant_id)
        if current.get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("public_generation_mismatch")
    stages = _stages(plan, create=True)
    assert stages is not None
    if key not in stages and len(stages) >= STAGED_RENDER_RESULTS_CAP:
        raise RequiredSpeechStageBackpressure("generation_stage_backpressure")
    stages[key] = copy.deepcopy(result)
    if working is not None and key in working:
        working[key] = copy.deepcopy(result)
    capsule = _terminal_context_capsule(
        result,
        variant_id=variant_id,
        generation=generation,
    )
    contexts = _terminal_contexts(plan, create=capsule is not None)
    if contexts is not None:
        if capsule is None:
            contexts.pop(key, None)
            if not contexts:
                internal = _internal(plan, create=False)
                assert internal is not None
                internal.pop(TERMINAL_PENDING_KEY, None)
        else:
            if key not in contexts and len(contexts) >= TERMINAL_PENDING_CAP:
                raise RequiredSpeechStageBackpressure("terminal_context_backpressure")
            contexts[key] = capsule


def update_required_speech_generation(
    plan: dict[str, Any],
    *,
    variant_id: str,
    generation: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Atomically merge one compose patch into the exact private generation."""

    locks = _locks(plan, create=False)
    if locks is None or locks.get(variant_id) != generation:
        raise RequiredSpeechOwnershipError("generation_lock_mismatch")
    key = _stage_key(variant_id, generation)
    working = _working_variants(plan, create=False)
    stages = _stages(plan, create=False)
    if working is None or key not in working or stages is None or key not in stages:
        raise RequiredSpeechOwnershipError("private_working_variant_missing")
    current = working[key]
    staged = stages[key]
    if (
        current.get("variant_id") != variant_id
        or current.get("render_generation_id") != generation
        or staged.get("variant_id") != variant_id
        or staged.get("render_generation_id") != generation
    ):
        raise RequiredSpeechOwnershipError("working_variant_owner_mismatch")
    merged = {
        **current,
        **{key: copy.deepcopy(value) for key, value in patch.items() if key != "variant_id"},
    }
    working[key] = merged
    stages[key] = copy.deepcopy(merged)
    return copy.deepcopy(merged)


def close_required_speech_generation_uploads(
    plan: dict[str, Any],
    *,
    generation: str,
) -> None:
    mark_cleanup_receipt_closed(
        plan,
        CleanupReceiptLocator(
            field=RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=generation,
        ),
    )


def consume_required_speech_generation(
    plan: dict[str, Any],
    *,
    job_id: str,
    variant_id: str,
    generation: str,
) -> dict[str, Any]:
    """Return the exact staged result and clear only its matching private owner."""

    locks = _locks(plan, create=False)
    if locks is None or locks.get(variant_id) != generation:
        raise RequiredSpeechOwnershipError("generation_lock_mismatch")
    stages = _stages(plan, create=False)
    key = _stage_key(variant_id, generation)
    if stages is None or key not in stages:
        raise RequiredSpeechOwnershipError("staged_result_missing")
    result = copy.deepcopy(stages[key])
    if result.get("variant_id") != variant_id or result.get("render_generation_id") != generation:
        raise RequiredSpeechOwnershipError("staged_result_owner_mismatch")
    internal = _internal(plan, create=False)
    if internal is None:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing")
    receipts = internal.get(RENDER_GENERATION_CLEANUP_FIELD)
    matches = (
        [
            raw
            for raw in receipts
            if isinstance(raw, dict)
            and raw.get("kind") is None
            and raw.get("generation") == generation
        ]
        if isinstance(receipts, list)
        else []
    )
    if len(matches) != 1:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing_or_ambiguous")
    receipt = matches[0]
    if receipt.get("prefix") != render_generation_prefix(job_id, generation):
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_prefix_mismatch")
    if receipt.get("upload_state") != "closed":
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_not_closed")
    if not isinstance(receipt.get("lease_expires_at"), str):
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed")
    if not remove_cleanup_receipt(
        plan,
        CleanupReceiptLocator(
            field=RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=generation,
        ),
    ):
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing")
    stages.pop(key)
    working = _working_variants(plan, create=False)
    if working is not None:
        working.pop(key, None)
    _remove_terminal_context(
        internal,
        variant_id=variant_id,
        generation=generation,
    )
    locks.pop(variant_id)
    internal = _internal(plan, create=False)
    assert internal is not None
    if not stages:
        internal.pop(STAGED_RENDER_RESULTS_KEY, None)
    if working == {}:
        internal.pop(WORKING_RENDER_VARIANTS_KEY, None)
    if not locks:
        internal.pop(REQUIRED_SPEECH_LOCKS_KEY, None)
    if not internal:
        plan.pop(PRIVATE_SPEECH_CLEANUP_KEY, None)
    return result


def peek_required_speech_generation(
    plan: dict[str, Any],
    *,
    variant_id: str,
    generation: str,
) -> dict[str, Any]:
    """Return an exact staged result without consuming ownership or cleanup debt."""

    locks = _locks(plan, create=False)
    if locks is None or locks.get(variant_id) != generation:
        raise RequiredSpeechOwnershipError("generation_lock_mismatch")
    key = _stage_key(variant_id, generation)
    working = _working_variants(plan, create=False)
    if working is not None and key in working:
        current = working[key]
        if current.get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("working_generation_mismatch")
    else:
        _variants, _index, current = _public_variant(plan, variant_id)
        if current.get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("public_generation_mismatch")
    stages = _stages(plan, create=False)
    if stages is None or key not in stages:
        raise RequiredSpeechOwnershipError("staged_result_missing")
    result = copy.deepcopy(stages[key])
    if result.get("variant_id") != variant_id or result.get("render_generation_id") != generation:
        raise RequiredSpeechOwnershipError("staged_result_owner_mismatch")
    return result


def classify_required_speech_resume(
    plan: dict[str, Any],
    *,
    job_id: str,
    variant_id: str,
    expected_music_track_id: object,
    expected_analysis_view: str,
    expected_detector_version: str,
    object_exists: Callable[[str], bool],
) -> RequiredSpeechResume:
    """Prove a private staged result is exactly resumable.

    This classifier never trusts a ready-looking public row.  It requires the
    generation lock, cleanup receipt, private stage, bounded scalar analysis
    capsule, generation-scoped object keys, and successful HEAD proof to agree.
    Any owned but incomplete attempt is explicitly ``rotate``; malformed global
    ownership is ``blocked`` so a caller cannot unlock ambiguous state.
    """

    try:
        if not isinstance(plan, dict):
            raise RequiredSpeechOwnershipError("assembly_plan_not_object")
        if plan.get("speech_cut_control") not in (None, {}):
            raise RequiredSpeechOwnershipError("speech_cut_control_active")
        locks = _locks(plan, create=False)
        if locks is None or variant_id not in locks:
            return RequiredSpeechResume("absent")
        generation = locks[variant_id]
        if not _GENERATION_RE.fullmatch(generation):
            return RequiredSpeechResume("rotate", generation, reason="generation_malformed")
        prefix = render_generation_prefix(job_id, generation)
        _variants, _index, public = _public_variant(plan, variant_id)
        if public.get("render_generation_id") != generation:
            raise RequiredSpeechOwnershipError("public_generation_mismatch")
        if public.get("render_status") not in {"pending", "rendering"}:
            return RequiredSpeechResume("rotate", generation, reason="public_not_in_progress")

        internal = _internal(plan, create=False)
        assert internal is not None
        receipts = internal.get(RENDER_GENERATION_CLEANUP_FIELD)
        matching_receipts = (
            [
                raw
                for raw in receipts
                if isinstance(raw, dict)
                and raw.get("kind") is None
                and raw.get("generation") == generation
            ]
            if isinstance(receipts, list)
            else []
        )
        if len(matching_receipts) != 1:
            raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing_or_ambiguous")
        receipt = matching_receipts[0]
        if receipt.get("prefix") != prefix:
            raise RequiredSpeechOwnershipError("active_cleanup_receipt_prefix_mismatch")
        lease_raw = receipt.get("lease_expires_at")
        if not isinstance(lease_raw, str):
            raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed")
        if receipt.get("upload_state") == "writing":
            try:
                lease_expires_at = datetime.fromisoformat(lease_raw)
            except ValueError as exc:
                raise RequiredSpeechOwnershipError(
                    "active_cleanup_receipt_lease_malformed"
                ) from exc
            if lease_expires_at.tzinfo is None:
                raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed")
            now = datetime.now(UTC)
            if lease_expires_at > now:
                return RequiredSpeechResume(
                    "blocked",
                    generation,
                    reason="uploads_still_active",
                    retry_after_s=(lease_expires_at - now).total_seconds(),
                )
            return RequiredSpeechResume(
                "rotate",
                generation,
                reason="upload_lease_expired",
            )
        if receipt.get("upload_state") != "closed":
            raise RequiredSpeechOwnershipError("active_cleanup_receipt_state_malformed")
        working = _working_variants(plan, create=False)
        key = _stage_key(variant_id, generation)
        if working is not None and key in working:
            raise RequiredSpeechOwnershipError("initial_resume_has_working_variant")
        stages = _stages(plan, create=False)
        if stages is None or key not in stages:
            return RequiredSpeechResume("rotate", generation, reason="staged_result_missing")
        result = stages[key]
        if (
            result.get("variant_id") != variant_id
            or result.get("render_generation_id") != generation
        ):
            raise RequiredSpeechOwnershipError("staged_result_owner_mismatch")
        if result.get("ok") is not True or not isinstance(result.get("output_url"), str):
            return RequiredSpeechResume("rotate", generation, reason="staged_result_not_ready")
        if not result["output_url"]:
            return RequiredSpeechResume("rotate", generation, reason="staged_output_url_missing")
        if result.get("music_track_id") != expected_music_track_id:
            return RequiredSpeechResume("rotate", generation, reason="track_mismatch")

        contexts = _terminal_contexts(plan, create=False)
        capsule = contexts.get(key) if contexts is not None else None
        result_context = result.get("_speech_cleanup_outcome_context")
        if not isinstance(capsule, dict) or not isinstance(result_context, dict):
            return RequiredSpeechResume("rotate", generation, reason="terminal_context_missing")
        if (
            capsule.get("variant_id") != variant_id
            or capsule.get("render_generation_id") != generation
            or capsule.get("analysis_view") != expected_analysis_view
            or capsule.get("detector_version") != expected_detector_version
            or capsule.get("analysis_attempt_id") != result_context.get("analysis_attempt_id")
            or capsule.get("analysis_view") != result_context.get("analysis_view")
            or capsule.get("detector_version") != result_context.get("detector_version")
            or any(
                capsule.get(field) != result_context.get(field)
                for field in (
                    "source_tag",
                    "selected_plan",
                    "candidate_status",
                    "output_removal_count",
                    "output_removed_ms",
                )
            )
        ):
            return RequiredSpeechResume("rotate", generation, reason="terminal_context_mismatch")
        if not isinstance(capsule.get("analysis_attempt_id"), str) or not capsule.get(
            "analysis_attempt_id"
        ):
            return RequiredSpeechResume("rotate", generation, reason="terminal_context_malformed")

        artifact_paths: list[str] = []
        for field in _RESUME_ARTIFACT_FIELDS:
            value = result.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                return RequiredSpeechResume("rotate", generation, reason=f"{field}_malformed")
            artifact_paths.append(value)
        video_path = result.get("video_path")
        if not isinstance(video_path, str) or not video_path:
            return RequiredSpeechResume("rotate", generation, reason="video_path_missing")
        for field in ("uploaded_lane_artifacts", "uploaded_lane_artifact_paths"):
            values = result.get(field)
            if values is None:
                continue
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value for value in values
            ):
                return RequiredSpeechResume("rotate", generation, reason=f"{field}_malformed")
            artifact_paths.extend(values)
        if any(not path.startswith(prefix) for path in artifact_paths):
            return RequiredSpeechResume("rotate", generation, reason="artifact_prefix_mismatch")
        object_paths = set(artifact_paths)
        matte_path = result.get("subject_matte_path")
        if isinstance(matte_path, str) and matte_path:
            object_paths.add(f"{matte_path}.json")
        try:
            all_objects_exist = all(bool(object_exists(path)) for path in sorted(object_paths))
        except Exception:  # noqa: BLE001 - unavailable metadata is never resume proof
            all_objects_exist = False
        if not all_objects_exist:
            return RequiredSpeechResume("rotate", generation, reason="artifact_unavailable")
        return RequiredSpeechResume(
            "resumable",
            generation,
            staged_result=copy.deepcopy(result),
        )
    except RequiredSpeechOwnershipError as exc:
        return RequiredSpeechResume("blocked", reason=str(exc))


def rotate_required_speech_generation_for_retry(
    plan: dict[str, Any],
    *,
    job_id: str,
    variant_id: str,
    generation: str,
) -> None:
    """Retire one exact non-resumable initial owner without losing cleanup debt.

    Callers reserve the replacement generation in the same row-locked plan
    mutation, so there is no committed unlocked gap.
    """

    if plan.get("speech_cut_control") not in (None, {}):
        raise RequiredSpeechOwnershipError("speech_cut_control_active")
    locks = _locks(plan, create=False)
    if locks is None or locks.get(variant_id) != generation:
        raise RequiredSpeechOwnershipError("generation_lock_mismatch")
    variants, index, public = _public_variant(plan, variant_id)
    if public.get("render_generation_id") != generation:
        raise RequiredSpeechOwnershipError("public_generation_mismatch")
    prefix = render_generation_prefix(job_id, generation)
    _assert_cleanup_rotation_safe(
        plan,
        generation=generation,
        expected_prefix=prefix,
    )
    _close_owned_cleanup_debt(
        plan,
        generation=generation,
        expected_prefix=prefix,
    )
    key = _stage_key(variant_id, generation)
    stages = _stages(plan, create=False)
    if stages is not None:
        staged = stages.get(key)
        if staged is not None and (
            staged.get("variant_id") != variant_id
            or staged.get("render_generation_id") != generation
        ):
            raise RequiredSpeechOwnershipError("staged_result_owner_mismatch")
        stages.pop(key, None)
    working = _working_variants(plan, create=False)
    if working is not None and key in working:
        raise RequiredSpeechOwnershipError("initial_retry_has_working_variant")
    internal = _internal(plan, create=False)
    assert internal is not None
    _remove_terminal_context(internal, variant_id=variant_id, generation=generation)
    locks.pop(variant_id)
    rotated = {
        field: copy.deepcopy(value)
        for field, value in public.items()
        if field
        not in {
            "render_generation_id",
            "render_started_at",
            "render_finished_at",
            "error",
            "error_class",
        }
        and not _value_references_prefix(value, prefix)
    }
    variants[index] = rotated
    if stages == {}:
        internal.pop(STAGED_RENDER_RESULTS_KEY, None)
    if not locks:
        internal.pop(REQUIRED_SPEECH_LOCKS_KEY, None)


def _value_references_prefix(value: object, prefix: str) -> bool:
    if isinstance(value, str):
        return value.startswith(prefix)
    if isinstance(value, dict):
        return any(_value_references_prefix(nested, prefix) for nested in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_references_prefix(nested, prefix) for nested in value)
    return False


def _failed_public_variant(
    variant: dict[str, Any],
    *,
    generation_prefix: str,
    error: str,
) -> dict[str, Any]:
    """Fail one exact row without retaining any provisional artifact reference."""

    failed = {
        key: copy.deepcopy(value)
        for key, value in variant.items()
        if not _value_references_prefix(value, generation_prefix)
    }
    failed.update(
        {
            "render_status": "failed",
            "ok": False,
            "error": str(error)[:300],
        }
    )
    return failed


def _safe_last_good_snapshot(
    plan: dict[str, Any],
    *,
    variant_id: str,
    generation_prefix: str,
) -> list[dict[str, Any]] | None:
    """Return an exact rollback vector only when it cannot reference this attempt."""

    previous = plan.get("speech_cut_previous_variants")
    if previous is None:
        # Legacy speech-cut dispatch stored only the target last-good row.  New
        # creator bundles may instead use ``speech_cut_previous_variant`` as the
        # private editor-lane input for the requested generation, so this field
        # is authoritative rollback material only when the public-vector
        # snapshot is absent.
        prior = plan.get("speech_cut_previous_variant")
        if not isinstance(prior, dict) or prior.get("variant_id") != variant_id:
            return None
        if prior.get("render_status") in {"pending", "rendering"}:
            return None
        if _value_references_prefix(prior, generation_prefix):
            return None
        current = plan.get("variants")
        if not isinstance(current, list) or any(not isinstance(item, dict) for item in current):
            return None
        restored = [
            copy.deepcopy(prior if item.get("variant_id") == variant_id else item)
            for item in current
        ]
    elif isinstance(previous, list) and all(isinstance(item, dict) for item in previous):
        restored = copy.deepcopy(previous)
    else:
        return None

    ids = [item.get("variant_id") for item in restored]
    if any(not isinstance(item_id, str) or not item_id for item_id in ids):
        return None
    if len(ids) != len(set(ids)) or ids.count(variant_id) != 1:
        return None
    restored_prior = next(item for item in restored if item.get("variant_id") == variant_id)
    if restored_prior.get("render_status") in {"pending", "rendering"}:
        return None
    if _value_references_prefix(restored, generation_prefix):
        return None
    return restored


def _terminalize_pre_reservation_speech_control(
    plan: dict[str, Any],
    *,
    job_id: str,
    error: str,
    expected_operation_id: str | None,
    expected_attempt_id: str | None,
) -> RequiredSpeechTerminalization | None:
    """Rollback a required speech request that never acquired an upload lock.

    Required-v1 dispatch commits ``speech_cut_control`` before broker publication
    so it can fence editors immediately.  The worker later adopts that exact
    operation/generation into a private generation lock and cleanup receipt.  If
    cancellation or the stale-job reaper wins before adoption, absence of that
    lock proves no required-speech upload may have started; clearing the control
    makes every later worker CAS fail while restoring the immutable public
    rollback vector.
    """

    if plan.get("speech_cleanup_contract") != "required_v1":
        return None
    control = plan.get("speech_cut_control")
    if control in (None, {}):
        return None
    if not isinstance(control, dict):
        raise RequiredSpeechOwnershipError("speech_cut_control_malformed")
    variant_id = control.get("variant_id")
    generation = control.get("render_generation_id")
    operation_id = control.get("operation_id")
    if not isinstance(variant_id, str) or not variant_id:
        raise RequiredSpeechOwnershipError("speech_cut_control_owner_missing")
    if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
        raise RequiredSpeechOwnershipError("speech_cut_control_generation_malformed")
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise RequiredSpeechOwnershipError("speech_cut_operation_missing")
    if expected_operation_id is not None and operation_id != expected_operation_id:
        raise RequiredSpeechOwnershipError("speech_cut_operation_mismatch")
    if expected_attempt_id is not None:
        claim = control.get("finalizer_claim")
        if not isinstance(claim, dict) or claim.get("attempt_id") != expected_attempt_id:
            raise RequiredSpeechOwnershipError("speech_cut_attempt_mismatch")

    internal = _internal(plan, create=False)
    if internal is not None:
        # A stage/working/context without a lock is ambiguous corruption, not a
        # pre-reservation request.  Likewise, a same-generation cleanup receipt
        # means storage ownership exists but its lock disappeared; never infer
        # that the provider boundary was not crossed.
        for container, reason in (
            (_stages(plan, create=False), "orphan_staged_result"),
            (_working_variants(plan, create=False), "orphan_working_variant"),
            (_terminal_contexts(plan, create=False), "orphan_terminal_context"),
        ):
            if container:
                raise RequiredSpeechOwnershipError(reason)
        receipts = internal.get(RENDER_GENERATION_CLEANUP_FIELD)
        if receipts is not None and not isinstance(receipts, list):
            raise RequiredSpeechOwnershipError("cleanup_receipts_malformed")
        if isinstance(receipts, list) and any(
            isinstance(receipt, dict) and receipt.get("generation") == generation
            for receipt in receipts
        ):
            raise RequiredSpeechOwnershipError("cleanup_receipt_without_generation_lock")

    restored = _safe_last_good_snapshot(
        plan,
        variant_id=variant_id,
        generation_prefix=render_generation_prefix(job_id, generation),
    )
    if restored is None:
        raise RequiredSpeechOwnershipError("last_good_snapshot_unavailable")

    plan["variants"] = restored
    plan["silence_cut_disabled"] = bool(control.get("prior_disabled"))
    plan["speech_cut_last_error"] = str(error)[:300]
    plan["speech_cut_control"] = None
    plan["speech_cut_previous_variant"] = None
    plan["speech_cut_previous_variants"] = None
    if internal is not None:
        for key in (
            REQUIRED_SPEECH_LOCKS_KEY,
            STAGED_RENDER_RESULTS_KEY,
            WORKING_RENDER_VARIANTS_KEY,
            TERMINAL_PENDING_KEY,
        ):
            if internal.get(key) == {}:
                internal.pop(key, None)
        if not internal:
            plan.pop(PRIVATE_SPEECH_CLEANUP_KEY, None)
    return RequiredSpeechTerminalization(
        status="terminalized",
        plan=plan,
        terminalized_count=1,
        restored_last_good=True,
    )


def _remove_terminal_context(
    internal: dict[str, Any],
    *,
    variant_id: str,
    generation: str,
) -> None:
    contexts = internal.get(TERMINAL_PENDING_KEY)
    if contexts is None:
        return
    if not isinstance(contexts, dict):
        raise RequiredSpeechOwnershipError("terminal_context_malformed")
    direct_key = _stage_key(variant_id, generation)
    contexts.pop(direct_key, None)
    matching_keys = [
        key
        for key, value in contexts.items()
        if isinstance(key, str)
        and isinstance(value, dict)
        and value.get("variant_id") == variant_id
        and value.get("render_generation_id") == generation
    ]
    for key in matching_keys:
        contexts.pop(key, None)
    if not contexts:
        internal.pop(TERMINAL_PENDING_KEY, None)


def _close_owned_cleanup_debt(
    plan: dict[str, Any],
    *,
    generation: str,
    expected_prefix: str,
) -> None:
    internal = _internal(plan, create=False)
    if internal is None:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing")
    receipts = internal.get(RENDER_GENERATION_CLEANUP_FIELD)
    if not isinstance(receipts, list):
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing")
    matches = [
        raw
        for raw in receipts
        if isinstance(raw, dict) and raw.get("kind") is None and raw.get("generation") == generation
    ]
    if len(matches) != 1:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing_or_ambiguous")
    receipt = matches[0]
    if receipt.get("prefix") != expected_prefix:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_prefix_mismatch")
    if receipt.get("upload_state") not in {"writing", "closed"}:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_state_malformed")
    if not isinstance(receipt.get("lease_expires_at"), str):
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed")
    mark_cleanup_receipt_closed(
        plan,
        CleanupReceiptLocator(
            field=RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=generation,
        ),
    )


def _assert_cleanup_rotation_safe(
    plan: dict[str, Any],
    *,
    generation: str,
    expected_prefix: str,
) -> None:
    """Require normal close or lease expiry before relinquishing upload ownership."""

    internal = _internal(plan, create=False)
    if internal is None:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing")
    receipts = internal.get(RENDER_GENERATION_CLEANUP_FIELD)
    matches = (
        [
            raw
            for raw in receipts
            if isinstance(raw, dict)
            and raw.get("kind") is None
            and raw.get("generation") == generation
        ]
        if isinstance(receipts, list)
        else []
    )
    if len(matches) != 1 or matches[0].get("prefix") != expected_prefix:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_missing_or_ambiguous")
    receipt = matches[0]
    state = receipt.get("upload_state")
    if state == "closed":
        return
    if state != "writing":
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_state_malformed")
    lease_raw = receipt.get("lease_expires_at")
    if not isinstance(lease_raw, str):
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed")
    try:
        lease_expires_at = datetime.fromisoformat(lease_raw)
    except ValueError as exc:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed") from exc
    if lease_expires_at.tzinfo is None:
        raise RequiredSpeechOwnershipError("active_cleanup_receipt_lease_malformed")
    if lease_expires_at > datetime.now(UTC):
        raise RequiredSpeechOwnershipError("generation_uploads_still_active")


def terminalize_required_speech_generations(
    plan: dict[str, Any],
    *,
    job_id: str,
    error: str = "render interrupted: worker died",
    now_epoch_s: float | None = None,
    expected_operation_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> RequiredSpeechTerminalization:
    """Fail-close every active required-speech generation in ``plan``.

    The function is intentionally pure: it validates and rewrites a deep copy,
    returning ``blocked`` with the original value on any ownership ambiguity.
    A matching render-generation cleanup receipt is closed but retained as debt;
    the post-commit storage reconciler owns deletion.  A speech-cut rerender is
    restored only from its exact safe rollback vector.  An ordinary initial
    render is marked failed and all attempt-prefix references are scrubbed.  No
    staged or merely path-bearing result can ever be promoted to ready.
    """

    if not isinstance(plan, dict):
        return RequiredSpeechTerminalization(
            status="blocked",
            plan=plan,
            reason="assembly_plan_not_object",
        )
    try:
        candidate = copy.deepcopy(plan)
        locks = _locks(candidate, create=False)
        if not locks:
            pre_reservation = _terminalize_pre_reservation_speech_control(
                candidate,
                job_id=job_id,
                error=error,
                expected_operation_id=expected_operation_id,
                expected_attempt_id=expected_attempt_id,
            )
            if pre_reservation is not None:
                return pre_reservation
            return RequiredSpeechTerminalization(status="unchanged", plan=plan)
        owner_count = len(locks)
        stages = _stages(candidate, create=False)
        working = _working_variants(candidate, create=False)
        if stages is not None:
            expected_stage_keys = {
                _stage_key(variant_id, generation) for variant_id, generation in locks.items()
            }
            if any(key not in expected_stage_keys for key in stages):
                raise RequiredSpeechOwnershipError("orphan_staged_result")
        if working is not None:
            expected_working_keys = {
                _stage_key(variant_id, generation) for variant_id, generation in locks.items()
            }
            if any(key not in expected_working_keys for key in working):
                raise RequiredSpeechOwnershipError("orphan_working_variant")

        control = candidate.get("speech_cut_control")
        control_target: str | None = None
        restored_last_good = False
        if control is not None:
            if not isinstance(control, dict):
                raise RequiredSpeechOwnershipError("speech_cut_control_malformed")
            control_target = control.get("variant_id")
            if not isinstance(control_target, str) or control_target not in locks:
                raise RequiredSpeechOwnershipError("speech_cut_control_owner_mismatch")
            if len(locks) != 1:
                raise RequiredSpeechOwnershipError("speech_cut_control_owner_ambiguous")
            owned_generation = locks[control_target]
            if control.get("render_generation_id") != owned_generation:
                raise RequiredSpeechOwnershipError("speech_cut_control_generation_mismatch")
            operation_id = control.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise RequiredSpeechOwnershipError("speech_cut_operation_missing")
            if expected_operation_id is not None and operation_id != expected_operation_id:
                raise RequiredSpeechOwnershipError("speech_cut_operation_mismatch")
            claim = control.get("finalizer_claim")
            if claim is not None:
                if not isinstance(claim, dict) or claim.get("operation_id") != operation_id:
                    raise RequiredSpeechOwnershipError("speech_cut_claim_malformed")
                attempt_id = claim.get("attempt_id")
                if not isinstance(attempt_id, str) or not attempt_id:
                    raise RequiredSpeechOwnershipError("speech_cut_claim_malformed")
                if expected_attempt_id is not None and attempt_id != expected_attempt_id:
                    raise RequiredSpeechOwnershipError("speech_cut_attempt_mismatch")
                if claim.get("render_generation_id") != owned_generation:
                    raise RequiredSpeechOwnershipError("speech_cut_claim_generation_mismatch")
                claim_disposition = classify_required_speech_claim(
                    claim,
                    now_epoch_s=now_epoch_s,
                )
                owns_fresh_claim = bool(
                    expected_operation_id
                    and expected_attempt_id
                    and operation_id == expected_operation_id
                    and claim.get("operation_id") == expected_operation_id
                    and claim.get("attempt_id") == expected_attempt_id
                )
                if claim_disposition.status == "fresh" and not owns_fresh_claim:
                    raise RequiredSpeechOwnershipError("speech_cut_claim_still_fresh")
                if not claim_disposition.recoverable and not owns_fresh_claim:
                    raise RequiredSpeechOwnershipError("speech_cut_claim_malformed")
            elif expected_attempt_id is not None:
                raise RequiredSpeechOwnershipError("speech_cut_attempt_mismatch")

        internal = _internal(candidate, create=False)
        assert internal is not None
        variants = candidate.get("variants")
        if not isinstance(variants, list) or any(not isinstance(item, dict) for item in variants):
            raise RequiredSpeechOwnershipError("variants_not_list")

        restored_variants: list[dict[str, Any]] | None = None
        terminal_contexts: list[dict[str, Any]] = []
        for variant_id, generation in list(locks.items()):
            _variants, index, public = _public_variant(candidate, variant_id)
            stage_key = _stage_key(variant_id, generation)
            working_variant = working.get(stage_key) if working is not None else None
            if working_variant is not None:
                if (
                    variant_id != control_target
                    or working_variant.get("variant_id") != variant_id
                    or working_variant.get("render_generation_id") != generation
                ):
                    raise RequiredSpeechOwnershipError("working_variant_owner_mismatch")
            elif public.get("render_generation_id") != generation:
                raise RequiredSpeechOwnershipError("public_generation_mismatch")
            prefix = render_generation_prefix(job_id, generation)
            # Missing/ambiguous debt blocks the entire transition.  Clearing
            # ownership without durable cleanup would orphan lifecycle-exempt bytes.
            # A still-writing receipt is equally authoritative: cancellation or
            # a hard-kill may race an upload that has crossed the provider
            # boundary but has not returned yet.  Only the writer's explicit
            # close, or expiry of its persisted lease, proves that no more bytes
            # can settle beneath this prefix.
            _assert_cleanup_rotation_safe(
                candidate,
                generation=generation,
                expected_prefix=prefix,
            )
            _close_owned_cleanup_debt(
                candidate,
                generation=generation,
                expected_prefix=prefix,
            )

            if variant_id == control_target:
                restored_variants = _safe_last_good_snapshot(
                    candidate,
                    variant_id=variant_id,
                    generation_prefix=prefix,
                )
                if restored_variants is None:
                    raise RequiredSpeechOwnershipError("last_good_snapshot_unavailable")
                restored_last_good = True
            else:
                variants[index] = _failed_public_variant(
                    public,
                    generation_prefix=prefix,
                    error=error,
                )

            if stages is not None:
                staged = stages.get(stage_key)
                if staged is not None and (
                    staged.get("variant_id") != variant_id
                    or staged.get("render_generation_id") != generation
                ):
                    raise RequiredSpeechOwnershipError("staged_result_owner_mismatch")
                stages.pop(stage_key, None)
            if working is not None:
                working.pop(stage_key, None)
            contexts = _terminal_contexts(candidate, create=False)
            capsule = contexts.get(stage_key) if contexts is not None else None
            if (
                isinstance(capsule, dict)
                and capsule.get("variant_id") == variant_id
                and capsule.get("render_generation_id") == generation
            ):
                # The terminalizer is the last exact owner of this private
                # capsule. Return a defensive copy to the row-locked caller so
                # it can append the correlated terminal outcome in the same DB
                # transition before the capsule is removed. Missing or
                # uncorrelated context deliberately remains "unknown" and does
                # not block lifecycle recovery.
                terminal_contexts.append(copy.deepcopy(capsule))
            _remove_terminal_context(
                internal,
                variant_id=variant_id,
                generation=generation,
            )
            locks.pop(variant_id)

        if restored_variants is not None:
            candidate["variants"] = restored_variants
            candidate["silence_cut_disabled"] = bool(control.get("prior_disabled"))
            candidate["speech_cut_last_error"] = str(error)[:300]
            candidate["speech_cut_control"] = None
            candidate["speech_cut_previous_variant"] = None
            candidate["speech_cut_previous_variants"] = None

        if stages == {}:
            internal.pop(STAGED_RENDER_RESULTS_KEY, None)
        if locks == {}:
            internal.pop(REQUIRED_SPEECH_LOCKS_KEY, None)
        if working == {}:
            internal.pop(WORKING_RENDER_VARIANTS_KEY, None)
        # Cleanup receipts deliberately keep the private container alive.
        if not internal:
            candidate.pop(PRIVATE_SPEECH_CLEANUP_KEY, None)
        return RequiredSpeechTerminalization(
            status="terminalized",
            plan=candidate,
            terminalized_count=owner_count,
            restored_last_good=restored_last_good,
            terminal_contexts=tuple(terminal_contexts),
        )
    except RequiredSpeechOwnershipError as exc:
        return RequiredSpeechTerminalization(
            status="blocked",
            plan=plan,
            reason=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — recovery must fail closed
        return RequiredSpeechTerminalization(
            status="blocked",
            plan=plan,
            reason=type(exc).__name__,
        )
