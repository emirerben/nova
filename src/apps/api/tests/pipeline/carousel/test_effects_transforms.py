"""Pure-math unit tests for the four Blossom-carousel effect transforms.

Layout constants mirrored from the browser reference pages (see
app/pipeline/carousel/effects.py module docstring):
    viewport_w = 1080, card_w = 540, card_h = 720, gap = 48
    pitch = card_w + gap = 588
    center_left = (viewport_w - card_w) / 2 = 270
    default_y = (1920 - card_h) / 2 = 600

All expected values below are hand-computed in the surrounding comments.
"""

from __future__ import annotations

import pytest

from app.pipeline.carousel.effects import (
    EFFECTS,
    CardGeometry,
    cards_stack_transform,
    cover_flow_transform,
    flipbook_transform,
    scale_sweep_transform,
    snap_bounds,
    snap_positions,
    transform_for,
    view_progress,
)

VIEWPORT_W = 1080.0
GEO = CardGeometry(card_w=540, card_h=720, gap=48, corner_radius=24)
PITCH = GEO.card_w + GEO.gap  # 588
CENTER_LEFT = (VIEWPORT_W - GEO.card_w) / 2  # 270
DEFAULT_Y = (1920 - GEO.card_h) / 2  # 600


# --------------------------------------------------------------------------- #
# view_progress
# --------------------------------------------------------------------------- #


def test_view_progress_card0_at_scroll0_is_exactly_half() -> None:
    # left(0) = center_left = 270 (card 0 is centered at scroll_x=0)
    # p = (1080 - 270) / (1080 + 540) = 810 / 1620 = 0.5 exactly
    p = view_progress(0.0, 0, GEO, VIEWPORT_W)
    assert p == pytest.approx(0.5)


def test_view_progress_card1_at_scroll0() -> None:
    # left(1) = center_left + 1*pitch = 270 + 588 = 858
    # p = (1080 - 858) / 1620 = 222 / 1620 = 0.137037037...
    p = view_progress(0.0, 1, GEO, VIEWPORT_W)
    assert p == pytest.approx(222 / 1620)
    assert p == pytest.approx(0.13703703703703704)


def test_view_progress_clamps_to_unit_range() -> None:
    # Way off to the right: left(0) = 270 - 5000 = -4730, huge raw p, clamps to 1.
    assert view_progress(5000.0, 0, GEO, VIEWPORT_W) == pytest.approx(1.0)
    # Way off to the left (large negative scroll pushes the card far right):
    # left(0) = 270 - (-5000) = 5270, raw p very negative, clamps to 0.
    assert view_progress(-5000.0, 0, GEO, VIEWPORT_W) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# scale_sweep_transform
# --------------------------------------------------------------------------- #


def test_scale_sweep_centered_card_is_full_scale_and_opaque() -> None:
    # p=0.5 -> t = 1 - |2*0.5 - 1| = 1 - 0 = 1
    # scale = 0.5 + 0.5*1 = 1.0 ; opacity = 0.35 + 0.65*1 = 1.0
    tf = scale_sweep_transform(0.0, 0, GEO, VIEWPORT_W)
    assert tf.scale == pytest.approx(1.0)
    assert tf.opacity == pytest.approx(1.0)
    assert tf.shadow_alpha == pytest.approx(0.25)
    assert tf.rotate_y_deg == 0.0
    assert tf.z_index == 0
    assert tf.x == pytest.approx(CENTER_LEFT)
    assert tf.y == pytest.approx(DEFAULT_Y)


def test_scale_sweep_one_pitch_away() -> None:
    # card_index=1, scroll_x=0 -> d = +588 (one pitch from center)
    # p = 0.5 - 588/1620 = 0.5 - 0.3629629... = 0.1370370...
    # t = 1 - |2p - 1| = 1 - 2*588/1620 = 1 - 0.7259259... = 0.2740740...
    # scale = 0.5 + 0.5*t = 0.6370370...
    # opacity = 0.35 + 0.65*t = 0.5281481...
    tf = scale_sweep_transform(0.0, 1, GEO, VIEWPORT_W)
    t = 1 - 2 * 588 / 1620
    assert t == pytest.approx(0.27407407407407414)
    assert tf.scale == pytest.approx(0.5 + 0.5 * t)
    assert tf.opacity == pytest.approx(0.35 + 0.65 * t)


