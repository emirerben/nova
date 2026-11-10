"""KRI-1352: the real planning turn restores a legacy over-length literal only to regenerate.

The Main Creator call, the clip loaders and the clip-analysis wait are stubbed (as in
`test_planner_analysis_recovery`); `_plan_live_turn`, its update pipeline and
`recover_overflowed_literals` are real. The turn stops at the clip-analysis wait, which returns
exactly the brief updates the pipeline produced.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    AskUser,
    CapabilityAvailability,
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.kria import planner
from app.kria.brief import BriefUpdate
from app.services import clip_intent_planning
from tests.kria.test_approved_long_sequence_copy import ENGLISH
from tests.kria.test_legacy_overflow_literal_recovery import LEGACY

REGENERATE = ProposeStrategy(
    kind="propose_strategy",
    strategy=CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="original_audio",
        render_program="guided",
        selected_media_ids=[],
        rationale="Try the approved words again.",
    ),
    summary="Generating it again with your approved words.",
)
QUESTION = AskUser(
    kind="ask_user",
    question="Which clip should open?",
    reason_code="choice",
    options=["First", "Last"],
)


async def _plan(monkeypatch: pytest.MonkeyPatch, action, model_updates=()):
    creator_id, item_id, thread_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    plan_id, persona_id = uuid.uuid4(), uuid.uuid4()
    rows = {
        planner.PlanItem: SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None),
        planner.ContentPlan: SimpleNamespace(id=plan_id, user_id=creator_id, persona_id=persona_id),
        planner.Persona: SimpleNamespace(id=persona_id, user_id=creator_id),
    }

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return rows[model]

    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    clip = SimpleNamespace(media_id="clip-pending", kind="video", analysis={})
    db = SimpleNamespace(get=AsyncMock(side_effect=get), execute=AsyncMock(), rollback=AsyncMock())
    for name in ("kria_clip_understanding_enabled", "kria_brief_binding_enabled"):
        monkeypatch.setattr(planner.settings, name, True)
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
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=LEGACY))
    monkeypatch.setattr(
        planner,
        "_load_creator_inputs",
        AsyncMock(
            return_value=planner._CreatorInputs(
                agent_input=SimpleNamespace(), intent_clips=[clip], creator_request="Try again"
            )
        ),
    )
    monkeypatch.setattr(
        planner,
        "_call_main_creator",
        AsyncMock(return_value=SimpleNamespace(action=action, brief_updates=list(model_updates))),
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    return await planner._plan_live_turn(
        db, thread_id=thread_id, item_id=item_id, creator_id=creator_id, user_message="Try again"
    )


@pytest.mark.asyncio
async def test_a_regeneration_turn_restores_the_legacy_literal(monkeypatch) -> None:
    result = await _plan(monkeypatch, REGENERATE)

    (change,) = result.brief_updates
    assert (change.operation, change.target_requirement_id) == ("change", "r2")
    assert change.literal == ENGLISH and change.expected_version == LEGACY.version
    assert result.brief_expected_version == LEGACY.version


@pytest.mark.asyncio
async def test_a_question_turn_leaves_the_brief_alone(monkeypatch) -> None:
    result = await _plan(monkeypatch, QUESTION)

    assert tuple(result.brief_updates) == ()


@pytest.mark.asyncio
async def test_the_creators_own_change_this_turn_wins(monkeypatch) -> None:
    own = BriefUpdate(
        operation="change",
        target_requirement_id="r2",
        expected_version=LEGACY.version,
        description="Drop the English",
    )

    result = await _plan(monkeypatch, REGENERATE, model_updates=[own])

    assert [update.target_requirement_id for update in result.brief_updates] == ["r2"]
    assert result.brief_updates[0].literal is None
    assert result.brief_updates[0].description == "Drop the English"


@pytest.mark.asyncio
async def test_the_creators_own_removal_this_turn_wins(monkeypatch) -> None:
    own = BriefUpdate(
        operation="remove", target_requirement_id="r2", expected_version=LEGACY.version
    )

    result = await _plan(monkeypatch, REGENERATE, model_updates=[own])

    assert [update.operation for update in result.brief_updates] == ["remove"]
