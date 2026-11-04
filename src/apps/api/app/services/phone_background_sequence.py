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

Rules (the contract in ``docs/reviews/speaker-background-windows-failure-modes.md``).
Rotation and mixed (an entry has no ``spoken_phrase``, or no phrase is heard at all):

* an entry with a ``spoken_phrase`` starts at the first loose-text match of it in the kept take
  and ends at the earliest of the next window's start and ``hold_s`` (else ``cadence_s``) later;
* the remaining time is filled by the entries without an anchor, in order, ``cadence_s`` each,
  cycling until the take ends (a phrase that is never heard counts as unanchored and is
  reported as ``phrase_not_heard``, S38);
* when every entry is anchored the gaps keep the fill background;
* windows are at least ``MIN_WINDOW_S`` long, never past the take end (S39), at most
  ``MAX_WINDOWS``; a sequence that would need more is stretched to a longer cadence.

On the word (every entry has a ``spoken_phrase`` and one is heard, KRI-1175, S46-S56):

* the visual is on screen while it is talked about, in the order of the talk: a phrase never
  heard gets no window (``phrase_not_heard``); words heard within ``MIN_WINDOW_S`` of the
  previous one are one mention, whose entries follow each other from its first word;
* an entry stays ``hold_s`` (else ``cadence_s``), but the last entry before the next mention
  holds until it; its ``hold_s`` ends it early and the fill shows in between, unless that gap
  would be a sliver (under ``MIN_WINDOW_S``), which the window absorbs;
* entries that cannot all fit before the next mention (or the take end) share it equally; those
  that would get under ``MIN_WINDOW_S`` get no window (``window_too_short``);
* the first window opens the video (from 0 s) unless it has ``hold_s``; a lead-in under
  ``MIN_WINDOW_S`` is absorbed either way, a longer one keeps the fill;
* once every visual was shown, those without ``hold_s`` take turns every ``cadence_s``, in the
  order they were first shown, to the take end (a leftover sliver joins the window before it
  unless that one has ``hold_s``; with no such visual the fill shows); the ``MAX_WINDOWS``
  stretch is that cadence only.

With ``BACKGROUND_FOLLOWS_TALK_ENABLED`` (item 17, S57-S66) the anchoring rules below apply to
EVERY phrased sequence, and the on-the-word tail follows the talk unless a rhythm was stated (a
stated rhythm keeps the tail rotation above exactly):

* a phrase neither the exact nor the KRI-961 matcher hears is aligned loosely to whole spoken
  words: a word may be split or joined across 2-3 words of 3+ letters ("TradingAgents" / "trading
  agents"), a final plural "s" is ignored, one word against one word is never loose, names under
  four letters never; a hearing inside ANOTHER entry's longer, different phrase ("agents" in
  "trading agents") is that entry's word, so the next free hearing anchors (a name only ever said
  inside another keeps its first hearing);
* with no stated rhythm (``keep_changing`` unset and ``cadence_s`` the unasked default) nothing
  cycles: the last window runs to the take end (its ``hold_s`` still ends it, the fill shows,
  unless the rest is a sliver).
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


def _strip_plural(text: str) -> str:
    """A final plural "s" dropped from a long word or run ("studios" -> "studio")."""
    if len(text) >= 5 and text.endswith("s") and not text.endswith(("ss", "us", "is")):
        return text[:-1]
    return text


_JOIN_MAX = 3  # a name split or joined across at most three words
_JOIN_MIN_LEN = 3  # every word of a joined run is a real word, not a stray letter or two


def _align_loose(phrase: list[str], spoken: list[str], a: int, b: int) -> int | None:
    """End (exclusive) in ``spoken`` of an alignment of ``phrase[a:]`` from ``spoken[b]``.

    Each step pairs one phrase word with one spoken word (equal modulo a final plural "s"), one
    phrase word with 2-3 spoken words joined, or 2-3 phrase words joined with one spoken word.
    """
    if a == len(phrase):
        return b
    if b >= len(spoken):
        return None
    steps = [(1, 1)]
    steps += [(1, k) for k in range(2, _JOIN_MAX + 1)] + [(k, 1) for k in range(2, _JOIN_MAX + 1)]
    for pn, sn in steps:
        p_run, s_run = phrase[a : a + pn], spoken[b : b + sn]
        if len(p_run) < pn or len(s_run) < sn:
            continue
        if pn > 1 and min(map(len, p_run)) < _JOIN_MIN_LEN:
            continue
        if sn > 1 and min(map(len, s_run)) < _JOIN_MIN_LEN:
            continue
        if _strip_plural("".join(p_run)) != _strip_plural("".join(s_run)):
            continue
        end = _align_loose(phrase, spoken, a + pn, b + sn)
        if end is not None:
            return end
    return None


