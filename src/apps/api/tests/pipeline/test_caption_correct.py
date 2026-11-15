from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import app.pipeline.caption_correct as cc

pytestmark = pytest.mark.journey("kri-708-caption-correction")

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
    # KRI-838: "es" used to stay a raw code here (the table was tr/en); it is "Spanish" now
    # (test_the_correction_prompt_names_every_caption_language). A language outside the
    # shared table still stays as written.
    assert cc._language_name("ru") == "ru"
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


# ── Workstream 3a downstream audit ────────────────────────────────────────────
# Punctuation now rides the timed word (e.g. a trailing comma from
# align_punctuated_text). This locks in that the trusted-alias matcher's
# existing punctuation-stripping in `_fold` already handles it — the plan's
# call-out is that this job's aliases "worked only because tokens were bare",
# so it must keep working once tokens carry punctuation.

_PUNCTUATED_CUES = [
    {
        "text": "usermagick is great",
        "start_s": 0.0,
        "end_s": 1.0,
        "words": [
            {"word_id": "p-1", "text": "UserMagick,", "start_s": 0.0, "end_s": 0.4},
            {"word_id": "p-2", "text": "is", "start_s": 0.4, "end_s": 0.6},
            {"word_id": "p-3", "text": "great.", "start_s": 0.6, "end_s": 1.0},
        ],
    }
]


def test_fold_strips_punctuation_riding_the_word() -> None:
    assert cc._fold("UserMagick,") == cc._fold("UserMagick")
    assert cc._fold("great.") == cc._fold("great")


def test_trusted_alias_match_survives_punctuation_riding_the_word(monkeypatch) -> None:
    proposal = {
        "line_index": 0,
        "start_word_index": 0,
        "end_word_index": 0,
        "original_span": "UserMagick",
        "target_alias": "Usersmagic",
    }
    monkeypatch.setattr(cc, "_llm_propose_substitutions", lambda *a, **k: [proposal])
    out = cc.correct_caption_cues(_PUNCTUATED_CUES, "en", trusted_aliases=["Usersmagic"])
    assert out[0]["words"][0]["text"] == "Usersmagic"
    assert out[0]["text"] == "Usersmagic is great."


def _timed(text: str) -> dict:
    tokens = text.split()
    return {
        "text": text,
        "start_s": 0.0,
        "end_s": float(len(tokens)),
        "words": [
            {"text": t, "start_s": float(i), "end_s": float(i + 1)} for i, t in enumerate(tokens)
        ],
    }


def test_kri708_rewording_keeps_spoken_order_and_word_timings(monkeypatch) -> None:
    spoken = [
        "Kim, Atakan'ı alacağız değil mi şimdi?",
        "Mesajlaştın mı hiç?",
        "Arabası hararet yapmıştı ama geliyormuş.",
        "Kaçer adet yapıyoruz",
    ]
    monkeypatch.setattr(
        cc,
        "_llm_correct_lines",
        lambda texts, language, *, model: [
            "Şimdi Atakan'ı alacağız değil mi?",
            "Mesaj attın mı hiç?",
            "Araba hararet yapmıştı ama geliyormuş.",
            "Kaçar adet yapıyoruz",
        ],
    )
    cues = [_timed(t) for t in spoken]
    out = cc.correct_caption_cues(cues, "tr")
    assert all(cue.get("words") for cue in out)
    # Reordered / reworded cues keep the spoken text and original timings.
    assert [c["text"] for c in out[:2]] == spoken[:2]
    assert out[:2] == cues[:2]
    # A spelling-level fix keeps timings too.
    assert [w["text"] for w in out[3]["words"]] == ["Kaçar", "adet", "yapıyoruz"]
    assert [(w["start_s"], w["end_s"]) for w in out[3]["words"]] == [
        (w["start_s"], w["end_s"]) for w in cues[3]["words"]
    ]
    for cue in out:
        assert " ".join(w["text"] for w in cue["words"]) == cue["text"]


