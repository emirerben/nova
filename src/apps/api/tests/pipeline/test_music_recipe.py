"""Unit tests for app/pipeline/music_recipe.py.

No I/O, no DB — pure algorithm tests.
"""

import pytest

from app.pipeline.music_recipe import (
    auto_best_section,
    count_slots,
    generate_music_recipe,
    merge_template_with_track,
)

# ── auto_best_section ─────────────────────────────────────────────────────────


def test_auto_best_section_empty_beats() -> None:
    start, end = auto_best_section([], window_s=30.0, track_duration_s=120.0)
    assert start == 0.0
    assert end == pytest.approx(30.0)


def test_auto_best_section_empty_beats_no_duration() -> None:
    start, end = auto_best_section([], window_s=45.0)
    assert start == 0.0
    assert end == pytest.approx(45.0)


def test_auto_best_section_finds_peak_density() -> None:
    # Sparse beats 0-30s, dense beats 60-90s
    sparse = [float(i) for i in range(0, 30, 4)]  # 8 beats
    dense = [float(i) for i in range(60, 90, 1)]  # 30 beats
    all_beats = sparse + dense

    start, end = auto_best_section(all_beats, window_s=30.0, track_duration_s=120.0)

    # Dense cluster should win
    assert start >= 55.0, f"Expected start near 60s, got {start}"
    assert end == pytest.approx(start + 30.0, abs=1.0)


def test_auto_best_section_caps_to_track_duration() -> None:
    beats = [10.0, 20.0, 25.0, 28.0]  # cluster near end
    start, end = auto_best_section(beats, window_s=30.0, track_duration_s=35.0)
    assert end <= 35.0
    assert start >= 0.0


def test_auto_best_section_track_shorter_than_window() -> None:
    beats = [1.0, 3.0, 5.0]
    start, end = auto_best_section(beats, window_s=30.0, track_duration_s=10.0)
    assert end <= 10.0
    assert start >= 0.0


# ── Layer 3: lyric-aware section selection ────────────────────────────────────


def test_auto_best_section_no_lyric_lines_is_backward_compatible() -> None:
    """When lyric_lines is None or empty, scoring collapses to pure beat density.

    Locks the backward-compat guarantee: every pre-Layer-3 caller (which never
    passed lyric_lines) gets identical (best_start, best_end) tuples.
    """
    sparse = [float(i) for i in range(0, 30, 4)]
    dense = [float(i) for i in range(60, 90, 1)]
    beats = sparse + dense

    s_none, e_none = auto_best_section(beats, window_s=30.0, track_duration_s=120.0)
    s_empty, e_empty = auto_best_section(
        beats, window_s=30.0, track_duration_s=120.0, lyric_lines=[]
    )
    # Pre-Layer-3 reference (call without the new kwarg)
    s_ref, e_ref = auto_best_section(beats, window_s=30.0, track_duration_s=120.0)

    assert (s_none, e_none) == (s_ref, e_ref)
    assert (s_empty, e_empty) == (s_ref, e_ref)


def test_auto_best_section_lyric_aware_breaks_tie_in_favor_of_vocal_window() -> None:
    """Two near-equal beat windows: lyric weight tips toward the vocal one.

    Pre-Layer-3 picked the densest beat window even when it sat over a pure
    instrumental peak with no vocals (dc33d047). With lyric-aware scoring,
    a beat-tie + lyric-rich window wins.
    """
    # Two equally-dense beat clusters: 0-30s and 60-90s, each with 30 beats.
    cluster_a = [float(i) for i in range(0, 30)]
    cluster_b = [float(i) for i in range(60, 90)]
    beats = cluster_a + cluster_b

    # Lyrics live entirely inside cluster B (sung verse section).
    lyric_lines = [
        {"text": "line one", "start_s": 62.0, "end_s": 65.0},
        {"text": "line two", "start_s": 66.0, "end_s": 69.0},
        {"text": "line three", "start_s": 72.0, "end_s": 75.0},
    ]
    start, _ = auto_best_section(
        beats, window_s=30.0, track_duration_s=120.0, lyric_lines=lyric_lines
    )
    # Without lyrics, either cluster could win (tie); with lyrics, cluster B
    # (60-89s) wins because 3 lines × 0.5 weight = +1.5 score.
    assert start >= 55.0, f"expected lyric-rich cluster B start near 60s, got {start}"


