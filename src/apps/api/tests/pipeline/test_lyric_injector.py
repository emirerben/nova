"""Lyric injection tests — verify per-slot overlay placement + style branches."""

from __future__ import annotations

import copy
import inspect

import pytest

from app.pipeline import lyric_injector
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
    assert ov0["start_s"] == pytest.approx(0.5, abs=1e-3)  # rebased into slot 0
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
    assert ov1["start_s"] == pytest.approx(1.0, abs=1e-3)


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
    assert overlays[0]["end_s"] == pytest.approx(overlays[1]["start_s"], abs=1e-6)
    assert overlays[1]["end_s"] == pytest.approx(overlays[2]["start_s"], abs=1e-6)
    # Last stage extends past line.end_s by _LAST_WORD_DWELL_S (0.30s).
    # line.end_s = 1.5 → expected last_end = 1.8 (slot-relative == section-
    # relative here since slot starts at 0).
    assert overlays[2]["end_s"] == pytest.approx(1.8, abs=1e-3)


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
    assert overlays[0]["end_s"] == pytest.approx(0.52, abs=1e-6)
    assert overlays[1]["start_s"] == pytest.approx(0.52, abs=1e-6)


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
    assert overlays[0]["start_s"] == pytest.approx(0.5, abs=1e-3)


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


# ── `"line"` style ────────────────────────────────────────────────────────────
#
# These lock the YouTube-lyric-video behavior: plain line, pre-roll, post-dwell
# past the vocal end, no per-word color sweep. Dense lines may cross-dissolve
# inside the fade-bound overlap budget. Defaults: pre_roll=0.40s,
# post_dwell=1.00s, max_overlap=0.40s, fade=50/250ms.


