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
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
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

_EPS = 1e-6


@dataclass(frozen=True)
class FootageFit:
    """``steps`` tile the voiceover exactly as the input did.

    ``fill_step_ids``: the steps added to fill a clip's leftover time. ``moment_at_s``: for
    every step whose named moment was located, the second of the video the moment starts
    showing at (its step's start when the clip opens on it), or None when the part of the
    clip that plays does not reach it.
    """

    steps: list[NarratedPhoneStep]
    fill_step_ids: frozenset[str]
    moment_at_s: dict[str, float | None]


def _in_point(moment: tuple[float, float], clip_s: float, window_s: float) -> float:
    """Where the clip starts so the moment is on screen when the clip comes on.

    The moment's own start, but never so late that less than a shortest step of footage is
    left (a moment in the clip's last second still gets a real cut around it).
    """
    start = float(moment[0])
    if start <= MOMENT_LEAD_S:
        return 0.0
    latest = clip_s - _EOF_GUARD_S - min(window_s, MIN_FILL_S)
    return round(max(0.0, min(start, latest)), 3)


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
    footage: _Footage, gap_s: float, *, before: str, after: str | None, allow_replay: bool
) -> list[tuple[str, float, float]] | None:
    """Cuts ``(media, source start, length)`` that fill ``gap_s`` at normal speed, or None.

    The gap is split into equal cuts near ``TARGET_FILL_S``. A cut never comes from the clip
    just before it; the last one also avoids the clip that follows, when it can. Only footage
    the edit has not shown, unless ``allow_replay``: then, once none is long enough,
    everything may play again. None (and nothing marked shown) when the gap cannot be filled.
    """
    count = max(1, round(gap_s / TARGET_FILL_S))
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
    """
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
