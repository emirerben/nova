from __future__ import annotations

import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.edit_training_dataset import (
    DatasetCandidate,
    assert_export_safe,
    build_preference_pairs,
    canonical_record,
    dataset_readiness,
    records_to_jsonl,
    records_to_parquet,
)
from app.services.edit_training_exports import (
    _receipt_matches_artifact_grant,
    _safe_media_manifest,
)

SECRET = "dataset-test-secret-at-least-16"


def _candidate(*, artifact: str, creator: str = "creator-a", rating: str = "good"):
    dimensions = (
        "overall_quality",
        "ai_guidance_and_response",
        "instruction_fit",
        "hook",
        "pacing",
        "cuts",
        "clip_selection",
        "clip_ordering",
        "captions",
        "text",
        "transitions",
        "music",
        "audio",
        "effects",
        "overlays",
    )
    return DatasetCandidate(
        artifact_id=artifact,
        creator_id=creator,
        plan_item_id="item-a",
        agent="edit_copilot",
        media_summary=[
            {
                "media_key": "m1",
                "kind": "video",
                "duration_s": 8.0,
                "visual_summary": "A beach at sunset",
                "gcs_path": "users/private/raw.mp4",
                "transcript": "must not export",
            }
        ],
        user_intent="Make this a fast montage",
        proposed={"operations": [{"type": "set_edit_direction"}]},
        execution={"outcome": "applied"},
        labels=[
            {"dimension": dimension, "rating": rating, "rationale": "clear"}
            for dimension in dimensions
        ],
        versions={"prompt": "1.0.0", "model": "test-model"},
        lineage_parts=("item-a", "intent-a", "analysis-a"),
    )


def test_canonical_record_uses_creator_group_split_and_drops_source_fields():
    first = canonical_record(_candidate(artifact="a1"), secret=SECRET)
    second = canonical_record(_candidate(artifact="a2"), secret=SECRET)

    assert first.creator_group == second.creator_group
    assert first.split == second.split
    assert "gcs_path" not in first.media_summary[0]
    assert "transcript" not in first.media_summary[0]
    assert "creator-a" not in records_to_jsonl([first]).decode()


def test_export_guard_rejects_paths_signed_urls_and_raw_payloads():
    with pytest.raises(ValueError, match="denied field"):
        assert_export_safe({"assembly_plan": {}})
    with pytest.raises(ValueError, match="storage URL"):
        assert_export_safe({"value": "gs://bucket/raw.mp4"})
    with pytest.raises(ValueError, match="storage URL"):
        assert_export_safe({"value": "https://x.test/a?X-Goog-Signature=secret"})


def test_jsonl_is_canonical_and_round_trips():
    record = canonical_record(_candidate(artifact="a1"), secret=SECRET)
    payload = json.loads(records_to_jsonl([record]))
    assert payload["schema_version"] == 1
    assert payload["artifact_key"] == record.artifact_key


def test_jsonl_and_parquet_encode_the_same_canonical_records(tmp_path):
    parquet = pytest.importorskip("pyarrow.parquet")
    records = [
        canonical_record(_candidate(artifact="a1"), secret=SECRET),
        canonical_record(_candidate(artifact="a2"), secret=SECRET),
    ]
    output = tmp_path / "dataset.parquet"
    records_to_parquet(records, str(output))
    parquet_rows = parquet.read_table(output).to_pylist()
    jsonl_rows = [json.loads(line) for line in records_to_jsonl(records).splitlines()]

    assert [row["artifact_key"] for row in parquet_rows] == [
        row["artifact_key"] for row in jsonl_rows
    ]
    assert [row["creator_group"] for row in parquet_rows] == [
        row["creator_group"] for row in jsonl_rows
    ]
    assert [json.loads(row["labels_json"]) for row in parquet_rows] == [
        row["labels"] for row in jsonl_rows
    ]


def test_preference_pairs_require_comparable_lineage_and_explicit_difference():
    good = canonical_record(_candidate(artifact="good", rating="good"), secret=SECRET)
    bad = canonical_record(_candidate(artifact="bad", rating="bad"), secret=SECRET)
    pairs = build_preference_pairs([bad, good])
    assert len(pairs) == 1
    assert pairs[0].chosen["execution"]["outcome"] == "applied"


def test_readiness_blocks_small_or_incomplete_corpus():
    record = canonical_record(_candidate(artifact="a1"), secret=SECRET)
    readiness = dataset_readiness([record])
    assert readiness.ready is False
    assert readiness.reviewed_artifacts == 1
    assert readiness.creator_groups == 1
    assert len(readiness.blockers) == 2


def test_readiness_requires_response_cuts_and_captions_as_distinct_factors():
    candidate = _candidate(artifact="a1")
    candidate = replace(
        candidate,
        labels=[
            label
            for label in candidate.labels
            if label["dimension"] not in {"ai_guidance_and_response", "cuts", "captions"}
        ],
    )
    readiness = dataset_readiness([canonical_record(candidate, secret=SECRET)])

    assert readiness.reviewed_artifacts == 0
    assert readiness.missing_dimensions["ai_guidance_and_response"] == 1
    assert readiness.missing_dimensions["cuts"] == 1
    assert readiness.missing_dimensions["captions"] == 1


def test_copilot_receipt_requires_the_artifacts_exact_consent_cycle():
    first_consent, later_consent = uuid.uuid4(), uuid.uuid4()
    artifact = SimpleNamespace(
        eligibility_basis="training_consent",
        consent_event_id=later_consent,
        internal_grant_id=None,
    )
    pre_consent_receipt = SimpleNamespace(
        eligibility_basis="training_consent",
        consent_event_id=first_consent,
        internal_grant_id=None,
    )
    current_receipt = SimpleNamespace(
        eligibility_basis="training_consent",
        consent_event_id=later_consent,
        internal_grant_id=None,
    )

    assert _receipt_matches_artifact_grant(pre_consent_receipt, artifact) is False
    assert _receipt_matches_artifact_grant(current_receipt, artifact) is True


def test_media_summary_projects_nested_analysis_without_source_payloads():
    artifact = SimpleNamespace(
        creator_id=uuid.uuid4(),
        media_manifest=[
            {
                "media_id": "source-1",
                "kind": "video",
                "duration_s": 8.0,
                "analysis": {
                    "description": "A runner crosses the finish line",
                    "width": 1080,
                    "height": 1920,
                    "motion": "fast",
                    "has_speech": False,
                    "transcript": "must never be projected",
                    "gcs_path": "users/private/source.mp4",
                },
            }
        ],
    )

    summary = _safe_media_manifest(artifact, secret=SECRET)

    assert summary[0]["visual_summary"] == "A runner crosses the finish line"
    assert summary[0]["width"] == 1080
    assert summary[0]["height"] == 1920
    assert summary[0]["motion"] == "fast"
    assert summary[0]["has_speech"] is False
    assert "transcript" not in summary[0]
    assert "gcs_path" not in summary[0]
