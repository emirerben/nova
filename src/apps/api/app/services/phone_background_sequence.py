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

With ``BACKGROUND_FOLLOWS_TALK_ENABLED`` (Qendresa item 17, S65-S68) a phrase is also heard when it
is said another way, for every phrased entry:

* a phrase neither the exact nor the KRI-961 matcher hears is aligned loosely to whole spoken
  words: a word may be split or joined across 2-3 words of 3+ letters ("TradingAgents" / "trading
  agents"), a final plural "s" is ignored, one word against one word is never loose, names under
  four letters never; a multi-word name also retries with its last word's plural toggled ("Claude
  code game studio" / "cloud code game studios");
* a hearing inside ANOTHER entry's longer, different phrase ("agents" in "trading agents") is that
  entry's word, so the next free hearing anchors (a name only ever said inside another keeps its
  first hearing).
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


def _plural_toggled_hearings(
    stream: Sequence, timed: list[dict], phrase: str
) -> list[tuple[float, float]]:
    """A sound-alike AND a plural together ("Claude code game studio" heard as "cloud code game
    studios"): the exact / KRI-961 matchers again with the last word's plural toggled. Phrases of
    two or more words only, so a single name ("Notion") never hears its plural."""
    from app.services import phone_reaction_grounding as grounding  # noqa: PLC0415

    words = grounding.fold_tokens(phrase)
    if len(words) < 2:
        return []
    last = words[-1]
    toggled = _strip_plural(last)
    if toggled == last:
        toggled = last + "s"
    return [
        (float(a), float(b))
        for a, b, _heard in grounding._phrase_windows(  # noqa: SLF001
            stream, timed, " ".join([*words[:-1], toggled])
        )
    ]


def _anchor_times_following_talk(
    entries: Sequence[SequenceEntry],
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
) -> dict[int, float | None]:
    """Output time of each phrased entry's word, flag on (S65-S68); ``None`` = not heard."""
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
        hearings[entry.index] = (
            found
            or _loose_hearings(stream, entry.spoken_phrase)
            or _plural_toggled_hearings(stream, timed, entry.spoken_phrase)
        )
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


def _clip_to_opening(
    windows: Sequence[OutputWindow], opening_s: float, notes: list[dict]
) -> list[OutputWindow]:
    """No window starts before the switch: clip it to start there, or drop it (with a note) when
    under ``MIN_WINDOW_S`` of it remains."""
    kept: list[OutputWindow] = []
    for window in windows:
        if window.start_s >= opening_s - _EPS:
            kept.append(window)
        elif window.end_s - opening_s < MIN_WINDOW_S - _EPS:
            notes.append(
                {
                    "lane": "background",
                    "window": window.index,
                    "reason": "inside_full_screen_opening",
                }
            )
        else:
            kept.append(OutputWindow(window.index, opening_s, window.end_s))
    return kept


