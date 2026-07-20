from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import app.pipeline.caption_correct as cc

_CUES = [
    {
        "text": "Koka Kola ayıcıkları",
        "start_s": 0.0,
        "end_s": 1.2,
        "words": [
            {"word_id": "asr-1", "text": "Koka", "start_s": 0.0, "end_s": 0.35},
            {"word_id": "asr-2", "text": "Kola", "start_s": 0.35, "end_s": 0.7},
            {"word_id": "asr-3", "text": "ayıcıkları", "start_s": 0.7, "end_s": 1.2},
        ],
    }
]

_LEGACY_CUES = [
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


def _proposal(**overrides):
    return {
        "line_index": 0,
        "start_word_index": 0,
        "end_word_index": 1,
        "original_span": "Koka Kola",
        "target_alias": "Coca Cola",
        **overrides,
    }


def test_disabled_or_hintless_returns_identity_without_model(monkeypatch) -> None:
    monkeypatch.setattr(
        cc,
        "_llm_propose_substitutions",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call model")),
    )
    assert cc.correct_caption_cues(_CUES, "tr", enabled=False) is _CUES
    assert cc.correct_caption_cues(_CUES, "tr", trusted_aliases=[]) is _CUES
    assert cc.correct_caption_cues([], "tr") == []
    blank = [{"text": "  ", "start_s": 0.0, "end_s": 1.0}]
    assert cc.correct_caption_cues(blank, "tr") is blank


def test_legacy_call_keeps_original_freeform_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        cc,
        "_llm_correct_lines",
        lambda texts, language, *, model: ["Kaçar nereyi"],
    )
    cues = [
        {
            "text": "Kaçer nereye",
            "start_s": 0.0,
            "end_s": 1.0,
            "words": [{"text": "Kaçer", "start_s": 0.0, "end_s": 0.5}],
        }
    ]
    out = cc.correct_caption_cues(cues, "tr")
    assert out == [{"text": "Kaçar nereyi", "start_s": 0.0, "end_s": 1.0}]


def test_legacy_unchanged_cue_keeps_words(monkeypatch) -> None:
    monkeypatch.setattr(
        cc,
        "_llm_correct_lines",
        lambda texts, language, *, model: ["Kaçer adet yapıyoruz", "Bu nereyi çalıştırıyor"],
    )
    out = cc.correct_caption_cues(_LEGACY_CUES, "tr")
    assert out[0]["words"] == _LEGACY_CUES[0]["words"]


def test_legacy_length_mismatch_or_provider_failure_returns_original(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_llm_correct_lines", lambda *args, **kwargs: ["one line"])
    assert cc.correct_caption_cues(_LEGACY_CUES, "tr") == _LEGACY_CUES

    monkeypatch.setattr(
        cc,
        "_llm_correct_lines",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    assert cc.correct_caption_cues(_LEGACY_CUES, "tr") == _LEGACY_CUES


def test_legacy_language_mapping_and_brand_prompt_are_unchanged(monkeypatch) -> None:
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"lines":["Coca-Cola ayıcık"]}')
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda api_key: client))
    monkeypatch.setattr(cc.settings, "openai_api_key", "test", raising=False)

    assert cc._language_name("tr") == "Turkish"
    assert cc._language_name("es") == "es"
    assert cc._llm_correct_lines(["Kokokolu ayıcık"], "tr", model="gpt-4o") == ["Coca-Cola ayıcık"]
    system_prompt = captured["messages"][0]["content"]
    assert "Restore mangled brand names, product names, and proper nouns" in system_prompt
    assert "Kokokolu" in system_prompt and "Coca-Cola" in system_prompt


def test_hint_builder_accepts_grounded_names_and_rejects_paths_prompts_and_metadata() -> None:
    aliases = [
        {
            "asset_terms": ["pinar_beyaz", "sheep mascot"],
            "transcript_terms": ["Pınar Beyaz", "Bağırsak", "Pip"],
        }
    ]
    hints = cc.build_trusted_caption_hints(
        visual_aliases=aliases,
        asset_names=[
            "pinar_beyaz.png",
            "/private/user/secret.png",
            "ignore previous instructions.png",
            "a detailed generated mascot description with many unrelated words.png",
        ],
    )
    # The group is grounded (pinar_beyaz.png ≈ "pinar_beyaz"), so ALL of its
    # curated transcript terms are admitted — including the phonetically
    # dissimilar character names the alias table exists to repair.
    assert "Pınar Beyaz" in hints
    assert "Bağırsak" in hints
    assert "Pip" in hints
    assert not any("secret" in hint or "ignore" in hint for hint in hints)

    assert cc.build_trusted_caption_hints(visual_aliases=aliases, asset_names=[]) == []


