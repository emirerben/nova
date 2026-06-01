"""Karaoke invariant suite.

Mirrors the Line invariant structure in `test_lyric_injector_no_stacking.py`
but locks the karaoke-specific contracts:
  - One overlay per line; word_timings array carries per-word duration_cs.
  - word_timings is strictly monotonically non-decreasing in start_s.
  - Overlay window `[start_s, end_s]` envelops every word's window.
  - section_anchor_s / section_end_anchor_s stamps are present and finite
    (otherwise the M2 resync pass is a silent no-op).
  - Post-snap re-anchor preserves song-time onset for every word within
    a ±50 ms budget when the slot is shifted by a realistic beat-snap drift.

These tests deliberately avoid running the renderer — they verify the data
contract between injector and renderer. The renderer's own contract is
covered by `test_lyric_injector_no_stacking.py` (line) and is out of scope
here.
"""

from __future__ import annotations

import math

import pytest

from app.pipeline.lyric_word_resync import resync_slot_overlays
from tests.pipeline._lyric_invariant_helpers import (
    inject_overlays_for_style,
    word_song_onset_s,
)

# Sync budget: visual onset must land within ±50 ms of vocal onset.
# 50 ms is the lower bound of perceptible audio-visual lag for speech
# (Spence & Squire 2003) — past this, viewers start to notice "dub-style"
# misalignment.
_SYNC_BUDGET_S: float = 0.050


# ── fixtures ───────────────────────────────────────────────────────────────


def _two_line_fixture() -> list[dict]:
    """Two lines with three real per-word timings each — the smallest
    fixture that exercises both per-line and per-word invariants.
    """
    return [
        {
            "text": "hello cruel world",
            "start_s": 2.0,
            "end_s": 4.0,
            "words": [
                {"text": "hello", "start_s": 2.0, "end_s": 2.6},
                {"text": "cruel", "start_s": 2.6, "end_s": 3.2},
                {"text": "world", "start_s": 3.2, "end_s": 4.0},
            ],
        },
        {
            "text": "this is the end",
            "start_s": 5.0,
            "end_s": 7.5,
            "words": [
                {"text": "this", "start_s": 5.0, "end_s": 5.5},
                {"text": "is", "start_s": 5.5, "end_s": 5.8},
                {"text": "the", "start_s": 5.8, "end_s": 6.2},
                {"text": "end", "start_s": 6.2, "end_s": 7.5},
            ],
        },
    ]


# ── per-line invariants ────────────────────────────────────────────────────


def test_karaoke_emits_one_overlay_per_line() -> None:
    """The karaoke contract is one overlay per lyric line — multiple
    overlays per line would split the highlight sweep across renders and
    desync it from the audio. Fixture has 2 lines → expect 2 overlays.
    """
    overlays = inject_overlays_for_style(style="karaoke", lines=_two_line_fixture())
    assert len(overlays) == 2, f"expected one overlay per line, got {len(overlays)}"
    for o in overlays:
        assert o["effect"] == "karaoke-line", (
            f"karaoke overlay must carry effect='karaoke-line', got {o.get('effect')!r}"
        )


def test_karaoke_overlay_window_envelops_every_word() -> None:
    """Each overlay's [start_s, end_s] must bracket every entry in
    word_timings. A word that falls outside its overlay's window won't
    render (or will render off-screen), so the karaoke sweep would skip it.
    """
    overlays = inject_overlays_for_style(style="karaoke", lines=_two_line_fixture())
    for ov in overlays:
        start = float(ov["start_s"])
        end = float(ov["end_s"])
        for wt in ov["word_timings"]:
            w_start = float(wt["start_s"])
            w_end = float(wt["end_s"])
            assert w_start >= -1e-6, f"word starts before time 0: {w_start}"
            assert w_end <= (end - start) + 1e-6, (
                f"word {wt['text']!r} end {w_end} exceeds overlay span "
                f"{end - start} (overlay {start} → {end})"
            )


def test_karaoke_word_timings_strictly_monotonic_in_start_s() -> None:
    """Per-word start times must be non-decreasing — a backwards jump means
    the highlight sweep would visually reverse, which is unmistakable on
    screen. Pin this so a future reshuffle of `_inject_karaoke` can't
    silently break ordering.
    """
    overlays = inject_overlays_for_style(style="karaoke", lines=_two_line_fixture())
    for ov in overlays:
        prev = -math.inf
        for wt in ov["word_timings"]:
            assert float(wt["start_s"]) >= prev, (
                f"word_timings not monotonic in overlay {ov['text']!r}: "
                f"saw start_s={wt['start_s']} after {prev}"
            )
            prev = float(wt["start_s"])


def test_karaoke_duration_cs_payload_present_and_non_negative() -> None:
    """`duration_cs` is the libass `\\kf` payload (centiseconds, integer).
    Missing or negative values cause the ASS writer to either skip the
    word or generate malformed tags. Pin both presence and lower bound.
    """
    overlays = inject_overlays_for_style(style="karaoke", lines=_two_line_fixture())
    for ov in overlays:
        for wt in ov["word_timings"]:
            assert "duration_cs" in wt, f"missing duration_cs in {ov['text']!r}"
            assert isinstance(wt["duration_cs"], int), (
                f"duration_cs must be int, got {type(wt['duration_cs']).__name__}"
            )
            assert wt["duration_cs"] >= 5, (
                f"duration_cs floor is 5cs (matches _inject_karaoke), got {wt['duration_cs']}"
            )


