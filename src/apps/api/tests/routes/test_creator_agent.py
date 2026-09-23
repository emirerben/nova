"""Focused Main Creator route/controller contracts."""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import MissingGreenlet
from starlette.requests import Request

from app.agents._runtime import ProviderQuotaExceededError, TerminalError
from app.agents._schemas.creator_agent import (
    CREATOR_REQUEST_MAX_CHARS,
    AskUser,
    CreativeStrategy,
    CreatorCraftBundle,
    CreatorEditSnapshot,
    CreatorMediaRef,
    ProposeStrategy,
    canonical_context_hash,
    canonical_manifest_hash,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.auth import get_current_user
from app.config import Settings, settings
from app.database import get_db
from app.limiter import limiter
from app.main import app
from app.models import CreatorAgentExecution, CreatorAgentSession, Job
from app.routes import creator_agent as creator_routes
from app.routes import plan_items as plan_item_routes
from app.routes.creator_agent import (
    CARRIED_BRIEF_EVENT,
    REFRESH_DIRECTION_MESSAGE,
    AutoIterationBody,
    ConfirmBody,
    StartBody,
    TurnBody,
    _apply_explicit_render_intent,
    _apply_plan_intent,
    _auto_iteration_already_finalized,
    _balanced_duration_s,
    _carried_brief_seed,
    _confirmed_creator_request,
    _creator_speech_cut_source_enabled,
    _explicit_media_scope,
    _fallback_strategy,
    _has_explicit_media_scope,
    _is_refresh_retry_message,
    _next_balanced_duration_s,
    _original_creator_request_for_refresh,
    _pin_strategy_fields_on_refresh,
    _pinned_narration_target_duration_s,
    _previous_accepted_strategy,
    _previous_creator_clip_order,
    _requests_preserved_clip_order,
    _require_feature,
    _reset_render_target,
    _resolved_cadence_for_turn,
    _seed_guided_specialist_brief,
    _selected_cadence_sources,
    _strict_creator_format,
)
from app.schemas.edit_proposal import (
    EditProposal,
    MixedMediaTimingProfile,
    MontageAudioPlan,
    MontageCadenceConstraint,
    ProposalBrief,
    recognize_cadence_reuse_policy,
    recognize_round_robin_cadence,
    recognize_total_duration_s,
    rejects_round_robin_cadence,
)
from app.services.creator_capabilities import (
    compile_strategy_to_plan,
    resolve_creator_manifest,
)


@pytest.fixture(autouse=True)
def _stub_creator_clip_metadata_dispatch(monkeypatch) -> None:
    from app.tasks.creator_clip_metadata import analyze_creator_clip_metadata

    monkeypatch.setattr(analyze_creator_clip_metadata, "apply_async", MagicMock())


def test_creator_request_contract_promotes_explicit_scope_and_required_intent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "creator_prompt_fidelity_enabled", True, raising=False)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 44.7,
        },
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "photo-1", "kind": "image"},
        ],
        guided_capability_enabled=True,
    )
    request = (
        "Use all image and video provided. Transition photos in 0.3 seconds and group them. "
        "Add a placeholder name to images and videos if focused on a single player. "
        "Highlight the sports and the score based on the audio."
    )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(
            audio_strategy="voiceover",
            render_program="native",
            clip_intents=[
                {
                    "intent_id": kind,
                    "op": "label",
                    "attribute": kind,
                    "label_source": "transcript",
                    "transcript_kind": kind,
                }
                for kind in ("participant", "score", "topic")
            ],
        ),
        request,
        manifest=manifest,
    )

    assert _has_explicit_media_scope(request)
    assert _explicit_media_scope(request) == "all"
    assert strategy.media_scope == "all"
    assert strategy.execution_contract == "guided_voiceover_v1"
    assert {intent.transcript_kind for intent in strategy.clip_intents} == {
        "participant",
        "score",
        "topic",
    }
    assert strategy.mixed_media_timing is not None
    assert strategy.render_program == "guided"


def test_all_media_capacity_preserves_content_aware_target_for_exact_all_choice() -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    original = CreativeStrategy(
        direction="guided_story",
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in manifest.media],
        target_duration_s=40,
        audio_strategy="licensed_music",
        opening_title="Keep this title",
        font_family="Inter",
        text_color="#FFD24A",
        montage_audio=MontageAudioPlan(source_media_ids=["clip-0"]),
    )

    question = creator_routes._all_media_capacity_question(manifest, original)

    assert question is not None
    assert question["reason_code"] == "all_media_capacity"
    all_option = question["options"][1]
    selected = creator_routes._all_media_capacity_choice(
        question["all_media_capacity"], all_option, manifest
    )
    assert selected is not None
    assert selected.direction == "fast_montage"
    assert selected.media_scope == "all"
    assert selected.target_duration_s == 40
    assert selected.audio_strategy == "licensed_music"
    assert selected.opening_title == "Keep this title"
    assert selected.font_family == "Inter"
    assert selected.montage_audio == MontageAudioPlan(source_media_ids=["clip-0"])
    assert creator_routes._all_media_capacity_question(manifest, selected) is None


def test_all_media_capacity_subset_choice_overrides_earlier_all_scope() -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    question = creator_routes._all_media_capacity_question(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            target_duration_s=24,
            audio_strategy="licensed_music",
        ),
    )

    assert question is not None
    selected = creator_routes._all_media_capacity_choice(
        question["all_media_capacity"], question["options"][0], manifest
    )
    assert selected is not None
    # The persisted exact mapping, applied after request-intent recognition,
    # is authoritative even though the original creator text said "use all".
    assert selected.media_scope == "selected"
    assert len(selected.selected_media_ids) == 17


def test_all_media_capacity_never_replaces_agent_duration_with_arithmetic_floor() -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    question = creator_routes._all_media_capacity_question(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            target_duration_s=31,
            audio_strategy="licensed_music",
            rationale="The action sequences need longer holds before the payoff.",
        ),
        requested_duration_is_explicit=False,
    )

    assert question is not None
    assert "I proposed 31 seconds" in question["message"]
    assert "27" not in " ".join(question["options"])
    assert {
        mapping["strategy"]["target_duration_s"]
        for mapping in question["all_media_capacity"]["option_mappings"]
    } == {31}


def test_historical_capacity_kind_uses_newer_explicit_all_instruction() -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    question = creator_routes._all_media_capacity_question(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            target_duration_s=24,
            audio_strategy="licensed_music",
        ),
    )
    assert question is not None
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload=question,
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": question["options"][0]},
        ),
    ]

    assert (
        creator_routes._historical_all_media_capacity_choice_kind(events, manifest)
        == "strongest_subset"
    )
    assert creator_routes._explicit_media_scope("use all clips", manifest) == "all"
    assert creator_routes._capacity_history_kind_for_turn(events, "use all clips", manifest) is None
    assert creator_routes._capacity_history_kind_for_turn([], "use all clips", manifest) is None


@pytest.mark.asyncio
async def test_route_uses_content_aware_duration_and_rationale_when_user_omits_it(
    monkeypatch,
) -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    append_event = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(
            return_value=SimpleNamespace(
                action=ProposeStrategy(
                    kind="propose_strategy",
                    strategy=CreativeStrategy(
                        direction="guided_story",
                        media_scope="all",
                        target_duration_s=56,
                        rationale=(
                            "The action sequences need longer holds while the reaction clips "
                            "can stay quick, so 56 seconds preserves their meaning."
                        ),
                    ),
                    summary="A holiday montage.",
                )
            )
        ),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Make a montage from my holiday clips",
    )

    assert result is response
    assert session.status == "awaiting_confirmation"
    assert session.active_plan["target_duration_s"] == 56
    assert "action sequences need longer holds" in session.active_plan["summary"]
    assert append_event.await_args.kwargs["event_type"] == "assistant_strategy"


@pytest.mark.asyncio
async def test_over_budget_question_falls_back_with_a_visible_notice(monkeypatch) -> None:
    # KRI-118 item 3: the model asked a genuine question, but the session's
    # question budget was already spent -- the resulting fallback strategy's
    # `assistant_strategy` event must say so, not silently swap in a plan.
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video", "duration_s": 8.0}],
        guided_capability_enabled=True,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=2,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    append_event = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(
            return_value=SimpleNamespace(
                action=AskUser(
                    kind="ask_user",
                    question="Should the video include your dog clips?",
                    reason_code="scope",
                )
            )
        ),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Make a montage from my holiday clips",
    )

    assert result is response
    assert session.status == "awaiting_confirmation"
    strategy_event = append_event.await_args.kwargs
    assert strategy_event["event_type"] == "assistant_strategy"
    notices = strategy_event["payload"]["notices"]
    assert len(notices) == 1
    assert notices[0].startswith("I had more questions but went ahead with: ")
    assert "Should the video include your dog clips?" in notices[0]


def _refresh_retry_fixture() -> tuple[dict[str, Any], str, list]:
    """A previously accepted guided_story plan plus its original request text.

    Mirrors prod thread A9604B72-47C2-48C6-B0D3-2AE5D67F27E8 / plan item
    b2242487-5e22-4b5c-9489-4e65a0896b7e: a three-part story direction that a
    failed planner attempt must not lose on Retry.
    """

    original_request = (
        "Dynamic opening to establish the scene; fast-paced rhythmic sequence using the "
        "shorter clips; strong closing hold on one of the longer moments"
    )
    previous_strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        render_program="guided",
        pacing="relaxed",
        target_duration_s=45,
        opening_title="Golden hour kickoff",
        closing_title="See you next weekend",
        shot_labels=["Warm-up", "Match point"],
        audio_strategy="licensed_music",
        story_structure=[
            "Dynamic opening to establish the scene",
            "Fast-paced rhythmic sequence using the shorter clips",
            "Strong closing hold on one of the longer moments",
        ],
        rationale="A three-part story arc.",
    )
    previous_active_plan = {
        "edit_plan": {"strategy": previous_strategy.model_dump(mode="json", exclude_none=True)},
        "creator_request": original_request,
        "opening_title": "Golden hour kickoff",
    }
    events: list = []
    return previous_active_plan, original_request, events


@pytest.mark.asyncio
async def test_refresh_retry_keeps_prior_accepted_direction_and_logs_pin_event(monkeypatch) -> None:
    """Retry after a failed guided_story plan must not adopt the model's
    fast_montage re-proposal: direction/pace/duration/titles are restored
    from the last accepted strategy, and creator_request/goal reach the
    specialist as the creator's ORIGINAL text, never the canned retry
    message. See CLAUDE.md-referenced prod thread A9604B72-... / defect 1.
    """

    previous_active_plan, original_request, events = _refresh_retry_fixture()
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(6)
        ],
        guided_capability_enabled=True,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=2,
        status="planning",
        events=events,
        agent_call_count=0,
        agent_call_budget=8,
        question_count=0,
        question_budget=2,
        active_plan=None,  # already cleared by _reset_render_target before retry
        last_error={"code": "execution_failed"},
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    log_mock = MagicMock()
    captured: dict[str, Any] = {}

    async def fake_to_thread(_func, agent_input, ctx=None):  # noqa: ARG001
        captured["agent_input"] = agent_input
        return SimpleNamespace(
            action=ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="fast_montage",
                    edit_format="montage",
                    render_program="native",
                    pacing="fast",
                    target_duration_s=15,
                    audio_strategy="licensed_music",
                    selected_media_ids=[f"clip-{index}" for index in range(6)],
                ),
                summary="A fast highlight reel.",
            )
        )

    monkeypatch.setattr(creator_routes, "log", log_mock)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=2,
        user_message=REFRESH_DIRECTION_MESSAGE,
        previous_active_plan=previous_active_plan,
    )

    assert result is response
    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["direction"] == "guided_story"
    assert strategy["render_program"] == "guided"
    assert strategy["pacing"] == "relaxed"
    assert strategy["target_duration_s"] == 45
    assert strategy["opening_title"] == "Golden hour kickoff"
    assert strategy["closing_title"] == "See you next weekend"
    assert strategy["shot_labels"] == ["Warm-up", "Match point"]

    # creator_request/goal consistency: the model AND the persisted plan (the
    # source _seed_guided_specialist_brief reads at confirmation) both see the
    # creator's original request, never the canned "Keep the same plan..."
    # confirmation-screen text.
    assert session.active_plan["creator_request"] == original_request
    assert captured["agent_input"].creator_request == original_request
    assert REFRESH_DIRECTION_MESSAGE not in session.active_plan["creator_request"]

    pinned_calls = [
        call
        for call in log_mock.info.call_args_list
        if call.args and call.args[0] == "creator_strategy_pinned_on_refresh"
    ]
    assert len(pinned_calls) == 1
    restored = pinned_calls[0].kwargs["restored_fields"]
    assert {
        "direction",
        "render_program",
        "pacing",
        "target_duration_s",
        "opening_title",
        "closing_title",
    }.issubset(set(restored))


@pytest.mark.asyncio
async def test_normal_new_message_after_failed_plan_is_not_pinned(monkeypatch) -> None:
    """A genuine new instruction (not the canned refresh message) must be
    free to change direction -- only the refresh/retry affordance pins the
    prior accepted strategy.
    """

    previous_active_plan, _original_request, events = _refresh_retry_fixture()
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(6)
        ],
        guided_capability_enabled=True,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=2,
        status="planning",
        events=events,
        agent_call_count=0,
        agent_call_budget=8,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")

    async def fake_to_thread(_func, _agent_input, ctx=None):  # noqa: ARG001
        return SimpleNamespace(
            action=ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="fast_montage",
                    edit_format="montage",
                    render_program="native",
                    pacing="fast",
                    target_duration_s=15,
                    audio_strategy="licensed_music",
                    selected_media_ids=[f"clip-{index}" for index in range(6)],
                ),
                summary="A fast highlight reel.",
            )
        )

    monkeypatch.setattr(creator_routes, "log", MagicMock())
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=2,
        user_message="Make it a fast montage",
        previous_active_plan=previous_active_plan,
    )

    assert result is response
    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["direction"] == "fast_montage"
    assert strategy["render_program"] == "native"
    assert strategy["pacing"] == "fast"
    assert strategy["target_duration_s"] == 15


def _user_event(sequence: int, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        role="user",
        sequence=sequence,
        event_type="user_message",
        payload={"message": message},
        client_event_id=f"event-{sequence}",
    )


def _carried_brief_event(sequence: int, brief: str) -> SimpleNamespace:
    return SimpleNamespace(
        role="system",
        sequence=sequence,
        event_type=CARRIED_BRIEF_EVENT,
        payload={"creator_request": brief},
        client_event_id=None,
    )


def _assistant_event(sequence: int, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        role="assistant",
        sequence=sequence,
        event_type="assistant_question",
        payload={"message": message},
        client_event_id=None,
    )


async def _run_carried_turn(
    monkeypatch,
    *,
    events: list,
    message: str,
    previous_active_plan: dict[str, Any],
    clip_intents: bool = False,
) -> tuple[dict[str, Any], SimpleNamespace]:
    strategy_plan, _original_request, _events = _refresh_retry_fixture()
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(6)
        ],
        guided_capability_enabled=True,
    )
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=2,
        status="planning",
        events=events,
        agent_call_count=0,
        agent_call_budget=8,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    captured: dict[str, Any] = {}

    async def fake_to_thread(_func, agent_input, ctx=None):  # noqa: ARG001
        captured["agent_input"] = agent_input
        return SimpleNamespace(
            action=ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy.model_validate(strategy_plan["edit_plan"]["strategy"]),
                summary="The same three-part story.",
            )
        )

    monkeypatch.setattr(creator_routes, "log", MagicMock())
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(
        creator_routes, "_response", AsyncMock(return_value=SimpleNamespace(status="ok"))
    )
    if clip_intents:
        from app.services.clip_intent_planning import PlannedIntentResolution  # noqa: PLC0415
        from app.services.clip_intent_resolution import IntentResolution  # noqa: PLC0415

        async def fake_plan_intents(**kwargs):
            captured["intent_creator_request"] = kwargs["creator_request"]
            return PlannedIntentResolution([], IntentResolution())

        monkeypatch.setattr(settings, "clip_intents_enabled", True)
        monkeypatch.setattr(
            creator_routes, "load_intent_clips_for_item", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(creator_routes, "plan_and_resolve_clip_intents", fake_plan_intents)

    await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=SimpleNamespace(id=uuid.uuid4()),
        session_id=session.id,
        expected_revision=2,
        user_message=message,
        previous_active_plan=previous_active_plan,
    )
    return captured, session


def _words(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "fresh_session_message",
        "fresh_session_refresh",
        "same_session_message",
        "fresh_session_second_turn",
        "fresh_session_retry_preparing",
        "fresh_session_refresh_then_revision",
    ],
)
async def test_fresh_session_after_a_failure_keeps_the_creators_brief(monkeypatch, case) -> None:
    """A message after a failed session starts a new one; the brief must survive
    it, on that first turn and on every later turn of the fresh session."""

    failed_plan, original_request, _events = _refresh_retry_fixture()
    brief = _carried_brief_event(0, original_request)
    # The fresh session's own plan after its first turn: no carry marker.
    own_plan = {**failed_plan, "creator_request": f"{original_request}\nSlower, please"}
    previous_active_plan = failed_plan
    if case == "fresh_session_message":
        message = "Slower, please"
        events = [brief, _user_event(1, message)]
        expected_request = [original_request, message]
        expected_conversation = [original_request, message]
    elif case == "fresh_session_refresh":
        message = REFRESH_DIRECTION_MESSAGE
        events = [brief, _user_event(1, message)]
        expected_request = [original_request]
        expected_conversation = [original_request, message]
    elif case == "same_session_message":
        # The session's own plan: its earlier messages are already in events.
        message = "Slower, please"
        events = [_user_event(0, original_request), _user_event(1, message)]
        expected_request = [original_request, message]
        expected_conversation = [original_request, message]
    elif case == "fresh_session_second_turn":
        message = "Make the title red"
        previous_active_plan = own_plan
        events = [
            brief,
            _user_event(1, "Slower, please"),
            _assistant_event(2, "Here is the plan"),
            _user_event(3, message),
        ]
        expected_request = [original_request, "Slower, please", message]
        expected_conversation = [original_request, "Slower, please", "Here is the plan", message]
    elif case == "fresh_session_retry_preparing":
        # Preparation failed on the fresh session's first turn; the retry
        # resumes that turn's saved message.
        message = "Slower, please"
        events = [
            brief,
            _user_event(1, message),
            _assistant_event(2, "Clip analysis is unavailable"),
            _user_event(3, "Retry preparing my clips"),
        ]
        expected_request = [original_request, message, "Retry preparing my clips", message]
        expected_conversation = [
            original_request,
            message,
            "Clip analysis is unavailable",
            "Retry preparing my clips",
        ]
    else:
        message = "Make the title red"
        previous_active_plan = {**failed_plan, "creator_request": original_request}
        events = [
            brief,
            _user_event(1, REFRESH_DIRECTION_MESSAGE),
            _assistant_event(2, "Same plan"),
            _user_event(3, message),
        ]
        expected_request = [original_request, REFRESH_DIRECTION_MESSAGE, message]
        expected_conversation = [original_request, REFRESH_DIRECTION_MESSAGE, "Same plan", message]

    captured, session = await _run_carried_turn(
        monkeypatch,
        events=events,
        message=message,
        previous_active_plan=previous_active_plan,
    )

    agent_input = captured["agent_input"]
    expected = _words(" ".join(expected_request))
    assert _words(agent_input.creator_request) == expected
    assert _words(session.active_plan["creator_request"]) == expected
    # The model sees the brief once, before the fresh session's own turns,
    # whether it came from this session's events or from the failed session.
    assert [turn["content"] for turn in agent_input.conversation] == expected_conversation


@pytest.mark.asyncio
async def test_clip_intent_planning_reads_the_carried_brief(monkeypatch) -> None:
    """Clip-specific instructions in the failed session's brief stay verifiable."""

    failed_plan, original_request, _events = _refresh_retry_fixture()
    captured, _session = await _run_carried_turn(
        monkeypatch,
        events=[_carried_brief_event(0, original_request), _user_event(1, "Slower, please")],
        message="Slower, please",
        previous_active_plan=failed_plan,
        clip_intents=True,
    )

    assert _words(captured["intent_creator_request"]) == _words(
        f"{original_request} Slower, please"
    )


