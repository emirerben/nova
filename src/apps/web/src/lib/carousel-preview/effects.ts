/**
 * Card-transform shapes for the four Blossom-carousel visual effects. Port
 * of `app/pipeline/carousel/effects.py`.
 *
 * Each `*Transform` function maps a scroll position + card index to the
 * card's on-canvas pose for one effect style. `transformFor` dispatches by
 * name.
 *
 * Pure math only. All arithmetic here mirrors the CSS that the browser
 * reference pages use (`animation-timeline: view(inline)` scroll
 * scrubbing), so the constants below are meant to visually match those
 * pages, not to be derived from first principles.
 *
 * ## Coordinate model
 *
 * - The canvas viewport is `viewportW` px wide (1080 in production) and
 *   VIEWPORT_H px tall (1920, hardcoded). Cards are laid out in a single
 *   horizontal row with constant pitch `cardW + gap`, centered under a
 *   leading/trailing pad of `(viewportW - cardW) / 2` so that card 0 is
 *   centered at `scrollX = 0`.
 * - `CardTransform.x` is the on-screen LEFT edge of the card, BEFORE
 *   scaling. The renderer scales (and rotates) the card about its own
 *   center, not `x` — so `x` here is always the pre-scale geometric left
 *   edge, even when `scale != 1`.
 * - `CardTransform.y` follows the same pre-scale convention and, unless an
 *   effect says otherwise, is fixed so the card's top is vertically
 *   centered in the canvas: `y = (VIEWPORT_H - cardH) / 2`.
 *
 * ## View-progress model
 *
 * CSS `animation-timeline: view(inline)` progress for card `i` is:
 *
 *     p = (viewportW - left(i)) / (viewportW + cardW)
 *
 * `p == 0` when the card's left edge sits at the right viewport edge (the
 * card is just entering from the right); `p == 1` when the card's right
 * edge has crossed the left viewport edge (the card has fully exited to
 * the left); `p == 0.5` when the card is perfectly centered. Clamped to
 * [0, 1].
 */

import { pythonRound } from "./spring";
import type { CardGeometry, CardTransform, EffectName } from "./types";
import { EFFECTS, createCardTransform } from "./types";
import { projectCardCorners } from "./project-corners";

// Re-exported so callers can `import { EFFECTS } from "./effects"` (the
// Python module that owns this constant, effects.py) without also needing
// to know it's canonically defined in types.ts on the TS side (avoiding a
// types.ts <-> effects.ts import cycle, since effects.ts's CardTransform/
// CardGeometry also live in types.ts). Safe to re-export via `export *` in
// index.ts alongside types.ts's own `export * from "./types"`: both star
// exports resolve to the SAME originating binding, which ES modules treat
// as non-ambiguous.
export { EFFECTS };

// Canvas height. Duplicated from project-corners.ts's CANVAS_H (not
// imported) for the same reason the Python source duplicates it: mirrors
// app/pipeline/carousel/effects.py:VIEWPORT_H — keep in sync (golden trace
// test pins this).
export const VIEWPORT_H = 1920.0;

// --- scale_sweep (homepage "blossom" effect) --------------------------
// Symmetric triangle keyframe on view progress, linear timing: cards
// scale/fade up as they approach center and back down as they leave,
// peaking at p=0.5.
// Mirrors app/pipeline/carousel/effects.py:SCALE_SWEEP_MIN.
export const SCALE_SWEEP_MIN = 0.5;
// Mirrors app/pipeline/carousel/effects.py:SCALE_SWEEP_OPACITY_MIN.
export const SCALE_SWEEP_OPACITY_MIN = 0.35;
// Mirrors app/pipeline/carousel/effects.py:SCALE_SWEEP_SHADOW_MAX.
export const SCALE_SWEEP_SHADOW_MAX = 0.25;

// --- cover_flow ---------------------------------------------------------
// Classic Cover Flow: side cards rotate toward the viewer around a
// vertical axis and recede in Z: rotateY(...) translateZ(...) scale(...).
// Driven by view-timeline progress `p` — NOT a pitch-normalized distance,
// so there is no separate "range" constant; `p`'s own [0, 1] clamp (in
// viewProgress) is the saturation point.
// Mirrors app/pipeline/carousel/effects.py:COVER_FLOW_MAX_DEG.
export const COVER_FLOW_MAX_DEG = 35.0;
// Mirrors app/pipeline/carousel/effects.py:COVER_FLOW_DEPTH_PX.
export const COVER_FLOW_DEPTH_PX = 200.0;
// Mirrors app/pipeline/carousel/effects.py:COVER_FLOW_SCALE_FALLOFF.
export const COVER_FLOW_SCALE_FALLOFF = 0.15;
// Mirrors app/pipeline/carousel/effects.py:COVER_FLOW_SHADOW_MAX.
export const COVER_FLOW_SHADOW_MAX = 0.35;

