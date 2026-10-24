"""KRI-843: a Talking take in three languages is transcribed per stretch, not in one language.

Don Julio T1 (2026-10-21, 25.7 s): Yasin asks "What's your name?" in English, Lucas the waiter
answers and explains the sauces in Spanish, a man at the table says "Cheers!", and the table
chats in Turkish. whisper detects ONE language for the whole take (Turkish) and TRANSLATES the
rest ("Evet, adın ne?"), so the creator's quoted start line is not found and the captions are
Turkish. The EN/TR cross-check only rescues it when Gemini's transcript reads clearly English,
and then the forced-English pass throws Lucas's Spanish away.

Replayed on the REAL audio's whisper.cpp passes (`tests/fixtures/kri843_don_julio_t1_passes.json`,
free, offline) through the real `analyze_speech_cleanup`, the snapshot hydration the render
worker does, `apply_take_window` for the creator's quoted start and end line (and the worker's own
`_phone_opening_cut` for the start line), `remap_words` and `build_plain_cues`. Only transcription
and silence detection are replayed.

How it can fail (written before the code):

F1  The reference is mixed but the text ID misses a language: the old EN/TR path runs.
F2  A single-language take is read as mixed (Spanish "de"/"mi"/"o" as Turkish, an English word in a
    Turkish reference): extra paid passes and a changed first pass.
F3  The Turkish translation's "evet"/"ne" at 2 s matches the chatter's "evet"/"ne" at 24 s
    when tokens are matched without order, so the translation beats the faithful English.
F4  A pass that dropped speech emits one long word over a whole stretch ("Gracias." 4-11.7 s) and
    wins on share, so Lucas is lost.
F5  A losing pass's zero-length or mistimed word slips between the chosen stretches.
F6  Merged words overlap, run backwards, pass the end of the take or have end < start: the cut plan,
    the captions or the snapshot validation break.
F7  An extra pass raises a transcription error or returns nothing: the analysis fails or loses the
    first pass. A programming error or a worker time limit must still escape.
F8  A reference with many languages makes unbounded paid passes.
F9  The single-language EN/TR cross-check changes (calls, receipt keys).
F12 The Gemini reference only covers the first 30 s: words after it are dropped.
F13 A 5-minute take (about 800 words, four passes) takes seconds to align.
F14 Gemini hallucinates (a made-up English paragraph, a "Video." loop): only confirmed stretches may
    be kept.
F15 Dropping unconfirmed words leaves sound without words: the cleanup cut must not remove speech.
F16 The kill switch off is not byte-identical to before.
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.captions import PHONE_CUE_BUDGET, build_plain_cues
from app.pipeline.phone_opening_cut import apply_take_window
from app.pipeline.silence_cut import is_filler_token, no_op_plan, remap_words
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_cleanup_apply import hydrate_speech_cleanup_snapshot
from app.pipeline.transcribe import TranscribeError, Transcript, Word
from app.services.clip_speech import SilenceDetectionResult
from app.tasks import generative_build as gb

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T1 = json.loads((FIXTURES / "kri843_don_julio_t1_passes.json").read_text())
M1 = json.loads((FIXTURES / "kri808_don_julio_clip_speech.json").read_text())["clips"]
DURATION_S = float(T1["duration_s"])
START_QUOTE = T1["quotes"]["start"]
END_QUOTE = T1["quotes"]["end"]
REFERENCES = {name: T1["references"][name]["text"] for name in ("R1", "R2")}
FINGERPRINT = "a" * 64


# -- replay ------------------------------------------------------------------------------------


def _transcript(language: str, rows: list[dict]) -> Transcript:
    """The shape `transcribe_whisper` returns; whisper.cpp's "-" dialogue dashes dropped."""
    words = []
    for row in rows:
        text = str(row["text"]).strip().lstrip("-").strip()
        if text and re.search(r"\w", text):
            words.append(
                Word(text=text, start_s=row["start_s"], end_s=row["end_s"], confidence=1.0)
            )
    return Transcript(words=words, full_text=" ".join(w.text for w in words), language=language)


def _t1_passes() -> dict[str, Transcript]:
    return {name: _transcript(p["language"], p["words"]) for name, p in T1["passes"].items()}


class _Replay:
    """`transcribe_fn`: ``language=None`` is the first pass, ``language="xx"`` the forced pass."""

    def __init__(self, passes: dict[str, Transcript], *, first: str, errors: dict | None = None):
        self.passes = passes
        self.first = first
        self.errors = errors or {}
        self.calls: list[str | None] = []
        self.prompts: list[str | None] = []

    def __call__(self, path: str, *, language: str | None, verbatim_prompt: str | None):
        self.calls.append(language)
        self.prompts.append(verbatim_prompt)
        if language in self.errors:
            raise self.errors[language]
        key = self.first if language is None else language
        return self.passes.get(key) or Transcript(words=[], full_text="", language=str(language))


def _silences(spans: list[list[float]]):
    def detect(_path: str, *, min_silence_s: float):
        return SilenceDetectionResult(spans=tuple((s, e) for s, e in spans), status="ok")

    return detect


def _analyze(
    passes: dict[str, Transcript],
    *,
    first: str,
    reference: str | None,
    reference_source: str | None = "clip_analysis",
    duration_s: float = DURATION_S,
    silences: list[list[float]] | None = None,
    errors: dict | None = None,
    retake_detect_fn=None,
    **extra,
) -> tuple[SpeechCleanupAnalysisResult, _Replay]:
    replay = _Replay(passes, first=first, errors=errors)
    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=FINGERPRINT,
            local_media_path="take.wav",
            duration_s=duration_s,
            reference_transcript=reference,
            reference_source=reference_source if reference else None,
            **extra,
        ),
        transcribe_fn=replay,
        silence_detect_fn=_silences(T1["silence_spans"] if silences is None else silences),
        retake_detect_fn=retake_detect_fn,
    )
    return result, replay


