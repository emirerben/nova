"""Pop-up (per-word-pop) invariant suite.

Locks the cumulative-reveal contracts that prevent visual overlap and
"double pop" stutters:
  - Stages are strictly monotonically non-decreasing in start_s.
  - Adjacent stages butt-join (end of N == start of N+1) so two cumulative
    reveals never render simultaneously — the same invariant the
    line-style overlap-prevention enforces, scaled to per-word grain.
  - Each stage carries the `pop_animated_suffix` field marking which word
    pops on entry (the renderer keeps the prefix static — without this
    field every stage would re-animate the entire line).
  - section_anchor_s / section_end_anchor_s stamps are present and finite.
  - Post-snap re-anchor preserves the butt-join after a realistic
    beat-snap drift.
"""

from __future__ import annotations

import math

import pytest

from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.lyric_word_resync import resync_slot_overlays
from tests.pipeline._lyric_invariant_helpers import (
    build_single_slot_recipe,
    inject_overlays_for_style,
    make_lyrics_cached,
)

# Per-stage gap below which we declare two stages as butted (adjacent),
# accounting for the 3-decimal rounding inside `_inject_per_word_pop`.
_BUTT_JOIN_EPSILON_S: float = 0.002

_SYNC_BUDGET_S: float = 0.050


def _three_word_fixture() -> list[dict]:
    """One line with three words at distinct onsets — the smallest
    cumulative-reveal scenario that exercises both stage transitions and
    last-word dwell behavior.
    """
    return [
        {
            "text": "I love you",
            "start_s": 2.0,
            "end_s": 5.0,
            "words": [
                {"text": "I", "start_s": 2.0, "end_s": 2.5},
                {"text": "love", "start_s": 2.5, "end_s": 3.4},
                {"text": "you", "start_s": 3.4, "end_s": 5.0},
            ],
        },
    ]


def _nested_adlib_fixture() -> list[dict]:
    return [
        {
            "text": "I swear to God I don't even know why I put up with",
            "start_s": 1.0,
            "end_s": 4.2,
            "words": [
                {"text": "I", "start_s": 1.0, "end_s": 1.12},
                {"text": "swear", "start_s": 1.12, "end_s": 1.35},
                {"text": "to", "start_s": 1.35, "end_s": 1.46},
                {"text": "God", "start_s": 1.46, "end_s": 1.68},
                {"text": "I", "start_s": 1.68, "end_s": 1.78},
                {"text": "don't", "start_s": 1.78, "end_s": 2.05},
                {"text": "even", "start_s": 2.05, "end_s": 2.32},
                {"text": "know", "start_s": 2.32, "end_s": 2.58},
                {"text": "why", "start_s": 2.58, "end_s": 2.8},
                {"text": "I", "start_s": 2.8, "end_s": 2.9},
                {"text": "put", "start_s": 2.9, "end_s": 3.12},
                {"text": "up", "start_s": 3.12, "end_s": 3.34},
                {"text": "with", "start_s": 3.34, "end_s": 4.2},
            ],
        },
        {
            "text": "Ok",
            "start_s": 1.32,
            "end_s": 1.85,
            "words": [
                {"text": "Ok", "start_s": 1.32, "end_s": 1.85},
            ],
        },
    ]


def _two_word_line() -> dict:
    return {
        "text": "Hello there",
        "start_s": 1.0,
        "end_s": 2.0,
        "words": [
            {"text": "Hello", "start_s": 1.0, "end_s": 1.4},
            {"text": "there", "start_s": 1.4, "end_s": 2.0},
        ],
    }


# ── per-stage shape invariants ─────────────────────────────────────────────


