"""Wire acceptance specified before v2 implementation; no network or executor."""

import json
from pathlib import Path

import pytest

from app.agents._creator_wire import WireContract, copilot_schema, main_creator_schema
from app.services.creator_model_comparison import schema_audit

ROOT = Path(__file__).parents[1] / "fixtures/agent_evals"


@pytest.mark.parametrize(
    "path", sorted((ROOT / "main_creator").rglob("*.json")), ids=lambda p: p.stem
)
def test_main_recorded_response_roundtrip(path):
    from app.agents.main_creator import MainCreatorAgent

    fixture = json.loads(path.read_text())
    data = json.loads(fixture["raw_text"])
    contract = WireContract(main_creator_schema())
    # Fixtures deliberately exercise parser repairs; wire accepts only already
    # normalized model-owned values, not invalid historical provider bytes.
    agent = MainCreatorAgent(None)
    parsed = agent.parse(json.dumps(data), agent.Input.model_validate(fixture["input"]))
    model_owned = contract.project(parsed.model_dump(mode="json"))
    encoded = contract.encode(model_owned)
    assert contract.decode(encoded) == model_owned
    reparsed = agent.parse(
        json.dumps(contract.decode(encoded)), agent.Input.model_validate(fixture["input"])
    )
    assert reparsed.model_dump(mode="json") == parsed.model_dump(mode="json")


def test_schemas_fit_provider_structural_limits_and_preserve_hidden_model_fields():
    main = WireContract(main_creator_schema())
    for model in ("gpt-6.1-sol", "claude-sonnet-5-5", "gemini-3.1-pro-preview"):
        audit = schema_audit(main.provider_schema(model), model)
        assert audit["compatible"], audit["blockers"]
    strategy = main.source["$defs"]["CreativeStrategy"]["properties"]
    assert {"clip_intents", "reaction_beats", "voice_mode", "pinned_texts", "song_sync"} <= set(
        strategy
    )
    assert "resolved_clip_intents" not in strategy


def test_copilot_closed_ops_and_omission_semantics():
    contract = WireContract(copilot_schema())
    value = {
        "intent": "edit",
        "reply": "Prepared",
        "ops": [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 2.5}],
    }
    assert contract.decode(contract.encode(value)) == value
    with pytest.raises(ValueError):
        contract.encode({**value, "ops": [{"op": "invent_op", "private": "secret"}]})
    with pytest.raises(ValueError):
        contract.encode(
            {**value, "ops": [{"op": "set_clip_duration", "slot_index": "zero", "duration_s": 2.5}]}
        )


@pytest.mark.parametrize(
    "path", sorted((ROOT / "edit_copilot").rglob("*.json")), ids=lambda p: p.stem
)
def test_copilot_recorded_response_keeps_parsed_intent_ops_and_reply(path):
    from app.agents.edit_copilot import EditCopilotAgent

    fixture = json.loads(path.read_text())
    agent = EditCopilotAgent(None)
    data = agent.Input.model_validate(fixture["input"])
    before = agent.parse(fixture["raw_text"], data)
    contract = WireContract(copilot_schema())
    projected = contract.project(json.loads(fixture["raw_text"]))
    after = EditCopilotAgent(None).parse(
        json.dumps(contract.decode(contract.encode(projected))), data
    )
    assert after.ops == before.ops
    assert after.intent == before.intent


def test_explicit_null_and_omission_remain_distinct():
    from app.agents._creator_wire import obj

    source = obj({"keep_or_clear": {"anyOf": [{"type": "string"}, {"type": "null"}]}})
    contract = WireContract(source)
    for value in ({}, {"keep_or_clear": None}, {"keep_or_clear": "creator copy"}):
        assert contract.decode(contract.encode(value)) == value


def test_anthropic_projection_has_no_reference_cycles_and_declares_depth_bound():
    from jsonschema import Draft202012Validator

    from app.agents._creator_wire import _json_defs, obj

    source = obj({"data": {"$ref": "#/$defs/NonNullJSONValue"}})
    source["$defs"] = _json_defs()
    contract = WireContract(source)
    shallow = contract.encode({"data": {"effect": {"amount": 0.5}}})
    deep = "value"
    for _ in range(20):
        deep = {"nested": deep}
    encoded_deep = contract.encode({"data": deep})
    anthro = contract.provider_schema("claude-sonnet-5-5")
    assert schema_audit(anthro, "claude-sonnet-5-5")["compatible"]
    assert Draft202012Validator(anthro).is_valid(shallow)
    assert not Draft202012Validator(anthro).is_valid(encoded_deep)
    assert Draft202012Validator(contract.provider_schema("gpt-6.1-sol")).is_valid(encoded_deep)
    recursive = contract.provider_schema("gpt-6.1-sol")
    audit = schema_audit(recursive, "claude-sonnet-5-5")
    assert not audit["compatible"]
    assert any("circular_reference" in v for v in audit["blockers"])
