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

from app.pipeline.lyric_injector import _finalize_lyric_audible_window
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


def _overlapping_karaoke_fixture() -> list[dict]:
    """Regression shape from the 14s lyric-preview overlap bug."""
    return [
        {
            "text": "When I'm fucked up that's the real me",
            "start_s": 13.0,
            "end_s": 14.45,
            "words": [
                {"text": "When", "start_s": 13.0, "end_s": 13.3},
                {"text": "I'm", "start_s": 13.3, "end_s": 13.55},
                {"text": "fucked", "start_s": 13.55, "end_s": 14.0},
                {"text": "up", "start_s": 14.0, "end_s": 14.35},
                {"text": "that's", "start_s": 14.35, "end_s": 14.45},
            ],
        },
        {
            "text": "When I'm fucked up that's the real me yeah",
            "start_s": 14.05,
            "end_s": 16.2,
            "words": [
                {"text": "When", "start_s": 14.05, "end_s": 14.35},
                {"text": "I'm", "start_s": 14.35, "end_s": 14.55},
                {"text": "fucked", "start_s": 14.55, "end_s": 14.9},
                {"text": "up", "start_s": 14.9, "end_s": 15.1},
                {"text": "that's", "start_s": 15.1, "end_s": 15.35},
                {"text": "the", "start_s": 15.35, "end_s": 15.55},
                {"text": "real", "start_s": 15.55, "end_s": 15.8},
                {"text": "me", "start_s": 15.8, "end_s": 16.0},
                {"text": "yeah", "start_s": 16.0, "end_s": 16.2},
            ],
        },
    ]


def _karaoke_overlay(
    *,
    text: str,
    start_s: float,
    end_s: float,
    original_start_s_song: float,
    original_words: list[dict],
) -> dict:
    original_end_s_song = max(float(w["end_s_song"]) for w in original_words)
    return {
        "text": text,
        "effect": "karaoke-line",
        "start_s": start_s,
        "end_s": end_s,
        "position": "bottom",
        "text_color": "#FFFFFF",
        "highlight_color": "#FFFF00",
        "word_timings": [
            {
                "text": w["text"],
                "start_s": round(float(w["start_s_song"]) - original_start_s_song, 3),
                "end_s": round(float(w["end_s_song"]) - original_start_s_song, 3),
                "duration_cs": 10,
            }
            for w in original_words
        ],
        "original_text": text,
        "original_start_s_song": original_start_s_song,
        "original_end_s_song": original_end_s_song,
        "original_words": original_words,
    }


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


def test_karaoke_finalize_clamps_surviving_overlapping_line_windows() -> None:
    """Nested cached lyric lines must not survive as simultaneous events."""
    overlays = inject_overlays_for_style(
        style="karaoke",
        target_duration_s=4.0,
        best_start_s=13.0,
        best_end_s=16.2,
        lines=_overlapping_karaoke_fixture(),
    )
    finalized = _finalize_lyric_audible_window(
        overlays,
        audio_mix_song_start_s=13.0,
        audio_mix_song_end_s=16.2,
    )

    assert len(finalized) == 2
    first, second = finalized
    assert first["end_s"] == pytest.approx(second["start_s"], abs=1e-3)
    assert first["section_end_anchor_s"] == pytest.approx(
        second["section_anchor_s"], abs=1e-3
    )

    first_span_s = float(first["end_s"]) - float(first["start_s"])
    for wt in first["word_timings"]:
        assert float(wt["end_s"]) <= first_span_s + 1e-6


def test_karaoke_finalize_invariant_no_overlapping_active_windows() -> None:
    overlays = inject_overlays_for_style(
        style="karaoke",
        target_duration_s=4.0,
        best_start_s=13.0,
        best_end_s=16.2,
        lines=_overlapping_karaoke_fixture(),
    )
    finalized = _finalize_lyric_audible_window(
        overlays,
        audio_mix_song_start_s=13.0,
        audio_mix_song_end_s=16.2,
    )

    sorted_overlays = sorted(finalized, key=lambda ov: float(ov["section_anchor_s"]))
    for prev, nxt in zip(sorted_overlays, sorted_overlays[1:], strict=False):
        assert float(prev["section_end_anchor_s"]) <= float(nxt["section_anchor_s"]) + 1e-6
        assert float(prev["end_s"]) <= float(nxt["start_s"]) + 1e-6


