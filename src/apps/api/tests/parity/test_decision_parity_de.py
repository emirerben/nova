"""German phrasings of the EN/TR decision parity cases (KRI-1362, "B").

Every case in ``test_decision_parity.CASES`` calls a real rule-based decision with an English
phrase and checks the Turkish variants decide the same. Here each decision gets a natural German
phrasing (``de``), run with German replies switched on (``kria_german_replies_enabled``), so
language detection that reads German behind the switch is exercised too. The German result must
equal the English one.

Like the Turkish suite this is a measurement plus a ratchet: a decision with no German path today
is a strict xfail whose reason says what is missing, so the list below is the honest gap list.
Closing a gap makes the case XPASS, which fails strictly: drop it from ``GAPS``.

The normalization cases (stage ``normalization``) fold or case Turkish spellings and have no
English counterpart, so they get no German phrasing; German letter folding is tested here
directly (``test_german_letters_fold``), with the switch-gated language helpers.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.parity.test_decision_parity import CASES, Case, _safe_call

# case id -> what a German creator would type for the same ask.
GERMAN: dict[str, str] = {
    "D01": "Wie ist der Stand?",
    "D02": "Wie läuft es?",
    "DC1-6": "Wie läuft's?",
    "D03": "Was kannst du?",
    "D04": "Du willst den Titel größer und golden.",
    "D10": "Du entscheidest",
    "D11": "Das überlasse ich dir",
    "D12": "Nimm alle Videos",
    "D13": "Behalte die Reihenfolge der Clips bei",
    "D14": "Füge den Whoosh-Soundeffekt hinzu",
    "D15": 'Titel "Yaz Tatili"',
    "D16": 'Abschlusstitel "Görüşürüz"',
    "D17": "Schnelles Tempo",
    "D18": "Fotos jeweils 0,3 Sekunden zwischen den Videos",
    "D19": "Fotos jeweils 0,3 Sekunden zwischen den Videos",
    "DC1-1": "Zeig den Titel zwei Sekunden lang",
    "DC1-2": "Füge ein paar Soundeffekte hinzu",
    "DC1-3": "Setz das Timing zurück",
    "DC1-4": "Ändere das Timing nicht",
    "DC1-5": "Mach die Fotos nicht zu schnell",
    "DC1-7": "Füge dem Video einen Soundeffekt hinzu",
    "DC1-8": "Nimm einen anderen Soundeffekt, nicht den Whoosh",
    "DC1-9": "Vergiss nicht, den Whoosh-Soundeffekt hinzuzufügen",
    "DC1-10": "Setz das Timing nicht zurück",
    "DC1-11": "Verwende nicht den Titel 'Emir'",
    "DC1-12": "Abspanntitel 'Görüşürüz'",
    "DC1-13": "Ersetze im Text 'Sen' durch 'Siz'",
    "DC1-14": "Zeig den Titel 2 Sekunden lang",
    "DC1-15": "Ist das Video fertig?",
    "DC1-16": "Ein schnelles Tempo",
    "DC1-17": "Verwende jeden Clip",
    "D20": "Fotos sehr schnell, Videos länger",
    "D21": "Mach es 30 Sekunden lang",
    "D22": "Wechsle alle 2 Sekunden zwischen den Clips",
    "D23": "Du kannst Clips wiederholen",
    "D24": "Schneide die Fotos nicht zu",
    "D25": "Fotos im Vollbild",
    "D26": 'Schreib nur "Summer 26"',
    "D27": 'Mein Freund sagte "das ist der beste Tag überhaupt"',
    "D28": 'Voiceover: "endlich haben wir es nach oben geschafft"',
    "D29": "Verwende meine Stimme aus den Clips",
    "D30": "Verwende immer die Schrift Inter",
    "D31": "Setz den Titel immer nach oben",
    "D32": "Verwende diese Schrift in jedem Video",
    "D33": "Fang mit dem Strandclip an",
    "D34": "Verwende vorübergehend gelben Text",
    "D35": "Ändere die Schriftart",
    "D36": "Englische Untertitel",
    "D37": "Türkische Untertitel",
    "D37b": "Untertitel auf Spanisch",
    "D37c": "Englische Untertitel für die englischen Teile, türkische für die türkischen",
    "O18": "Mach mein Reisevideo schwungvoll und füge Untertitel hinzu",
    "O27": "Mach mein Reisevideo schwungvoll und füge Untertitel hinzu",
    "D38": "äh",
    "D39": "halt",
    "D40": "äh also, die Sache ist die",
    "C2-01": "Willst du den Titel golden oder gelb?",
    "C2-02": "Ein köstlicher Kaffee",
    "C2-03": "drei eins",
    "C2-05": "Ich brauche mehr Informationen, um diesen Hook zu schreiben",
    "C2-04": "Der Text soll später kommen",
    "C2-06": "Vermeide immer die Schrift Inter",
    "C2-07": "Ich habe diesen Kaffee probiert und geliebt",
    "C2-08": "Ben steht den Kickflip",
    "C2-09": "Für mehr Infos, folge mir",
    "C2-10": "Bitte keine Spielstände",
    "C2-11": "Zeig die Spielstände",
    "C2-12": (
        "Ich habe die Kaffeeclips nach vorne gesetzt, weil du einen ruhigen Anfang wolltest."
    ),
    "C2-13": (
        'Schreib den Satz "heute zeige ich dir mein Lieblingscafé" aus dem Voiceover auf den '
        "Bildschirm"
    ),
    "C2-14": "Oh nein, schau dir das an",
    "C2-15": 'Nur "Paris 2024" auf dem Bildschirm',
    "C2-16": "drei zu eins",
    "D50": "Füge wie immer ein Löwengebrüll zum Video hinzu",
    "D51": "Verwende nie Gelb",
    "D52": "Verwende diese Schrift nicht mehr",
    "D53": "Mach es nur dieses Mal gelb",
    "D54": "Lösch den dritten Clip",
    "D55": "Lass den Titel zwei Sekunden stehen",
    "O37": "Sonnenlicht, so schön! Grüße",
    "O38": "Sonnenlicht, so schön! Grüße",
    "O39": "Sonnenlicht, so schön! Grüße",
    "O40": "Sonnenlicht, so schön! Grüße",
    "O41": "Sonnenlicht, so schön! Grüße",
    "O41z": "Sonnenlicht, so schön! Grüße",
    "O41b": "Schöne Brille, süß",
    "O41c": "Nur 99€",
    "O41d": "EIN TAG IN MÜNCHEN",
    "O41e": "Sonnenlicht, so schön",
    "O41f": "Sonnenlicht, so schön",
    "O41g": "Ich liebe dich|Mein Herz gehört dir|Heute Nacht",
}

# case id -> why the German phrasing does not decide like English today (strict xfail).
GAPS: dict[str, str] = {
    "D01": "_is_status_only_message: no German wording for status-only message during render",
    "D02": "is_status_question: no German wording for status question",
    "DC1-6": "is_status_question: no German wording for status question (contraction)",
    "D03": "is_help_question: no German wording for help question",
    "D04": "is_paraphrase_only: no German wording for reject paraphrase-only reply",
    "D10": "delegated_choice: no German wording for delegate choice (you choose)",
    "D11": "delegated_choice: no German wording for delegate choice (up to you)",
    "D12": "German 'all the videos' is not read as all media (falls back to selected)",
    "D13": "_requests_preserved_clip_order: no German wording for keep clip order",
    "D14": "_explicit_sfx_name: no German wording for named sound effect",
    "D15": "_apply_explicit_render_intent: no German wording for opening title extraction",
    "D16": "_apply_explicit_render_intent: no German wording for closing title extraction",
    "D17": "German 'fast pace' is not read (falls back to balanced pacing)",
    "D18": "_apply_explicit_render_intent: no German wording for photo sub-second hold (route)",
    "D19": "recognize_mixed_media_timing: no German wording for photo sub-second hold (schema)",
    "DC1-1": "_excerpt_states_seconds: no German wording for seconds as a number word",
    "DC1-2": "_GENERIC_SOUND_EFFECT_REQUEST_RE: no German wording for generic sound-effect ask",
    "DC1-3": "German 'reset the timing' is not read as a reset, so the photo timing is kept",
    "DC1-5": "German 'not too fast' is not read as taking back the fast photos",
    "DC1-9": "_explicit_sfx_name: no German wording for named sound effect (don't forget)",
    "DC1-12": "_apply_explicit_render_intent: no German wording for closing title from an end cue",
    "DC1-13": "German 'replace X with Y in the text' is read as a title, not a text edit",
    "DC1-15": "_is_status_only_message: no German wording for status question with a subject",
    "DC1-16": "German 'a fast pace' is not read (falls back to balanced pacing)",
    "DC1-17": "German 'every clip' is not read as all media (falls back to selected)",
    "D20": "recognize_mixed_media_timing: no German wording for quick photos, longer videos",
    "D21": "recognize_total_duration_s: no German wording for total duration",
    "D22": "recognize_round_robin_cadence: no German wording for round-robin cadence",
    "D23": "recognize_explicit_cadence_reuse_policy: no German wording for allow clip reuse",
    "D24": "recognize_image_layout: no German wording for photos uncropped",
    "D25": "recognize_image_layout: no German wording for photos fullscreen",
    "D26": (
        "_creator_quoted_captions_are_exhaustive: no German wording for exclusive quoted caption"
    ),
    "D27": "is_reported_speech_quote: no German wording for reported speech quote",
    "D29": "_wants_speech: no German wording for use my speech",
    "D30": "infer_typed_direction: no German wording for durable font rule",
    "D31": "classify_memory_candidate: no German wording for durable memory (always)",
    "D32": "classify_memory_candidate: no German wording for durable memory (all my videos)",
    "D34": "explicitly_temporary: no German wording for temporary style change",
    "D35": "_named_cues: no German wording for font change cue",
    "D36": "parse_caption_language_request: no German wording for English captions",
    "D37": "parse_caption_language_request: no German wording for Turkish captions",
    "D37b": "parse_caption_language_request: no German wording for Spanish captions",
    "D38": "is_filler_token: no German wording for filler vocalization cut",
    "D40": "_adjust_hook_score: no German wording for hook filler penalty",
    "C2-02": (
        "ai_draft_thought_has_unsupported_claim: no German wording for ungrounded caption claim"
    ),
    "C2-03": "_score_value: no German wording for spoken score words",
    "C2-05": "_is_refusal_text: no German wording for model refusal rejected",
    "C2-04": "_named_cues: no German wording for timing cue",
    "C2-07": (
        "ai_draft_thought_has_unsupported_claim: no German wording for "
        "first-person experience claim"
    ),
    "C2-11": "_narrated_storyboard_text_elements: no German wording for score treatment requested",
    "C2-15": (
        "_creator_quoted_captions_are_exhaustive: no German wording for "
        "exclusive quote (yalnız / bir tek / no other text)"
    ),
    "C2-16": "_score_value: no German wording for dative score words",
    "D51": "classify_memory_candidate: no German wording for durable memory (natural wordings)",
    "D52": "requires_stateful_extraction: no German wording for stateful memory change",
    "D53": "explicitly_temporary: no German wording for temporary style change (one-off wordings)",
    "D54": "_asks_remove: no German wording for remove one named clip",
    "D55": "title_hold_grounded: no German wording for title hold in words",
}

# Normalization folds Turkish spellings; German folding has its own test below.
NOT_APPLICABLE = frozenset(case.id for case in CASES if case.stage == "normalization")


@pytest.fixture
def german_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)


def evaluate_de(case: Case) -> dict[str, Any]:
    text = GERMAN[case.id]
    actual, error = _safe_call(case.fn, text)
    return {
        "id": case.id,
        "text": text,
        "expected": case.expect,
        "actual": actual,
        "error": error,
        "passed": error is None and actual == case.expect,
    }


def _params() -> list[Any]:
    params = []
    for case in CASES:
        if case.id in NOT_APPLICABLE:
            continue
        marks = []
        if case.id in GAPS:
            marks.append(pytest.mark.xfail(strict=True, reason=f"German gap: {GAPS[case.id]}"))
        params.append(pytest.param(case, id=f"{case.id}-de", marks=marks))
    return params


@pytest.mark.parametrize("case", _params())
def test_german_decides_like_english(case: Case, german_on: None) -> None:
    result = evaluate_de(case)
    assert result["error"] is None, result["error"]
    assert result["actual"] == case.expect, (
        f"{case.id} {case.capability} [de] {result['text']!r}: "
        f"expected {case.expect!r}, got {result['actual']!r}"
    )


def test_every_case_has_a_german_phrasing_or_a_reason() -> None:
    ids = {case.id for case in CASES}
    assert set(GERMAN) <= ids, sorted(set(GERMAN) - ids)
    assert set(GAPS) <= set(GERMAN), sorted(set(GAPS) - set(GERMAN))
    missing = ids - set(GERMAN) - NOT_APPLICABLE
    assert not missing, f"add a German phrasing (or a reason) for: {sorted(missing)}"


# ---------------------------------------------------------------- the German switch


_GERMAN_TEXTS = (
    "Mach den Titel größer und füge Untertitel hinzu",
    "Kannst du bitte die Musik leiser machen und den Schluss kürzen?",
    "Ich möchte, dass die Fotos schneller wechseln",
)


@pytest.mark.parametrize("text", _GERMAN_TEXTS)
def test_detection_reads_german_only_with_the_switch_on(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.kria.reply_language import detect_chat_language, resolve_reply_language

    monkeypatch.setattr(settings, "kria_german_replies_enabled", False, raising=False)
    off_detect = detect_chat_language(text)
    off_resolve = [
        resolve_reply_language(text),
        resolve_reply_language(text, previous="en"),
        resolve_reply_language(text, previous="tr"),
        resolve_reply_language(text, locale="de-DE,tr;q=0.8"),
        resolve_reply_language(text, previous="de"),
    ]
    # Off: German is never offered (the pre-switch result: no German, never a guess).
    assert off_detect != "de"
    assert "de" not in off_resolve

    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    assert detect_chat_language(text) == "de"
    assert resolve_reply_language(text) == "de"
    assert resolve_reply_language(text, locale="tr-TR") == "de"


# Emir's review of 1291454a9: English sentences with words German also uses ("will", "was",
# "video", "im") read as German by the agents' detector (`creator_text_language`).
_ENGLISH_WITH_SHARED_WORDS = (
    "I will make the video shorter",
    "This video was great",
    "Im making a video about my trip, keep it short",
    "Will you add the music to the video?",
)
_GERMAN_AGENT_TEXTS = (
    "Bitte mach das Video kürzer und ohne Musik",
    "Ich möchte die Fotos schneller wechseln",
)


@pytest.mark.parametrize("text", _ENGLISH_WITH_SHARED_WORDS)
def test_english_with_shared_words_stays_english_with_the_switch_on(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.kria.reply_language import creator_text_language, detect_chat_language

    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    assert creator_text_language(text) != "de"
    assert detect_chat_language(text) != "de"


@pytest.mark.parametrize("text", _GERMAN_AGENT_TEXTS)
def test_agents_read_german_creator_text_with_the_switch_on(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.kria.reply_language import creator_text_language

    monkeypatch.setattr(settings, "kria_german_replies_enabled", False, raising=False)
    assert creator_text_language(text) != "de"
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    assert creator_text_language(text) == "de"


def test_detection_off_is_the_pre_switch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch off, German text decides exactly as the EN/TR-only detector did (pinned values
    from before the switch existed)."""
    from app.config import settings
    from app.kria.reply_language import detect_chat_language, resolve_reply_language

    monkeypatch.setattr(settings, "kria_german_replies_enabled", False, raising=False)
    assert [detect_chat_language(t) for t in _GERMAN_TEXTS] == [None, None, None]
    assert [resolve_reply_language(t, previous="tr") for t in _GERMAN_TEXTS] == ["tr"] * 3
    assert [resolve_reply_language(t, locale="de-DE") for t in _GERMAN_TEXTS] == [None] * 3
    # English and Turkish keep deciding the same with the switch either way.
    for on in (False, True):
        monkeypatch.setattr(settings, "kria_german_replies_enabled", on, raising=False)
        assert detect_chat_language("make the title bigger and add captions") == "en"
        assert detect_chat_language("başlığı büyüt ve altyazı ekle lütfen") == "tr"


