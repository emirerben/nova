"""KRI-739: the lead line rides the chat event only when an editor turn bound receipts."""

from __future__ import annotations

from app.tasks.kria_runtime import _receipt_lead_fields

RECEIPTS = [{"requirement_id": "r1", "status": "met", "verification": "checked"}]


def test_a_bound_editor_turn_carries_the_lead_and_what_else_changed() -> None:
    assert _receipt_lead_fields("Done.", ["Retimed the title (0–4.3s → 3–7.3s)"], RECEIPTS) == {
        "reply_lead": "Done.",
        "also_changed": ["Retimed the title (0–4.3s → 3–7.3s)"],
    }


def test_nothing_else_changed_omits_the_list() -> None:
    assert _receipt_lead_fields("Done.", [], RECEIPTS) == {"reply_lead": "Done."}


def test_every_other_event_stays_byte_identical() -> None:
    assert _receipt_lead_fields(None, [], RECEIPTS) == {}
    assert _receipt_lead_fields("", ["x"], RECEIPTS) == {}
    # A lead with no receipts under it would show a line about rows that are not there.
    assert _receipt_lead_fields("Done.", ["x"], []) == {}