def _t1(first: str, reference: str | None, **kwargs):
    return _analyze(
        _t1_passes(), first=first, reference=REFERENCES[reference] if reference else None, **kwargs
    )


# -- the render boundaries ---------------------------------------------------------------------


def _snapshot(result: SpeechCleanupAnalysisResult, *, duration_s: float = DURATION_S):
    """What the render worker reads back: the persisted payload, hydrated and fenced."""
    payload = json.loads(json.dumps(result.to_payload()))
    return hydrate_speech_cleanup_snapshot(
        {
            "schema_version": 1,
            "analysis_id": str(uuid.uuid4()),
            "engine_version": "preflight-v1-2026-09-05",
            "detector_version": result.detector_version,
            "source": {
                "kind": "embedded_spine",
                "media_identity": "c0",
                "storage_path": "users/u/clip.mp4",
                "generation": "1",
                "window_start_s": 0.0,
                "window_end_s": duration_s,
                "source_policy_fingerprint": FINGERPRINT,
            },
            "analysis": payload,
        }
    ).require_source(
        kind="embedded_spine",
        storage_path="users/u/clip.mp4",
        window_start_s=0.0,
        window_duration_s=duration_s,
    )


def _brief_job() -> dict:
    """The approved brief of the Don Julio T1 chat, as far as the worker can read it.

    The brief reader only takes lines of two or more words (`MIN_QUOTE_TOKENS`), so the one-word
    end line "Cheers!" never reaches `_phone_opening_cut`; the end cut is applied below with
    `apply_take_window` directly, the call the worker makes for any line it did read.
    """
    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="start at the question",
                facts={"start_line": START_QUOTE},
            )
        ],
    )
    binding = BriefBinding.create("32a5b608-6397-4608-889f-dff5ad9bec40", brief)
    return {"creator_brief_binding": binding.model_dump(mode="json")}


@dataclass
class Rendered:
    start: dict
    end: dict
    kept: list[dict]  # cut-timeline words, plus ``source_start_s`` (where the take says them)
    cues: list[dict]
    language: str

    @property
    def kept_tokens(self) -> list[str]:
        return [_token(word["text"]) for word in self.kept]

    @property
    def kept_text(self) -> str:
        return " ".join(word["text"] for word in self.kept)

    @property
    def cue_text(self) -> str:
        return " ".join(cue["text"] for cue in self.cues)

    def said_between(self, low_s: float, high_s: float) -> list[str]:
        """Tokens of the kept words the take says from ``low_s`` to ``high_s`` (source seconds)."""
        return [
            _token(word["text"]) for word in self.kept if low_s <= word["source_start_s"] < high_s
        ]


def _token(text: str) -> str:
    return text.strip(" .,!?¡¿…\"'").lower()


def _through_the_worker(result: SpeechCleanupAnalysisResult, *, cleanup: bool) -> Rendered:
    """Snapshot -> start and end cut on the take's words -> remap -> the caption cues.

    ``cleanup=True``: the creator accepted speech cleanup, so its CutPlan is cut further by the
    quoted lines. ``cleanup=False``: only the quoted lines cut, from the checked preflight words.
    """
    snapshot = _snapshot(result)
    words = snapshot.source_words()
    plan, start, end = apply_take_window(
        snapshot.cut_plan if cleanup else no_op_plan(DURATION_S),
        words,
        start_quote=START_QUOTE,
        end_quote=END_QUOTE,
        duration_s=DURATION_S,
    )
    assert start is not None and end is not None
    # The worker's own wrapper (brief -> ask -> the same call) reads the start line alike.
    _plan, receipts = gb._phone_opening_cut(
        _brief_job(),
        snapshot.cut_plan if cleanup else None,
        words if cleanup else (),
        duration_s=DURATION_S,
        off_words=None if cleanup else words,
    )
    assert receipts is not None and receipts["opening_cut"] == {**start, "requirement_ids": ["r1"]}
    # `remap_words` drops exactly the words that lie wholly inside a removal.
    surviving = [
        word
        for word in words
        if not any(
            word.start_s >= r.start_s - 1e-6 and word.end_s <= r.end_s + 1e-6 for r in plan.removed
        )
    ]
    remapped = remap_words(words, plan)
    assert len(remapped) == len(surviving)
    kept = [
        {**item, "source_start_s": word.start_s}
        for item, word in zip(remapped, surviving, strict=True)
        if not cleanup or not is_filler_token(item["text"])
    ]
    cues = build_plain_cues(
        [
            Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
            for w in kept
        ],
        attach_words=True,
        budget=PHONE_CUE_BUDGET,
    )
    return Rendered(start=start, end=end, kept=kept, cues=cues, language=snapshot.analysis.language)


_TURKISH_LETTERS = re.compile("[ğşıİ]")
_TURKISH_WORDS = frozenset(
    "evet adın adin isminiz ne bak videoya videomu yandık yandik hiçbir hicbir şey sey "
    "bilmiyordum canım canim tepara ispanyol sağolun sagolun güzel guzel bilir".split()
)


def _turkish(tokens: list[str]) -> list[str]:
    return [t for t in tokens if _TURKISH_LETTERS.search(t) or t in _TURKISH_WORDS]


# -- 1. the Don Julio take, every first pass x both references ----------------------------------

ROWS = [
    pytest.param(first, reference, id=f"first-{first}-{reference}")
    for first in ("tr", "es", "en")
    for reference in ("R1", "R2")
]


# Hand ground truth (the fixture's `speech_truth`): Yasin's English, then Lucas's Spanish.
YASIN_S = (1.0, 3.9)
LUCAS_S = (3.9, 11.5)
# What whisper's own first pass says over the table murmur at 11.8-14.5 s. No pass confirms it,
# so it stays as it is: dropping it would leave sound without words, which the cleanup cut removes.
MURMUR = {"videoya", "bak"}
TRANSLATION = {"evet", "adın", "adin", "lukas"}  # the Turkish translation of "What's your name?"


