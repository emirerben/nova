"""Lip-sync montage planner: takes placed on the song's own clock (KRI-374).

The creator filmed raw takes while their song played on another phone. The song
is the master timeline and the camera audio is muted, so a take must show the
singer's mouth at exactly the song moment it was filmed for. The aligner
(``song_alignment``) says where each take sits: ``song_time = take_time + delta_s``.
This module turns those positions into one guided fast-montage plan. It is pure:
no I/O, no model call, no database.

Rules (all times are integer milliseconds of *song* time until the cuts are built):

* A take covers ``[delta + 0.3 s, delta + duration - 0.3 s]``. The margin keeps a
  cut away from the take's first and last frames, which are rarely clean.
* Only a ``confident`` take, or one whose position the creator confirmed (the
  ordered media ids of the song-order question, resolved against that take's
  alternates), is ever placed by song time. An ambiguous or unmatched take is
  NEVER placed at a guessed position; it can still serve as muted B-roll.
* Tiling is greedy in song order, each take used once. A switch between two takes
  happens only inside their overlap, on a lyric-line start (else a beat), and no
  segment is shorter than 1.0 s. A take fully inside another's coverage is left
  out (using it would mean cutting back to the first take, which is a second use).
* A gap between covered stretches is closed by extending both neighbours into
  their spare margin when it is at most twice the margin, else filled with muted
  B-roll (unmatched takes first, then the Visuals pool). A gap nothing can fill
  splits the montage; the largest covered stretch wins and the receipt names
  everything that was left out.
* The window is capped at ``MAX_PROPOSAL_DURATION_S``, keeping the densest-
  coverage stretch.
* ``source_start = output_start + window_start - delta`` for every take cut.

``resync_lipsync_moments`` re-derives that source start from the take's pinned
delta, so the compiler and, later, the editor commit path keep sync with the
*take* however the cut was rounded or moved.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.pipeline.unified_montage import (
    MIN_TOTAL_S,
    SNAPSHOT_FALLBACK_TITLE,
    STILL_MAX_S,
    BriefView,
    UnifiedClip,
    UnifiedMontagePlan,
    _fit_typography,
    _nfc,
    _story_beats,
    _title,
    _window_start_s,
)
from app.schemas.edit_proposal import (
    MAX_PROPOSAL_DURATION_S,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
)
from app.schemas.user_song import (
    SongAlignment,
    SongAnalysis,
    TakeAlignment,
    UserSongPlan,
    UserSongTake,
)

# A take is trusted this far inside its own ends.
COVER_MARGIN_MS = 300
# No lip-sync segment is shorter than this.
MIN_SEGMENT_MS = 1000
# Muted B-roll never holds a photo (or a long video) longer than a beat of
# attention, unless the pool is too small to fill the gap otherwise.
BROLL_HOLD_MS = int(STILL_MAX_S * 1000)
MIN_BROLL_PIECE_MS = 600
MAX_WINDOW_MS = MAX_PROPOSAL_DURATION_S * 1000
MIN_TOTAL_MS = int(MIN_TOTAL_S * 1000)
# Two candidate positions closer than this are the same position.
_SAME_POSITION_MS = 50
# Sync tolerance of the invariant source_start - output_start == window - delta.
SYNC_TOLERANCE_S = 0.0015


class LipsyncPlanError(ValueError):
    """The takes cannot make a lip-sync montage; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LipsyncSyncError(ValueError):
    """A lip-sync cut can no longer keep its take on the song's clock."""


def lipsync_sync_error_s(
    *, output_start_s: float, source_start_s: float, delta_s: float, window_start_s: float
) -> float:
    """How far a take cut is from ``source_start - output_start == window - delta``."""
    return abs((source_start_s - output_start_s) - (window_start_s - delta_s))


def _resynced_source_start_s(
    *, output_start_s: float, delta_s: float, window_start_s: float
) -> float:
    return round(float(output_start_s) + float(window_start_s) - float(delta_s), 3)