// --- cards_stack (smart-stack) ------------------------------------------
// `cards.html`'s actual `@keyframes stack-transform` (view-timeline
// progress `p`, 3 stops): 0%{translateX(24px) scale(0.94); opacity:1}
// 50%{translateX(0) scale(1); opacity:1} 100%{translateX(-38.4px)
// scale(0.94); opacity:0.3} — asymmetric (entering keeps full opacity,
// exiting fades), unlike scale_sweep's symmetric triangle.
// Mirrors app/pipeline/carousel/effects.py:STACK_ENTER_TRANSLATE_PX.
export const STACK_ENTER_TRANSLATE_PX = 24.0;
// Mirrors app/pipeline/carousel/effects.py:STACK_EXIT_TRANSLATE_PX.
export const STACK_EXIT_TRANSLATE_PX = -38.4;
// Mirrors app/pipeline/carousel/effects.py:STACK_SCALE_MIN.
export const STACK_SCALE_MIN = 0.94;
// Mirrors app/pipeline/carousel/effects.py:STACK_EXIT_OPACITY_MIN.
export const STACK_EXIT_OPACITY_MIN = 0.3;

// ROUND 2 (see effects.py's module docstring for the full Round 1 -> Round
// 2 writeup): `cards.html`'s `.card` no longer sets `position: sticky` (or
// `left`/`right`) at all — it's structurally identical to `flipbook.html`
// (plain `position: relative`), just with `stack-transform`'s asymmetric
// keyframes instead of flipbook's symmetric ones. No position floor/clamp
// constant is needed as a result.

// --- flipbook (spine-pivot page turn) ------------------------------------
// Pages pivot at the viewport center-line (the spine): an entering page
// (ahead of center) and an exiting page (behind center) rotate/recede
// symmetrically, via `m = 2*viewProgress - 1` — its z-index peak (1000) is
// viewTimelineZIndex's hardcoded midpoint, shared with coverFlowTransform,
// so there's no separate FLIPBOOK_Z_BASE constant.
// Mirrors app/pipeline/carousel/effects.py:FLIPBOOK_ENTER_DEG.
export const FLIPBOOK_ENTER_DEG = 35.0;
// Mirrors app/pipeline/carousel/effects.py:FLIPBOOK_DEPTH_PX.
export const FLIPBOOK_DEPTH_PX = 200.0;
// Mirrors app/pipeline/carousel/effects.py:FLIPBOOK_SHADOW_MAX.
export const FLIPBOOK_SHADOW_MAX = 0.3;

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value));
}

function pitch(geo: CardGeometry): number {
  return geo.cardW + geo.gap;
}

/**
 * Left edge of card 0's static (unscrolled) content position — also the
 * left edge any centered card sits at once scrolled into view.
 */
function centerLeft(geo: CardGeometry, viewportW: number): number {
  return (viewportW - geo.cardW) / 2;
}

/** On-screen left edge of card `cardIndex` at `scrollX`: L(i) - scrollX. */
function cardLeft(scrollX: number, cardIndex: number, geo: CardGeometry, viewportW: number): number {
  return centerLeft(geo, viewportW) + cardIndex * pitch(geo) - scrollX;
}

function defaultY(geo: CardGeometry): number {
  return (VIEWPORT_H - geo.cardH) / 2;
}

/**
 * CSS `animation-timeline: view(inline)` progress for this card, clamped to
 * [0, 1]. See the module docstring for the p=0/0.5/1 landmarks. Mirrors
 * app/pipeline/carousel/effects.py:view_progress.
 */
export function viewProgress(
  scrollX: number,
  cardIndex: number,
  geo: CardGeometry,
  viewportW: number,
): number {
  const left = cardLeft(scrollX, cardIndex, geo, viewportW);
  const p = (viewportW - left) / (viewportW + geo.cardW);
  return clamp(p, 0.0, 1.0);
}

