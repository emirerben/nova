from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import app.services.edit_proposals as proposals
from app.schemas.edit_proposal import (
    ApprovedProposalSnapshot,
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)


class _AssetRows:
    def scalars(self):
        return self

    def all(self):
        return []


class _Session:
    def execute(self, _query):
        return _AssetRows()


def _approved_item() -> tuple[SimpleNamespace, MediaRef]:
    media = MediaRef(
        lane="clip",
        media_id="clip-1",
        gcs_path="users/u/plan/i/corfu.mp4",
        generation="42",
        kind="video",
    )
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
                media_ids=[media.media_id],
                duration_s=4,
            )
        ],
    )
    digest = canonical_media_digest(snapshot.media)
    proposal = EditProposal(
        proposal_version=3,
        generation_attempt_id="attempt-1",
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved=ApprovedProposalSnapshot(
            proposal_version=3,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    )
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": media.media_id, "gcs_path": media.gcs_path}],
        edit_proposal=proposal.model_dump(mode="json"),
    )
    return item, media


def test_generate_trust_boundary_rejects_replaced_clip_generation(monkeypatch) -> None:
    item, _media = _approved_item()
    monkeypatch.setattr(proposals, "media_generations_match_sync", lambda _refs: False)

    error, approved = proposals.validate_approved_proposal_media_sync(
        _Session(), item, owner_id=uuid.uuid4()
    )

    assert error == "proposal_stale"
    assert approved is None


def test_generate_trust_boundary_rejects_detached_media_identity(monkeypatch) -> None:
    item, _media = _approved_item()
    item.clip_assignments = [{"media_id": "replacement-id", "gcs_path": "users/u/plan/i/corfu.mp4"}]
    called = False

    def _unexpected_generation_check(_refs):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(proposals, "media_generations_match_sync", _unexpected_generation_check)

    error, approved = proposals.validate_approved_proposal_media_sync(
        _Session(), item, owner_id=uuid.uuid4()
    )

    assert error == "proposal_stale"
    assert approved is None
    assert called is False


def test_generate_trust_boundary_rejects_replaced_asset_generation(monkeypatch) -> None:
    asset_id = uuid.uuid4()
    media = MediaRef(
        lane="asset",
        media_id=str(asset_id),
        gcs_path="users/u/plan/i/pool/corfu.jpg",
        generation="42",
        kind="image",
    )
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        duration_s=15,
        title="Corfu",
        media=[media],
        story_beats=[
            StoryBeat(beat_id="food", topic="Food", media_ids=[str(asset_id)], duration_s=4)
        ],
    )
    digest = canonical_media_digest(snapshot.media)
    proposal = EditProposal(
        proposal_version=3,
        generation_attempt_id="attempt-1",
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved=ApprovedProposalSnapshot(
            proposal_version=3,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    )
    item = SimpleNamespace(
        id=uuid.uuid4(), clip_assignments=[], edit_proposal=proposal.model_dump(mode="json")
    )
    asset = SimpleNamespace(
        id=asset_id,
        gcs_path=media.gcs_path,
        gcs_generation=media.generation,
        kind="image",
    )

    class _AssetSession:
        def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [asset]))

    monkeypatch.setattr(proposals, "media_generations_match_sync", lambda _refs: False)
    error, approved = proposals.validate_approved_proposal_media_sync(
        _AssetSession(), item, owner_id=uuid.uuid4()
    )

    assert error == "proposal_stale"
    assert approved is None