def resync_moment_rows(
    moments: Sequence[Mapping[str, Any]],
    user_song: UserSongPlan,
    *,
    source_durations: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Moment rows with every lip-sync take's source window re-derived from its delta.

    Rows whose ``media_id`` is not a pinned take (B-roll) and every row of a
    background plan come back unchanged. Raises ``LipsyncSyncError`` when a take
    can no longer sit where the song needs it (its source start would be before
    the take began, or its end past the take's end).
    """
    rows = [dict(row) for row in moments]
    if user_song.mode != "lipsync":
        return rows
    for row in rows:
        take = user_song.takes.get(str(row.get("media_id")))
        if take is None:
            continue
        start = _resynced_source_start_s(
            output_start_s=float(row["output_start_s"]),
            delta_s=take.delta_s,
            window_start_s=user_song.window_start_s,
        )
        if start < -SYNC_TOLERANCE_S:
            raise LipsyncSyncError(
                "A lip-sync clip was moved to a place in the song that starts before it was filmed."
            )
        start = max(0.0, start)
        end = round(start + float(row["duration_s"]), 3)
        limit = (source_durations or {}).get(str(row.get("media_id")))
        if limit is not None and end > limit + 0.05:
            raise LipsyncSyncError(
                "A lip-sync clip was moved to a place in the song that runs past its end."
            )
        row["source_start_s"] = start
        row["source_end_s"] = end
    return rows


def resync_lipsync_moments(plan: Any) -> Any:
    """``plan`` with each lip-sync take's source window re-derived from its pinned delta.

    ``plan`` is a ``GuidedStoryExecutionPlan`` (a copy is returned) or its dict
    form. A plan without a lip-sync ``user_song`` comes back unchanged. Called by
    ``compile_execution_plan`` and by the editor commit path, so sync belongs to
    the take, not to wherever the cut ended up after frame rounding or a reorder.
    """
    if isinstance(plan, Mapping):
        raw = plan.get("user_song")
        if raw is None:
            return plan
        song = raw if isinstance(raw, UserSongPlan) else UserSongPlan.model_validate(raw)
        if song.mode != "lipsync":
            return plan
        updated = dict(plan)
        updated["story_timeline"] = resync_moment_rows(plan["story_timeline"], song)
        return updated
    song = getattr(plan, "user_song", None)
    if song is None or song.mode != "lipsync":
        return plan
    rows = resync_moment_rows([moment.model_dump() for moment in plan.story_timeline], song)
    moments = [
        moment.model_copy(
            update={"source_start_s": row["source_start_s"], "source_end_s": row["source_end_s"]}
        )
        for moment, row in zip(plan.story_timeline, rows, strict=True)
    ]
    return plan.model_copy(update={"story_timeline": moments})


def refuse_lipsync_rate_change(plan_or_song: Any, moments: Sequence[Any] | None = None) -> None:
    """Raise ``LipsyncSyncError`` if a lip-sync take is retimed.

    A take at another speed drifts off the song, and the editor has no way to
    keep it on the clock, so a non-1.0 rate on a lip-sync moment is refused.
    Pass a plan, or a ``UserSongPlan`` plus the moments to check.
    """
    if moments is None:
        song = (
            plan_or_song.get("user_song")
            if isinstance(plan_or_song, Mapping)
            else getattr(plan_or_song, "user_song", None)
        )
        moments = (
            plan_or_song["story_timeline"]
            if isinstance(plan_or_song, Mapping)
            else plan_or_song.story_timeline
        )
    else:
        song = plan_or_song
    if song is None:
        return
    if isinstance(song, Mapping):
        song = UserSongPlan.model_validate(song)
    if song.mode != "lipsync":
        return
    for moment in moments:
        media_id = moment.get("media_id") if isinstance(moment, Mapping) else moment.media_id
        rate = moment.get("playback_rate") if isinstance(moment, Mapping) else moment.playback_rate
        if media_id in song.takes and rate not in (None, 1, 1.0):
            raise LipsyncSyncError("A lip-sync clip has to play at normal speed to stay in sync.")


# ── planner ──────────────────────────────────────────────────────────────────


@dataclass
class _Placed:
    clip: UnifiedClip
    delta_ms: int
    status: str
    confirmed: bool
    cover_start: int = 0
    cover_end: int = 0
    true_end: int = 0  # delta + duration: the take's real end in song time


@dataclass
class _Block:
    """One stretch of the output: a take on the song clock, or muted B-roll."""

    clip: UnifiedClip
    start: int
    end: int
    placed: _Placed | None = None  # None -> B-roll


def _ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000))


def _candidate_deltas_ms(row: TakeAlignment) -> list[int]:
    found: list[int] = []
    values = ([row.delta_s] if row.delta_s is not None else []) + [
        alt.delta_s for alt in row.alternates
    ]
    for value in values:
        candidate = _ms(value)
        if all(abs(candidate - existing) > _SAME_POSITION_MS for existing in found):
            found.append(candidate)
    return found


def _coverage(clip: UnifiedClip, delta_ms: int, song_ms: int) -> tuple[int, int, int]:
    duration_ms = int(math.floor(clip.duration_s * 1000))
    true_end = delta_ms + duration_ms
    start = max(0, delta_ms + COVER_MARGIN_MS)
    end = min(song_ms, true_end - COVER_MARGIN_MS)
    return start, end, true_end


def _place_takes(
    takes: Sequence[UnifiedClip],
    alignment: SongAlignment,
    confirmed_order: Sequence[str] | None,
    song_ms: int,
) -> tuple[dict[str, _Placed], dict[str, str]]:
    """Which takes may be placed by song time, and why the others may not."""
    placed: dict[str, _Placed] = {}
    reasons: dict[str, str] = {}
    uncertain: dict[str, TakeAlignment] = {}
    for clip in takes:
        row = alignment.takes.get(clip.media_id)
        if row is None or row.status == "unmatched":
            reasons[clip.media_id] = "unmatched"
        elif row.status == "confident" and row.delta_s is not None:
            placed[clip.media_id] = _Placed(clip, _ms(row.delta_s), "confident", False)
        else:
            uncertain[clip.media_id] = row
            reasons[clip.media_id] = "ambiguous_unconfirmed"

    def covered(clip: UnifiedClip, delta_ms: int) -> bool:
        start, end, _ = _coverage(clip, delta_ms, song_ms)
        return end - start >= MIN_SEGMENT_MS

    order = [
        media_id
        for media_id in (confirmed_order or ())
        if media_id in placed or media_id in uncertain
    ]
    confident_ids = set(placed)
    for index, media_id in enumerate(order):
        row = uncertain.get(media_id)
        if row is None:
            continue
        before = next((placed[m] for m in reversed(order[:index]) if m in placed), None)
        after = next((placed[m] for m in order[index + 1 :] if m in confident_ids), None)
        if before is None and after is None:
            reasons[media_id] = "no_fitting_position"
            continue
        clip = next(c for c in takes if c.media_id == media_id)
        chosen: int | None = None
        for candidate in _candidate_deltas_ms(row):
            start = candidate + COVER_MARGIN_MS
            if before is not None and start < before.delta_ms + COVER_MARGIN_MS:
                continue
            if after is not None and start > after.delta_ms + COVER_MARGIN_MS:
                continue
            if covered(clip, candidate):
                chosen = candidate
                break
        if chosen is None:
            reasons[media_id] = "no_fitting_position"
            continue
        placed[media_id] = _Placed(clip, chosen, row.status, True)
        reasons.pop(media_id, None)

    for media_id, item in list(placed.items()):
        item.cover_start, item.cover_end, item.true_end = _coverage(
            item.clip, item.delta_ms, song_ms
        )
        if item.cover_end - item.cover_start < MIN_SEGMENT_MS:
            del placed[media_id]
            reasons[media_id] = "too_short"
    return placed, reasons


def _pick_switch(
    low: int, high: int, line_starts: Sequence[int], beats: Sequence[int]
) -> tuple[int, str]:
    """Where to cut from one take to the next inside ``[low, high]``.

    A lyric-line start wins, else a beat; each nearest the middle of the overlap
    (the point both takes are farthest from their own ends). With neither, the
    middle itself.
    """
    middle = (low + high) // 2
    for kind, points in (("line", line_starts), ("beat", beats)):
        inside = [p for p in points if low <= p <= high]
        if inside:
            return min(inside, key=lambda p: (abs(p - middle), p)), kind
    return middle, "none"


def _tile_islands(
    placed: Mapping[str, _Placed], line_starts: Sequence[int], beats: Sequence[int]
) -> tuple[list[list[_Block]], list[dict[str, Any]], list[str]]:
    """Cover the song with the placed takes: islands of contiguous segments."""
    pending = sorted(placed.values(), key=lambda p: (p.cover_start, -p.cover_end))
    islands: list[list[_Block]] = []
    switches: list[dict[str, Any]] = []
    redundant: list[str] = []
    while pending:
        current = pending.pop(0)
        segment_start = current.cover_start
        blocks: list[_Block] = []
        while True:
            best: tuple[_Placed, int, int] | None = None
            for candidate in pending:
                if candidate.cover_end <= current.cover_end:
                    continue
                low = max(candidate.cover_start, segment_start + MIN_SEGMENT_MS)
                high = min(current.cover_end, candidate.cover_end - MIN_SEGMENT_MS)
                if low > high:
                    continue
                if best is None or (candidate.cover_end, -candidate.cover_start) > (
                    best[0].cover_end,
                    -best[0].cover_start,
                ):
                    best = (candidate, low, high)
            if best is None:
                blocks.append(_Block(current.clip, segment_start, current.cover_end, current))
                break
            candidate, low, high = best
            at, kind = _pick_switch(low, high, line_starts, beats)
            switches.append({"at_s": at / 1000, "snapped_to": kind})
            blocks.append(_Block(current.clip, segment_start, at, current))
            pending.remove(candidate)
            current, segment_start = candidate, at
        island_end = blocks[-1].end
        # Takes that start inside this stretch add nothing: they are contained,
        # or extend it by less than a segment.
        for leftover in [p for p in pending if p.cover_start <= island_end]:
            pending.remove(leftover)
            redundant.append(leftover.clip.media_id)
        islands.append(blocks)
    return islands, switches, redundant


@dataclass
class _Pool:
    """Footage that may play muted over a gap, each item at most once."""

    items: list[tuple[UnifiedClip, int]]

    def fill(self, gap_ms: int) -> list[_Block] | None:
        """B-roll pieces that exactly cover ``gap_ms`` (relative start 0), or None."""
        if gap_ms < MIN_BROLL_PIECE_MS:
            return None
        # First a quick-cut fill (every piece at most a beat of attention, pool
        # order); then, if the pool is too small for that, shortest items first so
        # photos are used before one long video carries the whole gap.
        for hold, items in (
            (BROLL_HOLD_MS, self.items),
            (None, sorted(self.items, key=lambda item: item[1])),
        ):
            pieces = self._try(gap_ms, hold, items)
            if pieces is not None:
                return pieces
        return None

    @staticmethod
    def _try(
        gap_ms: int, hold: int | None, items: Sequence[tuple[UnifiedClip, int]]
    ) -> list[_Block] | None:
        remaining = gap_ms
        cursor = 0
        pieces: list[_Block] = []
        for clip, capacity in items:
            if remaining <= 0:
                break
            effective = capacity if hold is None else min(capacity, hold)
            if effective < MIN_BROLL_PIECE_MS:
                continue
            length = min(effective, remaining)
            leftover = remaining - length
            if 0 < leftover < MIN_SEGMENT_MS:
                # Never leave a sliver for the next piece: shorten this one so the
                # next gets a full second (or take the lot when it fits).
                if remaining <= effective:
                    length = remaining
                elif remaining - MIN_SEGMENT_MS >= MIN_BROLL_PIECE_MS:
                    length = remaining - MIN_SEGMENT_MS
                else:
                    continue
            pieces.append(_Block(clip, cursor, cursor + length))
            cursor += length
            remaining -= length
        if remaining != 0:
            return None
        return pieces

    def take(self, pieces: Sequence[_Block]) -> None:
        used = {piece.clip.media_id for piece in pieces}
        self.items = [(c, cap) for c, cap in self.items if c.media_id not in used]


def _broll_capacity_ms(clip: UnifiedClip) -> int:
    if clip.kind == "image":
        return BROLL_HOLD_MS
    return int(math.floor(clip.duration_s * 1000))


def _covered_ms(blocks: Sequence[_Block]) -> int:
    return sum(b.end - b.start for b in blocks if b.placed is not None)


def _window_blocks(blocks: list[_Block]) -> tuple[list[_Block], bool]:
    """The densest ``MAX_WINDOW_MS`` stretch of ``blocks``; (blocks, was_trimmed)."""
    if blocks[-1].end - blocks[0].start <= MAX_WINDOW_MS:
        return blocks, False
    starts = {b.start for b in blocks} | {b.end - MAX_WINDOW_MS for b in blocks}
    starts = {s for s in starts if blocks[0].start <= s <= blocks[-1].end - MAX_WINDOW_MS}

    def density(window_start: int) -> int:
        window_end = window_start + MAX_WINDOW_MS
        return sum(
            max(0, min(b.end, window_end) - max(b.start, window_start))
            for b in blocks
            if b.placed is not None
        )

    best = min(starts, key=lambda s: (-density(s), s))
    window_end = best + MAX_WINDOW_MS
    kept: list[_Block] = []
    for block in blocks:
        start, end = max(block.start, best), min(block.end, window_end)
        floor = MIN_SEGMENT_MS if block.placed is not None else MIN_BROLL_PIECE_MS
        if end - start >= floor:
            kept.append(_Block(block.clip, start, end, block.placed))
    return kept, True


def plan_lipsync_montage(
    clips: Sequence[UnifiedClip],
    alignment: SongAlignment,
    song_analysis: SongAnalysis,
    view: BriefView | None = None,
    strategy: Mapping[str, Any] | None = None,
    confirmed_order: Sequence[str] | None = None,
    *,
    plan_item_id: str,
    font_covers: Callable[[str, str], bool] | None = None,
) -> UnifiedMontagePlan:
    """Build the guided fast-montage plan that lays ``clips`` on the song's clock.

    ``clips`` are the item's phone-bound takes (``lane="clip"``) and its Visuals
    (``lane="asset"``). ``alignment`` must be for ``song_analysis``' generation.
    ``confirmed_order`` is the creator's answer to the song-order question: the
    media ids in the order they confirmed. It is the only thing that lets an
    ambiguous take be placed, and only at one of its own candidate positions that
    fits between its confirmed neighbours. ``strategy``/``view`` supply only the
    creator's title, closing title and typography.

    Raises ``LipsyncPlanError`` (``stale_alignment``, ``song_not_analyzed``,
    ``no_synced_takes``, ``span_too_short``) when no montage can be made.
    """
    view = view or BriefView()
    strategy = strategy or {}
    if song_analysis.status != "ready" or song_analysis.duration_s <= 0:
        raise LipsyncPlanError("song_not_analyzed", "The song has not been analysed yet.")
    if alignment.song_generation != song_analysis.generation:
        raise LipsyncPlanError("stale_alignment", "The takes were matched to a different song.")
    if len({clip.media_id for clip in clips}) != len(clips):
        raise ValueError("montage clips must have unique media identities")
    song_ms = int(math.floor(song_analysis.duration_s * 1000))
    takes = [c for c in clips if c.lane == "clip" and c.kind == "video"]
    others = [c for c in clips if c not in takes]
    beats = sorted({_ms(b) for b in song_analysis.beats_s if b >= 0})
    line_starts = sorted({_ms(line.start_s) for line in song_analysis.lines})

    placed, reasons = _place_takes(takes, alignment, confirmed_order, song_ms)
    if not placed:
        raise LipsyncPlanError("no_synced_takes", "None of the takes could be placed on the song.")

    islands, switches, redundant = _tile_islands(placed, line_starts, beats)
    for media_id in redundant:
        reasons[media_id] = "overlapped"

    # ── B-roll pool: takes we could not place, then the Visuals pool ─────────
    pool_clips = [
        c for c in takes if c.media_id not in placed and reasons.get(c.media_id) != "too_short"
    ]
    pool_clips += others
    pool = _Pool(
        [(c, cap) for c in pool_clips if (cap := _broll_capacity_ms(c)) >= MIN_BROLL_PIECE_MS]
    )

    # ── join islands: bridge a small gap, fill a bigger one, else split ──────
    spans: list[list[_Block]] = [list(islands[0])]
    bridged = 0
    for island in islands[1:]:
        previous = spans[-1][-1]
        gap = island[0].start - previous.end
        assert previous.placed is not None and island[0].placed is not None
        if gap <= 2 * COVER_MARGIN_MS:
            first = gap // 2
            previous.end += first
            island[0].start -= gap - first
            bridged += 1
            spans[-1].extend(island)
            continue
        pieces = pool.fill(gap)
        if pieces is None:
            spans.append(list(island))
            continue
        pool.take(pieces)
        offset = previous.end
        spans[-1].extend(_Block(p.clip, p.start + offset, p.end + offset) for p in pieces)
        spans[-1].extend(island)

    best_span = max(spans, key=lambda span: (_covered_ms(span), -span[0].start))
    blocks, trimmed = _window_blocks(best_span)
    while blocks and blocks[0].placed is None:
        blocks.pop(0)
    while blocks and blocks[-1].placed is None:
        blocks.pop()
    if not blocks or blocks[-1].end - blocks[0].start < MIN_TOTAL_MS:
        raise LipsyncPlanError(
            "span_too_short", "The matched takes cover less than three seconds of the song."
        )
    window_start, window_end = blocks[0].start, blocks[-1].end

    # ── cuts, media refs ─────────────────────────────────────────────────────
    cuts: list[FastMontageCut] = []
    refs: list[MediaRef] = []
    pinned: dict[str, UserSongTake] = {}
    for index, block in enumerate(blocks):
        clip, length = block.clip, block.end - block.start
        if block.placed is not None:
            start = (block.start - block.placed.delta_ms) / 1000
            end = (block.end - block.placed.delta_ms) / 1000
            pinned[clip.media_id] = UserSongTake(
                delta_s=block.placed.delta_ms / 1000,
                status=block.placed.status,  # type: ignore[arg-type]
                confirmed_by_creator=block.placed.confirmed,
            )
        elif clip.kind == "image":
            start, end = 0.0, round(length / 1000, 3)
        else:
            start = _window_start_s(clip, length / 1000)
            end = round(start + length / 1000, 3)
            if end > clip.duration_s + 0.001:
                start = round(max(0.0, clip.duration_s - length / 1000), 3)
                end = round(start + length / 1000, 3)
        cuts.append(
            FastMontageCut(
                cut_id=f"lipsync-cut-{index + 1}",
                media_id=clip.media_id,
                source_start_s=round(start, 3),
                source_end_s=round(end, 3),
                output_duration_s=round(end - start, 3),
                role="hook" if index == 0 else "payoff" if index == len(blocks) - 1 else "build",
                transition="none",
                beat_align=False,
            )
        )
        aspect = clip.aspect
        if clip.width and clip.height:
            swapped = clip.orientation_degrees in (90, 270)
            aspect = (clip.height / clip.width) if swapped else (clip.width / clip.height)
        refs.append(
            MediaRef(
                lane=clip.lane,  # type: ignore[arg-type]
                media_id=clip.media_id,
                gcs_path=clip.proxy_path,
                generation=clip.generation,
                kind=clip.kind,  # type: ignore[arg-type]
                duration_s=clip.duration_s if clip.kind == "video" else None,
                aspect=aspect,
                analysis=dict(clip.analysis) if isinstance(clip.analysis, Mapping) else {},
                source_filename=clip.source_filename,
                user_context=clip.user_context,
                content_hash=clip.content_hash,
            )
        )
    total_ms = window_end - window_start
    total_s = round(sum(cut.output_duration_s for cut in cuts), 3)
    song_plan = UserSongPlan(
        mode="lipsync",
        plan_item_id=plan_item_id,
        generation=song_analysis.generation,
        duration_s=song_analysis.duration_s,
        window_start_s=window_start / 1000,
        window_end_s=window_end / 1000,
        takes=pinned,
    )
    if abs(total_s - total_ms / 1000) > 0.002:
        raise LipsyncPlanError("span_inconsistent", "The cut lengths do not add up to the window.")

    # ── title and typography (creator-confirmed only) ────────────────────────
    title, title_source = _title(strategy, view)
    closing = _nfc(strategy.get("closing_title")) or None
    requested_font = strategy.get("font_family")
    requested_font = requested_font if isinstance(requested_font, str) and requested_font else None
    family, title, closing, _labels = _fit_typography(
        font_covers, requested_font, title, closing, {}
    )
    if not title:
        title_source = "none"
    snapshot_kwargs: dict[str, Any] = {}
    if closing:
        snapshot_kwargs["closing_title"] = closing
    hold = strategy.get("opening_title_duration_s")
    if isinstance(hold, (int, float)) and not isinstance(hold, bool):
        snapshot_kwargs["opening_title_duration_s"] = hold
    style: dict[str, Any] = {}
    if family is not None:
        style["font_family"] = family
    if isinstance(strategy.get("text_color"), str) and strategy.get("text_color"):
        style["text_color"] = strategy["text_color"]

    def build(extra: Mapping[str, Any]) -> EditProposalSnapshot:
        return EditProposalSnapshot(
            direction="fast_montage",
            goal="",
            pace="fast",
            duration_s=total_s,
            title=(title or SNAPSHOT_FALLBACK_TITLE)[:100],
            opening_title=title,
            media=refs,
            story_beats=_story_beats(cuts),
            fast_cuts=cuts,
            media_scope="all",
            selected_media_ids=[ref.media_id for ref in refs],
            video_reuse_policy="once",
            user_song=song_plan,
            **snapshot_kwargs,
            **extra,
        )

    try:
        snapshot = build(style)
    except ValidationError:
        snapshot = build({})

    used = {block.clip.media_id for block in blocks}
    broll_ids = [b.clip.media_id for b in blocks if b.placed is None]
    dropped: list[dict[str, str]] = []
    for clip in clips:
        if clip.media_id in used:
            continue
        if reasons.get(clip.media_id) == "overlapped":
            reason = "overlapped"
        elif clip.media_id in placed:
            reason = "outside_window" if trimmed else "disconnected"
        elif clip.lane == "asset" or clip.media_id not in reasons:
            reason = "no_gap"
        else:
            reason = reasons[clip.media_id]
        dropped.append({"media_id": clip.media_id, "reason": reason})
    receipt: dict[str, Any] = {
        "mode": "lipsync",
        "window_start_s": song_plan.window_start_s,
        "window_end_s": song_plan.window_end_s,
        "capped_at_s": MAX_PROPOSAL_DURATION_S if trimmed else None,
        "placed": [
            {
                "media_id": b.clip.media_id,
                "song_start_s": b.start / 1000,
                "song_end_s": b.end / 1000,
                "delta_s": b.placed.delta_ms / 1000,
                "confirmed_by_creator": b.placed.confirmed,
            }
            for b in blocks
            if b.placed is not None
        ],
        "broll_ids": broll_ids,
        "bridged_gaps": bridged,
        "switches": [s for s in switches if window_start <= s["at_s"] * 1000 <= window_end],
        "dropped": dropped,
    }
    return UnifiedMontagePlan(
        snapshot=snapshot,
        clip_ids=[ref.media_id for ref in refs],
        title=title,
        title_source=title_source,
        label_clip_ids=[],
        dropped_label_clip_ids=[],
        short_label_clip_ids=[],
        ordering_basis="song_time",
        ordering_fallback_clip_ids=[],
        duration_s=total_s,
        brief_version=view.version,
        wants_per_clip_text=False,
        visual_ids=[b.clip.media_id for b in blocks if b.clip.lane == "asset"],
        user_song=song_plan,
        song_receipt=receipt,
    )


__all__ = [
    "COVER_MARGIN_MS",
    "LipsyncPlanError",
    "LipsyncSyncError",
    "MIN_SEGMENT_MS",
    "SYNC_TOLERANCE_S",
    "lipsync_sync_error_s",
    "plan_lipsync_montage",
    "refuse_lipsync_rate_change",
    "resync_lipsync_moments",
    "resync_moment_rows",
]
