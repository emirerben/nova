"""plans/010 T3: `verbatim_prompt` passthrough + segment quality signals on words.

No network: both Whisper backends are faked. Two contracts pinned here:

1. IRON RULE — with `verbatim_prompt=None` (the default) every constructed
   request is byte-identical to pre-change: whisper-1 gets NO `prompt` key at
   all (not prompt=None) and faster-whisper gets NO `initial_prompt`. The full
   kwargs SET is asserted, so any accidental addition to the default request
   fails these pins.
2. Segment-level `avg_logprob` / `no_speech_prob` (whisper-1 verbose_json
   `segments[]`; faster-whisper Segment objects) are mapped onto each Word by
   word midpoint within [segment.start, segment.end]; words outside every
   segment keep None. whisper-1 returns no per-word confidence (the 1.0
   hardcode stays), so these are the only quality signals on the prod path.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import openai
import pytest

import app.pipeline.transcribe as tr
from app.pipeline.transcribe import Transcript, Word

VERBATIM = "Uh, um, ııı, eee."


class _FakeResp:
    def __init__(self, words=None, segments=None, text="", language=""):
        self.words = words or []
        self.text = text
        self.language = language
        if segments is not None:  # absent attr mimics a segment-less response
            self.segments = segments


class _FakeTranscriptions:
    def __init__(self, sink: dict, resp: _FakeResp):
        self._sink = sink
        self._resp = resp

    def create(self, **kwargs: object) -> _FakeResp:
        self._sink.clear()
        self._sink.update(kwargs)
        return self._resp


class _FakeClient:
    def __init__(self, sink: dict, resp: _FakeResp):
        self.audio = type("A", (), {"transcriptions": _FakeTranscriptions(sink, resp)})()


def _fake_openai(monkeypatch, sink: dict, resp: _FakeResp | None = None) -> None:
    monkeypatch.setattr(openai, "OpenAI", lambda **_k: _FakeClient(sink, resp or _FakeResp()))


def _install_fake_faster_whisper(monkeypatch, segments: list, language: str = "en") -> dict:
    """Inject a fake `faster_whisper` module capturing transcribe() kwargs."""
    calls: dict = {}

    class _FakeWhisperModel:
        def __init__(self, *_a, **_k):
            pass

        def transcribe(self, audio_path, **kwargs):
            calls["audio_path"] = audio_path
            calls["kwargs"] = kwargs
            return iter(segments), SimpleNamespace(language=language)

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


@pytest.fixture()
def _audio(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"\x00")
    return str(p)


# ── IRON RULE: default path byte-identical (no prompt key at all) ─────────────


def test_openai_default_request_byte_identical(_audio, monkeypatch):
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    tr._transcribe_openai(_audio)
    assert sink["model"] == "whisper-1"
    assert sink["response_format"] == "verbose_json"
    assert sink["timestamp_granularities"] == ["word"]
    assert hasattr(sink["file"], "read")
    # Exact pre-change kwargs set — nothing may be added on the default path.
    assert set(sink) == {"model", "file", "response_format", "timestamp_granularities"}


def test_openai_default_with_language_still_no_prompt(_audio, monkeypatch):
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    tr._transcribe_openai(_audio, language="tr")
    assert sink["language"] == "tr"
    assert set(sink) == {"model", "file", "response_format", "timestamp_granularities", "language"}


def test_local_default_call_byte_identical(_audio, monkeypatch):
    calls = _install_fake_faster_whisper(monkeypatch, segments=[])
    tr._transcribe_local(_audio)
    # Exact pre-change kwargs — no `initial_prompt` key at all (not =None).
    assert calls["kwargs"] == {"word_timestamps": True, "language": None}


def test_local_default_with_language_still_no_initial_prompt(_audio, monkeypatch):
    calls = _install_fake_faster_whisper(monkeypatch, segments=[])
    tr._transcribe_local(_audio, language="tr")
    assert calls["kwargs"] == {"word_timestamps": True, "language": "tr"}


# ── verbatim_prompt threaded when provided ────────────────────────────────────


def test_openai_verbatim_prompt_threaded(_audio, monkeypatch):
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    tr._transcribe_openai(_audio, verbatim_prompt=VERBATIM)
    assert sink["prompt"] == VERBATIM
    assert set(sink) == {"model", "file", "response_format", "timestamp_granularities", "prompt"}


def test_local_verbatim_prompt_threaded(_audio, monkeypatch):
    calls = _install_fake_faster_whisper(monkeypatch, segments=[])
    tr._transcribe_local(_audio, verbatim_prompt=VERBATIM)
    assert calls["kwargs"] == {
        "word_timestamps": True,
        "language": None,
        "initial_prompt": VERBATIM,
    }


def test_transcribe_whisper_threads_verbatim_prompt_to_backend(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(tr, "_extract_audio", lambda _src, _dst: None)
    monkeypatch.setattr(tr.settings, "whisper_backend", "openai-api")

    def _fake_backend(audio_path, *, language=None, verbatim_prompt=None):
        seen.update(language=language, verbatim_prompt=verbatim_prompt)
        return Transcript()

    monkeypatch.setattr(tr, "_transcribe_openai", _fake_backend)
    tr.transcribe_whisper("/nonexistent.mp4", language="tr", verbatim_prompt=VERBATIM)
    assert seen == {"language": "tr", "verbatim_prompt": VERBATIM}


def test_transcribe_public_entrypoint_threads_verbatim_prompt(monkeypatch):
    seen: dict = {}

    def _fake_whisper(video_path, *, model=None, language=None, verbatim_prompt=None):
        seen.update(language=language, verbatim_prompt=verbatim_prompt)
        return Transcript()

    monkeypatch.setattr(tr, "transcribe_whisper", _fake_whisper)
    tr.transcribe("/x.mp4", verbatim_prompt=VERBATIM)
    assert seen == {"language": None, "verbatim_prompt": VERBATIM}


# ── segment-signal mapping (word midpoint within [segment.start, segment.end]) ─


def _w(start: float, end: float) -> Word:
    return Word(text="x", start_s=start, end_s=end, confidence=1.0)


def _seg(start: float, end: float, avg_logprob: float, no_speech_prob: float):
    return SimpleNamespace(
        start=start, end=end, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob
    )


def test_segment_mapping_words_map_to_containing_segment():
    seg_a = _seg(0.0, 2.0, -0.2, 0.05)
    seg_b = _seg(2.0, 4.0, -0.9, 0.6)
    words = [_w(0.4, 0.8), _w(2.5, 3.0)]
    tr._apply_segment_signals(words, [seg_a, seg_b])
    assert (words[0].segment_avg_logprob, words[0].segment_no_speech_prob) == (-0.2, 0.05)
    assert (words[1].segment_avg_logprob, words[1].segment_no_speech_prob) == (-0.9, 0.6)


def test_segment_mapping_word_outside_all_segments_keeps_none():
    words = [_w(5.0, 5.5)]  # midpoint 5.25 beyond every segment
    tr._apply_segment_signals(words, [_seg(0.0, 2.0, -0.2, 0.05)])
    assert words[0].segment_avg_logprob is None
    assert words[0].segment_no_speech_prob is None


def test_segment_mapping_boundary_midpoints_inclusive():
    seg_a = _seg(0.0, 2.0, -0.2, 0.05)
    seg_b = _seg(2.0, 4.0, -0.9, 0.6)
    # Midpoint exactly 0.0 (= seg_a.start) and exactly 4.0 (= seg_b.end): the
    # closed interval includes both edges.
    lead, tail = _w(-0.2, 0.2), _w(3.8, 4.2)
    tr._apply_segment_signals([lead, tail], [seg_a, seg_b])
    assert lead.segment_avg_logprob == -0.2
    assert tail.segment_avg_logprob == -0.9


def test_segment_mapping_shared_boundary_first_segment_wins():
    seg_a = _seg(0.0, 2.0, -0.2, 0.05)
    seg_b = _seg(2.0, 4.0, -0.9, 0.6)
    word = _w(1.9, 2.1)  # midpoint exactly 2.0 — on seg_a.end AND seg_b.start
    tr._apply_segment_signals([word], [seg_a, seg_b])
    assert word.segment_avg_logprob == -0.2


def test_openai_maps_segment_signals_from_verbose_json(_audio, monkeypatch):
    resp = _FakeResp(
        words=[
            SimpleNamespace(word="hi", start=0.5, end=0.9),
            SimpleNamespace(word="um", start=2.2, end=2.6),
            SimpleNamespace(word="tail", start=9.0, end=9.4),  # outside all segments
        ],
        segments=[_seg(0.0, 2.0, -0.25, 0.02), _seg(2.0, 4.0, -1.1, 0.7)],
        text="hi um tail",
    )
    _fake_openai(monkeypatch, {}, resp)
    t = tr._transcribe_openai(_audio)
    assert (t.words[0].segment_avg_logprob, t.words[0].segment_no_speech_prob) == (-0.25, 0.02)
    assert (t.words[1].segment_avg_logprob, t.words[1].segment_no_speech_prob) == (-1.1, 0.7)
    assert t.words[2].segment_avg_logprob is None
    assert t.words[2].segment_no_speech_prob is None
    # whisper-1 still returns no per-word confidence — the 1.0 hardcode stays.
    assert all(w.confidence == 1.0 for w in t.words)


def test_openai_response_without_segments_keeps_none(_audio, monkeypatch):
    resp = _FakeResp(words=[SimpleNamespace(word="hi", start=0.5, end=0.9)], text="hi")
    _fake_openai(monkeypatch, {}, resp)
    t = tr._transcribe_openai(_audio)
    assert t.words[0].segment_avg_logprob is None
    assert t.words[0].segment_no_speech_prob is None


def test_local_words_carry_segment_signals(_audio, monkeypatch):
    segments = [
        SimpleNamespace(
            text=" hi there",
            start=0.0,
            end=2.0,
            avg_logprob=-0.3,
            no_speech_prob=0.05,
            words=[
                SimpleNamespace(word=" hi", start=0.5, end=0.9, probability=0.9),
                SimpleNamespace(word=" there", start=1.0, end=1.4, probability=0.8),
            ],
        ),
        SimpleNamespace(
            text=" um",
            start=2.0,
            end=3.0,
            avg_logprob=-1.4,
            no_speech_prob=0.8,
            words=[SimpleNamespace(word=" um", start=2.2, end=2.6, probability=0.4)],
        ),
    ]
    _install_fake_faster_whisper(monkeypatch, segments)
    t = tr._transcribe_local(_audio)
    assert (t.words[0].segment_avg_logprob, t.words[0].segment_no_speech_prob) == (-0.3, 0.05)
    assert (t.words[1].segment_avg_logprob, t.words[1].segment_no_speech_prob) == (-0.3, 0.05)
    assert (t.words[2].segment_avg_logprob, t.words[2].segment_no_speech_prob) == (-1.4, 0.8)
    assert t.words[2].confidence == 0.4  # local per-word probability unchanged


# ── Word dataclass backward-compat ────────────────────────────────────────────


def test_word_positional_construction_backward_compat():
    w = Word("hi", 0.0, 1.0, 0.9)
    assert (w.text, w.start_s, w.end_s, w.confidence) == ("hi", 0.0, 1.0, 0.9)
    assert w.segment_avg_logprob is None
    assert w.segment_no_speech_prob is None


def test_word_keyword_construction_backward_compat():
    w = Word(text="hi", start_s=0.0, end_s=1.0, confidence=0.9)
    assert w.segment_avg_logprob is None
    assert w.segment_no_speech_prob is None
    full = Word(
        text="hi",
        start_s=0.0,
        end_s=1.0,
        confidence=0.9,
        segment_avg_logprob=-0.5,
        segment_no_speech_prob=0.1,
    )
    assert full.segment_avg_logprob == -0.5
    assert full.segment_no_speech_prob == 0.1
