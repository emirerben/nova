"""KRI-838 replay: a requested caption language is verified, translated, or honestly refused.

The incident (café T2, Don Julio T3): the creator asked for captions in another language than
the one spoken. Nova hinted whisper with the language and stamped the REQUEST on the captions,
whatever text came back. A whisper language hint does not reliably translate (local whisper
medium: a Turkish hint on English speech wrote the English again), and a Spanish ask was
dropped silently (the dispatcher only knew en/tr) after the draft had promised Spanish. The
review then believed the label.

Done when (KRI-838): (1) a Spanish caption request gives Spanish captions, or an honest
"can't" BEFORE approval, never a promise then English; (2) captions labelled `tr` whose text is
English are not "met".

Failure modes, written before the code (each is a test below or in the unit modules named):

 1. Café T2 Turkish ask on English speech: the hinted pass is English -> translated (Turkish
    cues, stamped `tr`), and the render-ready review of the ask is "met".          [a]
 2. Same, but the translator is off or hands the English back -> the variant is stamped `en`,
    `caption_language_request_unmet` says why, and the review is "partial": "The captions are
    in English, not Turkish." Never "met".                                         [b]
 3. The same on the cleanup-cut branch: only the KEPT words are translated, the cut and
    `raw_words` stay on the spoken words.                                          [a2]
 4. Don Julio T3 Spanish ask: the real prompt parses to `es` (it was dropped), the hinted pass
    is Spanish -> confirmed with NO translator call, stamped `es`, review "met".    [c]
 5. The pre-approval line promises Spanish only when the worker can deliver it; with
    translation off it says "can't" (EN and TR), and en/tr are still promised.      [d]
 6. Provider down / malformed answer / still the wrong language: fail open, never raise
    (tests/pipeline/test_caption_translate.py).
 7. Label vs text: a stale or wrong label never beats clear text
    (tests/kria/test_brief_rendered_speech_facts.py).
 8. Short or ambiguous text is "unknown", not a failure
    (tests/pipeline/test_caption_language_id.py).
 9. Voiceover (`_narrated_caption_transcript`): the same verify/translate/honest stamp. [e]

Whisper (local whisper.cpp medium output, see tests/fixtures/kri838_caption_language/README.md)
and the translator are stubbed; the dispatcher's parser, the real phone Talking job, the cue
builder and the brief checks are the real ones. The Turkish translator answers below are
authored stubs, not recorded model output.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.pipeline.caption_language import infer_language_from_text, parse_caption_language_request
from app.pipeline.transcribe import Transcript, Word
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_generative_build_silence_cut import DURATION
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _raw_preflight_snapshot,
    _setup_subtitled,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kri838_caption_language"
PROMPTS = json.loads((FIXTURES / "prompts.json").read_text(encoding="utf-8"))
CAFE_T2_PROMPT = PROMPTS["cafe_t2_caro"]
DONJULIO_T3_PROMPT = PROMPTS["donjulio_t3_lucas"]


def _words(name: str) -> list[Word]:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))["words"]
    return [Word(text=w["text"], start_s=w["start"], end_s=w["end"], confidence=0.9) for w in raw]


def _transcript(words: list[Word], language: str) -> Transcript:
    return Transcript(words=words, language=language, full_text=" ".join(w.text for w in words))


# whisper, per language hint. Forced Turkish on Caro's English speech writes English again, and
# whisper-1 reports the hinted language as the transcript's language -- the label lies too.
CARO_FORCED_TR = _transcript(_words("caro_t2_forced_tr_words.json"), "tr")
LUCAS_FORCED_ES = _transcript(_words("lucas_t3_forced_es_words.json"), "es")
# KRI-839: the phone Talking job always runs a SPOKEN (auto-language) pass first; the hinted
# pass is a second call. Caro auto-detects as English; Lucas's auto pass is whisper.cpp's real
# Finnish-looking hallucination (the mixed take: an English question, then Spanish).
CARO_SPOKEN_EN = _transcript(_words("caro_t2_auto_en_words.json"), "en")
LUCAS_SPOKEN_FI = _transcript(_words("lucas_t3_auto_words.json"), "fi")

# Authored stub of a Turkish translator answer for Caro's six caption sentences, keyed by the
# English line it must receive (so the test also pins the sentence chunking and that the
# model is handed the English, not Turkish).
CARO_TR_BY_LINE = {
    "Hi, I'm Carolina from Ecuador and this is the Oblaya de la Casa.": (
        "Merhaba, ben Ekvador'dan Carolina ve bu Oblea de la Casa."
    ),
    "It's a traditional Colombian dessert.": "Geleneksel bir Kolombiya tatlısı.",
    "It's waffle cookie with bread, no, with blackberry jam, cheese, dulce de leche,": (
        "Gofret bisküvi, ekmek, hayır, böğürtlen reçeli, peynir, dulce de leche,"
    ),
    "leche condensada and whipped cream.": "leche condensada ve krem şanti ile yapılıyor.",
    "Alright, delicious.": "Çok güzel, lezzetli.",
    "That's it.": "Hepsi bu kadar.",
}


def _caro_translator(lines, **_kwargs):
    return [CARO_TR_BY_LINE[line] for line in lines]


@pytest.fixture
def translator(monkeypatch) -> Mock:
    """The provider call, stubbed; the autouse conftest guard would raise otherwise."""
    mock = Mock(side_effect=_caro_translator)
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", mock)
    return mock


@pytest.fixture
def events(monkeypatch) -> Mock:
    from app.services import pipeline_trace

    recorded = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", recorded)
    return recorded


def _event(events: Mock, name: str) -> list[dict]:
    return [call.args[2] for call in events.call_args_list if call.args[1] == name]


def _whisper_by_hint(monkeypatch, by_hint: dict) -> Mock:
    mock = Mock(side_effect=lambda _path, language=None, **_kw: by_hint[language])
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)
    return mock


def _render(
    monkeypatch, *, prompt: str, by_hint: dict, snapshot_setup=None, gemini_transcript: str = ""
):
    """Run the real phone Talking job for a creator prompt; return (job, variant, whisper).

    ``gemini_transcript`` is what Gemini heard in the clip (``ClipMeta.transcript``): the
    only language evidence that does not come from the hinted whisper pass."""
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=DURATION)
    if gemini_transcript:
        meta = _Meta("c0", 5.0, transcript=gemini_transcript)
        monkeypatch.setattr(
            gb,
            "_ingest_clips",
            lambda *a, **k: {
                "clip_metas": [meta],
                "clip_id_to_gcs": {"c0": binding.proxy_path},
                "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
                "probe_map": {},
                "hero": meta,
            },
            raising=False,
        )
    job.all_candidates["language"] = "en"
    job.all_candidates["creator_request"] = prompt
    # What the dispatcher does with the creator's words (services/generative_jobs.py).
    request = parse_caption_language_request(prompt)
    if request is not None:
        job.all_candidates["caption_language_request"] = request
    whisper = _whisper_by_hint(monkeypatch, by_hint)
    if snapshot_setup is not None:
        snapshot_setup(snapshot, binding)
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    return job, variant, whisper


def _cue_texts(variant: dict) -> list[str]:
    return [cue["text"] for cue in variant["caption_cues"]]


def _review(variant: dict, description: str, **facts):
    """The render-ready receipt of a captions ask against the finished variant."""
    req = BriefRequirement(
        id="c", kind="style", scope="global", description=description, facts=dict(facts)
    )
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


# ------------------------------------------------------------------ [a] café T2, delivered


def test_a_cafe_t2_turkish_ask_on_english_speech_is_translated_and_met(
    monkeypatch, translator, events
):
    assert parse_caption_language_request(CAFE_T2_PROMPT) == "tr"

    _job, variant, whisper = _render(
        monkeypatch,
        prompt=CAFE_T2_PROMPT,
        by_hint={None: CARO_SPOKEN_EN, "tr": CARO_FORCED_TR},
    )

    # the spoken pass, then the Turkish-hinted one (which wrote English again)
    assert [call.kwargs.get("language") for call in whisper.call_args_list] == [None, "tr"]
    texts = _cue_texts(variant)
    joined = " ".join(texts)
    # the captions on screen are Turkish -- the label was never the evidence
    assert infer_language_from_text(joined) == "tr"
    assert "Merhaba" in joined and "Hi, I'm" not in joined
    assert variant["caption_language"] == "tr"
    # the model was handed Caro's English sentences, once
    translator.assert_called_once()
    assert translator.call_args.args[0][0].startswith("Hi, I'm Carolina from Ecuador")
    assert translator.call_args.kwargs["target_language"] == "Turkish"
    [translation] = _event(events, "caption_translation")
    assert translation["requested"] == "tr" and translation["detected"] == "en"
    assert _event(events, "caption_language_request_unmet") == []
    # and the review of the creator's ask reads the cues and says so
    receipt = _review(variant, "altyazılar Türkçe olsun")
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == "The captions are in Turkish."


def test_a2_the_cleanup_branch_translates_only_the_kept_words(monkeypatch, translator, events):
    """`required_v1` cleanup: the cut and the lanes run on the spoken words; the hinted pass
    (English again) is translated after the cut, so a sentence the cut shortened is never
    translated whole."""
    hinted = _transcript(
        [
            Word(text="so", start_s=0.5, end_s=0.7, confidence=1.0),
            Word(text="um,", start_s=1.0, end_s=1.3, confidence=1.0),
            Word(text="today", start_s=1.5, end_s=1.9, confidence=1.0),
            Word(text="we", start_s=2.0, end_s=2.2, confidence=1.0),
            Word(text="built", start_s=4.6, end_s=4.9, confidence=1.0),
            Word(text="the", start_s=5.0, end_s=5.2, confidence=1.0),
            Word(text="thing.", start_s=5.3, end_s=5.9, confidence=1.0),
        ],
        "tr",
    )
    translator.side_effect = lambda lines, **_kw: ["Yani bugün bu şeyi biz yaptık."]

    def cleanup(snapshot, binding):
        snapshot["speech_cleanup_contract"] = "required_v1"
        snapshot["_speech_cleanup_internal"] = {
            "preflight_snapshot": _raw_preflight_snapshot(storage_path=binding.proxy_path)
        }

    _job, variant, whisper = _render(
        monkeypatch,
        prompt="Make the subtitles Turkish please.",
        by_hint={"tr": hinted},
        snapshot_setup=cleanup,
    )

    assert [call.kwargs.get("language") for call in whisper.call_args_list] == ["tr"]
    # the model saw the KEPT words only: the filler and the cut pause are gone
    assert translator.call_args.args[0] == ["so today we built the thing."]
    assert variant["silence_cut_outcome"] == "applied"
    assert " ".join(_cue_texts(variant)).startswith("Yani bugün")
    assert variant["caption_language"] == "tr"
    assert all(cue["end_s"] <= 4.1 for cue in variant["caption_cues"])  # on the cut timeline


# --------------------------------------------------------------- [b] café T2, not delivered


@pytest.mark.parametrize("how", ["flag_off", "english_handed_back", "provider_down"])
def test_b_when_the_translation_does_not_happen_the_variant_says_english_and_the_review_agrees(
    monkeypatch, translator, events, how
):
    if how == "flag_off":
        monkeypatch.setattr(gb.settings, "caption_translation_enabled", False)
        translator.side_effect = AssertionError("the kill switch must stop the call")
    elif how == "english_handed_back":
        translator.side_effect = lambda lines, **_kw: list(lines)
    else:
        translator.side_effect = RuntimeError("provider down")

    _job, variant, _whisper = _render(
        monkeypatch,
        prompt=CAFE_T2_PROMPT,
        by_hint={None: CARO_SPOKEN_EN, "tr": CARO_FORCED_TR},
    )

    joined = " ".join(_cue_texts(variant))
    assert joined.startswith("Hi, I'm Carolina from Ecuador")  # still English: that is honest
    assert variant["caption_language"] == "en"  # NOT the request
    [unmet] = _event(events, "caption_language_request_unmet")
    assert unmet["requested"] == "tr" and unmet["detected"] == "en"
    assert unmet["variant_id"] == "subtitled" and unmet["stamped"] == "en"
    assert (
        unmet["reason"]
        == {
            "flag_off": "translation_disabled",
            "english_handed_back": "translation_not_in_requested_language",
            "provider_down": "translation_failed",
        }[how]
    )
    assert _event(events, "caption_translation") == []
    receipt = _review(variant, "altyazılar Türkçe olsun")
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == "The captions are in English, not Turkish."
    token = bind_reply_language("tr")
    try:
        in_turkish = _review(variant, "altyazılar Türkçe olsun")
    finally:
        release_reply_language(token)
    assert in_turkish.reason == "Altyazılar Türkçe değil, İngilizce olarak çıktı."


def test_b2_english_text_under_a_turkish_label_is_not_met_whoever_stamped_it(monkeypatch):
    """The review alone (today's variants, other writers, a hand edit): label `tr`, English
    lines -> not "met". This is the exact label-only verdict the incident exploited."""
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_language": "tr",
        "voiceover_caption_style": "sentence",
        "caption_cues": [
            {
                "text": "Hi, I'm Carolina from Ecuador and this is the Oblea de la Casa.",
                "start_s": 0.0,
                "end_s": 4.0,
            },
            {"text": "It's a traditional Colombian dessert.", "start_s": 4.0, "end_s": 6.0},
        ],
    }
    receipt = _review(variant, "altyazılar Türkçe olsun")
    assert receipt.status == "partial"
    assert receipt.reason == "The captions are in English, not Turkish."


# ------------------------------------------------------------------- [c] Don Julio T3, Spanish


def test_c_don_julio_t3_spanish_ask_is_heard_confirmed_and_met_without_a_translator(
    monkeypatch, translator, events
):
    # the dispatcher used to drop this (en/tr only): the real prompt now parses
    assert parse_caption_language_request(DONJULIO_T3_PROMPT) == "es"
    translator.side_effect = AssertionError("Spanish captions need no translator")

    _job, variant, whisper = _render(
        monkeypatch,
        prompt=DONJULIO_T3_PROMPT,
        by_hint={None: LUCAS_SPOKEN_FI, "es": LUCAS_FORCED_ES},
    )

    assert [call.kwargs.get("language") for call in whisper.call_args_list] == [None, "es"]
    joined = " ".join(_cue_texts(variant))
    assert "Acá tenemos salsas tradicionales argentinas" in joined
    assert "Tupana" not in joined  # the Finnish-looking spoken pass never reaches the captions
    assert variant["caption_language"] == "es"
    [requested] = _event(events, "caption_language_requested")
    assert requested["requested"] == "es" and requested["spoken"] == "fi"
    translator.assert_not_called()
    assert _event(events, "caption_translation") == []
    assert _event(events, "caption_language_request_unmet") == []
    receipt = _review(variant, "make the subtitles Spanish", captions="on", caption_language="es")
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == "The captions are in Spanish."
    # the bare wording alone (no structured fact) is caught too
    assert _review(variant, "make the subtitles Spanish").reason == "The captions are in Spanish."


def test_c2_a_spanish_ask_on_english_speech_is_translated_when_the_switch_is_on(
    monkeypatch, translator
):
    """Not the Lucas clip: Spanish asked of English speech, whisper hinted to Spanish writes a
    loose Spanish rendering at best; whatever it writes, English stays English and is
    translated, and the stamp follows the text."""
    translator.side_effect = lambda lines, **_kw: [
        "Hola, soy Carolina de Ecuador y esta es la receta de la casa."
        if line.startswith("Hi")
        else "Es un postre tradicional que hacemos para toda la familia."
        for line in lines
    ]
    _job, variant, _whisper = _render(
        monkeypatch,
        prompt="Spanish subtitles please",
        by_hint={None: CARO_SPOKEN_EN, "es": CARO_FORCED_TR},
    )

    assert variant["caption_language"] == "es"
    assert " ".join(_cue_texts(variant)).startswith("Hola, soy Carolina")


# ------------------------------------------------------------ [d] the pre-approval promise


def _draft_facts() -> PlanFacts:
    facts = plan_facts_from_strategy(
        {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "direction": "native",
            "caption_style": "clean",
            "pacing": "balanced",
            "target_duration_s": 35,
        }
    )
    return dataclasses.replace(
        facts, phone_talking=True, caption_words_at_render=True, take_duration_s=26.0
    )


def _plan_line(description: str, **facts) -> str:
    req = BriefRequirement(
        id="c", kind="style", scope="global", description=description, facts=dict(facts)
    )
    [receipt] = build_receipts([req], _draft_facts(), include_unchecked=True)
    return receipt.reason


def test_d_the_draft_promises_spanish_when_the_worker_can_deliver_it(monkeypatch):
    monkeypatch.setattr(gb.settings, "caption_translation_enabled", True)
    line = (
        "Every voice in your take gets captions, off-camera ones too; "
        "they'll be in Spanish, and I'll check that in the finished video"
    )
    assert _plan_line("Spanish subtitles", captions="on", caption_language="es") == line
    # the bare wording (no structured fact) used to be missed: "I can't check the captions"
    assert _plan_line("make the subtitles Spanish") == line


def test_d_with_translation_off_the_draft_says_it_cannot_before_approval(monkeypatch):
    monkeypatch.setattr(gb.settings, "caption_translation_enabled", False)
    for line in (
        _plan_line("Spanish subtitles", captions="on", caption_language="es"),
        _plan_line("make the subtitles Spanish"),
    ):
        assert line == (
            "Every voice in your take gets captions, off-camera ones too; "
            "I can't make Spanish captions yet, so they'll be in the language that's spoken"
        )
    token = bind_reply_language("tr")
    try:
        turkish = _plan_line("İspanyolca altyazı")
    finally:
        release_reply_language(token)
    assert "Altyazıları şimdilik İspanyolca yapamıyorum, konuşulan dilde olacaklar" in turkish
    assert "they'll be in" not in turkish


def test_d_turkish_and_english_asks_are_still_promised_with_translation_off(monkeypatch):
    monkeypatch.setattr(gb.settings, "caption_translation_enabled", False)
    assert "they'll be in Turkish, and I'll check that in the finished video" in _plan_line(
        "altyazılar Türkçe olsun"
    )
    assert "they'll be in English" in _plan_line("English subtitles")


# ------------------------------------------------------------- [e] voiceover (narrated)


def test_e_voiceover_hinted_pass_is_read_and_translated(monkeypatch, translator, events):
    spoken = _transcript(_words("caro_t2_auto_en_words.json"), "en")
    hinted = CARO_FORCED_TR
    whisper = Mock(return_value=hinted)
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", whisper)

    transcript, language = gb._narrated_caption_transcript(
        "job-1",
        "/tmp/voice.wav",
        spoken,
        caption_lang="en",
        requested_lang="tr",
        variant_id="narrated",
    )

    assert language == "tr"
    assert transcript.language == "tr"
    assert transcript.full_text.startswith("Merhaba, ben Ekvador'dan Carolina")
    assert [w.text for w in transcript.words][:3] == ["Merhaba,", "ben", "Ekvador'dan"]
    assert isinstance(transcript, Transcript) and all(isinstance(w, Word) for w in transcript.words)
    assert _event(events, "caption_translation")[0]["requested"] == "tr"


def test_e_voiceover_with_no_translation_returns_the_english_stamped_english(
    monkeypatch, translator, events
):
    spoken = _transcript(_words("caro_t2_auto_en_words.json"), "en")
    monkeypatch.setattr(
        "app.pipeline.transcribe.transcribe_whisper", Mock(return_value=CARO_FORCED_TR)
    )
    monkeypatch.setattr(gb.settings, "caption_translation_enabled", False)

    transcript, language = gb._narrated_caption_transcript(
        "job-1",
        "/tmp/voice.wav",
        spoken,
        caption_lang="en",
        requested_lang="tr",
        variant_id="narrated",
    )

    assert language == "en"
    assert transcript.full_text.startswith("Hi, I'm Carolina")
    [unmet] = _event(events, "caption_language_request_unmet")
    assert unmet["variant_id"] == "narrated" and unmet["stamped"] == "en"


# ----------------------------------------------------- [f] no independent evidence, no stamp


_WOW_WORDS = [
    Word(text="Wow.", start_s=0.0, end_s=0.5, confidence=1.0),
    Word(text="Amazing.", start_s=0.7, end_s=1.4, confidence=1.0),
]
# A hinted pass reports the language it was told to write, whatever it wrote.
_WOW_HINTED = _transcript(_WOW_WORDS, "tr")
# Ambiguous text: no function words to read a language from.
_AMBIGUOUS_TEXT = "Wow. Amazing."
_GEMINI_HEARD_EN = "Wow. That was amazing. I really love this place and the people here."


def test_f_a_detected_spoken_pass_is_the_stamp_for_ambiguous_unmet_text(
    monkeypatch, translator, events
):
    """Ambiguous hinted text, translator down: the spoken pass heard English, so that is the
    stamp -- not the request, not the job language."""
    translator.side_effect = RuntimeError("provider down")

    _job, variant, whisper = _render(
        monkeypatch,
        prompt="altyazılar Türkçe olsun",
        by_hint={None: _transcript(_WOW_WORDS, "en"), "tr": _WOW_HINTED},
    )

    assert [call.kwargs.get("language") for call in whisper.call_args_list] == [None, "tr"]
    assert " ".join(_cue_texts(variant)) == _AMBIGUOUS_TEXT
    assert variant["caption_language"] == "en"  # the spoken pass, not "tr"
    assert _event(events, "caption_language_request_unmet")[0]["stamped"] == "en"
    receipt = _review(variant, "altyazılar Türkçe olsun")
    assert receipt.status == "partial"
    assert receipt.reason == "The captions are in English, not Turkish."


def test_f_gemini_read_is_used_only_when_the_spoken_pass_fell_back(monkeypatch, translator, events):
    """The spoken pass reported no language and its text says none (the job language is the
    only thing left, and that is a guess): Gemini's independent read is the stamp."""
    translator.side_effect = RuntimeError("provider down")

    _job, variant, _whisper = _render(
        monkeypatch,
        prompt="altyazılar Türkçe olsun",
        by_hint={None: _transcript(_WOW_WORDS, ""), "tr": _WOW_HINTED},
        gemini_transcript=_GEMINI_HEARD_EN,
    )

    assert _event(events, "caption_language_fallback")  # the spoken pass did fall back
    assert variant["caption_language"] == "en"  # Gemini heard English
    receipt = _review(variant, "altyazılar Türkçe olsun")
    assert receipt.reason == "The captions are in English, not Turkish."

    # ...and a spoken pass that DID detect a language outranks Gemini's read (Spanish text, so
    # the EN/TR crosscheck of the spoken pass stays quiet and cannot re-transcribe it).
    translator.side_effect = RuntimeError("provider down")
    _job, other, _whisper = _render(
        monkeypatch,
        prompt="altyazılar Türkçe olsun",
        by_hint={None: _transcript(_WOW_WORDS, "de"), "tr": _WOW_HINTED},
        gemini_transcript="Wow. Qué increíble, me encanta este lugar y la gente de aquí.",
    )
    assert other["caption_language"] == "de"


def test_f_with_no_independent_evidence_the_stamp_is_empty_never_the_requests_label(
    monkeypatch, translator, events
):
    """Neither a detected spoken pass nor Gemini: "" (unknown). Not "tr" (the request) and not
    "en" (the job language the fallback chain ends on); the review says it cannot check."""
    translator.side_effect = RuntimeError("provider down")

    _job, variant, _whisper = _render(
        monkeypatch,
        prompt="altyazılar Türkçe olsun",
        by_hint={None: _transcript(_WOW_WORDS, ""), "tr": _WOW_HINTED},
    )

    assert variant["caption_language"] == ""
    assert " ".join(_cue_texts(variant)) == _AMBIGUOUS_TEXT
    assert _event(events, "caption_language_request_unmet")[0]["stamped"] == ""
    receipt = _review(variant, "altyazılar Türkçe olsun")
    assert receipt.status == "partial"
    assert receipt.verification == "unchecked"
    assert receipt.reason.startswith("I couldn't check which language the captions are in.")


def test_f_an_empty_hinted_pass_is_translated_from_the_spoken_words(
    monkeypatch, translator, events
):
    """KRI-839's helper keeps the spoken words when the hinted pass hears nothing; the
    verification then reads them against the ask and translates (Turkish was asked)."""
    translator.side_effect = lambda lines, **_kw: (
        ["Merhaba dostlar, bugün çok güzel bir gün."] * len(lines)
    )

    _job, variant, _whisper = _render(
        monkeypatch,
        prompt="altyazılar Türkçe olsun",
        by_hint={None: CARO_SPOKEN_EN, "tr": Transcript(words=[], language="tr", full_text="")},
    )

    assert _event(events, "caption_language_request_empty")
    assert _event(events, "caption_translation")
    assert variant["caption_language"] == "tr"
    assert infer_language_from_text(" ".join(_cue_texts(variant))) == "tr"


# ------------------------------------------------- [g] translation off = the legacy en/tr world


def test_g_with_translation_off_the_worker_does_not_hear_a_spanish_ask(
    monkeypatch, translator, events
):
    """The draft says "I can't make Spanish captions yet" (see [d]); the render must agree:
    no Spanish hint, no translator, captions in the spoken language."""
    monkeypatch.setattr(gb.settings, "caption_translation_enabled", False)
    spoken = _transcript(_words("caro_t2_auto_en_words.json"), "en")
    translator.side_effect = AssertionError("never")

    _job, variant, whisper = _render(monkeypatch, prompt=DONJULIO_T3_PROMPT, by_hint={None: spoken})

    assert [call.kwargs.get("language") for call in whisper.call_args_list] == [None]
    assert variant["caption_language"] == "en"
    assert _event(events, "caption_language_requested") == []
    assert _event(events, "caption_language_request_unmet") == []


def test_g_arabic_is_never_promised_and_never_rendered(monkeypatch, translator):
    monkeypatch.setattr(gb.settings, "caption_translation_enabled", True)
    line = _plan_line("Arabic subtitles")
    assert "I can't make Arabic captions yet" in line
    assert "they'll be in Arabic" not in line
    spoken = _transcript(_words("caro_t2_auto_en_words.json"), "en")
    translator.side_effect = AssertionError("never")
    _job, variant, whisper = _render(
        monkeypatch, prompt="Arapça altyazı olsun", by_hint={None: spoken}
    )
    assert [call.kwargs.get("language") for call in whisper.call_args_list] == [None]
    assert variant["caption_language"] == "en"


def test_e_voiceover_with_no_independent_spoken_language_stamps_unknown_not_the_request(
    monkeypatch, translator, events
):
    """The spoken pass reported no language and its text is too short to infer one: an unmet
    ask is stamped "" (unknown), not the language that was asked for."""
    spoken = _transcript(
        [Word(text="Wow.", start_s=0.0, end_s=0.4, confidence=1.0)],
        "",
    )
    hinted = _transcript(
        [
            Word(text="Wow.", start_s=0.0, end_s=0.4, confidence=1.0),
            Word(text="Amazing.", start_s=0.5, end_s=1.0, confidence=1.0),
        ],
        "tr",
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", Mock(return_value=hinted))
    translator.side_effect = RuntimeError("provider down")

    transcript, language = gb._narrated_caption_transcript(
        "job-1",
        "/tmp/voice.wav",
        spoken,
        caption_lang="en",
        requested_lang="tr",
        variant_id="narrated",
    )

    assert language == ""
    assert transcript.full_text == "Wow. Amazing."
