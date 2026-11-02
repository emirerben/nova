"""What a model's intent review is allowed to mean, decided by the server alone.

Failure modes, written before the code (matrix row in parentheses). Each has a test below.

  The review is void, never partly trusted (9):
    not JSON, truncated, not an object, an extra key such as free text or a candidate id,
    more than 12 findings, two findings for one requirement, an unmet finding with no frame,
    a defect_kind on a verdict that is not unmet, a frame alias that was not sent, a requirement
    alias that was not issued (an audio requirement is never issued), and a defect kind that does
    not fit the requirement's kind (a wording defect on footage).
  A reply that says nothing about an issued requirement is void as well (9, 5):
    the strict JSON schema cannot force coverage, so a model that checks one requirement and goes
    quiet on the rest must never be stored as a clean pass. Every issued alias needs exactly one
    finding (`unverifiable` is the way to decline), however the omission is dressed up: one of
    three, a low-confidence defect hiding a third, or no findings at all. Voiding is checked
    last, so a reply with a more specific fault still reports that fault.
  The model cannot claim what stills cannot show (5, 21):
    a met verdict on a timing requirement is recorded as unverifiable; a review that judged
    nothing is nothing_verifiable, never "everything matches".
  Authority stays on the server (21, 22, 23, 24, 26, 27, 28, 34):
    low confidence is ignored; medium never repairs or asks; a repair needs high confidence, a
    tier that repairs, staging preconditions that held, and a candidate matching BOTH the
    requirement and the defect kind; at most three candidates; a candidate that is not
    automatic is only ever a suggestion; wrong footage is a suggestion and never a question or
    a repair; a wording defect on a draft that already carries the authority is a render
    fidelity suggestion; a wording defect with no candidate is one question; extra questions
    become suggestions and a staged repair asks nothing.
  Outcome precedence (24, 34): repair > needs_confirmation > suggestion > no_findings.
  The schema the provider sees is closed over exactly the aliases issued (21).
"""

import json

import pytest

from app.agents._schemas.intent_review import (
    DEFECT_KINDS,
    InvalidReviewOutput,
    parse_review_output,
    response_schema,
)
from app.kria.intent_review_catalog import (
    Candidate,
    FrameRef,
    IssuedRequirement,
    ReviewCatalog,
    WithheldRequirement,
)
from app.kria.intent_review_disposition import (
    RepairPreconditions,
    dispose,
    evaluate_review,
    validate_review_output,
)


def req(alias, kind="text", **fields):
    return IssuedRequirement(
        requirement_id=alias,
        brief_id=f"b{alias[1:]}",
        kind=kind,
        label=fields.pop("label", f"{kind} ask"),
        **fields,
    )


def cand(kind, aliases, defects, *, auto=True, element="title", target=None):
    return Candidate(
        candidate_id=f"{kind[:4]}-{element}",
        kind=kind,
        requirement_ids=aliases,
        defect_kinds=defects,
        element_ref=element,
        role="title",
        allowed_paths=[f"editor_payload.text_elements[0].{element}"],
        target=target or {"text": "Before"},
        auto=auto,
    )


def make_catalog(requirements, candidates=(), withheld=()):
    return ReviewCatalog(
        digest="d" * 64,
        frames=[
            FrameRef(frame_id=f"f{i}", t_ms=1000 * i, sha256=f"{i}" * 64, w=360, h=640)
            for i in range(3)
        ],
        requirements=list(requirements),
        withheld=list(withheld),
        candidates=list(candidates),
    )


def finding(alias, verdict="met", defect=None, frames=(), confidence="high"):
    return {
        "requirement_id": alias,
        "verdict": verdict,
        "defect_kind": defect,
        "evidence_frame_ids": list(frames),
        "confidence": confidence,
    }


def raw(*findings, **extra):
    return json.dumps({"schema_version": 1, "findings": list(findings), **extra})


def evaluate(
    catalog, *findings, tier="repair", preconditions=RepairPreconditions(), fill=True, **extra
):
    """Judge a reply. Unless `fill` is off, an issued alias the test did not mention is declined
    as `unverifiable`, so a test about one requirement does not trip the coverage rule."""
    said = {f["requirement_id"] for f in findings}
    declined = (
        [finding(alias, "unverifiable") for alias in catalog.requirement_ids if alias not in said]
        if fill
        else []
    )
    return evaluate_review(
        raw(*findings, *declined, **extra), catalog, tier=tier, repair_preconditions=preconditions
    )