def _lay_out_on_the_word(
    entries: Sequence[SequenceEntry],
    anchors: dict[int, float],
    notes: list[dict],
    *,
    cadence_s: float,
    take_s: float,
    fill_is_photo: bool,
    opening_s: float = 0.0,
) -> SequenceLayout:
    """Every entry has a phrase and one is heard: each visual sits on the words that name it.

    ``opening_s`` (Qendresa item 16) is where the full-screen opening ends: no window starts
    before it (0.0 = no opening, every output byte-identical).
    """
    by_index = {entry.index: entry for entry in entries}
    mentions: list[list[tuple[float, int]]] = []
    # A name heard during the full-screen opening is seated AT the switch (spoken order kept),
    # so it shares the span from there instead of vanishing (opening 0.0: plain (at, idx)).
    seated = sorted((max(at, opening_s), at, idx) for idx, at in anchors.items())
    for at, _heard, idx in seated:
        # Words heard less than MIN_WINDOW_S after the previous one chain into the same mention.
        if mentions and at - mentions[-1][-1][0] <= MIN_WINDOW_S + _EPS:
            mentions[-1].append((at, idx))
        else:
            mentions.append([(at, idx)])
    if opening_s > _EPS and mentions[0][0][0] <= opening_s + _EPS:
        # S83: the names seated at the switch get at least MIN_WINDOW_S each. Nothing comes back
        # later (S57), so a visual squeezed out here would never show: a mention heard sooner is
        # pushed back just enough, and so on down the chain (each at most that much late). Never
        # past the last half second of the take: there the honest `window_too_short` note stays.
        floor = opening_s + len(mentions[0]) * MIN_WINDOW_S
        for mention in mentions[1:]:
            if mention[0][0] >= floor - _EPS or floor > take_s - MIN_WINDOW_S + _EPS:
                break
            mention[:] = [(max(at, floor), idx) for at, idx in mention]
            floor = mention[0][0] + len(mention) * MIN_WINDOW_S
        # Where the push-back stops at the take's last half second, a later mention can still sit
        # before (or within half a second of) the one pushed ahead of it (Emir's review of #2111:
        # Miro pushed to 7.5 s, Slack heard at 7.2 s): a reversed window and an overlap, and the
        # phone lane then refuses the whole background. Such a mention joins the one before it,
        # so its names share that span and those without room get `window_too_short`. Unless the
        # tail saturates, every mention already starts that far after the one before (no change).
        kept = [mentions[0]]
        for mention in mentions[1:]:
            seat = kept[-1][0][0]
            if mention[0][0] < seat + MIN_WINDOW_S - _EPS:
                kept[-1].extend((seat, idx) for _at, idx in mention)
            else:
                kept.append(mention)
        mentions = kept
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
    if opening_s > _EPS:
        # Safety net: the seating above already starts every mention at the switch or later.
        windows = _clip_to_opening(windows, opening_s, notes)
        if not windows:
            return SequenceLayout(
                windows=(),
                notes=tuple({tuple(sorted(n.items())): n for n in notes}.values()),
                cadence_s=cadence_s,
                anchored=frozenset(anchors),
            )
    # Lead-in: the talk opens on its first visual. Only a held first window over a base outside
    # the sequence keeps that base before it, unless that would be a sliver (S50, S58). With a
    # full-screen opening the first visual opens at the switch instead of 0 s (S74).
    first = windows[0]
    keeps_base = bool(by_index[first.index].hold_s) and not fill_is_photo
    if not keeps_base or first.start_s - opening_s < MIN_WINDOW_S - _EPS:
        windows[0] = OutputWindow(first.index, opening_s, first.end_s)
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
    opening_s: float | None = None,
) -> SequenceLayout:
    """The windows of ``entries`` on a kept take of ``take_s`` seconds, plus honest notes.

    ``fill_key`` is the fill's photo identity (None for a plain colour): when it names one of the
    entries' photos the fill is never left to show in a gap, so that photo cannot come back (S57).

    ``opening_s`` (Qendresa item 16, S74): the end of the speaker's full-screen opening, in
    kept-take seconds. No window starts before it; ``None`` leaves every output unchanged.
    """
    opening = float(opening_s) if opening_s else 0.0
    notes: list[dict] = []
    if not entries or take_s < MIN_WINDOW_S - _EPS:
        return SequenceLayout(windows=(), cadence_s=cadence_s)

    from app.config import settings  # noqa: PLC0415

    follows_talk = settings.background_follows_talk_enabled
    heard_at = _anchor_times_following_talk(entries, words, keep_segments) if follows_talk else {}
    anchors: dict[int, float] = {}
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
            opening_s=opening,
        )
    if opening > _EPS:
        # An entry heard during the full-screen opening joins the rotation (list order) instead of
        # being dropped with the part of the take the opening covers.
        anchors = {idx: at for idx, at in anchors.items() if at >= opening - _EPS}
    by_index = {entry.index: entry for entry in entries}
    pending = [e.index for e in entries if e.index not in anchors]

    current = cadence_s
    if not anchors:
        # A rotation: every entry once, back to back, stretched to cover the take (S57) -- the
        # part after the full-screen opening when there is one.
        span = take_s - opening
        current = max(cadence_s, span / len(pending), span / MAX_WINDOWS)
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
    if opening > _EPS:
        occupied = _clip_to_opening(occupied, opening, notes)
    t = opening
    for block in occupied:
        _fill_once(t, block.start_s, pending, current, windows)
        if not windows and block.start_s > opening + _EPS:
            # Nothing before the first anchored window: it opens the video (S58), or the corner
            # part right after a full-screen opening.
            block = OutputWindow(block.index, opening, block.end_s)
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


# ------------------------------------------------------------------ the full-screen opening


