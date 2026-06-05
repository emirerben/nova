import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.lyric_injector import inject_lyric_overlays
from app.pipeline.lyrics_preview import (
    LEAD_IN_S,
    PREVIEW_CRF,
    PREVIEW_WINDOW_S,
    LyricsPreviewInputError,
    _first_lyric_in_section,
    _read_best_bounds,
    _resolve_preview_window,
    _resolve_preview_window_with_policy,
    build_lyrics_preview_ass_files,
    build_lyrics_preview_recipe,
    render_lyrics_preview,
)
from app.pipeline.text_overlay import generate_animated_overlay_ass


def _track(**overrides):
    track = SimpleNamespace(
        id="track-preview",
        audio_gcs_path="music/track/audio.m4a",
        duration_s=5.0,
        track_config={},
        lyrics_cached={
            "source": "lrclib_synced+whisper",
            "lines": [
                {
                    "text": "hello world",
                    "start_s": 1.0,
                    "end_s": 2.0,
                    "words": [
                        {"text": "hello", "start_s": 1.0, "end_s": 1.5},
                        {"text": "world", "start_s": 1.5, "end_s": 2.0},
                    ],
                }
            ],
        },
    )
    for key, value in overrides.items():
        setattr(track, key, value)
    return track


def test_preview_ass_byte_identical_to_production_path(tmp_path: Path) -> None:
    track = _track()
    cfg = {"enabled": True, "style": "line", "post_dwell_s": 1.0}

    preview_files = build_lyrics_preview_ass_files(track, cfg, str(tmp_path / "preview"))

    production_dir = tmp_path / "production"
    production_dir.mkdir()
    recipe = {"slots": [{"position": 1, "target_duration_s": 5.0, "text_overlays": []}]}
    recipe = inject_lyric_overlays(
        recipe,
        track.lyrics_cached,
        best_start_s=0.0,
        best_end_s=5.0,
        lyrics_config=cfg,
    )
    production_files = generate_animated_overlay_ass(
        recipe["slots"][0]["text_overlays"],
        slot_duration_s=5.0,
        output_dir=str(production_dir),
        slot_index=0,
    )

    assert production_files
    assert [Path(p).read_text() for p in preview_files] == [
        Path(p).read_text() for p in production_files
    ]


def test_preview_rejects_missing_lyrics_cached(tmp_path: Path) -> None:
    with pytest.raises(LyricsPreviewInputError, match="cached lyrics"):
        build_lyrics_preview_ass_files(_track(lyrics_cached=None), {}, str(tmp_path))


def test_preview_recipe_clamps_to_20s_window_when_track_is_longer() -> None:
    """Tracks longer than PREVIEW_WINDOW_S get a clamped 20s preview slot.

    History: PR opening the Line Templates dashboard (2026-05-25). Before
    the clamp the preview rendered the entire 3-4 minute track, which made
    the iteration loop pointlessly slow for a feature focused on hook timing.
    """
    long_track = _track(duration_s=185.0)
    recipe = build_lyrics_preview_recipe(long_track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_recipe_renders_full_length_when_track_is_shorter_than_window() -> None:
    """Tracks shorter than PREVIEW_WINDOW_S keep their full duration — the
    clamp is a ceiling, not a floor. Locks the byte-identical guarantee for
    short fixtures (the 5s track used in the production-parity test above).
    """
    short_track = _track(duration_s=5.0)
    recipe = build_lyrics_preview_recipe(short_track, {})
    assert recipe["slots"][0]["target_duration_s"] == 5.0


def test_preview_recipe_at_exact_window_boundary() -> None:
    """Boundary value `duration_s == PREVIEW_WINDOW_S` lands on the clamp's
    inclusive side. Locks that a future refactor swapping `min(a, b)` for an
    `if a > b` guard would not silently shift behavior at 20.0s.
    """
    boundary_track = _track(duration_s=PREVIEW_WINDOW_S)
    recipe = build_lyrics_preview_recipe(boundary_track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_popup_drops_nested_short_adlib_line() -> None:
    """Regression for Pop-up preview job 1b23fc80: a one-word nested ad-lib
    rendered over the main cumulative lyric line in the same visual lane.
    """
    track = _track(
        duration_s=6.0,
        lyrics_cached={
            "source": "lrclib_synced+whisper",
            "lines": [
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
                    "words": [{"text": "Ok", "start_s": 1.32, "end_s": 1.85}],
                },
            ],
        },
    )
    recipe = build_lyrics_preview_recipe(track, {"enabled": True, "style": "per-word-pop"})
    overlays = recipe["slots"][0]["text_overlays"]

    texts = [str(ov.get("text", "")).strip() for ov in overlays]
    assert "Ok" not in texts
    assert any(text.endswith("why I put up with") for text in texts)
    active = [ov for ov in overlays if float(ov["start_s"]) <= 1.34 < float(ov["end_s"])]
    assert len(active) == 1


def test_preview_recipe_falls_back_to_best_end_s_when_duration_unknown_dict_shape() -> None:
    """When `duration_s` is missing or non-positive, the recipe falls back to
    `track_config.best_end_s` and clamps that against the preview window.

    Covers the production `track_config` shape (JSONB → dict at SQLAlchemy load).
    """
    track = _track(duration_s=0.0, track_config={"best_end_s": 12.0})
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == 12.0


def test_preview_recipe_falls_back_to_best_end_s_when_duration_unknown_object_shape() -> None:
    """Same fallback, but `track_config` is an object with `.best_end_s` rather
    than a dict. Defensive coverage so the resolver doesn't crash if any caller
    passes a Pydantic model or SimpleNamespace into the preview pipeline.
    """
    track = _track(
        duration_s=0.0,
        track_config=SimpleNamespace(best_end_s=12.0),
    )
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == 12.0


def test_preview_recipe_fallback_also_clamps_to_window() -> None:
    """If `best_end_s` exceeds PREVIEW_WINDOW_S the fallback still respects the
    20s ceiling. Catches a bug where the clamp lived only on the primary path.
    """
    track = _track(duration_s=0.0, track_config={"best_end_s": 90.0})
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_recipe_raises_when_duration_and_best_end_s_both_missing() -> None:
    """Neither `duration_s` nor `best_end_s` — the recipe can't pick a slot
    length, so it raises rather than producing a zero-length preview.
    """
    track = _track(duration_s=0.0, track_config={})
    with pytest.raises(LyricsPreviewInputError, match="duration is unknown"):
        build_lyrics_preview_recipe(track, {})


def test_preview_recipe_raises_on_negative_duration() -> None:
    """Negative duration is treated as unknown, not as a literal slot length."""
    track = _track(duration_s=-5.0, track_config={})
    with pytest.raises(LyricsPreviewInputError, match="duration is unknown"):
        build_lyrics_preview_recipe(track, {})


# ---------------------------------------------------------------------------
# Section-anchored preview window (2026-05-27)
#
# Locks the policy switch from "first lyric of song" → "first lyric WITHIN
# the admin-selected best section, with first-vocal-of-song as the fallback
# when the section has no lyrics". The Beat It bug (job 616d3e53) was that
# clicking section #2 had zero effect on the preview window; these tests
# guard against any regression of that behavior AND against breaking the
# pre-existing Billie Jean instrumental-intro fallback.
# ---------------------------------------------------------------------------


def _track_with_lines(line_starts: list[float], **overrides):
    """Build a track fixture with explicit lyric line start times.

    Each entry produces a 1-second line at the given `start_s`. Used by the
    section-anchor tests below so each scenario can express its in/out-of-
    section line layout in one line of test code.
    """
    track = _track(**overrides)
    track.lyrics_cached = {
        # Injector requires a publishable source after 2026-05-27 — see
        # `_INJECTOR_ALLOWED_SOURCES` in app/pipeline/lyric_injector.py.
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": f"line {idx}",
                "start_s": start,
                "end_s": start + 1.0,
                "words": [
                    {"text": f"line{idx}", "start_s": start, "end_s": start + 1.0},
                ],
            }
            for idx, start in enumerate(line_starts)
        ],
    }
    return track


