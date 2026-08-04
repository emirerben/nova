"""Tests for the carousel DIRECTOR (`app.pipeline.carousel.director`) — the
deterministic heuristic that picks mode/effect/focus per generative variant.
See `director.py`'s module docstring for the full rule set; this file pins
each rule plus the seed-determinism and diversify() contracts.

No skia, no ffmpeg — pure stdlib logic only.

`choreography.FocusMoment` and `segment.CarouselMomentSpec`'s `mode`/
`focus_moments`/`seed` fields are being landed by a concurrent lane and may
not exist yet when this file runs (director.py is written to code against
them regardless — see its module docstring's "code against schemas that may
not exist yet" framing). The autouse `_stub_carousel_schemas` fixture below
makes these tests meaningful either way:
  - if the real classes already have the documented shape, it's a no-op;
  - otherwise it injects a stand-in matching that exact shape, scoped to a
    single test via `monkeypatch` (auto-reverted after each test), so this
    suite validates `director.py`'s actual contract instead of skipping it.
This fixture becomes dead weight (harmless) once both land for real and can
be deleted then.
"""

from __future__ import annotations

import dataclasses
import sys
import types

import pytest

import app.pipeline.carousel.director as director
from app.pipeline.carousel.effects import EFFECTS


@pytest.fixture(autouse=True)
def _stub_carousel_schemas(monkeypatch):
    # --- choreography.FocusMoment -------------------------------------------
    try:
        from app.pipeline.carousel.choreography import FocusMoment  # noqa: F401
    except ImportError:

        @dataclasses.dataclass(frozen=True)
        class _FocusMomentStub:
            card_index: int
            hold_s: float = 2.0
            zoom_s: float = 0.6

        stub_mod = types.ModuleType("app.pipeline.carousel.choreography")
        stub_mod.FocusMoment = _FocusMomentStub
        monkeypatch.setitem(sys.modules, "app.pipeline.carousel.choreography", stub_mod)

    # --- segment.CarouselMomentSpec mode/focus_moments/seed fields ---------
    import app.pipeline.carousel.segment as segment_mod

    existing_fields = {f.name for f in dataclasses.fields(segment_mod.CarouselMomentSpec)}
    if not {"mode", "focus_moments", "seed"} <= existing_fields:

        @dataclasses.dataclass(frozen=True)
        class _CarouselMomentSpecStub:
            effect: str
            clip_paths: tuple
            duration_s: float = 4.0
            mode: str = "stills"
            focus_moments: tuple = ()
            seed: int = 0

        monkeypatch.setattr(segment_mod, "CarouselMomentSpec", _CarouselMomentSpecStub)

    yield


def _clips(
    *durations: float, interests: tuple[float, ...] | None = None
) -> list[director.ClipInfo]:
    interests = interests or tuple(0.5 for _ in durations)
    return [
        director.ClipInfo(path=f"/clip_{i}.mp4", duration_s=d, interest=interest)
        for i, (d, interest) in enumerate(zip(durations, interests))
    ]


# ── mode rules ───────────────────────────────────────────────────────────


def test_mode_weights_qualified_includes_focus():
    clips = _clips(3.0, 3.0, 3.0)
    weights = director._mode_weights(clips)
    assert weights == {"focus": 0.5, "rolling": 0.35, "stills": 0.15}


def test_mode_weights_unqualified_excludes_focus_too_few_clips():
    clips = _clips(3.0, 3.0)  # only 2 clips
    weights = director._mode_weights(clips)
    assert "focus" not in weights
    assert weights == {"rolling": 0.6, "stills": 0.4}


def test_mode_weights_unqualified_excludes_focus_short_clip():
    clips = _clips(3.0, 3.0, 2.0)  # 3rd clip below the 2.5s floor
    weights = director._mode_weights(clips)
    assert "focus" not in weights


def test_short_clips_never_get_focus_across_many_seeds():
    clips = _clips(1.0, 1.5, 2.0)
    for seed in range(50):
        spec = director.direct_carousel_moment(clips, seed=seed)
        assert spec.mode != "focus"


