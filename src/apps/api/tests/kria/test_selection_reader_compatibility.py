"""Additive readers preserve approvals that do not use new contracts."""

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.approval_readiness import assess_readiness
from app.schemas.story_intents import SelectionReconciliation
from app.services import creator_render_contract as contracts


def test_new_contract_readers_leave_legacy_capability_digest_unchanged(monkeypatch):
    legacy_matrix = {
        path: rule
        for path, rule in contracts.FIELD_MATRIX.items()
        if not path.startswith("selection_reconciliation")
        and path
        not in {
            "selection_reconciliation",
            "clip_intents[].source_kind",
            "resolved_clip_intents[].source_kind",
        }
    }
    legacy = CreativeStrategy().model_dump(mode="json")
    assert "selection_reconciliation" not in legacy
    actual = assess_readiness(None, legacy, media={}, manifest=None)["capabilities_digest"]
    monkeypatch.setattr(contracts, "FIELD_MATRIX", legacy_matrix)
    expected = assess_readiness(None, legacy, media={}, manifest=None)["capabilities_digest"]
    assert actual == expected


def test_used_contracts_are_included_in_capability_digest():
    legacy = CreativeStrategy().model_dump(mode="json")
    baseline = assess_readiness(None, legacy, media={}, manifest=None)["capabilities_digest"]
    constrained = {
        **legacy,
        "clip_intents": [
            {
                "intent_id": "video_scene",
                "op": "order",
                "attribute": "building",
                "source_kind": "video",
            }
        ],
    }
    assert (
        assess_readiness(None, constrained, media={}, manifest=None)["capabilities_digest"]
        != baseline
    )
    with_omission = {
        **legacy,
        "selection_reconciliation": SelectionReconciliation((), "unresolved"),
    }
    assert (
        assess_readiness(None, with_omission, media={}, manifest=None)["capabilities_digest"]
        != baseline
    )


def test_schema_matrix_walks_dataclass_references():
    paths = contracts.schema_field_paths()
    assert "selection_reconciliation.rejected_selections[].intent.source_kind" in paths
    assert "selection_reconciliation.story_refs.rows[].intent_ids[]" in paths
    assert paths == set(contracts.FIELD_MATRIX)


def test_server_reconciliation_is_not_a_model_or_tool_schema_field():
    assert "selection_reconciliation" not in CreativeStrategy.model_json_schema()["properties"]
    assert "selection_reconciliation" not in CreativeStrategy().model_dump(mode="json")
