"""Replay gate for semantic edit proposal cassettes; live calls use the normal capped harness."""

import math
from pathlib import Path

import pytest

from app.agents.edit_proposal import EditProposalAgentInput
from app.agents.semantic_edit_proposal import SemanticEditProposalAgent
from app.pipeline.guided_story import compile_execution_plan
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, canonical_media_digest
from app.schemas.semantic_edit import SemanticEditPlan
from app.services.semantic_edit_scheduler import assess_semantic_feasibility, schedule_semantic_edit
from app.tasks.generative_build import _grounded_context_labels

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "semantic_edit_proposal"
AGENT_NAME = "nova.plan.semantic_edit_proposal"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)
KRI129_LIVE_FIXTURE = next(
    path for path in FIXTURE_PATHS if path.stem == "kri129_fast_montage_target_equals_capacity"
)


def _compile_semantic_result(plan: SemanticEditPlan, input: EditProposalAgentInput) -> dict:
    """Exercise compiler v8 with the exact server schedule, not draft timings."""
    result = schedule_semantic_edit(plan, input)
    media = [
        MediaRef(
            lane=row.lane,
            media_id=row.media_id,
            gcs_path=f"users/{row.media_id}",
            generation="eval",
            kind=row.kind,
            duration_s=row.duration_s,
        )
        for row in input.media
    ]
    snapshot = EditProposalSnapshot(
        frame_schedule=result.schedule,
        direction=input.direction,
        pace=input.pace,
        duration_s=result.duration_s,
        title=input.opening_title or plan.title,
        media=media,
        story_beats=result.story_beats,
        fast_cuts=result.fast_cuts,
        montage_text_bindings=result.montage_text_bindings,
        montage_audio=input.montage_audio,
        montage_cadence=input.montage_cadence,
        mixed_media_timing=input.mixed_media_timing,
        video_reuse_policy=input.video_reuse_policy,
        media_scope=input.media_scope,
        selected_media_ids=input.selected_media_ids,
        clip_intents=input.clip_intents,
        opening_title=input.opening_title,
        opening_title_duration_s=input.opening_title_duration_s,
        closing_title=input.closing_title,
        shot_labels=input.shot_labels,
    )
    raw = {
        "proposal_version": 9,
        "media_digest": canonical_media_digest(snapshot.media, snapshot.narration),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": row.lane,
                "media_id": row.media_id,
                "gcs_path": row.gcs_path,
                "generation": row.generation,
                "kind": row.kind,
            }
            for row in media
        ],
    }
    return compile_execution_plan(raw, track=None)


def _assert_kri129_compiler_contract(output: dict, fixture, input: EditProposalAgentInput) -> None:
    plan = SemanticEditPlan.model_validate(output)
    scheduled = schedule_semantic_edit(plan, input)
    assert scheduled.schedule.total_frames == fixture.meta["expected_frames"]
    group = fixture.meta["expected_group"]
    assert [moment.media_id for moment in scheduled.schedule.moments[-len(group) :]] == group
    compiled = _compile_semantic_result(plan, input)
    assert compiled["compiler_version"] == 8
    assert compiled["resolved_duration_s"] == fixture.meta["expected_frames"] / 30
    assert compiled["montage_audio"]["source_media_ids"] == fixture.meta["expected_audio_ids"]
    assert any(row["id"] == "guided-title" for row in compiled["text_elements"])
    texts = {row["text"] for row in compiled["text_elements"]}
    assert set(fixture.meta["expected_bindings"]) <= texts
    assert "planner_fallback" not in compiled