# ── resync stamp invariants ────────────────────────────────────────────────


def test_karaoke_overlays_carry_finite_section_anchor_stamps() -> None:
    """Every karaoke overlay must carry both `section_anchor_s` and
    `section_end_anchor_s` as finite floats. Without these the M2 resync
    pass is a silent no-op and post-snap drift returns.
    """
    overlays = inject_overlays_for_style(style="karaoke", lines=_two_line_fixture())
    for ov in overlays:
        for key in ("section_anchor_s", "section_end_anchor_s"):
            assert key in ov, f"missing {key} on {ov['text']!r}"
            value = ov[key]
            assert isinstance(value, int | float), (
                f"{key} must be numeric, got {type(value).__name__}"
            )
            assert math.isfinite(float(value)), f"{key} not finite: {value}"


def test_karaoke_section_anchor_equals_line_start_s_in_section_coords() -> None:
    """The stamp on each overlay must equal the line's start_s in section
    coordinates. Without this contract the resync pass would re-anchor to
    the wrong song time.
    """
    overlays = inject_overlays_for_style(
        style="karaoke",
        best_start_s=0.0,
        lines=_two_line_fixture(),
    )
    expected = [2.0, 5.0]
    for ov, exp in zip(overlays, expected, strict=True):
        assert float(ov["section_anchor_s"]) == pytest.approx(exp, abs=1e-3), (
            f"section_anchor_s drift: got {ov['section_anchor_s']}, "
            f"expected {exp} (line start_s in section coords)"
        )


# ── sync correctness against beat-snap drift ───────────────────────────────


def test_karaoke_word_onsets_stay_within_sync_budget_after_realistic_beat_snap() -> None:
    """End-to-end sync proof. Inject karaoke. Simulate a +200 ms beat-snap
    drift on the slot (representative worst case on a 2.4 BPS / ~250 ms
    beat-interval track). Run the M2 resync pass. Verify every word's
    rewritten render-time-anchor matches its song-time onset to within
    ±50 ms (sync budget).

    Without the resync, the worst-case drift would be ~200 ms — well over
    the perceptual threshold. With it, drift collapses to floating-point
    rounding (< 1 ms).
    """
    lines = _two_line_fixture()
    overlays = inject_overlays_for_style(style="karaoke", lines=lines)

    # Simulate beat-snap: slot's section-relative start shifts forward by
    # 0.2 s (the pre-snap origin was 0.0). Drift comes off the end so the
    # post-snap slot duration is 19.8 s vs the recipe's 20.0 s.
    post_snap_section_start_s = 0.2
    slot_post_snap_duration_s = 19.8

    rewritten = resync_slot_overlays(
        overlays,
        slot_post_snap_section_start_s=post_snap_section_start_s,
        slot_post_snap_duration_s=slot_post_snap_duration_s,
    )
    assert rewritten == len(overlays), (
        f"resync did not rewrite every overlay (got {rewritten} of {len(overlays)})"
    )

    # For each word, the audio-aligned render time is
    #   render_time = post_snap_section_start + overlay.start_s + word.local_start_s
    # which should equal the word's section-time (line.start_s + word_offset).
    for ov_idx, ov in enumerate(overlays):
        line = lines[ov_idx]
        line_section_start = float(line["start_s"])  # already section-relative
        for wt in ov["word_timings"]:
            render_time_section = (
                post_snap_section_start_s + float(ov["start_s"]) + float(wt["start_s"])
            )
            expected_song_time_offset = word_song_onset_s(
                line_song_start_s=line_section_start,
                word_local_start_s=float(wt["start_s"]),
            )
            drift_s = abs(render_time_section - expected_song_time_offset)
            assert drift_s <= _SYNC_BUDGET_S, (
                f"karaoke word {wt['text']!r} drift {drift_s * 1000:.1f}ms "
                f"exceeds sync budget {_SYNC_BUDGET_S * 1000:.0f}ms after resync"
            )


def test_karaoke_word_onsets_drift_without_resync_on_realistic_beat_snap() -> None:
    """The inverse proof: when the M2 resync pass is bypassed, a 200 ms
    slot drift produces visible per-word drift. If this test ever fails
    with low drift, the recipe pipeline is silently snapping in some
    other place and the resync has become redundant — investigate
    rather than relaxing the assertion.
    """
    lines = _two_line_fixture()
    overlays = inject_overlays_for_style(style="karaoke", lines=lines)

    # Simulate the same beat-snap drift but skip the resync pass.
    post_snap_section_start_s = 0.2

    # Worst-case word drift = slot drift. Find at least ONE word whose
    # uncorrected render time differs from its target by >= _SYNC_BUDGET_S.
    saw_drift = False
    for ov_idx, ov in enumerate(overlays):
        line = lines[ov_idx]
        line_section_start = float(line["start_s"])
        for wt in ov["word_timings"]:
            render_time_section = (
                post_snap_section_start_s
                + float(ov["start_s"])  # NOT rewritten — pre-snap value
                + float(wt["start_s"])
            )
            expected = word_song_onset_s(
                line_song_start_s=line_section_start,
                word_local_start_s=float(wt["start_s"]),
            )
            if abs(render_time_section - expected) > _SYNC_BUDGET_S:
                saw_drift = True
                break
        if saw_drift:
            break
    assert saw_drift, (
        "Expected uncorrected karaoke overlays to drift past sync budget "
        "on a 200ms slot shift — they didn't. Either the beat-snap math "
        "or the resync hook has changed elsewhere; re-verify before relaxing."
    )