@pytest.mark.parametrize(("first", "reference"), ROWS)
@pytest.mark.parametrize("cleanup", [True, False], ids=["cleanup-on", "cleanup-off"])
def test_t1_opens_on_the_question_and_ends_after_the_toast(first, reference, cleanup) -> None:
    """The ticket's Done-when, per row. ``first`` is what whisper-1's auto-detect pass would be
    (``tr`` is what whisper.cpp says; ``es``/``en`` are stand-ins); the reference is Gemini's."""
    result, _replay = _t1(first, reference)

    shown = _through_the_worker(result, cleanup=cleanup)

    start, end = shown.start, shown.end
    assert start["status"] == "applied", (start, shown.kept_text)
    assert 0.8 <= start["start_s"] <= 1.6  # the question is said at about 1.2-2.1 s
    assert end["status"] == "applied", (end, shown.kept_text)
    assert 14.5 <= end["end_s"] <= 16.5  # "Cheers!" is said at about 14.5-15.0 s
    yasin, lucas = shown.said_between(*YASIN_S), shown.said_between(*LUCAS_S)
    assert "what's your name" in " ".join(yasin)  # the question, in English...
    assert _turkish(yasin) == [], shown.kept_text
    # ...and Lucas, in Spanish.
    assert any(t.startswith("salsa") for t in lucas) and any(
        t.startswith("tradicional") for t in lucas
    )
    assert _turkish(lucas) == [], shown.kept_text
    # Nothing of the Turkish translation survives; the only Turkish left is the murmur.
    assert not TRANSLATION & set(shown.kept_tokens), shown.kept_text
    assert set(_turkish(shown.kept_tokens)) <= MURMUR, shown.kept_text
    assert "what's your name" in shown.cue_text.lower()
    assert set(_turkish([_token(t) for t in shown.cue_text.split()])) <= MURMUR


# The two stretches of sound the merge once left wordless: the table chatter at 13.72-14.33 s
# ("Biliyor mu?") and "Cheers!" itself at 14.52-15.0 s.
SOUND_NOT_TO_CUT = [(13.72, 14.33), (14.52, 15.0)]


@pytest.mark.parametrize(("first", "reference"), ROWS)
def test_t1_merged_words_are_valid_and_cleanup_never_cuts_speech(first, reference) -> None:
    """F5/F6/F15: the words the cut plan and captions are built from, and what cleanup removed.

    With cleanup on, the cut plan removes nothing inside what the ground truth says is speech nor
    inside the sound islands above. Dropping the unconfirmed first-pass "Videoya bak." (11.8-14.5 s)
    once left 12-14.4 s wordless, so the cut removed 11.96-14.38 s through both."""
    result, _replay = _t1(first, reference)

    words = result.timed_words
    assert [(w.start_s, w.end_s) for w in words] == sorted((w.start_s, w.end_s) for w in words)
    assert all(0.0 <= w.start_s <= w.end_s <= DURATION_S + 1e-6 for w in words)
    _snapshot(result)  # the render fences words past the window (analysis_word_window)
    certain = [(s["start_s"] + 0.15, s["end_s"] - 0.15) for s in T1["speech_truth"] if s["certain"]]
    for removal in result.cut_plan.removed:
        for lo, hi in [*certain, *SOUND_NOT_TO_CUT]:
            assert removal.end_s <= lo or removal.start_s >= hi, (removal, (lo, hi))


@pytest.mark.parametrize("first", ["tr", "es", "en"])
def test_t1_gemini_audio_sample_gives_the_same_words(first) -> None:
    """The Kria v2 reference is a Gemini listen to the first 30 s: this take fits in it."""
    reference = REFERENCES["R1"]
    plain, _ = _t1(first, "R1")
    sampled, replay = _analyze(
        _t1_passes(),
        first=first,
        reference=reference,
        reference_source="gemini_audio",
        reference_covers_s=30.0,
    )

    assert [(w.text, w.start_s, w.end_s) for w in sampled.timed_words] == [
        (w.text, w.start_s, w.end_s) for w in plain.timed_words
    ]
    assert len(replay.calls) <= 4


def test_t1_first_pass_tr_reads_en_and_es_in_the_receipt() -> None:
    result, replay = _t1("tr", "R1")

    crosscheck = result.diagnostics["language_crosscheck"]
    assert replay.calls[0] is None and set(replay.calls[1:]) == {"en", "es"}
    assert len(set(replay.prompts)) == 1 and replay.prompts[0]  # same verbatim prompt each time
    assert crosscheck["mixed"] is True and crosscheck["applied"] is True
    assert crosscheck["whisper_language"] == "tr"
    assert set(crosscheck["reference_languages"]) == {"en", "es", "tr"}
    assert (set(crosscheck["tried"]), set(crosscheck["heard"])) == ({"en", "es"}, {"en", "es"})
    assert crosscheck["failed"] == []
    segments = crosscheck["segments"]
    # The question, Lucas, then the first pass: "Gracias." and "Cheers." confirmed, the murmur
    # between them kept but not confirmed. `language` is the pass a stretch was taken from.
    assert [(s["language"], s["confirmed"]) for s in segments[:3]] == [
        ("en", True),
        ("es", True),
        ("tr", True),
    ]
    assert {"start_s": 11.84, "end_s": 14.5, "language": "tr", "confirmed": False} in segments
    assert (
        segments[0]["start_s"] < 0.1 and 3.5 < segments[0]["end_s"] <= segments[1]["start_s"] + 0.2
    )
    assert [s["start_s"] for s in segments] == sorted(s["start_s"] for s in segments)
    assert result.language == "en"  # the language with the most words the reference confirmed


# -- 2. controls ----------------------------------------------------------------------------------


