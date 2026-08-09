/**
 * Port of the Blossom Carousel scroll physics for the editor's live preview.
 *
 * 1:1 TypeScript port of `app/pipeline/carousel/spring.py`. See that
 * module's docstring for the full provenance (vendored bundle version,
 * the Round 1 painted-rect-snap and drag-activation-latch investigations)
 * — preserved verbatim below since it documents hard-won browser quirks
 * this port must not "clean up".
 *
 * ## What Round 1 found
 *
 * The FRICTION/DAMPING constants and roles were NOT swapped (a live
 * hypothesis going into this pass): the bundle defines `var u = .72, d =
 * .12` and uses `u` (friction) for both the per-tick velocity decay AND as
 * the drag-tracking damp factor, `d` (damping) only for the released/idle
 * damp factor toward `target` — exactly the roles FRICTION/DAMPING already
 * had here.
 *
 * 1. Snap positions are computed from the PAINTED (scaled/rotated) card
 *    rect, not the flat layout rect — see effects.ts:snapPositions.
 * 2. The tick loop doesn't start immediately at pointerdown — it starts
 *    once *cumulative* pointer movement crosses DRAG_ACTIVATION_PX (10px).
 *    The frame that crosses the threshold runs its FIRST tick with an
 *    effectively-zero frame_delta_ms, AND resets `target` to the current
 *    `virtualScroll` (discarding the activating delta's contribution to
 *    target; velocity is untouched). `simulateFrom` below reproduces both
 *    the zero-delay tick and the target-reset with `tickActive` +
 *    `totalDragPx` on SpringState.
 * 3. The picked snap position is clamped to the scroller's flat scrollable
 *    range before being converted into a release force — `release()`'s
 *    optional `bounds` parameter (`undefined` skips the clamp).
 */

import type { GestureTrace, SpringFrame, SpringState } from "./types";
import { createSpringState } from "./types";

// Mirrors app/pipeline/carousel/spring.py:FRICTION — keep in sync (golden
// trace test pins this).
export const FRICTION = 0.72;
// Mirrors app/pipeline/carousel/spring.py:DAMPING — keep in sync (golden
// trace test pins this).
export const DAMPING = 0.12;

// Mirrors app/pipeline/carousel/spring.py:DRAG_ACTIVATION_PX — keep in sync
// (golden trace test pins this). Bundle: `_.x >= 10` (Proxy setter on the
// drag-distance accumulator) gates both (a) whether the tick loop runs at
// all during a drag, and (b) whether release()'s snap-seeking logic runs at
// pointerup — below this, a gesture is a tap, not a flick.
export const DRAG_ACTIVATION_PX = 10.0;

/**
 * Python's `round(value, ndigits)` uses round-half-to-even ("banker's
 * rounding") applied to the EXACT decimal expansion of the double — NOT
 * `Math.round(value * 10**n) / 10**n` (which rounds ties away from zero AND
 * introduces its own scaling error). This helper decodes the exact decimal
 * digits of `value` via `toFixed` (which the ECMA-262 spec requires to
 * produce the true decimal expansion, not a rounded approximation, for any
 * requested digit count that lands within the double's terminating
 * expansion) with a comfortable guard band past `ndigits`, then applies
 * ties-to-even at the `ndigits` boundary by hand.
 *
 * Used by `isSettled` (`round(velocity, 12) == 0`, spring.py) and by every
 * integer `round()` call ported from choreography.py/effects.py (frame
 * counts, z-index) — those pass `ndigits = 0`.
 */
export function pythonRound(value: number, ndigits = 0): number {
  if (!Number.isFinite(value)) return value;
  if (value === 0) return 0;

  const sign = value < 0 ? -1 : 1;
  const abs = Math.abs(value);

  // Guard digits past `ndigits` so we can see whether the true expansion is
  // an exact tie (`...5000...`) or merely close to one. 25 guard digits is
  // comfortably past a double's ~17 significant decimal digits.
  const guardDigits = Math.max(0, ndigits) + 25;
  const fixed = abs.toFixed(Math.min(100, guardDigits));
  const dotIdx = fixed.indexOf(".");
  const intPart = dotIdx === -1 ? fixed : fixed.slice(0, dotIdx);
  const fracPart = dotIdx === -1 ? "" : fixed.slice(dotIdx + 1);

  if (ndigits >= fracPart.length) {
    return sign * abs;
  }

  const keep = fracPart.slice(0, Math.max(0, ndigits));
  const rest = fracPart.slice(Math.max(0, ndigits));

  const digits = (intPart + keep).split("").map(Number);
  const firstRestDigit = rest.length > 0 ? Number(rest[0]) : 0;

  let roundUp = false;
  if (firstRestDigit > 5) {
    roundUp = true;
  } else if (firstRestDigit === 5) {
    const isExactHalf = /^5(0*)$/.test(rest);
    if (!isExactHalf) {
      roundUp = true;
    } else {
      const lastKeptDigit = digits.length > 0 ? digits[digits.length - 1] : 0;
      roundUp = lastKeptDigit % 2 === 1; // ties-to-even
    }
  }

  if (roundUp) {
    let i = digits.length - 1;
    while (i >= 0) {
      digits[i] += 1;
      if (digits[i] === 10) {
        digits[i] = 0;
        i -= 1;
      } else {
        break;
      }
    }
    if (i < 0) digits.unshift(1);
  }

  const newIntLen = digits.length - Math.max(0, ndigits);
  const newIntPart = digits.slice(0, newIntLen).join("") || "0";
  const newFracPart = digits.slice(newIntLen).join("");
  const resultStr = ndigits > 0 ? `${newIntPart}.${newFracPart}` : newIntPart;

  return sign * parseFloat(resultStr);
}

