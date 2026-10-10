"""KRI-541: captions and speech-cleanup asks judged against a finished phone render.

`plan_facts_from_phone_variant` keeps `editor=True` for the variant's text lanes and sets
`rendered_variant`, plus the edit format, caption style and cleanup outcome the render
recorded. A real editor edit (no `rendered_variant`) keeps answering "can't check".
"""

from __future__ import annotations

import pytest

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    plan_facts_from_editor_payload,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import bind_reply_language, release_reply_language

_CUES = [{"text": "I ran my first marathon", "start_s": 0.0, "end_s": 1.8}]


def _variant(**overrides) -> dict:
    variant = {
        "variant_id": "v1",
        "render_destination": "device",
        "resolved_archetype": "narrated",
        "caption_cues": list(_CUES),
        "voiceover_caption_style": "sentence",
        "silence_cut_outcome": "applied",
        "text_elements": [],
    }
    variant.update(overrides)
    return variant


def _req(kind: str, description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts
    )


# ---------------------------------------------------------------- facts off the variant


def test_narrated_variant_reads_format_captions_and_cleanup():
    facts = plan_facts_from_phone_variant(_variant())

    assert facts.rendered_variant is True
    assert facts.editor is True  # its text lanes are still editor facts
    assert facts.edit_format in NARRATED_EDIT_FORMATS
    assert facts.caption_style == "clean"
    assert facts.speech_cleanup_enabled is True
    assert facts.speech_cleanup_outcome == "applied"


def test_talking_variant_is_subtitled_with_word_captions():
    facts = plan_facts_from_phone_variant(
        _variant(resolved_archetype="subtitled", voiceover_caption_style="word")
    )

    assert facts.edit_format == "subtitled"
    assert facts.caption_style == "karaoke"


@pytest.mark.parametrize(
    ("overrides", "style"),
    [
        ({"caption_cues": None}, "none"),
        ({"caption_cues": []}, "none"),
        ({"caption_cues": None, "render_destination": "cloud"}, None),
        ({"voiceover_caption_style": None}, "clean"),
    ],
)
def test_caption_style_from_the_cues(overrides, style):
    assert plan_facts_from_phone_variant(_variant(**overrides)).caption_style == style


@pytest.mark.parametrize(
    ("overrides", "enabled", "outcome"),
    [
        ({"silence_cut_outcome": "no_change"}, True, "no_change"),
        ({"silence_cut_outcome": None}, False, "not_run"),
        ({"silence_cut_outcome": None, "render_destination": "cloud"}, None, None),
        ({"silence_cut_outcome": "insufficient_source_speech"}, None, None),
    ],
)
def test_cleanup_outcome(overrides, enabled, outcome):
    facts = plan_facts_from_phone_variant(_variant(**overrides))
    assert facts.speech_cleanup_enabled is enabled
    assert facts.speech_cleanup_outcome == outcome


def test_other_archetypes_add_no_speech_facts():
    facts = plan_facts_from_phone_variant(_variant(resolved_archetype="montage"))

    assert facts.rendered_variant is False
    assert facts.edit_format is None
    assert facts.caption_style is None
    assert facts.speech_cleanup_outcome is None


# ---------------------------------------------------------------- captions


