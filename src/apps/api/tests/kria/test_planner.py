from __future__ import annotations

import time
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
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import (
    _full_creator_request,
    _phone_editor_reply,
    _plan_editor_revision,
    adapt_creator_action,
    adapt_editor_action,
    plan_live_turn,
)
from app.kria.registry import KRIA_TOOLS
from app.kria.strategy_policy import CheckedStrategy
from app.models import ContentPlan, CreationThread, CreatorAgentSession, Job, Persona, PlanItem
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentResolution


@pytest.fixture(autouse=True)
def _no_standing_clip_selections(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fake DBs have no `execute`; thread-event selection loading is covered elsewhere."""
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))


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
            context_label={"kind": "sport"},
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
    assert not {"sport_labels", "context_label"} & set(default_strategy)

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
    assert "sport_labels" not in owned_strategy
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


def test_phone_editor_reply_replaces_web_validate_and_stage_wording() -> None:
    assert "validate and stage" not in _phone_editor_reply(
        "I prepared this edit for the editor to validate and stage."
    )
    assert _phone_editor_reply("I prepared a tighter opening.") == "I prepared a tighter opening."


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
    item = SimpleNamespace(current_job_id=job_id, clip_assignments=[])
    thread = SimpleNamespace(active_creator_agent_session_id=session_id, creator_id=uuid.uuid4())
    session = SimpleNamespace(
        target_job_id=job_id,
        target_variant_id="original_text",
        plan_item_id=uuid.uuid4(),
        target_generation_id=None,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=uuid.uuid4(),
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
        lambda *_args, **_kwargs: {"allowed_op_families": ["trim_output_start"]},
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
        (False, "transcript", "action"),
        (True, "transcript", "action"),
        (False, "transcript_without_narration", "question"),
        (True, "transcript_without_narration", "question"),
    ],
)
async def test_live_creator_clip_intent_resolution_is_server_owned_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    flag_enabled: bool,
    outcome: str,
    expected_turn: str,
) -> None:
    transcript = ClipIntent(
        intent_id="score",
        op="label",
        attribute="the spoken score",
        label_source="transcript",
        transcript_kind="score",
    )
    is_transcript = outcome.startswith("transcript")
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
        narration={
            "gcs_path": "voiceover-uploads/voice.mp3",
            "generation": "1",
            "duration_s": 20,
        }
        if outcome == "transcript"
        else None,
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
    # This test pins the clip-intent flow over a bare manifest; the server
    # strategy check (KRI-142) has its own tests in test_strategy_policy.py.
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )

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
                        execution_contract="guided_voiceover_v1" if is_transcript else None,
                        # These are model-authored and must not survive when
                        # the shared service says no intent is needed.
                        clip_intents=[transcript]
                        if is_transcript
                        else [ClipIntent(intent_id="model", op="label", attribute="the beach")],
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
    if is_transcript:
        resolver = AsyncMock(return_value=PlannedIntentResolution([transcript], IntentResolution()))
    elif outcome == "provider_failure":
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
    if outcome == "transcript":
        strategy = result.plan.intents[0].arguments["strategy"]
        assert strategy["clip_intents"][0]["label_source"] == "transcript"
        assert strategy["clip_intents"][0]["transcript_kind"] == "score"
        assert "resolved_clip_intents" not in strategy
        persist_answers.assert_not_awaited()
    elif outcome == "transcript_without_narration":
        assert not result.plan.intents
        persist_answers.assert_not_awaited()
    if not flag_enabled:
        resolver.assert_not_awaited()
        load_clips.assert_not_awaited()
        persist_answers.assert_not_awaited()
        return
    assert db.rollback.await_count >= 1
    load_clips.assert_awaited_once_with(db, item, persona)
    resolver.assert_awaited_once()
    if is_transcript:
        return
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


@pytest.mark.asyncio
async def test_generic_montage_context_with_empty_inventory_stays_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-244: context-only chronology must not become a resolver question."""
    request = (
        "I took the sunset pictures walking to the bus to go to the pub and the night ones "
        "cycling to go back home in London. Come up with crative ideas"
    )
    creator_id = uuid.uuid4()
    item_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="Build a London evening story from the strongest moments.",
        ),
        summary="A London sunset-to-night montage.",
    )
    inventory = AsyncMock(return_value=PlannedIntentResolution([], IntentResolution()))
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", inventory)
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )

    result = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=thread_id,
        item_id=item_id,
        creator_id=creator_id,
        user_message=request,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=[], creator_request=request
        ),
        output=SimpleNamespace(action=action),
        brief_request=(
            "Creative brief (everything the creator has asked for, still in force):\n"
            "- [order/global] chronological order from sunset walk to night cycle\n"
            f"Latest message: {request}"
        ),
    )

    assert result.plan.turn_value == "action"
    assert [intent.tool_name for intent in result.plan.intents] == [
        "draft.apply_strategy",
        "render.request",
    ]
    inventory.assert_awaited_once()
    assert request in inventory.await_args.kwargs["creator_request"]
    assert inventory.await_args.kwargs["latest_user_message"] == request
    assert "[order/global]" in inventory.await_args.kwargs["generated_brief"]


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False  # never swallow what the cache writer raises


