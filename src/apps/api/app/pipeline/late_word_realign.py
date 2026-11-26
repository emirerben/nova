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
in the stamps) stays exactly as whisper stamped it. The mirror shape (whole words stamped into
a pause BEFORE their voice) has not been seen in a job and is left alone.

Two more shapes from the same T2 take (KRI-1512), behind ``straddle`` / their own step:

* **a straddling run** (``find_late_runs(straddle=True)``): "Ve" 37.00-37.60 s starts on the
  wordless voice (36.84-37.41 s) and runs into the pause, "4'te" 37.60-38.20 s lies wholly in
  it. The word that starts on the island and ends in the pause joins the run; every other gate
  is the same, except that the stamps may be up to ``LATE_RUN_STRADDLE_LENGTH_RATIO`` times
  the island (a straddling run is its voice stretched over its pause);
* **a stretched head** (``realign_stretched_heads``): whisper stretched the START of the word
  after a pause back over the pause ("1." 6.20-6.84 s over the silence 5.58-6.55 s, voice from
  6.565 s; "sertlikten" 9.50-10.82 s over 9.24-10.24 s, voice from 10.305 s), so the stamped
  gap stays under ``MAX_PAUSE_S`` and rule 3 never sees the pause. The head is trimmed to the
  silence end (and the previous word's stamped end inside the silence back to its start) only
  when the whole silence is voiceless and the word's voice starts right after it.

One more shape from Cusco T3 (KRI-1627), behind ``pairs``:

* **a stretched pair**: whisper stretched BOTH the tail of the word before a pause and the head of
  the word after it into the pause ("Dağları'nın" 41.46-42.76 s and "bayırlarında" 42.76-44.50 s
  over the silence 42.028-43.617 s; voice to 42.031 s and from 43.593 s), so the stamps meet in
  the pause and rule 3 sees no gap. KRI-1512 refuses it as "both edges in one pause"; rule 0
  must, having only silencedetect. With the voicing detector as the second witness it is not
  ambiguous: the head is trimmed as above and the tail goes back to the silence start, only when
  the previous word also passes the mirror of the head's gates (its voice ends right before the
  silence, the part before the silence is its voice and can hold it).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.silence_cut import (
    MAX_PAUSE_S,
    SENTENCE_HEAD_MIN_VOICED_S,
    SENTENCE_TAIL_MIN_VOICED_S,
    SENTENCE_TAIL_REACH_S,
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
# A straddling run's stamps cover its voice AND the pause whisper stretched it over (T2 "Ve
# 4'te": 1.20 s of stamps on a 0.57 s voice, 2.09x). The syllable gate still bounds the words.
LATE_RUN_STRADDLE_LENGTH_RATIO = 2.5
LATE_RUN_MIN_VOICED_FRAC = 0.3
LATE_RUN_MAX_STAMP_VOICED_S = 0.05
# A stretched head is trimmed only when the word's voice starts this soon after the silence:
# PAD_S plus the 80 ms the ambient spans keep clear of sound, as rule 0's sentence carves.
STRETCHED_HEAD_ONSET_REACH_S = SENTENCE_TAIL_REACH_S
# A straddling run moves only when the word after it starts by the pause end plus this reach, so
# the voice after the pause is that word's and cannot be the run's own (the mirror shape).
LATE_RUN_STRADDLE_NEXT_REACH_S = SENTENCE_TAIL_REACH_S
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
    straddle: bool = False  # the first word starts on the island and runs into the pause

    def to_diagnostic(self) -> dict[str, Any]:
        """Content-safe receipt: counts and times, never the words themselves."""

        receipt: dict[str, Any] = {
            "words": self.last - self.first + 1,
            "stamped_s": [round(self.stamped[0], 3), round(self.stamped[1], 3)],
            "moved_to_s": [round(self.island[0], 3), round(self.island[1], 3)],
            "pause_s": [round(self.pause[0], 3), round(self.pause[1], 3)],
        }
        if self.straddle:
            # Only then: #2374's receipts stay byte-identical.
            receipt["straddle"] = True
        return receipt


@dataclass(frozen=True)
class StretchedHead:
    """One word whose stamped head lay in the pause before its voice, trimmed to the pause end."""

    index: int  # the trimmed word
    stamped_start: float  # where whisper started it
    pause: tuple[float, float]  # the silence span its head lay in
    previous_end: float | None  # the previous word's stamped end when it lay in the pause
    # KRI-1627: the previous word's stamped tail lay in the pause too (a stretched pair).
    tail: bool = False

    def to_diagnostic(self) -> dict[str, Any]:
        """Content-safe receipt: times only."""

        receipt: dict[str, Any] = {
            "start_s": [round(self.stamped_start, 3), round(self.pause[1], 3)],
            "pause_s": [round(self.pause[0], 3), round(self.pause[1], 3)],
        }
        if self.previous_end is not None:
            receipt["previous_end_s"] = [round(self.previous_end, 3), round(self.pause[0], 3)]
        if self.tail:
            # Only then: KRI-1512's receipts stay byte-identical.
            receipt["tail"] = True
        return receipt


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
    *,
    straddle: bool = False,
) -> list[LateRun]:
    """Runs whose geometry matches the late-stamped shape; the audio is not consulted.

    ``words`` are ``(text, start_s, end_s)`` sorted by start (the cleanup analysis order).
    ``straddle`` lets the run's first word start on the island and run into the pause
    (KRI-1512); off, the geometry is exactly #2374's.
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
        straddles = False
        if straddle and first > 0 and last + 1 < count:
            _text, lead_start, lead_end = words[first - 1]
            # The word before the run starts on the island and reaches into the pause further
            # than a drifting end stamp: whisper stretched it over the pause too, so it is the
            # run's first word (T2 "Ve"). The word after the run must own the voice after the
            # pause, or that voice may be the run's own (the mirror shape, left alone).
            if (
                island_lo - LATE_RUN_EDGE_TOL_S - _EPS <= lead_start < pause_lo - _EPS
                and pause_lo + LATE_RUN_EDGE_TOL_S - _EPS <= lead_end <= pause_hi + _EPS
                and words[last + 1][1] <= pause_hi + LATE_RUN_STRADDLE_NEXT_REACH_S + _EPS
            ):
                first -= 1
                straddles = True
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
        ratio = LATE_RUN_STRADDLE_LENGTH_RATIO if straddles else LATE_RUN_LENGTH_RATIO
        if (
            island_len > LATE_RUN_LENGTH_RATIO * run_len + _EPS
            or run_len > ratio * island_len + _EPS
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
                straddle=straddles,
            )
        )
    return runs


def realign_late_words(
    words: Sequence[tuple[str, float, float]],
    silence_spans: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    voicing: Voicing,
    straddle: bool = False,
) -> tuple[list[tuple[float, float]], list[LateRun]]:
    """Every word's ``(start_s, end_s)`` after moving each confirmed late run, and the runs.

    A run moves only when ``voicing`` hears the island as voice and the stamps as silence.
    Words outside a moved run keep their times exactly.
    """

    times = [(start, end) for _text, start, end in words]
    moved: list[LateRun] = []
    for run in find_late_runs(words, silence_spans, duration_s, straddle=straddle):
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


def find_stretched_heads(
    words: Sequence[tuple[str, float, float]],
    silence_spans: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    pairs: bool = False,
) -> list[StretchedHead]:
    """Words whose stamped head lies in a pause rule 3 cannot see; the audio is not consulted.

    The shape (KRI-1512, failure modes in docs/reviews/kri-1512-stretched-words-failure-modes.md):
    exactly one long silence span overlaps the word by at least ``TOKEN_SILENCE_MIN_OVERLAP_S``
    and covers its start but not its end; what is left after the span can hold the word
    (``SENTENCE_HEAD_MIN_VOICED_S``, ``SQUEEZE_MAX_SYLLABLES_PER_S``); the stamped gap before it
    is under ``MAX_PAUSE_S`` (rule 3 is blind to the pause); the previous word starts before the
    span and reaches into it by under ``TOKEN_SILENCE_MIN_OVERLAP_S``; no other word is stamped
    in the span; and the word is not the first of the take.

    ``pairs`` (KRI-1627, docs/reviews/kri-1627-stretched-pairs-failure-modes.md): a previous word
    whose stamped tail reaches into the same span by ``TOKEN_SILENCE_MIN_OVERLAP_S`` or more is
    taken as a stretched pair (``tail=True``) instead of refused, when its stamped end is no later
    than the word's start, no other long span overlaps it, and the part before the span can hold
    it (``SENTENCE_TAIL_MIN_VOICED_S``, ``SQUEEZE_MAX_SYLLABLES_PER_S``). The audio decides in
    ``realign_stretched_heads``.
    """

    spans = [
        (lo, hi)
        for lo, hi in _normalize_silences(silence_spans, duration_s)
        if hi - lo >= TOKEN_SILENCE_MIN_S - _EPS
    ]
    heads: list[StretchedHead] = []
    for index, (text, start, end) in enumerate(words):
        if index == 0:
            continue
        overlapping = [
            (lo, hi)
            for lo, hi in spans
            if _overlap(start, end, lo, hi) >= TOKEN_SILENCE_MIN_OVERLAP_S - _EPS
        ]
        if len(overlapping) != 1:
            continue  # no long pause in the word, or several quiet patches (a mumbled word)
        pause_lo, pause_hi = overlapping[0]
        if not (pause_lo - _EPS <= start and pause_hi < end - _EPS):
            continue  # the pause is not at the word's head
        remainder = end - pause_hi
        if remainder < SENTENCE_HEAD_MIN_VOICED_S - _EPS:
            continue
        if _syllables(text) / remainder > SQUEEZE_MAX_SYLLABLES_PER_S + _EPS:
            continue
        _text, previous_start, previous_end = words[index - 1]
        if start - previous_end >= MAX_PAUSE_S - _EPS:
            continue  # rule 3 already sees this pause
        if previous_start >= pause_lo - _EPS:
            continue  # the previous word lies in the pause itself: another shape
        tail = _overlap(previous_start, previous_end, pause_lo, pause_hi) >= (
            TOKEN_SILENCE_MIN_OVERLAP_S - _EPS
        )
        if tail and not (pairs and _stretched_tail_fits(words[index - 1], start, spans, pause_lo)):
            continue  # both edges in one pause: ambiguous without the audio (rule 0's F6)
        if any(
            other not in (index - 1, index)
            and (
                _overlap(other_start, other_end, pause_lo, pause_hi) > _EPS
                or pause_lo + _EPS < other_start < pause_hi - _EPS
            )
            for other, (_t, other_start, other_end) in enumerate(words)
        ):
            continue  # another word is stamped in the pause
        heads.append(
            StretchedHead(
                index=index,
                stamped_start=start,
                pause=(pause_lo, pause_hi),
                previous_end=previous_end if previous_end > pause_lo + _EPS else None,
                tail=tail,
            )
        )
    return heads


def _stretched_tail_fits(
    previous: tuple[str, float, float],
    next_start: float,
    spans: Sequence[tuple[float, float]],
    pause_lo: float,
) -> bool:
    """The geometry half of a stretched pair's tail (KRI-1627 failure modes 5-7)."""

    text, start, end = previous
    if end > next_start + _EPS:
        return False  # overlapping stamps, not two stretched edges (fm7)
    long_spans = [
        (lo, hi)
        for lo, hi in spans
        if _overlap(start, end, lo, hi) >= TOKEN_SILENCE_MIN_OVERLAP_S - _EPS
    ]
    if len(long_spans) != 1:
        return False  # several quiet patches in the word: a mumbled word (fm6)
    remainder = pause_lo - start
    if remainder < SENTENCE_TAIL_MIN_VOICED_S - _EPS:
        return False  # the part before the pause cannot hold the word (fm5)
    return _syllables(text) / remainder <= SQUEEZE_MAX_SYLLABLES_PER_S + _EPS


