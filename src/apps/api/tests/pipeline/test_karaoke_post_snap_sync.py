"""Karaoke word-highlight sync regression after beat-snap injection fix.

The bug
-------
`inject_lyric_overlays` was called with pre-beat-snap slot durations, so each
lyric line was assigned to the slot whose PRE-snap window contained it.  After
beat-snap changed slot 0's duration from 1.8 s → 2.2 s, a line that the
injector put in slot 1 (section time 2.0 ≥ pre-snap slot-0-end 1.8) now
belongs in slot 0 (2.0 < post-snap slot-0-end 2.2).  The `lyric_word_resync`
pass in `_collect_absolute_overlays` detects the slot-1 overlay is out-of-
bounds (new_start < 0) and clamps its start to 0 — producing an absolute start
of 2.2 s instead of the correct 2.0 s: 200 ms drift.

The fix
-------
Before calling `inject_lyric_overlays`, overwrite each slot's
`target_duration_s` with the output of `compute_snapped_slot_durations` so the
injector's `_build_slot_windows` sees post-snap windows.  The line is then
assigned to slot 0 (correct slot), `lyric_word_resync` succeeds with
new_start = 2.0 − 0.0 = 2.0, and the absolute overlay start is exactly 2.0 s.

Red → green
-----------
`_pre_fix_drift_s` documents the measured drift on pre-fix wiring (≥ 200 ms).
`test_karaoke_drift_below_50ms_on_post_snap_injection` asserts the post-fix
path lands within ±50 ms.  Separately, `test_pre_fix_wiring_exceeds_50ms`
asserts the pre-fix path is RED — the test would never have been meaningful if
this assertion did not hold.
"""

from __future__ import annotations

from app.pipeline.lyric_injector import inject_lyric_overlays
from app.tasks.template_orchestrate import (
    BEAT_SNAP_TOLERANCE_S,
    _collect_absolute_overlays,
    compute_snapped_slot_durations,
)

# ---------------------------------------------------------------------------
# Fixture parameters
# ---------------------------------------------------------------------------

# Slot 0: pre-snap 1.9 s.  Beat at 2.2 s → |2.2 − 1.9| = 0.3 < tolerance (0.4) →
# snap fires.  Post-snap slot 0 = 2.2 s.
_PRE_SNAP_DUR_0 = 1.9
_BEAT = 2.2
_POST_SNAP_DUR_0 = _BEAT  # snap lands exactly on the beat

# Slot 1: no beat nearby; duration unchanged.
_DUR_1 = 2.0

# Lyric line: section-relative time 2.0 s.
# Pre-snap window of slot 0 = [0, 1.9) → 2.0 ≥ 1.9 → injected into SLOT 1.
# Post-snap window of slot 0 = [0, 2.2) → 2.0 < 2.2 → injected into SLOT 0.
_LINE_START = 2.0
_LINE_END = 2.5
_WORD_START = _LINE_START
_WORD_END = _LINE_END

_BEST_START_S = 0.0  # section-relative = song-absolute for simplicity
_BEST_END_S = _LINE_END + 1.0

_TOLERANCE_S = 0.05  # ±50 ms (plan requirement)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cache() -> dict:
    return {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "sync test",
                "start_s": _LINE_START,
                "end_s": _LINE_END,
                "words": [
                    {"text": "sync", "start_s": _WORD_START, "end_s": _WORD_END}
                ],
            }
        ],
    }


def _make_recipe(slot_0_dur: float) -> dict:
    return {
        "slots": [
            {"position": 1, "target_duration_s": slot_0_dur, "text_overlays": []},
            {"position": 2, "target_duration_s": _DUR_1, "text_overlays": []},
        ]
    }


_LYRICS_CFG = {"enabled": True, "style": "karaoke"}


def _inject_and_collect(slot_0_dur_for_injection: float) -> list[dict]:
    """Inject with the given slot-0 duration, then collect with POST-snap durations."""
    recipe = _make_recipe(slot_0_dur_for_injection)
    out = inject_lyric_overlays(
        recipe,
        _make_cache(),
        _BEST_START_S,
        _BEST_END_S,
        _LYRICS_CFG,
    )
    post_snap_durations = [_POST_SNAP_DUR_0, _DUR_1]
    steps = [{"slot": s, "clip_id": f"c{i}"} for i, s in enumerate(out["slots"])]
    return _collect_absolute_overlays(steps, post_snap_durations, None, "")


def _karaoke_overlays(abs_overlays: list[dict]) -> list[dict]:
    return [o for o in abs_overlays if o.get("effect") == "karaoke-line"]


# ---------------------------------------------------------------------------
# Verify BEAT_SNAP_TOLERANCE_S covers the fixture setup
# ---------------------------------------------------------------------------


def test_fixture_beat_is_within_snap_tolerance():
    """Guard: the fixture's beat drift must be ≤ BEAT_SNAP_TOLERANCE_S so the
    snap fires on both the helper and in _plan_slots."""
    assert abs(_BEAT - _PRE_SNAP_DUR_0) <= BEAT_SNAP_TOLERANCE_S, (
        f"Fixture beat distance {abs(_BEAT - _PRE_SNAP_DUR_0):.3f} > "
        f"BEAT_SNAP_TOLERANCE_S {BEAT_SNAP_TOLERANCE_S}. "
        "Update _PRE_SNAP_DUR_0 or _BEAT so the snap fires."
    )


