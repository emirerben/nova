"""Offline KRI-524 regression for persistent stacked titles and follow-up edits."""

from __future__ import annotations

from app.kria.api_schemas import EditorCommitRequest
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from tests.evals.runners.snapshot_variant import build_synthetic_job
from tests.services.test_kria_editor_ops import _job

from .checkers import run_checker
from .models import FinalPlan, PlanClip, PlanText, Requirement
from .runner import _plan_with_texts, _plan_with_timeline_slots


def _variant() -> dict:
    return {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "kri524-gen",
        "resolved_archetype": "montage",
        "duration_s": 12.0,
        "text_elements": [
            {
                "id": "title-main",
                "role": "title",
                "text": "An afternoon in Kadıköy",
                "start_s": 0.0,
                "end_s": 2.0,
                "position": "middle",
                "alignment": "center",
                "x_frac": 0.5,
                "y_frac": 0.5,
            },
            {
                "id": "title-sub",
                "role": "title",
                "text": "Istanbul food walk",
                "start_s": 0.0,
                "end_s": 2.0,
                "position": "middle",
                "alignment": "center",
                "x_frac": 0.5,
                "y_frac": 0.5,
            },
            {
                "id": "location",
                "role": "label",
                "text": "Kadıköy Market",
                "start_s": 2.0,
                "end_s": 5.0,
                "position": "custom",
                "alignment": "left",
                "x_frac": 0.12,
                "y_frac": 0.16,
            },
        ],
        "ai_timeline": {
            "slots": [
                {
                    "slot_id": "shot-1",
                    "clip_index": 0,
                    "in_s": 1.0,
                    "duration_s": 6.0,
                    "source_duration_s": 20.0,
                    "removed": False,
                },
                {
                    "slot_id": "shot-2",
                    "clip_index": 1,
                    "in_s": 4.0,
                    "duration_s": 6.0,
                    "source_duration_s": 15.0,
                    "removed": False,
                },
            ]
        },
    }


def _plan(variant: dict) -> FinalPlan:
    return FinalPlan(
        total_duration_s=12.0,
        clips=[
            PlanClip(clip_id="shot-1", start_s=0, end_s=6, source_start_s=1),
            PlanClip(clip_id="shot-2", start_s=6, end_s=12, source_start_s=4),
        ],
        texts=[PlanText.model_validate(row) for row in variant["text_elements"]],
    )


def _req(checker: str, params: dict) -> Requirement:
    return Requirement(
        id=checker,
        kind="style" if checker == "text_geometry" else "text",
        request_type="style" if checker == "text_geometry" else "title_exact",
        checker=checker,
        params=params,
    )


def test_real_compiler_preserves_two_full_span_titles_and_top_left_label():
    variant = _variant()
    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "set_text_timing", "bar_index": 0, "start_s": 0.0, "end_s": 12.0},
            {"op": "set_text_timing", "bar_index": 1, "start_s": 0.0, "end_s": 12.0},
            {
                "op": "patch_text_style",
                "bar_indexes": [0, 1],
                "patch": {
                    "position": "custom",
                    "alignment": "left",
                    "x_frac": 0.12,
                    "font_family": "Inter",
                },
            },
            {"op": "patch_text_style", "bar_index": 0, "patch": {"y_frac": 0.86}},
            {"op": "patch_text_style", "bar_index": 1, "patch": {"y_frac": 0.94}},
            {
                "op": "patch_text_style",
                "bar_index": 2,
                "patch": {
                    "position": "custom",
                    "alignment": "left",
                    "x_frac": 0.12,
                    "y_frac": 0.16,
                },
            },
        ],
    )
    payload = compiled.payload.model_dump(mode="json", exclude_none=True)
    observed = _plan_with_texts(_plan(variant), payload["text_elements"])

    # Validate the real save payload on a synthetic Job, without DB or rendering.
    save_variant = {
        **variant,
        "text_elements": [{**row, "role": "generative_intro"} for row in variant["text_elements"]],
    }
    save_payload = compiled.payload.model_copy(
        update={
            "text_elements": [
                {**row, "role": "generative_intro"} for row in payload["text_elements"]
            ]
        }
    )
    gj.prepare_editor_commit(
        build_synthetic_job(save_variant),
        variant["variant_id"],
        EditorCommitRequest.model_validate(save_payload),
    )

    assert (
        run_checker(
            _req(
                "title_persistent",
                {"title_ids": ["title-main", "title-sub"]},
            ),
            observed,
            _job(variant),
        )[0]
        == "met"
    )
    assert (
        run_checker(
            _req(
                "source_preserved",
                {
                    "source_ranges": [
                        {"clip_id": "shot-1", "source_start_s": 1.0},
                        {"clip_id": "shot-2", "source_start_s": 4.0},
                    ]
                },
            ),
            observed,
            _job(variant),
        )[0]
        == "unmet"
    )  # source ends are intentionally unknown in this cassette
    assert (
        run_checker(
            _req(
                "text_geometry",
                {
                    "texts": {
                        "title-main": {
                            "position": "custom",
                            "alignment": "left",
                            "x_frac": 0.12,
                            "y_frac": 0.86,
                        },
                        "title-sub": {
                            "position": "custom",
                            "alignment": "left",
                            "x_frac": 0.12,
                            "y_frac": 0.94,
                        },
                        "location": {
                            "position": "custom",
                            "alignment": "left",
                            "x_frac": 0.12,
                            "y_frac": 0.16,
                        },
                    }
                },
            ),
            observed,
            _job(variant),
        )[0]
        == "met"
    )

    mutated = observed.model_copy(
        update={
            "texts": [
                text.model_copy(update={"y_frac": 0.2}) if text.id == "title-main" else text
                for text in observed.texts
            ]
        }
    )
    assert (
        run_checker(
            _req("text_geometry", {"texts": {"title-main": {"y_frac": 0.86}}}),
            mutated,
            _job(variant),
        )[0]
        == "unmet"
    )


