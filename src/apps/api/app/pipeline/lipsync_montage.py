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
* A take is trimmed to the part that matches the song (``match_start_s`` /
  ``match_end_s`` on its alignment row) but the full source stays on its
  ``MediaRef`` so the editor can extend it. A gap between trimmed takes is first
  closed with the neighbours' own unmatched footage (same delta, still in sync).
* Takes that cannot be placed are kept: short (``BROLL_HOLD_MS``), muted B-roll
  after the last sung take, within the song and the window cap. Takes in no cut
  at all ride along as unused media so the editor can still bring them in.
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

from app.config import settings
from app.pipeline.pinned_text import drawable_pins
from app.pipeline.take_assignment import (
    Assignment,
    CandidateSpec,
    TakeSpec,
    assign_takes,
    cover_ms,
    spec_from_alignment_row,
)
from app.pipeline.unified_montage import (
    MIN_TOTAL_S,
    SNAPSHOT_FALLBACK_TITLE,
    STILL_MAX_S,
    BriefView,
    UnifiedClip,
    UnifiedMontagePlan,
    _fit_pins,
    _fit_typography,
    _nfc,
    _pinned_texts,
    _story_beats,
    _title,
    _window_start_s,
)
from app.schemas.edit_proposal import (
    CREATOR_SELECTED_ORIENTATION_REASON,
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
# A gap bridge may dip this close to a take's own first/last frame (a cover
# margin is 300 ms; 100 ms still clears the unclean edge frames).
DIP_MARGIN_MS = 100
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


# The editor quantizes every cut source to its 1/30 s frame clock, so an untouched first cut can
# come back up to half a frame (plus rounding) away from the song-derived value. A head move of
# one frame or less is that noise and keeps the pinned window; anything bigger is a real trim.
HEAD_TRIM_TOLERANCE_S = 1 / 30 + 1e-6


def window_start_for_first_cut_head(
    user_song: Mapping[str, Any], moments: Sequence[Mapping[str, Any]]
) -> float | None:
    """The song window start that keeps a lip-sync take on the song after its head was trimmed.

    ``source_start - output_start == window_start - delta`` holds for every take cut, and the song
    clip plays from ``window_start`` at output 0. When the creator trims the head of the first cut
    (it starts earlier or later in its take than the plan placed it), the song has to start with the
    take or the singer drifts off the audio by exactly that trim (founder export 5a7f6c88: 0.3 s
    trim, song 0.3 s off). Returns ``delta + source_start - output_start`` of the cut that opens the
    video, or ``None`` (keep the pinned window) when nothing moved by more than a frame, the opener
    is not a pinned take, or it is not the take the plan opened with.
    """
    if user_song.get("mode") != "lipsync" or not moments:
        return None
    takes = user_song.get("takes") or {}
    ordered = sorted(moments, key=lambda row: float(row.get("output_start_s") or 0.0))
    first = ordered[0]
    take = takes.get(str(first.get("media_id")))
    if not isinstance(take, Mapping) or take.get("delta_s") is None:
        return None
    if abs(float(first.get("output_start_s") or 0.0)) > SYNC_TOLERANCE_S:
        return None
    current = float(user_song["window_start_s"])
    derived = (
        float(take["delta_s"]) + float(first["source_start_s"]) - float(first["output_start_s"])
    )
    if abs(derived - current) <= HEAD_TRIM_TOLERANCE_S or derived < 0:
        return None
    return round(derived, 3)


def resync_lipsync_moments(
    plan: Any, *, source_durations: Mapping[str, float] | None = None
) -> Any:
    """``plan`` with each lip-sync take's source window re-derived from its pinned delta.

    ``source_durations`` (media_id -> seconds) turns on the "take runs past its
    end" check; the editor commit passes the device-measured durations, since a
    take dragged later in the cut can need footage the singer never filmed.

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
        updated["story_timeline"] = resync_moment_rows(
            plan["story_timeline"], song, source_durations=source_durations
        )
        return updated
    song = getattr(plan, "user_song", None)
    if song is None or song.mode != "lipsync":
        return plan
    rows = resync_moment_rows(
        [moment.model_dump() for moment in plan.story_timeline],
        song,
        source_durations=source_durations,
    )
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
    likelihood: float = 0.0
    margin: float = 1.0
    position_basis: str = "aligner"
    cover_start: int = 0
    cover_end: int = 0
    true_end: int = 0  # delta + duration: the take's real end in song time
    # Take-time (ms) range that actually matches the song; None = whole take.
    match_ms: tuple[int, int] | None = None
    # Untrimmed coverage (margins kept): how far the take can be extended into
    # its own unmatched footage while staying on the song clock.
    full_start: int = 0
    full_end: int = 0
    method: str | None = None  # how the aligner found it: "audio" | "lyrics"


@dataclass
class _Block:
    """One stretch of the output: a take on the song clock, or muted B-roll."""

    clip: UnifiedClip
    start: int
    end: int
    placed: _Placed | None = None  # None -> B-roll


def _ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000))


def _match_ms(row: TakeAlignment | None) -> tuple[int, int] | None:
    if row is None or row.match_start_s is None or row.match_end_s is None:
        return None
    return _ms(row.match_start_s), _ms(row.match_end_s)


def _coverage(
    clip: UnifiedClip, delta_ms: int, song_ms: int, match: tuple[int, int] | None = None
) -> tuple[int, int, int, int, int]:
    """``(start, end, true_end, full_start, full_end)`` of a take in song time.

    ``start``/``end`` are clamped to the take's matched range (keeping the cover
    margins); ``full_*`` are the untrimmed coverage.
    """
    duration_ms = int(math.floor(clip.duration_s * 1000))
    true_end = delta_ms + duration_ms
    full_start = max(0, delta_ms + COVER_MARGIN_MS)
    full_end = min(song_ms, true_end - COVER_MARGIN_MS)
    if match is None:
        return full_start, full_end, true_end, full_start, full_end
    m0, m1 = match
    start = max(0, delta_ms + max(COVER_MARGIN_MS, m0))
    end = min(song_ms, delta_ms + min(duration_ms - COVER_MARGIN_MS, m1))
    return start, end, true_end, full_start, full_end


def _place_takes(
    takes: Sequence[UnifiedClip],
    alignment: SongAlignment,
    confirmed_order: Sequence[str] | None,
    song_ms: int,
    creator_choices: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, _Placed], dict[str, str]]:
    """Where each take sits on the song (by likelihood), and why the others do not.

    No ``status`` gates placement: every take with a non-conflicting candidate is
    placed (``take_assignment``). ``confirmed_order`` (the creator's answer to the
    song-order question) re-decides the takes the assignment was unsure about.
    """
    specs = {
        clip.media_id: spec_from_alignment_row(
            clip.media_id, clip.duration_s, alignment.takes.get(clip.media_id)
        )
        for clip in takes
    }
    # A row patched after the creator's answer (``apply_resolved_song_takes``) keeps
    # its aligner candidates but carries the creator's delta / "no position": the
    # patch wins over the stale candidates.
    for clip in takes:
        row = alignment.takes.get(clip.media_id)
        if row is None or not row.candidates:
            continue
        spec = specs[clip.media_id]
        if row.status == "unmatched" or row.delta_s is None:
            specs[clip.media_id] = TakeSpec(spec.media_id, spec.duration_ms, ())
        elif all(abs(_ms(row.delta_s) - c.delta_ms) > 50 for c in spec.candidates):
            patched = CandidateSpec(_ms(row.delta_s), row.confidence or 0.4, None, "lyrics")
            specs[clip.media_id] = TakeSpec(spec.media_id, spec.duration_ms, (patched,))
    spec_list = [specs[clip.media_id] for clip in takes]

    def run(pinned=None, exclude=None) -> Assignment:
        return assign_takes(
            spec_list,
            song_ms,
            overlap_ms=_ms(settings.song_align_max_overlap_s),
            cap_ms=MAX_WINDOW_MS,
            pinned=pinned,
            exclude=exclude,
            cover_margin_ms=COVER_MARGIN_MS,
            min_cover_ms=MIN_SEGMENT_MS,
        )

    pinned: dict[str, int] = {}
    exclude: dict[str, str] = {}
    creator_choice: dict[str, str] = {}  # media_id -> position_basis
    creator_confirmed: dict[str, bool] = {}
    if creator_choices:
        # The creator's answer (``resolve_with_order``) already decided these takes:
        # pin a candidate / stacked position, or keep the take as B-roll.
        for media_id, choice in creator_choices.items():
            if media_id not in specs:
                continue
            delta = choice.get("delta_s")
            if choice.get("place") == "broll" or delta is None:
                exclude[media_id] = str(choice.get("reason") or "no_fitting_position")
                continue
            pinned[media_id] = _ms(delta)
            creator_choice[media_id] = str(choice.get("position_basis") or "creator_position")
            creator_confirmed[media_id] = bool(choice.get("confirmed_by_creator"))
        assignment = run(pinned, exclude)
        confirmed_order = None
    else:
        assignment = run()
    if confirmed_order:
        order = [m for m in confirmed_order if m in specs]
        uncertain = {
            m
            for m in order
            if specs[m].candidates
            and (
                m in assignment.ask
                or getattr(alignment.takes.get(m), "status", None) == "ambiguous"
            )
        }
        delta_of = {m: c.delta_ms for m, c in assignment.placed.items()}
        for index, media_id in enumerate(order):
            if media_id not in uncertain:
                continue
            before = next((delta_of[m] for m in reversed(order[:index]) if m in delta_of), None)
            after = next(
                (delta_of[m] for m in order[index + 1 :] if m in delta_of and m not in uncertain),
                None,
            )
            if before is None and after is None:
                continue
            spec = specs[media_id]
            fits = [
                c
                for c in spec.candidates
                if (before is None or c.delta_ms >= before)
                and (after is None or c.delta_ms <= after)
                and cover_ms(spec, c, song_ms, COVER_MARGIN_MS) >= MIN_SEGMENT_MS
            ]
            if not fits:
                exclude[media_id] = "no_fitting_position"
                delta_of.pop(media_id, None)
                continue
            fits.sort(key=lambda c: (-c.likelihood, c.delta_ms))
            tied = [
                c
                for c in fits
                if c.likelihood >= settings.song_align_tie_ratio * fits[0].likelihood
            ]
            pinned[media_id] = fits[0].delta_ms
            delta_of[media_id] = fits[0].delta_ms
            creator_choice[media_id] = "creator_position" if len(tied) == 1 else "tie_break"
            creator_confirmed[media_id] = len(tied) == 1
        if pinned or exclude:
            assignment = run(pinned, exclude)

    placed: dict[str, _Placed] = {}
    reasons: dict[str, str] = dict(assignment.unplaced)
    for clip in takes:
        claim = assignment.placed.get(clip.media_id)
        if claim is None:
            reasons.setdefault(clip.media_id, "no_evidence")
            continue
        row = alignment.takes.get(clip.media_id)
        basis = creator_choice.get(clip.media_id) or (
            "tie_break" if claim.basis == "tie_break" else "aligner"
        )
        status = row.status if row is not None and row.status != "unmatched" else "ambiguous"
        placed[clip.media_id] = _Placed(
            clip,
            claim.delta_ms,
            status,
            creator_confirmed.get(clip.media_id, False),
            likelihood=claim.likelihood,
            margin=claim.margin,
            position_basis=basis,
            match_ms=claim.match_ms,
            method=claim.method,
        )
    for media_id, item in list(placed.items()):
        (
            item.cover_start,
            item.cover_end,
            item.true_end,
            item.full_start,
            item.full_end,
        ) = _coverage(item.clip, item.delta_ms, song_ms, item.match_ms)
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


def _media_ref(clip: UnifiedClip) -> MediaRef:
    aspect = clip.aspect
    if clip.width and clip.height:
        swapped = clip.orientation_degrees in (90, 270)
        aspect = (clip.height / clip.width) if swapped else (clip.width / clip.height)
    return MediaRef(
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


def _weight(block: _Block) -> float:
    """How much a placed block counts toward a span: its likelihood, never zero."""
    return max(block.placed.likelihood, 0.05) if block.placed is not None else 0.0


def _covered_ms(blocks: Sequence[_Block]) -> float:
    """Likelihood-weighted covered time: a sure take outweighs a long guess."""
    return sum((b.end - b.start) * _weight(b) for b in blocks if b.placed is not None)


def _window_blocks(blocks: list[_Block]) -> tuple[list[_Block], bool]:
    """The densest ``MAX_WINDOW_MS`` stretch of ``blocks``; (blocks, was_trimmed)."""
    if blocks[-1].end - blocks[0].start <= MAX_WINDOW_MS:
        return blocks, False
    starts = {b.start for b in blocks} | {b.end - MAX_WINDOW_MS for b in blocks}
    starts = {s for s in starts if blocks[0].start <= s <= blocks[-1].end - MAX_WINDOW_MS}

    def density(window_start: int) -> float:
        window_end = window_start + MAX_WINDOW_MS
        return sum(
            max(0, min(b.end, window_end) - max(b.start, window_start)) * _weight(b)
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
    output_orientation: str | None = None,
    creator_choices: Mapping[str, Mapping[str, Any]] | None = None,
) -> UnifiedMontagePlan:
    """Build the guided fast-montage plan that lays ``clips`` on the song's clock.

    ``clips`` are the item's phone-bound takes (``lane="clip"``) and its Visuals
    (``lane="asset"``). ``alignment`` must be for ``song_analysis``' generation.
    ``confirmed_order`` is the creator's answer to the song-order question: the
    media ids in the order they confirmed; with no ``creator_choices`` it re-decides
    the takes the assignment was unsure about (candidate between neighbours).
    ``creator_choices`` (``apply_resolved_song_takes``) carries the positions that
    answer already resolved, including stacked takes (``place="stack"``,
    ``position_basis="creator_stack"``) and B-roll. ``strategy``/``view`` supply only the
    creator's title, closing title and typography. ``output_orientation`` is the
    creator's explicit finished-video shape (KRI-306); without it the snapshot
    infers one from the footage.

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

    placed, reasons = _place_takes(takes, alignment, confirmed_order, song_ms, creator_choices)
    if not placed:
        raise LipsyncPlanError("no_synced_takes", "None of the takes could be placed on the song.")

    def layout(
        placed: dict[str, _Placed], reasons: dict[str, str]
    ) -> tuple[list[list[_Block]], list[dict[str, Any]], int]:
        """Tile ``placed`` and join the islands; returns (spans, switches, bridged)."""
        islands, switches, redundant = _tile_islands(placed, line_starts, beats)
        for media_id in redundant:
            reasons[media_id] = "overlapped"

        # ── B-roll pool: takes we could not place, then the Visuals pool ─────────
        pool_clips = [
            c for c in takes if c.media_id not in placed and reasons.get(c.media_id) != "too_short"
        ]  # a placed take is never filler: it only plays in sync at its own delta
        pool_clips += others
        pool = _Pool(
            [(c, cap) for c in pool_clips if (cap := _broll_capacity_ms(c)) >= MIN_BROLL_PIECE_MS]
        )

        # ── join islands: bridge a small gap, fill a bigger one, else split ──────
        # Per consecutive pair with gap g: (1) even split into both margins; (2) the
        # neighbours' own footage bridges it (cut on a lyric line, else a beat);
        # (3) the same, dipping into the 300 ms cover margins; (4) muted Visuals /
        # unplaced-take B-roll; (5) irreducible: split, the best span wins.
        def own_footage(previous: _Block, nxt: _Block, margin_left: int) -> tuple[int, int]:
            assert previous.placed is not None and nxt.placed is not None
            if margin_left == COVER_MARGIN_MS:
                prev_limit, next_limit = previous.placed.full_end, nxt.placed.full_start
            else:
                prev_limit = min(song_ms, previous.placed.true_end - margin_left)
                next_limit = max(0, nxt.placed.delta_ms + margin_left)
            return max(0, prev_limit - previous.end), max(0, nxt.start - next_limit)

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
            joined = False
            for margin_left in (COVER_MARGIN_MS, DIP_MARGIN_MS):
                avail_prev, avail_next = own_footage(previous, island[0], margin_left)
                if gap > avail_prev + avail_next:
                    continue
                low = max(island[0].start - avail_next, previous.end)
                high = min(previous.end + avail_prev, island[0].start)
                at, kind = _pick_switch(low, high, line_starts, beats)
                switches.append({"at_s": at / 1000, "snapped_to": kind})
                previous.end = at
                island[0].start = at
                bridged += 1
                spans[-1].extend(island)
                joined = True
                break
            if joined:
                continue
            pieces = pool.fill(gap)
            if pieces is None:
                spans.append(list(island))
                continue
            pool.take(pieces)
            offset = previous.end
            spans[-1].extend(_Block(p.clip, p.start + offset, p.end + offset) for p in pieces)
            spans[-1].extend(island)

        return spans, switches, bridged

    def placed_outside(span: list[_Block], among: Mapping[str, _Placed]) -> int:
        in_span = {blk.clip.media_id for blk in span}
        return sum(1 for m in among if m not in in_span)

    spans, switches, bridged = layout(placed, reasons)
    best_span = max(spans, key=lambda span: (_covered_ms(span), -span[0].start))
    stacked = {m: p for m, p in placed.items() if p.position_basis == "creator_stack"}
    if len(spans) > 1 and stacked:
        # Stacked takes are approximate-sync guesses: when they split the montage,
        # try once with them as muted B-roll so they can fill a gap instead. Keep
        # that layout only if it covers more of the song or strands fewer takes.
        alt_placed = {m: p for m, p in placed.items() if m not in stacked}
        if alt_placed:
            alt_reasons = {**reasons, **{m: "stack_as_broll" for m in stacked}}
            alt_spans, alt_switches, alt_bridged = layout(alt_placed, alt_reasons)
            alt_best = max(alt_spans, key=lambda span: (_covered_ms(span), -span[0].start))
            if _covered_ms(alt_best) > _covered_ms(best_span) or placed_outside(
                alt_best, placed
            ) < placed_outside(best_span, placed):
                placed, reasons = alt_placed, alt_reasons
                spans, switches, bridged, best_span = (
                    alt_spans,
                    alt_switches,
                    alt_bridged,
                    alt_best,
                )
    blocks, trimmed = _window_blocks(best_span)
    while blocks and blocks[0].placed is None:
        blocks.pop(0)
    while blocks and blocks[-1].placed is None:
        blocks.pop()
    if not blocks or blocks[-1].end - blocks[0].start < MIN_TOTAL_MS:
        raise LipsyncPlanError(
            "span_too_short", "The matched takes cover less than three seconds of the song."
        )

    # ── unplaced takes stay in the edit: short, muted, after the last sung take ──
    # (the song keeps playing; camera audio is muted for every B-roll cut).
    in_blocks = {b.clip.media_id for b in blocks}
    kept_ids: list[str] = []
    cursor = blocks[-1].end
    for clip in takes:  # creator (input) order
        if clip.media_id in in_blocks or clip.media_id in placed:
            continue  # placed takes never double as B-roll, even when they fell outside
        room = min(song_ms, blocks[0].start + MAX_WINDOW_MS) - cursor
        length = min(BROLL_HOLD_MS, _broll_capacity_ms(clip), room)
        if length < MIN_BROLL_PIECE_MS:
            continue
        blocks.append(_Block(clip, cursor, cursor + length))
        cursor += length
        kept_ids.append(clip.media_id)
    window_start, window_end = blocks[0].start, blocks[-1].end

    # ── cuts, media refs ─────────────────────────────────────────────────────
    cuts: list[FastMontageCut] = []
    refs: list[MediaRef] = []
    # Source windows are ``block - delta`` and ``MediaRef.duration_s`` stays the
    # FULL clip length, so the editor can extend a trimmed tail through
    # ``resync_moment_rows``. Known limit: the first cut's head cannot be
    # extended earlier, because the lip-sync ``window_start_s`` is locked
    # (USER_SONG_LIPSYNC_LOCKED in guided_story).
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
                likelihood=round(block.placed.likelihood, 4),
                position_basis=block.placed.position_basis,  # type: ignore[arg-type]
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
        refs.append(_media_ref(clip))
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
    pins = _pinned_texts(strategy)
    requested_font = strategy.get("font_family")
    requested_font = requested_font if isinstance(requested_font, str) and requested_font else None
    family, title, closing, _labels = _fit_typography(
        font_covers, requested_font, title, closing, {}, extra_texts=[pin.text for pin in pins]
    )
    pins = _fit_pins(pins, family or requested_font or "Fraunces", font_covers)
    pins = drawable_pins(pins, total_s, [cut.output_duration_s for cut in cuts])
    if not title:
        title_source = "none"
    snapshot_kwargs: dict[str, Any] = {}
    if closing:
        snapshot_kwargs["closing_title"] = closing
    if pins:
        snapshot_kwargs["pinned_texts"] = pins
    hold = strategy.get("opening_title_duration_s")
    if isinstance(hold, (int, float)) and not isinstance(hold, bool):
        snapshot_kwargs["opening_title_duration_s"] = hold
    if output_orientation in ("portrait", "landscape"):
        snapshot_kwargs["output_orientation"] = output_orientation
        snapshot_kwargs["output_orientation_reason"] = CREATOR_SELECTED_ORIENTATION_REASON
    style: dict[str, Any] = {}
    if family is not None:
        style["font_family"] = family
    if isinstance(strategy.get("text_color"), str) and strategy.get("text_color"):
        style["text_color"] = strategy["text_color"]

    used_ids = [ref.media_id for ref in refs]
    # Every take stays editable: takes in no cut ride along as unused media.
    unused_refs = [_media_ref(c) for c in takes if c.media_id not in set(used_ids)]
    if unused_refs:
        media_kwargs: dict[str, Any] = {
            "media": refs + unused_refs,
            "media_scope": "selected",
            "selected_media_ids": used_ids,
        }
    else:
        media_kwargs = {"media": refs, "media_scope": "all", "selected_media_ids": used_ids}

    def build(extra: Mapping[str, Any]) -> EditProposalSnapshot:
        return EditProposalSnapshot(
            direction="fast_montage",
            goal="",
            pace="fast",
            duration_s=total_s,
            title=(title or SNAPSHOT_FALLBACK_TITLE)[:100],
            opening_title=title,
            story_beats=_story_beats(cuts),
            fast_cuts=cuts,
            **media_kwargs,
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
        if clip.media_id in kept_ids:
            dropped.append({"media_id": clip.media_id, "reason": "kept_as_broll"})
            continue
        if clip.media_id in used:
            continue
        if reasons.get(clip.media_id) == "overlapped":
            reason = "overlapped"
        elif clip.media_id in placed:
            reason = "outside_window" if trimmed else "gap_unfillable"
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
                "method": b.placed.method,
                "likelihood": round(b.placed.likelihood, 4),
                "margin": round(b.placed.margin, 4),
                "position_basis": b.placed.position_basis,
            }
            for b in blocks
            if b.placed is not None
        ],
        "broll_ids": broll_ids,
        "kept_broll_ids": kept_ids,
        "placed_outside_ids": [
            c.media_id for c in takes if c.media_id in placed and c.media_id not in used
        ],
        "low_confidence_ids": [
            b.clip.media_id
            for b in blocks
            if b.placed is not None
            and (
                b.placed.likelihood < settings.song_align_ask_likelihood
                or b.placed.position_basis in ("tie_break", "creator_stack")
            )
        ],
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
    "HEAD_TRIM_TOLERANCE_S",
    "SYNC_TOLERANCE_S",
    "lipsync_sync_error_s",
    "plan_lipsync_montage",
    "refuse_lipsync_rate_change",
    "resync_lipsync_moments",
    "resync_moment_rows",
    "window_start_for_first_cut_head",
]
