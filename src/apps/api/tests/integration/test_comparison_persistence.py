"""Real draft save/reopen/undo for inert comparison outputs; no model/broker mocks."""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import User
from app.services import creator_comparison_persistence as persistence
from app.services.request_following_cases import digest


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost/nova",
        "postgresql://remote.example/nova_test",
        "postgresql://localhost/nova_creator_model_comparison_pilot",
        "sqlite:///nova_test",
    ],
)
def test_persistence_refuses_live_or_nonlocal_database(url):
    with pytest.raises(ValueError):
        persistence.require_scratch_database(url)


@pytest.mark.asyncio
async def test_main_output_commits_reopens_rejects_stale_and_undoes():
    from app.agents.main_creator import MainCreatorAgent

    fixture = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/main_creator/montage_strategy.json"
        ).read_text()
    )
    # This old parser cassette predates required format availability. Give this
    # synthetic persistence control an explicit current server capability.
    fixture["input"]["capability_manifest"]["capabilities"]["edit_format:montage"] = {
        "available": True
    }
    inp = MainCreatorAgent.Input.model_validate(fixture["input"])
    output = MainCreatorAgent(None).parse(fixture["raw_text"], inp).model_dump(mode="json")
    case = {
        "id": "control",
        "agent": "main_creator",
        "input": inp.model_dump(mode="json"),
        "source_kind": "synthetic_control",
    }
    case["input_sha256"] = digest(case["input"])
    async with AsyncSessionLocal() as db:
        before = await db.scalar(select(func.count()).select_from(User))
    result = await persistence.verify_saved_candidate(case, output)
    assert result["status"] == "verified"
    assert result["checks"] == {
        "committed_reopen": True,
        "stale_write_rejected": True,
        "undo_restores_prior": True,
        "restore_reopens_exactly": True,
        "scratch_rows_removed": True,
    }
    assert result["rendered"] is False and result["source_kind"] == "synthetic_control"
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == before


@pytest.mark.asyncio
async def test_editor_preserves_stacked_text_through_real_persistence():
    inp = {
        "utterance": "Align the two left titles",
        "prior_turns": [],
        "original_request": None,
        "variant_snapshot": {
            "base_generation": "generation",
            "allowed_op_families": ["text"],
            "slots": [],
            "text_bars": [
                {
                    "id": "a",
                    "text": "First",
                    "start_s": 0,
                    "end_s": 4,
                    "x_frac": 0.1,
                    "y_frac": 0.8,
                },
                {
                    "id": "b",
                    "text": "Second",
                    "start_s": 0,
                    "end_s": 4,
                    "x_frac": 0.12,
                    "y_frac": 0.9,
                },
            ],
        },
    }
    case = {
        "id": "control",
        "agent": "edit_copilot",
        "input": inp,
        "input_sha256": digest(inp),
        "source_kind": "synthetic_control",
        "creator_id": "source-owner",
        "source_job_id": "source-job",
    }
    context = {
        "job": {
            "id": "source-job",
            "user_id": "source-owner",
            "mode": "generative",
            "assembly_plan": {
                "variants": [
                    {
                        "variant_id": "test",
                        "render_generation_id": "generation",
                        "duration_s": 4,
                        "text_elements": inp["variant_snapshot"]["text_bars"],
                    }
                ]
            },
        }
    }
    output = {
        "intent": "edit",
        "reply": "Prepared",
        "ops": [
            {"op": "patch_text_style", "bar_index": 0, "patch": {"x_frac": 0.1}},
            {"op": "patch_text_style", "bar_index": 1, "patch": {"x_frac": 0.1}},
        ],
    }
    result = await persistence.verify_saved_candidate(case, output, execution_context=context)
    assert result["status"] == "verified"
    bars = result["saved_snapshot"]["editor_payload"]["text_elements"]
    assert [b["y_frac"] for b in bars] == [0.8, 0.9]
    assert [b["x_frac"] for b in bars] == [0.1, 0.1]
    assert result["execution_base"] == "reconstructed_from_recorded_lanes_and_owned_job"
    assert result["checks"]["scratch_rows_removed"]


@pytest.mark.asyncio
async def test_changed_input_or_foreign_context_cannot_be_persisted():
    case = {"id": "case", "agent": "edit_copilot", "input": {}, "input_sha256": "bad"}
    with pytest.raises(ValueError):
        await persistence.verify_saved_candidate(case, {"ops": []})
