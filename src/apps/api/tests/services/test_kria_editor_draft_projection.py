"""KRI-219 review fixes: multi-turn draft projection keeps every lane."""

from __future__ import annotations

from app.agents.editor_ops_v2 import register_all_handlers
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    merge_editor_draft,
    project_editor_draft,
)
from tests.services.test_kria_editor_ops import _job, _sfx_variant, _variant

register_all_handlers()


def _dump(compiled) -> dict:
    return compiled.payload.model_dump(mode="json", exclude_none=True)


def _add(variant: dict, effect_id: str, at_s: float):
    return _dump(
        compile_editor_ops(
            _job(variant),
            variant,
            [{"op": "add_sfx", "effect_id": effect_id, "at_s": at_s}],
        )
    )


def test_add_sfx_twice_keeps_both_placements() -> None:
    variant = _variant()
    first = _add(variant, "a", 1.0)
    projected = project_editor_draft(variant, first)
    second = _add(projected, "b", 2.0)
    merged = merge_editor_draft(first, second)
    assert [row["sound_effect_id"] for row in merged["sound_effects"]] == ["a", "b"]


def test_patch_and_remove_sfx_index_the_turn_one_draft() -> None:
    variant = _variant()
    first = _add(variant, "a", 1.0)
    projected = project_editor_draft(variant, first)
    patched = _dump(
        compile_editor_ops(
            _job(projected), projected, [{"op": "patch_sfx", "sfx_index": 0, "gain": 0.4}]
        )
    )
    assert patched["sound_effects"][0]["gain"] == 0.4
    removed = _dump(
        compile_editor_ops(_job(projected), projected, [{"op": "remove_sfx", "sfx_index": 0}])
    )
    assert removed["sound_effects"] == []


def test_camera_effects_project_across_turns() -> None:
    variant = _variant()
    op = {"op": "add_camera_effect", "start_s": 0.5, "end_s": 1.5, "intensity": 0.05}
    first = _dump(compile_editor_ops(_job(variant), variant, [op]))
    projected = project_editor_draft(variant, first)
    second = _dump(compile_editor_ops(_job(projected), projected, [op]))
    merged = merge_editor_draft(first, second)
    assert len(merged["camera_effects"]) == 2


def test_background_gain_projects_into_treatment() -> None:
    variant = _variant()
    variant["smart_music_treatment"] = {"track_id": "t1", "gain_db": -18.0}
    projected = project_editor_draft(
        variant, {"background_music": {"track_id": "t1", "gain_db": -9.0}}
    )
    assert projected["smart_music_treatment"]["gain_db"] == -9.0
    assert variant["smart_music_treatment"]["gain_db"] == -18.0


def test_original_level_only_mix_keeps_music_level_and_projects_original() -> None:
    variant = _variant()
    variant["render_destination"] = "device"
    variant["original_audio_level"] = 1.0
    projected = project_editor_draft(variant, {"mix": {"original_level": 0.2}})
    assert projected["mix"] == 0.5
    assert projected["original_audio_level"] == 0.2


def test_sfx_snapshot_lists_draft_placements(monkeypatch) -> None:
    variant = _sfx_variant()
    projected = project_editor_draft(
        variant, {"sound_effects": [{"id": "x", "at_s": 2.0}, {"id": "y", "at_s": 3.0}]}
    )
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True, "sfx": True},
    )
    placements = build_editor_snapshot(_job(projected), projected)["sfx"]["placements"]
    assert [p["id"] for p in placements] == ["x", "y"]