RESTORE = cand("restore_exact_text", ["R1"], ["copy_mismatch", "text_missing"])
FIT = cand("fit_text_to_duration", ["R1"], ["text_cut_off", "text_missing"], element="end_s")
MOVE = cand("move_text_to_safe_anchor", ["R1"], ["text_obstructed"], auto=False, element="pos")


# --- the review is void ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"schema_version":1,"findings":[{"requirement_id":"R1"',
        "[]",
        json.dumps({"schema_version": 2, "findings": []}),
        raw(finding("R1"), note="all good"),
        raw({**finding("R1"), "wording": "Better title"}),
        raw({**finding("R1"), "candidate_id": "restore_exact_text"}),
        raw(*[finding(f"R{n}") for n in range(1, 14)]),
        raw(finding("R1"), finding("R1", "unverifiable")),
        raw(finding("R1", "unmet", "copy_mismatch", frames=[])),
        raw(finding("R1", "unmet", None, frames=["f0"])),
        raw(finding("R1", "met", "copy_mismatch", frames=["f0"])),
        raw(finding("R1", "unmet", "copy_mismatch", frames=["f0", "f1", "f2", "f3"])),
        raw(finding("R1", "unmet", "copy_mismatch", frames=["f0", "f0"])),
        raw(finding("R1", "unmet", "copy_mismatch", frames=["f9"])),
        raw(finding("R0")),
        raw(finding("R13")),
        raw(finding("R1", "unmet", "audio_off", frames=["f0"])),
        raw(finding("R1", "maybe")),
        raw(finding("R1", confidence="certain")),
    ],
)
def test_any_contract_violation_voids_the_whole_review(text):
    with pytest.raises(InvalidReviewOutput):
        parse_review_output(text)


def test_an_unissued_alias_voids_the_review_even_beside_valid_findings():
    catalog = make_catalog(
        [req("R1")], withheld=[WithheldRequirement(brief_id="b9", reason="audio")]
    )
    with pytest.raises(InvalidReviewOutput) as audio:
        evaluate(catalog, finding("R1"), finding("R2", "unmet", "other", frames=["f0"]))
    assert audio.value.reason == "unknown_requirement"


def test_a_frame_that_was_not_sent_voids_the_review():
    catalog = make_catalog([req("R1")])
    with pytest.raises(InvalidReviewOutput) as unseen:
        evaluate(catalog, finding("R1", "unmet", "copy_mismatch", frames=["f5"]))
    assert unseen.value.reason == "unknown_frame"


@pytest.mark.parametrize(
    "kind,defect",
    [
        ("select", "copy_mismatch"),
        ("select", "text_cut_off"),
        ("text", "wrong_content"),
        ("text", "order_mismatch"),
        ("order", "text_missing"),
        ("timing", "wrong_content"),
        ("style", "copy_mismatch"),
    ],
)
def test_a_defect_kind_that_does_not_fit_the_requirement_kind_voids_the_review(kind, defect):
    catalog = make_catalog([req("R1", kind)])
    with pytest.raises(InvalidReviewOutput) as bad:
        evaluate(catalog, finding("R1", "unmet", defect, frames=["f0"]))
    assert bad.value.reason == "incompatible_defect_kind"


@pytest.mark.parametrize(
    "said",
    [
        [finding("R1")],
        [finding("R2"), finding("R3", frames=["f0"])],
        [finding("R1"), finding("R2", "unmet", "other", ["f0"], "low")],
        [finding("R1"), finding("R3", "unverifiable")],
        [],
    ],
    ids=["one_of_three", "first_left_out", "low_unmet_hides_the_third", "middle_left_out", "empty"],
)
def test_a_reply_that_skips_an_issued_requirement_voids_the_review(said):
    catalog = make_catalog([req("R1"), req("R2"), req("R3", "select")])
    with pytest.raises(InvalidReviewOutput) as skipped:
        evaluate(catalog, *said, fill=False)
    assert skipped.value.reason == "missing_requirement"