def test_preview_window_anchors_at_first_lyric_in_section() -> None:
    """Section-anchored: lyric at 130s within section [127.2, 141.0].

    Anchor = max(127.2, 130 - 2.0) = 128.0; duration = min(20, 141.0-128.0) = 13.0.
    """
    track = _track_with_lines(
        [28.0, 130.0, 132.0, 200.0],
        duration_s=300.0,
        track_config={"best_start_s": 127.2, "best_end_s": 141.0},
    )
    assert _resolve_preview_window(track) == (128.0, 13.0)


def test_preview_line_style_drops_text_whose_words_are_outside_audio_window() -> None:
    """Regression for lyrics-preview job 48c7f737.

    The selected preview window was [130, 148]. The line bounds for
    "Some more again" overlapped that window after LRC re-anchoring, but
    the line's word timings were all before 124s. Full renders run the
    audible-window finalizer and drop that stale line; preview must do the
    same before writing ASS.
    """
    track = _track(
        duration_s=339.792,
        track_config={"best_start_s": 130.0, "best_end_s": 148.0},
        lyrics_cached={
            "source": "lrclib_synced+whisper",
            "lines": [
                {
                    "text": "Some more again",
                    "start_s": 123.96,
                    "end_s": 133.37,
                    "words": [
                        {"text": "Some", "start_s": 121.59, "end_s": 122.39},
                        {"text": "more", "start_s": 122.39, "end_s": 123.19},
                        {"text": "again", "start_s": 123.19, "end_s": 123.99},
                    ],
                },
                {
                    "text": "It didn't matter what they wanted to see",
                    "start_s": 133.42,
                    "end_s": 137.55,
                    "words": [
                        {"text": "It", "start_s": 133.58, "end_s": 134.38},
                        {"text": "didn't", "start_s": 134.38, "end_s": 135.06},
                        {"text": "matter", "start_s": 135.06, "end_s": 135.5},
                        {"text": "what", "start_s": 135.52, "end_s": 135.9},
                        {"text": "they", "start_s": 135.92, "end_s": 136.3},
                        {"text": "wanted", "start_s": 136.32, "end_s": 136.7},
                        {"text": "to", "start_s": 136.72, "end_s": 137.1},
                        {"text": "see", "start_s": 137.12, "end_s": 137.5},
                    ],
                },
            ],
        },
    )

    recipe = build_lyrics_preview_recipe(track, {"enabled": True, "style": "line"})
    texts = [o.get("display_text") or o.get("text") for o in recipe["slots"][0]["text_overlays"]]

    assert "Some more again" not in texts
    assert "It didn't matter what they wanted to see" in texts


def test_preview_window_falls_back_when_section_has_no_lyrics() -> None:
    """Section contains no lyrics → falls back to first-vocal-of-song policy.

    Billie Jean shape: section selected in instrumental territory, all lines
    after the section. Without the fallback we'd render a silent 20s preview.
    """
    track = _track_with_lines(
        [30.8, 35.0],
        duration_s=200.0,
        track_config={"best_start_s": 0.0, "best_end_s": 20.0},
    )
    assert _resolve_preview_window(track) == (28.8, 20.0)


def test_preview_window_falls_back_when_track_config_missing() -> None:
    """No track_config bounds → fallback path, byte-identical to pre-2026-05-27
    behavior. Pre-existing tracks that haven't been re-sectioned still work.
    """
    track = _track_with_lines(
        [30.8, 35.0],
        duration_s=200.0,
        track_config={},
    )
    assert _resolve_preview_window(track) == (28.8, 20.0)


def test_preview_window_clamps_anchor_at_section_start() -> None:
    """Lyric at best_start_s + 0.3s → LEAD_IN would go below section start,
    so anchor clamps at best_start_s. Preview must not bleed audio from
    outside the admin-selected section (e.g. drum fill ending the prior
    section would otherwise leak into a chorus preview).
    """
    track = _track_with_lines(
        [127.5],
        duration_s=300.0,
        track_config={"best_start_s": 127.2, "best_end_s": 141.0},
    )
    start_s, duration_s = _resolve_preview_window(track)
    assert start_s == 127.2  # clamped, NOT 127.5 - 2.0
    assert duration_s == round(141.0 - 127.2, 3)  # 13.8


def test_preview_window_duration_capped_by_section_end() -> None:
    """Section span is only 14s → preview duration is 14s, not 20s. The window
    must never exceed best_end_s even when PREVIEW_WINDOW_S is larger.
    """
    track = _track_with_lines(
        [128.0],
        duration_s=300.0,
        track_config={"best_start_s": 127.2, "best_end_s": 141.2},
    )
    start_s, duration_s = _resolve_preview_window(track)
    # 128 - 2 = 126 < 127.2 → anchor clamps at 127.2; available = 141.2 - 127.2 = 14.0
    assert start_s == 127.2
    assert duration_s == 14.0