def test_the_correction_prompt_names_every_caption_language():
    """KRI-838: a translated Spanish caption set is corrected as "Spanish", not as "es"."""
    assert cc._language_name("es") == "Spanish"
    assert cc._language_name("pt-BR") == "pt-br"  # an unlisted tag stays as written
    assert cc._language_name("tr") == "Turkish"
    assert cc._language_name("EN") == "English"
    assert cc._language_name("xx") == "xx"
    assert cc._language_name(None) == "the spoken language"


# ── KRI-1263: a good spelling fix must land, a bad one must not take the line with it ─────────
# Failure modes the word alignment has to survive (see the PR body):
#   timing drift, hallucinated rewrites, Turkish case/suffix, names/brands, numbers/emoji,
#   split/merge at line edges, a refused change leaking onto its neighbours, trace counts.


def _spoken(text: str) -> dict:
    return _timed(text)


def _correct(monkeypatch, spoken: list[str], proposed: list[str], language: str = "en"):
    monkeypatch.setattr(cc, "_llm_correct_lines", lambda texts, lang, *, model: proposed)
    return cc.correct_caption_cues([_spoken(t) for t in spoken], language)


def _spans(cue: dict) -> list[tuple[float, float]]:
    return [(w["start_s"], w["end_s"]) for w in cue["words"]]


def _assert_timeline_intact(before: dict, after: dict) -> None:
    spans = _spans(after)
    assert spans[0][0] == before["words"][0]["start_s"]
    assert spans[-1][1] == before["words"][-1]["end_s"]
    assert all(start <= end for start, end in spans)
    assert all(a[1] <= b[0] + 1e-9 for a, b in zip(spans, spans[1:]))
    assert " ".join(w["text"] for w in after["words"]) == after["text"]


def test_kri1263_misspellings_the_old_filter_dropped_now_land(monkeypatch) -> None:
    out = _correct(
        monkeypatch,
        ["I have alot of time", "your going home", "teh dog barks"],
        ["I have a lot of time", "you're going home", "the dog barks"],
    )
    assert [c["text"] for c in out] == [
        "I have a lot of time",
        "you're going home",
        "the dog barks",
    ]


def test_kri1263_edited_word_keeps_its_timing(monkeypatch) -> None:
    before = _spoken("your going home")
    [after] = _correct(monkeypatch, ["your going home"], ["you're going home"])
    assert after["text"] == "you're going home"
    assert _spans(after) == _spans(before)
    _assert_timeline_intact(before, after)


def test_kri1263_split_word_shares_its_source_span(monkeypatch) -> None:
    before = _spoken("I have alot of time")
    [after] = _correct(monkeypatch, [before["text"]], ["I have a lot of time"])
    assert [w["text"] for w in after["words"]] == ["I", "have", "a", "lot", "of", "time"]
    a, lot = after["words"][2], after["words"][3]
    assert (a["start_s"], lot["end_s"]) == (2.0, 3.0)
    assert a["end_s"] == lot["start_s"]
    assert _spans(after)[:2] + _spans(after)[4:] == [(0.0, 1.0), (1.0, 2.0), (3.0, 4.0), (4.0, 5.0)]
    _assert_timeline_intact(before, after)


def test_kri1263_merged_words_span_both(monkeypatch) -> None:
    before = _spoken("go in to the room")
    [after] = _correct(monkeypatch, [before["text"]], ["go into the room"])
    assert [w["text"] for w in after["words"]] == ["go", "into", "the", "room"]
    assert (after["words"][1]["start_s"], after["words"][1]["end_s"]) == (1.0, 3.0)
    _assert_timeline_intact(before, after)


def test_kri1263_split_and_merge_at_the_line_edges(monkeypatch) -> None:
    first = _spoken("alot happened")
    last = _spoken("it was alot")
    tail_merge = _spoken("we went to day light")
    out = _correct(
        monkeypatch,
        [first["text"], last["text"], tail_merge["text"]],
        ["a lot happened", "it was a lot", "we went to daylight"],
    )
    assert [c["text"] for c in out] == ["a lot happened", "it was a lot", "we went to daylight"]
    for before, after in zip((first, last, tail_merge), out):
        _assert_timeline_intact(before, after)
    assert out[2]["words"][-1]["end_s"] == 5.0


