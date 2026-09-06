"""Media mutation must invalidate an approved edit proposal on any footage swap.

Before the chat-first preflight, ``set_item_clips`` compared
``main_footage_identity(item)`` -- the ordered ``(media_id, gcs_path)`` vector --
and marked the edit proposal stale whenever it changed.  The preflight facade
replaced that comparison with the narration *fingerprint*, which is ``None``
whenever no narration source resolves (montage formats, clips without audio).
For those items both fingerprints are ``None``, so a full footage replacement
looked unchanged and an approved proposal built against the old clips survived.

This regresses with the feature flag off, so it is pinned independently of any
speech-cleanup setting.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models import PlanItem
from app.schemas.edit_proposal import (
    ApprovedProposalSnapshot,
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.edit_proposals import parse_edit_proposal
from app.services.plan_item_media import current_detector_policy, mutate_plan_item_media


def _approved_proposal(path: str) -> dict:
    media_id = str(uuid.uuid4())
    media = MediaRef(lane="clip", media_id=media_id, gcs_path=path, generation="42", kind="video")
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Share what stood out",
        pace="balanced",
        duration_s=24,
        title="What I noticed",
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water set the pace.",
                media_ids=[media_id],
                duration_s=4,
            )
        ],
    )
    digest = canonical_media_digest([media])
    return EditProposal(
        proposal_version=3,
        generation_attempt_id=str(uuid.uuid4()),
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved=ApprovedProposalSnapshot(
            proposal_version=3,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    ).model_dump(mode="json")


def _montage_item(paths: list[str]) -> PlanItem:
    item = PlanItem()
    item.clip_assignments = [
        {"gcs_path": path, "shot_id": None, "media_id": f"m{index}"}
        for index, path in enumerate(paths)
    ]
    item.clip_gcs_paths = list(paths)
    # Deliberately outside SUPPORTED_EDIT_FORMATS: no narration source resolves,
    # so both narration fingerprints are None.
    item.edit_format = "montage"
    item.audio_mode = "kria"
    item.edit_proposal = _approved_proposal(paths[0])
    return item


def test_footage_replacement_stales_proposal_without_a_narration_source() -> None:
    item = _montage_item(["users/u/old-a.mp4", "users/u/old-b.mp4"])

    result = mutate_plan_item_media(
        item,
        detector_policy=current_detector_policy(),
        current_analysis=None,
        clip_assignments=[
            {"gcs_path": "users/u/new-x.mp4", "shot_id": None, "media_id": "n0"},
            {"gcs_path": "users/u/new-y.mp4", "shot_id": None, "media_id": "n1"},
        ],
    )

    # No narration source exists on either side, so the speech-cleanup lane
    # correctly reports no change...
    assert result.source_changed is False
    # ...but the footage really was replaced, so approval must not survive.
    assert parse_edit_proposal(item.edit_proposal).status == "stale"


def test_unrelated_metadata_edit_keeps_proposal_approved() -> None:
    """Only identity changes invalidate; notes/order metadata must not."""

    item = _montage_item(["users/u/old-a.mp4", "users/u/old-b.mp4"])

    mutate_plan_item_media(
        item,
        detector_policy=current_detector_policy(),
        current_analysis=None,
        clip_assignments=[
            {
                "gcs_path": "users/u/old-a.mp4",
                "shot_id": None,
                "media_id": "m0",
                "user_note": "keep the wide shot",
            },
            {"gcs_path": "users/u/old-b.mp4", "shot_id": None, "media_id": "m1"},
        ],
    )

    assert parse_edit_proposal(item.edit_proposal).status == "approved"
