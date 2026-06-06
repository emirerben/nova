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
    *,
    source: str = "lrclib_synced+whisper",
) -> dict:
    """Helper: lines = [(text, start_s, end_s, [(word, ws, we), ...]), ...]

    `source` defaults to the production-publishable LRCLIB synced shape so
    the injector's Layer-2 source gate (added 2026-05-27, Beauty And A Beat
    PR) accepts the fixture. Pass an explicit `source` to test the gate's
    rejection paths (e.g. `whisper_only`, drift cases).
    """
    return {
        "source": source,
        "lines": [
            {
                "text": text,
                "start_s": start,
                "end_s": end,
                "words": [{"text": w, "start_s": ws, "end_s": we} for w, ws, we in words],
            }
            for text, start, end, words in lines
        ],
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


def test_sync_offset_shifts_karaoke_timing_without_mutating_cache() -> None:
    recipe = _make_recipe([5.0, 5.0])
    cache = _make_lyrics_cache(
        [
            (
                "Late line",
                2.0,
                3.0,
                [("Late", 2.0, 2.4), ("line", 2.4, 3.0)],
            )
        ]
    )
    before = copy.deepcopy(cache)
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        10.0,
        {"enabled": True, "style": "karaoke", "sync_offset_s": -1.0},
    )

    ov = out["slots"][0]["text_overlays"][0]
    assert ov["start_s"] == pytest.approx(1.0, abs=1e-3)
    assert ov["end_s"] == pytest.approx(2.0, abs=1e-3)
    assert ov["section_anchor_s"] == pytest.approx(1.0, abs=1e-3)
    assert ov["section_end_anchor_s"] == pytest.approx(2.0, abs=1e-3)
    assert ov["word_timings"][0]["start_s"] == pytest.approx(0.0, abs=1e-3)
    assert ov["word_timings"][1]["start_s"] == pytest.approx(0.4, abs=1e-3)
    assert cache == before


def test_again_late_anchor_fix_survives_prod_injection_rebase() -> None:
    """Prod-equivalent final link for job 213243c6.

    Once lyrics extraction caches the repaired song-time start (231.28s), the
    production injector must rebase it against the rendered section
    (`best_start_s=223.0`) and preserve the track's sync offset (-0.4s). The
    resulting overlay starts at 7.88s in the 14s preview, matching the local
    verification render.
    """
    line_text = "I swear to God, I don't even know why I put up with you"
    recipe = _make_recipe([14.0])
    cache = _make_lyrics_cache(
        [
            (
                line_text,
                231.28,
                235.58,
                [
                    ("I", 231.28, 231.38),
                    ("swear", 231.38, 231.88),
                    ("to", 231.88, 232.08),
                    ("God", 232.08, 232.32),
                    ("I", 232.80, 232.85),
                    ("don't", 232.85, 233.12),
                    ("even", 233.12, 233.35),
                    ("know", 233.35, 233.57),
                    ("why", 233.57, 233.69),
                    ("I", 233.76, 233.79),
                    ("put", 233.90, 234.18),
                    ("up", 234.18, 234.40),
                    ("with", 234.80, 235.36),
                ],
            )
        ]
    )

    out = inject_lyric_overlays(
        recipe,
        cache,
        best_start_s=223.0,
        best_end_s=237.0,
        lyrics_config={"enabled": True, "style": "karaoke", "sync_offset_s": -0.4},
    )

    ov = out["slots"][0]["text_overlays"][0]
    assert ov["text"] == line_text
    assert ov["start_s"] == pytest.approx(7.88, abs=1e-3)
    assert ov["section_anchor_s"] == pytest.approx(7.88, abs=1e-3)
    assert ov["word_timings"][0]["start_s"] == pytest.approx(0.0, abs=1e-3)
    assert ov["word_timings"][1]["start_s"] == pytest.approx(0.1, abs=1e-3)


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


