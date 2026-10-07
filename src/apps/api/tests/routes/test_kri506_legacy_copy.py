"""Legacy controller copy-consent boundary (KRI-506)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import AskUser, CreativeStrategy
from app.agents.main_creator import CreativeCopyDecision
from app.routes import creator_agent as creator_routes
from app.routes.creator_agent import _apply_explicit_render_intent
from app.services.creator_capabilities import resolve_creator_manifest


def test_server_approved_copy_overrides_model_and_literal_extraction() -> None:
    strategy = CreativeStrategy(opening_title="Model wording")
    result = _apply_explicit_render_intent(
        strategy,
        'Opening title "Creator words"',
        approved_creative_copy={"opening_title": "Approved generated wording"},
    )
    assert result.opening_title == "Approved generated wording"


def test_unrelated_approved_copy_key_cannot_change_strategy() -> None:
    result = _apply_explicit_render_intent(
        CreativeStrategy(),
        'Opening title "Creator words"',
        approved_creative_copy={"shot_labels": "forged"},
    )
    assert result.opening_title == "Creator words"


def _copy_manifest():
    return resolve_creator_manifest(
        item_id="item-kri506",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video", "duration_s": 5.0}],
        guided_capability_enabled=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "proposed_text"),
    [("unresolved", None), ("candidate", "One day, one city"), ("delegated", "One day, one city")],
)
async def test_controller_copy_questions_ignore_budget_and_never_create_active_plan(
    monkeypatch, status, proposed_text
) -> None:
    manifest = _copy_manifest()
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
        question_budget=0,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )
    response = SimpleNamespace(status="briefing")
    append_event = AsyncMock()
    decision = CreativeCopyDecision(
        target="opening_title",
        status=status,
        proposed_text=proposed_text,
        language="en",
    )
    model_output = SimpleNamespace(
        action=AskUser(
            kind="ask_user",
            question="Generate a title?",
            reason_code="creative_copy",
            options=["Revise it", "Use this wording"],
        ),
        creative_decision=decision,
        brief_updates=[],
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
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", AsyncMock(return_value=model_output))
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=1,
        user_message="Add an opening hook",
    )

    assert result is response
    assert session.status == "briefing"
    assert session.active_plan is None
    payload = append_event.await_args.kwargs["payload"]
    question = payload["choice_question"]
    assert question["kind"] == (
        "creative_copy_wording" if proposed_text else "creative_copy_authorship"
    )
    if proposed_text:
        assert {option["key"] for option in question["options"]} >= {
            "approve",
            "revise",
            "cancel",
        }
        # Server keys, rather than the model's deliberately reversed labels, define the card.
        approve = next(option for option in question["options"] if option["key"] == "approve")
        assert approve["label"] == "Use this wording"


def test_legacy_copy_selection_uses_exact_displayed_label_and_server_key() -> None:
    question = {
        "question_id": "q1",
        "conflict": "creative_copy",
        "kind": "creative_copy_wording",
        "creative_target": "opening_title",
        "options": [
            {"key": "approve", "label": "Use this wording"},
            {"key": "revise", "label": "Revise it"},
            {"key": "cancel", "label": "Skip it"},
        ],
    }
    event = SimpleNamespace(role="assistant", sequence=1, payload={"choice_question": question})

    assert creator_routes._legacy_copy_selection([event], "Use this wording") == {
        "question_id": "q1",
        "option_key": "approve",
    }


def test_legacy_copy_selection_uses_fixed_server_labels_for_turkish() -> None:
    question = {
        "question_id": "q-tr",
        "conflict": "creative_copy",
        "kind": "creative_copy_wording",
        "creative_target": "opening_title",
        "options": [
            {"key": "approve", "label": "Bu ifadeyi kullan"},
            {"key": "revise", "label": "Düzenle"},
            {"key": "cancel", "label": "Atla"},
        ],
    }
    event = SimpleNamespace(role="assistant", sequence=1, payload={"choice_question": question})

    assert creator_routes._legacy_copy_selection([event], "Bu ifadeyi kullan") == {
        "question_id": "q-tr",
        "option_key": "approve",
    }
