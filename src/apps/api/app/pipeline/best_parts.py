"""Deterministic source-window selection for footage-owned montage timing.

The selector deliberately knows nothing about briefs, audio, or render schemas.
Callers supply the already-resolved source pool in its final order, then decide
whether footage owns the output duration.  This keeps a compiler and a phone
planner on the same source windows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_BEST_PARTS_DURATION_S = 120.0
MIN_INCLUDED_PART_S = 0.1


@dataclass(frozen=True)
class BestPart:
    media_id: str
    source_start_s: float
    source_end_s: float
    score: float
    reason: str

    @property
    def duration_s(self) -> float:
        return round(self.source_end_s - self.source_start_s, 3)

    def receipt(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "source_start_s": self.source_start_s,
            "source_end_s": self.source_end_s,
            "score": self.score,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BestPartsConflict:
    code: str
    requested_duration_s: float
    available_duration_s: float
    message: str


@dataclass(frozen=True)
class BestPartsResult:
    """Selected windows in input order, with non-silent capacity conflicts."""

    parts: tuple[BestPart, ...]
    requested_duration_s: float
    duration_s: float
    capacity_s: float
    conflicts: tuple[BestPartsConflict, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def receipt(self) -> list[dict[str, Any]]:
        return [part.receipt() for part in self.parts]


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_fields(source: Any) -> tuple[str, float, Mapping[str, Any]]:
    mapped_id = source.get("media_id") if isinstance(source, Mapping) else ""
    media_id = str(getattr(source, "media_id", "") or mapped_id)
    raw_duration = getattr(source, "duration_s", None)
    raw_analysis = getattr(source, "analysis", None)
    if isinstance(source, Mapping):
        raw_duration = source.get("duration_s", raw_duration)
        raw_analysis = source.get("analysis", raw_analysis)
    duration = _number(raw_duration)
    analysis = raw_analysis if isinstance(raw_analysis, Mapping) else {}
    return media_id, max(0.0, duration or 0.0), analysis


def _candidate(source: Any, minimum_duration_s: float = MIN_INCLUDED_PART_S) -> BestPart | None:
    media_id, duration, analysis = _source_fields(source)
    if not media_id or duration < MIN_INCLUDED_PART_S:
        return None
    candidates: list[tuple[float, float, float]] = []
    moments = analysis.get("best_moments")
    if isinstance(moments, list):
        for moment in moments:
            if not isinstance(moment, Mapping):
                continue
            start, end = _number(moment.get("start_s")), _number(moment.get("end_s"))
            if start is None or end is None:
                continue
            start, end = max(0.0, start), min(duration, end)
            if end - start < MIN_INCLUDED_PART_S:
                continue
            score = _number(moment.get("energy"))
            candidates.append((score if score is not None else 0.0, start, end))
    if candidates:
        # Higher energy wins; the earliest valid moment breaks ties deterministically.
        score, start, end = min(candidates, key=lambda row: (-row[0], row[1], row[2]))
        protected = min(duration, max(MIN_INCLUDED_PART_S, minimum_duration_s))
        if end - start < protected:
            # Keep the analysed action in view and extend around it when a
            # protected label hold needs more footage than the moment itself.
            end = min(duration, start + protected)
            start = max(0.0, end - protected)
        return BestPart(media_id, round(start, 3), round(end, 3), round(score, 3), "best_moment")
    # An empty, malformed, or out-of-range analysis must not change which source
    # is included. The fallback is explicit in the persisted receipt.
    return BestPart(media_id, 0.0, round(duration, 3), 0.0, "analysis_fallback")


def _fair_durations(
    capacities: Sequence[float], target: float, floors: Sequence[float]
) -> list[float]:
    """Water-fill ``target`` fairly, redistributing a source's capped share."""
    result = list(floors)
    remaining = target - sum(result)
    active = {index for index, capacity in enumerate(capacities) if capacity > floors[index]}
    while remaining > 1e-8 and active:
        share = remaining / len(active)
        used = 0.0
        capped: set[int] = set()
        for index in active:
            room = capacities[index] - result[index]
            add = min(share, room)
            result[index] += add
            used += add
            if room <= share + 1e-8:
                capped.add(index)
        remaining -= used
        active -= capped
        if used <= 1e-8:
            break
    return [round(value, 3) for value in result]


