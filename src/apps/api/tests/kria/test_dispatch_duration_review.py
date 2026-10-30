"""Real dispatch regression; factory-output fixtures do not qualify KRI-1071."""

import copy
import json
import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.dispatch_duration_review import capture_dispatch_duration, review_dispatch_duration
from app.services import generative_jobs
from app.services.creator_render_contract import read_render_contract
from tests.tasks.test_kri788_order_answer_dispatch import T1_STRATEGY, _dispatch, _item

FAILURE_MATRIX = (
    "The factory overwrites a pre-factory correction.",
    "Final contract20 can coexist with strategy3 or missing provenance.",
    "Scoped or quoted numbers cannot authorize global corrections.",
    "Conflicting globals require clarification.",
    "Matching dispatch settings cannot verify a later recipe.",
)


def dispatch_case(monkeypatch, utterance, *, total=20, scoped=3, recognized=3):
    item = _item()
    requirements = [
        BriefRequirement(
            id="shot",
            kind="timing",
            scope="clip:lineup-sheet",
            authority="explicit",
            description="Show the lineup sheet",
            facts={"duration_s": scoped},
        )
    ]
    if total is not None:
        requirements.append(
            BriefRequirement(
                id="total",
                kind="timing",
                scope="global",
                description="Whole output duration",
                facts={"duration_s": total},
            )
        )
    binding = BriefBinding.create(
        uuid.uuid4(),
        CreativeBrief(version=1, requirements=requirements),
        latest_message=utterance,
        full_creator_request=utterance,
        media_snapshot=snapshot_media(item),
    )
    approved = {**T1_STRATEGY, "target_duration_s": total or 20}
    monkeypatch.setattr(
        generative_jobs,
        "recognize_total_duration_s",
        recognized if callable(recognized) else lambda _: recognized,
    )
    job = _dispatch(monkeypatch, item, approved, binding.model_dump(mode="json"))
    job.id = uuid.uuid4()
    return (
        job,
        binding,
        approved,
        capture_dispatch_duration(job, binding=binding, approved_plan=approved),
    )


@pytest.mark.parametrize(
    "utterance,total,scoped,recognized",
    [
        ("Show the lineup sheet for 3 seconds then make it 20 seconds", 20, 3, 3),
        ("Hold the shot for 2 seconds then make it 15 seconds", 15, 2, None),
        ('Show the words "for 3 seconds" then make it 20 seconds', 20, 3, 3),
    ],
)
def test_late_audit_catches_factory_corruption(monkeypatch, utterance, total, scoped, recognized):
    job, binding, approved, capture = dispatch_case(
        monkeypatch,
        utterance,
        total=total,
        scoped=scoped,
        recognized=recognized,
    )
    before = copy.deepcopy((job.all_candidates, job.assembly_plan))
    assert approved["target_duration_s"] == total
    assert read_render_contract(job.assembly_plan).duration_s == total
    result = review_dispatch_duration(capture)
    assert result.status == "correction_required"
    assert result.candidate.operation == "restore_approved_total_duration"
    assert result.candidate.target_s == total
    assert result.candidate.binding_digest == binding.digest
    assert result.candidate.source_snapshot_digest == capture.digest
    assert binding.brief.requirements[0].facts["duration_s"] == scoped
    assert (job.all_candidates, job.assembly_plan) == before
    assert not result.recipe_checked


def test_current_recognizer_through_real_dispatch(monkeypatch):
    utterance = "Show the lineup sheet for 3 seconds then make it 20 seconds"
    observed = generative_jobs.recognize_total_duration_s
    job, _, _, capture = dispatch_case(monkeypatch, utterance, recognized=observed)
    actual = job.all_candidates["creator_strategy"].get("creator_duration_s")
    assert review_dispatch_duration(capture).status == (
        "consistent" if actual == 20 else "correction_required"
    )


def test_correct_factory_fixture_still_requires_recipe(monkeypatch):
    _, _, _, capture = dispatch_case(monkeypatch, "Make it 20 seconds", recognized=20)
    result = review_dispatch_duration(capture)
    assert result.status == "consistent"
    assert not result.recipe_checked


def test_scoped_only_never_becomes_global(monkeypatch):
    _, _, _, capture = dispatch_case(monkeypatch, "Show the sheet for 3 seconds", total=None)
    assert review_dispatch_duration(capture).status == "no_explicit_global"