/**
 * z-index for a `*-z` keyframe of the shape `cover-flow.html`/
 * `flipbook.html` both use:
 *     0%   { z-index: calc(100 - sibling-index()); }
 *     50%  { z-index: 1000; }
 *     100% { z-index: sibling-index(); }
 * i.e. linearly interpolated (in `p`) between two DOM-index-based
 * constants at the ends and a fixed peak at the center — NOT a
 * distance-based value, unlike this module's rotate/translateZ/scale
 * formulas. `sibling-index()` is 1-based per the CSS spec; this repo's
 * cards are referenced by a 0-based `cardIndex` throughout, so `cardIndex`
 * is used as-is here rather than `cardIndex + 1` — the exact base only
 * shifts every card's z-index by a constant 1, which never changes paint
 * ORDER. Mirrors app/pipeline/carousel/effects.py:_view_timeline_z_index.
 */
function viewTimelineZIndex(p: number, cardIndex: number): number {
  if (p <= 0.5) {
    return pythonRound(100 - cardIndex + (1000 - (100 - cardIndex)) * (p / 0.5));
  }
  return pythonRound(1000 + (cardIndex - 1000) * ((p - 0.5) / 0.5));
}

export interface TransformOptions {
  readonly positionScrollX?: number;
}

/**
 * Symmetric triangle keyframe on view progress: `t = 1 - |2p - 1|` is 0 at
 * the edges of the scroll-timeline and 1 exactly centered; scale and
 * opacity both ramp linearly with `t`.
 *
 * `scrollX` drives the view-timeline PROGRESS (p/t and everything derived
 * from it — scale, opacity, shadow); `positionScrollX` (defaults to
 * `scrollX`) drives the card's LAYOUT position (`x`). These are genuinely
 * different scroll values one frame apart when driven by a lagged virtual
 * scroll (a captured browser frame's `scrollLeft`, hence layout position,
 * is NOT lagged, but its `animation-timeline: view(inline)`-driven
 * `transform` IS, by one frame). Mirrors
 * app/pipeline/carousel/effects.py:scale_sweep_transform.
 */
export function scaleSweepTransform(
  scrollX: number,
  cardIndex: number,
  geo: CardGeometry,
  viewportW: number,
  options: TransformOptions = {},
): CardTransform {
  const positionScrollX = options.positionScrollX ?? scrollX;
  const p = viewProgress(scrollX, cardIndex, geo, viewportW);
  const t = 1 - Math.abs(2 * p - 1);
  const scale = SCALE_SWEEP_MIN + (1 - SCALE_SWEEP_MIN) * t;
  const opacity = SCALE_SWEEP_OPACITY_MIN + (1 - SCALE_SWEEP_OPACITY_MIN) * t;
  return createCardTransform({
    x: cardLeft(positionScrollX, cardIndex, geo, viewportW),
    y: defaultY(geo),
    scale,
    opacity,
    rotateYDeg: 0.0,
    translateZPx: 0.0,
    zIndex: 0,
    shadowAlpha: SCALE_SWEEP_SHADOW_MAX * t,
  });
}

/**
 * Side cards rotate toward the center (rotateY(+-35deg)), recede in Z
 * (translateZ(-200px)), and shrink slightly, all driven by the card's
 * view-timeline progress `p` — NOT the pitch-normalized distance `d /
 * pitch`, since the two are close near the center but diverge increasingly
 * approaching (and beyond) the `cover` range's edges, because `p`
 * SATURATES at 0/1 (viewProgress clamps it) while `d / pitch` keeps
 * growing linearly forever.
 *
 * `m = 2p - 1` recasts `p`'s [0, 1] range as [-1, 1] centered on 0. Mirrors
 * app/pipeline/carousel/effects.py:cover_flow_transform.
 */
export function coverFlowTransform(
  scrollX: number,
  cardIndex: number,
  geo: CardGeometry,
  viewportW: number,
  options: TransformOptions = {},
): CardTransform {
  const positionScrollX = options.positionScrollX ?? scrollX;
  const p = viewProgress(scrollX, cardIndex, geo, viewportW);
  const m = 2 * p - 1;
  const mAbs = Math.abs(m);
  const rotateYDeg = COVER_FLOW_MAX_DEG * m;
  const translateZPx = -COVER_FLOW_DEPTH_PX * mAbs;
  const scale = 1 - COVER_FLOW_SCALE_FALLOFF * mAbs;
  return createCardTransform({
    x: cardLeft(positionScrollX, cardIndex, geo, viewportW),
    y: defaultY(geo),
    scale,
    opacity: 1.0,
    rotateYDeg,
    translateZPx,
    zIndex: viewTimelineZIndex(p, cardIndex),
    shadowAlpha: COVER_FLOW_SHADOW_MAX * mAbs,
  });
}

