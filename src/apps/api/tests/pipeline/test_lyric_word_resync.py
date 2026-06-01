"""Unit tests for `app.pipeline.lyric_word_resync`.

Covers:
  - `resync_overlay_against_snapped_slot` / `resync_slot_overlays` —
    re-anchors karaoke + popup overlays against post-snap slot durations
    while leaving every other overlay byte-identical.
"""

from __future__ import annotations

import copy

import pytest

from app.pipeline.lyric_word_resync import (
    resync_overlay_against_snapped_slot,
    resync_slot_overlays,
)

# ── re-anchor against post-snap slot ────────────────────────────────────────


def _make_karaoke_overlay(
    *,
    section_anchor_s: float = 5.0,
    section_end_anchor_s: float = 7.5,
    pre_snap_slot_start_s: float = 4.0,
) -> dict:
    """Build a karaoke overlay whose pre-snap windowing matches a slot
    starting at ``pre_snap_slot_start_s`` (in section coords). Mirrors the
    shape produced by `lyric_injector._inject_karaoke`.
    """
    return {
        "text": "hello world",
        "effect": "karaoke-line",
        "start_s": round(section_anchor_s - pre_snap_slot_start_s, 3),
        "end_s": round(section_end_anchor_s - pre_snap_slot_start_s, 3),
        "highlight_color": "#FFFF00",
        "word_timings": [
            {"text": "hello", "start_s": 0.0, "end_s": 1.2, "duration_cs": 120},
            {"text": "world", "start_s": 1.2, "end_s": 2.5, "duration_cs": 130},
        ],
        "section_anchor_s": section_anchor_s,
        "section_end_anchor_s": section_end_anchor_s,
    }


def test_resync_rewrites_start_end_against_post_snap_offset() -> None:
    """Beat-snap shifted the slot by +0.4s (now starts at 4.4s in section
    coords). The overlay's section_anchor_s = 5.0s, so its new slot-relative
    start_s must be 5.0 - 4.4 = 0.6s — NOT the pre-snap 1.0s. Without this
    rewrite the karaoke sweep lands 400 ms ahead of the vocal.
    """
    overlay = _make_karaoke_overlay(
        section_anchor_s=5.0,
        section_end_anchor_s=7.5,
        pre_snap_slot_start_s=4.0,
    )
    # Pre-snap state assertion (sanity)
    assert overlay["start_s"] == pytest.approx(1.0)
    assert overlay["end_s"] == pytest.approx(3.5)

    rewritten = resync_overlay_against_snapped_slot(
        overlay,
        slot_post_snap_section_start_s=4.4,
        slot_post_snap_duration_s=4.0,
    )
    assert rewritten is True
    assert overlay["start_s"] == pytest.approx(0.6, abs=1e-3)
    assert overlay["end_s"] == pytest.approx(3.1, abs=1e-3)
    # word_timings are NOT rewritten — they're relative to overlay start
    # and the audio they describe didn't move.
    assert overlay["word_timings"][0]["start_s"] == pytest.approx(0.0)


def test_resync_skips_overlay_without_stamp() -> None:
    """Line-style overlays do NOT carry section_anchor_s. The pass must
    leave them byte-identical so Line's behavior is unchanged.
    """
    line_overlay = {
        "text": "lyric line",
        "effect": "lyric-line",
        "start_s": 1.2,
        "end_s": 3.4,
        "fade_in_ms": 50,
        "fade_out_ms": 250,
    }
    snapshot = copy.deepcopy(line_overlay)
    rewritten = resync_overlay_against_snapped_slot(
        line_overlay,
        slot_post_snap_section_start_s=10.0,
        slot_post_snap_duration_s=5.0,
    )
    assert rewritten is False
    assert line_overlay == snapshot, "line overlay was mutated by resync pass"


def test_resync_skips_non_lyric_effects_even_if_stamped() -> None:
    """Defense in depth: even if some other code path stamped
    section_anchor_s onto a non-lyric overlay, the pass refuses to rewrite
    it. Only registered effects (karaoke-line, pop-in) participate.
    """
    bogus = {
        "text": "label",
        "effect": "font-cycle",
        "start_s": 2.0,
        "end_s": 4.0,
        "section_anchor_s": 5.0,
        "section_end_anchor_s": 7.0,
    }
    snapshot = copy.deepcopy(bogus)
    rewritten = resync_overlay_against_snapped_slot(
        bogus,
        slot_post_snap_section_start_s=3.0,
        slot_post_snap_duration_s=4.0,
    )
    assert rewritten is False
    assert bogus == snapshot


