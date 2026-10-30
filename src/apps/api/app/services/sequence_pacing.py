"""Resize an ordered text sequence while retaining its original pacing."""

from copy import deepcopy
from math import fsum, isfinite
from typing import Literal

_MIN_DURATION_S = 0.25


class SequenceReadabilityError(ValueError):
    def __init__(self, minimum_span_s: float, message: str):
        self.minimum_span_s = minimum_span_s
        super().__init__(message)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def retime_sequence(
    rows: list[dict],
    start: float,
    end: float,
    *,
    gap_policy: Literal["scale", "close", "fixed"] = "scale",
) -> list[dict]:
    """Return copied rows with new bounds and the last authored timing baseline.

    Positive gaps scale with the whole span by default. Word durations share the
    remaining budget proportionally, with a 250 ms minimum for every word.
    Manual timing edits replace the whole baseline; our own projections retain it.
    """
    start = _number(start, "Sequence start")
    end = _number(end, "Sequence end")
    if start < 0 or end < start:
        raise ValueError("Sequence bounds must be nonnegative with end at or after start")
    if gap_policy not in {"scale", "close", "fixed"}:
        raise ValueError(f"Unknown sequence gap policy: {gap_policy!r}")
    if not rows:
        return []

    current_times = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Sequence row {index} must be an object")
        row_start = _number(row.get("start_s"), f"Row {index} start")
        row_end = _number(row.get("end_s"), f"Row {index} end")
        if row_start < 0 or row_end <= row_start:
            raise ValueError(f"Row {index} requires nonnegative bounds and a positive duration")
        params = row.get("source_params")
        if params is not None and not isinstance(params, dict):
            raise ValueError(f"Row {index} source_params must be an object")
        current_times.append((row_start, row_end))

    current_pacing = [
        (
            row_end - row_start,
            max(0.0, current_times[index + 1][0] - row_end) if index + 1 < len(rows) else 0.0,
        )
        for index, (row_start, row_end) in enumerate(current_times)
    ]
    baseline = []
    authored_timing_changed = False
    for index, row in enumerate(rows):
        params = row.get("source_params") or {}
        if "sequence_timing" in params:
            timing = params["sequence_timing"]
            if not isinstance(timing, dict):
                raise ValueError(f"Row {index} sequence_timing must be an object")
            duration = _number(timing.get("duration_s"), f"Row {index} baseline duration")
            gap = _number(timing.get("gap_after_s"), f"Row {index} baseline gap")
            if duration <= 0 or gap < 0 or (index == len(rows) - 1 and gap != 0):
                raise ValueError(f"Row {index} has invalid sequence timing baseline")
            if "applied_duration_s" in timing or "applied_gap_after_s" in timing:
                applied_duration = _number(
                    timing.get("applied_duration_s"), f"Row {index} applied duration"
                )
                applied_gap = _number(timing.get("applied_gap_after_s"), f"Row {index} applied gap")
                if (
                    applied_duration <= 0
                    or applied_gap < 0
                    or (index == len(rows) - 1 and applied_gap != 0)
                ):
                    raise ValueError(f"Row {index} has invalid sequence timing projection")
                current_duration, current_gap = current_pacing[index]
                if (
                    abs(current_duration - applied_duration) > 0.000002
                    or abs(current_gap - applied_gap) > 0.000002
                ):
                    authored_timing_changed = True
        else:
            duration, gap = current_pacing[index]
            timing = {"duration_s": duration, "gap_after_s": gap}
        baseline.append((duration, gap, timing))

    if authored_timing_changed:
        baseline = [
            (duration, gap, {**timing, "duration_s": duration, "gap_after_s": gap})
            for (duration, gap), (_, _, timing) in zip(current_pacing, baseline)
        ]

    try:
        total_duration = fsum(duration for duration, _, _ in baseline)
        total_gap = fsum(gap for _, gap, _ in baseline)
        baseline_span = fsum((total_duration, total_gap))
    except OverflowError as exc:
        raise ValueError("Sequence baseline span must be finite") from exc

    span = end - start
    if gap_policy == "scale":
        allocated_gaps = [span * (gap / baseline_span) for _, gap, _ in baseline]
    elif gap_policy == "fixed":
        allocated_gaps = [round(gap, 6) for _, gap in current_pacing]
    else:
        allocated_gaps = [0.0] * len(rows)
    gap_budget = fsum(allocated_gaps)
    word_budget = span - gap_budget
    minimum = len(rows) * _MIN_DURATION_S
    if word_budget < minimum - 1e-12:
        minimum_span = (
            minimum * (baseline_span / total_duration)
            if gap_policy == "scale"
            else minimum + gap_budget
        )
        if not isfinite(minimum_span):
            raise ValueError("The sequence has no finite readable duration")
        raise SequenceReadabilityError(
            minimum_span,
            f"Sequence minimum word duration is {minimum:g}s for {len(rows)} words; "
            f"{gap_policy} gap policy reserves {gap_budget:g}s of the {span:g}s span",
        )

    word_budget = max(word_budget, minimum)
    durations = [_MIN_DURATION_S] * len(rows)
    active = list(range(len(rows)))
    remaining = word_budget
    while active:
        weight_sum = fsum(baseline[index][0] for index in active)
        shares = {index: remaining * (baseline[index][0] / weight_sum) for index in active}
        clamped = [index for index in active if shares[index] < _MIN_DURATION_S]
        if not clamped:
            for index in active:
                durations[index] = shares[index]
            break
        clamped_set = set(clamped)
        active = [index for index in active if index not in clamped_set]
        remaining = word_budget - (len(rows) - len(active)) * _MIN_DURATION_S

    result = deepcopy(rows)
    cursor = start
    for index, row in enumerate(result):
        row["start_s"] = start if index == 0 else round(cursor, 6)
        cursor = fsum((cursor, durations[index]))
        row["end_s"] = end if index == len(rows) - 1 else round(cursor, 6)
        cursor = fsum((cursor, allocated_gaps[index]))
        params = row.get("source_params")
        if params is None:
            params = {}
            row["source_params"] = params
        params["sequence_timing"] = deepcopy(baseline[index][2])

    for index, row in enumerate(result):
        if row["end_s"] - row["start_s"] < _MIN_DURATION_S - 0.000002 or (
            index and row["start_s"] < result[index - 1]["end_s"]
        ):
            raise ValueError(
                "Sequence bounds cannot represent the minimum duration without overlap"
            )
        row["source_params"]["sequence_timing"].update(
            applied_duration_s=round(row["end_s"] - row["start_s"], 6),
            applied_gap_after_s=(
                round(max(0.0, result[index + 1]["start_s"] - row["end_s"]), 6)
                if index + 1 < len(result)
                else 0.0
            ),
        )
    return result