@pytest.mark.asyncio
async def test_phone_clip_vision_answers_do_not_fail_a_resolved_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-291: iPhone montage clips are raw `clip_assignments`, and vision answers
    are only cached for pool assets. The strict cache write raised on the phone
    clip's id, so a fully resolved turn answered "I couldn't reliably match that
    request to your clips" — and every retry re-asked vision and failed the same way.
    Runs the REAL cache writer: the stubbed one in the tests above hid this."""
    phone_clip = "analysis-proxy-ios-0EBED783-578F-4DFB-B562-623C6776994C.mp4"
    item_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="An evening of bowling in Istanbul.",
        ),
        summary="A bowling-night montage.",
    )
    request = 'Show "Mahmoud from Tunisia: reads books, bowls too" on the brown T-shirt bowler.'
    intent = ClipIntent(
        intent_id="mahmoud",
        op="caption",
        attribute="the guy in the brown T-shirt bowling",
        creator_text="Mahmoud from Tunisia: reads books, bowls too",
    )
    resolved = ResolvedClipIntent(
        **intent.model_dump(),
        assignments=[ClipAssignment(media_id=phone_clip, evidence="brown T-shirt", confidence=0.9)],
    )
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [intent],
            IntentResolution(
                intents=[resolved],
                vision_answers={
                    phone_clip: {
                        "is the person bowling wearing a brown t-shirt": {
                            "answer": "yes",
                            "confidence": 0.9,
                            "evidence": "brown T-shirt",
                        }
                    }
                },
            ),
        )
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(id=item_id)),
        begin_nested=lambda: _Savepoint(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )

    result = await planner._plan_from_creator_output(
        db,
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message=request,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=[], creator_request=request
        ),
        output=SimpleNamespace(action=action),
    )

    assert result.plan.turn_value == "action", result.plan.response
    strategy = result.plan.intents[0].arguments["strategy"]
    assert strategy["resolved_clip_intents"][0]["assignments"][0]["media_id"] == phone_clip
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    # KRI-433: the turn asks for the phone clip's answer to be cached on its
    # assignment (the row is a stub here, so that write only logs and moves on).
    assert any(
        call.args[0] is PlanItem and call.kwargs.get("with_for_update")
        for call in db.get.await_args_list
    )


async def _degraded_clip_intent_turn(
    monkeypatch: pytest.MonkeyPatch, resolver: AsyncMock
) -> planner.PlannedKriaTurn:
    item_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="Build a games-day story.",
        ),
        summary="A games-day montage.",
    )
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message="Group by sport.",
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=[], creator_request="Group by sport."
        ),
        output=SimpleNamespace(action=action),
    )


@pytest.mark.asyncio
async def test_kria_turn_gets_the_larger_foreground_vision_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-282: chat clips have no background vision lane, so the turn itself must."""
    resolver = AsyncMock(return_value=PlannedIntentResolution([], IntentResolution()))
    await _degraded_clip_intent_turn(monkeypatch, resolver)
    kwargs = resolver.await_args.kwargs
    assert kwargs["max_vision_requeries"] == planner.settings.kria_clip_intents_max_vision_requeries
    assert kwargs["vision_deadline_s"] == planner.settings.kria_clip_intents_vision_deadline_s
    assert kwargs["max_vision_requeries"] > 4