def test_auto_best_section_lyric_weight_zero_collapses_to_beat_only() -> None:
    """lyric_weight=0 explicitly disables the contribution (same as None)."""
    cluster_a = [float(i) for i in range(0, 30)]
    cluster_b = [float(i) for i in range(60, 90)]
    beats = cluster_a + cluster_b
    lyric_lines = [{"text": "in B", "start_s": 70.0, "end_s": 75.0}]

    s_weighted, _ = auto_best_section(
        beats,
        window_s=30.0,
        track_duration_s=120.0,
        lyric_lines=lyric_lines,
        lyric_weight=0.0,
    )
    s_unweighted, _ = auto_best_section(beats, window_s=30.0, track_duration_s=120.0)
    # Both calls must pick the same window — the lyric-tie-break is disabled.
    assert s_weighted == s_unweighted


def test_auto_best_section_beat_dominance_still_wins_over_lyric_weight() -> None:
    """A 10-extra-beat cluster with zero lyrics still beats a sparse vocal verse.

    Sanity check that lyric_weight=0.5 doesn't let a single lyric line override
    a substantially beat-denser window. Prevents pathological cases where a
    tiny vocal sample drowns out a clear chorus.
    """
    # Cluster A: 40 beats, no lyrics (chorus / drop)
    cluster_a = [float(i) * 0.5 for i in range(0, 40)]
    # Cluster B: 5 beats, with one lyric line (sparse verse)
    cluster_b = [60.0, 65.0, 70.0, 75.0, 80.0]
    beats = cluster_a + cluster_b
    lyric_lines = [{"text": "lone line", "start_s": 65.0, "end_s": 70.0}]

    start, _ = auto_best_section(
        beats, window_s=20.0, track_duration_s=120.0, lyric_lines=lyric_lines
    )
    # Cluster A (0-20s, 40 beats, score 40) beats cluster B
    # (60-80s, 5 beats + 1 line × 0.5 = 5.5 score). Lyric weight doesn't
    # overpower a clear beat-density signal.
    assert start < 30.0, f"expected beat-dense cluster A near 0s, got {start}"


def test_auto_best_section_malformed_lyric_rows_are_skipped() -> None:
    """Defensive: malformed JSONB rows (missing keys, non-numeric values) skip.

    `MusicTrack.lyrics_cached` is JSONB and can drift across schema versions or
    pick up bad migrations. The scorer must not crash on a single bad row.
    """
    beats = [float(i) for i in range(0, 30)]
    lyric_lines = [
        {"text": "good", "start_s": 5.0, "end_s": 8.0},
        {"text": "no times"},  # missing start_s / end_s — keys default to 0.0 → le==ls → skip
        {"text": "bad string", "start_s": "x", "end_s": 8.0},  # TypeError on float()
        {"text": "inverted", "start_s": 20.0, "end_s": 10.0},  # le < ls — skip
    ]
    start, end = auto_best_section(
        beats, window_s=15.0, track_duration_s=60.0, lyric_lines=lyric_lines
    )
    # Should not raise; should still pick a sensible window.
    assert 0.0 <= start
    assert end > start


# ── count_slots ───────────────────────────────────────────────────────────────


def test_count_slots_empty_beats() -> None:
    assert count_slots([], 0.0, 30.0, 8) == 0


def test_count_slots_fewer_beats_than_n_returns_zero() -> None:
    # The Marea (Fred Again) prod incident: 5 beats in [156.6, 170.0] with n=8
    # produced 0 slots, but the admin PATCH endpoint had no guard so the job
    # failed at run time instead of at config save time.
    marea_beats = [159.474, 165.447, 165.895, 167.026, 169.799]
    assert count_slots(marea_beats, 156.6, 170.0, 8) == 0
    # And with n=4 (the admin's failed remediation attempt) it's still
    # too narrow to be useful — 1 slot is the math, but the validator
    # still accepts >=1; that's a UX problem, not a 0-slot bug.
    assert count_slots(marea_beats, 156.6, 170.0, 4) == 1


