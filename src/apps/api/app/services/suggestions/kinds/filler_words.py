"""``filler_words``: switch on speech cleanup for a talking edit that has measurable fillers.

Evidence is the existing speech-cleanup preflight row (``SpeechCleanupAnalysis``): the engine's
own filler count and the seconds its cut plan removes. There is no second detector here, so the
card can never claim a filler the engine would not cut (the engine's voice guard and re-listen
have already dropped a "filler" that was really a word).

The action is ``ItemSettingAction(speech_cleanup_enabled=True)``. ``apply_item_setting`` is its
executor: it gates and flips the setting exactly like ``PATCH /plan-items/{id}`` and stops there.
The re-render is the normal approval flow, which stamps the ``required_v1`` cleanup contract
from the setting. It is deliberately not ``apply_speech_cut_candidate`` (cloud only, needs
pending candidates, refused with unsaved editor changes).

Enabling cleanup runs the whole cut plan, so long pauses go with the fillers. A plan that also
holds restarted sentences is never offered: those need the retakes consent the approval card
owns, which a one-tap card cannot give.

Loader contract (the serve lane): put ``load_evidence(...)``'s result in
``ctx.kind_inputs["filler_words"]``; without it the kind is simply unavailable.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.services.suggestion_chip_copy import language_of
from app.services.suggestions import registry
from app.services.suggestions.builders import PHONE
from app.services.suggestions.context import SuggestionContext, Target
from app.services.suggestions.models import (
    Action,
    ItemSettingAction,
    SuggestionKind,
    WitnessResult,
)

log = structlog.get_logger()

KIND_NAME = "filler_words"
INPUTS_KEY = "filler_words"
SETTING_FIELD = "speech_cleanup_enabled"

# Chosen from real takes (tests/evals/test_suggestion_kind_filler.py prints the table): the cafe
# take has 4 fillers and the engine cuts 1.76 s for them (~0.44 s each); the provolone takes
# have a hallucinated "uh" the engine drops, so they have none. Three fillers is the first count
# that is a pattern, not a slip, and it saves about 1.3 s, which a creator can hear in a short
# take. One or two stray "um"s are not worth a card.
MIN_FILLERS = 3
MIN_SAVED_MS = 1000

_TOUCHED = frozenset({"item:speech_cleanup"})


@dataclass(frozen=True)
class FillerEvidence:
    """What the card needs, read from bounded public columns only: no words, paths or spans."""

    enabled: bool = False
    capability: bool = False
    capability_reason: str | None = None
    # none | stale | queued | running | failed | no_findings | ready
    status: str = "none"
    decision: str | None = None
    filler_count: int = 0
    pause_count: int = 0
    retake_count: int = 0
    removed_ms: int = 0
    # Phone analysis proxy with other than one clip: the phone cannot cut that shape.
    phone_cannot_cut: bool = False


def _count(value: object) -> int:
    try:
        return max(0, int(value or 0))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _capability(item: Any, settings: Any):
    from app.services.speech_cleanup import (  # noqa: PLC0415
        capability_for_item,
        renderer_enabled_for_item,
    )

    return capability_for_item(
        item,
        mode=settings.speech_cleanup_mode,
        engine_enabled=settings.silence_cut_enabled,
        renderer_enabled=renderer_enabled_for_item(
            item,
            subtitled_enabled=settings.subtitled_archetype_enabled,
            talking_head_enabled=settings.edit_format_talking_head_enabled,
            narrated_self_narration_enabled=settings.narrated_self_narration_enabled,
        ),
    )


def evidence_for_item(
    item: Any, analysis: Any, *, current_fingerprint: str | None, settings: Any | None = None
) -> FillerEvidence:
    """The evidence of ``item``'s current analysis row. Pure: the caller does the reads.

    ``current_fingerprint`` is the source-policy fingerprint of the narration the renderer would
    use now; a row stamped with another one (a deploy bumped the detector, the source moved) is
    ``stale`` and counts for nothing.
    """
    if settings is None:
        from app.config import settings as live  # noqa: PLC0415

        settings = live
    capability = _capability(item, settings)
    base = {
        "enabled": bool(getattr(item, SETTING_FIELD, False)),
        "capability": capability.available,
        "capability_reason": capability.reason,
    }
    if analysis is None:
        return FillerEvidence(**base)
    if current_fingerprint is None or analysis.source_policy_fingerprint != current_fingerprint:
        return FillerEvidence(**base, status="stale")
    counts = analysis.category_counts if isinstance(analysis.category_counts, Mapping) else {}
    from app.kria.media_sources import is_analysis_proxy_path  # noqa: PLC0415

    return FillerEvidence(
        **base,
        status=str(analysis.status or "none"),
        decision=analysis.decision,
        filler_count=_count(counts.get("filler_sounds")),
        pause_count=_count(counts.get("long_pauses")),
        retake_count=_count(counts.get("retakes")),
        removed_ms=_count(analysis.estimated_removed_ms),
        phone_cannot_cut=bool(
            is_analysis_proxy_path(str(analysis.source_storage_path or ""))
            and len(getattr(item, "clip_gcs_paths", None) or []) != 1
        ),
    )


def _fingerprint(item: Any) -> str | None:
    from app.services.plan_item_media import (  # noqa: PLC0415
        current_detector_policy,
        resolve_item_narration,
    )

    source = resolve_item_narration(item, detector_policy=current_detector_policy()).source
    return source.source_policy_fingerprint if source is not None else None


async def load_evidence(db: Any, item: Any) -> FillerEvidence:
    """The read the serve and tap contexts make. Never raises: no evidence is no card."""
    from app.services.speech_cleanup_preflight import current_analysis_async  # noqa: PLC0415

    try:
        analysis = await current_analysis_async(db, item.id)
        return evidence_for_item(item, analysis, current_fingerprint=_fingerprint(item))
    except Exception:  # noqa: BLE001 - a card that cannot be proven is not offered
        log.warning("suggestion_filler.load_failed", exc_info=True)
        return FillerEvidence()


def load_evidence_sync(db: Any, item: Any) -> FillerEvidence:
    """``load_evidence`` for the worker, which commits a tap on a sync session."""
    from app.services.speech_cleanup_preflight import current_analysis_sync  # noqa: PLC0415

    try:
        analysis = current_analysis_sync(db, item.id)
        return evidence_for_item(item, analysis, current_fingerprint=_fingerprint(item))
    except Exception:  # noqa: BLE001
        log.warning("suggestion_filler.load_failed", exc_info=True)
        return FillerEvidence()


def _evidence(ctx: SuggestionContext) -> FillerEvidence | None:
    found = ctx.kind_inputs.get(INPUTS_KEY)
    return found if isinstance(found, FillerEvidence) else None


def unoffered_reason(
    evidence: FillerEvidence | None, *, settings: Any | None = None, enabled_ok: bool = False
) -> str | None:
    """Why this evidence must not become a card, or None when it may.

    ``enabled_ok`` lets the already-on case through so ``availability`` and ``already_present``
    stay two separate answers.
    """
    if settings is None:
        from app.config import settings as live  # noqa: PLC0415

        settings = live
    if evidence is None:
        return "no_evidence"
    if not settings.silence_cut_enabled:
        return "engine_disabled"
    if not evidence.capability:
        return f"unavailable:{evidence.capability_reason}"
    if evidence.enabled and not enabled_ok:
        return "already_enabled"
    if evidence.status != "ready":
        return f"analysis_{evidence.status}"
    if evidence.decision == "keep_original":
        return "creator_kept_original"
    if evidence.decision == "clean" and not enabled_ok:
        return "already_decided_clean"
    if evidence.phone_cannot_cut:
        return "phone_cannot_cut"
    if evidence.retake_count:
        return "retakes_need_consent"
    if evidence.filler_count < MIN_FILLERS:
        return "too_few_fillers"
    if evidence.removed_ms < MIN_SAVED_MS:
        return "saves_too_little"
    return None


def _available(ctx: SuggestionContext) -> bool:
    return unoffered_reason(_evidence(ctx), enabled_ok=True) is None


def _already_present(ctx: SuggestionContext) -> bool:
    evidence = _evidence(ctx)
    return evidence is not None and (evidence.enabled or evidence.decision == "clean")


def _build_action(
    ctx: SuggestionContext, targets: Sequence[Target], copy: str | None
) -> Action | None:
    if unoffered_reason(_evidence(ctx)) is not None:
        return None
    return ItemSettingAction(field=SETTING_FIELD, value=True)


def _witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    if not isinstance(action, ItemSettingAction) or action.field != SETTING_FIELD:
        return WitnessResult(ok=False, tier="setting", reason="not_cleanup_setting")
    if action.value is not True:
        return WitnessResult(ok=False, tier="setting", reason="not_turning_on")
    reason = unoffered_reason(_evidence(ctx))
    if reason is not None:
        return WitnessResult(ok=False, tier="setting", reason=reason)
    return WitnessResult(ok=True, tier="setting", touched=_TOUCHED)


def evidence_lines(evidence: FillerEvidence, language: str) -> list[str]:
    """Measured, closed-form card evidence: a count and a duration, never a transcript word."""
    seconds = evidence.removed_ms / 1000
    if language_of(language) == "tr":
        return [
            f"{evidence.filler_count} dolgu sesi",
            f"{seconds:.1f} sn kısalır".replace(".", ","),
        ]
    return [f"{evidence.filler_count} filler sounds", f"Saves {seconds:.1f} s"]


# ── the tap's executor ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingOutcome:
    applied: bool
    reason: str | None = None
    # Hand to ``plan_item_media.publish_preflight_after_commit`` once the caller commits.
    publish_analysis_id: uuid.UUID | None = None


def apply_item_setting(db: Any, item: Any, action: ItemSettingAction) -> SettingOutcome:
    """Turn cleanup on for ``item``, as the plan-item PATCH does. Does not commit or render.

    The caller holds ``item`` locked ``FOR UPDATE`` inside the transaction that commits the tap
    (the order ``PATCH`` takes) and runs the normal approval/render flow afterwards; the dispatch
    reads the setting and stamps ``required_v1``. The evidence is read again here, so a card
    served for an analysis that has since been replaced, declined or outgrown is refused instead
    of flipping a setting the creator was never offered. A refusal writes nothing.
    """
    if action.field != SETTING_FIELD or action.value is not True:
        return SettingOutcome(False, "unsupported_setting")
    kind = registry.get(KIND_NAME)
    if kind is None or not registry.enabled(kind):
        return SettingOutcome(False, "kind_disabled")
    reason = unoffered_reason(load_evidence_sync(db, item))
    if reason is not None:
        return SettingOutcome(False, reason)
    item.speech_cleanup_enabled = True
    item.user_edited = True
    from app.services.speech_cleanup_preflight import schedule_item_preflight_sync  # noqa: PLC0415

    return SettingOutcome(True, publish_analysis_id=schedule_item_preflight_sync(db, item))


KIND = SuggestionKind(
    kind=KIND_NAME,
    states=frozenset({"ready"}),
    required_signals=(),
    availability=_available,
    already_present=_already_present,
    build_action=_build_action,
    witness=_witness,
    surfaces=PHONE,
    flag="kria_suggestion_filler_words_enabled",
)