def test_preview_window_anchor_does_not_leak_outside_section_when_lead_in_negative() -> None:
    """First lyric exactly at best_start_s. Anchor must not go below 0 OR below
    section start. Defensive coverage for sections that start at 0s.
    """
    track = _track_with_lines(
        [5.0],
        duration_s=60.0,
        track_config={"best_start_s": 5.0, "best_end_s": 25.0},
    )
    start_s, duration_s = _resolve_preview_window(track)
    # 5.0 - 2.0 = 3.0 < 5.0 (section_start) → clamps to 5.0
    assert start_s == 5.0
    # available = min(60, 25) - 5 = 20.0
    assert duration_s == 20.0


def test_read_best_bounds_dict_shape() -> None:
    """Production track_config arrives as a dict (JSONB → SQLAlchemy load)."""
    assert _read_best_bounds({"best_start_s": 127.2, "best_end_s": 141.0}) == (127.2, 141.0)
    assert _read_best_bounds({}) == (None, None)
    assert _read_best_bounds({"best_start_s": 127.2}) == (127.2, None)


def test_read_best_bounds_object_shape() -> None:
    """Defensive: callers that pass Pydantic / SimpleNamespace shapes."""
    cfg = SimpleNamespace(best_start_s=127.2, best_end_s=141.0)
    assert _read_best_bounds(cfg) == (127.2, 141.0)
    assert _read_best_bounds(None) == (None, None)


def test_read_best_bounds_rejects_non_finite() -> None:
    """NaN / Inf must coerce to None on the offending axis. All NaN comparisons
    return False so a NaN best_start_s would silently fail every in-section
    check and fire the fallback — this guard converts that into an explicit
    "no section configured" signal at the read boundary.
    """
    assert _read_best_bounds({"best_start_s": float("nan"), "best_end_s": 141.0}) == (
        None,
        141.0,
    )
    assert _read_best_bounds({"best_start_s": 127.2, "best_end_s": float("inf")}) == (
        127.2,
        None,
    )
    assert _read_best_bounds({"best_start_s": "not-a-float", "best_end_s": None}) == (
        None,
        None,
    )


def test_first_lyric_in_section_filters_correctly() -> None:
    """Half-open `[best_start_s, best_end_s)` semantics with start-only fallback
    (lines lacking `end_s`). Returns min start_s of in-section lines. Lines
    outside the section are ignored; non-list / non-dict shapes return None.
    """
    lyrics = {
        "lines": [
            {"start_s": 28.0},
            {"start_s": 130.0},
            {"start_s": 132.0},
            {"start_s": 141.0},  # exactly at best_end_s — EXCLUDED (half-open)
            {"start_s": 200.0},
        ]
    }
    assert _first_lyric_in_section(lyrics, 127.2, 141.0) == 130.0
    # No lines in section
    assert _first_lyric_in_section(lyrics, 60.0, 100.0) is None
    # Empty input shapes
    assert _first_lyric_in_section({"lines": []}, 0.0, 100.0) is None
    assert _first_lyric_in_section(None, 0.0, 100.0) is None
    # Non-finite line is skipped
    bad = {"lines": [{"start_s": float("nan")}, {"start_s": 130.0}]}
    assert _first_lyric_in_section(bad, 127.2, 141.0) == 130.0


def test_first_lyric_in_section_excludes_line_at_section_end() -> None:
    """Half-open upper bound: a line whose `start_s` equals `best_end_s` does
    NOT belong to the section. Locks the 2026-05-27 rev2 semantics switch from
    inclusive-on-both-ends to `[best_start_s, best_end_s)`.

    Rationale: a line that starts at the section end has nothing renderable
    inside the preview window — the preview ends right when the line begins.
    """
    lyrics = {"lines": [{"start_s": 141.0}]}
    assert _first_lyric_in_section(lyrics, 127.2, 141.0) is None


def test_first_lyric_in_section_counts_overlapping_pre_section_line() -> None:
    """Interval-overlap path: a line that starts BEFORE the section but ends
    INSIDE it counts as belonging to the section. Common case in pop songs
    where a pre-chorus line bleeds into the chorus.

    Section [127.2, 141.0], line [126.9, 129.0]:
      `line_start (126.9) < best_end_s (141.0)` AND
      `line_end (129.0) > best_start_s (127.2)` → overlap, counts.
    Returns the raw `line.start_s = 126.9`; the caller clamps the anchor
    so audio does not bleed before the section start.
    """
    lyrics = {"lines": [{"start_s": 126.9, "end_s": 129.0}]}
    assert _first_lyric_in_section(lyrics, 127.2, 141.0) == 126.9


def test_first_lyric_in_section_ignores_non_finite_end_s() -> None:
    """When `end_s` is NaN/Inf/missing, fall back to start-only membership
    (still half-open). Guards against a future cache shape change leaking
    a non-finite end into the overlap math.
    """
    # NaN end → start-only path → 130.0 is in [127.2, 141.0)
    assert (
        _first_lyric_in_section(
            {"lines": [{"start_s": 130.0, "end_s": float("nan")}]}, 127.2, 141.0
        )
        == 130.0
    )
    # Missing end → start-only path → 130.0 is in [127.2, 141.0)
    assert _first_lyric_in_section({"lines": [{"start_s": 130.0}]}, 127.2, 141.0) == 130.0


def test_first_lyric_in_section_overlap_exact_boundary_touch_excluded() -> None:
    """Half-open overlap: a line ending exactly at `best_start_s` does NOT
    overlap (line_end > best_start_s must be strict). A line starting
    exactly at `best_end_s` does NOT overlap (line_start < best_end_s
    must be strict). Locks the strict-inequality choice.
    """
    # Line touching the upper bound from outside: start at best_end_s.
    assert (
        _first_lyric_in_section({"lines": [{"start_s": 141.0, "end_s": 145.0}]}, 127.2, 141.0)
        is None
    )
    # Line touching the lower bound from below: end at best_start_s.
    assert (
        _first_lyric_in_section({"lines": [{"start_s": 120.0, "end_s": 127.2}]}, 127.2, 141.0)
        is None
    )


def test_preview_window_section_anchored_with_pre_section_overlap() -> None:
    """End-to-end of the overlap path: pre-chorus line bleeds into the
    chorus section. Anchor must clamp to best_start_s — never let audio
    play from before the admin-selected section.

    Section [127.2, 141.0], line [126.9, 129.0]:
      first_in_section = 126.9
      anchor = max(0.0, 127.2, 126.9 - 2.0) = max(0.0, 127.2, 124.9) = 127.2
      available = min(track_duration, 141.0) - 127.2 = 13.8
    """
    track = _track(
        duration_s=300.0,
        track_config={"best_start_s": 127.2, "best_end_s": 141.0},
    )
    track.lyrics_cached = {
        # Source required by injector's Layer-2 gate (added 2026-05-27).
        "source": "lrclib_synced+whisper",
        "lines": [
            {"text": "intro", "start_s": 126.9, "end_s": 129.0},
            {"text": "more", "start_s": 130.0, "end_s": 131.0},
        ],
    }
    start_s, duration_s = _resolve_preview_window(track)
    assert start_s == 127.2  # clamped to section start, NOT 124.9
    assert duration_s == round(141.0 - 127.2, 3)  # 13.8


