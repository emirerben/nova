/**
 * Port of test_effects_transforms.py's hand-computed literals — same
 * comments, same expected values, TS side of the parity contract.
 *
 * Layout constants mirrored from the browser reference pages:
 *     viewport_w = 1080, card_w = 540, card_h = 720, gap = 48
 *     pitch = card_w + gap = 588
 *     center_left = (viewport_w - card_w) / 2 = 270
 *     default_y = (1920 - card_h) / 2 = 600
 */

import {
  EFFECTS,
  cardsStackTransform,
  coverFlowTransform,
  flipbookTransform,
  scaleSweepTransform,
  snapBounds,
  snapPositions,
  transformFor,
  viewProgress,
} from "../effects";
import { projectCardCorners } from "../project-corners";
import type { CardGeometry } from "../types";

const VIEWPORT_W = 1080.0;
const GEO: CardGeometry = { cardW: 540, cardH: 720, gap: 48, cornerRadius: 24 };
const CENTER_LEFT = (VIEWPORT_W - GEO.cardW) / 2; // 270
const DEFAULT_Y = (1920 - GEO.cardH) / 2; // 600

// --------------------------------------------------------------------- //
// view_progress
// --------------------------------------------------------------------- //

describe("viewProgress", () => {
  it("card 0 at scroll 0 is exactly half", () => {
    // left(0) = center_left = 270 (card 0 is centered at scroll_x=0)
    // p = (1080 - 270) / (1080 + 540) = 810 / 1620 = 0.5 exactly
    const p = viewProgress(0.0, 0, GEO, VIEWPORT_W);
    expect(p).toBeCloseTo(0.5, 9);
  });

  it("card 1 at scroll 0", () => {
    // left(1) = center_left + 1*pitch = 270 + 588 = 858
    // p = (1080 - 858) / 1620 = 222 / 1620 = 0.137037037...
    const p = viewProgress(0.0, 1, GEO, VIEWPORT_W);
    expect(p).toBeCloseTo(222 / 1620, 9);
    expect(p).toBeCloseTo(0.13703703703703704, 9);
  });

  it("clamps to unit range", () => {
    // Way off to the right: left(0) = 270 - 5000 = -4730, huge raw p,
    // clamps to 1.
    expect(viewProgress(5000.0, 0, GEO, VIEWPORT_W)).toBeCloseTo(1.0, 9);
    // Way off to the left (large negative scroll pushes the card far
    // right): left(0) = 270 - (-5000) = 5270, raw p very negative, clamps
    // to 0.
    expect(viewProgress(-5000.0, 0, GEO, VIEWPORT_W)).toBeCloseTo(0.0, 9);
  });
});

// --------------------------------------------------------------------- //
// scale_sweep_transform
// --------------------------------------------------------------------- //

describe("scaleSweepTransform", () => {
  it("centered card is full scale and opaque", () => {
    // p=0.5 -> t = 1 - |2*0.5 - 1| = 1 - 0 = 1
    // scale = 0.5 + 0.5*1 = 1.0 ; opacity = 0.35 + 0.65*1 = 1.0
    const tf = scaleSweepTransform(0.0, 0, GEO, VIEWPORT_W);
    expect(tf.scale).toBeCloseTo(1.0, 9);
    expect(tf.opacity).toBeCloseTo(1.0, 9);
    expect(tf.shadowAlpha).toBeCloseTo(0.25, 9);
    expect(tf.rotateYDeg).toBe(0.0);
    expect(tf.zIndex).toBe(0);
    expect(tf.x).toBeCloseTo(CENTER_LEFT, 9);
    expect(tf.y).toBeCloseTo(DEFAULT_Y, 9);
  });

  it("one pitch away", () => {
    // card_index=1, scroll_x=0 -> d = +588 (one pitch from center)
    // t = 1 - |2p - 1| = 1 - 2*588/1620 = 1 - 0.7259259... = 0.2740740...
    // scale = 0.5 + 0.5*t ; opacity = 0.35 + 0.65*t
    const tf = scaleSweepTransform(0.0, 1, GEO, VIEWPORT_W);
    const t = 1 - (2 * 588) / 1620;
    expect(t).toBeCloseTo(0.27407407407407414, 9);
    expect(tf.scale).toBeCloseTo(0.5 + 0.5 * t, 9);
    expect(tf.opacity).toBeCloseTo(0.35 + 0.65 * t, 9);
  });

  it("fully off screen hits the floor", () => {
    // Pushed far enough that p clamps to 1 -> t = 1 - |2*1 - 1| = 0
    // scale floors at SCALE_SWEEP_MIN=0.5, opacity floors at
    // OPACITY_MIN=0.35
    const tf = scaleSweepTransform(5000.0, 0, GEO, VIEWPORT_W);
    expect(tf.scale).toBeCloseTo(0.5, 9);
    expect(tf.opacity).toBeCloseTo(0.35, 9);
    expect(tf.shadowAlpha).toBeCloseTo(0.0, 9);
  });

  it("symmetric about center", () => {
    // card_index=1 at scroll 0 -> d=+588 ; card_index=-1 at scroll 0 -> d=-588
    // t only depends on |d|, so scale/opacity must match.
    const ahead = scaleSweepTransform(0.0, 1, GEO, VIEWPORT_W);
    const behind = scaleSweepTransform(0.0, -1, GEO, VIEWPORT_W);
    expect(ahead.scale).toBeCloseTo(behind.scale, 9);
    expect(ahead.opacity).toBeCloseTo(behind.opacity, 9);
  });
});

