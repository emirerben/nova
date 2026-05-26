"""Unit tests for app/services/music_sections.py.

Pure-function tests — no DB, no Gemini, no Celery. These pin the
"rank-1 is canonical" contract that ``analyze_music_track_task`` and
the backfill script both depend on.
"""

from __future__ import annotations

from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
from app.pipeline.music_recipe import count_slots
from app.services.music_sections import (
    rank_one_bounds_from_sections,
    reconcile_track_config_to_rank_one,
    refresh_recipe_cached_for_bounds,
)

# ── rank_one_bounds_from_sections ─────────────────────────────────────────


def test_rank_one_bounds_from_valid_sections() -> None:
    sections = [
        {"rank": 1, "start_s": 60.0, "end_s": 78.0},
        {"rank": 2, "start_s": 95.0, "end_s": 113.0},
    ]
    bounds = rank_one_bounds_from_sections(sections, CURRENT_SECTION_VERSION)
    assert bounds == (60.0, 78.0)


def test_rank_one_bounds_rejects_stale_version() -> None:
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 78.0}]
    assert rank_one_bounds_from_sections(sections, "stale-version") is None


def test_rank_one_bounds_rejects_empty_list() -> None:
    assert rank_one_bounds_from_sections([], CURRENT_SECTION_VERSION) is None


def test_rank_one_bounds_rejects_none() -> None:
    assert rank_one_bounds_from_sections(None, CURRENT_SECTION_VERSION) is None


def test_rank_one_bounds_clamps_overlong_end() -> None:
    """end_s > start_s + MAX_SECTION_DURATION_S (20s) gets clamped."""
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 999.0}]
    bounds = rank_one_bounds_from_sections(sections, CURRENT_SECTION_VERSION)
    assert bounds is not None
    start_s, end_s = bounds
    assert start_s == 60.0
    assert end_s == 80.0  # 60 + MAX_SECTION_DURATION_S


def test_rank_one_bounds_rejects_malformed_floats() -> None:
    sections = [{"rank": 1, "start_s": "bad", "end_s": 78.0}]
    assert rank_one_bounds_from_sections(sections, CURRENT_SECTION_VERSION) is None


# ── reconcile_track_config_to_rank_one ────────────────────────────────────


def _beats_between(start: float, end: float, step: float) -> list[float]:
    """Generate evenly-spaced beat timestamps between [start, end]."""
    out: list[float] = []
    t = start
    while t <= end:
        out.append(round(t, 3))
        t += step
    return out


def test_reconcile_promotes_rank_one_and_recomputes_clips() -> None:
    """rank-1 bounds replace legacy 45s window; required_clips reflect the new window."""
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 80.0}]
    beats = _beats_between(0.0, 180.0, 0.5)  # 360 beats across track
    cfg = {
        "best_start_s": 100.0,
        "best_end_s": 145.0,  # legacy 45s
        "slot_every_n_beats": 8,
        "required_clips_min": 11,
        "required_clips_max": 22,
    }

    new_cfg, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=sections,
        section_version=CURRENT_SECTION_VERSION,
    )

    assert source == "song_sections"
    assert new_cfg["best_start_s"] == 60.0
    assert new_cfg["best_end_s"] == 80.0
    # 20s window @ 0.5s/beat = 41 beats, slot_every_n_beats=8 → 5 slots
    # required_clips_min = floor(5/2) = 2, required_clips_max = 5
    assert new_cfg["required_clips_min"] == 2
    assert new_cfg["required_clips_max"] == 5
    # Unrelated fields preserved
    assert new_cfg["slot_every_n_beats"] == 8


def test_reconcile_keeps_legacy_when_sections_missing() -> None:
    cfg = {"best_start_s": 100.0, "best_end_s": 145.0, "slot_every_n_beats": 8}
    beats = _beats_between(0.0, 180.0, 0.5)

    new_cfg, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=None,
        section_version=CURRENT_SECTION_VERSION,
    )

    assert source == "auto_best_section"
    assert new_cfg == cfg


def test_reconcile_keeps_legacy_when_version_stale() -> None:
    cfg = {"best_start_s": 100.0, "best_end_s": 145.0, "slot_every_n_beats": 8}
    beats = _beats_between(0.0, 180.0, 0.5)
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 80.0}]

    new_cfg, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=sections,
        section_version="stale-version",
    )

    assert source == "auto_best_section"
    assert new_cfg["best_start_s"] == 100.0


def test_reconcile_does_not_mutate_input() -> None:
    cfg = {"best_start_s": 100.0, "best_end_s": 145.0, "slot_every_n_beats": 8}
    cfg_before = dict(cfg)
    beats = _beats_between(0.0, 180.0, 0.5)
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 80.0}]

    reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=sections,
        section_version=CURRENT_SECTION_VERSION,
    )

    assert cfg == cfg_before