def test_a_single_language_take_keeps_todays_path() -> None:
    """M1_05 (Yasin's question, 4 s): prod whisper-1 said Turkish, Gemini heard English. Today's
    cross-check transcribes again in English; nothing about it may change."""
    clip = M1["M1_05"]
    first = _transcript(
        clip["prod_whisper_first_pass"]["language"], clip["prod_whisper_first_pass"]["words"]
    )
    again = _transcript("en", clip["local_whisper_medium"]["en"]["words"])
    reference = clip["prod_record"]["transcript"]

    result, replay = _analyze(
        {"first": first, "en": again},
        first="first",
        reference=reference,
        duration_s=4.0,
        silences=[],
    )

    assert replay.calls == [None, "en"]
    assert result.language == "en"
    assert " ".join(w.text for w in result.timed_words) == "What's your name? Lucas."
    assert result.diagnostics["language_crosscheck"] == {
        "whisper_language": "tr",
        "reference_language": "en",
        "applied": True,
        "reference_source": "clip_analysis",
    }


def test_a_turkish_take_against_a_turkish_reference_needs_no_extra_pass() -> None:
    clip = M1["M1_04"]
    first = _transcript("tr", clip["prod_whisper_first_pass"]["words"])

    result, replay = _analyze(
        {"first": first},
        first="first",
        reference=clip["prod_record"]["transcript"],
        duration_s=2.6,
        silences=[],
    )

    assert replay.calls == [None]
    assert result.language == "tr"
    assert result.diagnostics["language_crosscheck"] == {
        "whisper_language": "tr",
        "reference_language": "tr",
        "applied": False,
        "reference_source": "clip_analysis",
    }


@pytest.mark.parametrize(
    "reference",
    [
        # Turkish with English words: still one language.
        "Bugün çok güzel bir video çektim, okay, hadi başlayalım, super bir gün olacak yani.",
        # Spanish: "de", "mi" and "o" must not read as Turkish, and one language is not mixed.
        "Hola, hoy vamos a probar las salsas de la casa, mi favorita es la criolla o la de ajo.",
    ],
    ids=["turkish-with-english-words", "spanish-with-de-mi-o"],
)
def test_one_language_references_are_not_read_as_mixed(reference) -> None:
    from app.services.mixed_language_transcript import reference_languages

    assert len(reference_languages(reference)) <= 1


@pytest.mark.parametrize(
    ("text", "languages"),
    [
        pytest.param(REFERENCES["R1"], {"en", "es", "tr"}, id="don-julio-whole-take"),
        pytest.param(REFERENCES["R2"], {"en", "es"}, id="don-julio-up-to-cheers"),
        pytest.param(M1["M1_05"]["prod_record"]["transcript"], {"en"}, id="english-with-espanol"),
        pytest.param(M1["M1_04"]["prod_record"]["transcript"], {"tr"}, id="turkish-chatter"),
        pytest.param(
            "Hey guys, this is Carlos, and today we are gonna try the best tacos in town.",
            {"en"},
            id="english-with-a-spanish-name",
        ),
        pytest.param(
            "Guten Tag, ich bin heute hier und das ist wirklich sehr gut. Hello everyone, "
            "thank you for watching and please subscribe to the channel.",
            {"de", "en"},
            id="german-and-english",
        ),
        pytest.param(
            "Bonjour tout le monde, nous sommes ici et c'est très bien pour vous. Merci "
            "beaucoup, et bonne journée à tous, je vous embrasse.",
            {"fr"},
            id="french",
        ),
    ],
)
def test_the_reference_text_id_finds_the_languages_spoken(text, languages) -> None:
    from app.services.mixed_language_transcript import reference_languages

    assert set(reference_languages(text)) == languages


# Gemini wrote the waiter's line several ways for the same audio; each must name Spanish beside
# the English around it (a take whose reference only reads English falls back to the old path).
SPANISH_FRAME = (
    "Español, yeah. What's your name? Hey Lucas. Lucas. Yeah. {} "
    "Salsa criolla, chimichurri con sal marina. Gracias. Cheers. Cheers."
)
SPANISH_LINES = [
    "Acá tenemos salsas tradicionales argentinas.",
    "Aca tenemos salsas tradicionales argentinas.",
    "Tenemos salsas tradicionales argentinas.",
    "Estas son las salsas tradicionales de Argentina.",
]


@pytest.mark.parametrize("line", SPANISH_LINES)
def test_spanish_variants_name_spanish_beside_english(line) -> None:
    from app.services.mixed_language_transcript import reference_languages

    assert set(reference_languages(SPANISH_FRAME.format(line))) == {"en", "es"}


@pytest.mark.parametrize("line", [SPANISH_LINES[1], SPANISH_LINES[3]])
def test_a_spanish_line_names_spanish_on_its_own(line) -> None:
    from app.services.mixed_language_transcript import reference_languages

    assert reference_languages(line) == ["es"]


ENGLISH_TAKES = [
    "Hey guys, welcome back to the channel. Today we're going to try the best tacos in Mexico "
    "City, and I'm super excited. Let's go!",
    "So this is my favorite spot in Barcelona. As soon as you walk in, you can smell the garlic. "
    "Do as I say and order the paella.",
    "Okay so I'm gonna show you how I make my morning routine. First, I wake up at six. Then I "
    "drink a glass of water. And then I go for a run.",
    "Uh, so, um, yeah, I wanna talk about, you know, how to edit a video fast. It's not that hard "
    "if you have a plan, right?",
    "Ben and Kim went to the market. Do you want some bread? As for me, I prefer the cheese. "
    "Tag your friends in the comments and let me know what you think.",
    "Pour the sauce over the pasta and add some more cheese. Bon appetit! Then let it cook for "
    "ten minutes, per the recipe.",
    "Hi, I'm Carolina from Ecuador. And this is the obleas de la casa. It's a traditional "
    "Colombian dessert.",
    M1["M1_05"]["prod_record"]["transcript"],
    "So today we built the thing. Let's talk about the best football players in the Turkish "
    "Super League this season.",
]
TURKISH_TAKES = [
    "Merhaba arkadaşlar, bugün sizinle çok güzel bir yere gidiyoruz. Burası İstanbul'un en güzel "
    "kahvecisi, hadi başlayalım.",
    "Bu video için çok teşekkürler. Bugün bir şey denedim ama pek beğenmedim. Yani, bence daha "
    "iyi olabilirdi.",
    "Bugün size çok kolay bir tarif göstereceğim. Önce soğanı doğruyoruz, sonra biraz yağ "
    "ekliyoruz. Çok lezzetli oluyor, deneyin.",
    "Abi, nerede bu adam? Biz burada bekliyoruz ama gelmedi. Ben aradım, açmıyor. Ne yapacağız "
    "şimdi?",
    "Bu videoyu çok güzel çektim bugün, hadi bakalım neler var",
    M1["M1_04"]["prod_record"]["transcript"],
    " ".join(
        w["text"] for w in json.loads((FIXTURES / "kri632_t1_talking_words.json").read_text())
    ),
]