@pytest.mark.asyncio
async def test_kria_turn_rereads_clip_understanding_within_its_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prod b2a41da6: the gate must re-read analysis, not trust the turn-start snapshot."""
    monkeypatch.setattr(planner.settings, "kria_clip_understanding_wait_s", 45.0)
    reread = AsyncMock(return_value=[])
    monkeypatch.setattr(planner, "_reload_intent_clips", reread)
    resolver = AsyncMock(return_value=PlannedIntentResolution([], IntentResolution()))
    token = planner.turn_deadline.set(time.monotonic() + 150)
    try:
        await _degraded_clip_intent_turn(monkeypatch, resolver)
    finally:
        planner.turn_deadline.reset(token)
    kwargs = resolver.await_args.kwargs
    assert kwargs["require_clip_understanding"] is planner.settings.kria_clip_understanding_enabled
    assert kwargs["understanding_wait_until"] - time.monotonic() == pytest.approx(45.0, abs=0.5)
    await kwargs["refresh_clips"]()
    reread.assert_awaited_once()
    assert set(reread.await_args.kwargs) == {"item_id", "creator_id"}


@pytest.mark.asyncio
async def test_pending_vision_is_not_reported_as_a_match_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real 47-clip failure: the foreground vision cap left clips pending, and
    the generic 'couldn't reliably match' reply made a converging retry look broken."""
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [],
            IntentResolution(
                status="pending",
                error_code="vision_batch_deadline_or_foreground_cap",
                diagnostics={
                    "stage": "vision",
                    "clips": 47,
                    "intents": 8,
                    "shard_ms": [4100, 5200, 6100],
                    "vision_calls": 12,
                    "pending_clips": 9,
                    "nested": {"dropped": True},
                },
            ),
        )
    )
    result = await _degraded_clip_intent_turn(monkeypatch, resolver)

    assert result.plan.turn_value == "recovery"
    assert "couldn't reliably match" not in (result.plan.response or "")
    assert "still checking" in (result.plan.response or "")
    diag = result.plan.diagnostics
    assert diag["status"] == "pending"
    assert diag["reason"] == "vision_batch_deadline_or_foreground_cap"
    assert diag["pending_clips"] == 9 and diag["shard_ms"] == [4100, 5200, 6100]
    assert "nested" not in diag  # only scalars / short scalar lists are persisted
    # It survives the persisted-turn round trip (admin /turns shows `plan`).
    assert result.plan.model_dump()["diagnostics"]["pending_clips"] == 9


@pytest.mark.asyncio
async def test_provider_failure_persists_a_reason_code_not_a_bare_apology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = AsyncMock(side_effect=RuntimeError("secret creator sentence"))
    result = await _degraded_clip_intent_turn(monkeypatch, resolver)

    assert "couldn't reliably match" in (result.plan.response or "")
    assert result.plan.diagnostics["reason"] == "planner_exception"
    assert result.plan.diagnostics["error_type"] == "RuntimeError"
    assert "secret" not in str(result.plan.diagnostics)


@pytest.mark.asyncio
async def test_resolver_stage_failure_reason_reaches_the_turn_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [],
            IntentResolution(
                status="provider_unavailable",
                error_code="provider_outcome_unknown",
                diagnostics={"stage": "resolver", "shards": 8, "split_retries": 1},
            ),
        )
    )
    result = await _degraded_clip_intent_turn(monkeypatch, resolver)

    assert result.plan.diagnostics == {
        "status": "provider_unavailable",
        "reason": "provider_outcome_unknown",
        "stage": "resolver",
        "shards": 8,
        "split_retries": 1,
    }


def test_plans_without_diagnostics_dump_byte_identical() -> None:
    plan = KriaTurnPlan(mode="respond", turn_value="question", response="Which clip?")
    assert "diagnostics" not in plan.model_dump()


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