def test_popup_emits_one_overlay_per_word_stage() -> None:
    """Cumulative reveal contract: N words ⇒ N stages (one overlay each).
    Pin so a future refactor that switches to N-1 stages (drop the first
    "I" overlay, for example) is caught immediately.
    """
    fixture = _three_word_fixture()
    overlays = inject_overlays_for_style(style="per-word-pop", lines=fixture)
    expected_stages = sum(len(line["words"]) for line in fixture)
    assert len(overlays) == expected_stages, (
        f"expected {expected_stages} stages, got {len(overlays)}"
    )
    for ov in overlays:
        assert ov["effect"] == "pop-in", (
            f"per-word-pop overlay must carry effect='pop-in', got {ov.get('effect')!r}"
        )


def test_popup_stages_carry_pop_animated_suffix_field() -> None:
    """`pop_animated_suffix` tells the renderer which word should animate
    on entry; without it the prefix re-pops on every stage. Pin presence.
    """
    overlays = inject_overlays_for_style(style="per-word-pop", lines=_three_word_fixture())
    for ov in overlays:
        suffix = ov.get("pop_animated_suffix")
        assert isinstance(suffix, str) and suffix.strip(), (
            f"missing or empty pop_animated_suffix on stage {ov.get('text')!r}"
        )


def test_popup_no_stack_ignores_template_pop_suffix_overlays() -> None:
    """A template text overlay can also carry ``pop_animated_suffix``.

    The lyric no-stack cleanup must only touch lyric-owned pop stages, not
    pre-existing template text that happens to use the same renderer hint.
    """
    template_overlay = {
        "text": "Template hook",
        "effect": "pop-in",
        "pop_animated_suffix": "hook",
        "start_s": 0.0,
        "end_s": 5.0,
        "position": "bottom",
        "position_x_frac": 0.06,
        "text_anchor": "left",
    }
    recipe = build_single_slot_recipe(target_duration_s=6.0)
    recipe["slots"][0]["text_overlays"] = [template_overlay]

    out = inject_lyric_overlays(
        recipe,
        make_lyrics_cached(lines=[_two_word_line()]),
        best_start_s=0.0,
        best_end_s=6.0,
        lyrics_config={"enabled": True, "style": "per-word-pop"},
    )

    rendered_template = out["slots"][0]["text_overlays"][0]
    assert rendered_template["text"] == "Template hook"
    assert rendered_template["start_s"] == 0.0
    assert rendered_template["end_s"] == 5.0


def test_popup_no_stack_ignores_malformed_non_lyric_pop_suffix_overlays() -> None:
    """Malformed recipe text should not crash lyric injection.

    Recipes come from JSONB/admin-edited sources, so a non-lyric overlay with a
    renderer suffix hint and bad timing must be ignored by the lyric cleanup.
    """
    template_overlay = {
        "text": "Template hook",
        "effect": "pop-in",
        "pop_animated_suffix": "hook",
        "start_s": "not-a-number",
        "end_s": "also-not-a-number",
        "position": "bottom",
        "position_x_frac": 0.06,
        "text_anchor": "left",
    }
    recipe = build_single_slot_recipe(target_duration_s=6.0)
    recipe["slots"][0]["text_overlays"] = [template_overlay]

    out = inject_lyric_overlays(
        recipe,
        make_lyrics_cached(lines=[_two_word_line()]),
        best_start_s=0.0,
        best_end_s=6.0,
        lyrics_config={"enabled": True, "style": "per-word-pop"},
    )

    rendered_template = out["slots"][0]["text_overlays"][0]
    assert rendered_template["text"] == "Template hook"
    assert rendered_template["start_s"] == "not-a-number"
    assert rendered_template["end_s"] == "also-not-a-number"


def test_popup_stages_are_monotonic_in_start_s() -> None:
    """Stage onsets must be non-decreasing. A backwards stage would render
    a later word first — visually impossible to read as a coherent reveal.
    """
    overlays = inject_overlays_for_style(style="per-word-pop", lines=_three_word_fixture())
    prev = -math.inf
    for ov in overlays:
        assert float(ov["start_s"]) >= prev, (
            f"stages not monotonic: {ov['text']!r} starts at {ov['start_s']} "
            f"after a stage at {prev}"
        )
        prev = float(ov["start_s"])


