"""Golden: a filler stamped late is cut where it was said, and no filler cut takes a word.

Merope café take (KRI-790, the brief's live whisper-1 words, 0-29.35 s). From 3.8 s to 12 s
whisper-1 stamped every word 0.3-0.5 s late, the fillers included. Rule 1 cut where the stamps
said: "Uh" 5.09-5.61 removed "we are wi-", "um" 8.80-9.00 removed "tell", and the real "uh"
(~4.5-4.9) and "um" (~8.1-8.5) stayed in. The KRI-970 squeeze guard cannot see it: nothing is
squeezed, the whole stretch is shifted.

The fix has two parts (``filler_check``):
* re-time the words with a CTC forced aligner and give each lexicon filler the gap between its
  real neighbours, but only when every filler passes the squeeze guard on whisper's own stamps,
  has room there, and the take is English;
* re-listen around every filler cut and cancel the cut when a real word next to it disappears.

How this could fail (written before the code):
- the aligner moves the provolone "uh," (T1/T3) onto "the", and the cut takes a real word again;
- a filler the guard refuses on whisper's stamps is cut once its neighbour is no longer squeezed;
- a filler with no room between its aligned neighbours is cut over a word whisper dropped;
- the aligner is missing or raises, and cleanup fails instead of falling back to today's plan;
- a non-English take is aligned with an English-only model;
- tokens with no letters ("20", "-") break the alignment or shift every later word;
- aligned word edges are tight, so a cut clips the end of the word before it;
- the re-listen step never runs because alignment was skipped;
- a cancelled cut comes back when the plan is rebuilt (the V2 candidate re-times words to pauses);
- a cancelled acoustic-island cut comes back because only lexicon tokens were protected;
- a transcription error in the re-listen step fails the analysis instead of keeping the cut;
- a filler the re-listen hears as a word ("people") cancels a good cut (judged as lost);
- half-heard words at the window edge cancel good cuts;
- many filler cuts make unbounded paid transcription calls;
- the stored words and the plan disagree, so captions or the snapshot check break;
- retake spans (word indices) shift because a token was added or removed;
- the switch off is not byte-identical to before;
- the analysis identity ignores the switch, so a flip reuses old analyses;
- real fillers that already land right (sourdough) stop being cut.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import app.pipeline.speech_cleanup_analysis as analysis
from app.pipeline.silence_cut import REASON_FILLER_LEXICAL, is_filler_token
from app.pipeline.speech_cleanup_alignment import AlignedWords, AlignerUnavailable, viterbi
from app.pipeline.speech_cleanup_analysis import (
    RELISTEN_MAX_CUTS,
    SpeechCleanupAnalysisInput,
    analyze_speech_cleanup,
)
from app.pipeline.transcribe import TranscribeError
from tests._silent_pcm import silent_pcm

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_CAFE = json.loads((_FIXTURES / "kri970_cafe_late_stamps.json").read_text())
_TAKES = json.loads((_FIXTURES / "kri970_provolone_takes.json").read_text())["takes"]


def _overlap(a: float, b: float, lo: float, hi: float) -> float:
    return max(0.0, min(b, hi) - max(a, lo))


def _run(
    words,
    silences,
    duration,
    *,
    check=True,
    language="en",
    monkeypatch=None,
    align=None,
    heard=None,
):
    """The real analysis; only the aligner and the re-listen transcription are replayed."""

    if monkeypatch is not None:
        monkeypatch.setattr(analysis, "align_words", align or _align_must_not_run)
        monkeypatch.setattr(analysis, "_window_words", heard or _same_words_heard)
    transcript = SimpleNamespace(
        words=[SimpleNamespace(**w) for w in words], language=language, low_confidence=False
    )
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="kri970-filler-check",
            local_media_path="/dev/null",
            duration_s=duration,
            mixed_gap_mode="apply",
            filler_check=check,
        ),
        transcribe_fn=lambda *_a, **_k: transcript,
        silence_detect_fn=lambda *_a, **_k: SimpleNamespace(
            spans=tuple(map(tuple, silences)), status="ok"
        ),
        pcm_loader=silent_pcm,
    )


def _align_must_not_run(*_a, **_k):
    raise AssertionError("aligner ran where it must not")


def _cafe_aligner(*_a, **_k):
    return AlignedWords(
        spans=[tuple(s) if s else None for s in _CAFE["aligned_spans"]],
        median_score=_CAFE["aligned_median_score"],
    )


def _same_words_heard(_path, start, end, cut, **_k):
    return [("same", start + 0.1)]  # the cut changes nothing anyone can hear


def _cafe(**kw):
    return _run(_CAFE["words"], _CAFE["silences"], _CAFE["duration_s"], **kw)


def _cuts(result):
    return [(r.start_s, r.end_s, r.reason) for r in result.cut_plan.to_cut_plan().removed]


def test_whisper_stamps_alone_cut_real_words_in_this_take(monkeypatch):
    # The incident, on the real input: without the check rule 1 cuts "we are with" and "tell".
    result = _cafe(check=False, monkeypatch=monkeypatch)
    lost = [
        name
        for name, (lo, hi) in _CAFE["real_words"].items()
        if any(_overlap(a, b, lo, hi) > 0.3 * (hi - lo) for a, b, _ in _cuts(result))
    ]
    assert {"we", "are", "with", "tell"} <= set(lost)
    assert "filler_check" not in result.diagnostics


def test_aligned_fillers_are_cut_where_they_were_said(monkeypatch):
    result = _cafe(monkeypatch=monkeypatch, align=_cafe_aligner)
    report = result.diagnostics["filler_check"]
    assert report["aligned"] is True and report["reason"] == "aligned"
    cuts = _cuts(result)
    for lo, hi in _CAFE["real_fillers"]:
        assert any(_overlap(a, b, lo, hi) >= 0.5 * (hi - lo) for a, b, _ in cuts), (lo, hi, cuts)
    for name, (lo, hi) in _CAFE["real_words"].items():
        assert all(_overlap(a, b, lo, hi) <= 0.02 for a, b, _ in cuts), (name, cuts)


def test_stored_words_are_the_words_the_plan_was_built_from(monkeypatch):
    result = _cafe(monkeypatch=monkeypatch, align=_cafe_aligner)
    stored = [(w.text, w.start_s) for w in result.timed_words]
    assert [t for t, _ in stored] == [w["text"] for w in _CAFE["words"]]  # same tokens, same order
    uh = next(w for w in result.timed_words if w.text == "Uh")
    assert uh.start_s < 5.0  # the filler now sits where it was said (4.45-4.95), not at 5.09
    starts = [s for _, s in stored]
    assert starts == sorted(starts)


@pytest.mark.parametrize("name", ["T1", "T3"])
def test_provolone_takes_are_never_retimed(name, monkeypatch):
    take = _TAKES[name]
    on = _run(take["words"], take["silences"], take["duration_s"], monkeypatch=monkeypatch)
    off = _run(take["words"], take["silences"], take["duration_s"], check=False)
    assert on.diagnostics["filler_check"]["reason"] == "squeezed_filler"
    assert _cuts(on) == _cuts(off)
    lo, hi = take["spoken_provolone_s"]
    assert all(_overlap(a, b, lo, hi) == 0 for a, b, _ in _cuts(on))


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (AlignerUnavailable("no model"), "aligner_unavailable"),
        (RuntimeError("torch fell over"), "aligner_error:RuntimeError"),
        (OSError("disk"), "aligner_error:OSError"),
    ],
)
def test_aligner_failure_keeps_whisper_stamps(failure, reason, monkeypatch):
    def broken(*_a, **_k):
        raise failure

    on = _cafe(monkeypatch=monkeypatch, align=broken)
    off = _cafe(check=False)
    assert on.diagnostics["filler_check"]["reason"] == reason
    assert _cuts(on) == _cuts(off)
    assert [w.start_s for w in on.timed_words] == [w["start_s"] for w in _CAFE["words"]]


def test_low_alignment_score_keeps_whisper_stamps(monkeypatch):
    def weak(*_a, **_k):
        return AlignedWords(spans=_cafe_aligner().spans, median_score=0.05)

    assert _cafe(monkeypatch=monkeypatch, align=weak).diagnostics["filler_check"]["reason"] == (
        "low_alignment_score"
    )


def test_non_english_take_is_not_aligned(monkeypatch):
    result = _cafe(monkeypatch=monkeypatch, language="tr")  # aligner would raise if called
    assert result.diagnostics["filler_check"]["reason"] == "not_english"


def test_filler_without_room_keeps_whisper_stamps(monkeypatch):
    def no_room(*_a, **_k):
        spans = [tuple(s) if s else None for s in _CAFE["aligned_spans"]]
        i = next(i for i, w in enumerate(_CAFE["words"]) if w["text"] == "Uh")
        spans[i + 1] = (spans[i - 1][1] + 0.05, spans[i + 1][1])  # "we" right after "everyone."
        return AlignedWords(spans=spans, median_score=0.5)

    result = _cafe(monkeypatch=monkeypatch, align=no_room)
    assert result.diagnostics["filler_check"]["reason"] == "filler_without_room"
    assert [w.start_s for w in result.timed_words] == [w["start_s"] for w in _CAFE["words"]]


def test_a_token_with_no_letters_sits_in_the_gap_around_it(monkeypatch):
    words = [
        {"text": "about", "start_s": 1.0, "end_s": 1.3},
        {"text": "20", "start_s": 1.3, "end_s": 1.6},
        {"text": "uh,", "start_s": 1.6, "end_s": 2.1},
        {"text": "degrees", "start_s": 2.1, "end_s": 2.6},
    ]

    def aligner(*_a, **_k):
        return AlignedWords(spans=[(0.6, 0.9), None, (1.5, 1.55), (1.9, 2.3)], median_score=0.9)

    result = _run(words, [], 6.0, monkeypatch=monkeypatch, align=aligner)
    stored = {w.text: (w.start_s, w.end_s) for w in result.timed_words}
    assert stored["20"][0] >= stored["about"][1] - 1e-6
    assert stored["uh,"][0] >= stored["20"][1] - 1e-6
    assert stored["uh,"][1] <= stored["degrees"][0] + 1e-6


def _heard_without(lost_near: dict[float, list[str]]):
    """Re-listen replay: the cut starting at a key loses those words; every other cut loses none."""

    def heard(_path, start, end, cut, **_k):
        near = [(c, ws) for c, ws in lost_near.items() if start <= c <= end]
        if cut is None:
            return [(w, round(c - start + 0.05, 3)) for c, ws in near for w in ws] + [("ctx", 0.0)]
        gone = next((ws for c, ws in lost_near.items() if abs(cut[0] - c) < 1e-6), [])
        return [(w, 0.0) for _c, ws in near for w in ws if w not in gone] + [("ctx", 0.0)]

    return heard


def test_relisten_drops_a_cut_that_takes_a_word(monkeypatch):
    def unavailable(*_a, **_k):
        raise AlignerUnavailable("no model")

    before = _cafe(check=False)
    after = _cafe(
        monkeypatch=monkeypatch,
        align=unavailable,
        heard=_heard_without({5.09: ["we", "are", "with"], 8.8: ["tell"]}),
    )
    report = after.diagnostics["filler_check"]["relisten"]
    assert sorted(d["start_s"] for d in report["dropped"]) == [5.09, 8.8]
    assert [c for c in _cuts(after)] == [c for c in _cuts(before) if c[0] not in (5.09, 8.8)]
    plan = after.cut_plan
    assert plan.time_saved_s == pytest.approx(sum(r.end_s - r.start_s for r in plan.removed))


def test_relisten_never_judges_fillers_or_far_words():
    lost = analysis._words_lost(
        [("um", 1.5), ("we", 1.6), ("merhaba", 3.4)],  # cut is 1.4-1.9 in window time
        [("we", 1.4)],
        1.4 - analysis.RELISTEN_JUDGE_S,
        1.9 + analysis.RELISTEN_JUDGE_S,
    )
    assert lost == []
    assert analysis._words_lost([("tell", 1.5)], [("about", 1.5)], 0.9, 2.4) == ["tell"]
    assert analysis._words_lost([("cookie", 1.5)], [("cookies", 1.5)], 0.9, 2.4) == []
    assert analysis._relisten_key("Two,") == analysis._relisten_key("2") == "2"


def test_relisten_transcription_failure_keeps_the_cut(monkeypatch):
    def failing(*_a, **_k):
        raise TranscribeError("whisper down")

    def unavailable(*_a, **_k):
        raise AlignerUnavailable("no model")

    before = _cafe(check=False)
    after = _cafe(monkeypatch=monkeypatch, align=unavailable, heard=failing)
    assert _cuts(after) == _cuts(before)
    assert after.diagnostics["filler_check"]["relisten"]["checked"] == 0


def test_relisten_programming_error_is_not_swallowed(monkeypatch):
    def buggy(*_a, **_k):
        raise KeyError("bug")

    def unavailable(*_a, **_k):
        raise AlignerUnavailable("no model")

    with pytest.raises(KeyError):
        _cafe(monkeypatch=monkeypatch, align=unavailable, heard=buggy)


def test_relisten_bounds_paid_transcription(monkeypatch):
    words, t = [], 0.5
    for i in range(RELISTEN_MAX_CUTS + 3):
        words += [
            {"text": f"word{i}", "start_s": t, "end_s": t + 0.4},
            {"text": "um,", "start_s": t + 0.4, "end_s": t + 0.8},
        ]
        t += 1.2
    words.append({"text": "end", "start_s": t, "end_s": t + 0.4})
    calls = []

    def heard(_p, start, end, cut, **_k):
        calls.append(cut)
        return [("same", 0.1)]

    def unavailable(*_a, **_k):
        raise AlignerUnavailable("no model")

    monkeypatch.setattr(analysis, "align_words", unavailable)
    monkeypatch.setattr(analysis, "_window_words", heard)
    result = _run(words, [], t + 2.0)
    report = result.diagnostics["filler_check"]["relisten"]
    assert report["checked"] == RELISTEN_MAX_CUTS
    assert report["unchecked"] >= 1
    assert len(calls) == 2 * RELISTEN_MAX_CUTS


def test_policy_fingerprint_names_the_switch(monkeypatch):
    from app.config import settings
    from app.services.plan_item_media import current_detector_policy

    monkeypatch.setattr(settings, "speech_cleanup_filler_check_enabled", True)
    on = current_detector_policy()
    monkeypatch.setattr(settings, "speech_cleanup_filler_check_enabled", False)
    off = current_detector_policy()
    assert "filler-check=enabled" in on and "filler-check=off" in off


def test_viterbi_finds_the_only_path():
    # 6 frames, labels: 0 blank, 1 "A", 2 "B". The audio says A at frames 1-2, B at frame 4.
    lp = np.full((6, 3), np.log(0.01))
    for t, label in enumerate([0, 1, 1, 0, 2, 0]):
        lp[t, label] = np.log(0.98)
    assert viterbi(lp, [1, 2], blank=0) == [(1, 2), (4, 4)]


def test_viterbi_keeps_a_doubled_letter_apart():
    lp = np.full((5, 2), np.log(0.01))
    for t, label in enumerate([1, 0, 1, 0, 0]):
        lp[t, label] = np.log(0.98)
    assert viterbi(lp, [1, 1], blank=0) == [(0, 0), (2, 2)]


def test_a_real_filler_next_to_normal_words_is_still_found_by_the_lexicon():
    assert is_filler_token("Uh") and is_filler_token("um,") and not is_filler_token("[kept]")
    assert REASON_FILLER_LEXICAL == "filler_lexical"
