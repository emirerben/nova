"""Plan requested labels once against the immutable narration and visual timeline."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents.narration_annotations import NarrationAnnotationAgent, NarrationAnnotationInput
from app.pipeline.guided_story import GuidedStoryError
from app.pipeline.narration_labels import materialize_narration_labels
from app.schemas.edit_proposal import EditProposalSnapshot


def materialize_guided_narration_labels(
    plan: dict[str, Any],
    *,
    snapshot: EditProposalSnapshot,
    strategy: dict[str, Any],
    creator_request: str,
    job_id: str,
) -> dict[str, Any]:
    narration = snapshot.narration
    if narration is None:
        return plan
    requirements = {
        "participant_labels": strategy.get("participant_labels", "none"),
        "score_labels": bool(strategy.get("score_labels")),
        "sport_labels": bool(strategy.get("sport_labels")),
    }
    if not any(
        (
            requirements["participant_labels"] != "none",
            requirements["score_labels"],
            requirements["sport_labels"],
        )
    ):
        return plan
    receipt = plan.get("narration_label_receipt")
    if (
        isinstance(receipt, dict)
        and receipt.get("requirements") == requirements
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