/**
 * Framerate-independent exponential smoothing of `x` toward `y`.
 *
 * JS (source): `lerp(x, y, 1 - Math.exp(Math.log(1 - t) * (delta / (1000 /
 * 60))))`. Mirrors app/pipeline/carousel/spring.py:damp.
 */
export function damp(x: number, y: number, t: number, deltaMs: number): number {
  const factor = 1 - Math.exp(Math.log(1 - t) * (deltaMs / (1000 / 60)));
  return x + (y - x) * factor;
}

/** Mirrors app/pipeline/carousel/spring.py:project. */
export function project(target: number, velocity: number, friction: number = FRICTION): number {
  return target + velocity / (1 - friction);
}

/**
 * Advance the spring by one animation frame. Only meaningful once the tick
 * loop is actually running (`state.tickActive`) — `simulateFrom` is
 * responsible for not calling this before DRAG_ACTIVATION_PX is crossed.
 * Mirrors app/pipeline/carousel/spring.py:tick.
 */
export function tick(state: SpringState, frameDeltaMs: number): SpringState {
  const velocity = state.velocity * FRICTION;
  let target: number;
  let virtualScroll: number;
  if (!state.isDragging) {
    target = state.target + velocity;
    virtualScroll = damp(state.virtualScroll, target, DAMPING, frameDeltaMs);
  } else {
    target = state.target;
    virtualScroll = damp(state.virtualScroll, state.target, FRICTION, frameDeltaMs);
  }
  return { ...state, velocity, target, virtualScroll };
}

/**
 * Called once at pointer-up: project the resting scroll position, snap to
 * the nearest candidate within a proximity threshold (else the nearest snap
 * position overall), clamp that choice to `bounds` (the scroller's flat
 * scrollable range, if given), and convert the required correction into a
 * velocity that drives the overshoot-settle animation.
 *
 * Below DRAG_ACTIVATION_PX of total drag, the bundle treats the gesture as
 * a tap, not a flick, and skips this entirely — just stops dragging with
 * whatever target/velocity already stand. Mirrors
 * app/pipeline/carousel/spring.py:release.
 */
export function release(
  state: SpringState,
  snapPositions: readonly number[],
  snapportWidth: number,
  bounds?: readonly [number, number],
): SpringState {
  if (state.totalDragPx <= DRAG_ACTIVATION_PX) {
    return { ...state, isDragging: false };
  }

  const velocity = state.velocity * 2;
  const restingX = project(state.target, velocity, FRICTION);
  const threshold = snapportWidth / 3;
  const candidates = snapPositions.filter((p) => Math.abs(p - restingX) <= threshold);
  const pool = candidates.length > 0 ? candidates : snapPositions;
  let slideX = pool.reduce((best, p) =>
    Math.abs(p - restingX) < Math.abs(best - restingX) ? p : best,
  );
  if (bounds !== undefined) {
    const [lo, hi] = bounds;
    slideX = Math.min(hi, Math.max(lo, slideX));
  }
  const force = (slideX - state.target) * (1 - FRICTION) * (1 / FRICTION);
  return { ...state, isDragging: false, velocity: force };
}

/** Mirrors app/pipeline/carousel/spring.py:is_settled. */
export function isSettled(state: SpringState): boolean {
  return pythonRound(state.velocity, 12) === 0.0;
}

/** Mirrors app/pipeline/carousel/spring.py:rubberband_offset. */
export function rubberbandOffset(
  offset: number,
  overscroll: number,
  isDragging: boolean,
  frameDeltaMs: number,
): number {
  const t = isDragging ? 0.8 : DAMPING;
  return damp(offset, overscroll * -0.2, t, frameDeltaMs);
}

export interface SimulateFromOptions {
  readonly bounds?: readonly [number, number];
  readonly maxFrames?: number;
  readonly startFrameIndex?: number;
}