// --------------------------------------------------------------------- //
// cover_flow_transform
// --------------------------------------------------------------------- //

describe("coverFlowTransform", () => {
  it("centered card is flat and full scale", () => {
    // p=viewProgress(0,0)=0.5 exactly (card 0 centered at scroll 0) ->
    // m=2p-1=0. z_index=_view_timeline_z_index(0.5,0): p==0.5 is the
    // keyframe PEAK -> 1000
    const tf = coverFlowTransform(0.0, 0, GEO, VIEWPORT_W);
    expect(tf.rotateYDeg).toBeCloseTo(0.0, 9);
    expect(tf.scale).toBeCloseTo(1.0, 9);
    expect(tf.zIndex).toBe(1000);
    expect(tf.translateZPx).toBeCloseTo(0.0, 9);
    expect(tf.opacity).toBeCloseTo(1.0, 9);
    expect(tf.shadowAlpha).toBeCloseTo(0.0, 9);
  });

  it("one pitch away", () => {
    // card_index=1, scroll_x=0 -> p=222/1620=0.13703703703703704 ->
    // m=2p-1=-0.7259259...
    // rotate=35*m=-25.407407407407405 ; tz=-200*|m|=-145.18518518518516
    // scale=1-0.15*|m|=0.8911111111111111
    // z_index (p<=0.5 branch): round(99 + 901*0.27407407...) = 346
    const tf = coverFlowTransform(0.0, 1, GEO, VIEWPORT_W);
    expect(tf.rotateYDeg).toBeCloseTo(-25.407407407407405, 9);
    expect(tf.translateZPx).toBeCloseTo(-145.18518518518516, 9);
    expect(tf.scale).toBeCloseTo(0.8911111111111111, 9);
    expect(tf.zIndex).toBe(346);
  });

  it("two pitches away saturates the same as further", () => {
    // card_index=2, scroll_x=0 -> left(2)=1446 > viewport_w(1080), raw
    // fraction negative -> clamped to p=0. m=-1 (the 0% keyframe exactly):
    // rotate=-35, tz=-200, scale=0.85. z_index at p=0: round(100-2)=98.
    const two = coverFlowTransform(0.0, 2, GEO, VIEWPORT_W);
    expect(two.rotateYDeg).toBeCloseTo(-35.0, 9);
    expect(two.translateZPx).toBeCloseTo(-200.0, 9);
    expect(two.scale).toBeCloseTo(0.85, 9);
    expect(two.zIndex).toBe(98);

    // card_index=3 is even further off-screen but hits the SAME p=0 floor
    // -> identical rotate/translateZ/scale (only z_index differs).
    const three = coverFlowTransform(0.0, 3, GEO, VIEWPORT_W);
    expect(three.rotateYDeg).toBeCloseTo(two.rotateYDeg, 9);
    expect(three.translateZPx).toBeCloseTo(two.translateZPx, 9);
    expect(three.scale).toBeCloseTo(two.scale, 9);
    expect(three.zIndex).toBe(97); // round(100 - 3)
  });

  it("rotation is odd in signed distance", () => {
    // rotate_y(-d) == -rotate_y(d)
    const ahead = coverFlowTransform(0.0, 1, GEO, VIEWPORT_W);
    const behind = coverFlowTransform(0.0, -1, GEO, VIEWPORT_W);
    expect(behind.rotateYDeg).toBeCloseTo(-ahead.rotateYDeg, 9);
    expect(behind.scale).toBeCloseTo(ahead.scale, 9);
    expect(behind.translateZPx).toBeCloseTo(ahead.translateZPx, 9);
  });
});

// --------------------------------------------------------------------- //
// cards_stack_transform
// --------------------------------------------------------------------- //