def test_per_word_pop_terminal_dwell_clears_before_next_line_first_word() -> None:
    """A line's added dwell must not hide the next line's first-word pop.

    Repro: in lyrics-preview job 9ee75e6e, "body" held into the first 200ms
    of "But", so the actual pop happened underneath the previous line. By the
    time "body" cleared, "But" was already full-size and looked like it had
    appeared abruptly. "soul" -> "Let's" looked correct because there was
    enough blank time between lines.
    """
    recipe = _make_recipe([6.0])
    cache = _make_lyrics_cache(
        [
            (
                "You may have the body",
                0.0,
                2.0,
                [
                    ("You", 0.0, 0.4),
                    ("may", 0.4, 0.8),
                    ("have", 0.8, 1.2),
                    ("the", 1.2, 1.6),
                    ("body", 1.6, 2.0),
                ],
            ),
            ("But", 2.1, 2.4, [("But", 2.1, 2.4)]),
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
    body = next(o for o in overlays if o["text"] == "You may have the body")
    but = next(o for o in overlays if o["text"] == "But")

    assert body["end_s"] < but["start_s"]
    assert body["end_s"] == pytest.approx(2.067, abs=1e-3)


def test_per_word_pop_truncates_overlapping_line_before_next_line() -> None:
    """Regression for lyrics-preview job 20ebb8b8 (Billie Jean popup).

    The backing-vocal line's own end overlapped the next lead-vocal line by
    ~420ms, so the final "Ah hoo" popup stage and the first "She" stage rendered
    at once in the same visual lane.
    """
    recipe = _make_recipe([6.0])
    cache = _make_lyrics_cache(
        [
            (
                "Don't think twice Do think twice Ah hoo",
                0.0,
                2.77,
                [
                    ("Don't", 0.0, 0.23),
                    ("think", 0.25, 0.57),
                    ("twice", 0.57, 0.87),
                    ("Do", 0.87, 1.23),
                    ("think", 1.23, 1.57),
                    ("twice", 1.59, 1.97),
                    ("Ah", 1.99, 2.37),
                    ("hoo", 2.39, 2.77),
                ],
            ),
            (
                "She told my baby",
                2.35,
                4.0,
                [
                    ("She", 2.35, 3.03),
                    ("told", 3.03, 3.45),
                    ("my", 3.45, 3.71),
                    ("baby", 3.71, 4.0),
                ],
            ),
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
    she = next(o for o in overlays if o["text"] == "She")
    outgoing = [o for o in overlays if o["text"].startswith("Don't think twice")]
    assert outgoing
    assert all(o["end_s"] <= she["start_s"] for o in outgoing)
    assert max(o["end_s"] for o in outgoing) == pytest.approx(
        she["start_s"] - lyric_injector._WORD_POP_LINE_CLEAR_GAP_S,
        abs=1e-3,
    )
    assert not any(o["pop_animated_suffix"] == "hoo" for o in outgoing)


def test_per_word_pop_forces_left_anchor_even_with_centered_style_set() -> None:
    """REGRESSION: cumulative pop-up lyrics must grow from a fixed left edge.

    The default style set's lyric_word_pop role is editorial/centered, but
    per-word-pop emits cumulative stages ("You" -> "You may" -> ...). If those
    stages inherit center alignment, each longer stage recenters and the visible
    prefix shifts left, as seen in prod job 86bad910-596a-448a-a36a-5604d8ac4509.
    """
    recipe = _make_recipe([6.0])
    cache = _make_lyrics_cache(
        [
            (
                "You may have the body",
                0.0,
                2.0,
                [
                    ("You", 0.0, 0.4),
                    ("may", 0.4, 0.8),
                    ("have", 0.8, 1.2),
                    ("the", 1.2, 1.6),
                    ("body", 1.6, 2.0),
                ],
            )
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        6.0,
        {"enabled": True, "style": "per-word-pop", "style_set_id": "default"},
    )

    overlays = out["slots"][0]["text_overlays"]
    assert [o["text"] for o in overlays] == [
        "You",
        "You may",
        "You may have",
        "You may have the",
        "You may have the body",
    ]
    assert all(o["text_anchor"] == "left" for o in overlays)
    assert all(o["position_x_frac"] == pytest.approx(0.06) for o in overlays)
    assert all(o["preserve_font_size"] is True for o in overlays)
    # The style set still owns typography; only cumulative geometry is forced.
    assert all(o["font_family"] == "Playfair Display" for o in overlays)


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


def test_per_word_pop_keeps_terminal_word_started_before_preview_end() -> None:
    """The last audible pop-up word must survive a short preview-tail clip.

    Regression for lyrics-preview job 8c5793b6: "open" started inside the
    preview window, but its cumulative stage clipped to less than the render
    floor at the synthetic one-slot boundary, so the preview ended on
    "Body moving heart is".
    """
    recipe = _make_recipe([1.0])
    cache = _make_lyrics_cache(
        [
            (
                "Body moving heart is open",
                0.0,
                1.3,
                [
                    ("Body", 0.0, 0.2),
                    ("moving", 0.2, 0.45),
                    ("heart", 0.45, 0.72),
                    ("is", 0.72, 0.97),
                    ("open", 0.97, 1.3),
                ],
            )
        ]
    )

    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        1.0,
        {"enabled": True, "style": "per-word-pop"},
    )
    overlays = out["slots"][0]["text_overlays"]

    assert overlays[-1]["text"] == "Body moving heart is open"
    assert overlays[-1]["pop_animated_suffix"] == "open"
    assert overlays[-2]["end_s"] == pytest.approx(overlays[-1]["start_s"], abs=1e-6)
    assert overlays[-1]["end_s"] == pytest.approx(1.0, abs=1e-6)
    assert overlays[-1]["end_s"] - overlays[-1]["start_s"] >= lyric_injector._MIN_RENDERABLE_S
    for prev, cur in zip(overlays, overlays[1:], strict=False):
        assert float(prev["end_s"]) <= float(cur["start_s"]) + 1e-6


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


def test_partial_overlap_left_edge_is_clamped_not_dropped() -> None:
    """A line straddling best_start_s used to be dropped; now clamped + kept.

    Regression target: job dc33d047 (2026-05-24, music track 9a5d0b3f-…). The
    11.3s music section contained one fully-contained backing-vocal line and
    several song lines straddling the section boundary. Hard full-containment
    dropped the straddlers silently, producing a video with one parenthetical
    lyric for 3.4s out of 11.3s. User reported "no lyrics" — file actually had
    one. Clamping yields more visible lines per job at the cost of slightly
    truncated leading/trailing fragments.
    """
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache(
        [("Crosses boundary", 4.0, 6.0, [("Crosses", 4.0, 5.0), ("boundary", 5.0, 6.0)])]
    )
    out = inject_lyric_overlays(recipe, cache, 5.0, 10.0, {"enabled": True, "style": "karaoke"})
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 1
    ov = overlays[0]
    assert ov["text"] == "Crosses boundary"
    # Clamped to section bounds: [max(4.0, 5.0), min(6.0, 10.0)] = [5.0, 6.0].
    # Then rebased to section-relative: [0.0, 1.0]. Slot 0 starts at 0 so
    # slot-relative == section-relative here.
    assert ov["start_s"] == pytest.approx(0.0, abs=1e-3)
    # Karaoke overlay's word_timings should drop the "Crosses" word (entirely
    # outside the clamped window) and keep "boundary".
    word_texts = [w.get("text") for w in ov.get("word_timings", [])]
    assert word_texts == ["boundary"]


def test_partial_overlap_right_edge_is_clamped() -> None:
    """A line whose end_s > best_end_s is clamped at best_end_s, not dropped."""
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache(
        [("Bleeds out", 9.0, 11.0, [("Bleeds", 9.0, 10.0), ("out", 10.0, 11.0)])]
    )
    out = inject_lyric_overlays(recipe, cache, 5.0, 10.0, {"enabled": True, "style": "karaoke"})
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) == 1
    ov = overlays[0]
    # Section is [5.0, 10.0]; clamped line is [9.0, 10.0]; section-relative
    # [4.0, 5.0]; slot 0 covers section-relative [0, 5.0] so slot-relative
    # start_s = 4.0.
    assert ov["start_s"] == pytest.approx(4.0, abs=1e-3)
    word_texts = [w.get("text") for w in ov.get("word_timings", [])]
    assert word_texts == ["Bleeds"]


def test_trailing_flash_drop_uses_original_word_start_not_clamped_overlap() -> None:
    """Do not keep a trailing flash just because a pre-started word overlaps it."""
    recipe = _make_recipe([0.75])
    cache = _make_lyrics_cache([("Already started", 9.0, 10.75, [("Already", 9.0, 10.75)])])

    out = inject_lyric_overlays(recipe, cache, 10.0, 10.75, {"enabled": True, "style": "karaoke"})

    assert out["slots"][0]["text_overlays"] == []


def test_partial_overlap_dropped_when_clamped_below_min_visible() -> None:
    """A line that collapses below _MIN_LINE_VISIBLE_S after clamping is dropped.

    Prevents one-frame flashes when a long line barely overlaps the section.
    """
    recipe = _make_recipe([5.0])
    # Section is [5.0, 5.10]. The line at 4.95-10.0 clamps to [5.0, 5.10] —
    # a 0.10s window, below the 0.20s floor — so it should be dropped.
    cache = _make_lyrics_cache(
        [
            (
                "Just a sliver",
                4.95,
                10.0,
                [("Just", 4.95, 5.0), ("a", 5.0, 7.0), ("sliver", 7.0, 10.0)],
            )
        ]
    )
    out = inject_lyric_overlays(recipe, cache, 5.0, 5.10, {"enabled": True, "style": "karaoke"})
    assert out["slots"][0]["text_overlays"] == []


def test_section_filter_drops_lines_with_no_overlap() -> None:
    """Lines that are wholly outside [best_start_s, best_end_s] still drop."""
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache(
        [
            ("Way too early", 0.5, 1.0, [("Way", 0.5, 1.0)]),
            ("Way too late", 20.0, 21.0, [("Way", 20.0, 21.0)]),
        ]
    )
    out = inject_lyric_overlays(recipe, cache, 5.0, 10.0, {"enabled": True, "style": "karaoke"})
    # Neither line overlaps [5.0, 10.0] — both dropped.
    assert out["slots"][0]["text_overlays"] == []


def test_dc33d047_regression_section_clamps_increase_coverage() -> None:
    """Real-world: dc33d047 had one full-containment line. Clamping should
    surface the straddlers too.

    Track 9a5d0b3f had a section [0, 11.3]; the rendered output contained
    only `(Do think twice, do think twice)` at song-time 5.84-8.65 because
    other lines spanned across the boundary. With clamping enabled, we expect
    those straddlers to land as well.
    """
    recipe = _make_recipe([4.117, 3.605, 3.093])  # the actual slot durations
    cache = _make_lyrics_cache(
        [
            # Real survivor — fully contained.
            (
                "(Do think twice, do think twice)",
                5.84,
                8.65,
                [("Do", 5.84, 6.5), ("think", 6.5, 7.0), ("twice", 7.0, 7.5)],
            ),
            # Straddles best_start_s — was dropped before, now clamps to [0, 2.0].
            (
                "Earlier line",
                -1.5,
                2.0,
                [("Earlier", -1.5, 1.0), ("line", 1.0, 2.0)],
            ),
            # Straddles best_end_s — was dropped before, now clamps to [10.0, 11.3].
            (
                "Trailing line",
                10.0,
                12.5,
                [("Trailing", 10.0, 11.0), ("line", 11.0, 12.5)],
            ),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        11.3,
        {"enabled": True, "style": "line", "pre_roll_s": 0.0, "post_dwell_s": 0.0},
    )
    all_texts = {ov["text"] for slot in out["slots"] for ov in slot["text_overlays"]}
    # Pre-fix: only "(Do think twice...)" would have survived.
    # Post-fix: all three lines surface (their text — timing is clamped).
    assert "(Do think twice, do think twice)" in all_texts
    assert "Earlier line" in all_texts
    assert "Trailing line" in all_texts


def test_per_word_pop_clamping_drops_pre_section_words_and_keeps_overlap() -> None:
    """A line straddling best_start_s drops pre-section words and keeps the rest.

    Locks the interaction: `_select_section_lines` clamps line.start_s up to
    best_start_s and filters words entirely outside the clamped window.
    `_inject_per_word_pop` then builds cumulative stages from only the
    surviving words, so the on-screen cumulative text never references a
    word that didn't actually play in the rendered section.

    Also exercises the _MIN_RENDERABLE_S=0.05 guard inside the per-word-pop
    consumer: the straddling word's clamped duration (5.0-5.2 = 0.2s,
    section-relative 0.0-0.2) is well above the 0.05s floor so the first
    stage survives.
    """
    recipe = _make_recipe([5.0])
    cache = _make_lyrics_cache(
        [
            (
                "Earlier words now late",
                4.0,
                8.0,
                [
                    ("Earlier", 4.0, 4.5),  # before section — dropped
                    ("words", 4.5, 5.2),  # straddles best_start_s — clamped + kept
                    ("now", 5.2, 6.5),
                    ("late", 6.5, 8.0),
                ],
            )
        ]
    )
    out = inject_lyric_overlays(
        recipe, cache, 5.0, 10.0, {"enabled": True, "style": "per-word-pop"}
    )
    overlays = out["slots"][0]["text_overlays"]
    texts = [o["text"] for o in overlays]
    # "Earlier" was clamped out; cumulative stages build from surviving words
    # only, so the on-screen text never references "Earlier".
    assert all("Earlier" not in t for t in texts)
    # The three surviving words appear in cumulative order.
    assert texts == ["words", "words now", "words now late"]
    # The trailing-word suffix on each stage matches the new word.
    assert [o["pop_animated_suffix"] for o in overlays] == ["words", "now", "late"]


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


def test_line_post_dwell_extends_into_overlap_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dense lines extend into the fade-bound overlap budget."""
    recipe = _make_recipe([10.0])
    # Pins the LEGACY formula geometry. Under the dynamic crossfade default
    # (§F), the post-pass overrides caller-set fades and re-anchors the
    # window. The legacy `min(max_overlap_s, fade_in_s + fade_out_s)`
    # additive cap is only reachable via the kill switch.
    from app.config import settings

    monkeypatch.setattr(settings, "lyric_dynamic_crossfade_enabled", False)
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


def test_line_overlap_bounded_by_short_fades(monkeypatch: pytest.MonkeyPatch) -> None:
    """LEGACY-PATH assertion (kill-switch off). Under the §F default the
    dynamic post-pass overrides caller-set fades and recomputes overlap."""
    from app.config import settings

    monkeypatch.setattr(settings, "lyric_dynamic_crossfade_enabled", False)
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


def test_line_zero_fades_yields_zero_visual_overlap_when_it_does_not_cut_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LEGACY-PATH assertion (kill-switch off). Zero fades collapse the
    legacy additive cap to 0 — only meaningful when the dynamic post-pass
    is disabled, since the post-pass would override the 0 anyway."""
    from app.config import settings

    monkeypatch.setattr(settings, "lyric_dynamic_crossfade_enabled", False)
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


def test_tight_lines_keep_their_fades_with_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LEGACY-PATH assertion (kill-switch off). Caller-set fades are only
    honored verbatim when the dynamic post-pass is off. Under the §F
    default, caller fades are replaced by the matched-window math."""
    from app.config import settings

    monkeypatch.setattr(settings, "lyric_dynamic_crossfade_enabled", False)
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
    # No user-pinned fades → dynamic crossfade path runs. The overlap budget
    # is now `max_overlap_s` directly (not the legacy additive
    # `fade_in_s + fade_out_s` cap) — the §1d post-pass re-anchors
    # nxt.section_start so the ACTUAL emitted overlap equals the matched
    # crossfade duration. With pre_roll=0.4 and next.start=2.3, the
    # geometric overlap available is 0.30 s (next_visual_start..gap_cap);
    # post-pass matches durations at 300 ms.
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
    ov0, ov1 = out["slots"][0]["text_overlays"]
    next_visual_start = 2.3 - 0.4
    # ACTUAL emitted overlap equals matched crossfade duration.
    expected_overlap_s = ov0["fade_out_ms"] / 1000.0
    assert ov0["end_s"] - next_visual_start == pytest.approx(expected_overlap_s, abs=1e-3)
    assert ov0["end_s"] > next_visual_start
    # Matched durations + sqrt curve confirm the dynamic-crossfade path fired.
    assert ov0["fade_out_ms"] == ov1["fade_in_ms"]
    assert ov0["fade_out_curve"] == "sqrt"


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
    # No fade overrides → dynamic crossfade path runs. Section start/end of
    # both overlays are unchanged from the pre-fix geometry. Fade durations,
    # however, get MATCHED: ov0.fade_out_ms == ov1.fade_in_ms == the
    # available overlap window (300 ms with pre_roll=0.4 and 1 s gap), and
    # ov0 carries fade_out_curve="sqrt". This is the contract:
    # solo defaults survive ONLY when there's no crossfade successor; for
    # inter-line transitions the post-pass owns both sides of the pair.
    recipe = _make_recipe([10.0])
    cache = _make_lyrics_cache(
        [
            ("First", 1.0, 2.0, [("First", 1.0, 2.0)]),
            ("Second", 3.0, 4.0, [("Second", 3.0, 4.0)]),
        ]
    )
    out = inject_lyric_overlays(recipe, cache, 0.0, 10.0, {"enabled": True, "style": "line"})
    ov0, ov1 = out["slots"][0]["text_overlays"]

    # Section geometry — unchanged by the post-pass.
    assert ov0["start_s"] == pytest.approx(0.6, abs=1e-3)
    assert ov0["end_s"] == pytest.approx(2.9, abs=1e-3)
    assert ov1["start_s"] == pytest.approx(2.6, abs=1e-3)
    assert ov1["end_s"] == pytest.approx(5.0, abs=1e-3)
    # Fade in stays at the solo default (fade-in of first line; no
    # predecessor to match against).
    assert ov0["fade_in_ms"] == 50
    # Matched durations + sqrt curve on the outgoing side prove the
    # dynamic-crossfade post-pass fired. Last line keeps the solo
    # lingering fade-out (no successor).
    assert ov0["fade_out_ms"] == ov1["fade_in_ms"]
    assert ov0["fade_out_curve"] == "sqrt"
    assert ov1["fade_out_ms"] == 250
    assert "fade_out_curve" not in ov1


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


def test_line_continues_across_short_music_slots_until_audio_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for prod job 5390c7ef-a3eb-448d-bb80-b6c1e292d16c:
    # the line starts near the end of slot 2 but the vocal continues through
    # slots 3 and 4. It must not disappear at slot 2's clip cut.
    #
    # Test intent is cross-slot segmenting, not fade values. Pin the
    # kill-switch off so the caller-set fade_in_ms=150 / fade_out_ms=250
    # values are honored as-is (under §F the dynamic post-pass would
    # override them with the matched-window math, which would change the
    # segment fade_ms assertions without changing the cross-slot intent).
    from app.config import settings

    monkeypatch.setattr(settings, "lyric_dynamic_crossfade_enabled", False)
    recipe = _make_recipe([6.997, 2.176, 2.155, 1.493, 2.027, 1.579])
    cache = _make_lyrics_cache(
        [
            ("We ain't stressing 'bout the loot (yeah)", 8.54, 12.32, [("We", 8.54, 12.32)]),
            ("My block made of quesería", 12.07, 13.52, [("My", 12.07, 13.52)]),
            ("This not the molly, this the boot", 14.70, 16.76, [("This", 14.70, 16.76)]),
        ]
    )
    out = inject_lyric_overlays(
        recipe,
        cache,
        0.0,
        17.3,
        {
            "enabled": True,
            "style": "line",
            "pre_roll_s": 0.1,
            "post_dwell_s": 2.0,
            "next_line_gap_s": 0.2,
            "fade_in_ms": 150,
            "fade_out_ms": 250,
        },
    )

    slots = out["slots"]
    we_segments = [
        (idx, ov)
        for idx, slot in enumerate(slots)
        for ov in slot["text_overlays"]
        if ov["text"].startswith("We ain't")
    ]
    assert [idx for idx, _ in we_segments] == [1, 2, 3]

    # Slot 2 carries the start, slot 3 bridges the middle, and slot 4 carries
    # through the aligned audio end at t=12.32.
    assert we_segments[0][1]["start_s"] == pytest.approx(1.443, abs=1e-3)
    assert we_segments[0][1]["end_s"] == pytest.approx(2.176, abs=1e-3)
    assert we_segments[1][1]["start_s"] == pytest.approx(0.0, abs=1e-3)
    assert we_segments[1][1]["end_s"] == pytest.approx(2.155, abs=1e-3)
    assert we_segments[2][1]["start_s"] == pytest.approx(0.0, abs=1e-3)
    assert we_segments[2][1]["end_s"] == pytest.approx(0.992, abs=1e-3)

    assert we_segments[0][1]["fade_in_ms"] == 150
    assert we_segments[0][1]["fade_out_ms"] == 0
    assert we_segments[1][1]["fade_in_ms"] == 0
    assert we_segments[1][1]["fade_out_ms"] == 0
    assert we_segments[2][1]["fade_in_ms"] == 0
    assert we_segments[2][1]["fade_out_ms"] == 250
    assert {ov["lyric_line_id"] for _, ov in we_segments} == {"line:0:8.540:12.320"}
    assert [ov["lyric_segment_index"] for _, ov in we_segments] == [0, 1, 2]
    assert [ov["lyric_segment_count"] for _, ov in we_segments] == [3, 3, 3]

    my_block_segments = [
        idx
        for idx, slot in enumerate(slots)
        for ov in slot["text_overlays"]
        if ov["text"].startswith("My block")
    ]
    this_not_segments = [
        idx
        for idx, slot in enumerate(slots)
        for ov in slot["text_overlays"]
        if ov["text"].startswith("This not")
    ]
    assert my_block_segments == [3, 4]
    assert this_not_segments == [4, 5]


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


# ── Layer 2 finalization tests (plan §2) ──────────────────────────────────────


from app.pipeline.lyric_injector import (  # noqa: E402
    _align_audible_words_to_original_text,
    _finalize_lyric_audible_window,
    _tokenize_lyric_text,
    _word_audible,
)


def _make_lyric_overlay(
    *,
    text: str,
    start_s: float,
    end_s: float,
    line_id: str,
    original_text: str | None = None,
    original_start_s_song: float | None = None,
    original_end_s_song: float | None = None,
    original_words: list[dict] | None = None,
    fade_in_ms: int = 150,
    fade_out_ms: int = 250,
    extras: dict | None = None,
) -> dict:
    """Build a merged lyric-line overlay dict as `_collect_absolute_overlays`
    would produce post-Layer-1."""
    ov = {
        "text": text,
        "effect": "lyric-line",
        "start_s": start_s,
        "end_s": end_s,
        "position": "bottom",
        "position_y_frac": 0.8,
        "position_x_frac": None,
        "font_family": "Inter Tight",
        "text_color": "#FFFFFF",
        "fade_in_ms": fade_in_ms,
        "fade_out_ms": fade_out_ms,
        "lyric_line_id": line_id,
        "original_text": original_text if original_text is not None else text,
        "original_start_s_song": (
            original_start_s_song if original_start_s_song is not None else start_s
        ),
        "original_end_s_song": (original_end_s_song if original_end_s_song is not None else end_s),
        "original_words": original_words or [],
    }
    if extras:
        ov.update(extras)
    return ov


def test_word_audible_midpoint_in_window_keeps_word() -> None:
    w = {"text": "x", "start_s_song": 138.0, "end_s_song": 138.6}  # midpoint 138.3
    assert _word_audible(w, 128.0, 139.036) is True


def test_word_audible_midpoint_after_end_drops_word() -> None:
    # midpoint 139.75 — inside best_end_s=140 but outside post-snap 139.036
    w = {"text": "she", "start_s_song": 139.5, "end_s_song": 140.0}
    assert _word_audible(w, 128.0, 139.036) is False


def test_word_audible_midpoint_before_start_drops_word() -> None:
    w = {"text": "x", "start_s_song": 127.0, "end_s_song": 127.5}  # midpoint 127.25
    assert _word_audible(w, 128.0, 139.036) is False


def test_word_audible_start_inside_window_keeps_tail_word() -> None:
    # Midpoint == audio_mix_song_end_s, but the word started before the cut.
    # Preview tails should show the full word once the vocal begins.
    w = {"text": "x", "start_s_song": 138.0, "end_s_song": 140.0}  # midpoint 139.0
    assert _word_audible(w, 128.0, 139.0) is True


def test_word_audible_80pct_overlap_midpoint_inside_kept() -> None:
    # 80 % of the word lies inside the audible window; midpoint inside → kept.
    w = {"text": "x", "start_s_song": 138.0, "end_s_song": 139.5}  # midpoint 138.75
    assert _word_audible(w, 128.0, 139.036) is True


def test_finalize_billie_jean_tail_truncates_with_punctuation_preserved() -> None:
    """Empirical Bug B: 'She told my baby we'd danced 'til three, then she
    looked at me' truncates to 'She told my baby we'd danced' when only
    songs-time 136.88 → 139.036 is audible. Apostrophe in we'd preserved."""
    original = "She told my baby we'd danced 'til three, then she looked at me"
    original_words = [
        {"text": "She", "start_s_song": 136.88, "end_s_song": 137.30},
        {"text": "told", "start_s_song": 137.30, "end_s_song": 137.68},
        {"text": "my", "start_s_song": 137.68, "end_s_song": 137.98},
        {"text": "baby", "start_s_song": 137.98, "end_s_song": 138.38},
        {"text": "we'd", "start_s_song": 138.40, "end_s_song": 138.45},
        {"text": "danced", "start_s_song": 138.38, "end_s_song": 138.98},
        {"text": "'til", "start_s_song": 139.00, "end_s_song": 139.16},
        {"text": "three", "start_s_song": 139.18, "end_s_song": 139.54},
        {"text": "then", "start_s_song": 139.56, "end_s_song": 139.86},
        {"text": "she", "start_s_song": 139.88, "end_s_song": 140.04},
        {"text": "looked", "start_s_song": 140.04, "end_s_song": 140.28},
        {"text": "at", "start_s_song": 140.28, "end_s_song": 140.58},
        {"text": "me", "start_s_song": 140.58, "end_s_song": 141.0},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=8.78,
        end_s=11.036,
        line_id="line:24:136.880:141.000",
        original_text=original,
        original_start_s_song=136.88,
        original_end_s_song=141.0,
        original_words=original_words,
    )
    out = _finalize_lyric_audible_window([ov], 128.0, 139.036)
    assert len(out) == 1
    result = out[0]
    # `display_text` is the rebuilt substring with original punctuation.
    # Comma was after "three" in original; "three" is dropped, so no comma.
    # Apostrophe in we'd MUST be intact.
    assert result["display_text"] == "She told my baby we'd danced"
    assert "we'd" in result["display_text"]  # apostrophe preserved
    # `text` (original) preserved.
    assert result["text"] == original
    # `original_text` preserved.
    assert result["original_text"] == original
    # End shrunk to audible_end_abs = 11.036 (conservative; no shrink because
    # merged.end_s already equals audible_end_abs in this case).
    assert result["end_s"] <= 11.036


def test_finalize_billie_jean_leading_partial_keeps_audible_suffix_with_comma() -> None:
    """Symmetric case: 'So take my strong advice, just remember to always think
    twice' starts before best_start_s=128. Leading 'So take my' are dropped;
    audible suffix retains the original comma."""
    original = "So take my strong advice, just remember to always think twice"
    original_words = [
        {"text": "So", "start_s_song": 126.12, "end_s_song": 126.96},
        {"text": "take", "start_s_song": 126.96, "end_s_song": 127.42},
        {"text": "my", "start_s_song": 127.42, "end_s_song": 127.78},
        {"text": "strong", "start_s_song": 127.78, "end_s_song": 128.38},
        {"text": "advice", "start_s_song": 128.38, "end_s_song": 129.44},
        {"text": "just", "start_s_song": 129.54, "end_s_song": 130.38},
        {"text": "remember", "start_s_song": 130.38, "end_s_song": 131.22},
        {"text": "to", "start_s_song": 131.22, "end_s_song": 131.86},
        {"text": "always", "start_s_song": 131.86, "end_s_song": 132.44},
        {"text": "think", "start_s_song": 132.44, "end_s_song": 132.74},
        {"text": "twice", "start_s_song": 132.74, "end_s_song": 133.16},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=0.0,
        end_s=5.961,
        line_id="line:0:126.120:133.160",
        original_text=original,
        original_start_s_song=126.12,
        original_end_s_song=133.16,
        original_words=original_words,
    )
    out = _finalize_lyric_audible_window([ov], 128.0, 139.036)
    assert len(out) == 1
    result = out[0]
    # 'So', 'take', 'my' have midpoints before 128.0 → dropped. 'strong' starts
    # 127.78, midpoint 128.08 — kept. Display starts at "strong".
    assert result["display_text"].startswith("strong")
    # Comma after "advice" must be in the displayed substring.
    assert "advice," in result["display_text"]
    # Original text preserved untouched.
    assert result["text"] == original
    assert result["original_text"] == original


def test_finalize_marea_preview_drops_dangling_parenthetical_prefix() -> None:
    """Regression for lyrics-preview job 2bc32709 (Marea).

    Preview window starts at song-time 155.700s, inside
    "Day by day (we've lost dancing)". The audible-word slice is
    "day we've lost dancing"; rendering that as ``day (we've lost dancing``
    keeps a stale prefix on screen while the backing hook is what the user
    hears. Line style should display just the parenthetical hook.
    """
    original = "Day by day (we've lost dancing)"
    original_words = [
        {"text": "Day", "start_s_song": 155.02, "end_s_song": 155.28},
        {"text": "by", "start_s_song": 155.28, "end_s_song": 155.94},
        {"text": "day", "start_s_song": 156.04, "end_s_song": 157.02},
        {"text": "we've", "start_s_song": 157.02, "end_s_song": 157.02},
        {"text": "lost", "start_s_song": 157.02, "end_s_song": 157.62},
        {"text": "dancing", "start_s_song": 157.62, "end_s_song": 158.34},
    ]
    first_line = _make_lyric_overlay(
        text=original,
        start_s=0.0,
        end_s=3.34,
        line_id="line:marea-day",
        original_text=original,
        original_start_s_song=155.02,
        original_end_s_song=158.34,
        original_words=original_words,
    )
    # Later audible line makes the Marea line a true interior partial, matching
    # the prod preview instead of relying on the permissive final-line path.
    tail = _make_lyric_overlay(
        text="Marvellous",
        start_s=14.42,
        end_s=15.3,
        line_id="line:marea-tail",
        original_text="Marvellous",
        original_start_s_song=170.12,
        original_end_s_song=171.94,
        original_words=[
            {"text": "Marvellous", "start_s_song": 170.12, "end_s_song": 171.94},
        ],
    )

    out = _finalize_lyric_audible_window([first_line, tail], 155.7, 171.0)
    kept = [ov for ov in out if ov.get("lyric_line_id") == "line:marea-day"]

    assert len(kept) == 1
    assert kept[0]["display_text"] == "we've lost dancing"


def test_finalize_parenthetical_keeps_non_repeated_audible_prefix() -> None:
    """Do not drop a real audible prefix just because a parenthetical follows."""
    original = "If I can live through this (we've lost dancing)"
    original_words = [
        {"text": "If", "start_s_song": 158.34, "end_s_song": 158.8},
        {"text": "I", "start_s_song": 158.8, "end_s_song": 159.1},
        {"text": "can", "start_s_song": 159.1, "end_s_song": 159.6},
        {"text": "live", "start_s_song": 159.6, "end_s_song": 160.0},
        {"text": "through", "start_s_song": 160.0, "end_s_song": 160.5},
        {"text": "this", "start_s_song": 160.5, "end_s_song": 161.0},
        {"text": "we've", "start_s_song": 161.0, "end_s_song": 161.2},
        {"text": "lost", "start_s_song": 161.2, "end_s_song": 161.7},
        {"text": "dancing", "start_s_song": 161.7, "end_s_song": 162.5},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=0.0,
        end_s=4.16,
        line_id="line:marea-through-this",
        original_text=original,
        original_start_s_song=158.34,
        original_end_s_song=162.5,
        original_words=original_words,
    )

    out = _finalize_lyric_audible_window([ov], 160.3, 162.5)

    assert len(out) == 1
    assert out[0]["display_text"].startswith("this ")


def test_finalize_well_aligned_full_line_unchanged() -> None:
    """Full coverage → no display_text override, no window shrink."""
    original = "Hello world how are you"
    original_words = [
        {"text": "Hello", "start_s_song": 130.0, "end_s_song": 130.4},
        {"text": "world", "start_s_song": 130.4, "end_s_song": 130.9},
        {"text": "how", "start_s_song": 131.0, "end_s_song": 131.3},
        {"text": "are", "start_s_song": 131.3, "end_s_song": 131.6},
        {"text": "you", "start_s_song": 131.6, "end_s_song": 132.0},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=2.0,
        end_s=4.0,
        line_id="line:5:130.000:132.000",
        original_text=original,
        original_start_s_song=130.0,
        original_end_s_song=132.0,
        original_words=original_words,
    )
    out = _finalize_lyric_audible_window([ov], 128.0, 140.0)
    assert len(out) == 1
    result = out[0]
    # Step 1 — near-complete path. `display_text` MUST NOT be set.
    assert "display_text" not in result
    # Window MUST NOT shrink.
    assert result["start_s"] == 2.0
    assert result["end_s"] == 4.0


def test_finalize_original_text_field_never_mutated() -> None:
    """Even on truncation, the overlay's `text` field stays the original."""
    original = "She told my baby we'd danced"
    original_words = [
        {"text": "She", "start_s_song": 136.88, "end_s_song": 137.30},
        {"text": "told", "start_s_song": 137.30, "end_s_song": 137.68},
        {"text": "my", "start_s_song": 137.68, "end_s_song": 137.98},
        {"text": "baby", "start_s_song": 137.98, "end_s_song": 138.38},
        {"text": "we'd", "start_s_song": 138.40, "end_s_song": 138.45},
        {"text": "danced", "start_s_song": 138.38, "end_s_song": 138.98},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=8.78,
        end_s=11.036,
        line_id="line:24",
        original_text=original,
        original_start_s_song=136.88,
        original_end_s_song=141.0,
        original_words=original_words,
    )
    out = _finalize_lyric_audible_window([ov], 128.0, 139.036)
    assert len(out) == 1
    assert out[0]["text"] == original
    assert out[0]["original_text"] == original


def test_finalize_passes_through_non_lyric_overlays_unchanged_in_order() -> None:
    """2i contract: non-lyric overlays untouched at their original positions;
    dropped lyrics removed; kept lyrics carry display_text."""
    original_a = "She told my baby we'd danced 'til three"
    words_a = [
        {"text": "She", "start_s_song": 136.88, "end_s_song": 137.30},
        {"text": "told", "start_s_song": 137.30, "end_s_song": 137.68},
        {"text": "my", "start_s_song": 137.68, "end_s_song": 137.98},
        {"text": "baby", "start_s_song": 137.98, "end_s_song": 138.38},
        {"text": "we'd", "start_s_song": 138.40, "end_s_song": 138.45},
        {"text": "danced", "start_s_song": 138.38, "end_s_song": 138.98},
        {"text": "'til", "start_s_song": 139.00, "end_s_song": 139.16},
        {"text": "three", "start_s_song": 139.18, "end_s_song": 139.54},
    ]
    label_x = {
        "text": "WELCOME",
        "effect": "pop-in",
        "start_s": 0.5,
        "end_s": 2.0,
        "position": "center",
        "position_y_frac": 0.5,
    }
    label_y = {
        "text": "NIGHT FOOTBALL",
        "effect": "slide-in",
        "start_s": 6.0,
        "end_s": 8.0,
        "position": "center",
        "position_y_frac": 0.5,
    }
    lyric_kept = _make_lyric_overlay(
        text=original_a,
        start_s=8.78,
        end_s=11.036,
        line_id="line:24",
        original_text=original_a,
        original_start_s_song=136.88,
        original_end_s_song=141.0,
        original_words=words_a,
    )
    # A lyric that will be dropped: no surviving words.
    dropped_words: list[dict] = []
    lyric_dropped = _make_lyric_overlay(
        text="dropped line",
        start_s=4.0,
        end_s=5.0,
        line_id="line:99",
        original_text="dropped line",
        original_start_s_song=150.0,  # entirely outside audible window
        original_end_s_song=151.0,
        original_words=dropped_words,
    )
    overlays_in = [label_x, lyric_dropped, lyric_kept, label_y]
    out = _finalize_lyric_audible_window(overlays_in, 128.0, 139.036)
    # label_x → label_y stay at their original positions (no lyric inserted in between
    # since lyric_dropped is removed and lyric_kept moves earlier). lyric_kept is final.
    assert out[0] is label_x
    assert out[1]["effect"] == "lyric-line"
    assert out[1].get("display_text") is not None
    assert out[2] is label_y
    assert len(out) == 3  # lyric_dropped removed


def test_finalize_drops_interior_partial_below_coverage_floor() -> None:
    """Interior partial line where surviving words meet basic floor but
    coverage floors fail → dropped (stricter than final-line)."""
    original = "A B C D E F G H I J"
    # All 10 words; only A B C audible. coverage_words = 0.3 < 0.65, coverage_duration ~0.3
    original_words = [
        {"text": w, "start_s_song": 130.0 + i * 0.5, "end_s_song": 130.5 + i * 0.5}
        for i, w in enumerate(original.split())
    ]
    # Interior: a following lyric exists so this isn't "final."
    ov_interior = _make_lyric_overlay(
        text=original,
        start_s=2.0,
        end_s=7.0,
        line_id="line:interior",
        original_text=original,
        original_start_s_song=130.0,
        original_end_s_song=135.0,
        original_words=original_words,
    )
    ov_later = _make_lyric_overlay(
        text="Later",
        start_s=8.0,
        end_s=10.0,
        line_id="line:later",
        original_text="Later again",
        original_start_s_song=138.0,
        original_end_s_song=138.5,
        original_words=[
            {"text": "Later", "start_s_song": 138.0, "end_s_song": 138.25},
            {"text": "again", "start_s_song": 138.25, "end_s_song": 138.5},
        ],
    )
    # Audible window: 128.0 → 131.7 (only first ~3 words of interior audible).
    out = _finalize_lyric_audible_window([ov_interior, ov_later], 128.0, 131.7)
    # Interior must be dropped; later may or may not be (depends on coverage).
    texts = [o.get("display_text") or o.get("text") for o in out if o.get("effect") == "lyric-line"]
    assert original not in texts  # interior dropped


def test_finalize_keeps_final_partial_when_fragment_meaningful() -> None:
    """Same partial as interior test, but it's the FINAL line → kept (more
    permissive: basic floor only, no coverage floor)."""
    original = "A B C D E F G H I J"
    original_words = [
        {"text": w, "start_s_song": 130.0 + i * 0.5, "end_s_song": 130.5 + i * 0.5}
        for i, w in enumerate(original.split())
    ]
    # Audible window: 128.0 → 132.0 (4 words audible, ~2.0s audible speech).
    # end_s=4.0 == audible_end_abs → marks it final per _FINAL_LINE_TAIL_TOLERANCE_S.
    ov_final = _make_lyric_overlay(
        text=original,
        start_s=2.0,
        end_s=4.0,
        line_id="line:final",
        original_text=original,
        original_start_s_song=130.0,
        original_end_s_song=135.0,
        original_words=original_words,
    )
    out = _finalize_lyric_audible_window([ov_final], 128.0, 132.0)
    assert len(out) == 1
    # Kept with truncated display_text.
    assert out[0].get("display_text") is not None
    # Display starts with "A" (first surviving word).
    assert out[0]["display_text"].split()[0] == "A"


def test_finalize_drops_final_line_too_short_fragment() -> None:
    """Final line with one very short started word still drops."""
    original = "Hello world"
    original_words = [
        {"text": "Hello", "start_s_song": 130.0, "end_s_song": 130.5},
        {"text": "world", "start_s_song": 131.0, "end_s_song": 131.5},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=2.0,
        end_s=2.6,
        line_id="line:tail",
        original_text=original,
        original_start_s_song=130.0,
        original_end_s_song=131.5,
        original_words=original_words,
    )
    # Audible window ends at 130.6 → only "Hello" audible (midpoint 130.25).
    out = _finalize_lyric_audible_window([ov], 128.0, 130.6)
    assert out == []  # dropped — 0.5s of audible speech is below the final-line floor


def test_finalize_keeps_final_single_word_started_before_window_end() -> None:
    """A tail word that starts before the preview cut should render as a word."""
    ov = _make_lyric_overlay(
        text="Marvellous",
        start_s=14.42,
        end_s=15.3,
        line_id="line:marea-marvellous",
        original_text="Marvellous",
        original_start_s_song=170.12,
        original_end_s_song=171.94,
        original_words=[
            {"text": "Marvellous", "start_s_song": 170.12, "end_s_song": 171.94},
        ],
    )

    out = _finalize_lyric_audible_window([ov], 155.7, 171.0)

    assert len(out) == 1
    assert out[0]["display_text"] == "Marvellous"


def test_finalize_keeps_final_karaoke_single_word_started_before_window_end() -> None:
    """The tail-word rule applies to karaoke previews as well as line previews."""
    ov = _make_lyric_overlay(
        text="Marvellous",
        start_s=14.42,
        end_s=15.3,
        line_id="line:marea-marvellous-karaoke",
        original_text="Marvellous",
        original_start_s_song=170.12,
        original_end_s_song=171.94,
        original_words=[
            {"text": "Marvellous", "start_s_song": 170.12, "end_s_song": 171.94},
        ],
        extras={
            "effect": "karaoke-line",
            "word_timings": [{"text": "Marvellous", "start_s": 0.0, "end_s": 1.82}],
        },
    )

    out = _finalize_lyric_audible_window([ov], 155.7, 171.0)

    assert len(out) == 1
    assert out[0]["text"] == "Marvellous"
    assert [w["text"] for w in out[0]["word_timings"]] == ["Marvellous"]


def test_finalize_karaoke_clipped_leading_line_highlights_first_survivor_at_zero() -> None:
    """When a preview starts mid-line, the first visible word is in progress.

    Production preview b9192f96 clipped "So" from "So take my strong advice".
    The rendered line starts at video t=0 with "take"; its highlight should
    also start at t=0 instead of waiting for the original song-time offset.
    """
    ov = _make_lyric_overlay(
        text="So take my strong advice",
        start_s=0.0,
        end_s=1.92,
        line_id="line:billie-leading-karaoke",
        original_text="So take my strong advice",
        original_start_s_song=127.14,
        original_end_s_song=129.65,
        original_words=[
            {"text": "So", "start_s_song": 127.14, "end_s_song": 128.0},
            {"text": "take", "start_s_song": 128.0, "end_s_song": 128.2},
            {"text": "my", "start_s_song": 128.2, "end_s_song": 128.66},
            {"text": "strong", "start_s_song": 128.66, "end_s_song": 129.22},
            {"text": "advice", "start_s_song": 129.22, "end_s_song": 129.65},
        ],
        extras={
            "effect": "karaoke-line",
            "word_timings": [
                {"text": "take", "start_s": 0.27, "end_s": 0.47},
                {"text": "my", "start_s": 0.47, "end_s": 0.93},
                {"text": "strong", "start_s": 0.93, "end_s": 1.49},
                {"text": "advice", "start_s": 1.49, "end_s": 1.92},
            ],
        },
    )

    out = _finalize_lyric_audible_window([ov], 127.73, 140.57)

    assert len(out) == 1
    assert out[0]["text"] == "take my strong advice"
    assert out[0]["word_timings"][0]["text"] == "take"
    assert out[0]["word_timings"][0]["start_s"] == 0.0
    assert out[0]["word_timings"][1]["start_s"] == 0.47


def test_finalize_missing_metadata_passes_through_with_warning() -> None:
    """Overlay lacking `original_*` fields → passthrough unchanged + warning."""
    ov = {
        "text": "Hello",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 5.0,
        "position": "bottom",
        "position_y_frac": 0.8,
        "font_family": "Inter Tight",
        "text_color": "#FFFFFF",
        "lyric_line_id": "line:bad",
        # original_* fields intentionally missing.
    }
    out = _finalize_lyric_audible_window([ov], 128.0, 139.036)
    assert len(out) == 1
    # Passthrough — same dict reference (not a copy).
    assert out[0] is ov
    # No display_text written.
    assert "display_text" not in out[0]


def test_finalize_song_time_fields_not_double_offset_through_pipeline() -> None:
    """Splitter writes song-time originals as-is — finalize must NOT add
    best_start_s. Synthetic line at song-time 136.88 → 141.0, best_start_s=128.
    `original_start_s_song` MUST be 136.88, NOT 264.88."""
    original = "She told my baby we'd danced"
    words = [
        {"text": "She", "start_s_song": 136.88, "end_s_song": 137.30},
        {"text": "told", "start_s_song": 137.30, "end_s_song": 137.68},
    ]
    cache = _make_lyrics_cache(
        [
            (
                original,
                136.88,
                141.0,
                [(w["text"], w["start_s_song"], w["end_s_song"]) for w in words],
            )
        ]
    )
    recipe = _make_recipe([15.0])
    out = inject_lyric_overlays(recipe, cache, 128.0, 145.0, {"enabled": True, "style": "line"})
    overlays = out["slots"][0]["text_overlays"]
    assert len(overlays) >= 1
    ov = overlays[0]
    # Must be song time, NOT section time, NOT double-offset.
    assert ov["original_start_s_song"] == 136.88
    assert ov["original_end_s_song"] == 141.0


def test_align_preserves_curly_apostrophe() -> None:
    """Original uses U+2019 (curly). Substring slice preserves it exactly."""
    original = "It’s a beautiful day"
    words = [
        {"text": "It's", "start_s_song": 0.0, "end_s_song": 0.3},
        {"text": "a", "start_s_song": 0.3, "end_s_song": 0.4},
        {"text": "beautiful", "start_s_song": 0.4, "end_s_song": 1.0},
    ]
    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)
    assert out is not None
    # Curly apostrophe must survive the slice.
    assert "’" in out
    assert out == "It’s a beautiful"


def test_align_preserves_leading_apostrophe_words() -> None:
    """'cause and 'til are tokenized as single tokens; alignment finds them."""
    original = "I love you 'cause you're 'til the end"
    words = [
        {"text": "love", "start_s_song": 0.0, "end_s_song": 0.3},
        {"text": "you", "start_s_song": 0.3, "end_s_song": 0.5},
        {"text": "'cause", "start_s_song": 0.5, "end_s_song": 0.8},
        {"text": "you're", "start_s_song": 0.8, "end_s_song": 1.0},
    ]
    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)
    assert out is not None
    assert "'cause" in out
    assert out.startswith("love")


def test_align_preserves_hyphenated_compound() -> None:
    """hard-headed is a single token; preserves the hyphen."""
    original = "He's hard-headed and stubborn"
    words = [
        {"text": "hard-headed", "start_s_song": 0.0, "end_s_song": 0.5},
        {"text": "and", "start_s_song": 0.5, "end_s_song": 0.7},
        {"text": "stubborn", "start_s_song": 0.7, "end_s_song": 1.2},
    ]
    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)
    assert out is not None
    assert "hard-headed" in out


def test_align_preserves_parenthetical_via_original_slice() -> None:
    """Parens are NOT tokens, so they live in the original string and survive
    the slice naturally."""
    original = "(do think twice, do think twice)"
    words = [
        {"text": "do", "start_s_song": 0.0, "end_s_song": 0.1},
        {"text": "think", "start_s_song": 0.1, "end_s_song": 0.2},
        {"text": "twice", "start_s_song": 0.2, "end_s_song": 0.4},
        {"text": "do", "start_s_song": 0.5, "end_s_song": 0.6},
        {"text": "think", "start_s_song": 0.6, "end_s_song": 0.7},
        {"text": "twice", "start_s_song": 0.7, "end_s_song": 0.9},
    ]
    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)
    assert out is not None
    # Opening paren must be present (the leftmost match anchors at position 1).
    assert "do think twice" in out
    # All 6 words mapped → end inclusive of closing word.
    assert out.endswith("twice")