def test_popup_stages_butt_join_with_no_visible_overlap() -> None:
    """The cumulative-reveal contract: stage N ends exactly where stage
    N+1 begins. Without this either:
      - end_N > start_{N+1}  ⇒ two overlays render simultaneously
        (the prod "double-pop" glitch);
      - end_N < start_{N+1}  ⇒ the screen blanks between words
        (the prod 89cde014 glitch on cumulative reveals).
    Pin both directions to 2ms of float-rounding tolerance.
    """
    overlays = inject_overlays_for_style(style="per-word-pop", lines=_three_word_fixture())
    # The last stage may carry a dwell — exclude it from the butt-join chain.
    for i in range(len(overlays) - 1):
        gap = float(overlays[i + 1]["start_s"]) - float(overlays[i]["end_s"])
        assert abs(gap) <= _BUTT_JOIN_EPSILON_S, (
            f"stages {i} and {i + 1} not butt-joined: gap = {gap * 1000:.1f}ms "
            f"(end {overlays[i]['end_s']}, next start {overlays[i + 1]['start_s']})"
        )


def test_popup_stage_durations_are_non_zero() -> None:
    """A zero-duration stage would render as a single-frame flash (or not
    at all). Pin the floor implied by the _MIN_RENDERABLE_S check inside
    `_inject_per_word_pop`.
    """
    overlays = inject_overlays_for_style(style="per-word-pop", lines=_three_word_fixture())
    for ov in overlays:
        dur = float(ov["end_s"]) - float(ov["start_s"])
        assert dur > 0.0, (
            f"zero-duration stage {ov.get('text')!r} survived the helper's "
            f"renderable check (start {ov['start_s']}, end {ov['end_s']})"
        )


# ── cross-line overlap invariants ──────────────────────────────────────────


def test_popup_drops_nested_short_adlib_line() -> None:
    """A one-word ad-lib inside a longer line shares the same pop-up lane.
    Rendering both at once makes unreadable overlapping text, so the short
    nested line must be suppressed and the canonical main lyric kept.
    """
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=5.0,
        lines=_nested_adlib_fixture(),
    )
    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    assert "Ok" not in texts
    assert any(text.endswith("why I put up with") for text in texts)


def test_popup_nested_adlib_no_longer_creates_simultaneous_active_lines() -> None:
    """Regression for job 1b23fc80: at 1.34s the old injector emitted
    both the main cumulative line and the nested "Ok" line.
    """
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=5.0,
        lines=_nested_adlib_fixture(),
    )
    active = [ov for ov in overlays if float(ov["start_s"]) <= 1.34 < float(ov["end_s"])]
    assert len(active) == 1
    assert str(active[0].get("text", "")).startswith("I swear")


def test_popup_drops_short_adlib_that_overlaps_long_line_tail() -> None:
    """Exact production shape from preview job 1b23fc80: the short ad-lib
    starts before the long line clears and extends slightly past it. It is not
    fully contained, but still shares the same one-lane pop-up surface.
    """
    lines = [
        {
            "text": "I swear to God I don't even know why I put up with you",
            "start_s": 9.04,
            "end_s": 12.53,
            "words": [
                {"text": "I", "start_s": 9.04, "end_s": 9.48},
                {"text": "swear", "start_s": 9.48, "end_s": 9.907},
                {"text": "to", "start_s": 9.907, "end_s": 10.333},
                {"text": "God", "start_s": 10.333, "end_s": 10.74},
                {"text": "I", "start_s": 10.74, "end_s": 11.1},
                {"text": "don't", "start_s": 11.1, "end_s": 11.2},
                {"text": "even", "start_s": 11.2, "end_s": 11.3},
                {"text": "know", "start_s": 11.3, "end_s": 11.4},
                {"text": "why", "start_s": 11.4, "end_s": 11.5},
                {"text": "I", "start_s": 11.5, "end_s": 11.6},
                {"text": "put", "start_s": 11.6, "end_s": 11.8},
                {"text": "up", "start_s": 11.8, "end_s": 12.0},
                {"text": "with", "start_s": 12.0, "end_s": 12.28},
                {"text": "you", "start_s": 12.28, "end_s": 12.53},
            ],
        },
        {
            "text": "Ok stop",
            "start_s": 12.23,
            "end_s": 13.08,
            "words": [
                {"text": "Ok", "start_s": 12.23, "end_s": 12.48},
                {"text": "stop", "start_s": 12.48, "end_s": 13.08},
            ],
        },
    ]
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=14.0,
        lines=lines,
    )
    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    assert "Ok" not in texts
    assert "Ok stop" not in texts
    active = [ov for ov in overlays if float(ov["start_s"]) <= 12.4 < float(ov["end_s"])]
    assert len(active) == 1
    assert str(active[0].get("text", "")).startswith("I swear")