def test_carried_brief_seed_skips_repeated_lines_and_keeps_the_new_message() -> None:
    original = "Narrated story about the bridge.\nTitle: 1882.\nNo other text."
    carried = {"creator_request": f"{original}\nSlower, please"}

    # Re-pasting the original prompt keeps only what it does not repeat.
    assert _carried_brief_seed(carried, original) == "Slower, please"
    assert _carried_brief_seed({"creator_request": original}, f"  {original}  ") == ""
    # Whole words only: "eye" is not repeated by "eyes".
    assert _carried_brief_seed({"creator_request": "eye"}, "Big eyes") == "eye"
    assert _carried_brief_seed(None, "Slower") == ""

    # The brief, not the new message, gives way at the shared bound.
    message = "Make the title red"
    long_brief = "x" * (CREATOR_REQUEST_MAX_CHARS - 5)
    seed = _carried_brief_seed({"creator_request": long_brief}, message)
    events = [_carried_brief_event(0, seed), _user_event(1, message)]
    request = _confirmed_creator_request(events, message)
    assert len(request) <= CREATOR_REQUEST_MAX_CHARS
    assert request.endswith(f"\n{message}")


def test_is_refresh_retry_message_exact_match_only() -> None:
    assert _is_refresh_retry_message(REFRESH_DIRECTION_MESSAGE) is True
    assert _is_refresh_retry_message(f"  {REFRESH_DIRECTION_MESSAGE}  ") is True
    assert _is_refresh_retry_message("Keep the same plan") is False
    assert _is_refresh_retry_message("Make it a fast montage") is False


def test_previous_accepted_strategy_reads_prior_edit_plan_strategy() -> None:
    previous_active_plan, _original_request, _events = _refresh_retry_fixture()
    strategy = _previous_accepted_strategy(previous_active_plan)
    assert strategy is not None
    assert strategy.direction == "guided_story"
    assert strategy.render_program == "guided"
    assert _previous_accepted_strategy(None) is None
    assert _previous_accepted_strategy({"edit_plan": {}}) is None


def test_pin_strategy_fields_on_refresh_only_restores_divergent_fields() -> None:
    _previous_active_plan, _original_request, _events = _refresh_retry_fixture()
    previous = CreativeStrategy(
        direction="guided_story",
        pacing="relaxed",
        target_duration_s=45,
        opening_title="Golden hour kickoff",
        audio_strategy="licensed_music",
    )
    # Only `direction` and `target_duration_s` diverge; the rest already match.
    proposed = CreativeStrategy(
        direction="fast_montage",
        pacing="relaxed",
        target_duration_s=15,
        opening_title="Golden hour kickoff",
        audio_strategy="licensed_music",
    )
    pinned, restored = _pin_strategy_fields_on_refresh(proposed, previous)
    assert pinned.direction == "guided_story"
    assert pinned.target_duration_s == 45
    assert set(restored) == {"direction", "target_duration_s"}

    identical, restored_none = _pin_strategy_fields_on_refresh(previous, previous)
    assert identical is previous
    assert restored_none == []


def test_original_creator_request_for_refresh_prefers_previous_plan() -> None:
    previous_active_plan, original_request, _events = _refresh_retry_fixture()
    result = _original_creator_request_for_refresh(previous_active_plan, [])
    assert result == original_request


def test_original_creator_request_for_refresh_falls_back_to_events_excluding_canned_message() -> (
    None
):
    events = [
        SimpleNamespace(
            sequence=1,
            role="user",
            event_type="user_message",
            payload={"message": "Make a fast highlight reel from my clips"},
        ),
        SimpleNamespace(
            sequence=2,
            role="assistant",
            event_type="assistant_strategy",
            payload={"message": "Proposed a plan"},
        ),
        SimpleNamespace(
            sequence=3,
            role="user",
            event_type="user_message",
            payload={"message": REFRESH_DIRECTION_MESSAGE},
        ),
    ]
    result = _original_creator_request_for_refresh(None, events)
    assert result == "Make a fast highlight reel from my clips"
    assert REFRESH_DIRECTION_MESSAGE not in result


@pytest.mark.asyncio
async def test_exact_all_media_capacity_choice_bypasses_model_and_call_budget(monkeypatch) -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    question = creator_routes._all_media_capacity_question(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            target_duration_s=40,
            audio_strategy="licensed_music",
        ),
    )
    assert question is not None
    option = question["options"][1]
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload=question,
            )
        ],
        agent_call_count=2,
        agent_call_budget=2,
        question_count=1,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=manifest.manifest_hash,
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    to_thread = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=option,
    )

    assert result is response
    to_thread.assert_not_awaited()
    assert session.agent_call_count == 2
    assert session.status == "awaiting_confirmation"
    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["direction"] == "fast_montage"
    assert strategy["media_scope"] == "all"
    assert strategy["target_duration_s"] == 40


@pytest.mark.asyncio
async def test_historical_capacity_choice_resolves_before_available_question_budget(
    monkeypatch,
) -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 4.0} for index in range(33)
        ],
        guided_capability_enabled=True,
    )
    question = creator_routes._all_media_capacity_question(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            target_duration_s=40,
            audio_strategy="licensed_music",
        ),
    )
    assert question is not None
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload=question,
            ),
            SimpleNamespace(
                sequence=2,
                role="user",
                event_type="user_message",
                payload={"message": question["options"][1]},
            ),
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=manifest.manifest_hash,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            media_scope="all",
            target_duration_s=40,
            audio_strategy="licensed_music",
        ),
        summary="A fast all-media cut.",
    )
    to_thread = AsyncMock(return_value=SimpleNamespace(action=action))
    append_event = AsyncMock()
    response = SimpleNamespace(status="awaiting_confirmation")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Make the opening more energetic",
    )

    assert result is response
    to_thread.assert_awaited_once()
    assert session.question_count == 0
    assert all(
        call.kwargs.get("event_type") != "assistant_question"
        for call in append_event.await_args_list
    )
    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["media_scope"] == "all"
    assert strategy["target_duration_s"] == 40


def test_explicit_typed_intent_survives_varied_wording_and_negative_scope(monkeypatch) -> None:
    monkeypatch.setattr(settings, "creator_prompt_fidelity_enabled", True, raising=False)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 20,
        },
        media=[{"media_id": "clip-1", "kind": "video"}],
        guided_capability_enabled=True,
    )
    typed = CreativeStrategy(
        audio_strategy="voiceover",
        execution_contract="guided_voiceover_v1",
        media_scope="all",
        participant_labels="single_subject",
        score_labels=True,
        sport_labels=True,
        context_label={"kind": "sport"},
    )

    retained = _apply_explicit_render_intent(typed, "Tüm yüklenen medyayı kullan.", manifest)
    corrected = _apply_explicit_render_intent(
        retained,
        "Don't use all media; use only the selected clips.",
        manifest,
        latest_user_message="Don't use all media; use only the selected clips.",
    )

    assert retained.media_scope == "all"
    assert {intent.transcript_kind for intent in retained.clip_intents} == {
        "participant",
        "score",
        "topic",
    }
    assert corrected.media_scope == "selected"
    assert _explicit_media_scope("Don't use all media") == "selected"


def test_confirmed_creator_request_keeps_the_shared_bound() -> None:
    first = "a" * 7000
    second = "b" * 7000
    events = [
        SimpleNamespace(sequence=0, role="user", payload={"message": first}),
        SimpleNamespace(sequence=1, role="user", payload={"message": second}),
    ]

    request = _confirmed_creator_request(events, "")

    assert len(request) == 12000
    assert request.startswith(first)


@pytest.mark.parametrize(
    ("caption_style", "expected_style"),
    [("auto", None), ("editorial", "sentence"), ("clean", "sentence"), ("kinetic", "word")],
)
def test_guided_brief_seeds_pinned_narration_for_worker_transcription(
    monkeypatch, caption_style, expected_style
) -> None:
    monkeypatch.setattr(settings, "creator_prompt_fidelity_enabled", True, raising=False)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 12,
        },
        media=[{"media_id": "clip-1", "kind": "video"}],
        guided_capability_enabled=True,
    )
    plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            audio_strategy="voiceover",
            execution_contract="guided_voiceover_v1",
            media_scope="all",
            caption_style=caption_style,
        ),
    )
    item = SimpleNamespace(
        edit_proposal=None,
        voiceover_gcs_path="voiceover-uploads/user/item/voice.webm",
        voiceover_generation="voice-generation-1",
        voiceover_duration_s=12,
    )

    _seed_guided_specialist_brief(
        item, plan, summary="Use the narration", creator_request="Use all"
    )

    assert item.edit_proposal["brief"]["narration"] == {
        "gcs_path": "voiceover-uploads/user/item/voice.webm",
        "generation": "voice-generation-1",
        "duration_s": 12.0,
        "words": [],
        "language": "",
        **({"caption_style": expected_style} if expected_style else {}),
    }


def _craft_bundle(
    *,
    session_id: uuid.UUID,
    job_id: uuid.UUID,
    generation_id: str,
    idempotency_key: str = "craft-1",
) -> CreatorCraftBundle:
    pins = {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": str(job_id),
        "expected_variant_id": "variant-1",
        "expected_generation_id": generation_id,
        "expected_revision": 3,
        "expected_ownership_epoch": 4,
    }
    return CreatorCraftBundle(
        session_id=str(session_id),
        idempotency_key=idempotency_key,
        commands=[{**pins, "command": "set_caption_style", "caption_style": "word"}],
        **pins,
    )


def test_creator_clip_metadata_dispatch_uses_analysis_queue() -> None:
    from app.tasks.creator_clip_metadata import (
        CREATOR_CLIP_METADATA_QUEUE,
        analyze_creator_clip_metadata,
    )

    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=5)

    creator_routes._enqueue_creator_clip_metadata(item, plan)

    analyze_creator_clip_metadata.apply_async.assert_called_once_with(
        args=[str(item.id), 5], queue=CREATOR_CLIP_METADATA_QUEUE
    )


def test_preserve_clip_order_requires_explicit_order_language() -> None:
    assert _requests_preserved_clip_order("Keep this exact generation and clip order")
    assert _requests_preserved_clip_order("preserve the same sequence")
    assert not _requests_preserved_clip_order("Make the opening more energetic")


@pytest.mark.asyncio
async def test_previous_creator_clip_order_maps_durable_timeline_to_item_paths() -> None:
    user_id = uuid.uuid4()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        current_job_id=uuid.uuid4(),
        clip_gcs_paths=[
            "users/u/thread/first.mp4",
            "users/u/thread/second.mp4",
            "users/u/thread/third.mp4",
        ],
    )
    session = SimpleNamespace(creator_id=user_id, target_variant_id="original_text")
    previous_job = SimpleNamespace(
        user_id=user_id,
        content_plan_item_id=item.id,
        status="variants_ready",
        all_candidates={
            "clip_paths": [
                "generative-jobs/old/sources/000_first.mp4",
                "generative-jobs/old/sources/001_second.mp4",
                "generative-jobs/old/sources/002_third.mp4",
            ]
        },
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "ai_timeline": {
                        "slots": [
                            {"order": 0, "clip_index": 1},
                            {"order": 1, "clip_index": 0},
                            {"order": 2, "clip_index": 2},
                        ]
                    },
                    "user_timeline": {
                        "slots": [
                            {"order": 0, "clip_index": 2},
                            {"order": 1, "clip_index": 0},
                        ]
                    },
                }
            ]
        },
    )
    db = AsyncMock()
    db.get.return_value = previous_job

    order = await _previous_creator_clip_order(
        db,
        item,
        session,
        "Keep this exact generation and clip order.",
    )

    assert order == [2, 0]


@pytest.fixture()
def client() -> TestClient:
    user = SimpleNamespace(id=uuid.uuid4())

    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value
    app.dependency_overrides.clear()
    settings.main_creator_agent_enabled = False
    settings.main_creator_agent_rollout_percent = 0


def _manifest(monkeypatch, *, has_voiceover: bool = False):
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(creator_capabilities.settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(creator_capabilities.settings, "main_creator_agent_rollout_percent", 100)
    return resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=has_voiceover,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )


def test_creator_speech_cut_uses_candidate_specific_kill_switch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "silence_cut_enabled", False)
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    assert _creator_speech_cut_source_enabled("retake_review") is True
    assert _creator_speech_cut_source_enabled("silence_review") is False

    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "retake_cut_enabled", False)
    assert _creator_speech_cut_source_enabled("retake_review") is False
    assert _creator_speech_cut_source_enabled("filler_review") is True
    assert _creator_speech_cut_source_enabled("untrusted_source") is False


def test_required_creator_speech_dispatch_keeps_last_good_public(monkeypatch) -> None:
    from app.agents._schemas.creator_agent import ApplySpeechCutCommand
    from app.pipeline.speech_cut_state import cut_revision, make_candidate

    candidate = make_candidate(
        start_s=1.0,
        end_s=1.5,
        reason="filler_acoustic",
        source="retake_review",
        preview="um",
        source_fingerprint="source-a",
        transcript_hash="transcript-a",
    )
    job_id = uuid.uuid4()
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
        "base_video_path": f"generative-jobs/{job_id}/base.mp4",
        "speech_cut_candidates": [candidate],
        "speech_cut_forced_removals": [],
        "speech_cuts_disabled": False,
        "silence_cut": {"removed": []},
    }
    variant["speech_cut_revision"] = cut_revision(variant)
    public_before = copy.deepcopy(variant)
    job = SimpleNamespace(
        id=job_id,
        status="variants_ready",
        assembly_plan={
            "speech_cleanup_contract": "required_v1",
            "silence_cut_disabled": True,
            "variants": [variant],
        },
    )
    command = ApplySpeechCutCommand(
        command="apply_speech_cut",
        candidate_id=candidate["candidate_id"],
        expected_cut_revision=variant["speech_cut_revision"],
        expected_manifest_hash="a" * 64,
        expected_context_hash="b" * 64,
        expected_job_id=str(job_id),
        expected_variant_id="subtitled",
        expected_generation_id=variant["render_generation_id"],
        expected_revision=1,
        expected_ownership_epoch=1,
    )
    monkeypatch.setattr(creator_routes.settings, "retake_cut_enabled", True)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_a, **_k: None)

    request, _operation_id, _prior = creator_routes._stage_creator_speech_cut(
        job,
        variant_id="subtitled",
        command=command,
    )

    assert request["operation"] == "apply_speech_cut_candidate"
    assert job.assembly_plan["variants"] == [public_before]
    assert job.assembly_plan["speech_cut_previous_variants"] == [public_before]
    assert job.assembly_plan["silence_cut_disabled"] is True
    control = job.assembly_plan["speech_cut_control"]
    assert control["render_generation_id"]
    assert control["in_flight"]


@pytest.mark.parametrize(
    ("count", "status", "expected"),
    [
        (0, "running", False),
        (1, "running", True),
        (0, "queued", True),
        (0, "complete", True),
    ],
)
def test_auto_iteration_finalization_is_one_cycle_idempotent(
    count: int, status: str, expected: bool
) -> None:
    session = SimpleNamespace(
        automatic_revision_count=count,
        last_review={"auto_iteration": {"status": status}},
    )

    assert _auto_iteration_already_finalized(session) is expected


@pytest.mark.asyncio
async def test_auto_iteration_keeps_ready_phase_until_craft_succeeds(monkeypatch) -> None:
    """The craft gateway must see the ready phase on first dispatch and retry."""

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_feedback",
        revision=11,
        ownership_epoch=7,
        auto_iteration_opt_in=False,
        max_render_attempts=2,
        render_attempts=1,
        automatic_revision_count=0,
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
        last_review={
            "status": "complete",
            "review_mode": "objective",
            "render_generation_id": "generation-1",
            "confidence": 0.9,
            "quality_score": 3.0,
            "expected_improvement": 0.5,
            "objective_tag": "objective_quality",
            "allowlist_action": "caption_legibility",
            "proposed_revision": {"revision_id": "revision-1", "summary": "Fix captions"},
        },
    )
    item = SimpleNamespace(id=item_id)
    plan = SimpleNamespace(ownership_epoch=7)
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-1",
                    "render_status": "ready",
                }
            ]
        },
    )
    manifest = SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64)
    db = AsyncMock()
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = None
    db.execute.return_value = query_result
    db.get.return_value = job
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(side_effect=[session, session]))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )

    async def append_event(*_args, **_kwargs):
        session.revision += 1

    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(
        creator_routes,
        "evaluate_auto_iteration",
        lambda *_args, **_kwargs: SimpleNamespace(decision="eligible"),
    )
    captured: dict = {}

    def build_bundle(**kwargs):
        captured["pin"] = kwargs["pin"]
        return SimpleNamespace(model_dump=lambda mode: {"bounded": True})

    monkeypatch.setattr(creator_routes, "build_auto_bundle", build_bundle)

    async def craft(*_args, **_kwargs):
        captured["status_at_craft"] = session.status
        return SimpleNamespace(generation="generation-2", receipt_id="craft-receipt-1")

    monkeypatch.setattr(creator_routes, "execute_creator_craft", craft)
    monkeypatch.setattr(
        creator_routes,
        "_response",
        AsyncMock(return_value=SimpleNamespace(status="rendering")),
    )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_quality_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_auto_iteration_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    result = await creator_routes.request_creator_auto_iteration(
        str(item_id),
        AutoIterationBody(
            session_id=session_id,
            expected_revision=11,
            opt_in=True,
            client_event_id="auto-event-1",
        ),
        user,
        db,
    )

    assert result.status == "rendering"
    assert captured["status_at_craft"] == "awaiting_feedback"
    assert captured["pin"]["expected_revision"] == 12


def test_strict_creator_formats_never_use_montage_fallback() -> None:
    assert _strict_creator_format("day_vlog") is True
    assert _strict_creator_format("single_hero") is True
    assert _strict_creator_format("montage") is False


def _craft_route_context(*, user_id: uuid.UUID, job_id: uuid.UUID, session_id: uuid.UUID):
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=session_id,
        creator_id=user_id,
        plan_item_id=item_id,
        status="awaiting_feedback",
        revision=3,
        ownership_epoch=4,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=4,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-2",
                    "render_status": "ready",
                }
            ]
        },
    )
    manifest = SimpleNamespace(
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        capabilities={"caption_style": SimpleNamespace(available=True)},
    )
    return item, plan, session, job, manifest


def _required_speech_craft_context(
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    session_id: uuid.UUID,
    with_caption: bool = False,
):
    from app.pipeline.speech_cut_state import cut_revision, make_candidate

    item, plan, session, job, _manifest = _craft_route_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
    )
    candidate = make_candidate(
        start_s=1.0,
        end_s=1.5,
        reason="filler_acoustic",
        source="retake_review",
        preview="um",
        source_fingerprint="source-a",
        transcript_hash="transcript-a",
    )
    variant = {
        "variant_id": "variant-1",
        "resolved_archetype": "subtitled",
        "render_generation_id": "generation-2",
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
        "base_video_path": f"generative-jobs/{job_id}/base.mp4",
        "voiceover_caption_style": "sentence",
        "speech_cut_candidates": [candidate],
        "speech_cut_forced_removals": [],
        "speech_cuts_disabled": False,
        "silence_cut": {"removed": []},
    }
    variant["speech_cut_revision"] = cut_revision(variant)
    job.assembly_plan = {
        "speech_cleanup_contract": "required_v1",
        "silence_cut_disabled": True,
        "variants": [variant],
    }
    manifest = SimpleNamespace(
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        capabilities={
            "automatic_cut": SimpleNamespace(available=True),
            "caption_style": SimpleNamespace(available=True),
        },
    )
    pins = {
        "expected_manifest_hash": manifest.manifest_hash,
        "expected_context_hash": manifest.context_hash,
        "expected_job_id": str(job_id),
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-2",
        "expected_revision": 3,
        "expected_ownership_epoch": 4,
    }
    commands = [
        {
            **pins,
            "command": "apply_speech_cut",
            "candidate_id": candidate["candidate_id"],
            "expected_cut_revision": variant["speech_cut_revision"],
        }
    ]
    if with_caption:
        commands.append({**pins, "command": "set_caption_style", "caption_style": "word"})
    body = CreatorCraftBundle(
        session_id=str(session_id),
        idempotency_key=("speech-caption" if with_caption else "speech-only"),
        commands=commands,
        **pins,
    )
    return item, plan, session, job, manifest, body


def _configure_required_speech_craft(
    monkeypatch,
    *,
    item,
    plan,
    session,
    manifest,
) -> None:
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_stable_manifest_fingerprint", lambda _manifest: "stable")
    monkeypatch.setattr(creator_routes.settings, "retake_cut_enabled", True)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args, **_kwargs: None)