def test_count_slots_equal_to_n_returns_zero_boundary() -> None:
    # range(0, len-n, n) where len==n gives range(0, 0, n) = empty.
    # Boundary case the original arithmetic gets right by luck.
    beats = [float(i) for i in range(8)]
    assert count_slots(beats, 0.0, 8.0, 8) == 0


def test_count_slots_just_above_n_returns_one() -> None:
    beats = [float(i) for i in range(9)]
    assert count_slots(beats, 0.0, 9.0, 8) == 1


def test_count_slots_matches_range_arithmetic() -> None:
    beats = [float(i) for i in range(0, 40)]
    for n in (2, 4, 8, 12):
        window_beats = [b for b in beats if 0.0 <= b <= 39.0]
        expected = max(0, len(range(0, max(0, len(window_beats) - n), n)))
        assert count_slots(beats, 0.0, 39.0, n) == expected, f"n={n}"


def test_count_slots_window_outside_beats_returns_zero() -> None:
    beats = [1.0, 2.0, 3.0]
    assert count_slots(beats, 100.0, 200.0, 4) == 0


def test_count_slots_agrees_with_generate_music_recipe_when_positive() -> None:
    # When count_slots > 0, generate_music_recipe should produce exactly
    # that many slots. Single source of truth.
    beats = [float(i) * 0.5 for i in range(40)]  # 0.0 .. 19.5 step 0.5
    n = 4
    expected = count_slots(beats, 0.0, 19.5, n)
    assert expected > 0
    recipe = generate_music_recipe(
        {
            "beat_timestamps_s": beats,
            "track_config": {
                "best_start_s": 0.0,
                "best_end_s": 19.5,
                "slot_every_n_beats": n,
            },
            "duration_s": 30.0,
        }
    )
    assert recipe["shot_count"] == expected


def test_count_slots_agrees_with_generate_music_recipe_when_zero() -> None:
    # When count_slots == 0, generate_music_recipe must raise.
    beats = [1.0, 2.0, 3.0]
    assert count_slots(beats, 0.0, 10.0, 8) == 0
    with pytest.raises(ValueError, match="0 slots"):
        generate_music_recipe(
            {
                "beat_timestamps_s": beats,
                "track_config": {
                    "best_start_s": 0.0,
                    "best_end_s": 10.0,
                    "slot_every_n_beats": 8,
                },
                "duration_s": 30.0,
            }
        )


# ── generate_music_recipe ─────────────────────────────────────────────────────


def _make_track_data(
    beats: list[float],
    best_start: float = 0.0,
    best_end: float = 30.0,
    slot_every_n: int = 8,
    duration_s: float = 60.0,
) -> dict:
    return {
        "beat_timestamps_s": beats,
        "track_config": {
            "best_start_s": best_start,
            "best_end_s": best_end,
            "slot_every_n_beats": slot_every_n,
        },
        "duration_s": duration_s,
    }


def test_generate_music_recipe_basic() -> None:
    # 17 beats at 0.5s intervals = 8.5s range, every 4 beats → 2 slots
    beats = [i * 0.5 for i in range(17)]
    data = _make_track_data(beats, best_start=0.0, best_end=8.5, slot_every_n=4)
    recipe = generate_music_recipe(data)

    assert recipe["shot_count"] >= 2
    assert len(recipe["slots"]) == recipe["shot_count"]
    for slot in recipe["slots"]:
        assert slot["target_duration_s"] > 0
        assert slot["slot_type"] == "broll"


def test_generate_music_recipe_empty_beats() -> None:
    data = _make_track_data([], best_start=0.0, best_end=30.0)
    with pytest.raises(ValueError, match="0 slots"):
        generate_music_recipe(data)


def test_generate_music_recipe_window_boundaries_respected() -> None:
    # Beats from 0–60s; window is 30–50s
    beats = [float(i) for i in range(0, 60)]
    data = _make_track_data(beats, best_start=30.0, best_end=50.0, slot_every_n=4)
    recipe = generate_music_recipe(data)

    # All beat timestamps in recipe should be relative to start (≥0)
    for b in recipe["beat_timestamps_s"]:
        assert b >= 0.0, f"Beat {b} is before start"
    # Total duration ≈ window length
    assert recipe["total_duration_s"] == pytest.approx(20.0, abs=1.0)


