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
MAX_SPOKEN_BEST_PARTS_DURATION_S = 240.0


MIN_INCLUDED_PART_S = 0.1


def edit_cap_s(natural_owner: object) -> float:
    """The longest edit the planner builds: a spoken source may run twice a footage edit."""
    return (
        MAX_SPOKEN_BEST_PARTS_DURATION_S
        if natural_owner in {"voiceover", "take"}
        else MAX_BEST_PARTS_DURATION_S
    )


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


def _limit_part_duration(part: BestPart, maximum_duration_s: float) -> BestPart:
    """Apply an allocation ceiling before any later expansion is considered."""

    if part.duration_s <= maximum_duration_s + 1e-8:
        return part
    return BestPart(
        part.media_id,
        part.source_start_s,
        round(part.source_start_s + maximum_duration_s, 3),
        part.score,
        part.reason,
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
        requested_maximum = _number(maximums.get(media_id))
        allocation_maximum = (
            duration if requested_maximum is None else min(duration, max(0.0, requested_maximum))
        )
        part = _limit_part_duration(part, allocation_maximum)
        selected.append(part)
        raw_capacities.append(duration)
        allocation_maxima.append(allocation_maximum)
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


# KRI-596: "the best six moments from the long takes, each very short". A moment is
# never shorter than a readable shot, and two moments of one take keep a gap so they
# read as two moments rather than one stuttering cut; a wider gap is tried first so a
# take's moments spread over it.
MIN_MOMENT_S = 0.8
MOMENT_GAP_S = 0.25
# A window "shows" an analysed moment when it covers half of the shorter of the two.
_MOMENT_COVER_SHARE = 0.5


def _free_spans(
    duration: float, taken: Sequence[tuple[float, float]], gap: float
) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(taken):
        if start - gap > cursor:
            spans.append((cursor, start - gap))
        cursor = max(cursor, end + gap)
    if duration > cursor:
        spans.append((cursor, duration))
    return spans


def _next_moment(
    duration: float,
    moments: Sequence[tuple[float, float, float]],
    taken: Sequence[tuple[float, float]],
    width: float,
    gap: float,
) -> tuple[float, float, float, str] | None:
    """The strongest analysed moment a ``width`` window still shows beside ``taken``
    (start, end, score, reason), else None."""
    spans = [(s, e) for s, e in _free_spans(duration, taken, gap) if e - s + 1e-9 >= width]
    for score, start, end in moments:
        middle = (start + end) / 2
        need = _MOMENT_COVER_SHARE * min(width, end - start)
        for free_start, free_end in spans:
            # Slide the window inside this free stretch, as close to the moment as it gets.
            at = min(max(middle - width / 2, free_start), free_end - width)
            if min(at + width, end) - max(at, start) + 1e-9 >= need:
                return at, at + width, score, "best_moment"
    return None


def _fill(
    duration: float, taken: Sequence[tuple[float, float]], width: float, gap: float
) -> tuple[float, float, float, str] | None:
    """No analysed moment left: the middle of the widest free stretch."""
    free = [(e - s, s, e) for s, e in _free_spans(duration, taken, gap) if e - s + 1e-9 >= width]
    if not free:
        return None
    _room, start, end = max(free)
    at = start + (end - start - width) / 2
    return at, at + width, 0.0, "moment_fill"


def _moment_quotas(durations: Mapping[str, float], count: int) -> dict[str, int]:
    """``count`` moments shared by footage length (largest remainder), one at least each."""
    total = sum(durations.values())
    raw = {media_id: count * duration / total for media_id, duration in durations.items()}
    quotas = {media_id: max(1, int(share)) for media_id, share in raw.items()}
    while sum(quotas.values()) > count:
        # The one-each floor overshot: take back from the take with the most.
        quotas[max(quotas, key=lambda media_id: quotas[media_id])] -= 1
    order = sorted(durations, key=lambda media_id: -(raw[media_id] - int(raw[media_id])))
    index = 0
    while sum(quotas.values()) < count:
        quotas[order[index % len(order)]] += 1
        index += 1
    return quotas


def select_moment_windows(
    sources: Sequence[Any],
    *,
    target_duration_s: float,
    moment_count: int,
    pinned_last: tuple[str, float, float] | None = None,
) -> BestPartsResult | None:
    """Several short, non-overlapping windows per source (KRI-596).

    ``moment_count`` windows share ``target_duration_s`` equally and are shared among the
    sources by footage length, at least one each; inside a take its strongest analysed
    moments win, and they play in source order. ``pinned_last`` is a source window that
    must be its take's last moment (the creator's quoted closing line, KRI-597): it is
    held whole (grown backwards to an equal share when shorter), and that take's other
    moments come from before it when they fit. ``None`` when the sources cannot hold
    that many separate moments, so the caller keeps one window per source.
    """
    rows = [_source_fields(source) for source in sources]
    if not rows or any(not media_id or duration <= 0 for media_id, duration, _a in rows):
        return None
    durations = {media_id: duration for media_id, duration, _a in rows}
    target = _number(target_duration_s)
    if len(durations) != len(rows) or target is None or target <= 0:
        return None
    if moment_count <= len(rows):
        return None
    share = target / moment_count
    taken: dict[str, list[tuple[float, float]]] = {media_id: [] for media_id in durations}
    pinned: BestPart | None = None
    budget, count = target, moment_count
    if pinned_last is not None:
        media_id, start, end = pinned_last
        if media_id not in durations or not 0 <= start < end <= durations[media_id] + 1e-6:
            return None
        start = max(0.0, min(start, end - share))
        pinned = BestPart(media_id, round(start, 3), round(end, 3), 0.0, "closing_quote")
        taken[media_id].append((pinned.source_start_s, pinned.source_end_s))
        budget, count = target - pinned.duration_s, moment_count - 1
    width = budget / count if count > 0 else 0.0
    if width < MIN_MOMENT_S:
        return None
    ranked: dict[str, list[tuple[float, float, float]]] = {}
    # KRI-625: stretches the clip analysis marked unusable (covered lens, camera on
    # the ground, blur). Avoided while any other room is left; absent = no data.
    unusable: dict[str, list[tuple[float, float]]] = {}
    for media_id, duration, analysis in rows:
        spans = []
        for span in analysis.get("unusable_spans") or []:
            if not isinstance(span, Mapping):
                continue
            start, end = _number(span.get("start_s")), _number(span.get("end_s"))
            if start is not None and end is not None and end > start:
                spans.append((max(0.0, start), min(duration, end)))
        unusable[media_id] = [(s, e) for s, e in spans if e > s]
        moments = []
        for moment in analysis.get("best_moments") or []:
            if not isinstance(moment, Mapping):
                continue
            start, end = _number(moment.get("start_s")), _number(moment.get("end_s"))
            if start is None or end is None or min(duration, end) - max(0.0, start) <= 0:
                continue
            energy = _number(moment.get("energy")) or 0.0
            moments.append((energy, max(0.0, start), min(duration, end)))
        ranked[media_id] = sorted(moments, key=lambda row: (-row[0], row[1]))
    chosen: dict[str, list[BestPart]] = {media_id: [] for media_id in durations}

    def take(media_id: str) -> bool:
        duration, blocks = durations[media_id], taken[media_id]
        tries = [blocks]
        if pinned is not None and pinned.media_id == media_id:
            # Before the closing line first: the take then walks forward into it.
            tries.insert(0, [*blocks, (pinned.source_end_s, duration)])
        if unusable[media_id]:
            # Clean footage first; only when none is left do the unusable stretches
            # count again, so quality data can never lose a moment (fail-open).
            tries = [[*t, *unusable[media_id]] for t in tries] + tries
        found = None
        for blocked in tries:
            for gap in (width, MOMENT_GAP_S):
                found = _next_moment(duration, ranked[media_id], blocked, width, gap)
                if found is None and gap == MOMENT_GAP_S:
                    found = _fill(duration, blocked, width, gap)
                if found is not None:
                    break
            if found is not None:
                break
        if found is None:
            return False
        start, end, score, reason = found
        part = BestPart(media_id, round(start, 3), round(start + width, 3), round(score, 3), reason)
        chosen[media_id].append(part)
        blocks.append((part.source_start_s, part.source_end_s))
        ranked[media_id] = [m for m in ranked[media_id] if not (m[1] < end and m[2] > start)]
        return True

    quotas = _moment_quotas(durations, moment_count)
    leftover = 0
    for media_id in durations:
        wanted = quotas[media_id] - (1 if pinned is not None and pinned.media_id == media_id else 0)
        for _ in range(max(0, wanted)):
            if not take(media_id):
                leftover += 1
    while leftover:
        if not any(take(media_id) for media_id in sorted(durations, key=lambda m: -durations[m])):
            return None
        leftover -= 1
    if any(not chosen[m] and (pinned is None or pinned.media_id != m) for m in durations):
        return None
    parts: list[BestPart] = []
    for media_id in durations:
        own = sorted(chosen[media_id], key=lambda part: part.source_start_s)
        if pinned is not None and pinned.media_id == media_id:
            own.append(pinned)
        parts.extend(own)
    total = round(sum(part.duration_s for part in parts), 3)
    return BestPartsResult(tuple(parts), round(target, 3), total, round(sum(durations.values()), 3))


def is_fixed_moment(pin: Mapping[str, Any]) -> bool:
    evidence = pin.get("evidence")
    return pin.get("origin") == "creator_answer" or (
        pin.get("origin") == "grounded"
        and isinstance(evidence, Mapping)
        and evidence.get("kind") == "targeted_model_inspection"
    )


def select_pinned_moment_windows(
    source: Any,
    pins: Sequence[Mapping[str, Any]],
    *,
    width_s: float,
) -> BestPartsResult | None:
    """KRI-723: one window per named moment of ONE take, in the creator's order.

    ``pins`` are ``named_moments.Pin.json()`` rows. A ``creator_answer`` window is the
    creator's exact interval: never re-centred, trimmed or moved. An inspected grounding
    also keeps its exact interval. Other ``grounded`` moments get
    a ``width_s`` window as close to its evidenced interval as the creator's windows allow;
    an ``ai_pick_by_request`` one (the creator said "you pick") or an ``unlocated`` one (an app
    that could not ask) the strongest analysed moment still free, else the middle of the
    widest free stretch. ``None`` when a window cannot be placed without overlapping another
    (the caller turns that into a length conflict, never guessed windows).
    """
    media_id, duration, analysis = _source_fields(source)
    if not media_id or duration <= 0 or not pins:
        return None
    width = min(float(width_s), duration)
    parts: list[BestPart | None] = [None] * len(pins)
    taken: list[tuple[float, float]] = []
    for index, pin in enumerate(pins):
        if not is_fixed_moment(pin):
            continue
        start, end = _number(pin.get("source_start_s")), _number(pin.get("source_end_s"))
        if start is None or end is None or not 0 <= start < end:
            return None
        end = min(end, duration)
        if end - start < MIN_INCLUDED_PART_S:
            return None
        if any(start < hi - 0.001 and end > lo + 0.001 for lo, hi in taken):
            return None
        reason = "creator_answer" if pin.get("origin") == "creator_answer" else "grounded_moment"
        parts[index] = BestPart(media_id, round(start, 3), round(end, 3), 0.0, reason)
        taken.append((start, end))
    ranked = sorted(
        (
            (_number(m.get("energy")) or 0.0, max(0.0, s), min(duration, e))
            for m in analysis.get("best_moments") or []
            if isinstance(m, Mapping)
            and (s := _number(m.get("start_s"))) is not None
            and (e := _number(m.get("end_s"))) is not None
            and min(duration, e) > max(0.0, s)
        ),
        key=lambda row: (-row[0], row[1]),
    )
    # Evidence first: a grounded moment claims its interval before any free pick can.
    for index, pin in enumerate(pins):
        if pin.get("origin") != "grounded" or is_fixed_moment(pin):
            continue
        start, end = _number(pin.get("source_start_s")), _number(pin.get("source_end_s"))
        if start is None or end is None:
            return None
        found = _next_moment(duration, [(1.0, start, end)], taken, width, 0.0)
        if found is None:
            return None
        at = found[0]
        parts[index] = BestPart(
            media_id, round(at, 3), round(at + width, 3), 0.0, "grounded_moment"
        )
        taken.append((at, at + width))
    anchors = [part is not None for part in parts]
    # Then each free pick, in the creator's order, inside its share of the stretch between the
    # moments around it (so unlocated moments keep the asked order, like ordinal windows did);
    # anywhere free when that stretch cannot hold it.
    for index, pin in enumerate(pins):
        origin = pin.get("origin")
        if origin not in ("ai_pick_by_request", "unlocated"):
            continue
        before = [part for part in parts[:index] if part is not None]
        lo = before[-1].source_end_s if before else 0.0
        later = [i for i in range(index + 1, len(pins)) if anchors[i]]
        hi = parts[later[0]].source_start_s if later else duration  # type: ignore[union-attr]
        run = sum(
            1
            for i in range(index, later[0] if later else len(pins))
            if pins[i].get("origin") in ("ai_pick_by_request", "unlocated")
        )
        found = None
        if hi - lo >= width:
            share = lo + (hi - lo) / max(run, 1)
            bounds = [*taken, *([(0.0, lo)] if lo > 0 else []), (max(share, lo + width), duration)]
            found = _next_moment(duration, ranked, bounds, width, MOMENT_GAP_S) or _fill(
                duration, bounds, width, MOMENT_GAP_S
            )
        found = (
            found
            or _next_moment(duration, ranked, taken, width, MOMENT_GAP_S)
            or _fill(duration, taken, width, MOMENT_GAP_S)
        )
        if found is None:
            return None
        at = found[0]
        reason = "ai_pick_by_request" if origin == "ai_pick_by_request" else "unlocated_moment"
        parts[index] = BestPart(media_id, round(at, 3), round(at + width, 3), 0.0, reason)
        taken.append((at, at + width))
    placed = tuple(part for part in parts if part is not None)
    if len(placed) != len(pins):
        return None
    total = round(sum(part.duration_s for part in placed), 3)
    return BestPartsResult(placed, total, total, round(duration, 3))


def allocate_best_parts_for_voiceover(
    sources: Sequence[Any], voiceover_duration_s: float
) -> BestPartsResult:
    """Allocate selected footage to narration; never imply footage reuse/looping."""
    return select_best_parts(sources, target_duration_s=voiceover_duration_s)
