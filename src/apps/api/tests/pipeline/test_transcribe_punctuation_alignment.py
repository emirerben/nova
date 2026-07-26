"""Workstream 3a: `align_punctuated_text` restores punctuation/capitalization from
the full-text transcript onto whisper-1's (punctuation-free) timed word stream.

Covers the pure alignment function directly plus its wiring into
`_transcribe_openai`, including the `CAPTION_PUNCTUATION_ENABLED` kill switch
(byte-identical to pre-feature when off — same pattern as the
`EDITORIAL_SEQUENCE_ENABLED` guards).
"""

from __future__ import annotations

from types import SimpleNamespace

import openai
import pytest

import app.pipeline.transcribe as tr
from app.pipeline.transcribe import Word, align_punctuated_text


def _w(text: str, start: float, end: float, confidence: float = 1.0) -> Word:
    return Word(text=text, start_s=start, end_s=end, confidence=confidence)


# ── pure function: 1:1 match ──────────────────────────────────────────────────


def test_1to1_match_restores_punctuation_and_case():
    words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
    out = align_punctuated_text("Hello, world!", words)
    assert [w.text for w in out] == ["Hello,", "world!"]
    # Timing is untouched.
    assert [(w.start_s, w.end_s) for w in out] == [(0.0, 0.5), (0.5, 1.0)]


def test_1to1_match_recovers_contraction_apostrophe_whisper_dropped():
    # whisper-1's word-level tokens drop apostrophes ("that's" -> "thats");
    # comparison strips ALL punctuation so this still 1:1 matches, and the
    # display token (with the apostrophe restored) wins as the final text.
    words = [_w("thats", 0.0, 0.5)]
    out = align_punctuated_text("that's", words)
    assert out[0].text == "that's"


def test_1to1_match_carries_segment_signals_forward():
    w = Word(
        text="hi",
        start_s=0.0,
        end_s=0.5,
        confidence=1.0,
        segment_avg_logprob=-0.2,
        segment_no_speech_prob=0.05,
    )
    out = align_punctuated_text("Hi.", [w])
    assert out[0].segment_avg_logprob == -0.2
    assert out[0].segment_no_speech_prob == 0.05


# ── pure function: merge case (k <= 3) ────────────────────────────────────────


def test_merge_case_two_tokens_number_with_comma():
    words = [
        _w("thats", 0.0, 0.3),
        _w("200", 0.3, 0.6),
        _w("000", 0.6, 0.9),
        _w("dollars", 0.9, 1.2),
    ]
    out = align_punctuated_text("that's 200,000 dollars", words)
    assert [w.text for w in out] == ["that's", "200,000", "dollars"]
    merged = out[1]
    assert (merged.start_s, merged.end_s) == (0.3, 0.9)


def test_merge_case_three_tokens():
    words = [_w("1", 0.0, 0.2), _w("2", 0.2, 0.4), _w("3", 0.4, 0.6)]
    out = align_punctuated_text("1,2,3", words)
    assert [w.text for w in out] == ["1,2,3"]
    assert (out[0].start_s, out[0].end_s) == (0.0, 0.6)


def test_merge_case_confidence_is_min_of_merged_words():
    words = [
        _w("200", 0.0, 0.3, confidence=0.9),
        _w("000", 0.3, 0.6, confidence=0.4),
    ]
    out = align_punctuated_text("200,000", words)
    assert out[0].confidence == 0.4


def test_merge_case_does_not_fire_beyond_k_equals_3():
    # 4-way split would need k=4; the spec caps merges at k<=3, so this must bail.
    words = [_w("1", 0.0, 0.1), _w("2", 0.1, 0.2), _w("3", 0.2, 0.3), _w("4", 0.3, 0.4)]
    out = align_punctuated_text("1,2,3,4", words)
    assert out is words  # fail-open: bailed to the original list


# ── pure function: bounded lookahead resync (<=2 tokens each side) ───────────


def test_lookahead_recovers_extra_spoken_word_not_in_full_text():
    # whisper's word list has a stray "uh" the punctuated full_text dropped.
    words = [_w("so", 0.0, 0.2), _w("uh", 0.2, 0.3), _w("hello", 0.3, 0.6)]
    out = align_punctuated_text("So hello.", words)
    assert [w.text for w in out] == ["So", "uh", "hello."]
    # The unmatched word keeps its original (unpunctuated) text verbatim.
    assert out[1].text == "uh"


def test_lookahead_within_two_token_window_both_sides():
    words = [_w("a", 0.0, 0.1), _w("b", 0.1, 0.2), _w("c", 0.2, 0.3), _w("d", 0.3, 0.4)]
    # "b" and "c" are extra spoken tokens absent from the punctuated text.
    out = align_punctuated_text("a d", words)
    assert [w.text for w in out] == ["a", "b", "c", "d"]


