import json

import pytest

from app.services.jev_brief_shadow import JevPayload, build_jev_questions
from app.services.jev_client import JEV_MODEL, JevError, JevEvaluation

from .build_manifest import build
from .loader import load_jsonl
from .models import Case, Dataset, Prediction
from .report import render_report
from .run_live import evaluate_cases
from .scorer import score, sweep_thresholds


def make_case(case_id="c1", split="calibration", group="g1", human=True):
    return Case(
        case_id=case_id,
        split=split,
        provenance="human_authored",
        group_id=group,
        language="en",
        scenario="good",
        payload={"x": 1},
        required_items=[{"item_id": "r", "required": True}],
        unsupported_claims=[{"claim_id": "u", "unsupported": True}],
        label_metadata={"label_source": "human", "human_adjudicated": human},
    )


def make_prediction(case_id="c1", **kwargs):
    return Prediction(
        case_id=case_id,
        required_items=[{"item_id": "r", "probability": kwargs.get("r", 0.9)}],
        unsupported_claims=[{"claim_id": "u", "probability": kwargs.get("u", 0.9)}],
        **{
            k: v
            for k, v in kwargs.items()
            if k in {"latency_ms", "cost_usd", "error", "success", "fallback", "retries"}
        },
    )


def test_validation_duplicates_leakage_and_decision_count():
    with pytest.raises(ValueError, match="duplicate case_id"):
        Dataset(cases=[make_case(), make_case()])
    with pytest.raises(ValueError, match="group leakage"):
        Dataset(cases=[make_case(group="same"), make_case("c2", split="held_out", group="same")])
    with pytest.raises(ValueError, match="decision_count"):
        Dataset(cases=[make_case()], decision_count=100)
    with pytest.raises(ValueError, match="100-200"):
        Dataset(cases=[make_case()]).validate_decision_set()


def test_confusion_abstention_and_zero_denominators():
    case = Case(
        case_id="c",
        split="calibration",
        provenance="derived",
        group_id="g",
        language="en",
        scenario="ambiguous",
        payload={},
        required_items=[{"item_id": "r", "required": True}],
        unsupported_claims=[{"claim_id": "u", "unsupported": False}],
        label_metadata={"label_source": "derived", "human_adjudicated": False},
    )
    result = score(
        [case],
        [
            Prediction(
                case_id="c",
                required_items=[{"item_id": "r", "probability": 0.5}],
                unsupported_claims=[{"claim_id": "u", "probability": 0.5}],
            )
        ],
        confidence_band=(0.4, 0.6),
    )
    assert result["required"]["tp"] == result["required"]["fp"] == 0
    assert result["required"]["abstention"] == 1
    assert result["unsupported_claims"]["precision"] is None


def test_operations_percentiles_cost_and_alignment():
    cases = [make_case("c1"), make_case("c2", group="g2")]
    predictions = [
        make_prediction("c1", latency_ms=10, cost_usd=0.2),
        make_prediction("c2", latency_ms=20, cost_usd=0.4, error=True, success=False, retries=1),
    ]
    result = score(cases, predictions)
    assert result["operational"]["p50_latency_ms"] == 15
    assert result["operational"]["p95_latency_ms"] == 19.5
    assert result["operational"]["total_cost_usd"] == pytest.approx(0.6)
    with pytest.raises(ValueError, match="exactly one"):
        score(cases, [make_prediction("c1")])


def test_provider_failure_is_an_abstention_not_an_invented_score():
    case = make_case()
    result = score(
        [case],
        [Prediction(case_id="c1", success=False, error=True, fallback=True)],
    )
    assert result["required"]["coverage"] == 0
    assert result["unsupported_claims"]["coverage"] == 0
    assert result["operational"]["error_rate"] == 1


def test_sweep_rejects_held_out_and_report_is_deterministic(tmp_path):
    case = make_case(split="held_out")
    pred = make_prediction()
    with pytest.raises(ValueError, match="calibration"):
        sweep_thresholds([make_case(split="held_out")], [pred], [0.5])
    cases_path, predictions_path = tmp_path / "cases.jsonl", tmp_path / "predictions.jsonl"
    cases_path.write_text(json.dumps(case.model_dump()) + "\n")
    predictions_path.write_text(json.dumps(pred.model_dump()) + "\n")
    first = render_report(
        cases_path,
        predictions_path,
        threshold_source="calibration",
        gates={"required_recall": 0.9},
    )
    assert first == render_report(
        cases_path,
        predictions_path,
        threshold_source="calibration",
        gates={"required_recall": 0.9},
    )
    assert "decision_set_size" in first and "Human-adjudicated" in first


