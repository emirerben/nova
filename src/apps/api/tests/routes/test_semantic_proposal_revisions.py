"""Public proposal responses never leak semantic planner diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.routes.plan_items as plan_items
from app.routes.plan_items import _edit_proposal_response
from app.schemas.edit_frame_schedule import EditFrameSchedule, FrameScheduledMoment
from app.schemas.edit_proposal import EditProposal
from tests.routes.test_edit_proposal_routes import (
    _draft_item,
    _patch_route_dependencies,
    _snapshot,
)


def test_public_response_redacts_non_null_planning_diagnostics() -> None:
    item = SimpleNamespace(
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt",
            status="failed",
            planning_diagnostics={"failure_reason": "private scheduler evidence"},
        ).model_dump(mode="json"),
        clip_assignments=[],
        id="item",
    )

    payload = _edit_proposal_response(item)

    assert payload is not None
    assert "planning_diagnostics" not in payload


def test_public_response_still_serializes_when_private_diagnostics_exist() -> None:
    item = SimpleNamespace(
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt",
            status="analyzing",
            planning_diagnostics={"requested_frames": 90},
        ).model_dump(mode="json"),
        clip_assignments=[],
        id="item",
    )

    assert _edit_proposal_response(item)["status"] == "analyzing"


@pytest.mark.asyncio
async def test_patch_discards_forged_schedule_before_server_refresh(monkeypatch) -> None:
    item = _draft_item()
    db = _patch_route_dependencies(monkeypatch, item, media_current=True)
    body = plan_items.UpdateEditProposalBody(expected_proposal_version=2, snapshot=_snapshot())
    forged_schedule = EditFrameSchedule(
        total_frames=720,
        transition_frames=0,
        direction="guided_story",
        moments=[
            FrameScheduledMoment(
                moment_id="coast",
                beat_id="coast",
                media_id="clip-1",
                source_start_frame=1,
                source_end_frame=721,
                output_start_frame=0,
                output_end_frame=720,
                role="hook",
            )
        ],
    )
    forged = body.snapshot.model_copy(update={"frame_schedule": forged_schedule})
    body = plan_items.UpdateEditProposalBody(expected_proposal_version=2, snapshot=forged)
    seen = {}

    def save(_item, *, snapshot, **_kwargs):
        seen["schedule"] = snapshot.frame_schedule
        return None

    monkeypatch.setattr("app.services.edit_proposals.save_proposal_draft", save)
    await plan_items.update_item_edit_proposal(str(item.id), body, SimpleNamespace(id=1), db)
    assert seen["schedule"] is None
