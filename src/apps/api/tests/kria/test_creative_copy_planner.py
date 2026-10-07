"""Planner unit coverage for the KRI-506 server-owned copy lifecycle."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.agents.main_creator import CreativeCopyDecision
from app.kria import planner
from app.kria.planner import PlannedKriaTurn
from app.services.creative_copy_decisions import media_digest

DIGEST = "d" * 24


def _inputs(*, state: dict | None = None, digest: str = DIGEST) -> SimpleNamespace:
    return SimpleNamespace(creative_copy_state=state or {}, creative_copy_digest=digest)


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64)


def _decision(status: str, *, text: str | None = None) -> CreativeCopyDecision:
    return CreativeCopyDecision(
        target="opening_title",
        status=status,
        proposed_text=text,
        source_evidence="creator request" if text else None,
    )


def test_initial_candidate_emits_wording_question() -> None:
    result = planner._creative_copy_turn(
        _decision("candidate", text="One day, one city"),
        inputs=_inputs(),
        creator_request="Write a hook",
        manifest=_manifest(),
    )

    assert result is not None
    assert result.plan.turn_value == "question"
    assert result.plan.choice_question["kind"] == "creative_copy_wording"
    assert result.plan.choice_question["candidate"] == "One day, one city"
    assert result.plan.choice_question["dependency_digest"] == DIGEST


def test_explicit_delegation_with_candidate_has_same_wording_gate() -> None:
    result = planner._creative_copy_turn(
        _decision("delegated", text="Let the city surprise you"),
        inputs=_inputs(),
        creator_request="You choose the hook",
        manifest=_manifest(),
    )

    assert result is not None
    assert result.plan.choice_question["kind"] == "creative_copy_wording"
    assert result.plan.choice_question["candidate"] == "Let the city surprise you"


def test_unresolved_emits_authorship_question() -> None:
    result = planner._creative_copy_turn(
        _decision("unresolved"),
        inputs=_inputs(),
        creator_request="Add an opening hook",
        manifest=_manifest(),
    )

    assert result is not None
    assert result.plan.choice_question["kind"] == "creative_copy_authorship"
    assert "would you like me to write one" in result.plan.response.casefold()


def test_approved_state_plus_unresolved_does_not_reask() -> None:
    state = {"opening_title": SimpleNamespace(approved="Approved title", cancelled=False)}

    assert (
        planner._creative_copy_turn(
            _decision("unresolved"),
            inputs=_inputs(state=state),
            creator_request="Continue",
            manifest=_manifest(),
        )
        is None
    )


def test_stale_candidate_emits_question_with_current_digest() -> None:
    state = {
        "opening_title": SimpleNamespace(
            status="stale", candidate="Reconfirm this title", approved=None, cancelled=False
        )
    }
    current = "e" * 24
    result = planner._creative_copy_turn(
        _decision("unresolved"),
        inputs=_inputs(state=state, digest=current),
        creator_request="Continue",
        manifest=_manifest(),
    )

    assert result is not None
    assert result.plan.choice_question["kind"] == "creative_copy_wording"
    assert result.plan.choice_question["candidate"] == "Reconfirm this title"
    assert result.plan.choice_question["dependency_digest"] == current


def test_creator_supplied_does_not_become_generated_question() -> None:
    assert (
        planner._creative_copy_turn(
            _decision("creator_supplied", text="My exact words"),
            inputs=_inputs(),
            creator_request="My exact words",
            manifest=_manifest(),
        )
        is None
    )


def _planned_strategy() -> PlannedKriaTurn:
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        audio_strategy="original_audio",
        pacing="balanced",
        render_program="guided",
        rationale="A short montage.",
    )
    action = ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="A montage.")
    return PlannedKriaTurn(
        plan=planner.adapt_creator_action(action), manifest_hash="a" * 64, context_hash="b" * 64
    )


@pytest.mark.asyncio
async def test_gate_retains_approved_text_without_generic_choice_answers(monkeypatch) -> None:
    snapshot = {"clip_assignments": [{"media_id": "clip-1", "kind": "video"}]}
    digest = media_digest(snapshot)
    planned = replace(
        _planned_strategy(),
        media_snapshot=snapshot,
        creative_copy_resolution={
            "target": "opening_title",
            "dependency_digest": digest,
            "status": "creator_supplied",
            "text": "Approved creator title",
        },
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        type(planner.settings), "kria_choice_questions_enabled", False, raising=False
    )

    result = await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid4(), creator_id=uuid4()
    )

    assert result.plan.mode == "act"
    assert result.plan.intents[0].arguments["strategy"]["opening_title"] == "Approved creator title"


@pytest.mark.asyncio
async def test_gate_retains_cancelled_copy_target_as_omitted(monkeypatch) -> None:
    snapshot = {"clip_assignments": [{"media_id": "clip-1", "kind": "video"}]}
    digest = media_digest(snapshot)
    planned = replace(
        _planned_strategy(),
        media_snapshot=snapshot,
        creative_copy_resolution={
            "target": "opening_title",
            "dependency_digest": digest,
            "status": "cancelled",
        },
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        type(planner.settings), "kria_choice_questions_enabled", False, raising=False
    )

    result = await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid4(), creator_id=uuid4()
    )

    assert result.plan.intents[0].arguments["strategy"]["omitted_copy_targets"] == ["opening_title"]


@pytest.mark.asyncio
async def test_incidental_user_phrase_is_not_authorization_to_print_it():
    from app.agents.main_creator import MainCreatorOutput

    output = MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(opening_title="make it epic"),
            summary="An epic opening",
        )
    )
    result = await planner._plan_creator_action(
        None,
        thread_id=uuid4(),
        item_id=uuid4(),
        creator_id=uuid4(),
        user_message="make it epic",
        manifest=_manifest(),
        inputs=_inputs(),
        output=output,
    )
    assert result.plan.mode == "respond"
    assert result.plan.choice_question["kind"] == "creative_copy_wording"
    assert result.plan.choice_question["candidate"] == "make it epic"