def _loose_hearings(stream: Sequence, phrase: str) -> list[tuple[float, float]]:
    """Where whole spoken words say ``phrase`` split or joined differently, or with a final
    plural "s" ("TradingAgents" / "trading agents", "game studio" / "game studios")."""
    from app.services import phone_reaction_grounding as grounding  # noqa: PLC0415

    words = grounding.fold_tokens(phrase)
    if len("".join(_strip_plural(w) for w in words)) < 4:
        return []
    texts = [token.text for token in stream]
    hits: list[tuple[float, float]] = []
    for i in range(len(texts)):
        end = _align_loose(words, texts, 0, i)
        # One word against one word is the exact / KRI-961 matchers' job.
        if end is not None and (len(words) > 1 or end - i > 1):
            hits.append((stream[i].start_s, stream[end - 1].end_s))
    return hits


def _anchor_times_following_talk(
    entries: Sequence[SequenceEntry],
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
) -> dict[int, float | None]:
    """Output time of each phrased entry's word, flag on (S64-S66); ``None`` = not heard."""
    from app.services import phone_reaction_grounding as grounding  # noqa: PLC0415

    timed = [word for word in (words or []) if isinstance(word, dict)]
    stream = grounding._token_stream(timed)  # noqa: SLF001 - the tokenizer beats ground with
    hearings: dict[int, list[tuple[float, float]]] = {}
    folded: dict[int, tuple[str, ...]] = {}
    for entry in entries:
        if not entry.spoken_phrase or not timed:
            continue
        folded[entry.index] = tuple(grounding.fold_tokens(entry.spoken_phrase))
        found = [
            (float(a), float(b))
            for a, b, _heard in grounding._phrase_windows(  # noqa: SLF001
                stream, timed, entry.spoken_phrase
            )
        ]
        hearings[entry.index] = found or _loose_hearings(stream, entry.spoken_phrase)
    anchors: dict[int, float | None] = {}
    for entry in entries:
        if not entry.spoken_phrase:
            continue
        kept = [
            (start, end, to_output_time(start, keep_segments))
            for start, end in hearings.get(entry.index, [])
        ]
        kept = [(start, end, at) for start, end, at in kept if at is not None]
        free = [
            (at, start, end)
            for start, end, at in kept
            if not any(
                folded[other] != folded[entry.index]
                and o_start <= start + _EPS
                and end <= o_end + _EPS
                and o_end - o_start > end - start + _EPS
                for other, spans in hearings.items()
                for o_start, o_end in spans
            )
        ]
        # A name only ever said inside another name keeps its first hearing (as without the flag).
        anchors[entry.index] = free[0][0] if free else (kept[0][2] if kept else None)
    return anchors


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


def _seat_mention(
    members: Sequence[SequenceEntry],
    start: float,
    end: float,
    *,
    cadence_s: float,
    more_follow: bool,
    notes: list[dict],
) -> list[OutputWindow]:
    """Windows for the entries of one mention, back to back from ``start`` inside ``[start, end)``.

    ``end`` is the next mention (``more_follow``) or the take end. Each member stays ``hold_s``
    (else a cadence) while the span allows; before another mention the last one holds until it.
    Members that do not fit share the span equally, and those that would get under
    ``MIN_WINDOW_S`` are dropped with a note.
    """
    wants = [member.hold_s or cadence_s for member in members]
    span = end - start
    # Before another mention the last member needs room for a window; at the take end, its length.
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
    if not more_follow:
        stop = min(t + wants[-1], end)
    elif last.hold_s and end - (t + last.hold_s) >= MIN_WINDOW_S - _EPS:
        # The fill shows between an explicit hold and the next mention, unless that is a sliver.
        stop = t + last.hold_s
    else:
        stop = end
    placed.append(OutputWindow(last.index, t, stop))
    return placed