def test_preview_window_section_anchored_clamps_anchor_to_zero() -> None:
    """Defense vs negative `best_start_s`: anchor must clamp to `max(0.0, ...)`.
    Even though the frontend should not produce negative bounds, the API
    column accepts finite negatives and `_read_best_bounds` lets them through.
    Without the 0.0 clamp, FFmpeg `-ss -1.500` is silently treated as 0 but
    the downstream `duration_s` math is off.

    Section [-5.0, 10.0], line at 0.5:
      first_in_section = 0.5
      anchor = max(0.0, -5.0, 0.5 - 2.0) = max(0.0, -5.0, -1.5) = 0.0
      available = min(track_duration, 10.0) - 0.0 = 10.0
    """
    track = _track_with_lines(
        [0.5],
        duration_s=60.0,
        track_config={"best_start_s": -5.0, "best_end_s": 10.0},
    )
    start_s, duration_s = _resolve_preview_window(track)
    assert start_s == 0.0
    assert duration_s == 10.0


def test_resolve_preview_window_with_policy_reports_section() -> None:
    """Policy field tracks the section-anchored success case."""
    track = _track_with_lines(
        [28.0, 130.0],
        duration_s=300.0,
        track_config={"best_start_s": 127.2, "best_end_s": 141.0},
    )
    start_s, duration_s, policy, reason = _resolve_preview_window_with_policy(track)
    assert policy == "section"
    assert reason is None
    assert (start_s, duration_s) == (128.0, 13.0)


def test_resolve_preview_window_with_policy_reports_no_bounds() -> None:
    """track_config missing bounds → policy=fallback, reason=no_bounds."""
    track = _track_with_lines([30.8], duration_s=200.0, track_config={})
    _, _, policy, reason = _resolve_preview_window_with_policy(track)
    assert policy == "fallback"
    assert reason == "no_bounds"


def test_resolve_preview_window_with_policy_reports_invalid_bounds() -> None:
    """`best_end_s <= best_start_s` → policy=fallback, reason=invalid_bounds.
    Zero-span sections (start==end) and reversed bounds both flow here.
    """
    track = _track_with_lines(
        [30.8], duration_s=200.0, track_config={"best_start_s": 50.0, "best_end_s": 50.0}
    )
    _, _, policy, reason = _resolve_preview_window_with_policy(track)
    assert policy == "fallback"
    assert reason == "invalid_bounds"


def test_resolve_preview_window_with_policy_reports_no_lyrics_in_section() -> None:
    """Section has valid bounds but contains no overlapping lyric line →
    policy=fallback, reason=no_lyrics_in_section. The classic case the
    Billie Jean fallback handles.
    """
    track = _track_with_lines(
        [30.8, 60.0],
        duration_s=200.0,
        track_config={"best_start_s": 0.0, "best_end_s": 20.0},
    )
    _, _, policy, reason = _resolve_preview_window_with_policy(track)
    assert policy == "fallback"
    assert reason == "no_lyrics_in_section"


def test_render_lyrics_preview_emits_telemetry_when_fallback_anchor_inside_section(
    monkeypatch, tmp_path: Path
) -> None:
    """CRITICAL: the previous telemetry implementation missed this case.

    Section [20.0, 30.0] with NO overlapping lyrics. First vocal of song at
    30.8s. Fallback fires; anchor = max(0, 30.8 - 2.0) = 28.8s, which sits
    INSIDE the configured section [20, 30]. The pre-rev2 telemetry compared
    the final anchor against section bounds and would NOT emit the event —
    silently losing the very signal admins were supposed to monitor.

    With policy-based telemetry the event fires whenever `policy=="fallback"`
    and section bounds were configured, regardless of where the anchor lands.
    """
    captured: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.record_pipeline_event",
        lambda s, e, d=None: captured.append((s, e, d or {})),
    )
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.download_to_file",
        lambda _g, local: Path(local).write_bytes(b"audio"),
    )

    def _fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _l, _o: "https://example.com/preview.mp4",
    )

    track = _track_with_lines(
        [30.8, 35.0],
        duration_s=200.0,
        track_config={"best_start_s": 20.0, "best_end_s": 30.0},
    )
    render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-1")

    matching = [
        (s, e, d) for (s, e, d) in captured if (s, e) == ("preview", "anchor_outside_section")
    ]
    assert matching, (
        "policy-based telemetry must fire when the fallback path runs, even "
        f"if the fallback anchor happens to land inside the section. Events: {captured}"
    )
    payload = matching[0][2]
    # 28.8 is INSIDE [20.0, 30.0] — this is exactly the case the old impl missed.
    assert payload["preview_start_s"] == 28.8
    assert 20.0 <= payload["preview_start_s"] <= 30.0
    assert payload["best_start_s"] == 20.0
    assert payload["best_end_s"] == 30.0
    assert payload["reason"] == "no_lyrics_in_section"


def test_billie_jean_byte_identical_when_no_section_bounds() -> None:
    """CRITICAL REGRESSION: Billie Jean shape — 30s instrumental intro, first
    vocal at 30.8s, no `track_config` section bounds set. The 2026-05-27
    section-anchored rewrite must produce the EXACT same window the pre-fix
    code produced (the 2026-05-25 Billie Jean fix), or we'd regress 30s
    silent previews.

    Pre-fix output for this fixture:
      start_s = max(0, 30.8 - LEAD_IN_S) = 28.8
      duration_s = min(PREVIEW_WINDOW_S, 200 - 28.8) = 20.0
    """
    track = _track_with_lines(
        [30.8, 35.0, 60.0],
        duration_s=200.0,
        track_config={},  # no section configured — purest fallback case
    )
    assert _resolve_preview_window(track) == (
        round(30.8 - LEAD_IN_S, 3),
        PREVIEW_WINDOW_S,
    )


