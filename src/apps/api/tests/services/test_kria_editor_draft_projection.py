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


# --- KRI-219: the phone reads `sections` first; chat edits must land there too ------------


def _bootstrap_payload(variant: dict) -> dict:
    """What `read_or_bootstrap_draft` stores when the editor is opened first."""
    from app.kria.drafts import _editor_snapshot

    return _editor_snapshot(variant, "gen-1")


def test_chat_text_edit_lands_in_sections_when_editor_was_opened_first() -> None:
    variant = _variant()
    prior = _bootstrap_payload(variant)
    assert "sections" in prior
    compiled = compile_editor_ops(
        _job(variant),
        project_editor_draft(variant, prior),
        [{"op": "edit_text", "bar_index": 0, "text": "skeeps"}],
    )
    merged = merge_editor_draft(prior, _dump(compiled))
    # The flat key (commit shape) AND the nested copy the iOS decoder reads.
    assert merged["text_elements"][0]["text"] == "skeeps"
    assert merged["sections"]["text_elements"][0]["text"] == "skeeps"
    # Untouched sections and the base generation survive.
    assert merged["base_generation"] == "gen-1"
    assert set(prior["sections"]) <= set(merged["sections"])


def test_chat_remove_music_nulls_the_track_inside_sections() -> None:
    variant = {**_variant(), "music_track_id": "track-1"}
    prior = _bootstrap_payload(variant)
    assert prior["sections"]["music_track_id"] == "track-1"
    merged = merge_editor_draft(prior, {"remove_music": True})
    assert merged["sections"]["music_track_id"] is None


def test_chat_mix_edit_updates_sections_mix_and_keeps_audio_mix_reader_working() -> None:
    variant = _variant()
    prior = _bootstrap_payload(variant)
    merged = merge_editor_draft(prior, {"mix": {"music_level": 0.3}})
    assert merged["sections"]["mix"]["music_level"] == 0.3


def test_flat_previous_payload_is_unchanged_by_the_sections_overlay() -> None:
    previous = {"text_elements": [{"text": "a"}], "base_generation": "g"}
    merged = merge_editor_draft(previous, {"text_elements": [{"text": "b"}]})
    assert "sections" not in merged
    assert merged["text_elements"] == [{"text": "b"}]
