from __future__ import annotations

from pathlib import Path

import pytest

from app.agents._schemas.text_element import TextElement
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)
from tests.evals.runners.snapshot_variant import build_synthetic_job
from tests.services.test_kria_editor_ops import _job
from tests.test_edit_copilot import _parse


def test_prompt_contract_shows_string_segments_equal_windows_and_fade_patch() -> None:
    prompt = Path(__file__).resolve().parents[2] / "prompts" / "edit_copilot_ops" / "text.txt"
    text = prompt.read_text()
    assert '"segments":["Morning coffee.","Afternoon walk.","Evening lights."]' in text
    assert "never objects and never per-segment start/end fields" in text
    assert "equal contiguous non-overlapping windows" in text
    assert '"entrance":"fade","exit":"fade"' in text
    assert "after_animation_of" in text and "does not control sequence windows" in text


def _variant(text: str = "one two three", *, start: float = 0.0, end: float = 9.0) -> dict:
    return {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "sequence-test",
        "resolved_archetype": "montage",
        "duration_s": end,
        "text_elements": [
            {
                "id": "title",
                "role": "generative_intro",
                "text": text,
                "start_s": start,
                "end_s": end,
                "font_family": "Inter",
                "size_px": 64,
                "color": "#FFFFFF",
                "position": "custom",
                "alignment": "left",
                "x_frac": 0.12,
                "y_frac": 0.18,
                "animation_phases": {
                    "entrance": "fade",
                    "exit": "fade",
                    "loop": "none",
                    "speed": 1,
                },
            }
        ],
        "caption_cues": [],
        "ai_timeline": {"slots": []},
    }


def _snapshot(text: str = "one two three") -> dict:
    return {
        "editor_ops_version": 2,
        "allowed_op_families": ["text"],
        "text_bars": [
            {
                "id": "title",
                "role": "generative_intro",
                "text": text,
                "start_s": 0.0,
                "end_s": 9.0,
                "font_family": "Inter",
                "size_px": 64,
                "color": "#FFFFFF",
                "position": "custom",
                "alignment": "left",
                "x_frac": 0.12,
                "y_frac": 0.18,
                "animation_phases": {
                    "entrance": "fade",
                    "exit": "fade",
                    "loop": "none",
                    "speed": 1,
                },
            }
        ],
    }


def _op(segments: list[str], **extra) -> dict:
    return {
        "op": "replace_text_sequence",
        "selector": {"ids": ["title"]},
        "segments": segments,
        **extra,
    }


def test_real_agent_parser_then_compiler_emits_one_atomic_three_chunk_sequence() -> None:
    parsed = _parse([_op(["one", "two", "three"])], snapshot=_snapshot())
    assert parsed.outcome == "proposed"
    compiled = compile_editor_ops(_job(_variant()), _variant(), parsed.ops)
    rows = compiled.payload.text_elements
    assert len(parsed.ops) == 1
    assert [row["text"] for row in rows] == ["one", "two", "three"]
    assert [(row["start_s"], row["end_s"]) for row in rows] == [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    assert len({row["id"] for row in rows}) == 3


def test_title_selector_follows_sequence_lineage_and_leaves_free_text_alone() -> None:
    variant = _variant()
    first = compile_editor_ops(
        _job(variant),
        variant,
        [
            {
                **_op(["one", "two", "three"]),
                "expected_source_text": "one two three",
                "target_ids": ["title"],
                "expected_count": 1,
            }
        ],
    )
    # This mirrors the API save boundary: TextElement validation/model_dump
    # drops unknown top-level fields but preserves the source_params blob.
    persisted_children = [
        TextElement.model_validate(row).model_dump() for row in first.payload.text_elements
    ]
    assert all(row["source_params"]["sequence_source_id"] == "title" for row in persisted_children)
    variant = {
        **variant,
        "text_elements": persisted_children
        + [
            {"id": "free", "role": "text", "text": "leave me", "start_s": 0, "end_s": 9},
        ],
    }
    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["text"],
        "text_bars": [
            {
                "id": row["id"],
                "role": row.get("role"),
                "text": row["text"],
                "start_s": row["start_s"],
                "end_s": row["end_s"],
                "source_params": row.get("source_params"),
            }
            for row in variant["text_elements"]
        ],
    }
    parsed = _parse(
        [{"op": "rewrite_text", "selector": {"group": "title"}, "text": "changed"}],
        snapshot=snapshot,
    )
    assert parsed.outcome == "proposed"
    assert parsed.ops[0]["target_ids"] == [
        "title::sequence-1",
        "title::sequence-2",
        "title::sequence-3",
    ]
    compiled = compile_editor_ops(_job(variant), variant, parsed.ops)
    assert [row["text"] for row in compiled.payload.text_elements[:3]] == [
        "changed",
        "changed",
        "changed",
    ]
    assert compiled.payload.text_elements[-1]["text"] == "leave me"