def test_conflicting_globals_require_clarification(monkeypatch):
    job, binding, approved, _ = dispatch_case(monkeypatch, "Make it 20 seconds", recognized=20)
    brief = binding.brief.model_copy(deep=True)
    brief.requirements.append(
        BriefRequirement(
            id="conflict",
            kind="timing",
            scope="global",
            description="Whole output fifteen",
            facts={"duration_s": 15},
        )
    )
    binding = BriefBinding.create(binding.thread_id, brief, full_creator_request="15 or 20 seconds")
    job.assembly_plan = {
        **job.assembly_plan,
        "creator_brief_binding": binding.model_dump(mode="json"),
    }
    assert (
        review_dispatch_duration(
            capture_dispatch_duration(
                job,
                binding=binding,
                approved_plan=approved,
            )
        ).status
        == "clarification_needed"
    )


def test_capture_rejects_wrong_authority(monkeypatch):
    job, binding, approved, _ = dispatch_case(monkeypatch, "Make it 20 seconds", recognized=20)
    other = BriefBinding.create(
        binding.thread_id, binding.brief, full_creator_request="Other utterance"
    )
    with pytest.raises(ValueError, match="binding"):
        capture_dispatch_duration(job, binding=other, approved_plan=approved)


def test_pre_factory_candidate_is_overwritten(monkeypatch):
    job, _, approved, capture = dispatch_case(monkeypatch, "Shot for 3 seconds; make it 20 seconds")
    assert approved["target_duration_s"] == 20
    assert job.all_candidates["creator_strategy"]["target_duration_s"] == 3
    assert review_dispatch_duration(capture).status == "correction_required"


def test_snapshot_frozen_but_new_final_state_must_be_reaudited(monkeypatch):
    job, binding, _, capture = dispatch_case(monkeypatch, "Make it 20 seconds", recognized=20)
    job.all_candidates["creator_strategy"]["target_duration_s"] = 3
    assert review_dispatch_duration(capture).status == "consistent"
    fresh = capture_dispatch_duration(
        job,
        binding=binding,
        approved_plan=json.loads(capture.approved_plan_json),
    )
    assert fresh.digest != capture.digest
    assert review_dispatch_duration(fresh).status == "correction_required"


def test_real_native_save_recipe_is_checked_separately_and_preserves_shot_scope(monkeypatch):
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation
    from app.routes import generative_jobs as routes
    from app.services.device_render import device_status
    from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
    from tests.routes._editor_save_matrix import guided_phone_job

    _, binding, _, capture = dispatch_case(
        monkeypatch, "Shot for 3 seconds; make it 20 seconds", recognized=20
    )
    native_job, _, _ = guided_phone_job(monkeypatch, count=4, clip_s=5)
    variant = native_job.assembly_plan["variants"][0]
    snapshot = build_editor_snapshot(native_job, variant)
    raw = [
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"duration_s": 3}},
        {"op": "patch_slots", "selector": {"slot_indexes": [3]}, "patch": {"duration_s": 7}},
    ]
    state = EditorOperationParseState(confidence=0.99)
    ops = [parse_editor_operation(op, snapshot, state) for op in raw]
    assert all(ops)
    compiled = compile_editor_ops(native_job, variant, ops)
    routes.prepare_editor_commit(native_job, "guided_story", compiled.payload)
    recipe = device_status(native_job, "guided_story").request.recipe
    result = review_dispatch_duration(capture, compiled_recipe=recipe)
    assert result.status == "consistent" and result.recipe_checked
    clips = [clip for track in recipe.tracks for clip in track.clips]
    assert clips[0].source_duration / clips[0].rate == 3
    assert recipe.duration == 20
    assert [clip.source_asset_id for clip in clips] == [f"m{i}" for i in range(4)]
    assert binding.brief.requirements[0].scope == "clip:lineup-sheet"
    assert binding.brief.requirements[0].facts["duration_s"] == 3

    shorter_job, _, _ = guided_phone_job(monkeypatch, count=4, clip_s=3)
    shorter_recipe = device_status(shorter_job, "guided_story").request.recipe
    result = review_dispatch_duration(capture, compiled_recipe=shorter_recipe)
    assert result.status == "correction_required"
    assert "compiled_recipe.duration" in result.mismatches


def test_capture_rejects_replaced_generation(monkeypatch):
    job, binding, approved, _ = dispatch_case(monkeypatch, "Make it 20 seconds", recognized=20)
    job.assembly_plan = {**job.assembly_plan, "creator_generation_id": "newer"}
    with pytest.raises(ValueError, match="generation"):
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)