def test_generate_music_recipe_slot_positions_sequential() -> None:
    beats = [float(i) for i in range(0, 40)]
    data = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=8)
    recipe = generate_music_recipe(data)

    positions = [s["position"] for s in recipe["slots"]]
    assert positions == list(range(1, len(positions) + 1))


def test_generate_music_recipe_required_clips_derived() -> None:
    beats = [float(i) for i in range(0, 40)]
    data = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=4)
    recipe = generate_music_recipe(data)

    n_slots = recipe["shot_count"]
    assert recipe["required_clips_min"] >= 1
    assert recipe["required_clips_max"] >= recipe["required_clips_min"]
    assert recipe["required_clips_max"] <= n_slots


def test_generate_music_recipe_too_few_beats_for_slots() -> None:
    # Only 3 beats, slot_every_n=8 → impossible → 0 slots
    beats = [1.0, 2.0, 3.0]
    data = _make_track_data(beats, best_start=0.0, best_end=10.0, slot_every_n=8)
    with pytest.raises(ValueError, match="0 slots"):
        generate_music_recipe(data)


def test_generate_music_recipe_sync_style() -> None:
    beats = [float(i) for i in range(0, 40)]
    data = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=4)
    recipe = generate_music_recipe(data)
    assert recipe["sync_style"] == "cut-on-beat"


# ── Narrative payoff duration weighting ───────────────────────────────────────


def test_generate_music_recipe_guide_weights_target_durations() -> None:
    # 4 slots from uniform beats; guide says [2s, 2s, 2s, 8s] → payoff last.
    beats = [float(i) for i in range(0, 36)]  # 35 beats, n=8 → 4 slots
    data = _make_track_data(beats, best_start=0.0, best_end=35.0, slot_every_n=8)
    guide = [
        {"what": "intro", "duration_s": 2},
        {"what": "setup", "duration_s": 2},
        {"what": "rising", "duration_s": 2},
        {"what": "payoff", "duration_s": 8},
    ]
    recipe = generate_music_recipe(data, filming_guide=guide)

    slots = recipe["slots"]
    assert len(slots) == 4
    # Payoff slot (last) should have the largest target_duration_s
    target_durs = [s["target_duration_s"] for s in slots]
    assert target_durs[3] > target_durs[0]
    assert target_durs[3] > target_durs[1]
    # Roughly 4:1 ratio (8/(2+2+2+8) vs 2/(14) → 4× larger)
    assert target_durs[3] / target_durs[0] > 3.0


def test_generate_music_recipe_guide_total_duration_preserved() -> None:
    # The guide weighting must not change the recipe's total_duration_s.
    beats = [float(i) for i in range(0, 36)]
    data = _make_track_data(beats, best_start=0.0, best_end=35.0, slot_every_n=8)
    guide = [
        {"what": "a", "duration_s": 3},
        {"what": "b", "duration_s": 5},
        {"what": "c", "duration_s": 1},
        {"what": "d", "duration_s": 6},
    ]
    recipe_with = generate_music_recipe(data, filming_guide=guide)
    recipe_without = generate_music_recipe(data)

    # Total target sum may differ slightly due to per-slot rounding, but
    # the recipe-level total_duration_s must stay the same (beat-derived).
    assert recipe_with["total_duration_s"] == recipe_without["total_duration_s"]


def test_generate_music_recipe_guide_too_short_falls_back_to_uniform() -> None:
    # Guide has fewer entries than slots → uniform beat split preserved.
    beats = [float(i) for i in range(0, 36)]  # 4 slots
    data = _make_track_data(beats, best_start=0.0, best_end=35.0, slot_every_n=8)
    guide = [{"what": "hook", "duration_s": 10}]  # only 1 entry, need 4
    recipe_with = generate_music_recipe(data, filming_guide=guide)
    recipe_without = generate_music_recipe(data)

    for s_with, s_without in zip(recipe_with["slots"], recipe_without["slots"]):
        assert s_with["target_duration_s"] == s_without["target_duration_s"]