# ── effect rules ─────────────────────────────────────────────────────────


def test_effect_weights_large_deck_favors_cover_flow_and_flipbook():
    clips = _clips(2.0, 10.0, 3.0, 15.0)  # 4 clips, deliberately non-homogeneous durations
    weights = director._effect_weights(clips)
    assert weights["cover_flow"] == 2.0
    assert weights["flipbook"] == 2.0
    assert weights["scale_sweep"] == 1.0
    assert weights["cards_stack"] == 1.0


def test_effect_weights_three_clips_favors_scale_sweep():
    clips = _clips(3.0, 5.0, 8.0)  # 3 clips, non-homogeneous durations
    weights = director._effect_weights(clips)
    assert weights["scale_sweep"] == 2.0
    assert weights["cover_flow"] == 1.0
    assert weights["flipbook"] == 1.0


def test_effect_weights_homogeneous_durations_favor_cards_stack():
    clips = _clips(4.0, 4.02, 3.98, 4.01, 3.99)  # 5 clips, near-identical durations
    weights = director._effect_weights(clips)
    assert weights["cards_stack"] == 2.0


def test_effect_weights_non_homogeneous_durations_do_not_favor_cards_stack():
    clips = _clips(2.0, 10.0)
    weights = director._effect_weights(clips)
    assert weights["cards_stack"] == 1.0


def test_effect_selection_respects_allowed_effects_filter():
    clips = _clips(3.0, 3.0, 3.0, 3.0)
    for seed in range(30):
        spec = director.direct_carousel_moment(clips, seed=seed, allowed_effects=("cards_stack",))
        assert spec.effect == "cards_stack"


# ── focus target ranking + duration floor ───────────────────────────────


def test_rank_focus_candidates_orders_by_interest_then_duration():
    clips = _clips(3.0, 5.0, 5.0, 2.5, interests=(0.2, 0.9, 0.9, 0.9))
    ranked = director._rank_focus_candidates(clips)
    # Highest interest wins (indices 1, 2, 3 tie at 0.9); among the tie,
    # longer duration wins (index 3 has 2.5s, loses to 1 & 2's 5.0s); among
    # the remaining tie (1 vs 2, same interest AND duration), original list
    # position breaks it (stable sort) -> 1 before 2. Index 0 (lowest
    # interest) is last.
    assert ranked == [1, 2, 3, 0]


def test_build_focus_moments_honors_duration_floor():
    # Highest-interest candidate (index 0) is too short to ever clear the
    # hold_s(>=1.5) + 1.5s floor; the runner-up (index 1) is long enough for
    # any jitter draw. Must skip 0 and land on 1.
    clips = [
        director.ClipInfo(path="/short.mp4", duration_s=2.6, interest=0.9),
        director.ClipInfo(path="/long.mp4", duration_s=10.0, interest=0.5),
    ]
    rng = director.random.Random(0)
    built = director._build_focus_moments(clips, rng, target_duration_s=6.0)
    assert built is not None
    assert len(built) == 1
    assert built[0].card_index == 1


def test_build_focus_moments_returns_none_when_no_candidate_clears_floor():
    # Every clip qualifies for "focus" mode (>= 2.5s) but none is long enough
    # to clear even the minimum possible floor (hold_s_min=1.5 + 1.5 = 3.0s).
    clips = _clips(2.5, 2.5, 2.6)
    rng = director.random.Random(0)
    built = director._build_focus_moments(clips, rng, target_duration_s=6.0)
    assert built is None


def test_unqualified_focus_falls_back_to_rolling_end_to_end():
    clips = _clips(2.5, 2.5, 2.6)  # qualifies for mode="focus" but no candidate clears the floor
    for seed in range(20):
        spec = director.direct_carousel_moment(clips, seed=seed, allowed_modes=("focus",))
        assert spec.mode == "rolling"
        assert spec.focus_moments == ()


def test_focus_target_count_scales_with_duration():
    assert director._focus_target_count(6.0) == 1
    assert director._focus_target_count(8.0) == 1
    assert director._focus_target_count(8.01) == 2
    assert director._focus_target_count(15.0) == 2