def test_popup_drops_leading_sentence_tail_from_clamped_preview_start() -> None:
    """Exact opening shape from job 1b23fc80.

    The preview window starts mid-line, leaving the tail ``do you?`` before the
    actual opening phrase ``You men are all alike``. Pop-up should start on the
    meaningful phrase, not on a clipped previous sentence fragment.
    """
    lines = [
        {
            "text": "You don't even realize what you did, do you? You men are all alike",
            "original_text": "You don't even realize what you did, do you? You men are all alike",
            "start_s": -1.88,
            "end_s": 1.16,
            "words": [
                {"text": "do", "start_s": -0.07, "end_s": 0.1},
                {"text": "you", "start_s": 0.12, "end_s": 0.32},
                {"text": "You", "start_s": 0.34, "end_s": 0.39},
                {"text": "men", "start_s": 0.32, "end_s": 0.48},
                {"text": "are", "start_s": 0.48, "end_s": 0.56},
                {"text": "all", "start_s": 0.56, "end_s": 0.84},
                {"text": "alike", "start_s": 0.84, "end_s": 1.16},
            ],
        }
    ]
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=2.0,
        lines=lines,
    )

    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    assert texts
    assert not any(text.startswith("do") for text in texts)
    assert any(text.startswith("You men") for text in texts)
    assert texts[-1] == "You men are all alike"


def test_popup_drops_repeated_parenthetical_prefix_from_clamped_marea_preview() -> None:
    """Regression for Marea pop-up preview job c9dc62c7.

    The preview starts inside ``Day by day (we've lost dancing)``. Pop-up used
    the raw section-clamped words and opened on ``by day`` even though the
    audible hook is the parenthetical phrase.
    """
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=15.3,
        best_start_s=155.7,
        best_end_s=171.0,
        lines=[
            {
                "text": "Day by day (we've lost dancing)",
                "start_s": 155.02,
                "end_s": 158.34,
                "words": [
                    {"text": "Day", "start_s": 155.02, "end_s": 155.28},
                    {"text": "by", "start_s": 155.28, "end_s": 155.94},
                    {"text": "day", "start_s": 156.04, "end_s": 157.02},
                    {"text": "we've", "start_s": 157.02, "end_s": 157.02},
                    {"text": "lost", "start_s": 157.02, "end_s": 157.62},
                    {"text": "dancing", "start_s": 157.62, "end_s": 158.34},
                ],
            },
            {
                "text": "Marvellous",
                "start_s": 170.12,
                "end_s": 171.94,
                "words": [
                    {"text": "Marvellous", "start_s": 170.12, "end_s": 171.94},
                ],
            },
        ],
    )

    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    assert texts
    assert texts[0] == "we've lost"
    assert "we've lost dancing" in texts
    assert not any(text.startswith("by") or text.startswith("day") for text in texts)


