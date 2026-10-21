"""KRI-808: the Don Julio M1 clip records, replayed through the real speech step.

Prod (2026-10-21, plan item 7a4ce1d5) handed Main Creator "Ne isminiz?" for a Turkish
speaker's English "What's your name?", "Спасибо." for "Gracias", a 24-word sentence for a
1.1 s clip and a "Video. Video. Video..." loop, so the quoted line "What's your name?" had
no timed words to land on. The fixture holds the records prod stored, whisper-1's first
passes rebuilt from them, and free whisper.cpp medium passes (auto, forced en/tr/es) on the
kit clips as the stand-in for whisper-1's forced re-runs. Only the transcription I/O is
replaced; ``_with_speech_segments``, ``clip_speech_check``, ``clip_record`` and
``ground_excerpt`` run for real.

Failure modes this pins (written before the fix):

1. whisper misdetects a short accented clip and stores a translation (M1_05, M1_09);
2. over-correction: the chat's English is forced onto the waiter's Spanish (M1_03, M1_08)
   or the Turkish table chatter (M1_04, M1_07);
3. a forced re-run that prints a placeholder or a translation replaces the first pass;
4. an invented Gemini transcript drives the language check (M1_06 loop, M1_02);
5./6. an impossible-length or short-loop Gemini transcript claims speech (M1_02, M1_06);
7. whisper's silence placeholders, caption credits and loops are stored as speech;
8. render-time words come back in another language than the stored segments;
10. a failing re-run fails the analysis; 11. no chat language known;
14. the kill switch does not restore the old first pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services import speech_segments
from app.services.clip_understanding import clip_record
from app.services.speech_segments import ground_excerpt
from app.tasks import kria_clip_understanding as task

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri808_don_julio_clip_speech.json").read_text()
)
CLIPS = FIXTURE["clips"]


def _entry(clip: str) -> dict:
    """The clip as prod analysed it: Gemini's record, before any whisper segments."""
    row = CLIPS[clip]
    stored = row["prod_record"]
    return {
        "gcs_path": f"users/u/{clip}.mp4",
        "media_id": clip,
        "kind": "video",
        "duration_s": row["duration_s"],
        "analysis": {
            "duration_s": row["duration_s"],
            "understanding": {
                "summary": "a lunch at Don Julio",
                "speech": {
                    "has_speech": stored["has_speech"],
                    "transcript": stored["transcript"],
                },
            },
        },
    }


class _Whisper:
    """Recorded transcription: the first pass from ``first_pass``, re-runs from medium."""

    def __init__(self, first_pass: str) -> None:
        self.first_pass = first_pass
        self.calls: list[tuple[str, str | None]] = []

    def auto(self, path: str, **_kw):
        clip = Path(path).stem
        self.calls.append((clip, None))
        if self.first_pass == "prod":
            run = CLIPS[clip]["prod_whisper_first_pass"]
        else:
            run = CLIPS[clip]["local_whisper_medium"]["auto"]
        return [dict(w) for w in run["words"]], run["language"]

    def forced(self, path: str, language: str, **_kw):
        clip = Path(path).stem
        self.calls.append((clip, language))
        run = CLIPS[clip]["local_whisper_medium"][language]
        return [dict(w) for w in run["words"]], language


@pytest.fixture(params=["prod", "local"])
def whisper(request, monkeypatch) -> _Whisper:
    fake = _Whisper(request.param)
    monkeypatch.setattr(speech_segments, "transcribe_stored_clip", fake.auto)
    # raising=False: the same replay runs unchanged against the pre-fix tree (red control).
    monkeypatch.setattr(speech_segments, "transcribe_stored_clip_in", fake.forced, raising=False)
    monkeypatch.setattr(
        task, "_chat_language", lambda _item: [FIXTURE["chat_language"]], raising=False
    )
    monkeypatch.setattr(settings, "speech_excerpt_montage_enabled", True)
    if "clip_speech_language_check_enabled" in type(settings).model_fields:
        monkeypatch.setattr(settings, "clip_speech_language_check_enabled", True)
    return fake


def _speech(clip: str):
    out = _segmented(_entry(clip))
    return clip_record(out["analysis"]).speech


def _segmented(entry: dict) -> dict:
    try:
        return task._with_speech_segments(entry, item_id="item")
    except TypeError:  # the pre-fix signature (red control)
        return task._with_speech_segments(entry)


def _words(clip: str, speech) -> list[dict]:
    """Segments back to words, as a quoted-line pass grounds them."""
    return [
        {"text": token, "start_s": seg.start_s, "end_s": seg.end_s}
        for seg in speech.segments
        for token in seg.text.split()
    ]


def test_quoted_english_line_has_timed_words(whisper) -> None:
    speech = _speech("M1_05")
    assert speech.language == "en"
    assert ground_excerpt(_words("M1_05", speech), "What's your name?") is not None
    assert not any("isminiz" in seg.text for seg in speech.segments)


def test_gracias_is_never_written_in_cyrillic(whisper) -> None:
    speech = _speech("M1_09")
    texts = " ".join(seg.text for seg in speech.segments)
    assert "Спасибо" not in texts
    assert "Gracias" in texts and speech.transcript == "Gracias."