def _assert_kri129_grounded_label_contract(fixture, input: EditProposalAgentInput) -> None:
    """Resolved labels survive the snapshot and render only through the grounding fence."""
    expected = fixture.meta.get("expected_grounded_labels", {})
    assert expected
    refs = [
        MediaRef(
            lane=row.lane,
            media_id=row.media_id,
            gcs_path=f"users/{row.media_id}",
            generation="eval",
            kind=row.kind,
            duration_s=row.duration_s,
            analysis={
                "understanding": {
                    "kind": row.kind,
                    "summary": expected.get(row.media_id, ""),
                    "subject": "",
                }
            },
        )
        for row in input.media
    ]
    scheduled = schedule_semantic_edit(SemanticEditPlan.model_validate(fixture.output), input)
    snapshot = EditProposalSnapshot(
        direction=input.direction,
        duration_s=input.target_duration_s,
        title=input.opening_title or "semantic eval",
        media=refs,
        story_beats=scheduled.story_beats,
        fast_cuts=scheduled.fast_cuts,
        montage_audio=input.montage_audio,
        montage_cadence=input.montage_cadence,
        mixed_media_timing=input.mixed_media_timing,
        video_reuse_policy=input.video_reuse_policy,
        clip_intents=input.clip_intents,
    )
    assert snapshot.clip_intents == input.clip_intents
    rows = _grounded_context_labels(
        snapshot.clip_intents or [],
        {ref.media_id: ref.gcs_path for ref in snapshot.media},
        [],
        creator_request=input.creator_request,
        media_refs=list(snapshot.media),
    )
    assert {row["clip_id"]: row["sport"] for row in rows} == expected
    assert all(row["source"] == "grounded_label" for row in rows)


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_semantic_edit_proposal_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    assert fixture.agent == AGENT_NAME
    assert fixture.prompt_version == SemanticEditProposalAgent.spec.prompt_version
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge_for(fixture.agent) if with_judge else None,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, f"{result.fixture_id}: {result.summary()} {result.structural_failures}"
    assert result.output is not None
    assert all("duration_s" not in chapter for chapter in result.output["chapters"])
    if expected_title := fixture.meta.get("expected_title"):
        assert result.output["title"] == expected_title
    if expected_bindings := fixture.meta.get("expected_bindings"):
        assert set(expected_bindings) <= {
            binding["text"] for binding in result.output["text_bindings"]
        }
    if expected_audio_ids := fixture.meta.get("expected_audio_ids"):
        assert result.output["montage_audio"]["source_media_ids"] == expected_audio_ids
    # Fixture families with fully probed sources also replay the deterministic
    # materializer: semantic output never gets to smuggle a timestamp through
    # this boundary, and every scheduled window stays inside its source.
    input = EditProposalAgentInput.model_validate(fixture.input)
    plan = SemanticEditPlan.model_validate(result.output)
    used = {source.media_id for chapter in plan.chapters for source in chapter.sources}
    media = {row.media_id: row for row in input.media if row.media_id in used}
    feasibility = assess_semantic_feasibility(input)
    if feasibility.status == "infeasible":
        # Legacy cassettes with absent video probes or a pinned narration
        # longer than total footage are documented preflight failures; they
        # remain semantic parsing coverage but cannot have a frame schedule.
        assert feasibility.reason in {
            "video duration is unknown",
            "required media is too short",
            "pinned timing exceeds footage capacity",
            "fixed cadence requires source reuse but reuse is prohibited",
        }
    else:
        scheduled = schedule_semantic_edit(plan, input).schedule
        assert scheduled.total_frames == feasibility.effective_frames
        if feasibility.status == "feasible":
            assert scheduled.total_frames == round(
                float(input.narration_duration_s or input.target_duration_s) * 30
            )
        for moment in scheduled.moments:
            source = media[moment.media_id]
            if source.kind == "video":
                assert moment.source_end_frame <= math.ceil(float(source.duration_s or 0) * 30)
    if fixture_path == KRI129_LIVE_FIXTURE:
        _assert_kri129_compiler_contract(result.output, fixture, input)
        _assert_kri129_grounded_label_contract(fixture, input)


@pytest.mark.parametrize("repeat", range(5), ids=lambda index: f"run-{index + 1}")
def test_kri129_caption_contract_live_five_times(
    repeat: int,
    eval_mode: str,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    """Paid only under --eval-mode=live; each call remains ledger-attributed."""
    if eval_mode != "live":
        pytest.skip("five-run KRI-129 acceptance target is live-only")
    fixture = load_fixture(KRI129_LIVE_FIXTURE)
    result = run_eval(
        fixture,
        model_client=live_model_client,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
        request_id_suffix=f"repeat-{repeat + 1}",
    )
    assert result.passed, f"run {repeat + 1}: {result.summary()} {result.structural_failures}"
    assert result.output is not None
    assert result.output["title"] == fixture.meta["expected_title"]
    assert set(fixture.meta["expected_bindings"]) <= {
        binding["text"] for binding in result.output["text_bindings"]
    }
    _assert_kri129_compiler_contract(
        result.output,
        fixture,
        EditProposalAgentInput.model_validate(fixture.input),
    )
    _assert_kri129_grounded_label_contract(
        fixture,
        EditProposalAgentInput.model_validate(fixture.input),
    )