@pytest.mark.parametrize("bar_id", ["title", "kria-title"])
def test_explicit_title_role_is_a_title_selector_candidate(bar_id: str) -> None:
    snapshot = _snapshot()
    snapshot["text_bars"][0]["id"] = bar_id
    snapshot["text_bars"][0]["role"] = "title"
    parsed = _parse(
        [{"op": "rewrite_text", "selector": {"group": "title"}, "text": "updated"}],
        snapshot=snapshot,
    )
    assert parsed.outcome == "proposed"
    assert parsed.ops[0]["target_ids"] == [bar_id]


def test_real_save_round_trip_keeps_lineage_for_title_follow_up() -> None:
    variant = _variant()
    variant["text_elements"][0]["source_params"] = {
        "mode": "sequence",
        "identity": "intro-title",
    }
    job = build_synthetic_job(variant)
    first = compile_editor_ops(
        job,
        variant,
        _parse([_op(["one", "two", "three"])], snapshot=_snapshot()).ops,
    ).payload
    # Exercise the real save validator, then read the normalized saved variant
    # from the job rather than projecting directly from the pre-save payload.
    gj.prepare_editor_commit(job, variant["variant_id"], first)
    updated = next(
        item
        for item in job.assembly_plan["variants"]
        if item["variant_id"] == variant["variant_id"]
    )
    saved_rows = updated["text_elements"]
    assert all("sequence_source_id" not in row for row in saved_rows)
    assert all(row["source_params"]["mode"] == "sequence" for row in saved_rows)
    assert all(row["source_params"]["identity"] == "intro-title" for row in saved_rows)
    snapshot = build_editor_snapshot(job, updated)
    follow_up = _parse(
        [{"op": "patch_text", "selector": {"group": "title"}, "patch": {"font_family": "DM Sans"}}],
        snapshot=snapshot,
    )
    assert follow_up.outcome == "proposed"
    assert follow_up.ops[0]["target_ids"] == [
        "title::sequence-1",
        "title::sequence-2",
        "title::sequence-3",
    ]
    second = compile_editor_ops(job, updated, follow_up.ops).payload
    assert [row["text"] for row in second.text_elements] == [row["text"] for row in saved_rows]
    assert [(row["start_s"], row["end_s"]) for row in second.text_elements] == [
        (row["start_s"], row["end_s"]) for row in saved_rows
    ]
    assert all(row["font_family"] == "DM Sans" for row in second.text_elements)


def test_sixteen_and_seventeen_chunks_are_each_one_operation() -> None:
    for count in (16, 17):
        words = [f"w{i}" for i in range(count)]
        source = " ".join(words)
        variant = _variant(source, end=float(count))
        snapshot = _snapshot(source)
        parsed = _parse([_op(words)], snapshot=snapshot)
        assert parsed.outcome == "proposed"
        assert len(parsed.ops) == 1
        compiled = compile_editor_ops(_job(variant), variant, parsed.ops)
        assert len(compiled.payload.text_elements) == count


def test_sequence_inherits_geometry_font_color_and_independent_fade_phases() -> None:
    variant = _variant()
    op = _op(
        ["one", "two", "three"],
        patch={
            "animation_phases": {
                "entrance": "pop",
                "exit": "slide",
                "loop": "none",
                "speed": 1,
            }
        },
    )
    compiled = compile_editor_ops(
        _job(variant),
        variant,
        [
            {
                **op,
                "expected_source_text": "one two three",
                "target_ids": ["title"],
                "expected_count": 1,
                "selector": {"ids": ["title"]},
            },
        ],
    )
    rows = compiled.payload.text_elements
    assert all(row["font_family"] == "Inter" and row["x_frac"] == 0.12 for row in rows)
    assert all(row["animation_phases"]["entrance"] == "pop" for row in rows)
    assert all(row["animation_phases"]["exit"] == "slide" for row in rows)


def test_sequence_supports_single_chunk_and_unicode_whitespace_punctuation() -> None:
    source = "İstanbul —  café   closed"
    variant = _variant(source, end=4.0)
    snapshot = _snapshot(source)
    parsed = _parse(
        [_op(["İstanbul — café closed"], patch={"color": "#00FF00"})], snapshot=snapshot
    )
    assert parsed.ops and len(parsed.ops) == 1
    compiled = compile_editor_ops(_job(variant), variant, parsed.ops)
    assert len(compiled.payload.text_elements) == 1
    assert compiled.payload.text_elements[0]["text"] == "İstanbul — café closed"


def test_single_segment_style_update_preserves_identity_and_does_not_claim_split() -> None:
    variant = _variant("Hello")
    parsed = _parse([_op(["Hello"], patch={"color": "#00FF00"})], snapshot=_snapshot("Hello"))
    compiled = compile_editor_ops(_job(variant), variant, parsed.ops)
    assert compiled.payload.text_elements[0]["id"] == "title"
    assert "sequence_source_id" not in (
        compiled.payload.text_elements[0].get("source_params") or {}
    )
    assert "split" not in " ".join(compiled.changes).lower()


