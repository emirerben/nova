"""KRI-558: the compiler records what a bundle changed, so receipts can name it.

Failure modes this file pins (written before the code, one test each):
* a legacy ``effect`` / ``Inter-Bold`` alias reads as a change the creator never asked for;
* a no-op patch (value already set) reads as a change;
* a retime that only a clip edit caused (guided rebase) proves a text-timing request;
* ``size_scale`` on a row with no size loses its direction;
* removed / added rows or clips are missed, or clips are keyed wrong after a reorder;
* a describer phrase drifts between English and Turkish.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.kria.reply_language import reply_language_for
from app.services.kria_editor_ops import compile_editor_ops
from app.services.kria_editor_ops_diff import (
    color_family,
    describe_diff,
    font_family_key,
)


def _row(row_id: str, text: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": row_id,
        "text": text,
        "start_s": 1.0,
        "end_s": 9.0,
        "font_family": "Playfair Display",
        "size_px": 64,
        "color": "#FFFFFF",
        "effect": "static",
        **kw,
    }


def _job() -> Any:
    return SimpleNamespace(all_candidates={"clip_paths": []}, assembly_plan={}, id="job-1")


def _variant(rows: list[dict[str, Any]], slots: list[dict[str, Any]] | None = None) -> dict:
    variant: dict[str, Any] = {"variant_id": "v", "text_elements": rows}
    if slots is not None:
        variant["user_timeline"] = {"slots": slots}
    return variant


def _compile(rows, ops, slots=None):
    return compile_editor_ops(_job(), _variant(rows, slots), ops)


def _patch(ids: list[str], patch: dict[str, Any]) -> dict[str, Any]:
    return {
        "op": "patch_text",
        "selector": {"ids": ids},
        "target_ids": ids,
        "expected_count": len(ids),
        "patch": patch,
    }


ROWS = [_row("a", "Lisbon"), _row("b", "Part 1"), _row("c", "Day 1")]
IDS = ["a", "b", "c"]


def _fields(compiled) -> dict[str, int]:
    return {f"{e.lane}.{e.field}": len(e.targets) for e in compiled.diff.entries}


def test_fade_on_every_text_is_one_entrance_entry_naming_all_three() -> None:
    compiled = _compile(ROWS, [_patch(IDS, {"animation_phases": {"entrance": "fade"}})])
    assert _fields(compiled) == {"text.entrance": 3}
    assert describe_diff(compiled.diff.entries, live_text_count=3) == [
        "Added a fade-in animation to all 3 texts"
    ]


def test_legacy_effect_and_phases_are_the_same_entrance_so_no_fake_change() -> None:
    rows = [_row("a", "Lisbon", effect="fade-in")]
    compiled = _compile(rows, [_patch(["a"], {"animation_phases": {"entrance": "fade"}})])
    assert "text.entrance" not in _fields(compiled)


def test_font_alias_is_the_same_family_but_a_new_family_is_a_change() -> None:
    assert (
        font_family_key("Inter-Bold")
        == font_family_key("Inter")
        == font_family_key("Inter Regular")
    )
    assert font_family_key("Inter Tight") != font_family_key("Inter")
    rows = [_row("a", "Lisbon", font_family="Inter-Bold")]
    same = _compile(rows, [_patch(["a"], {"font_family": "Inter"})])
    assert same.diff.empty()
    other = _compile(rows, [_patch(["a"], {"font_family": "Alte Haas Grotesk"})])
    assert describe_diff(other.diff.entries, live_text_count=1) == [
        "Font → Alte Haas Grotesk on 'Lisbon'"
    ]


def test_a_patch_that_sets_what_is_already_there_is_not_a_change() -> None:
    compiled = _compile(ROWS, [_patch(IDS, {"color": "#ffffff"})])
    assert compiled.diff.empty()


def test_size_scale_keeps_its_direction_even_when_the_row_had_no_size() -> None:
    rows = [{k: v for k, v in _row("a", "Lisbon").items() if k != "size_px"}]
    compiled = _compile(rows, [_patch(["a"], {"size_scale": 0.8})])
    (entry,) = [e for e in compiled.diff.entries if e.field == "size"]
    assert entry.targets[0].after < entry.targets[0].before
    assert describe_diff(compiled.diff.entries, live_text_count=1)[0].startswith(
        "Made 'Lisbon' smaller"
    )


def test_lining_up_on_the_left_is_a_position_entry_not_a_move_of_everything() -> None:
    rows = [
        _row("a", "Lisbon", position="custom", x_frac=0.5, y_frac=0.84),
        _row("b", "Part 1", position="custom", x_frac=0.4, y_frac=0.91),
    ]
    compiled = _compile(rows, [_patch(["a", "b"], {"position": "custom", "x_frac": 0.08})])
    assert _fields(compiled)["text.position"] == 2
    assert describe_diff(compiled.diff.entries, live_text_count=2) == [
        "Lined up both texts on the left"
    ]


def test_removed_and_rewritten_texts_are_entries() -> None:
    compiled = _compile(
        ROWS,
        [
            {
                "op": "remove_texts",
                "selector": {"ids": ["c"]},
                "target_ids": ["c"],
                "expected_count": 1,
            }
        ],
    )
    assert _fields(compiled) == {"text.removed": 1}


def test_text_timing_without_a_timing_op_is_derived_and_never_a_real_change() -> None:
    from app.services.kria_editor_ops_diff import DiffEntry, DiffTarget, EditorDiff

    target = DiffTarget("a", "label", None, "Day 1", (0.0, 3.0), (0.5, 3.5))
    diff = EditorDiff(entries=(DiffEntry("text", "timing", (target,), derived=True),))
    assert diff.empty()
    assert describe_diff(diff.entries) == []


def test_slot_duration_removal_and_order_are_keyed_by_slot_id() -> None:
    slots = [
        {"slot_id": f"s{i}", "clip_index": i, "in_s": 0.0, "duration_s": 3.0} for i in range(4)
    ]
    compiled = _compile(
        ROWS,
        [
            {"op": "set_clip_duration", "slot_index": 1, "duration_s": 1.0},
            {"op": "reorder_clip", "from_index": 3, "to_index": 0},
            {"op": "remove_clip", "slot_index": 3},
        ],
        slots,
    )
    fields = _fields(compiled)
    assert fields["timeline.duration_s"] == 1
    assert fields["timeline.removed"] == 1
    assert fields["timeline.order"] >= 1
    assert "timeline.total_duration" in fields


def test_describer_speaks_turkish_when_the_turn_is_turkish() -> None:
    compiled = _compile(ROWS, [_patch(IDS, {"animation_phases": {"entrance": "fade"}})])
    with reply_language_for("tr"):
        (line,) = describe_diff(compiled.diff.entries, live_text_count=3)
    assert "Added" not in line and "yazının hepsi" in line


def test_color_family_buckets() -> None:
    assert color_family("#F2F2F2") == "white"
    assert color_family("#FFFFFF") == "white"
    assert color_family("#000000") == "black"
    assert color_family("#FFD24A") == "yellow"
    assert color_family("#FFD700") == "yellow"
    assert color_family("nope") is None