/**
 * Lower-level engine behind `simulate`: replay `dragDeltasPx` (same
 * finger-delta convention as `GestureTrace.dragDeltasPx`) against an
 * ARBITRARY starting `state` — which need not be a fresh pointerdown; the
 * caller decides what isDragging/totalDragPx/tickActive should be going in
 * — then release() and tick until settled.
 *
 * `frameIndex` (hence each emitted `SpringFrame.tS = frameIndex / fps`)
 * starts counting from `startFrameIndex`, NOT 0: this lets a caller chain
 * multiple calls (e.g. choreography.ts's buildTimeline stitching several
 * flicks back to back onto one continuous clock) by passing the previous
 * call's final frame count back in.
 *
 * Returns `{ finalState, frames }` — `frames` holds one SpringFrame per
 * animation frame advanced (delta loop + settle loop), NOT a frame for
 * `state` itself. Mirrors app/pipeline/carousel/spring.py:simulate_from.
 */
export function simulateFrom(
  initialState: SpringState,
  dragDeltasPx: readonly number[],
  snapPositions: readonly number[],
  snapportWidth: number,
  fps: number,
  options: SimulateFromOptions = {},
): { finalState: SpringState; frames: SpringFrame[] } {
  const { bounds, maxFrames = 600, startFrameIndex = 0 } = options;
  const frameDeltaMs = 1000 / fps;
  const frames: SpringFrame[] = [];
  let frameIndex = startFrameIndex;
  let state = initialState;

  const emit = (): void => {
    frameIndex += 1;
    frames.push({
      tS: frameIndex / fps,
      virtualScroll: state.virtualScroll,
      velocity: state.velocity,
      target: state.target,
    });
  };

  for (const fingerDelta of dragDeltasPx) {
    const scrollDelta = -fingerDelta;
    const wasActive = state.tickActive;
    const totalDragPx = state.totalDragPx + Math.abs(scrollDelta);
    state = {
      ...state,
      target: state.target + scrollDelta,
      velocity: state.velocity + scrollDelta,
      totalDragPx,
    };
    const justActivated = !wasActive && totalDragPx >= DRAG_ACTIVATION_PX;
    if (justActivated) {
      // The activation-latching frame: the bundle resets `target` to the
      // CURRENT virtualScroll (discarding this delta's — and every prior
      // delta's — contribution to target; velocity is untouched), then
      // runs its first tick with the clock reference just set, so ~0ms
      // elapsed — a real tick (it still decays velocity) that leaves
      // virtualScroll in place since target == virtualScroll going in.
      state = { ...state, tickActive: true, target: state.virtualScroll };
      state = tick(state, 0.0);
    } else if (wasActive) {
      state = tick(state, frameDeltaMs);
    }
    // else: still below the activation threshold this frame — no tick;
    // target/velocity keep accumulating undamped/undecayed.
    emit();
  }

  state = release(state, snapPositions, snapportWidth, bounds);

  while (!isSettled(state) && frameIndex < maxFrames) {
    state = tick(state, frameDeltaMs);
    emit();
  }

  return { finalState: state, frames };
}

export interface SimulateOptions {
  readonly startScroll?: number;
  readonly maxFrames?: number;
  readonly bounds?: readonly [number, number];
}

/**
 * Replay a scripted drag+release gesture through the spring and return one
 * SpringFrame per animation frame, from the pointerdown frame through
 * settle — frame `i` of the returned list lines up 1:1 with `trace[i]` of a
 * browser capture (see this module's docstring: the browser's harness.js
 * also captures a frame right after pointerdown, before any movement or
 * ticking has happened).
 *
 * Sign convention: `gesture.dragDeltasPx` are FINGER movement per frame
 * (negative = finger moves left = carousel advances). Blossom computes
 * `deltaX = pointerStart.x - clientX`, i.e. scroll delta = -fingerDelta.
 *
 * Delegates the post-pointerdown drag+release+settle loop to `simulateFrom`
 * — this function only owns the leading pointerdown-frame emission. Mirrors
 * app/pipeline/carousel/spring.py:simulate.
 */
export function simulate(
  gesture: GestureTrace,
  snapPositions: readonly number[],
  snapportWidth: number,
  options: SimulateOptions = {},
): SpringFrame[] {
  const { startScroll = 0.0, maxFrames = 600, bounds } = options;
  const state = createSpringState({
    virtualScroll: startScroll,
    target: startScroll,
    velocity: 0.0,
    isDragging: true,
  });
  const leading: SpringFrame = {
    tS: 1 / gesture.fps,
    virtualScroll: state.virtualScroll,
    velocity: state.velocity,
    target: state.target,
  };
  const { frames: rest } = simulateFrom(
    state,
    gesture.dragDeltasPx,
    snapPositions,
    snapportWidth,
    gesture.fps,
    { bounds, maxFrames, startFrameIndex: 1 },
  );
  return [leading, ...rest];
}