@pytest.mark.parametrize(
    ("text", "language"),
    [pytest.param(t, "en", id=f"en-{i}") for i, t in enumerate(ENGLISH_TAKES)]
    + [pytest.param(t, "tr", id=f"tr-{i}") for i, t in enumerate(TURKISH_TAKES)],
)
def test_one_language_creator_transcripts_name_exactly_one_language(text, language) -> None:
    from app.services.mixed_language_transcript import reference_languages

    assert reference_languages(text) == [language]


def test_language_word_lists_do_not_overlap() -> None:
    """A word in two lists would vote for two languages: the lists are disjoint on purpose."""
    from app.services import mixed_language_transcript as module

    seen: dict[str, str] = {}
    for language, words in module.LEXICONS.items():
        for word in words:
            assert word not in seen, f"{word!r} is in both {seen[word]} and {language}"
            seen[word] = language


def test_an_unrelated_reference_confirms_nothing() -> None:
    """F14: the reference is mixed but about something else: the first pass stands, unchanged."""
    unrelated = (
        "Hello everyone, welcome back to the channel and thank you for watching today. "
        "Hoy vamos a visitar el mercado de la ciudad, tiene muchas frutas y verduras muy frescas."
    )
    passes = _t1_passes()

    result, replay = _analyze(passes, first="tr", reference=unrelated, silences=[])

    assert replay.calls[0] is None and len(replay.calls) <= 4
    assert [(w.text, w.start_s, w.end_s) for w in result.timed_words] == [
        (w.text, w.start_s, w.end_s) for w in passes["tr"].words
    ]
    assert result.language == "tr"
    crosscheck = result.diagnostics["language_crosscheck"]
    assert crosscheck["applied"] is False
    assert crosscheck.get("segments", []) == []


@pytest.mark.parametrize("reference", ["R1", "R2"])
def test_kill_switch_off_is_todays_path_exactly(monkeypatch, reference) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_mixed_language_enabled", False)
    passes = _t1_passes()

    result, replay = _analyze(
        passes, first="tr", reference=REFERENCES[reference], silences=T1["silence_spans"]
    )

    if reference == "R1":  # Gemini's transcript reads neither English nor Turkish: one pass
        assert replay.calls == [None]
        assert result.language == "tr"
        expected = passes["tr"]
        applied, reference_language = False, ""
    else:  # reads English: transcribed again in English, which drops Lucas
        assert replay.calls == [None, "en"]
        assert result.language == "en"
        expected = passes["en"]
        applied, reference_language = True, "en"
    assert [(w.text, w.start_s, w.end_s) for w in result.timed_words] == [
        (w.text, w.start_s, w.end_s) for w in expected.words
    ]
    assert result.diagnostics["language_crosscheck"] == {
        "whisper_language": "tr",
        "reference_language": reference_language,
        "applied": applied,
        "reference_source": "clip_analysis",
    }


# -- 3. the failure modes, on small made-up takes --------------------------------------------------
#
# A 25 s take: Yasin asks in English, Lucas answers in Spanish, then Turkish chatter. The
# first pass is Turkish and TRANSLATES the question into "Evet, ne?" twice (words that the
# chatter at 20 s really says); the English pass is faithful but drops the Spanish into one
# long "Gracias." (F4); the Spanish pass is faithful for Lucas and translates the question.

TAKE_S = 25.0
SYN_REFERENCE = (
    "What's your name? Lucas. Acá tenemos salsas tradicionales argentinas. Gracias. "
    "Evet, ne ne evet. Yeah. Right."
)


def _spec(language: str, spec: str) -> Transcript:
    """``"text start end | text start end"``, text without spaces."""
    rows = []
    for item in spec.split("|"):
        text, start, end = item.split()
        rows.append({"text": text.replace("_", " "), "start_s": float(start), "end_s": float(end)})
    return _transcript(language, rows)


def _syn_passes() -> dict[str, Transcript]:
    return {
        "tr": _spec(
            "tr",
            "Evet, 1.0 1.4 | ne? 1.4 2.0 | Evet, 2.0 2.4 | ne? 2.4 3.0 "
            "| Katenimo 4.0 4.5 | salsas 4.5 5.0 | tradicionales 5.0 6.0 | afgentinas. 6.0 7.0 "
            "| Gracias. 8.5 9.0 "
            "| Evet, 20.0 20.4 | ne 20.4 20.8 | ne 20.8 21.2 | evet. 21.2 22.0 "
            "| Hiçbir 23.0 23.4 | şey 23.4 23.7 | bilmiyorum. 23.7 24.0",
        ),
        "en": _spec(
            "en",
            "What's 1.0 1.3 | your 1.3 1.6 | name? 1.6 2.0 | Lucas. 2.0 3.0 "
            "| Gracias. 4.0 19.0 | Yeah. 24.6 25.1 | Right. 25.2 25.8",
        ),
        "es": _spec(
            "es",
            "¿Qué 1.0 1.3 | es 1.3 1.5 | tu 1.5 1.7 | nombre? 1.7 2.0 | Lucas. 2.0 3.0 "
            "| Acá 4.0 4.4 | tenemos 4.4 5.0 | salsas 5.0 5.6 | tradicionales 5.6 6.8 "
            "| argentinas. 6.8 8.0 | Gracias. 8.3 8.9",
        ),
    }