@pytest.mark.parametrize(
    ("header", "off", "on"),
    [
        ("de-DE", None, "de"),
        ("de", None, "de"),
        ("de-AT,en;q=0.8", None, "de"),
        ("de-DE,tr;q=0.8", None, "de"),
        ("tr-TR,de;q=0.8", "tr", "tr"),
        ("en-US", "en", "en"),
    ],
)
def test_locale_language_is_german_only_with_the_switch_on(
    header: str, off: str | None, on: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.kria.reply_language import locale_language

    monkeypatch.setattr(settings, "kria_german_replies_enabled", False, raising=False)
    assert locale_language(header) == off
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    assert locale_language(header) == on


def test_prompt_language_line_names_german_only_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.kria.reply_language import prompt_language_line

    monkeypatch.setattr(settings, "kria_german_replies_enabled", False, raising=False)
    assert prompt_language_line("de") == ""
    turkish = prompt_language_line("tr")
    assert "Turkish (tr)" in turkish
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    line = prompt_language_line("de")
    assert "German (de)" in line and "natural, everyday German" in line
    assert prompt_language_line("tr") == turkish
    assert prompt_language_line("en") == ""


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("größer", "grosser"),
        ("Nürnberg", "nurnberg"),
        ("ß", "ss"),
        ("ẞ", "ss"),
        ("GROẞ", "gross"),
        ("Straße", "strasse"),
        ("Ärger über Öl", "arger uber ol"),
    ],
)
def test_german_letters_fold(text: str, folded: str) -> None:
    from app.text_fold import loose_text

    assert loose_text(text) == folded
