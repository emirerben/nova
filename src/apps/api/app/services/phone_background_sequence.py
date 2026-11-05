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

Rules (the contract in ``docs/reviews/speaker-background-windows-failure-modes.md``). Above all
(S57-S62, decided 2026-11-05 after S4 "fb 1"): **the same photo never comes up twice.** Entries
carry their photo's identity (``media_key``) and ``lay_out`` gets the fill's (``fill_key``; None for
a plain colour): a photo listed twice keeps only the entry heard first (``duplicate_media``),
nothing ever cycles, and the fill never shows when it is one of the sequence's own photos -- the
window before a gap holds through it instead. A longer scene beats a repeat.

Rotation and mixed (an entry has no ``spoken_phrase``, or no phrase is heard at all):

* an entry with a ``spoken_phrase`` starts at the first loose-text match of it in the kept take
  and ends at the earliest of the next window's start and ``hold_s`` (else ``cadence_s``) later;
* the gaps are filled by the entries without an anchor, in order, ``cadence_s`` each, each once;
  when they run out the window before the gap holds through it (the first window starts at 0 s);
  a phrase that is never heard counts as unanchored and is reported as ``phrase_not_heard`` (S38);
* with no anchor at all every entry shows once, back to back from 0 s, each
  ``max(cadence_s, take / entries, take / MAX_WINDOWS)`` (``cadence_stretched`` when longer);
* windows are at least ``MIN_WINDOW_S`` long, never past the take end (S39), at most
  ``MAX_WINDOWS``; entries that no longer fit in the take get no window.

On the word (every entry has a ``spoken_phrase`` and one is heard, KRI-1175, S46-S56):

* the visual is on screen while it is talked about, in the order of the talk: a phrase never
  heard gets no window (``phrase_not_heard``); words heard within ``MIN_WINDOW_S`` of the
  previous one are one mention, whose entries follow each other from its first word;
* an entry stays ``hold_s`` (else ``cadence_s``), but the last entry before the next mention
  holds until it; its ``hold_s`` ends it early and the fill shows in between only when the fill
  is not one of the sequence's photos and the gap is not a sliver (under ``MIN_WINDOW_S``);
* entries that cannot all fit before the next mention (or the take end) share it equally; those
  that would get under ``MIN_WINDOW_S`` get no window (``window_too_short``);
* the first window opens the video (from 0 s); only a held first entry over a fill outside the
  sequence keeps that fill before it (a lead-in under ``MIN_WINDOW_S`` is absorbed either way);
* after the last mention the last visual holds to the take end (S51 reversed: no turns again).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

MIN_WINDOW_S = 0.5
MAX_WINDOWS = 40
_EPS = 1e-6


@dataclass(frozen=True)
class SequenceEntry:
    """One resolved sequence entry: its position in the strategy's list and what it asked for."""

    index: int
    spoken_phrase: str | None = None
    hold_s: float | None = None
    # The photo / video this entry shows (the strategy's media id): two entries with one key are
    # one photo, shown once (S59). None = unknown (never merged).
    media_key: str | None = None


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


def _fill_once(
    gap_start: float,
    gap_end: float,
    pending: list[int],
    cadence_s: float,
    out: list[OutputWindow],
) -> None:
    """Fill ``[gap_start, gap_end)`` with the ``pending`` entries (consumed), ``cadence_s`` each.

    Each entry is used once (S57). A leftover shorter than half a cadence joins the window before
    it rather than flashing a sliver of the next photo; when the entries run out the window before
    the gap holds through it, so a gap is never left to the fill here.
    """
    t = gap_start
    sliver = max(MIN_WINDOW_S, cadence_s / 2.0)
    while pending and gap_end - t >= MIN_WINDOW_S - _EPS:
        length = min(cadence_s, gap_end - t)
        if gap_end - (t + length) < sliver - _EPS:
            length = gap_end - t
        out.append(OutputWindow(pending.pop(0), t, t + length))
        t += length
    if gap_end - t > _EPS and out and out[-1].end_s >= gap_start - _EPS:
        out[-1] = OutputWindow(out[-1].index, out[-1].start_s, gap_end)


def _seat_mention(
    members: Sequence[SequenceEntry],
    start: float,
    end: float,
    *,
    cadence_s: float,
    more_follow: bool,
    fill_is_photo: bool,
    notes: list[dict],
) -> list[OutputWindow]:
    """Windows for the entries of one mention, back to back from ``start`` inside ``[start, end)``.

    ``end`` is the next mention (``more_follow``) or the take end. Each member stays ``hold_s``
    (else a cadence) while the span allows; the last one holds until ``end``. Members that do not
    fit share the span equally, and those that would get under ``MIN_WINDOW_S`` are dropped with a
    note. Only a held last member over a fill outside the sequence ends early (S60).
    """
    wants = [member.hold_s or cadence_s for member in members]
    span = end - start
    # Before another mention the last member needs room for a window; at the take end, its length
    # (S48: members that cannot all get theirs share the span; the last then holds to ``end``).
    needed = sum(wants[:-1]) + (MIN_WINDOW_S if more_follow else wants[-1])
    if needed > span + _EPS:
        count = len(members)
        if span / count < MIN_WINDOW_S - _EPS:
            count = max(1, int(span / MIN_WINDOW_S + 1e-9))
        for member in members[count:]:
            notes.append(
                {"lane": "background", "window": member.index, "reason": "window_too_short"}
            )
        share = span / count
        shared = [
            OutputWindow(member.index, start + position * share, start + (position + 1) * share)
            for position, member in enumerate(members[:count])
        ]
        shared[-1] = OutputWindow(shared[-1].index, shared[-1].start_s, end)
        return shared
    placed: list[OutputWindow] = []
    t = start
    for member, want in zip(members[:-1], wants[:-1], strict=True):
        placed.append(OutputWindow(member.index, t, t + want))
        t += want
    last = members[-1]
    stop = end
    if last.hold_s and not fill_is_photo and end - (t + last.hold_s) >= MIN_WINDOW_S - _EPS:
        # The creator's base (a colour, or a photo outside the sequence) shows after an explicit
        # hold; a fill that is one of the photos would be a repeat, so the hold gives way (S60).
        stop = t + last.hold_s
    placed.append(OutputWindow(last.index, t, stop))
    return placed


