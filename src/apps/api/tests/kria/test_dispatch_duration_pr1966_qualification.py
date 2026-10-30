"""Exact PR1966 qualification. Real factory dispatch; provider and broker remain offline."""

import copy
import json
import uuid
from dataclasses import replace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.dispatch_duration_review import capture_dispatch_duration, review_dispatch_duration
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.creator_render_contract import read_render_contract
from app.services.length_policy import creator_duration_s
from tests.kria.test_kri1071_per_shot_seconds import M5
from tests.pipeline.test_unified_montage import clip
from tests.tasks.test_kri788_order_answer_dispatch import T1_STRATEGY, _dispatch, _item


def actual_dispatch(monkeypatch, utterance, *, total=20, hold=3, misfiled=False):
    item = _item()
    first = item.clip_assignments[0]
    second = copy.deepcopy(first)
    second.update(
        media_id="second",
        manifest_identity="second",
        gcs_path=first["gcs_path"].replace(".mp4", "-second.mp4"),
    )
    second["upload_contract"]["proxy"]["original"]["sha256"] = "d" * 64
    item.clip_assignments = [first, second]
    item.clip_gcs_paths = [row["gcs_path"] for row in item.clip_assignments]
    item.edit_format = "montage"
    requirements = [
        BriefRequirement(
            id="shot",
            kind="timing",
            scope="global" if misfiled else f"clip:{first['media_id']}",
            authority="explicit",
            description="Show this shot",
            facts={"duration_s": hold},
        )
    ]
    if total is not None:
        requirements.append(
            BriefRequirement(
                id="total",
                kind="timing",
                scope="global",
                description=f"Make it {total} seconds",
                facts={"duration_s": total},
            )
        )
    brief = CreativeBrief(version=1, requirements=requirements)
    approved = {
        **T1_STRATEGY,
        "target_duration_s": total or 20,
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "fast_montage",
        "opening_title": None,
        "selected_media_ids": [row["media_id"] for row in item.clip_assignments],
    }
    binding = BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message=utterance,
        full_creator_request=utterance,
        media_snapshot=snapshot_media(item),
    )
    job = _dispatch(monkeypatch, item, approved, binding.model_dump(mode="json"))
    job.id = uuid.uuid4()
    return job, binding, approved, item


@pytest.mark.parametrize(
    "utterance,total,hold",
    [
        (M5, 20, 3),
        ("Show the sheet for 3 seconds then make it 20 seconds", 20, 3),
        ("Hold the shot for 2 seconds then make it 15 seconds", 15, 2),
        ('Show the words "for 3 seconds" then make it 20 seconds', 20, 3),
    ],
)
def test_actual_corrected_factory_and_final_plan_agree(
    monkeypatch, tmp_path, utterance, total, hold
):
    job, binding, approved, item = actual_dispatch(monkeypatch, utterance, total=total, hold=hold)
    strategy = job.all_candidates["creator_strategy"]
    assert set(job.all_candidates["clip_paths"]) == set(item.clip_gcs_paths)
    assert strategy["target_duration_s"] == strategy["creator_duration_s"] == total
    assert read_render_contract(job.assembly_plan).duration_s == total
    assert (
        review_dispatch_duration(
            capture_dispatch_duration(job, binding=binding, approved_plan=approved)
        ).status
        == "consistent"
    )
    clips = [
        replace(
            clip(index, duration=row["duration_s"]),
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation=str(row["storage_generation"]),
        )
        for index, row in enumerate(item.clip_assignments)
    ]
    plan = plan_unified_montage(
        clips,
        brief_view(binding.brief),
        strategy=strategy,
        creator_duration_proven=creator_duration_s(brief=binding.brief) is not None,
    )
    assert plan.duration_s == pytest.approx(total, abs=1 / 30)
    assert binding.brief.requirements[0].facts["duration_s"] == hold
    (tmp_path / "dispatch-duration-trace.json").write_text(
        json.dumps(
            {
                "upstream_commit": "09026e996254d20603850a2079e39ed1be0f33c0",
                "original_request": utterance,
                "approved_plan": approved,
                "input_clip_assignments": item.clip_assignments,
                "scoped_source_planned_s": sum(
                    cut.output_duration_s
                    for cut in plan.snapshot.fast_cuts
                    if cut.media_id == item.clip_assignments[0]["media_id"]
                ),
                "frozen_brief": binding.brief.model_dump(mode="json"),
                "final_job_strategy": strategy,
                "contract": read_render_contract(job.assembly_plan).model_dump(mode="json"),
                "planned_duration_s": plan.duration_s,
                "planned_cuts": [cut.model_dump(mode="json") for cut in plan.snapshot.fast_cuts],
                "scoped_shot_fulfillment_verified": False,
                "qualification": "real dispatch/factory plus headless plan; no native export",
            },
            indent=2,
        )
    )


def test_pronoun_referring_to_shot_has_no_global_repair_authority(monkeypatch):
    job, binding, approved, _ = actual_dispatch(
        monkeypatch, "For this shot, make it 3 seconds", total=None
    )
    assert job.all_candidates["creator_strategy"]["creator_duration_s"] == 3
    result = review_dispatch_duration(
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    )
    assert result.status == "no_explicit_global" and result.candidate is None


def test_misfiled_global_hold_disagreeing_with_approved_plan_requires_clarification(monkeypatch):
    job, binding, approved, _ = actual_dispatch(
        monkeypatch, "Show the sheet for 3 seconds", total=None, misfiled=True
    )
    result = review_dispatch_duration(
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    )
    assert result.status == "clarification_needed" and result.candidate is None


def test_two_conflicting_globals_are_refused_by_real_dispatch(monkeypatch):
    with pytest.raises(AssertionError, match="invalid_clips"):
        actual_dispatch(
            monkeypatch, "Show the sheet for 3 seconds; make it 20 seconds", misfiled=True
        )


def test_quoted_make_it_remains_parser_risk_but_late_review_detects_it(monkeypatch):
    job, binding, approved, _ = actual_dispatch(
        monkeypatch,
        'Show the text "make it 3 seconds"; make it 20 seconds',
    )
    assert job.all_candidates["creator_strategy"]["creator_duration_s"] == 3
    result = review_dispatch_duration(
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    )
    assert result.status == "correction_required" and result.candidate.target_s == 20


def test_matching_wrong_global_pins_are_not_semantically_qualified(monkeypatch):
    job, binding, approved, _ = actual_dispatch(
        monkeypatch, "For this shot, make it 3 seconds", total=None, misfiled=True
    )
    approved = {**approved, "target_duration_s": 3}
    result = review_dispatch_duration(
        capture_dispatch_duration(job, binding=binding, approved_plan=approved)
    )
    assert result.status == "consistent"
    assert not result.recipe_checked
    assert result.candidate is None
