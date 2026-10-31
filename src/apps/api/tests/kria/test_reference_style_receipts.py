"""A quoted style reference must not become a false failed target.

Absent typed source/destination evidence, report the actual applied changes
neutrally. Never turn a wrong target, wrong value, or partial copy into Done.
"""

import copy

import pytest

from app.kria.contracts import RequirementReceipt
from app.kria.editor_receipts import receipt_lead
from app.kria.reply_language import reply_language_for
from tests.kria.test_kri558_editor_receipts import ROWS, _patch, _req, _turn


@pytest.mark.parametrize("targets", [["b"], ["a", "b"]])
def test_quantified_style_reference_reports_applied_changes_without_false_failure(targets):
    rows = copy.deepcopy(ROWS)
    rows[0].update(font_family="Hanken Grotesk", size_px=84)
    requirement = _req(
        "style", "Use the font and size of the Lisbon text for all the other text blocks"
    )
    receipts, reply, compiled = _turn(
        [requirement],
        [_patch(targets, {"font_family": "Hanken Grotesk", "size_px": 84})],
        rows=rows,
    )
    receipt = receipts["r1"]
    assert receipt.status == "partial"
    assert receipt.verification == "unchecked"
    assert "Couldn't" not in reply
    assert "Done:" not in reply
    assert "Hanken Grotesk" in reply
    assert "Also changed" not in reply
    assert "Done" not in receipt_lead(list(receipts.values()))
    before = {row["id"]: row for row in rows}
    after = {row["id"]: row for row in compiled.payload.text_elements}
    assert before.keys() == after.keys()
    for row_id in before:
        for key in ("text", "start_s", "end_s"):
            assert after[row_id][key] == before[row_id][key]
    assert all(
        row["font_family"] == "Hanken Grotesk" and row["size_px"] == 84 for row in after.values()
    )


def test_quantified_reference_with_a_wrong_copy_never_claims_fulfillment():
    rows = copy.deepcopy(ROWS)
    rows[0].update(font_family="Hanken Grotesk", size_px=84)
    receipts, reply, _ = _turn(
        [_req("style", "Use the font and size of the Lisbon text for all the other text blocks")],
        [_patch(["b"], {"font_family": "Montserrat", "size_px": 60})],
        rows=rows,
    )
    assert receipts["r1"].status != "met"
    assert "Done:" not in reply
    assert "Montserrat" in reply


def test_single_named_target_keeps_its_strict_wrong_target_result():
    receipts, reply, _ = _turn(
        [_req("style", "Make the Lisbon text smaller")], [_patch(["b"], {"size_scale": 0.8})]
    )
    assert receipts["r1"].status == "not_possible"
    assert reply.startswith("Couldn't:")


def test_concrete_value_failure_is_not_reclassified_as_uncertain_reference():
    receipts, reply, _ = _turn(
        [_req("style", "Use Inter on all the Lisbon text blocks", font_family="Inter")],
        [_patch(["b"], {"font_family": "Montserrat"})],
    )
    assert receipts["r1"].status != "met"
    assert "Done:" not in reply


def test_uncertain_native_lead_does_not_count_unchecked_asks_as_completed():
    unknown = RequirementReceipt(
        requirement_id="unknown", status="partial", verification="unchecked"
    )
    failed = RequirementReceipt(
        requirement_id="failed", status="not_possible", verification="checked"
    )
    met = RequirementReceipt(requirement_id="met", status="met", verification="checked")
    assert receipt_lead([unknown]) == "Here’s what changed."
    assert receipt_lead([failed, unknown]) == "Here’s what changed and what I couldn’t do."
    assert receipt_lead([failed, unknown, met]) == "I did 1 of your 3 asks."
    assert receipt_lead([met]) == "Done."
    with reply_language_for("tr"):
        assert receipt_lead([unknown]) == "İşte değişiklikler."
        assert "tamamladım" not in receipt_lead([failed, unknown])
