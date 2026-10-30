"""Synthetic reader compatibility; no live replacement producer is enabled."""

from app.kria.contracts import KriaTurnPlan
from app.services.creator_render_contract import (
    FIELD_MATRIX,
    field_matrix_for_strategy,
    schema_field_paths,
)
from tests.kria.test_order_replacement import proposal


def test_private_offer_roundtrips_without_changing_legacy_wire_or_provider_schema():
    legacy = KriaTurnPlan(mode="respond", turn_value="question", response="Choose a sequence.")
    before = legacy.model_dump(mode="json")
    assert "order_replacement_proposal" not in before
    assert "order_replacement_strategy" not in before
    assert "order_replacement_media_snapshot" not in before
    properties = KriaTurnPlan.model_json_schema()["properties"]
    assert "order_replacement_proposal" not in properties
    assert "order_replacement_strategy" not in properties
    assert "order_replacement_media_snapshot" not in properties
    stored = legacy.model_copy(
        update={
            "order_replacement_proposal": proposal(),
            "order_replacement_strategy": {"story_structure": ["Retained creator copy"]},
        }
    ).model_dump(mode="json")
    loaded = KriaTurnPlan.model_validate(stored)
    assert loaded.order_replacement_proposal == proposal()
    assert loaded.order_replacement_strategy == {"story_structure": ["Retained creator copy"]}
    assert {k: v for k, v in stored.items() if not k.startswith("order_replacement_")} == before


def test_every_proof_field_is_classified_and_legacy_capabilities_omit_unused_fields():
    assert schema_field_paths() == set(FIELD_MATRIX)
    assert not any(
        p.startswith("confirmed_order_replacement") for p in field_matrix_for_strategy({})
    )
    enabled = field_matrix_for_strategy({"confirmed_order_replacement": {"version": 1}})
    assert all(p in enabled for p in FIELD_MATRIX if p.startswith("confirmed_order_replacement"))
