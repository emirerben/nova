"""Unit tests for stage B.5 tokenization."""

from __future__ import annotations

import pytest

from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon
from app.pipeline.text_overlay_v2.tokenize import (
    split_detection_into_words,
    tokenize_detections_into_words,
)


def _poly(x_min: float, y_min: float, x_max: float, y_max: float) -> OcrPolygon:
    return OcrPolygon(
        points=[(x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max)]
    )


def _det(text: str, *, t: float = 0.0, bbox=(0.1, 0.4, 0.9, 0.5)) -> FrameDetection:
    return FrameDetection(
        frame_t_s=t, text=text, polygon=_poly(*bbox), confidence=0.9
    )


# ── Single-word fast path ─────────────────────────────────────────────────────


def test_single_word_passthrough():
    det = _det("Hello")
    out = split_detection_into_words(det)
    assert len(out) == 1
    assert out[0] is det  # identity — no allocation


def test_single_word_with_surrounding_whitespace_passthrough():
    # A real OCR detection sometimes has trailing spaces; treat the result of
    # tokenization as one word, not "empty plus word".
    det = _det("  Hello  ")
    out = split_detection_into_words(det)
    assert len(out) == 1
    # We don't strip the text — the caller decides — but the count is right.
    assert out[0] is det


def test_empty_or_whitespace_only_passthrough():
    # The Pydantic schema enforces text min_length=1, so we cannot construct
    # an empty-text detection. Whitespace-only is the relevant edge case.
    det = _det(" \t ")
    out = split_detection_into_words(det)
    assert out == [det]


# ── Multi-word single-line ────────────────────────────────────────────────────


def test_two_words_split_horizontally():
    # "if you" — parent bbox spans x=0.4 to 0.6 (width 0.2), y=0.5 to 0.55.
    det = _det("if you", bbox=(0.4, 0.5, 0.6, 0.55))
    out = split_detection_into_words(det)
    assert [d.text for d in out] == ["if", "you"]
    # Both inherit timestamp and confidence.
    assert all(d.frame_t_s == det.frame_t_s for d in out)
    assert all(d.confidence == det.confidence for d in out)
    # "if" (2 chars) gets 2/5 of the width = 0.08; "you" (3 chars) gets 0.12.
    if_aabb = out[0].polygon.aabb()
    you_aabb = out[1].polygon.aabb()
    assert if_aabb[0] == pytest.approx(0.4)
    assert if_aabb[2] == pytest.approx(0.48, abs=1e-9)
    assert you_aabb[0] == pytest.approx(0.48, abs=1e-9)
    assert you_aabb[2] == pytest.approx(0.6)
    # Y range is shared (single line).
    assert if_aabb[1] == you_aabb[1] == 0.5
    assert if_aabb[3] == you_aabb[3] == 0.55


def test_three_words_proportional_widths():
    # "the cat sat" — 3+3+3 chars, parent width 0.6.
    det = _det("the cat sat", bbox=(0.2, 0.4, 0.8, 0.5))
    out = split_detection_into_words(det)
    assert [d.text for d in out] == ["the", "cat", "sat"]
    widths = [d.polygon.aabb()[2] - d.polygon.aabb()[0] for d in out]
    # Equal char counts → equal widths (0.2 each).
    for w in widths:
        assert w == pytest.approx(0.2, abs=1e-9)


def test_word_widths_proportional_to_char_count():
    # "a longword" — 1 + 8 chars, parent width 0.9.
    det = _det("a longword", bbox=(0.05, 0.5, 0.95, 0.55))
    out = split_detection_into_words(det)
    a_width = out[0].polygon.aabb()[2] - out[0].polygon.aabb()[0]
    long_width = out[1].polygon.aabb()[2] - out[1].polygon.aabb()[0]
    # 1/9 and 8/9 of total width 0.9.
    assert a_width == pytest.approx(0.1, abs=1e-9)
    assert long_width == pytest.approx(0.8, abs=1e-9)


# ── Multi-line ────────────────────────────────────────────────────────────────


def test_two_lines_one_word_each():
    # The exact pattern that broke fdaf3bbc: "if you\nput" coming out of OCR
    # as one multi-line detection. Should split into per-line, then per-word.
    det = _det("if\nput", bbox=(0.3, 0.4, 0.5, 0.6))
    out = split_detection_into_words(det)
    assert [d.text for d in out] == ["if", "put"]
    # Each line takes half the vertical extent.
    if_aabb = out[0].polygon.aabb()
    put_aabb = out[1].polygon.aabb()
    assert if_aabb[1] == 0.4
    assert if_aabb[3] == pytest.approx(0.5, abs=1e-9)
    assert put_aabb[1] == pytest.approx(0.5, abs=1e-9)
    assert put_aabb[3] == 0.6
    # Both span full width since each line is a single word.
    assert if_aabb[0] == put_aabb[0] == 0.3
    assert if_aabb[2] == put_aabb[2] == 0.5


def test_multiline_multiword_split_both_axes():
    # "if you\nput in" — common pattern as the next-frame OCR after "if you".
    det = _det("if you\nput in", bbox=(0.2, 0.4, 0.6, 0.6))
    out = split_detection_into_words(det)
    assert [d.text for d in out] == ["if", "you", "put", "in"]
    # First line: y range 0.4 to 0.5.
    assert all(d.polygon.aabb()[1] == 0.4 and d.polygon.aabb()[3] == pytest.approx(0.5, abs=1e-9)
               for d in out[:2])
    # Second line: y range 0.5 to 0.6.
    assert all(
        d.polygon.aabb()[1] == pytest.approx(0.5, abs=1e-9) and d.polygon.aabb()[3] == 0.6
        for d in out[2:]
    )


def test_blank_lines_are_skipped():
    # OCR sometimes emits a stray blank line in a paragraph block. Skip it.
    det = _det("Hello\n\nWorld", bbox=(0.0, 0.0, 1.0, 1.0))
    out = split_detection_into_words(det)
    assert [d.text for d in out] == ["Hello", "World"]


# ── Inheritance ───────────────────────────────────────────────────────────────


def test_frame_timestamp_inherited():
    det = _det("foo bar baz", t=3.14)
    out = split_detection_into_words(det)
    assert all(d.frame_t_s == 3.14 for d in out)


def test_confidence_inherited():
    det = FrameDetection(
        frame_t_s=0.0,
        text="foo bar",
        polygon=_poly(0.0, 0.0, 1.0, 0.1),
        confidence=0.42,
    )
    out = split_detection_into_words(det)
    assert all(d.confidence == 0.42 for d in out)


# ── Batch ─────────────────────────────────────────────────────────────────────


def test_tokenize_detections_into_words_flattens_in_order():
    dets = [
        _det("hello world", t=0.0),
        _det("foo", t=0.5),  # single word — passthrough
        _det("a b c", t=1.0),
    ]
    out = tokenize_detections_into_words(dets)
    # 2 + 1 + 3 = 6 outputs, in left-to-right input order.
    assert [d.text for d in out] == ["hello", "world", "foo", "a", "b", "c"]
    assert [d.frame_t_s for d in out] == [0.0, 0.0, 0.5, 1.0, 1.0, 1.0]


def test_tokenize_empty_list_returns_empty():
    assert tokenize_detections_into_words([]) == []
