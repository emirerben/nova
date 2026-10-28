"""Golden: a filler token whisper laid over a real word is not a filler (KRI-970).

Don Julio lunch kit, the waiter's "It's the provolone cheese", cut twice in prod:

* T1 (job 81ce940b, 8.2 s clip): whisper-1 stamped ``uh,`` 3.02-3.48 over "the provo-"
  and squeezed ``provolone`` into 3.58-3.76 (0.18 s for four syllables). Rule 1 cut
  3.02-3.58; the output says "It's ...n cheese" under a caption reading "provolone".
* T3 (job 0d8d644f, the raw 48 s take): ``uh,`` 5.12-5.44, ``provolone`` 5.44-5.72
  (0.28 s); the cut 5.12-5.44 is the middle of the word ("It's poulon cheese").

No "uh" is said in either take. The signature both share: a lexicon filler sitting next
to a several-syllable word whisper gave far less time than anyone can say it in.

How this could fail (written before the code):
- the squeezed word sits BEFORE the filler, not after it;
- a small stamped gap separates the filler from the squeezed word (T1: 0.10 s);
- rule 0 (V2 token reconciliation) moves the timings, so the guard judges other words
  than the ones rule 1 cuts;
- the refused filler is still removed by another rule: an acoustic island or a pause trim
  that treats it as a removable filler boundary;
- the baseline (V1) plan still cuts it, and a fallback to the baseline ships the cut;
- a cached analysis from before the fix is reused (detector version not bumped);
- punctuation or capitals on the neighbour ("Provolone,") defeat the syllable count;
- a REAL "uh" stops being cut: next to a short one-syllable word whisper stamps tight
  ("we" 0.08 s), next to a normally timed long word, at the start or end of the clip,
  next to another filler, in Turkish;
- a far-away squeezed word (across a pause) blocks a real filler it has nothing to do with.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.silence_cut import (
    REASON_FILLER_LEXICAL,
    build_cut_plan,
    build_cut_plan_comparison,
)
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION
from tests._silent_pcm import silent_pcm

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_TAKES = json.loads((_FIXTURES / "kri970_provolone_takes.json").read_text())["takes"]
_MEROPE = json.loads((_FIXTURES / "kri790_merope_t1_take.json").read_text())


def w(text: str, start: float, end: float) -> dict:
    return {"text": text, "start_s": start, "end_s": end}


def _cut_over(plan, lo: float, hi: float) -> list[tuple[float, float, str]]:
    return [
        (round(r.start_s, 3), round(r.end_s, 3), r.reason)
        for r in plan.removed
        if r.start_s < hi - 1e-6 and r.end_s > lo + 1e-6
    ]


def _lexical(plan) -> list[tuple[float, float]]:
    return [
        (round(r.start_s, 3), round(r.end_s, 3))
        for r in plan.removed
        if r.reason == REASON_FILLER_LEXICAL
    ]


def _take(name: str):
    take = _TAKES[name]
    silences = [tuple(span) for span in take["silences"]]
    return take, take["words"], silences, take["duration_s"]


@pytest.mark.parametrize("name", ["T1", "T3"])
def test_prod_cut_the_word_on_this_exact_input(name):
    take = _TAKES[name]
    lo, hi = take["spoken_provolone_s"]
    assert any(
        row["reason"] == REASON_FILLER_LEXICAL and row["start_s"] < hi and row["end_s"] > lo
        for row in take["prod_removed"]
    )


@pytest.mark.parametrize("name", ["T1", "T3"])
def test_neither_plan_cuts_into_provolone(name):
    take, words, silences, duration = _take(name)
    lo, hi = take["spoken_provolone_s"]
    comparison = build_cut_plan_comparison(
        words, silences, duration, over_budget_policy="clamp", max_removal_frac_required=1.0
    )
    assert comparison.candidate is not None
    for plan in (comparison.baseline, comparison.candidate):
        assert _cut_over(plan, lo, hi) == []


@pytest.mark.parametrize("name", ["T1", "T3"])
def test_the_cleanup_analysis_end_to_end_keeps_provolone(name):
    take, words, silences, duration = _take(name)
    lo, hi = take["spoken_provolone_s"]
    transcript = SimpleNamespace(
        words=[SimpleNamespace(**word) for word in words], language="en", low_confidence=False
    )
    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=f"kri970-{name}",
            local_media_path="/dev/null",
            duration_s=duration,
            mixed_gap_mode="apply",
        ),
        transcribe_fn=lambda *_args, **_kwargs: transcript,
        silence_detect_fn=lambda *_args, **_kwargs: SimpleNamespace(
            spans=tuple(silences), status="ok"
        ),
        pcm_loader=silent_pcm,
    )
    assert _cut_over(result.cut_plan.to_cut_plan(), lo, hi) == []
    assert not [f for f in result.findings if f.start_s < hi and f.end_s > lo]


def test_t1_still_tightens_its_real_pause():
    _take_row, words, silences, duration = _take("T1")
    plan = build_cut_plan_comparison(
        words, silences, duration, over_budget_policy="clamp"
    ).candidate
    assert [(round(r.start_s, 3), round(r.end_s, 3), r.reason) for r in plan.removed] == [
        (2.13, 2.355, "silence")
    ]


def test_detector_version_was_bumped_for_the_guard():
    # Preflight snapshot reuse keys on this string: an un-bumped change keeps serving the
    # analyses that cut "provolone".
    assert DETECTOR_VERSION != "mixed-gap-v5-retakes"


# --- Real fillers keep being cut -----------------------------------------------------


def test_every_real_filler_in_the_merope_take_is_still_cut():
    """KRI-790 Merope T1 (prod transcript): "Um" 0.05, "Um" 2.21, "Uh" 5.09, "um" 8.80.

    "Uh" sits between "everyone." (0.62 s) and "we" (0.08 s, one syllable): a tight
    one-syllable stamp is ordinary whisper, never evidence of a squeezed word.
    """
    words = [row for row in _MEROPE["words"] if row["end_s"] <= 12.0]
    plan = build_cut_plan(words, [], 12.0)
    starts = sorted(lo for lo, _hi in _lexical(plan))
    fillers = [row for row in words if row["text"].strip(",.").lower() in ("um", "uh")]
    assert len(fillers) == 4
    for row in fillers:
        assert any(
            lo <= row["start_s"] + 1e-6 and hi >= row["end_s"] - 1e-6 for lo, hi in _lexical(plan)
        ), (row, starts)


def _with(filler: dict, before: dict | None, after: dict | None) -> list[dict]:
    words = [w("Okay", 0.5, 0.8), w("so", 0.9, 1.1)]
    words.extend(row for row in (before, filler, after) if row is not None)
    words.extend([w("and", 6.0, 6.2), w("done.", 6.3, 6.7)])
    return words


def _filler_cut(words: list[dict], filler: dict) -> bool:
    plan = build_cut_plan(words, [], 8.0)
    return any(
        lo <= filler["start_s"] + 1e-6 and hi >= filler["end_s"] - 1e-6 for lo, hi in _lexical(plan)
    )


def test_a_real_uh_between_normally_timed_words_is_cut():
    filler = w("uh,", 3.0, 3.4)
    words = _with(filler, w("restaurant", 2.4, 2.95), w("tomorrow", 3.45, 3.95))
    assert _filler_cut(words, filler)


def test_a_squeezed_word_after_the_filler_refuses_the_cut():
    filler = w("uh,", 3.0, 3.4)
    words = _with(filler, w("It's,", 2.8, 3.0), w("provolone", 3.4, 3.6))
    assert not _filler_cut(words, filler)


def test_a_squeezed_word_before_the_filler_refuses_the_cut():
    filler = w("um", 3.0, 3.45)
    words = _with(filler, w("Provolone,", 2.85, 3.0), w("cheese.", 3.5, 3.9))
    assert not _filler_cut(words, filler)


def test_a_zero_length_long_word_next_to_the_filler_refuses_the_cut():
    filler = w("uh", 3.0, 3.5)
    # "steak" is one vowel group: a zero-length short word is ordinary whisper output.
    # (The count is vowel groups, so "grilled" reads as two: it errs toward keeping.)
    assert _filler_cut(_with(filler, w("It's", 2.8, 3.0), w("steak", 3.5, 3.5)), filler)
    assert not _filler_cut(_with(filler, w("It's", 2.8, 3.0), w("provolone", 3.5, 3.5)), filler)


def test_a_squeezed_word_across_a_pause_does_not_block_a_real_filler():
    filler = w("uh,", 3.0, 3.4)
    words = _with(filler, w("so,", 2.6, 2.8), w("provolone", 4.2, 4.3))
    assert _filler_cut(words, filler)


def test_turkish_filler_next_to_a_squeezed_turkish_word_is_kept():
    filler = w("ııı,", 3.0, 3.4)
    words = _with(filler, w("Bu", 2.7, 2.9), w("güzelmiş", 3.4, 3.55))
    assert not _filler_cut(words, filler)
    normal = _with(filler, w("Bu", 2.7, 2.9), w("güzelmiş", 3.45, 3.95))
    assert _filler_cut(normal, filler)


def test_a_filler_at_the_clip_edges_or_beside_another_filler_is_still_cut():
    first = w("Um,", 0.2, 0.6)
    second = w("uh,", 0.65, 0.95)
    words = [first, second, w("tomorrow", 1.0, 1.5), w("we", 1.55, 1.63), w("go.", 1.7, 2.0)]
    words += [w("Really", 3.0, 3.4), w("nice", 3.5, 3.8), w("place.", 3.9, 4.4)]
    words += [w("uh", 7.2, 7.6)]
    plan = build_cut_plan(words, [], 8.0)
    for filler in (first, second, words[-1]):
        assert any(
            lo <= filler["start_s"] + 1e-6 and hi >= filler["end_s"] - 1e-6
            for lo, hi in _lexical(plan)
        ), (filler, _lexical(plan))


def test_a_refused_filler_is_not_removed_by_any_other_rule():
    """Silence on both sides of the refused token: the acoustic and pause rules must treat
    it as a word (window-clipped flanks, normal pause residual), so nothing eats into it."""
    filler = w("uh,", 3.0, 3.4)
    words = _with(filler, w("It's,", 2.0, 2.2), w("provolone", 3.4, 3.6))
    silences = [(2.25, 2.95), (3.65, 5.9)]
    comparison = build_cut_plan_comparison(words, silences, 8.0, over_budget_policy="clamp")
    for plan in (comparison.baseline, comparison.candidate):
        assert _cut_over(plan, 3.0, 3.6) == []