def test_render_lyrics_preview_emits_anchor_outside_section_telemetry(
    monkeypatch, tmp_path: Path
) -> None:
    """Defense-in-depth: when section bounds are configured but the resolved
    anchor falls outside them (fallback path fired because the section had no
    lyrics inside), `render_lyrics_preview` MUST emit a
    `preview.anchor_outside_section` pipeline_trace event. The audit trail
    lets admins detect users picking vocal-free sections without realizing
    the preview silently drops back to the song intro.

    Locks the call site against a future refactor silently removing the
    telemetry — without this test, only humans reading the diff would catch
    a deleted `record_pipeline_event` call.
    """
    captured: list[tuple[str, str, dict]] = []

    def fake_record_event(stage: str, event: str, data: dict | None = None) -> None:
        captured.append((stage, event, data or {}))

    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.record_pipeline_event", fake_record_event)
    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    # Section is [0, 20] but first lyric is at 30.8s — fallback fires and the
    # anchor (28.8s) lands outside the configured section. Event must emit.
    track = _track_with_lines(
        [30.8, 35.0],
        duration_s=200.0,
        track_config={"best_start_s": 0.0, "best_end_s": 20.0},
    )
    render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-1")

    matching = [
        (s, e, d) for (s, e, d) in captured if (s, e) == ("preview", "anchor_outside_section")
    ]
    assert matching, f"expected one preview.anchor_outside_section event, got: {captured}"
    assert len(matching) == 1
    payload = matching[0][2]
    assert payload["preview_start_s"] == 28.8
    assert payload["best_start_s"] == 0.0
    assert payload["best_end_s"] == 20.0
    assert payload["reason"] == "no_lyrics_in_section"


def test_render_lyrics_preview_telemetry_tolerates_float_rounding(
    monkeypatch, tmp_path: Path
) -> None:
    """The anchor inside `_resolve_preview_window` is `round(anchor, 3)` but
    `track_config.best_start_s` may carry more precision (e.g. 30.4561). Without
    rounding the comparison's right-hand side, a section saved at 30.4561 with
    the anchor floor-clamped to the same value would emit a false-positive
    `anchor_outside_section` event: rounded preview (30.456) is less than the
    unrounded section_start (30.4561). The metric must reflect the rendered
    window at FFmpeg precision, not at storage precision.
    """
    captured: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.record_pipeline_event",
        lambda s, e, d=None: captured.append((s, e, d or {})),
    )
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.download_to_file",
        lambda _g, local: Path(local).write_bytes(b"audio"),
    )

    def _fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _l, _o: "https://example.com/preview.mp4",
    )

    # Section bound with 4 decimals; first lyric at section start triggers
    # the floor clamp. preview_start_s rounds to 30.456 while best_start_s
    # stays at 30.4561 in the comparison. Pre-fix this fired the event.
    track = _track_with_lines(
        [30.4561],
        duration_s=200.0,
        track_config={"best_start_s": 30.4561, "best_end_s": 50.4561},
    )
    render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-1")

    outside = [(s, e) for (s, e, _) in captured if (s, e) == ("preview", "anchor_outside_section")]
    assert outside == [], (
        f"rounding-tolerance regression: anchor_outside_section fired on the "
        f"section-anchored happy path. Events: {captured}"
    )


def test_render_lyrics_preview_skips_telemetry_when_anchor_inside_section(
    monkeypatch, tmp_path: Path
) -> None:
    """Inverse of the previous test: when the section-anchored path succeeds
    (anchor lands inside [best_start_s, best_end_s]), no telemetry fires.
    Locks the gating condition so a future refactor can't accidentally emit
    the event on EVERY preview render (which would make the audit log
    useless).
    """
    captured: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.record_pipeline_event",
        lambda s, e, d=None: captured.append((s, e, d or {})),
    )
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.download_to_file",
        lambda _g, local: Path(local).write_bytes(b"audio"),
    )

    def _fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _l, _o: "https://example.com/preview.mp4",
    )

    # Section [127.2, 141.0] contains lyric at 130.0 → anchor lands at 128.0
    # which is INSIDE the section. No outside-section event.
    track = _track_with_lines(
        [28.0, 130.0],
        duration_s=300.0,
        track_config={"best_start_s": 127.2, "best_end_s": 141.0},
    )
    render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-1")

    outside = [(s, e) for (s, e, _) in captured if (s, e) == ("preview", "anchor_outside_section")]
    assert outside == [], f"expected zero anchor_outside_section events, got: {captured}"


def test_render_lyrics_preview_builds_browser_safe_ffmpeg(monkeypatch, tmp_path: Path) -> None:
    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    output_url, meta = render_lyrics_preview(
        _track(), {"enabled": True, "style": "line"}, job_id="job-1"
    )

    assert output_url == "https://example.com/preview.mp4"
    cmd = meta["ffmpeg_cmd"]
    assert "-nostdin" in cmd
    assert "yuv420p" in cmd
    assert "+faststart" in cmd
    assert "-shortest" in cmd
    assert any("subtitles=" in part for part in cmd)
    # Audio codec must be AAC for cross-browser playback (Safari + iOS won't
    # play opus or vorbis in mp4). _encoding_args pulls in BODY_SLOT_AUDIO_OUT_ARGS
    # which sets this — pin it so a future refactor of that constant can't
    # silently break the preview's browser playback.
    assert "aac" in cmd

    # Encoder policy (test_encoder_policy.py) locks the preset class but NOT
    # the CRF literal. Pin CRF here so a future tweak forces a conscious
    # quality-budget decision rather than a silent preset/CRF drift.
    assert "-crf" in cmd
    crf_value = cmd[cmd.index("-crf") + 1]
    assert crf_value == PREVIEW_CRF, (
        f"preview CRF drifted to {crf_value!r} — update PREVIEW_CRF constant + this test"
    )
    assert "ultrafast" not in cmd  # regression guard for the v0 → v1 flip

    # -t is the layer that actually caps the final MP4 duration; -shortest
    # alone is not enough because lavfi `color=...` is an infinite source.
    # The 5s test track (first line at 1.0s, anchor=0) resolves to a 5s preview.
    assert "-t" in cmd
    t_value = cmd[cmd.index("-t") + 1]
    assert t_value == "5.000", f"unexpected -t cap {t_value!r}, expected 5.000s"
    assert meta["preview_duration_s"] == 5.0
    # `-ss` must appear once (the audio input-seek). Default fixture first
    # line is 1.0s < LEAD_IN_S=2.0, so anchor clamps to 0.000s.
    assert cmd.count("-ss") == 1
    ss_value = cmd[cmd.index("-ss") + 1]
    assert ss_value == "0.000", f"unexpected -ss anchor {ss_value!r}, expected 0.000s"
    assert meta["preview_start_s"] == 0.0