def test_scale_sweep_fully_off_screen_hits_the_floor() -> None:
    # Pushed far enough that p clamps to 1 -> t = 1 - |2*1 - 1| = 0
    # scale floors at SCALE_SWEEP_MIN=0.5, opacity floors at OPACITY_MIN=0.35
    tf = scale_sweep_transform(5000.0, 0, GEO, VIEWPORT_W)
    assert tf.scale == pytest.approx(0.5)
    assert tf.opacity == pytest.approx(0.35)
    assert tf.shadow_alpha == pytest.approx(0.0)


def test_scale_sweep_symmetric_about_center() -> None:
    # card_index=1 at scroll 0 -> d=+588 ; card_index=-1 at scroll 0 -> d=-588
    # left(-1) = 270 - 588 = -318 ; center_x = -318+270 = -48 ; d = -48-540 = -588
    # t only depends on |d|, so scale/opacity must match.
    ahead = scale_sweep_transform(0.0, 1, GEO, VIEWPORT_W)
    behind = scale_sweep_transform(0.0, -1, GEO, VIEWPORT_W)
    assert ahead.scale == pytest.approx(behind.scale)
    assert ahead.opacity == pytest.approx(behind.opacity)


# --------------------------------------------------------------------------- #
# cover_flow_transform
# --------------------------------------------------------------------------- #


def test_cover_flow_centered_card_is_flat_and_full_scale() -> None:
    # p=view_progress(0,0)=0.5 exactly (card 0 centered at scroll 0) -> m=2p-1=0
    # rotate=35*0=0 ; tz=-200*|0|=0 ; scale=1-0.15*|0|=1
    # z_index=_view_timeline_z_index(0.5,0): p==0.5 is the keyframe PEAK -> 1000
    tf = cover_flow_transform(0.0, 0, GEO, VIEWPORT_W)
    assert tf.rotate_y_deg == pytest.approx(0.0)
    assert tf.scale == pytest.approx(1.0)
    assert tf.z_index == 1000
    assert tf.translate_z_px == pytest.approx(0.0)
    assert tf.opacity == pytest.approx(1.0)
    assert tf.shadow_alpha == pytest.approx(0.0)


def test_cover_flow_one_pitch_away() -> None:
    # card_index=1, scroll_x=0 -> p=view_progress(0,1)=222/1620=0.13703703703703704
    # (same p test_view_progress_card1_at_scroll0 already pins) -> m=2p-1=-0.7259259...
    # rotate=35*m=-25.407407407407405 ; tz=-200*|m|=-145.18518518518516
    # scale=1-0.15*|m|=0.8911111111111111
    # z_index (p<=0.5 branch): round((100-1) + (1000-99)*(p/0.5))
    #                         = round(99 + 901*0.27407407...) = 346
    tf = cover_flow_transform(0.0, 1, GEO, VIEWPORT_W)
    assert tf.rotate_y_deg == pytest.approx(-25.407407407407405)
    assert tf.translate_z_px == pytest.approx(-145.18518518518516)
    assert tf.scale == pytest.approx(0.8911111111111111)
    assert tf.z_index == 346


