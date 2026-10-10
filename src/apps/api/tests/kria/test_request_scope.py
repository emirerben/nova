"""KRI-524 scope-consensus regressions for rendered brief follow-ups."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.brief_extractor import BriefExtractionOutput
from app.agents._schemas.creator_agent import AskUser
from app.kria import planner
from app.kria.brief import BriefUpdateBatchError, CurrentPlanShape, route_requirements
from app.kria.planner import _extraction_request_scope
from tests.kria.test_creative_brief import _ASK, _wire_planner


def test_conflicting_batch_scopes_fail_before_routing_or_ledger_application() -> None:
    """The caller resolves scope before applying any extracted updates."""
    with pytest.raises(BriefUpdateBatchError, match="batches disagree"):
        _extraction_request_scope(
            [
                BriefExtractionOutput(request_scope="edit"),
                BriefExtractionOutput(request_scope="rebuild"),
            ]
        )


def test_clarification_batches_need_one_shared_question() -> None:
    shared = "Should I revise the title or remake the video?"
    assert _extraction_request_scope(
        [
            BriefExtractionOutput(request_scope="clarify", clarification=shared),
            BriefExtractionOutput(request_scope="clarify", clarification=shared),
        ]
    ) == ("clarify", shared)

    with pytest.raises(BriefUpdateBatchError, match="clarification is ambiguous"):
        _extraction_request_scope(
            [
                BriefExtractionOutput(request_scope="clarify", clarification=shared),
                BriefExtractionOutput(
                    request_scope="clarify", clarification="Which clip should change?"
                ),
            ]
        )


def test_unscoped_legacy_fixture_remains_an_edit_compatibility_path() -> None:
    assert _extraction_request_scope([BriefExtractionOutput()]) == ("edit", None)


def test_semantic_edit_scope_disables_legacy_try_again_replan_regex() -> None:
    shape = CurrentPlanShape(has_render=True, can_edit_text=True)
    assert route_requirements([], shape, message="Try again", full_replan=False) == "editor_ops"


def test_semantic_rebuild_routes_without_an_editor_target() -> None:
    assert route_requirements([], CurrentPlanShape(has_render=False), full_replan=True) == "replan"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,has_target,expected",
    [("edit", True, "editor_ops"), ("rebuild", False, "replan"), ("clarify", True, None)],
)
async def test_deferred_extraction_retains_semantic_scope(
    monkeypatch, scope, has_target, expected
) -> None:
    output = SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])
    db, item, creator_id, copilot, creator_runs = _wire_planner(
        monkeypatch,
        output=output,
        editor_plan=None,
        snapshot={"text_bars": [{"text": "A walk"}]} if has_target else None,
    )
    monkeypatch.setattr(
        planner,
        "_call_brief_extractor",
        AsyncMock(
            return_value=BriefExtractionOutput(
                request_scope=scope,
                clarification="Which change should I retry?" if scope == "clarify" else None,
            )
        ),
    )
    updates, route = await planner.extract_deferred_brief(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="Try again",
    )
    assert updates == ()
    assert route == expected
    copilot.assert_not_awaited()
    assert not creator_runs