def _lay_out_on_the_word(
    entries: Sequence[SequenceEntry],
    anchors: dict[int, float],
    notes: list[dict],
    *,
    cadence_s: float,
    take_s: float,
    hold_last: bool = False,
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
    head: list[OutputWindow] = []
    for position, mention in enumerate(mentions):
        more_follow = position + 1 < len(mentions)
        head.extend(
            _seat_mention(
                [by_index[idx] for _at, idx in mention],
                mention[0][0],
                mentions[position + 1][0][0] if more_follow else take_s,
                cadence_s=cadence_s,
                more_follow=more_follow,
                notes=notes,
            )
        )
    # Lead-in: the talk opens on its first visual; a held first window keeps the fill before it
    # unless that would be a sliver.
    first = head[0]
    if not by_index[first.index].hold_s or first.start_s < MIN_WINDOW_S - _EPS:
        head[0] = OutputWindow(first.index, 0.0, first.end_s)

    if hold_last:
        # No rhythm was asked: the last visual talked about stays (its own hold_s still ends it,
        # unless the rest would be a sliver).
        last = head[-1]
        rest = take_s - last.end_s
        if rest > _EPS and not (by_index[last.index].hold_s and rest >= MIN_WINDOW_S - _EPS):
            head[-1] = OutputWindow(last.index, last.start_s, take_s)
        return SequenceLayout(
            windows=tuple(sorted(head[:MAX_WINDOWS], key=lambda window: window.start_s)),
            notes=tuple({tuple(sorted(n.items())): n for n in notes}.values()),
            cadence_s=cadence_s,
            anchored=frozenset(anchors),
        )

    # Tail: every mention is behind us, so the visuals shown without an explicit hold take turns
    # in the order they were first shown, starting after the one on screen.
    shown: list[int] = []
    for window in head:
        if window.index not in shown:
            shown.append(window.index)
    cycle = [idx for idx in shown if not by_index[idx].hold_s]
    cursor = 1 if cycle and cycle[0] == head[-1].index else 0
    tail_start = head[-1].end_s
    tail_s = take_s - tail_start
    current = cadence_s
    while True:
        windows = list(head)
        last = windows[-1]
        sliver = tail_s < max(MIN_WINDOW_S, current / 2.0) - _EPS
        # A held last window keeps its asked length unless the rest would flash under the minimum.
        held_last = bool(by_index[last.index].hold_s) and tail_s >= MIN_WINDOW_S - _EPS
        if cycle == [last.index] or (sliver and not held_last):
            windows[-1] = OutputWindow(last.index, last.start_s, take_s)
        elif not sliver:
            _fill_gap(tail_start, take_s, cycle, cursor, current, windows)
        # Only the tail cadence stretches; a tail with a single window cannot shrink further.
        if len(windows) <= MAX_WINDOWS or len(windows) - len(head) <= 1 or current >= tail_s:
            break
        current = min(current * _STRETCH, tail_s)
    if len(windows) > MAX_WINDOWS:
        windows = windows[:MAX_WINDOWS]
    if current != cadence_s:
        notes.append({"lane": "background", "reason": "cadence_stretched"})
    unique_notes = list({tuple(sorted(note.items())): note for note in notes}.values())
    return SequenceLayout(
        windows=tuple(sorted(windows, key=lambda window: window.start_s)),
        notes=tuple(unique_notes),
        cadence_s=current,
        anchored=frozenset(anchors),
    )


def lay_out(
    entries: Sequence[SequenceEntry],
    *,
    cadence_s: float,
    take_s: float,
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
    keep_changing: bool = False,
) -> SequenceLayout:
    """The windows of ``entries`` on a kept take of ``take_s`` seconds, plus honest notes."""
    from app.agents._schemas.reaction_beats import (  # noqa: PLC0415
        DEFAULT_BACKGROUND_CADENCE_S,
    )
    from app.config import settings  # noqa: PLC0415

    follows_talk = settings.background_follows_talk_enabled
    notes: list[dict] = []
    if not entries or take_s < MIN_WINDOW_S - _EPS:
        return SequenceLayout(windows=(), cadence_s=cadence_s)

    anchors: dict[int, float] = {}
    heard_at = _anchor_times_following_talk(entries, words, keep_segments) if follows_talk else {}
    for entry in entries:
        if not entry.spoken_phrase:
            continue
        at = (
            heard_at.get(entry.index) if follows_talk else _anchor_time(entry, words, keep_segments)
        )
        if at is None or at >= take_s - MIN_WINDOW_S + _EPS:
            notes.append(
                {"lane": "background", "window": entry.index, "reason": "phrase_not_heard"}
            )
        else:
            anchors[entry.index] = at
    if anchors and all(entry.spoken_phrase for entry in entries):
        stated_rhythm = keep_changing or abs(cadence_s - DEFAULT_BACKGROUND_CADENCE_S) > _EPS
        return _lay_out_on_the_word(
            entries,
            anchors,
            notes,
            cadence_s=cadence_s,
            take_s=take_s,
            hold_last=follows_talk and not stated_rhythm,
        )
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