def test_strategy_adapter_keeps_only_deferred_transcript_intents():
    from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent

    transcript = ClipIntent(
        intent_id="score",
        op="label",
        attribute="spoken score",
        label_source="transcript",
        transcript_kind="score",
    )
    visual = ClipIntent(intent_id="sport", op="label", attribute="the sport shown")
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            clip_intents=[transcript, visual],
            resolved_clip_intents=[ResolvedClipIntent(**visual.model_dump())],
        ),
        summary="Show the requested labels.",
    )
    plan = adapt_creator_action(action)
    strategy = plan.intents[0].arguments["strategy"]
    assert len(strategy["clip_intents"]) == 1
    assert strategy["clip_intents"][0]["label_source"] == "transcript"
    assert "resolved_clip_intents" not in strategy


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unsupported", "no_effect"])
async def test_overlay_display_ask_defers_to_replan_instead_of_copilot_refusal(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """KRI-297: "Use all overlays as full screen" is not an editor op; the copilot's
    refusal must not be the answer -- `_plan_editor_revision` returns None so the
    planner re-plans it (strategy.overlay_display). Other refusals still stand."""
    target = SimpleNamespace(conversation=[], snapshot={}, job_id=uuid.uuid4())
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))
    copilot = AsyncMock(
        return_value=SimpleNamespace(
            ops=[], outcome=outcome, reply="That kind of edit isn't available."
        )
    )
    monkeypatch.setattr(planner, "run_copilot_turn", copilot)
    db = SimpleNamespace(rollback=AsyncMock())

    deferred = await _plan_editor_revision(
        db,
        thread_id=uuid.uuid4(),
        item=SimpleNamespace(),
        user_message="Use all overlays as full screen.",
    )
    assert deferred is None

    refused = await _plan_editor_revision(
        db, thread_id=uuid.uuid4(), item=SimpleNamespace(), user_message="make the text bigger"
    )
    assert refused is not None and refused.turn_value == "recovery"


def test_clip_understanding_wait_without_a_turn_deadline_is_the_setting(monkeypatch):
    monkeypatch.setattr(planner.settings, "kria_clip_understanding_wait_s", 45.0)
    until = planner._clip_understanding_wait_until()
    assert until - time.monotonic() == pytest.approx(45.0, abs=0.5)


@pytest.mark.parametrize(
    ("seconds_left", "expected"),
    [
        (300.0, 45.0),  # plenty of turn left: the setting caps the wait
        (100.0, 100.0 - 40.0 - 20.0),  # keeps the vision deadline + commit reserve
        (30.0, 30.0 - 40.0 - 20.0),  # already past: re-read once, never wait
    ],
)
def test_clip_understanding_wait_leaves_the_turn_time_to_finish(
    monkeypatch, seconds_left, expected
):
    monkeypatch.setattr(planner.settings, "kria_clip_understanding_wait_s", 45.0)
    monkeypatch.setattr(planner.settings, "kria_clip_intents_vision_deadline_s", 40.0)
    token = planner.turn_deadline.set(time.monotonic() + seconds_left)
    try:
        until = planner._clip_understanding_wait_until()
        assert until - time.monotonic() == pytest.approx(expected, abs=0.5)
    finally:
        planner.turn_deadline.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis", "kicked"),
    [
        (None, True),  # never analysed: a killed run must not leave it blank forever
        ({"understanding": {"summary": "a strike"}}, False),
        ({"understanding_attempts": 3}, False),  # settled empty: nothing left to try
    ],
)
async def test_turn_rekicks_clip_understanding_only_for_unanalysed_clips(
    monkeypatch: pytest.MonkeyPatch, analysis, kicked
) -> None:
    from app.services.clip_intent_resolution import IntentClip
    from app.tasks import kria_clip_understanding

    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(planner.settings, "kria_clip_understanding_enabled", True)
    monkeypatch.setattr(kria_clip_understanding, "enqueue_clip_understanding", enqueued.append)
    item_id = uuid.uuid4()

    await planner._kick_clip_understanding(
        item_id, [IntentClip(media_id="analysis-proxy-ios-A.mp4", kind="video", analysis=analysis)]
    )

    assert enqueued == ([item_id] if kicked else [])