def test_popup_clips_previous_line_when_next_long_line_starts_before_cached_line_end() -> None:
    """Regression for Marea pop-up preview job c9dc62c7 around ``Will``.

    LRCLIB can leave the previous line's cached end past the next line's first
    word. The terminal cumulative stage must clear when the next pop-up line
    starts; otherwise the frame reads as overlapping ``Will`` + ``What`` text.
    """
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=15.3,
        lines=[
            {
                "text": "What comes next",
                "start_s": 7.8,
                # Overlong line bound: last word ends at 9.15s, but the cached
                # line lingers into the next lyric line.
                "end_s": 11.4,
                "words": [
                    {"text": "What", "start_s": 7.8, "end_s": 8.2},
                    {"text": "comes", "start_s": 8.2, "end_s": 8.65},
                    {"text": "next", "start_s": 8.65, "end_s": 9.15},
                ],
            },
            {
                "text": "Will be marvellous",
                "start_s": 10.8,
                "end_s": 13.0,
                "words": [
                    {"text": "Will", "start_s": 10.8, "end_s": 11.15},
                    {"text": "be", "start_s": 11.15, "end_s": 11.5},
                    {"text": "marvellous", "start_s": 11.5, "end_s": 13.0},
                ],
            },
        ],
    )

    stage_by_text = {str(ov.get("text", "")).strip(): ov for ov in overlays}
    assert stage_by_text["What comes next"]["end_s"] == pytest.approx(
        stage_by_text["Will"]["start_s"],
        abs=_BUTT_JOIN_EPSILON_S,
    )
    active_at_will = [
        str(ov.get("text", "")).strip()
        for ov in overlays
        if float(ov["start_s"]) <= 10.9 < float(ov["end_s"])
    ]
    assert active_at_will == ["Will"]


def test_popup_repairs_collapsed_non_monotonic_word_timings_for_sync() -> None:
    """Regression for job 1b23fc80 at ~12s.

    The cached word timings collapsed ``don't`` through ``up`` into one 50ms
    window and made ``with`` start before them. The pop-up builder used to drop
    those sub-renderable stages and reveal half the line at once, making the
    lyric visibly late/out of sync with the audio.
    """
    words = [
        {"text": "I", "start_s": 9.04, "end_s": 9.46},
        {"text": "swear", "start_s": 9.48, "end_s": 9.867},
        {"text": "to", "start_s": 9.907, "end_s": 10.293},
        {"text": "God", "start_s": 10.333, "end_s": 10.72},
        {"text": "I", "start_s": 10.74, "end_s": 11.5},
        {"text": "don't", "start_s": 11.52, "end_s": 11.57},
        {"text": "even", "start_s": 11.52, "end_s": 11.57},
        {"text": "know", "start_s": 11.52, "end_s": 11.57},
        {"text": "why", "start_s": 11.52, "end_s": 11.57},
        {"text": "I", "start_s": 11.52, "end_s": 11.57},
        {"text": "put", "start_s": 11.52, "end_s": 11.57},
        {"text": "up", "start_s": 11.52, "end_s": 11.57},
        {"text": "with", "start_s": 11.5, "end_s": 12.26},
        {"text": "you", "start_s": 12.28, "end_s": 12.53},
    ]
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=14.0,
        lines=[
            {
                "text": "I swear to God I don't even know why I put up with you",
                "start_s": 9.04,
                "end_s": 12.53,
                "words": words,
            }
        ],
    )

    assert len(overlays) == len(words)
    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    for expected in (
        "I swear to God I don't",
        "I swear to God I don't even",
        "I swear to God I don't even know",
        "I swear to God I don't even know why",
        "I swear to God I don't even know why I put",
        "I swear to God I don't even know why I put up",
        "I swear to God I don't even know why I put up with",
    ):
        assert expected in texts

    stage_by_text = {str(ov.get("text", "")).strip(): ov for ov in overlays}
    assert abs(float(stage_by_text["I swear to God I"]["end_s"]) - 11.52) <= 0.002
    assert abs(float(stage_by_text["I swear to God I don't"]["start_s"]) - 11.52) <= 0.002

    for i in range(len(overlays) - 1):
        assert float(overlays[i]["start_s"]) <= float(overlays[i + 1]["start_s"])
        gap = float(overlays[i + 1]["start_s"]) - float(overlays[i]["end_s"])
        assert abs(gap) <= _BUTT_JOIN_EPSILON_S