def test_generate_music_recipe_no_guide_is_backward_compatible() -> None:
    # Omitting filming_guide must produce identical output to pre-feature behavior.
    beats = [float(i) for i in range(0, 36)]
    data = _make_track_data(beats, best_start=0.0, best_end=35.0, slot_every_n=8)
    recipe_explicit = generate_music_recipe(data, filming_guide=None)
    recipe_omitted = generate_music_recipe(data)

    assert recipe_explicit["slots"] == recipe_omitted["slots"]
    assert recipe_explicit["total_duration_s"] == recipe_omitted["total_duration_s"]


# ── merge_template_with_track ────────────────────────────────────────────────


def _make_parent_recipe(
    n_slots: int = 8,
    slot_duration: float = 3.0,
    with_overlays: bool = True,
) -> dict:
    """Build a minimal parent recipe with N slots."""
    slots = []
    for i in range(n_slots):
        slot: dict = {
            "position": i + 1,
            "target_duration_s": slot_duration,
            "slot_type": "hook" if i == 0 else "broll",
            "transition_in": "whip-pan" if i % 2 == 0 else "dissolve",
            "color_hint": "warm",
            "speed_factor": 1.2,
            "energy": 5.0,
            "text_overlays": [],
        }
        if with_overlays:
            slot["text_overlays"] = [
                {
                    "role": "hook",
                    "text": f"Overlay {i + 1}",
                    "start_s": 0.0,
                    "end_s": slot_duration,
                    "position": "center",
                    "effect": "fade-in",
                    "font_style": "sans",
                    "text_size": "medium",
                    "text_color": "#FFFFFF",
                }
            ]
        slots.append(slot)

    return {
        "shot_count": n_slots,
        "total_duration_s": n_slots * slot_duration,
        "slots": slots,
        "copy_tone": "cinematic",
        "caption_style": "bold",
        "creative_direction": "luxury travel",
        "color_grade": "warm",
        "transition_style": "whip-pan",
        "interstitials": [
            {"type": "curtain-close", "after_slot": 4, "hold_s": 0.5, "hold_color": "#000000"}
        ],
        "sync_style": "freeform",
        "pacing_style": "moderate",
        "beat_timestamps_s": [],
    }


def test_merge_8_parent_12_music_slots() -> None:
    """8 parent slots + 12 beat slots → 12 merged slots with visual props."""
    parent = _make_parent_recipe(n_slots=8)
    # 50 beats → every 4 → 12 slots (range 0..48, steps of 4 = 12 groups)
    beats = [float(i) for i in range(50)]
    track = _make_track_data(beats, best_start=0.0, best_end=49.0, slot_every_n=4)

    merged = merge_template_with_track(parent, track)

    assert len(merged["slots"]) == 12
    # All slots should have visual properties from parent
    for slot in merged["slots"]:
        assert slot["transition_in"] in ("whip-pan", "dissolve")
        assert slot["color_hint"] == "warm"
        assert slot["speed_factor"] == 1.2
    # Beat-sync overrides
    assert merged["sync_style"] == "cut-on-beat"
    assert merged["pacing_style"] == "fast"
    # Top-level fields from parent
    assert merged["copy_tone"] == "cinematic"
    assert merged["creative_direction"] == "luxury travel"


def test_merge_1_parent_slot_minimal() -> None:
    """Merge with a single parent slot — all music slots inherit from it."""
    parent = _make_parent_recipe(n_slots=1, slot_duration=10.0)
    beats = [float(i) for i in range(20)]
    track = _make_track_data(beats, best_start=0.0, best_end=19.0, slot_every_n=4)

    merged = merge_template_with_track(parent, track)

    assert len(merged["slots"]) >= 1
    for slot in merged["slots"]:
        assert slot["slot_type"] == "hook"  # all inherit from slot 0


def test_merge_0_beats_raises() -> None:
    """Merge with 0 beats → ValueError (from generate_music_recipe)."""
    parent = _make_parent_recipe(n_slots=4)
    track = _make_track_data([], best_start=0.0, best_end=30.0)

    with pytest.raises(ValueError, match="0 slots"):
        merge_template_with_track(parent, track)


