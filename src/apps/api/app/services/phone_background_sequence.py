"""Lay a creator's background sequence out on the kept take (KRI-1162).

The Main Creator names an ordered ``sequence`` of Visuals-pool photos / videos, optionally each
with the words that bring it in (``spoken_phrase``) and how long it stays (``hold_s``), plus a
``cadence_s`` for the entries nobody anchored. This module turns that REQUEST into timed windows:
pure arithmetic over the spoken words and the speech cut, no I/O, so every failure mode has a
direct test (``tests/tasks/test_speaker_background_strategy_worker.py``).

Time bases. The layout happens in OUTPUT time (what the viewer sees: the take after the speech
cleanup cut), because "a new photo every 4 seconds" is a promise about what is watched. The
phone lane wants the ORIGINAL take's time (``remap_lanes_for_cut`` moves it), so every window is
converted back with ``to_source_time`` before it leaves; a window that straddles a removal
then remaps to exactly the output span laid out here. ``words`` are the spoken words in the
ORIGINAL take's time (the worker's ``raw_words``, the same transcript reaction beats ground
against); a phrase heard only inside a removed span is not heard in the kept take.

Rules (the contract in ``docs/reviews/speaker-background-windows-failure-modes.md``):

* an entry with a ``spoken_phrase`` starts at the first loose-text match of it in the kept take
  and ends at the earliest of the next window's start and ``hold_s`` (else ``cadence_s``) later;
* the remaining time is filled by the entries without an anchor, in order, ``cadence_s`` each,
  cycling until the take ends (a phrase that is never heard counts as unanchored and is
  reported as ``phrase_not_heard``, S38);
* when every entry is anchored the gaps keep the fill background;
* windows are at least ``MIN_WINDOW_S`` long, never past the take end (S39), at most
  ``MAX_WINDOWS``; a sequence that would need more is stretched to a longer cadence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

MIN_WINDOW_S = 0.5
MAX_WINDOWS = 40
_EPS = 1e-6
_STRETCH = 1.25


@dataclass(frozen=True)
class SequenceEntry:
    """One resolved sequence entry: its position in the strategy's list and what it asked for."""

    index: int
    spoken_phrase: str | None = None
    hold_s: float | None = None


@dataclass(frozen=True)
class OutputWindow:
    """One laid-out window in OUTPUT (kept-take) seconds; ``index`` is the entry's position."""

    index: int
    start_s: float
    end_s: float


@dataclass(frozen=True)
class SequenceLayout:
    windows: tuple[OutputWindow, ...]
    notes: tuple[dict, ...] = ()
    cadence_s: float = 4.0
    anchored: frozenset[int] = field(default_factory=frozenset)


def kept_duration(keep_segments: Sequence[tuple[float, float]] | None, full_s: float) -> float:
    """The take's length after the cut (the whole clip when nothing was cut)."""
    if keep_segments is None:
        return float(full_s)
    return float(sum(max(0.0, end - start) for start, end in keep_segments))


def to_output_time(
    source_s: float, keep_segments: Sequence[tuple[float, float]] | None
) -> float | None:
    """Where an original-take instant plays in the kept take; ``None`` inside a removal."""
    if keep_segments is None:
        return float(source_s)
    played = 0.0
    for start, end in keep_segments:
        if start - _EPS <= source_s < end:
            return played + max(0.0, source_s - start)
        played += max(0.0, end - start)
    return None


def to_source_time(
    output_s: float,
    keep_segments: Sequence[tuple[float, float]] | None,
    *,
    as_end: bool = False,
) -> float:
    """The original-take instant that plays at ``output_s``.

    At a boundary between two kept segments a window START belongs to the later segment and a
    window END to the earlier one, so a window across a removal remaps (by
    ``silence_cut.remap_range``) to exactly its output span.
    """
    if keep_segments is None:
        return float(output_s)
    played = 0.0
    last_end = 0.0
    for start, end in keep_segments:
        length = max(0.0, end - start)
        if length <= 0.0:
            continue
        if (output_s <= played + length + _EPS) if as_end else (output_s < played + length - _EPS):
            return start + max(0.0, output_s - played)
        played += length
        last_end = end
    return last_end


