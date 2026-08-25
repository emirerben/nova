from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.edit_guide import EditGuideInput, EditGuideMediaSummary
from app.agents.edit_proposal import EditProposalAgentInput, EditProposalMedia
from app.schemas.edit_proposal import (
    MAX_EDIT_PROPOSAL_MEDIA,
    EditProposalSnapshot,
    EditProposalSnapshotResponse,
    MediaRef,
    StoryBeat,
)
from app.schemas.guided_edit_revision import guided_editor_revision_from_approval


def _refs(count: int) -> list[MediaRef]:
    return [
        MediaRef(
            lane="clip",
            media_id=f"clip-{index}",
            gcs_path=f"users/u/{index}.mp4",
            generation=str(index + 1),
            kind="video",
            duration_s=2,
        )
        for index in range(count)
    ]


def test_guided_edit_contract_accepts_all_item_media_and_rejects_more() -> None:
    refs = _refs(MAX_EDIT_PROPOSAL_MEDIA)
    agent_input = EditProposalAgentInput(
        direction="guided_story",
        goal="Show the full trip.",
        pace="balanced",
        target_duration_s=24,
        media=[
            EditProposalMedia(media_id=ref.media_id, lane=ref.lane, kind=ref.kind) for ref in refs
        ],
    )
    snapshot = EditProposalSnapshot(
        duration_s=24,
        title="Trip",
        media=refs,
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Start",
                media_ids=[refs[0].media_id],
                duration_s=4,
            )
        ],
    )
    response = EditProposalSnapshotResponse.model_validate(snapshot.model_dump())
    guide_input = EditGuideInput(
        phase="briefing",
        media=[EditGuideMediaSummary(kind="video") for _ in refs],
    )
    revision = guided_editor_revision_from_approval(
        proposal_version=1,
        media_digest="a" * 64,
        snapshot={"media": [ref.model_dump() for ref in refs]},
        execution_plan={
            "story_timeline": [
                {
                    "moment_id": "moment-1",
                    "media_id": refs[0].media_id,
                    "duration_s": 2,
                    "source_end_s": 2,
                    "output_end_s": 2,
                }
            ]
        },
    )

    assert len(agent_input.media) == MAX_EDIT_PROPOSAL_MEDIA
    assert len(snapshot.media) == MAX_EDIT_PROPOSAL_MEDIA
    assert len(response.media) == MAX_EDIT_PROPOSAL_MEDIA
    assert len(guide_input.media) == MAX_EDIT_PROPOSAL_MEDIA
    assert len(revision["sources"]) == MAX_EDIT_PROPOSAL_MEDIA

    overflow = _refs(MAX_EDIT_PROPOSAL_MEDIA + 1)
    with pytest.raises(ValidationError):
        EditProposalAgentInput(
            direction="guided_story",
            goal="Show the full trip.",
            pace="balanced",
            target_duration_s=24,
            media=[
                EditProposalMedia(media_id=ref.media_id, lane=ref.lane, kind=ref.kind)
                for ref in overflow
            ],
        )
    with pytest.raises(ValidationError):
        EditProposalSnapshot(
            duration_s=24,
            title="Trip",
            media=overflow,
            story_beats=[
                StoryBeat(
                    beat_id="beat-1",
                    topic="Start",
                    media_ids=[overflow[0].media_id],
                    duration_s=4,
                )
            ],
        )
    with pytest.raises(ValidationError):
        EditProposalSnapshotResponse(
            duration_s=24,
            title="Trip",
            media=overflow,
            story_beats=[
                StoryBeat(
                    beat_id="beat-1",
                    topic="Start",
                    media_ids=[overflow[0].media_id],
                    duration_s=4,
                )
            ],
        )
    with pytest.raises(ValidationError):
        EditGuideInput(
            phase="briefing",
            media=[EditGuideMediaSummary(kind="video") for _ in overflow],
        )
    with pytest.raises(ValidationError):
        guided_editor_revision_from_approval(
            proposal_version=1,
            media_digest="a" * 64,
            snapshot={"media": [ref.model_dump() for ref in overflow]},
            execution_plan={
                "story_timeline": [
                    {
                        "moment_id": "moment-1",
                        "media_id": overflow[0].media_id,
                        "duration_s": 2,
                        "source_end_s": 2,
                        "output_end_s": 2,
                    }
                ]
            },
        )