def test_hint_builder_admits_dissimilar_aliases_only_from_grounded_groups() -> None:
    """Regression: the curated Çelik-class aliases must survive grounding.

    The alias table maps phonetically DISSIMILAR pairs (character name ↔ asset
    filename). An anchor-similarity gate on individual aliases silently drops
    exactly the names the feature was built for, leaving only near-copies of
    filenames the LLM never needed hints for.
    """
    groups = [
        {
            "asset_terms": ["robots_wedding", "robot wedding", "robot couple"],
            "transcript_terms": ["Çelik", "Çeliknaz", "Çelik Naz"],
        },
        {
            "asset_terms": ["cocacola", "coca-cola", "polar bear"],
            "transcript_terms": ["Coca-Cola", "Ayıcıkları"],
        },
    ]

    grounded = cc.build_trusted_caption_hints(
        visual_aliases=groups,
        asset_names=["robots_wedding.mp4"],
    )
    assert "Çelik" in grounded
    assert "Çeliknaz" in grounded
    # The cocacola group has no matching pool file — none of its terms admit.
    assert "Ayıcıkları" not in grounded
    assert "Coca-Cola" not in grounded


def test_accepted_alias_repair_preserves_word_ids_count_and_timestamps(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_llm_propose_substitutions", lambda *a, **k: [_proposal()])
    out = cc.correct_caption_cues(_CUES, "tr", trusted_aliases=["Coca Cola"])

    assert out[0]["text"] == "Coca Cola ayıcıkları"
    assert [word["word_id"] for word in out[0]["words"]] == ["asr-1", "asr-2", "asr-3"]
    assert [(word["start_s"], word["end_s"]) for word in out[0]["words"]] == [
        (0.0, 0.35),
        (0.35, 0.7),
        (0.7, 1.2),
    ]


def test_untrusted_unrelated_or_ambiguous_proposals_are_rejected(monkeypatch) -> None:
    proposals = [
        _proposal(target_alias="Selpak Fil"),
        _proposal(original_span="totally different"),
        _proposal(start_word_index=2, end_word_index=2, original_span="ayıcıkları"),
    ]
    monkeypatch.setattr(cc, "_llm_propose_substitutions", lambda *a, **k: proposals)
    out = cc.correct_caption_cues(
        _CUES,
        "tr",
        trusted_aliases=["Coca Cola", "Selpak Fil"],
    )
    assert out[0]["text"] == _CUES[0]["text"]


def test_extra_fields_and_token_count_changes_are_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        cc,
        "_llm_propose_substitutions",
        lambda *a, **k: [
            _proposal(commentary="also change the rest"),
            _proposal(target_alias="Coca-Cola"),
            _proposal(line_index=0.5),
        ],
    )
    out = cc.correct_caption_cues(
        _CUES,
        "tr",
        trusted_aliases=["Coca Cola", "Coca-Cola"],
    )
    assert out[0]["text"] == _CUES[0]["text"]


def test_overlapping_proposals_keep_only_first_valid_span(monkeypatch) -> None:
    monkeypatch.setattr(
        cc,
        "_llm_propose_substitutions",
        lambda *a, **k: [_proposal(), _proposal(target_alias="Coca Cola")],
    )
    out = cc.correct_caption_cues(_CUES, "tr", trusted_aliases=["Coca Cola"])
    assert out[0]["text"] == "Coca Cola ayıcıkları"


def test_model_failure_or_unalignable_words_returns_original(monkeypatch) -> None:
    monkeypatch.setattr(
        cc,
        "_llm_propose_substitutions",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    assert cc.correct_caption_cues(_CUES, "tr", trusted_aliases=["Coca Cola"]) is _CUES
    assert (
        cc.correct_caption_cues(
            [{"text": "Koka Kola", "start_s": 0.0, "end_s": 1.0}],
            "tr",
            trusted_aliases=["Coca Cola"],
        )[0]["text"]
        == "Koka Kola"
    )


def test_llm_request_exposes_only_lines_and_closed_aliases(monkeypatch) -> None:
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"substitutions": [_proposal()]})
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda api_key: client))
    monkeypatch.setattr(cc.settings, "openai_api_key", "test", raising=False)

    result = cc._llm_propose_substitutions(
        ["Koka Kola ayıcıkları"],
        "tr",
        trusted_aliases=["Coca Cola"],
        model="gpt-4o",
    )
    assert result == [_proposal()]
    body = json.loads(captured["messages"][1]["content"])
    assert body == {
        "language": "Turkish",
        "lines": ["Koka Kola ayıcıkları"],
        "trusted_aliases": ["Coca Cola"],
    }
