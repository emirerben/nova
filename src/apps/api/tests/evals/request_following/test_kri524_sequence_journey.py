"""Offline KRI-524 sequence journey through parser, compiler, and save validation."""

from __future__ import annotations

import pytest

from app.agents._schemas.text_animation_phases import TextAnimationPhases
from app.pipeline.text_animation_phases import sample_text_phases
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import compile_editor_ops, project_editor_draft
from tests.agents.test_editor_text_sequence import _op, _snapshot, _variant
from tests.evals.runners.snapshot_variant import build_synthetic_job
from tests.test_edit_copilot import _parse


def test_sequence_journey_saves_generated_children_and_preserves_source_audio():
    variant = _variant()
    variant["original_audio_level"] = 0.4
    variant["ai_timeline"]["slots"] = [
        {
            "slot_id": "shot-1",
            "clip_index": 0,
            "in_s": 2.0,
            "duration_s": 9.0,
            "source_duration_s": 20.0,
            "removed": False,
        }
    ]
    snapshot = _snapshot()
    parsed = _parse([_op(["one", "two", "three"])], snapshot=snapshot)
    assert parsed.outcome == "proposed" and len(parsed.ops) == 1

    compiled = compile_editor_ops(build_synthetic_job(variant), variant, parsed.ops)
    rows = compiled.payload.text_elements
    assert [row["text"] for row in rows] == ["one", "two", "three"]
    assert [(row["start_s"], row["end_s"]) for row in rows] == [
        (0.0, 3.0),
        (3.0, 6.0),
        (6.0, 9.0),
    ]
    assert len({row["id"] for row in rows}) == 3
    assert all(row["font_family"] == "Inter" for row in rows)
    assert all(row["x_frac"] == 0.12 and row["y_frac"] == 0.18 for row in rows)
    assert all(row["animation_phases"]["entrance"] == "fade" for row in rows)
    assert all(row["animation_phases"]["exit"] == "fade" for row in rows)

    save_variant = {**variant, "text_elements": variant["text_elements"]}
    save_job = build_synthetic_job(save_variant)
    payload = compiled.payload.model_copy(update={"text_elements": rows})
    gj.prepare_editor_commit(save_job, variant["variant_id"], payload)
    projected = project_editor_draft(variant, payload.model_dump(mode="json"))
    assert projected["original_audio_level"] == 0.4
    assert projected["ai_timeline"]["slots"][0]["in_s"] == 2.0
    assert projected["ai_timeline"]["slots"][0]["duration_s"] == 9.0


def test_sequence_followup_style_edit_targets_generated_children_without_rewording():
    variant = _variant()
    first = compile_editor_ops(
        build_synthetic_job(variant),
        variant,
        _parse([_op(["one", "two", "three"])], snapshot=_snapshot()).ops,
    ).payload
    projected = project_editor_draft(variant, first.model_dump(mode="json"))
    snapshot = {
        **_snapshot(),
        "text_bars": projected["text_elements"],
    }
    parsed = _parse(
        [{"op": "patch_text", "selector": {"group": "all"}, "patch": {"font_family": "DM Sans"}}],
        snapshot=snapshot,
    )
    assert parsed.outcome == "proposed" and len(parsed.ops) == 1
    second = compile_editor_ops(build_synthetic_job(projected), projected, parsed.ops).payload
    assert [row["text"] for row in second.text_elements] == ["one", "two", "three"]
    assert [(row["start_s"], row["end_s"]) for row in second.text_elements] == [
        (0.0, 3.0),
        (3.0, 6.0),
        (6.0, 9.0),
    ]
    assert all(row["font_family"] == "DM Sans" for row in second.text_elements)


def test_compiled_sequence_phase_windows_have_deterministic_fade_boundaries():
    """Exercise the local phase sampler against actual compiler-created children.

    This is phase-level evidence only: it proves that each generated child owns
    an isolated timeline window and that fade samples are deterministic at its
    edges and midpoint. It does not claim a PNG, video, or phone export render.
    """
    variant = _variant()
    compiled = compile_editor_ops(
        build_synthetic_job(variant),
        variant,
        _parse([_op(["one", "two", "three"])], snapshot=_snapshot()).ops,
    ).payload
    rows = list(compiled.text_elements)
    assert len(rows) == 3

    def alpha_at(row, absolute_time: float) -> float:
        start = row["start_s"]
        duration = row["end_s"] - start
        phases = TextAnimationPhases.model_validate(row["animation_phases"])
        return sample_text_phases(phases, absolute_time - start, duration).alpha

    # At every exact handoff, both the outgoing and incoming child are
    # invisible. This catches accidental inclusive-end/inclusive-start windows.
    for boundary in [rows[0]["end_s"], rows[1]["end_s"]]:
        assert [alpha_at(row, boundary) for row in rows] == [0, 0, 0]

    # At each child's midpoint, exactly that child is fully visible and all
    # other compiled children are outside their windows.
    for active_index, row in enumerate(rows):
        midpoint = (row["start_s"] + row["end_s"]) / 2
        alphas = [alpha_at(candidate, midpoint) for candidate in rows]
        assert alphas[active_index] == 1
        assert [alpha for index, alpha in enumerate(alphas) if index != active_index] == [0, 0]

    # Sampling the same actual child/window twice must be stable, including the
    # fade envelopes immediately inside each edge.
    for row in rows:
        start = row["start_s"]
        end = row["end_s"]
        near_start = alpha_at(row, start + 0.1)
        near_end = alpha_at(row, end - 0.1)
        assert near_start == pytest.approx(near_end)
        assert 0 < near_start < 1
        assert alpha_at(row, start) == 0
        assert alpha_at(row, end) == 0