def realign_stretched_heads(
    words: Sequence[tuple[str, float, float]],
    silence_spans: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    voicing: Voicing,
    pairs: bool = False,
) -> tuple[list[tuple[float, float]], list[StretchedHead]]:
    """Every word's ``(start_s, end_s)`` after trimming each confirmed stretched head.

    A head is trimmed only when ``voicing`` hears the whole pause as voiceless, hears the word's
    voice start within ``STRETCHED_HEAD_ONSET_REACH_S`` of the pause end, and hears the rest of
    the stamp as voice. The word then starts at the pause end; the previous word's stamped end
    inside the pause moves back to the pause start. Every other word keeps its times exactly.

    A stretched pair (``pairs``, KRI-1627) also needs the mirror for the previous word: its voice
    ends within ``STRETCHED_HEAD_ONSET_REACH_S`` before the pause start, and its stamp before the
    pause is voice. Either side failing moves neither word.
    """

    times = [(start, end) for _text, start, end in words]
    trimmed: list[StretchedHead] = []
    for head in find_stretched_heads(words, silence_spans, duration_s, pairs=pairs):
        pause_lo, pause_hi = head.pause
        _start, end = times[head.index]
        if voicing(pause_lo, pause_hi) > LATE_RUN_MAX_STAMP_VOICED_S + _EPS:
            continue  # voice in the pause: quiet speech the silence floor missed
        if voicing(pause_hi, min(end, pause_hi + STRETCHED_HEAD_ONSET_REACH_S)) <= _EPS:
            continue  # unexplained sound between the pause and the voice
        if voicing(pause_hi, end) < LATE_RUN_MIN_VOICED_FRAC * (end - pause_hi) - _EPS:
            continue  # the rest of the stamp is not the word's voice
        if head.tail:
            previous_start, _previous_end = times[head.index - 1]
            if voicing(max(previous_start, pause_lo - STRETCHED_HEAD_ONSET_REACH_S), pause_lo) <= (
                _EPS
            ):
                continue  # unexplained sound between the previous voice and the pause (fm3)
            if voicing(previous_start, pause_lo) < (
                LATE_RUN_MIN_VOICED_FRAC * (pause_lo - previous_start) - _EPS
            ):
                continue  # the previous stamp before the pause is not its voice (fm4)
        times[head.index] = (pause_hi, end)
        if head.previous_end is not None:
            previous_start, _previous_end = times[head.index - 1]
            times[head.index - 1] = (previous_start, max(previous_start, pause_lo))
        trimmed.append(head)
    return times, trimmed