def test_creator_speech_cut_rejects_inflight_sibling_before_snapshot(monkeypatch) -> None:
    from app.agents._schemas.creator_agent import ApplySpeechCutCommand

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    _item, _plan, _session, job, _manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=uuid.uuid4(),
    )
    job.assembly_plan["variants"].append(
        {
            "variant_id": "song_text",
            "render_generation_id": uuid.uuid4().hex,
            "render_status": "pending",
            "ok": False,
        }
    )
    before = copy.deepcopy(job.assembly_plan)
    command = next(value for value in body.commands if isinstance(value, ApplySpeechCutCommand))
    monkeypatch.setattr(creator_routes.settings, "retake_cut_enabled", True)

    with pytest.raises(HTTPException) as caught:
        creator_routes._stage_creator_speech_cut(
            job,
            variant_id="variant-1",
            command=command,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "variant_initial_render_in_progress"
    assert job.assembly_plan == before


def test_creator_craft_response_projects_private_state_without_mutating_receipt() -> None:
    import copy

    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        status="succeeded",
        result={
            "generation": "generation-3",
            "preview": {
                "caption_style": "word",
                "_speech_cleanup_internal": {"secret": True},
                "candidate": {
                    "clip_source_instance_ids": ["private-id"],
                    "clip_metadata_identity_index_v2": {"records": []},
                    "clip_paths": ["source.mp4"],
                },
            },
        },
    )
    stored = copy.deepcopy(receipt.result)

    response = creator_routes._craft_response(receipt)

    assert response.preview == {
        "caption_style": "word",
        "candidate": {"clip_paths": ["source.mp4"]},
    }
    assert receipt.result == stored


def test_creator_session_response_projects_nested_private_state(monkeypatch) -> None:
    payload = {
        "id": str(uuid.uuid4()),
        "status": "awaiting_feedback",
        "revision": 3,
        "render_attempts": 1,
        "max_render_attempts": 2,
        "can_render": True,
        "pending_plan": {
            "summary": "Keep this",
            "_speech_cleanup_internal": {"secret": True},
            "candidate": {
                "clip_source_instance_ids": ["private-id"],
                "clip_paths": ["source.mp4"],
            },
        },
        "current_job_id": None,
        "last_review": None,
        "events": [],
        "auto_iteration": None,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }
    monkeypatch.setattr(creator_routes, "serialize_session", lambda _session: payload)

    response = creator_routes._creator_session_response(SimpleNamespace())

    assert response.pending_plan == {
        "summary": "Keep this",
        "candidate": {"clip_paths": ["source.mp4"]},
    }


@pytest.mark.asyncio
async def test_required_creator_speech_only_uses_one_private_generation(monkeypatch) -> None:
    from app.tasks.generative_build import rerender_speech_timing

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
    )
    public_before = copy.deepcopy(job.assembly_plan["variants"])
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    captured: dict[str, CreatorAgentExecution] = {}

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = uuid.uuid4()
            captured["receipt"] = value

    db.add = MagicMock(side_effect=add)
    enqueue = MagicMock()
    monkeypatch.setattr(rerender_speech_timing, "apply_async", enqueue)
    _configure_required_speech_craft(
        monkeypatch,
        item=item,
        plan=plan,
        session=session,
        manifest=manifest,
    )

    response = await creator_routes.execute_creator_craft(
        str(item.id), body, SimpleNamespace(id=user_id), db
    )

    receipt = captured["receipt"]
    control = job.assembly_plan["speech_cut_control"]
    generation = control["render_generation_id"]
    assert response.generation == generation
    assert receipt.result["generation"] == generation
    assert receipt.result["prepared"]["generation"] == generation
    assert receipt.result["speech_cut_operation_id"] == control["operation_id"]
    assert receipt.result["prepared"]["speech_cut_operation_id"] == control["operation_id"]
    assert session.target_generation_id == generation
    assert job.assembly_plan["variants"] == public_before
    assert job.assembly_plan["speech_cut_previous_variants"] == public_before
    staged = job.assembly_plan["speech_cut_previous_variant"]
    assert staged["render_generation_id"] == generation
    assert staged["speech_cut_candidates"][0]["status"] == "applying"
    enqueue.assert_called_once_with(
        args=[str(job_id), control["operation_id"]],
        queue="plan-jobs",
        task_id=f"creator-craft-{receipt.id}-{generation}",
    )


@pytest.mark.asyncio
async def test_required_creator_speech_and_editor_keep_editor_lane_private(monkeypatch) -> None:
    from app.tasks.generative_build import rerender_speech_timing

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
        with_caption=True,
    )
    public_before = copy.deepcopy(job.assembly_plan["variants"])
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    captured: dict[str, CreatorAgentExecution] = {}

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = uuid.uuid4()
            captured["receipt"] = value

    db.add = MagicMock(side_effect=add)

    def prepare_editor(job_value, variant_id, *_args, **_kwargs):
        variants = copy.deepcopy(job_value.assembly_plan["variants"])
        target = next(value for value in variants if value["variant_id"] == variant_id)
        assert target == public_before[0]
        target.update(
            {
                "voiceover_caption_style": "word",
                "render_generation_id": "discarded-editor-generation",
                "render_status": "rendering",
                "ok": False,
            }
        )
        job_value.assembly_plan = {**job_value.assembly_plan, "variants": variants}
        return {
            "generation": "discarded-editor-generation",
            "has_render_section": True,
            "sections": {"caption_meta": True},
        }

    enqueue = MagicMock()
    monkeypatch.setattr(creator_routes, "prepare_editor_commit", prepare_editor)
    monkeypatch.setattr(rerender_speech_timing, "apply_async", enqueue)
    _configure_required_speech_craft(
        monkeypatch,
        item=item,
        plan=plan,
        session=session,
        manifest=manifest,
    )

    response = await creator_routes.execute_creator_craft(
        str(item.id), body, SimpleNamespace(id=user_id), db
    )

    receipt = captured["receipt"]
    control = job.assembly_plan["speech_cut_control"]
    generation = control["render_generation_id"]
    staged = job.assembly_plan["speech_cut_previous_variant"]
    assert response.generation == generation
    assert receipt.result["generation"] == generation
    assert receipt.result["prepared"]["generation"] == generation
    assert session.target_generation_id == generation
    assert staged["render_generation_id"] == generation
    assert staged["voiceover_caption_style"] == "word"
    assert staged["speech_cut_candidates"][0]["status"] == "applying"
    assert job.assembly_plan["variants"] == public_before
    assert job.assembly_plan["speech_cut_previous_variants"] == public_before
    assert "discarded-editor-generation" not in repr(job.assembly_plan)
    enqueue.assert_called_once_with(
        args=[str(job_id), control["operation_id"]],
        queue="plan-jobs",
        task_id=f"creator-craft-{receipt.id}-{generation}",
    )


@pytest.mark.asyncio
async def test_creator_craft_rejects_stale_exact_generation_pin(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id),
            _craft_bundle(
                session_id=session_id,
                job_id=job_id,
                generation_id="generation-1",
            ),
            SimpleNamespace(id=user_id),
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Creator render generation changed"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_craft_rejects_private_initial_generation_before_staging(
    monkeypatch,
) -> None:
    import copy

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    job.assembly_plan["_speech_cleanup_internal"] = {
        "required_speech_generation_locks": {"variant-1": "initial-generation"}
    }
    job.status = "processing"
    stored = copy.deepcopy(job.assembly_plan)
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = job
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    resolve_context = AsyncMock(return_value=(manifest, []))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        resolve_context,
    )
    build_commit = MagicMock()
    monkeypatch.setattr(creator_routes, "build_core_craft_editor_commit", build_commit)

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id),
            _craft_bundle(
                session_id=session_id,
                job_id=job_id,
                generation_id="generation-2",
            ),
            SimpleNamespace(id=user_id),
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "variant_initial_render_in_progress"
    assert job.assembly_plan == stored
    resolve_context.assert_not_awaited()
    build_commit.assert_not_called()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_iteration_rejects_private_initial_generation_without_controller_write(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_feedback",
        revision=11,
        ownership_epoch=7,
        auto_iteration_opt_in=False,
        max_render_attempts=2,
        render_attempts=1,
        automatic_revision_count=0,
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
        last_review={
            "status": "complete",
            "review_mode": "objective",
            "render_generation_id": "generation-1",
            "confidence": 0.9,
            "quality_score": 3.0,
            "expected_improvement": 0.5,
            "objective_tag": "objective_quality",
            "allowlist_action": "caption_legibility",
            "proposed_revision": {"revision_id": "revision-1", "summary": "Fix captions"},
        },
    )
    item = SimpleNamespace(id=item_id)
    plan = SimpleNamespace(ownership_epoch=7)
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-1",
                    "render_status": "pending",
                }
            ],
            "_speech_cleanup_internal": {
                "required_speech_generation_locks": {"variant-1": "initial-generation"}
            },
        },
    )
    db = AsyncMock()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    db.execute.return_value = no_row
    db.get.return_value = job
    append_event = AsyncMock()
    resolve_context = AsyncMock()
    craft = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "resolve_item_creator_context", resolve_context)
    monkeypatch.setattr(creator_routes, "execute_creator_craft", craft)
    monkeypatch.setattr(
        creator_routes,
        "evaluate_auto_iteration",
        lambda *_args, **_kwargs: SimpleNamespace(decision="eligible"),
    )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_quality_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_auto_iteration_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    with pytest.raises(HTTPException) as caught:
        await creator_routes.request_creator_auto_iteration(
            str(item_id),
            AutoIterationBody(
                session_id=session_id,
                expected_revision=11,
                opt_in=True,
                client_event_id="auto-event-locked",
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "variant_initial_render_in_progress"
    assert session.auto_iteration_opt_in is False
    append_event.assert_not_awaited()
    resolve_context.assert_not_awaited()
    craft.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_craft_replays_succeeded_receipt_without_reenqueue(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    body = _craft_bundle(
        session_id=session_id,
        job_id=job_id,
        generation_id="generation-2",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(body.model_dump(mode="json")),
        status="succeeded",
        result={
            "generation": "generation-2",
            "prepared": {"generation": "generation-2", "sections": {"caption_meta": True}},
            "preview": {"caption_style": "word"},
        },
    )
    # The first direct craft commit advanced the controller revision before
    # publishing. An exact idempotent replay still carries the original pin.
    session.revision = body.expected_revision + 1
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = receipt
    db.execute.return_value = receipt_result
    db.get.return_value = job
    enqueue = MagicMock()
    monkeypatch.setattr(creator_routes, "enqueue_editor_commit_render", enqueue)
    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )

    response = await creator_routes.execute_creator_craft(
        str(item.id), body, SimpleNamespace(id=user_id), db
    )

    assert response.status == "succeeded"
    assert response.generation == "generation-2"
    assert response.preview == {"caption_style": "word"}
    enqueue.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_craft_enqueue_failure_restores_plan_and_fails_receipt(monkeypatch) -> None:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest = _craft_route_context(
        user_id=user_id, job_id=job_id, session_id=session_id
    )
    body = _craft_bundle(
        session_id=session_id,
        job_id=job_id,
        generation_id="generation-2",
    )
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "variant-1",
                "render_generation_id": "generation-2",
                "render_status": "ready",
                "caption_meta": {"style": "sentence"},
            },
            {"variant_id": "sibling", "render_generation_id": "sibling-1", "rank": 2},
        ],
        "unrelated_state": "before",
    }
    previous_assembly_plan = job.assembly_plan.copy()
    receipt_id = uuid.uuid4()
    failed_receipt = SimpleNamespace(id=receipt_id, status="running", error=None)
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.side_effect = [job, job, session, failed_receipt]

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = receipt_id

    db.add = MagicMock(side_effect=add)
    editor_commit = SimpleNamespace(
        caption_meta=SimpleNamespace(style="word"),
        timeline_slots=None,
        sound_effects=None,
        media_overlays=None,
    )

    def prepare(*_args, **_kwargs):
        job.assembly_plan = {
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_generation_id": "generation-3",
                    "render_status": "rendering",
                },
                {"variant_id": "sibling", "render_generation_id": "sibling-2", "rank": 3},
            ],
            "unrelated_state": "concurrent-update",
        }
        return {"generation": "generation-3", "sections": {"caption_meta": True}}

    monkeypatch.setattr(creator_routes, "_require_feature", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(
        creator_routes,
        "build_core_craft_editor_commit",
        lambda *_args, **_kwargs: editor_commit,
    )
    monkeypatch.setattr(creator_routes, "prepare_editor_commit", prepare)
    monkeypatch.setattr(creator_routes, "craft_preview", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(creator_routes, "_stable_manifest_fingerprint", lambda _manifest: "stable")
    monkeypatch.setattr(
        creator_routes,
        "enqueue_editor_commit_render",
        MagicMock(side_effect=RuntimeError("broker unavailable")),
    )

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id), body, SimpleNamespace(id=user_id), db
        )

    assert caught.value.status_code == 503
    assert job.assembly_plan["variants"][0] == previous_assembly_plan["variants"][0]
    assert job.assembly_plan["variants"][1]["render_generation_id"] == "sibling-2"
    assert job.assembly_plan["unrelated_state"] == "concurrent-update"
    assert failed_receipt.status == "failed"
    assert failed_receipt.error["code"] == "craft_enqueue_failed"
    assert failed_receipt.error["rolled_back"] is True
    # The route advances the controller revision together with the staged
    # generation; broker publication failure restores that exact session state.
    assert session.revision == 3
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_creator_speech_enqueue_failure_rolls_back_private_owner(
    monkeypatch,
) -> None:
    from app.tasks.generative_build import rerender_speech_timing

    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()
    item, plan, session, job, manifest, body = _required_speech_craft_context(
        user_id=user_id,
        job_id=job_id,
        session_id=session_id,
    )
    plan_before = copy.deepcopy(job.assembly_plan)
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    captured: dict[str, CreatorAgentExecution] = {}

    def add(value):
        if isinstance(value, CreatorAgentExecution):
            value.id = uuid.uuid4()
            captured["receipt"] = value

    def get(model, *_args, **_kwargs):
        if model is Job:
            return job
        if model is CreatorAgentSession:
            return session
        if model is CreatorAgentExecution:
            return captured["receipt"]
        raise AssertionError(f"unexpected model lookup: {model}")

    db.add = MagicMock(side_effect=add)
    db.get = AsyncMock(side_effect=get)
    enqueue = MagicMock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr(rerender_speech_timing, "apply_async", enqueue)
    _configure_required_speech_craft(
        monkeypatch,
        item=item,
        plan=plan,
        session=session,
        manifest=manifest,
    )

    with pytest.raises(HTTPException) as caught:
        await creator_routes.execute_creator_craft(
            str(item.id), body, SimpleNamespace(id=user_id), db
        )

    receipt = captured["receipt"]
    assert caught.value.status_code == 503
    assert job.assembly_plan == plan_before
    assert job.status == "variants_ready"
    assert job.started_at is None
    assert session.status == "awaiting_feedback"
    assert session.revision == 3
    assert getattr(session, "target_generation_id", None) is None
    assert receipt.status == "failed"
    assert receipt.error["code"] == "craft_enqueue_failed"
    assert receipt.error["rolled_back"] is True
    assert receipt.error["generation"] in enqueue.call_args.kwargs["task_id"]
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_creator_craft_rollback_restores_speech_owned_state_only() -> None:
    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    session_id = uuid.uuid4()
    started_at = datetime.now(UTC)
    previous_plan = {
        "variants": [
            {"variant_id": "target", "render_generation_id": "old", "render_status": "ready"}
        ],
        "silence_cut_disabled": True,
        "speech_cut_control": {"operation_id": "old-op"},
        "speech_cut_previous_variant": {"variant_id": "target", "render_status": "ready"},
        "speech_cut_previous_variants": [{"variant_id": "target"}],
        "speech_cut_last_error": "old-error",
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "target",
                    "render_generation_id": "new",
                    "render_status": "rendering",
                },
                {"variant_id": "sibling", "render_generation_id": "sibling-new"},
            ],
            "silence_cut_disabled": False,
            "speech_cut_control": {"operation_id": "new-op"},
            "speech_cut_previous_variant": {"variant_id": "target", "render_status": "rendering"},
            "speech_cut_previous_variants": [{"variant_id": "sibling"}],
            "speech_cut_last_error": None,
            "unrelated": "concurrent-update",
        },
    )
    session = SimpleNamespace(
        id=session_id,
        status="rendering",
        target_job_id=job_id,
        target_variant_id="target",
        target_generation_id="new",
        render_attempts=2,
        iteration_count=2,
        revision=4,
    )
    failed_receipt = SimpleNamespace(id=receipt_id, status="running", error=None)
    db = AsyncMock()
    db.get.side_effect = [job, session, failed_receipt]

    await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=session_id,
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation="new",
        error=RuntimeError("broker unavailable"),
        previous_job_state={"status": "variants_ready", "started_at": started_at.isoformat()},
        previous_session_state={
            "status": "awaiting_feedback",
            "target_job_id": str(job_id),
            "target_variant_id": "target",
            "target_generation_id": "old",
            "render_attempts": 1,
            "iteration_count": 1,
            "revision": 3,
        },
    )

    assert job.status == "variants_ready"
    assert job.started_at == started_at
    assert job.assembly_plan["variants"][0] == previous_plan["variants"][0]
    assert job.assembly_plan["variants"][1]["render_generation_id"] == "sibling-new"
    assert job.assembly_plan["unrelated"] == "concurrent-update"
    for key in (
        "silence_cut_disabled",
        "speech_cut_control",
        "speech_cut_previous_variant",
        "speech_cut_previous_variants",
        "speech_cut_last_error",
    ):
        assert job.assembly_plan[key] == previous_plan[key]
    assert failed_receipt.status == "failed"
    assert session.status == "awaiting_feedback"
    assert session.target_generation_id == "old"
    assert session.render_attempts == 1
    assert session.revision == 3
    # Canonical lock order (app/db_locks.CANONICAL_LOCK_ORDER): Job before
    # CreatorAgentSession.  This assertion is a lock-order guard, not an
    # implementation detail -- see tests/routes/test_lock_order.py.
    assert [call.args[0] for call in db.get.await_args_list] == [
        Job,
        CreatorAgentSession,
        CreatorAgentExecution,
    ]


@pytest.mark.asyncio
async def test_required_creator_speech_rollback_refuses_superseding_operation() -> None:
    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    session_id = uuid.uuid4()
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {"variant_id": "target", "render_generation_id": "old", "render_status": "ready"}
        ],
    }
    current_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": copy.deepcopy(previous_plan["variants"]),
        "speech_cut_control": {
            "variant_id": "target",
            "operation_id": "superseding-operation",
            # Reusing the token makes this specifically prove that operation id,
            # not generation alone, is part of the private ownership CAS.
            "render_generation_id": "speech-generation",
        },
        "speech_cut_previous_variant": {"variant_id": "target", "private": "new"},
        "speech_cut_previous_variants": copy.deepcopy(previous_plan["variants"]),
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(current_plan),
    )
    session = SimpleNamespace(
        id=session_id,
        status="rendering",
        target_job_id=job_id,
        target_variant_id="target",
        target_generation_id="speech-generation",
        render_attempts=2,
        iteration_count=2,
        revision=4,
    )
    receipt = SimpleNamespace(id=receipt_id, status="running", error=None)
    db = AsyncMock()
    db.get.side_effect = [job, session, receipt]
    stored_plan = copy.deepcopy(job.assembly_plan)
    stored_started_at = job.started_at

    await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=session_id,
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation="speech-generation",
        speech_cut_operation_id="original-operation",
        error=RuntimeError("broker unavailable"),
        previous_job_state={"status": "variants_ready", "started_at": None},
        previous_session_state={
            "status": "awaiting_feedback",
            "target_job_id": str(job_id),
            "target_variant_id": "target",
            "target_generation_id": "old",
            "render_attempts": 1,
            "iteration_count": 1,
            "revision": 3,
        },
    )

    assert job.assembly_plan == stored_plan
    assert job.status == "processing"
    assert job.started_at == stored_started_at
    assert session.status == "rendering"
    assert session.target_generation_id == "speech-generation"
    assert session.revision == 4
    assert receipt.status == "failed"
    assert receipt.error["rolled_back"] is False