def first_heard_at(
    entries: Sequence[SequenceEntry],
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
) -> float | None:
    """Earliest kept-take hearing of any phrased entry, with the same anchoring `lay_out` uses
    (the follows-the-talk matcher when ``BACKGROUND_FOLLOWS_TALK_ENABLED``), or ``None``."""
    from app.config import settings  # noqa: PLC0415

    phrased = [entry for entry in entries if entry.spoken_phrase]
    if not phrased or not words:
        return None
    if settings.background_follows_talk_enabled:
        found = [
            at
            for at in _anchor_times_following_talk(phrased, words, keep_segments).values()
            if at is not None
        ]
    else:
        found = [
            at
            for at in (_anchor_time(entry, words, keep_segments) for entry in phrased)
            if at is not None
        ]
    return min(found) if found else None


OPENING_MIN_S = 1.0  # at least this long on screen, and at least this much take left after it
OPENING_DEFAULT_S = 5.0
OPENING_FIRST_HEARD_RANGE_S = (2.0, 8.0)
OPENING_TITLE_MAX_S = 8.0
_OPENING_SNAP_S = 1.5
_OPENING_SENTENCE_SNAP_S = 1.0


def full_screen_opening_end(
    *,
    words: Sequence[dict] | None,
    keep_segments: Sequence[tuple[float, float]] | None,
    take_s: float,
    stated_s: float | None,
    first_heard_s: float | None,
    title_end_s: float | None,
) -> tuple[float | None, str | None]:
    """Where the speaker's full-screen opening ends, in KEPT-take seconds (S72, S73).

    Returns ``(x, None)`` or ``(None, "take_too_short")``. The target is the stated length, else
    the first recording's word when heard 2-8 s in, else 5 s, never before the hook title ends
    (when it ends within 8 s). It snaps to the middle of the gap between two kept words within
    1.5 s (preferring a sentence end within 1.0 s), else to the nearest word end, so the switch
    never falls inside a spoken word. At least 1 s of take must be on either side; a take too
    short to hold the opening through the title AND leave 1 s after it gets no opening.
    """
    lo, hi = OPENING_MIN_S, float(take_s) - OPENING_MIN_S
    if hi < lo - _EPS:
        return None, "take_too_short"
    if title_end_s is not None and title_end_s <= OPENING_TITLE_MAX_S:
        # The switch never lands before the hook title ends, and a real corner (1 s) still
        # follows it: a take too short for both gets no opening (S73, S75), never a switch
        # while the title is still on screen.
        lo = max(lo, float(title_end_s))
        if hi < lo - _EPS:
            return None, "take_too_short"
    if stated_s is not None:
        target = float(stated_s)
    elif first_heard_s is not None and (
        OPENING_FIRST_HEARD_RANGE_S[0] <= first_heard_s <= OPENING_FIRST_HEARD_RANGE_S[1]
    ):
        target = float(first_heard_s)
    else:
        target = OPENING_DEFAULT_S
    if title_end_s is not None and title_end_s <= OPENING_TITLE_MAX_S:
        target = max(target, float(title_end_s))
    target = min(max(target, lo), hi)

    kept: list[tuple[float, float, str]] = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        try:
            start, end = float(word.get("start_s")), float(word.get("end_s"))
        except (TypeError, ValueError):
            continue
        at = to_output_time(start, keep_segments)
        if at is not None:
            kept.append((at, at + max(0.0, end - start), str(word.get("text") or "")))
    kept.sort()
    if not kept:
        return target, None

    def _in_word(x: float) -> bool:
        return any(s + _EPS < x < e - _EPS for s, e, _t in kept)

    gaps: list[tuple[float, bool]] = []  # (time, follows a sentence end)
    for (_s, e, text), (s_next, _e, _t) in zip(kept, kept[1:], strict=False):
        if s_next >= e - _EPS:
            gaps.append(((e + s_next) / 2.0, text.rstrip("\"')").endswith((".", "!", "?"))))
    in_range = [(t, sentence) for t, sentence in gaps if lo - _EPS <= t <= hi + _EPS]
    sentence_hits = [
        t for t, sentence in in_range if sentence and abs(t - target) <= _OPENING_SENTENCE_SNAP_S
    ]
    if sentence_hits:
        return min(sentence_hits, key=lambda t: abs(t - target)), None
    near = [t for t, _s in in_range if abs(t - target) <= _OPENING_SNAP_S]
    if near:
        return min(near, key=lambda t: abs(t - target)), None
    ends = [e for _s, e, _t in kept if lo - _EPS <= e <= hi + _EPS and not _in_word(e)]
    if ends:
        return min(ends, key=lambda t: abs(t - target)), None
    if not _in_word(target):
        return target, None
    return None, "take_too_short"
