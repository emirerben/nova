from __future__ import annotations

import copy

import pytest

from app.pipeline.lyric_injector import (
    _apply_lyric_style_overrides,
    apply_lyric_line_overrides,
    build_lyric_overlay_snapshot,
    inject_lyric_overlays,
)


def _cache() -> dict:
    return {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "Hello world",
                "start_s": 10.0,
                "end_s": 12.0,
                "words": [
                    {"text": "Hello", "start_s": 10.0, "end_s": 10.75},
                    {"text": "world", "start_s": 10.75, "end_s": 12.0},
                ],
            },
            {
                "text": "Next lyric line",
                "start_s": 12.4,
                "end_s": 15.1,
                "words": [
                    {"text": "Next", "start_s": 12.4, "end_s": 13.0},
                    {"text": "lyric", "start_s": 13.0, "end_s": 14.0},
                    {"text": "line", "start_s": 14.0, "end_s": 15.1},
                ],
            },
        ],
    }


def test_equal_word_count_preserves_word_timings_verbatim() -> None:
    cache = _cache()
    out = apply_lyric_line_overrides(
        cache,
        {"L0": {"text": "Good night", "orig_text": "Hello world", "orig_start_s": 10.0}},
    )

    assert out is not cache
    assert out["lines"][0]["text"] == "Good night"
    assert [w["text"] for w in out["lines"][0]["words"]] == ["Good", "night"]
    assert [(w["start_s"], w["end_s"]) for w in out["lines"][0]["words"]] == [
        (10.0, 10.75),
        (10.75, 12.0),
    ]
    assert cache["lines"][0]["text"] == "Hello world"


def test_different_word_count_redistributes_monotonic_inside_line_window() -> None:
    cache = _cache()
    out = apply_lyric_line_overrides(
        cache,
        {
            "L0": {
                "text": "A much longer rewrite",
                "orig_text": "Hello world",
                "orig_start_s": 10.0,
            }
        },
    )

    words = out["lines"][0]["words"]
    assert [w["text"] for w in words] == ["A", "much", "longer", "rewrite"]
    assert words[0]["start_s"] == pytest.approx(10.0)
    assert words[-1]["end_s"] == pytest.approx(12.0)
    for prev, nxt in zip(words, words[1:], strict=False):
        assert prev["end_s"] <= nxt["start_s"]
    assert all(10.0 <= w["start_s"] <= w["end_s"] <= 12.0 for w in words)


def test_line_bounds_are_byte_equal_for_every_line() -> None:
    cache = _cache()
    before = [(line["start_s"], line["end_s"]) for line in cache["lines"]]
    out = apply_lyric_line_overrides(
        cache,
        {
            "L0": {"text": "Changed", "orig_text": "Hello world", "orig_start_s": 10.0},
            "L1": {
                "text": "Different amount of words here",
                "orig_text": "Next lyric line",
                "orig_start_s": 12.4,
            },
        },
    )

    assert [(line["start_s"], line["end_s"]) for line in out["lines"]] == before


def test_fingerprint_mismatch_skips_override_and_does_not_mutate_input() -> None:
    cache = _cache()
    before = copy.deepcopy(cache)

    out = apply_lyric_line_overrides(
        cache,
        {"L0": {"text": "Changed", "orig_text": "Other words", "orig_start_s": 99.0}},
    )

    assert out == before
    assert cache == before


def test_malformed_entries_never_raise_and_valid_entries_still_apply() -> None:
    cache = _cache()
    out = apply_lyric_line_overrides(
        cache,
        {
            "bad": {"text": "ignored", "orig_text": "Hello world", "orig_start_s": 10.0},
            "L999": {"text": "ignored", "orig_text": "Hello world", "orig_start_s": 10.0},
            "L0": "not a dict",
            "L1": {"text": "Valid edit", "orig_text": "Next lyric line", "orig_start_s": 12.4},
        },
    )

    assert out["lines"][0]["text"] == "Hello world"
    assert out["lines"][1]["text"] == "Valid edit"


def test_style_overrides_patch_only_style_fields_and_snapshot_groups_lines() -> None:
    recipe = {"slots": [{"position": 1, "target_duration_s": 4.0, "text_overlays": []}]}
    out = inject_lyric_overlays(
        recipe,
        _cache(),
        10.0,
        14.0,
        {"enabled": True, "style": "karaoke"},
    )
    before_timing = [(o["start_s"], o["end_s"]) for o in out["slots"][0]["text_overlays"]]
    _apply_lyric_style_overrides(
        out,
        {
            "L0": {
                "style": {
                    "color": "#112233",
                    "highlight_color": "#445566",
                    "font_family": "Inter",
                    "size_px": 72,
                }
            }
        },
    )

    overlay = out["slots"][0]["text_overlays"][0]
    assert overlay["text_color"] == "#112233"
    assert overlay["highlight_color"] == "#445566"
    assert overlay["font_family"] == "Inter"
    assert overlay["text_size_px"] == 72
    assert [(o["start_s"], o["end_s"]) for o in out["slots"][0]["text_overlays"]] == before_timing

    snapshot = build_lyric_overlay_snapshot(out, [4.0])
    assert snapshot[0]["line_key"] == "L0"
    assert snapshot[0]["text"] == "Hello world"
    assert snapshot[0]["start_s"] == pytest.approx(0.0)
    assert snapshot[0]["end_s"] == pytest.approx(2.0)
    assert snapshot[0]["color"] == "#112233"