/**
 * Cards animate through `stack-transform`'s 3-stop, view-timeline-progress
 * (`p`) keyframes — see the STACK_* constants' comments for the exact
 * values — same triangle-on-`p` shape as scaleSweepTransform /
 * coverFlowTransform / flipbookTransform, just asymmetric (translateX/
 * scale/opacity all differ between the entering [0, 0.5] and exiting [0.5,
 * 1] halves).
 *
 * ROUND 2: `x` is plain flat-layout-plus-`translateX`, same pattern as
 * every other effect here — no floor/clamp constant. `CardTransform.x` is
 * documented as the PRE-SCALE left edge (the renderer scales about the
 * card's own center using `x` as input), and CSS `transform: translateX(tx)
 * scale(s)` applied to an element scales it about its own center FIRST,
 * then shifts by `tx` — i.e. the renderer's own post-scale-left
 * computation and this function's `tx` shift compose by plain addition on
 * `x` itself, so `x = flatLeft + tx` directly. Mirrors
 * app/pipeline/carousel/effects.py:cards_stack_transform.
 */
export function cardsStackTransform(
  scrollX: number,
  cardIndex: number,
  geo: CardGeometry,
  viewportW: number,
  options: TransformOptions = {},
): CardTransform {
  const positionScrollX = options.positionScrollX ?? scrollX;
  const p = viewProgress(scrollX, cardIndex, geo, viewportW);

  let tx: number;
  let scale: number;
  let opacity: number;
  if (p <= 0.5) {
    const f = p / 0.5;
    tx = STACK_ENTER_TRANSLATE_PX * (1 - f);
    scale = STACK_SCALE_MIN + (1 - STACK_SCALE_MIN) * f;
    opacity = 1.0;
  } else {
    const f = (p - 0.5) / 0.5;
    tx = STACK_EXIT_TRANSLATE_PX * f;
    scale = 1 - (1 - STACK_SCALE_MIN) * f;
    opacity = 1 - (1 - STACK_EXIT_OPACITY_MIN) * f;
  }

  const flatLeft = cardLeft(positionScrollX, cardIndex, geo, viewportW);
  const x = flatLeft + tx;

  return createCardTransform({
    x,
    y: defaultY(geo),
    scale,
    opacity,
    rotateYDeg: 0.0,
    translateZPx: 0.0,
    zIndex: viewTimelineZIndex(p, cardIndex),
    shadowAlpha: 0.0,
  });
}

/**
 * Spine-pivot page turn, driven by view-timeline progress `p` (see
 * coverFlowTransform's docstring for why `p` and not a pitch-normalized
 * distance — the same refit applies here): `m = 2p - 1` is `-1` at p=0
 * (entering keyframe: translateZ(-200px) rotateY(-35deg)), `0` at p=0.5
 * (`none` — flat, no transform), `+1` at p=1 (exiting keyframe:
 * translateZ(-200px) rotateY(35deg)); `|m|` alone drives depth (odd in `m`,
 * even effect on translateZ, matching flip-transform's symmetric
 * keyframes).
 *
 * `flipbook.html`'s keyframe CSS lists `translateZ(...) rotateY(...)`, the
 * opposite composition order from cover_flow's `rotateY(...)
 * translateZ(...) scale(...)` — see CardTransform.rotateBeforeTranslate.
 * Mirrors app/pipeline/carousel/effects.py:flipbook_transform.
 */
export function flipbookTransform(
  scrollX: number,
  cardIndex: number,
  geo: CardGeometry,
  viewportW: number,
  options: TransformOptions = {},
): CardTransform {
  const positionScrollX = options.positionScrollX ?? scrollX;
  const p = viewProgress(scrollX, cardIndex, geo, viewportW);
  const m = 2 * p - 1;
  const mAbs = Math.abs(m);
  return createCardTransform({
    x: cardLeft(positionScrollX, cardIndex, geo, viewportW),
    y: defaultY(geo),
    scale: 1.0,
    opacity: 1.0,
    rotateYDeg: FLIPBOOK_ENTER_DEG * m,
    translateZPx: -FLIPBOOK_DEPTH_PX * mAbs,
    zIndex: viewTimelineZIndex(p, cardIndex),
    shadowAlpha: FLIPBOOK_SHADOW_MAX * mAbs,
    // flipbook.html's keyframes are `translateZ(...) rotateY(...)` — the
    // opposite composition order from cover_flow's `rotateY(...)
    // translateZ(...) scale(...)`. See CardTransform.rotateBeforeTranslate.
    rotateBeforeTranslate: true,
  });
}

