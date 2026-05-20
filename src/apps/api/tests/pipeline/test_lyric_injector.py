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


def test_per_word_pop_accumulates_cumulative_line_text() -> None:
    """Each stage carries cumulative-line text; only the suffix is animated.

    Locks the fix for the original "tek kelime gidiyor sonra direk gidiyor"
    failure where each overlay held a single word that vanished before the
    next appeared, leaving the lyrics unreadable.
    """
    recipe = _make_recipe([6.0])
    cache = _make_lyrics_cache(
        [
            (
                "I got room",
                0.0,
                1.8,
                [("I", 0.0, 0.4), ("got", 0.4, 0.9), ("room", 0.9, 1.8)],
            )
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        6.0,
        {"enabled": True, "style": "per-word-pop"},
    )
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 3
    # Cumulative text builds up word by word.
    assert overlays[0]["text"] == "I"
    assert overlays[1]["text"] == "I got"
    assert overlays[2]["text"] == "I got room"
    # Each stage tells the renderer which trailing word to animate so the
    # already-visible prefix doesn't re-pop on every new word.
    assert overlays[0]["pop_animated_suffix"] == "I"
    assert overlays[1]["pop_animated_suffix"] == "got"
    assert overlays[2]["pop_animated_suffix"] == "room"
    assert all(o["effect"] == "pop-in" for o in overlays)


def test_per_word_pop_overlays_are_butted_with_no_gap_or_overlap() -> None:
    """Middle stages end EXACTLY at the next word's start_s — no floor.

    Forcing a minimum-duration floor on a middle stage would push its end_s
    past the next stage's start_s, two overlays would render simultaneously,
    and the screen would glitch with stacked text boxes. The fix drops the
    floor for middle stages; the last stage gets a small dwell so the full
    line settles before clearing.
    """
    recipe = _make_recipe([6.0])
    cache = _make_lyrics_cache(
        [
            (
                "abc def ghi",
                0.0,
                1.5,
                [("abc", 0.0, 0.5), ("def", 0.5, 1.0), ("ghi", 1.0, 1.5)],
            )
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        6.0,
        {"enabled": True, "style": "per-word-pop"},
    )
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 3
    # Middle stages butt edge-to-edge: stage[i].end_s == stage[i+1].start_s.
    assert abs(overlays[0]["end_s"] - overlays[1]["start_s"]) < 1e-6
    assert abs(overlays[1]["end_s"] - overlays[2]["start_s"]) < 1e-6
    # Last stage extends past line.end_s by _LAST_WORD_DWELL_S (0.30s).
    # line.end_s = 1.5 → expected last_end = 1.8 (slot-relative == section-
    # relative here since slot starts at 0).
    assert abs(overlays[2]["end_s"] - 1.8) < 1e-3


def test_per_word_pop_drops_sub_renderable_middle_stage() -> None:
    """A middle word whose natural span < _MIN_RENDERABLE_S is dropped.

    The next stage's cumulative text still includes the dropped word, so the
    viewer sees it as part of that stage. Floor-clamping the duration instead
    would cause the dropped stage to overlap the next one and glitch.
    """
    recipe = _make_recipe([6.0])
    # Middle word "b" lasts only 20ms (next word starts at 0.52) — below the
    # 50ms _MIN_RENDERABLE_S threshold, so it must be dropped.
    cache = _make_lyrics_cache(
        [
            (
                "a b c",
                0.0,
                1.0,
                [("a", 0.0, 0.5), ("b", 0.5, 0.52), ("c", 0.52, 1.0)],
            )
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        6.0,
        {"enabled": True, "style": "per-word-pop"},
    )
    overlays = out["slots"][0]["text_overlays"]
    # Three words but only two stages emitted — the "a b" stage was too short
    # to render. The "a b c" stage's cumulative text still surfaces "b".
    assert [o["text"] for o in overlays] == ["a", "a b c"]
    # The kept stages are still butted: stage 0 ends right at "c"'s start
    # (where the dropped "b" stage would have begun).
    assert abs(overlays[0]["end_s"] - 0.52) < 1e-6
    assert abs(overlays[1]["start_s"] - 0.52) < 1e-6


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