def _syn(first: str = "tr", *, reference: str = SYN_REFERENCE, **kwargs):
    return _analyze(
        _syn_passes(), first=first, reference=reference, duration_s=TAKE_S, silences=[], **kwargs
    )


def _texts(result: SpeechCleanupAnalysisResult) -> list[str]:
    return [w.text for w in result.timed_words]


def test_f3_f4_translation_tokens_from_another_time_and_a_long_word_lose() -> None:
    result, _replay = _syn()

    texts = _texts(result)
    assert texts[:4] == ["What's", "your", "name?", "Lucas."]  # not "Evet, ne? Evet, ne?" (F3)
    assert texts[4:9] == ["Acá", "tenemos", "salsas", "tradicionales", "argentinas."]
    assert texts.count("Gracias.") == 1  # the 15 s one-word pass lost (F4)...
    gracias = next(w for w in result.timed_words if w.text == "Gracias.")
    # ...and of two equal readings, the first pass's wins
    assert (gracias.start_s, gracias.end_s) == (8.5, 9.0)
    assert texts[10:14] == ["Evet,", "ne", "ne", "evet."]  # the chatter that IS in the reference
    assert not [t for t in texts[:10] if t.lower().startswith("evet")]
    # Said, but Gemini's transcript does not have it: nothing replaces it, so the first pass
    # stands (cut as speech, not as a pause); "Yeah." is the English pass's, confirmed.
    assert texts[14:] == ["Hiçbir", "şey", "bilmiyorum.", "Yeah."]


def test_f5_f6_merged_words_stay_inside_the_take_and_in_order() -> None:
    result, _replay = _syn()

    words = result.timed_words
    assert words[-1].text == "Yeah." and words[-1].end_s == TAKE_S  # 25.1 s clamped to 25.0 s
    assert "Right." not in _texts(result)  # said (by one pass) only after the take ended
    assert all(w.start_s <= w.end_s <= TAKE_S for w in words)
    assert [w.start_s for w in words] == sorted(w.start_s for w in words)
    assert all(a.end_s <= b.start_s + 1e-6 for a, b in zip(words, words[1:], strict=False))
    _snapshot(result, duration_s=TAKE_S)


WEAK_REFERENCE = "What's your name? Lucas. Acá tenemos salsas. Gracias. No. Yes."


@pytest.mark.parametrize(
    ("heard", "extra_pass", "applied"),
    [
        pytest.param(
            "a faithful stretch",
            "What's 1.0 1.3 | your 1.3 1.6 | name? 1.6 2.0",
            True,
            id="control-faithful",
        ),
        pytest.param("a lone short word", "No. 9.0 9.5", False, id="one-short-word"),
        pytest.param(
            "two short words one letter off",
            "Non 9.0 9.3 | yet. 9.3 9.6",  # "no" and "yes", spelled a letter apart
            False,
            id="short-words-are-never-fuzzy",
        ),
        pytest.param(
            "two of five words",
            "Lucas 6.0 6.4 | and 6.4 6.6 | Gracias 6.6 7.2 | were 7.2 7.5 | here. 7.5 8.0",
            False,
            id="two-of-five-words",
        ),
    ],
)
def test_weak_agreement_with_the_reference_confirms_nothing(heard, extra_pass, applied) -> None:
    """F2/F14: a reference mixed in two languages, and an extra pass that shares a word or two
    with it by chance: the first pass is returned as it is."""
    passes = {"tr": _spec("tr", "Hola 1.0 1.5 | amigo. 1.5 2.0"), "en": _spec("en", extra_pass)}

    result, _replay = _analyze(
        passes, first="tr", reference=WEAK_REFERENCE, duration_s=TAKE_S, silences=[]
    )

    assert result.diagnostics["language_crosscheck"]["applied"] is applied, heard
    assert _texts(result) == (["What's", "your", "name?"] if applied else ["Hola", "amigo."])


def test_f7_a_failed_or_empty_extra_pass_is_skipped() -> None:
    result, replay = _syn(errors={"es": TranscribeError("provider down")})

    texts = _texts(result)
    assert replay.calls[0] is None and set(replay.calls[1:]) == {"en", "es"}
    assert texts[:4] == ["What's", "your", "name?", "Lucas."]  # the English pass still lands
    assert "Katenimo" in texts and "tenemos" not in " ".join(texts)  # no Spanish pass: first pass
    crosscheck = result.diagnostics["language_crosscheck"]
    assert crosscheck["applied"] is True
    assert (crosscheck["tried"], crosscheck["heard"], crosscheck["failed"]) == (
        ["en", "es"],
        ["en"],
        ["es"],
    )

    passes = _syn_passes()
    passes["en"] = Transcript(words=[], full_text="", language="en")
    passes["es"] = Transcript(words=[], full_text="", language="es")
    nothing, _ = _analyze(
        passes, first="tr", reference=SYN_REFERENCE, duration_s=TAKE_S, silences=[]
    )
    assert _texts(nothing) == [w.text for w in passes["tr"].words]  # first pass, untouched
    assert nothing.language == "tr"
    assert nothing.diagnostics["language_crosscheck"]["applied"] is False
    # Asked for, heard nothing: tried but neither heard nor failed.
    crosscheck = nothing.diagnostics["language_crosscheck"]
    assert (crosscheck["tried"], crosscheck["heard"], crosscheck["failed"]) == (
        ["en", "es"],
        [],
        [],
    )


@pytest.mark.parametrize(
    "error",
    [TypeError("a bug"), KeyError("a bug")],
    ids=["type-error", "key-error"],
)
def test_f7_a_programming_error_in_an_extra_pass_escapes(error) -> None:
    with pytest.raises(type(error)):
        _syn(errors={"en": error})


