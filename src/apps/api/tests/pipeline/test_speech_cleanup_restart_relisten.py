"""A restart removal is re-listened on its NEIGHBOURS only, never on its own abandoned words.

Çiğdem S3 take A (live whisper-1 words, KRI-720 gap 2): "...cinnamon roll | and Mira's perfume
on, oh, sorry, again, | and Mira's perfume on a stack of pancakes." The restart overlapped the
filler "oh", so it was re-listened; ``_words_lost`` then judged every word inside the cut and a
restart's abandoned words are lost by design ("sorry", "again", the first "and"), so the whole
restart was dropped.

How this could fail (written before the code):
- R1: the restart is still judged on its own words and dropped;
- R2: the restart is no longer re-listened at all, so a wrong span (a real word eaten) is kept;
- R3: the word right before the cut ("roll") disappears and the restart is still kept;
- R4: the redelivery "and" after the cut disappears and the restart is still kept;
- R5: a repeated word ("and" inside the span and right after it) is miscounted by the pool;
- R6: a filler-only removal that takes a real word is no longer dropped;
- R7: the slate cut (a retake-kind cut too) is dropped or judged on its own words.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import app.pipeline.speech_cleanup_analysis as analysis
from app.pipeline.silence_cut import REASON_RETAKE, CutPlan, Removal
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from tests._silent_pcm import silent_pcm

_WORDS = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri720_cigdem_take_a_words.json"
    ).read_text()
)
_RESTART = (39, 45)  # "and Mira's perfume on, oh, sorry, again,"
_SLATE = (0, 3)  # "Wait, the light, okay."
_RESTART_CUT = (13.94, 17.54)


def _run(heard, words=_WORDS, spans=(_RESTART, _SLATE), duration=20.108, silences=()):
    transcript = SimpleNamespace(
        words=[SimpleNamespace(**w, confidence=1.0) for w in words],
        language="en",
        low_confidence=False,
    )
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="kri720-restart",
            local_media_path="/dev/null",
            duration_s=duration,
            mixed_gap_mode="apply",
            filler_check=True,
            include_silence_and_fillers=True,
            retake_spans=spans,
        ),
        transcribe_fn=lambda *_a, **_k: transcript,
        silence_detect_fn=lambda *_a, **_k: SimpleNamespace(spans=silences, status="ok"),
        pcm_loader=silent_pcm,
    )


def _hearing(words=_WORDS, lose=()):
    """whisper-1 hears exactly the words left in the window; ``lose`` words vanish after a cut."""

    def heard(_path, start, end, cut, **_k):
        out = []
        for w in words:
            mid = (w["start_s"] + w["end_s"]) / 2
            if not start <= mid < end or (cut and cut[0] <= mid < cut[1]):
                continue
            if cut and (w["text"], round(w["start_s"], 2)) in lose:
                continue
            gap = (cut[1] - cut[0]) if cut and mid >= cut[1] else 0.0
            key = analysis._relisten_key(w["text"])
            if key:
                out.append((key, mid - start - gap))
        return out

    return heard


def _cuts(result):
    return [(r.start_s, r.end_s) for r in result.cut_plan.to_cut_plan().removed]


def _restart_kept(result):
    return any(
        abs(a - _RESTART_CUT[0]) < 0.3 and abs(b - _RESTART_CUT[1]) < 0.3 for a, b in _cuts(result)
    )


def test_restart_removal_stays_when_only_its_own_words_are_gone(monkeypatch):
    monkeypatch.setattr(analysis, "_window_words", _hearing())
    result = _run(None)
    report = result.diagnostics["filler_check"]["relisten"]
    assert _restart_kept(result)
    assert report["dropped"] == []
    assert report["checked"] >= 1  # still re-listened (R2)
    assert any(b <= 2.6 for _a, b in _cuts(result))  # the slate cut stays (R7)


def test_restart_removal_dropped_when_word_before_the_cut_is_lost(monkeypatch):
    monkeypatch.setattr(analysis, "_window_words", _hearing(lose={("roll", 13.6)}))
    result = _run(None)
    assert not _restart_kept(result)
    assert any(
        d["lost"] == ["roll"] for d in result.diagnostics["filler_check"]["relisten"]["dropped"]
    )


def test_restart_removal_dropped_when_redelivery_word_is_lost(monkeypatch):
    monkeypatch.setattr(analysis, "_window_words", _hearing(lose={("and", 17.66)}))
    result = _run(None)
    assert not _restart_kept(result)


def test_repeated_and_inside_and_after_the_span_is_judged_per_side(monkeypatch):
    # The "and" inside the span is gone from the cut window by design; the redelivery "and"
    # after it must still be matched to ITS OWN heard word, not consumed by another "and".
    monkeypatch.setattr(analysis, "_window_words", _hearing())
    result = _run(None)
    assert result.diagnostics["filler_check"]["relisten"]["dropped"] == []
    monkeypatch.setattr(analysis, "_window_words", _hearing(lose={("and", 17.66)}))
    assert not _restart_kept(_run(None))


def test_filler_only_removal_that_takes_a_real_word_is_still_dropped(monkeypatch):
    words = [
        {"text": "we", "start_s": 0.0, "end_s": 0.3},
        {"text": "were", "start_s": 0.4, "end_s": 0.7},
        {"text": "um", "start_s": 0.9, "end_s": 1.3},
        {"text": "going", "start_s": 1.4, "end_s": 1.9},
        {"text": "home", "start_s": 2.0, "end_s": 2.5},
        {"text": "tonight", "start_s": 3.2, "end_s": 3.9},
        {"text": "okay", "start_s": 4.4, "end_s": 4.9},
    ]
    monkeypatch.setattr(analysis, "_window_words", _hearing(words, lose={("were", 0.4)}))
    result = _run(None, words=words, spans=(), duration=5.6, silences=((0.7, 0.9), (1.3, 1.4)))
    report = result.diagnostics["filler_check"]["relisten"]
    assert report["checked"] >= 1
    assert report["dropped"] and "were" in report["dropped"][0]["lost"]


def test_real_word_inside_the_removal_but_outside_the_span_is_judged(monkeypatch):
    # The span stops before "again," (index 45) but a merged/snapped removal still swallows it:
    # that real word is not the restart's own, so it must be heard after the cut, and is not.
    monkeypatch.setattr(analysis, "_window_words", _hearing())
    words = [analysis.SpeechCleanupTimedWord(**w) for w in _WORDS]
    removal = Removal(start_s=13.94, end_s=17.54, reason=REASON_RETAKE)
    plan = CutPlan(
        keep_segments=[(0.0, 13.94), (17.54, 20.108)], removed=[removal], time_saved_s=3.6
    )
    kept, report = analysis._relistened_plan(
        plan,
        words,
        audio_path="/dev/null",
        duration_s=20.108,
        language="en",
        transcribe=None,
        retake_spans=((39, 44),),
    )
    assert kept.removed == []
    assert [d["lost"] for d in report["dropped"]] == [["again"]]
    # control: the same removal with the span reaching "again," keeps it
    kept, report = analysis._relistened_plan(
        plan,
        words,
        audio_path="/dev/null",
        duration_s=20.108,
        language="en",
        transcribe=None,
        retake_spans=((39, 45),),
    )
    assert kept.removed == [removal] and report["dropped"] == []