def test_kri1263_punctuation_riding_the_word_survives_a_split(monkeypatch) -> None:
    [after] = _correct(monkeypatch, ["it was alot, honestly."], ["it was a lot, honestly."])
    assert [w["text"] for w in after["words"]] == ["it", "was", "a", "lot,", "honestly."]


def test_kri1263_a_rewording_is_refused_but_a_clean_fix_in_the_same_line_lands(
    monkeypatch,
) -> None:
    [after] = _correct(
        monkeypatch,
        ["I think thier plan is fine"],
        ["I believe their plan is fine"],
    )
    assert after["text"] == "I think their plan is fine"


def test_kri1263_hallucinated_rewrites_keep_the_spoken_line(monkeypatch) -> None:
    spoken = [
        "so good",
        "the cat sat down",
        "we went there today",
        "Arabası hararet yapmıştı",
        "one two three four five",
    ]
    proposed = [
        "so very good very much",
        "the dog sat down",
        "today we went there",
        "Araba hararet yapmıştı",
        "one two",
    ]
    out = _correct(monkeypatch, spoken, proposed)
    assert [c["text"] for c in out] == spoken


def test_kri1263_a_line_whose_word_count_moves_by_more_than_two_is_kept_whole(
    monkeypatch,
) -> None:
    before = _spoken("alot of time")
    [after] = _correct(monkeypatch, [before["text"]], ["a lot of time and then some more"])
    assert after == before


def test_kri1263_numbers_never_change(monkeypatch) -> None:
    out = _correct(
        monkeypatch,
        ["we have 10 dogs", "meet at 5 pm", "it cost 15 dollars"],
        ["we have 11 dogs", "meet at 6 pm", "it cost fifteen dollars"],
    )
    assert [c["text"] for c in out] == ["we have 10 dogs", "meet at 5 pm", "it cost 15 dollars"]


@pytest.mark.parametrize(
    ("spoken_number", "proposed_number"),
    [
        ("1.5", "15"),
        ("15", "1.5"),
        (".5", "5"),
        ("1,5", "15"),
        ("1,000", "1000"),
        ("1.000,5", "10005"),
        ("-5", "5"),
        ("−5", "5"),
        ("+5", "-5"),
        ("1e-5", "1e5"),
        ("1/2", "12"),
        ("1:30", "130"),
        ("5%", "5"),
        ("$5", "5"),
        ("(5)", "5"),
        ("10²", "102"),
        ("1.5,", "15."),
        ("1.5", "1.5,"),
        ("14", "¼"),
        ("12", "½"),
        ("13", "⅓"),
        ("¼", "14"),
        ("¼", "½"),
        ("$¼", "$½"),
        ("Ⅳ", "Ⅵ"),
        ("xⅣ", "xⅥ"),
        ("四", "五"),
        ("x四", "x五"),
        ("㎟", "mm2"),
        ("mm2", "㎟"),
        ("㎟", "㎠"),
        ("x㎟", "x㎠"),
        ("㋀", "1月"),
        ("㍙", "1点"),
        ("1日", "㏠"),
        ("⼀", "一"),
        ("①", "1"),
        ("½kg", "½ kg"),
        ("½ kg", "½kg"),
        ("Ⅳkg", "Ⅳ kg"),
        ("Ⅳ kg", "Ⅳkg"),
        ("㎟x", "㎟ x"),
        ("㎟ x", "㎟x"),
    ],
)
def test_kri1263_numeric_tokens_keep_their_exact_spoken_form(
    monkeypatch, spoken_number: str, proposed_number: str
) -> None:
    # Numeric punctuation can carry value, sign, units or locale. Do not guess
    # which parts the proofreader may remove, even for a trailing comma.
    spoken = f"it cost {spoken_number} dollars"
    [after] = _correct(monkeypatch, [spoken], [f"it cost {proposed_number} dollars"])
    assert after == _spoken(spoken)