def test_f7_a_worker_time_limit_in_an_extra_pass_escapes() -> None:
    from billiard.exceptions import SoftTimeLimitExceeded

    with pytest.raises(SoftTimeLimitExceeded):
        _syn(errors={"en": SoftTimeLimitExceeded()})


def test_f8_extra_passes_are_capped() -> None:
    five_languages = (
        "What's your name? Where are you from? Hello everyone, thank you for watching. "
        "Tenemos salsas y también hay gracias aquí. Acá tenemos mucho más para ustedes. "
        "Evet, ne var ne yok? Bu çok güzel bir gün. "
        "Guten Tag, ich bin hier und das ist sehr gut. "
        "Bonjour, nous sommes ici et c'est très bien pour vous."
    )
    passes = {"first": _spec("xx", "Hello 1.0 1.5 | there. 1.5 2.0")}
    for code in ("en", "es", "tr", "de", "fr"):
        passes[code] = _spec(code, "Hello 1.0 1.5 | there. 1.5 2.0")

    result, replay = _analyze(
        passes, first="first", reference=five_languages, duration_s=TAKE_S, silences=[]
    )

    assert len(replay.calls) <= 4  # the first pass + at most three extra
    crosscheck = result.diagnostics["language_crosscheck"]
    assert len(crosscheck["tried"]) <= 3 and len(crosscheck["reference_languages"]) >= 4


@pytest.mark.parametrize("covers_s", [None, 10.0], ids=["whole-take", "first-10s"])
def test_f12_words_after_the_references_coverage_keep_the_first_pass(covers_s) -> None:
    extra = {} if covers_s is None else {"reference_covers_s": covers_s}
    result, _replay = _syn(reference_source="gemini_audio", **extra)

    texts = _texts(result)
    assert texts[:4] == ["What's", "your", "name?", "Lucas."]
    assert ["Evet,", "ne", "ne", "evet.", "Hiçbir", "şey", "bilmiyorum."] == texts[10:17]
    if covers_s is None:
        assert texts[17:] == ["Yeah."]  # the English pass's, confirmed by the reference
    else:  # Gemini never heard past 10 s: no extra pass's word there is ever taken
        assert texts[17:] == []


def test_f6_merged_words_are_sorted_inside_the_take_and_never_overlap() -> None:
    """Seeded random passes (zero-length words, words nested in the next word, words past the
    take, a reference that covers only part of it): whatever the merge returns is valid."""
    from app.services.mixed_language_transcript import MixedPass, merge_passes

    rng = random.Random(843)
    vocabulary = "salsa lucas hello evet name video gracias cheers tenemos what's your good".split()

    def words(duration: float, count: int) -> list[dict]:
        out, now = [], 0.0
        for _ in range(count):
            start = now + rng.random() * 0.6
            end = start + rng.choice([0.0, 0.0, rng.random() * 1.5, rng.random() * 6.0])
            text = rng.choice(vocabulary) + rng.choice(["", ".", "?", ","])
            out.append({"text": text, "start_s": round(start, 2), "end_s": round(end, 2)})
            now = start if rng.random() < 0.3 else end  # a word that starts before the last ended
            if start > duration + 2:
                break
        return out

    applied = 0
    for _ in range(400):
        duration = rng.choice([8.0, 20.0, 40.0])
        first = MixedPass("tr", words(duration, rng.randint(2, 25)))
        extras = [MixedPass(code, words(duration, rng.randint(0, 25))) for code in ("en", "es")]
        reference = " ".join(rng.choice(vocabulary) for _ in range(rng.randint(3, 30)))
        merged = merge_passes(
            first,
            extras[: rng.randint(0, 2)],
            reference,
            duration_s=duration,
            covers_s=rng.choice([None, None, 5.0, 12.0]),
        )
        if merged is None:
            continue
        applied += 1
        out = list(merged.words)
        assert [(w["start_s"], w["end_s"]) for w in out] == sorted(
            (w["start_s"], w["end_s"]) for w in out
        )
        assert all(0.0 <= w["start_s"] <= w["end_s"] <= duration for w in out), out
        assert all(a["end_s"] <= b["start_s"] for a, b in zip(out, out[1:], strict=False)), out
    assert applied >= 30  # the property was exercised, not skipped


# One Turkish tour of a cafe with a short English greeting. Gemini's transcript of the take stops
# after the second sentence, so it does not have the prices (10-13 s) or "Hadi gidelim." (14-15 s).
SHORT_REFERENCE = (
    "Merhaba arkadaşlar. Hello guys. Bugün size çok güzel bir yer göstereceğim. "
    "Burası İstanbul'un en eski kahvecisi."
)
SHORT_REFERENCE_SILENCES = [
    [1.5, 1.6],
    [2.4, 3.0],
    [6.0, 6.5],
    [9.2, 10.0],
    [13.0, 14.0],
    [15.2, 20.0],
]


