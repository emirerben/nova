from copy import deepcopy

import pytest

from app.services.sequence_pacing import retime_sequence


def rows_with_gaps():
    return [
        {"id": "a", "start_s": 2.0, "end_s": 3.0},
        {"id": "b", "start_s": 3.5, "end_s": 5.5},
        {"id": "c", "start_s": 6.5, "end_s": 9.5},
    ]


def durations(rows):
    return [row["end_s"] - row["start_s"] for row in rows]


def gaps(rows):
    return [right["start_s"] - left["end_s"] for left, right in zip(rows, rows[1:])]


def test_uneven_durations_preserve_ratios_without_active_floors():
    result = retime_sequence(rows_with_gaps(), 10, 25)
    assert durations(result) == pytest.approx([2, 4, 6])
    assert gaps(result) == pytest.approx([1, 2])
    assert result[0]["start_s"] == 10
    assert result[-1]["end_s"] == 25


def test_compression_uses_waterfill_and_expansion_restores_original_pacing():
    rows = [
        {"start_s": 0, "end_s": 1},
        {"start_s": 1, "end_s": 4},
        {"start_s": 4, "end_s": 10},
    ]
    compressed = retime_sequence(rows, 0, 1)
    assert durations(compressed) == pytest.approx([0.25, 0.25, 0.5])
    expanded = retime_sequence(compressed, 5, 25)
    assert durations(expanded) == pytest.approx([2, 6, 12])
    assert expanded == retime_sequence(rows, 5, 25)


@pytest.mark.parametrize(
    ("policy", "expected_durations", "expected_gaps"),
    [
        ("scale", [2, 4, 6], [1, 2]),
        ("fixed", [2.25, 4.5, 6.75], [0.5, 1]),
        ("close", [2.5, 5, 7.5], [0, 0]),
    ],
)
def test_gap_policies(policy, expected_durations, expected_gaps):
    result = retime_sequence(rows_with_gaps(), 10, 25, gap_policy=policy)
    assert durations(result) == pytest.approx(expected_durations)
    assert gaps(result) == pytest.approx(expected_gaps)


def test_closing_gaps_keeps_baseline_for_later_restoration():
    closed = retime_sequence(rows_with_gaps(), 0, 10, gap_policy="close")
    restored = retime_sequence(closed, 2, 9.5)
    assert durations(restored) == pytest.approx([1, 2, 3])
    assert gaps(restored) == pytest.approx([0.5, 1])


@pytest.mark.parametrize("policy", ["scale", "fixed", "close"])
def test_retiming_same_bounds_is_idempotent(policy):
    once = retime_sequence(rows_with_gaps(), 1 / 30, 121 / 30, gap_policy=policy)
    assert retime_sequence(once, 1 / 30, 121 / 30, gap_policy=policy) == once


def test_frame_boundary_uses_shared_edges_and_exact_outer_bounds():
    rows = [{"start_s": index / 30, "end_s": (index + 1) / 30} for index in range(12)]
    start, end = 1 / 30, 92 / 30
    result = retime_sequence(rows, start, end)
    assert result[0]["start_s"] == start
    assert result[-1]["end_s"] == end
    assert min(durations(result)) >= 0.25 - 0.000002
    for left, right in zip(result, result[1:]):
        assert left["end_s"] == right["start_s"]
        assert left["end_s"] == round(left["end_s"], 6)


@pytest.mark.parametrize("policy", ["scale", "fixed", "close"])
def test_too_short_refuses_without_mutating_input(policy):
    rows = rows_with_gaps()
    original = deepcopy(rows)
    with pytest.raises(ValueError, match="minimum.*gap"):
        retime_sequence(rows, 0, 0.7, gap_policy=policy)
    assert rows == original


def test_open_fields_params_and_metadata_are_preserved_and_deepcopied():
    rows = rows_with_gaps()
    rows[0].update(text="hello", style={"color": [1, 2, 3]}, custom="retained")
    rows[0]["source_params"] = {"unknown": {"list": [42]}, "sequence_id": "seq"}
    original = deepcopy(rows)
    result = retime_sequence(rows, 0, 15)
    assert rows == original
    assert [row["id"] for row in result] == ["a", "b", "c"]
    for before, after in zip(rows, result):
        assert {
            k: v for k, v in after.items() if k not in {"start_s", "end_s", "source_params"}
        } == {k: v for k, v in before.items() if k not in {"start_s", "end_s", "source_params"}}
    assert result[0]["source_params"] == {
        **rows[0]["source_params"],
        "sequence_timing": {
            "duration_s": 1.0,
            "gap_after_s": 0.5,
            "applied_duration_s": 2.0,
            "applied_gap_after_s": 1.0,
        },
    }
    assert result[-1]["source_params"]["sequence_timing"]["gap_after_s"] == 0
    result[0]["source_params"]["unknown"]["list"].append(99)
    result[0]["style"]["color"].append(4)
    assert rows == original