def test_ceiling_is_not_silently_converted_to_exact_duration(monkeypatch):
    job, binding, approved, _ = dispatch_case(monkeypatch, "At most 20 seconds", recognized=20)
    brief = binding.brief.model_copy(deep=True)
    brief.requirements[-1].facts["duration_bound"] = "at_most"
    binding = BriefBinding.create(
        binding.thread_id, brief, full_creator_request="At most 20 seconds"
    )
    job.assembly_plan = {
        **job.assembly_plan,
        "creator_brief_binding": binding.model_dump(mode="json"),
    }
    result = review_dispatch_duration(
        capture_dispatch_duration(
            job,
            binding=binding,
            approved_plan=approved,
        )
    )
    assert result.status == "consistent"
    assert result.candidate is None


def test_approved_choice_can_supply_global_authority(monkeypatch):
    from app.services.choice_questions import CONFLICT_LENGTH_VS_NATURAL

    job, binding, approved, _ = dispatch_case(
        monkeypatch,
        "Use the approved twenty seconds",
        total=None,
        recognized=20,
    )
    binding = BriefBinding.create(
        binding.thread_id,
        binding.brief,
        full_creator_request="Use twenty seconds",
        choice_answers=[{"conflict": CONFLICT_LENGTH_VS_NATURAL, "duration_s": 20}],
    )
    job.assembly_plan = {
        **job.assembly_plan,
        "creator_brief_binding": binding.model_dump(mode="json"),
    }
    result = review_dispatch_duration(
        capture_dispatch_duration(
            job,
            binding=binding,
            approved_plan=approved,
        )
    )
    assert result.status == "consistent"
    assert not result.recipe_checked


@pytest.mark.parametrize(
    "values",
    [[True], ["20"], [None], [-1], [0], [float("nan")], [float("inf")], [15, 20], [20, 15]],
)
@pytest.mark.parametrize("global_total", [None, 20])
def test_invalid_or_conflicting_approved_choices_never_authorize_correction(
    monkeypatch, values, global_total
):
    from app.services.choice_questions import CONFLICT_LENGTH_VS_NATURAL

    job, binding, approved, _ = dispatch_case(
        monkeypatch, "Use the agreed duration", total=global_total, recognized=3
    )
    binding = BriefBinding.create(
        binding.thread_id,
        binding.brief,
        full_creator_request="Use the agreed duration",
        choice_answers=[
            {"conflict": CONFLICT_LENGTH_VS_NATURAL, "duration_s": value} for value in values
        ],
    )
    job.assembly_plan = {
        **job.assembly_plan,
        "creator_brief_binding": binding.model_dump(mode="python"),
    }
    result = review_dispatch_duration(
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    )
    assert result.status == "clarification_needed"
    assert result.candidate is None


def test_valid_global_and_different_approved_choice_require_clarification(monkeypatch):
    from app.services.choice_questions import CONFLICT_LENGTH_VS_NATURAL

    job, binding, approved, _ = dispatch_case(monkeypatch, "Make it 20 seconds", recognized=20)
    binding = BriefBinding.create(
        binding.thread_id,
        binding.brief,
        full_creator_request="20 or15",
        choice_answers=[{"conflict": CONFLICT_LENGTH_VS_NATURAL, "duration_s": 15}],
    )
    job.assembly_plan = {
        **job.assembly_plan,
        "creator_brief_binding": binding.model_dump(mode="json"),
    }
    result = review_dispatch_duration(
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    )
    assert result.status == "clarification_needed" and result.candidate is None


@pytest.mark.parametrize("clip_s,expected", [(3, "consistent"), (6, "clarification_needed")])
def test_ceiling_recipe_does_not_propose_exact_repair(monkeypatch, clip_s, expected):
    from app.services.device_render import device_status
    from tests.routes._editor_save_matrix import guided_phone_job

    job, binding, approved, _ = dispatch_case(monkeypatch, "At most 20 seconds", recognized=20)
    brief = binding.brief.model_copy(deep=True)
    brief.requirements[-1].facts["duration_bound"] = "at_most"
    binding = BriefBinding.create(
        binding.thread_id, brief, full_creator_request="At most 20 seconds"
    )
    job.assembly_plan = {
        **job.assembly_plan,
        "creator_brief_binding": binding.model_dump(mode="json"),
    }
    capture = capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    native_job, _, _ = guided_phone_job(monkeypatch, count=4, clip_s=clip_s)
    result = review_dispatch_duration(
        capture, compiled_recipe=device_status(native_job, "guided_story").request.recipe
    )
    assert result.status == expected
    assert result.recipe_checked
    assert result.candidate is None
