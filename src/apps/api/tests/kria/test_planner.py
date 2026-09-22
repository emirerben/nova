from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import TerminalError
from app.agents._schemas.creator_agent import (
    AskUser,
    CapabilityAvailability,
    ContextLabelIntent,
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.kria import planner
from app.kria.planner import (
    _full_creator_request,
    _plan_editor_revision,
    adapt_creator_action,
    adapt_editor_action,
    plan_live_turn,
)
from app.kria.registry import KRIA_TOOLS
from app.models import ContentPlan, CreationThread, CreatorAgentSession, Job, Persona, PlanItem
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentResolution


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


def test_model_clip_intent_fields_are_stripped_unless_server_owned_values_are_supplied() -> None:
    untrusted = ClipIntent(intent_id="model", op="label", attribute="the sport")
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="day_vlog",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="A grounded edit.",
            sport_labels=True,
            context_label=ContextLabelIntent(),
            clip_intents=[untrusted],
            resolved_clip_intents=[
                ResolvedClipIntent(
                    intent_id="model",
                    op="label",
                    attribute="the sport",
                    assignments=[
                        ClipAssignment(
                            media_id="untrusted-media",
                            value="Untrusted",
                            confidence=1,
                            evidence="model claim",
                            grounding="creator_text",
                        )
                    ],
                )
            ],
        ),
        summary="Draft it.",
    )

    default_strategy = adapt_creator_action(action).intents[0].arguments["strategy"]
    assert "clip_intents" not in default_strategy
    assert "resolved_clip_intents" not in default_strategy
    assert default_strategy["sport_labels"] is True
    assert default_strategy["context_label"]["kind"] == "sport"

    server_intent = ClipIntent(intent_id="server", op="label", attribute="the sport")
    server_resolved = ResolvedClipIntent(
        intent_id="server",
        op="label",
        attribute="the sport",
        assignments=[
            ClipAssignment(
                media_id="asset-verified",
                value="Volleyball",
                confidence=0.9,
                evidence="A volleyball court is visible.",
                grounding="vision_verified",
            )
        ],
    )
    owned_strategy = (
        adapt_creator_action(
            action,
            server_clip_intents=[server_intent],
            server_resolved_clip_intents=[server_resolved],
        )
        .intents[0]
        .arguments["strategy"]
    )
    assert owned_strategy["clip_intents"][0]["intent_id"] == "server"
    assert owned_strategy["sport_labels"] is False
    assert "context_label" not in owned_strategy
    resolved_media_id = owned_strategy["resolved_clip_intents"][0]["assignments"][0]["media_id"]
    assert resolved_media_id == "asset-verified"