def test_cover_flow_two_pitches_away_saturates_same_as_further() -> None:
    # card_index=2, scroll_x=0 -> left(2)=270+1176=1446 > viewport_w(1080), so
    # view_progress's raw fraction is negative -> clamped to p=0 (the `cover`
    # range's own 0%/100% boundary; unlike the old pitch-normalized-distance
    # model this replaced, `p` genuinely SATURATES here, it doesn't keep
    # growing past the visual floor — see cover_flow_transform's docstring).
    # m=2*0-1=-1 (the 0% keyframe exactly): rotate=-35, tz=-200, scale=0.85.
    # z_index at p=0 (p<=0.5 branch, fraction=0): round(100-2)=98.
    two = cover_flow_transform(0.0, 2, GEO, VIEWPORT_W)
    assert two.rotate_y_deg == pytest.approx(-35.0)
    assert two.translate_z_px == pytest.approx(-200.0)
    assert two.scale == pytest.approx(0.85)
    assert two.z_index == 98

    # card_index=3 is even further off-screen but hits the SAME p=0 floor ->
    # identical rotate/translateZ/scale (only z_index differs, since THAT
    # formula is index-based, not saturated): this is the real browser
    # behavior confirmed against tools/carousel_reference/out/cover_flow/
    # trace.json frame 0, where cards 2/3/4 render pixel-identical poses.
    three = cover_flow_transform(0.0, 3, GEO, VIEWPORT_W)
    assert three.rotate_y_deg == pytest.approx(two.rotate_y_deg)
    assert three.translate_z_px == pytest.approx(two.translate_z_px)
    assert three.scale == pytest.approx(two.scale)
    assert three.z_index == 97  # round(100 - 3)


def test_cover_flow_rotation_is_odd_in_signed_distance() -> None:
    # rotate_y(-d) == -rotate_y(d); verified via the +1/-1 pitch pair used above.
    ahead = cover_flow_transform(0.0, 1, GEO, VIEWPORT_W)
    behind = cover_flow_transform(0.0, -1, GEO, VIEWPORT_W)
    assert behind.rotate_y_deg == pytest.approx(-ahead.rotate_y_deg)
    # scale/translate_z/shadow depend only on |m| so they stay equal.
    assert behind.scale == pytest.approx(ahead.scale)
    assert behind.translate_z_px == pytest.approx(ahead.translate_z_px)


# --------------------------------------------------------------------------- #
# cards_stack_transform
# --------------------------------------------------------------------------- #


def test_cards_stack_current_card_sits_at_center() -> None:
    # p=0.5 (card 0 centered at scroll 0) -> peak keyframe: translateX=0, scale=1.
    # ROUND 2: cards.html no longer uses `position: sticky` (see effects.py's
    # module-level comment above STACK_ENTER_TRANSLATE_PX for why), so x is
    # plain flat_left(270) + tx(0) = 270, no pin-floor clamp.
    tf = cards_stack_transform(0.0, 0, GEO, VIEWPORT_W)
    assert tf.x == pytest.approx(270.0)
    assert tf.scale == pytest.approx(1.0)
    assert tf.z_index == 1000
    assert tf.opacity == pytest.approx(1.0)


def test_cards_stack_one_back() -> None:
    # card_index=1, scroll_x=0 -> p=0.13703703703703704 (entering half, f=p/0.5)
    # tx=24*(1-f)=17.422222222222224 ; scale=0.94+0.06*f ;
    # x = flat_left(858) + tx = 875.4222222222222.
    tf = cards_stack_transform(0.0, 1, GEO, VIEWPORT_W)
    assert tf.x == pytest.approx(875.4222222222222)
    assert tf.scale == pytest.approx(0.9564444444444444)
    assert tf.z_index == 346  # same _view_timeline_z_index as cover_flow's one-pitch case
    assert tf.opacity == pytest.approx(1.0)


def test_cards_stack_two_back_saturates_at_floor_scale() -> None:
    # card_index=2, scroll_x=0 -> p=0 (saturated, same `cover`-range floor as
    # cover_flow_transform's two-pitch case) -> f=0 -> tx=24, scale=0.94.
    # x = flat_left(1446) + tx(24) = 1470.
    tf = cards_stack_transform(0.0, 2, GEO, VIEWPORT_W)
    assert tf.x == pytest.approx(1470.0)
    assert tf.scale == pytest.approx(0.94)
    assert tf.opacity == pytest.approx(1.0)


