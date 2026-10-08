"""Failure modes for the narrow rendered-edit brief extractor."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import RunContext, SchemaError
from app.agents.brief_extractor import (
    BriefExtractionInput,
    BriefExtractionOutput,
    BriefExtractorAgent,
)
from tests.agents.conftest import MockModelClient


def _input() -> BriefExtractionInput:
    return BriefExtractionInput(
        creator_request=(
            "Creative brief v0 (everything the creator has asked for, still in force):\n(none)"
        ),
        user_message="Add a new title “Lisbon”. Animate it",
        conversation=[{"role": "user", "content": "Add a new title “Lisbon”. Animate it"}],
    )


def test_output_is_brief_only_and_validates_existing_updates() -> None:
    output = BriefExtractorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(
            {
                "brief_updates": [
                    {
                        "operation": "add",
                        "kind": "text",
                        "scope": "title",
                        "literal": "Lisbon",
                        "description": None,
                        "facts": {},
                    }
                ]
            }
        ),
        _input(),
    )
    assert isinstance(output, BriefExtractionOutput)
    assert output.brief_updates[0].literal == "Lisbon"


def test_action_envelope_is_rejected_instead_of_being_treated_as_extraction() -> None:
    with pytest.raises(SchemaError):
        BriefExtractorAgent(None).parse(
            json.dumps({"action": {"kind": "ask_user", "question": "What next?"}}),
            _input(),
        )


def test_required_scope_rejects_missing_invalid_and_ambiguous_scope_payloads() -> None:
    agent = BriefExtractorAgent(None)  # type: ignore[arg-type]
    required = _input().model_copy(update={"require_request_scope": True})
    for payload in (
        {"brief_updates": []},
        {"brief_updates": [], "request_scope": "retry"},
        {"brief_updates": [], "request_scope": "clarify"},
    ):
        with pytest.raises(SchemaError):
            agent.parse(json.dumps(payload), required)


def test_scope_is_optional_for_legacy_direct_callers() -> None:
    output = BriefExtractorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps({"brief_updates": []}), _input()
    )
    assert output.request_scope is None


def test_required_scope_accepts_edit_rebuild_and_clarification() -> None:
    agent = BriefExtractorAgent(None)  # type: ignore[arg-type]
    required = _input().model_copy(update={"require_request_scope": True})
    edit = agent.parse(json.dumps({"brief_updates": [], "request_scope": "edit"}), required)
    rebuild = agent.parse(json.dumps({"brief_updates": [], "request_scope": "rebuild"}), required)
    clarify = agent.parse(
        json.dumps(
            {
                "brief_updates": [],
                "request_scope": "clarify",
                "clarification": "Should I revise the title or remake the video?",
            }
        ),
        required,
    )
    assert (edit.request_scope, rebuild.request_scope, clarify.clarification) == (
        "edit",
        "rebuild",
        "Should I revise the title or remake the video?",
    )


def test_prompt_contains_only_brief_contract_and_current_request() -> None:
    prompt = BriefExtractorAgent(None).render_prompt(_input())  # type: ignore[arg-type]
    assert "brief_updates" in prompt
    assert "Lisbon" in prompt
    assert "SERVER CAPABILITY MANIFEST" not in prompt
    assert "target_duration_s" not in prompt
    assert "`action`" not in prompt
    assert "propose a full strategy" not in prompt


def test_runtime_context_has_pre_render_owner_fields() -> None:
    context = RunContext(
        request_id="thread",
        plan_item_id="item",
        creator_agent_session_id="session",
        creator_id="creator",
    )
    assert context.plan_item_id == "item"
    assert context.creator_agent_session_id == "session"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Add a title “Lisbon” under the text. Anymate typewrite to all texts but "
            "show lisbon after the current text finished animating",
            ("Lisbon", "style", "timing"),
        ),
        ("Add a new title “Lisbon”. Animate it", ("Lisbon", "style")),
    ],
)
def test_original_rendered_followups_replay_through_brief_only_schema(
    message: str, expected: tuple[str, ...]
) -> None:
    client = MockModelClient()
    updates = [
        {
            "operation": "add",
            "kind": "text",
            "scope": "title",
            "literal": "Lisbon",
            "description": None,
            "facts": {},
        }
    ]
    if "style" in expected:
        updates.append(
            {
                "operation": "add",
                "kind": "style",
                "scope": "title",
                "literal": None,
                "description": "typewrite animation",
                "facts": {},
            }
        )
    if "timing" in expected:
        updates.append(
            {
                "operation": "add",
                "kind": "timing",
                "scope": "title",
                "literal": None,
                "description": "after the current text finished animating",
                "facts": {},
            }
        )
    client.queue(
        BriefExtractorAgent.spec.model,
        {"brief_updates": updates},
    )
    context = (
        "Creative brief v1 (everything the creator has asked for, still in force):\n"
        "text/title: “Good Morning from the Erbens”\n"
        "text/per_clip: the generic morning clips\n"
        "Current on-screen title: Good Morning from the Erbens\n"
        "References: four generic morning clips, each 3.75s; total cut 15s"
    )
    output = BriefExtractorAgent(client).run(
        BriefExtractionInput(
            user_message=message,
            creator_request=context,
            conversation=[{"role": "user", "content": message}],
        ),
        ctx=RunContext(
            request_id="thread",
            plan_item_id="item",
            creator_agent_session_id="session",
            creator_id="creator",
        ),
    )
    assert [update.kind for update in output.brief_updates] == [
        "text" if value == "Lisbon" else value for value in expected
    ]
    assert output.brief_updates[0].literal == "Lisbon"
    assert "brief_updates" in client.invocations[0]["prompt"]
    suffix = client.invocations[0]["prompt"].split("Return only one JSON object", 1)[-1]
    assert "action" not in suffix


def test_malformed_batch_uses_narrow_agent_schema_retry_policy() -> None:
    client = MockModelClient()
    client.queue(
        BriefExtractorAgent.spec.model,
        {"brief_updates": [{"kind": "not-a-kind"}]},
        {"brief_updates": []},
    )
    output = BriefExtractorAgent(client).run(
        BriefExtractionInput(
            user_message="Add a title Lisbon",
            creator_request="Good Morning from the Erbens",
        )
    )
    assert output.brief_updates == []
    assert len(client.invocations) == 2
    assert "Fix this schema error" in client.invocations[1]["prompt"]
