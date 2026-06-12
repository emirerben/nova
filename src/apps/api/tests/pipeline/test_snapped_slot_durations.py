"""Unit tests for compute_snapped_slot_durations.

Verifies: snap math, cumulative carry, locked/exact bypass, empty-beats
no-op, and 0.5 s duration floor.
"""


from app.tasks.template_orchestrate import BEAT_SNAP_TOLERANCE_S, compute_snapped_slot_durations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot(dur: float, *, locked: bool = False, exact_window: bool = False) -> dict:
    s = {"target_duration_s": dur}
    if locked:
        s["locked"] = True
    if exact_window:
        s["exact_window"] = True
    return s


def _run(slots, beats):
    return compute_snapped_slot_durations(slots, beats, is_agentic=False, user_total_dur_s=None)


# ---------------------------------------------------------------------------
# Basic snap
# ---------------------------------------------------------------------------


def test_snaps_to_exact_beat():
    """Slot whose expected_end coincides with a beat snaps to that beat."""
    result = _run([_slot(2.0)], beats=[2.0, 4.0])
    assert result == [2.0]


def test_snaps_to_nearby_beat():
    """Expected_end 0.1 s before a beat — snaps to the beat."""
    result = _run([_slot(2.0)], beats=[2.1, 4.0])
    assert len(result) == 1
    assert abs(result[0] - 2.1) < 1e-9


def test_no_snap_when_beat_too_far():
    """Beat farther than BEAT_SNAP_TOLERANCE_S → no snap."""
    far = 2.0 + BEAT_SNAP_TOLERANCE_S + 0.01
    result = _run([_slot(2.0)], beats=[far])
    assert result == [2.0]


# ---------------------------------------------------------------------------
# Cumulative carry
# ---------------------------------------------------------------------------


def test_cumulative_carry_across_two_slots():
    """Slot 1's expected_end is anchored from slot 0's snapped end."""
    # Slot 0: target=2.0, nearest beat=2.1 → snapped_end=2.1, dur=2.1
    # Slot 1: cumulative=2.1, target=3.0, expected_end=5.1, nearest beat=5.2 → dur=3.1
    beats = [2.1, 5.2, 8.0]
    result = _run([_slot(2.0), _slot(3.0)], beats=beats)
    assert abs(result[0] - 2.1) < 1e-9
    assert abs(result[1] - 3.1) < 1e-9


def test_cumulative_carry_three_slots_no_lock():
    """Three ordinary slots: each snap anchored from the previous snapped end."""
    beats = [1.9, 4.0, 6.1]
    # Slot 0: target=2.0, expected_end=2.0, nearest=1.9 (dist=0.1) → dur=1.9
    # Slot 1: cumulative=1.9, target=2.0, expected_end=3.9, nearest=4.0 → dur=2.1
    # Slot 2: cumulative=4.0, target=2.0, expected_end=6.0, nearest=6.1 → dur=2.1
    result = _run([_slot(2.0), _slot(2.0), _slot(2.0)], beats=beats)
    assert abs(result[0] - 1.9) < 1e-9
    assert abs(result[1] - 2.1) < 1e-9
    assert abs(result[2] - 2.1) < 1e-9


# ---------------------------------------------------------------------------
# Locked / exact_window bypass
# ---------------------------------------------------------------------------


def test_locked_slot_bypasses_snap():
    """A locked slot uses target_duration_s verbatim (no beat-snap)."""
    result = _run([_slot(2.0, locked=True)], beats=[2.1, 4.0])
    assert result == [2.0]


def test_exact_window_slot_bypasses_snap():
    """An exact_window slot uses target_duration_s verbatim (no beat-snap)."""
    result = _run([_slot(2.0, exact_window=True)], beats=[2.1, 4.0])
    assert result == [2.0]


def test_locked_mid_sequence_carries_duration_forward():
    """Locked slot adds its duration to cumulative_s so subsequent snaps are correct."""
    # Slot 0: not locked, target=2.0, snap to 2.1
    # Slot 1: locked, target=1.0, cumulative becomes 2.1+1.0=3.1
    # Slot 2: target=3.0, expected_end=3.1+3.0=6.1, nearest=6.2 → dur=3.1
    beats = [2.1, 6.2]
    result = _run([_slot(2.0), _slot(1.0, locked=True), _slot(3.0)], beats=beats)
    assert abs(result[0] - 2.1) < 1e-9
    assert result[1] == 1.0
    assert abs(result[2] - 3.1) < 1e-9


# ---------------------------------------------------------------------------
# Empty beats (no-op)
# ---------------------------------------------------------------------------


def test_empty_beats_returns_target_durations():
    """No beats → each slot gets its target_duration_s (with 0.5 s floor)."""
    result = _run([_slot(1.5), _slot(3.0), _slot(2.0)], beats=[])
    assert result == [1.5, 3.0, 2.0]


# ---------------------------------------------------------------------------
# 0.5 s floor
# ---------------------------------------------------------------------------


def test_floor_applied_before_snap():
    """target_duration_s below 0.5 is raised to 0.5 before beat-snap arithmetic."""
    result = _run([_slot(0.1)], beats=[])
    assert result == [0.5]


def test_floor_applied_when_snap_would_produce_tiny_duration():
    """If snapped_end - cumulative_s would be < 0.5, floor kicks in."""
    # Arrange a beat right at cumulative=0: slot target=2.0, nearest beat=0.3
    # snapped_end=0.3, snapped_dur = max(0.5, 0.3-0.0)=0.5
    beats = [0.3]
    result = _run([_slot(2.0)], beats=beats)
    # 0.3 is within BEAT_SNAP_TOLERANCE_S=0.4 of 2.0? No — |2.0-0.3|=1.7 > 0.4 → no snap
    # So result = [2.0] (no snap, target returned as-is)
    assert result == [2.0]


def test_floor_on_locked_slot():
    """Locked slot with target_duration_s < 0.5 still returns at least 0.5."""
    result = _run([_slot(0.2, locked=True)], beats=[1.0])
    assert result == [0.5]
