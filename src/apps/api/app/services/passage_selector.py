"""Choose which passages of one spoken take stay in the video (KRI-1555).

``select_passages(words, ask)`` is the single place that decides, by meaning, what a
spoken take keeps. The creator's request, a length target and an optional outline go
in together with the WHOLE timed transcript; an ordered, non-overlapping list of kept
source ranges comes out, each with its quoted text and a reason, plus every passage
that was left out and why.

Two layers keep the model honest.

* The model (``nova.plan.passage_selector``) answers in sentence-unit ids and a
  verbatim quote, never in seconds. Its quote is re-grounded against the words of the
  id range it named (``ground_passage``), so an id range that points at other speech
  is dropped, and every kept range starts and ends on a sentence, padded into the
  silence beside it (``safe_window``), never inside a word.
* Plain code owns the clock and the length. A target is enforced here: candidates are
  fitted by the model's ``strength``, weakest first to go, so the result lands inside
  the requested length instead of the model's guess at it.

The text this module quotes comes from the creator's own transcript, never from the
model. ``plan_speech_montage`` shares ``ground_passage`` for its own quoted sections.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Any, Literal, NamedTuple

from app.agents.passage_selector import (
    PassageSelectorInput,
    PassageSelectorOutput,
    PassageUnitView,
)
from app.services.speech_segments import (
    GroundedExcerpt,
    ground_excerpt,
    safe_window,
    sentence_groups,
)

WINDOW_CHARS = 9000
_UNIT_OVERHEAD_CHARS = 48
CANDIDATE_OVERSHOOT = 1.5
TARGET_LOW = 0.9
TARGET_HIGH = 1.1
MIN_PASSAGE_S = 0.8
MAX_QUOTE_CHARS = 400
_QUOTE_EDGE_WORDS = 7
_EPS = 1e-6

LeftOutReason = Literal["off_topic", "requested_drop", "over_length", "not_selected", "ungrounded"]


class PassageSelectionError(Exception):
    """No usable selection. The caller keeps the whole take rather than cut on a guess."""


@dataclass(frozen=True)
class SelectionAsk:
    """What the creator asked for.

    ``keep_whole`` keeps everything except ``drop_topics`` and wins over ``target_s``.
    ``target_is_ceiling`` ("at most 30 s") needs a ``target_s``.
    """

    request: str = ""
    target_s: float | None = None
    target_is_ceiling: bool = False
    outline: str | None = None
    drop_topics: tuple[str, ...] = ()
    keep_whole: bool = False

    def __post_init__(self) -> None:
        if self.target_s is not None and self.target_s <= 0:
            raise ValueError("target_s must be positive")
        if self.target_is_ceiling and self.target_s is None:
            raise ValueError("a ceiling needs a target_s")


@dataclass(frozen=True)
class KeptPassage:
    start_s: float
    end_s: float
    quote: str
    reason: str


@dataclass(frozen=True)
class LeftOutPassage:
    start_s: float
    end_s: float
    quote: str
    reason: LeftOutReason


@dataclass(frozen=True)
class PassageSelection:
    """``kept`` is in source order and never overlaps; ``left_out`` holds spoken passages only."""

    kept: tuple[KeptPassage, ...]
    left_out: tuple[LeftOutPassage, ...] = ()
    target_s: float | None = None
    fits_target: bool = True

    @property
    def kept_s(self) -> float:
        return round(sum(p.end_s - p.start_s for p in self.kept), 3)


PassageRunner = Callable[[PassageSelectorInput], PassageSelectorOutput]


@dataclass(frozen=True)
class GroundingFailure:
    reason: Literal["not_said", "too_short", "too_long"]


def ground_passage(
    words: Sequence[Any],
    quote: str,
    *,
    duration_s: float | None = None,
    min_after_s: float = 0.0,
    min_s: float = MIN_PASSAGE_S,
    max_s: float | None = None,
) -> GroundedExcerpt | GroundingFailure:
    """A quote resolved to a sentence-safe window, or why it cannot be played."""
    grounded = ground_excerpt(words, quote, duration_s=duration_s, min_after_s=min_after_s)
    if grounded is None:
        return GroundingFailure("not_said")
    length = grounded.end_s - grounded.start_s
    if length < min_s:
        return GroundingFailure("too_short")
    if max_s is not None and length > max_s:
        return GroundingFailure("too_long")
    return grounded


class _Timing(NamedTuple):
    start_s: float
    end_s: float


@dataclass(frozen=True)
class _Unit:
    id: int
    first_word: int
    last_word: int
    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class _Run:
    first: int
    last: int
    strength: int
    reason: str


@dataclass
class _Timeline:
    rows: list[dict[str, Any]]
    units: list[_Unit]
    timings: list[_Timing]
    duration_s: float | None

    @classmethod
    def build(cls, words: Iterable[Any], duration_s: float | None) -> _Timeline:
        rows = [_row(w) for w in words or []]
        rows = [r for r in rows if str(r["text"]).strip() and r["end_s"] >= r["start_s"]]
        units: list[_Unit] = []
        cursor = 0
        for group in sentence_groups(rows):
            first, last = cursor, cursor + len(group) - 1
            cursor = last + 1
            text = " ".join(str(r["text"]).strip() for r in rows[first : last + 1])
            units.append(
                _Unit(len(units), first, last, rows[first]["start_s"], rows[last]["end_s"], text)
            )
        timings = [_Timing(r["start_s"], r["end_s"]) for r in rows]
        return cls(rows, units, timings, duration_s)

    def window(self, first: int, last: int) -> tuple[float, float]:
        return safe_window(
            self.timings, self.units[first].first_word, self.units[last].last_word, self.duration_s
        )

    def length(self, ids: Iterable[int]) -> float:
        total = 0.0
        for first, last in _groups(sorted(set(ids))):
            start, end = self.window(first, last)
            total += end - start
        return total

    def words(self, first: int, last: int) -> list[dict[str, Any]]:
        return self.rows[self.units[first].first_word : self.units[last].last_word + 1]

    def quote(self, first: int, last: int) -> str:
        texts = [self.units[i].text for i in range(first, last + 1)]
        full = " ".join(texts)
        if len(full) <= MAX_QUOTE_CHARS:
            return full
        words = full.split()
        return (
            " ".join(words[:_QUOTE_EDGE_WORDS]) + " ... " + " ".join(words[-_QUOTE_EDGE_WORDS:])
        )[:MAX_QUOTE_CHARS]


def _row(word: Any) -> dict[str, Any]:
    get = word.get if isinstance(word, dict) else lambda k, d=None: getattr(word, k, d)
    return {
        "text": get("text", "") or "",
        "start_s": float(get("start_s", 0.0) or 0.0),
        "end_s": float(get("end_s", 0.0) or 0.0),
    }


def _groups(ids: Sequence[int]) -> list[tuple[int, int]]:
    """Maximal runs of consecutive ids, as (first, last)."""
    out: list[tuple[int, int]] = []
    for i in ids:
        if out and i == out[-1][1] + 1:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def _windows(units: Sequence[_Unit], window_chars: int) -> list[list[_Unit]]:
    windows: list[list[_Unit]] = [[]]
    size = 0
    for unit in units:
        cost = len(unit.text) + _UNIT_OVERHEAD_CHARS
        if windows[-1] and size + cost > window_chars:
            windows.append([])
            size = 0
        windows[-1].append(unit)
        size += cost
    return windows


def select_passages(
    words: Sequence[Any],
    ask: SelectionAsk,
    *,
    run_agent: PassageRunner,
    language: str = "",
    duration_s: float | None = None,
    max_passage_s: float | None = None,
    window_chars: int = WINDOW_CHARS,
) -> PassageSelection:
    """Which passages of ``words`` (whisper word timings of ONE take) the video keeps.

    Raises ``PassageSelectionError`` when nothing usable came back; the model's own
    failures propagate unchanged so the caller can tell a refusal from a timeout.
    ``max_passage_s`` splits a kept range at sentence boundaries into pieces no longer
    than it (a single sentence longer than it stays whole).
    """
    timeline = _Timeline.build(words, duration_s)
    if not timeline.units:
        raise PassageSelectionError("the take has no speech")
    all_ids = [u.id for u in timeline.units]
    drop_only = bool(ask.drop_topics) and (
        ask.keep_whole or (ask.target_s is None and not (ask.outline or "").strip())
    )
    if ask.keep_whole and not ask.drop_topics:
        return _assemble(
            timeline, set(all_ids), {}, {i: "kept whole" for i in all_ids}, ask, max_passage_s
        )

    total_s = sum(u.end_s - u.start_s for u in timeline.units) or 1.0
    windows = _windows(timeline.units, window_chars)
    labels: dict[int, LeftOutReason] = {}
    reasons: dict[int, str] = {}
    candidates: list[_Run] = []
    removed: set[int] = set()
    for index, window in enumerate(windows):
        window_s = sum(u.end_s - u.start_s for u in window)
        budget = None
        if ask.target_s is not None and not drop_only:
            budget = round(
                min(CANDIDATE_OVERSHOOT * ask.target_s * window_s / total_s, window_s), 1
            )
        out = run_agent(
            _window_input(ask, window, index, len(windows), budget, drop_only, language)
        )
        for row in out.dropped:
            for i in range(row.first, row.last + 1):
                removed.add(i)
                labels[i] = row.reason
        if drop_only:
            continue
        for passage in out.passages:
            if not _verified(timeline, passage.first, passage.last, passage.quote):
                for i in range(passage.first, passage.last + 1):
                    labels.setdefault(i, "ungrounded")
                continue
            candidates.append(_Run(passage.first, passage.last, passage.strength, passage.reason))

    if drop_only:
        kept_ids = set(all_ids) - removed
        reasons = {i: "not removed" for i in kept_ids}
    else:
        runs = _without(_merge_overlaps(candidates), removed)
        chosen = _fit(timeline, runs, ask)
        kept_ids = {i for run in chosen for i in range(run.first, run.last + 1)}
        for run in runs:
            for i in range(run.first, run.last + 1):
                if i not in kept_ids:
                    labels[i] = "over_length"
        for run in chosen:
            for i in range(run.first, run.last + 1):
                reasons[i] = run.reason
    if not kept_ids:
        raise PassageSelectionError("no passage survived grounding and the length fit")
    return _assemble(timeline, kept_ids, labels, reasons, ask, max_passage_s)


def _window_input(
    ask: SelectionAsk,
    window: Sequence[_Unit],
    index: int,
    count: int,
    budget: float | None,
    drop_only: bool,
    language: str,
) -> PassageSelectorInput:
    return PassageSelectorInput(
        mode="drop" if drop_only else "pick",
        request=ask.request,
        target_s=ask.target_s,
        target_is_ceiling=ask.target_is_ceiling,
        candidate_budget_s=budget,
        outline=ask.outline,
        drop_topics=list(ask.drop_topics),
        language=language,
        window_index=index,
        window_count=count,
        units=[
            PassageUnitView(id=u.id, s=round(u.start_s, 2), e=round(u.end_s, 2), text=u.text)
            for u in window
        ],
    )


def _verified(timeline: _Timeline, first: int, last: int, quote: str) -> bool:
    """The quote is said inside the id range and touches both its first and last sentence."""
    grounded = ground_passage(timeline.words(first, last), quote, min_s=0.0)
    if isinstance(grounded, GroundingFailure):
        return False
    return (
        grounded.word_start_s <= timeline.units[first].end_s + _EPS
        and grounded.word_end_s >= timeline.units[last].start_s - _EPS
    )


def _merge_overlaps(runs: Sequence[_Run]) -> list[_Run]:
    merged: list[_Run] = []
    for run in sorted(runs, key=lambda r: (r.first, r.last)):
        if merged and run.first <= merged[-1].last:
            top = merged[-1]
            strongest = run if run.strength > top.strength else top
            merged[-1] = _Run(
                top.first, max(top.last, run.last), strongest.strength, strongest.reason
            )
        else:
            merged.append(run)
    return merged


def _without(runs: Sequence[_Run], removed: set[int]) -> list[_Run]:
    out: list[_Run] = []
    for run in runs:
        ids = [i for i in range(run.first, run.last + 1) if i not in removed]
        out.extend(_Run(a, b, run.strength, run.reason) for a, b in _groups(ids))
    return out


def _ids(runs: Iterable[_Run]) -> list[int]:
    return [i for run in runs for i in range(run.first, run.last + 1)]


def _fit(timeline: _Timeline, runs: Sequence[_Run], ask: SelectionAsk) -> list[_Run]:
    """The strongest candidates whose merged length lands in the asked length."""
    if not runs:
        return []
    if ask.target_s is None:
        return list(runs)
    cap = ask.target_s * (1.0 if ask.target_is_ceiling else TARGET_HIGH)
    goal = None if ask.target_is_ceiling else ask.target_s
    chosen: list[_Run] = []
    for run in sorted(runs, key=lambda r: (-r.strength, r.first)):
        if timeline.length(_ids([*chosen, run])) <= cap + _EPS:
            chosen.append(run)
        if goal is not None and timeline.length(_ids(chosen)) >= goal - _EPS:
            break
    if chosen:
        return sorted(chosen, key=lambda r: r.first)
    best = max(runs, key=lambda r: (r.strength, -r.first))
    last = best.last
    while last > best.first and timeline.length(range(best.first, last + 1)) > cap + _EPS:
        last -= 1
    if timeline.length(range(best.first, last + 1)) > cap + _EPS:
        raise PassageSelectionError("no sentence is short enough for the requested length")
    return [_Run(best.first, last, best.strength, best.reason)]


def _assemble(
    timeline: _Timeline,
    kept_ids: set[int],
    labels: dict[int, LeftOutReason],
    reasons: dict[int, str],
    ask: SelectionAsk,
    max_passage_s: float | None,
) -> PassageSelection:
    kept: list[KeptPassage] = []
    for first, last in _groups(sorted(kept_ids)):
        for a, b in _split(timeline, first, last, max_passage_s):
            start, end = timeline.window(a, b)
            why = "; ".join(dict.fromkeys(reasons[i] for i in range(a, b + 1) if reasons.get(i)))
            kept.append(KeptPassage(start, end, timeline.quote(a, b), why[:160]))
    left: list[LeftOutPassage] = []
    for first, last in _groups([u.id for u in timeline.units if u.id not in kept_ids]):
        by_label = groupby(range(first, last + 1), key=lambda i: labels.get(i, "not_selected"))
        for label, ids in by_label:
            run = list(ids)
            start, end = timeline.window(run[0], run[-1])
            left.append(LeftOutPassage(start, end, timeline.quote(run[0], run[-1]), label))
    total = round(sum(p.end_s - p.start_s for p in kept), 3)
    return PassageSelection(
        kept=tuple(kept),
        left_out=tuple(left),
        target_s=ask.target_s,
        fits_target=_fits(total, ask),
    )


def _split(
    timeline: _Timeline, first: int, last: int, max_s: float | None
) -> list[tuple[int, int]]:
    if max_s is None:
        return [(first, last)]
    pieces: list[tuple[int, int]] = []
    start = first
    for i in range(first + 1, last + 1):
        a, b = timeline.window(start, i)
        if b - a > max_s:
            pieces.append((start, i - 1))
            start = i
    pieces.append((start, last))
    return pieces


def _fits(total_s: float, ask: SelectionAsk) -> bool:
    if ask.target_s is None:
        return True
    if ask.target_is_ceiling:
        return total_s <= ask.target_s + _EPS
    return TARGET_LOW * ask.target_s - _EPS <= total_s <= TARGET_HIGH * ask.target_s + _EPS


__all__ = [
    "GroundingFailure",
    "KeptPassage",
    "LeftOutPassage",
    "PassageRunner",
    "PassageSelection",
    "PassageSelectionError",
    "SelectionAsk",
    "ground_passage",
    "select_passages",
]