def test_resync_returns_false_when_overlay_falls_entirely_outside_slot() -> None:
    """Beat-snap shifted the slot so much that the overlay's section
    window is no longer inside it. The pass refuses to rewrite (which
    would produce an empty render window) and logs for visibility.
    """
    overlay = _make_karaoke_overlay(
        section_anchor_s=20.0,
        section_end_anchor_s=22.0,
        pre_snap_slot_start_s=19.0,
    )
    rewritten = resync_overlay_against_snapped_slot(
        overlay,
        slot_post_snap_section_start_s=0.0,
        slot_post_snap_duration_s=5.0,  # slot ends at section_time=5.0
    )
    assert rewritten is False, "rewrite must not happen when window doesn't overlap"


def test_resync_preserves_tail_past_slot_end_for_post_join_burn() -> None:
    """Overlay's section window extends past the slot's post-snap end.
    Karaoke is burned after the slots are joined, so the line must be allowed
    to continue across the visual cut until the next lyric starts.
    """
    overlay = _make_karaoke_overlay(
        section_anchor_s=4.0,
        section_end_anchor_s=10.0,
        pre_snap_slot_start_s=4.0,
    )
    rewritten = resync_overlay_against_snapped_slot(
        overlay,
        slot_post_snap_section_start_s=4.0,
        slot_post_snap_duration_s=5.0,
    )
    assert rewritten is True
    assert overlay["start_s"] == pytest.approx(0.0)
    assert overlay["end_s"] == pytest.approx(6.0)


def test_resync_preserves_popup_tail_past_slot_end_for_post_join_burn() -> None:
    """Per-word-pop stages are vocal-timed too. A cumulative stage can start
    before a visual cut and naturally end at the next word after that cut; the
    post-join burn should keep the stage visible until that next word starts.
    """
    overlay = {
        "text": "I love",
        "effect": "pop-in",
        "start_s": 0.8,
        "end_s": 1.0,  # pre-resync value was clipped to the visual slot end
        "pop_animated_suffix": "love",
        "section_anchor_s": 4.8,
        "section_end_anchor_s": 5.5,
    }
    rewritten = resync_overlay_against_snapped_slot(
        overlay,
        slot_post_snap_section_start_s=4.0,
        slot_post_snap_duration_s=1.0,
    )
    assert rewritten is True
    assert overlay["start_s"] == pytest.approx(0.8)
    assert overlay["end_s"] == pytest.approx(1.5)


def test_resync_slot_walks_all_eligible_overlays() -> None:
    """resync_slot_overlays returns the count of overlays it rewrote and
    skips ineligible entries silently.
    """
    karaoke_a = _make_karaoke_overlay(section_anchor_s=5.0, section_end_anchor_s=6.0)
    karaoke_b = _make_karaoke_overlay(section_anchor_s=7.0, section_end_anchor_s=8.0)
    line = {
        "text": "x",
        "effect": "lyric-line",
        "start_s": 0.5,
        "end_s": 2.5,
    }
    bogus_non_dict = "not an overlay"  # type: ignore[assignment]

    slot_overlays = [karaoke_a, line, karaoke_b, bogus_non_dict]  # type: ignore[list-item]
    count = resync_slot_overlays(
        slot_overlays,
        slot_post_snap_section_start_s=4.5,
        slot_post_snap_duration_s=5.0,
    )
    assert count == 2, "expected 2 karaoke rewrites, line untouched"
    assert line == {
        "text": "x",
        "effect": "lyric-line",
        "start_s": 0.5,
        "end_s": 2.5,
    }


def test_resync_slot_with_no_eligible_overlays_returns_zero() -> None:
    """A slot containing only non-lyric overlays pays zero — fast path
    for the common case (templates without lyrics).
    """
    overlays = [
        {"text": "Welcome", "effect": "fade-in", "start_s": 0.0, "end_s": 2.0},
        {"text": "Subject", "effect": "scale-up", "start_s": 1.0, "end_s": 3.0},
    ]
    snapshot = copy.deepcopy(overlays)
    count = resync_slot_overlays(
        overlays,
        slot_post_snap_section_start_s=0.0,
        slot_post_snap_duration_s=5.0,
    )
    assert count == 0
    assert overlays == snapshot