def test_single_unchanged_segment_is_not_a_split() -> None:
    parsed = _parse([_op(["Hello"])], snapshot=_snapshot("Hello"))
    assert parsed.ops == []


@pytest.mark.parametrize(
    "segments",
    [["one", "three"], ["one", "two", "two", "three"], ["one", "four", "three"]],
)
def test_sequence_rejects_conservation_errors(segments: list[str]) -> None:
    parsed = _parse([_op(segments)], snapshot=_snapshot())
    assert parsed.ops == []


def test_sequence_rejects_ambiguous_or_multiple_targets() -> None:
    snapshot = {
        **_snapshot(),
        "text_bars": _snapshot()["text_bars"]
        + [{"id": "other", "text": "other", "start_s": 0, "end_s": 1}],
    }
    assert (
        _parse([_op(["one", "two", "three"], selector={"group": "all"})], snapshot=snapshot).ops
        == []
    )


def test_sequence_rejects_more_than_one_hundred_segments_and_stale_source() -> None:
    assert _parse([_op(["one"] * 101)], snapshot=_snapshot()).ops == []
    stale = _parse([_op(["one", "two", "three"])], snapshot=_snapshot("one two four"))
    assert stale.ops == []


def test_sequence_rejects_stale_compiler_id_and_source_without_partial_mutation() -> None:
    variant = _variant()
    bad = {
        **_op(["one", "two", "three"]),
        "expected_source_text": "one two three",
        "target_ids": ["missing"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [bad])
    assert [row["id"] for row in variant["text_elements"]] == ["title"]


def test_sequence_rejects_stale_source_copy_even_when_target_id_is_current() -> None:
    variant = _variant()
    op = {
        **_op(["one", "two", "three"]),
        "expected_source_text": "one two four",
        "target_ids": ["title"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [op])
    assert variant["text_elements"][0]["text"] == "one two three"


@pytest.mark.parametrize(
    "segments",
    [["one", 2, "three"], ["one", "", "two three"], ["one", "x" * 501, "two three"]],
)
def test_compiler_revalidates_segment_types_and_limits(segments: list[object]) -> None:
    variant = _variant()
    op = {
        **_op(segments),
        "expected_source_text": "one two three",
        "target_ids": ["title"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [op])
    assert variant["text_elements"][0]["text"] == "one two three"


@pytest.mark.parametrize("patch", [None, {}, {"unknown": True}, {"size_scale": "bad"}])
def test_compiler_rejects_invalid_sequence_patch_without_mutation(patch: object) -> None:
    variant = _variant()
    op = {
        **_op(["one", "two", "three"]),
        "patch": patch,
        "expected_source_text": "one two three",
        "target_ids": ["title"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [op])
    assert [row["id"] for row in variant["text_elements"]] == ["title"]


def test_compiler_rejects_rounded_zero_duration_windows_before_mutation() -> None:
    variant = _variant(start=0.0, end=0.000001)
    op = {
        **_op(["one", "two", "three"]),
        "expected_source_text": "one two three",
        "target_ids": ["title"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError, match="zero-duration"):
        compile_editor_ops(_job(variant), variant, [op])
    assert [row["id"] for row in variant["text_elements"]] == ["title"]


def test_sequence_rejects_child_id_collision_without_mutating_input() -> None:
    variant = _variant()
    variant["text_elements"].append(
        {"id": "title::sequence-1", "role": "other", "text": "existing", "start_s": 0, "end_s": 1}
    )
    op = {
        **_op(["one", "two", "three"]),
        "expected_source_text": "one two three",
        "target_ids": ["title"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [op])
    assert [row["id"] for row in variant["text_elements"]] == ["title", "title::sequence-1"]


def test_sequence_is_unavailable_without_v2_capability() -> None:
    parsed = _parse(
        [_op(["one", "two", "three"])], snapshot={**_snapshot(), "editor_ops_version": 1}
    )
    assert parsed.ops == []


def test_sequence_allows_more_than_selector_limit_existing_rows_under_output_cap() -> None:
    source = "one " + " ".join(f"word-{index}" for index in range(99))
    variant = _variant(source, end=100.0)
    variant["text_elements"] += [
        {"id": f"existing-{index}", "role": "other", "text": "x", "start_s": 0, "end_s": 1}
        for index in range(101)
    ]
    op = {
        **_op(["one", *[f"word-{index}" for index in range(99)]]),
        "expected_source_text": source,
        "target_ids": ["title"],
        "expected_count": 1,
    }
    compiled = compile_editor_ops(_job(variant), variant, [op])
    assert len(compiled.payload.text_elements) == 201


def test_sequence_rejects_source_without_valid_window_and_preserves_atomicity() -> None:
    variant = _variant(start=3.0, end=3.0)
    op = {
        **_op(["one", "two", "three"]),
        "expected_source_text": "one two three",
        "target_ids": ["title"],
        "expected_count": 1,
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [op])
    assert variant["text_elements"][0]["text"] == "one two three"
