"""KRI-520: the receipts reply and its reasons read in the creator's language.

After every draft and render the creator reads ``reply_from_receipts``. A Turkish
chat gets Turkish line labels, headers and reasons; an unbound turn (English) is
byte-identical to before. Reasons are written into receipts when a turn runs, so
everything that classifies a stored reason (neutral, format limit, wordless title)
accepts both languages.
"""

from __future__ import annotations

import pytest

from app.kria import brief_checks
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    NO_TITLE_REASON,
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    is_no_title_reason,
    needs_creator_choice,
    render_block_recovery,
    reply_from_receipts,
)
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for

CLIPS = ("c1", "c2", "c3")


def _req(
    kind: str,
    description: str | None = None,
    *,
    scope: str = "global",
    literal: str | None = None,
    rid: str = "r1",
    **facts,
) -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind=kind, scope=scope, description=description, literal=literal, facts=facts
    )


def _reply(reqs, facts, *, summary="Hazır.", outcomes=(), notices=()) -> str:
    receipts = build_receipts(reqs, facts, include_unchecked=True)
    brief = CreativeBrief(version=1, requirements=list(reqs))
    return reply_from_receipts(brief, receipts, summary=summary, outcomes=outcomes, notices=notices)


# ----------------------------------------------------------------- labels and headers


def test_english_reply_is_unchanged_when_no_language_is_bound() -> None:
    reqs = [_req("text", "label every clip", scope="per_clip")]
    facts = PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "A"}, rendered_output=True)

    assert _reply(reqs, facts) == (
        "Not everything you asked for made it in:\n"
        "- Partly: label every clip (Text landed on 1 of 3 clips)"
    )


def test_english_language_bound_is_still_english() -> None:
    reqs = [_req("text", "label every clip", scope="per_clip")]
    facts = PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "A"}, rendered_output=True)
    unbound = _reply(reqs, facts)

    with reply_language_for("en"):
        assert _reply(reqs, facts) == unbound


def test_turkish_labels_header_and_reason() -> None:
    reqs = [_req("text", "etiket ekle", scope="per_clip")]
    facts = PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "A"}, rendered_output=True)

    with reply_language_for("tr"):
        text = _reply(reqs, facts)

    assert text == (
        "İstediklerinin hepsi videoya giremedi:\n"
        "- Kısmen: etiket ekle (Yazı 3 klibin 1 tanesine geldi)"
    )


def test_turkish_not_possible_and_met_labels_and_kept_summary() -> None:
    not_possible = [_req("order", "çekim sırasına göre", key="capture_time")]
    with reply_language_for("tr"):
        text = _reply(not_possible, PlanFacts(ordering_basis="attachment"))
    assert text.startswith(
        "İstediklerinin hepsi videoya giremedi:\n- Yapamadım: çekim sırasına göre"
    )
    assert "Bu taslak istediğin sıraya göre değil" in text

    met = [_req("text", "başlık", scope="title", literal="Galata")]
    with reply_language_for("tr"):
        text = _reply(met, PlanFacts(title="Galata", texts=("Galata",), title_source="creator"))
    # Every judged requirement is met: the model's own summary stays, then the line.
    assert text == 'Hazır.\n- Yapıldı: başlık ("Galata")'


def test_turkish_unchecked_header_and_label() -> None:
    reqs = [_req("style", "yazı rengi sarı olsun")]
    with reply_language_for("tr"):
        text = _reply(reqs, PlanFacts())
    assert text.startswith(
        "İstediğin değişikliklerin hepsini doğrulayamadım:\n- Doğrulayamadım: yazı rengi sarı olsun"
    )
    assert "Bunu henüz otomatik olarak doğrulayamıyorum" in text


def test_english_unchecked_header_is_unchanged() -> None:
    reqs = [_req("style", "make the text yellow")]
    text = _reply(reqs, PlanFacts())
    assert text.startswith(
        "I couldn't verify every requested change:\n- Couldn't verify: make the text yellow"
    )


def _edit_reply(reqs, facts, **kwargs) -> str:
    receipts = build_receipts(reqs, facts, include_unchecked=True)
    brief = CreativeBrief(version=1, requirements=list(reqs))
    return reply_from_receipts(brief, receipts, summary="Done!", edit_applied=True, **kwargs)


