"""KRI-805: the creator's answer to Kria's own label question records on the first attempt.

Prod thread F4083F1D (2026-10-21, Main Creator v64): Kria asked for a printable label for
r2 (`[text/per_clip] on the sauce bowls ("chimichurri + salsa criolla")`) and the creator
answered "chimichurri & salsa criolla". Three of four Main Creator attempts across turns
3424b400 and e5022b5e failed `sensitive_agent_error:brief_updates_invalid`; one turn ended
in "I couldn't turn that into a plan this time". The raw output is redacted, so these tests
push every plausible shape of the needed `change` through the real path: provider text ->
MainCreatorAgent.run (parse + schema retries) -> plan_live_turn -> apply_updates.

Failure modes, written before the fix:
- F1 `expected_version` missing, null or garbled: the contract shows only "Creative brief
  v1", but the prompt asks for the version "from the contract". The server owns that token.
- F2 a wrong integer version (KRI-611 already binds it; must stay bound).
- F3 a partial change (target plus only the changed field): omitted fields keep the target's.
- F4 a remove padded with null/empty fields beside its target.
- F5 `facts: null`.
- F6 the target copied as the contract renders it ("[r2]", "R2").
- F7 operation "update"/"replace"/"edit"/"modify" (or "delete") naming a target.
- F8 the retry was told only "brief_updates contains an invalid entry", so it repeated itself.
Still refused (never guess, never apply stale work):
- U1 two mutations of one target; U2 a missing, unknown or superseded target; U3 a remove
  carrying real content; U4 an add naming a target; U5 a change naming nothing to change;
  U6 a brief that moved on after the prompt was shown; U7 an unbound change reaching apply.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelInvocation, SchemaError
from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import (
    BriefRequirement,
    BriefUpdate,
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
    render_brief_request,
)
from app.kria.planner import plan_live_turn
from tests.kria.test_creative_brief import _ASK, _MANIFEST, _wire_planner

ANSWER = "chimichurri & salsa criolla"


def _prod_brief() -> CreativeBrief:
    """Thread F4083F1D's brief v1, as the failing turn's contract listed it."""
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal="Lunch at Don Julio 🇦🇷"),
            BriefRequirement(
                id="r2",
                kind="text",
                scope="per_clip",
                literal="chimichurri + salsa criolla",
                description="on the sauce bowls",
            ),
            BriefRequirement(
                id="r3",
                kind="order",
                scope="global",
                description=(
                    "Sequence: waiter, sauce bowls, menus, cheers, ending on friend in red cap"
                ),
                facts={
                    "closing": "friend in the red cap smiling at the camera",
                    "first_clip": "waiter smiling at the camera",
                },
            ),
            BriefRequirement(
                id="r4",
                kind="select",
                scope="global",
                description="Skip the blurry shot of the empty table",
            ),
            BriefRequirement(
                id="r5",
                kind="timing",
                scope="global",
                description="Make it 15 seconds",
                facts={"duration_s": 15},
            ),
            BriefRequirement(
                id="r6",
                kind="audio",
                scope="global",
                description="Keep waiter's name exchange audible",
                facts={
                    "audio_intent": "source_unchanged",
                    "triggers": ["What's your name?", "Lucas."],
                },
            ),
            BriefRequirement(
                id="r7",
                kind="audio",
                scope="global",
                description='Keep that "Cheers!" audible too',
                facts={"audio_intent": "source_unchanged", "triggers": ["Cheers!"]},
            ),
            BriefRequirement(
                id="r8",
                kind="audio",
                scope="global",
                description="Suggest an upbeat tango song for it",
            ),
        ],
    )


_FULL = {
    "operation": "change",
    "target_requirement_id": "r2",
    "expected_version": 1,
    "kind": "text",
    "scope": "per_clip",
    "literal": ANSWER,
    "description": "on the sauce bowls",
    "facts": {},
}
_ADD_LABEL = {
    "operation": "add",
    "kind": "text",
    "scope": "per_clip",
    "literal": ANSWER,
    "description": "on the sauce bowls",
    "facts": {},
}
_NULL_PADDING = {"kind": None, "scope": None, "literal": None, "description": None}


def _without(entry: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key not in keys}


# Every way the Main Creator can say "r2 now reads <answer>" that a reader can map to one
# meaning. On main, all but the first two fail before the brief is touched.
REPAIRABLE: dict[str, list[dict[str, Any]]] = {
    "complete change": [_FULL],
    "F2 wrong version": [{**_FULL, "expected_version": 0}],
    "F1 no version": [_without(_FULL, "expected_version")],
    "F1 null version": [{**_FULL, "expected_version": None}],
    "F1 version as rendered": [{**_FULL, "expected_version": "v1"}],
    "F3 partial change": [
        {
            "operation": "change",
            "target_requirement_id": "r2",
            "expected_version": 1,
            "literal": ANSWER,
        }
    ],
    "F3 partial change, no version": [
        {"operation": "change", "target_requirement_id": "r2", "literal": ANSWER}
    ],
    "F3 partial change, null padded": [
        {"operation": "change", "target_requirement_id": "r2", **_NULL_PADDING, "literal": ANSWER}
    ],
    "F4 null-padded remove + add": [
        {"operation": "remove", "target_requirement_id": "r2", **_NULL_PADDING, "facts": {}},
        _ADD_LABEL,
    ],
    "F4+F5 remove padded with null facts + add": [
        {"operation": "remove", "target_requirement_id": "r2", **_NULL_PADDING, "facts": None},
        _ADD_LABEL,
    ],
    "F5 null facts": [{**_FULL, "facts": None}],
    "F6 target as rendered": [{**_FULL, "target_requirement_id": "[r2]"}],
    "F6 upper-case target": [{**_FULL, "target_requirement_id": "R2"}],
    "F7 update": [{**_FULL, "operation": "update"}],
    "F7 replace, no version": [_without({**_FULL, "operation": "replace"}, "expected_version")],
}

# Batches whose meaning a reader cannot pin down: the brief must stay exactly as it was.
UNSAFE: dict[str, list[dict[str, Any]]] = {
    "U1 two changes of r2": [_FULL, {**_FULL, "description": "on the two bowls"}],
    "U2 unknown target": [{**_FULL, "target_requirement_id": "r99"}],
    "U2 no target": [_without(_FULL, "target_requirement_id")],
    "U3 remove carrying content": [
        {"operation": "remove", "target_requirement_id": "r2", "literal": ANSWER}
    ],
    "U4 add naming a target": [{**_FULL, "operation": "add"}],
    "U5 change naming nothing": [{"operation": "change", "target_requirement_id": "r2"}],
    "U5 complete change with no words": [{**_FULL, "literal": None, "description": None}],
    "unknown operation": [{**_FULL, "operation": "merge"}],
}


class _ScriptedGemini:
    """Provider transport only: answers every Main Creator call with the same text."""

    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_: Any) -> ModelInvocation:
        self.prompts.append(prompt)
        return ModelInvocation(raw_text=self.raw_text, tokens_in=1, tokens_out=1)


async def _answer_label_question(monkeypatch: pytest.MonkeyPatch, brief_updates: list[dict]):  # noqa: ANN202
    raw = json.dumps({"action": _ASK, "brief_updates": brief_updates}, ensure_ascii=False)
    db, item, creator_id, _copilot, _runs = _wire_planner(
        monkeypatch, output=None, editor_plan=None, snapshot=None
    )
    item.current_job_id = None  # nothing rendered yet: the Main Creator reads the brief
    gemini = _ScriptedGemini(raw)
    monkeypatch.setattr(settings, "ai_cost_control_enabled", False)
    monkeypatch.setattr(planner, "MainCreatorAgent", MainCreatorAgent)
    monkeypatch.setattr(planner, "default_client", lambda: gemini)
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=_prod_brief()))
    result = await plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item.id,
        creator_id=creator_id,
        user_message=ANSWER,
    )
    return result, gemini


def _labels(brief: CreativeBrief) -> list[str | None]:
    return [req.literal for req in brief.live() if req.kind == "text" and req.scope == "per_clip"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(REPAIRABLE), ids=list(REPAIRABLE))
async def test_answer_to_kria_label_question_records_on_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    result, gemini = await _answer_label_question(monkeypatch, REPAIRABLE[shape])

    assert len(gemini.prompts) == 1, "the first Main Creator attempt must be accepted"
    assert "Creative brief v1" in gemini.prompts[0]
    assert result.brief_coverage is None or "reason" not in result.brief_coverage
    # Bound to the version the prompt showed, so the persist fence can still refuse stale work.
    assert result.brief_expected_version == 1
    assert {u.expected_version for u in result.brief_updates if u.operation != "add"} <= {1}
    after = apply_updates(_prod_brief(), result.brief_updates, source_turn_id="t")
    assert _labels(after) == [ANSWER]
    label = next(req for req in after.live() if req.literal == ANSWER)
    assert (label.kind, label.scope, label.description) == (
        "text",
        "per_clip",
        "on the sauce bowls",
    )
    # The other seven requirements are untouched.
    before = {req.id: req for req in _prod_brief().live() if req.id != "r2"}
    assert {req.id: req for req in after.live() if req.id in before} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(UNSAFE), ids=list(UNSAFE))
async def test_an_ambiguous_brief_batch_still_leaves_the_brief_unchanged(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    result, _gemini = await _answer_label_question(monkeypatch, UNSAFE[shape])

    assert result.brief_coverage["reason"] in {
        "request_extraction_failed",
        "creator_planning_failed",
    }
    assert result.brief_updates == ()
    assert _labels(_prod_brief()) == ["chimichurri + salsa criolla"]


def test_the_retry_is_told_which_entry_and_rule_failed_without_creator_words() -> None:
    """F8: prod's retry got "brief_updates contains an invalid entry" and repeated itself."""
    agent = MainCreatorAgent(object())
    agent_input = MainCreatorInput(
        user_message=ANSWER,
        creator_request=render_brief_request(_prod_brief(), latest_message=ANSWER),
        capability_manifest=_MANIFEST,
        brief_enabled=True,
    )
    raw = json.dumps(
        {"action": _ASK, "brief_updates": [_ADD_LABEL, *UNSAFE["U3 remove carrying content"]]}
    )
    with pytest.raises(SchemaError) as excinfo:
        agent.parse(raw, agent_input)

    assert excinfo.value.error_class == "brief_updates_invalid"  # type: ignore[attr-defined]
    feedback = agent.schema_clarification()
    assert "brief_updates[1]" in feedback
    assert "remove" in feedback and "target_requirement_id" in feedback
    assert "chimichurri" not in feedback and "chimichurri" not in str(excinfo.value)