def test_cards_stack_far_entering_card_is_small_but_not_hidden() -> None:
    # card_index=4, scroll_x=0 -> p=0 (same floor as card 2/3 — `p` saturates,
    # it does not keep shrinking/fading past the `cover` range's edge). Unlike
    # the old pitch-distance model, there is no opacity cliff for entering
    # cards: opacity stays 1 all the way to the 0% keyframe; only EXITING
    # cards (p > 0.5) fade, per stack-transform's actual (asymmetric)
    # keyframes — see test_cards_stack_passed_card_exits_left_and_fades.
    tf = cards_stack_transform(0.0, 4, GEO, VIEWPORT_W)
    assert tf.scale == pytest.approx(0.94)
    assert tf.opacity == pytest.approx(1.0)


def test_cards_stack_passed_card_exits_left_and_fades() -> None:
    # scroll_x=294, card_index=0 -> p=view_progress(294,0)=0.6814814814814815
    # (exiting half: f=(p-0.5)/0.5=0.362962962962963) -> tx=-38.4*f=
    # -13.937777777777777 ; scale=1-0.06*f ; opacity=1-0.7*f
    # (STACK_EXIT_OPACITY_MIN=0.3 floor at f=1). flat_left(0,294)=-24, so
    # x = flat_left + tx = -24 + -13.937777777777777 = -37.937777777777775
    # (no sticky pin floor in ROUND 2 — the card is genuinely off-screen left).
    tf = cards_stack_transform(294.0, 0, GEO, VIEWPORT_W)
    assert tf.x == pytest.approx(-37.937777777777775)
    assert tf.opacity == pytest.approx(0.7459259259259259)
    assert tf.scale == pytest.approx(0.9782222222222222)
    assert tf.z_index == 637


# --------------------------------------------------------------------------- #
# flipbook_transform
# --------------------------------------------------------------------------- #


def test_flipbook_centered_page_is_flat_and_on_top() -> None:
    tf = flipbook_transform(0.0, 0, GEO, VIEWPORT_W)
    assert tf.rotate_y_deg == pytest.approx(0.0)
    assert tf.z_index == 1000
    assert tf.translate_z_px == pytest.approx(0.0)


def test_flipbook_entering_page_halfway() -> None:
    # scroll_x=-294, card_index=0 -> p=view_progress(-294,0)=0.31851851851851853
    # -> m=2p-1=-0.36296296296296293
    # rotate_y = FLIPBOOK_ENTER_DEG*m = 35*m = -12.703703703703702 (NOT -17.5:
    # that was the old n=d/pitch model's answer — see flipbook_transform's
    # docstring for why `p`, not pitch-distance, is the correct progress
    # metric, verified to ~2e-4px against the captured browser trace)
    # translate_z = -200*|m| = -72.59259259259258
    # z_index (p<=0.5 branch): round(100 + (1000-100)*(p/0.5)) = round(100 + 900*0.6370...) = 673
    tf = flipbook_transform(-294.0, 0, GEO, VIEWPORT_W)
    assert tf.rotate_y_deg == pytest.approx(-12.703703703703702)
    assert tf.translate_z_px == pytest.approx(-72.59259259259258)
    assert tf.z_index == 673


def test_flipbook_exiting_page_fully_passed() -> None:
    # scroll_x=588, card_index=0 -> p=view_progress(588,0)=0.8629629629629629
    # -> m=2p-1=0.7259259259259259
    # rotate_y = 35*m = 25.407407407407405 (again NOT the old n-clamped-at-1
    # model's +35 — `p` here is comfortably short of the outer p=1 edge)
    # translate_z = -200*|m| = -145.18518518518516
    # z_index (p>0.5 branch): round(1000 + (0-1000)*((p-0.5)/0.5))
    #                        = round(1000-1000*0.7259...) = 274
    tf = flipbook_transform(588.0, 0, GEO, VIEWPORT_W)
    assert tf.rotate_y_deg == pytest.approx(25.407407407407405)
    assert tf.translate_z_px == pytest.approx(-145.18518518518516)
    assert tf.z_index == 274


