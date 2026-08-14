from types import SimpleNamespace

import pytest

from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.edit_proposals import (
    ProposalConflictError,
    approve_proposal,
    begin_proposal_attempt,
    mark_edit_proposal_stale,
    proposal_generate_error,
    save_proposal_draft,
)
from app.services.plan_clips import ensure_clip_media_ids


def _item(**overrides):
    values = {"clip_assignments": [], "edit_proposal": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot() -> EditProposalSnapshot:
    media = [
        MediaRef(
            lane="clip",
            media_id="clip-1",
            gcs_path="users/u/plan/i/corfu.mp4",
            generation="42",
            kind="video",
        )
    ]
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Share what stood out in Corfu",
        pace="balanced",
        duration_s=24,
        title="What I noticed in Corfu",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Coast",
                thought="The water set the pace for the whole trip.",
                media_ids=["clip-1"],
                duration_s=4,
            )
        ],
    )


def test_canonical_digest_ignores_editorial_order_but_tracks_generation() -> None:
    a = MediaRef(lane="clip", media_id="a", gcs_path="a.mp4", generation="1", kind="video")
    b = MediaRef(lane="asset", media_id="b", gcs_path="b.jpg", generation="2", kind="image")
    assert canonical_media_digest([a, b]) == canonical_media_digest([b, a])
    assert canonical_media_digest([a, b]) != canonical_media_digest(
        [a, b.model_copy(update={"generation": "3"})]
    )


def test_clip_ids_are_backfilled_once() -> None:
    item = _item(clip_assignments=[{"gcs_path": "one.mp4", "shot_id": None}])
    assert ensure_clip_media_ids(item) is True
    media_id = item.clip_assignments[0]["media_id"]
    assert ensure_clip_media_ids(item) is False
    assert item.clip_assignments[0]["media_id"] == media_id


def test_draft_approval_and_media_stale_retain_last_approval() -> None:
    item = _item()
    analyzing = begin_proposal_attempt(item)
    snapshot = _snapshot()
    # The drafting task owns the immutable media digest.
    raw = dict(item.edit_proposal)
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["status"] = "drafting"
    item.edit_proposal = raw

    draft = save_proposal_draft(
        item, expected_version=analyzing.proposal_version, snapshot=snapshot
    )
    approved = approve_proposal(item, expected_version=draft.proposal_version)
    assert proposal_generate_error(item) is None
    assert approved.last_approved is not None

    assert mark_edit_proposal_stale(item) is True
    assert proposal_generate_error(item) == "proposal_stale"
    assert item.edit_proposal["last_approved"]["snapshot"]["title"] == snapshot.title


def test_compare_and_swap_rejects_lost_update() -> None:
    item = _item()
    current = begin_proposal_attempt(item)
    with pytest.raises(ProposalConflictError, match="another tab"):
        save_proposal_draft(
            item,
            expected_version=current.proposal_version - 1,
            snapshot=_snapshot(),
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("analyzing", "proposal_analyzing"),
        ("drafting", "proposal_analyzing"),
        ("draft", "proposal_draft"),
        ("failed", "proposal_draft"),
        ("stale", "proposal_stale"),
    ],
)
def test_generate_error_codes_are_stable(status: str, expected: str) -> None:
    item = _item()
    proposal = begin_proposal_attempt(item)
    item.edit_proposal = {**item.edit_proposal, "status": status}
    assert item.edit_proposal["proposal_version"] == proposal.proposal_version
    assert proposal_generate_error(item) == expected