def test_popup_repairs_late_malformed_line_start_after_large_gap() -> None:
    """Regression for job 1b23fc80 around 8s.

    The previous line is well-timed, then a malformed line with collapsed word
    timings starts 1.64s later. Pop-up should not leave that whole blank gap
    and then burst into the phrase late.
    """
    lines = [
        {
            "text": "What's really important like my feelings",
            "start_s": 4.7,
            "end_s": 7.4,
            "words": [
                {"text": "What's", "start_s": 4.7, "end_s": 5.06},
                {"text": "really", "start_s": 5.06, "end_s": 5.34},
                {"text": "important", "start_s": 5.34, "end_s": 5.94},
                {"text": "like", "start_s": 6.18, "end_s": 6.54},
                {"text": "my", "start_s": 6.54, "end_s": 6.92},
                {"text": "feelings", "start_s": 6.92, "end_s": 7.4},
            ],
        },
        {
            "text": "I swear to God I don't even know why I put up with you",
            "start_s": 9.04,
            "end_s": 12.53,
            "words": [
                {"text": "I", "start_s": 9.04, "end_s": 9.46},
                {"text": "swear", "start_s": 9.48, "end_s": 9.867},
                {"text": "to", "start_s": 9.907, "end_s": 10.293},
                {"text": "God", "start_s": 10.333, "end_s": 10.72},
                {"text": "I", "start_s": 10.74, "end_s": 11.5},
                {"text": "don't", "start_s": 11.52, "end_s": 11.57},
                {"text": "even", "start_s": 11.52, "end_s": 11.57},
                {"text": "know", "start_s": 11.52, "end_s": 11.57},
                {"text": "why", "start_s": 11.52, "end_s": 11.57},
                {"text": "I", "start_s": 11.52, "end_s": 11.57},
                {"text": "put", "start_s": 11.52, "end_s": 11.57},
                {"text": "up", "start_s": 11.52, "end_s": 11.57},
                {"text": "with", "start_s": 11.5, "end_s": 12.26},
                {"text": "you", "start_s": 12.28, "end_s": 12.53},
            ],
        },
    ]
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=14.0,
        lines=lines,
    )

    stage_by_text = {str(ov.get("text", "")).strip(): ov for ov in overlays}
    assert float(stage_by_text["I"]["start_s"]) == pytest.approx(8.0, abs=0.002)
    assert float(stage_by_text["I swear to God"]["start_s"]) == pytest.approx(8.971, abs=0.002)
    assert float(stage_by_text["I swear to God I don't"]["start_s"]) == pytest.approx(
        9.618, abs=0.002
    )


def test_popup_keeps_short_line_that_is_not_nested() -> None:
    """A standalone short lyric is valid pop-up content; only contained
    ad-libs are dropped.
    """
    lines = _three_word_fixture() + [
        {
            "text": "Ok",
            "start_s": 5.3,
            "end_s": 5.8,
            "words": [
                {"text": "Ok", "start_s": 5.3, "end_s": 5.8},
            ],
        },
    ]
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=8.0,
        lines=lines,
    )
    assert any(str(ov.get("text", "")).strip() == "Ok" for ov in overlays)


