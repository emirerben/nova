"""KRI-126 Part 2: make the silent deterministic-fallback draft visible.

Before this change, `_run_draft_attempt`'s ``except TerminalError`` branch set
a local ``fallback_used`` flag and logged ``edit_proposal.deterministic_
fallback``, but nothing was persisted -- the admin debug endpoint had no way
to show that a draft came from the deterministic recovery path rather than
the real specialist. These tests cover:

  - a TerminalError-triggered fallback draft persists ``planner_fallback``
    (reason/direction/at) on the ``EditProposal`` envelope;
  - a later successful (non-fallback) draft clears it back to ``None``;
  - the field never reaches the public/OpenAPI-visible ``EditProposalResponse``
    model that backs plan-item and proposal GET routes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import app.tasks.edit_proposal_build as proposal_build
from app.schemas.edit_proposal import (
    EditProposal,
    EditProposalResponse,
    ProposalBrief,
    ProposalPlannerFallback,
    parse_edit_proposal,
)
from tests.tasks.test_edit_proposal_build import (
    _PROD_CLIP_ASSIGNMENT,
    _prepare_terminal_agent_attempt,
    _proposal,
)


def test_terminal_error_fallback_persists_planner_fallback_marker(monkeypatch) -> None:
    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "draft"
    assert persisted.planner_fallback is not None
    assert persisted.planner_fallback.direction == "guided_story"
    assert "malformed provider media ref" in persisted.planner_fallback.reason


def test_terminal_error_fallback_marker_survives_auto_approval(monkeypatch) -> None:
    """auto_finalize=True still reaches "approved"; the marker must persist."""

    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=True
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.planner_fallback is not None
    assert persisted.planner_fallback.direction == "guided_story"


def test_successful_draft_after_fallback_clears_the_marker(monkeypatch) -> None:
    """A stale marker from an earlier attempt must not survive a real draft."""

    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)
    stale = _proposal(brief=ProposalBrief(direction="guided_story", duration_s=6))
    stale["planner_fallback"] = {
        "reason": "a previous, unrelated planner crash",
        "direction": "guided_story",
        "at": "2026-01-01T00:00:00+00:00",
    }
    item.edit_proposal = stale

    def _run(agent, agent_input, ctx=None):  # noqa: ANN001, ARG001
        return agent.parse(
            json.dumps(
                {
                    "title": "The Acropolis",
                    "duration_s": 6,
                    "story_beats": [
                        {
                            "topic": "Architecture",
                            "thought": "The Acropolis",
                            "media_ids": [str(_PROD_CLIP_ASSIGNMENT["media_id"])],
                            "duration_s": 6,
                        }
                    ],
                }
            ),
            agent_input,
        )

    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _run)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "draft"
    assert persisted.planner_fallback is None


def test_manual_correction_after_fallback_clears_the_marker(monkeypatch) -> None:
    """PATCH /edit-proposal saves through save_proposal_draft: a creator's own
    corrected draft is not a fallback and must not inherit the marker."""

    from app.services.edit_proposals import save_proposal_draft  # noqa: PLC0415

    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)
    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )
    fallback_draft = parse_edit_proposal(item.edit_proposal)
    assert fallback_draft is not None
    assert fallback_draft.planner_fallback is not None
    assert fallback_draft.draft is not None

    save_proposal_draft(
        item,
        expected_version=fallback_draft.proposal_version,
        snapshot=fallback_draft.draft,
        clear_approval_mode=True,
    )

    corrected = parse_edit_proposal(item.edit_proposal)
    assert corrected is not None
    assert corrected.planner_fallback is None


def test_planner_fallback_never_reaches_the_public_response_model() -> None:
    """EditProposalResponse backs plan-item/proposal GET routes -- it must
    redact planner_fallback the same way it already redacts
    conversation_attempt."""

    envelope = EditProposal(
        proposal_version=1,
        generation_attempt_id="attempt-1",
        status="draft",
        brief=ProposalBrief(),
        planner_fallback=ProposalPlannerFallback(
            reason="the guided specialist crashed",
            direction="guided_story",
            at="2026-01-01T00:00:00+00:00",
        ),
    )
    assert envelope.planner_fallback is not None

    response = EditProposalResponse.model_validate(envelope.model_dump(mode="json"))

    assert response.planner_fallback is None
    # The field key may still appear (null), but the fallback content itself
    # -- an admin-only diagnostic that can carry model output -- must never
    # reach a public/OpenAPI-visible payload.
    assert "the guided specialist crashed" not in json.dumps(response.model_dump(mode="json"))


def test_authored_plan_the_renderer_cannot_allocate_recovers_at_planning_time(monkeypatch) -> None:
    """KRI-129: the planner repairs instead of rejecting, so every authored
    draft is dry-run through the compiler. One that cannot be allocated must
    become a visible, marked recovery now, not a render failure after approval."""

    from app.pipeline import guided_story  # noqa: PLC0415

    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)

    def _run(agent, agent_input, ctx=None):  # noqa: ANN001, ARG001
        return agent.parse(
            json.dumps(
                {
                    "title": "The Acropolis",
                    "duration_s": 6,
                    "story_beats": [
                        {
                            "topic": "Architecture",
                            "thought": "The Acropolis",
                            "media_ids": [str(_PROD_CLIP_ASSIGNMENT["media_id"])],
                            "duration_s": 6,
                        }
                    ],
                }
            ),
            agent_input,
        )

    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _run)
    calls = {"count": 0}
    real = guided_story.validate_proposal_compiles

    def _first_draft_cannot_compile(snapshot):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            raise guided_story.GuidedStoryError(
                "guided_story_duration_impossible", "too short to show all approved media"
            )
        return real(snapshot)

    monkeypatch.setattr(guided_story, "validate_proposal_compiles", _first_draft_cannot_compile)

    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )

    persisted = parse_edit_proposal(item.edit_proposal)
    assert persisted is not None
    assert persisted.status == "draft"
    assert persisted.planner_fallback is not None
    assert "render dry run" in persisted.planner_fallback.reason
    assert persisted.draft is not None
    assert persisted.draft.story_beats[0].beat_id.startswith("fallback-beat-")