def test_clean_captions_style_ask_is_met():
    receipt = check_requirement(
        _req("style", "clean captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "met"
    assert receipt.reason is None


def test_add_captions_text_ask_is_met():
    receipt = check_requirement(
        _req("text", "add captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "met"


def test_word_by_word_ask_against_sentence_captions_is_partial():
    receipt = check_requirement(
        _req("style", "word by word captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "partial"
    assert "full sentences" in receipt.reason


def test_word_by_word_ask_against_word_captions_is_met():
    facts = plan_facts_from_phone_variant(_variant(voiceover_caption_style="word"))
    receipt = check_requirement(_req("style", "word by word captions"), facts)
    assert receipt.status == "met"


def test_no_captions_ask_on_a_captioned_render_says_video_not_draft():
    receipt = check_requirement(
        _req("style", "no captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "partial"
    assert receipt.reason == "Captions are on in this video."


def test_captions_ask_on_an_uncaptioned_render():
    facts = plan_facts_from_phone_variant(_variant(caption_cues=None))
    receipt = check_requirement(_req("style", "clean captions"), facts)
    assert receipt.status == "partial"
    assert receipt.reason == "This video has no captions."


def test_caption_look_ask_stays_unchecked():
    receipt = check_requirement(
        _req("style", "yellow captions at the bottom"), plan_facts_from_phone_variant(_variant())
    )
    req = _req("style", "yellow captions at the bottom")
    assert not is_judged(req, receipt)


# ---------------------------------------------------------------- speech cleanup


def test_applied_cleanup_meets_a_pause_ask():
    receipt = check_requirement(
        _req("audio", "cut the long pauses"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "met"
    assert receipt.reason is None


def test_cleanup_that_found_nothing_is_met_and_says_so():
    facts = plan_facts_from_phone_variant(_variant(silence_cut_outcome="no_change"))
    receipt = check_requirement(_req("audio", "cut the long pauses"), facts)
    assert receipt.status == "met"
    assert "found no long pauses" in receipt.reason


def test_cleanup_that_did_not_run_is_an_honest_partial():
    facts = plan_facts_from_phone_variant(_variant(silence_cut_outcome=None))
    req = _req("audio", "cut the long pauses")
    receipt = check_requirement(req, facts)
    assert receipt.status == "partial"
    assert "didn't run on this video" in receipt.reason
    assert is_judged(req, receipt)
    assert is_format_limit(receipt.reason)


@pytest.mark.parametrize(
    "description",
    [
        "cut long pauses and the restart of the kilometer thirty sentence",
        "cut the pauses and the retake",
    ],
)
def test_a_named_retake_is_left_for_the_editor(description):
    receipt = check_requirement(
        _req("audio", description), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "partial"
    assert receipt.reason.startswith("Speech cleanup cut the long pauses")
    assert "trim that in the editor" in receipt.reason


def test_a_length_in_the_cleanup_ask_follows_the_cut_take():
    req = _req("timing", "cut the long pauses and keep it under 45 s", duration_s=45)
    receipt = check_requirement(req, plan_facts_from_phone_variant(_variant()))
    assert receipt.status == "partial"
    assert "45s" in receipt.reason


def test_unknown_cleanup_outcome_stays_unchecked():
    facts = plan_facts_from_phone_variant(
        _variant(silence_cut_outcome="insufficient_source_speech")
    )
    req = _req("audio", "cut the long pauses")
    assert not is_judged(req, check_requirement(req, facts))


# ---------------------------------------------------------------- editor edits unchanged


def test_a_real_editor_edit_still_cannot_check_captions_or_cleanup():
    """An editor turn's facts carry only on-screen text: even with a speech format and a
    caption style somehow set, nothing about captions or cleanup is claimed."""
    import dataclasses

    facts = dataclasses.replace(
        plan_facts_from_editor_payload({"text_elements": [{"id": "t1", "text": "Hi"}]}),
        edit_format="narrated",
        caption_style="clean",
        speech_cleanup_enabled=True,
    )
    assert facts.editor is True
    assert facts.rendered_variant is False
    captions = _req("text", "add captions")
    cleanup = _req("audio", "cut the long pauses")
    receipts = build_receipts([captions, cleanup], facts, include_unchecked=True)
    assert [r.verification for r in receipts] == ["unchecked", "unchecked"]


def test_a_draft_still_reports_cleanup_at_approval():
    facts = PlanFacts(edit_format="narrated", speech_cleanup_offered=True)
    receipt = check_requirement(_req("audio", "cut the long pauses"), facts)
    assert receipt.status == "partial"
    assert receipt.reason.startswith("Choose Clean up speech")


# ---------------------------------------------------------------- KRI-549 caption words
#
# Prod thread 9b625eec (Kadıköy T3): a Turkish phone Talking edit captioned in Turkish with
# every place name spelled right, yet the render-ready reply said "Doğrulayamadım: altyazılar
# Türkçe olsun; ... (Bu taslaktaki altyazıları henüz kontrol edemiyorum)". The variant's
# `caption_language` and caption lines now judge the language and the names.

_T3_R2 = BriefRequirement(
    id="r2",
    kind="text",
    scope="global",
    facts={},
    literal=None,
    description="altyazılar Türkçe olsun; Moda, Bahariye, Yeldeğirmeni ve Kadıköy doğru yazılsın",
)
# The finished variant's caption lines, verbatim (job 3a6b9ff6).
_T3_LINES = (
    "Selam, bugün sizi Kadıköy'de en sevdiğim 3 kahveciye götürüyorum.",
    "Bakın, ben günde 4 kahve içiyorum, o yüzden bu konuda biraz uzmanım.",
    "İlk durak Moda'da, deniz kenarında küçücük bir yer.",
    "Buranın kahve çekirdeklerini kendileri kavuruyor.",
    "Filtre kahve efsane.",
    "İkinci durak Bahariye'de.",
    "Biraz kalabalık ama kahve fiyatları çok uygun.",
    "Sabah kahve almak için birebir.",
    "Üçüncü ve en sevdiğim yer Yeldeğirmeni'nde.",
    "İçerisi kitaplarla dolu, kahve kokusu sokağa taşıyor.",
    "Ben her hafta sonu buradayım.",
    "Kahve içip kitap okuyorum.",
    "Siz Kadıköy'de en iyi kahve nerede diyorsunuz?",
    "Yorumlara yazın.",
    "Hadi kahve sizden.",
)


def _t3_variant(lines=_T3_LINES, **overrides) -> dict:
    variant = _variant(
        resolved_archetype="subtitled",
        caption_language="tr",
        caption_cues=[
            {"text": text, "start_s": float(i), "end_s": i + 0.9} for i, text in enumerate(lines)
        ],
        silence_cut_outcome=None,
    )
    variant.update(overrides)
    return variant


def _replace_in_lines(old: str, new: str) -> tuple[str, ...]:
    return tuple(line.replace(old, new) for line in _T3_LINES)


def _judged(req: BriefRequirement, variant: dict):
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


def test_the_variant_carries_its_caption_lines_and_language():
    facts = plan_facts_from_phone_variant(_t3_variant())
    assert facts.caption_texts == _T3_LINES
    assert facts.caption_language == "tr"


@pytest.mark.parametrize(
    ("overrides", "texts"),
    [
        ({"caption_cues": None}, ()),  # a device render with no cues shows no captions
        ({"caption_cues": [], "render_destination": "cloud"}, None),  # filled in later
        ({"captions_enabled": False}, None),  # turned off: nothing on screen to read
        ({"resolved_archetype": "montage"}, None),
    ],
)
def test_caption_lines_only_when_the_render_shows_them(overrides, texts):
    assert plan_facts_from_phone_variant(_t3_variant(**overrides)).caption_texts == texts


def test_the_t3_ask_is_met_on_the_real_render():
    receipt = _judged(_T3_R2, _t3_variant())
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == (
        "The captions are in Turkish. Moda, Bahariye, Yeldeğirmeni and Kadıköy are spelled "
        "as you wrote them."
    )


def test_the_t3_reasons_are_turkish_in_a_turkish_chat():
    token = bind_reply_language("tr")
    try:
        met = _judged(_T3_R2, _t3_variant())
        miss = _judged(_T3_R2, _t3_variant(_replace_in_lines("Yeldeğirmeni", "Yeldegirmeni")))
    finally:
        release_reply_language(token)
    assert met.reason == (
        "Altyazılar Türkçe. Moda, Bahariye, Yeldeğirmeni ve Kadıköy yazdığın gibi yazılmış."
    )
    assert miss.reason.startswith(
        'Altyazılarda Yeldeğirmeni yerine "Yeldegirmeni" yazıyor. Bunu editörde düzeltebilirsin.'
    )


def test_captions_in_another_language_are_not_met():
    receipt = _judged(_T3_R2, _t3_variant(caption_language="en"))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason.startswith("The captions are in English, not Turkish.")


@pytest.mark.parametrize(
    ("old", "new", "name"),
    [
        ("Yeldeğirmeni", "Yeldegirmeni", "Yeldeğirmeni"),
        ("Yeldeğirmeni", "yel değirmeni", "Yeldeğirmeni"),
        ("Yeldeğirmeni'nde", "Yeldeğirmeninde", "Yeldeğirmeni"),
        ("Kadıköy", "Kadikoy", "Kadıköy"),
    ],
)
def test_a_name_spelled_another_way_is_an_honest_miss(old, new, name):
    receipt = _judged(_T3_R2, _t3_variant(_replace_in_lines(old, new)))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    shown = new.split("'")[0]
    assert f'The captions write "{shown}" for {name}.' in receipt.reason
    assert "fix that in the editor" in receipt.reason
    # The language and the names that are right are still said.
    assert "The captions are in Turkish." in receipt.reason


def test_one_wrong_spelling_beside_a_right_one_is_still_a_miss():
    lines = list(_T3_LINES)
    lines[12] = lines[12].replace("Kadıköy", "Kadikoy")  # the first one stays right
    receipt = _judged(_T3_R2, _t3_variant(tuple(lines)))
    assert receipt.status == "partial"
    assert '"Kadikoy" for Kadıköy' in receipt.reason


def test_a_name_the_captions_never_show_is_partial_with_its_own_reason():
    lines = tuple(line for line in _T3_LINES if "Bahariye" not in line)
    receipt = _judged(_T3_R2, _t3_variant(lines))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason.startswith(
        "Bahariye doesn't appear in the captions, so I couldn't check its spelling."
    )
    assert "Moda, Yeldeğirmeni and Kadıköy are spelled as you wrote them." in receipt.reason
    assert "can't check the captions" not in receipt.reason


@pytest.mark.parametrize(
    ("description", "language", "lines"),
    [
        ("English captions", "en", ("I ran today",)),
        ("subtitles in English", "en", ("I ran today",)),
        ("captions should be in English", "en", ("I ran today",)),
        ("Türkçe altyazı", "tr", ("Bugün koştum",)),
        ("altyazıları Türkçeye çevir", "tr", ("Bugün koştum",)),
        (
            "English captions, and spell Moda and Kadıköy right",
            "en",
            ("Moda is by the sea", "Kadıköy's best coffee"),
        ),
        (
            "Make sure the captions are in English and Moda and Kadıköy are spelled correctly",
            "en",
            ("Moda is by the sea", "Kadıköy's best coffee"),
        ),
    ],
)
def test_english_and_turkish_phrasings_are_met(description, language, lines):
    req = BriefRequirement(id="c", kind="style", scope="global", description=description)
    receipt = _judged(req, _t3_variant(lines, caption_language=language))
    assert (receipt.status, receipt.verification) == ("met", "checked")


def test_an_english_ask_on_turkish_captions_is_not_met():
    req = BriefRequirement(id="c", kind="text", scope="global", description="subtitles in English")
    receipt = _judged(req, _t3_variant())
    assert receipt.status == "partial"
    assert receipt.reason == "The captions are in Turkish, not English."


@pytest.mark.parametrize(
    ("description", "names"),
    [
        (_T3_R2.description, ("Moda", "Bahariye", "Yeldeğirmeni", "Kadıköy")),
        ("Türkçe altyazı, Moda'yı ve Kadıköy'ü doğru yaz", ("Moda", "Kadıköy")),
        ("Türkçe altyazı; yer isimleri doğru yazılsın (Moda, Kadıköy)", ("Moda", "Kadıköy")),
        ("altyazıda isimlerin yazımına dikkat: Moda, Yel Değirmeni", ("Moda", "Yel Değirmeni")),
        ("English captions, spelling Moda right", ("Moda",)),
        (
            "Provide English subtitles translated from the Turkish voiceover, spelling the "
            "specific place names exactly: Göreme, Paşabağ, Avanos, Kızılçukur",
            ("Göreme", "Paşabağ", "Avanos", "Kızılçukur"),
        ),
        (
            "Make sure the captions are in Turkish and Moda, Bahariye and Kadıköy are "
            "spelled right",
            ("Moda", "Bahariye", "Kadıköy"),
        ),
        ("I want Rio de Janeiro spelled correctly in the captions", ("Rio de Janeiro",)),
        # Lowercase names cannot be told from ordinary words: nothing is claimed about them.
        ("altyazılar Türkçe olsun; moda ve kadıköy doğru yazılsın", ()),
        ("altyazılar Türkçe olsun", None),
    ],
)
def test_names_are_read_from_the_list_next_to_the_spelling_cue(description, names):
    from app.kria.brief_checks import _spelled_names

    req = BriefRequirement(id="c", kind="text", scope="global", description=description)
    assert _spelled_names(req) == names


@pytest.mark.parametrize(
    "description",
    [
        "altyazılar Türkçe olsun; moda ve kadıköy doğru yazılsın",  # names not readable
        "add captions with the exact names",  # a spelling ask with no names
    ],
)
def test_a_spelling_ask_without_readable_names_stays_unchecked(description):
    req = BriefRequirement(id="c", kind="text", scope="global", description=description)
    receipt = _judged(req, _t3_variant())
    assert receipt.verification == "unchecked"
    assert not is_judged(req, check_requirement(req, plan_facts_from_phone_variant(_t3_variant())))
    assert "couldn't tell which words to check the spelling of" in receipt.reason


def test_an_unknown_caption_language_stays_unchecked():
    variant = _t3_variant()
    del variant["caption_language"]
    receipt = _judged(_T3_R2, variant)
    assert receipt.verification == "unchecked"
    assert receipt.reason.startswith("I couldn't check which language the captions are in.")


def test_a_render_with_no_captions_says_so():
    receipt = _judged(_T3_R2, _t3_variant(caption_cues=None))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == "This video has no captions."


def test_a_look_ask_stays_with_the_style_checker():
    req = BriefRequirement(
        id="c",
        kind="style",
        scope="global",
        description="yellow Turkish captions, spell Moda right",
    )
    receipt = _judged(req, _t3_variant())
    assert receipt.verification == "unchecked"
    assert receipt.reason == "I can't check the captions on this draft yet."


def test_word_by_word_with_a_language_needs_word_captions():
    req = BriefRequirement(
        id="c", kind="style", scope="global", description="Türkçe altyazı, kelime kelime"
    )
    sentences = _judged(req, _t3_variant())
    words = _judged(req, _t3_variant(voiceover_caption_style="word"))
    assert sentences.status == "partial"
    assert "not word by word" in sentences.reason
    assert words.status == "met"


def test_the_draft_and_editor_answers_are_unchanged():
    """No caption lines (a strategy draft, an editor edit) -> today's `_check_captions`."""
    draft = PlanFacts(edit_format="subtitled", caption_style="editorial")
    editor = plan_facts_from_editor_payload({"text_elements": [{"id": "t1", "text": "Moda"}]})
    for facts in (draft, editor):
        receipt = check_requirement(_T3_R2, facts)
        assert receipt.reason == "I can't check the captions on this draft yet."
        assert not is_judged(_T3_R2, receipt)