@pytest.mark.asyncio
async def test_required_creator_enqueue_response_loss_preserves_adopted_private_owner() -> None:
    """A published task may claim/reserve before ``apply_async`` reports failure."""

    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    session_id = uuid.uuid4()
    operation_id = "speech-operation"
    generation = "speech-generation"
    last_good = {
        "variant_id": "target",
        "render_generation_id": "last-good",
        "render_status": "ready",
        "ok": True,
    }
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [copy.deepcopy(last_good)],
    }
    current_plan = {
        **copy.deepcopy(previous_plan),
        "speech_cut_control": {
            "variant_id": "target",
            "operation_id": operation_id,
            "render_generation_id": generation,
            "finalizer_claim": {
                "operation_id": operation_id,
                "attempt_id": "task-1:0:attempt",
                "render_generation_id": generation,
            },
        },
        "speech_cut_previous_variant": {
            **copy.deepcopy(last_good),
            "render_generation_id": generation,
            "render_status": "rendering",
            "ok": False,
        },
        "speech_cut_previous_variants": [copy.deepcopy(last_good)],
        "_speech_cleanup_internal": {
            "required_speech_generation_locks": {"target": generation},
            "working_render_variants": {
                f"target:{generation}": {
                    "variant_id": "target",
                    "render_generation_id": generation,
                    "render_status": "rendering",
                    "ok": False,
                }
            },
            "render_generation_cleanup_pending": [
                {
                    "generation": generation,
                    "prefix": f"generative-jobs/{job_id}/render-generations/{generation}/",
                    "upload_state": "writing",
                    "lease_expires_at": "2026-09-02T12:30:00+00:00",
                }
            ],
        },
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(current_plan),
    )
    session = SimpleNamespace(
        id=session_id,
        status="rendering",
        target_job_id=job_id,
        target_variant_id="target",
        target_generation_id=generation,
        render_attempts=2,
        iteration_count=2,
        revision=4,
    )
    receipt = SimpleNamespace(
        id=receipt_id,
        status="running",
        result={"prepared": {"generation": generation}},
        error=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.get.side_effect = [job, session, receipt]
    stored_job_plan = copy.deepcopy(job.assembly_plan)
    stored_job_state = (job.status, job.started_at)
    stored_session_state = copy.deepcopy(session.__dict__)

    disposition = await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=session_id,
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation=generation,
        speech_cut_operation_id=operation_id,
        error=RuntimeError("broker response lost"),
        previous_job_state={"status": "variants_ready", "started_at": None},
        previous_session_state={
            "status": "awaiting_feedback",
            "target_job_id": str(job_id),
            "target_variant_id": "target",
            "target_generation_id": "last-good",
            "render_attempts": 1,
            "iteration_count": 1,
            "revision": 3,
        },
    )

    assert disposition == "enqueue_uncertain"
    assert job.assembly_plan == stored_job_plan
    assert (job.status, job.started_at) == stored_job_state
    assert session.__dict__ == stored_session_state
    assert receipt.status == "running"
    assert receipt.result == {"prepared": {"generation": generation}}
    assert receipt.error["code"] == "craft_enqueue_uncertain"
    assert receipt.error["rolled_back"] is False
    assert receipt.completed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    [
        {},
        "malformed-claim",
        {"operation_id": "speech-operation"},
    ],
    ids=["empty", "non-object", "incomplete"],
)
async def test_required_creator_rollback_preserves_malformed_claim(claim) -> None:
    """Only ``finalizer_claim=None`` is route-owned pre-reservation state."""

    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    operation_id = "speech-operation"
    generation = "speech-generation"
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {"variant_id": "target", "render_generation_id": "old", "render_status": "ready"}
        ],
    }
    current_plan = {
        **copy.deepcopy(previous_plan),
        "speech_cut_control": {
            "variant_id": "target",
            "operation_id": operation_id,
            "render_generation_id": generation,
            "finalizer_claim": copy.deepcopy(claim),
        },
        "speech_cut_previous_variant": {"variant_id": "target"},
        "speech_cut_previous_variants": copy.deepcopy(previous_plan["variants"]),
    }
    job = SimpleNamespace(
        id=job_id,
        status="processing",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(current_plan),
    )
    receipt = SimpleNamespace(
        id=receipt_id,
        status="running",
        result={"prepared": {"generation": generation}},
        error=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.get.side_effect = [job, receipt]

    disposition = await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=uuid.uuid4(),
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation=generation,
        speech_cut_operation_id=operation_id,
        error=RuntimeError("broker response lost"),
    )

    assert disposition == "enqueue_uncertain"
    assert job.assembly_plan == current_plan
    assert job.status == "processing"
    assert receipt.status == "running"
    assert receipt.error["code"] == "craft_enqueue_uncertain"


@pytest.mark.asyncio
async def test_required_creator_rollback_preserves_already_published_generation() -> None:
    """A lost broker response can arrive after the worker's final transaction."""

    job_id = uuid.uuid4()
    receipt_id = uuid.uuid4()
    generation = uuid.uuid4().hex
    previous_plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {
                "variant_id": "target",
                "render_generation_id": uuid.uuid4().hex,
                "render_status": "ready",
            }
        ],
    }
    published_plan = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": None,
        "speech_cut_previous_variant": None,
        "speech_cut_previous_variants": None,
        "variants": [
            {
                "variant_id": "target",
                "render_generation_id": generation,
                "render_status": "ready",
                "video_path": (
                    f"generative-jobs/{job_id}/render-generations/{generation}/final.mp4"
                ),
            }
        ],
    }
    job = SimpleNamespace(
        id=job_id,
        status="variants_ready",
        started_at=datetime.now(UTC),
        assembly_plan=copy.deepcopy(published_plan),
    )
    receipt = SimpleNamespace(
        id=receipt_id,
        status="running",
        result={"prepared": {"generation": generation}},
        error=None,
        completed_at=None,
    )
    db = AsyncMock()
    db.get.side_effect = [job, receipt]

    disposition = await creator_routes._rollback_craft_commit(
        db,
        receipt_id=receipt_id,
        session_id=uuid.uuid4(),
        job_id=job_id,
        previous_assembly_plan=previous_plan,
        variant_id="target",
        generation=generation,
        speech_cut_operation_id=uuid.uuid4().hex,
        error=RuntimeError("broker response lost"),
    )

    assert disposition == "enqueue_uncertain"
    assert job.assembly_plan == published_plan
    assert job.status == "variants_ready"
    assert receipt.status == "running"
    assert receipt.error["code"] == "craft_enqueue_uncertain"
    assert receipt.completed_at is None


class _ExpiringNamespace:
    """Small ORM-like double that rejects attribute reads after rollback."""

    def __init__(self, **values):
        self.__dict__.update(values)
        self.__dict__["_expired"] = False

    def expire(self) -> None:
        self.__dict__["_expired"] = True

    def __getattribute__(self, name):
        if name not in {"__dict__", "expire", "_expired"}:
            if object.__getattribute__(self, "__dict__").get("_expired", False):
                raise MissingGreenlet("attribute access after AsyncSession.rollback()")
        return object.__getattribute__(self, name)


def test_apply_plan_intent_never_activates_missing_voiceover(monkeypatch) -> None:
    manifest = _manifest(monkeypatch, has_voiceover=True)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="narrated",
            audio_strategy="voiceover",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )
    item = SimpleNamespace(
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    with pytest.raises(HTTPException, match="Record a voiceover"):
        _apply_plan_intent(item, edit_plan)


def test_apply_plan_intent_maps_creative_caption_style_to_renderer_contract(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            caption_style="kinetic",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
    )
    item = SimpleNamespace(
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )

    _apply_plan_intent(item, edit_plan)

    assert item.voiceover_caption_style == "word"


def test_confirmed_guided_strategy_becomes_specialist_brief(monkeypatch) -> None:
    creator_request = "Photos should have a very fast transition, videos can be a bit longer"
    manifest = _manifest(monkeypatch)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            story_structure=["Cold open", "Build", "Payoff"],
            pacing="fast",
            render_program="guided",
            selected_media_ids=["clip-1"],
            image_layout="supporting_card",
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast", video_hold="longer", boundary_style="cut"
            ),
        ),
    )
    item = SimpleNamespace(edit_proposal=None)
    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="A sharp three-beat story",
        creator_request=creator_request,
    )

    assert item.edit_proposal["status"] == "briefing"
    assert item.edit_proposal["brief_ready"] is True
    assert item.edit_proposal["brief"] == {
        "direction": "guided_story",
        "goal": "Cold open; Build; Payoff",
        "pace": "fast",
        "duration_s": 24,
        "creator_request": creator_request,
        "image_layout": "supporting_card",
        "mixed_media_timing": {
            "image_hold": "very_fast",
            "video_hold": "longer",
            "boundary_style": "cut",
        },
        "output_orientation": "portrait",
    }


def test_specialist_brief_keeps_long_request_and_bounds_transcript_turns(monkeypatch) -> None:
    # A chat request may run to CREATOR_REQUEST_MAX_CHARS and a rationale to
    # 2,000 chars, but a transcript turn holds 1,000. Seeding used to raise
    # string_too_long, so "Create this video" returned a 500.
    creator_request = "Chapter 1 · Messi in a Barça shirt · 3 seconds · Messi is only #2. " * 19
    manifest = _manifest(monkeypatch)
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            render_program="guided",
            selected_media_ids=["clip-1"],
            rationale="r" * 2000,
        ),
    )
    item = SimpleNamespace(edit_proposal=None)

    _seed_guided_specialist_brief(item, edit_plan, summary="", creator_request=creator_request)

    assert len(creator_request) > 1000
    assert item.edit_proposal["brief"]["creator_request"] == creator_request
    user_turn, agent_turn = item.edit_proposal["conversation"]
    assert user_turn["content"] == creator_request.strip()[:1000]
    assert agent_turn["content"] == "r" * 1000


def test_specialist_brief_normalizes_pool_asset_audio_and_cadence_ids(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "asset-video-a", "kind": "video", "duration_s": 10},
            {"media_id": "asset-video-b", "kind": "video", "duration_s": 10},
        ],
    )
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            audio_strategy="original_audio",
            selected_media_ids=["asset-video-a", "asset-video-b"],
            montage_audio=MontageAudioPlan(
                preserve_source_audio=True,
                source_media_ids=["asset-video-a", "asset-video-b"],
            ),
            montage_cadence=MontageCadenceConstraint(
                source_media_ids=["asset-video-a", "asset-video-b"],
                cut_duration_s=1,
            ),
        ),
    )
    item = SimpleNamespace(edit_proposal=None)

    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="Alternate both pool videos.",
        creator_request="Alternate every one second and keep the original audio.",
    )

    assert item.edit_proposal["brief"]["montage_audio"]["source_media_ids"] == [
        "video-a",
        "video-b",
    ]
    assert item.edit_proposal["brief"]["montage_cadence"]["source_media_ids"] == [
        "video-a",
        "video-b",
    ]


def test_story_shape_round_trips_into_specialist_brief(monkeypatch) -> None:
    # KRI-118 item 1: a chat-picked story shape survives compile_strategy_to_plan
    # and lands on the guided specialist's ProposalBrief under the same field
    # names (story_shape/hero_media_id).
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(creator_capabilities.settings, "creator_montage_shapes_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "asset-hero", "kind": "video", "duration_s": 10},
            {"media_id": "clip-1", "kind": "video", "duration_s": 5},
        ],
    )
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            archetype="single_hero",
            hero_media_id="asset-hero",
            render_program="guided",
            media_scope="all",
        ),
    )
    assert edit_plan.strategy.archetype == "single_hero"
    item = SimpleNamespace(edit_proposal=None)

    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="I'm building this around your clip, with the rest as cutaways.",
        creator_request="Make my dive clip the star.",
    )

    assert item.edit_proposal["brief"]["story_shape"] == "single_hero"
    # The chat manifest's asset-* prefix is translated to the bare id the
    # guided specialist/planner uses, matching montage_audio/montage_cadence.
    assert item.edit_proposal["brief"]["hero_media_id"] == "hero"


def test_native_mixed_timing_replaces_stale_approved_proposal_with_fresh_brief(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy="licensed_music",
            render_program="native",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            ),
        ),
    )
    item = SimpleNamespace(
        edit_proposal={
            "proposal_version": 7,
            "generation_attempt_id": "old-approved-attempt",
            "status": "approved",
        }
    )

    _seed_guided_specialist_brief(
        item,
        edit_plan,
        summary="Fast photos and longer videos.",
        creator_request="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert edit_plan.strategy.render_program == "guided"
    assert item.edit_proposal["proposal_version"] == 9
    assert item.edit_proposal["status"] == "briefing"
    assert item.edit_proposal["brief_ready"] is True
    assert item.edit_proposal["brief"]["mixed_media_timing"] == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }


def test_truncated_main_creator_fallback_preserves_exact_mixed_media_request(
    monkeypatch,
) -> None:
    strategy = _fallback_strategy(
        _manifest(monkeypatch),
        user_message="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert strategy.direction == "fast_montage"
    assert strategy.mixed_media_timing is not None
    assert strategy.mixed_media_timing.model_dump() == {
        "image_hold": "very_fast",
        "video_hold": "longer",
        "boundary_style": "cut",
    }


def test_creator_route_uses_pinned_narration_duration_without_explicit_total(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "creator_prompt_fidelity_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 44.688,
        },
        media=[{"media_id": "clip-1", "kind": "video"}],
        guided_capability_enabled=True,
    )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(audio_strategy="voiceover", target_duration_s=24),
        "Make a narrated edit with the uploaded voiceover.",
        manifest=manifest,
    )

    assert _pinned_narration_target_duration_s(manifest) == 44.688
    assert strategy.target_duration_s == 44.688

    explicit = _apply_explicit_render_intent(
        strategy,
        "Make it 12 seconds long.",
        manifest=manifest,
    )
    assert explicit.target_duration_s == 12


def test_creator_route_keeps_exact_creator_authored_title_copy(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        'Use opening title "Sunday finals".',
        manifest=manifest,
    )

    assert strategy.opening_title == "Sunday finals"


@pytest.mark.asyncio
async def test_route_fallback_preserves_pinned_voiceover_and_all_media_draft(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "creator_prompt_fidelity_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 44.688,
        },
        media=[
            {"media_id": f"clip-{index:02d}", "kind": "video", "duration_s": 4}
            for index in range(20)
        ]
        + [{"media_id": f"photo-{index:02d}", "kind": "image"} for index in range(19)],
        guided_capability_enabled=True,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(side_effect=TerminalError("model truncated output")),
    )
    append_event = AsyncMock()
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=(
            "Use all uploaded media. Transition photos in 0.3 seconds and let the videos breathe."
        ),
    )

    assert result is response
    assert session.status == "briefing"
    assert session.active_plan is None
    assert session.last_error == {"code": "provider_unavailable"}
    failure = append_event.await_args.kwargs
    assert failure["event_type"] == "assistant_error"
    assert failure["payload"] == {
        "message": (
            "I couldn't reach the planner right now. Your request is saved; try again later."
        ),
        "code": "provider_unavailable",
    }


@pytest.mark.asyncio
async def test_route_provider_quota_saves_request_restores_call_count_and_retry_clears_error(
    monkeypatch,
) -> None:
    manifest = _manifest(monkeypatch)
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="briefing")
    append_event = AsyncMock()
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
            target_duration_s=4,
        ),
        summary="A fast montage.",
    )
    to_thread = AsyncMock(
        side_effect=[
            ProviderQuotaExceededError(reason="monthly_spend_limit"),
            SimpleNamespace(action=action),
        ]
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    first = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Use the first clip as a fast montage.",
    )

    assert first is response
    assert session.status == "briefing"
    assert session.active_plan is None
    assert session.agent_call_count == 0
    assert session.last_error == {"code": "provider_quota_exceeded"}
    assert append_event.await_args.kwargs["event_type"] == "assistant_error"
    assert append_event.await_args.kwargs["payload"] == {
        "message": (
            "I'm getting rate-limited by the planner right now. "
            "Your request is saved; try again in a moment."
        ),
        "code": "provider_quota_exceeded",
    }

    session.status = "planning"
    append_event.reset_mock()
    second = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Use the first clip as a fast montage.",
    )

    assert second is response
    assert session.status == "awaiting_confirmation"
    assert session.active_plan is not None
    assert session.last_error is None
    assert session.agent_call_count == 1


@pytest.mark.asyncio
async def test_planning_turn_flag_off_does_not_admit_creator_preparation(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    monkeypatch.setattr(settings, "creator_clip_preparation_enabled", False)
    preparation = AsyncMock(side_effect=AssertionError("preparation must stay disabled"))
    monkeypatch.setattr("app.services.creator_preparation.maybe_prepare", preparation)

    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            media_scope="all",
            target_duration_s=8,
        ),
        summary="A fast montage.",
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Make a fast montage.",
    )

    assert result is response
    preparation.assert_not_awaited()


@pytest.mark.asyncio
async def test_preparation_retry_controller_restores_saved_request_and_keeps_receipt(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace()
    attempt_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="briefing",
        revision=3,
        render_attempts=0,
        active_plan={"stale": "plan"},
        preparation={"attempt_id": str(attempt_id), "status": "failed"},
        last_error={"code": "analysis_unavailable"},
    )
    attempt = SimpleNamespace(
        id=attempt_id,
        session_id=session.id,
        creator_id=user.id,
        inputs={
            "user_message": "Use the beach clips in a calm sequence.",
            "previous_active_plan": {"direction": "guided_story"},
        },
    )
    db = AsyncMock()
    duplicate = MagicMock()
    duplicate.scalar_one_or_none.return_value = None
    db.execute.return_value = duplicate
    db.get.return_value = attempt
    append_event = AsyncMock()
    planning = AsyncMock(return_value=SimpleNamespace(status="awaiting_confirmation"))
    monkeypatch.setattr(creator_routes, "rollout_eligible", lambda _user_id: True)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_run_planning_turn", planning)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("test", 1),
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
    )
    retry_message = "Retry preparing my clips."
    result = await creator_routes.creator_session_turn_controller(
        request,
        str(item.id),
        TurnBody(
            session_id=session.id,
            expected_revision=3,
            message=retry_message,
            client_event_id="retry-preparation-1",
        ),
        user,
        db,
    )

    assert result.status == "awaiting_confirmation"
    assert session.status == "planning"
    assert session.preparation is None
    append_event.assert_awaited_once()
    event = append_event.await_args.kwargs
    assert event["event_type"] == "user_message"
    assert event["payload"] == {"message": retry_message}
    planning.assert_awaited_once()
    assert planning.await_args.kwargs["user_message"] == attempt.inputs["user_message"]
    assert (
        planning.await_args.kwargs["previous_active_plan"] == attempt.inputs["previous_active_plan"]
    )


def test_main_creator_preserves_photo_runs_and_ordered_sport_context(monkeypatch) -> None:
    strategy = _fallback_strategy(
        _manifest(monkeypatch),
        user_message=(
            "Amongst the videos, add groups of photos that transition in 0.1 seconds. "
            "Group football, basketball, and beach volleyball sequentially by sport and context."
        ),
    )

    assert strategy.mixed_media_timing is not None
    assert strategy.mixed_media_timing.image_grouping == "runs"
    assert strategy.mixed_media_timing.sequence_grouping == "sport_context"
    assert strategy.mixed_media_timing.sequence_group_order == [
        "football",
        "basketball",
        "beach_volleyball",
    ]


@pytest.mark.asyncio
async def test_planning_fails_closed_when_mixed_media_specialist_is_unavailable(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
        ],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=1,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
            mixed_media_timing=MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            ),
        ),
        summary="Fast photos and longer videos.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="failed")

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Photos should have a very fast transition, videos can be a bit longer",
    )

    assert result is response
    assert session.status == "failed"
    assert session.active_plan is None
    assert session.last_error["code"] == "mixed_media_timing_unavailable"
    assert append_event.await_args.kwargs["event_type"] == "assistant_error"
    assert append_event.await_args.kwargs["payload"] == {
        "message": (
            "Mixed photo and video timing is temporarily unavailable. "
            "No fallback edit was rendered."
        ),
        "code": "mixed_media_timing_unavailable",
    }


