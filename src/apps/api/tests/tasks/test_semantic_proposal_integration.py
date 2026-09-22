"""Semantic proposal failure fences at the task boundary."""

from __future__ import annotations

from types import SimpleNamespace

import app.tasks.edit_proposal_build as proposal_build
from app.schemas.edit_proposal import EditProposal


def test_semantic_failure_is_fail_closed_and_keeps_private_diagnostics(monkeypatch) -> None:
    item = SimpleNamespace(edit_proposal=None)
    proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="attempt",
        status="analyzing",
        planning_diagnostics={"schedule": {"total_frames": 90}},
    )
    monkeypatch.setattr("app.config.settings.edit_proposal_semantic_enabled", True)

    proposal_build._fail(item, proposal, "semantic_edit_infeasible", "too short")

    assert item.edit_proposal["status"] == "failed"
    assert item.edit_proposal["design_fallback"] == proposal_build.MAIN_CREATOR_FAIL_CLOSED
    assert item.edit_proposal["planning_diagnostics"]["schedule"]["total_frames"] == 90


def test_stale_attempt_fence_does_not_treat_drafting_as_active(monkeypatch) -> None:
    row = SimpleNamespace(
        ownership_epoch=1,
        edit_proposal=EditProposal(
            proposal_version=1, generation_attempt_id="old", status="drafting"
        ).model_dump(mode="json"),
    )
    db = SimpleNamespace(execute=lambda _query: SimpleNamespace(one_or_none=lambda: row))
    monkeypatch.setattr(proposal_build, "sync_session", lambda: _Context(db))

    assert proposal_build._attempt_is_active("item", "old", 1) is False


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False