def test_report_missing_held_out_prediction_is_no_go(tmp_path):
    case = make_case(split="held_out", human=False)
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    cases_path.write_text(json.dumps(case.model_dump()) + "\n")
    predictions_path.write_text("")
    report = render_report(cases_path, predictions_path)
    assert "NO-GO" in report
    assert "missing_provider_predictions" in report
    assert "calibration_threshold_provenance" in report


def test_report_rejects_extra_prediction_rows(tmp_path):
    case = make_case(split="held_out")
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    cases_path.write_text(json.dumps(case.model_dump()) + "\n")
    predictions_path.write_text(
        "\n".join(
            json.dumps(row.model_dump())
            for row in (make_prediction("c1"), make_prediction("unexpected"))
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="unexpected prediction case_id"):
        render_report(cases_path, predictions_path)


def test_jsonl_diagnostics_and_manifest_regeneration(tmp_path):
    manifest = build()
    assert manifest == build()
    assert len(manifest) == 25
    assert (
        sum(len(row["required_items"]) + len(row["unsupported_claims"]) for row in manifest) == 100
    )
    assert {row["scenario"] for row in manifest} == {"good", "flawed", "ambiguous", "non_english"}
    assert all(not row["label_metadata"]["human_adjudicated"] for row in manifest)
    groups = {}
    for row in manifest:
        groups.setdefault(row["group_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in groups.values())
    for row in manifest:
        payload = JevPayload.model_validate(row["payload"])
        assert set(build_jev_questions(payload)) == {
            "required_0",
            "required_1",
            "claim_0",
            "claim_1",
        }
        assert {label["item_id"] for label in row["required_items"]} == {
            item.id for item in payload.brief.requirements
        }
        assert {label["claim_id"] for label in row["unsupported_claims"]} == {
            item.id for item in payload.candidate_claims
        }
        serialized = json.dumps(row["payload"])
        assert "fixture_path" not in serialized
        assert "tests/fixtures" not in serialized
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not-json\n")
    with pytest.raises(ValueError, match="row 1"):
        load_jsonl(bad, Case)


def test_live_runner_uses_real_question_and_prediction_ids():
    dataset = Dataset(cases=[Case.model_validate(row) for row in build()], decision_count=100)

    class FakeClient:
        def evaluate(self, state, questions):
            return JevEvaluation(
                model=JEV_MODEL,
                answers={question_id: 0.75 for question_id in questions},
                input_tokens=10,
                output_tokens=0,
                attempts=1,
                latency_ms=2.0,
            )

    predictions = evaluate_cases(
        dataset,
        "secret",
        client_factory=lambda *_args, **_kwargs: FakeClient(),
    )
    assert len(predictions) == 25
    assert {item.item_id for item in predictions[0].required_items} == {"r0_0", "r0_1"}
    assert {item.claim_id for item in predictions[0].unsupported_claims} == {
        "claim_0",
        "claim_1",
    }


def test_live_runner_records_provider_failures_without_scores():
    dataset = Dataset(cases=[Case.model_validate(row) for row in build()], decision_count=100)

    class FailedClient:
        def evaluate(self, state, questions):
            raise JevError("rate_limited", attempts=2, status_code=529, retryable=True)

    predictions = evaluate_cases(
        dataset,
        "secret",
        client_factory=lambda *_args, **_kwargs: FailedClient(),
    )
    assert all(row.error and row.fallback for row in predictions)
    assert all(not row.required_items and not row.unsupported_claims for row in predictions)


def test_live_runner_records_mapping_failures_without_aborting():
    dataset = Dataset(cases=[Case.model_validate(row) for row in build()], decision_count=100)

    class MalformedClient:
        def evaluate(self, state, questions):
            return {"answers": {}, "model": JEV_MODEL}

    predictions = evaluate_cases(
        dataset,
        "secret",
        client_factory=lambda *_args, **_kwargs: MalformedClient(),
    )
    assert len(predictions) == 25
    assert all(row.error_code == "ValueError" for row in predictions)
    assert all(row.error and row.fallback for row in predictions)
