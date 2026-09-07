from app.services.creator_direction import CreatorDirectionSnapshot
from app.services.creator_direction_receipts import (
    project_direction_receipt,
    stamp_private_receipt,
)
from app.services.creator_direction_snapshot import serialize_private_snapshot


def test_receipt_keeps_the_rule_text_used_at_snapshot_time() -> None:
    source_item = {
        "id": "memory-1",
        "instruction": "Always use Inter",
        "normalized_key": "font_family",
        "structured_value": {"font_family": "Inter"},
        "enforcement": "constraint",
    }
    snapshot = CreatorDirectionSnapshot(enabled=True, revision=4, items=(source_item,))
    private = stamp_private_receipt(serialize_private_snapshot(snapshot, source="test"), snapshot)

    source_item["instruction"] = "Always use Playfair Display"

    receipt = project_direction_receipt(private)
    assert receipt["memory_revision"] == 4
    assert receipt["rules"][0]["instruction"] == "Always use Inter"
    assert "Playfair" not in str(receipt)


def test_project_override_replaces_account_rule_in_receipt() -> None:
    snapshot = CreatorDirectionSnapshot(
        enabled=True,
        revision=5,
        items=(
            {
                "id": "memory-1",
                "instruction": "Always use Inter",
                "normalized_key": "font_family",
                "structured_value": {"font_family": "Inter"},
                "enforcement": "constraint",
            },
        ),
        overrides=(
            {
                "id": "override-1",
                "instruction": "Use Playfair for this project",
                "normalized_key": "font_family",
                "structured_value": {"font_family": "Playfair Display"},
            },
        ),
    )

    receipt = project_direction_receipt(
        stamp_private_receipt(serialize_private_snapshot(snapshot, source="test"), snapshot)
    )

    assert receipt["applied_count"] == 1
    assert [rule["instruction"] for rule in receipt["rules"]] == ["Use Playfair for this project"]
    assert receipt["rules"][0]["overridden"] is True
    assert receipt["rules"][0]["scope_label"] == "This project only"