# --------------------------------------------------------------------------- #
# transform_for dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("effect", "direct_fn"),
    [
        ("scale_sweep", scale_sweep_transform),
        ("cover_flow", cover_flow_transform),
        ("cards_stack", cards_stack_transform),
        ("flipbook", flipbook_transform),
    ],
)
def test_transform_for_dispatches_to_the_matching_function(effect, direct_fn) -> None:
    via_dispatch = transform_for(effect, 123.0, 2, GEO, VIEWPORT_W)
    direct = direct_fn(123.0, 2, GEO, VIEWPORT_W)
    assert via_dispatch == direct


def test_transform_for_all_effects_names_are_covered() -> None:
    for effect in EFFECTS:
        # Should not raise for any name declared in EFFECTS.
        transform_for(effect, 0.0, 0, GEO, VIEWPORT_W)


def test_transform_for_unknown_effect_raises_value_error() -> None:
    with pytest.raises(ValueError, match="carousel"):
        transform_for("not_a_real_effect", 0.0, 0, GEO, VIEWPORT_W)


# --------------------------------------------------------------------------- #
# snap_positions / snap_bounds
# --------------------------------------------------------------------------- #
#
# `snap_positions` no longer reduces to a flat `i * pitch` grid (that was the
# Round 1 bug — see effects.py's module-level docstring for the derivation):
# it evaluates each card's PAINTED pose at scroll_x=0 (mount time) via the
# real effect transform + `renderer.project_card_corners`, since a
# scale/rotation already applied by `animation-timeline: view(inline)` at
# mount shifts the bundle's native `scroll-snap-align: center` position away
# from the flat grid. For `scale_sweep` specifically: card 0 is centered at
# scroll 0 (scale=1, no shift) so its snap position is still exactly 0; card
# 1 is off-center (scale<1, shifted) so its snap position (686.0) is NOT
# `1 * 588 = 588`; card 2's snap position (1311.0) is the exact value this
# whole lane's parity bug traced back to — see spring.py's module docstring.


def test_snap_positions_scale_sweep_is_not_a_flat_grid() -> None:
    # Card 0 is centered (scale=1) at scroll 0 -> unshifted, snap stays 0.
    # Cards 1-3 are off-center and scaled down -> each snap position is
    # shifted FROM the flat grid by that card's own scale-driven offset.
    # Card 3 (index 3 of 4, i.e. the LAST card in a 4-card layout) is clamped
    # to snap_bounds(4, ...)'s upper bound (1764.0) -- its raw (unclamped)
    # scale-shifted position would exceed the scroller's flat scrollable
    # range, same as the bundle's own `ne()` clamp (see spring.release's
    # `bounds` param).
    positions = snap_positions("scale_sweep", 4, GEO, VIEWPORT_W)
    assert positions[0] == pytest.approx(0.0)
    assert positions == pytest.approx([0.0, 686.0, 1311.0, 1764.0])
    assert positions[-1] == pytest.approx(snap_bounds(4, GEO, VIEWPORT_W)[1])


def test_snap_positions_empty_for_zero_cards() -> None:
    assert snap_positions("scale_sweep", 0, GEO, VIEWPORT_W) == []


def test_snap_bounds_is_flat_scrollable_range() -> None:
    # Layout scrollWidth is unaffected by any effect's transform (only the
    # PAINTED position is), so snap_bounds has no `effect` parameter and is
    # identical across all four effects for the same card count/geometry.
    # flat_content_w = 2*center_left(270) + 4*540 + 3*48 = 540+2160+144 = 2844
    # bound_max = 2844 - viewport_w(1080) = 1764
    assert snap_bounds(4, GEO, VIEWPORT_W) == (0.0, 1764.0)
    assert snap_bounds(0, GEO, VIEWPORT_W) == (0.0, 0.0)