async def _failed_phone_planning_turn(  # noqa: ANN202
    monkeypatch,  # noqa: ANN001
    manifest,  # noqa: ANN001
    strategy,  # noqa: ANN001
    user_message: str = "Use all of my footage",
):
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=1,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="Use it.")
    append_event = AsyncMock()
    response = SimpleNamespace(status="failed")

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=user_message,
    )

    assert result is response
    assert session.active_plan is None
    return session, append_event.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pool_kind", "verified_features", "strategy", "limit"),
    [
        (
            "image",
            [],
            {"media_scope": "all"},
            "It can only use the videos attached to this project, not photos or videos "
            "from Visuals.",
        ),
        (
            "video",
            ["stillImages"],
            {"media_scope": "all"},
            "It can show photos from Visuals, but videos from Visuals can't be used, "
            "and a photo can't supply sound or cut timing.",
        ),
        (
            "image",
            ["visualVideos"],
            {"media_scope": "all"},
            "It can use videos from Visuals, but photos from Visuals can't be used.",
        ),
        (
            "image",
            ["stillImages", "visualVideos"],
            {"montage_audio": {"source_media_ids": ["asset-pool-1"]}},
            "It can use photos and videos from Visuals, but a photo can't supply sound "
            "or cut timing.",
        ),
    ],
)
async def test_planning_names_phone_media_rejection_honestly(
    monkeypatch, pool_kind, verified_features, strategy, limit
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        creator_capabilities.settings, "phone_render_verified_features", verified_features
    )
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-pool-1", "kind": pool_kind},
        ],
        phone_source_media_ids=["clip-1"],
        phone_rendering_allowed=True,
    )

    session, event = await _failed_phone_planning_turn(
        monkeypatch, manifest, CreativeStrategy(edit_format="montage", **strategy)
    )

    assert session.status == "failed"
    assert session.last_error == {
        "code": "phone_media_unavailable",
        "message": "phone rendering requires bound video sources",
    }
    assert event["event_type"] == "assistant_error"
    assert event["payload"] == {
        "message": f"This edit renders on your iPhone. {limit} No fallback edit was rendered.",
        "code": "phone_media_unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "code", "message"),
    [
        (
            {"edit_format": "talking_head"},
            "phone_format_unavailable",
            # KRI-132 phone-voiceover-gate follow-up: this copy is now
            # format-agnostic (talking_head itself still has no phone
            # compiler under any settings combination, so this case's outcome
            # is unchanged -- only the wording is).
            "This kind of video can't render on your iPhone right now. Choose a format "
            "that renders on this iPhone (Montage, or Talking to camera / Narrated "
            "where available). No fallback edit was rendered.",
        ),
        (
            {"edit_format": "montage", "audio_strategy": "voiceover"},
            "phone_voiceover_unavailable",
            "A voiceover can't render on your iPhone yet, and this project's videos "
            "render on this iPhone. Ask for this edit without a voiceover. "
            "No fallback edit was rendered.",
        ),
    ],
)
async def test_planning_names_phone_format_and_voiceover_rejections_honestly(
    monkeypatch, strategy, code, message
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(creator_capabilities.settings, "edit_format_talking_head_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
        phone_source_media_ids=["clip-1"],
        phone_rendering_allowed=True,
    )

    session, event = await _failed_phone_planning_turn(
        monkeypatch, manifest, CreativeStrategy(**strategy)
    )

    # Never the mixed photo/video timing copy: that outage has nothing to do with this.
    assert session.status == "failed"
    assert session.last_error["code"] == code
    assert event["event_type"] == "assistant_error"
    assert event["payload"] == {"message": message, "code": code}


@pytest.mark.asyncio
async def test_a_cadence_over_visuals_videos_the_phone_cannot_draw_is_refused_honestly(
    monkeypatch,
) -> None:
    # A Visuals-only phone project (stillImages verified, visualVideos not) whose
    # pool holds two videos from the web: the round-robin branch builds a cadence
    # from them, and the phone refuses it before the later planning path runs.
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(
        creator_capabilities.settings, "phone_render_verified_features", ["stillImages"]
    )
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "asset-photo", "kind": "image"},
            {"media_id": "asset-video-a", "kind": "video", "duration_s": 20},
            {"media_id": "asset-video-b", "kind": "video", "duration_s": 20},
        ],
        phone_source_media_ids=[],
        phone_rendering_allowed=True,
        phone_visuals_only=True,
    )

    session, event = await _failed_phone_planning_turn(
        monkeypatch,
        manifest,
        CreativeStrategy(edit_format="montage", target_duration_s=10),
        user_message="Alternate the two videos every 1 second for 10 seconds.",
    )

    assert session.status == "failed"
    assert session.last_error["code"] == "phone_media_unavailable"
    assert event["event_type"] == "assistant_error"
    assert event["payload"] == {
        "message": "This edit renders on your iPhone. It can show photos from Visuals, but "
        "videos from Visuals can't be used, and a photo can't supply sound or cut timing. "
        "No fallback edit was rendered.",
        "code": "phone_media_unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_voiceover", "allowed", "phone_ids", "message"),
    [
        (
            True,
            True,
            ["clip-1"],
            "A voiceover can't render on your iPhone yet, and this project's videos "
            "render on this iPhone. Remove the voiceover and I can design the edit.",
        ),
        (
            False,
            True,
            [],
            "I couldn't verify this project's iPhone footage, so it can't render on "
            "this iPhone yet. Reconnect its original footage, then try again.",
        ),
        (
            False,
            False,
            ["clip-1"],
            "Rendering on your iPhone is temporarily unavailable, and this project's "
            "videos render there. Your project is saved; try again later.",
        ),
    ],
)
async def test_planning_never_asks_a_blocked_phone_project_for_another_clip(
    monkeypatch, has_voiceover, allowed, phone_ids, message
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
        phone_source_media_ids=phone_ids,
        phone_rendering_allowed=allowed,
        has_voiceover=has_voiceover,
    )

    session, event = await _failed_phone_planning_turn(
        monkeypatch, manifest, CreativeStrategy(edit_format="montage")
    )

    assert session.status == "briefing"
    assert event["event_type"] == "assistant_question"
    assert event["payload"] == {"message": message}


@pytest.mark.asyncio
async def test_planning_repairs_missing_model_media_ids_from_authoritative_manifest(
    monkeypatch,
) -> None:
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "attached-clip-1", "kind": "video"}],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=1,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=[],
        ),
        summary="A focused cut from the attached footage.",
    )
    response = SimpleNamespace(status="awaiting_confirmation")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Make this energetic.",
    )

    assert result is response
    assert session.status == "awaiting_confirmation"
    assert session.active_plan["edit_plan"]["strategy"]["selected_media_ids"] == ["attached-clip-1"]


@pytest.mark.asyncio
async def test_planning_fails_closed_when_cadence_conflicts_with_voiceover(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 20},
            {"media_id": "match-b", "kind": "video", "duration_s": 20},
        ],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage", audio_strategy="voiceover", target_duration_s=10
        ),
        summary="Alternate the matches under the voiceover.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Alternate every 1 second for 10 seconds.",
    )

    assert result is response
    assert session.status == "failed"
    assert session.last_error["code"] == "montage_cadence_unavailable"
    assert append_event.await_args.kwargs["payload"]["code"] == ("montage_cadence_unavailable")


@pytest.mark.asyncio
async def test_required_cadence_question_fails_closed_at_question_budget(monkeypatch) -> None:
    session = SimpleNamespace(
        status="planning",
        question_count=2,
        question_budget=2,
        last_error=None,
    )
    append_event = AsyncMock()
    monkeypatch.setattr(creator_routes, "append_event", append_event)

    await creator_routes._record_required_cadence_question(
        AsyncMock(),
        session,
        payload={"message": "Choose two videos."},
    )

    assert session.status == "failed"
    assert session.last_error == {"code": "question_budget_exhausted"}
    assert append_event.await_args.kwargs["event_type"] == "assistant_error"


@pytest.mark.asyncio
async def test_alternation_prompt_asks_for_balanced_twelve_second_capacity(
    monkeypatch,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 6.633},
            {"media_id": "match-b", "kind": "video", "duration_s": 26.433},
        ],
    )
    message = "Show one second from one, switch to the other one, and back and forth."
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=0,
                role="user",
                event_type="user_message",
                payload={"message": message},
            )
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage",
            render_program="guided",
            target_duration_s=24,
        ),
        summary="Alternate the two matches.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=message,
    )

    assert result is response
    assert session.status == "briefing"
    payload = append_event.await_args.kwargs["payload"]
    assert payload["message"] == (
        "I can make a balanced 12-second edit without repeating footage. "
        "I recommend using the strongest moments. What would you prefer?"
    )
    assert payload["options"][0] == "Use the best 12 seconds"
    assert payload["cadence_context"]["recommended_duration_s"] == 12


@pytest.mark.asyncio
async def test_unrecognized_source_selection_is_reasked_before_agent_fallback(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 10},
            {"media_id": "match-b", "kind": "video", "duration_s": 10},
            {"media_id": "match-c", "kind": "video", "duration_s": 10},
        ],
    )
    context = {
        "kind": "source_selection",
        "cut_duration_s": 1,
        "reuse_policy": "no_repeat",
        "selections": {"Alternate match-a and match-b": ["match-a", "match-b"]},
    }
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="briefing",
        events=[
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload={"cadence_context": context},
            )
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    to_thread = AsyncMock()
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Whatever you think",
    )

    assert result is response
    to_thread.assert_not_awaited()
    assert append_event.await_args.kwargs["payload"]["reason_code"] == ("cadence_source_selection")
    assert session.status == "briefing"


@pytest.mark.asyncio
async def test_pending_source_selection_honors_latest_cadence_cancellation(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 10},
            {"media_id": "match-b", "kind": "video", "duration_s": 10},
            {"media_id": "match-c", "kind": "video", "duration_s": 10},
        ],
    )
    context = {
        "kind": "source_selection",
        "cut_duration_s": 1,
        "reuse_policy": "no_repeat",
        "selections": {"Alternate match-a and match-b": ["match-a", "match-b"]},
    }
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload={"cadence_context": context},
            )
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    to_thread = AsyncMock(
        return_value=SimpleNamespace(
            action=AskUser(
                kind="ask_user",
                question="What story should this become instead?",
                reason_code="story_direction",
            )
        )
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Don't alternate; make it a guided story",
    )

    assert result is response
    to_thread.assert_awaited_once()
    assert append_event.await_args.kwargs["payload"]["reason_code"] == "story_direction"


@pytest.mark.asyncio
async def test_selected_pair_with_pending_duration_fails_closed(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 10},
            {"media_id": "match-b", "kind": "video", "duration_s": 10},
            {"media_id": "match-c", "kind": "video", "duration_s": None},
        ],
    )
    context = {
        "kind": "source_selection",
        "cut_duration_s": 1,
        "reuse_policy": "no_repeat",
        "selections": {"Alternate match-a and match-c": ["match-a", "match-c"]},
    }
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=0,
                role="user",
                event_type="user_message",
                payload={"message": "Alternate every 1 second"},
            ),
            SimpleNamespace(
                sequence=1,
                role="assistant",
                event_type="assistant_question",
                payload={"cadence_context": context},
            ),
        ],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(edit_format="montage", render_program="guided"),
        summary="Alternate the selected matches.",
    )
    append_event = AsyncMock()
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Alternate match-a and match-c",
    )

    assert result is response
    assert append_event.await_args.kwargs["payload"]["reason_code"] == (
        "cadence_duration_unavailable"
    )
    assert append_event.await_args.kwargs["payload"]["cadence_context"]["source_media_ids"] == [
        "match-a",
        "match-c",
    ]


@pytest.mark.asyncio
async def test_model_target_duration_survives_non_regex_cadence_wording(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 20},
            {"media_id": "match-b", "kind": "video", "duration_s": 20},
        ],
    )
    message = "Alternate each match every one second; keep the finished piece ten seconds long."
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            edit_format="montage", render_program="guided", target_duration_s=10
        ),
        summary="Alternate the matches for ten seconds.",
    )
    compile_plan = MagicMock(
        return_value={
            "summary": "Alternate the matches for ten seconds.",
            "plan_hash": "a" * 64,
            "target_duration_s": 10,
            "montage_cadence": {},
        }
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "compile_active_plan", compile_plan)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="awaiting_confirmation")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=message,
    )

    assert result is response
    assert compile_plan.call_args.kwargs["strategy"].target_duration_s == 10
    assert compile_plan.call_args.kwargs["strategy"].montage_cadence is not None


@pytest.mark.asyncio
async def test_unrelated_revision_keeps_accepted_sources_with_three_videos(monkeypatch) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "match-a", "kind": "video", "duration_s": 20},
            {"media_id": "match-b", "kind": "video", "duration_s": 20},
            {"media_id": "match-c", "kind": "video", "duration_s": 20},
        ],
    )
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=2,
        status="planning",
        events=[
            SimpleNamespace(
                sequence=1,
                role="user",
                event_type="user_message",
                payload={"message": "Alternate every 1 second."},
            ),
            SimpleNamespace(
                sequence=2,
                role="assistant",
                event_type="assistant_strategy",
                payload={
                    "target_duration_s": 12,
                    "montage_cadence": cadence.model_dump(mode="json"),
                },
            ),
        ],
        agent_call_count=0,
        agent_call_budget=3,
        question_count=1,
        question_budget=3,
        active_plan=None,
        last_error=None,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(edit_format="montage", render_program="guided"),
        summary="Keep the cadence and refine the captions.",
    )
    compile_plan = MagicMock(
        return_value={
            "summary": "Keep the cadence and refine the captions.",
            "plan_hash": "a" * 64,
            "target_duration_s": 12,
            "montage_cadence": cadence.model_dump(mode="json"),
        }
    )
    append_event = AsyncMock()
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(return_value=SimpleNamespace(action=action)),
    )
    monkeypatch.setattr(creator_routes, "compile_active_plan", compile_plan)
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    response = SimpleNamespace(status="awaiting_confirmation")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=2,
        user_message="Make the captions clean.",
    )

    assert result is response
    planned = compile_plan.call_args.kwargs["strategy"]
    assert planned.target_duration_s == 12
    assert planned.montage_cadence == cadence
    assert append_event.await_args.kwargs["event_type"] == "assistant_strategy"
    payload = append_event.await_args.kwargs["payload"]
    assert "confirm it before I change the video" in payload["message"]
    assert "proposal_summary" in payload


def test_confirmed_creator_request_preserves_instruction_across_clarification() -> None:
    initial = "Photos should have a very fast transition, videos can be a bit longer"
    events = [
        SimpleNamespace(sequence=0, role="user", payload={"message": initial}),
        SimpleNamespace(sequence=1, role="assistant", payload={"message": "Use music?"}),
        SimpleNamespace(sequence=2, role="user", payload={"message": "Yes"}),
    ]

    assert _confirmed_creator_request(events, "Yes") == f"{initial}\nYes"


def test_capacity_recommendation_preserves_cadence_context(monkeypatch) -> None:
    manifest = _manifest(monkeypatch).model_copy(
        update={
            "media": [
                {"media_id": "match-a", "kind": "video", "duration_s": 6.633},
                {"media_id": "match-b", "kind": "video", "duration_s": 26.433},
            ]
        }
    )
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "capacity",
                    "cadence": cadence.model_dump(mode="json"),
                    "requested_duration_s": 24,
                    "recommended_duration_s": 12,
                    "recommendation": "Use the best 12 seconds",
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Use the best 12 seconds"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="original request\nUse the best 12 seconds",
        user_message="Use the best 12 seconds",
    )

    assert resolved == cadence
    assert target_s == 12


def test_capacity_answer_can_choose_a_shorter_custom_length(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "capacity",
                    "cadence": cadence.model_dump(mode="json"),
                    "requested_duration_s": 24,
                    "recommended_duration_s": 12,
                    "recommendation": "Use the best 12 seconds",
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Make it 10 seconds"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="original request\nMake it 10 seconds",
        user_message="Make it 10 seconds",
    )

    assert resolved == cadence
    assert target_s == 10


def test_insufficient_capacity_recommendation_explicitly_enables_reuse(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    recommendation = "Allow the strongest moments to repeat"
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "capacity",
                    "cadence": cadence.model_dump(mode="json"),
                    "requested_duration_s": 24,
                    "recommended_duration_s": 2,
                    "recommendation": recommendation,
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": recommendation},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request=f"original request\n{recommendation}",
        user_message=recommendation,
    )

    assert resolved == cadence.model_copy(update={"reuse_policy": "allow_repeat"})
    assert target_s == 24


def test_source_selection_accepts_free_form_video_labels(monkeypatch) -> None:
    base_manifest = _manifest(monkeypatch)
    manifest = type(base_manifest).model_validate(
        {
            **base_manifest.model_dump(mode="json"),
            "media": [
                {"media_id": "match-a", "kind": "video", "duration_s": 10, "label": "Final"},
                {
                    "media_id": "match-b",
                    "kind": "video",
                    "duration_s": 10,
                    "label": "Semi-final",
                },
                {
                    "media_id": "match-c",
                    "kind": "video",
                    "duration_s": 10,
                    "label": "Quarter-final",
                },
            ],
        }
    )
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "source_selection",
                    "cut_duration_s": 1,
                    "reuse_policy": "no_repeat",
                    "selections": {"Alternate Final and Semi-final": ["match-a", "match-b"]},
                }
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Use Semi-final and Quarter-final"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every second\nUse Semi-final and Quarter-final",
        user_message="Use Semi-final and Quarter-final",
    )

    assert resolved == MontageCadenceConstraint(
        source_media_ids=["match-b", "match-c"], cut_duration_s=1
    )
    assert target_s is None


def test_reuse_policy_does_not_treat_a_prohibition_as_permission() -> None:
    assert recognize_cadence_reuse_policy("Do not repeat any footage") == "no_repeat"
    assert recognize_cadence_reuse_policy("Can you not repeat footage?") == "no_repeat"
    assert recognize_cadence_reuse_policy("Repeat the best moments if needed") == "allow_repeat"


def test_cadence_recognizers_separate_cut_timing_total_length_and_cancellation() -> None:
    request = "Make a 3-second edit and alternate every 1 second"

    assert recognize_round_robin_cadence(request) == 1
    assert recognize_total_duration_s(request) == 3
    assert recognize_total_duration_s("I want a 10-second video") == 10
    assert _balanced_duration_s(limit_s=24, cycle_s=1.4) == 23.8
    assert _next_balanced_duration_s(minimum_s=3, limit_s=12, cycle_s=2) == 4
    assert rejects_round_robin_cadence("Don't alternate; make it a guided story") is True


def test_duration_revision_does_not_resurrect_cadence_after_cancellation() -> None:
    events = [
        SimpleNamespace(
            sequence=0,
            role="user",
            event_type="user_message",
            payload={"message": "Alternate every 1 second."},
        ),
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_strategy",
            payload={
                "target_duration_s": 12,
                "montage_cadence": {
                    "mode": "round_robin",
                    "source_media_ids": ["match-a", "match-b"],
                    "cut_duration_s": 1,
                    "reuse_policy": "no_repeat",
                },
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Don't alternate; make it a guided story."},
        ),
        SimpleNamespace(
            sequence=3,
            role="user",
            event_type="user_message",
            payload={"message": "Make it 10 seconds."},
        ),
    ]
    manifest = SimpleNamespace(
        media=[
            CreatorMediaRef(media_id="match-a", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-b", kind="video", duration_s=10),
        ]
    )

    cadence, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request=(
            "Alternate every 1 second.\n"
            "Don't alternate; make it a guided story.\n"
            "Make it 10 seconds."
        ),
        user_message="Make it 10 seconds.",
    )

    assert cadence is None
    assert target_s is None


def test_unrecognized_source_selection_does_not_resolve() -> None:
    manifest = SimpleNamespace(
        media=[
            CreatorMediaRef(media_id="match-a", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-b", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-c", kind="video", duration_s=10),
        ]
    )
    context = {
        "kind": "source_selection",
        "selections": {"Alternate match-a and match-b": ["match-a", "match-b"]},
    }

    assert _selected_cadence_sources(context, manifest, "Whatever you think") is None


def test_latest_planned_cadence_survives_unrelated_revision(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_strategy",
            payload={
                "target_duration_s": 12,
                "montage_cadence": cadence.model_dump(mode="json"),
            },
        ),
        SimpleNamespace(
            sequence=2,
            role="user",
            event_type="user_message",
            payload={"message": "Make the captions clean"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every 1 second\nMake the captions clean",
        user_message="Make the captions clean",
    )

    assert resolved == cadence
    assert target_s == 12


def test_duration_retry_preserves_selected_sources_with_three_videos() -> None:
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-c"], cut_duration_s=1)
    manifest = SimpleNamespace(
        media=[
            CreatorMediaRef(media_id="match-a", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-b", kind="video", duration_s=10),
            CreatorMediaRef(media_id="match-c", kind="video", duration_s=10),
        ]
    )
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={
                "cadence_context": {
                    "kind": "duration_unavailable",
                    "cadence": cadence.model_dump(mode="json"),
                    "cut_duration_s": 1,
                    "source_media_ids": ["match-a", "match-c"],
                }
            },
        )
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every 1 second\nUse match-a and match-c\nTry again",
        user_message="Try again",
    )

    assert resolved == cadence
    assert target_s is None


def test_latest_turn_can_cancel_planned_cadence(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    cadence = MontageCadenceConstraint(source_media_ids=["match-a", "match-b"], cut_duration_s=1)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_strategy",
            payload={
                "target_duration_s": 12,
                "montage_cadence": cadence.model_dump(mode="json"),
            },
        )
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Alternate every 1 second\nDon't alternate anymore",
        user_message="Don't alternate anymore; make it a guided story",
    )

    assert resolved is None
    assert target_s is None


