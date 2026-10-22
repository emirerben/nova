"""Explicit nth-spoken-phrase removal stays source-bound from brief to receipt."""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    plan_facts_from_phone_variant,
)
from app.kria.speech_cut_ask import brief_speech_cuts, requirement_speech_cut


def _req(description: str, *, facts: dict | None = None, req_id: str = "r1") -> BriefRequirement:
    return BriefRequirement(
        id=req_id, kind="timing", scope="global", description=description, facts=facts or {}
    )


def _variant(rows: list[dict] | None = None, **overrides: object) -> dict:
    return {
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "caption_cues": [],
        "explicit_speech_cuts": rows or [],
    } | overrides


def test_structured_phrase_removal_is_preferred() -> None:
    req = _req(
        "leave the captions alone",
        facts={"speech_remove": {"quote": "Neredesin", "occurrence": 2}},
    )
    assert requirement_speech_cut(req) == type(requirement_speech_cut(req))(
        quote="Neredesin", occurrence=2, requirement_ids=()
    )


@pytest.mark.parametrize(
    ("description", "quote", "occurrence"),
    [
        ("Remove only the second “Neredesin”", "Neredesin", 2),
        ("İkinci 'Neredesin' sorusunu kes, bir kere sorulsun", "Neredesin", 2),
        ("Remove “Neredesin”", "Neredesin", None),
    ],
)
def test_legacy_reader_accepts_one_safe_quote(
    description: str, quote: str, occurrence: int | None
) -> None:
    ask = requirement_speech_cut(_req(description))
    assert ask is not None
    assert (ask.quote, ask.occurrence) == (quote, occurrence)


@pytest.mark.parametrize(
    "description",
    [
        "Don't remove the second 'Neredesin'",
        "İkinci 'Neredesin'i kesme",
        "Remove the second 'Neredesin' subtitle",
        "Remove every 'Neredesin'",
        "Remove the sixth 'Neredesin'",
        "Remove the second 'Neredesin' and third 'Neredesin'",
        "Maybe remove the second 'Neredesin'",
    ],
)
def test_legacy_reader_refuses_unsafe_or_ambiguous_requests(description: str) -> None:
    assert requirement_speech_cut(_req(description)) is None


def test_malformed_structured_fact_never_falls_back_to_words() -> None:
    req = _req('Remove the second "Neredesin"', facts={"speech_remove": {"quote": "Neredesin"}})
    assert requirement_speech_cut(req) is None


def test_brief_groups_duplicate_requests_and_keeps_requirement_ids() -> None:
    brief = CreativeBrief(
        requirements=[
            _req('Remove second "Neredesin"', req_id="r1"),
            _req('Remove the 2nd "Neredesin"', req_id="r2"),
        ]
    )
    assert brief_speech_cuts(brief) == (
        type(brief_speech_cuts(brief)[0])("Neredesin", 2, ("r1", "r2")),
    )


def test_destructive_phrase_grouping_keeps_accents_and_digits_distinct() -> None:
    brief = CreativeBrief(
        requirements=[
            _req('Remove second "kâr"', req_id="r1"),
            _req('Remove second "kar"', req_id="r2"),
            _req('Remove second "three"', req_id="r3"),
            _req('Remove second "3"', req_id="r4"),
        ]
    )
    assert [(ask.quote, ask.requirement_ids) for ask in brief_speech_cuts(brief)] == [
        ("kâr", ("r1",)),
        ("kar", ("r2",)),
        ("three", ("r3",)),
        ("3", ("r4",)),
    ]


def test_start_at_quoted_line_is_never_mistaken_for_a_phrase_removal() -> None:
    brief = CreativeBrief(
        requirements=[
            _req(
                "Baştaki sessiz kısmı kes, video “Deniz'im neredesin” diye başlasın",
                req_id="r1",
            ),
            _req("İkinci “Neredesin”i kes, bir kere sorayım", req_id="r2"),
        ]
    )
    assert brief_speech_cuts(brief) == (type(brief_speech_cuts(brief)[0])("Neredesin", 2, ("r2",)),)


def test_talking_draft_promises_source_resolution_without_claiming_the_cut() -> None:
    receipt = check_requirement(_req('Remove second "Neredesin"'), PlanFacts(phone_talking=True))
    assert receipt.status == "partial"
    assert (
        receipt.reason
        == "Will locate the requested occurrence in the original speech before cutting; no guess"
    )


def test_only_matching_applied_receipt_proves_the_cut() -> None:
    req = _req('Remove second "Neredesin"')
    facts = plan_facts_from_phone_variant(
        _variant(
            [
                {
                    "requirement_ids": ["r1"],
                    "quote": "Neredesin",
                    "occurrence": 2,
                    "status": "applied",
                    "source_span": {"start_s": 4.0, "end_s": 5.0},
                    "word_indexes": [7],
                }
            ]
        )
    )
    assert check_requirement(req, facts).status == "met"