def test_a_skipped_requirement_never_reaches_a_stored_clean_status():
    catalog = make_catalog([req("R1"), req("R2")])
    for said in ([finding("R1")], [finding("R1"), finding("R3")]):
        with pytest.raises(InvalidReviewOutput):
            evaluate(catalog, *said, fill=False)
    declined = evaluate(catalog, finding("R1"), finding("R2", "unverifiable"), fill=False)
    assert (declined.status, declined.reason) == ("no_findings", "reviewed")


def test_a_more_specific_fault_is_reported_before_the_missing_requirement():
    catalog = make_catalog([req("R1"), req("R2")])
    with pytest.raises(InvalidReviewOutput) as unseen:
        evaluate(catalog, finding("R1", "unmet", "copy_mismatch", ["f5"]), fill=False)
    assert unseen.value.reason == "unknown_frame"
    with pytest.raises(InvalidReviewOutput) as unfitting:
        evaluate(catalog, finding("R2", "unmet", "wrong_content", ["f0"]), fill=False)
    assert unfitting.value.reason == "incompatible_defect_kind"


def test_every_defect_kind_the_schema_allows_is_a_known_vocabulary_word():
    sent = response_schema(["R1"], ["f0"])["properties"]["findings"]["items"]["properties"]
    assert set(sent["defect_kind"]["enum"]) == {*DEFECT_KINDS, None}


# --- stills cannot show everything -----------------------------------------------------------


def test_a_met_verdict_on_a_timing_requirement_is_not_taken_as_verified():
    catalog = make_catalog([req("R1", "timing"), req("R2")])
    result = evaluate(catalog, finding("R1", "met", frames=["f0"]), finding("R2"))
    timing = next(f for f in result.findings if f.requirement_id == "R1")
    assert timing.verdict == "unverifiable" and timing.evidence_frame_ids == []
    assert result.status == "no_findings"


def test_a_review_that_judged_nothing_is_nothing_verifiable_never_clean():
    catalog = make_catalog([req("R1"), req("R2", "timing")])
    for findings in ([], [finding("R1", "unverifiable", confidence="low")]):
        result = evaluate(catalog, *findings)
        assert (result.status, result.reason) == ("unavailable", "nothing_verifiable")
    timing_only = evaluate(catalog, finding("R2", "met"))
    assert (timing_only.status, timing_only.reason) == ("unavailable", "nothing_verifiable")


def test_only_low_confidence_defects_and_no_met_judges_nothing():
    catalog = make_catalog([req("R1")], [RESTORE])
    result = evaluate(catalog, finding("R1", "unmet", "copy_mismatch", ["f0"], "low"))
    assert (result.status, result.reason) == ("unavailable", "nothing_verifiable")


# --- authority stays on the server -----------------------------------------------------------


def test_a_correct_edit_is_no_findings():
    catalog = make_catalog([req("R1"), req("R2", "select")], [RESTORE])
    result = evaluate(catalog, finding("R1", frames=["f0"]), finding("R2", frames=["f1"]))
    assert (result.status, result.reason) == ("no_findings", "reviewed")
    assert result.disposition.suggestions == [] and result.disposition.question is None


def test_low_confidence_is_logged_and_ignored_beside_a_met_requirement():
    catalog = make_catalog([req("R1"), req("R2")], [RESTORE])
    result = evaluate(
        catalog,
        finding("R1", "unmet", "copy_mismatch", ["f0"], "low"),
        finding("R2"),
    )
    assert result.status == "no_findings" and result.disposition.ignored == ["R1"]


def test_medium_confidence_with_an_automatic_candidate_is_only_a_suggestion():
    catalog = make_catalog([req("R1")], [RESTORE])
    result = evaluate(catalog, finding("R1", "unmet", "copy_mismatch", ["f0"], "medium"))
    assert result.status == "suggestion" and result.disposition.repair_candidate_ids == []
    assert [(s.candidate_id, s.why) for s in result.disposition.suggestions] == [
        (RESTORE.candidate_id, "medium_confidence")
    ]