@pytest.mark.parametrize(
    ("clip", "language", "words"),
    [
        ("M1_08", "es", "Acá tenemos salsas tradicionales argentinas"),
        ("M1_03", "es", "criolla"),
    ],
)
def test_waiters_spanish_is_never_translated_to_the_chat_language(
    whisper, clip, language, words
) -> None:
    speech = _speech(clip)
    assert speech.language == language
    assert words in " ".join(seg.text for seg in speech.segments)
    assert all(lang is None for name, lang in whisper.calls if name == clip)  # no re-run


@pytest.mark.parametrize("clip", ["M1_04", "M1_07"])
def test_turkish_chatter_is_never_forced_into_english(whisper, clip) -> None:
    speech = _speech(clip)
    assert speech.language in {"tr", ""}
    assert not any(token in seg.text for seg in speech.segments for token in ("Yes", "Can I"))


def test_cheers_stays_quotable(whisper) -> None:
    speech = _speech("M1_01")
    assert ground_excerpt(_words("M1_01", speech), "Cheers") is not None or whisper.first_pass == (
        "local"  # medium writes it "Çeriz"; whisper-1's first pass has "Cheers."
    )


@pytest.mark.parametrize("clip", ["M1_02", "M1_06"])
def test_invented_or_looping_transcripts_claim_no_speech(whisper, clip) -> None:
    speech = _speech(clip)
    assert speech.transcript == "" and not speech.has_speech and not speech.segments
    assert not [name for name, _ in whisper.calls if name == clip]  # whisper never asked


def test_prod_records_main_creator_received_are_cleaned_on_read() -> None:
    """Records already stored keep their text; the reader drops what nobody said."""
    for clip in ("M1_02", "M1_06"):
        row = CLIPS[clip]
        stored = {
            "duration_s": row["duration_s"],
            "understanding": {"summary": "x", "speech": row["prod_record"]},
        }
        speech = clip_record(stored).speech
        assert speech.transcript == ""
    m1_05 = clip_record(
        {"duration_s": 4.0, "understanding": {"speech": CLIPS["M1_05"]["prod_record"]}}
    ).speech
    assert m1_05.transcript.startswith("Español, yeah. What's your name?")


def test_no_chat_language_still_never_stores_a_translation(whisper, monkeypatch) -> None:
    monkeypatch.setattr(task, "_chat_language", lambda _item: [])
    speech = _speech("M1_09")
    assert "Спасибо" not in " ".join(seg.text for seg in speech.segments)


def test_a_failing_rerun_keeps_the_analysis(whisper, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("whisper down")

    monkeypatch.setattr(speech_segments, "transcribe_stored_clip_in", boom)
    out = _segmented(_entry("M1_05"))
    assert out["analysis"]["understanding"]["speech"]["transcript"].startswith("Español")
    assert not clip_record(out["analysis"]).speech.segments  # the translation is not stored


def test_kill_switch_stores_the_first_pass_as_before(whisper, monkeypatch) -> None:
    monkeypatch.setattr(settings, "clip_speech_language_check_enabled", False)
    if whisper.first_pass != "prod":
        pytest.skip("the kill switch replays prod's own first pass")
    speech = _speech("M1_09")
    assert [seg.text for seg in speech.segments] == ["Спасибо."]
    assert speech.language == "ru"


def test_speech_in_any_script_is_kept_when_both_listeners_agree() -> None:
    """Neighbour: a Russian (or Korean, Arabic) creator's words are speech, not noise."""
    from app.services.clip_speech_check import pick_spoken_words

    words = [
        {"text": "Спасибо", "start_s": 0.0, "end_s": 0.5},
        {"text": "большое.", "start_s": 0.5, "end_s": 1.0},
    ]
    picked = pick_spoken_words(
        words,
        "ru",
        reference="Спасибо большое!",
        candidates=lambda: ["en"],
        retranscribe=lambda _lang: pytest.fail("agreeing listeners need no re-run"),
        duration_s=1.5,
    )
    assert picked.outcome == "kept" and picked.language == "ru" and len(picked.words) == 2


def test_whisper_silence_inventions_are_not_speech() -> None:
    """Placeholders, caption credits and a looped sentence (the kit's quiet M1_02 re-runs)."""
    from app.services.clip_speech_check import clean_words

    for language in ("en", "tr", "es"):
        assert clean_words(CLIPS["M1_02"]["local_whisper_medium"][language]["words"]) == []
    looped = [
        {"text": word, "start_s": i * 0.5, "end_s": i * 0.5 + 0.4}
        for i, word in enumerate(["Cheers!", "Cheers!", "Tepara.", "Tepara.", "Tepara."])
    ]
    assert [w["text"] for w in clean_words(looped)] == ["Cheers!", "Cheers!", "Tepara."]


def test_bracketed_markers_go_but_the_words_beside_them_stay() -> None:
    from app.services.clip_speech_check import clean_words

    words = CLIPS["M1_01"]["local_whisper_medium"]["en"]["words"]
    assert [w["text"] for w in clean_words(words)] == ["-", "Cheers."]
    unclosed = [
        {"text": "♪", "start_s": 0.0, "end_s": 0.2},
        {"text": "Hello", "start_s": 0.3, "end_s": 0.6},
        {"text": "there.", "start_s": 0.6, "end_s": 0.9},
    ]
    assert [w["text"] for w in clean_words(unclosed)] == ["♪", "Hello", "there."]


_REAL_CHAT_LANGUAGE = getattr(task, "_chat_language", None)  # absent on the pre-fix tree