def test_render_lyrics_preview_lavfi_source_uses_output_settings(
    monkeypatch, tmp_path: Path
) -> None:
    """The lavfi black-canvas spec must read from `settings.output_*` (not hardcoded
    1080x1920:r=30). Locks against a future drift between the production output
    resolution and the preview's source resolution.
    """
    from app.config import settings  # noqa: PLC0415

    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    _, meta = render_lyrics_preview(_track(), {"enabled": True, "style": "line"}, job_id="job-1")
    expected = (
        f"color=c=black:s={settings.output_width}x{settings.output_height}:r={settings.output_fps}"
    )
    assert expected in meta["ffmpeg_cmd"], (
        f"lavfi source string {expected!r} not found in cmd: {meta['ffmpeg_cmd']}"
    )


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="exact mp4 duration differs on the macOS brew ffmpeg build vs CI/prod "
    "Linux apt ffmpeg; the dev-loop gate runs on macOS, CI (Linux) still runs this",
)
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg + ffprobe required for the duration-cap integration test",
)
@pytest.mark.timeout(180)
def test_render_lyrics_preview_final_mp4_duration_caps_at_window(
    monkeypatch, tmp_path: Path
) -> None:
    """Integration: with a 60-second audio source and a long-duration track,
    the final MP4 must be ≤ PREVIEW_WINDOW_S (with a small encoder tolerance).

    This is the only test that actually executes FFmpeg + ffprobe. It catches
    a class of bug the mocked tests can't: that `-shortest` plus an infinite
    lavfi color source would otherwise let the output run for the full audio
    duration. The fix layer is the explicit `-t {preview_duration_s}` flag
    emitted by `_build_preview_ffmpeg_cmd`.

    History: previous revision relied on `-shortest` alone, which silently
    rendered 3-minute previews because lavfi `color=...` never ends.
    """
    # Build a 60-second AAC audio file so the audio source is far longer than
    # the 20s window. If `-t` is missing or wrong, the output MP4 will be ~60s.
    long_audio = tmp_path / "audio.aac"
    audio_build = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=60",
            "-c:a",
            "aac",
            str(long_audio),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert audio_build.returncode == 0, audio_build.stderr.decode(errors="replace")[-500:]
    assert long_audio.exists() and long_audio.stat().st_size > 0

    # Capture the final MP4 by mocking the GCS upload to copy the local file
    # out of the tempdir before render_lyrics_preview returns and tears it down.
    captured: dict[str, Path] = {}

    def fake_download(_gcs_path: str, local_path: str) -> None:
        # Stand in for GCS: copy our 60s synthetic audio into the renderer's
        # tempdir so the real ffmpeg can mux it.
        shutil.copyfile(str(long_audio), local_path)

    def fake_upload(local_path: str, _object_path: str) -> str:
        captured_path = tmp_path / "final_output.mp4"
        shutil.copyfile(local_path, captured_path)
        captured["mp4"] = captured_path
        return f"https://example.com/{Path(local_path).name}"

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.upload_public_read", fake_upload)

    # Track claims 185s duration — well past the 20s window. _resolve_preview_window
    # must clamp to PREVIEW_WINDOW_S, and the FFmpeg `-t` must follow.
    track = _track(duration_s=185.0)
    output_url, meta = render_lyrics_preview(
        track, {"enabled": True, "style": "line"}, job_id="job-1"
    )

    assert output_url.startswith("https://"), output_url
    assert meta["preview_duration_s"] == PREVIEW_WINDOW_S, (
        f"resolver returned {meta['preview_duration_s']}, expected {PREVIEW_WINDOW_S}"
    )

    # ffprobe the captured MP4: format.duration must be ~20s, NOT ~60s.
    mp4_path = captured["mp4"]
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(mp4_path),
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    duration_str = probe.stdout.decode().strip()
    actual_duration_s = float(duration_str)

    # Encoder rounding can push the actual output 0.1–0.5s past the requested
    # -t value (closed-GOP boundary alignment + audio frame boundaries).
    # Reject anything that's clearly wrong (running into the 60s audio).
    assert actual_duration_s <= PREVIEW_WINDOW_S + 0.5, (
        f"final MP4 ran for {actual_duration_s:.2f}s — should be ≤ {PREVIEW_WINDOW_S}s. "
        f"-t cap is not bounding the output. Cmd was: {meta['ffmpeg_cmd']}"
    )
    # And it should be close to 20s, not 5s — i.e. the clamp actually ran the
    # whole 20-second window when the source allows it.
    assert actual_duration_s >= PREVIEW_WINDOW_S - 0.5, (
        f"final MP4 ran for {actual_duration_s:.2f}s — clamp truncated too aggressively"
    )


# ── Auto-anchor behavior ──────────────────────────────────────────────────────
#
# Empirical regression case: Billie Jean (track 9a5d0b3f-…) — first lyric line
# at 30.80s, track duration 295.84s. Under the prior `[0, 20s]` policy,
# `_select_section_lines` rejected every line and the preview failed with
# "Lyric preview produced no renderable lyric overlays." (job 12e93b45-…,
# 2026-05-25). These tests pin the anchored-window behavior so a regression
# can't bring the silent-on-instrumental-intro failure back.


def _track_with_first_line_at(start_s: float, **overrides):
    """Variant fixture: `_track()` with the line's `start_s` overridden."""
    track = _track(**overrides)
    track.lyrics_cached = {
        "source": "lrclib_synced+whisper",
        "lines": [
            {
                "text": "hello world",
                "start_s": start_s,
                "end_s": start_s + 1.0,
                "words": [
                    {"text": "hello", "start_s": start_s, "end_s": start_s + 0.5},
                    {"text": "world", "start_s": start_s + 0.5, "end_s": start_s + 1.0},
                ],
            }
        ],
    }
    return track


def test_preview_anchors_at_first_lyric_line_when_intro_exceeds_lead_in() -> None:
    """Billie-Jean-style: first vocal at 30.80s, 295.84s track. Anchor at
    `30.80 - LEAD_IN_S` so the dashboard renders the song's body, not 20s of
    silent intro that would trip "no renderable lyric overlays".
    """
    track = _track_with_first_line_at(30.80, duration_s=295.841)
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S
    # The injector receives [best_start_s, best_end_s] = [28.80, 48.80] and
    # rebases the line: start_s=30.80 in absolute → 30.80-28.80=2.00 in
    # section-relative coords. Confirms the anchor flows end-to-end.
    overlays = recipe["slots"][0].get("text_overlays") or []
    assert overlays, "expected at least one lyric overlay in anchored window"
    # The line's section-relative start = max(0, line_start - pre_roll - best_start_s).
    # `_inject_line` adds pre_roll = 0.40s by default, so:
    #   overlay.start_s ≈ max(0, 30.80 - 0.40 - 28.80) = 1.60s
    assert overlays[0]["start_s"] == pytest.approx(1.60, abs=0.01)