def follow_late_runs(words: Sequence[Any], late_runs: Sequence[Any] | None) -> list[Any]:
    """Another pass's words of the same take, moved the way the analysis moved its own.

    A caption pass in another language (or a re-transcription in the heard language) runs
    whisper on the same take again, and it can stamp the same words into the same pause. The
    cleanup plan now cuts that pause, so `silence_cut.remap_words` would drop them from the
    captions. ``late_runs`` is the analysis's ``diagnostics["late_words"]``: pauses that the
    silence spans and the voicing detector proved wordless. A word lying in one of them (as
    `find_late_runs` reads "in dead air") is mapped onto that run's voice with the run's own
    linear map; so is, for a run recorded as a straddle, a word that starts after the run's
    stamped start and ends in the pause. Every other word keeps its times, and no word starts
    before the one before it ends. Words are ``Word``-like objects or dicts with
    ``start_s``/``end_s``, in time order; the same kinds come back. Without runs the list comes
    back as given.
    """

    maps: list[tuple[float, float, float, float, float, float, bool]] = []
    for run in late_runs or ():
        try:
            pause_lo, pause_hi = (float(v) for v in run["pause_s"])
            stamped_lo, stamped_hi = (float(v) for v in run["stamped_s"])
            island_lo, island_hi = (float(v) for v in run["moved_to_s"])
        except (KeyError, TypeError, ValueError):
            continue
        straddle = run.get("straddle") is True
        if stamped_hi - stamped_lo > _EPS and island_hi - island_lo > _EPS:
            maps.append(
                (pause_lo, pause_hi, stamped_lo, stamped_hi, island_lo, island_hi, straddle)
            )
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

    words = list(words)
    out: list[Any] = []
    previous_end: float | None = None
    for index, word in enumerate(words):
        start, end = times(word)
        new_start, new_end = start, end
        for pause_lo, pause_hi, stamped_lo, stamped_hi, island_lo, island_hi, straddle in maps:
            # As in `find_late_runs`: it reaches into the pause further than a drifting end (the
            # analysis's own moved words end at the pause start, which the receipt rounds), and
            # the pass has a word in the pause right after it.
            straddler = (
                straddle
                and stamped_lo - LATE_RUN_EDGE_TOL_S - _EPS <= start < pause_lo - _EPS
                and pause_lo + LATE_RUN_EDGE_TOL_S - _EPS <= end <= pause_hi + _EPS
                and index + 1 < len(words)
                and _in_dead_air(*times(words[index + 1]), pause_lo, pause_hi)
            )
            if not (straddler or _in_dead_air(start, end, pause_lo, pause_hi)):
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


