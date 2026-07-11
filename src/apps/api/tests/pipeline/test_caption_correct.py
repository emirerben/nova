"""LLM caption correction — the guards that keep captions synced.

No network: `_llm_correct_lines` is monkeypatched. The load-bearing invariants are the
1:1 line mapping (a length mismatch or failure must NOT desync timing → return originals)
and that a corrected cue drops its stale per-word timings so word-pop re-synthesizes.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import app.pipeline.caption_correct as cc

_CUES = [
    {
        "text": "Kaçer adet yapıyoruz",
        "start_s": 0.0,
        "end_s": 1.0,
        "words": [
            {"text": "Kaçer", "start_s": 0.0, "end_s": 0.5},
            {"text": "adet", "start_s": 0.5, "end_s": 0.7},
            {"text": "yapıyoruz", "start_s": 0.7, "end_s": 1.0},
        ],
    },
    {"text": "Bu nereye çalıştırıyor", "start_s": 1.0, "end_s": 2.0},
]


def test_disabled_returns_input_unchanged(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(cc, "_llm_correct_lines", lambda *a, **k: called.__setitem__("n", 1))
    out = cc.correct_caption_cues(_CUES, "tr", enabled=False)
    assert out is _CUES and called["n"] == 0  # no LLM call at all


def test_empty_cues_short_circuit(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("should not call the LLM for empty/blank cues")

    monkeypatch.setattr(cc, "_llm_correct_lines", _never)
    assert cc.correct_caption_cues([], "tr") == []
    assert cc.correct_caption_cues([{"text": "  ", "start_s": 0, "end_s": 1}], "tr") == [
        {"text": "  ", "start_s": 0, "end_s": 1}
    ]


def test_corrects_text_preserves_timing_and_drops_stale_words(monkeypatch):
    monkeypatch.setattr(
        cc,
        "_llm_correct_lines",
        lambda texts, lang, *, model: ["Kaçar adet yapıyoruz", "Bu nereyi çalıştırıyor"],
    )
    out = cc.correct_caption_cues(_CUES, "tr")
    assert [c["text"] for c in out] == ["Kaçar adet yapıyoruz", "Bu nereyi çalıştırıyor"]
    # timing untouched
    assert (out[0]["start_s"], out[0]["end_s"]) == (0.0, 1.0)
    assert (out[1]["start_s"], out[1]["end_s"]) == (1.0, 2.0)
    # changed cue dropped its stale per-word timings → word-pop re-synthesizes (E3)
    assert "words" not in out[0]


def test_unchanged_cue_keeps_its_words(monkeypatch):
    # LLM returns the same text for cue 0 → its real word timings must survive.
    monkeypatch.setattr(
        cc,
        "_llm_correct_lines",
        lambda texts, lang, *, model: ["Kaçer adet yapıyoruz", "Bu nereyi çalıştırıyor"],
    )
    out = cc.correct_caption_cues(_CUES, "tr")
    assert "words" in out[0] and len(out[0]["words"]) == 3


def test_length_mismatch_returns_originals(monkeypatch):
    # A model that merges 2 lines into 1 would desync every downstream timestamp → discard.
    monkeypatch.setattr(cc, "_llm_correct_lines", lambda *a, **k: ["only one line"])
    out = cc.correct_caption_cues(_CUES, "tr")
    assert [c["text"] for c in out] == [c["text"] for c in _CUES]  # untouched


def test_api_failure_returns_originals(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("openai 500")

    monkeypatch.setattr(cc, "_llm_correct_lines", _boom)
    out = cc.correct_caption_cues(_CUES, "tr")
    assert [c["text"] for c in out] == [c["text"] for c in _CUES]


def test_language_name_mapping():
    assert cc._language_name("tr") == "Turkish"
    assert cc._language_name("en") == "English"
    assert cc._language_name("es") == "es"


def test_llm_prompt_includes_brand_name_hint(monkeypatch):
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"lines":["Coca-Cola ayıcık"]}')
                    )
                ]
            )

    class _Client:
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda api_key: _Client()))
    monkeypatch.setattr(cc.settings, "openai_api_key", "test-key", raising=False)

    assert cc._llm_correct_lines(["Kokokolu ayıcık"], "tr", model="gpt-4o") == ["Coca-Cola ayıcık"]
    system_prompt = captured["messages"][0]["content"]
    assert "Restore mangled brand names, product names, and proper nouns" in system_prompt
    assert "Kokokolu" in system_prompt
    assert "Coca-Cola" in system_prompt
