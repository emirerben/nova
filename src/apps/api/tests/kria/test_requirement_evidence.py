"""KRI-459: receipts separate evidence from a fulfilled request."""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_judged,
    plan_facts_from_editor_payload,
    reply_from_receipts,
)
from app.kria.contracts import RequirementReceipt


def _req(*, scope: str = "title", literal: str | None = "Hello") -> BriefRequirement:
    return BriefRequirement(id="r1", kind="text", scope=scope, literal=literal)


def test_receipt_evidence_fields_are_additive_and_legacy_payloads_still_validate() -> None:
    receipt = RequirementReceipt(
        requirement_id="r1",
        status="met",
        verification="checked",
        stage="checked",
        target_media_ids=["clip-1"],
    )
    assert receipt.model_dump(mode="json")["verification"] == "checked"
    legacy = RequirementReceipt.model_validate({"requirement_id": "r1", "status": "met"})
    assert legacy.verification is None and legacy.stage is None and legacy.target_media_ids == []


def test_unchecked_receipts_are_opt_in_and_are_never_judged() -> None:
    req = _req(scope="global", literal=None)
    facts = PlanFacts()

    assert build_receipts([req], facts) == []
    [unchecked] = build_receipts([req], facts, include_unchecked=True)
    assert unchecked.status == "partial"
    assert unchecked.verification == "unchecked"
    assert unchecked.stage == "understood"
    assert not is_judged(req, unchecked)


def test_bound_receipts_mark_determinate_evidence_checked() -> None:
    req = _req()
    [receipt] = build_receipts([req], PlanFacts(title="Hello"), include_unchecked=True)
    assert receipt.status == "met"
    assert receipt.verification == "checked"
    assert receipt.stage == "checked"
    assert is_judged(req, receipt)


def test_unchecked_receipt_suppresses_generic_success_summary() -> None:
    req = _req(scope="global", literal=None)
    brief = CreativeBrief(version=1, requirements=[req])
    [unchecked] = build_receipts([req], PlanFacts(), include_unchecked=True)

    reply = reply_from_receipts(brief, [unchecked], summary="Everything is ready.")
    assert "Everything is ready" not in reply
    assert "couldn't verify" in reply.casefold()
    assert req.text() in reply


def test_clip_scoped_caption_requires_its_own_target_evidence() -> None:
    req = _req(scope="clip:target", literal="Exact caption")
    # An unrelated editor text element contains the exact words, but it cannot
    # establish that the target clip received them.
    [receipt] = build_receipts(
        [req],
        PlanFacts(editor=True, texts=("Exact caption",), has_clip_structure=False),
        include_unchecked=True,
    )
    assert receipt.status == "partial"
    assert receipt.verification == "unchecked"
    assert receipt.target_media_ids == ["target"]
    assert not is_judged(req, receipt)


def test_editor_metadata_cannot_supply_visible_text_evidence() -> None:
    req = _req()
    facts = plan_facts_from_editor_payload(
        {
            "source_request": "Put Hello on screen",
            "output_path": "renders/Hello.mp4",
            "metadata": {"creator_note": "Hello"},
            "text_elements": [{"text": "Different visible text"}],
        }
    )
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    assert receipt.status == "partial"
    assert receipt.verification == "checked"
