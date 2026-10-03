"""KRI-282: timed segments + quote grounding for spoken-excerpt montages."""

from __future__ import annotations

from app.schemas.clip_understanding import ClipSpeech, ClipUnderstanding
from app.services.speech_segments import (
    attach_segments_to_analysis,
    ground_excerpt,
    words_to_segments,
)

TEXT = (
    "So the thing I learned in Lisbon was, never rush a good espresso. "
    "Honestly it changed my whole morning routine! Then we left."
)


def _words(text: str = TEXT, step: float = 0.35, dur: float = 0.3) -> list[dict]:
    words, t = [], 0.0
    for token in text.split():
        words.append({"text": token, "start_s": round(t, 3), "end_s": round(t + dur, 3)})
        t += step
    return words


def test_segments_split_on_sentence_punctuation() -> None:
    segments = words_to_segments(_words())
    assert [s.text for s in segments] == [
        "So the thing I learned in Lisbon was, never rush a good espresso.",
        "Honestly it changed my whole morning routine!",
        "Then we left.",
    ]
    assert segments[0].start_s == 0.0 and segments[0].end_s < segments[1].start_s


def test_segments_split_on_long_pause() -> None:
    words = _words("hello there my friend how are you")
    for word in words[4:]:
        word["start_s"] += 2.0
        word["end_s"] += 2.0
    assert [s.text for s in words_to_segments(words)] == [
        "hello there my friend",
        "how are you",
    ]


def test_exact_quote_grounds_with_sentence_safe_padding() -> None:
    words = _words()
    got = ground_excerpt(words, "never rush a good espresso")
    assert got is not None and got.exact
    first = next(w for w in words if w["text"] == "never")
    last = next(w for w in words if w["text"] == "espresso.")
    # Pads outward but never reaches the neighbouring words.
    assert got.start_s <= first["start_s"] and got.end_s >= last["end_s"]
    before = words[words.index(first) - 1]
    after = words[words.index(last) + 1]
    assert got.start_s >= before["end_s"] and got.end_s <= after["start_s"]


def test_head_ellipsis_tail_spans_a_passage() -> None:
    got = ground_excerpt(_words(), "never rush ... morning routine")
    assert got is not None and got.exact
    assert got.word_end_s > ground_excerpt(_words(), "never rush").word_end_s


def test_near_miss_quote_grounds_fuzzy_but_unrelated_does_not() -> None:
    fuzzy = ground_excerpt(_words(), "never rush a great espresso")
    assert fuzzy is not None and not fuzzy.exact and fuzzy.overlap >= 0.8
    assert ground_excerpt(_words(), "completely unrelated phrase here") is None
    assert ground_excerpt(_words(), "") is None
    assert ground_excerpt([], "never rush") is None


def test_accents_case_and_punctuation_fold() -> None:
    words = _words("Bugün İstanbul'da çok güzel bir gün geçirdik")
    assert ground_excerpt(words, "istanbul'da cok guzel") is not None


def test_repeated_phrase_resolves_in_order_with_min_after() -> None:
    words = _words("buy it now because you really should buy it now today")
    first = ground_excerpt(words, "buy it now")
    second = ground_excerpt(words, "buy it now", min_after_s=first.word_end_s)
    assert second is not None and second.word_start_s > first.word_end_s


def test_attach_segments_is_additive_and_flag_agnostic() -> None:
    analysis = {"understanding": {"speech": {"has_speech": True, "transcript": "hi"}}, "x": 1}
    out = attach_segments_to_analysis(analysis, _words(), "en")
    assert out["x"] == 1 and analysis["understanding"]["speech"].get("segments") is None
    speech = out["understanding"]["speech"]
    assert speech["transcript"] == "hi" and speech["language"] == "en" and speech["segments"]
    assert attach_segments_to_analysis(analysis, [], "en") is analysis


def test_clip_speech_segments_omitted_when_empty_and_prompt_view_gated() -> None:
    assert "segments" not in ClipSpeech(has_speech=True, transcript="hi").model_dump()
    record = ClipUnderstanding(
        speech=ClipSpeech(
            has_speech=True,
            to_camera=True,
            transcript="hello",
            segments=[{"start_s": 0.0, "end_s": 2.0, "text": "hello there"}, {"bad": 1}],
        )
    )
    assert "segments" not in record.prompt_view()["speech"]
    view = record.prompt_view(include_segments=True)["speech"]
    assert view["segments"] == [{"s": 0.0, "e": 2.0, "text": "hello there"}]