def test_karaoke_finalize_dropping_final_fragment_does_not_shorten_previous_line() -> None:
    """Job 1af7113b had a meaningful previous line followed by a tiny final
    duplicate fragment. Dropping the fragment must not clamp the previous line
    before "that's the real me" finishes."""
    lines = [
        {
            "text": "When I'm fucked up, that's the real me",
            "start_s": 55.08,
            "end_s": 57.18,
            "words": [
                {"text": "When", "start_s": 55.08, "end_s": 55.30},
                {"text": "I'm", "start_s": 55.30, "end_s": 55.58},
                {"text": "fucked", "start_s": 55.58, "end_s": 55.92},
                {"text": "up", "start_s": 55.92, "end_s": 56.22},
                {"text": "that's", "start_s": 56.22, "end_s": 56.48},
                {"text": "the", "start_s": 56.50, "end_s": 56.56},
                {"text": "real", "start_s": 56.56, "end_s": 56.84},
                {"text": "me", "start_s": 56.84, "end_s": 57.18},
            ],
        },
        {
            "text": "When I'm fucked up, that's the real me, yeah",
            "start_s": 56.93,
            "end_s": 59.57,
            "words": [
                {"text": "When", "start_s": 56.93, "end_s": 57.16},
                {"text": "I'm", "start_s": 57.18, "end_s": 57.48},
                {"text": "fucked", "start_s": 57.48, "end_s": 57.80},
                {"text": "up", "start_s": 57.80, "end_s": 58.16},
                {"text": "that's", "start_s": 58.20, "end_s": 58.48},
                {"text": "the", "start_s": 58.50, "end_s": 58.55},
                {"text": "real", "start_s": 58.56, "end_s": 58.80},
                {"text": "me", "start_s": 58.80, "end_s": 59.30},
                {"text": "yeah", "start_s": 59.32, "end_s": 59.57},
            ],
        },
    ]
    overlays = inject_overlays_for_style(
        style="karaoke",
        target_duration_s=16.64,
        best_start_s=42.86,
        best_end_s=59.5,
        lines=lines,
    )

    finalized = _finalize_lyric_audible_window(
        overlays,
        audio_mix_song_start_s=42.86,
        audio_mix_song_end_s=57.434,
    )

    assert len(finalized) == 1
    assert finalized[0]["text"] == "When I'm fucked up, that's the real me"
    assert finalized[0]["end_s"] == pytest.approx(14.32, abs=1e-3)


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


def test_karaoke_overlays_carry_original_song_time_metadata() -> None:
    """The post-snap audible-window finalizer needs song-time originals.
    Without these fields karaoke falls back to passthrough and cannot drop
    out-of-window tails like job 1af7113b."""
    overlays = inject_overlays_for_style(style="karaoke", lines=_two_line_fixture())
    for ov in overlays:
        assert ov["original_text"] == ov["text"]
        assert isinstance(ov["original_start_s_song"], int | float)
        assert isinstance(ov["original_end_s_song"], int | float)
        assert ov["original_words"], f"missing original_words on {ov['text']!r}"


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


# ── final audible-window regressions ────────────────────────────────────────


def test_karaoke_finalize_drops_job_1af7113b_final_tail_fragment() -> None:
    """Regression for job 1af7113b.

    The selected track section was 16.64s, but post-snap video/audio duration
    was 14.574s. The final karaoke line started at 14.07s and could only show
    about 0.48s of speech, so the yellow sweep never reached "real me yeah".
    """
    best_start_s = 42.86
    post_snap_video_duration_s = 14.574
    original_words = [
        {"text": "When", "start_s_song": 56.93, "end_s_song": 57.16},
        {"text": "I'm", "start_s_song": 57.18, "end_s_song": 57.48},
        {"text": "fucked", "start_s_song": 57.48, "end_s_song": 57.80},
        {"text": "up", "start_s_song": 57.80, "end_s_song": 58.16},
        {"text": "that's", "start_s_song": 58.20, "end_s_song": 58.48},
        {"text": "the", "start_s_song": 58.50, "end_s_song": 58.55},
        {"text": "real", "start_s_song": 58.56, "end_s_song": 58.80},
        {"text": "me", "start_s_song": 58.80, "end_s_song": 59.30},
        {"text": "yeah", "start_s_song": 59.32, "end_s_song": 59.57},
    ]
    overlay = _karaoke_overlay(
        text="When I'm fucked up, that's the real me, yeah",
        start_s=14.07,
        end_s=16.64,
        original_start_s_song=56.93,
        original_words=original_words,
    )

    out = _finalize_lyric_audible_window(
        [overlay],
        audio_mix_song_start_s=best_start_s,
        audio_mix_song_end_s=best_start_s + post_snap_video_duration_s,
    )

    assert out == []


def test_karaoke_finalize_keeps_meaningful_final_partial_with_rebuilt_timings() -> None:
    """A final partial is fine when enough words are actually audible. The
    renderer consumes word_timings, so the kept overlay must rebuild those
    timings instead of only changing text metadata."""
    original_words = [
        {"text": w, "start_s_song": 130.0 + i * 0.5, "end_s_song": 130.5 + i * 0.5}
        for i, w in enumerate("A B C D E F G H I J".split())
    ]
    overlay = _karaoke_overlay(
        text="A B C D E F G H I J",
        start_s=2.0,
        end_s=7.0,
        original_start_s_song=130.0,
        original_words=original_words,
    )

    out = _finalize_lyric_audible_window(
        [overlay],
        audio_mix_song_start_s=128.0,
        audio_mix_song_end_s=132.0,
    )

    assert len(out) == 1
    result = out[0]
    assert result["text"] == "A B C D"
    assert [w["text"] for w in result["word_timings"]] == ["A", "B", "C", "D"]
    span = result["end_s"] - result["start_s"]
    assert all(float(w["end_s"]) <= span + 1e-6 for w in result["word_timings"])
