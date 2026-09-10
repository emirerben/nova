from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import TerminalError
from app.agents._schemas.creator_agent import (
    AskUser,
    CapabilityAvailability,
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.kria import planner
from app.kria.planner import (
    _plan_editor_revision,
    adapt_creator_action,
    adapt_editor_action,
    plan_live_turn,
)
from app.kria.registry import KRIA_TOOLS
from app.models import ContentPlan, CreationThread, CreatorAgentSession, Job, Persona, PlanItem


def test_question_action_stays_a_direct_value_adding_response() -> None:
    plan = adapt_creator_action(
        AskUser(
            kind="ask_user",
            question="Should the matcha reveal feel calm or energetic?",
            reason_code="pacing_choice",
            options=["Calm", "Energetic"],
        )
    )

    assert plan.mode == "respond"
    assert plan.turn_value == "question"
    assert plan.intents == []


def test_strategy_becomes_reversible_draft_then_separate_render_request() -> None:
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="day_vlog",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="Build from the morning setup to the finished matcha reveal.",
        ),
        summary="Open on the whisk, keep the business update concise, and end on the product.",
    )

    plan = adapt_creator_action(action)

    assert plan.mode == "act"
    assert [intent.tool_name for intent in plan.intents] == [
        "draft.apply_strategy",
        "render.request",
    ]
    assert plan.intents[1].depends_on == ["apply-strategy"]
    assert plan.intents[1].arguments == {}
    assert KRIA_TOOLS.get("draft.apply_strategy", 1).definition.risk == "reversible_draft"
    assert KRIA_TOOLS.get("render.request", 1).definition.risk == "approval_required"


def test_model_cannot_author_render_target_pins() -> None:
    arguments = KRIA_TOOLS.get("render.request", 1).arguments_model

    try:
        arguments.model_validate({"draft_id": "untrusted", "draft_revision": 99})
    except ValueError:
        pass
    else:
        raise AssertionError("render.request accepted model-authored target pins")


def test_editor_revision_stays_a_portable_draft_without_render_request() -> None:
    plan = adapt_editor_action(
        reply="I prepared a tighter opening and quieter music.",
        ops=[
            {"op": "trim_output_start", "start_s": 0.8},
            {"op": "set_mix", "music_level": 0.3},
        ],
    )

    assert plan.mode == "act"
    assert [intent.tool_name for intent in plan.intents] == ["draft.apply_editor_ops"]
    assert plan.intents[0].arguments["operations"][0]["op"] == "trim_output_start"
    assert KRIA_TOOLS.get("draft.apply_editor_ops", 1).definition.risk == "reversible_draft"


@pytest.mark.asyncio
async def test_editor_revision_copies_orm_values_before_releasing_read_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = False

    class ExpiringRow:
        @property
        def role(self) -> str:
            if expired:
                raise AssertionError("ORM role accessed after rollback")
            return "user"

        @property
        def content(self) -> str:
            if expired:
                raise AssertionError("ORM content accessed after rollback")
            return "Tighten the opening"

    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item = SimpleNamespace(current_job_id=job_id)
    thread = SimpleNamespace(active_creator_agent_session_id=session_id)
    session = SimpleNamespace(
        target_job_id=job_id,
        target_variant_id="original_text",
        plan_item_id=uuid.uuid4(),
        target_generation_id=None,
    )
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {"variant_id": "original_text", "render_status": "ready"},
            ]
        },
    )

    async def get(model, identifier):  # noqa: ANN001, ANN202
        return {
            CreationThread: thread,
            CreatorAgentSession: session,
            Job: job,
        }[model]

    async def rollback() -> None:
        nonlocal expired
        expired = True

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: None,
                scalars=lambda: SimpleNamespace(all=lambda: [ExpiringRow()]),
            )
        ),
        rollback=AsyncMock(side_effect=rollback),
    )
    copilot = AsyncMock(
        return_value=SimpleNamespace(
            ops=[],
            outcome="clarification",
            reply="Which moment should open?",
        )
    )
    monkeypatch.setattr(
        planner,
        "build_editor_snapshot",
        lambda *_args: {"allowed_op_families": ["trim_output_start"]},
    )
    monkeypatch.setattr(planner, "run_copilot_turn", copilot)

    result = await _plan_editor_revision(
        db,
        thread_id=uuid.uuid4(),
        item=item,
        user_message="Make it faster",
    )

    assert result is not None
    assert result.turn_value == "question"
    assert copilot.await_args.args[0].turns == [{"role": "user", "content": "Tighten the opening"}]
    assert copilot.await_args.kwargs["job_id"] == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_live_creator_plan_releases_transaction_and_offloads_sync_agent(
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        edit_format="montage",
    )
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

    async def get(model, _identifier):  # noqa: ANN001, ANN202
        return {PlanItem: item, ContentPlan: content_plan, Persona: persona}[model]

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        rollback=AsyncMock(),
    )
    monkeypatch.setattr(
        planner,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(planner, "creator_context", lambda *_args: ("creator", "item"))

    class FakeAgent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            if terminal:
                raise TerminalError("model exhausted")
            return SimpleNamespace(
                action=AskUser(
                    kind="ask_user",
                    question="Should the opening feel calm or energetic?",
                    reason_code="pacing_choice",
                    options=["Calm", "Energetic"],
                )
            )

    monkeypatch.setattr(planner, "MainCreatorAgent", FakeAgent)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    original_to_thread = planner.asyncio.to_thread
    offloaded = False

    async def checked_to_thread(function, *args, **kwargs):  # noqa: ANN001, ANN202
        nonlocal offloaded
        offloaded = True
        assert db.rollback.await_count == 1
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(planner.asyncio, "to_thread", checked_to_thread)

    if terminal:
        with pytest.raises(RuntimeError, match="reliable editorial plan"):
            await plan_live_turn(
                db,
                thread_id=uuid.uuid4(),
                item_id=item_id,
                creator_id=creator_id,
                user_message="Shape this edit",
            )
    else:
        result = await plan_live_turn(
            db,
            thread_id=uuid.uuid4(),
            item_id=item_id,
            creator_id=creator_id,
            user_message="Shape this edit",
        )
        assert result.plan.turn_value == "question"
    assert offloaded is True


def test_explicit_server_editor_action_retains_exact_render_approval() -> None:
    plan = adapt_editor_action(
        reply="Apply the reviewed speech cut.",
        ops=[{"op": "apply_speech_cut_candidate", "candidate_id": "cut-1"}],
        request_render=True,
    )
    assert [intent.tool_name for intent in plan.intents] == [
        "draft.apply_editor_ops",
        "render.request",
    ]
    assert plan.intents[1].depends_on == ["apply-editor-ops"]