def test_line_emits_one_overlay_per_line_with_lyric_line_effect() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("Hello world", 1.0, 2.0, [("Hello", 1.0, 1.5), ("world", 1.5, 2.0)]),
            ("Goodbye now", 4.0, 5.0, [("Goodbye", 4.0, 4.5), ("now", 4.5, 5.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "hold_to_next_threshold_ms": 0},
    )
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 2
    for ov in overlays:
        assert ov["effect"] == "lyric-line"
        assert ov["role"] == "lyrics"
        # No per-word sweep — plain line should not carry word_timings.
        assert "word_timings" not in ov
        # Fade durations attached per overlay (defaults).
        assert ov["fade_in_ms"] == 50
        assert ov["fade_out_ms"] == 250
    assert overlays[0]["text"] == "Hello world"
    assert overlays[1]["text"] == "Goodbye now"


def test_line_applies_pre_roll_to_start() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache([("Hi", 1.0, 2.0, [("Hi", 1.0, 2.0)])])
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    ov = out["slots"][0]["text_overlays"][0]
    # Default pre_roll=0.40 → start_s = 1.0 - 0.40 = 0.60
    assert ov["start_s"] == pytest.approx(0.6, abs=1e-3)


def test_line_clamps_pre_roll_to_section_start() -> None:
    """A line starting right at the section edge can't pre-roll below 0."""
    recipe = _make_recipe([10.0])
    # Line at section-relative start 0.05 with default pre_roll 0.40 would
    # produce a negative window; injector must clamp to 0.
    cache = _make_lyrics_cache([("Edge", 0.05, 1.0, [("Edge", 0.05, 1.0)])])
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["start_s"] >= 0.0
    assert ov["start_s"] == pytest.approx(0.0, abs=1e-3)


def test_line_post_dwell_extends_into_overlap_budget() -> None:
    """Dense lines extend into the fade-bound overlap budget."""
    recipe = _make_recipe([10.0])
    # First line ends at 2.0; second line starts at 2.3.
    # Natural post-dwell would end first line at 2.0 + 2.0 = 4.0.
    # next_visual_start = 2.3 - 0.40 = 1.90.
    # overlap_budget = min(0.4, 0.15 + 0.10) = 0.25.
    # Expected end: min(4.0, 1.90 + 0.25, 2.3) = 2.15.
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.3, 3.0, [("Second", 2.3, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.4,
            "post_dwell_s": 2.0,
            "fade_in_ms": 150,
            "fade_out_ms": 100,
            "max_overlap_s": 0.4,
            "next_line_gap_s": 0.0,
        },
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    next_visual_start = 2.3 - 0.4
    overlap_budget = min(0.4, 0.15 + 0.10)
    assert ov0["end_s"] == pytest.approx(next_visual_start + overlap_budget, abs=1e-3)


def test_line_post_dwell_honored_when_section_has_slack() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 3.5, 4.0, [("Second", 3.5, 4.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.1,
            "post_dwell_s": 0.5,
            "fade_in_ms": 150,
            "fade_out_ms": 250,
        },
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    assert ov0["end_s"] == pytest.approx(2.0 + 0.5, abs=1e-3)


def test_line_post_dwell_capped_by_static_overlap_budget() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.3, 3.0, [("Second", 2.3, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.5,
            "post_dwell_s": 2.0,
            "fade_in_ms": 300,
            "fade_out_ms": 300,
            "max_overlap_s": lyric_injector._LINE_MAX_OVERLAP_S,
            "next_line_gap_s": 0.0,
        },
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    next_visual_start = 2.3 - 0.5
    visual_overlap_s = ov0["end_s"] - next_visual_start
    assert ov0["end_s"] == pytest.approx(
        next_visual_start + lyric_injector._LINE_MAX_OVERLAP_S,
        abs=1e-3,
    )
    assert visual_overlap_s <= lyric_injector._LINE_MAX_OVERLAP_S + 1e-9


def test_line_overlap_bounded_by_short_fades() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.3, 3.0, [("Second", 2.3, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.4,
            "post_dwell_s": 2.0,
            "fade_in_ms": 50,
            "fade_out_ms": 50,
            "max_overlap_s": lyric_injector._LINE_MAX_OVERLAP_S,
            "next_line_gap_s": 0.0,
        },
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    next_visual_start = 2.3 - 0.4
    visual_overlap_s = ov0["end_s"] - next_visual_start
    assert ov0["end_s"] == pytest.approx(next_visual_start + 0.1, abs=1e-3)
    assert visual_overlap_s == pytest.approx(0.1, abs=1e-3)


def test_line_zero_fades_yields_zero_visual_overlap_when_it_does_not_cut_audio() -> None:
    """Zero fades cap against visual start when the audio span still fits."""
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.6, 3.0, [("Second", 2.6, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.4,
            "post_dwell_s": 2.0,
            "fade_in_ms": 0,
            "fade_out_ms": 0,
            "max_overlap_s": lyric_injector._LINE_MAX_OVERLAP_S,
        },
    )
    ov0, ov1 = out["slots"][0]["text_overlays"]
    next_visual_start = 2.6 - 0.4
    assert ov0["end_s"] <= next_visual_start + 1e-9
    assert ov1["start_s"] >= ov0["end_s"]


def test_tight_lines_keep_their_fades() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.3, 3.0, [("Second", 2.3, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.4,
            "post_dwell_s": 2.0,
            "fade_in_ms": 150,
            "fade_out_ms": 100,
            "max_overlap_s": lyric_injector._LINE_MAX_OVERLAP_S,
            "next_line_gap_s": 0.0,
        },
    )
    ov0, ov1 = out["slots"][0]["text_overlays"]
    assert ov0["fade_out_ms"] == 100
    assert ov1["fade_in_ms"] == 150


def test_default_fades_when_keys_missing_do_not_disable_overlap() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.3, 3.0, [("Second", 2.3, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.4,
            "post_dwell_s": 2.0,
            "next_line_gap_s": 0.0,
        },
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    next_visual_start = 2.3 - 0.4
    expected_overlap_s = min(
        lyric_injector._LINE_MAX_OVERLAP_S,
        (lyric_injector._LINE_FADE_IN_MS + lyric_injector._LINE_FADE_OUT_MS) / 1000.0,
    )
    assert expected_overlap_s == pytest.approx(
        (lyric_injector._LINE_FADE_IN_MS + lyric_injector._LINE_FADE_OUT_MS) / 1000.0,
        abs=1e-3,
    )
    assert ov0["end_s"] - next_visual_start == pytest.approx(expected_overlap_s, abs=1e-3)
    assert ov0["end_s"] > next_visual_start


def test_line_last_line_uses_full_post_dwell() -> None:
    """No next line → use the full post-dwell."""
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache([("Only line", 1.0, 2.0, [("Only", 1.0, 1.5), ("line", 1.5, 2.0)])])
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    ov = out["slots"][0]["text_overlays"][0]
    # 2.0 + 1.0 = 3.0
    assert ov["end_s"] == pytest.approx(3.0, abs=1e-3)


def test_line_reads_tuning_from_config() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache([("Hi", 1.0, 2.0, [("Hi", 1.0, 2.0)])])
    cfg = {
        "enabled": True,
        "style": "line",
        "pre_roll_s": 0.30,
        "post_dwell_s": 1.50,
        "fade_in_ms": 200,
        "fade_out_ms": 400,
    }
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, cfg)
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["start_s"] == pytest.approx(0.70, abs=1e-3)  # 1.0 - 0.30
    assert ov["end_s"] == pytest.approx(3.50, abs=1e-3)  # 2.0 + 1.50
    assert ov["fade_in_ms"] == 200
    assert ov["fade_out_ms"] == 400


def test_line_no_overrides_preserves_default_timing_contract() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 3.0, 4.0, [("Second", 3.0, 4.0)]),
        ]
    )
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    ov0, ov1 = out["slots"][0]["text_overlays"]

    assert ov0["start_s"] == pytest.approx(0.6, abs=1e-3)
    assert ov0["end_s"] == pytest.approx(2.9, abs=1e-3)
    assert ov0["fade_in_ms"] == 50
    assert ov0["fade_out_ms"] == 250
    assert ov1["start_s"] == pytest.approx(2.6, abs=1e-3)
    assert ov1["end_s"] == pytest.approx(5.0, abs=1e-3)


def test_line_post_dwell_can_hold_two_seconds_when_caps_have_slack() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 6.0, 7.0, [("Second", 6.0, 7.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "post_dwell_s": 2.0},
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    assert ov0["end_s"] <= 4.0
    assert ov0["end_s"] == pytest.approx(4.0, abs=1e-3)


def test_line_pre_roll_override_moves_visual_start_and_clamps_first_line() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("Edge", 0.5, 1.0, [("Edge", 0.5, 1.0)]),
            ("Later", 2.0, 3.0, [("Later", 2.0, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "pre_roll_s": 0.8},
    )
    ov0, ov1 = out["slots"][0]["text_overlays"]
    assert ov0["start_s"] == pytest.approx(0.0, abs=1e-3)
    assert ov1["start_s"] == pytest.approx(1.2, abs=1e-3)


def test_line_next_line_gap_caps_post_dwell_when_there_is_slack() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 3.0, 4.0, [("Second", 3.0, 4.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "post_dwell_s": 2.0, "next_line_gap_s": 0.3},
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    assert ov0["end_s"] <= 3.0 - 0.3
    assert ov0["end_s"] == pytest.approx(2.7, abs=1e-3)


def test_line_next_line_gap_never_cuts_current_audio() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 2.1, 3.0, [("Second", 2.1, 3.0)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "post_dwell_s": 2.0, "next_line_gap_s": 0.3},
    )
    ov0 = out["slots"][0]["text_overlays"][0]
    assert ov0["end_s"] >= 2.0
    assert ov0["end_s"] == pytest.approx(2.0, abs=1e-3)


def test_line_max_overlap_s_is_reachable_when_fades_are_long_enough() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 3.0, 4.0, [("Second", 3.0, 4.0)]),
        ]
    )
    cfg = {
        "enabled": True,
        "style": "line",
        "pre_roll_s": 1.2,
        "post_dwell_s": 2.0,
        "next_line_gap_s": 0.0,
        "max_overlap_s": 1.0,
        "fade_in_s": 0.4,
        "fade_out_s": 0.6,
    }
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, cfg)
    ov0 = out["slots"][0]["text_overlays"][0]
    expected_end = min(
        2.0 + 2.0,
        3.0 - 1.2 + min(1.0, 0.4 + 0.6),
        3.0 - 0.0,
    )
    expected_end = max(expected_end, 2.0)
    assert ov0["end_s"] == pytest.approx(expected_end, abs=1e-3)
    assert ov0["end_s"] - (3.0 - 1.2) == pytest.approx(1.0, abs=1e-3)