def test_merge_interstitial_remapping() -> None:
    """Interstitial after_slot indices are proportionally remapped."""
    parent = _make_parent_recipe(n_slots=4)
    parent["interstitials"] = [
        {"type": "curtain-close", "after_slot": 2, "hold_s": 0.5, "hold_color": "#000000"}
    ]
    beats = [float(i) for i in range(40)]
    track = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=4)

    merged = merge_template_with_track(parent, track)
    n_music = len(merged["slots"])

    assert len(merged["interstitials"]) == 1
    remapped = merged["interstitials"][0]["after_slot"]
    # after_slot=2 in 4 parent slots → proportional in N music slots
    expected = max(1, min(round(2 * n_music / 4), n_music))
    assert remapped == expected


def test_merge_text_overlay_timing_scaling() -> None:
    """Text overlay timing is proportionally scaled to new slot duration."""
    parent = _make_parent_recipe(n_slots=4, slot_duration=6.0)
    # Parent slot 0 overlay: start=0, end=6 (full duration)
    beats = [float(i) for i in range(20)]
    track = _make_track_data(beats, best_start=0.0, best_end=19.0, slot_every_n=4)

    merged = merge_template_with_track(parent, track)

    # First music slot should have scaled overlay
    first_slot = merged["slots"][0]
    assert len(first_slot["text_overlays"]) > 0
    ov = first_slot["text_overlays"][0]
    # Overlay should span the full new slot duration (0 to target_duration_s)
    assert ov["start_s"] == pytest.approx(0.0, abs=0.01)
    assert ov["end_s"] == pytest.approx(first_slot["target_duration_s"], abs=0.1)


def test_merge_parent_no_overlays() -> None:
    """Merge works cleanly when parent has no text overlays."""
    parent = _make_parent_recipe(n_slots=4, with_overlays=False)
    beats = [float(i) for i in range(20)]
    track = _make_track_data(beats, best_start=0.0, best_end=19.0, slot_every_n=4)

    merged = merge_template_with_track(parent, track)

    for slot in merged["slots"]:
        assert slot["text_overlays"] == []


def test_merge_custom_track_config() -> None:
    """Merge respects custom best_start_s/best_end_s from track config."""
    parent = _make_parent_recipe(n_slots=4)
    beats = [float(i) for i in range(0, 100)]
    # Only use beats 50-80
    track = _make_track_data(beats, best_start=50.0, best_end=80.0, slot_every_n=4)

    merged = merge_template_with_track(parent, track)

    # total_duration_s should be ~30s (80-50)
    assert merged["total_duration_s"] == pytest.approx(30.0, abs=1.0)
    # All beat timestamps should be relative to start (≥0)
    for b in merged["beat_timestamps_s"]:
        assert b >= 0.0


# ── merge_audio_recipe ───────────────────────────────────────────────────────

from app.pipeline.music_recipe import merge_audio_recipe  # noqa: E402


