"""Likelihood-based assignment of lip-sync takes to song positions (KRI-471).

Pure: no I/O, no model call, no aligner import. The aligner scores every place a
take could sit (``PlacementCandidate``); this module decides which candidate each
take actually claims, with NO status gate: every take that has at least one
usable, non-conflicting candidate is placed.

Rules (all times integer milliseconds):

* A take claims the song range its candidate matched (``delta + match range``),
  else its whole footage ``[delta, delta + duration]``. Two takes conflict when
  their claims overlap by more than ``overlap_ms`` AND neither extends the other
  (one is inside the other, or a near copy): the tiler would drop that take as
  redundant. Staggered overlaps (re-sung stretches) are not conflicts.
* Takes are decided strongest first (best likelihood, then margin, then id).
  Each claims its highest-likelihood non-conflicting candidate. Candidates within
  ``tie_ratio`` of the take's best are interchangeable (a repeated chorus); the
  tie is broken toward the cluster of already-placed claims (inside the hull,
  then nearest, then the smaller extension), then the earliest delta.
* The assignment maximises (total effective likelihood, takes placed, tie-break
  preference) with an exact branch-and-bound over the candidates, falling back to
  the greedy answer when the node budget runs out.
* ``ask`` names the takes whose position the creator could usefully confirm:
  ``no_evidence`` (nothing to place them by), ``tie`` (the tie-break chose),
  ``weak`` (the chosen candidate is under ``ask_likelihood``, or the take lost its
  best candidate to a conflict).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

# Defaults mirrored from the planner so this module stays dependency-free.
DEFAULT_OVERLAP_MS = 1000
DEFAULT_CAP_MS = 120_000
DEFAULT_COVER_MARGIN_MS = 300
DEFAULT_MIN_COVER_MS = 1000
DEFAULT_NODE_CAP = 200_000
# Two candidate deltas this close are the same position.
SAME_POSITION_MS = 50
# Spare room left before a stacked run (it hugs the take before it).
STACK_LEAD_MS = 0
# Candidates this far apart are distinct positions when computing the margin.
DISTINCT_POSITION_MS = 250

_EPS = 1e-9


@dataclass(frozen=True)
class CandidateSpec:
    """One place a take could sit. ``song_time = take_time + delta_ms``."""

    delta_ms: int
    likelihood: float
    # Take-time range (ms) that matches the song; None = the whole take.
    match_ms: tuple[int, int] | None = None
    method: str | None = None


@dataclass(frozen=True)
class TakeSpec:
    media_id: str
    duration_ms: int
    candidates: tuple[CandidateSpec, ...] = ()


@dataclass(frozen=True)
class Claim:
    """The song range one take holds, and why it holds it."""

    media_id: str
    delta_ms: int
    likelihood: float
    margin: float
    claim_start_ms: int
    claim_end_ms: int
    match_ms: tuple[int, int] | None
    method: str | None
    # "aligner" (clear winner) | "tie_break" (equal candidates, chosen by layout
    # preference) | "pinned" (the caller fixed the position, e.g. a creator answer).
    basis: str = "aligner"


@dataclass
class Assignment:
    placed: dict[str, Claim] = field(default_factory=dict)
    # media_id -> "no_evidence" | "conflict" | "too_short" | caller-supplied reason
    unplaced: dict[str, str] = field(default_factory=dict)
    # media_id -> "tie" | "weak" | "no_evidence"
    ask: dict[str, str] = field(default_factory=dict)


def spec_from_alignment_row(media_id: str, duration_s: float, row: Any | None) -> TakeSpec:
    """A ``TakeSpec`` from a ``TakeAlignment`` (duck-typed: no schema import)."""
    duration_ms = int(math.floor(float(duration_s) * 1000))
    if row is None:
        return TakeSpec(media_id, duration_ms)
    found: list[CandidateSpec] = []
    for cand in row.candidates_or_legacy():
        delta = int(round(float(cand.delta_s) * 1000))
        if any(abs(delta - c.delta_ms) <= SAME_POSITION_MS for c in found):
            continue
        match = None
        if cand.match_start_s is not None and cand.match_end_s is not None:
            match = (
                int(round(float(cand.match_start_s) * 1000)),
                int(round(float(cand.match_end_s) * 1000)),
            )
        found.append(CandidateSpec(delta, float(cand.likelihood), match, cand.method))
    return TakeSpec(media_id, duration_ms, tuple(found))


def margin_of(candidates: Sequence[CandidateSpec]) -> float:
    """1.0 for one candidate, ``(best - second_distinct) / best`` otherwise, 0 for none."""
    if not candidates:
        return 0.0
    ranked = sorted(candidates, key=lambda c: -c.likelihood)
    best = ranked[0]
    if best.likelihood <= 0:
        return 0.0
    for other in ranked[1:]:
        if abs(other.delta_ms - best.delta_ms) > DISTINCT_POSITION_MS:
            return max(0.0, (best.likelihood - other.likelihood) / best.likelihood)
    return 1.0


def claim_range(spec: TakeSpec, cand: CandidateSpec) -> tuple[int, int]:
    if cand.match_ms is not None:
        return cand.delta_ms + cand.match_ms[0], cand.delta_ms + cand.match_ms[1]
    return cand.delta_ms, cand.delta_ms + spec.duration_ms


def cover_ms(
    spec: TakeSpec,
    cand: CandidateSpec,
    song_ms: int,
    margin_ms: int = DEFAULT_COVER_MARGIN_MS,
) -> int:
    """How much song the take can actually show at this candidate (margins kept)."""
    start = max(0, cand.delta_ms + margin_ms)
    end = min(song_ms, cand.delta_ms + spec.duration_ms - margin_ms)
    if cand.match_ms is not None:
        start = max(0, cand.delta_ms + max(margin_ms, cand.match_ms[0]))
        end = min(song_ms, cand.delta_ms + min(spec.duration_ms - margin_ms, cand.match_ms[1]))
    return end - start


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return min(a[1], b[1]) - max(a[0], b[0])


def _conflict(a: tuple[int, int], b: tuple[int, int], overlap_ms: int, stagger_ms: int) -> bool:
    """Do two claims fight for the same song? Staggered overlaps (each take adds
    ``stagger_ms`` of new song past the other) are fine: the tiler cuts from one
    to the next inside the overlap. Containment, or a near-copy, is a conflict."""
    if _overlap(a, b) <= overlap_ms:
        return False
    first, second = (a, b) if a[0] <= b[0] else (b, a)
    staggered = second[0] > first[0] and second[1] - first[1] >= stagger_ms
    return not staggered


@dataclass
class _Option:
    cand: CandidateSpec
    claim: tuple[int, int]
    eff: float


@dataclass
class _Entry:
    spec: TakeSpec
    best: float
    margin: float
    options: list[_Option]


def _tie_key(
    claim: tuple[int, int], hull: tuple[int, int] | None, delta: int, cap_ms: int
) -> tuple[int, int, int, int]:
    """Smaller is better: (breaks the cap, distance to hull, hull extension, delta)."""
    if hull is None:
        return (0, 0, 0, delta)
    lo, hi = min(hull[0], claim[0]), max(hull[1], claim[1])
    over = 1 if hi - lo > cap_ms else 0
    if claim[1] < hull[0]:
        dist = hull[0] - claim[1]
    elif claim[0] > hull[1]:
        dist = claim[0] - hull[1]
    else:
        dist = 0
    return (over, dist, (hi - lo) - (hull[1] - hull[0]), delta)


def _grow(hull: tuple[int, int] | None, claim: tuple[int, int]) -> tuple[int, int]:
    if hull is None:
        return claim
    return min(hull[0], claim[0]), max(hull[1], claim[1])


def assign_takes(
    takes: Sequence[TakeSpec],
    song_ms: int,
    *,
    overlap_ms: int = DEFAULT_OVERLAP_MS,
    cap_ms: int = DEFAULT_CAP_MS,
    pinned: Mapping[str, int] | None = None,
    exclude: Mapping[str, str] | None = None,
    tie_ratio: float | None = None,
    ask_likelihood: float | None = None,
    cover_margin_ms: int = DEFAULT_COVER_MARGIN_MS,
    min_cover_ms: int = DEFAULT_MIN_COVER_MS,
    node_cap: int = DEFAULT_NODE_CAP,
    exact: bool = True,
) -> Assignment:
    """Decide which candidate each take claims. See the module docstring.

    ``pinned`` maps media_id -> delta_ms the caller already decided (a creator
    answer): those takes are placed there first and everything else must not
    conflict with them. ``exclude`` maps media_id -> reason for takes that must
    stay unplaced. ``exact=False`` forces the greedy answer.
    """
    if tie_ratio is None or ask_likelihood is None:
        from app.config import settings

        tie_ratio = settings.song_align_tie_ratio if tie_ratio is None else tie_ratio
        ask_likelihood = (
            settings.song_align_ask_likelihood if ask_likelihood is None else ask_likelihood
        )
    pinned = dict(pinned or {})
    exclude = dict(exclude or {})
    result = Assignment()
    placed_claims: dict[str, tuple[int, int]] = {}
    hull: tuple[int, int] | None = None
    by_id = {t.media_id: t for t in takes}

    # ── pinned takes go first ────────────────────────────────────────────────
    for media_id, delta in pinned.items():
        spec = by_id.get(media_id)
        if spec is None:
            continue
        match_cand = next(
            (c for c in spec.candidates if abs(c.delta_ms - delta) <= SAME_POSITION_MS), None
        )
        cand = CandidateSpec(
            delta,
            match_cand.likelihood if match_cand else 0.0,
            match_cand.match_ms if match_cand else None,
            match_cand.method if match_cand else None,
        )
        rng = claim_range(spec, cand)
        result.placed[media_id] = Claim(
            media_id,
            delta,
            cand.likelihood,
            margin_of(spec.candidates) if spec.candidates else 0.0,
            rng[0],
            rng[1],
            cand.match_ms,
            cand.method,
            "pinned",
        )
        placed_claims[media_id] = rng
        hull = _grow(hull, rng)

    # ── entries for everything else ──────────────────────────────────────────
    entries: list[_Entry] = []
    for spec in takes:
        media_id = spec.media_id
        if media_id in result.placed:
            continue
        if media_id in exclude:
            result.unplaced[media_id] = exclude[media_id]
            if spec.candidates:
                result.ask[media_id] = "weak"
            else:
                result.ask[media_id] = "no_evidence"
            continue
        if not spec.candidates:
            result.unplaced[media_id] = "no_evidence"
            result.ask[media_id] = "no_evidence"
            continue
        usable = [
            c
            for c in spec.candidates
            if cover_ms(spec, c, song_ms, cover_margin_ms) >= min_cover_ms
        ]
        if not usable:
            result.unplaced[media_id] = "too_short"
            result.ask[media_id] = "weak"
            continue
        usable.sort(key=lambda c: (-c.likelihood, c.delta_ms))
        best = usable[0].likelihood
        options = [
            _Option(
                c,
                claim_range(spec, c),
                best if c.likelihood >= tie_ratio * best - _EPS else c.likelihood,
            )
            for c in usable
        ]
        entries.append(_Entry(spec, best, margin_of(spec.candidates), options))
    entries.sort(key=lambda e: (-e.best, -e.margin, e.spec.media_id))

    def conflicts(claim: tuple[int, int], others: Mapping[str, tuple[int, int]]) -> bool:
        return any(_conflict(claim, other, overlap_ms, min_cover_ms) for other in others.values())

    # ── greedy (also the DFS incumbent and the node-cap fallback) ────────────
    def greedy() -> dict[str, _Option]:
        chosen: dict[str, _Option] = {}
        claims = dict(placed_claims)
        h = hull
        for entry in entries:
            free = [o for o in entry.options if not conflicts(o.claim, claims)]
            if not free:
                continue
            top = max(o.cand.likelihood for o in free)
            group = [o for o in free if o.cand.likelihood >= tie_ratio * top - _EPS]
            pick = min(group, key=lambda o: _tie_key(o.claim, h, o.cand.delta_ms, cap_ms))
            chosen[entry.spec.media_id] = pick
            claims[entry.spec.media_id] = pick.claim
            h = _grow(h, pick.claim)
        return chosen

    def score(chosen: Mapping[str, _Option]) -> tuple:
        total = 0.0
        over = dist = ext = delta_sum = 0
        h = hull
        for entry in entries:  # decision order
            opt = chosen.get(entry.spec.media_id)
            if opt is None:
                continue
            total += opt.eff
            o, d, x, dl = _tie_key(opt.claim, h, opt.cand.delta_ms, cap_ms)
            over, dist, ext, delta_sum = over + o, dist + d, ext + x, delta_sum + dl
            h = _grow(h, opt.claim)
        return (round(total, 9), len(chosen), -over, -dist, -ext, -delta_sum)

    best_choice = greedy()
    if exact and entries:
        best_score = score(best_choice)
        suffix = [0.0] * (len(entries) + 1)
        for i in range(len(entries) - 1, -1, -1):
            suffix[i] = suffix[i + 1] + entries[i].best

        class _Budget(Exception):
            pass

        nodes = 0
        found: dict[str, _Option] = dict(best_choice)

        def dfs(
            index: int,
            chosen: dict[str, _Option],
            claims: dict[str, tuple[int, int]],
            total: float,
        ) -> None:
            nonlocal nodes, best_score, found
            nodes += 1
            if nodes > node_cap:
                raise _Budget
            if index == len(entries):
                current = score(chosen)
                if current > best_score:
                    best_score, found = current, dict(chosen)
                return
            if total + suffix[index] < best_score[0] - _EPS:
                return
            entry = entries[index]
            for opt in entry.options:
                if conflicts(opt.claim, claims):
                    continue
                chosen[entry.spec.media_id] = opt
                claims[entry.spec.media_id] = opt.claim
                dfs(index + 1, chosen, claims, total + opt.eff)
                del chosen[entry.spec.media_id]
                del claims[entry.spec.media_id]
            dfs(index + 1, chosen, claims, total)

        try:
            dfs(0, {}, dict(placed_claims), 0.0)
            best_choice = found
        except _Budget:
            best_choice = greedy()

    # ── build claims and the ask set ─────────────────────────────────────────
    for entry in entries:
        media_id = entry.spec.media_id
        opt = best_choice.get(media_id)
        if opt is None:
            result.unplaced[media_id] = "conflict"
            result.ask[media_id] = "weak"
            continue
        # The tie group ignores conflicts: a repeat that lost its spot to a conflict
        # was still a guess among equals, so the creator may want to confirm it.
        top = entry.best
        group = [o for o in entry.options if o.cand.likelihood >= tie_ratio * top - _EPS]
        in_group = opt in group
        tie = in_group and len(group) > 1
        result.placed[media_id] = Claim(
            media_id,
            opt.cand.delta_ms,
            opt.cand.likelihood,
            entry.margin,
            opt.claim[0],
            opt.claim[1],
            opt.cand.match_ms,
            opt.cand.method,
            "tie_break" if tie else "aligner",
        )
        if tie:
            result.ask[media_id] = "tie"
        elif (not in_group) or opt.cand.likelihood < ask_likelihood:
            result.ask[media_id] = "weak"
    return result


@dataclass(frozen=True)
class Choice:
    """Where one take sits once the creator has ordered the takes.

    ``place``: ``pinned`` (a candidate position), ``stack`` (laid by the creator's
    order inside a gap, approximate sync) or ``broll`` (no room; muted footage).
    """

    media_id: str
    order_index: int
    delta_ms: int | None
    place: str
    basis: str
    confirmed: bool
    likelihood: float = 0.0
    reason: str | None = None


def resolve_with_order(
    takes: Sequence[TakeSpec],
    order: Sequence[str],
    song_ms: int,
    *,
    first_line_ms: int = 0,
    overlap_ms: int = DEFAULT_OVERLAP_MS,
    cap_ms: int = DEFAULT_CAP_MS,
    tie_ratio: float | None = None,
    ask_likelihood: float | None = None,
    cover_margin_ms: int = DEFAULT_COVER_MARGIN_MS,
    min_cover_ms: int = DEFAULT_MIN_COVER_MS,
) -> dict[str, Choice]:
    """Positions for every take given the creator's confirmed ``order``.

    Takes the assignment was sure about (not in ``ask``) stay where it put them.
    An uncertain take with candidates takes the candidate strictly between its
    neighbours' starts that conflicts with nothing (likelihood, then contiguity,
    then earliest): one fit -> ``creator_position`` (the creator's answer decided
    it); several tied fits -> ``tie_break`` (not confirmed). A take with no
    candidates, or none that fits, is STACKED: the maximal run of such takes
    between the nearest placed neighbours is laid end to end in the creator's
    order, hugging the take before the gap with overlap shared evenly when the gap is
    too small (``creator_stack``, likelihood 0, approximate sync).
    A take with no room left is ``broll``.
    """
    if tie_ratio is None or ask_likelihood is None:
        from app.config import settings

        tie_ratio = settings.song_align_tie_ratio if tie_ratio is None else tie_ratio
        ask_likelihood = (
            settings.song_align_ask_likelihood if ask_likelihood is None else ask_likelihood
        )
    by_id = {t.media_id: t for t in takes}
    ids = [m for m in dict.fromkeys(order) if m in by_id]
    ids += [t.media_id for t in takes if t.media_id not in set(ids)]
    base = assign_takes(
        takes,
        song_ms,
        overlap_ms=overlap_ms,
        cap_ms=cap_ms,
        tie_ratio=tie_ratio,
        ask_likelihood=ask_likelihood,
        cover_margin_ms=cover_margin_ms,
        min_cover_ms=min_cover_ms,
    )
    uncertain = {m for m in ids if m in base.ask}
    choices: dict[str, Choice] = {}
    known: dict[str, tuple[int, tuple[int, int]]] = {}  # id -> (delta, claim range)
    anchors: set[str] = set()
    for index, media_id in enumerate(ids):
        claim = base.placed.get(media_id)
        if media_id in uncertain or claim is None:
            continue
        known[media_id] = (claim.delta_ms, (claim.claim_start_ms, claim.claim_end_ms))
        anchors.add(media_id)
        choices[media_id] = Choice(
            media_id, index, claim.delta_ms, "pinned", claim.basis, False, claim.likelihood
        )

    def conflicts(rng: tuple[int, int]) -> bool:
        return any(_conflict(rng, other, overlap_ms, min_cover_ms) for _d, other in known.values())

    # ── pass 1: uncertain takes that have a candidate fitting the order ─────
    for index, media_id in enumerate(ids):
        if media_id not in uncertain:
            continue
        spec = by_id[media_id]
        lo = next((known[m][0] for m in reversed(ids[:index]) if m in known), None)
        hi = next((known[m][0] for m in ids[index + 1 :] if m in anchors), None)
        fits: list[CandidateSpec] = []
        for cand in spec.candidates:
            if lo is not None and cand.delta_ms <= lo:
                continue
            if hi is not None and cand.delta_ms >= hi:
                continue
            if cover_ms(spec, cand, song_ms, cover_margin_ms) < min_cover_ms:
                continue
            if conflicts(claim_range(spec, cand)):
                continue
            fits.append(cand)
        if not fits:
            continue
        top = max(c.likelihood for c in fits)
        tied = [c for c in fits if c.likelihood >= tie_ratio * top - _EPS]
        ref = lo if lo is not None else hi

        def contiguity(c: CandidateSpec, ref: int | None = ref) -> tuple[int, int]:
            return (abs(c.delta_ms - ref) if ref is not None else 0, c.delta_ms)

        pick = min(tied, key=contiguity)
        known[media_id] = (pick.delta_ms, claim_range(spec, pick))
        choices[media_id] = Choice(
            media_id,
            index,
            pick.delta_ms,
            "pinned",
            "creator_position" if len(tied) == 1 else "tie_break",
            len(tied) == 1,
            pick.likelihood,
        )

    # ── pass 2: stack the rest in the creator's order ────────────────────────
    def span_with(rng: tuple[int, int], neighbours: Sequence[str | None]) -> int:
        ranges = [rng] + [known[m][1] for m in neighbours if m is not None and m in known]
        return max(r[1] for r in ranges) - min(r[0] for r in ranges)

    def _cover_end(m: str) -> int:
        # Last frame the tiler will show of a placed take (margins, matched range).
        return min(known[m][0] + by_id[m].duration_ms - cover_margin_ms, known[m][1][1])

    def _cover_start(m: str) -> int:
        return max(known[m][0] + cover_margin_ms, known[m][1][0])

    index = 0
    while index < len(ids):
        if ids[index] in choices:
            index += 1
            continue
        end = index
        while end < len(ids) and ids[end] not in choices:
            end += 1
        run = ids[index:end]
        before = next((m for m in reversed(ids[:index]) if m in known), None)
        after = next((m for m in ids[end:] if m in known), None)
        if after is not None and before is not None and known[after][0] <= known[before][0]:
            after = None  # the creator's order contradicts the song order: ignore it
        total = sum(by_id[m].duration_ms for m in run)
        if before is None and after is None:
            cursor = max(0, first_line_ms)
        elif before is not None:
            # Runs meet their neighbours at the cover margins (A's last trusted frame
            # == the run's first), so the tiler can join them without a filler.
            gap_start = _cover_end(before) - cover_margin_ms
            gap_end = _cover_start(after) + cover_margin_ms if after is not None else song_ms
            slack = gap_end - gap_start - total
            # Negative slack overlaps both neighbours evenly (the tiler trims); spare
            # room is NOT centred (a take adrift in a long gap joins nothing): it hugs
            # the previous take with at most STACK_LEAD_MS of lead.
            if after is None:
                cursor = gap_start  # nothing to share an overlap with: no room => B-roll
            else:
                cursor = gap_start + (slack // 2 if slack < 0 else min(slack // 2, STACK_LEAD_MS))
        else:
            assert after is not None
            gap_end = _cover_start(after) + cover_margin_ms
            slack = gap_end - max(0, first_line_ms) - total
            cursor = gap_end - total - (0 if slack < 0 else min(slack // 2, STACK_LEAD_MS))
        cursor = max(0, cursor)
        for offset, media_id in enumerate(run):
            spec = by_id[media_id]
            cand = CandidateSpec(cursor, 0.0)
            rng = claim_range(spec, cand)
            if (
                cover_ms(spec, cand, song_ms, cover_margin_ms) < min_cover_ms
                or span_with(rng, (before if before is not None else after,)) > cap_ms
            ):
                choices[media_id] = Choice(
                    media_id, index + offset, None, "broll", "creator_stack", True, 0.0, "no_room"
                )
                continue
            known[media_id] = (cursor, rng)
            choices[media_id] = Choice(
                media_id, index + offset, cursor, "stack", "creator_stack", True, 0.0
            )
            cursor += spec.duration_ms
        index = end
    return {m: choices[m] for m in ids}


def with_basis(claim: Claim, basis: str) -> Claim:
    return replace(claim, basis=basis)


__all__ = [
    "Assignment",
    "CandidateSpec",
    "Choice",
    "Claim",
    "TakeSpec",
    "assign_takes",
    "claim_range",
    "cover_ms",
    "margin_of",
    "resolve_with_order",
    "spec_from_alignment_row",
    "with_basis",
]
