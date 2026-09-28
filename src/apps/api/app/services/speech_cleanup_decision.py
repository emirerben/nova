"""Shared speech-cleanup decision policy for v1 chat and Kria runtime-v2 approvals.

Two callers must apply byte-identical enforce-mode policy to a submitted
speech-cleanup analysis id / choice:

- `routes/creation_threads.py` (v1 `generate`/`confirm_generation`), which maps
  a conflict to the exact `HTTPException(409, detail=<code>)` it has always
  raised.
- `app/kria/runtime.decide_approval` (runtime-v2), which has no equivalent
  card round-trip before dispatch and so must apply the same fence at
  approval time, mapping a conflict to a `RuntimeFailure` instead.

This module owns only the read-side validation (plus the narrow "a deploy
restamped every fingerprint" write that both `refresh_policy_stale_analysis_async`
and this module's callers already treat as one owner). It raises nothing
HTTP-shaped: every caller maps `SpeechCleanupDecisionConflict` to its own error
type. It never commits -- see `refreshed`/`refreshed_analysis_id` below.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PlanItem, SpeechCleanupAnalysis
from app.services.active_narration_source import ActiveNarrationResolution

SpeechCleanupChoice = Literal["clean", "keep_original"]
SpeechCleanupFullChoice = Literal["clean", "keep_original", "create_without_cleanup"]

SpeechCleanupConflictCode = Literal[
    "speech_cleanup_analysis_changed",
    "speech_cleanup_pending",
    "speech_cleanup_failed",
    "speech_cleanup_choice_not_allowed",
    "speech_cleanup_choice_required",
]

# Same wording surfaced by v1's card projection -- a v2 client has no
# equivalent card copy of its own, so `decide_approval` uses this for its
# `RuntimeFailure` message. v1 itself does not use this dict: it has always
# raised a bare `HTTPException(409, detail=<code>)` and that stays unchanged.
SPEECH_CLEANUP_CONFLICT_COPY: dict[str, str] = {
    "speech_cleanup_analysis_changed": (
        "The speech check changed before I could start. Approve again to choose "
        "how to handle the pauses."
    ),
    "speech_cleanup_pending": (
        "The speech check is still running. I'll ask again once it finishes."
    ),
    "speech_cleanup_failed": (
        "The speech check didn't finish. Approve again to render without it, "
        "or ask me to check again."
    ),
    "speech_cleanup_choice_not_allowed": (
        "No pauses or filler words were found, so there's no cleanup choice to make. Approve again."
    ),
    "speech_cleanup_choice_required": (
        "Choose how to handle the pauses and filler words before I render."
    ),
}


@dataclass(frozen=True)
class SpeechCleanupDecisionOk:
    """The submitted (or absent) analysis id / choice pass validation unchanged."""

    analysis_id: uuid.UUID | None
    choice: SpeechCleanupChoice | None


@dataclass(frozen=True)
class SpeechCleanupDecisionConflict:
    """Enforce mode refuses this decision. `refreshed*` is a bookkeeping side effect.

    ``refreshed`` is true only when this call also retired/replaced a
    policy-stale current analysis (a detector/engine deploy restamped every
    fingerprint). The caller MUST commit before surfacing the conflict when
    ``refreshed`` is true -- a rollback would restore the superseded row and
    strand the item on the same dead end next attempt -- and should publish
    ``refreshed_analysis_id`` (if set) after that commit, exactly like a fresh
    `schedule_item_preflight_*` id.
    """

    code: SpeechCleanupConflictCode
    refreshed: bool = False
    refreshed_analysis_id: uuid.UUID | None = None


SpeechCleanupDecisionResult = SpeechCleanupDecisionOk | SpeechCleanupDecisionConflict


async def evaluate_enforce_mode_decision(
    db: AsyncSession,
    item: PlanItem,
    *,
    cleanup_analysis_id: uuid.UUID | None,
    cleanup_choice: SpeechCleanupChoice | None,
    resolution: ActiveNarrationResolution | None = None,
) -> SpeechCleanupDecisionResult:
    """Validate a submitted speech-cleanup decision under enforce mode.

    Mirrors the inline fence `routes/creation_threads.py`'s `generate`/
    `confirm_generation` handler ran before this extraction (2026-09), so v1
    and the Kria runtime-v2 approval path can never diverge. The caller must
    already know it is operating under
    ``settings.speech_cleanup_preflight_mode == "enforce"`` -- this function
    does not re-check the mode, so a caller in another mode must not call it
    at all (v1's original inline block only ran inside that gate, and skipping
    the call entirely is how a non-enforce caller keeps its old behavior).
    """

    # Lazy, on every call -- exactly like the inline v1 block this replaces --
    # so a test (or another caller) that monkeypatches these at their origin
    # module keeps working; a top-level import would bind a stale reference.
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        resolve_item_narration,
    )
    from app.services.speech_cleanup_preflight import (  # noqa: PLC0415
        current_analysis_async,
        preflight_enabled_for_source,
        refresh_policy_stale_analysis_async,
    )

    resolved = resolution or resolve_item_narration(item, detector_policy=current_detector_policy())
    current_cleanup = await current_analysis_async(db, item.id)
    enforced_for_source = bool(
        resolved.source
        and preflight_enabled_for_source(
            resolved.source.source_policy_fingerprint,
            mode=settings.speech_cleanup_preflight_mode,
            rollout_percent=settings.speech_cleanup_preflight_rollout_percent,
        )
    )
    if (
        current_cleanup is not None
        and resolved.source is not None
        and current_cleanup.source_policy_fingerprint != resolved.source.source_policy_fingerprint
    ):
        # A deploy that bumps the detector/engine restamps every fingerprint,
        # so this row can be stale for media that never moved. Replace it
        # here and keep the conflict -- the client re-reads the fresh card
        # (v1) or re-approves (v2) either way.
        refresh = await refresh_policy_stale_analysis_async(db, item, resolved)
        if refresh.refreshed:
            return SpeechCleanupDecisionConflict(
                "speech_cleanup_analysis_changed",
                refreshed=True,
                refreshed_analysis_id=refresh.analysis_id,
            )
    if enforced_for_source and current_cleanup is None:
        return SpeechCleanupDecisionConflict("speech_cleanup_pending")
    if current_cleanup is not None and (
        resolved.source is None
        or current_cleanup.source_policy_fingerprint != resolved.source.source_policy_fingerprint
    ):
        return SpeechCleanupDecisionConflict("speech_cleanup_analysis_changed")
    if enforced_for_source and current_cleanup is not None:
        if cleanup_analysis_id != current_cleanup.id:
            return SpeechCleanupDecisionConflict("speech_cleanup_analysis_changed")
        if current_cleanup.status in {"queued", "running"}:
            return SpeechCleanupDecisionConflict("speech_cleanup_pending")
        if current_cleanup.status == "failed":
            return SpeechCleanupDecisionConflict("speech_cleanup_failed")
        if current_cleanup.status == "no_findings" and cleanup_choice is not None:
            return SpeechCleanupDecisionConflict("speech_cleanup_choice_not_allowed")
        if current_cleanup.status == "ready" and cleanup_choice is None:
            return SpeechCleanupDecisionConflict("speech_cleanup_choice_required")
    return SpeechCleanupDecisionOk(analysis_id=cleanup_analysis_id, choice=cleanup_choice)


@dataclass(frozen=True)
class LegacyCleanupDefault:
    """The server-picked decision for a client that never asked the question.

    An app build already in the field (before the speech-cleanup UI shipped)
    has no way to submit an analysis id or choice. Enforce mode must never
    block it -- it silently applies the same default a checked/unchecked
    creator would land on most often.
    """

    analysis_id: uuid.UUID | None
    choice: SpeechCleanupFullChoice | None


def legacy_default_decision(
    current_cleanup: SpeechCleanupAnalysis | None,
) -> LegacyCleanupDefault:
    if current_cleanup is None:
        return LegacyCleanupDefault(None, None)
    if current_cleanup.status == "ready" and int(current_cleanup.candidate_count or 0) > 0:
        return LegacyCleanupDefault(current_cleanup.id, "keep_original")
    if current_cleanup.status in {"queued", "running", "failed"}:
        return LegacyCleanupDefault(current_cleanup.id, "create_without_cleanup")
    # "no_findings" (and any other terminal status): nothing to submit --
    # dispatch's own `enforced_for_source` re-check finds no findings to ask
    # about and proceeds without a choice, same as an aware creator would.
    return LegacyCleanupDefault(None, None)


def resolve_next_audio_mode(strategy: Any, item: PlanItem) -> str | None:
    """The `PlanItem.audio_mode` a strategy's `audio_strategy` implies.

    Shared by `decide_approval` (runtime-v2, applies the mutation early) and
    `_claim_approval_dispatch` (whose later call becomes a no-op once decide
    already applied it). Returns ``None`` for the `voiceover_required` case --
    the caller decides how to surface that (today, only the claim does; a
    caller that mutates the item earlier, like `decide_approval`, must skip
    its own mutation entirely and leave that failure to the claim).
    """

    if strategy.audio_strategy == "original_audio":
        return "original"
    if strategy.audio_strategy == "licensed_music":
        return "kria"
    if item.voiceover_gcs_path:
        return "voiceover"
    return None