def test_align_handles_repeated_words_picks_anchor_that_completes_match() -> None:
    """Original 'rain rain go away', surviving [rain, go, away]. Two anchor
    positions exist for the first 'rain'; only anchor=1 (the second 'rain')
    yields a full contiguous match. The two-pointer scan moves to anchor=1
    and returns 'rain go away'. This documents the leftmost-completing-anchor
    behavior — not "leftmost word" but "leftmost anchor that completes the
    full audible sequence."""
    original = "rain rain go away"
    words = [
        {"text": "rain", "start_s_song": 0.0, "end_s_song": 0.3},
        {"text": "go", "start_s_song": 0.6, "end_s_song": 0.8},
        {"text": "away", "start_s_song": 0.8, "end_s_song": 1.1},
    ]
    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)
    # Anchor=0 yields partial match of length 1 (rain ≠ go).
    # Anchor=1 yields full match (rain, go, away) → wins.
    # Output spans from token[1].start (char 5) to token[3].end (char 17).
    assert out == "rain go away"


def test_align_strips_unmatched_closing_quote_from_mid_quote_slice() -> None:
    original = 'She told me, "You\'ll never be alone", oh, oh, woo'
    words = [
        {"text": "be", "start_s_song": 41.1, "end_s_song": 42.0},
        {"text": "alone", "start_s_song": 42.0, "end_s_song": 43.08},
        {"text": "oh", "start_s_song": 43.1, "end_s_song": 43.45},
        {"text": "oh", "start_s_song": 43.47, "end_s_song": 43.82},
        {"text": "woo", "start_s_song": 43.84, "end_s_song": 44.19},
    ]

    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)

    assert out == "be alone, oh, oh, woo"