def _extend_around(
    part: BestPart, *, source_duration_s: float, duration_s: float, reason: str
) -> BestPart:
    """Grow a chosen window around its moment without moving it off-source."""

    current = part.duration_s
    target = min(source_duration_s, max(current, duration_s))
    extra = target - current
    if extra <= 1e-8:
        return part
    # Prefer equal expansion on both sides, then spend the remaining room on
    # whichever side still fits. This keeps the analysed moment central whenever
    # the source permits it and remains deterministic at either source edge.
    left = min(extra / 2, part.source_start_s)
    right = min(extra - left, source_duration_s - part.source_end_s)
    left = min(left + max(0.0, extra - left - right), part.source_start_s)
    right = min(right + max(0.0, extra - left - right), source_duration_s - part.source_end_s)
    return BestPart(
        part.media_id,
        round(part.source_start_s - left, 3),
        round(part.source_end_s + right, 3),
        part.score,
        reason if part.reason == "best_moment" else part.reason,
    )


def select_best_parts(
    sources: Sequence[Any],
    *,
    target_duration_s: float | None = None,
    cap_duration_s: float = MAX_BEST_PARTS_DURATION_S,
    minimum_duration_s: Mapping[str, float] | None = None,
    maximum_duration_s: Mapping[str, float] | None = None,
    minimum_total_duration_s: float | None = None,
) -> BestPartsResult:
    """Choose every source's strongest valid window and allocate its duration.

    Results preserve ``sources`` order. A target is never met by dropping a
    source or silently clipping below 100 ms: callers receive a typed conflict
    and can surface or route it before rendering.
    """
    cap = _number(cap_duration_s)
    cap = max(0.0, cap if cap is not None else MAX_BEST_PARTS_DURATION_S)
    minimums = minimum_duration_s or {}
    maximums = maximum_duration_s or {}
    selected: list[BestPart] = []
    raw_capacities: list[float] = []
    allocation_maxima: list[float] = []
    unusable_ids: list[str] = []
    for source in sources:
        media_id, duration, _analysis = _source_fields(source)
        part = _candidate(
            source,
            max(MIN_INCLUDED_PART_S, _number(minimums.get(media_id)) or MIN_INCLUDED_PART_S),
        )
        if part is None:
            if media_id and duration > 0:
                unusable_ids.append(media_id)
            continue
        selected.append(part)
        raw_capacities.append(duration)
        requested_maximum = _number(maximums.get(media_id))
        allocation_maxima.append(
            duration if requested_maximum is None else min(duration, max(0.0, requested_maximum))
        )
    capacities = [
        min(part.source_end_s - part.source_start_s, allocation_maximum)
        for part, allocation_maximum in zip(selected, allocation_maxima, strict=True)
    ]
    floors = [
        min(capacity, max(MIN_INCLUDED_PART_S, _number(minimums.get(part.media_id)) or 0.0))
        for part, capacity in zip(selected, capacities, strict=True)
    ]
    capacity = round(sum(capacities), 3)
    requested_raw = _number(target_duration_s)
    requested = min(capacity, cap) if requested_raw is None else requested_raw
    conflicts: list[BestPartsConflict] = []
    if unusable_ids:
        conflicts.append(
            BestPartsConflict(
                "source_below_minimum",
                MIN_INCLUDED_PART_S,
                capacity,
                "Every selected source needs at least a 0.1 second usable window.",
            )
        )
    coverage_floor = round(sum(floors), 3)
    minimum_total = _number(minimum_total_duration_s)
    extend_for_minimum = False
    extend_for_target = False
    # This is a selector safety floor, not a replacement for a creator target.
    # An explicit target remains exactly that target, including one below 3s.
    if requested_raw is None and minimum_total is not None and minimum_total > 0:
        raw_capacity = round(sum(allocation_maxima), 3)
        if raw_capacity < minimum_total - 1e-8:
            conflicts.append(
                BestPartsConflict(
                    "snapshot_min_duration",
                    minimum_total,
                    raw_capacity,
                    "The selected source pool cannot make the minimum snapshot duration "
                    "without reuse.",
                )
            )
        elif not unusable_ids and capacity < minimum_total - 1e-8:
            requested = minimum_total
            capacity = raw_capacity
            floors = list(capacities)
            extend_for_minimum = True
    # A proven creator target can use unused capacity around the chosen best
    # moment. The moment chooses *where* the window starts; it is not a hidden
    # cap on an otherwise achievable target.
    if requested_raw is not None and not unusable_ids and requested_raw > capacity + 1e-8:
        allocation_capacity = round(sum(allocation_maxima), 3)
        if requested_raw <= min(allocation_capacity, cap) + 1e-8:
            capacity = allocation_capacity
            floors = list(capacities)
            extend_for_target = True
    if requested_raw is not None and requested_raw <= 0:
        conflicts.append(
            BestPartsConflict(
                "invalid_target",
                requested_raw,
                capacity,
                "The requested duration must be positive.",
            )
        )
        requested = 0.0
    if coverage_floor > cap + 1e-8:
        conflicts.append(
            BestPartsConflict(
                "cap_below_coverage",
                cap,
                capacity,
                "The cap cannot include every source for at least 0.1 seconds.",
            )
        )
    if requested_raw is not None and requested < coverage_floor - 1e-8:
        conflicts.append(
            BestPartsConflict(
                "target_below_coverage",
                requested,
                capacity,
                "The requested duration cannot include every source for at least 0.1 seconds.",
            )
        )
    if requested_raw is not None and requested_raw > min(capacity, cap) + 1e-8:
        conflicts.append(
            BestPartsConflict(
                "insufficient_capacity",
                requested_raw,
                min(capacity, cap),
                "The selected source windows cannot reach the requested duration without reuse.",
            )
        )
    if not selected:
        if sources:
            conflicts.append(
                BestPartsConflict(
                    "no_usable_sources",
                    requested,
                    capacity,
                    "No source has a usable 0.1 second window.",
                )
            )
        return BestPartsResult((), round(requested, 3), 0.0, capacity, tuple(conflicts))
    actual_target = min(max(0.0, requested), capacity, cap)
    if actual_target < coverage_floor - 1e-8:
        return BestPartsResult(
            tuple(selected), round(requested, 3), 0.0, capacity, tuple(conflicts)
        )
    allocation_capacities = (
        allocation_maxima if (extend_for_minimum or extend_for_target) else capacities
    )
    durations = _fair_durations(allocation_capacities, actual_target, floors)
    if extend_for_minimum or extend_for_target:
        extension_reason = (
            "best_moment_extended_for_minimum"
            if extend_for_minimum
            else "best_moment_extended_for_target"
        )
        allocated = tuple(
            _extend_around(
                part,
                source_duration_s=raw_capacity,
                duration_s=duration,
                reason=extension_reason,
            )
            for part, raw_capacity, duration in zip(
                selected, raw_capacities, durations, strict=True
            )
        )
    else:
        allocated = tuple(
            BestPart(
                part.media_id,
                part.source_start_s,
                round(part.source_start_s + duration, 3),
                part.score,
                part.reason,
            )
            for part, duration in zip(selected, durations, strict=True)
        )
    return BestPartsResult(
        allocated, round(requested, 3), round(sum(durations), 3), capacity, tuple(conflicts)
    )


def allocate_best_parts_for_voiceover(
    sources: Sequence[Any], voiceover_duration_s: float
) -> BestPartsResult:
    """Allocate selected footage to narration; never imply footage reuse/looping."""
    return select_best_parts(sources, target_duration_s=voiceover_duration_s)