def test_a_short_reference_never_erases_the_speech_it_does_not_have() -> None:
    """Dropping the stretches no pass confirms left them wordless, and the cleanup cut then removed
    13-20 s of real Turkish speech ("Hadi gidelim"). The first pass stands wherever nothing
    replaces it."""
    passes = {
        "tr": _spec(
            "tr",
            "Merhaba 0.0 0.6 | arkadaşlar. 0.6 1.5 | Halo 1.6 2.0 | gays. 2.0 2.4 "
            "| Bugün 3.0 3.4 | size 3.4 3.6 | çok 3.6 3.9 | güzel 3.9 4.3 | bir 4.3 4.5 "
            "| yer 4.5 4.9 | göstereceğim. 4.9 6.0 "
            "| Burası 6.5 7.0 | İstanbul'un 7.0 7.8 | en 7.8 8.0 | eski 8.0 8.5 "
            "| kahvecisi. 8.5 9.2 "
            "| Fiyatlar 10.0 10.6 | biraz 10.6 11.0 | pahalı 11.0 11.6 | ama 11.6 11.9 "
            "| değer. 11.9 13.0 | Hadi 14.0 14.4 | gidelim. 14.4 15.2",
        ),
        "en": _spec("en", "Hello 1.6 2.0 | guys. 2.0 2.4 | Prices 10.0 10.6 | are 10.6 11.0"),
    }

    result, replay = _analyze(
        passes,
        first="tr",
        reference=SHORT_REFERENCE,
        duration_s=20.0,
        silences=SHORT_REFERENCE_SILENCES,
    )

    assert replay.calls == [None, "en"]
    assert result.diagnostics["language_crosscheck"]["applied"] is True
    texts = _texts(result)
    assert texts[2:4] == [
        "Hello",
        "guys.",
    ]  # the confirmed English replaced the garbled "Halo gays."
    assert "Halo" not in texts
    assert texts[-7:] == ["Fiyatlar", "biraz", "pahalı", "ama", "değer.", "Hadi", "gidelim."]
    for removal in result.cut_plan.removed:  # the speech at 10.0-13.0 s and 14.0-15.2 s stays in
        assert removal.end_s <= 10.0 or removal.start_s >= 13.0, removal
        assert removal.end_s <= 14.0 or removal.start_s >= 15.2, removal


@pytest.mark.parametrize("gap_s", [0.05, 0.15])
def test_a_sentence_that_follows_a_replaced_one_is_kept_and_not_cut(gap_s) -> None:
    """The next sentence starts a hair after the replaced "Halo gays." and Gemini's transcript
    stops before it. It is not replaced by anything, so the words stay and the cleanup cut finds
    speech there (wordless sound would be removed as a pause)."""
    start = 2.4 + gap_s
    passes = {
        "tr": _spec(
            "tr",
            "Merhaba 0.0 0.6 | arkadaşlar. 0.6 1.5 | Halo 1.6 2.0 | gays. 2.0 2.4 "
            f"| Bugün {start} {start + 0.4} | size {start + 0.4} {start + 0.8} "
            f"| çok {start + 0.8} {start + 1.2} | güzel {start + 1.2} {start + 1.6} "
            f"| yer. {start + 1.6} {start + 2.0}",
        ),
        "en": _spec("en", "Hello 1.6 2.0 | guys. 2.0 2.4"),
    }

    result, _replay = _analyze(
        passes,
        first="tr",
        reference="Merhaba arkadaşlar. Hello guys.",
        duration_s=20.0,
        silences=[[1.5, 1.6], [start + 2.0, 20.0]],
    )

    assert result.diagnostics["language_crosscheck"]["applied"] is True
    assert _texts(result) == [
        "Merhaba",
        "arkadaşlar.",
        "Hello",
        "guys.",
        "Bugün",
        "size",
        "çok",
        "güzel",
        "yer.",
    ]
    for removal in result.cut_plan.removed:
        assert removal.end_s <= start or removal.start_s >= start + 2.0, removal


def test_a_retake_span_indexes_the_merged_words_not_the_first_pass() -> None:
    """#1753 composition: the retake detector reads the stored words, so the span it returns
    ("Tenemos ... Argentina,") is an index into the MERGED transcript, and the cut removes
    exactly that stretch (about 4.0-6.3 s) as a retake."""
    seen: list[list[str]] = []

    def retakes(words, _language):
        texts = [w.text for w in words]
        seen.append(texts)
        return ((texts.index("Tenemos"), texts.index("Argentina,")),)

    result, _replay = _t1("tr", "R1", retake_detect_fn=retakes)

    assert result.diagnostics["language_crosscheck"]["applied"] is True
    assert seen and "Katenimo" not in seen[0] and "Tenemos" in seen[0]  # the merged words
    removed = [r for r in result.cut_plan.removed if r.reason == "retake"]
    assert len(removed) == 1
    assert removed[0].start_s == pytest.approx(4.0, abs=0.15)
    assert removed[0].end_s == pytest.approx(6.3, abs=0.15)


def _long_take(rng: random.Random, tokens: int) -> list[str]:
    """Speech-like: a few hundred distinct words (4-12 letters), the common ones said often."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    vocabulary = ["".join(rng.choices(letters, k=rng.randint(4, 12))) for _ in range(300)]
    vocabulary += "we are going to see the best place aquí tenemos salsas para ustedes".split()
    weights = [1 / (rank + 1) for rank in range(len(vocabulary))]
    return rng.choices(vocabulary, weights=weights, k=tokens)


def test_f13_a_five_minute_take_aligns_quickly(monkeypatch) -> None:
    rng = random.Random(843)
    tokens = _long_take(rng, 800)
    reference = (
        ". ".join(" ".join(tokens[i : i + 10]).capitalize() for i in range(0, len(tokens), 10))
        + ". What's your name? Acá tenemos salsas. Evet, ne ne evet."
    )

    def pass_of(language: str, noise: float) -> Transcript:
        words = []
        for index, token in enumerate(tokens):
            text = token if rng.random() > noise else token[::-1]
            start = index * 0.37
            words.append(
                {
                    "text": text + ("." if index % 10 == 9 else ""),
                    "start_s": start,
                    "end_s": start + 0.3,
                }
            )
        return _transcript(language, words)

    passes = {"tr": pass_of("tr", 0.3), "en": pass_of("en", 0.1), "es": pass_of("es", 0.2)}

    def timed() -> float:
        started = time.perf_counter()
        _analyze(passes, first="tr", reference=reference, duration_s=300.0, silences=[])
        return time.perf_counter() - started

    mixed = timed()
    result, replay = _analyze(
        passes, first="tr", reference=reference, duration_s=300.0, silences=[]
    )
    assert len(replay.calls) <= 4 and result.timed_words
    with monkeypatch.context() as patch:
        patch.setattr(settings, "speech_cleanup_mixed_language_enabled", False)
        cleanup_only = timed()
    # The cut plan of 800 words costs what it costs; the alignment of four passes must add little.
    assert mixed - cleanup_only < 2.0, (mixed, cleanup_only)
