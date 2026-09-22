"""Plan requested labels once against the immutable narration and visual timeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.creator_agent import CreativeStrategy, legacy_clip_intents
from app.agents.narration_annotations import NarrationAnnotationAgent, NarrationAnnotationInput
from app.pipeline.guided_story import GuidedStoryError
from app.pipeline.narration_labels import NarrationLabelRequirements, materialize_narration_labels
from app.schemas.edit_proposal import EditProposalSnapshot


def _parse_strategy(
    strategy: Mapping[str, Any] | CreativeStrategy,
    *,
    has_narration: bool,
) -> CreativeStrategy:
    """Validate the strategy, preserving the historical narrated sport lane."""

    if isinstance(strategy, CreativeStrategy):
        return strategy
    raw = _legacy_mapper_payload(strategy, has_narration=has_narration)
    return CreativeStrategy.model_validate(raw)


def _legacy_mapper_payload(
    strategy: Mapping[str, Any] | CreativeStrategy,
    *,
    has_narration: bool,
) -> dict[str, Any]:
    """Prepare a raw legacy payload for the shared schema mapper."""

    raw = dict(strategy) if isinstance(strategy, Mapping) else {}
    # Before KRI-156, guided voiceover requests stored ``sport_labels`` but
    # did not consistently retain the execution contract.  In the only
    # historical lane where that field meant spoken topic labels, restore
    # the contract before the schema's legacy mapper consumes it.
    if (
        has_narration
        and not raw.get("clip_intents")
        and (
            raw.get("sport_labels") is True
            or (
                isinstance(raw.get("context_label"), Mapping)
                and raw["context_label"].get("kind") == "sport"
            )
        )
        and raw.get("execution_contract") is None
    ):
        raw["execution_contract"] = "guided_voiceover_v1"
    return raw


def _intent_identity(intents: list[Any]) -> list[dict[str, str]]:
    return [
        {
            "intent_id": intent.intent_id,
            "op": intent.op,
            "attribute": intent.attribute,
            "label_source": intent.label_source,
            "transcript_kind": intent.transcript_kind or "",
        }
        for intent in intents
        if intent.label_source == "transcript" and intent.op == "label"
    ]


def _transcript_intent_identity(
    strategy: Mapping[str, Any] | CreativeStrategy,
    *,
    has_narration: bool,
) -> list[dict[str, str]]:
    """The semantic request identity for receipt replay, excluding visual data."""

    parsed = _parse_strategy(strategy, has_narration=has_narration)
    # ``resolved_clip_intents`` deliberately has no role here. It is a visual
    # resolver receipt and cannot make a spoken-transcript label appear.
    return _intent_identity(parsed.clip_intents or [])


def _transcript_requirements(
    strategy: Mapping[str, Any] | CreativeStrategy,
    *,
    has_narration: bool,
) -> dict[str, Any]:
    """Translate only transcript-owned intents into the legacy materializer API."""

    kinds = {
        intent["transcript_kind"]
        for intent in _transcript_intent_identity(strategy, has_narration=has_narration)
    }
    return {
        "participant_labels": "single_subject" if "participant" in kinds else "none",
        "score_labels": "score" in kinds,
        # The current materializer treats this as a generic topic-enabled
        # switch. The intent schema, rather than this compatibility adapter,
        # carries the creator's semantic attribute.
        "context_labels": ["topic"] if "topic" in kinds else [],
    }


def _is_legacy_transcript_strategy(strategy: Mapping[str, Any] | CreativeStrategy) -> bool:
    """Whether an identity-less historical receipt can still be replayed safely."""

    identities = _transcript_intent_identity(strategy, has_narration=True)
    if not identities:
        return False
    if isinstance(strategy, Mapping) and any(
        key in strategy
        for key in ("participant_labels", "score_labels", "sport_labels", "context_label")
    ):
        expected = _intent_identity(
            legacy_clip_intents(_legacy_mapper_payload(strategy, has_narration=True))
        )
        return identities == expected

    # A typed strategy may already have crossed the legacy read adapter. Build
    # the canonical old mappings through that same adapter, then accept only an
    # exact member (including its historical attribute), never a legacy-looking
    # ID paired with new semantics.
    canonical = _intent_identity(
        legacy_clip_intents(
            {
                "participant_labels": "single_subject",
                "score_labels": True,
                "sport_labels": True,
                "execution_contract": "guided_voiceover_v1",
            }
        )
    )
    return all(intent in canonical for intent in identities)


def _receipt_requirements(value: object) -> dict[str, Any] | None:
    """Normalize old and new receipt shapes before deciding whether to replay.

    Old receipts stored the materializer's closed requirements directly. Newer
    receipts may contain the strategy-shaped generic intent list. Both reduce
    to the same three internal switches, so a receipt cannot mask a newly
    requested transcript kind on a repair pass.
    """

    if not isinstance(value, Mapping):
        return None
    raw = dict(value)
    if "clip_intents" in raw:
        return _transcript_requirements(raw, has_narration=True)

    legacy = NarrationLabelRequirements.from_value(raw)
    context = raw.get("context_label")
    topic_requested = legacy.requests("topic") or (
        isinstance(context, Mapping) and context.get("kind") == "sport"
    )
    return {
        "participant_labels": legacy.participant_labels,
        "score_labels": legacy.score_labels,
        "context_labels": ["topic"] if topic_requested else [],
    }


def _receipt_intent_identity(receipt: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """Read new identity fields and the temporary embedded-intent receipt form."""

    identity = receipt.get("clip_intents")
    if isinstance(identity, list):
        return identity
    requirements = receipt.get("requirements")
    if isinstance(requirements, Mapping) and "clip_intents" in requirements:
        return _transcript_intent_identity(requirements, has_narration=True)
    return None


def materialize_guided_narration_labels(
    plan: dict[str, Any],
    *,
    snapshot: EditProposalSnapshot,
    strategy: Mapping[str, Any] | CreativeStrategy,
    creator_request: str,
    job_id: str,
) -> dict[str, Any]:
    narration = snapshot.narration
    if narration is None:
        return plan
    requirements = _transcript_requirements(strategy, has_narration=True)
    intent_identity = _transcript_intent_identity(strategy, has_narration=True)
    if not any(
        (
            requirements["participant_labels"] != "none",
            requirements["score_labels"],
            requirements["context_labels"],
        )
    ):
        return plan
    receipt = plan.get("narration_label_receipt")
    receipt_identity = _receipt_intent_identity(receipt) if isinstance(receipt, Mapping) else None
    can_replay_legacy_receipt = receipt_identity is None and _is_legacy_transcript_strategy(
        strategy
    )
    if (
        isinstance(receipt, dict)
        and _receipt_requirements(receipt.get("requirements")) == requirements
        and (receipt_identity == intent_identity or can_replay_legacy_receipt)
        and isinstance(plan.get("narration_label_text_elements"), list)
    ):
        # The caller validated this immutable execution plan against its
        # approved snapshot. Reuse its authored labels on repair/read/retry.
        return plan
    words = [
        {"word_id": f"w{index:06d}", **word.model_dump(mode="json")}
        for index, word in enumerate(narration.words)
    ]
    if not words:
        raise GuidedStoryError(
            "guided_narration_transcript_missing",
            "The requested narration labels need a transcript of your recording.",
        )
    timeline = [
        {**moment, "timeline_id": moment["moment_id"], "asset_id": moment["media_id"]}
        for moment in plan["story_timeline"]
    ]
    analysis = [
        {**ref.analysis, "asset_id": ref.media_id, "kind": ref.kind} for ref in snapshot.media
    ]
    if requirements["participant_labels"] == "single_subject":
        from app.services.guided_narration_focus import (
            analyze_guided_participant_focus,  # noqa: PLC0415
        )

        timeline = analyze_guided_participant_focus(timeline, job_id=job_id)
    try:
        output = NarrationAnnotationAgent(default_client()).run(
            NarrationAnnotationInput(
                creator_request=creator_request,
                words=words,
                timeline=timeline,
                media_analysis=analysis,
                requirements=requirements,
            ),
            ctx=RunContext(job_id=job_id),
        )
    except Exception as exc:  # noqa: BLE001 - requested labels must not silently disappear
        raise GuidedStoryError(
            "guided_narration_labels_unavailable",
            "The requested labels could not be grounded in your recording. Please retry.",
        ) from exc
    result = materialize_narration_labels(
        words,
        timeline,
        analysis,
        requirements,
        output.annotations,
    )
    return {
        **plan,
        "narration_label_text_elements": list(result.elements),
        "narration_label_receipt": {
            "requirements": requirements,
            "clip_intents": intent_identity,
            "accepted": [asdict(row) for row in result.accepted],
            "rejected": [asdict(row) for row in result.rejected],
            "participant_focus": [
                {
                    "timeline_id": row["timeline_id"],
                    "asset_id": row["asset_id"],
                    "single_subject": row.get("single_subject"),
                    "evidence": row.get("focus_evidence"),
                }
                for row in timeline
                if "focus_evidence" in row
            ],
        },
    }
