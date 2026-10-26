"""Turn the clips' own sound on and off where the creator said, on the phone's clock (KRI-942).

`app.kria.clip_sound_toggle_ask` reads the asks off the approved brief ("clip sound off except
that last line", "mute the last clip", "no sound from 5 to 8 seconds"). This module decides,
on the clips of a COMPILED phone recipe (the device's own timeline, after refits and
transitions), which stretches of which clip play their own sound, and judges a recipe against
the asks. Everything here is PURE: no I/O, no settings, no model.

Resolution
----------

Each picture clip starts with the base: its own sound on (or only the strategy's named
sources on, or none when the edit keeps no camera sound). The asks then paint stretches on or
off in a fixed order, so two asks never race: every whole-video ask in brief order, then every
targeted ask in brief order; inside one ask its ``sound_except`` parts paint the opposite after
its main part. The last paint covering an instant decides it.

- "first" / "last" is the first / last picture clip of the edit, never the attachment order.
- A named clip is every cut of it. Seconds count from the video's start, or from where the
  named clip starts in the edit.
- A spoken line is grounded in that clip's timed speech (`phone_clip_sound.ground_trigger_line`,
  the KRI-842 grounding, injected as ``ground``) and mapped through every cut that holds it,
  padded by ``LINE_PAD_S`` on each side inside the cut so no word is clipped. A line that isn't
  found, is said in two clips when no clip was named, or isn't in the edit paints nothing.
- Runs shorter than ``MIN_RUN_S`` are not worth a mute: a short silent run plays, a short
  leftover of base sound at a clip edge goes silent.

A clip ends up all on, all off (``source_audio_muted``), or partly off (``mute spans``, which the
compiler turns into the recipe's ``mute_windows``; the engine ramps each edge over 25 ms).

Judging a recipe
----------------

`judge_recipe` reads what the recipe really plays: every clip of a camera source (video, audio
and overlay tracks) at ``volume * original_volume`` (video tracks) minus the recipe's mute
windows. Each ask is judged only on the stretches it still owns after later asks: an "on"
stretch needs its clip's source audible across it, an "off" stretch needs that source silent
(a whole-video "off" needs every camera source silent there, except a source the final plan
plays at that instant). Edges get ``EDGE_TOLERANCE_S`` for the ramps and rounding. This is how
the receipt can say "met" for the speech-excerpt route or after an editor Save without trusting
any planner, and "Couldn't" when the pinned recipe plays the sound after all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from app.kria.clip_sound_toggle_ask import SOUND_OFF, SOUND_ON, SoundTarget, SoundToggleAsk

if TYPE_CHECKING:
    from app.pipeline.phone_clip_sound import GroundedLine

RECEIPT_VERSION = 1

# A kept (or silenced) line plays this much before and after its grounded words.
LINE_PAD_S = 0.2
# A run of sound or silence shorter than this is not built: inaudible as a change, and a
# mute window that short is a click, not a silence.
MIN_RUN_S = 0.2
# What the judge forgives at each edge: the engine's 25 ms ramp plus rounding.
EDGE_TOLERANCE_S = 0.06
# Total seconds of mismatch an ask may have and still be met (rounded edges add up).
_MISMATCH_TOLERANCE_S = 0.05
_EPS = 1e-6

BUILT = "built"
MET = "met"
PARTIAL = "partial"
NOT_POSSIBLE = "not_possible"

# Why an ask (or part of it) was not built or not met. `brief_checks` words each one.
NOT_IN_EDIT = "not_in_edit"
CLIP_MISMATCH = "clip_mismatch"
WINDOW_PAST_END = "window_past_end"
LINE_NOT_FOUND = "line_not_found"
LINE_AMBIGUOUS = "line_ambiguous"
LINE_UNKNOWN = "line_unknown"
LINE_NOT_IN_EDIT = "line_not_in_edit"
LINE_PARTLY_IN_EDIT = "line_partly_in_edit"
NO_SOUND = "no_sound"
PARTIAL_UNSUPPORTED = "partial_unsupported"
CONTRACT_CONFLICT = "contract_conflict"
RENDER_REFUSED = "render_refused"
NOT_BUILT = "not_built"
OVERRIDDEN = "overridden"

BASE_OWNER = -1
SNAP_OWNER = -2

Interval = tuple[float, float]
Ground = Callable[[tuple[str, ...], str], "GroundedLine"]


@dataclass(frozen=True)
class ToggleClip:
    """One picture clip of the compiled recipe: where its own sound plays."""

    clip_id: str
    media_id: str
    start: float  # timeline seconds
    end: float  # timeline seconds where its sound stops (a held freeze tail is silent)
    source_start: float
    rate: float = 1.0

    def to_source(self, t: float) -> float:
        return self.source_start + (t - self.start) * self.rate

    def to_timeline(self, s: float) -> float:
        return self.start + (s - self.source_start) / self.rate

    @property
    def source_end(self) -> float:
        return self.to_source(self.end)


@dataclass(frozen=True)
class Run:
    start: float
    end: float
    state: str  # "on" | "off"
    owner: int  # ask index, BASE_OWNER, or SNAP_OWNER


@dataclass(frozen=True)
class AskRow:
    """What resolution found for one ask (static: no judgement of a recipe)."""

    requirement_id: str
    state: str
    targeted: bool
    reason_code: str | None = None  # the main part could not be placed at all
    partial_code: str | None = None  # placed, but some of it could not be
    line: str | None = None
    line_media_id: str | None = None
    line_start_s: float | None = None  # source seconds of the grounded line
    line_end_s: float | None = None


@dataclass(frozen=True)
class SoundPlan:
    """The resolved on/off plan for every picture clip of one compiled recipe."""

    clips: tuple[ToggleClip, ...]
    runs: Mapping[str, tuple[Run, ...]]
    rows: tuple[AskRow, ...]
    asks_touch: bool = True  # False: no ask and no base selection changed anything
    degraded_clip_ids: frozenset[str] = field(default_factory=frozenset)

    def decision(self, clip_id: str) -> tuple[str, list[Interval]]:
        """``("on", [])``, ``("off", [])`` or ``("partial", muted timeline spans)``."""
        runs = self.runs.get(clip_id) or ()
        off = _merge([(r.start, r.end) for r in runs if r.state == SOUND_OFF])
        if not off:
            return SOUND_ON, []
        clip = next(c for c in self.clips if c.clip_id == clip_id)
        if _length(off) >= clip.end - clip.start - _EPS:
            return SOUND_OFF, []
        return PARTIAL, off

    @property
    def partial_clip_ids(self) -> list[str]:
        return [c.clip_id for c in self.clips if self.decision(c.clip_id)[0] == PARTIAL]

    @property
    def any_audible(self) -> bool:
        return any(self.decision(c.clip_id)[0] != SOUND_OFF for c in self.clips)

    def without_partial(self) -> SoundPlan:
        """The plan a phone that can't mute part of a clip builds: those clips keep their sound.

        A partly silent clip plays whole, so a kept line is still heard; the asks that owned
        its silent part are judged against that and say why (``PARTIAL_UNSUPPORTED``).
        """
        partial = set(self.partial_clip_ids)
        if not partial:
            return self
        runs = dict(self.runs)
        for clip_id in partial:
            runs[clip_id] = tuple(
                replace(run, state=SOUND_ON) if run.state == SOUND_OFF else run
                for run in runs[clip_id]
            )
        return replace(self, runs=runs, degraded_clip_ids=frozenset(partial))

    def owners_of(self, clip_ids: Iterable[str]) -> set[str]:
        """Requirement ids of the asks owning an "off" run on these clips."""
        wanted = set(clip_ids)
        owners = {
            run.owner
            for clip_id, runs in self.runs.items()
            if clip_id in wanted
            for run in runs
            if run.state == SOUND_OFF and run.owner >= 0
        }
        return {self.rows[index].requirement_id for index in owners}

    def mute_spans_source(self, clip_id: str) -> list[Interval]:
        """A partly silent clip's muted spans in its own source seconds (a later trim keeps
        the same footage muted)."""
        kind, spans = self.decision(clip_id)
        if kind != PARTIAL:
            return []
        clip = next(c for c in self.clips if c.clip_id == clip_id)
        return [(round(clip.to_source(a), 3), round(clip.to_source(b), 3)) for a, b in spans]


# -- intervals ---------------------------------------------------------------------------


def _merge(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[1] - i[0] > _EPS):
        if out and start <= out[-1][1] + _EPS:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _length(intervals: Iterable[Interval]) -> float:
    return sum(max(0.0, end - start) for start, end in intervals)


def _intersect(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            start, end = max(s1, s2), min(e1, e2)
            if end - start > _EPS:
                out.append((start, end))
    return _merge(out)


def _subtract(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    out = list(_merge(a))
    for cut_start, cut_end in _merge(b):
        kept = []
        for start, end in out:
            if cut_end <= start or cut_start >= end:
                kept.append((start, end))
                continue
            if cut_start > start:
                kept.append((start, cut_start))
            if cut_end < end:
                kept.append((cut_end, end))
        out = kept
    return _merge(out)


def _shrink(interval: Interval, by: float) -> Interval | None:
    start, end = interval[0] + by, interval[1] - by
    return (start, end) if end - start > _EPS else None


# -- resolution --------------------------------------------------------------------------


def _media_match(ref: str, media_ids: Sequence[str]) -> str | None:
    """The clip ``ref`` names (its id, or a 6+ letter part of exactly one id), else None."""
    from app.kria.brief_checks import _CLIP_ID_PART_RE  # noqa: PLC0415

    known = list(dict.fromkeys(media_ids))
    if ref in known:
        return ref
    if len(ref) < 6 or not ref.isalnum():
        return None
    folded = ref.casefold()
    hits = [m for m in known if folded in {p.casefold() for p in _CLIP_ID_PART_RE.split(m)}]
    return hits[0] if len(hits) == 1 else None


@dataclass
class _Placed:
    intervals: dict[str, list[Interval]] = field(default_factory=dict)  # by clip id
    reason: str | None = None
    partial: str | None = None
    line: str | None = None
    line_media: str | None = None
    line_window: Interval | None = None


def _place(
    target: SoundTarget,
    clips: Sequence[ToggleClip],
    duration_s: float,
    ground: Ground | None,
) -> _Placed:
    """Where ``target`` falls on the timeline, per clip, or why it falls nowhere."""
    placed = _Placed()
    candidates = list(clips)
    media_ids = [c.media_id for c in clips]
    if target.media_ref is not None:
        media = _media_match(target.media_ref, media_ids)
        if media is None:
            placed.reason = NOT_IN_EDIT
            return placed
        candidates = [c for c in clips if c.media_id == media]
    if target.clip_position is not None:
        if not clips:
            placed.reason = NOT_IN_EDIT
            return placed
        chosen = clips[0] if target.clip_position == "first" else clips[-1]
        if target.media_ref is not None and chosen not in candidates:
            placed.reason = CLIP_MISMATCH
            return placed
        candidates = [chosen]
    windows = {c.clip_id: [(c.start, c.end)] for c in candidates}
    if target.has_window:
        origin = min(c.start for c in candidates) if target.names_clip else 0.0
        bound = max(c.end for c in candidates) if target.names_clip else duration_s
        lo = origin + (target.from_s or 0.0)
        hi = min(bound, origin + target.to_s) if target.to_s is not None else bound
        if lo >= min(bound, duration_s) - _EPS or hi - lo <= _EPS:
            placed.reason = WINDOW_PAST_END
            return placed
        windows = {
            clip_id: _intersect(spans, [(lo, min(hi, duration_s))])
            for clip_id, spans in windows.items()
        }
    if target.lines:
        if ground is None:
            placed.reason = LINE_UNKNOWN
            return placed
        hits: list[tuple[str, GroundedLine]] = []
        statuses: list[str] = []
        for media in dict.fromkeys(c.media_id for c in candidates):
            grounded = ground(target.lines, media)
            statuses.append(grounded.status)
            if grounded.status == "found":
                hits.append((media, grounded))
        if len(hits) > 1:
            placed.reason = LINE_AMBIGUOUS
            return placed
        if not hits:
            placed.reason = (
                LINE_AMBIGUOUS
                if "ambiguous" in statuses
                else LINE_UNKNOWN
                if statuses and all(s == "unknown" for s in statuses)
                else LINE_NOT_FOUND
            )
            return placed
        media, grounded = hits[0]
        placed.line, placed.line_media = grounded.quote, media
        placed.line_window = (grounded.start_s, grounded.end_s)
        pieces: dict[str, list[Interval]] = {}
        held = 0.0
        for clip in candidates:
            if clip.media_id != media:
                continue
            start = max(grounded.start_s, clip.source_start)
            end = min(grounded.end_s, clip.source_end)
            if end - start <= _EPS:
                continue
            held += end - start
            padded = (
                max(clip.start, clip.to_timeline(start) - LINE_PAD_S),
                min(clip.end, clip.to_timeline(end) + LINE_PAD_S),
            )
            pieces[clip.clip_id] = _intersect(windows.get(clip.clip_id, []), [padded])
        if not any(pieces.values()):
            placed.reason = LINE_NOT_IN_EDIT
            return placed
        if held < grounded.end_s - grounded.start_s - EDGE_TOLERANCE_S:
            placed.partial = LINE_PARTLY_IN_EDIT
        windows = pieces
    placed.intervals = {clip_id: spans for clip_id, spans in windows.items() if spans}
    if not placed.intervals:
        placed.reason = WINDOW_PAST_END
    return placed


def _final_runs(clip: ToggleClip, paints: Sequence[Run]) -> list[Run]:
    edges = sorted(
        {clip.start, clip.end}
        | {p.start for p in paints if clip.start < p.start < clip.end}
        | {p.end for p in paints if clip.start < p.end < clip.end}
    )
    runs: list[Run] = []
    for start, end in zip(edges, edges[1:], strict=False):
        if end - start <= _EPS:
            continue
        mid = (start + end) / 2
        top = next((p for p in reversed(paints) if p.start <= mid < p.end), None)
        state, owner = (top.state, top.owner) if top else (SOUND_ON, BASE_OWNER)
        if runs and runs[-1].state == state and runs[-1].owner == owner:
            runs[-1] = replace(runs[-1], end=end)
        else:
            runs.append(Run(start, end, state, owner))
    return runs


def _snap(clip: ToggleClip, runs: list[Run]) -> list[Run]:
    """Drop runs too short to build (see `MIN_RUN_S`); a snapped run is owned by no ask."""
    if clip.end - clip.start <= MIN_RUN_S:
        return runs
    out = []
    for index, run in enumerate(runs):
        short = run.end - run.start < MIN_RUN_S - _EPS
        at_edge = index == 0 or index == len(runs) - 1
        if short and run.state == SOUND_OFF and len(runs) > 1:
            run = replace(run, state=SOUND_ON, owner=SNAP_OWNER)
        elif short and run.state == SOUND_ON and run.owner == BASE_OWNER and at_edge:
            if len(runs) > 1:
                run = replace(run, state=SOUND_OFF, owner=SNAP_OWNER)
        out.append(run)
    return out


def resolve_sound_toggles(
    asks: Sequence[SoundToggleAsk],
    clips: Sequence[ToggleClip],
    *,
    duration_s: float,
    ground: Ground | None = None,
    base_on_media_ids: frozenset[str] | None = None,
    base_on: bool = True,
    muted_clip_ids: frozenset[str] = frozenset(),
) -> SoundPlan:
    """The on/off plan for ``clips`` (picture clips of one compiled recipe, any order).

    ``base_on`` False: the edit keeps no camera sound unless an ask turns some on.
    ``base_on_media_ids``: only these clips start with their sound on (the strategy's named
    sources). ``muted_clip_ids``: clips the plan already silenced (an editor choice). All three
    describe the base the asks paint over.
    """
    ordered = tuple(sorted(clips, key=lambda c: (c.start, c.clip_id)))
    paints: dict[str, list[Run]] = {}
    for clip in ordered:
        on = (
            base_on
            and clip.clip_id not in muted_clip_ids
            and (base_on_media_ids is None or clip.media_id in base_on_media_ids)
        )
        paints[clip.clip_id] = [
            Run(clip.start, clip.end, SOUND_ON if on else SOUND_OFF, BASE_OWNER)
        ]
    rows: list[AskRow] = []
    placements: list[tuple[_Placed, list[_Placed]]] = []
    for ask in asks:
        main = _place(ask.target, ordered, duration_s, ground)
        exceptions = [_place(part, ordered, duration_s, ground) for part in ask.exceptions]
        placements.append((main, exceptions))
        line_from = main if main.line else next((e for e in exceptions if e.line), None)
        partial = main.partial or next(
            (e.reason or e.partial for e in exceptions if e.reason or e.partial), None
        )
        rows.append(
            AskRow(
                requirement_id=ask.requirement_id,
                state=ask.state,
                targeted=ask.targeted,
                reason_code=main.reason,
                partial_code=None if main.reason else partial,
                line=line_from.line if line_from else None,
                line_media_id=line_from.line_media if line_from else None,
                line_start_s=round(line_from.line_window[0], 3)
                if line_from and line_from.line_window
                else None,
                line_end_s=round(line_from.line_window[1], 3)
                if line_from and line_from.line_window
                else None,
            )
        )
    order = [i for i, ask in enumerate(asks) if not ask.targeted] + [
        i for i, ask in enumerate(asks) if ask.targeted
    ]
    for index in order:
        ask = asks[index]
        main, exceptions = placements[index]
        if main.reason:
            continue
        opposite = SOUND_ON if ask.state == SOUND_OFF else SOUND_OFF
        for clip_id, spans in main.intervals.items():
            paints[clip_id].extend(Run(a, b, ask.state, index) for a, b in spans)
        for part in exceptions:
            for clip_id, spans in part.intervals.items():
                paints[clip_id].extend(Run(a, b, opposite, index) for a, b in spans)
    runs = {c.clip_id: tuple(_snap(c, _final_runs(c, paints[c.clip_id]))) for c in ordered}
    touched = bool(asks) or base_on_media_ids is not None or not base_on
    return SoundPlan(clips=ordered, runs=runs, rows=tuple(rows), asks_touch=touched)


# -- reading a recipe --------------------------------------------------------------------


def toggle_clips_from_recipe(
    recipe: Any, *, video_track_id: str, asset_to_media: Mapping[str, str]
) -> list[ToggleClip]:
    """The picture clips of ``recipe``'s main track whose source is footage the creator filmed."""
    track = next((t for t in recipe.tracks if t.id == video_track_id), None)
    if track is None:
        return []
    out = []
    for clip in track.clips:
        media = asset_to_media.get(clip.source_asset_id)
        if media is None:
            continue
        rate = float(clip.rate or 1.0)
        start = float(clip.timeline_start)
        out.append(
            ToggleClip(
                clip_id=clip.id,
                media_id=media,
                start=start,
                end=start + float(clip.source_duration) / rate,
                source_start=float(clip.source_start),
                rate=rate,
            )
        )
    return sorted(out, key=lambda c: (c.start, c.clip_id))