class TestMergeAudioRecipe:
    def test_proportional_mapping_n_beat_m_gemini(self):
        """N beat slots + M Gemini slots → N merged slots with Gemini visuals."""
        beat_recipe = {
            "shot_count": 4,
            "total_duration_s": 16.0,
            "slots": [
                {
                    "position": i + 1,
                    "target_duration_s": 4.0,
                    "slot_type": "broll",
                    "energy": 5.0,
                    "priority": 5,
                    "text_overlays": [],
                    "transition_in": "cut",
                    "speed_factor": 1.0,
                }
                for i in range(4)
            ],
            "beat_timestamps_s": [0.0, 4.0, 8.0, 12.0],
            "sync_style": "cut-on-beat",
            "pacing_style": "fast",
            "color_grade": "none",
            "transition_style": "cut",
            "copy_tone": "energetic",
            "caption_style": "none",
            "creative_direction": "beat-sync",
            "interstitials": [],
        }
        gemini_recipe = {
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 8.0,
                    "slot_type": "hook",
                    "transition_in": "whip-pan",
                    "color_hint": "warm",
                    "speed_factor": 0.8,
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 8.0,
                    "slot_type": "broll",
                    "transition_in": "dissolve",
                    "color_hint": "cool",
                    "speed_factor": 1.2,
                    "text_overlays": [],
                },
            ],
            "color_grade": "warm",
            "transition_style": "whip-pans on drops",
            "pacing_style": "fast-paced",
            "copy_tone": "bold",
            "creative_direction": "Energetic music video",
            "caption_style": "bold overlay",
            "subject_niche": "pop",
            "interstitials": [],
        }

        merged = merge_audio_recipe(beat_recipe, gemini_recipe)

        # Should have 4 slots (beat count preserved)
        assert len(merged["slots"]) == 4
        # Beat timing preserved
        assert merged["slots"][0]["target_duration_s"] == 4.0
        assert merged["slots"][3]["target_duration_s"] == 4.0
        # Gemini visuals applied (slots 0,1 map to Gemini slot 0; slots 2,3 map to Gemini slot 1)
        assert merged["slots"][0]["transition_in"] == "whip-pan"
        assert merged["slots"][0]["color_hint"] == "warm"
        assert merged["slots"][2]["transition_in"] == "dissolve"
        assert merged["slots"][2]["color_hint"] == "cool"
        # Top-level fields from Gemini
        assert merged["color_grade"] == "warm"
        assert merged["copy_tone"] == "bold"
        assert merged["creative_direction"] == "Energetic music video"

    def test_gemini_zero_slots_returns_beat_only(self):
        """When Gemini returns 0 slots, beat recipe is returned unchanged."""
        beat_recipe = {
            "shot_count": 2,
            "total_duration_s": 8.0,
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 4.0,
                    "slot_type": "broll",
                    "transition_in": "cut",
                    "text_overlays": [],
                },
                {
                    "position": 2,
                    "target_duration_s": 4.0,
                    "slot_type": "broll",
                    "transition_in": "cut",
                    "text_overlays": [],
                },
            ],
            "color_grade": "none",
        }
        gemini_recipe = {"slots": [], "color_grade": "warm"}

        merged = merge_audio_recipe(beat_recipe, gemini_recipe)

        # No Gemini visuals applied
        assert merged["slots"][0]["transition_in"] == "cut"
        assert merged["color_grade"] == "none"  # unchanged


def test_music_recipe_dict_constructs_template_recipe() -> None:
    """Regression: generate_music_recipe()'s output must be splat-safe into
    TemplateRecipe via build_recipe(). Previously failed with
    `TemplateRecipe.__init__() got an unexpected keyword argument 'required_clips_min'`
    because the music orchestrator splatted the dict directly without stripping
    routing/validation-only keys (admin endpoints still need those keys on the dict).
    """
    from app.pipeline.agents.gemini_analyzer import TemplateRecipe, build_recipe

    data = {
        # 40 beats at 0.5s intervals → 0.0s..19.5s
        "beat_timestamps_s": [i * 0.5 for i in range(40)],
        "track_config": {
            "best_start_s": 0.0,
            "best_end_s": 16.0,
            "slot_every_n_beats": 8,
        },
        "duration_s": 20.0,
    }
    recipe_dict = generate_music_recipe(data)

    # The dict MUST keep the routing/validation keys — admin endpoints
    # (admin.py:1562, admin.py:2000) and POST /music-jobs (music_jobs.py:133)
    # read them back from recipe_cached / track_config.
    assert "required_clips_min" in recipe_dict
    assert "required_clips_max" in recipe_dict

    # Document the OLD broken path: splatting the dict directly raises,
    # which is exactly what _run_music_job hit in prod before this fix.
    # Pins the regression so reverting to raw `TemplateRecipe(**recipe_dict)`
    # breaks the test instead of breaking music jobs silently.
    with pytest.raises(TypeError, match="required_clips_min"):
        TemplateRecipe(**recipe_dict)

    # The shared helper strips those keys so the dataclass constructs cleanly.
    recipe = build_recipe(recipe_dict)
    assert isinstance(recipe, TemplateRecipe)
    assert recipe.shot_count == len(recipe_dict["slots"])
    assert recipe.shot_count > 0
