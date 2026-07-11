"""Plan 006 backend: content-aware trim, pacing, freeze cap, backfill guards.

Pins the eng-review decisions:
  B  — pick_trim_window clamps (start ∈ [0, dur−window], None-duration ⇒ no trim),
       trim computed from the FINAL window, MediaOverlay cross-field sanitizer.
  C  — analysis_is_stale: stubs NEVER stale (keyless loop guard); version-keyed.
  D  — pacing_cap_s(main) = clamp(main×0.15, 4, 10); freeze cap ≤ 1s on video cards.
  2-A — pacing applies to ALL card kinds (image regression is pacing-scoped).
"""

from __future__ import annotations

from app.agents._schemas.media_overlay import MediaOverlay
from app.agents.overlay_placement import RawPlacement
from app.services.overlay_autoplace import (
    build_suggestions,
    pacing_cap_s,
    pick_trim_window,
)
from app.tasks.autoplace import ANALYSIS_VERSION, analysis_is_stale

# ── pacing (decision D + 2-A) ─────────────────────────────────────────────────


def test_pacing_cap_matrix() -> None:
    assert pacing_cap_s(20.0) == 4.0  # floor
    assert pacing_cap_s(60.0) == 9.0  # 60 × 0.15
    assert pacing_cap_s(120.0) == 10.0  # ceiling


# ── pick_trim_window (decision B) ─────────────────────────────────────────────

MOMENTS = [
    {"start_s": 2.0, "end_s": 12.0, "energy": 7.0, "description": "demo"},
    {"start_s": 20.0, "end_s": 23.0, "energy": 9.0, "description": "peak"},
]


def test_trim_picks_highest_energy_fitting_moment() -> None:
    # window 5s: only the first moment (10s long) fits; peak (3s) does not.
    assert pick_trim_window(MOMENTS, 5.0, 30.0) == (2.0, 7.0)


def test_trim_falls_back_to_highest_energy_when_none_fit() -> None:
    # window 8s: no moment ≥ 8s... first is 10s — use window 15 so none fit.
    trim = pick_trim_window(MOMENTS, 15.0, 30.0)
    assert trim is not None
    start, end = trim
    assert start == 15.0  # clamped into [0, 30−15]; peak start 20 → 15
    assert end == 30.0


def test_trim_clamps_out_of_range_moment_start() -> None:
    # Gemini hallucination: moment beyond real duration must never produce
    # trim=start≥end (the whole-render ffmpeg crash class).
    moments = [{"start_s": 25.0, "end_s": 40.0, "energy": 5.0, "description": "x"}]
    trim = pick_trim_window(moments, 6.0, 20.0)
    assert trim == (14.0, 20.0)  # start clamped to dur−window


def test_trim_none_when_duration_unknown_or_no_moments() -> None:
    assert pick_trim_window(MOMENTS, 5.0, None) is None
    assert pick_trim_window(MOMENTS, 5.0, 0) is None
    assert pick_trim_window([], 5.0, 30.0) is None
    assert pick_trim_window([{"bad": "shape"}], 5.0, 30.0) is None


# ── build_suggestions: freeze cap + trim ordering + image regression ──────────


def _asset(kind: str = "video", duration: float | None = 30.0, moments=None) -> dict:
    return {
        "id": "a1",
        "gcs_path": "users/u/plan/i/pool/x.mp4" if kind == "video" else "users/u/plan/i/pool/x.png",
        "kind": kind,
        "source_filename": "x.mp4",
        "duration_s": duration,
        "aspect": 1.78,
        "analysis": {"best_moments": moments or [], "analysis_version": 2},
    }


def _placement(start: float = 5.0, end: float = 11.0) -> RawPlacement:
    return RawPlacement(
        asset_id="a1",
        slot="top",
        start_s=start,
        end_s=end,
        confidence_tier="confident",
        reason="r",
        transcript_anchor="a",
        sfx_intent="none",
    )


WORDS = [{"word": "w", "start_s": 5.0, "end_s": 5.4}]


def test_video_trim_uses_final_window_and_lands_in_envelope() -> None:
    # Main 60s → cap 9s; requested window 6s stays; trim = moment-based 6s slice.
    out = build_suggestions(
        [_placement(5.0, 11.0)],
        assets_by_id={"a1": _asset(moments=MOMENTS)},
        words=WORDS,
        duration_s=60.0,
        occupied=[],
        glossary=[],
    )
    assert len(out) == 1
    o = out[0]["overlay"]
    assert o["clip_trim_start_s"] == 2.0
    assert o["clip_trim_end_s"] == 8.0  # start + FINAL window (6s) — ordering pinned
    assert o["clip_duration_s"] == 30.0
    # Sanity: trim window ⊆ [0, duration] and length == on-screen window.
    assert 0 <= o["clip_trim_start_s"] < o["clip_trim_end_s"] <= 30.0
    assert abs((o["clip_trim_end_s"] - o["clip_trim_start_s"]) - (o["end_s"] - o["start_s"])) < 0.01