def test_applied_edit_with_an_unjudgeable_ask_reads_calmly_not_as_a_failure() -> None:
    """KRI-534: "add fade-in" applied; "I couldn't verify every change" read as an error."""
    reqs = [_req("style", "Add fade-in animation to all of them")]
    text = _edit_reply(reqs, PlanFacts(editor=True))
    assert text == (
        "Updated your edit.\n"
        "I can't check this automatically, so have a look: Add fade-in animation to all of them"
    )
    assert "verify" not in text.lower()
    assert "Done!" not in text  # the model's own summary is never echoed


def test_applied_edit_lists_several_unjudgeable_asks_and_keeps_the_notices() -> None:
    reqs = [
        _req("style", "fade in the text", rid="r1"),
        _req("style", "left-align the text", rid="r2"),
    ]
    text = _edit_reply(reqs, PlanFacts(editor=True), notices=("I kept your clip order.",))
    assert text == (
        "Updated your edit.\n"
        "I can't check these automatically, so have a look:\n"
        "- fade in the text\n- left-align the text\n"
        "I kept your clip order."
    )


def test_applied_edit_reads_in_turkish() -> None:
    reqs = [_req("style", "yazılara animasyon ekle")]
    with reply_language_for("tr"):
        text = _edit_reply(reqs, PlanFacts(editor=True))
    assert text == (
        "Düzenlemeni güncelledim.\n"
        "Bunu otomatik olarak kontrol edemiyorum, bir göz at: yazılara animasyon ekle"
    )


def test_applied_edit_keeps_judged_lines_next_to_the_open_ask() -> None:
    reqs = [
        _req("text", "title", scope="title", literal="Galata", rid="r1"),
        _req("style", "fade in the text", rid="r2"),
    ]
    facts = PlanFacts(editor=True, title="Galata", texts=("Galata",), title_source="creator")
    text = _edit_reply(reqs, facts)
    assert text.startswith("Updated your edit.\n- Done: ")
    assert text.endswith("I can't check this automatically, so have a look: fade in the text")


def test_applied_edit_with_a_judged_miss_keeps_the_honest_failure_wording() -> None:
    """Never say "Updated your edit" next to a requirement that was checked and missed."""
    reqs = [
        _req("text", "title", scope="title", literal="Galata", rid="r1"),
        _req("style", "fade in the text", rid="r2"),
    ]
    facts = PlanFacts(editor=True, title="Something else", texts=("Something else",))
    text = _edit_reply(reqs, facts)
    assert text.startswith("I couldn't verify every requested change:")
    assert "Updated your edit" not in text


def test_the_default_reply_is_unchanged_for_drafts_and_renders() -> None:
    reqs = [_req("style", "fade in the text")]
    assert _reply(reqs, PlanFacts(editor=True)).startswith(
        "I couldn't verify every requested change:"
    )


def test_turkish_guess_line_and_chosen_outcome() -> None:
    reqs = [_req("text", "etiket", scope="per_clip", literal="Galata")]
    facts = PlanFacts(
        clip_ids=CLIPS,
        per_clip_text={"c1": "A", "c2": "B", "c3": "C"},
        inferred_text={"c1": "Beşiktaş"},
        rendered_output=True,
    )
    outcomes = [
        {
            "op": "group",
            "name": "group by football, pub",
            "status": "chosen",
            "reason": "you chose strictly chronological order, so football is in 2 stretches",
        },
    ]
    with reply_language_for("tr"):
        text = _reply(reqs, facts, outcomes=outcomes)

    assert (
        "- Senin seçimin: gruplama: football, pub (kesin kronolojik sırayı seçtin, "
        "o yüzden football 2 parçaya bölünüyor)" in text
    )
    assert (
        "- Şunları tahmin ettim, yanlış olan varsa söyle: Beşiktaş "
        "(senin sözlerinden değil, çekimlerden aldığım yazılar)"
    ) in text


def test_english_guess_line_is_unchanged() -> None:
    reqs = [_req("text", "label", scope="per_clip", literal="Galata")]
    facts = PlanFacts(
        clip_ids=CLIPS,
        per_clip_text={"c1": "A", "c2": "B", "c3": "C"},
        inferred_text={"c1": "Beşiktaş"},
        rendered_output=True,
    )
    assert (
        "- I guessed these, tell me if any is wrong: Beşiktaş "
        "(text I took from the footage, not from your words)"
    ) in _reply(reqs, facts)


