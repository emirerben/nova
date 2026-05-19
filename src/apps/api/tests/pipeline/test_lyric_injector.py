"""Lyric injection tests — verify per-slot overlay placement + style branches."""

from __future__ import annotations

from app.pipeline.lyric_injector import inject_lyric_overlays


def _make_recipe(slot_durations: list[float]) -> dict:
    return {
        "slots": [
            {"position": i + 1, "target_duration_s": d, "text_overlays": []}
            for i, d in enumerate(slot_durations)
        ]
    }


def _make_lyrics_cache(
    lines: list[tuple[str, float, float, list[tuple[str, float, float]]]],
) -> dict:
    """Helper: lines = [(text, start_s, end_s, [(word, ws, we), ...]), ...]"""
    return {
        "lines": [
            {
                "text": text,
                "start_s": start,
                "end_s": end,
                "words": [{"text": w, "start_s": ws, "end_s": we} for w, ws, we in words],
            }
            for text, start, end, words in lines
        ]
    }


def test_disabled_config_leaves_recipe_unchanged() -> None:
    recipe = _make_recipe([5.0, 5.0])
    cache = _make_lyrics_cache([("Hello", 0.0, 1.0, [("Hello", 0.0, 1.0)])])
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": False})
    for slot in out["slots"]:
        assert slot["text_overlays"] == []


def test_karaoke_injects_one_overlay_per_line_in_correct_slot() -> None:
    recipe = _make_recipe([5.0, 5.0])
    cache = _make_lyrics_cache(
        [
            ("Hello world", 0.5, 1.5, [("Hello", 0.5, 1.0), ("world", 1.0, 1.5)]),
            ("Goodbye now", 6.0, 7.5, [("Goodbye", 6.0, 6.8), ("now", 6.8, 7.5)]),
        ]
    )
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "karaoke"})
    # Line 1 lives in slot 0 (0-5s), line 2 in slot 1 (5-10s)
    assert len(out["slots"][0]["text_overlays"]) == 1
    assert len(out["slots"][1]["text_overlays"]) == 1
    ov0 = out["slots"][0]["text_overlays"][0]
    assert ov0["effect"] == "karaoke-line"
    assert ov0["text"] == "Hello world"
    assert ov0["start_s"] == 0.5  # rebased into slot 0
    # Per-word timings carry duration_cs
    assert "word_timings" in ov0
    assert len(ov0["word_timings"]) == 2
    assert all("duration_cs" in w for w in ov0["word_timings"])
    # Highlight color + role
    assert ov0["role"] == "lyrics"
    assert ov0["highlight_color"]
    ov1 = out["slots"][1]["text_overlays"][0]
    # Line 2 starts at video time 6.0; slot 1 starts at video time 5.0,
    # so the overlay's slot-relative start is 1.0.
    assert abs(ov1["start_s"] - 1.0) < 1e-3


def test_per_word_pop_emits_one_overlay_per_word() -> None:
    recipe = _make_recipe([6.0])
    cache = _make_lyrics_cache([("Hi there", 0.0, 1.5, [("Hi", 0.0, 0.5), ("there", 0.5, 1.5)])])
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        6.0,
        {"enabled": True, "style": "per-word-pop"},
    )
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 2
    assert overlays[0]["effect"] == "pop-in"
    assert overlays[0]["text"] == "Hi"
    assert overlays[1]["text"] == "there"


def test_section_filter_drops_lines_outside_window() -> None:
    """A line that ends before best_start_s or starts after best_end_s drops."""
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache(
        [
            ("Early", 0.5, 1.0, [("Early", 0.5, 1.0)]),
            ("In section", 5.5, 6.0, [("In", 5.5, 5.7), ("section", 5.7, 6.0)]),
            ("Late", 20.0, 21.0, [("Late", 20.0, 21.0)]),
        ]
    )
    # Section = [5.0, 10.0]. Only "In section" overlaps.
    out = inject_lyric_overlays(recipe, cache, 5.0, 10.0, {"enabled": True, "style": "karaoke"})
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 1
    assert overlays[0]["text"] == "In section"
    # 5.5s in absolute time → 0.5s in section-relative → 0.5s in slot-relative
    assert abs(overlays[0]["start_s"] - 0.5) < 1e-3


def test_partial_overlap_line_is_dropped() -> None:
    """A line that straddles section boundary is dropped (v1 hard rule)."""
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache(
        [("Crosses boundary", 4.0, 6.0, [("Crosses", 4.0, 5.0), ("boundary", 5.0, 6.0)])]
    )
    out = inject_lyric_overlays(recipe, cache, 5.0, 10.0, {"enabled": True, "style": "karaoke"})
    assert out["slots"][0]["text_overlays"] == []


def test_no_cache_is_noop() -> None:
    recipe = _make_recipe([5.0])
    out = inject_lyric_overlays(recipe, None, 0.0, 5.0, {"enabled": True})
    assert out["slots"][0]["text_overlays"] == []


def test_unknown_style_is_noop() -> None:
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache([("Hi", 0.0, 0.5, [("Hi", 0.0, 0.5)])])
    out = inject_lyric_overlays(recipe, cache, 0.0, 5.0, {"enabled": True, "style": "ascii-rain"})
    assert out["slots"][0]["text_overlays"] == []