def recorded_late_runs(snapshot: Any) -> list[Any]:
    """The moves a speech-cleanup snapshot's analysis recorded (``diagnostics["late_words"]``).

    Empty when there are none, or when the snapshot carries no diagnostics at all (an opening-cut
    snapshot built without the cleanup analysis): there is then nothing for another pass to
    follow."""

    analysis = getattr(snapshot, "analysis", None)
    diagnostics = getattr(analysis, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        return []
    runs = diagnostics.get("late_words")
    return list(runs) if isinstance(runs, list) else []


__all__ = [
    "LATE_RUN_EDGE_TOL_S",
    "LATE_RUN_IN_SILENCE_FRAC",
    "LATE_RUN_LENGTH_RATIO",
    "LATE_RUN_MAX_STAMP_VOICED_S",
    "LATE_RUN_MIN_ISLAND_S",
    "LATE_RUN_MIN_LEFT_PAUSE_S",
    "LATE_RUN_MIN_VOICED_FRAC",
    "LATE_RUN_STRADDLE_LENGTH_RATIO",
    "LATE_RUN_STRADDLE_NEXT_REACH_S",
    "STRETCHED_HEAD_ONSET_REACH_S",
    "LateRun",
    "StretchedHead",
    "Voicing",
    "find_late_runs",
    "find_stretched_heads",
    "follow_late_runs",
    "realign_late_words",
    "realign_stretched_heads",
    "recorded_late_runs",
]