@pytest.mark.parametrize(
    "row",
    [
        None,
        {
            "requirement_ids": ["r2"],
            "quote": "Neredesin",
            "occurrence": 2,
            "status": "applied",
            "source_span": {"start_s": 4.0, "end_s": 5.0},
            "word_indexes": [7],
        },
        {
            "requirement_ids": ["r1"],
            "quote": "Neredesin",
            "occurrence": 1,
            "status": "applied",
            "source_span": {"start_s": 4.0, "end_s": 5.0},
            "word_indexes": [7],
        },
        {
            "requirement_ids": ["r1"],
            "quote": "Neredesin",
            "occurrence": 2,
            "status": "not_applied",
            "reason": "multiple_sources",
        },
    ],
)
def test_absent_or_mismatched_receipt_never_claims_the_cut(row: dict | None) -> None:
    req = _req('Remove second "Neredesin"')
    facts = plan_facts_from_phone_variant(_variant([] if row is None else [row]))
    receipt = check_requirement(req, facts)
    assert receipt.status == "not_possible"
    if row and row["status"] == "not_applied":
        assert receipt.reason == "Choose one speaker clip before I cut that phrase"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("ambiguous_phrase_occurrence", "which occurrence to cut"),
        ("missing_target", "couldn't find that spoken phrase"),
        ("uncertain_timestamps", "couldn't confidently place"),
        ("multiple_sources", "Choose one speaker clip"),
        ("too_short", "too short to cut"),
        ("internal_worker_detail", "Couldn't verify"),
    ],
)
def test_speech_cut_worker_codes_never_reach_creator(reason: str, expected: str) -> None:
    req = _req('Remove second "Neredesin"')
    facts = plan_facts_from_phone_variant(
        _variant(
            [
                {
                    "requirement_ids": ["r1"],
                    "quote": "Neredesin",
                    "occurrence": 2,
                    "status": "not_applied",
                    "reason": reason,
                }
            ]
        )
    )
    receipt = check_requirement(req, facts)
    assert receipt.status == "not_possible"
    assert expected in (receipt.reason or "")
    assert reason not in (receipt.reason or "")


def test_bound_receipt_is_judged_after_render() -> None:
    req = _req('Remove second "Neredesin"')
    facts = plan_facts_from_phone_variant(_variant())
    receipts = build_receipts([req], facts, include_unchecked=True)
    assert receipts[0].status == "not_possible"
    assert receipts[0].verification == "checked"


def _noise_req() -> BriefRequirement:
    return BriefRequirement(
        id="noise-r1",
        kind="audio",
        scope="global",
        description="Reduce the engine noise so my voice is clearer",
    )


def test_noise_reduction_draft_is_planned_only_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.kria.brief_checks.engine_noise_reduction_available", lambda: True)
    receipt = check_requirement(_noise_req(), PlanFacts(phone_talking=True))
    assert receipt.status == "partial"
    assert "not independently isolated" in (receipt.reason or "")


def test_noise_reduction_draft_refuses_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.kria.brief_checks.engine_noise_reduction_available", lambda: False)
    receipt = check_requirement(_noise_req(), PlanFacts(phone_talking=True))
    assert receipt.status == "not_possible"
    assert "isn't available" in (receipt.reason or "")


def test_noise_reduction_refuses_unsupported_format() -> None:
    receipt = check_requirement(_noise_req(), PlanFacts())
    assert receipt.status == "not_possible"
    assert "single-source Talking" in (receipt.reason or "")


def test_noise_reduction_applied_stays_partial_and_requires_matching_id() -> None:
    variant = _variant(
        engine_noise_reduction={
            "version": 1,
            "requirement_ids": ["noise-r1"],
            "status": "applied",
            "method": "stationary_noise_reduction",
            "reason": "reduced_not_isolated",
        }
    )
    receipt = check_requirement(_noise_req(), plan_facts_from_phone_variant(variant))
    assert receipt.status == "partial"
    assert "not independently isolated" in (receipt.reason or "")


def test_noise_reduction_missing_or_wrong_receipt_is_not_possible() -> None:
    variant = _variant(
        engine_noise_reduction={
            "version": 1,
            "requirement_ids": ["another-requirement"],
            "status": "not_applied",
            "method": "stationary_noise_reduction",
            "reason": "multiple_sources",
        }
    )
    receipt = check_requirement(_noise_req(), plan_facts_from_phone_variant(variant))
    assert receipt.status == "not_possible"


@pytest.mark.parametrize(
    "description",
    [
        'Remove the second title "Neredesin"',
        'Delete "Neredesin" from captions',
        'İkinci "Neredesin" başlığını sil',
    ],
)
def test_on_screen_text_never_authorizes_spoken_cut(description):
    req = BriefRequirement(id="r1", kind="timing", scope="global", description=description)
    assert requirement_speech_cut(req) is None


def test_structured_text_request_cannot_authorize_speech_cut():
    req = BriefRequirement(
        id="r1",
        kind="text",
        scope="global",
        facts={"speech_remove": {"quote": "Neredesin", "occurrence": 2}},
    )
    assert requirement_speech_cut(req) is None