def test_kri1263_refused_numeric_edit_keeps_other_spelling_and_punctuation(monkeypatch) -> None:
    spoken = "well it cost -1.5 dolars again today"
    [after] = _correct(monkeypatch, [spoken], ["Well, it cost 15 dollars again today."])
    assert after["text"] == "Well, it cost -1.5 dollars again today."
    assert _spans(after) == _spans(_spoken(spoken))
    _assert_timeline_intact(_spoken(spoken), after)


@pytest.mark.parametrize("number", ["1.5", "½", "Ⅳ", "㎡"])
def test_kri1263_unchanged_number_allows_surrounding_punctuation(monkeypatch, number) -> None:
    spoken = f"well it cost {number} dollars today"
    proposed = f"Well, it cost {number} dollars today."
    [after] = _correct(monkeypatch, [spoken], [proposed])
    assert after["text"] == proposed
    assert _spans(after) == _spans(_spoken(spoken))


def test_kri1263_emoji_are_kept_and_never_swapped(monkeypatch) -> None:
    out = _correct(
        monkeypatch,
        ["lets go 🔥 now", "teh fire 🔥 burns"],
        ["lets go 🎉 now", "the fire 🔥 burns"],
    )
    assert out[0]["text"] == "lets go 🔥 now"
    assert out[1]["text"] == "the fire 🔥 burns"


def test_kri1263_turkish_case_and_vowel_fixes_land_but_a_dropped_suffix_does_not(
    monkeypatch,
) -> None:
    out = _correct(
        monkeypatch,
        ["Bu nereye çalıştırıyor", "Kaçer adet yapıyoruz", "arabaya bindi", "istanbul güzel"],
        ["Bu nereyi çalıştırıyor", "Kaçar adet yapıyoruz", "araba bindi", "İstanbul güzel"],
        language="tr",
    )
    assert [c["text"] for c in out] == [
        "Bu nereyi çalıştırıyor",
        "Kaçar adet yapıyoruz",
        "arabaya bindi",
        "İstanbul güzel",
    ]


def test_kri1263_a_brand_swapped_for_an_unrelated_name_is_refused(monkeypatch) -> None:
    out = _correct(
        monkeypatch,
        ["we love Nutella here", "send it to Gonzalo"],
        ["we love Samsung here", "send it to Gonzales"],
    )
    assert out[0]["text"] == "we love Nutella here"
    assert out[1]["text"] == "send it to Gonzales"


def test_kri1263_a_refused_change_does_not_move_punctuation_onto_neighbours(monkeypatch) -> None:
    [after] = _correct(
        monkeypatch,
        ["Kim, Atakan'ı alacağız değil mi şimdi?"],
        ["Şimdi Atakan'ı alacağız değil mi?"],
        language="tr",
    )
    assert after["text"] == "Kim, Atakan'ı alacağız değil mi şimdi?"


def test_kri1263_trace_carries_accepted_and_reverted_counts(monkeypatch) -> None:
    events: list[tuple] = []
    import app.services.pipeline_trace as trace

    monkeypatch.setattr(trace, "record_pipeline_event", lambda *a: events.append(a))
    _correct(
        monkeypatch,
        ["I have alot of time", "so good", "I think thier plan"],
        ["I have a lot of time", "so very good very much", "I believe their plan"],
    )
    [(stage, name, payload)] = events
    assert (stage, name) == ("captions", "caption_correction")
    assert payload["accepted_words"] == 2
    assert payload["reverted_words"] == 4
    assert payload["reverted_lines"] == 1
    assert [e["before"] for e in payload["reverted_examples"]] == ["so good", "I think thier plan"]


def test_kri1263_untimed_cues_still_take_the_corrected_text(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_llm_correct_lines", lambda texts, lang, *, model: ["a lot"])
    [after] = cc.correct_caption_cues([{"text": "alot", "start_s": 0.0, "end_s": 1.0}], "en")
    assert after["text"] == "a lot"


def test_kri1263_punctuation_far_from_a_refused_insertion_still_lands(monkeypatch) -> None:
    [after] = _correct(
        monkeypatch,
        ["Caro can you describe it two sentences"],
        ["Caro, can you describe it in two sentences"],
    )
    assert after["text"] == "Caro, can you describe it two sentences"
