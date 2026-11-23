"""KRI-1583: the intent review can say on-screen text cannot be read (`text_illegible`).

The deliberately black-on-dark control is the qualification case `illegible_text_control`:
the title is on screen as approved, drawn near-black with no outline on a dark strip
(`.intent-review-qualification/frames/illegible_text_control/`). Its paid live run is a later
stage; here the server path is proven with the preregistered stub reviewers.

Ways this fails, written before the code:
1. A stamped job's review never offers the defect, so "everything matches" stays the answer.
2. An unstamped review changes: its prompt, schema, catalog dump or packet digest moves.
3. The model reports `text_illegible` on an unstamped review and the server accepts it.
4. A legibility finding stages a repair, asks a question, or claims the text is fine.
5. A lazy reviewer that approves everything passes the control.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents._schemas.intent_review import LEGIBILITY_DEFECT, InvalidReviewOutput
from app.agents.intent_review import (
    LEGIBILITY_PROMPT_VERSION,
    PROMPT_VERSION,
    IntentReviewInput,
    render_review_prompt,
)
from app.kria import intent_review_qualification as q
from app.kria.intent_review_disposition import RepairPreconditions, evaluate_review
from app.pipeline.informative_text import POLICY_KEY

pytestmark = pytest.mark.journey("kri-1585-informative-text")

_GOLDENS = Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals" / "intent_review"
# sha256 over every pre-existing intent review fixture's rendered prompt and response schema
# (`_v3_digest`), computed on origin/main 12c59fec29 before this change.
V3_PROMPTS_AND_SCHEMAS_SHA256 = "7421b3649010001e7af847ebdfb4619adf8913cca9e53993e7210f6c862cefe1"


def _v3_digest() -> str:
    from app.agents._schemas.intent_review import response_schema

    out = {}
    for path in sorted(_GOLDENS.glob("*/*.json")):
        if path.stem == "illegible_text_control":
            continue
        agent_input = IntentReviewInput.model_validate(json.loads(path.read_text())["input"])
        catalog = agent_input.catalog
        body = render_review_prompt(agent_input) + json.dumps(
            response_schema(catalog.requirement_ids, catalog.frame_ids), sort_keys=True
        )
        out[f"{path.parent.name}/{path.stem}"] = hashlib.sha256(body.encode()).hexdigest()
    return hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()


def test_unstamped_reviews_send_v3_byte_for_byte():
    assert _v3_digest() == V3_PROMPTS_AND_SCHEMAS_SHA256


@pytest.fixture(scope="module")
def built():
    return q.build_all()


def _enum(review) -> list:
    finding = review.response_schema["properties"]["findings"]["items"]
    return finding["properties"]["defect_kind"]["enum"]


def test_only_a_stamped_review_offers_the_defect(built):
    plain, stamped = built["correct_edit"].built, built["illegible_text_control"].built
    assert plain.prompt_version == PROMPT_VERSION
    assert LEGIBILITY_DEFECT not in plain.prompt and LEGIBILITY_DEFECT not in _enum(plain)
    assert "text_legibility" not in plain.catalog
    assert stamped.prompt_version == LEGIBILITY_PROMPT_VERSION
    assert LEGIBILITY_DEFECT in _enum(stamped)
    assert stamped.catalog["text_legibility"] is True
    for line in stamped.prompt.splitlines():
        if line.lstrip().startswith(("- text:", "- text_sequence:", "- style:")):
            assert LEGIBILITY_DEFECT in line, line


def test_an_unstamped_review_that_says_illegible_is_void(built):
    case = built["correct_edit"]
    reply = json.loads(q.perfect_reply(case))
    reply["findings"][0].update(
        verdict="unmet", defect_kind=LEGIBILITY_DEFECT, evidence_frame_ids=[q._anchor(case)]
    )
    with pytest.raises(InvalidReviewOutput) as voided:
        evaluate_review(
            json.dumps(reply),
            case.catalog,
            tier="repair",
            repair_preconditions=RepairPreconditions(),
        )
    assert voided.value.reason == "incompatible_defect_kind"


@pytest.mark.parametrize("tier", ["review", "repair"])
def test_the_black_on_dark_control_is_flagged_and_changes_nothing(built, tier):
    case = built["illegible_text_control"]
    evaluation = evaluate_review(
        q.perfect_reply(case),
        case.catalog,
        tier=tier,
        repair_preconditions=RepairPreconditions(),
    )
    (title,) = [f for f in evaluation.findings if f.verdict == "unmet"]
    assert title.defect_kind == LEGIBILITY_DEFECT
    assert evaluation.status == "suggestion"
    assert [s.why for s in evaluation.disposition.suggestions] == ["no_candidate"]
    assert evaluation.disposition.repair_candidate_ids == []
    assert evaluation.disposition.question is None


def test_every_preregistered_check_holds_with_the_control_added(built):
    checks = [*q.run_scorer_checks(built), *q.run_discrimination_checks(built)]
    assert [c.name for c in checks if not c.ok] == []
    names = {c.name for c in checks}
    assert {
        "perfect_reviewer/illegible_text_control",
        "approves_everything/illegible_text_control",
        "flags_everything/illegible_text_control",
        "illegible_text_control_differs_only_where_claimed",
    } <= names


@pytest.mark.parametrize(
    ("variant_policy", "job_policy", "stamped"),
    [
        (None, None, False),
        ({"version": 1}, {"version": 1, POLICY_KEY: True}, False),
        (None, {"version": 1, POLICY_KEY: True}, True),
        ({"version": 1, POLICY_KEY: True}, None, True),
    ],
)
def test_the_packet_reads_the_stamp_as_a_save_reads_it(variant_policy, job_policy, stamped):
    from app.kria.render_review_context import _stamped_for_informative_text

    variant = {} if variant_policy is None else {"phone_text_style": variant_policy}
    job = SimpleNamespace(
        assembly_plan={} if job_policy is None else {"phone_text_style": job_policy}
    )
    assert _stamped_for_informative_text(job, variant) is stamped