def audible_runs(
    recipe: Any, *, camera_assets: Iterable[str], silent_assets: Iterable[str] = ()
) -> dict[str, list[Interval]]:
    """Per camera source asset, where the recipe really plays its sound (timeline seconds)."""
    cameras = set(camera_assets) - set(silent_assets)
    windows = list(getattr(recipe.audio, "mute_windows", None) or [])
    original = float(recipe.audio.original_volume)
    out: dict[str, list[Interval]] = {asset: [] for asset in cameras}
    for track in recipe.tracks:
        for clip in track.clips:
            if clip.source_asset_id not in cameras:
                continue
            if clip.volume <= 0 or (track.kind == "video" and original <= 0):
                continue
            start = float(clip.timeline_start)
            span = [(start, start + float(clip.source_duration) / float(clip.rate or 1.0))]
            muted = [(w.start, w.end) for w in windows if clip.id in w.clip_ids]
            out[clip.source_asset_id].extend(_subtract(span, muted))
    return {asset: _merge(spans) for asset, spans in out.items()}


def judge_recipe(
    plan: SoundPlan,
    recipe: Any,
    *,
    camera_assets: Iterable[str],
    silent_assets: Iterable[str] = (),
    reasons: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The receipt: each ask judged against what ``recipe`` really plays.

    ``reasons`` (by requirement id) is why the worker could not build part of an ask (a phone
    that can't mute part of a clip, a compile that refused it); without one a mismatch says
    ``NOT_BUILT``. Camera asset ids equal their media ids (`PhoneSourceBinding.render_asset`).
    """
    silent = set(silent_assets)
    playing = audible_runs(recipe, camera_assets=camera_assets, silent_assets=silent)
    desired_on: dict[str, list[Interval]] = {}
    for clip in plan.clips:
        desired_on.setdefault(clip.media_id, []).extend(
            (r.start, r.end) for r in plan.runs[clip.clip_id] if r.state == SOUND_ON
        )
    desired_on = {media: _merge(spans) for media, spans in desired_on.items()}
    media_of = {c.clip_id: c.media_id for c in plan.clips}
    rows = []
    for index, row in enumerate(plan.rows):
        owned_on: list[tuple[str, Interval]] = []
        owned_off: list[tuple[str, Interval]] = []
        for clip_id, runs in plan.runs.items():
            for run in runs:
                if run.owner != index:
                    continue
                (owned_on if run.state == SOUND_ON else owned_off).append(
                    (media_of[clip_id], (run.start, run.end))
                )
        owned = _length(i for _m, i in owned_on) + _length(i for _m, i in owned_off)
        mismatch = checked = 0.0
        no_sound = False
        for media, interval in owned_on:
            core = _shrink(interval, EDGE_TOLERANCE_S)
            if core is None:
                continue
            checked += core[1] - core[0]
            if media in silent:
                no_sound = True
            mismatch += _length(_subtract([core], playing.get(media, [])))
        for media, interval in owned_off:
            core = _shrink(interval, EDGE_TOLERANCE_S)
            if core is None:
                continue
            checked += core[1] - core[0]
            sources = [media] if row.targeted else list(playing)
            for source in sources:
                heard = _intersect([core], playing.get(source, []))
                if not row.targeted:
                    heard = _subtract(heard, desired_on.get(source, []))
                mismatch += _length(heard)
        reason = (reasons or {}).get(row.requirement_id)
        if row.reason_code and owned <= _EPS:
            status, code = NOT_POSSIBLE, row.reason_code
        elif owned <= _EPS:
            status, code = NOT_POSSIBLE, reason or OVERRIDDEN
        elif mismatch <= _MISMATCH_TOLERANCE_S:
            status, code = (PARTIAL, row.partial_code) if row.partial_code else (MET, None)
        else:
            status = PARTIAL if checked - mismatch > MIN_RUN_S else NOT_POSSIBLE
            code = NO_SOUND if no_sound else reason or NOT_BUILT
        rows.append(
            {
                "requirement_id": row.requirement_id,
                "state": row.state,
                "status": status,
                "reason_code": code,
                "on_windows": _rounded(i for _m, i in owned_on),
                "off_windows": _rounded(i for _m, i in owned_off),
                "line": row.line,
                "line_media_id": row.line_media_id,
                "line_start_s": row.line_start_s,
                "line_end_s": row.line_end_s,
                "mismatch_s": round(mismatch, 3),
            }
        )
    return {"version": RECEIPT_VERSION, "rows": rows}


def _rounded(intervals: Iterable[Interval]) -> list[list[float]]:
    return [[round(a, 2), round(b, 2)] for a, b in _merge(intervals)]


__all__ = [
    "BUILT",
    "CLIP_MISMATCH",
    "CONTRACT_CONFLICT",
    "EDGE_TOLERANCE_S",
    "LINE_AMBIGUOUS",
    "LINE_NOT_FOUND",
    "LINE_NOT_IN_EDIT",
    "LINE_PAD_S",
    "LINE_PARTLY_IN_EDIT",
    "LINE_UNKNOWN",
    "MET",
    "MIN_RUN_S",
    "NOT_BUILT",
    "NOT_IN_EDIT",
    "NOT_POSSIBLE",
    "NO_SOUND",
    "OVERRIDDEN",
    "PARTIAL",
    "PARTIAL_UNSUPPORTED",
    "RENDER_REFUSED",
    "RECEIPT_VERSION",
    "WINDOW_PAST_END",
    "AskRow",
    "Run",
    "SoundPlan",
    "ToggleClip",
    "audible_runs",
    "judge_recipe",
    "resolve_sound_toggles",
    "toggle_clips_from_recipe",
]