def test_preexisting_overlap_becomes_zero_gap_without_sorting():
    rows = [{"id": "first", "start_s": 2, "end_s": 4}, {"id": "second", "start_s": 1, "end_s": 2}]
    result = retime_sequence(rows, 0, 3)
    assert [row["id"] for row in result] == ["first", "second"]
    assert durations(result) == [2, 1]
    assert gaps(result) == [0]


@pytest.mark.parametrize(
    "start,end", [(-1, 1), (2, 1), (0, float("nan")), (float("inf"), 2), (0, float("inf"))]
)
def test_invalid_target_bounds_fail(start, end):
    with pytest.raises(ValueError):
        retime_sequence(rows_with_gaps(), start, end)


@pytest.mark.parametrize(
    "start,end", [(-1, 1), (1, 1), (2, 1), (0, float("nan")), (0, float("inf"))]
)
def test_invalid_current_times_fail_atomically(start, end):
    rows = rows_with_gaps()
    rows[1].update(start_s=start, end_s=end)
    with pytest.raises(ValueError):
        retime_sequence(rows, 0, 10)
    assert "source_params" not in rows[0]


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"duration_s": 0, "gap_after_s": 0},
        {"duration_s": 1, "gap_after_s": -1},
        {"duration_s": float("nan"), "gap_after_s": 0},
        {"duration_s": 1, "gap_after_s": float("inf")},
    ],
)
def test_invalid_existing_metadata_fails(metadata):
    rows = rows_with_gaps()
    rows[1]["source_params"] = {"sequence_timing": metadata}
    with pytest.raises(ValueError):
        retime_sequence(rows, 0, 10)
    assert "source_params" not in rows[0]


def test_nonzero_last_baseline_gap_is_invalid():
    rows = rows_with_gaps()
    rows[-1]["source_params"] = {"sequence_timing": {"duration_s": 3, "gap_after_s": 1}}
    with pytest.raises(ValueError):
        retime_sequence(rows, 0, 10)


def test_empty_sequence_and_invalid_gap_policy():
    assert retime_sequence([], 0, 1) == []
    with pytest.raises(ValueError):
        retime_sequence(rows_with_gaps(), 0, 10, gap_policy="invalid")


def test_manual_word_duration_edit_rebaselines_whole_group():
    rows = retime_sequence(rows_with_gaps(), 0, 15)
    rows[0]["end_s"] = 1
    original = deepcopy(rows)
    result = retime_sequence(rows, 10, 40)
    assert durations(result) == pytest.approx([2, 8, 12])
    assert gaps(result) == pytest.approx([4, 4])
    assert [r["source_params"]["sequence_timing"]["duration_s"] for r in result] == [1, 4, 6]
    assert rows == original


def test_manual_gap_edit_becomes_new_scaled_pacing():
    rows = retime_sequence(rows_with_gaps(), 0, 15)
    rows[1]["start_s"] += 1
    rows[1]["end_s"] += 1
    result = retime_sequence(rows, 0, 30)
    assert durations(result) == pytest.approx([4, 8, 12])
    assert gaps(result) == pytest.approx([4, 2])
    assert [r["source_params"]["sequence_timing"]["gap_after_s"] for r in result] == [2, 1, 0]


def test_style_edits_and_uniform_shift_keep_original_baseline_after_floor_compression():
    original = [
        {"start_s": 0, "end_s": 1},
        {"start_s": 1, "end_s": 4},
        {"start_s": 4, "end_s": 10},
    ]
    rows = retime_sequence(original, 0, 1)
    for row in rows:
        row["start_s"] += 3.1415926
        row["end_s"] += 3.1415926
        row["source_params"].update(font_family="new font", position={"x": 0.7, "y": 0.1})
    result = retime_sequence(rows, 5, 25)
    assert durations(result) == pytest.approx([2, 6, 12])
    assert [r["source_params"]["sequence_timing"]["duration_s"] for r in result] == [1, 3, 6]
    assert all(row["source_params"]["font_family"] == "new font" for row in result)


def test_projection_matches_rounded_output_and_is_idempotent():
    rows = retime_sequence(rows_with_gaps(), 1 / 30, 121 / 30)
    for index, row in enumerate(rows):
        timing = row["source_params"]["sequence_timing"]
        assert timing["applied_duration_s"] == round(durations(rows)[index], 6)
        assert timing["applied_gap_after_s"] == round((gaps(rows) + [0])[index], 6)
    assert retime_sequence(rows, 1 / 30, 121 / 30) == rows