def test_latest_turn_overrides_prior_cadence_timing_and_reuse(monkeypatch) -> None:
    manifest = _manifest(monkeypatch).model_copy(
        update={
            "media": [
                CreatorMediaRef(media_id="match-a", kind="video", duration_s=20),
                CreatorMediaRef(media_id="match-b", kind="video", duration_s=20),
            ]
        }
    )

    resolved, target_s = _resolved_cadence_for_turn(
        events=[],
        manifest=manifest,
        creator_request=(
            "Alternate every 1 second without repeating.\n"
            "Actually, alternate every 2 seconds and allow the moments to repeat."
        ),
        user_message="Actually, alternate every 2 seconds and allow the moments to repeat.",
    )

    assert resolved is not None
    assert resolved.cut_duration_s == 2
    assert resolved.reuse_policy == "allow_repeat"
    assert target_s is None


def test_stale_cadence_question_is_not_reused(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    events = [
        SimpleNamespace(
            sequence=1,
            role="assistant",
            event_type="assistant_question",
            payload={"cadence_context": {"kind": "capacity"}},
        ),
        SimpleNamespace(
            sequence=2,
            role="assistant",
            event_type="assistant_strategy",
            payload={"message": "A newer plan"},
        ),
        SimpleNamespace(
            sequence=3,
            role="user",
            event_type="user_message",
            payload={"message": "Use the best 12 seconds"},
        ),
    ]

    resolved, target_s = _resolved_cadence_for_turn(
        events=events,
        manifest=manifest,
        creator_request="Use the best 12 seconds",
        user_message="Use the best 12 seconds",
    )

    assert resolved is None
    assert target_s is None


def test_main_creator_prompt_contains_no_storage_capabilities(monkeypatch) -> None:
    manifest = _manifest(monkeypatch)
    prompt = MainCreatorAgent(SimpleNamespace()).render_prompt(
        MainCreatorInput(
            user_message="Make it quick",
            capability_manifest=manifest,
        )
    )
    assert "clip-1" in prompt
    assert "gs://" not in prompt
    assert "s3://" not in prompt
    assert "FFmpeg commands" in prompt


def test_rollout_flags_fail_closed_when_dependencies_are_missing() -> None:
    base = {"storage_bucket": "test", "database_url": "postgresql://test/test"}
    with pytest.raises(ValidationError, match="execution requires"):
        Settings(**base, main_creator_agent_execution_enabled=True)
    with pytest.raises(ValidationError, match="auto iteration requires review"):
        Settings(
            **base,
            main_creator_agent_enabled=True,
            main_creator_agent_auto_iteration_enabled=True,
        )
    with pytest.raises(ValidationError, match="auto iteration requires quality review"):
        Settings(
            **base,
            main_creator_agent_enabled=True,
            main_creator_agent_execution_enabled=True,
            main_creator_agent_review_enabled=True,
            main_creator_agent_auto_iteration_enabled=True,
        )
    with pytest.raises(ValidationError, match="auto iteration requires execution"):
        Settings(
            **base,
            main_creator_agent_enabled=True,
            main_creator_agent_review_enabled=True,
            main_creator_agent_quality_review_enabled=True,
            main_creator_agent_auto_iteration_enabled=True,
        )


def test_replan_clears_every_prior_render_identity() -> None:
    session = SimpleNamespace(
        active_plan={"plan_hash": "old"},
        target_job_id=uuid.uuid4(),
        target_variant_id="variant-old",
        target_generation_id="generation-old",
        last_review={"decision": "approve"},
        last_good={"job_id": "kept-for-rollback"},
    )

    _reset_render_target(session)

    assert session.active_plan is None
    assert session.target_job_id is None
    assert session.target_variant_id is None
    assert session.target_generation_id is None
    assert session.last_review is None
    assert session.last_good == {"job_id": "kept-for-rollback"}


@pytest.mark.parametrize("body_type", [StartBody, TurnBody])
def test_creator_messages_reject_whitespace_at_the_api_boundary(body_type) -> None:
    values = {"message": "   ", "client_event_id": "event-1"}
    if body_type is TurnBody:
        values.update(session_id="11111111-1111-1111-1111-111111111111", expected_revision=0)
    with pytest.raises(ValidationError, match="must not be blank"):
        body_type.model_validate(values)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/session",
            {"message": "   ", "client_event_id": "event-1"},
        ),
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/turn",
            {
                "session_id": "22222222-2222-2222-2222-222222222222",
                "expected_revision": 0,
                "message": "   ",
                "client_event_id": "event-1",
            },
        ),
    ],
)
def test_creator_routes_return_422_for_blank_messages(
    client: TestClient, path: str, payload: dict
) -> None:
    assert client.post(path, json=payload).status_code == 422


def test_creator_route_rollout_gate_is_hidden_as_404(client: TestClient) -> None:
    response = client.post(
        "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/session",
        json={"message": "Make it fast", "client_event_id": "event-1"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Creator agent unavailable"


@pytest.mark.parametrize("execution", [False, True])
def test_chat_controller_bypasses_legacy_creator_rollout_gates(
    monkeypatch: pytest.MonkeyPatch, execution: bool
) -> None:
    monkeypatch.setattr(creator_routes, "rollout_eligible", lambda _user_id: False)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", False)

    _require_feature(uuid.uuid4(), execution=execution, allow_chat=True)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/session",
            {"message": "Make it fast", "client_event_id": "rate-start"},
        ),
        (
            "/plan-items/11111111-1111-1111-1111-111111111111/creator-agent/turn",
            {
                "session_id": "22222222-2222-2222-2222-222222222222",
                "expected_revision": 0,
                "message": "Make it fast",
                "client_event_id": "rate-turn",
            },
        ),
    ],
)
def test_creator_mutations_are_rate_limited_before_model_call(
    client: TestClient, monkeypatch, path: str, payload: dict
) -> None:
    limiter._storage.reset()
    model_call = MagicMock()
    monkeypatch.setattr(MainCreatorAgent, "run", model_call)
    try:
        responses = [client.post(path, json=payload) for _ in range(13)]
    finally:
        limiter._storage.reset()

    assert [response.status_code for response in responses[:12]] == [404] * 12
    assert responses[-1].status_code == 429
    model_call.assert_not_called()


@pytest.mark.asyncio
async def test_start_replays_terminal_session_for_persisted_start_event(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="completed",
        revision=2,
        events=[
            SimpleNamespace(
                client_event_id="terminal-event",
                sequence=0,
                payload={"message": "Make it personal"},
            )
        ],
    )
    db = AsyncMock()
    prior_result = MagicMock()
    prior_result.scalar_one_or_none.return_value = session
    db.execute.return_value = prior_result
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("test", 1),
            "scheme": "http",
            "server": ("test", 80),
            "query_string": b"",
        }
    )

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    load_session = AsyncMock(return_value=session)
    monkeypatch.setattr(creator_routes, "_load_session", load_session)
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)
    planning = AsyncMock()
    monkeypatch.setattr(creator_routes, "_run_planning_turn", planning)
    response = SimpleNamespace(status="completed")
    response_for = AsyncMock(return_value=response)
    monkeypatch.setattr(creator_routes, "_response", response_for)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    result = await creator_routes.start_creator_session(
        request,
        str(item.id),
        StartBody(message="Make it personal", client_event_id="terminal-event"),
        user,
        db,
    )

    assert result is response
    latest_session.assert_not_awaited()
    planning.assert_not_awaited()
    load_session.assert_awaited_once_with(db, session.id, user.id, item.id, for_update=True)
    response_for.assert_awaited_once_with(db, session)


@pytest.mark.asyncio
async def test_start_rejects_when_raw_generate_already_minted_active_job(monkeypatch) -> None:
    """The inverse start-vs-generate race is fenced by the same item lock."""
    user = SimpleNamespace(id=uuid.uuid4())
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    job = SimpleNamespace(id=job_id, status="queued")
    db = AsyncMock()
    no_prior = MagicMock()
    no_prior.scalar_one_or_none.return_value = None
    db.execute.return_value = no_prior
    db.get.return_value = job

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    owned_context = AsyncMock(return_value=(item, plan, SimpleNamespace()))
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)

    with pytest.raises(HTTPException) as exc:
        await creator_routes.start_creator_session(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "client": ("test", 1),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            ),
            str(item.id),
            StartBody(message="Alternate every second", client_event_id="start-after-generate"),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert "current render" in str(exc.value.detail)
    owned_context.assert_awaited_once_with(db, str(item.id), user.id, for_update=True)
    db.get.assert_awaited_once_with(
        Job,
        job_id,
        with_for_update=True,
        populate_existing=True,
    )
    latest_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_rejects_when_raw_auto_design_reservation_won_race(monkeypatch) -> None:
    """An analyzing auto-design owns the item before its render Job exists."""
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(
        id=uuid.uuid4(),
        current_job_id=None,
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="raw-auto-attempt",
            status="analyzing",
            approval_mode="auto",
            brief=ProposalBrief(),
        ).model_dump(mode="json"),
    )
    plan = SimpleNamespace(ownership_epoch=4)
    db = AsyncMock()
    no_prior = MagicMock()
    no_prior.scalar_one_or_none.return_value = None
    db.execute.return_value = no_prior

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)

    with pytest.raises(HTTPException) as exc:
        await creator_routes.start_creator_session(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "client": ("test", 1),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            ),
            str(item.id),
            StartBody(message="Alternate every second", client_event_id="start-during-auto"),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert "already designing" in str(exc.value.detail)
    latest_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_rejects_fresh_auto_design_even_with_old_terminal_job(monkeypatch) -> None:
    """A stale completed Job cannot satisfy a newer proposal attempt."""
    user = SimpleNamespace(id=uuid.uuid4())
    old_job_id = uuid.uuid4()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        current_job_id=old_job_id,
        edit_proposal=EditProposal(
            proposal_version=1,
            generation_attempt_id="fresh-raw-auto-attempt",
            status="analyzing",
            approval_mode="auto",
            brief=ProposalBrief(),
        ).model_dump(mode="json"),
    )
    plan = SimpleNamespace(ownership_epoch=4)
    old_job = SimpleNamespace(
        id=old_job_id,
        status="variants_ready",
        assembly_plan={"guided_edit": {"generation_attempt_id": "older-attempt"}},
    )
    db = AsyncMock()
    no_prior = MagicMock()
    no_prior.scalar_one_or_none.return_value = None
    db.execute.return_value = no_prior
    db.get.return_value = old_job

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    latest_session = AsyncMock()
    monkeypatch.setattr(creator_routes, "_latest_session", latest_session)

    with pytest.raises(HTTPException) as exc:
        await creator_routes.start_creator_session(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/",
                    "headers": [],
                    "client": ("test", 1),
                    "scheme": "http",
                    "server": ("test", 80),
                    "query_string": b"",
                }
            ),
            str(item.id),
            StartBody(message="Use a new direction", client_event_id="fresh-after-old-job"),
            user,
            db,
        )

    assert exc.value.status_code == 409
    assert "already designing" in str(exc.value.detail)
    latest_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("carried", "has_history"),
    [
        (None, False),
        ({"creator_request": "Narrated story. Title: 1882."}, False),
        ({"creator_request": "Narrated story. Title: 1882."}, True),
    ],
)
async def test_start_locks_an_existing_session_before_appending(
    monkeypatch, carried, has_history
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=4)
    history = [_user_event(0, "Earlier message")] if has_history else []
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="briefing",
        revision=0,
        active_plan=None,
        events=list(history),
    )
    db = AsyncMock()
    duplicate_result = MagicMock()
    duplicate_result.scalar_one_or_none.return_value = None
    db.execute.return_value = duplicate_result

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_latest_session", AsyncMock(return_value=session))
    load_session = AsyncMock(return_value=session)
    monkeypatch.setattr(creator_routes, "_load_session", load_session)
    append_event = AsyncMock(side_effect=lambda *_args, **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    planning = AsyncMock(return_value=SimpleNamespace(id="response"))
    monkeypatch.setattr(creator_routes, "_run_planning_turn", planning)

    await creator_routes.start_creator_session_controller(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [],
                "client": ("test", 1),
                "scheme": "http",
                "server": ("test", 80),
                "query_string": b"",
            }
        ),
        str(item.id),
        StartBody(message="Make it personal", client_event_id="event-1"),
        user,
        db,
        allow_chat=True,
        carried_active_plan=carried,
    )

    load_session.assert_awaited_once_with(db, session.id, user.id, item.id, for_update=True)
    planning.assert_awaited_once()
    assert planning.await_args.kwargs["allow_chat"] is True
    # A session without its own plan continues the failed session's plan.
    assert planning.await_args.kwargs["previous_active_plan"] is carried
    appended = [call.kwargs for call in append_event.await_args_list]
    assert appended[-1]["event_type"] == "user_message"
    if carried is not None and not has_history:
        # A fresh session opens with the failed session's brief, durably, and
        # this request's planning turn already sees it.
        assert [event["event_type"] for event in appended] == [
            CARRIED_BRIEF_EVENT,
            "user_message",
        ]
        assert appended[0]["role"] == "system"
        assert appended[0]["payload"] == {"creator_request": "Narrated story. Title: 1882."}
        assert [event.event_type for event in session.events] == [CARRIED_BRIEF_EVENT]
    else:
        assert len(appended) == 1
        assert len(session.events) == len(history)


@pytest.mark.asyncio
async def test_turn_controller_forwards_allow_chat_to_planning(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="briefing",
        revision=0,
        render_attempts=0,
        active_plan=None,
    )
    db = AsyncMock()
    duplicate_result = MagicMock()
    duplicate_result.scalar_one_or_none.return_value = None
    db.execute.return_value = duplicate_result

    monkeypatch.setattr(creator_routes, "rollout_eligible", lambda _user_id: False)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    planning = AsyncMock(return_value=SimpleNamespace(id="response"))
    monkeypatch.setattr(creator_routes, "_run_planning_turn", planning)

    await creator_routes.creator_session_turn_controller(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [],
                "client": ("test", 1),
                "scheme": "http",
                "server": ("test", 80),
                "query_string": b"",
            }
        ),
        str(item.id),
        TurnBody(
            session_id=session.id,
            expected_revision=0,
            message="Make it personal",
            client_event_id="turn-1",
        ),
        user,
        db,
        allow_chat=True,
    )

    planning.assert_awaited_once()
    assert planning.await_args.kwargs["allow_chat"] is True