# ── pure function: fail-open bail ─────────────────────────────────────────────


def test_bail_on_residual_mismatch_returns_original_words_unchanged():
    words = [_w("completely", 0.0, 0.5), _w("different", 0.5, 1.0)]
    out = align_punctuated_text("totally unrelated text here", words)
    assert out is words
    assert [w.text for w in out] == ["completely", "different"]


def test_bail_when_display_tokens_exhausted_before_words():
    words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0), _w("extra", 1.0, 1.5)]
    out = align_punctuated_text("Hello world", words)
    assert out is words


def test_bail_when_display_tokens_left_over_after_words():
    words = [_w("hello", 0.0, 0.5)]
    out = align_punctuated_text("Hello there world", words)
    assert out is words


def test_bail_logs_alignment_bailed_counter(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tr.log, "info", lambda event, **kw: events.append((event, kw)))
    words = [_w("completely", 0.0, 0.5)]
    align_punctuated_text("nope", words)
    assert any(event == "alignment_bailed" for event, _ in events)


def test_empty_words_returns_words_untouched():
    assert align_punctuated_text("anything", []) == []


def test_empty_full_text_bails():
    words = [_w("hello", 0.0, 0.5)]
    out = align_punctuated_text("", words)
    assert out is words


def test_empty_full_text_whitespace_only_bails():
    words = [_w("hello", 0.0, 0.5)]
    out = align_punctuated_text("   ", words)
    assert out is words


def test_pure_function_never_mutates_input_words_in_place():
    original = _w("hello", 0.0, 0.5)
    words = [original]
    align_punctuated_text("Hello.", words)
    assert original.text == "hello"  # unchanged — a NEW Word was returned


# ── wiring into _transcribe_openai + kill switch ──────────────────────────────


class _FakeResp:
    def __init__(self, words=None, text="", language=""):
        self.words = words or []
        self.text = text
        self.language = language


class _FakeTranscriptions:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def create(self, **_kwargs: object) -> _FakeResp:
        return self._resp


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self.audio = type("A", (), {"transcriptions": _FakeTranscriptions(resp)})()


@pytest.fixture()
def _audio(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"\x00")
    return str(p)


def _fake_openai(monkeypatch, resp: _FakeResp) -> None:
    monkeypatch.setattr(openai, "OpenAI", lambda **_k: _FakeClient(resp))


def test_transcribe_openai_applies_punctuation_when_enabled(_audio, monkeypatch):
    monkeypatch.setattr(tr.settings, "caption_punctuation_enabled", True, raising=False)
    resp = _FakeResp(
        words=[
            SimpleNamespace(word="hello", start=0.0, end=0.5),
            SimpleNamespace(word="world", start=0.5, end=1.0),
        ],
        text="Hello, world!",
    )
    _fake_openai(monkeypatch, resp)
    t = tr._transcribe_openai(_audio)
    assert [w.text for w in t.words] == ["Hello,", "world!"]


def test_kill_switch_disabled_reproduces_pre_fix_output(_audio, monkeypatch):
    """CAPTION_PUNCTUATION_ENABLED=false -> byte-identical to today: whisper's
    raw (punctuation-free) word text passes through untouched, even though
    `full_text` carries punctuation that WOULD have been applied."""
    monkeypatch.setattr(tr.settings, "caption_punctuation_enabled", False, raising=False)
    resp = _FakeResp(
        words=[
            SimpleNamespace(word="hello", start=0.0, end=0.5),
            SimpleNamespace(word="world", start=0.5, end=1.0),
        ],
        text="Hello, world!",
    )
    _fake_openai(monkeypatch, resp)
    t = tr._transcribe_openai(_audio)
    assert [w.text for w in t.words] == ["hello", "world"]


def test_kill_switch_disabled_never_calls_align_punctuated_text(_audio, monkeypatch):
    monkeypatch.setattr(tr.settings, "caption_punctuation_enabled", False, raising=False)
    monkeypatch.setattr(
        tr,
        "align_punctuated_text",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not align when disabled")),
    )
    resp = _FakeResp(words=[SimpleNamespace(word="hello", start=0.0, end=0.5)], text="Hello.")
    _fake_openai(monkeypatch, resp)
    t = tr._transcribe_openai(_audio)
    assert [w.text for w in t.words] == ["hello"]


def test_transcribe_openai_bails_open_on_residual_mismatch(_audio, monkeypatch):
    monkeypatch.setattr(tr.settings, "caption_punctuation_enabled", True, raising=False)
    resp = _FakeResp(
        words=[SimpleNamespace(word="hello", start=0.0, end=0.5)],
        text="totally unrelated garbled transcript text",
    )
    _fake_openai(monkeypatch, resp)
    t = tr._transcribe_openai(_audio)
    assert [w.text for w in t.words] == ["hello"]