def test_freeze_cap_shrinks_window_for_short_video_asset() -> None:
    # 3s asset in a 9s request on a 60s main: window tightens to 3+1=4s (≤1s freeze).
    out = build_suggestions(
        [_placement(5.0, 14.0)],
        assets_by_id={"a1": _asset(duration=3.0)},
        words=WORDS,
        duration_s=60.0,
        occupied=[],
        glossary=[],
    )
    assert len(out) == 1
    o = out[0]["overlay"]
    assert o["end_s"] - o["start_s"] == 4.0  # asset 3s + 1s freeze allowance


def test_pacing_caps_image_windows_too() -> None:
    # 2-A: 20s main → cap 4s applies to IMAGE cards as well.
    out = build_suggestions(
        [_placement(5.0, 14.0)],
        assets_by_id={"a1": _asset(kind="image", duration=None)},
        words=WORDS,
        duration_s=20.0,
        occupied=[],
        glossary=[],
    )
    assert len(out) == 1
    o = out[0]["overlay"]
    assert o["end_s"] - o["start_s"] == 4.0
    # Image regression: trim never set on image cards (None = "no trim" in the
    # render path; the envelope round-trip materializes keys with None values).
    assert o["clip_trim_start_s"] is None
    assert o["clip_trim_end_s"] is None


def test_video_without_moments_gets_no_trim() -> None:
    # The 1b backfill path: stale/moment-less analysis ⇒ plays from 0:00.
    out = build_suggestions(
        [_placement(5.0, 11.0)],
        assets_by_id={"a1": _asset(moments=[])},
        words=WORDS,
        duration_s=60.0,
        occupied=[],
        glossary=[],
    )
    assert len(out) == 1
    assert out[0]["overlay"]["clip_trim_start_s"] is None
    assert out[0]["overlay"]["clip_trim_end_s"] is None


# ── analysis_is_stale (decision C — the loop guard) ───────────────────────────


def test_stub_analysis_is_never_stale() -> None:
    assert analysis_is_stale({"source": "stub"}) is False
    assert analysis_is_stale({"source": "stub", "analysis_version": 1}) is False


def test_pre_006_real_analysis_is_stale() -> None:
    assert analysis_is_stale({"source": "clip_metadata"}) is True
    assert analysis_is_stale({"source": "clip_metadata", "analysis_version": 1}) is True


def test_current_real_analysis_is_fresh() -> None:
    assert (
        analysis_is_stale({"source": "clip_metadata", "analysis_version": ANALYSIS_VERSION})
        is False
    )


def test_v3_video_analysis_is_not_stale_for_image_only_alpha_bump() -> None:
    assert (
        analysis_is_stale({"source": "clip_metadata", "analysis_version": 3}, kind="video") is False
    )


def test_v3_image_analysis_is_stale_for_alpha_bump() -> None:
    assert (
        analysis_is_stale({"source": "image_metadata", "analysis_version": 3}, kind="image") is True
    )


def test_v2_analysis_is_stale_for_any_kind() -> None:
    assert analysis_is_stale({"source": "clip_metadata", "analysis_version": 2}, kind="video")
    assert analysis_is_stale({"source": "image_metadata", "analysis_version": 2}, kind="image")


def test_v4_image_analysis_is_fresh() -> None:
    assert (
        analysis_is_stale({"source": "image_metadata", "analysis_version": 4}, kind="image")
        is False
    )


# ── MediaOverlay cross-field trim sanitizer (decision B) ──────────────────────


def _card(**kw) -> dict:
    base = {
        "id": "c1",
        "kind": "video",
        "src_gcs_path": "users/u/plan/i/pool/x.mp4",
        "start_s": 0.0,
        "end_s": 3.0,
    }
    base.update(kw)
    return base


def test_schema_drops_inverted_trim_pair() -> None:
    card = MediaOverlay.model_validate(_card(clip_trim_start_s=25.0, clip_trim_end_s=20.0))
    assert card.clip_trim_start_s is None and card.clip_trim_end_s is None


def test_schema_clamps_trim_end_to_clip_duration() -> None:
    card = MediaOverlay.model_validate(
        _card(clip_trim_start_s=2.0, clip_trim_end_s=40.0, clip_duration_s=10.0)
    )
    assert card.clip_trim_end_s == 10.0


def test_schema_drops_trim_start_beyond_duration() -> None:
    card = MediaOverlay.model_validate(
        _card(clip_trim_start_s=15.0, clip_trim_end_s=18.0, clip_duration_s=10.0)
    )
    assert card.clip_trim_start_s is None and card.clip_trim_end_s is None


def test_schema_keeps_valid_trim_pair() -> None:
    card = MediaOverlay.model_validate(
        _card(clip_trim_start_s=2.0, clip_trim_end_s=8.0, clip_duration_s=10.0)
    )
    assert (card.clip_trim_start_s, card.clip_trim_end_s) == (2.0, 8.0)