const TRANSFORM_BY_EFFECT: Record<
  EffectName,
  (
    scrollX: number,
    cardIndex: number,
    geo: CardGeometry,
    viewportW: number,
    options?: TransformOptions,
  ) => CardTransform
> = {
  scale_sweep: scaleSweepTransform,
  cover_flow: coverFlowTransform,
  cards_stack: cardsStackTransform,
  flipbook: flipbookTransform,
};

/** Mirrors app/pipeline/carousel/effects.py:transform_for. */
export function transformFor(
  effect: EffectName,
  scrollX: number,
  cardIndex: number,
  geo: CardGeometry,
  viewportW: number,
  options: TransformOptions = {},
): CardTransform {
  const fn = TRANSFORM_BY_EFFECT[effect];
  if (!fn) {
    throw new Error(`Unknown carousel effect ${JSON.stringify(effect)}; expected one of ${EFFECTS}`);
  }
  return fn(scrollX, cardIndex, geo, viewportW, options);
}

/**
 * Scroll positions that center each card in turn, replicating the vendored
 * bundle's actual `scroll-snap-align: center` math rather than a naive flat
 * `i * pitch` grid.
 *
 * The bundle computes each card's snap position from
 * `card.getBoundingClientRect()` — WHATEVER pose is currently painted,
 * including any `animation-timeline: view(inline)`-driven scale/rotation
 * the effect has already applied at mount time (snap targets are
 * (re)computed once, from a `ResizeObserver` callback on the scroller's own
 * box, not per animation frame) — combined with `card.clientWidth` (the
 * flat, UNSCALED layout width; `clientWidth` never reflects a CSS
 * `transform`). Mixing a scaled/rotated rect.left with an unscaled
 * half-width is exactly the quirk that makes `scale_sweep`'s canonical
 * flick settle at `scrollLeft=1311`, not `1176` — see spring.ts's module
 * docstring for the full derivation.
 *
 * Replicated here by evaluating `transformFor(effect, scrollX=0, ...)` for
 * each card (the pose painted at mount, before any gesture), taking the
 * AABB left edge of that projected pose via `projectCardCorners`, adding
 * back HALF THE FLAT card width (not the scaled/projected width — mirrors
 * `clientWidth`), and finally clamping to the scroller's flat scrollable
 * range. Mirrors app/pipeline/carousel/effects.py:snap_positions.
 */
export function snapPositions(
  effect: EffectName,
  nCards: number,
  geo: CardGeometry,
  viewportW: number,
): number[] {
  const flatContentW =
    2 * centerLeft(geo, viewportW) + nCards * geo.cardW + Math.max(0, nCards - 1) * geo.gap;
  const boundMax = Math.max(0.0, flatContentW - viewportW);

  const positions: number[] = [];
  for (let i = 0; i < nCards; i += 1) {
    const t = transformFor(effect, 0.0, i, geo, viewportW);
    const corners = projectCardCorners(t, geo);
    const visualLeft = Math.min(...corners.map(([x]) => x));
    const snapX = visualLeft + geo.cardW / 2.0 - viewportW / 2.0;
    positions.push(clamp(snapX, 0.0, boundMax));
  }
  return positions;
}

/**
 * The scroller's flat (unscaled) scrollable range `[0, scrollWidth -
 * scrollerWidth]`, for `release`'s `bounds` clamp. Layout `scrollWidth` is
 * unaffected by CSS `transform`, so this is NOT effect-dependent (unlike
 * `snapPositions`, which mixes in each effect's painted-at-mount pose).
 * Mirrors app/pipeline/carousel/effects.py:snap_bounds.
 */
export function snapBounds(
  nCards: number,
  geo: CardGeometry,
  viewportW: number,
): [number, number] {
  const flatContentW =
    2 * centerLeft(geo, viewportW) + nCards * geo.cardW + Math.max(0, nCards - 1) * geo.gap;
  return [0.0, Math.max(0.0, flatContentW - viewportW)];
}
