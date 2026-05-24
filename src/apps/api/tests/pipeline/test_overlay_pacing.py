"""Unit tests for the slot overlay pacing pass (legibility floor + redistribute).

Locks the prod 89cde014 fixes: a cumulative reveal crammed below the readable
floor gets expanded and the slowest phrases get compressed to keep the slot
duration fixed; a near-zero singleton becomes a visible window; and well-paced
slots / agentic pct overlays are left untouched.
"""

from __future__ import annotations

from app.pipeline.overlay_pacing import (
    MIN_PER_WORD_S,
    MIN_SINGLETON_OVERLAY_S,
    _eff_end,
    _eff_start,
    normalize_slot_overlay_pacing,
)


def _stage(text: str, start_s: float, end_s: float, suffix: str | None) -> dict:
    """A cumulative reveal stage overlay dict (mirrors the recipe shape)."""
    d = {"sample_text": text, "start_s": start_s, "end_s": end_s, "position": "center"}
    if suffix is not None:
        d["pop_animated_suffix"] = suffix
    return d


def _crammed_phrase(start: float) -> list[dict]:
    """ "the work to get there." — 4 stages crammed at ~86 ms (the bug)."""
    return [
        _stage("the work", start + 0.000, start + 0.086, "work"),
        _stage("the work to", start + 0.086, start + 0.172, "to"),
        _stage("the work to get", start + 0.172, start + 0.258, "get"),
        _stage("the work to get there.", start + 0.258, start + 0.430, "there."),
    ]


def test_crammed_phrase_expands_to_floor_and_preserves_slot_duration() -> None:
    overlays = _crammed_phrase(0.0)
    slot_dur = 5.0
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=slot_dur)

    assert warns["stages_expanded"] >= 3
    # Every newest-word window (its own span) is now readable.
    for o in out:
        assert _eff_end(o) - _eff_start(o) >= MIN_PER_WORD_S - 1e-6
    # Nothing spills past the slot.
    assert max(_eff_end(o) for o in out) <= slot_dur + 1e-6


def test_too_fast_phrase_compresses_slow_phrase_to_fund_it() -> None:
    # A slow phrase (4 words at 1.0 s each = 4.0 s) followed by the crammed one,
    # in a slot too tight to hold both fully expanded -> the slow phrase shrinks.
    slow = [
        _stage("It's", 0.0, 1.0, "It's"),
        _stage("It's not", 1.0, 2.0, "not"),
        _stage("It's not just", 2.0, 3.0, "just"),
        _stage("It's not just luck", 3.0, 4.0, "luck"),
    ]
    overlays = slow + _crammed_phrase(4.0)
    slot_dur = 4.8  # < 4.0 (slow) + 4*0.35 (crammed) = 5.4 -> must compress
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=slot_dur)

    # Slot total is preserved (filled, not overflowed).
    assert max(_eff_end(o) for o in out) <= slot_dur + 1e-6
    # The slow phrase's first word lost time (was 1.0 s).
    assert _eff_end(out[0]) - _eff_start(out[0]) < 1.0


def test_near_zero_singleton_expands_to_visible_window() -> None:
    overlays = [_stage("and good timing so...", 10.488, 10.513, None)]
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=12.0)

    assert warns["singletons_expanded"] == 1
    assert _eff_end(out[0]) - _eff_start(out[0]) >= MIN_SINGLETON_OVERLAY_S - 1e-6


def test_well_paced_slot_is_unchanged() -> None:
    overlays = [
        _stage("It's", 0.0, 0.5, "It's"),
        _stage("It's not", 0.5, 1.0, "not"),
        _stage("It's not just", 1.0, 1.5, "just"),
        _stage("It's not just luck", 1.5, 2.5, "luck"),
    ]
    before = [dict(o) for o in overlays]
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=5.0)

    assert warns["stages_expanded"] == 0
    assert warns["singletons_expanded"] == 0
    for a, b in zip(before, out, strict=True):
        assert round(_eff_start(a), 3) == round(_eff_start(b), 3)
        assert round(_eff_end(a), 3) == round(_eff_end(b), 3)


def test_pct_timed_overlays_passed_through_untouched() -> None:
    overlays = [
        {"sample_text": "agentic", "start_pct": 0.1, "end_pct": 0.2, "position": "center"},
    ]
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=10.0)

    assert out[0]["start_pct"] == 0.1
    assert out[0]["end_pct"] == 0.2
    assert "start_s" not in out[0] or out[0].get("start_s") is None
    assert warns["stages_expanded"] == 0


def test_overflow_uncompressible_keeps_floor_and_warns() -> None:
    # 10 words need >= 3.5 s at the floor, but the slot is only 2.0 s.
    overlays = [
        _stage(" ".join(f"w{j}" for j in range(k + 1)), k * 0.05, (k + 1) * 0.05, f"w{k}")
        for k in range(10)
    ]
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=2.0)

    assert warns["slot_overflow_uncompressible"] == 1
    # The floor is honoured even though the slot can't hold everything.
    for o in out:
        assert _eff_end(o) - _eff_start(o) >= MIN_PER_WORD_S - 1e-6


def test_no_duration_expands_without_compressing() -> None:
    overlays = _crammed_phrase(0.0)
    out, warns = normalize_slot_overlay_pacing(overlays, slot_duration_s=None)

    assert warns["stages_expanded"] >= 3
    for o in out:
        assert _eff_end(o) - _eff_start(o) >= MIN_PER_WORD_S - 1e-6


def test_stages_butt_edge_to_edge_after_pass() -> None:
    out, _ = normalize_slot_overlay_pacing(_crammed_phrase(0.0), slot_duration_s=5.0)
    for a, b in zip(out[:-1], out[1:], strict=True):
        # Consecutive cumulative stages of one phrase are contiguous (no gap,
        # no overlap) so exactly one is on screen at a time.
        assert abs(_eff_end(a) - _eff_start(b)) < 1e-3


def test_empty_and_no_seconds_overlays_are_noops() -> None:
    assert normalize_slot_overlay_pacing([], slot_duration_s=5.0) == (
        [],
        {
            "overlays_pushed_past_target": 0,
            "stages_expanded": 0,
            "singletons_expanded": 0,
            "slot_overflow_uncompressible": 0,
        },
    )