describe("cardsStackTransform", () => {
  it("current card sits at center", () => {
    // p=0.5 (card 0 centered at scroll 0) -> peak keyframe: translateX=0,
    // scale=1. ROUND 2: no sticky pin floor, so x is plain flat_left(270)
    // + tx(0) = 270.
    const tf = cardsStackTransform(0.0, 0, GEO, VIEWPORT_W);
    expect(tf.x).toBeCloseTo(270.0, 9);
    expect(tf.scale).toBeCloseTo(1.0, 9);
    expect(tf.zIndex).toBe(1000);
    expect(tf.opacity).toBeCloseTo(1.0, 9);
  });

  it("one back", () => {
    // card_index=1, scroll_x=0 -> p=0.13703703703703704 (entering half,
    // f=p/0.5). tx=24*(1-f)=17.422222222222224 ; scale=0.94+0.06*f ;
    // x = flat_left(858) + tx = 875.4222222222222.
    const tf = cardsStackTransform(0.0, 1, GEO, VIEWPORT_W);
    expect(tf.x).toBeCloseTo(875.4222222222222, 9);
    expect(tf.scale).toBeCloseTo(0.9564444444444444, 9);
    expect(tf.zIndex).toBe(346); // same _view_timeline_z_index as cover_flow's one-pitch case
    expect(tf.opacity).toBeCloseTo(1.0, 9);
  });

  it("two back saturates at floor scale", () => {
    // card_index=2, scroll_x=0 -> p=0 (saturated) -> f=0 -> tx=24,
    // scale=0.94. x = flat_left(1446) + tx(24) = 1470.
    const tf = cardsStackTransform(0.0, 2, GEO, VIEWPORT_W);
    expect(tf.x).toBeCloseTo(1470.0, 9);
    expect(tf.scale).toBeCloseTo(0.94, 9);
    expect(tf.opacity).toBeCloseTo(1.0, 9);
  });

  it("far entering card is small but not hidden", () => {
    // card_index=4, scroll_x=0 -> p=0 (same floor as card 2/3). No
    // opacity cliff for entering cards: opacity stays 1.
    const tf = cardsStackTransform(0.0, 4, GEO, VIEWPORT_W);
    expect(tf.scale).toBeCloseTo(0.94, 9);
    expect(tf.opacity).toBeCloseTo(1.0, 9);
  });

  it("passed card exits left and fades", () => {
    // scroll_x=294, card_index=0 -> p=0.6814814814814815 (exiting half:
    // f=(p-0.5)/0.5=0.362962962962963) -> tx=-38.4*f=-13.937777777777777 ;
    // scale=1-0.06*f ; opacity=1-0.7*f. flat_left(0,294)=-24, so
    // x = flat_left + tx = -37.937777777777775 (no sticky pin floor).
    const tf = cardsStackTransform(294.0, 0, GEO, VIEWPORT_W);
    expect(tf.x).toBeCloseTo(-37.937777777777775, 9);
    expect(tf.opacity).toBeCloseTo(0.7459259259259259, 9);
    expect(tf.scale).toBeCloseTo(0.9782222222222222, 9);
    expect(tf.zIndex).toBe(637);
  });
});

// --------------------------------------------------------------------- //
// flipbook_transform
// --------------------------------------------------------------------- //

describe("flipbookTransform", () => {
  it("centered page is flat and on top", () => {
    const tf = flipbookTransform(0.0, 0, GEO, VIEWPORT_W);
    expect(tf.rotateYDeg).toBeCloseTo(0.0, 9);
    expect(tf.zIndex).toBe(1000);
    expect(tf.translateZPx).toBeCloseTo(0.0, 9);
  });

  it("entering page halfway", () => {
    // scroll_x=-294, card_index=0 -> p=0.31851851851851853 ->
    // m=2p-1=-0.36296296296296293
    // rotate_y = 35*m = -12.703703703703702 (NOT -17.5: that was the old
    // pitch-distance model's answer)
    // translate_z = -200*|m| = -72.59259259259258
    // z_index (p<=0.5 branch): round(100 + 900*0.6370...) = 673
    const tf = flipbookTransform(-294.0, 0, GEO, VIEWPORT_W);
    expect(tf.rotateYDeg).toBeCloseTo(-12.703703703703702, 9);
    expect(tf.translateZPx).toBeCloseTo(-72.59259259259258, 9);
    expect(tf.zIndex).toBe(673);
  });

  it("exiting page fully passed", () => {
    // scroll_x=588, card_index=0 -> p=0.8629629629629629 ->
    // m=2p-1=0.7259259259259259
    // rotate_y = 35*m = 25.407407407407405
    // translate_z = -200*|m| = -145.18518518518516
    // z_index (p>0.5 branch): round(1000-1000*0.7259...) = 274
    const tf = flipbookTransform(588.0, 0, GEO, VIEWPORT_W);
    expect(tf.rotateYDeg).toBeCloseTo(25.407407407407405, 9);
    expect(tf.translateZPx).toBeCloseTo(-145.18518518518516, 9);
    expect(tf.zIndex).toBe(274);
  });
});