def _lay_out_on_the_word(
    entries: Sequence[SequenceEntry],
    anchors: dict[int, float],
    notes: list[dict],
    *,
    cadence_s: float,
    take_s: float,
    fill_is_photo: bool,
) -> SequenceLayout:
    """Every entry has a phrase and one is heard: each visual sits on the words that name it."""
    by_index = {entry.index: entry for entry in entries}
    mentions: list[list[tuple[float, int]]] = []
    for at, idx in sorted((at, idx) for idx, at in anchors.items()):
        # Words heard less than MIN_WINDOW_S after the previous one chain into the same mention.
        if mentions and at - mentions[-1][-1][0] <= MIN_WINDOW_S + _EPS:
            mentions[-1].append((at, idx))
        else:
            mentions.append([(at, idx)])
    windows: list[OutputWindow] = []
    for position, mention in enumerate(mentions):
        more_follow = position + 1 < len(mentions)
        windows.extend(
            _seat_mention(
                [by_index[idx] for _at, idx in mention],
                mention[0][0],
                mentions[position + 1][0][0] if more_follow else take_s,
                cadence_s=cadence_s,
                more_follow=more_follow,
                fill_is_photo=fill_is_photo,
                notes=notes,
            )
        )
    # Lead-in: the talk opens on its first visual. Only a held first window over a base outside
    # the sequence keeps that base before it, unless that would be a sliver (S50, S58).
    first = windows[0]
    keeps_base = bool(by_index[first.index].hold_s) and not fill_is_photo
    if not keeps_base or first.start_s < MIN_WINDOW_S - _EPS:
        windows[0] = OutputWindow(first.index, 0.0, first.end_s)
    # Tail: the last visual holds to the take end (S51 reversed: nothing takes turns again);
    # a held last window over a separate base keeps its asked length unless the rest is a sliver.
    last = windows[-1]
    tail_s = take_s - last.end_s
    if tail_s > _EPS:
        held = bool(by_index[last.index].hold_s) and not fill_is_photo
        if not held or tail_s < MIN_WINDOW_S - _EPS:
            windows[-1] = OutputWindow(last.index, last.start_s, take_s)
    windows = windows[:MAX_WINDOWS]
    unique_notes = list({tuple(sorted(note.items())): note for note in notes}.values())
    return SequenceLayout(
        windows=tuple(sorted(windows, key=lambda window: window.start_s)),
        notes=tuple(unique_notes),
        cadence_s=cadence_s,
        anchored=frozenset(anchors),
    )


def _one_window_per_photo(
    entries: Sequence[SequenceEntry], anchors: dict[int, float], notes: list[dict]
) -> list[SequenceEntry]:
    """S59: entries naming one photo (``media_key``) keep only the one heard first (else the first
    listed); the others get no window and a ``duplicate_media`` note."""
    kept: dict[str, SequenceEntry] = {}
    for entry in entries:
        key = entry.media_key
        if key is None:
            continue
        current = kept.get(key)
        if current is None:
            kept[key] = entry
            continue
        here, there = anchors.get(entry.index), anchors.get(current.index)
        if here is not None and (there is None or here < there):
            kept[key] = entry
    survivors = {entry.index for entry in kept.values()}
    out: list[SequenceEntry] = []
    for entry in entries:
        if entry.media_key is not None and entry.index not in survivors:
            notes.append({"lane": "background", "window": entry.index, "reason": "duplicate_media"})
            anchors.pop(entry.index, None)
            continue
        out.append(entry)
    return out


def lay_out(
    entries: Sequence[SequenceEntry],
    *,
    cadence_s: float,
    take_s: float,
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
    fill_key: str | None = None,
) -> SequenceLayout:
    """The windows of ``entries`` on a kept take of ``take_s`` seconds, plus honest notes.

    ``fill_key`` is the fill's photo identity (None for a plain colour): when it names one of the
    entries' photos the fill is never left to show in a gap, so that photo cannot come back (S57).
    """
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
    phrased_all = all(entry.spoken_phrase for entry in entries)
    entries = _one_window_per_photo(entries, anchors, notes)
    fill_is_photo = fill_key is not None and any(e.media_key == fill_key for e in entries)
    if anchors and phrased_all:
        return _lay_out_on_the_word(
            entries,
            anchors,
            notes,
            cadence_s=cadence_s,
            take_s=take_s,
            fill_is_photo=fill_is_photo,
        )
    by_index = {entry.index: entry for entry in entries}
    pending = [e.index for e in entries if e.index not in anchors]

    current = cadence_s
    if not anchors:
        # A rotation: every entry once, back to back, stretched to cover the take (S57).
        current = max(cadence_s, take_s / len(pending), take_s / MAX_WINDOWS)
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
    t = 0.0
    for block in occupied:
        _fill_once(t, block.start_s, pending, current, windows)
        if not windows and block.start_s > _EPS:
            # Nothing before the first anchored window: it opens the video (S58).
            block = OutputWindow(block.index, 0.0, block.end_s)
        windows.append(block)
        t = block.end_s
    # The trailing gap runs to the take end (S39): the last window ends exactly there.
    _fill_once(t, take_s, pending, current, windows)
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