@pytest.mark.asyncio
async def test_confirm_controller_forwards_chat_capability_to_context(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=None)
    plan = SimpleNamespace(ownership_epoch=1)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash="s" * 64,
        active_plan={"version": 1, "plan_hash": "a" * 64},
        render_attempts=0,
        max_render_attempts=2,
    )
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    resolve_context = AsyncMock(return_value=(SimpleNamespace(manifest_hash="f" * 64), []))

    monkeypatch.setattr(creator_routes, "rollout_eligible", lambda _user_id: False)
    monkeypatch.setattr(
        creator_routes, "_owned_context", AsyncMock(return_value=(item, plan, SimpleNamespace()))
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(creator_routes, "_confirmed_edit_plan", lambda _active: object())
    monkeypatch.setattr(creator_routes, "resolve_item_creator_context", resolve_context)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", False)

    with pytest.raises(HTTPException) as caught:
        await creator_routes.confirm_creator_plan_controller(
            str(item.id),
            ConfirmBody(
                session_id=session.id,
                expected_revision=0,
                plan_version=1,
                plan_hash="a" * 64,
                client_event_id="confirm-chat-1",
            ),
            user,
            db,
            allow_chat=True,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Footage or capabilities changed; review the plan again"
    resolve_context.assert_awaited_once()
    assert resolve_context.await_args.kwargs["guided_capability_enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("live_change", [None, "media", "capabilities"])
async def test_confirm_retry_restores_only_hash_verified_original_current_edit(
    monkeypatch, live_change: str | None
) -> None:
    """A terminal Creator retry can reopen its own failed edit snapshot only."""

    from app.tasks import content_plan_build

    user = SimpleNamespace(id=uuid.uuid4())
    item_id, failed_job_id, replacement_job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    original = _manifest(monkeypatch)
    live_without_hash = original.model_copy(
        update={
            "current_edit": CreatorEditSnapshot(
                status="failed", variant_id="original_text", edit_hash="f" * 64
            )
        }
    )
    if live_change == "media":
        changed_media = [
            original.media[0].model_copy(update={"media_id": "replacement-clip"}),
            *original.media[1:],
        ]
        live_without_hash = live_without_hash.model_copy(update={"media": changed_media})
    elif live_change == "capabilities":
        changed_capabilities = dict(original.capabilities)
        assert changed_capabilities
        changed_capabilities.pop(next(iter(changed_capabilities)))
        live_without_hash = live_without_hash.model_copy(
            update={"capabilities": changed_capabilities}
        )
    live = live_without_hash.model_copy(
        update={"manifest_hash": canonical_manifest_hash(live_without_hash)}
    )
    assert live.manifest_hash != original.manifest_hash
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="native",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(original, strategy)
    item = SimpleNamespace(id=item_id, current_job_id=failed_job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=3,
        ownership_epoch=4,
        manifest_hash=original.manifest_hash,
        active_plan={
            "version": 1,
            "plan_hash": "a" * 64,
            "creator_request": "Make a clean montage",
            "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
            "original_current_edit_present": True,
            "original_current_edit": None,
        },
        render_attempts=1,
        max_render_attempts=2,
        iteration_count=1,
        target_job_id=failed_job_id,
        last_error=None,
    )
    failed_job = SimpleNamespace(
        id=failed_job_id,
        status="processing_failed",
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=4,
        all_candidates={
            "creator_strategy": edit_plan.strategy.model_dump(mode="json", exclude_none=True)
        },
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.return_value = receipt_result
    receipts: dict[str, CreatorAgentExecution] = {}

    def add(row) -> None:
        if isinstance(row, CreatorAgentExecution):
            row.id = uuid.uuid4()
            receipts["receipt"] = row

    async def get(model, _identifier, **_kwargs):
        if model is Job:
            return failed_job
        if model is CreatorAgentExecution:
            return receipts.get("receipt")
        return None

    db.add = MagicMock(side_effect=add)
    db.get.side_effect = get
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(live, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", MagicMock())
    monkeypatch.setattr(
        creator_routes, "_previous_creator_clip_order", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        creator_routes, "_lock_confirmed_render_graph", AsyncMock(return_value=session)
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="rendering")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(
            return_value=SimpleNamespace(outcome="dispatched", job_id=str(replacement_job_id))
        ),
    )
    monkeypatch.setattr(content_plan_build, "dispatch_item_render_for", MagicMock())
    assert creator_routes._retry_target_matches_confirmed_plan(
        item=item,
        plan=plan,
        session=session,
        job=failed_job,
        target_job_id=failed_job_id,
        strategy=edit_plan.strategy,
    )
    confirmation = creator_routes.confirm_creator_plan_controller(
        str(item_id),
        ConfirmBody(
            session_id=session.id,
            expected_revision=3,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="retry-after-failed-current-edit",
        ),
        user,
        db,
        allow_chat=True,
        retry_target_job_id=failed_job_id,
    )

    if live_change is not None:
        with pytest.raises(HTTPException) as rejected:
            await confirmation
        assert rejected.value.status_code == 409
        assert "Footage or capabilities changed" in rejected.value.detail
        assert session.target_job_id == failed_job_id
        assert session.render_attempts == 1
        assert not receipts
        creator_routes.asyncio.to_thread.assert_not_awaited()
        return

    returned = await confirmation
    assert returned is response
    assert session.target_job_id == replacement_job_id
    assert session.render_attempts == 2
    assert receipts["receipt"].expected_manifest_hash == original.manifest_hash
    assert creator_routes.asyncio.to_thread.await_count == 1


@pytest.mark.asyncio
async def test_legacy_retry_recovers_only_hash_verified_agent_run_snapshot(monkeypatch) -> None:
    original_base = _manifest(monkeypatch)
    original_without_hash = original_base.model_copy(
        update={
            "current_edit": CreatorEditSnapshot(
                status="failed", variant_id="first_failed", edit_hash="a" * 64
            )
        }
    )
    original = original_without_hash.model_copy(
        update={"manifest_hash": canonical_manifest_hash(original_without_hash)}
    )
    live_without_hash = original.model_copy(
        update={
            "current_edit": CreatorEditSnapshot(
                status="failed", variant_id="second_failed", edit_hash="b" * 64
            )
        }
    )
    live = live_without_hash.model_copy(
        update={"manifest_hash": canonical_manifest_hash(live_without_hash)}
    )
    session = SimpleNamespace(
        id=uuid.uuid4(),
        active_plan={},
        manifest_hash=original.manifest_hash,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        {"capability_manifest": original.model_dump(mode="json")}
    ]
    db = AsyncMock()
    db.execute.return_value = result

    recovered = await creator_routes._original_current_edit_for_retry(
        db, session=session, manifest=live
    )

    assert recovered == {
        "revision": 0,
        "status": "failed",
        "variant_id": "first_failed",
        "edit_hash": "a" * 64,
    }


@pytest.mark.parametrize(
    "mutation",
    ["wrong_owner", "wrong_epoch", "running", "inflight_variant", "bad_generation", "bad_attempt"],
)
def test_retry_target_rejects_nonexact_failed_render(mutation: str) -> None:
    user_id, item_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    strategy = CreativeStrategy(
        edit_format="montage", render_program="guided" if mutation == "bad_attempt" else "native"
    )
    session = SimpleNamespace(
        creator_id=user_id,
        target_job_id=job_id,
        ownership_epoch=2,
        target_variant_id="original_text" if mutation == "bad_generation" else None,
        target_generation_id="generation-a" if mutation == "bad_generation" else None,
        active_plan={"guided_generation_attempt_id": "expected-attempt"},
    )
    job = SimpleNamespace(
        id=job_id,
        status="processing_failed" if mutation != "running" else "rendering",
        user_id=uuid.uuid4() if mutation == "wrong_owner" else user_id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=3 if mutation == "wrong_epoch" else 2,
        all_candidates={"creator_strategy": strategy.model_dump(mode="json", exclude_none=True)},
        assembly_plan={
            "guided_edit": {"generation_attempt_id": "different-attempt"},
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_generation_id": "generation-b"
                    if mutation == "bad_generation"
                    else "generation-a",
                    "render_status": "rendering" if mutation == "inflight_variant" else "failed",
                }
            ],
        },
    )

    assert not creator_routes._retry_target_matches_confirmed_plan(
        item=SimpleNamespace(id=item_id, current_job_id=job_id),
        plan=SimpleNamespace(ownership_epoch=2),
        session=session,
        job=job,
        target_job_id=job_id,
        strategy=strategy,
    )


@pytest.mark.asyncio
async def test_confirm_completion_locks_plan_job_before_session(monkeypatch) -> None:
    """Finalization must use the worker's Plan -> Item -> Job -> Session order."""

    user_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(id=uuid.uuid4())
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        content_plan_item_id=item.id,
    )
    session = SimpleNamespace(id=uuid.uuid4())
    calls: list[str] = []

    async def owned_context(*_args, **kwargs):
        assert kwargs == {"for_update": True}
        calls.append("plan_item")
        return item, plan, SimpleNamespace()

    async def get(model, object_id, **kwargs):
        assert model is Job
        assert object_id == job.id
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        calls.append("job")
        return job

    async def load_session(*_args, **kwargs):
        assert kwargs == {"for_update": True}
        calls.append("session")
        return session

    db = SimpleNamespace(get=get)
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(creator_routes, "_load_session", load_session)

    completed = await creator_routes._lock_confirmed_render_graph(
        db,
        item_id=item.id,
        user_id=user_id,
        session_id=session.id,
        job_id=job.id,
    )

    assert completed is session
    assert calls == ["plan_item", "job", "session"]


@pytest.mark.asyncio
async def test_confirm_without_an_active_plan_returns_conflict(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    plan = SimpleNamespace(ownership_epoch=1)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="briefing",
        revision=0,
        active_plan=None,
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.return_value = receipt_result
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))

    with pytest.raises(HTTPException) as caught:
        await creator_routes.confirm_creator_plan(
            str(item.id),
            ConfirmBody(
                session_id=session.id,
                expected_revision=0,
                plan_version=1,
                plan_hash="a" * 64,
                client_event_id="confirm-1",
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "Creator plan changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_direct_confirm_refuses_guided_clean_choice_while_cleanup_is_off(
    monkeypatch, enabled: bool
) -> None:
    """The plan-item confirm route shares the thread route's flag-off refusal."""
    from app.services.creator_execution_contract import GUIDED_VOICEOVER_CONTRACT
    from app.services.guided_speech_cleanup import DISABLED_MESSAGE

    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=None)
    plan = SimpleNamespace(ownership_epoch=1)
    strategy = {
        "execution_contract": GUIDED_VOICEOVER_CONTRACT,
        "render_program": "guided",
        "audio_strategy": "voiceover",
    }
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item.id,
        status="awaiting_confirmation",
        revision=0,
        # A different epoch is the next fence: reaching it proves the
        # cleanup check let the confirmation through.
        ownership_epoch=0,
        active_plan={"version": 1, "plan_hash": "a" * 64, "edit_plan": {"strategy": strategy}},
        render_attempts=0,
        max_render_attempts=2,
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db = AsyncMock()
    db.execute.return_value = receipt_result
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_voiceover_speech_cleanup_enabled", enabled)
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(
        creator_routes, "_owned_context", AsyncMock(return_value=(item, plan, SimpleNamespace()))
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(creator_routes, "_confirmed_edit_plan", lambda _active: object())

    with pytest.raises(HTTPException) as caught:
        await creator_routes.confirm_creator_plan(
            str(item.id),
            ConfirmBody(
                session_id=session.id,
                expected_revision=0,
                plan_version=1,
                plan_hash="a" * 64,
                client_event_id="confirm-clean-1",
                speech_cleanup_analysis_id=uuid.uuid4(),
                speech_cleanup_choice="clean",
            ),
            user,
            db,
        )

    assert caught.value.status_code == 409
    expected = "Creator ownership changed" if enabled else DISABLED_MESSAGE
    assert caught.value.detail == expected
    assert session.render_attempts == 0
    assert session.status == "awaiting_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery_action",
    ["retry_required", "retry_preflight_dispatch"],
)
@pytest.mark.parametrize(
    ("dispatch_outcome", "expected_status"),
    [
        ("dispatched", "rendering"),
        ("publish_failed", "failed"),
        ("speech_cleanup_recovery_conflict", "conflict"),
    ],
)
async def test_chat_cleanup_recovery_reaches_dispatch_without_rescheduling_analysis(
    monkeypatch,
    recovery_action: str,
    dispatch_outcome: str,
    expected_status: str,
) -> None:
    from app.tasks import content_plan_build

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    failed_job_id = uuid.uuid4()
    recovered_job_id = uuid.uuid4()
    analysis_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="native",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    active = {
        "version": 1,
        "plan_hash": "a" * 64,
        "creator_request": "Make a clean montage",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    item = SimpleNamespace(id=item_id, current_job_id=failed_job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=3,
        ownership_epoch=4,
        manifest_hash=manifest.manifest_hash,
        active_plan=active,
        render_attempts=1,
        max_render_attempts=3,
        iteration_count=1,
        target_variant_id=None,
        target_job_id=None,
        last_error=None,
    )
    failed_job = SimpleNamespace(id=failed_job_id, status="variants_failed")
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    receipt_holder: dict[str, CreatorAgentExecution] = {}
    db = AsyncMock()
    db.execute.return_value = receipt_result

    def add(row) -> None:
        if isinstance(row, CreatorAgentExecution):
            row.id = uuid.uuid4()
            receipt_holder["receipt"] = row

    async def get(model, identifier, **_kwargs):
        if model is Job and identifier == failed_job_id:
            return failed_job
        if model is CreatorAgentExecution:
            return receipt_holder.get("receipt")
        return None

    db.add = MagicMock(side_effect=add)
    db.get.side_effect = get
    dispatch = MagicMock(
        return_value=SimpleNamespace(
            outcome=dispatch_outcome,
            job_id=(
                str(recovered_job_id)
                if dispatch_outcome != "speech_cleanup_recovery_conflict"
                else None
            ),
        )
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(
        creator_routes,
        "_apply_plan_intent",
        MagicMock(side_effect=AssertionError("recovery must not reapply mutable plan intent")),
    )
    monkeypatch.setattr(
        creator_routes,
        "_previous_creator_clip_order",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        creator_routes,
        "_lock_confirmed_render_graph",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status=expected_status)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    monkeypatch.setattr(content_plan_build, "dispatch_item_render_for", dispatch)

    call = creator_routes.confirm_creator_plan_controller(
        str(item_id),
        ConfirmBody(
            session_id=session.id,
            expected_revision=3,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="cleanup-retry:r7",
        ),
        user,
        db,
        allow_chat=True,
        speech_cleanup_recovery_action=recovery_action,
        speech_cleanup_recovery_job_id=failed_job_id,
        speech_cleanup_recovery_generation_id="failed-generation",
        speech_cleanup_recovery_analysis_id=analysis_id,
    )
    if expected_status == "conflict":
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 409
        assert caught.value.detail == "speech_cleanup_analysis_changed"
    else:
        returned = await call
        assert returned is response
    dispatch.assert_called_once_with(
        str(item_id),
        4,
        bypass_guided_edit_gate=edit_plan.strategy.render_program == "native",
        creator_strategy=edit_plan.strategy.model_dump(mode="json", exclude_none=True),
        creator_clip_order=None,
        creator_request="Make a clean montage",
        speech_cleanup_analysis_id=None,
        speech_cleanup_choice=None,
        speech_cleanup_action=recovery_action,
        expected_job_id=str(failed_job_id),
        expected_render_generation_id="failed-generation",
        expected_speech_cleanup_analysis_id=str(analysis_id),
    )
    creator_routes._apply_plan_intent.assert_not_called()
    receipt = receipt_holder["receipt"]
    if expected_status == "conflict":
        assert session.target_job_id is None
        assert session.status == "awaiting_confirmation"
        assert session.render_attempts == 1
        assert session.iteration_count == 1
        assert receipt.status == "stale"
        assert receipt.error == {"code": "speech_cleanup_recovery_changed"}
    else:
        assert session.target_job_id == recovered_job_id
        assert session.status == expected_status
        assert receipt.status == ("succeeded" if expected_status == "rendering" else "failed")
        if expected_status == "failed":
            assert receipt.result == {"job_id": str(recovered_job_id)}
            assert session.last_error["code"] == "dispatch_publish_failed"
            assert session.render_attempts == 1
            assert session.iteration_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["not_enrolled", "unapproved_guided", "unsupported_format", "voiceover_unavailable"]
)
async def test_chat_dispatch_phone_gate_rejection_surfaces_typed_code(
    monkeypatch, reason: str
) -> None:
    """A phone_gate-tagged invalid_clips dispatch result must surface its own
    typed code/message, not fall into the blanket 'execution_failed' dead end
    (2026-09 fix — the iOS Retry affordance could never recover from that).

    Mirrors `test_chat_cleanup_recovery_reaches_dispatch_without_rescheduling
    _analysis`'s harness (same mocked collaborators), minus the speech-cleanup
    recovery kwargs -- an ordinary confirm still reaches the same dispatch-
    outcome handling block in `confirm_creator_plan_controller`.
    """
    from app.tasks import content_plan_build
    from app.tasks.content_plan_build import PHONE_GATE_MESSAGES

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="native",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    active = {
        "version": 1,
        "plan_hash": "a" * 64,
        "creator_request": "Make a clean montage",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    item = SimpleNamespace(id=item_id, current_job_id=None)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=3,
        ownership_epoch=4,
        manifest_hash=manifest.manifest_hash,
        active_plan=active,
        render_attempts=1,
        max_render_attempts=3,
        iteration_count=1,
        target_variant_id=None,
        target_job_id=None,
        last_error=None,
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    receipt_holder: dict[str, CreatorAgentExecution] = {}
    db = AsyncMock()
    db.execute.return_value = receipt_result

    def add(row) -> None:
        if isinstance(row, CreatorAgentExecution):
            row.id = uuid.uuid4()
            receipt_holder["receipt"] = row

    async def get(model, _identifier, **_kwargs):
        if model is CreatorAgentExecution:
            return receipt_holder.get("receipt")
        return None

    db.add = MagicMock(side_effect=add)
    db.get.side_effect = get
    dispatch = MagicMock(
        return_value=SimpleNamespace(outcome="invalid_clips", job_id=None, reason=reason)
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", MagicMock())
    monkeypatch.setattr(
        creator_routes,
        "_previous_creator_clip_order",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    monkeypatch.setattr(content_plan_build, "dispatch_item_render_for", dispatch)

    returned = await creator_routes.confirm_creator_plan_controller(
        str(item_id),
        ConfirmBody(
            session_id=session.id,
            expected_revision=3,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="phone-gate-1",
        ),
        user,
        db,
        allow_chat=True,
    )

    assert returned is response
    dispatch.assert_called_once()
    code, message = PHONE_GATE_MESSAGES[reason]
    assert session.status == "failed"
    assert session.last_error == {"code": code, "message": message}
    receipt = receipt_holder["receipt"]
    assert receipt.status == "failed"
    assert receipt.error == {"code": code, "message": message}
    # The confirm flow may emit an earlier progress event before dispatch;
    # only the LAST one (the failure) needs to carry the typed code.
    _args, kwargs = creator_routes.append_event.await_args
    assert kwargs["payload"] == {"message": message, "code": code}


@pytest.mark.asyncio
async def test_chat_dispatch_speech_cleanup_unavailable_on_phone_surfaces_typed_code(
    monkeypatch,
) -> None:
    """KRI-118 item 7: `speech_cleanup_unavailable_on_phone` (L1's new bare
    outcome, not wrapped in invalid_clips/reason) must surface its own typed
    code/message here too, not fall into the generic 'execution_failed'
    RuntimeError catch-all. Same harness as
    `test_chat_dispatch_phone_gate_rejection_surfaces_typed_code`.
    """
    from app.tasks import content_plan_build

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="native",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    active = {
        "version": 1,
        "plan_hash": "a" * 64,
        "creator_request": "Make a clean montage",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    item = SimpleNamespace(id=item_id, current_job_id=None)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=3,
        ownership_epoch=4,
        manifest_hash=manifest.manifest_hash,
        active_plan=active,
        render_attempts=1,
        max_render_attempts=3,
        iteration_count=1,
        target_variant_id=None,
        target_job_id=None,
        last_error=None,
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    receipt_holder: dict[str, CreatorAgentExecution] = {}
    db = AsyncMock()
    db.execute.return_value = receipt_result

    def add(row) -> None:
        if isinstance(row, CreatorAgentExecution):
            row.id = uuid.uuid4()
            receipt_holder["receipt"] = row

    async def get(model, _identifier, **_kwargs):
        if model is CreatorAgentExecution:
            return receipt_holder.get("receipt")
        return None

    db.add = MagicMock(side_effect=add)
    db.get.side_effect = get
    dispatch = MagicMock(
        return_value=SimpleNamespace(
            outcome="speech_cleanup_unavailable_on_phone", job_id=None, reason=None
        )
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", MagicMock())
    monkeypatch.setattr(
        creator_routes,
        "_previous_creator_clip_order",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    monkeypatch.setattr(content_plan_build, "dispatch_item_render_for", dispatch)

    returned = await creator_routes.confirm_creator_plan_controller(
        str(item_id),
        ConfirmBody(
            session_id=session.id,
            expected_revision=3,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="speech-cleanup-phone-1",
        ),
        user,
        db,
        allow_chat=True,
    )

    assert returned is response
    dispatch.assert_called_once()
    assert session.status == "failed"
    assert session.last_error["code"] == "speech_cleanup_unavailable_on_phone"
    assert "iPhone" in session.last_error["message"]
    receipt = receipt_holder["receipt"]
    assert receipt.status == "failed"
    assert receipt.error == session.last_error
    _args, kwargs = creator_routes.append_event.await_args
    assert kwargs["payload"]["code"] == "speech_cleanup_unavailable_on_phone"


@pytest.mark.asyncio
async def test_chat_cleanup_publish_failure_crash_replay_refunds_once(
    monkeypatch,
) -> None:
    """A crash after terminalizing the Job must resume through compensation."""
    from app.tasks import content_plan_build

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    failed_job_id = uuid.uuid4()
    analysis_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="native",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    active = {
        "version": 1,
        "plan_hash": "a" * 64,
        "creator_request": "Make a clean montage",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    item = SimpleNamespace(id=item_id, current_job_id=failed_job_id)
    plan = SimpleNamespace(ownership_epoch=4)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        revision=3,
        ownership_epoch=4,
        manifest_hash=manifest.manifest_hash,
        active_plan=active,
        render_attempts=2,
        max_render_attempts=3,
        iteration_count=2,
        target_variant_id=None,
        target_job_id=None,
        last_error=None,
    )
    body = ConfirmBody(
        session_id=session.id,
        expected_revision=3,
        plan_version=1,
        plan_hash="a" * 64,
        client_event_id="cleanup-publish-replay:r7",
    )
    digest_input = body.model_dump(mode="json")
    digest_input.update(
        {
            "speech_cleanup_recovery_action": "retry_preflight_dispatch",
            "speech_cleanup_recovery_job_id": str(failed_job_id),
            "speech_cleanup_recovery_generation_id": "failed-generation",
            "speech_cleanup_recovery_analysis_id": str(analysis_id),
        }
    )
    now = datetime.now(UTC)
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(digest_input),
        status="running",
        created_at=now,
        error=None,
        result=None,
        completed_at=None,
    )
    failed_job = SimpleNamespace(
        id=failed_job_id,
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=4,
        status="processing_failed",
        failure_reason="dispatch_publish_failed",
        created_at=now + timedelta(microseconds=1),
        all_candidates={
            "creator_strategy": edit_plan.strategy.model_dump(mode="json", exclude_none=True)
        },
        assembly_plan={"creator_generation_id": "failed-generation"},
    )
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = receipt
    db = AsyncMock()
    db.execute.return_value = receipt_result

    async def get(model, identifier, **_kwargs):
        if model is Job and identifier == failed_job_id:
            return failed_job
        if model is CreatorAgentExecution and identifier == receipt.id:
            return receipt
        return None

    db.get.side_effect = get
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "_lock_confirmed_render_graph",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    dispatch = MagicMock()
    monkeypatch.setattr(content_plan_build, "dispatch_item_render_for", dispatch)

    returned = await creator_routes.confirm_creator_plan_controller(
        str(item_id),
        body,
        user,
        db,
        allow_chat=True,
        speech_cleanup_recovery_action="retry_preflight_dispatch",
        speech_cleanup_recovery_job_id=failed_job_id,
        speech_cleanup_recovery_generation_id="failed-generation",
        speech_cleanup_recovery_analysis_id=analysis_id,
    )

    assert returned is response
    dispatch.assert_not_called()
    assert session.target_job_id == failed_job_id
    assert session.status == "failed"
    assert session.render_attempts == 1
    assert session.iteration_count == 1
    assert session.last_error["code"] == "dispatch_publish_failed"
    assert receipt.status == "failed"
    assert receipt.result == {"job_id": str(failed_job_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phone_original_audio", "speech_cleanup_choice"),
    [(False, "clean"), (True, "keep_original"), (False, None)],
)
async def test_guided_confirm_returns_rendering_after_rollback_expires_loaded_rows(
    monkeypatch,
    phone_original_audio,
    speech_cleanup_choice,
) -> None:
    """The guided confirmation must not read ORM instances after its rollback.

    ``_maybe_auto_design_generate`` commits the proposal/task reservation.  The
    confirmation route then rolls back to clear its old lock state.  In an
    ``AsyncSession`` that expires the original ``session`` and ``item`` rows;
    touching one of them afterwards raises ``MissingGreenlet`` instead of
    returning the durable rendering state.
    """

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    if phone_original_audio:
        manifest = resolve_creator_manifest(
            item_id="item-1",
            edit_format="montage",
            media=[{"media_id": "clip-1", "kind": "video"}],
            phone_source_media_ids=["clip-1"],
            phone_rendering_allowed=True,
            guided_capability_enabled=True,
        )
    initial_item = _ExpiringNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    session = _ExpiringNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash=manifest.manifest_hash,
        active_plan=None,
        render_attempts=0,
        max_render_attempts=2,
        iteration_count=0,
    )
    live_item = SimpleNamespace(id=item_id, current_job_id=None)
    refreshed_item = SimpleNamespace(
        id=item_id,
        current_job_id=job_id,
        edit_proposal=None,
    )
    completed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        active_plan={},
        target_job_id=None,
        status="executing",
    )
    rehydrated_session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        ownership_epoch=1,
        active_plan={},
        status="executing",
    )
    plan = SimpleNamespace(ownership_epoch=1)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="original_audio" if phone_original_audio else "licensed_music",
        render_program="native" if phone_original_audio else "guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    assert edit_plan.strategy.render_program == "guided"
    if phone_original_audio:
        assert edit_plan.strategy.montage_audio is not None
        assert edit_plan.strategy.montage_audio.preserve_source_audio is True
    active = {
        "version": 1,
        "plan_hash": "a" * 64,
        "summary": "A focused guided edit",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
        "guided_speech_cleanup": {
            "generation_attempt_id": "old-attempt",
            "analysis_id": str(uuid.uuid4()),
            "choice": "clean",
        },
    }
    session.active_plan = active
    speech_cleanup_analysis_id = uuid.uuid4()
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(
            {
                "session_id": str(session_id),
                "expected_revision": 0,
                "plan_version": 1,
                "plan_hash": "a" * 64,
                "client_event_id": "guided-confirm-1",
            }
        ),
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    candidate = SimpleNamespace(
        id=job_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=1,
        status="processing",
        assembly_plan={},
    )
    db = AsyncMock()
    db.add = MagicMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result

    async def rollback_and_expire() -> None:
        initial_item.expire()
        session.expire()

    db.rollback.side_effect = rollback_and_expire

    async def db_get(model, object_id, **_kwargs):
        if model is Job:
            return candidate
        if model is CreatorAgentExecution:
            return receipt
        return None

    db.get.side_effect = db_get
    owned_context = AsyncMock(
        side_effect=[
            (initial_item, plan, SimpleNamespace()),
            (live_item, plan, SimpleNamespace()),
            (refreshed_item, plan, SimpleNamespace()),
            (refreshed_item, plan, SimpleNamespace()),
        ]
    )
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(
        creator_routes,
        "_load_session",
        AsyncMock(side_effect=[session, rehydrated_session, completed]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", lambda *_args: None)
    monkeypatch.setattr(
        creator_routes, "_seed_guided_specialist_brief", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(
        creator_routes,
        "_response",
        AsyncMock(return_value=SimpleNamespace(status="rendering")),
    )

    async def reserve_guided_proposal(*_args, generation_attempt_id, **_kwargs):
        # The consent envelope is durable before the helper can reserve and
        # publish guided work.  A no-choice confirmation must also clear the
        # prior attempt's envelope.
        assert db.commit.await_count == 1
        if speech_cleanup_choice is None:
            assert session.active_plan["guided_speech_cleanup"] is None
        else:
            assert session.active_plan["guided_speech_cleanup"] == {
                "generation_attempt_id": generation_attempt_id,
                "analysis_id": str(speech_cleanup_analysis_id),
                "choice": speech_cleanup_choice,
            }
        assert session.active_plan["guided_generation_attempt_id"] == generation_attempt_id
        refreshed_item.edit_proposal = {
            "generation_attempt_id": generation_attempt_id,
            "proposal_version": 7,
        }
        candidate.assembly_plan = {"guided_edit": {"generation_attempt_id": generation_attempt_id}}
        return SimpleNamespace()

    monkeypatch.setattr(plan_item_routes, "_maybe_auto_design_generate", reserve_guided_proposal)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_auto_design_enabled", True)

    returned = await creator_routes.confirm_creator_plan(
        str(item_id),
        ConfirmBody(
            session_id=session_id,
            expected_revision=0,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="guided-confirm-1",
            speech_cleanup_analysis_id=(
                speech_cleanup_analysis_id if speech_cleanup_choice is not None else None
            ),
            speech_cleanup_choice=speech_cleanup_choice,
        ),
        user,
        db,
    )

    assert returned.status == "rendering"
    assert completed.status == "rendering"
    assert completed.target_job_id == job_id
    assert receipt.status == "succeeded"


@pytest.mark.asyncio
async def test_guided_confirm_persists_failure_after_post_rollback_expiry(monkeypatch) -> None:
    """A post-rollback error must become a durable failed receipt, not a 500."""

    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    initial_item = _ExpiringNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    session = _ExpiringNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash=manifest.manifest_hash,
        active_plan=None,
        render_attempts=0,
        max_render_attempts=2,
        iteration_count=0,
    )
    failed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        last_error=None,
    )
    plan = SimpleNamespace(ownership_epoch=1)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    session.active_plan = {
        "version": 1,
        "plan_hash": "a" * 64,
        "summary": "A focused guided edit",
        "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
    }
    request = {
        "session_id": session_id,
        "expected_revision": 0,
        "plan_version": 1,
        "plan_hash": "a" * 64,
        "client_event_id": "guided-confirm-fail-1",
    }
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash({**request, "session_id": str(session_id)}),
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db = AsyncMock()
    db.add = MagicMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result

    async def rollback_and_expire() -> None:
        initial_item.expire()
        session.expire()

    db.rollback.side_effect = rollback_and_expire
    db.get.return_value = receipt
    owned_context = AsyncMock(
        side_effect=[
            (initial_item, plan, SimpleNamespace()),
            (SimpleNamespace(id=item_id, current_job_id=None), plan, SimpleNamespace()),
            MissingGreenlet("expired ORM row"),
        ]
    )
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(
        creator_routes,
        "_load_session",
        AsyncMock(side_effect=[session, failed]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", lambda *_args: None)
    monkeypatch.setattr(
        creator_routes, "_seed_guided_specialist_brief", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(
        creator_routes,
        "_response",
        AsyncMock(return_value=SimpleNamespace(status="failed")),
    )
    monkeypatch.setattr(
        plan_item_routes,
        "_maybe_auto_design_generate",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_auto_design_enabled", True)

    returned = await creator_routes.confirm_creator_plan(
        str(item_id),
        ConfirmBody(
            session_id=session_id,
            expected_revision=0,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="guided-confirm-fail-1",
        ),
        user,
        db,
    )

    assert returned.status == "failed"
    assert failed.status == "failed"
    assert failed.last_error["code"] == "execution_failed"
    assert receipt.status == "failed"
    assert receipt.error["code"] == "execution_failed"


@pytest.mark.asyncio
async def test_guided_confirm_resumes_exact_job_without_auto_design(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_id = str(uuid.uuid4())
    speech_cleanup_analysis_id = uuid.uuid4()
    guided_speech_cleanup = {
        "generation_attempt_id": attempt_id,
        "analysis_id": str(speech_cleanup_analysis_id),
        "choice": "clean",
    }
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        revision=0,
        ownership_epoch=1,
        active_plan={
            "version": 1,
            "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
            "guided_generation_attempt_id": attempt_id,
            "guided_speech_cleanup": guided_speech_cleanup,
        },
    )
    completed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        active_plan=session.active_plan,
        target_job_id=None,
        status="executing",
    )
    body = ConfirmBody(
        session_id=session_id,
        expected_revision=0,
        plan_version=1,
        plan_hash="a" * 64,
        client_event_id="guided-resume-1",
    )
    receipt = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=canonical_context_hash(body.model_dump(mode="json")),
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    candidate = SimpleNamespace(
        id=job_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        user_id=user.id,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=1,
        all_candidates={},
        assembly_plan={"guided_edit": {"generation_attempt_id": attempt_id}},
    )
    db = AsyncMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = receipt
    db.execute.return_value = receipt_result

    async def db_get(model, _object_id, **_kwargs):
        if model is Job:
            return candidate
        if model is CreatorAgentExecution:
            return receipt
        return None

    db.get.side_effect = db_get
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(ownership_epoch=1), SimpleNamespace())),
    )
    monkeypatch.setattr(
        creator_routes, "_load_session", AsyncMock(side_effect=[session, completed])
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="rendering")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))
    auto_design = AsyncMock()
    monkeypatch.setattr(plan_item_routes, "_maybe_auto_design_generate", auto_design)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)

    returned = await creator_routes.confirm_creator_plan(str(item_id), body, user, db)

    assert returned is response
    assert completed.status == "rendering"
    assert completed.target_job_id == job_id
    # Receipt replays retain the already-durable attempt envelope. They do
    # not mint an attempt or overwrite consent from the original confirmation.
    assert completed.active_plan["guided_speech_cleanup"] == guided_speech_cleanup
    assert receipt.status == "succeeded"
    auto_design.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_confirm_missing_refreshed_receipt_returns_durable_failure(
    monkeypatch,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    session_id = uuid.uuid4()
    manifest = _manifest(monkeypatch)
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        render_program="guided",
        selected_media_ids=["clip-1"],
    )
    edit_plan = compile_strategy_to_plan(manifest, strategy)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None)
    session = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="awaiting_confirmation",
        revision=0,
        ownership_epoch=1,
        manifest_hash=manifest.manifest_hash,
        active_plan={
            "version": 1,
            "plan_hash": "a" * 64,
            "summary": "A focused guided edit",
            "edit_plan": edit_plan.model_dump(mode="json", exclude_none=True),
        },
        render_attempts=0,
        max_render_attempts=2,
        iteration_count=0,
    )
    rehydrated = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        ownership_epoch=1,
        active_plan=session.active_plan,
    )
    failed = SimpleNamespace(
        id=session_id,
        creator_id=user.id,
        plan_item_id=item_id,
        status="executing",
        last_error=None,
    )
    db = AsyncMock()
    db.add = MagicMock()
    receipt_result = MagicMock()
    receipt_result.scalar_one_or_none.return_value = None
    db.execute.return_value = receipt_result
    db.get.return_value = None
    refreshed_item = SimpleNamespace(id=item_id, current_job_id=None, edit_proposal=None)
    owned_context = AsyncMock(
        side_effect=[
            (item, SimpleNamespace(ownership_epoch=1), SimpleNamespace()),
            (item, SimpleNamespace(ownership_epoch=1), SimpleNamespace()),
            (refreshed_item, SimpleNamespace(ownership_epoch=1), SimpleNamespace()),
        ]
    )
    monkeypatch.setattr(creator_routes, "_owned_context", owned_context)
    monkeypatch.setattr(
        creator_routes,
        "_load_session",
        AsyncMock(side_effect=[session, rehydrated, failed]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "_apply_plan_intent", lambda *_args: None)
    monkeypatch.setattr(
        creator_routes, "_seed_guided_specialist_brief", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    response = SimpleNamespace(status="failed")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    async def reserve_guided_proposal(*_args, generation_attempt_id, **_kwargs):
        refreshed_item.edit_proposal = {
            "generation_attempt_id": generation_attempt_id,
            "proposal_version": 7,
        }
        return SimpleNamespace()

    monkeypatch.setattr(plan_item_routes, "_maybe_auto_design_generate", reserve_guided_proposal)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_execution_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_rollout_percent", 100)
    monkeypatch.setattr(settings, "guided_auto_design_enabled", True)

    returned = await creator_routes.confirm_creator_plan(
        str(item_id),
        ConfirmBody(
            session_id=session_id,
            expected_revision=0,
            plan_version=1,
            plan_hash="a" * 64,
            client_event_id="guided-missing-receipt-1",
        ),
        user,
        db,
    )

    assert returned is response
    assert failed.status == "failed"
    assert failed.last_error["code"] == "execution_failed"
    assert db.rollback.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_render_preserves_direction_and_refunds_attempt(monkeypatch) -> None:
    user_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        plan_item_id=item.id,
        status="rendering",
        render_attempts=1,
        iteration_count=1,
    )
    receipt = SimpleNamespace(id=uuid.uuid4(), status="running", error=None, completed_at=None)
    db = AsyncMock()
    db.get.return_value = receipt
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    append = AsyncMock()
    monkeypatch.setattr(creator_routes, "append_event", append)
    response = SimpleNamespace(status="briefing")
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    returned = await creator_routes._concurrent_render_response(
        db,
        session=session,
        item=item,
        user_id=user_id,
        receipt=receipt,
    )

    assert returned is response
    assert session.status == "briefing"
    assert session.render_attempts == 0
    assert receipt.status == "stale"
    assert receipt.error == {"code": "concurrent_render"}
    append.assert_awaited_once()


@pytest.mark.parametrize(
    "creator_message",
    [
        "Add a text saying Summer in Madrid. Make it pastel yellow",
        'Add text saying "Summer in Madrid". Make the text pastel yellow.',
        "Text saying Summer in Madrid; text color pastel yellow.",
    ],
)
def test_explicit_text_saying_preserves_literal_copy_and_pastel_yellow(
    monkeypatch, creator_message
):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(opening_title="Wrong title", text_color="#FFD24A"),
        creator_message,
        manifest=_manifest(monkeypatch),
    )
    assert strategy.opening_title == "Summer in Madrid"
    assert strategy.text_color == "#FFF0A6"


