"""KRI-520 (W1): Turkish chats can answer and read the choice questions.

Covers typed Turkish answers (delegation, ordinals, decline/skip, İ/I/ı folding, ASCII
typed Turkish), Turkish option labels that still accept their English label, the creative
copy (title wording) questions, and the small creator-visible strings of the clip picker,
song order, speech cleanup and copy gate. English behavior must not move.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from app.services.choice_questions import (
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_ORDER_BASIS,
    CONFLICT_TITLE_TEXT,
    OPT_ATTACHMENT_ORDER,
    OPT_CHRONOLOGICAL,
    OPT_EXTEND,
    OPT_FEWER,
    OPT_GROUP_FIRST,
    OPT_NO_TITLE,
    OPT_UNORDERED,
    ask_user_choice,
    build_choice_question,
    choice_notices,
    choice_question_text,
    collect_conflicts,
    delegated_choice,
    detect_order_vs_group,
    match_open_choice,
    normalize_reply,
    title_text_choice,
)
from app.services.clip_selection import clip_question_text
from app.services.creative_copy_decisions import (
    OPT_APPROVE,
    OPT_CANCEL,
    OPT_GENERATE,
    OPT_REVISE,
    OPT_WRITE_MY_OWN,
    authorship_question,
    localize_question,
    media_digest,
    wording_question,
)
from app.services.creative_copy_gate import creative_copy_problem
from app.services.song_order import song_order_question_text
from app.services.speech_cleanup_decision import SPEECH_CLEANUP_CONFLICT_COPY

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
MIXED = [("football", ["c0", "c2", "c4"]), ("dodgeball", ["c1", "c3", "c5"])]


def _mixed(**kw):
    return detect_order_vs_group(
        wants_capture_order=True,
        groups=MIXED,
        clips=[(f"c{i}", T0 + timedelta(minutes=i * 10)) for i in range(6)],
        **kw,
    )


def _question(candidate) -> dict:
    return build_choice_question(candidate)


def _rows(count: int, *, dated: bool = True) -> list[dict]:
    rows = []
    for i in range(count):
        row: dict = {"media_id": f"c{i:02d}", "kind": "video", "duration_s": 3.0}
        if dated:
            row["capture"] = {"capture_time": f"2026-09-20T09:{i % 60:02d}:00Z"}
        rows.append(row)
    return rows


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements))


def _duration_choice():
    timing = BriefRequirement(
        id="r1",
        kind="timing",
        scope="global",
        description="Keep 15 seconds",
        facts={"duration_s": 15},
    )
    found = collect_conflicts({}, _brief(timing), {"clip_assignments": _rows(30)})
    assert [c.kind for c in found] == [CONFLICT_DURATION_VS_COUNT]
    return found[0]


def _order_choice():
    order = BriefRequirement(
        id="r2",
        kind="order",
        scope="global",
        description="in the order I filmed them",
        facts={"key": "capture_time"},
    )
    found = collect_conflicts({}, _brief(order), {"clip_assignments": _rows(4, dated=False)})
    assert [c.kind for c in found] == [CONFLICT_ORDER_BASIS]
    return found[0]


# ── normalisation: İ / I / ı and ASCII-typed Turkish ──────────────────────────


def test_normalize_reply_folds_turkish_capitals_and_dotless_i() -> None:
    assert normalize_reply("İLERİ") == "ileri"
    assert normalize_reply("BAŞLIK YOK") == "başlik yok"
    assert normalize_reply("Kısa Kalsın!") == normalize_reply("KISA KALSIN")
    assert normalize_reply("ILIK") == normalize_reply("ılık")


@pytest.mark.parametrize(
    "text",
    ["Keep it SHORT!", "Option 2.", "  you   choose ", "Résumé", "Straße", "ﬁne", "Don't skip"],
)
def test_normalize_reply_english_is_unchanged(text: str) -> None:
    old = re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()
    assert normalize_reply(text) == old


# ── typed Turkish declines ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        "BAŞLIK YOK",
        "başlık yok",
        "baslik yok",
        "Hayır",
        "hayir",
        "HAYIR!",
        "yok",
        "Atla",
        "geç",
        "gec",
        "başlık istemiyorum",
        "Başlık istemiyorum.",
        "başlığa gerek yok",
        "basliga gerek yok",
        "başlık koyma",
        "Başlıksız devam et",
        "başlık olmasın",
    ],
)
def test_turkish_no_title_replies_decline_the_title(reply: str) -> None:
    question = _question(title_text_choice(["r1"]).candidate())
    assert match_open_choice(question, reply) == OPT_NO_TITLE


@pytest.mark.parametrize(
    "reply", ["Yaz Tatili", "başlık: yok artık", "no plans", "hayır dedim sana"]
)
def test_real_words_are_never_taken_for_a_decline(reply: str) -> None:
    question = _question(title_text_choice(["r1"]).candidate())
    assert match_open_choice(question, reply) is None


def test_creative_copy_skip_accepts_turkish_declines_everywhere() -> None:
    for build in (
        lambda: authorship_question(target="opening_title", dependency_digest="d"),
        lambda: wording_question(
            target="opening_title", candidate="Hafta sonu", dependency_digest="d"
        ),
    ):
        question = build()
        for reply in (
            "hayır",
            "HAYIR",
            "Hayir",
            "atla",
            "geç",
            "başlık yok",
            "BAŞLIK YOK",
            "baslik yok",
            "başlık istemiyorum",
            "gerek yok",
            "skip",
            "no title",
        ):
            assert match_open_choice(question, reply) == OPT_CANCEL, reply
        # "Yok" answers "Aklında bir fikir var mı?" ("I don't have one"); never a skip.
        assert match_open_choice(question, "yok") != OPT_CANCEL


# ── delegation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        "sen seç",
        "Sen Seç!",
        "sen sec",
        "sen karar ver",
        "SEN KARAR VER",
        "sana bırakıyorum",
        "Sana bıraktım",
        "sürpriz yap",
        "surpriz yap",
        "dilediğin gibi",
        "dilediğin gibi yap",
    ],
)
def test_turkish_delegation_picks_the_recommended_option(reply: str) -> None:
    question = _question(_mixed(noun="sport"))
    assert match_open_choice(question, reply) is None
    assert delegated_choice(question, reply) == OPT_GROUP_FIRST


@pytest.mark.parametrize("reply", ["sen seç ama kısa olsun", "bence ikincisi", "yarın sen seç"])
def test_a_turkish_instruction_that_is_not_a_delegation_stays_unanswered(reply: str) -> None:
    question = _question(_mixed(noun="sport"))
    assert delegated_choice(question, reply) is None


def test_turkish_delegation_never_drops_a_title_or_approves_copy() -> None:
    title = _question(title_text_choice(["r1"]).candidate())
    assert delegated_choice(title, "sen seç") is None
    copy = authorship_question(target="opening_title", dependency_digest="d")
    assert delegated_choice(copy, "sen seç") is None


def test_english_delegation_is_unchanged() -> None:
    question = _question(_mixed(noun="sport"))
    assert delegated_choice(question, "You choose") == OPT_GROUP_FIRST
    assert delegated_choice(question, "up to you.") == OPT_GROUP_FIRST
    assert delegated_choice(question, "whatever") is None
    assert delegated_choice(question, "I don't care") is None
    # Their Turkish twins are non-answers too.
    assert delegated_choice(question, "fark etmez") is None
    assert delegated_choice(question, "hangisi olursa") is None


def test_one_option_question_takes_ordinals_as_the_creators_words() -> None:
    title = _question(title_text_choice(["r1"]).candidate())
    for words in ("İlk", "ilk", "birinci"):
        assert match_open_choice(title, words) is None, words
    assert match_open_choice(title, "1") == OPT_NO_TITLE  # unchanged English list number


# ── ordinals ──────────────────────────────────────────────────────────────────


def _three_options() -> dict:
    return {
        "options": [
            {"key": "a", "label": "Alpha plan"},
            {"key": "b", "label": "Beta plan"},
            {"key": "c", "label": "Gamma plan"},
        ]
    }


@pytest.mark.parametrize(
    ("reply", "key"),
    [
        ("birinci", "a"),
        ("Birinci seçenek", "a"),
        ("ilki", "a"),
        ("İLK", "a"),
        ("ilk", "a"),
        ("birincisi", "a"),
        ("1. seçenek", "a"),
        ("seçenek 1", "a"),
        ("Seçenek 1", "a"),
        ("1.", "a"),
        ("1", "a"),
        ("bir numara", "a"),
        ("1 numara", "a"),
        ("ikinci", "b"),
        ("ikincisi", "b"),
        ("ikincisini", "b"),
        ("2. seçenek", "b"),
        ("2 numara", "b"),
        ("iki numara", "b"),
        ("üçüncü", "c"),
        ("ÜÇÜNCÜ", "c"),
        ("ucuncu", "c"),
        ("üçüncüsü", "c"),
        ("3. şık", "c"),
        ("üç numara", "c"),
    ],
)
def test_turkish_ordinals_pick_the_numbered_option(reply: str, key: str) -> None:
    assert match_open_choice(_three_options(), reply) == key


@pytest.mark.parametrize(
    "reply", ["bir", "iki", "dördüncü", "kırmızı", "seçenek", "ikinci ve üçüncü"]
)
def test_unclear_turkish_numbers_are_not_answers(reply: str) -> None:
    assert match_open_choice(_three_options(), reply) is None


def test_english_numbering_is_unchanged() -> None:
    question = _three_options()
    assert match_open_choice(question, "2") == "b"
    assert match_open_choice(question, "Option 3") == "c"
    assert match_open_choice(question, "number 1") == "a"
    assert match_open_choice(question, "beta plan") == "b"
    assert match_open_choice(question, "plan") is None
    assert match_open_choice(question, "") is None


def test_ascii_fallback_never_merges_two_options_that_differ_by_an_accent() -> None:
    question = {
        "options": [{"key": "a", "label": "Cafe"}, {"key": "b", "label": "Café"}],
    }
    assert match_open_choice(question, "café") == "b"
    assert match_open_choice(question, "cafe") == "a"
    assert match_open_choice(question, "CAFÉ") == "b"


# ── Turkish labels keep their English label as an alias ───────────────────────


def test_order_vs_group_in_english_is_unchanged() -> None:
    found = _mixed(noun="sport")
    assert found.options[0].label == "Group by sport, chronological inside each sport"
    assert found.options[1].label == "Keep it strictly chronological; sports may interleave"
    assert found.options[0].aliases == () and found.options[1].aliases == ()
    text = choice_question_text(found)
    assert text.splitlines()[0].startswith("You asked for a chronological video")
    assert "1. Group by sport, chronological inside each sport (recommended)" in text
    assert text.endswith("Tap an option, or tell me in your own words.")


def test_order_vs_group_in_turkish_has_turkish_copy_and_english_aliases() -> None:
    english = _mixed(noun="sport")
    with reply_language_for("tr"):
        found = _mixed(noun="sport")
        text = choice_question_text(found)
    assert "spor" in found.options[0].label
    assert found.options[0].label != english.options[0].label
    assert english.options[0].label in found.options[0].aliases
    assert english.options[1].label in found.options[1].aliases
    assert "Maalesef" in text and "(önerilen)" in text
    assert text.endswith("Bir seçeneğe dokun ya da kendi sözlerinle anlat.")
    assert "football ve dodgeball" in text
    question = _question(found)
    # Turkish label, the English label the client may echo, the digit and a spelled ordinal.
    assert match_open_choice(question, found.options[0].label) == OPT_GROUP_FIRST
    assert match_open_choice(question, english.options[0].label) == OPT_GROUP_FIRST
    assert match_open_choice(question, english.options[1].label) == OPT_CHRONOLOGICAL
    assert match_open_choice(question, found.options[1].label.upper()) == OPT_CHRONOLOGICAL
    assert match_open_choice(question, "ikinci") == OPT_CHRONOLOGICAL
    assert match_open_choice(question, "kronolojik kalsın") == OPT_CHRONOLOGICAL


def test_the_planners_english_noun_is_rendered_in_turkish_only_in_a_turkish_chat() -> None:
    with reply_language_for("tr"):
        sport = _mixed(noun="sport")
        group = _mixed(noun="group")
        other = _mixed(noun="takım")
        default = _mixed()
    assert "spor bazında" in sport.intro and "Her spor bir blok" in sport.options[0].label
    assert "grup bazında" in group.intro and "Her grup bir blok" in group.options[0].label
    assert "takım bazında" in other.intro  # unknown nouns are kept as written
    assert "grup bazında" in default.intro
    assert "sport" not in sport.intro and "sport" not in sport.options[1].label
    assert "grouped by sport" in _mixed(noun="sport").intro  # unbound stays English


def test_a_turkish_question_is_still_answered_after_the_chat_goes_english() -> None:
    with reply_language_for("tr"):
        question = _question(_mixed(noun="group"))
    # Replayed later with no language bound: the stored labels and aliases decide.
    assert match_open_choice(question, "sen seç") is None
    assert delegated_choice(question, "sen seç") == OPT_GROUP_FIRST
    assert match_open_choice(question, "Group by group, chronological inside each group") == (
        OPT_GROUP_FIRST
    )


def test_duration_question_turkish_labels_and_typed_answers() -> None:
    english = _duration_choice()
    with reply_language_for("tr"):
        found = _duration_choice()
        text = choice_question_text(found.candidate())
    assert found.input_digest == english.input_digest  # the digest never depends on language
    extend, fewer = found.options
    assert extend.key == OPT_EXTEND and fewer.key == OPT_FEWER
    assert extend.label.startswith("Süreyi") and "saniyeye uzat" in extend.label
    assert "klip" in fewer.label
    assert english.options[0].label in extend.aliases
    assert english.options[1].label in fewer.aliases
    assert found.intro == 'İsteğinde "Keep 15 seconds" yazıyor ama bu düzenlemede 30 klip var.'
    assert "Maalesef" in text and "30 klibin" in text
    question = _question(found.candidate())
    for reply in (
        "uzat",
        "Süreyi uzat",
        "daha uzun yap",
        "birinci",
        extend.label,
        english.options[0].label,
    ):
        assert match_open_choice(question, reply) == OPT_EXTEND, reply
    for reply in ("daha az klip", "KISA KALSIN", "kisa kalsin", "ikinci", english.options[1].label):
        assert match_open_choice(question, reply) == OPT_FEWER, reply
    # English answers keep working in a Turkish question and vice versa.
    english_question = _question(english.candidate())
    assert match_open_choice(english_question, "extend it") == OPT_EXTEND
    assert match_open_choice(english_question, "uzat") == OPT_EXTEND
    assert match_open_choice(english_question, "kısa kalsın") == OPT_FEWER


def test_duration_question_in_english_is_unchanged() -> None:
    found = _duration_choice()
    assert found.intro == 'Your brief says "Keep 15 seconds" and this edit uses 30 clips.'
    assert found.options[0].label == "Extend it to 24 seconds"
    assert found.options[1].label == "Keep 15 seconds with 18 clips"
    assert found.options[0].description == (
        "Every clip gets at least 0.8 seconds, so all 30 stay in."
    )
    assert found.reason == (
        "30 clips can't each stay on screen long enough to be seen in 15 seconds "
        "(each needs about 0.8 seconds)."
    )


def _placement_choice():
    brief = _brief(
        BriefRequirement(
            id="r7",
            kind="text",
            scope="per_clip",
            literal="Km 1",
            description="the first kilometre",
        )
    )
    found = collect_conflicts({"shot_labels": ["Km 1", "Km 2", "Km 1"]}, brief, {})
    assert [c.kind for c in found] == ["text_placement"]
    return found[0]


def test_text_placement_question_turkish_copy_and_shot_aliases() -> None:
    english = _placement_choice()
    assert english.options[0].label == "On shot 1"
    assert english.reason == (
        "those words appear on shot 1 and shot 3 of the draft, so I can't tell which shot you mean."
    )
    with reply_language_for("tr"):
        found = _placement_choice()
    assert found.intro == 'Bir sahne için "Km 1" yazısını verdin.'
    assert "1. sahne ve 3. sahne" in found.reason
    assert [o.label for o in found.options] == ["1. sahnede", "3. sahnede"]
    assert "On shot 3" in found.options[1].aliases
    question = _question(found.candidate())
    assert match_open_choice(question, "3. sahne") == "shot_3"
    assert match_open_choice(question, "Sahne 1") == "shot_1"
    assert match_open_choice(question, "ikinci") == "shot_3"
    assert match_open_choice(question, "shot 3") == "shot_3"
    assert match_open_choice(question, "On shot 1") == "shot_1"


def test_order_basis_turkish_aliases_and_the_fark_etmez_collision() -> None:
    english = _order_choice()
    with reply_language_for("tr"):
        found = _order_choice()
    assert found.options[0].key == OPT_ATTACHMENT_ORDER
    assert english.options[0].label in found.options[0].aliases
    assert found.intro == "Klipleri çektiğin sırayla istedin."
    assert found.reason == "hiçbir klibinde çekim saati yok, bu yüzden onları o sıraya koyamıyorum."
    question = _question(found.candidate())
    for reply in ("eklediğim sıra", "Yüklediğim sıra", "EKLEME SIRASI", "ekledigim sira"):
        assert match_open_choice(question, reply) == OPT_ATTACHMENT_ORDER, reply
    # Restating "the order I filmed" is not agreeing to the order the clips were added.
    for reply in ("çektiğim sıra", "ÇEKİM SIRASI", "cekim sirasi"):
        assert match_open_choice(question, reply) != OPT_ATTACHMENT_ORDER, reply
    for reply in ("sırasız", "Sıra olmasın", "sira olmasin", "fark etmez", "sıra olmadan devam et"):
        assert match_open_choice(question, reply) == OPT_UNORDERED, reply
    # "fark etmez" answers the order question itself, before any delegation default.
    assert match_open_choice(question, "fark etmez") is not None


def test_title_text_question_turkish_copy_keeps_the_english_label() -> None:
    english = title_text_choice(["r1"])
    with reply_language_for("tr"):
        found = title_text_choice(["r1"])
        text = choice_question_text(found.candidate())
        note_tr = choice_notices({"choice_answers": [_answer(CONFLICT_TITLE_TEXT, OPT_NO_TITLE)]})
    assert found.options[0].label == "Başlıksız devam et"
    assert "Continue without a title" in found.options[0].aliases
    assert "Başlıksız devam et" in text and "aynen kullanırım" in text
    assert note_tr == ["Kelimeleri söylemediğin için başlığı koymuyorum."]
    assert english.options[0].label == "Continue without a title"
    question = _question(found.candidate())
    assert match_open_choice(question, "Continue without a title") == OPT_NO_TITLE
    assert match_open_choice(question, "BAŞLIKSIZ DEVAM ET") == OPT_NO_TITLE
    assert match_open_choice(question, "baslıksız devam et") == OPT_NO_TITLE


def _answer(kind: str, option: str, source: str = "creator") -> dict:
    return {"conflict": kind, "kind": kind, "option": option, "source": source}


def test_disclosures_speak_the_chat_language() -> None:
    answers = {
        "choice_answers": [_answer(CONFLICT_DURATION_VS_COUNT, OPT_EXTEND, "creator_delegated")]
    }
    assert choice_notices(answers) == [
        "I extended it so every clip stays on screen long enough to be seen. "
        "(You left it to me, so I went with it.)"
    ]
    with reply_language_for("tr"):
        (note,) = choice_notices(answers)
    assert note.startswith("Süreyi uzattım") and note.endswith("bunu seçtim.)")


def test_ask_user_choice_footer_is_localized_but_the_models_options_are_not() -> None:
    asked = ask_user_choice("Hangi şarkı?", "song", ["Sakin", "Hareketli"])
    assert asked is not None
    assert asked[0].endswith("Tap an option, or tell me in your own words.")
    with reply_language_for("tr"):
        asked = ask_user_choice("Hangi şarkı?", "song", ["Sakin", "Hareketli"])
    assert asked is not None
    text, payload = asked
    assert text.splitlines()[:3] == ["Hangi şarkı?", "1. Sakin", "2. Hareketli"]
    assert text.endswith("Bir seçeneğe dokun ya da kendi sözlerinle anlat.")
    assert [o["label"] for o in payload["options"]] == ["Sakin", "Hareketli"]
    assert match_open_choice(payload, "ikincisi") == "option_2"
    assert match_open_choice(payload, "HAREKETLİ") == "option_2"


# ── creative copy questions ───────────────────────────────────────────────────

_TR_LABELS = ("Bir fikrim var", "Bir tane yaz", "Bu ifadeyi kullan", "Düzenle", "Atla")


def test_creative_copy_questions_are_english_when_unbound() -> None:
    question = authorship_question(target="opening_title", dependency_digest="d")
    assert [o["label"] for o in question["options"]] == [
        "I have an idea",
        "Generate one",
        "Skip it",
    ]
    wording = wording_question(
        target="opening_title", candidate="Hafta sonu", dependency_digest="d"
    )
    assert [o["label"] for o in wording["options"]] == ["Use this wording", "Revise it", "Skip it"]
    assert "aliases" not in question["options"][0]


def test_creative_copy_builders_localize_automatically_in_a_turkish_chat() -> None:
    with reply_language_for("tr"):
        question = authorship_question(target="opening_title", dependency_digest="d")
        wording = wording_question(
            target="opening_title", candidate="Hafta sonu", dependency_digest="d"
        )
    assert [o["label"] for o in question["options"]] == [
        _TR_LABELS[0],
        _TR_LABELS[1],
        _TR_LABELS[4],
    ]
    assert [o["label"] for o in wording["options"]] == [_TR_LABELS[2], _TR_LABELS[3], _TR_LABELS[4]]
    by_key = {o["key"]: o for o in question["options"]}
    assert "I have an idea" in by_key[OPT_WRITE_MY_OWN]["aliases"]
    assert "Generate one" in by_key[OPT_GENERATE]["aliases"]
    assert "Skip it" in by_key[OPT_CANCEL]["aliases"]
    # Every spelling still answers: Turkish label, English label, typed Turkish.
    assert match_open_choice(question, "Bir fikrim var") == OPT_WRITE_MY_OWN
    assert match_open_choice(question, "I have an idea") == OPT_WRITE_MY_OWN
    assert match_open_choice(question, "BİR TANE YAZ") == OPT_GENERATE
    assert match_open_choice(question, "Generate one") == OPT_GENERATE
    assert match_open_choice(question, "sen yaz") == OPT_GENERATE
    assert match_open_choice(question, "ATLA") == OPT_CANCEL
    assert match_open_choice(question, "Skip it") == OPT_CANCEL
    assert match_open_choice(wording, "bu ifadeyi kullan") == OPT_APPROVE
    assert match_open_choice(wording, "Use this wording") == OPT_APPROVE
    assert match_open_choice(wording, "düzenle") == OPT_REVISE
    assert match_open_choice(wording, "Revise it") == OPT_REVISE
    # Consent stays a deliberate answer: a bare "tamam"/"evet" approves nothing.
    assert match_open_choice(wording, "tamam") is None
    assert match_open_choice(wording, "evet") is None


def test_localize_question_is_idempotent_and_keeps_the_english_alias() -> None:
    once = localize_question(
        authorship_question(target="opening_title", dependency_digest="d"), "tr"
    )
    twice = localize_question(
        localize_question(authorship_question(target="opening_title", dependency_digest="d"), "tr"),
        "tr-TR",
    )
    assert once["options"] == twice["options"]
    with reply_language_for("tr"):
        auto = authorship_question(target="opening_title", dependency_digest="d")
    assert localize_question(auto, "tr")["options"] == auto["options"]
    for option in twice["options"]:
        aliases = option.get("aliases") or []
        assert len(aliases) == len(set(aliases))
    cancel = next(o for o in twice["options"] if o["key"] == OPT_CANCEL)
    assert cancel["label"] == "Atla" and "Skip it" in cancel["aliases"]


def test_localize_question_into_another_language_still_keeps_earlier_labels() -> None:
    with reply_language_for("tr"):
        question = authorship_question(target="opening_title", dependency_digest="d")
    localize_question(question, "es")
    write = next(o for o in question["options"] if o["key"] == OPT_WRITE_MY_OWN)
    assert write["label"] == "Tengo una idea"
    assert "I have an idea" in write["aliases"] and "Bir fikrim var" in write["aliases"]
    assert match_open_choice(question, "bir fikrim var") == OPT_WRITE_MY_OWN


# ── the copy gate and the other creator-visible strings ───────────────────────


def _gate_events():
    question = authorship_question(target="opening_title", dependency_digest=media_digest(None))
    return [("assistant", {"choice_question": question})]


def test_copy_gate_message_speaks_the_chat_language() -> None:
    assert creative_copy_problem(_gate_events(), None) == (
        "Let's finish choosing the wording before preparing the edit."
    )
    with reply_language_for("tr"):
        assert creative_copy_problem(_gate_events(), None) == (
            "Düzenlemeyi hazırlamadan önce ekrandaki yazıyı netleştirelim."
        )


def test_copy_gate_other_messages_have_turkish_twins() -> None:
    digest = media_digest(None)
    question = authorship_question(target="opening_title", dependency_digest=digest)
    cancelled = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": OPT_CANCEL,
                }
            },
        ),
    ]
    english = {
        creative_copy_problem(cancelled, None),
        creative_copy_problem(cancelled, {}, strategy={"opening_title": "Hafta sonu"}),
    }
    with reply_language_for("tr"):
        turkish = {
            creative_copy_problem(cancelled, None),
            creative_copy_problem(cancelled, {}, strategy={"opening_title": "Hafta sonu"}),
        }
    assert english == {
        "Let's review the edit plan with your saved wording before changing this video.",
        "You asked to leave that text out. Let's update the plan before rendering.",
    }
    assert len(turkish) == 2 and turkish.isdisjoint(english)


def test_clip_question_text_is_localized() -> None:
    one = [{"label": "dodgeball"}]
    many = [{"label": f"grup {i}"} for i in range(6)]
    assert clip_question_text(one) == (
        'I couldn\'t tell which of your clips show "dodgeball". '
        "Tap the clips that do, or tell me there aren't any."
    )
    assert clip_question_text(many) == (
        'I couldn\'t tell which of your clips match "grup 0", "grup 1", "grup 2", "grup 3" '
        "and 2 more. Tap the clips for each one, or mark the ones that have none."
    )
    with reply_language_for("tr"):
        assert clip_question_text(one) == (
            'Kliplerinden hangilerinde "dodgeball" olduğunu anlayamadım. '
            "Olanlara dokun ya da hiç olmadığını söyle."
        )
        text = clip_question_text(many)
    assert '"grup 0", "grup 1", "grup 2", "grup 3" ve 2 tane daha' in text
    assert "Her biri için klipleri seç" in text


def test_song_order_question_text_is_localized() -> None:
    def question(unsure: int):
        return SimpleNamespace(items=[SimpleNamespace(status="ambiguous")] * unsure)

    assert "one of your clips" in song_order_question_text(question(1))
    assert song_order_question_text(question(3)).startswith("I'm not sure where 3 of your clips")
    with reply_language_for("tr"):
        assert song_order_question_text(question(1)).startswith("Kliplerinden birinin şarkıda")
        assert song_order_question_text(question(3)).startswith("Kliplerinden 3 tanesinin")


def test_speech_cleanup_conflict_copy_speaks_the_chat_language() -> None:
    codes = list(SPEECH_CLEANUP_CONFLICT_COPY)
    assert len(codes) == 5 and "speech_cleanup_pending" in SPEECH_CLEANUP_CONFLICT_COPY
    assert SPEECH_CLEANUP_CONFLICT_COPY["speech_cleanup_pending"] == (
        "The speech check is still running. I'll ask again once it finishes."
    )
    assert SPEECH_CLEANUP_CONFLICT_COPY["speech_cleanup_choice_required"] == (
        "Choose how to handle the pauses and filler words before I render."
    )
    english = {code: SPEECH_CLEANUP_CONFLICT_COPY[code] for code in codes}
    with reply_language_for("tr"):
        turkish = {code: SPEECH_CLEANUP_CONFLICT_COPY[code] for code in codes}
        assert (
            SPEECH_CLEANUP_CONFLICT_COPY.get("speech_cleanup_failed")
            == turkish["speech_cleanup_failed"]
        )
    assert all(turkish[c] != english[c] for c in codes)
    assert (
        turkish["speech_cleanup_pending"]
        == "Konuşma kontrolü hâlâ sürüyor. Bitince sana tekrar soracağım."
    )