def test_full_creator_request_preserves_all_user_messages_and_current_message_last() -> None:
    rows = [
        SimpleNamespace(role="user", content="Use the park clips."),
        SimpleNamespace(role="assistant", content="Sure."),
        SimpleNamespace(role="user", content="End with the celebration."),
    ]

    assert _full_creator_request(rows, current_message="End with the celebration.") == (
        "Use the park clips.\nEnd with the celebration."
    )
    assert (
        _full_creator_request(
            [SimpleNamespace(role="user", content="x" * 12_001)], current_message=""
        )
        is None
    )


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

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return {PlanItem: item, ContentPlan: content_plan, Persona: persona}[model]

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        rollback=AsyncMock(),
        commit=AsyncMock(),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag_enabled", "outcome", "expected_turn"),
    [
        (False, "resolved", "action"),
        (True, "resolved", "action"),
        (True, "needs_creator", "question"),
        (True, "legacy_question", "question"),
        (True, "pending", "recovery"),
        (True, "provider_failure", "recovery"),
        (True, "cache_failure", "recovery"),
    ],
)
async def test_live_creator_clip_intent_resolution_is_server_owned_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    flag_enabled: bool,
    outcome: str,
    expected_turn: str,
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
    refreshed_item = SimpleNamespace(id=item_id)
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

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        if model is PlanItem and _kwargs.get("populate_existing"):
            return refreshed_item
        return {PlanItem: item, ContentPlan: content_plan, Persona: persona}[model]

    history = [
        SimpleNamespace(role="user", content="Use the beach clips."),
        SimpleNamespace(role="assistant", content="I can do that."),
        SimpleNamespace(role="user", content="End with the sunset."),
    ]
    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: history[:1])),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: history)),
            ]
            if flag_enabled
            else [SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: history))]
        ),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", flag_enabled)
    monkeypatch.setattr(
        planner,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(planner, "creator_context", lambda *_args: ("creator", "item"))
    clips = [SimpleNamespace(media_id="asset-verified")]
    load_clips = AsyncMock(return_value=clips)
    monkeypatch.setattr(planner, "load_intent_clips_for_item", load_clips)
    persist_answers = AsyncMock(
        side_effect=RuntimeError("cache write failed") if outcome == "cache_failure" else None
    )
    monkeypatch.setattr(planner, "persist_clip_intent_vision_answers", persist_answers)

    class FakeAgent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            return SimpleNamespace(
                action=ProposeStrategy(
                    kind="propose_strategy",
                    strategy=CreativeStrategy(
                        direction="guided_story",
                        edit_format="day_vlog",
                        audio_strategy="licensed_music",
                        pacing="fast",
                        render_program="guided",
                        selected_media_ids=[],
                        rationale="A beach story.",
                        # These are model-authored and must not survive when
                        # the shared service says no intent is needed.
                        clip_intents=[
                            ClipIntent(intent_id="model", op="label", attribute="the beach")
                        ],
                        resolved_clip_intents=[
                            ResolvedClipIntent(
                                intent_id="model",
                                op="label",
                                attribute="the beach",
                            )
                        ],
                    ),
                    summary="Build the beach story.",
                )
            )

    monkeypatch.setattr(planner, "MainCreatorAgent", FakeAgent)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    if outcome == "provider_failure":
        resolver = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    elif outcome == "needs_creator":
        resolver = AsyncMock(
            return_value=PlannedIntentResolution(
                [],
                IntentResolution(status="needs_creator", question="Which beach clip do you mean?"),
            )
        )
    elif outcome == "legacy_question":
        resolver = AsyncMock(
            return_value=PlannedIntentResolution(
                [], IntentResolution(question="Which beach clip do you mean?")
            )
        )
    elif outcome == "pending":
        resolver = AsyncMock(
            return_value=PlannedIntentResolution(
                [],
                IntentResolution(
                    status="pending",
                    vision_answers={
                        "asset-verified": {
                            "what is shown": {"answer": "a beach", "generation": "7"}
                        }
                    },
                ),
            )
        )
    else:
        resolver = AsyncMock(
            return_value=PlannedIntentResolution(
                [],
                IntentResolution(
                    vision_answers={
                        "asset-verified": {
                            "what is shown": {"answer": "a beach", "generation": "7"}
                        }
                    }
                ),
            )
        )
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)

    result = await plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=creator_id,
        user_message="End with the sunset.",
    )

    assert result.plan.turn_value == expected_turn
    if not flag_enabled:
        resolver.assert_not_awaited()
        load_clips.assert_not_awaited()
        persist_answers.assert_not_awaited()
        return
    assert db.rollback.await_count >= 1
    load_clips.assert_awaited_once_with(db, item, persona)
    resolver.assert_awaited_once()
    if outcome == "resolved":
        strategy = result.plan.intents[0].arguments["strategy"]
        assert "clip_intents" not in strategy
        assert "resolved_clip_intents" not in strategy
        assert resolver.await_args.kwargs["creator_request"] == (
            "Use the beach clips.\nEnd with the sunset."
        )
        assert resolver.await_args.kwargs["candidate_intents"][0].intent_id == "model"
        persist_answers.assert_awaited_once()
        assert persist_answers.await_args.args[1] is refreshed_item
        assert persist_answers.await_args.kwargs["creator_id"] == creator_id
        assert persist_answers.await_args.kwargs["strict"] is True
        assert db.get.await_args_list[-1].kwargs["populate_existing"] is True
        assert db.commit.await_count == 1
    elif outcome == "pending":
        assert result.plan.intents == []
        persist_answers.assert_awaited_once()
        assert persist_answers.await_args.args[1] is refreshed_item
        assert db.get.await_args_list[-1].kwargs["populate_existing"] is True
        assert db.commit.await_count == 1
    elif outcome == "cache_failure":
        assert result.plan.intents == []
        persist_answers.assert_awaited_once()
        assert db.commit.await_count == 0
        assert db.rollback.await_count == 2
    else:
        assert result.plan.intents == []
        persist_answers.assert_not_awaited()


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
