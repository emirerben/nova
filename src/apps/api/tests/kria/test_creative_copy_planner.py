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
    snapshot = {"clip_assignments": [{"media_id": "clip-1", "kind": "video", "duration_s": 3}]}
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
    snapshot = {"clip_assignments": [{"media_id": "clip-1", "kind": "video", "duration_s": 3}]}
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


def test_incident_kri586_shape_with_empty_state_becomes_a_server_wording_question() -> None:
    """Prod 2026-10-11: delegated hook, empty copy state, misnamed `creative_decision` keys.

    After parse repair the first-turn candidate must reach the server's own wording
    question (consent still pending), not a generic ``ask_user:*`` card.
    """
    import json

    from app.agents.main_creator import MainCreatorAgent
    from tests.agents.test_main_creator_agent import _input

    raw = json.dumps(
        {
            "action": {
                "kind": "ask_user",
                "question": "How about Sunrise miles for the hook?",
                "reason_code": "hook_candidate",
                "options": [],
            },
            "creative_decision": {
                "target": "r1",
                "state": "candidate",
                "candidate": "Sunrise miles",
            },
        }
    )
    output = MainCreatorAgent(None).parse(raw, _input())  # type: ignore[arg-type]

    result = planner._creative_copy_turn(
        output.creative_decision,
        inputs=_inputs(),
        creator_request="Add a hook that matches the video",
        manifest=_manifest(),
        response=output.action.question,
    )

    assert result is not None
    assert result.plan.choice_question["conflict"] == "creative_copy"
    assert result.plan.choice_question["kind"] == "creative_copy_wording"
    assert result.plan.choice_question["candidate"] == "Sunrise miles"


# KRI-682 (prod 2026-10-15, Tigre night M1): the creator typed `Başlık: "Gece 23:00,
# Tigre"` on turn 1; on turn 3 the strategy carried that exact title but its evidence
# excerpt did not match the creator's words, so Kria asked "Şunu deneyebilirim: …
# Bu ifade sana uyar mı?" for the creator's own title.
#
# Ways this can go wrong, each pinned below:
# - the quote sits in an earlier message, not the current one;
# - the model's evidence is the brief's rewrite (`Başlık ("…")`), not the creator's text;
# - iPhone smart quotes (“…”) instead of straight ones;
# - spacing / Unicode form differs between the quote and the strategy value;
# - the quote is beyond the model's 1000-char conversation excerpt;
# - an unquoted phrase must still need approval (see the incidental-phrase test above);
# - a title that only overlaps the quote must still need approval;
# - a quote in an assistant message (Kria's own suggestion) is not creator wording.
_TIGRE_FIRST = (
    "Gece 11'de tekneyle Tigre merkeze arkadaşımız Atakan'ı almaya gittik. "
    'Başlık: "Gece 23:00, Tigre". "Hadi gidelim" dediğimiz iskele klibiyle başla.'
)
_TIGRE_LATEST = "İlk klip: iskelede Amigo Sergio diye seslendiğimiz karanlık klip."


def _quoted_title_inputs(
    conversation: list[dict], *, raw_creator_request: str | None = None
) -> SimpleNamespace:
    inputs = _inputs()
    inputs.agent_input = SimpleNamespace(conversation=conversation)
    inputs.raw_creator_request = raw_creator_request
    return inputs


class _PastCopyGate(Exception):
    """Raised by the first runtime check after the copy gate; that part is not under test."""


async def _title_turn(
    title: str,
    *,
    conversation: list[dict],
    user_message: str = _TIGRE_LATEST,
    evidence: str | None = 'Başlık ("Gece 23:00, Tigre")',
    raw_creator_request: str | None = None,
    monkeypatch,
) -> PlannedKriaTurn | None:
    """The copy-gate question, or None when the title went through to planning."""
    from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence
    from app.agents.main_creator import MainCreatorOutput

    def past_gate(*_args, **_kwargs):
        raise _PastCopyGate

    monkeypatch.setattr(planner, "check_strategy_for_runtime_v2", past_gate)
    output = MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(opening_title=title),
            summary="Night boat montage",
            render_intent_evidence=CreatorRenderIntentEvidence(opening_title=evidence),
        )
    )
    try:
        return await planner._plan_creator_action(
            None,
            thread_id=uuid4(),
            item_id=uuid4(),
            creator_id=uuid4(),
            user_message=user_message,
            manifest=SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64, media=()),
            inputs=_quoted_title_inputs(conversation, raw_creator_request=raw_creator_request),
            output=output,
        )
    except _PastCopyGate:
        return None


def _asks_wording(result: PlannedKriaTurn | None) -> bool:
    question = (result.plan.choice_question or {}) if result is not None else {}
    return question.get("conflict") == "creative_copy"


@pytest.mark.asyncio
async def test_kri682_title_quoted_in_an_earlier_message_is_not_re_asked(monkeypatch):
    result = await _title_turn(
        "Gece 23:00, Tigre",
        conversation=[
            {"role": "user", "content": _TIGRE_FIRST},
            {"role": "user", "content": _TIGRE_LATEST},
        ],
        monkeypatch=monkeypatch,
    )
    assert not _asks_wording(result)


@pytest.mark.asyncio
async def test_kri682_smart_quotes_and_spacing_still_count(monkeypatch):
    result = await _title_turn(
        "Gece 23:00, Tigre",
        conversation=[{"role": "user", "content": "Başlık: “Gece  23:00,  Tigre” olsun."}],
        evidence=None,
        monkeypatch=monkeypatch,
    )
    assert not _asks_wording(result)


@pytest.mark.asyncio
async def test_kri682_quote_past_the_model_excerpt_uses_the_full_request(monkeypatch):
    long_first = "x" * 1200 + ' Başlık: "Gece 23:00, Tigre".'
    result = await _title_turn(
        "Gece 23:00, Tigre",
        conversation=[{"role": "user", "content": long_first[:1000]}],
        raw_creator_request=long_first,
        monkeypatch=monkeypatch,
    )
    assert not _asks_wording(result)


@pytest.mark.asyncio
async def test_kri682_title_that_only_overlaps_the_quote_is_still_asked(monkeypatch):
    result = await _title_turn(
        "Gece 23:00",
        conversation=[{"role": "user", "content": _TIGRE_FIRST}],
        monkeypatch=monkeypatch,
    )
    assert _asks_wording(result)
    assert result.plan.choice_question["candidate"] == "Gece 23:00"


@pytest.mark.asyncio
async def test_kri682_assistant_quote_is_not_creator_wording(monkeypatch):
    result = await _title_turn(
        "Gece Yarısı Teknesi",
        conversation=[
            {"role": "assistant", "content": 'Şunu deneyebilirim: "Gece Yarısı Teknesi".'},
            {"role": "user", "content": _TIGRE_LATEST},
        ],
        monkeypatch=monkeypatch,
    )
    assert _asks_wording(result)