def test_a_bound_change_still_refuses_a_brief_that_moved_on() -> None:
    """U6/U7: binding is to the brief the prompt showed, never to the latest ledger."""
    agent = MainCreatorAgent(object())
    agent_input = MainCreatorInput(
        user_message=ANSWER,
        creator_request=render_brief_request(_prod_brief(), latest_message=ANSWER),
        capability_manifest=_MANIFEST,
        brief_enabled=True,
    )
    raw = json.dumps({"action": _ASK, "brief_updates": REPAIRABLE["F1 no version"]})
    unbound = agent.parse(raw, agent_input).brief_updates
    with pytest.raises(BriefUpdateBatchError, match="exact current brief version"):
        apply_updates(_prod_brief(), unbound, source_turn_id="t")

    bound = [
        update.model_copy(update={"expected_version": 1}) if update.operation != "add" else update
        for update in unbound
    ]
    moved_on = _prod_brief().model_copy(update={"version": 2})
    with pytest.raises(BriefUpdateBatchError, match="exact current brief version"):
        apply_updates(moved_on, bound, source_turn_id="t")


@pytest.mark.parametrize(
    "shape", ["F3 partial change, no version", "F6 target as rendered", "F7 update"]
)
def test_the_brief_extractor_completes_and_binds_the_same_shapes(shape: str) -> None:
    """The extractor shares the parser; what it hands the planner is complete and bound."""
    result = BriefExtractorAgent(None).parse(
        json.dumps({"brief_updates": REPAIRABLE[shape]}, ensure_ascii=False),
        BriefExtractionInput(user_message=ANSWER, current_brief=_prod_brief()),
    )
    assert [
        (u.operation, u.target_requirement_id, u.expected_version, u.kind, u.scope)
        for u in result.brief_updates
    ] == [("change", "r2", 1, "text", "per_clip")]
    assert result.brief_updates[0].description == "on the sauce bowls"
    after = apply_updates(_prod_brief(), result.brief_updates, source_turn_id="t")
    assert _labels(after) == [ANSWER]


def test_a_complete_change_still_replaces_every_field() -> None:
    """R1: a change that names kind and scope keeps its replace semantics."""
    brief = _prod_brief()
    update = BriefUpdate(
        operation="change",
        target_requirement_id="r6",
        expected_version=1,
        kind="audio",
        scope="global",
        description="Keep the name exchange audible",
        facts={"audio_intent": "source_unchanged"},
    )
    after = {req.id: req for req in apply_updates(brief, [update], source_turn_id="t").live()}
    assert after["r6"].facts == {"audio_intent": "source_unchanged"}
    assert after["r6"].description == "Keep the name exchange audible"