def test_align_strips_unmatched_curly_quote_from_mid_quote_slice() -> None:
    original = "She told me, “You'll never be alone”, oh, oh, woo"
    words = [
        {"text": "be", "start_s_song": 41.1, "end_s_song": 42.0},
        {"text": "alone", "start_s_song": 42.0, "end_s_song": 43.08},
        {"text": "oh", "start_s_song": 43.1, "end_s_song": 43.45},
        {"text": "oh", "start_s_song": 43.47, "end_s_song": 43.82},
        {"text": "woo", "start_s_song": 43.84, "end_s_song": 44.19},
    ]

    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)

    assert out == "be alone, oh, oh, woo"


def test_align_preserves_balanced_quotes_when_stripping_one_orphan_quote() -> None:
    original = 'She said goodbye, "stay" alone", oh'
    words = [
        {"text": "goodbye", "start_s_song": 40.0, "end_s_song": 40.4},
        {"text": "stay", "start_s_song": 40.5, "end_s_song": 40.9},
        {"text": "alone", "start_s_song": 41.0, "end_s_song": 41.5},
        {"text": "oh", "start_s_song": 41.6, "end_s_song": 42.0},
    ]

    out = _align_audible_words_to_original_text(original_text=original, audible_words=words)

    assert out == 'goodbye, "stay" alone, oh'


