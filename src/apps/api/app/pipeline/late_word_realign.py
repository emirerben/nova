"""Words whisper stamped into a pause, moved back onto the voice they belong to (KRI-1506).

Cusco DJI T2 (job 887f89d6, 2026-11-16, Turkish talking clip): the speaker says "Kilitli
sistemler", pauses about 1.9 s, then says "yıkılmayı zorlaştırıyor". whisper-1 stamped
"Kilitli sistemler" at 13.96-15.24 s, wholly inside that pause (silence 13.38-15.29 s), and
left the words' real voice (12.31-13.38 s) with no word on it. Everything that reads word times
then went wrong at once:

* the list label and the caption cue for those words started 1.6 s late, in the silence
  (KRI-1506);
* the pause looked like speech, so Clean up speech kept the take's longest pause (2.05 s in
  the output) while cutting shorter ones (KRI-1507);
* rule 2 proposed the wordless voice as an acoustic filler, and only the KRI-1017 voice guard
  kept the real words in the video.

The move is made for that one shape only, and only when two independent signals agree:

* **dead air**: a run of consecutive words each lies at least ``LATE_RUN_IN_SILENCE_FRAC``
  inside ONE silence span of at least ``TOKEN_SILENCE_MIN_S``, the same span for the whole
  run, and the word after the run is not stamped in that pause too (it overlaps the span by
  less than ``TOKEN_SILENCE_MIN_OVERLAP_S``), so the voice after the pause is that word's;
* **wordless voice right before it**: the island is the sound between the last real pause
  (at least ``LATE_RUN_MIN_LEFT_PAUSE_S``) and this one. No earlier word reaches into it
  (``LATE_RUN_EDGE_TOL_S`` of slack for a drifting end stamp), it is at least
  ``LATE_RUN_MIN_ISLAND_S`` long, its length is within ``LATE_RUN_LENGTH_RATIO`` of the run's
  stamped length, and the run's syllables fit it at ``SQUEEZE_MAX_SYLLABLES_PER_S`` or slower;
* **the audio itself agrees** (``voicing``, measured on decoded PCM by the KRI-1017 voicing
  detector, independent of both whisper and silencedetect): the island holds sustained voicing
  for at least ``LATE_RUN_MIN_VOICED_FRAC`` of its length, and the run's stamps in the pause
  hold at most ``LATE_RUN_MAX_STAMP_VOICED_S`` of it.

The run is then mapped linearly onto the island: text, order and relative lengths are kept,
its first word starts where the voice starts and its last word ends where the pause starts.
Anything else (a run split across two pauses, an island an earlier word reaches into, a word
stamped in the pause after the run, a run the island is too short or too long for, voicing
in the stamps) stays exactly as whisper stamped it. The mirror shape (words stamped into a
pause BEFORE their voice) has not been seen in a job and is left alone.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.silence_cut import (
    SQUEEZE_MAX_SYLLABLES_PER_S,
    TOKEN_SILENCE_MIN_OVERLAP_S,
    TOKEN_SILENCE_MIN_S,
    _normalize_silences,
    _syllables,
)

LATE_RUN_IN_SILENCE_FRAC = 0.9
# A silence shorter than this inside the island is a stop closure or a breath between words,
# not the pause that ends the previous sentence part.
LATE_RUN_MIN_LEFT_PAUSE_S = 0.25
LATE_RUN_EDGE_TOL_S = 0.1
LATE_RUN_MIN_ISLAND_S = 0.25
LATE_RUN_LENGTH_RATIO = 2.0
LATE_RUN_MIN_VOICED_FRAC = 0.3
LATE_RUN_MAX_STAMP_VOICED_S = 0.05
_EPS = 1e-9

# ``voicing(lo_s, hi_s)``: seconds of sustained voicing the decoded audio holds in the span.
Voicing = Callable[[float, float], float]


@dataclass(frozen=True)
class LateRun:
    """One run of words moved out of a pause onto the voice before it."""

    first: int  # index of the first moved word
    last: int  # index of the last moved word (inclusive)
    stamped: tuple[float, float]  # where whisper stamped the run
    island: tuple[float, float]  # the wordless voice the run moves onto
    pause: tuple[float, float]  # the silence span the run was stamped in

    def to_diagnostic(self) -> dict[str, Any]:
        """Content-safe receipt: counts and times, never the words themselves."""

        return {
            "words": self.last - self.first + 1,
            "stamped_s": [round(self.stamped[0], 3), round(self.stamped[1], 3)],
            "moved_to_s": [round(self.island[0], 3), round(self.island[1], 3)],
            "pause_s": [round(self.pause[0], 3), round(self.pause[1], 3)],
        }


def _overlap(lo: float, hi: float, span_lo: float, span_hi: float) -> float:
    return max(0.0, min(hi, span_hi) - max(lo, span_lo))


def _in_dead_air(start: float, end: float, span_lo: float, span_hi: float) -> bool:
    if end - start <= _EPS:
        # A zero-length stamp is a point: inside or not.
        return span_lo - _EPS <= start <= span_hi + _EPS
    return _overlap(start, end, span_lo, span_hi) >= LATE_RUN_IN_SILENCE_FRAC * (end - start) - _EPS


def find_late_runs(
    words: Sequence[tuple[str, float, float]],
    silence_spans: Sequence[tuple[float, float]],
    duration_s: float,
) -> list[LateRun]:
    """Runs whose geometry matches the late-stamped shape; the audio is not consulted.

    ``words`` are ``(text, start_s, end_s)`` sorted by start (the cleanup analysis order).
    """

    spans = _normalize_silences(silence_spans, duration_s)
    count = len(words)
    runs: list[LateRun] = []
    for pause_lo, pause_hi in spans:
        if pause_hi - pause_lo < TOKEN_SILENCE_MIN_S - _EPS:
            continue
        inside = [
            index
            for index, (_text, start, end) in enumerate(words)
            if _in_dead_air(start, end, pause_lo, pause_hi)
        ]
        if not inside:
            continue
        first, last = inside[0], inside[-1]
        if inside != list(range(first, last + 1)):
            continue  # a word with voice sits between them: not one run
        if last + 1 < count:
            _text, next_start, next_end = words[last + 1]
            if (
                _overlap(next_start, next_end, pause_lo, pause_hi)
                >= TOKEN_SILENCE_MIN_OVERLAP_S - _EPS
            ):
                continue  # the next word is in the pause too: the shift is not this shape
        earlier_pauses = [
            hi
            for lo, hi in spans
            if hi < pause_lo - _EPS and hi - lo >= LATE_RUN_MIN_LEFT_PAUSE_S - _EPS
        ]
        island_lo = earlier_pauses[-1] if earlier_pauses else 0.0
        if first > 0:
            earlier_end = max(end for _t, _s, end in words[:first])
            if earlier_end > island_lo + LATE_RUN_EDGE_TOL_S + _EPS:
                continue  # an earlier word reaches into the voice: it may be that word's
            # The run never starts before an earlier word ends, so the words keep their order
            # (retake spans index them) and every gate below judges what is left of the island.
            island_lo = max(island_lo, earlier_end)
        island_hi = pause_lo
        island_len = island_hi - island_lo
        if island_len < LATE_RUN_MIN_ISLAND_S - _EPS:
            continue
        run_lo = words[first][1]
        run_hi = max(end for _t, _s, end in words[first : last + 1])
        run_len = run_hi - run_lo
        if run_len <= _EPS:
            continue
        if (
            island_len > LATE_RUN_LENGTH_RATIO * run_len + _EPS
            or run_len > LATE_RUN_LENGTH_RATIO * island_len + _EPS
        ):
            continue
        syllables = sum(_syllables(text) for text, _s, _e in words[first : last + 1])
        if syllables / island_len > SQUEEZE_MAX_SYLLABLES_PER_S + _EPS:
            continue
        runs.append(
            LateRun(
                first=first,
                last=last,
                stamped=(run_lo, run_hi),
                island=(island_lo, island_hi),
                pause=(pause_lo, pause_hi),
            )
        )
    return runs


def realign_late_words(
    words: Sequence[tuple[str, float, float]],
    silence_spans: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    voicing: Voicing,
) -> tuple[list[tuple[float, float]], list[LateRun]]:
    """Every word's ``(start_s, end_s)`` after moving each confirmed late run, and the runs.

    A run moves only when ``voicing`` hears the island as voice and the stamps as silence.
    Words outside a moved run keep their times exactly.
    """

    times = [(start, end) for _text, start, end in words]
    moved: list[LateRun] = []
    for run in find_late_runs(words, silence_spans, duration_s):
        island_lo, island_hi = run.island
        island_len = island_hi - island_lo
        if voicing(island_lo, island_hi) < LATE_RUN_MIN_VOICED_FRAC * island_len - _EPS:
            continue
        stamps_lo = max(run.stamped[0], run.pause[0])
        stamps_hi = min(run.stamped[1], run.pause[1])
        if stamps_hi > stamps_lo and voicing(stamps_lo, stamps_hi) > (
            LATE_RUN_MAX_STAMP_VOICED_S + _EPS
        ):
            continue
        run_lo, run_hi = run.stamped
        scale = island_len / (run_hi - run_lo)
        for index in range(run.first, run.last + 1):
            start, end = times[index]
            new_start = min(island_hi, max(island_lo, island_lo + (start - run_lo) * scale))
            new_end = min(island_hi, max(new_start, island_lo + (end - run_lo) * scale))
            times[index] = (new_start, new_end)
        moved.append(run)
    return times, moved


def follow_late_runs(words: Sequence[Any], late_runs: Sequence[Any] | None) -> list[Any]:
    """Another pass's words of the same take, moved the way the analysis moved its own.

    A caption pass in another language (or a re-transcription in the heard language) runs
    whisper on the same take again, and it can stamp the same words into the same pause. The
    cleanup plan now cuts that pause, so `silence_cut.remap_words` would drop them from the
    captions. ``late_runs`` is the analysis's ``diagnostics["late_words"]``: pauses that the
    silence spans and the voicing detector proved wordless. A word lying in one of them (as
    `find_late_runs` reads "in dead air") is mapped onto that run's voice with the run's own
    linear map; every other word keeps its times, and no word starts before the one before it
    ends. Words are ``Word``-like objects or dicts with ``start_s``/``end_s``, in time order;
    the same kinds come back. Without runs the list comes back as given.
    """

    maps: list[tuple[float, float, float, float, float, float]] = []
    for run in late_runs or ():
        try:
            pause_lo, pause_hi = (float(v) for v in run["pause_s"])
            stamped_lo, stamped_hi = (float(v) for v in run["stamped_s"])
            island_lo, island_hi = (float(v) for v in run["moved_to_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamped_hi - stamped_lo > _EPS and island_hi - island_lo > _EPS:
            maps.append((pause_lo, pause_hi, stamped_lo, stamped_hi, island_lo, island_hi))
    if not maps:
        return list(words)

    def times(word: Any) -> tuple[float, float]:
        if isinstance(word, dict):
            return float(word["start_s"]), float(word["end_s"])
        return float(word.start_s), float(word.end_s)

    def with_times(word: Any, start: float, end: float) -> Any:
        if isinstance(word, dict):
            return {**word, "start_s": start, "end_s": end}
        if dataclasses.is_dataclass(word):
            return dataclasses.replace(word, start_s=start, end_s=end)
        return word.model_copy(update={"start_s": start, "end_s": end})

    out: list[Any] = []
    previous_end: float | None = None
    for word in words:
        start, end = times(word)
        new_start, new_end = start, end
        for pause_lo, pause_hi, stamped_lo, stamped_hi, island_lo, island_hi in maps:
            if not _in_dead_air(start, end, pause_lo, pause_hi):
                continue
            scale = (island_hi - island_lo) / (stamped_hi - stamped_lo)
            new_start = min(island_hi, max(island_lo, island_lo + (start - stamped_lo) * scale))
            new_end = min(island_hi, max(new_start, island_lo + (end - stamped_lo) * scale))
            if previous_end is not None and new_start < previous_end:
                new_start = previous_end
                new_end = max(new_end, new_start)
            break
        moved = (new_start, new_end) != (start, end)
        out.append(with_times(word, new_start, new_end) if moved else word)
        previous_end = new_end if previous_end is None else max(previous_end, new_end)
    return out


__all__ = [
    "LATE_RUN_EDGE_TOL_S",
    "LATE_RUN_IN_SILENCE_FRAC",
    "LATE_RUN_LENGTH_RATIO",
    "LATE_RUN_MAX_STAMP_VOICED_S",
    "LATE_RUN_MIN_ISLAND_S",
    "LATE_RUN_MIN_LEFT_PAUSE_S",
    "LATE_RUN_MIN_VOICED_FRAC",
    "LateRun",
    "Voicing",
    "find_late_runs",
    "follow_late_runs",
    "realign_late_words",
]