def test_outcome_rows_read_in_turkish() -> None:
    outcomes = [
        {
            "op": "order",
            "name": "first: the blue video",
            "position": "first",
            "status": "partial",
            "reason": "the clips did not end up there",
        },
        {
            "op": "caption",
            "name": "text for football",
            "status": "not_possible",
            "reason": "I found no clips of it",
        },
        {
            "op": "label",
            "name": "the football name on its clips",
            "status": "not_possible",
            "reason": "no clip got one",
        },
    ]
    brief = CreativeBrief(version=1, requirements=[])
    with reply_language_for("tr"):
        text = reply_from_receipts(brief, [], outcomes=outcomes)
    assert text == (
        "İstediklerinin hepsi videoya giremedi:\n"
        "- Kısmen: ilk: the blue video (klipler oraya gelmedi)\n"
        "- Yapamadım: football için yazı (Bununla ilgili klip bulamadım)\n"
        "- Yapamadım: kliplerindeki football adı (hiçbir klibe gelmedi)"
    )
    english = reply_from_receipts(brief, [], outcomes=outcomes)
    assert "- Partly: first: the blue video (the clips did not end up there)" in english


def test_reply_stays_inside_the_length_cap_in_turkish() -> None:
    reqs = [_req("text", f"yazı {i}", scope="per_clip", rid=f"r{i}") for i in range(40)]
    facts = PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "A"}, rendered_output=True)
    with reply_language_for("tr"):
        text = _reply(reqs, facts)
    assert len(text) <= brief_checks.MAX_REPLY_CHARS


# ------------------------------------------------------------------- reasons, by check


@pytest.mark.parametrize(
    ("req", "facts", "turkish"),
    [
        (
            _req("timing", "30 saniye olsun", duration_s=30),
            PlanFacts(duration_s=40.0, outro_s=1.6),
            "Bu taslak yaklaşık 40 saniye (bitmiş videoda buna ek olarak 1.6 saniyelik bir "
            "kapanış var); sen 30 saniye istedin.",
        ),
        (
            _req("order", "sırala", key="capture_time"),
            PlanFacts(ordering_basis="song_time"),
            "Bu taslak istediğin sıraya göre değil, şuna göre dizilmiş: şarkının zamanı.",
        ),
        (
            _req("text", "etiket", scope="per_clip"),
            PlanFacts(clip_ids=CLIPS, rendered_output=True),
            "Bu taslakta 3 klibin hiçbirine kendi yazısı gelmedi.",
        ),
        (
            _req("text", "etiket", scope="clip:c1", literal="Galata"),
            PlanFacts(clip_ids=CLIPS, per_clip_text={"c1": "Kule"}),
            "O klibin yazısı verdiğin yazıyla aynı değil.",
        ),
        (
            _req("text", "başlık", scope="title"),
            PlanFacts(title="Merhaba", title_source="default"),
            "Başlık vermediğin için sade bir varsayılan başlık kullandım.",
        ),
        (
            _req("text", "başlık", scope="global", literal="Galata"),
            PlanFacts(texts=("Kule",)),
            "Bu tam yazı bu taslakta yok.",
        ),
        (
            _req("style", "karaoke captions"),
            PlanFacts(edit_format="subtitled", caption_style="clean"),
            "Altyazılar kelime kelime değil, tam cümleler olarak açık.",
        ),
        (
            _req("timing", "videoyu olduğu gibi bırak", keep_whole_take=True),
            PlanFacts(edit_format="montage"),
            "Bu düzenleme (montaj) çekimlerini kısaltır; "
            "Konuşmalı düzenleme çekimin tamamını korur.",
        ),
    ],
)
def test_check_reasons_are_turkish_in_a_turkish_turn(req, facts, turkish) -> None:
    with reply_language_for("tr"):
        receipt = check_requirement(req, facts)
    assert receipt.reason == turkish