def test_helper_produces_post_snap_dur_for_slot_0():
    """compute_snapped_slot_durations gives _POST_SNAP_DUR_0 for slot 0."""
    slots = [
        {"target_duration_s": _PRE_SNAP_DUR_0},
        {"target_duration_s": _DUR_1},
    ]
    result = compute_snapped_slot_durations(
        slots, [_BEAT], is_agentic=False, user_total_dur_s=None
    )
    assert abs(result[0] - _POST_SNAP_DUR_0) < 1e-9, (
        f"Helper returned {result[0]:.4f} for slot 0; expected {_POST_SNAP_DUR_0}"
    )


# ---------------------------------------------------------------------------
# Pre-fix wiring must be RED (STOP condition 5 guard)
# ---------------------------------------------------------------------------


def test_pre_fix_wiring_exceeds_50ms():
    """PRE-fix injection (using pre-snap slot-0 duration) drifts > 50 ms.

    This test MUST PASS; if it fails, the test was never measuring real drift
    and should not be shipped (STOP condition 5 in the plan).
    """
    overlays = _inject_and_collect(_PRE_SNAP_DUR_0)
    karaoke = _karaoke_overlays(overlays)

    assert len(karaoke) == 1, (
        f"Expected 1 karaoke overlay, got {len(karaoke)}; fixture may be wrong"
    )
    ov = karaoke[0]
    drift_s = abs(ov["start_s"] - _LINE_START)
    tol_ms = _TOLERANCE_S * 1000
    assert drift_s > _TOLERANCE_S, (
        f"Pre-fix drift {drift_s * 1000:.0f} ms is WITHIN tolerance ({tol_ms:.0f} ms). "
        "Pre-fix wiring is already correct — test was never red (STOP condition 5)."
    )


# ---------------------------------------------------------------------------
# Post-fix wiring must be GREEN
# ---------------------------------------------------------------------------


def test_karaoke_drift_below_50ms_on_post_snap_injection():
    """POST-fix injection (using post-snap slot-0 duration) drifts ≤ 50 ms.

    Absolute overlay start must be within ±50 ms of the song-section anchor
    for karaoke to appear in sync with the vocal.

    This is the primary regression guard.  It must be GREEN on post-fix
    wiring and RED on pre-fix wiring (verified by the companion test above).
    """
    overlays = _inject_and_collect(_POST_SNAP_DUR_0)
    karaoke = _karaoke_overlays(overlays)

    assert len(karaoke) == 1, (
        f"Expected 1 karaoke overlay after fix, got {len(karaoke)}"
    )
    ov = karaoke[0]
    drift_s = abs(ov["start_s"] - _LINE_START)
    assert drift_s <= _TOLERANCE_S, (
        f"Post-fix karaoke drift {drift_s * 1000:.0f} ms exceeds {_TOLERANCE_S * 1000:.0f} ms. "
        f"Overlay start_s={ov['start_s']:.4f}, expected ≈ {_LINE_START}. "
        "The snap injection fix is not working."
    )


# ---------------------------------------------------------------------------
# End-to-end: compute_snapped_slot_durations + inject + collect
# ---------------------------------------------------------------------------


def test_using_helper_to_seed_recipe_eliminates_drift():
    """Full integration: helper seeds recipe, then inject + collect gives ≤ 50 ms.

    Mirrors the call-site pattern:
        snapped = compute_snapped_slot_durations(slots, beats, ...)
        for i, s in enumerate(snapped):
            recipe["slots"][i]["target_duration_s"] = s
        inject_lyric_overlays(recipe, ...)
    """
    recipe = _make_recipe(_PRE_SNAP_DUR_0)
    beats = [_BEAT]

    # Apply fix: overwrite slots with snapped durations
    snapped = compute_snapped_slot_durations(
        recipe["slots"], beats, is_agentic=False, user_total_dur_s=None
    )
    for i, s in enumerate(snapped):
        recipe["slots"][i]["target_duration_s"] = s

    out = inject_lyric_overlays(
        recipe, _make_cache(), _BEST_START_S, _BEST_END_S, _LYRICS_CFG
    )
    post_snap_durations = [_POST_SNAP_DUR_0, _DUR_1]
    steps = [{"slot": s, "clip_id": f"c{i}"} for i, s in enumerate(out["slots"])]
    abs_overlays = _collect_absolute_overlays(steps, post_snap_durations, None, "")
    karaoke = _karaoke_overlays(abs_overlays)

    assert len(karaoke) == 1
    drift_s = abs(karaoke[0]["start_s"] - _LINE_START)
    assert drift_s <= _TOLERANCE_S, (
        f"End-to-end drift {drift_s * 1000:.0f} ms > {_TOLERANCE_S * 1000:.0f} ms"
    )