def test_preview_anchor_clamps_to_zero_when_first_line_within_lead_in() -> None:
    """Tracks whose first lyric is closer to t=0 than LEAD_IN_S stay anchored
    at 0 — the lead-in is a maximum pre-vocal buffer, not a forced one.
    Preserves the byte-identical guarantee with the existing 5s test fixture.
    """
    track = _track_with_first_line_at(1.5, duration_s=20.0)
    assert 1.5 < LEAD_IN_S
    recipe = build_lyrics_preview_recipe(track, {})
    # 20s track from anchor=0 → full 20s window.
    assert recipe["slots"][0]["target_duration_s"] == PREVIEW_WINDOW_S


def test_preview_window_truncates_to_track_tail_when_anchor_near_end() -> None:
    """First lyric at 8s, total 10s track → window is `[6, 10]` = 4s, not 20s.
    Without the tail bound, FFmpeg's `-t` would extend past the audio and the
    preview would render silence after the song ends.
    """
    track = _track_with_first_line_at(8.0, duration_s=10.0)
    recipe = build_lyrics_preview_recipe(track, {})
    assert recipe["slots"][0]["target_duration_s"] == pytest.approx(4.0, abs=1e-3)


def test_render_lyrics_preview_ffmpeg_emits_ss_immediately_before_audio_input(
    monkeypatch, tmp_path: Path
) -> None:
    """`-ss` is an INPUT option and must land between the lavfi color input
    and the audio `-i`. Order-of-args matters in FFmpeg: a misplaced `-ss`
    after the audio `-i` becomes an output-seek (decode-and-discard, slow)
    or, if it lands as the lavfi seek, becomes a no-op against an infinite
    source. This test pins the exact order so a future refactor of
    `_build_preview_ffmpeg_cmd` can't silently break input-seek behavior.
    """

    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
    monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.pipeline.lyrics_preview.upload_public_read",
        lambda _local, _obj: "https://example.com/preview.mp4",
    )

    track = _track_with_first_line_at(30.80, duration_s=295.841)
    _, meta = render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-1")
    cmd = meta["ffmpeg_cmd"]

    # Locate the audio `-i {local_audio}` pair (the SECOND `-i`; the first is
    # the lavfi color source).
    i_indices = [i for i, tok in enumerate(cmd) if tok == "-i"]
    assert len(i_indices) == 2, f"expected exactly 2 -i args, got {len(i_indices)}: {cmd}"
    audio_i_idx = i_indices[1]
    # `-ss <value> -i <audio>` — the two tokens before the audio `-i` must be
    # `-ss` and its numeric value.
    assert cmd[audio_i_idx - 2] == "-ss", f"-ss is not immediately before the audio -i; cmd: {cmd}"
    assert cmd[audio_i_idx - 1] == "28.800", (
        f"unexpected -ss value {cmd[audio_i_idx - 1]!r}, expected 28.800"
    )
    assert meta["preview_start_s"] == pytest.approx(28.80, abs=1e-3)
    assert meta["preview_duration_s"] == PREVIEW_WINDOW_S


def test_preview_raises_when_first_lyric_anchor_exceeds_track_duration() -> None:
    """Corrupted-row defense: if a backfill / manual edit puts the first lyric
    `start_s` past the track's duration (e.g. duration=10s but first line at
    15s — only possible from bad data), `_resolve_preview_window` raises
    `LyricsPreviewInputError` rather than shipping a zero-or-negative-length
    preview that would silently produce a broken MP4. Locks the exact
    exception type + message so a future refactor can't downgrade to a
    silent return.
    """
    track = _track_with_first_line_at(15.0, duration_s=10.0)
    with pytest.raises(LyricsPreviewInputError, match="exceeds track duration"):
        build_lyrics_preview_recipe(track, {})


def test_render_lyrics_preview_writes_per_job_path() -> None:
    """Two preview jobs for the same track must produce distinct GCS object
    paths so a later job does not silently overwrite an earlier one. The bug
    being guarded against: job A's status row stored URL `/.../K.mp4` and
    job B's render then wrote to the same `K.mp4`, so admins watching the
    status response for job A saw bytes from job B's render. Verifies the
    `{track_id}/{style}/{job_id}/...` namespacing directly without standing up the
    full FFmpeg + GCS path.
    """
    track = SimpleNamespace(
        id="track-A",
        audio_gcs_path="music/track-A/audio.m4a",
        duration_s=60.0,
        track_config={},
        lyrics_cached={
            "source": "lrclib_synced+whisper",
            "lines": [{"text": "x", "start_s": 1.0, "end_s": 2.0}],
        },
    )

    captured: list[str] = []

    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    def fake_upload(_local: str, object_path: str) -> str:
        captured.append(object_path)
        return f"https://example.com/{object_path}"

    import pytest as _pytest  # noqa: PLC0415

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
        monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
        monkeypatch.setattr("app.pipeline.lyrics_preview.upload_public_read", fake_upload)

        render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-1")
        render_lyrics_preview(track, {"enabled": True, "style": "line"}, job_id="job-2")
    finally:
        monkeypatch.undo()

    assert captured == [
        "music-lyrics-previews/track-A/line/job-1/lyrics-preview.mp4",
        "music-lyrics-previews/track-A/line/job-2/lyrics-preview.mp4",
    ], f"expected per-job paths, got {captured}"