def test_followup_style_compile_uses_projected_text_state_and_does_not_replan_timeline():
    variant = _variant()
    first = compile_editor_ops(
        _job(variant),
        variant,
        [
            {"op": "set_text_timing", "bar_index": 0, "start_s": 0.0, "end_s": 12.0},
            {"op": "set_text_timing", "bar_index": 1, "start_s": 0.0, "end_s": 12.0},
            {
                "op": "patch_text_style",
                "bar_indexes": [0, 1],
                "patch": {
                    "position": "custom",
                    "alignment": "left",
                    "x_frac": 0.12,
                    "font_family": "Inter",
                },
            },
            {"op": "patch_text_style", "bar_index": 0, "patch": {"y_frac": 0.86}},
            {"op": "patch_text_style", "bar_index": 1, "patch": {"y_frac": 0.94}},
        ],
    ).payload.model_dump(mode="json", exclude_none=True)
    projected = project_editor_draft(variant, first)
    second = compile_editor_ops(
        _job(projected),
        projected,
        [{"op": "patch_text_style", "bar_index": 2, "patch": {"font_family": "DM Sans"}}],
    ).payload.model_dump(mode="json", exclude_none=True)

    assert second.get("timeline_slots") is None
    assert [(row["id"], row["start_s"], row["end_s"]) for row in second["text_elements"]] == [
        ("title-main", 0.0, 12.0),
        ("title-sub", 0.0, 12.0),
        ("location", 2.0, 5.0),
    ]
    assert [row["text"] for row in second["text_elements"]] == [
        "An afternoon in Kadıköy",
        "Istanbul food walk",
        "Kadıköy Market",
    ]
    assert [
        (row["position"], row["alignment"], row["x_frac"], row["y_frac"])
        for row in second["text_elements"][:2]
    ] == [
        ("custom", "left", 0.12, 0.86),
        ("custom", "left", 0.12, 0.94),
    ]
    assert [row["font_family"] for row in second["text_elements"]] == ["Inter", "Inter", "DM Sans"]


def test_timeline_projection_uses_output_duration_for_speed_and_skips_unknown_beats():
    projected = _plan_with_timeline_slots(
        FinalPlan(total_duration_s=12),
        [
            {
                "media_id": "fast",
                "in_s": 2.0,
                "duration_s": 6.0,
                "playback_rate": 2.0,
            },
            {"media_id": "beat-sized", "duration_beats": 4, "duration_s": None},
        ],
    )
    assert [(clip.clip_id, clip.start_s, clip.end_s) for clip in projected.clips] == [
        ("fast", 0.0, 3.0)
    ]
    assert projected.duration_s == 3.0
    assert projected.timing_verified is False
    assert (
        _plan_with_timeline_slots(
            projected, [{"media_id": "fast", "in_s": 2, "duration_s": 6, "playback_rate": 2}]
        ).timing_verified
        is False
    )
    title_req = _req("title_persistent", {"title_ids": ["title-main"]})
    assert (
        run_checker(
            title_req,
            projected.model_copy(
                update={
                    "texts": [PlanText(id="title-main", role="title", text="x", start_s=0, end_s=3)]
                }
            ),
            _job(_variant()),
        )[0]
        == "unmet"
    )
    assert (
        _plan_with_timeline_slots(
            FinalPlan(total_duration_s=12), [{"media_id": "removed", "removed": True}]
        ).duration_s
        == 0.0
    )


def test_multiturn_speed_projection_carries_source_duration_once():
    first = _plan_with_timeline_slots(
        FinalPlan(),
        [{"media_id": "fast", "in_s": 2.0, "duration_s": 6.0, "playback_rate": 2.0}],
    )
    second = _plan_with_timeline_slots(
        first,
        [
            {
                "media_id": "fast",
                "in_s": first.clips[0].source_start_s,
                "duration_s": first.clips[0].source_duration_s,
                "playback_rate": first.clips[0].playback_rate,
            }
        ],
    )
    assert first.duration_s == second.duration_s == 3.0


def test_trim_compile_preserves_source_boundary_and_original_audio_level():
    variant = _variant()
    variant.update({"render_destination": "device", "original_audio_level": 0.4})
    compiled = compile_editor_ops(
        _job(variant), variant, [{"op": "set_clip_in", "slot_index": 0, "in_s": 3.0}]
    )
    slot = compiled.payload.timeline_slots[0]
    assert slot.in_s == 3.0
    assert slot.duration_s == 6.0
    assert variant["ai_timeline"]["slots"][0]["source_duration_s"] == 20.0
    observed = FinalPlan(
        clips=[
            PlanClip(
                clip_id="shot-1",
                start_s=0,
                end_s=6,
                source_start_s=slot.in_s,
                source_end_s=slot.in_s + slot.duration_s,
            )
        ]
    )
    source_req = _req(
        "source_preserved",
        {"source_ranges": [{"clip_id": "shot-1", "source_start_s": 3, "source_end_s": 9}]},
    )
    assert run_checker(source_req, observed, _job(variant))[0] == "met"
    changed = observed.model_copy(
        update={
            "clips": [
                observed.clips[0].model_copy(update={"source_start_s": 4, "source_end_s": 10})
            ]
        }
    )
    assert run_checker(source_req, changed, _job(variant))[0] == "unmet"
    projected = project_editor_draft(variant, compiled.payload.model_dump(mode="json"))
    assert projected["original_audio_level"] == 0.4
