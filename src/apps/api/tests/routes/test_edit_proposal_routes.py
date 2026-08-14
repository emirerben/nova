from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.routes.plan_items as plan_items
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
    parse_edit_proposal,
)


def _snapshot() -> EditProposalSnapshot:
    media = MediaRef(
        lane="clip",
        media_id="clip-1",
        gcs_path="users/u/plan/i/corfu.mp4",
        generation="42",
        kind="video",
    )
    return EditProposalSnapshot(
        direction="guided_story",
        goal="Share what stood out",
        pace="balanced",
        duration_s=24,
        title="What I noticed in Corfu",
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water set the pace.",
                media_ids=[media.media_id],
                duration_s=4,
            )
        ],
    )


def _draft_item() -> SimpleNamespace:
    snapshot = _snapshot()
    proposal = EditProposal(
        proposal_version=2,
        generation_attempt_id="attempt-1",
        media_digest=canonical_media_digest(snapshot.media),
        status="draft",
        brief=ProposalBrief(),
        draft=snapshot,
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[
            {
                "media_id": snapshot.media[0].media_id,
                "gcs_path": snapshot.media[0].gcs_path,
            }
        ],
        edit_proposal=proposal.model_dump(mode="json"),
    )


def _patch_route_dependencies(monkeypatch, item, *, media_current: bool) -> AsyncMock:
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(
        plan_items,
        "_proposal_media_is_current",
        AsyncMock(return_value=media_current),
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    return AsyncMock()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/plan-items/item/edit-proposal/draft",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )


@pytest.mark.asyncio
async def test_draft_requires_media_before_dispatch(monkeypatch) -> None:
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[], edit_proposal=None)
    plan = SimpleNamespace(ownership_epoch=4)
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    count_result = SimpleNamespace(scalar_one=lambda: 0)
    db = AsyncMock()
    db.execute.return_value = count_result

    with pytest.raises(HTTPException) as exc:
        await plan_items.draft_item_edit_proposal(
            _request(),
            str(item.id),
            plan_items.DraftEditProposalBody(),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_required"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_double_click_reuses_active_attempt(monkeypatch) -> None:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"gcs_path": "users/u/plan/i/corfu.mp4"}],
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt-1",
            status="analyzing",
        ).model_dump(mode="json"),
    )
    monkeypatch.setattr(plan_items.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=4), SimpleNamespace())),
    )
    monkeypatch.setattr(plan_items, "plan_item_response", lambda loaded: loaded)
    db = AsyncMock()

    response = await plan_items.draft_item_edit_proposal(
        _request(),
        str(item.id),
        plan_items.DraftEditProposalBody(),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )

    assert response is item
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rejects_stale_compare_and_swap_version(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    body = plan_items.UpdateEditProposalBody(
        expected_proposal_version=1,
        snapshot=_snapshot(),
    )

    with pytest.raises(HTTPException) as exc:
        await plan_items.update_item_edit_proposal(
            str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_conflict"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_discards_client_supplied_media_analysis(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    snapshot = _snapshot()
    snapshot.media[0].analysis = {"invented": "client-controlled"}
    body = plan_items.UpdateEditProposalBody(
        expected_proposal_version=2,
        snapshot=snapshot,
    )

    await plan_items.update_item_edit_proposal(
        str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.draft is not None
    assert persisted.draft.media[0].analysis == {}


@pytest.mark.asyncio
async def test_approve_marks_plan_stale_when_media_identity_changed(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=False)
    body = plan_items.ApproveEditProposalBody(expected_proposal_version=2)

    with pytest.raises(HTTPException) as exc:
        await plan_items.approve_item_edit_proposal(
            str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "proposal_stale"
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "stale"
    assert persisted.proposal_version == 3
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_current_draft_persists_immutable_approval(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    body = plan_items.ApproveEditProposalBody(expected_proposal_version=2)

    response = await plan_items.approve_item_edit_proposal(
        str(item.id), body, SimpleNamespace(id=uuid.uuid4()), db
    )

    assert response is item
    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None and persisted.status == "approved"
    assert persisted.proposal_version == 3
    assert persisted.last_approved is not None
    assert persisted.last_approved.snapshot.title == "What I noticed in Corfu"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_validation_rejects_replaced_pool_asset_object(monkeypatch) -> None:
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
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[])
    asset = SimpleNamespace(
        id=asset_id,
        gcs_path=media.gcs_path,
        gcs_generation=media.generation,
        kind="image",
    )
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [asset]))
    db = AsyncMock()
    db.execute.return_value = rows
    monkeypatch.setattr(
        plan_items.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(generation="replacement-generation"),
    )

    assert (
        await plan_items._proposal_media_is_current(item, snapshot, db, user_id=uuid.uuid4())
        is False
    )