def test_render_lyrics_preview_writes_per_style_path() -> None:
    """Two preview jobs against the same track in different styles must land
    in distinct GCS prefixes so concurrent multi-style renders never overwrite
    each other. Pre-fix: every preview wrote to ``{track_id}/{job_id}/...``
    AND the route hardcoded ``style: "line"``, so the admin dashboard could
    not even render Pop-up or Karaoke. This test pins the post-fix layout
    ``{track_id}/{style}/{job_id}/...`` and the per-style token mapping
    (``per-word-pop`` collapses to ``popup`` for URL friendliness).
    """
    track = SimpleNamespace(
        id="track-A",
        audio_gcs_path="music/track-A/audio.m4a",
        duration_s=60.0,
        track_config={},
        lyrics_cached={
            "source": "lrclib_synced+whisper",
            "lines": [
                {
                    "text": "hello",
                    "start_s": 1.0,
                    "end_s": 2.0,
                    "words": [
                        {"text": "hello", "start_s": 1.0, "end_s": 2.0},
                    ],
                }
            ],
        },
    )

    captured: list[str] = []

    def fake_download(_gcs_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"audio")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0, stderr=b"")

    def fake_upload(_local: str, object_path: str) -> str:
        captured.append(object_path)
        return f"https://example.com/{object_path}"

    import pytest as _pytest  # noqa: PLC0415

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("app.pipeline.lyrics_preview.download_to_file", fake_download)
        monkeypatch.setattr("app.pipeline.lyrics_preview.subprocess.run", fake_run)
        monkeypatch.setattr("app.pipeline.lyrics_preview.upload_public_read", fake_upload)

        for style in ("line", "karaoke", "per-word-pop"):
            render_lyrics_preview(track, {"enabled": True, "style": style}, job_id=f"job-{style}")
    finally:
        monkeypatch.undo()

    assert captured == [
        "music-lyrics-previews/track-A/line/job-line/lyrics-preview.mp4",
        "music-lyrics-previews/track-A/karaoke/job-karaoke/lyrics-preview.mp4",
        "music-lyrics-previews/track-A/popup/job-per-word-pop/lyrics-preview.mp4",
    ], f"per-style path namespace drifted: {captured}"


def test_build_lyrics_preview_recipe_honors_style_override(tmp_path: Path) -> None:
    """Pre-fix the preview module hardcoded ``style: "line"`` inside
    ``build_lyrics_preview_recipe``, so the dashboard could never render Pop-up
    or Karaoke regardless of what the admin selected. Asserts the chosen
    style now reaches ``inject_lyric_overlays`` unchanged, by inspecting the
    effect on the resulting overlay (each style emits a distinct effect tag:
    ``lyric-line`` for line, ``karaoke-line`` for karaoke).
    """
    track = _track()

    recipe_line = build_lyrics_preview_recipe(track, {"enabled": True, "style": "line"})
    recipe_karaoke = build_lyrics_preview_recipe(track, {"enabled": True, "style": "karaoke"})

    line_effects = {
        o.get("effect")
        for slot in recipe_line.get("slots", [])
        for o in slot.get("text_overlays", [])
    }
    karaoke_effects = {
        o.get("effect")
        for slot in recipe_karaoke.get("slots", [])
        for o in slot.get("text_overlays", [])
    }

    assert "lyric-line" in line_effects, f"expected lyric-line, got {line_effects}"
    assert "karaoke-line" in karaoke_effects, (
        f"expected karaoke-line (style passthrough), got {karaoke_effects}"
    )
    assert "lyric-line" not in karaoke_effects, (
        "style override leaked: karaoke recipe must not contain lyric-line overlays"
    )


def test_build_lyrics_preview_recipe_clamps_overlapping_karaoke_lines() -> None:
    lyrics_cached = {
        "source": "lrclib_synced+whisper",
        "lines": [
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
        ],
    }
    track = _track(
        duration_s=17.0,
        track_config={"best_start_s": 13.0, "best_end_s": 16.5},
        lyrics_cached=lyrics_cached,
    )

    recipe = build_lyrics_preview_recipe(track, {"enabled": True, "style": "karaoke"})
    overlays = recipe["slots"][0]["text_overlays"]

    assert len(overlays) == 2
    first, second = overlays
    assert first["effect"] == "karaoke-line"
    assert first["end_s"] == pytest.approx(second["start_s"], abs=1e-3)
    assert first["section_end_anchor_s"] == pytest.approx(second["section_anchor_s"], abs=1e-3)


def test_first_line_start_s_rejects_non_finite_floats() -> None:
    """`float("nan")` and `float("inf")` both succeed and would propagate past
    the `<=` comparison in `_resolve_preview_window` (all NaN comparisons
    return False), ending up as FFmpeg `-ss nan` (FFmpeg errors out) or as
    `NaN` in the JSON status response (frontend renders "NaN:NaN"). The
    `math.isfinite` guard inside `_first_line_start_s` is what stops this.
    """
    from app.pipeline.lyrics_preview import _first_line_start_s  # noqa: PLC0415

    assert _first_line_start_s({"lines": [{"start_s": float("nan")}]}) is None
    assert _first_line_start_s({"lines": [{"start_s": float("inf")}]}) is None
    assert _first_line_start_s({"lines": [{"start_s": float("-inf")}]}) is None
    # String "nan" / "inf" survive `float()`; the finite guard must also reject these.
    assert _first_line_start_s({"lines": [{"start_s": "nan"}]}) is None
    assert _first_line_start_s({"lines": [{"start_s": "inf"}]}) is None
    # Mixed finite + non-finite: returns min of finite values only.
    assert (
        _first_line_start_s(
            {"lines": [{"start_s": float("nan")}, {"start_s": 5.0}, {"start_s": 2.0}]}
        )
        == 2.0
    )


def test_first_line_start_s_handles_malformed_cache_inputs() -> None:
    """`_first_line_start_s` has four guard paths that all return None:
    (a) non-dict cache, (b) `lines` missing/empty, (c) non-dict line entries,
    (d) non-numeric `start_s`. Plus a contract from its docstring: it must
    `min()` across the array so an unsorted backfill still picks the right
    anchor. None of these were exercised by the higher-level tests because
    the route's empty-lines guard short-circuits most of them. Tests them
    directly so a refactor that collapses one branch surfaces here.
    """
    from app.pipeline.lyrics_preview import _first_line_start_s  # noqa: PLC0415

    # (a) non-dict cache
    assert _first_line_start_s(None) is None
    assert _first_line_start_s("not a dict") is None
    # (b) empty / missing lines
    assert _first_line_start_s({}) is None
    assert _first_line_start_s({"lines": []}) is None
    # (c) non-dict line entries skipped
    assert _first_line_start_s({"lines": ["not a dict", 42, None]}) is None
    # (d) non-numeric / missing start_s skipped; if every entry is bad → None
    assert _first_line_start_s({"lines": [{"start_s": "abc"}, {"start_s": None}]}) is None
    # Mixed valid + invalid: returns min across valid entries only.
    assert (
        _first_line_start_s({"lines": [{"start_s": "x"}, {"start_s": 5.0}, {"start_s": 2.0}]})
        == 2.0
    )
    # Unsorted lines — docstring promises min() across the array, not lines[0].
    assert (
        _first_line_start_s({"lines": [{"start_s": 9.0}, {"start_s": 3.0}, {"start_s": 7.0}]})
        == 3.0
    )