def test_negated_text_saying_does_not_add_title(monkeypatch):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        "Do not add text saying Summer in Madrid.",
        manifest=_manifest(monkeypatch),
    )
    assert strategy.opening_title is None


# --- KRI-129 part E: closing/ending titles must never be mis-assigned to
# opening_title (and vice versa), and a font-name cue must survive "for the
# font" phrasing. No regression test shipped with the original fix. ---------


def test_closing_title_cue_sets_closing_not_opening(monkeypatch):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        'closing title "Thanks for watching"',
        manifest=_manifest(monkeypatch),
    )
    assert strategy.closing_title == "Thanks for watching"
    assert strategy.opening_title is None


def test_end_title_cue_sets_closing_not_opening(monkeypatch):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        'end title should say "Thanks for watching"',
        manifest=_manifest(monkeypatch),
    )
    assert strategy.closing_title == "Thanks for watching"
    assert strategy.opening_title is None


def test_intro_title_cue_sets_opening(monkeypatch):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        'Add an intro title saying "Emir Olympics London Edition"',
        manifest=_manifest(monkeypatch),
    )
    assert strategy.opening_title == "Emir Olympics London Edition"
    assert strategy.closing_title is None


def test_outro_music_title_cue_belongs_to_a_different_clause(monkeypatch):
    """ "outro" precedes a separate clause (before the comma) and must not be
    read as a closing-title cue for the "title" mention that follows it."""

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        'outro music, title "A"',
        manifest=_manifest(monkeypatch),
    )
    assert strategy.opening_title == "A"
    assert strategy.closing_title is None


def test_negated_closing_title_adds_no_title(monkeypatch):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        "do not add a closing title",
        manifest=_manifest(monkeypatch),
    )
    assert strategy.opening_title is None
    assert strategy.closing_title is None


def test_use_font_name_for_the_font_phrasing(monkeypatch):
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        "Use Anton for the font",
        manifest=_manifest(monkeypatch),
    )
    assert strategy.font_family == "Anton"


def test_two_quoted_titles_in_one_message_set_both_ends(monkeypatch):
    """Known gap fixed: a message classifying two distinct quoted title
    mentions -- one opening, one closing -- by their own clause prefixes must
    set BOTH, never lose the second to the first `re.search` hit."""

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(),
        'title "A", closing title "B"',
        manifest=_manifest(monkeypatch),
    )
    assert strategy.opening_title == "A"
    assert strategy.closing_title == "B"


@pytest.mark.asyncio
@pytest.mark.parametrize("compile_fails_once", [False, True])
@pytest.mark.parametrize("semantic_plan", [False, True])
async def test_route_schema_failure_preserves_madrid_text_and_source_capacity(
    monkeypatch,
    compile_fails_once,
    semantic_plan,
) -> None:
    from app.services import creator_capabilities

    monkeypatch.setattr(creator_capabilities.settings, "creator_prompt_fidelity_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=False,
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": duration}
            for index, duration in enumerate([4.068333, 4.9, 3.733333])
        ],
        guided_capability_enabled=True,
    )
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="awaiting_confirmation")

    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio,
        "to_thread",
        AsyncMock(side_effect=TerminalError("model truncated output")),
    )
    append_event = AsyncMock()
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    message = "Add a text saying Summer in Madrid. Make it pastel yellow"
    if semantic_plan:
        from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence

        message = "Put Summer in Madrid across the opening. Give the letters a soft buttery tint."
        monkeypatch.setattr(
            creator_routes.asyncio,
            "to_thread",
            AsyncMock(
                return_value=SimpleNamespace(
                    action=ProposeStrategy(
                        kind="propose_strategy",
                        strategy=CreativeStrategy(
                            opening_title="Summer in Madrid",
                            text_color="#FFF0A6",
                            audio_strategy="original_audio",
                        ),
                        summary="Summer in Madrid in soft yellow over your clips.",
                        render_intent_evidence=CreatorRenderIntentEvidence(
                            opening_title="Put Summer in Madrid across the opening.",
                            text_color="Give the letters a soft buttery tint.",
                        ),
                    ),
                )
            ),
        )

    if compile_fails_once:
        real_compile = creator_routes.compile_active_plan
        attempts = 0

        def compile_with_first_failure(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("Incomplete proposal")
            return real_compile(*args, **kwargs)

        monkeypatch.setattr(creator_routes, "compile_active_plan", compile_with_first_failure)

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message=message,
    )

    assert result is response
    if not semantic_plan:
        assert session.status == "briefing"
        assert session.active_plan is None
        assert session.last_error == {"code": "provider_unavailable"}
        failure = append_event.await_args.kwargs
        assert failure["event_type"] == "assistant_error"
        assert failure["payload"] == {
            "message": (
                "I couldn't reach the planner right now. Your request is saved; try again later."
            ),
            "code": "provider_unavailable",
        }
        return
    assert session.status == "awaiting_confirmation"
    assert "completed" not in session.active_plan["summary"].casefold()
    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["opening_title"] == "Summer in Madrid"
    assert strategy["text_color"] == "#FFF0A6"
    assert session.active_plan["target_duration_s"] <= 12.701666
    assert session.active_plan["video_reuse_policy"] == "once"
    assert len(strategy["selected_media_ids"]) == 3


@pytest.mark.parametrize(
    "creator_message,title,color,evidence",
    [
        (
            "Could you put Summer in Madrid across the opening? "
            "A soft buttery shade would be nice.",
            "Summer in Madrid",
            "#FFF0A6",
            {
                "opening_title": "Could you put Summer in Madrid across the opening?",
                "text_color": "A soft buttery shade would be nice.",
            },
        ),
        (
            "Videonun başına Madrid'de Yaz yaz. Harfler açık sarı olsun.",
            "Madrid'de Yaz",
            "#FFF0A6",
            {
                "opening_title": "Videonun başına Madrid'de Yaz yaz.",
                "text_color": "Harfler açık sarı olsun.",
            },
        ),
        (
            "Escribe Verano en Madrid al principio, con letras amarillas suaves.",
            "Verano en Madrid",
            "#FFF0A6",
            {
                "opening_title": (
                    "Escribe Verano en Madrid al principio, con letras amarillas suaves."
                ),
                "text_color": "con letras amarillas suaves.",
            },
        ),
        (
            "No words on screen anymore. Keep the soft yellow for later.",
            None,
            None,
            {
                "opening_title": "No words on screen anymore.",
                "text_color": "Keep the soft yellow for later.",
            },
        ),
    ],
)
def test_grounded_semantic_text_intent_does_not_require_english_keywords(
    monkeypatch,
    creator_message,
    title,
    color,
    evidence,
):
    from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(opening_title=title, text_color=color),
        creator_message,
        manifest=_manifest(monkeypatch),
        render_intent_evidence=CreatorRenderIntentEvidence(**evidence),
    )
    assert strategy.opening_title == title
    assert strategy.text_color == color


@pytest.mark.parametrize(
    "title,quote",
    [
        ("Beautiful sunsets", "Beautiful sunsets"),  # Metadata cannot invent an excerpt.
        ("Amazing Madrid", "Put Summer in Madrid across the opening."),  # Invented copy.
    ],
)
def test_semantic_title_requires_creator_evidence_and_exact_copy(monkeypatch, title, quote):
    from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence

    strategy = _apply_explicit_render_intent(
        CreativeStrategy(opening_title=title, text_color="#123456"),
        "Put Summer in Madrid across the opening.",
        manifest=_manifest(monkeypatch),
        render_intent_evidence=CreatorRenderIntentEvidence(
            opening_title=quote,
            text_color="Metadata says dark blue",
        ),
    )
    assert strategy.opening_title is None
    assert strategy.text_color is None


@pytest.mark.parametrize(
    "title,latest",
    [
        ("Winter in Berlin", "Put Winter in Berlin across the opening."),
        (None, "Leave the video without any writing now."),
    ],
)
def test_latest_semantic_revision_survives_capped_creator_history(monkeypatch, title, latest):
    from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence

    history = ('Use title "Summer in Madrid". ' + "Earlier direction. " * 1000)[:12000]
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(opening_title=title),
        history,
        manifest=_manifest(monkeypatch),
        latest_user_message=latest,
        render_intent_evidence=CreatorRenderIntentEvidence(opening_title=latest),
    )
    assert strategy.opening_title == title