def test_legacy_metadata_keeps_canonical_behavior_without_projection():
    rows = rows_with_gaps()
    for row in rows:
        row["source_params"] = {"sequence_timing": {"duration_s": 1, "gap_after_s": 0}}
    result = retime_sequence(rows, 0, 3)
    assert durations(result) == [1, 1, 1]
    assert gaps(result) == [0, 0]


def test_rebaseline_preserves_foreign_metadata_and_source_params():
    rows = retime_sequence(rows_with_gaps(), 0, 15)
    rows[0]["source_params"]["sequence_timing"]["foreign"] = {"values": [1]}
    rows[0]["source_params"]["unrelated"] = {"values": [2]}
    rows[0]["end_s"] = 1
    original = deepcopy(rows)
    result = retime_sequence(rows, 0, 30)
    assert result[0]["source_params"]["sequence_timing"]["foreign"] == {"values": [1]}
    assert result[0]["source_params"]["unrelated"] == {"values": [2]}
    result[0]["source_params"]["sequence_timing"]["foreign"]["values"].append(3)
    result[0]["source_params"]["unrelated"]["values"].append(4)
    assert rows == original


@pytest.mark.parametrize(
    "projection",
    [
        {"applied_duration_s": 1},
        {"applied_gap_after_s": 0},
        {"applied_duration_s": 0, "applied_gap_after_s": 0},
        {"applied_duration_s": float("inf"), "applied_gap_after_s": 0},
        {"applied_duration_s": 1, "applied_gap_after_s": -1},
        {"applied_duration_s": 1, "applied_gap_after_s": None},
    ],
)
def test_malformed_projection_refuses_atomically_even_after_manual_edit(projection):
    rows = retime_sequence(rows_with_gaps(), 0, 15)
    rows[0]["end_s"] = 1
    rows[1]["source_params"]["sequence_timing"] = {
        "duration_s": 2,
        "gap_after_s": 1,
        **projection,
    }
    original = deepcopy(rows)
    with pytest.raises(ValueError):
        retime_sequence(rows, 0, 30)
    assert rows == original


def test_nonzero_last_projected_gap_is_invalid():
    rows = retime_sequence(rows_with_gaps(), 0, 15)
    rows[-1]["source_params"]["sequence_timing"]["applied_gap_after_s"] = 1
    with pytest.raises(ValueError):
        retime_sequence(rows, 0, 30)


def test_fixed_policy_preserves_current_gaps_after_previous_scale():
    rows = retime_sequence(rows_with_gaps(), 0, 15)
    result = retime_sequence(rows, 0, 30, gap_policy="fixed")
    assert gaps(result) == [1, 2]
    assert durations(result) == [4.5, 9, 13.5]


@pytest.mark.parametrize("delta,expected_baseline", [(0.000001, 1), (0.000003, 1.000003)])
def test_authored_timing_change_tolerance(delta, expected_baseline):
    rows = retime_sequence(rows_with_gaps(), 2, 9.5)
    rows[0]["end_s"] += delta
    result = retime_sequence(rows, 0, 15)
    assert result[0]["source_params"]["sequence_timing"]["duration_s"] == pytest.approx(
        expected_baseline, abs=1e-9
    )


def test_exact_readability_floor_accepts_floating_point_roundoff():
    rows = [
        {"start_s": 0, "end_s": 1.7006262228267517},
        {"start_s": 2.072855666882655, "end_s": 2.4152123385122293},
    ]
    result = retime_sequence(rows, 0, 0.5910995008979162)
    assert result[0]["start_s"] == 0
    assert result[-1]["end_s"] == 0.5910995008979162
    assert all(row["end_s"] - row["start_s"] >= 0.25 - 0.000002 for row in result)
    assert result[0]["end_s"] <= result[1]["start_s"]
    with pytest.raises(ValueError):
        retime_sequence(rows, 0, 0.5910995008979162 - 0.00001)


def test_fixed_gap_repeated_resize_is_stable_at_serialized_precision():
    from random import Random

    random = Random(1060)
    for _ in range(100):
        rows = []
        cursor = 0
        for _ in range(random.randint(2, 30)):
            duration = random.uniform(0.1, 3)
            rows.append({"start_s": cursor, "end_s": cursor + duration})
            cursor += duration + random.uniform(0, 0.8)
        target = cursor * 2
        once = retime_sequence(rows, 0, target, gap_policy="fixed")
        assert retime_sequence(once, 0, target, gap_policy="fixed") == once