def test_line_fade_seconds_aliases_emit_milliseconds() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache([("Hi", 1.0, 2.0, [("Hi", 1.0, 2.0)])])
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "fade_in_s": 0.1, "fade_out_s": 0.4},
    )
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["fade_in_ms"] == 100
    assert ov["fade_out_ms"] == 400


def test_line_fade_seconds_alias_wins_over_legacy_ms() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache([("Hi", 1.0, 2.0, [("Hi", 1.0, 2.0)])])
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "line", "fade_in_s": 0.1, "fade_in_ms": 200},
    )
    ov = out["slots"][0]["text_overlays"][0]
    assert ov["fade_in_ms"] == 100


def test_line_injection_does_not_mutate_cached_lyrics() -> None:
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 3.0, 4.0, [("Second", 3.0, 4.0)]),
        ]
    )
    before = copy.deepcopy(cache)
    inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    assert cache == before


def test_line_style_does_not_affect_karaoke_path() -> None:
    """Template-scoping guard: switching to `line` must not emit karaoke fields.

    Reverse direction is also implied — picking `karaoke` must not emit
    `fade_in_ms` / `fade_out_ms`. Verifies the dispatch is mutually exclusive
    so other templates that rely on karaoke aren't disturbed.
    """
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache([("Hello", 1.0, 2.0, [("Hello", 1.0, 1.5), ("world", 1.5, 2.0)])])

    out_line = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    ov_line = out_line["slots"][0]["text_overlays"][0]
    assert ov_line["effect"] == "lyric-line"
    assert "word_timings" not in ov_line
    assert "highlight_color" not in ov_line

    # Karaoke path on a fresh recipe — must still produce word_timings and
    # NOT carry the line-style fade fields.
    recipe2 = _make_recipe([10.0])
    out_kar = inject_lyric_overlays(
        recipe2, cache, 0.0, 10.0, {"enabled": True, "style": "karaoke"}
    )
    ov_kar = out_kar["slots"][0]["text_overlays"][0]
    assert ov_kar["effect"] == "karaoke-line"
    assert "word_timings" in ov_kar
    assert "fade_in_ms" not in ov_kar
    assert "fade_out_ms" not in ov_kar


def test_line_only_timing_knobs_are_not_read_by_other_styles() -> None:
    karaoke_source = inspect.getsource(lyric_injector._inject_karaoke)
    pop_source = inspect.getsource(lyric_injector._inject_per_word_pop)
    line_only_keys = (
        "pre_roll_s",
        "post_dwell_s",
        "next_line_gap_s",
        "max_overlap_s",
        "fade_in_s",
        "fade_out_s",
        "fade_in_ms",
        "fade_out_ms",
        "hold_to_next_threshold_ms",
    )
    for key in line_only_keys:
        assert key not in karaoke_source
        assert key not in pop_source