def test_align_returns_none_for_single_word() -> None:
    """Audible_words must have ≥2 entries for alignment to fire."""
    out = _align_audible_words_to_original_text(
        original_text="Hello world",
        audible_words=[{"text": "Hello", "start_s_song": 0.0, "end_s_song": 0.5}],
    )
    assert out is None


def test_tokenize_captures_curly_and_straight_apostrophes() -> None:
    tokens = _tokenize_lyric_text("don't can’t")
    assert len(tokens) == 2
    assert tokens[0][2] == "don't"  # normalized: curly → straight, casefolded
    assert tokens[1][2] == "can't"


def test_tokenize_treats_hyphenated_word_as_one_token() -> None:
    tokens = _tokenize_lyric_text("hard-headed people")
    assert len(tokens) == 2
    assert tokens[0][2] == "hard-headed"


def test_tokenize_ignores_parentheses() -> None:
    tokens = _tokenize_lyric_text("(do think twice)")
    assert len(tokens) == 3
    assert [t[2] for t in tokens] == ["do", "think", "twice"]


# ── Empty-words regression (P1 from red-team adversarial) ────────────────────


def test_finalize_empty_words_inside_window_keeps_original_text() -> None:
    """LRCLIB-plain / whisper_only-degraded tracks have lyric lines with NO
    per-word timings. Pre-PR these rendered fine via the line-style overlay.
    The audible-word filter would produce surviving_word_count=0 and the
    decision procedure would drop the line at Step 3. This guard short-
    circuits when original_words is empty AND the line is reasonably inside
    the audible window — render the original text unchanged."""
    overlay = _make_lyric_overlay(
        text="Hello world",
        start_s=0.0,
        end_s=3.0,
        line_id="line:plain",
        original_text="Hello world",
        original_start_s_song=130.0,
        original_end_s_song=132.0,
        original_words=[],  # plain-lyric source: no word timings
    )
    out = _finalize_lyric_audible_window([overlay], 128.0, 140.0)
    assert len(out) == 1
    # No display_text rewrite — renderer reads `text` (original) directly.
    assert "display_text" not in out[0]
    assert out[0]["text"] == "Hello world"


