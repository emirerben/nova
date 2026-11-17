"""KRI-1466: fit a phone Voiceover edit's steps to the footage there is.

Each step shows one clip while the voice says its words. Two things went wrong on Kria TikTok
video 1 (job c8fcb7c0, 2026-11-14), "show ... the TOR on the big screen on "Tor"":

* The goal clip came on screen on the word, from its first frame. The big screen reads TOR
  2.76 s into the clip, so the creator saw an empty pitch on "Tor".
* It was the last clip, so it had to cover the remaining 15.4 s of voice with 7.7 s of
  footage, and the compiler slowed it to half speed (`_fit_step_window`). The TOR screen came
  5.6 s after the word. Three other clips had most of their footage unseen.

``fit_steps_to_footage`` changes the steps before they are compiled:

    step ──▶ a moment the creator named is located in its clip?
               yes: the clip starts there (``source_start_s``), so it is on screen on the word
                    (unless the clip must then be slowed more than before: see below)
             ──▶ enough footage from there for the whole step (or nearly: a mild slow-down
                 nobody sees, or a gap too short for a cut of its own)?
               yes: unchanged
               no:  the clip plays what it has at normal speed, and the rest of the step is
                    filled with cuts of footage the edit has not shown yet, each at normal
                    speed. Footage plays twice only when the creator asked for repeats; a gap
                    the unseen footage cannot fill is left as it was (slowed, as before)

Pure: steps, clip lengths and located moments in; steps out. No I/O, no settings, no model.
A step it cannot improve (one clip alone, a clip of unknown length, a clip nothing may follow)
is returned as it came, in-point included, so the compiler's slow-down makes the video it
always made; ``moment_at_s`` then says how late the named moment plays, for the receipt.

Tahini run (job 1d062f21, 2026-11-17, ``NARRATED_CUT_PACE_ENABLED``): the creator asked to
"change the picture about once a second; reuse a clip if needed" over a 33.1 s voiceover and
twelve ~5 s clips, and named three clips twice: in the opening list and on words at the end
("the fork bite on "grab a fork""). The edit had twelve shots (one per clip, 1.5-5.2 s), each
twice-named clip played only in the opening, the last 5.2 s showed one clip, and a 3.7 s clip
was slowed to 0.73 over a 5.04 s step because its 1.38 s gap was "no cut of its own". Three
optional asks change that, and with none of them the steps are the ones above, byte for byte:

* ``cut_s`` (a typed pace on the brief): every step longer than ``PACE_SPLIT_AFTER`` paces
  keeps its own clip for about one pace (longer while a word the creator named for it is
  said), and the rest of the step is cut into shots of about ``cut_s`` from footage not yet
  shown (replayed only when the creator allows repeats). Cutting to other footage is preferred
  to slowing a clip: a short gap left by a short clip becomes a shot of its own once it is at
  least ``pace_min_cut_s(cut_s)``.
* ``cues`` (`ClipCue`): a clip the creator named on a word that the step playing on that word
  does not show is shown again there, from the moment the creator named (when the clip's
  analysis located it and that part is unseen, or repeats are allowed), else from a part of it
  not yet shown. The step is split at the word (moved at most ``MOMENT_LEAD_S`` to give both
  shots their minimum); a cue that cannot be placed so is left out, and the review says so.
* ``floor_in_points``: an in-point is rounded down, not to the nearest millisecond, so a moment
  in a clip's last 1.5 s still leaves a whole shortest cut (Tahini "sticky": 3.4917 rounded
  to 3.492 left 1.49967 s and the fill was refused, so the named moment never played).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.pipeline.narrated_alignment import MIN_ALIGNED_STEP_S
from app.pipeline.phone_narrated_plan import _EOF_GUARD_S, NarratedPhoneStep
from app.services.editor_limits import EDITOR_MAX_TIMELINE_SLOTS
from app.services.named_moments import locate_in_clip

# A filler is a cut of its own: never shorter than an aligned step may be.
MIN_FILL_S = MIN_ALIGNED_STEP_S
# The length a filler aims for, and the longest one can come out (a gap of 4.5 s is one cut).
TARGET_FILL_S = 3.0
MAX_FILL_S = 1.5 * TARGET_FILL_S
# A clip slowed less than this is left alone: the eye does not read it as slow motion.
MIN_STRETCH_RATE = 0.8
# A moment this soon after the clip's first frame is on screen on the word already.
MOMENT_LEAD_S = 0.5

# Tahini run: a typed pace. The shortest cut it makes is this share of the pace, and never
# shorter than ``MIN_PACE_CUT_S`` (the montage cadence's own floor is 0.4 s).
MIN_PACE_CUT_S = 0.5
PACE_MIN_SHARE = 0.6
# A step longer than this many paces is cut into shots of about one pace.
PACE_SPLIT_AFTER = 1.5
# A clip shown again on its named word is a shot of its own: without a pace, never shorter.
MIN_CUE_S = 1.0

_EPS = 1e-6


def pace_min_cut_s(cut_s: float) -> float:
    """The shortest shot a pace of ``cut_s`` seconds cuts."""
    return max(MIN_PACE_CUT_S, PACE_MIN_SHARE * float(cut_s))


@dataclass(frozen=True)
class ClipCue:
    """A clip the creator named on a spoken word ("the fork bite on "grab a fork"").

    ``at_s`` / ``end_s``: when the first named word starts and the last one ends, in voiceover
    seconds. ``moment``: where the clip shows what the creator named (clip seconds), when its
    analysis located it; None otherwise.
    """

    cue_id: str
    media_id: str
    at_s: float
    end_s: float
    moment: tuple[float, float] | None = None


@dataclass(frozen=True)
class FootageFit:
    """``steps`` tile the voiceover exactly as the input did.

    ``fill_step_ids``: the steps added to fill a clip's leftover time. ``moment_at_s``: for
    every step whose named moment was located, the second of the video the moment starts
    showing at (its step's start when the clip opens on it), or None when the part of the
    clip that plays does not reach it. ``cue_step_ids`` (Tahini run): by cue id, the step added
    to show a named clip again on its word; such a step is the creator's named shot, never a
    filler. A cue the step on its word already showed, or that could not be placed, is absent.
    """

    steps: list[NarratedPhoneStep]
    fill_step_ids: frozenset[str]
    moment_at_s: dict[str, float | None]
    cue_step_ids: dict[str, str] = field(default_factory=dict)


def _in_point(
    moment: tuple[float, float], clip_s: float, window_s: float, *, floor: bool = False
) -> float:
    """Where the clip starts so the moment is on screen when the clip comes on.

    The moment's own start, but never so late that less than a shortest step of footage is
    left (a moment in the clip's last second still gets a real cut around it). ``floor``
    (Tahini run) rounds down, so that shortest step is never a hair short.
    """
    start = float(moment[0])
    if start <= MOMENT_LEAD_S:
        return 0.0
    latest = clip_s - _EOF_GUARD_S - min(window_s, MIN_FILL_S)
    point = max(0.0, min(start, latest))
    if floor:
        return math.floor(point * 1000 + 1e-6) / 1000
    return round(point, 3)


def _usable_s(step: NarratedPhoneStep, clip_s: float) -> float:
    """Footage the compiler will read from the step's in-point (`_fit_step_window`)."""
    return clip_s - step.source_start_s - _EOF_GUARD_S


def _moment_at(step: NarratedPhoneStep, clip_s: float, moment: tuple[float, float]) -> float | None:
    start, end = float(moment[0]), float(moment[1])
    window = step.end_s - step.start_s
    usable = _usable_s(step, clip_s)
    if usable <= 0 or end <= step.source_start_s + _EPS:
        return None
    rate = min(1.0, usable / window)
    at = step.start_s + max(0.0, start - step.source_start_s) / rate
    return round(at, 3) if at < step.end_s - _EPS else None


@dataclass(frozen=True)
class LocatedMoment:
    """A moment the creator named for a clip, where the clip's analysis recorded it."""

    words: str
    start_s: float
    end_s: float


def located_step_moments(
    steps: Sequence[NarratedPhoneStep],
    *,
    words_by_step: Mapping[str, Sequence[str]],
    analysis_by_step: Mapping[str, Mapping[str, Any]],
    durations: Mapping[str, float],
) -> dict[str, LocatedMoment]:
    """By step id, the one moment the creator's words for that step's clip name inside it.

    ``words_by_step`` are the creator's own words for the clip ("the TOR on the big screen");
    ``analysis_by_step`` is the clip's record (``best_moments`` / ``understanding.
    notable_moments``: structured times only, `named_moments.locate_in_clip`). Words that
    match no recorded moment, or two equally (a clip that is "the train" from start to end),
    locate nothing, and so do two of a clip's words that point at different moments: the
    clip then starts where it always did. Nothing is guessed.
    """
    out: dict[str, LocatedMoment] = {}
    for step in steps:
        clip_s = durations.get(step.media_id)
        analysis = analysis_by_step.get(step.step_id)
        if not clip_s or not analysis:
            continue
        found: dict[tuple[float, float], str] = {}
        for words in words_by_step.get(step.step_id, ()):
            grounding = locate_in_clip(words, analysis, float(clip_s))
            if grounding.status == "grounded" and grounding.found is not None:
                found.setdefault((grounding.found.start_s, grounding.found.end_s), words)
        if len(found) == 1:
            ((start, end), words) = next(iter(found.items()))
            out[step.step_id] = LocatedMoment(words=words, start_s=start, end_s=end)
    return out


class _Footage:
    """Which parts of each clip the edit has shown, and what is still free to show."""

    def __init__(self, durations: Mapping[str, float], order: list[str]) -> None:
        self._ends = {media: durations[media] - _EOF_GUARD_S for media in order}
        self._order = order
        self._shown: dict[str, list[tuple[float, float]]] = {media: [] for media in order}

    def show(self, media_id: str, start_s: float, end_s: float) -> None:
        if media_id in self._shown:
            self._shown[media_id].append((start_s, end_s))

    def unshow(self, media_id: str, start_s: float, end_s: float) -> None:
        if (start_s, end_s) in self._shown.get(media_id, ()):
            self._shown[media_id].remove((start_s, end_s))

    def unshow_cut(self, media_id: str, start_s: float, length_s: float) -> None:
        self.unshow(media_id, start_s, start_s + length_s)

    def snapshot(self) -> dict[str, list[tuple[float, float]]]:
        return {media: list(spans) for media, spans in self._shown.items()}

    def restore(self, seen: Mapping[str, list[tuple[float, float]]]) -> None:
        self._shown = {media: list(spans) for media, spans in seen.items()}

    def forget(self) -> None:
        """Everything has been seen: any footage may play again."""
        for spans in self._shown.values():
            spans.clear()

    def free_spans(self, media_id: str) -> list[tuple[float, float]]:
        """The parts of ``media_id`` the edit has not shown (empty for an unknown clip)."""
        return self._free(media_id) if media_id in self._shown else []

    def _free(self, media_id: str) -> list[tuple[float, float]]:
        free: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in sorted(self._shown[media_id]):
            if start - cursor > _EPS:
                free.append((cursor, start))
            cursor = max(cursor, end)
        if self._ends[media_id] - cursor > _EPS:
            free.append((cursor, self._ends[media_id]))
        return free

    def pick(self, length_s: float, avoid: Collection[str]) -> tuple[str, float] | None:
        """The clip and in-point of the next cut: the least-shown clip with room for it."""
        best: tuple[float, float, int, str, float] | None = None
        for index, media_id in enumerate(self._order):
            if media_id in avoid:
                continue
            shown = sum(end - start for start, end in self._shown[media_id])
            for start, end in self._free(media_id):
                if end - start + _EPS < length_s:
                    continue
                rank = (shown, -(end - start), index, media_id, start)
                if best is None or rank < best:
                    best = rank
        return (best[3], best[4]) if best is not None else None


def _fill(
    footage: _Footage,
    gap_s: float,
    *,
    before: str,
    after: str | None,
    allow_replay: bool,
    target_s: float = TARGET_FILL_S,
) -> list[tuple[str, float, float]] | None:
    """Cuts ``(media, source start, length)`` that fill ``gap_s`` at normal speed, or None.

    The gap is split into equal cuts near ``target_s`` (``TARGET_FILL_S``, or a typed pace). A
    cut never comes from the clip just before it; the last one also avoids the clip that
    follows, when it can. Only footage the edit has not shown, unless ``allow_replay``: then,
    once none is long enough, everything may play again. None (and nothing marked shown) when
    the gap cannot be filled.
    """
    count = max(1, round(gap_s / target_s))
    length = gap_s / count
    cuts: list[tuple[str, float, float]] = []
    previous = before
    seen = footage.snapshot()
    for index in range(count):
        soft = {previous, after} if after is not None and index == count - 1 else {previous}
        picked = footage.pick(length, soft) or footage.pick(length, {previous})
        if picked is None and allow_replay:
            footage.forget()
            picked = footage.pick(length, soft) or footage.pick(length, {previous})
        if picked is None:
            footage.restore(seen)
            return None
        media_id, start = picked
        footage.show(media_id, start, start + length)
        cuts.append((media_id, start, length))
        previous = media_id
    return cuts


def fit_steps_to_footage(
    steps: list[NarratedPhoneStep],
    *,
    durations: Mapping[str, float],
    moments: Mapping[str, tuple[float, float]] | None = None,
    keep_whole: Collection[str] = (),
    allow_replay: bool = False,
    max_steps: int = EDITOR_MAX_TIMELINE_SLOTS,
    cut_s: float | None = None,
    cues: Sequence[ClipCue] = (),
    floor_in_points: bool = False,
) -> FootageFit:
    """See the module docstring.

    ``durations``: each clip's own length by media id (a step whose clip is missing is left
    alone). ``moments``: by step id, the located moment the creator named for that step's
    clip, in the clip's own seconds. ``keep_whole``: step ids that keep their whole window
    (a clip the creator asked to end on: nothing may follow it). ``allow_replay``: the
    creator asked for footage to repeat (``video_reuse_policy: allow_repeat``), so a filler
    may show footage again once all of it has been seen; otherwise a gap the unseen footage
    cannot fill is left to the compiler's slow-down, as before. ``max_steps``: the most clips
    the editor's timeline holds; a fill that would pass it is not made.

    ``cut_s`` / ``cues`` / ``floor_in_points``: the Tahini-run asks (module docstring). With
    none of them the result is the one this function always returned.
    """
    if cut_s is not None or cues or floor_in_points:
        return _fit_with_asks(
            steps,
            durations=durations,
            moments=moments or {},
            keep_whole=keep_whole,
            allow_replay=allow_replay,
            max_steps=max_steps,
            cut_s=cut_s,
            cues=cues,
        )
    moments = moments or {}
    # Every step as it would start on its named moment; an in-point somebody already set (a
    # clip that plays on after the voice) stays.
    moved: list[NarratedPhoneStep] = []
    for step in steps:
        clip_s = durations.get(step.media_id)
        moment = moments.get(step.step_id)
        start = (
            _in_point(moment, float(clip_s), step.end_s - step.start_s)
            if clip_s and moment and step.source_start_s == 0
            else 0.0
        )
        moved.append(step.model_copy(update={"source_start_s": start}) if start > 0 else step)

    def played(step: NarratedPhoneStep) -> tuple[float, float]:
        length = min(step.end_s - step.start_s, _usable_s(step, float(durations[step.media_id])))
        return step.source_start_s, step.source_start_s + length

    order = list(dict.fromkeys(s.media_id for s in moved if durations.get(s.media_id)))
    footage = _Footage({media: float(durations[media]) for media in order}, order)
    for step in moved:
        if durations.get(step.media_id):
            footage.show(step.media_id, *played(step))

    out: list[NarratedPhoneStep] = []
    fill_ids: set[str] = set()
    for index, (original, step) in enumerate(zip(steps, moved, strict=True)):
        clip_s = durations.get(step.media_id)
        if not clip_s:
            out.append(step)
            continue
        window = step.end_s - step.start_s
        usable = _usable_s(step, float(clip_s))
        gap = window - usable
        if usable / window >= MIN_STRETCH_RATE:
            out.append(step)
            continue
        cuts = None
        if gap >= MIN_FILL_S and step.step_id not in keep_whole and usable >= MIN_FILL_S:
            following = moved[index + 1].media_id if index + 1 < len(moved) else None
            cuts = _fill(
                footage, gap, before=step.media_id, after=following, allow_replay=allow_replay
            )
        if cuts is not None and len(out) + 1 + len(cuts) + len(moved) - index - 1 > max_steps:
            for media_id, start, length in cuts:
                footage.unshow_cut(media_id, start, length)
            cuts = None
        if cuts is None:
            # Nothing may follow it, nothing else can, or the gap is no cut of its own: the
            # compiler slows it, as it always did. Starting later would only slow it more
            # (or slow a clip that fitted), so it starts where it came.
            if step is not original:
                footage.unshow(step.media_id, *played(step))
                footage.show(original.media_id, *played(original))
            out.append(original)
            continue
        cursor = round(step.start_s + usable, 3)
        out.append(step.model_copy(update={"end_s": cursor}))
        for number, (media_id, source_start, length) in enumerate(cuts, start=1):
            end = step.end_s if number == len(cuts) else round(cursor + length, 3)
            fill_id = f"{step.step_id}_fill{number}"
            out.append(
                NarratedPhoneStep(
                    step_id=fill_id,
                    media_id=media_id,
                    source_start_s=round(source_start, 3),
                    start_s=cursor,
                    end_s=end,
                )
            )
            fill_ids.add(fill_id)
            cursor = end

    by_id = {step.step_id: step for step in out}
    moment_at = {
        step_id: _moment_at(by_id[step_id], float(durations[by_id[step_id].media_id]), moment)
        for step_id, moment in moments.items()
        if step_id in by_id and durations.get(by_id[step_id].media_id)
    }
    return FootageFit(steps=out, fill_step_ids=frozenset(fill_ids), moment_at_s=moment_at)


# -- Tahini run: a typed pace, clips named on words, whole shortest cuts ------------------------


def _paced_keep(
    window: float, usable: float, hold: float, cut_s: float, min_cut: float
) -> float | None:
    """Seconds a step keeps its own clip before other footage takes over, or None to leave it.

    A step up to ``PACE_SPLIT_AFTER`` paces that its clip covers is a shot of the pace already.
    A longer one keeps its clip for one pace, or while the word the creator named for it is
    said (``hold``). A clip too short for its step keeps what it has, and the rest becomes a
    shot of its own once that is at least ``min_cut`` (cut, not slowed). None when no shot of
    at least ``min_cut`` can follow without cutting the named word short.
    """
    short_step = window <= PACE_SPLIT_AFTER * cut_s + _EPS
    if short_step and usable >= window - _EPS:
        return None
    keep = min(window if short_step else max(cut_s, hold), usable)
    if window - keep < min_cut - _EPS:
        if usable >= window - _EPS:
            return None  # the rest is no shot of its own, and the clip covers it
        keep = window - min_cut
    if keep < min_cut - _EPS or keep < hold - _EPS or keep > usable + _EPS:
        return None
    return keep


def _cue_window(
    footage: _Footage,
    cue: ClipCue,
    clip_s: float,
    length_s: float,
    min_s: float,
    allow_replay: bool,
) -> tuple[float, float] | None:
    """``(source start, footage it may play)`` for a named clip shown again on its word.

    The moment the creator named, when that part of the clip is unseen; else, with repeats
    allowed, the moment again; else the longest unseen part of the clip; else, with repeats
    allowed, the clip from its first frame. None when no unseen part is a shot of its own.
    """
    end = clip_s - _EOF_GUARD_S
    spans = footage.free_spans(cue.media_id)
    if cue.moment is not None:
        start = _in_point(cue.moment, clip_s, length_s, floor=True)
        need = min(length_s, end - start)
        for span_start, span_end in spans:
            if span_start - _EPS <= start and start + need <= span_end + _EPS:
                return start, span_end - start
        if allow_replay:
            return start, end - start
    roomy = [span for span in spans if span[1] - span[0] >= min_s - _EPS]
    if roomy:
        span_start, span_end = max(roomy, key=lambda span: (span[1] - span[0], -span[0]))
        return span_start, span_end - span_start
    if allow_replay:
        return 0.0, end
    return None


def _fit_with_asks(
    steps: list[NarratedPhoneStep],
    *,
    durations: Mapping[str, float],
    moments: Mapping[str, tuple[float, float]],
    keep_whole: Collection[str],
    allow_replay: bool,
    max_steps: int,
    cut_s: float | None,
    cues: Sequence[ClipCue],
) -> FootageFit:
    """`fit_steps_to_footage` with the Tahini-run asks (module docstring)."""
    pace = float(cut_s) if cut_s else None
    min_cut = pace_min_cut_s(pace) if pace else MIN_CUE_S
    limits: dict[str, float] = {}  # step id -> the most footage it may play (a cue's window)

    def usable_of(step: NarratedPhoneStep) -> float:
        usable = _usable_s(step, float(durations[step.media_id]))
        return min(usable, limits.get(step.step_id, usable))

    def played(step: NarratedPhoneStep) -> tuple[float, float]:
        length = min(step.end_s - step.start_s, usable_of(step))
        return step.source_start_s, step.source_start_s + length

    # (the step as it came, the step on its named moment); an in-point already set stays.
    entries: list[tuple[NarratedPhoneStep, NarratedPhoneStep]] = []
    for step in steps:
        clip_s = durations.get(step.media_id)
        moment = moments.get(step.step_id)
        start = (
            _in_point(moment, float(clip_s), step.end_s - step.start_s, floor=True)
            if clip_s and moment and step.source_start_s == 0
            else 0.0
        )
        moved = step.model_copy(update={"source_start_s": start}) if start > 0 else step
        entries.append((step, moved))

    order = list(
        dict.fromkeys(
            media
            for media in [*(s.media_id for _o, s in entries), *(c.media_id for c in cues)]
            if durations.get(media)
        )
    )
    footage = _Footage({media: float(durations[media]) for media in order}, order)
    for _original, step in entries:
        if durations.get(step.media_id):
            footage.show(step.media_id, *played(step))

    # A clip named on a word, shown again there when the step on the word shows another.
    cue_steps: dict[str, str] = {}
    cue_moments: dict[str, tuple[float, float]] = {}
    numbers: dict[str, int] = {}
    for cue in sorted(cues, key=lambda c: (c.at_s, c.cue_id)):
        clip_s = durations.get(cue.media_id)
        index = next(
            (i for i, (_o, s) in enumerate(entries) if s.start_s <= cue.at_s < s.end_s), None
        )
        if not clip_s or index is None or len(entries) + 1 > max_steps:
            continue
        original, step = entries[index]
        if step.media_id == cue.media_id or step.step_id in keep_whole:
            continue
        # The split comes on the word, moved at most MOMENT_LEAD_S (never past the named
        # words) so that both shots keep their minimum.
        lowest = max(step.start_s + min_cut, cue.at_s - MOMENT_LEAD_S)
        highest = min(step.end_s - min_cut, cue.at_s + MOMENT_LEAD_S, max(cue.end_s, cue.at_s))
        if lowest > highest + _EPS:
            continue
        split = round(min(max(cue.at_s, lowest), highest), 3)
        window = _cue_window(footage, cue, float(clip_s), step.end_s - split, min_cut, allow_replay)
        if window is None:
            continue
        source_start, room = window
        base = step.step_id.split("_cue")[0]
        numbers[base] = numbers.get(base, 0) + 1
        step_id = f"{base}_cue{numbers[base]}"
        shown = NarratedPhoneStep(
            step_id=step_id,
            media_id=cue.media_id,
            source_start_s=source_start,
            start_s=split,
            end_s=step.end_s,
        )
        if durations.get(step.media_id):
            footage.unshow(step.media_id, *played(step))
        head = step.model_copy(update={"end_s": split})
        entries[index] = (original.model_copy(update={"end_s": split}), head)
        limits[step_id] = room
        entries.insert(index + 1, (shown, shown))
        if durations.get(step.media_id):
            footage.show(step.media_id, *played(head))
        footage.show(cue.media_id, *played(shown))
        cue_steps[cue.cue_id] = step_id
        if cue.moment is not None:
            cue_moments[step_id] = cue.moment

    # How long a step keeps its clip at least: while the words the creator named it on are said.
    holds: dict[str, float] = {}
    for _original, step in entries:
        for cue in cues:
            placed_here = cue_steps.get(cue.cue_id) == step.step_id
            if cue.media_id != step.media_id or not (
                placed_here or step.start_s <= cue.at_s < step.end_s
            ):
                continue
            held = min(step.end_s, max(cue.end_s, cue.at_s)) - step.start_s
            holds[step.step_id] = max(holds.get(step.step_id, 0.0), held)

    out: list[NarratedPhoneStep] = []
    fill_ids: set[str] = set()
    cue_ids = set(cue_steps.values())
    for index, (original, step) in enumerate(entries):
        clip_s = durations.get(step.media_id)
        if not clip_s:
            out.append(step)
            continue
        window = step.end_s - step.start_s
        usable = usable_of(step)
        whole = step.step_id in keep_whole
        keep: float | None
        if pace is not None:
            keep = (
                None
                if whole
                else _paced_keep(window, usable, holds.get(step.step_id, 0.0), pace, min_cut)
            )
            target = pace
        else:
            gap = window - usable
            keep = (
                usable
                if usable / window < MIN_STRETCH_RATE
                and gap >= MIN_FILL_S
                and not whole
                and usable >= MIN_FILL_S - _EPS
                else None
            )
            target = TARGET_FILL_S
        cuts = None
        if keep is not None:
            following = entries[index + 1][1].media_id if index + 1 < len(entries) else None
            footage.unshow(step.media_id, *played(step))
            footage.show(step.media_id, step.source_start_s, step.source_start_s + keep)
            cuts = _fill(
                footage,
                window - keep,
                before=step.media_id,
                after=following,
                allow_replay=allow_replay,
                target_s=target,
            )
            if cuts is not None and len(out) + 1 + len(cuts) + len(entries) - index - 1 > max_steps:
                for media_id, start, length in cuts:
                    footage.unshow_cut(media_id, start, length)
                cuts = None
            if cuts is None:
                footage.unshow(step.media_id, step.source_start_s, step.source_start_s + keep)
                footage.show(step.media_id, *played(step))
        if cuts is None:
            if usable / window >= MIN_STRETCH_RATE or step.step_id in cue_ids:
                out.append(step)
                continue
            # Slowed as it always was; starting later would only slow it more.
            if step is not original:
                footage.unshow(step.media_id, *played(step))
                footage.show(original.media_id, *played(original))
            out.append(original)
            continue
        cursor = round(step.start_s + keep, 3)
        out.append(step.model_copy(update={"end_s": cursor}))
        for number, (media_id, source_start, length) in enumerate(cuts, start=1):
            end = step.end_s if number == len(cuts) else round(cursor + length, 3)
            fill_id = f"{step.step_id}_fill{number}"
            out.append(
                NarratedPhoneStep(
                    step_id=fill_id,
                    media_id=media_id,
                    source_start_s=round(source_start, 3),
                    start_s=cursor,
                    end_s=end,
                )
            )
            fill_ids.add(fill_id)
            cursor = end

    by_id = {step.step_id: step for step in out}
    located = {**moments, **cue_moments}
    moment_at = {
        step_id: _moment_at(by_id[step_id], float(durations[by_id[step_id].media_id]), moment)
        for step_id, moment in located.items()
        if step_id in by_id and durations.get(by_id[step_id].media_id)
    }
    return FootageFit(
        steps=out,
        fill_step_ids=frozenset(fill_ids),
        moment_at_s=moment_at,
        cue_step_ids={cue_id: sid for cue_id, sid in cue_steps.items() if sid in by_id},
    )