def test_reconcile_tolerates_none_slot_every_n_beats() -> None:
    """Legacy DB rows may have ``slot_every_n_beats: null``. ``int(None)``
    crashes; this asserts the defensive ``or 8`` fallback. Backfill rows
    that pre-date the post-fix analyze task can hit this path.
    """
    cfg = {"best_start_s": 0.0, "best_end_s": 0.0, "slot_every_n_beats": None}
    beats = _beats_between(0.0, 180.0, 0.5)
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 80.0}]

    new_cfg, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=sections,
        section_version=CURRENT_SECTION_VERSION,
    )

    # Falls back to the default 8 beats/slot — promotion succeeds, not crashes.
    assert source == "song_sections"
    assert new_cfg["best_start_s"] == 60.0
    assert new_cfg["required_clips_max"] >= 1


def test_reconcile_keeps_legacy_when_section_too_narrow_for_slots() -> None:
    """A section narrower than slot_every_n_beats * beat_interval yields 0 slots.

    Promoting in that case would store a window that makes
    ``generate_music_recipe`` raise ``ValueError`` at every job. Keep the
    legacy 45s window instead and surface via source="auto_best_section".
    """
    cfg = {"best_start_s": 100.0, "best_end_s": 145.0, "slot_every_n_beats": 8}
    # Only 3 beats inside the rank-1 window — not enough for 8/slot.
    beats = [1.0, 2.0, 3.0, 4.0, 60.1, 60.5, 61.0]
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 62.0}]

    new_cfg, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats,
        sections=sections,
        section_version=CURRENT_SECTION_VERSION,
    )

    assert source == "auto_best_section"
    assert new_cfg["best_start_s"] == 100.0


def test_reconcile_decision_matches_count_slots_at_boundary() -> None:
    """Reconcile's fallback decision MUST agree with `count_slots`.

    Pins the shared-helper invariant: the analyzer guard
    (music_orchestrate.py), the admin PATCH validator (admin_music.py),
    and reconcile_track_config_to_rank_one all derive the same arithmetic
    from `app.pipeline.music_recipe.count_slots`. If any of them ever
    drifts, this test catches it.
    """
    cfg = {"best_start_s": 0.0, "best_end_s": 45.0, "slot_every_n_beats": 8}
    # Boundary case 1: rank-1 with exactly enough beats for 1 slot (9 beats, n=8).
    # count_slots == 1 → reconcile must promote.
    beats_promote = [60.0 + i * 0.5 for i in range(9)]  # 9 beats in [60, 64]
    sections = [{"rank": 1, "start_s": 60.0, "end_s": 64.0}]
    assert count_slots(beats_promote, 60.0, 64.0, 8) == 1
    _, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats_promote,
        sections=sections,
        section_version=CURRENT_SECTION_VERSION,
    )
    assert source == "song_sections", "rank-1 with count_slots==1 must promote"

    # Boundary case 2: rank-1 with exactly n beats (8 beats, n=8) — count_slots == 0.
    # Reconcile must fall back to keep the legacy window.
    beats_fallback = [60.0 + i * 0.5 for i in range(8)]
    assert count_slots(beats_fallback, 60.0, 64.0, 8) == 0
    _, source = reconcile_track_config_to_rank_one(
        track_config=cfg,
        beats=beats_fallback,
        sections=sections,
        section_version=CURRENT_SECTION_VERSION,
    )
    assert source == "auto_best_section", "rank-1 with count_slots==0 must fall back"


# ── refresh_recipe_cached_for_bounds ─────────────────────────────────────


def test_refresh_recipe_cached_rebuilds_against_new_window() -> None:
    """The refreshed cache has slots timed against the new window, not the old."""
    beats = _beats_between(0.0, 180.0, 0.5)
    new_cfg = {
        "best_start_s": 60.0,
        "best_end_s": 80.0,
        "slot_every_n_beats": 8,
    }
    # Old cached recipe (45s window, visual fields we want to preserve).
    old_cached = {
        "shot_count": 11,
        "total_duration_s": 45.0,
        "slots": [
            {
                "position": i + 1,
                "target_duration_s": 4.0,
                "slot_type": "broll",
                "transition_in": "whip-pan",
                "color_hint": "warm",
                "text_overlays": [],
                "speed_factor": 1.0,
                "energy": 5.0,
                "priority": 5,
            }
            for i in range(11)
        ],
        "color_grade": "warm",
        "transition_style": "whip-pans",
        "creative_direction": "energetic",
        "copy_tone": "playful",
    }

    refreshed = refresh_recipe_cached_for_bounds(
        recipe_cached=old_cached,
        beats=beats,
        track_config=new_cfg,
        duration_s=180.0,
    )

    assert refreshed["total_duration_s"] == 20.0  # new window length
    assert len(refreshed["slots"]) < 11  # shorter window → fewer slots
    # Visual fields preserved from the cached recipe
    assert refreshed["color_grade"] == "warm"
    assert refreshed["transition_style"] == "whip-pans"
    # First slot inherits visuals from the proportionally-mapped old slot
    assert refreshed["slots"][0]["transition_in"] == "whip-pan"