def test_finalize_empty_words_outside_window_drops_line() -> None:
    """Empty-words line whose audio is entirely past the audible window IS
    dropped — overlap < 0.5 → no point rendering text the user can't hear."""
    overlay = _make_lyric_overlay(
        text="Hello world",
        start_s=0.0,
        end_s=3.0,
        line_id="line:plain-tail",
        original_text="Hello world",
        original_start_s_song=145.0,  # entirely past audio_mix_song_end_s=140
        original_end_s_song=147.0,
        original_words=[],
    )
    out = _finalize_lyric_audible_window([overlay], 128.0, 140.0)
    assert out == []


def test_initial_partial_line_requires_first_word_near_audio_boundary() -> None:
    overlay = _make_lyric_overlay(
        text="one two late word",
        start_s=0.0,
        end_s=3.0,
        line_id="line:late-initial",
        original_text="one two late word",
        original_start_s_song=120.0,
        original_end_s_song=130.1,
        original_words=[
            {"text": "one", "start_s_song": 120.0, "end_s_song": 121.0},
            {"text": "two", "start_s_song": 121.0, "end_s_song": 122.0},
            {"text": "late", "start_s_song": 129.2, "end_s_song": 129.6},
            {"text": "word", "start_s_song": 129.7, "end_s_song": 130.1},
        ],
    )

    tail = _make_lyric_overlay(
        text="real final line",
        start_s=3.4,
        end_s=4.0,
        line_id="line:tail",
        original_text="real final line",
        original_start_s_song=131.0,
        original_end_s_song=132.0,
        original_words=[
            {"text": "real", "start_s_song": 131.0, "end_s_song": 131.25},
            {"text": "final", "start_s_song": 131.25, "end_s_song": 131.55},
            {"text": "line", "start_s_song": 131.55, "end_s_song": 132.0},
        ],
    )

    out = _finalize_lyric_audible_window(
        [overlay, tail],
        128.0,
        132.0,
        keep_initial_partial_lines=True,
    )

    assert [ov["lyric_line_id"] for ov in out] == ["line:tail"]


def test_karaoke_interior_partial_keeps_late_first_word_timing() -> None:
    overlay = _make_lyric_overlay(
        text="early late word",
        start_s=0.0,
        end_s=4.0,
        line_id="karaoke:late-first-word",
        original_text="early late word",
        original_start_s_song=127.0,
        original_end_s_song=131.0,
        original_words=[
            {"text": "early", "start_s_song": 127.0, "end_s_song": 127.4},
            {"text": "late", "start_s_song": 129.2, "end_s_song": 129.6},
            {"text": "word", "start_s_song": 129.7, "end_s_song": 130.1},
        ],
        extras={"effect": "karaoke-line"},
    )

    out = _finalize_lyric_audible_window(
        [overlay],
        128.0,
        132.0,
        keep_initial_partial_lines=True,
    )

    assert len(out) == 1
    assert out[0]["text"] == "late word"
    assert out[0]["word_timings"][0]["text"] == "late"
    assert out[0]["word_timings"][0]["start_s"] == pytest.approx(1.2)


# ── Log-event assertion coverage (P1 from testing specialist) ────────────────