def test_high_confidence_repairs_only_on_a_repair_tier_with_preconditions_held():
    catalog = make_catalog([req("R1")], [RESTORE])
    wrong = finding("R1", "unmet", "copy_mismatch", ["f0"])
    repaired = evaluate(catalog, wrong)
    assert repaired.status == "repair_staged"
    assert repaired.disposition.repair_candidate_ids == [RESTORE.candidate_id]
    assert repaired.disposition.question is None
    for tier, preconditions, why in (
        ("review", RepairPreconditions(), "tier_review"),
        ("evidence", RepairPreconditions(), "tier_review"),
        ("repair", None, "tier_review"),
        ("repair", RepairPreconditions("head_moved"), "head_moved"),
        ("repair", RepairPreconditions("thread_busy"), "thread_busy"),
        ("repair", RepairPreconditions("protected"), "protected"),
    ):
        result = evaluate(catalog, wrong, tier=tier, preconditions=preconditions)
        assert result.status == "suggestion", (tier, preconditions)
        assert result.disposition.repair_candidate_ids == []
        assert [(s.candidate_id, s.why) for s in result.disposition.suggestions] == [
            (RESTORE.candidate_id, why)
        ]


def test_a_candidate_must_match_both_the_requirement_and_the_defect_kind():
    catalog = make_catalog([req("R1"), req("R2")], [RESTORE])
    other_requirement = evaluate(catalog, finding("R2", "unmet", "copy_mismatch", ["f0"]))
    assert other_requirement.disposition.repair_candidate_ids == []
    other_defect = evaluate(catalog, finding("R1", "unmet", "text_cut_off", ["f0"]), finding("R2"))
    assert other_defect.disposition.repair_candidate_ids == []
    assert other_defect.status == "suggestion"
    assert [s.why for s in other_defect.disposition.suggestions] == ["no_candidate"]


def test_at_most_three_candidates_are_repaired_and_the_rest_are_suggestions():
    candidates = [
        cand("restore_exact_text", [f"R{n}"], ["copy_mismatch"], element=f"e{n}")
        for n in range(1, 5)
    ]
    catalog = make_catalog([req(f"R{n}") for n in range(1, 5)], candidates)
    result = evaluate(
        catalog, *[finding(f"R{n}", "unmet", "copy_mismatch", ["f0"]) for n in range(1, 5)]
    )
    assert result.disposition.repair_candidate_ids == [c.candidate_id for c in candidates[:3]]
    assert [(s.candidate_id, s.why) for s in result.disposition.suggestions] == [
        (candidates[3].candidate_id, "repair_cap")
    ]


def test_a_candidate_that_is_not_automatic_is_never_repaired():
    catalog = make_catalog([req("R1")], [MOVE])
    result = evaluate(catalog, finding("R1", "unmet", "text_obstructed", ["f0"]))
    assert result.status == "suggestion" and result.disposition.repair_candidate_ids == []
    assert [(s.candidate_id, s.why) for s in result.disposition.suggestions] == [
        (MOVE.candidate_id, "candidate_not_auto")
    ]


def test_wrong_footage_is_a_suggestion_never_a_question_or_a_repair():
    catalog = make_catalog([req("R1", "select"), req("R2")], [RESTORE])
    result = evaluate(
        catalog, finding("R1", "unmet", "wrong_content", ["f0", "f1"]), finding("R2", frames=["f2"])
    )
    assert result.status == "suggestion"
    assert result.disposition.question is None and result.disposition.repair_candidate_ids == []
    assert [(s.requirement_id, s.candidate_id, s.why) for s in result.disposition.suggestions] == [
        ("R1", None, "no_candidate")
    ]


@pytest.mark.parametrize("kind", ["order", "timing", "style"])
def test_non_text_requirements_only_ever_suggest(kind):
    defect = {"order": "order_mismatch", "timing": "text_missing", "style": "style_mismatch"}[kind]
    catalog = make_catalog([req("R1", kind), req("R2")])
    result = evaluate(catalog, finding("R1", "unmet", defect, ["f0"]), finding("R2"))
    assert result.status == "suggestion" and result.disposition.question is None


def test_wording_that_the_draft_already_has_is_a_render_fidelity_suggestion():
    catalog = make_catalog([req("R1", draft_matches_authority=True)])
    result = evaluate(catalog, finding("R1", "unmet", "copy_mismatch", ["f0"]))
    assert result.status == "suggestion" and result.disposition.question is None
    assert [(s.candidate_id, s.why) for s in result.disposition.suggestions] == [
        (None, "render_fidelity")
    ]


