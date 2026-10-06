"""KRI-459: analysis recovery preserves the request before asking to continue."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    AskUser,
    CapabilityAvailability,
    ResolvedCreatorManifest,
)
from app.kria import planner
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief
from app.services import clip_intent_planning


@pytest.mark.asyncio
async def test_pending_analysis_extracts_brief_updates_without_compiling_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None)
    content_plan = SimpleNamespace(id=plan_id, user_id=creator_id, persona_id=persona_id)
    persona = SimpleNamespace(id=persona_id, user_id=creator_id)
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    prior = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="existing",
                kind="text",
                scope="title",
                literal="Keep this answer",
            )
        ],
    )
    updates = (BriefUpdate(kind="order", scope="global", description="start with the cooking"),)
    clip = SimpleNamespace(media_id="clip-pending", kind="video", analysis={})

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return {
            planner.PlanItem: item,
            planner.ContentPlan: content_plan,
            planner.Persona: persona,
        }[model]

    db = SimpleNamespace(get=AsyncMock(side_effect=get), execute=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(planner.settings, "kria_clip_understanding_enabled", True)
    monkeypatch.setattr(planner.settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(planner.settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(planner, "load_intent_clips_for_item", AsyncMock(return_value=[clip]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(
        clip_intent_planning, "wait_for_clip_understanding", AsyncMock(return_value=([clip], {}))
    )
    monkeypatch.setattr(planner, "_reload_intent_clips", AsyncMock(return_value=[clip]))
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=prior))
    monkeypatch.setattr(
        planner,
        "_load_creator_inputs",
        AsyncMock(
            return_value=planner._CreatorInputs(
                agent_input=SimpleNamespace(), intent_clips=[clip], creator_request="long request"
            )
        ),
    )
    creator_call = AsyncMock(
        return_value=SimpleNamespace(
            action=AskUser(
                kind="ask_user",
                question="Which cooking clips should lead?",
                reason_code="choice",
                options=["Cooking", "Other"],
            ),
            brief_updates=list(updates),
        )
    )
    monkeypatch.setattr(planner, "_call_main_creator", creator_call)
    compile_action = AsyncMock(
        side_effect=AssertionError("pending analysis must not compile an action")
    )
    monkeypatch.setattr(planner, "_plan_from_creator_output", compile_action)

    result = await planner._plan_live_turn(
        db,
        thread_id=thread_id,
        item_id=item_id,
        creator_id=creator_id,
        user_message="Continue with the cooking first.",
    )

    assert result.plan.mode == "respond"
    assert result.plan.turn_value == "recovery"
    assert "request and completed answers are saved" in result.plan.response
    assert tuple(result.brief_updates) == updates
    assert result.brief_expected_version == prior.version
    assert result.brief_coverage["stage"] == "analysis"
    assert result.brief_coverage["missing_media_ids"] == ["clip-pending"]
    compile_action.assert_not_awaited()
    creator_call.assert_awaited_once()