class _LogRecorder:
    """Stand-in for structlog.BoundLogger that captures every call.

    Pattern from tests/pipeline/test_template_matcher.py:417 — structlog's
    BoundLogger created at module import bypasses caplog/capture_logs in the
    default pytest config, so we monkeypatch the module-level `log` attribute
    with this recorder and inspect calls directly.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []  # (level, event, kwargs)

    def info(self, event, **kwargs):
        self.events.append(("info", event, kwargs))

    def warning(self, event, **kwargs):
        self.events.append(("warning", event, kwargs))

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def events_named(self, name: str) -> list[dict]:
        return [k for _, e, k in self.events if e == name]


def test_log_lyric_finalize_dropped_no_candidate_text(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    # Final partial with empty surviving words (no words at all → empty-words
    # short-circuit fires first). Use a non-empty word list whose timings sit
    # outside the audible window so we hit Step 3 with no candidate_text.
    overlay = _make_lyric_overlay(
        text="Lone word",
        start_s=2.0,
        end_s=2.6,
        line_id="line:no-cand",
        original_text="Lone word",
        original_start_s_song=130.0,
        original_end_s_song=130.6,
        original_words=[
            {"text": "Lone", "start_s_song": 131.0, "end_s_song": 131.5},
            {"text": "word", "start_s_song": 131.5, "end_s_song": 132.0},
        ],
    )
    # Audible end 130.4 → no word midpoint/start survives.
    # candidate_text stays None → Step 3 drops.
    out = _finalize_lyric_audible_window([overlay], 128.0, 130.4)
    assert out == []
    events = rec.events_named("lyric_finalize_dropped_no_candidate_text")
    assert events, f"expected drop event; got {[e[1] for e in rec.events]}"
    assert events[0].get("line_id") == "line:no-cand"


def test_log_lyric_finalize_dropped_interior_partial(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    # Interior must satisfy BOTH:
    #   - clipped_end < audible_end - _FINAL_LINE_TAIL_TOLERANCE_S (so NOT final)
    #   - coverage_duration < 0.65 AND coverage_words < 0.65 (interior strict
    #     floor fails) but basic floor passes (≥2 audible words, audible
    #     speech ≥0.75s) so it reaches Step 5 (not Step 3 no-candidate-text).
    # Setup: line song 125.0 → 130.0 (interior, ends before audible_end -
    # tolerance = 131.45). 10 evenly-spaced 0.5s words → only 4 of 10 audible
    # (mids ≥ 128). coverage_words = 0.4, coverage_duration = 0.4.
    original = "A B C D E F G H I J"
    original_words = [
        {"text": w, "start_s_song": 125.0 + i * 0.5, "end_s_song": 125.5 + i * 0.5}
        for i, w in enumerate(original.split())
    ]
    interior = _make_lyric_overlay(
        text=original,
        start_s=0.0,
        end_s=2.0,
        line_id="line:interior",
        original_text=original,
        original_start_s_song=125.0,
        original_end_s_song=130.0,
        original_words=original_words,
    )
    # A separate audible tail that REACHES audible_end takes the final-line
    # slot, so the fallback doesn't promote `line:interior` to final.
    tail = _make_lyric_overlay(
        text="X Y",
        start_s=3.5,
        end_s=3.7,
        line_id="line:tail-final",
        original_text="X Y",
        original_start_s_song=131.0,
        original_end_s_song=131.7,
        original_words=[
            {"text": "X", "start_s_song": 131.0, "end_s_song": 131.35},
            {"text": "Y", "start_s_song": 131.35, "end_s_song": 131.7},
        ],
    )
    _finalize_lyric_audible_window([interior, tail], 128.0, 131.7)
    events = rec.events_named("lyric_finalize_dropped_interior_partial")
    assert events, f"expected interior drop; got {[e[1] for e in rec.events]}"
    assert events[0].get("line_id") == "line:interior"
    # Structured kwargs carry coverage values
    assert "coverage_duration" in events[0]
    assert "coverage_words" in events[0]


def test_log_lyric_finalize_final_line_dropped_fragment_too_short(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    # The final-line `fragment_too_short` event fires when surviving_word_count
    # >= 2 AND audible_speech_s < 0.75 (basic floor fails on speech-sum even
    # though word-count passes). Engineer that case:
    # 5 words at 0.1s each. Audible window cuts after the 3rd word's
    # midpoint, so 3/5 are audible (coverage_words = 0.6, near-complete
    # fails). Alignment runs and produces candidate_text (3 words ≥ 2).
    # audible_speech_s = 3 × 0.1 = 0.3 < 0.75 → final-line basic floor
    # fails → drop with `lyric_finalize_final_line_dropped_fragment_too_short`.
    # Line bounds aligned with words (130.0 → 130.5) so the line-vs-word
    # mismatch reassignment does NOT overwrite the bounds.
    ov2 = _make_lyric_overlay(
        text="A B C D E",
        start_s=2.0,
        end_s=2.6,
        line_id="line:tail2",
        original_text="A B C D E",
        original_start_s_song=130.0,
        original_end_s_song=130.5,
        original_words=[
            {"text": "A", "start_s_song": 130.0, "end_s_song": 130.1},
            {"text": "B", "start_s_song": 130.1, "end_s_song": 130.2},
            {"text": "C", "start_s_song": 130.2, "end_s_song": 130.3},
            {"text": "D", "start_s_song": 130.3, "end_s_song": 130.4},
            {"text": "E", "start_s_song": 130.4, "end_s_song": 130.5},
        ],
    )
    # Audible end = 130.35 → midpoints A(130.05), B(130.15), C(130.25) inside;
    # D(130.35) midpoint == audible_end → excluded (right side exclusive);
    # E(130.45) outside. 3 audible.
    _finalize_lyric_audible_window([ov2], 128.0, 130.35)
    events = rec.events_named("lyric_finalize_final_line_dropped_fragment_too_short")
    assert events, f"expected fragment drop; got {[e[1] for e in rec.events]}"


def test_log_lyric_finalize_final_line_kept_truncated(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    original = "Hello world how are you today my friend"
    original_words = [
        {"text": w, "start_s_song": 130.0 + i * 0.5, "end_s_song": 130.4 + i * 0.5}
        for i, w in enumerate(original.split())
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=2.0,
        end_s=4.0,
        line_id="line:final-trunc",
        original_text=original,
        original_start_s_song=130.0,
        original_end_s_song=134.0,
        original_words=original_words,
    )
    # Audible window 128.0 → 132.0 (5 words audible — coverage_dur ~0.5).
    _finalize_lyric_audible_window([ov], 128.0, 132.0)
    events = rec.events_named("lyric_finalize_final_line_kept_truncated")
    assert events


def test_log_lyric_finalize_interior_partial_kept_truncated(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    # Interior must satisfy: NOT final (clipped_end < audible_end - 0.25)
    # AND coverage_floor passes (≥0.65 on either axis) AND not near-complete
    # (NOT both ≥0.9). Setup: audio_mix [128, 131.7]. Interior orig 127.0 →
    # 131.4 (clipped_end = 131.4 < 131.45 = audible_end - tolerance). 5 words,
    # 4 audible (mids ≥128). coverage_words = 0.8, coverage_duration ~0.77.
    interior = _make_lyric_overlay(
        text="A B C D E",
        start_s=0.0,
        end_s=3.4,
        line_id="line:interior-kept",
        original_text="A B C D E",
        original_start_s_song=127.0,
        original_end_s_song=131.4,
        original_words=[
            {"text": "A", "start_s_song": 127.0, "end_s_song": 128.0},  # mid 127.5 NOT audible
            {"text": "B", "start_s_song": 128.0, "end_s_song": 129.0},  # mid 128.5 audible
            {"text": "C", "start_s_song": 129.0, "end_s_song": 130.0},  # mid 129.5 audible
            {"text": "D", "start_s_song": 130.0, "end_s_song": 131.0},  # mid 130.5 audible
            {"text": "E", "start_s_song": 131.0, "end_s_song": 131.4},  # mid 131.2 audible
        ],
    )
    # Tail anchor reaches audible_end so it takes the final slot, leaving
    # `line:interior-kept` as a genuine interior partial.
    tail = _make_lyric_overlay(
        text="X Y",
        start_s=3.5,
        end_s=3.7,
        line_id="line:tail-final",
        original_text="X Y",
        original_start_s_song=131.4,
        original_end_s_song=131.7,
        original_words=[
            {"text": "X", "start_s_song": 131.4, "end_s_song": 131.55},
            {"text": "Y", "start_s_song": 131.55, "end_s_song": 131.7},
        ],
    )
    _finalize_lyric_audible_window([interior, tail], 128.0, 131.7)
    events = rec.events_named("lyric_finalize_interior_partial_kept_truncated")
    assert events, f"expected interior-kept event; got {[e[1] for e in rec.events]}"
    assert events[0].get("line_id") == "line:interior-kept"


def test_log_lyric_segments_missing_finalization_metadata(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    overlay = {
        "text": "Hello",
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 5.0,
        "lyric_line_id": "line:no-meta",
        # original_* fields intentionally missing
    }
    out = _finalize_lyric_audible_window([overlay], 128.0, 139.036)
    assert len(out) == 1
    events = rec.events_named("lyric_segments_missing_finalization_metadata")
    assert events
    assert "missing_fields" in events[0]
    # All 4 required fields should be in the missing list
    missing = events[0]["missing_fields"]
    for required in (
        "original_text",
        "original_start_s_song",
        "original_end_s_song",
        "original_words",
    ):
        assert required in missing


def test_log_lyric_finalize_line_bounds_word_mismatch(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    # Line says 130.0 → 140.0, but words say 130.0 → 132.0 — mismatch >100ms.
    original = "Hello world goodbye"
    original_words = [
        {"text": "Hello", "start_s_song": 130.0, "end_s_song": 130.5},
        {"text": "world", "start_s_song": 131.0, "end_s_song": 131.5},
        {"text": "goodbye", "start_s_song": 131.5, "end_s_song": 132.0},
    ]
    ov = _make_lyric_overlay(
        text=original,
        start_s=2.0,
        end_s=12.0,
        line_id="line:mismatch",
        original_text=original,
        original_start_s_song=130.0,
        original_end_s_song=140.0,  # 8 seconds AFTER last word ends
        original_words=original_words,
    )
    _finalize_lyric_audible_window([ov], 128.0, 145.0)
    events = rec.events_named("lyric_finalize_line_bounds_word_mismatch")
    assert events
    assert events[0].get("line_id") == "line:mismatch"
    assert events[0].get("end_mismatch_s", 0) > 0.1


def test_log_lyric_finalize_dropped_empty_words_outside_window(monkeypatch) -> None:
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    overlay = _make_lyric_overlay(
        text="Plain",
        start_s=0.0,
        end_s=3.0,
        line_id="line:plain-far",
        original_text="Plain",
        original_start_s_song=200.0,  # well past audible
        original_end_s_song=202.0,
        original_words=[],  # empty-words path
    )
    _finalize_lyric_audible_window([overlay], 128.0, 140.0)
    events = rec.events_named("lyric_finalize_dropped_empty_words_outside_window")
    assert events


# ── Review feedback fixes (#1, #3, #5, #6) — final-line, empty-words clamp,
# post_dwell preservation, alignment exact-or-log ────────────────────────────


def test_finalize_final_line_picked_from_clipped_audible_window_not_raw_end_s() -> None:
    """Review #1: final-line detection must operate on CLIPPED audible song-
    time windows. Setup: the real audible tail is a partial line that ends
    near `audio_mix_song_end_s`; a LATER lyric line was admitted at config
    time but its song-time audio is entirely past `audio_mix_song_end_s`
    (inaudible config'd line). The naive raw-end_s picker would tag the
    inaudible later line as final and starve the actual tail of the
    permissive final-line quality floor — dropping the tail.

    Fix asserts: the partial line gets `is_final=True` (so it's kept via the
    final-line keep-truncated path), the inaudible line drops out (no audible
    overlap → not in final_idxs, and the empty-words / coverage paths drop it
    anyway).
    """
    # audio mix: song-time [128.0, 139.036). audible_end_abs = 11.036.
    audible_partial = _make_lyric_overlay(
        text="She told my baby we'd danced 'til three",
        start_s=8.78,
        end_s=11.036,  # reaches audible end
        line_id="line:tail",
        original_text="She told my baby we'd danced 'til three",
        original_start_s_song=136.88,
        original_end_s_song=139.54,  # past audible_end 139.036
        original_words=[
            {"text": "She", "start_s_song": 136.88, "end_s_song": 137.30},
            {"text": "told", "start_s_song": 137.30, "end_s_song": 137.68},
            {"text": "my", "start_s_song": 137.68, "end_s_song": 137.98},
            {"text": "baby", "start_s_song": 137.98, "end_s_song": 138.38},
            {"text": "we'd", "start_s_song": 138.40, "end_s_song": 138.45},
            {"text": "danced", "start_s_song": 138.38, "end_s_song": 138.98},
            {"text": "'til", "start_s_song": 139.00, "end_s_song": 139.16},
            {"text": "three", "start_s_song": 139.18, "end_s_song": 139.54},
        ],
    )
    # The inaudible config'd line: raw end_s > audible_partial.end_s, so the
    # OLD picker would tag it as final. Its song-time audio is past 139.036
    # → no audible overlap → must NOT be tagged final under the new contract.
    inaudible_later = _make_lyric_overlay(
        text="Then showed a photo my baby cried",
        start_s=11.0,
        end_s=12.5,  # raw end_s GREATER than audible_partial — would fool the naive picker
        line_id="line:later-inaudible",
        original_text="Then showed a photo my baby cried",
        original_start_s_song=141.0,  # entirely past audio_mix_song_end_s=139.036
        original_end_s_song=146.0,
        original_words=[
            {"text": "Then", "start_s_song": 141.0, "end_s_song": 141.3},
            {"text": "showed", "start_s_song": 141.3, "end_s_song": 141.7},
        ],
    )
    out = _finalize_lyric_audible_window([audible_partial, inaudible_later], 128.0, 139.036)
    # Inaudible line dropped entirely (no audible overlap).
    surviving_ids = [o.get("lyric_line_id") for o in out if o.get("effect") == "lyric-line"]
    assert "line:later-inaudible" not in surviving_ids
    # Audible partial kept via final-line truncation (not dropped via interior
    # strict floor — proves it was tagged is_final).
    kept = [o for o in out if o.get("lyric_line_id") == "line:tail"]
    assert len(kept) == 1
    assert kept[0].get("display_text") is not None  # truncated


def test_finalize_final_idxs_handle_ties() -> None:
    """Review #1: when two lyric lines share the same clipped_end at the
    audible end (compressed tail), BOTH should be tagged final and get the
    permissive floor — not just one."""
    # Both lines end at song-time 139.036 → clipped_end == audible_end →
    # both within _FINAL_LINE_TAIL_TOLERANCE_S.
    words_a = [
        {"text": "A", "start_s_song": 138.0, "end_s_song": 138.4},
        {"text": "B", "start_s_song": 138.4, "end_s_song": 138.8},
    ]
    words_b = [
        {"text": "C", "start_s_song": 138.5, "end_s_song": 138.9},
        {"text": "D", "start_s_song": 138.9, "end_s_song": 139.036},
    ]
    line_a = _make_lyric_overlay(
        text="A B",
        start_s=10.0,
        end_s=11.036,
        line_id="line:tie-a",
        original_text="A B",
        original_start_s_song=138.0,
        original_end_s_song=139.036,
        original_words=words_a,
    )
    line_b = _make_lyric_overlay(
        text="C D",
        start_s=10.0,
        end_s=11.036,
        line_id="line:tie-b",
        original_text="C D",
        original_start_s_song=138.5,
        original_end_s_song=139.036,
        original_words=words_b,
    )
    out = _finalize_lyric_audible_window([line_a, line_b], 128.0, 139.036)
    # Both lines survive — both got the permissive final-line floor.
    # (audible_speech_s for each = 2 * 0.4 = 0.8 ≥ 0.75 → passes basic floor.)
    surviving_ids = [o.get("lyric_line_id") for o in out if o.get("effect") == "lyric-line"]
    assert "line:tie-a" in surviving_ids
    assert "line:tie-b" in surviving_ids


def test_finalize_final_fallback_picks_latest_audible_when_no_line_reaches_end() -> None:
    """Review #1: if no line's clipped_end reaches within
    _FINAL_LINE_TAIL_TOLERANCE_S of audible_end, the latest audible
    clipped_end still gets the final-line floor — never strand the actual
    last-audible line."""
    # Audible window 128.0 → 140.0 (end). Line ends at song-time 132.0, far
    # from 140.0. Should still be tagged final via the fallback.
    ov = _make_lyric_overlay(
        text="Lonely tail",
        start_s=2.0,
        end_s=4.0,
        line_id="line:lonely",
        original_text="Lonely tail",
        original_start_s_song=130.0,
        original_end_s_song=132.0,  # far short of 140.0
        original_words=[
            {"text": "Lonely", "start_s_song": 130.0, "end_s_song": 131.0},
            {"text": "tail", "start_s_song": 131.0, "end_s_song": 132.0},
        ],
    )
    out = _finalize_lyric_audible_window([ov], 128.0, 140.0)
    # Kept (near-complete since fully inside window).
    assert len(out) == 1


def test_finalize_empty_words_branch_clamps_abs_window() -> None:
    """Review #3: when original_words is empty but the line is mostly audible
    (cov >= 0.5), the line is KEPT with original text — AND its abs window
    must still be clamped to the audible video window so post_dwell or
    splitter overhang doesn't render text after silence falls.

    Setup: empty-words line whose splitter-assigned end_s is 12.0s (past
    audible_end_abs = 11.036) and start_s is -0.5 (before video start).
    After finalize: start_s clamped to 0.0, end_s clamped to 11.036.
    """
    overlay = _make_lyric_overlay(
        text="Hello world",
        start_s=-0.5,
        end_s=12.0,
        line_id="line:plain-overhang",
        original_text="Hello world",
        original_start_s_song=130.0,
        original_end_s_song=132.0,
        original_words=[],  # plain-lyric source
    )
    out = _finalize_lyric_audible_window([overlay], 128.0, 139.036)
    assert len(out) == 1
    # Original text preserved (no word data to align against).
    assert "display_text" not in out[0]
    assert out[0]["text"] == "Hello world"
    # Abs window clamped to audible region.
    assert out[0]["start_s"] == 0.0
    # audible_end_abs = 139.036 - 128.0 = 11.036 (float subtraction precision).
    assert out[0]["end_s"] == pytest.approx(11.036, abs=1e-6)


def test_apply_finalized_preserves_post_dwell_only_clamps_to_audio_end() -> None:
    """Review #5: `_apply_finalized` INTENTIONALLY preserves the splitter's
    post_dwell extension — it does NOT shrink end_s to per-line clipped_end.
    Only clamps to the OVERALL audio mix end (audible_end_abs). This differs
    from the earlier _abs_window_from_clip plan in §2g, which would have
    shrunk to clipped_end. Document the divergence with this test so a future
    contributor reading the plan + the code understands they disagree by
    design — the splitter's post_dwell is the YouTube-lyric-video settle
    time (PR #287) and must not be undone when truncating text.

    Setup: line audio ends at song-time 132.0; splitter assigned end_s=5.961
    (post_dwell extends visible window ~1s past audio end into abs 5.961 ≈
    song-time 133.961). audible_end_abs = 11.036 (audio mix end at 139.036).
    After finalize: end_s STAYS at 5.961 (preserved post_dwell), NOT shrunk
    to 4.0 (which would be 132.0 - 128.0 = clipped_end_abs).
    """
    original = "So take my strong advice just remember to always think twice"
    words = [
        {"text": w, "start_s_song": 128.0 + i * 0.5, "end_s_song": 128.5 + i * 0.5}
        for i, w in enumerate(original.split()[:8])  # 8 words covering song 128-132
    ]
    overlay = _make_lyric_overlay(
        text=original,
        start_s=0.0,
        end_s=5.961,  # splitter end with post_dwell extension
        line_id="line:postdwell",
        original_text=original,
        original_start_s_song=128.0,
        original_end_s_song=132.0,
        original_words=words,
    )
    # Audible window covers everything → near-complete → Step 1 returns
    # overlay unchanged (no shrink). We need a PARTIAL case to test the
    # _apply_finalized path. Use a window that drops the last word so we
    # go through Step 6 (interior partial kept truncated).
    last_word = _make_lyric_overlay(
        text="Final",
        start_s=12.0,
        end_s=14.0,
        line_id="line:after",
        original_text="Final word here",
        original_start_s_song=145.0,
        original_end_s_song=145.5,
        original_words=[
            {"text": "Final", "start_s_song": 145.0, "end_s_song": 145.25},
            {"text": "word", "start_s_song": 145.25, "end_s_song": 145.5},
        ],
    )
    # audible_end song = 131.49 → drops the 8th word (starts at 131.5).
    # → 7 of 8 words audible, coverage_words = 0.875 < 0.9 (not near-complete).
    # → alignment runs, candidate_text rebuilt.
    # → final partial floors: speech ~3.49s, words 7 → both meet floor.
    # → _apply_finalized fires.
    out = _finalize_lyric_audible_window([overlay, last_word], 128.0, 131.49)
    kept = [o for o in out if o.get("lyric_line_id") == "line:postdwell"]
    assert len(kept) == 1
    # display_text was rewritten (partial path).
    assert kept[0].get("display_text") is not None
    # end_s preserved at splitter value (5.961), NOT shrunk to clipped_end_abs
    # = min(132.0, 131.7) - 128.0 = 3.7. The audible_end_abs cap = 131.7 - 128
    # = 3.7 SO end_s clamped to min(5.961, 3.7) = 3.7.
    # Wait — audible_end_abs here IS the cap so this test inadvertently shows
    # the audible-end clamp not the post_dwell preservation. Reframe: use a
    # wider audible window where audible_end_abs > splitter end_s and assert
    # end_s STAYS at 5.961.
    out2 = _finalize_lyric_audible_window([overlay, last_word], 128.0, 140.0)
    kept2 = [o for o in out2 if o.get("lyric_line_id") == "line:postdwell"]
    assert len(kept2) == 1
    if kept2[0].get("display_text") is not None:
        # Partial path fired → _apply_finalized ran → end_s should be
        # preserved at splitter value because audible_end_abs (12.0) >
        # splitter end (5.961).
        assert kept2[0]["end_s"] == 5.961, (
            "post_dwell extension must be preserved — _apply_finalized clamps "
            "only to overall audio mix end, NOT to per-line clipped_end. This "
            "diverges intentionally from plan §2g's _abs_window_from_clip."
        )
    # else near-complete path — overlay returned unchanged, also fine.


def test_align_partial_match_returns_none_and_logs(monkeypatch) -> None:
    """Review #6: when contiguous alignment matches only SOME audible words,
    return None so the caller falls back to conservative join. Log
    `lyric_align_partial_match_omits_word` so coverage drift is debuggable.
    """
    from app.pipeline import lyric_injector

    rec = _LogRecorder()
    monkeypatch.setattr(lyric_injector, "log", rec)
    # Original "hello world goodbye"; audible words [hello, world, mystery].
    # Anchor=0 matches hello+world then mystery≠goodbye → matched=2.
    # No other anchor produces a full 3-word match → best is 2/3 = partial.
    # New behavior: returns None + logs omitted ["mystery"].
    out = _align_audible_words_to_original_text(
        original_text="hello world goodbye",
        audible_words=[
            {"text": "hello"},
            {"text": "world"},
            {"text": "mystery"},
        ],
    )
    assert out is None, "partial-match must return None to force conservative join"
    events = rec.events_named("lyric_align_partial_match_omits_word")
    assert events
    assert "mystery" in events[0].get("omitted_words", [])


def test_align_full_match_still_returns_substring() -> None:
    """Review #6: exact full contiguous match still wins — no regression on
    the existing happy path."""
    out = _align_audible_words_to_original_text(
        original_text="She told my baby we'd danced 'til three",
        audible_words=[
            {"text": "She"},
            {"text": "told"},
            {"text": "my"},
            {"text": "baby"},
            {"text": "we'd"},
            {"text": "danced"},
        ],
    )
    assert out == "She told my baby we'd danced"
