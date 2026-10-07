"""KRI-520: the edit copilot understands and answers Turkish.

Covers the honesty guard (a Turkish reply that claims an edit nothing made), the
full-screen overlay ask, the bulk follow-up regexes, the Turkish-aware label fold and
the server-written notes appended to model replies. English must stay byte-identical,
so every Turkish behaviour has an unbound/English twin here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import (
    _CREATIVE_CAPTION_RE,
    _DURATION_ANSWER_RE,
    _PENDING_NEGATION_RE,
    _REFERENT_PRONOUN_RE,
    _REMOVE_WORDS,
    _SHADOW_WORDS,
    _STACK_RE,
    _VAGUE_DURATION_RE,
    EditCopilotAgent,
    EditCopilotInput,
    EditCopilotOutput,
    _duration_seconds,
    _explicit_media_kinds,
    _label_fold,
    _parse_op,
    _ParseState,
    _server_reply,
)
from app.kria.reply_language import reply_language_for
from app.routes._copilot import (
    OVERLAY_DISPLAY_LIMIT_REPLY,
    _claims_success,
    _honest_outcome,
    _negates_success,
    is_overlay_display_ask,
)
from app.services.clip_facts import timezone_note
from app.services.kria_editor_ops import build_editor_snapshot
from tests.services.test_kria_capture_time_ops import (  # noqa: F401
    _add,
    _label_op,
    _seen_snapshot,
    _snapshot,
    guided,
)
from tests.services.test_kria_capture_time_ops import _parse as _parse_ops
from tests.test_edit_copilot import _bulk_snapshot, _pending_with_integrity
from tests.test_edit_copilot import _parse as _parse_basic

GOLDEN = Path(__file__).parent / "fixtures" / "agent_evals" / "edit_copilot" / "golden"


# ── honesty guard ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        "Başlığı büyüttüm.",
        "Müziği kaldırdım.",
        "Altyazıları ekledim",
        "Klibi kısalttım.",
        "BAŞLIĞI BÜYÜTTÜM",
        "baslik buyuttum",
        "Başlık değiştirildi.",
        "Müzik kaldırıldı",
        "Sildim.",
        "Taşıdım",
        "Koydum.",
        "Güncelledim",
        "Düzelttim",
        "Ayarladım",
        "Çıkardım",
        "Kıstım",
        "Dizdim",
        "Hazırladım",
        "Uyguladım",
        "Yaptım",
        "Uzattım",
        "Küçülttüm",
        "Tamam, yaptım!",
        "Her videoya saati ekledim.",
    ],
)
def test_turkish_edit_claims_are_success_claims(reply: str) -> None:
    assert _claims_success(reply), reply


@pytest.mark.parametrize(
    "reply",
    [
        "İstedim.",
        "Bilmiyorum.",
        "Ne yapmak istersin?",
        "Hazırım.",
        "Tamam.",
        "Hangi klibi kısaltayım?",
        "Bunu yapabilirim.",
        "Başlığı büyütmemi ister misin?",
        "Kaç saniye istersin?",
        "Sana yardım edebilirim.",
        "Bir ayrıntıya ihtiyacım var.",
    ],
)
def test_turkish_non_claims_are_not_success_claims(reply: str) -> None:
    assert not _claims_success(reply), reply


@pytest.mark.parametrize(
    "reply",
    [
        "Başlığı büyütmedim.",
        "Müziği kaldıramadım.",
        "Yapamadım.",
        "Bunu yapamıyorum.",
        "Başlığı büyüttüm ama müziği kaldıramadım.",
        "Müziği kaldırdım, başlık zaten büyüktü.",
        "Klibi kısalttım ama henüz bitmedi.",
        "Müziği kaldırdım, başlık değil.",
        "Bunu yapmayacağım, başlığı büyüttüm demek yanlış olur.",
        "Başlığı değiştirmeyecek, Müziği kaldırdım diyemem.",
        "Olmaz, sildim diyemem.",
        "Edemedim, taşıdım sanma.",
    ],
)
def test_turkish_negation_excuses_a_claim(reply: str) -> None:
    assert _negates_success(reply), reply
    assert not _claims_success(reply), reply


def test_komedi_is_not_a_negation() -> None:
    assert not _negates_success("Komedi klibini ekledim.")
    assert _claims_success("Komedi klibini ekledim.")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Done.", True),
        ("Tightened the cuts.", True),
        ("Moved the clip to the end.", True),
        ("I changed the title but not the timing.", False),
        ("I cannot change score timing.", False),
        ("Sounds good, a tighter pace works.", False),
        ("Which clip do you mean?", False),
        ("Dimmed the lights and set the mood.", True),
        ("Comedic timing sets the tone.", False),
    ],
)
def test_english_success_guard_is_unchanged(reply: str, expected: bool) -> None:
    assert _claims_success(reply) is expected


def _output(**kwargs) -> EditCopilotOutput:
    defaults = {"intent": "edit", "ops": [], "confidence": 0.9, "reply": ""}
    return EditCopilotOutput(**{**defaults, **kwargs})


def test_turkish_success_claim_without_an_edit_is_not_surfaced() -> None:
    output = _output(reply="Başlığı büyüttüm.")
    outcome, reply = _honest_outcome(output, [])
    assert outcome == "no_effect"
    assert "büyüttüm" not in reply
    assert reply == "That change is already reflected in the draft."
    with reply_language_for("tr"):
        outcome, reply = _honest_outcome(output, [])
    assert outcome == "no_effect"
    assert reply == "Bu değişiklik taslakta zaten var."


def test_turkish_claim_with_ops_becomes_the_canned_line_plus_notes() -> None:
    output = _output(reply="Müziği kaldırdım.", reply_notes="Klip 3 için çekim saati yok.")
    ops = [{"op": "patch_slots"}]
    with reply_language_for("tr"):
        outcome, reply = _honest_outcome(output, ops)
    assert outcome == "proposed"
    assert reply == (
        "Bu düzenlemeyi hazırladım, editör kontrol edip uygulayacak. Klip 3 için çekim saati yok."
    )
    # An honest Turkish line passes through; English copy is unbound-identical.
    kept = _output(reply="Daha hızlı bir tempo iyi olur.")
    assert _honest_outcome(kept, ops) == ("proposed", "Daha hızlı bir tempo iyi olur.")
    english = _output(reply="Moved it.", reply_notes="n.")
    assert _honest_outcome(english, ops) == (
        "proposed",
        "I prepared this edit for the editor to validate and stage. n.",
    )


def test_turkish_canned_outcomes() -> None:
    reasons_stale = [{"op": "x", "reason": "stale_target", "detail": "gone"}]
    reasons_failed = [{"op": "x", "reason": "invalid_value", "detail": "bad"}]
    with reply_language_for("tr"):
        assert _honest_outcome(_output(intent="clarify", reply="Yaptım."), [])[1] == (
            "Taslağı değiştirmeden önce bir ayrıntıya ihtiyacım var."
        )
        assert _honest_outcome(_output(rejection_reasons=reasons_stale), []) == (
            "stale",
            "Bu düzenleme taslağın eski bir halini temel alıyor. Editörü yenile ve tekrar dene.",
        )
        assert _honest_outcome(_output(intent="reject"), []) == (
            "unsupported",
            "Bu tür bir düzenleme bu taslakta henüz yapılamıyor.",
        )
        assert _honest_outcome(_output(rejection_reasons=reasons_failed), []) == (
            "failed",
            "Bu istek için geçerli bir taslak değişikliği oluşturamadım. Tekrar dene.",
        )
        assert _honest_outcome(
            _output(rejection_reasons=reasons_failed, reply_notes="Efekti 'x' yapamadım."), []
        ) == ("failed", "Bunu uygulayamadım: Efekti 'x' yapamadım.")
        # A structured rejection beats a Turkish success claim.
        claim = _output(
            reply="Başlığı büyüttüm.",
            rejection_reasons=[{"op": "x", "reason": "no_op", "detail": ""}],
        )
        assert _honest_outcome(claim, [])[1] == "Bu değişikliği bu düzenlemede yapamadım."
        # An honest Turkish "no" is kept for an unsupported ask.
        no = _output(intent="reject", reply="Bu klibin sesini kısamam.")
        assert _honest_outcome(no, []) == ("unsupported", "Bu klibin sesini kısamam.")
        # ...but a claim of success on an unsupported ask is replaced.
        lie = _output(intent="reject", reply="Başlığı büyüttüm.")
        assert _honest_outcome(lie, [])[1] == "Bu tür bir düzenleme bu taslakta henüz yapılamıyor."
    # English twins, with and without the language bound.
    for scope in (reply_language_for("en"), reply_language_for(None)):
        with scope:
            assert _honest_outcome(_output(intent="reject"), [])[1] == (
                "That kind of edit isn't available for this draft yet."
            )
            assert _honest_outcome(_output(rejection_reasons=reasons_failed), [])[1] == (
                "I couldn't build a valid draft change for that request. Try again."
            )


# ── full-screen overlay ask ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Görselleri tam ekran yap",
        "GÖRSELLERİ TAM EKRAN YAP",
        "gorselleri tam ekran yap",
        "Tüm görseller tam ekran olsun",
        "Fotoğrafları ekranı kaplasın",
        "Resimler ekranı doldursun",
        "resim içinde resim yap",
        "Klipleri tam ekran göster",
        "Videoları tam kadraj yap",
        "Use all overlays as full screen.",
    ],
)
def test_overlay_display_ask_reads_turkish(message: str) -> None:
    assert is_overlay_display_ask(message), message


@pytest.mark.parametrize(
    "message",
    [
        "Altyazıları tam ekran yap",
        "Başlığı büyüt",
        "Tam ekran olsun",
        "Görselleri sil",
        "Yazıyı ekranın ortasına koy",
        "make the captions full screen",
        "make the text bigger",
        "",
    ],
)
def test_overlay_display_ask_negatives(message: str) -> None:
    assert not is_overlay_display_ask(message), message


def test_unsupported_overlay_display_ask_names_the_limit_in_turkish() -> None:
    output = _output(intent="reject")
    message = "Görselleri tam ekran yap"
    with reply_language_for("tr"):
        outcome, reply = _honest_outcome(output, [], message=message)
    assert outcome == "unsupported"
    assert "yeni bir düzenleme" in reply
    assert reply != OVERLAY_DISPLAY_LIMIT_REPLY
    assert _honest_outcome(output, [], message="Use all overlays as full screen.")[1] == (
        OVERLAY_DISPLAY_LIMIT_REPLY
    )
    assert _honest_outcome(output, [], message=message)[1] == OVERLAY_DISPLAY_LIMIT_REPLY


# ── bulk follow-up regexes ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("10 saniye", "10"),
        ("10 sn", "10"),
        ("10sn", "10"),
        ("10 saniyelik", "10"),
        ("Hepsi 10 saniyeye ayarla", "10"),
        ("0.2 saniye olsun", "0.2"),
        ("5 seconds each", "5"),
        ("make them 3s", "3"),
    ],
)
def test_duration_answer_reads_turkish_units(text: str, number: str) -> None:
    match = _DURATION_ANSWER_RE.search(text)
    assert match is not None and match.group(1) == number


def test_duration_answer_accepts_a_decimal_comma() -> None:
    match = _DURATION_ANSWER_RE.search("2,5 saniye")
    assert match is not None and _duration_seconds(match) == 2.5
    english = _DURATION_ANSWER_RE.search("2.5 seconds")
    assert english is not None and _duration_seconds(english) == 2.5


@pytest.mark.parametrize("text", ["10 klip", "saniye", "beş saniye", "10 clips", ""])
def test_duration_answer_ignores_non_durations(text: str) -> None:
    assert _DURATION_ANSWER_RE.search(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "kısalt",
        "Hepsini kısaltalım",
        "daha kısa yap",
        "kısa yap",
        "KISALT",
        "kisalt",
        "make them shorter",
        "shorten it",
    ],
)
def test_vague_duration_reads_turkish(text: str) -> None:
    assert _VAGUE_DURATION_RE.search(text), text


@pytest.mark.parametrize(
    "text",
    # Lengthening is never a vague "how short?", in Turkish as in English.
    ["kısaca anlat", "10 saniye yap", "uzaklaştır", "make them longer", "uzat", "daha uzun olsun"],
)
def test_vague_duration_negatives(text: str) -> None:
    assert _VAGUE_DURATION_RE.search(text) is None, text


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("fotoğraflar", {"image"}),
        ("Fotoğrafları 5 saniye yap", {"image"}),
        ("resimler", {"image"}),
        ("resmi kısalt", {"image"}),
        ("görselleri üst üste koy", {"image"}),
        ("foto", {"image"}),
        ("videolar", {"video"}),
        ("videoları kısalt", {"video"}),
        ("klipler", {"all"}),
        ("klibi kısalt", {"all"}),
        ("klibe bak", {"all"}),
        ("video klipleri", {"video"}),
        ("fotoğraflar ve videolar", {"image", "video"}),
        ("images", {"image"}),
        ("the clips", {"all"}),
        ("bunu kısalt", set()),
    ],
)
def test_media_kind_words_read_turkish(text: str, kinds: set[str]) -> None:
    assert _explicit_media_kinds(text) == kinds


@pytest.mark.parametrize(
    "text",
    ["onları", "Bunları 5 saniye yap", "hepsini", "Onlar", "bunlar", "Hepsi", "tümünü", "them"],
)
def test_referent_pronouns_read_turkish(text: str) -> None:
    assert _REFERENT_PRONOUN_RE.search(text), text


@pytest.mark.parametrize("text", ["tüm klipler", "bunu", "onu", "it", "all clips"])
def test_referent_pronoun_negatives(text: str) -> None:
    assert _REFERENT_PRONOUN_RE.search(text) is None, text


@pytest.mark.parametrize(
    "text",
    [
        "yapma",
        "videolar hariç",
        "olmadan yap",
        "videoların dışında",
        "videolar değil",
        "dokunma",
        "except the videos",
        "don't",
    ],
)
def test_pending_negation_reads_turkish(text: str) -> None:
    assert _PENDING_NEGATION_RE.search(text), text


@pytest.mark.parametrize("text", ["yapmak istiyorum", "yap", "hepsini yap", "all of them"])
def test_pending_negation_negatives(text: str) -> None:
    assert _PENDING_NEGATION_RE.search(text) is None, text


@pytest.mark.parametrize("text", ["üst üste koy", "ÜST ÜSTE", "istifle", "stack them"])
def test_stack_reads_turkish(text: str) -> None:
    assert _STACK_RE.search(text), text


def test_label_fold_is_turkish_aware() -> None:
    assert _label_fold("AÇIKLA") == "açikla"
    assert _CREATIVE_CAPTION_RE.search(_label_fold("her klibi AÇIKLA"))
    assert _CREATIVE_CAPTION_RE.search(_label_fold("her klibi açıkla"))
    assert _CREATIVE_CAPTION_RE.search(_label_fold("her klibi acikla"))
    assert _CREATIVE_CAPTION_RE.search(_label_fold("HANGİ BÖLÜM"))
    assert _REMOVE_WORDS.search(_label_fold("KALDIR"))
    assert _REMOVE_WORDS.search(_label_fold("kaldır"))
    assert _REMOVE_WORDS.search(_label_fold("efekti kapat"))
    assert _REMOVE_WORDS.search(_label_fold("OLMASIN"))
    assert _SHADOW_WORDS.search(_label_fold("GÖLGE"))
    assert _label_fold("  Hello   WORLD ") == "hello world"
    assert _label_fold("ＡＢＣ") == "abc"


# ── bulk plan continues from a Turkish answer ─────────────────────────────────

_IMAGES = {"scope": "timeline", "media_kind": "image", "quantifier": "all"}


def _bulk_turn(utterance: str, ops: list[dict], reply: str = "Hazır."):
    snapshot = _bulk_snapshot()
    prior = [
        {
            "role": "assistant",
            "content": "Hangi görseller, ve ne kadar kısa?",
            "clarification_context": {"selector": _IMAGES, "referent": "images"},
            "pending_actions": _pending_with_integrity(
                snapshot, [{"op": "stack_images"}, {"op": "set_media_duration"}], _IMAGES
            ),
        }
    ]
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": ops,
            "confidence": 0.95,
            "reply": reply,
            "needs_clarification": False,
        },
        ensure_ascii=False,
    )
    return EditCopilotAgent(ModelClient()).parse(
        raw,
        EditCopilotInput(utterance=utterance, prior_turns=prior, variant_snapshot=snapshot),
    )


def test_turkish_pronoun_answer_continues_the_pending_plan() -> None:
    out = _bulk_turn("hepsini 0,2 saniye yap", [{"op": "stack_images", "selector": _IMAGES}])
    names = {operation["op"] for operation in out.ops}
    assert names == {"stack_images", "set_media_duration"}
    duration = next(op for op in out.ops if op["op"] == "set_media_duration")
    assert duration["duration_s"] == 0.2
    assert duration["selector"]["media_kind"] == "image"


def test_turkish_answer_naming_images_and_stacking_continues_the_plan() -> None:
    out = _bulk_turn(
        "fotoğrafları üst üste koy, 3 sn olsun",
        [{"op": "stack_images", "selector": _IMAGES}],
    )
    assert {operation["op"] for operation in out.ops} == {"stack_images", "set_media_duration"}
    assert next(op for op in out.ops if op["op"] == "set_media_duration")["duration_s"] == 3.0


def test_turkish_negation_does_not_carry_the_pending_plan() -> None:
    out = _bulk_turn(
        "hepsini 5 saniye yap ama üst üste yapma",
        [
            {
                "op": "set_media_duration",
                "selector": _IMAGES,
                "duration_s": 5,
            }
        ],
    )
    assert [operation["op"] for operation in out.ops] == ["set_media_duration"]


def test_turkish_vague_length_asks_before_guessing_a_number() -> None:
    ops = [{"op": "set_media_duration", "selector": _IMAGES, "duration_s": 1}]
    out = _bulk_turn("hepsini daha kısa yap", ops)
    assert out.ops == [] and out.needs_clarification
    assert out.reply == (
        "Which images would you like to put together, and how short should each image be?"
    )
    with reply_language_for("tr"):
        out = _bulk_turn("hepsini daha kısa yap", ops)
    assert out.ops == [] and out.needs_clarification
    assert out.reply == (
        "Hangi görselleri bir araya getirmek istersin ve her görsel ne kadar kısa olsun?"
    )


def test_empty_reply_gets_a_turkish_default() -> None:
    raw = json.dumps({"intent": "describe", "ops": [], "confidence": 0.9, "reply": ""})
    input_data = EditCopilotInput(utterance="x", prior_turns=[], variant_snapshot={})
    agent = EditCopilotAgent(ModelClient())
    assert agent.parse(raw, input_data).reply == "Got it. What else should we change?"
    with reply_language_for("tr"):
        assert agent.parse(raw, input_data).reply == "Tamam. Başka neyi değiştirelim?"


# ── server-written notes appended to model replies ────────────────────────────


def test_timezone_note_localizes_but_keeps_the_zone_id() -> None:
    assert timezone_note("Europe/Istanbul", "place") == "Times are shown in Europe/Istanbul time."
    with reply_language_for("tr"):
        note = timezone_note("Europe/Istanbul", "place")
        assert note == "Saatler şu saat diliminde gösteriliyor: Europe/Istanbul."
        assert "UTC" in timezone_note("UTC", "utc")
        assert "Times are" not in timezone_note("UTC", "utc")
    assert timezone_note("UTC", "utc").startswith("Times are shown in UTC")


def test_server_reply_is_turkish_when_bound() -> None:
    ops = [
        {
            "op": "label_each_clip",
            "label_from": "capture_time",
            "mode": "append",
            "labels": [1, 2, 3],
        }
    ]
    assert _server_reply(ops, "n.") == "Added the filming hour to 3 clips. n."
    assert _server_reply([{"op": "x"}], "n.") == "Updated the edit. n."
    with reply_language_for("tr"):
        assert _server_reply(ops, "n.") == "3 klibe çekim saatini ekledim. n."
        replace = [{**ops[0], "mode": "replace"}]
        assert _server_reply(replace, "n.") == "3 klibe etiket ekledim. n."
        assert _server_reply([{"op": "x"}], "n.") == "Düzenlemeyi güncelledim. n."


def test_filming_time_notes_are_one_language(guided) -> None:  # noqa: F811
    job, variant, _rev = guided
    snapshot = _snapshot(job, variant)
    english = _label_op(snapshot, mode="append", time_format="hour")
    assert "No filming time for clip 3" in english.reply
    assert "Times are shown in Europe/Istanbul time." in english.reply
    with reply_language_for("tr"):
        out = _label_op(snapshot, mode="append", time_format="hour")
        _outcome, reply = _honest_outcome(out, out.ops)
    assert "Klip 3 için çekim saati yok." in out.reply
    assert "Saatler şu saat diliminde gösteriliyor: Europe/Istanbul." in out.reply
    assert "No filming time" not in out.reply and "Times are" not in out.reply
    assert reply == (
        "Bu düzenlemeyi hazırladım, editör kontrol edip uygulayacak. "
        "Klip 3 için çekim saati yok. "
        "Saatler şu saat diliminde gösteriliyor: Europe/Istanbul."
    )
    assert "Europe/Istanbul" in reply


def _replay_golden(name: str):
    golden = json.loads((GOLDEN / f"{name}.json").read_text())
    return EditCopilotAgent(ModelClient()).parse(
        golden["raw_text"], EditCopilotInput.model_validate(golden["input"])
    )


def test_descriptive_caption_notes_follow_the_chat_language() -> None:
    english = _replay_golden("kria_v2_capture_descriptive_captions_seen_tr")
    assert "I wrote these from what I saw in each clip" in english.reply
    assert "clip 1: Havalimanı karşılama" in english.reply
    with reply_language_for("tr"):
        out = _replay_golden("kria_v2_capture_descriptive_captions_seen_tr")
        _outcome, reply = _honest_outcome(out, out.ops)
    assert "I wrote" not in out.reply and "I left out" not in out.reply
    assert (
        "Bunları her klipte gördüklerime göre yazdım, yanlış olan varsa söyle: "
        "klip 1: Havalimanı karşılama; klip 2: Dans partisi; klip 4: Sahilde yürüyüş."
    ) in out.reply
    assert "Klip 3 dışarıda kaldı: ne gösterdiğini anlayamadım. Söylersen ekleyeyim." in out.reply
    # Same as English: the notes' own "I can't tell" negation lets the reply through.
    assert reply == out.reply


def test_effect_removal_note_follows_the_chat_language() -> None:
    english = _replay_golden("kria_v2_capture_restyle_added_captions")
    assert "I turned off the typewriter animation; say if you also want the shadow gone." in (
        english.reply
    )
    with reply_language_for("tr"):
        out = _replay_golden("kria_v2_capture_restyle_added_captions")
    assert "Typewriter animasyonunu kapattım; gölgeyi de kaldırmamı istersen söyle." in out.reply
    assert "I turned off" not in out.reply


ASK_DESCRIBE_TR = "Her klibe düğünün hangi bölümü olduğunu açıkla (wedding, airport pickup gibi)"


def test_caption_notes_for_dropped_mismatched_and_skipped_clips_are_turkish(guided) -> None:  # noqa: F811
    job, variant, _rev = guided
    seen = {
        "m0": "Playing football on an outdoor turf pitch at night",
        "m1": "Running up stairs and celebrating in a rustic bar",
        "m3": "Guests hugging at an airport arrivals hall",
    }
    ops = [
        _add("Pre-wedding soccer", 0, 2),
        _add("Running up stairs", 2, 4),
        _add("Airport pickup", 6, 8),
    ]
    with reply_language_for("tr"):
        out = _parse_ops(_seen_snapshot(job, variant, seen), ops, utterance=ASK_DESCRIBE_TR)
    assert [op["text"] for op in out.ops] == ["Running up stairs", "Airport pickup"]
    assert (
        'Klip 1 gördüğüm kadarıyla "wedding" gibi görünmüyor, o yüzden bu ifadeyi koymadım. '
        "Ne olduğunu söyle ya da gördüğümü anlatmamı iste."
    ) in out.reply
    assert "klip 2: Running up stairs; klip 4: Airport pickup" in out.reply
    assert "Klip 3 dışarıda kaldı" in out.reply
    assert not any(word in out.reply for word in ("I wrote", "I left out", "clip 2", "Tell me"))


def test_unseen_and_skipped_caption_notes_are_turkish(guided) -> None:  # noqa: F811
    job, variant, _rev = guided
    with reply_language_for("tr"):
        dropped = _parse_ops(
            _snapshot(job, variant), [_add("Airport pickup", 0, 2)], utterance=ASK_DESCRIBE_TR
        )
        kept = _parse_ops(
            _seen_snapshot(job, variant),
            [_add("Airport pickup", 0, 2), _add("Dancing", 2, 4), _add("Something", 4, 6)],
            utterance=ASK_DESCRIBE_TR,
        )
    assert dropped.ops == []
    assert dropped.reply == (
        "Bu kliplerin ne gösterdiğini anlayamadım, o yüzden onlara yazı yazmadım. "
        "Her klibin ne olduğunu söyle (örneğin 'klip 1 havalimanı karşılaması'), "
        "ben de ekleyeyim."
    )
    assert "Klip 4 için yazı eklemedim." in kept.reply


@pytest.mark.parametrize("seen", [True, False])
def test_descriptive_caption_clarification_is_turkish(guided, seen) -> None:  # noqa: F811
    job, variant, _rev = guided
    snapshot = _seen_snapshot(job, variant) if seen else _snapshot(job, variant)
    ops = [{"op": "label_each_clip", "source": "facts"}]
    ask = "her klibe ne olduğunu yaz"
    english = _parse_ops(snapshot, ops, utterance=ASK_DESCRIBE_TR)
    with reply_language_for("tr"):
        out = _parse_ops(snapshot, ops, utterance=ask)
    assert out.ops == [] and out.outcome == "clarification"
    assert out.reply != english.reply
    if seen:
        assert out.reply.startswith("Yer ve saat etiketleri her klibin ne gösterdiğini anlatamaz.")
        assert "write a short caption" in english.reply
    else:
        assert out.reply.startswith("Klipleri yalnızca elimdeki bilgilerle")
        assert "Tell me what each clip is" in english.reply


def test_label_each_clip_refusals_follow_the_chat_language(guided) -> None:  # noqa: F811
    job, variant, _rev = guided
    snapshot = build_editor_snapshot(job, variant)
    snapshot["label_facts"] = True
    for row in snapshot["text_bars"]:
        if row.get("clip_id"):
            row["edited"] = True
    for slot in snapshot["slots"]:
        slot["facts"] = [{"kind": "place", "value": "Lisbon"}]

    def detail(utterance: str) -> str:
        state = _ParseState(0.9)
        state.utterance = utterance
        assert _parse_op({"op": "label_each_clip", "source": "facts"}, snapshot, state) is None
        return state.rejection_reasons[0]["detail"]

    with reply_language_for("tr"):
        assert detail("etiketler klipleriyle hizalı değil").startswith(
            "Etiketlerin zamanlamasını bu şekilde düzeltemem"
        )
        assert detail("her klibe yeri yaz") == (
            "Elle düzenlediğin etiketleri korudum, başka bir klibe etiket gerekmiyor."
        )
    assert detail("her klibe yeri yaz") == (
        "The labels you edited by hand were kept, and no other clip needs a label."
    )
    assert detail("etiketler klipleriyle hizalı değil").startswith("I can't fix label timing")


def test_unavailable_edit_and_value_notes_are_turkish() -> None:
    ops = [{"op": "edit_text", "bar_index": 0, "text": "yeni"}]
    english = _parse_basic(ops, allowed=["style"])
    assert english.rejection_reasons[0]["detail"] == "I can't change that on this edit yet."
    with reply_language_for("tr"):
        out = _parse_basic(ops, allowed=["style"])
        font = _parse_basic(
            [{"op": "patch_text_style", "bar_index": 0, "patch": {"font_family": "Papyrus"}}]
        )
        case = _parse_basic(
            [{"op": "patch_text_style", "bar_index": 0, "patch": {"text_case": "weird"}}]
        )
    assert out.rejection_reasons[0]["detail"] == "Bunu bu düzenlemede henüz değiştiremiyorum."
    assert _honest_outcome(out, [])[1] == "Bunu bu düzenlemede henüz değiştiremiyorum."
    assert "'Papyrus' adında bir yazı tipim yok" in font.reply
    assert "Harf biçimini 'weird' yapamadım" in case.reply