def test_two_focus_targets_for_long_moment():
    clips = _clips(6.0, 6.0, 6.0, 6.0, interests=(0.9, 0.8, 0.7, 0.6))
    rng = director.random.Random(0)
    built = director._build_focus_moments(clips, rng, target_duration_s=10.0)
    assert built is not None
    assert len(built) == 2
    assert built[0].card_index == 0
    assert built[1].card_index == 1


# ── seed determinism ─────────────────────────────────────────────────────


def test_same_seed_same_inputs_produces_identical_spec():
    clips = _clips(3.0, 4.0, 5.0, 2.5)
    a = director.direct_carousel_moment(clips, seed=42)
    b = director.direct_carousel_moment(clips, seed=42)
    assert a == b


def test_different_seeds_eventually_differ():
    clips = _clips(3.0, 4.0, 5.0, 2.5)
    specs = [director.direct_carousel_moment(clips, seed=s) for s in range(20)]
    pairs = {(s.mode, s.effect) for s in specs}
    assert len(pairs) > 1, "expected at least two distinct (mode, effect) pairs across 20 seeds"


# ── clip order preserved ─────────────────────────────────────────────────


def test_clip_paths_preserve_input_order():
    clips = [
        director.ClipInfo(path="/z.mp4", duration_s=3.0),
        director.ClipInfo(path="/a.mp4", duration_s=4.0),
        director.ClipInfo(path="/m.mp4", duration_s=5.0),
    ]
    spec = director.direct_carousel_moment(clips, seed=7)
    assert spec.clip_paths == ("/z.mp4", "/a.mp4", "/m.mp4")


def test_duration_s_always_set_to_target_even_for_focus_mode():
    clips = _clips(6.0, 6.0, 6.0)
    spec = director.direct_carousel_moment(
        clips, seed=1, target_duration_s=6.5, allowed_modes=("focus",)
    )
    assert spec.duration_s == 6.5


# ── diversify() ───────────────────────────────────────────────────────────


def test_diversify_produces_distinct_pairs_across_three_calls():
    clips = _clips(3.0, 4.0, 5.0, 2.5, interests=(0.9, 0.5, 0.3, 0.7))
    used: list = []
    for i in range(3):
        spec = director.diversify(used, clips=clips, seed=100 + i)
        used.append(spec)

    pairs = [(s.mode, s.effect) for s in used]
    assert len(set(pairs)) == len(pairs), f"expected 3 distinct (mode, effect) pairs, got {pairs}"


def test_diversify_never_raises_when_pool_exhausted():
    clips = _clips(3.0, 4.0, 5.0)
    # Force a single reachable (mode, effect) pair and pre-seed it as "used"
    # so every attempt collides -> diversify must still return a spec, not
    # raise, once max_attempts is exhausted.
    spec = director.direct_carousel_moment(
        clips, seed=0, allowed_modes=("stills",), allowed_effects=("scale_sweep",)
    )
    used = [spec]
    result = director.diversify(
        used,
        clips=clips,
        seed=0,
        allowed_modes=("stills",),
        allowed_effects=("scale_sweep",),
        max_attempts=5,
    )
    assert result.mode == "stills"
    assert result.effect == "scale_sweep"


def test_diversify_forwards_extra_kwargs_to_direct_carousel_moment():
    clips = _clips(3.0, 4.0, 5.0)
    result = director.diversify(
        [], clips=clips, seed=0, allowed_effects=("flipbook",), target_duration_s=9.0
    )
    assert result.effect == "flipbook"
    assert result.duration_s == 9.0


# ── weighted-choice helper sanity ────────────────────────────────────────


def test_weighted_choice_respects_allowed_restriction_fallback():
    weights = director._restrict_weights({"a": 1.0, "b": 1.0}, ("c",))
    assert weights == {"c": 1.0}


def test_effects_constant_matches_carousel_effects_module():
    assert director.EFFECTS == EFFECTS
