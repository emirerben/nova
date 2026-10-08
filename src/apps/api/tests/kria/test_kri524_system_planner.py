"""KRI-524 cross-layer planner regressions.

These tests exercise the offline planner boundary rather than a model response.  The
typed brief update is the contract the Main Creator would persist; the test ensures
that a copilot shortcut cannot commit a text-only subset before that contract is
available for routing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import AskUser
from app.kria import planner
from app.kria.brief import (
    BriefUpdate,
    CurrentPlanShape,
    plan_shape_from_editor_snapshot,
    route_requirements,
)
from app.kria.planner import plan_live_turn
from tests.kria.test_creative_brief import _ASK, _ops_plan, _upd, _wire_planner


def _shape(*, timeline: bool = False, audio: bool = False) -> CurrentPlanShape:
    return CurrentPlanShape(
        has_render=True,
        has_per_clip_text_lane=True,
        can_fill_per_clip_text=True,
        can_edit_timeline=timeline,
        can_order_by_capture_time=timeline,
        can_edit_audio=audio,
    )


def test_supported_audio_and_title_style_are_composed_by_editor_route() -> None:
    """Independent supported families must not force a generic recovery route."""

    reqs = [
        _upd("audio", "global", description="use the selected song"),
        _upd("style", "title", description="make the title smaller"),
    ]

    # The current route treats every non-text/style/timing mixture as replan,
    # even when the editor snapshot advertises the corresponding operations.
    assert route_requirements(reqs, _shape(audio=True)) == "editor_ops"


def test_explicit_clip_order_and_title_style_are_composed() -> None:
    reqs = [
        _upd("order", "global", facts={"key": "position", "index": 1}),
        _upd("style", "title", description="make the title smaller"),
    ]
    assert route_requirements(reqs, _shape(timeline=True), message="move clip 2 first") == (
        "editor_ops"
    )


def test_semantic_selection_still_requires_replan() -> None:
    reqs = [
        _upd("select", "global", description="use only the funniest clips"),
        _upd("style", "title", description="make the title smaller"),
    ]
    assert route_requirements(
        reqs, _shape(timeline=True), message="use only the funniest clips"
    ) == ("replan")


def test_v2_clip_timeline_can_route_global_timing_to_editor() -> None:
    shape = plan_shape_from_editor_snapshot(
        {"editor_ops_version": 2, "allowed_op_families": ["clip", "text"]}
    )
    assert route_requirements([_upd("timing", "global")], shape) == "editor_ops"


@pytest.mark.asyncio
async def test_compound_text_and_whole_source_request_cannot_commit_text_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copilot style subset must not bypass typed timing extraction.

    The user request contains an in-place title change and a whole-source duration
    constraint.  Main Creator understands both as typed requirements, while the
    copilot proposal contains only ``patch_text_style``.  Before the fix the
    planner takes the deferred fast path and commits the subset without running
    extraction, so the whole-source requirement cannot influence routing.
    """

    output = SimpleNamespace(
        action=AskUser(**_ASK),
        brief_updates=[
            _upd("style", "title", description="make the title smaller"),
            _upd("timing", "global", facts={"duration_s": 20}, description="keep the whole take"),
        ],
    )
    db, item, creator_id, _copilot, _runs = _wire_planner(
        monkeypatch,
        output=output,
        editor_plan=_ops_plan("patch_text_style"),
        snapshot={"text_bars": [], "allowed_op_families": ["text"]},
    )
    monkeypatch.setattr(planner.settings, "kria_copilot_first_enabled", True)
    monkeypatch.setattr(planner, "_FAST_PATH_MAX_CHARS", 500)

    result = await plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="Make the title smaller and keep the whole video.",
    )

    # The brief extractor must run before any editor-only plan is accepted so the
    # global timing requirement can route to a source-preserving plan/check.
    assert result.defer_brief is False
    assert result.brief_route == "replan"


@pytest.mark.asyncio
async def test_compound_request_does_not_report_editor_success_when_extraction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction failure must prevent a partial editor response for a compound ask."""

    output = SimpleNamespace(
        action=AskUser(**_ASK),
        brief_updates=[
            BriefUpdate(kind="style", scope="title", description="smaller"),
            BriefUpdate(kind="timing", scope="global", description="whole take"),
        ],
    )
    db, item, creator_id, _copilot, _runs = _wire_planner(
        monkeypatch,
        output=output,
        editor_plan=_ops_plan("patch_text_style"),
        snapshot={"text_bars": [], "allowed_op_families": ["text"]},
    )
    monkeypatch.setattr(planner.settings, "kria_copilot_first_enabled", True)

    async def extraction_failure(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("brief extractor unavailable")

    monkeypatch.setattr(planner, "_call_brief_extractor", extraction_failure)
    result = await plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="Make the title smaller and keep the whole video.",
    )

    assert result.plan.mode == "respond"
    assert result.brief_route is None
    _copilot.assert_not_awaited()


def test_caption_and_music_changes_use_their_actual_lanes():
    shape = plan_shape_from_editor_snapshot(
        {"editor_ops_version": 2, "allowed_op_families": ["caption", "music"]}
    )
    assert (
        route_requirements(
            [
                _upd("style", "global", description="word captions"),
                _upd("audio", "global", description="quieter music"),
            ],
            shape,
        )
        == "editor_ops"
    )
    assert route_requirements([_upd("text", "title", literal="Hello")], shape) == "replan"


def test_capture_order_composes_with_audio_without_keyword_routing():
    shape = CurrentPlanShape(
        has_render=True, can_edit_timeline=True, can_order_by_capture_time=True, can_edit_audio=True
    )
    assert (
        route_requirements(
            [
                _upd("order", "global", facts={"key": "capture_time"}),
                _upd("audio", "global", description="quieter music"),
            ],
            shape,
        )
        == "editor_ops"
    )


@pytest.mark.asyncio
async def test_editor_failure_preserves_extracted_request_and_names_failed_stage(monkeypatch):
    output = SimpleNamespace(
        action=AskUser(**_ASK), brief_updates=[_upd("style", "title", description="smaller")]
    )
    db, item, creator_id, copilot, _ = _wire_planner(
        monkeypatch,
        output=output,
        editor_plan=None,
        snapshot={"allowed_op_families": ["text"], "text_bars": []},
    )
    copilot.side_effect = RuntimeError("editor model unavailable")
    result = await plan_live_turn(
        db,
        thread_id=item.id,
        item_id=item.id,
        creator_id=creator_id,
        user_message="Make the title smaller",
    )
    assert not result.plan.intents
    assert result.brief_coverage["reason"] == "editor_planning_failed"
    assert len(result.brief_updates) == 1
    assert "read every" not in result.plan.response