def test_popup_keeps_adjacent_short_line_with_tiny_boundary_overlap() -> None:
    """Real lyric lines can have small alignment overlap at a boundary.

    A two-word line that starts a few frames before the previous long line ends
    is not an ad-lib sitting on top of the phrase; it is the next lyric and must
    still render.
    """
    lines = [
        {
            "text": "I keep waiting for the morning",
            "start_s": 0.0,
            "end_s": 2.06,
            "words": [
                {"text": "I", "start_s": 0.0, "end_s": 0.2},
                {"text": "keep", "start_s": 0.2, "end_s": 0.6},
                {"text": "waiting", "start_s": 0.6, "end_s": 1.2},
                {"text": "for", "start_s": 1.2, "end_s": 1.5},
                {"text": "the", "start_s": 1.5, "end_s": 1.7},
                {"text": "morning", "start_s": 1.7, "end_s": 2.06},
            ],
        },
        {
            "text": "so long",
            "start_s": 2.0,
            "end_s": 2.8,
            "words": [
                {"text": "so", "start_s": 2.0, "end_s": 2.35},
                {"text": "long", "start_s": 2.35, "end_s": 2.8},
            ],
        },
    ]
    overlays = inject_overlays_for_style(
        style="per-word-pop",
        target_duration_s=4.0,
        lines=lines,
    )
    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    assert "so" in texts
    assert "so long" in texts


# ── stamp invariants ───────────────────────────────────────────────────────


def test_popup_overlays_carry_finite_section_anchor_stamps() -> None:
    """Every pop-in overlay must carry both `section_anchor_s` and
    `section_end_anchor_s` as finite floats — without them the M2
    resync pass is a silent no-op for popup, the same as for karaoke.
    """
    overlays = inject_overlays_for_style(style="per-word-pop", lines=_three_word_fixture())
    for ov in overlays:
        for key in ("section_anchor_s", "section_end_anchor_s"):
            assert key in ov, f"missing {key} on stage {ov.get('text')!r}"
            value = ov[key]
            assert isinstance(value, int | float), (
                f"{key} must be numeric, got {type(value).__name__}"
            )
            assert math.isfinite(float(value)), f"{key} not finite: {value}"


# ── sync correctness against beat-snap drift ───────────────────────────────


def test_popup_butt_join_preserved_after_resync_against_beat_snap() -> None:
    """Run the M2 resync pass against a simulated beat-snap drift.
    Verify the butt-join contract still holds — drift would manifest as
    adjacent stages overlapping (visible double-pop) or gapping (blank
    flash between words).
    """
    overlays = inject_overlays_for_style(style="per-word-pop", lines=_three_word_fixture())
    rewritten = resync_slot_overlays(
        overlays,
        slot_post_snap_section_start_s=0.2,  # 200ms drift, realistic worst case
        slot_post_snap_duration_s=19.8,
    )
    assert rewritten == len(overlays), (
        f"resync rewrote {rewritten} of {len(overlays)} stages — every popup "
        f"stage should be rewritten because all carry section_anchor_s"
    )
    for i in range(len(overlays) - 1):
        gap = float(overlays[i + 1]["start_s"]) - float(overlays[i]["end_s"])
        assert abs(gap) <= _BUTT_JOIN_EPSILON_S, (
            f"butt-join broken after resync: stages {i} and {i + 1} gap {gap * 1000:.1f}ms"
        )


def test_popup_stage_onsets_stay_within_sync_budget_after_resync() -> None:
    """Each stage's render-time onset must match the word's song-time
    onset within the ±50 ms perceptual budget after resync — same proof
    structure as the karaoke sync test.
    """
    lines = _three_word_fixture()
    overlays = inject_overlays_for_style(style="per-word-pop", lines=lines)
    post_snap_section_start_s = 0.2
    resync_slot_overlays(
        overlays,
        slot_post_snap_section_start_s=post_snap_section_start_s,
        slot_post_snap_duration_s=19.8,
    )
    # For popup, each overlay's section_anchor_s IS the word's section-time
    # onset (per `_inject_per_word_pop` stamping stage.start_s). After
    # resync, post_snap_start + overlay.start_s should equal section_anchor_s.
    for ov in overlays:
        render_time_section = post_snap_section_start_s + float(ov["start_s"])
        expected = float(ov["section_anchor_s"])
        drift = abs(render_time_section - expected)
        assert drift <= _SYNC_BUDGET_S, (
            f"popup stage {ov.get('text')!r} drift {drift * 1000:.1f}ms "
            f"exceeds sync budget {_SYNC_BUDGET_S * 1000:.0f}ms after resync"
        )
