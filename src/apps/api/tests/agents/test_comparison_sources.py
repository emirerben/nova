"""Point-in-time real-source import; no invented creator text or human review."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.creator_comparison_sources import from_recorded_main


@pytest.fixture
def valid_main_source():
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    now = datetime.now(UTC)
    from app.agents.main_creator import MainCreatorAgent

    inp = MainCreatorAgent.Input.model_validate(fixture["input"]).model_dump(mode="json")
    message = inp["user_message"]
    source = {
        "eligibility": {
            "thread_id": "thread",
            "creator_id": "creator",
            "eligibility_key": "grant",
            "source_sha256": "a" * 64,
            "expires_at": (now + timedelta(days=1)).isoformat(),
        },
        "candidate": {
            "id": "queue-case",
            "intent_review": {
                "decision": "approved",
                "reviewer": "actual human",
                "version": 1,
                "expectations": [
                    {
                        "text": "Keep the requested intent",
                        "source_pointers": ["/sections/events/0/content"],
                    }
                ],
            },
        },
        "capture": {
            "sections": {
                "plan_item": {"id": inp["capability_manifest"]["item_id"]},
                "events": [
                    {
                        "kind": "user_message",
                        "actor": "user",
                        "content": message,
                        "created_at": (now - timedelta(seconds=1)).isoformat(),
                    }
                ],
            }
        },
    }
    recorded = {
        "id": "run",
        "agent_name": "nova.creator.main",
        "prompt_version": "historical",
        "created_at": now.isoformat(),
        "input_json": copy.deepcopy(inp),
    }
    return source, recorded


def test_only_provably_empty_redaction_can_be_recovered(valid_main_source):
    source, recorded = copy.deepcopy(valid_main_source)
    recorded["input_json"]["creator_direction"] = {
        "redacted": True,
        "chars": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    case = from_recorded_main(source, recorded)
    assert case["input"]["creator_direction"] == "" and case["source_kind"] == "exact_invocation"
    assert case["intent_review"]["decision"] == "source_reviewed"
    assert case["intent_review"]["reviewed_by"] == "actual human"
    recorded["input_json"]["creator_direction"]["chars"] = 10
    with pytest.raises(ValueError):
        from_recorded_main(source, recorded)


@pytest.mark.parametrize(
    "failure",
    [
        "agent",
        "item",
        "nested_redaction",
        "normalized",
        "naive_time",
        "missing_time",
        "before_turn",
        "after_turn",
    ],
)
def test_main_import_requires_exact_owned_turn(valid_main_source, failure):
    source, recorded = copy.deepcopy(valid_main_source)
    event = source["capture"]["sections"]["events"][0]
    invoked = datetime.fromisoformat(recorded["created_at"])
    if failure == "agent":
        recorded["agent_name"] = "nova.edit.copilot"
    elif failure == "item":
        source["capture"]["sections"]["plan_item"]["id"] = "foreign-item"
    elif failure == "nested_redaction":
        recorded["input_json"]["capability_manifest"]["catalog"] = {
            "hidden": {"redacted": True, "chars": 12}
        }
    elif failure == "normalized":
        del recorded["input_json"]["creator_direction"]
    elif failure == "naive_time":
        recorded["created_at"] = invoked.replace(tzinfo=None).isoformat()
    elif failure == "missing_time":
        del event["created_at"]
    elif failure == "before_turn":
        event["created_at"] = (invoked + timedelta(seconds=1)).isoformat()
    elif failure == "after_turn":
        source["capture"]["sections"]["events"].append(
            {
                "kind": "user_message",
                "actor": "user",
                "content": "Later correction",
                "created_at": (invoked - timedelta(milliseconds=500)).isoformat(),
            }
        )
    with pytest.raises(ValueError):
        from_recorded_main(source, recorded)


@pytest.fixture
def editor_source(valid_main_source):
    from types import SimpleNamespace

    source, _ = copy.deepcopy(valid_main_source)
    variants = [
        {
            "variant_id": "first",
            "text_elements": [
                {"id": "a", "text": "Wrong first title", "start_s": 0.0, "end_s": 2.0}
            ],
        },
        {
            "variant_id": "selected",
            "text_elements": [
                {"id": "b", "text": "Reviewed selected title", "start_s": 0.0, "end_s": 2.0}
            ],
        },
    ]
    job = SimpleNamespace(
        id="reviewed-job",
        user_id="creator",
        assembly_plan={"variants": variants},
        all_candidates={},
    )
    source["capture"]["sections"]["job-reviewed-job"] = {
        "id": "reviewed-job",
        "assembly_plan": copy.deepcopy(job.assembly_plan),
    }
    return source, job


def test_editor_reconstruction_pins_explicit_reviewed_second_variant(editor_source):
    from app.services.creator_comparison_sources import from_editor_snapshot
    from app.services.kria_editor_ops import build_editor_snapshot
    from app.services.request_following_cases import digest

    source, job = editor_source
    expected = build_editor_snapshot(job, copy.deepcopy(job.assembly_plan["variants"][1]))
    case = from_editor_snapshot(source, job, request_event_index=0, variant_id="selected")
    assert case["snapshot_sha256"] == digest(expected)
    assert case["source_job_id"] == "reviewed-job"
    assert case["source_variant_id"] == "selected"
    assert case["source_kind"] == "reconstructed_real" and case["quality_verdict"] == "unassessed"
    assert case["intent_review"]["decision"] == "source_reviewed"


@pytest.mark.parametrize(
    "failure",
    [
        "same_owner_other_job",
        "captured_job_id",
        "missing_variant",
        "duplicate_variant",
        "unreviewed_variant",
        "negative_event",
        "past_events",
    ],
)
def test_editor_reconstruction_rejects_unbound_job_variant_or_turn(
    editor_source, monkeypatch, failure
):
    from app.services.creator_comparison_sources import from_editor_snapshot

    source, job = editor_source
    index = 0
    if failure == "same_owner_other_job":
        job.id = "another-same-owner-job"
    elif failure == "captured_job_id":
        source["capture"]["sections"]["job-reviewed-job"]["id"] = "another-job"
    elif failure == "missing_variant":
        job.assembly_plan["variants"] = job.assembly_plan["variants"][:1]
    elif failure == "duplicate_variant":
        job.assembly_plan["variants"].append(copy.deepcopy(job.assembly_plan["variants"][1]))
    elif failure == "unreviewed_variant":
        source["capture"]["sections"]["job-reviewed-job"]["assembly_plan"]["variants"] = (
            job.assembly_plan["variants"][:1]
        )
    elif failure == "negative_event":
        index = -1
    elif failure == "past_events":
        index = 1
    monkeypatch.setattr(
        "app.services.kria_editor_ops.build_editor_snapshot",
        lambda *args: pytest.fail("unbound snapshot must not be built"),
    )
    with pytest.raises(ValueError):
        from_editor_snapshot(source, job, request_event_index=index, variant_id="selected")