// --------------------------------------------------------------------- //
// transform_for dispatch
// --------------------------------------------------------------------- //

describe("transformFor dispatch", () => {
  it.each([
    ["scale_sweep", scaleSweepTransform],
    ["cover_flow", coverFlowTransform],
    ["cards_stack", cardsStackTransform],
    ["flipbook", flipbookTransform],
  ] as const)("dispatches to the matching function: %s", (effect, directFn) => {
    const viaDispatch = transformFor(effect, 123.0, 2, GEO, VIEWPORT_W);
    const direct = directFn(123.0, 2, GEO, VIEWPORT_W);
    expect(viaDispatch).toEqual(direct);
  });

  it("all effect names are covered", () => {
    for (const effect of EFFECTS) {
      expect(() => transformFor(effect, 0.0, 0, GEO, VIEWPORT_W)).not.toThrow();
    }
  });

  it("unknown effect raises", () => {
    // @ts-expect-error — intentionally passing an invalid effect name.
    expect(() => transformFor("not_a_real_effect", 0.0, 0, GEO, VIEWPORT_W)).toThrow(/carousel/);
  });
});

// --------------------------------------------------------------------- //
// snap_positions / snap_bounds
// --------------------------------------------------------------------- //
//
// snapPositions no longer reduces to a flat `i * pitch` grid (that was the
// Round 1 bug): it evaluates each card's PAINTED pose at scroll_x=0 (mount
// time) via the real effect transform + projectCardCorners, since a
// scale/rotation already applied by `animation-timeline: view(inline)` at
// mount shifts the bundle's native `scroll-snap-align: center` position
// away from the flat grid.

describe("snapPositions / snapBounds", () => {
  it("scale_sweep is not a flat grid", () => {
    // Card 0 is centered (scale=1) at scroll 0 -> unshifted, snap stays 0.
    // Cards 1-3 are off-center and scaled down -> each snap position is
    // shifted FROM the flat grid by that card's own scale-driven offset.
    // Card 3 is clamped to snap_bounds(4, ...)'s upper bound (1764.0).
    const positions = snapPositions("scale_sweep", 4, GEO, VIEWPORT_W);
    expect(positions[0]).toBeCloseTo(0.0, 6);
    expect(positions[1]).toBeCloseTo(686.0, 6);
    expect(positions[2]).toBeCloseTo(1311.0, 6);
    expect(positions[3]).toBeCloseTo(1764.0, 6);
    expect(positions[positions.length - 1]).toBeCloseTo(snapBounds(4, GEO, VIEWPORT_W)[1], 9);
  });

  it("is empty for zero cards", () => {
    expect(snapPositions("scale_sweep", 0, GEO, VIEWPORT_W)).toEqual([]);
  });

  it("the scale_sweep 1311 resting case matches projectCardCorners' AABB directly", () => {
    // Landmark from effects.py's module docstring: card index 2 sits at
    // flat/unscaled center 1176, but at scroll_x=0 (mount time) it's
    // painted at scale=0.5 (view-progress t=0, off in the wings), so its
    // PAINTED left edge is shifted +135px right of its flat left edge
    // (card_w * (1 - 0.5) / 2 = 135) — 1176 + 135 = 1311.
    const t = transformFor("scale_sweep", 0.0, 2, GEO, VIEWPORT_W);
    expect(t.scale).toBeCloseTo(0.5, 9);
    const corners = projectCardCorners(t, GEO);
    const visualLeft = Math.min(...corners.map(([x]) => x));
    const snapX = visualLeft + GEO.cardW / 2.0 - VIEWPORT_W / 2.0;
    expect(snapX).toBeCloseTo(1311.0, 6);
  });

  it("snap_bounds is the flat scrollable range, effect-independent", () => {
    // flat_content_w = 2*center_left(270) + 4*540 + 3*48 = 2844
    // bound_max = 2844 - viewport_w(1080) = 1764
    expect(snapBounds(4, GEO, VIEWPORT_W)).toEqual([0.0, 1764.0]);
    expect(snapBounds(0, GEO, VIEWPORT_W)).toEqual([0.0, 0.0]);
  });
});
