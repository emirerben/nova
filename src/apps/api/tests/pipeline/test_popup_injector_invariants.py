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

from app.pipeline.lyric_word_resync import resync_slot_overlays
from tests.pipeline._lyric_invariant_helpers import inject_overlays_for_style

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
