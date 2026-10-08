"""Contextual ledger validation for the narrow brief extractor."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import RunContext, SchemaError, TerminalSchemaError
from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.kria.brief import BriefRequirement, CreativeBrief
from tests.agents.conftest import MockModelClient


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(
                id="r6",
                kind="style",
                scope="global",
                description="fade each word in sequence",
            )
        ],
    )


def _input() -> BriefExtractionInput:
    return BriefExtractionInput(
        user_message="Change the animation to fade each word in sequence",
        creator_request="Creative brief v3: [r6] [style/global] fade each word in sequence",
        current_brief=_brief(),
    )


def _change(*, expected_version: int, target: str = "r6") -> dict:
    return {
        "operation": "change",
        "target_requirement_id": target,
        "expected_version": expected_version,
        "kind": "style",
        "scope": "global",
        "literal": None,
        "description": "fade each word in sequence with entrance and exit fades",
        "facts": {},
    }


@pytest.mark.parametrize(
    "updates",
    [
        [_change(expected_version=3, target="missing")],
        [_change(expected_version=3), _change(expected_version=3)],
    ],
)
def test_context_rejects_invalid_change_batches_before_success(updates: list[dict]) -> None:
    agent = BriefExtractorAgent(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        agent.parse(
            json.dumps({"brief_updates": updates}),
            _input(),
        )


def test_context_validation_is_schema_retryable() -> None:
    client = MockModelClient()
    client.queue(
        BriefExtractorAgent.spec.model,
        {"brief_updates": [_change(expected_version=3, target="missing")]},
        {"brief_updates": [_change(expected_version=3)]},
    )
    output = BriefExtractorAgent(client).run(
        _input(),
        ctx=RunContext(
            request_id="thread",
            plan_item_id="item",
            creator_agent_session_id="session",
            creator_id="creator",
        ),
    )
    assert output.brief_updates[0].expected_version == 3
    assert len(client.invocations) == 2
    assert "target requirement is missing or superseded" in client.invocations[1]["prompt"]


def test_context_validation_exhaustion_fails_closed() -> None:
    client = MockModelClient()
    client.queue(
        BriefExtractorAgent.spec.model,
        {"brief_updates": [_change(expected_version=3, target="missing")]},
        {"brief_updates": [_change(expected_version=3, target="missing")]},
        {"brief_updates": [_change(expected_version=3, target="missing")]},
    )
    with pytest.raises(TerminalSchemaError):
        BriefExtractorAgent(client).run(_input())
    assert len(client.invocations) == 3