def test_beat_and_closing_problems_are_turkish() -> None:
    req = _req("style", "pop up a sticker when I say pasta and end on the closing photo")
    facts = PlanFacts(
        reaction_beats=(),
        reaction_beats_available=True,
        dropped_beat_triggers=("pasta",),
        closing_requested=True,
        closing_visual_id=None,
    )
    with reply_language_for("tr"):
        receipt = check_requirement(req, facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == (
        "Şunlar için fotoğraf ya da çıkartma bulamadım: pasta; "
        "Söylediğin kapanış fotoğrafını bulamadım."
    )


def test_route_reversed_reason_is_turkish() -> None:
    from app.kria.brief_checks import EndpointFact

    req = _req("order", "rota", key="capture_time", start="Arnavutköy", end="Eminönü")
    facts = PlanFacts(
        ordering_basis="capture_time",
        first_endpoint=EndpointFact(label="Eminönü", places=("Eminönü, Fatih",)),
        last_endpoint=EndpointFact(label="Arnavutköy", places=("Arnavutköy",)),
    )
    with reply_language_for("tr"):
        receipt = check_requirement(req, facts)
    assert receipt.reason is not None
    assert receipt.reason.startswith("Klipler şöyle çekilmiş: başlangıç Eminönü, bitiş Arnavutköy.")
    assert "Çekim sırasını korudum" in receipt.reason


def test_sequence_unmet_names_the_spot_in_turkish() -> None:
    req = _req("order", "sırala", key="chronological")
    facts = PlanFacts(
        ordering_basis="capture_time",
        strict_order=True,
        sequence_unmet=("mavi video (ilk)",),
        sequence_absent=("kafe",),
    )
    with reply_language_for("tr"):
        receipt = check_requirement(req, facts)
    assert receipt.reason == (
        "Klipler çekim sırasında, ama bunlar istediğin yerde değil: mavi video (ilk); "
        "şunlar için klip bulamadım: kafe."
    )


def test_speech_cleanup_notes_are_turkish_and_stay_a_format_limit() -> None:
    req = _req(
        "timing",
        "Cut out the long pauses and the part where I say 'let me start again'. Keep it under 45 s",
        duration_s=45,
    )
    facts = PlanFacts(edit_format="subtitled", caption_style="karaoke", speech_cleanup_offered=True)
    with reply_language_for("tr"):
        receipt = check_requirement(req, facts)
        [bound] = build_receipts([req], facts, include_unchecked=True)

    assert receipt.reason == (
        "Onaylarken \u201cKonuşmayı temizle\u201d seçeneğini seç, uzun duraklamalar kesilir; "
        "tekrar çekim ya da belirli bir cümle henüz otomatik kesilmiyor, onu editörde kırp; "
        "uzunluk çekimin geriye kalan kısmına göre belirleniyor, 45 sn için söz veremem"
    )
    # A limit of the format in Turkish too: it never asks for a simpler version.
    assert is_format_limit(receipt.reason)
    assert bound.verification == "checked"
    assert not needs_creator_choice(bound)


def test_english_speech_cleanup_reason_is_unchanged() -> None:
    req = _req(
        "timing",
        "Cut out the long pauses and the part where I say 'let me start again'. Keep it under 45 s",
        duration_s=45,
    )
    facts = PlanFacts(edit_format="subtitled", caption_style="karaoke", speech_cleanup_offered=True)
    assert check_requirement(req, facts).reason == (
        "Choose Clean up speech when you approve and the long pauses are cut; "
        "a retake or a specific line isn't cut automatically yet, so trim that in the "
        "editor; the length follows what's left of your take, so I can't promise 45s"
    )


# ------------------------------------------------- classifying reasons in either language


def test_every_static_english_reason_has_a_turkish_twin() -> None:
    english = {
        value
        for name, value in vars(brief_checks).items()
        if name.startswith(("_CANT_", "_CLEANUP_", "_ORDER_"))
        or name
        in {
            "_NO_CHECKER",
            "_NO_TITLE",
            "_NAMED_CUTS_NEED_EDITOR",
            "_CAPTIONS_WORD_BY_WORD",
            "_TALKING_KEEPS_WHOLE_TAKE",
            "_VOICEOVER_SETS_LENGTH",
        }
        if isinstance(value, str)
    }
    missing = [text for text in english if text not in brief_checks._REASON_TR]
    assert not missing
    for text in english:
        twin = brief_checks._REASON_TR[text]
        assert twin != text
        assert len(twin) <= 300


def test_neutral_reasons_are_neutral_in_both_languages() -> None:
    req = _req("text", "başlık", scope="title")
    for language in (None, "tr"):
        with reply_language_for(language):
            receipt = check_requirement(req, PlanFacts(title="Merhaba"))
        assert receipt.status == "partial"
        assert not is_judged(req, receipt)


def test_a_turkish_judged_reason_is_still_judged() -> None:
    req = _req("timing", "30 saniye olsun", duration_s=30)
    with reply_language_for("tr"):
        receipt = check_requirement(req, PlanFacts(duration_s=40.0))
    assert is_judged(req, receipt)


@pytest.mark.parametrize("language", [None, "tr"])
def test_format_limits_are_recognised_in_both_languages(language) -> None:
    req = _req("timing", "45 saniye olsun", duration_s=45)
    with reply_language_for(language):
        talking = check_requirement(req, PlanFacts(edit_format="subtitled"))
        voiceover = check_requirement(req, PlanFacts(edit_format="narrated"))
    for receipt in (talking, voiceover):
        assert is_format_limit(receipt.reason)
    assert not is_format_limit("Bu taslak yaklaşık 40 saniye; sen 30 saniye istedin.")


def test_a_stored_english_reason_reads_in_turkish() -> None:
    """A worker without the chat language writes English; the reply still reads Turkish."""
    req = _req("style", "karaoke captions")
    stored = RequirementReceipt(
        requirement_id="r1",
        status="partial",
        verification="checked",
        stage="checked",
        reason="Captions are on as full sentences, not word by word.",
    )
    brief = CreativeBrief(version=1, requirements=[req])
    english_only = brief_checks._CANT_CHECK_CAPTIONS
    neutral = stored.model_copy(update={"reason": english_only})
    with reply_language_for("tr"):
        assert not is_judged(req, neutral)
        text = reply_from_receipts(brief, [stored])
    # Dynamic English reasons stay as the worker wrote them; the labels are Turkish.
    assert text.startswith("İstediklerinin hepsi videoya giremedi:\n- Kısmen: karaoke captions")


def test_stored_static_english_reason_is_translated_on_display() -> None:
    req = _req("order", "sırala", key="capture_time")
    stored = RequirementReceipt(
        requirement_id="r1",
        status="partial",
        verification="checked",
        stage="checked",
        reason=brief_checks._ORDER_NOT_APPLIED,
    )
    brief = CreativeBrief(version=1, requirements=[req])
    with reply_language_for("tr"):
        text = reply_from_receipts(brief, [stored])
    assert "Anlattığını kliplerle eşleştiremedim" in text
    assert "I couldn't match" not in text


# ------------------------------------------------------------------------- title block


def test_wordless_title_reason_is_found_in_both_languages() -> None:
    req = _req("text", "başlık ekle", scope="title")
    english = check_requirement(req, PlanFacts())
    with reply_language_for("tr"):
        turkish = check_requirement(req, PlanFacts())

    assert english.reason == NO_TITLE_REASON
    assert turkish.reason != NO_TITLE_REASON
    assert is_no_title_reason(english.reason)
    assert is_no_title_reason(turkish.reason)
    assert not is_no_title_reason("Başka bir neden")
    assert not is_no_title_reason(None)


def test_title_block_recovery_in_turkish() -> None:
    req = _req("text", "başlık ekle", scope="title")
    with reply_language_for("tr"):
        reason = check_requirement(req, PlanFacts()).reason
        only_title = render_block_recovery([{"reason": reason}])
        mixed = render_block_recovery([{"reason": reason}, {"reason": "Başka bir sorun."}])

    assert only_title.decline_reason == "needs_choice"
    assert only_title.field_path == "opening_title"
    assert only_title.alternative == 'Başlığın kelimelerini söyle ya da "başlık olmasın" yaz.'
    assert only_title.message == (
        "Videoyu henüz oluşturamadım: başlık istedin ama kelimelerini vermedin. "
        "Taslağın kaydedildi. " + only_title.alternative
    )
    assert mixed.decline_reason is None
    assert mixed.message == (
        "Videoyu henüz oluşturamadım: başlık istedin ama kelimelerini vermedin. "
        "Başka bir sorun. Taslağın kaydedildi. Tekrar mı deneyeyim, yoksa isteği "
        'sadeleştireyim mi? Başlık için kelimeleri söyle ya da "başlık olmasın" yaz.'
    )


def test_title_block_recovery_in_english_is_unchanged() -> None:
    only_title = render_block_recovery([{"reason": NO_TITLE_REASON}])
    assert only_title.message == (
        "I couldn't make the video yet: you asked for a title but gave no words. "
        'Your draft is saved. Tell me the words for the title, or say "continue without a title".'
    )
    assert only_title.alternative == (
        'Tell me the words for the title, or say "continue without a title".'
    )
    generic = render_block_recovery([{"reason": "x"}])
    assert generic.message == "x Your draft is saved. Should I try again or simplify this request?"


def test_title_block_turkish_way_forward_is_an_answer_the_gate_accepts() -> None:
    """The typed way forward must be one of the phrases the title question accepts."""
    from app.services.choice_questions import _NO_TITLE_ALIASES

    with reply_language_for("tr"):
        alternative = render_block_recovery([{"reason": NO_TITLE_REASON}]).alternative
    assert alternative is not None
    assert '"başlık olmasın"' in alternative
    assert "başlık olmasın" in _NO_TITLE_ALIASES