def test_wording_with_no_candidate_is_one_question_and_the_rest_are_suggestions():
    catalog = make_catalog(
        [req("R1", draft_matches_authority=False), req("R2", draft_matches_authority=None)]
    )
    result = evaluate(
        catalog,
        finding("R1", "unmet", "copy_mismatch", ["f0"]),
        finding("R2", "unmet", "text_missing", ["f1"]),
    )
    assert result.status == "needs_confirmation"
    assert result.disposition.question.requirement_id == "R1"
    assert [(s.requirement_id, s.why) for s in result.disposition.suggestions] == [
        ("R2", "no_candidate")
    ]
    assert result.disposition.repair_candidate_ids == []


def test_layout_defects_without_a_candidate_never_ask_about_wording():
    catalog = make_catalog([req("R1"), req("R2")])
    result = evaluate(
        catalog,
        finding("R1", "unmet", "text_cut_off", ["f0"]),
        finding("R2", "unmet", "text_obstructed", ["f1"]),
    )
    assert result.status == "suggestion" and result.disposition.question is None


def test_medium_confidence_wording_is_a_suggestion_not_a_question():
    catalog = make_catalog([req("R1")])
    result = evaluate(catalog, finding("R1", "unmet", "copy_mismatch", ["f0"], "medium"))
    assert result.status == "suggestion" and result.disposition.question is None
    assert [s.why for s in result.disposition.suggestions] == ["medium_confidence"]


def test_a_staged_repair_asks_nothing():
    catalog = make_catalog([req("R1"), req("R2")], [RESTORE])
    result = evaluate(
        catalog,
        finding("R1", "unmet", "copy_mismatch", ["f0"]),
        finding("R2", "unmet", "copy_mismatch", ["f1"]),
    )
    assert result.status == "repair_staged" and result.disposition.question is None
    assert [(s.requirement_id, s.why) for s in result.disposition.suggestions] == [
        ("R2", "no_candidate")
    ]


def test_outcome_precedence_is_repair_then_question_then_suggestion_then_clean():
    wording = req("R2", draft_matches_authority=False)
    catalog = make_catalog([req("R1"), wording, req("R3", "select")], [RESTORE])
    suggestion = finding("R3", "unmet", "wrong_content", ["f0"])
    question = finding("R2", "unmet", "copy_mismatch", ["f1"])
    repair = finding("R1", "unmet", "copy_mismatch", ["f2"])
    clean = finding("R2")
    assert evaluate(catalog, suggestion, question, repair).status == "repair_staged"
    assert evaluate(catalog, suggestion, question).status == "needs_confirmation"
    assert evaluate(catalog, suggestion, clean).status == "suggestion"
    assert evaluate(catalog, clean).status == "no_findings"


def test_validate_returns_findings_without_changing_unmet_ones():
    catalog = make_catalog([req("R1")])
    output = parse_review_output(raw(finding("R1", "unmet", "copy_mismatch", ["f0", "f1"])))
    (kept,) = validate_review_output(output, catalog)
    assert kept.verdict == "unmet" and kept.evidence_frame_ids == ["f0", "f1"]


def test_dispose_orders_suggestions_by_issued_requirement_not_by_model_order():
    catalog = make_catalog([req("R1", "select"), req("R2", "select")])
    output = parse_review_output(
        raw(
            finding("R2", "unmet", "wrong_content", ["f0"]),
            finding("R1", "unmet", "wrong_content", ["f1"]),
        )
    )
    disposition = dispose(
        validate_review_output(output, catalog),
        catalog,
        tier="review",
        repair_preconditions=None,
    )
    assert [s.requirement_id for s in disposition.suggestions] == ["R1", "R2"]


# --- the provider sees a closed schema -------------------------------------------------------


def test_the_response_schema_is_closed_over_exactly_the_issued_aliases_and_frames():
    schema = response_schema(["R1", "R2"], ["f0", "f1", "f2"])
    assert schema["additionalProperties"] is False
    item = schema["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert item["properties"]["requirement_id"]["enum"] == ["R1", "R2"]
    assert item["properties"]["evidence_frame_ids"]["items"]["enum"] == ["f0", "f1", "f2"]
    assert not {"wording", "text", "candidate_id", "reason", "ops"} & set(item["properties"])
    with pytest.raises(ValueError):
        response_schema([], ["f0"])
    with pytest.raises(ValueError):
        response_schema(["R1"], [])