def _anchor_time(
    entry: SequenceEntry,
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
) -> float | None:
    """Output time of the first loose-text match of ``entry.spoken_phrase`` in the kept take."""
    phrase = entry.spoken_phrase
    if not phrase or not words:
        return None
    from app.services import phone_reaction_grounding as grounding  # noqa: PLC0415

    timed = [word for word in words if isinstance(word, dict)]
    stream = grounding._token_stream(timed)  # noqa: SLF001 - the one tokenizer beats ground with
    for start_s, _end_s, _heard in grounding._phrase_windows(stream, timed, phrase):  # noqa: SLF001
        placed = to_output_time(float(start_s), keep_segments)
        if placed is not None:
            return placed
    return None


def _fill_gap(
    gap_start: float,
    gap_end: float,
    cycle: list[int],
    cursor: int,
    cadence_s: float,
    out: list[OutputWindow],
) -> int:
    """Cadence-fill ``[gap_start, gap_end)`` from ``cycle`` (entry indexes); the new cursor.

    The tail is absorbed into the last window when it would be a sliver (under half a cadence), so
    the last window of a gap ends exactly at the gap's end (the take end for the final gap).
    """
    if not cycle or gap_end - gap_start < MIN_WINDOW_S - _EPS:
        return cursor
    if len(cycle) == 1:
        out.append(OutputWindow(cycle[0], gap_start, gap_end))
        return cursor
    t = gap_start
    # A leftover shorter than half a cadence joins the window before it rather than flashing
    # a sliver of the next photo.
    sliver = max(MIN_WINDOW_S, cadence_s / 2.0)
    while gap_end - t >= MIN_WINDOW_S - _EPS:
        length = min(cadence_s, gap_end - t)
        if gap_end - (t + length) < sliver - _EPS:
            length = gap_end - t
        out.append(OutputWindow(cycle[cursor % len(cycle)], t, t + length))
        cursor += 1
        t += length
    return cursor


def lay_out(
    entries: Sequence[SequenceEntry],
    *,
    cadence_s: float,
    take_s: float,
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
) -> SequenceLayout:
    """The windows of ``entries`` on a kept take of ``take_s`` seconds, plus honest notes."""
    notes: list[dict] = []
    if not entries or take_s < MIN_WINDOW_S - _EPS:
        return SequenceLayout(windows=(), cadence_s=cadence_s)

    anchors: dict[int, float] = {}
    for entry in entries:
        if not entry.spoken_phrase:
            continue
        at = _anchor_time(entry, words, keep_segments)
        if at is None or at >= take_s - MIN_WINDOW_S + _EPS:
            notes.append(
                {"lane": "background", "window": entry.index, "reason": "phrase_not_heard"}
            )
        else:
            anchors[entry.index] = at
    by_index = {entry.index: entry for entry in entries}
    cycle = [e.index for e in entries if e.index not in anchors]

    current = cadence_s
    while True:
        windows: list[OutputWindow] = []
        placed: list[tuple[float, int]] = sorted((at, idx) for idx, at in anchors.items())
        occupied: list[OutputWindow] = []
        for position, (start, idx) in enumerate(placed):
            hold = by_index[idx].hold_s or current
            end = min(start + hold, take_s)
            if position + 1 < len(placed):
                end = min(end, placed[position + 1][0])
            if end - start < MIN_WINDOW_S - _EPS:
                notes.append({"lane": "background", "window": idx, "reason": "window_too_short"})
                continue
            occupied.append(OutputWindow(idx, start, end))
        cursor = 0
        t = 0.0
        for block in occupied:
            cursor = _fill_gap(t, block.start_s, cycle, cursor, current, windows)
            windows.append(block)
            t = block.end_s
        # The trailing gap runs to the take end (S39): the last window ends exactly there.
        _fill_gap(t, take_s, cycle, cursor, current, windows)
        if len(windows) <= MAX_WINDOWS or current >= take_s:
            break
        current = min(current * _STRETCH, take_s)
    if len(windows) > MAX_WINDOWS:
        windows = windows[:MAX_WINDOWS]
    if current != cadence_s:
        notes.append({"lane": "background", "reason": "cadence_stretched"})
    unique_notes = list({tuple(sorted(note.items())): note for note in notes}.values())
    return SequenceLayout(
        windows=tuple(windows),
        notes=tuple(unique_notes),
        cadence_s=current,
        anchored=frozenset(anchors),
    )
