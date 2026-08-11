/**
 * Timeline authoring for carousel V2's FOCUS CHOREOGRAPHY and ROLLING
 * modes: turns a list of FocusMoments (or a plain duration, for rolling)
 * into a frame-by-frame FrameState timeline — scroll position plus
 * focus/dim state. Port of `app/pipeline/carousel/choreography.py` (the
 * timeline-authoring half only; `renderer.render_choreography_frames`,
 * which PAINTS this timeline, is out of scope for the editor's live
 * preview).
 *
 * Reuses the parity-proven spring engine (spring.ts) unchanged: every
 * scroll movement here is a REAL flick run through `simulateFrom`, just
 * with a solved-for delta-scale so it lands on a specific card instead of
 * replaying CANONICAL_FLICK verbatim. See `solveFlickScale`'s docstring
 * for the numeric approach.
 *
 * Coordinate model: this module always drives the spring off the FLAT
 * (unscaled) snap grid `i * (cardW + gap)` — NOT `effects.snapPositions`'
 * per-effect painted-pose grid. That grid exists to replicate a real-DOM
 * CSS quirk (mount-time snap targets computed from an already-painted,
 * possibly scaled pose); it has no bearing here because this module isn't
 * reproducing a captured browser trace, it's AUTHORING a new one from
 * scratch, and the flat grid is provably the scroll position that centers
 * card `i` for every effect (`effects.viewProgress(i*pitch, i, ...) ==
 * 0.5` regardless of which effect transform consumes it).
 * `buildTimeline`/`rollingTimeline` therefore take `geo`/`viewportW` but no
 * `effect` parameter at all.
 */

import { PythonRandom } from "./python-random";
import { pythonRound, simulateFrom } from "./spring";
import { CANONICAL_FLICK } from "./gesture";
import * as effects from "./effects";
import type { CardGeometry, FocusMoment, FrameState, SpringState } from "./types";
import { createFocusMoment, createFrameState, createSpringState } from "./types";

// Fraction of the render canvas' non-focused-card dim applied at full focus
// (CSS `filter: brightness(1 - DIM_MAX)` equivalent, approximated in the
// renderer as a black overlay at this alpha). Mirrors
// app/pipeline/carousel/choreography.py:DIM_MAX — keep in sync (fixture
// test pins this).
export const DIM_MAX = 0.55;

// Beyond this focus_t, the renderer switches a focused card's face source
// from the small card-tier JPEGs to the full-resolution tier — not
// consumed by this TS port (no renderer here), kept for parity/reference.
// Mirrors app/pipeline/carousel/choreography.py:FULLRES_SWITCH_T.
export const FULLRES_SWITCH_T = 0.35;

// Jitter band applied (via a seeded PythonRandom) to every hold/pad
// duration below — NOT to flick physics or zoomS, which stay deterministic
// functions of the spring engine / the caller's FocusMoment. Mirrors
// app/pipeline/carousel/choreography.py:JITTER_FRAC — keep in sync
// (fixture test pins this).
export const JITTER_FRAC = 0.1;

function pitch(geo: CardGeometry): number {
  return geo.cardW + geo.gap;
}

/** Scroll position that centers card `i`: `i * pitch` — see the module
 * docstring for why this (not effects.snapPositions) is the right grid for
 * an authored timeline. Mirrors choreography.py:_flat_snap_positions. */
function flatSnapPositions(nCards: number, geo: CardGeometry): number[] {
  const p = pitch(geo);
  return Array.from({ length: nCards }, (_, i) => i * p);
}

/** Mirrors choreography.py:_nearest_snap_index — picks the FIRST minimal
 * index on a tie, matching Python's `min(..., key=...)`. */
function nearestSnapIndex(x: number, snaps: readonly number[]): number {
  let bestIdx = 0;
  let bestDist = Infinity;
  for (let i = 0; i < snaps.length; i += 1) {
    const dist = Math.abs(snaps[i] - x);
    if (dist < bestDist) {
      bestDist = dist;
      bestIdx = i;
    }
  }
  return bestIdx;
}

/** Mirrors choreography.py:_ease_out_cubic. */
function easeOutCubic(t: number): number {
  const clamped = Math.max(0.0, Math.min(1.0, t));
  return 1.0 - (1.0 - clamped) ** 3;
}

/**
 * A settled state (from a prior flick's settle-loop, or the initial
 * centered position) reinterpreted as the START of a brand-new drag: fresh
 * totalDragPx/tickActive (a new gesture hasn't crossed DRAG_ACTIVATION_PX
 * yet), `target` reset to the current (already converged) `virtualScroll`,
 * velocity zeroed. Mirrors choreography.py:_reset_for_new_gesture.
 */
function resetForNewGesture(state: SpringState): SpringState {
  return {
    ...state,
    isDragging: true,
    totalDragPx: 0.0,
    tickActive: false,
    velocity: 0.0,
    target: state.virtualScroll,
  };
}

function landedIndex(
  state: SpringState,
  k: number,
  snaps: readonly number[],
  snapportWidth: number,
  fps: number,
  bounds: readonly [number, number] | undefined,
): number {
  const deltas = CANONICAL_FLICK.dragDeltasPx.map((d) => d * k);
  const { finalState } = simulateFrom(state, deltas, snaps, snapportWidth, fps, { bounds });
  return nearestSnapIndex(finalState.virtualScroll, snaps);
}

/**
 * Find a scale factor `k` such that scaling CANONICAL_FLICK.dragDeltasPx by
 * `k` and replaying it from `state` lands on `snaps[targetIndex]` — pure
 * numeric root-find over the spring engine itself (no hand-tuned deltas).
 *
 * `k`'s SIGN reuses CANONICAL_FLICK's own sign convention: positive `k`
 * replays it as-is (finger drags left, scroll increases, carousel advances
 * toward higher indices); negative `k` flips every delta's sign.
 * `landedIndex(k)` is a monotonic, non-decreasing step function of `k` in
 * practice, so a doubling-then-bisecting search converges on the smallest
 * `k` (in the needed direction) whose landing lands on `targetIndex`.
 * Mirrors choreography.py:_solve_flick_scale.
 */
function solveFlickScale(
  state: SpringState,
  targetIndex: number,
  snaps: readonly number[],
  snapportWidth: number,
  fps: number,
  bounds: readonly [number, number] | undefined,
): number {
  const currentIndex = nearestSnapIndex(state.virtualScroll, snaps);
  if (currentIndex === targetIndex) return 0.0;

  const direction = targetIndex > currentIndex ? 1.0 : -1.0;
  const reached = (idx: number): boolean =>
    direction > 0 ? idx >= targetIndex : idx <= targetIndex;

  let lo = 0.0;
  let hi = direction * 1.0;
  let bracketed = false;
  for (let i = 0; i < 40; i += 1) {
    if (reached(landedIndex(state, hi, snaps, snapportWidth, fps, bounds))) {
      bracketed = true;
      break;
    }
    hi *= 2.0;
  }
  if (!bracketed) {
    // Never bracketed targetIndex (e.g. it's outside the reachable
    // bounds) — fall back to whatever the largest tried `hi` lands on; the
    // caller still gets a legal, if short, flick rather than a crash.
    return hi;
  }

  for (let i = 0; i < 50; i += 1) {
    const mid = (lo + hi) / 2.0;
    if (reached(landedIndex(state, mid, snaps, snapportWidth, fps, bounds))) {
      hi = mid;
    } else {
      lo = mid;
    }
  }

  return hi;
}

/**
 * Solve + replay one flick from `state` to `targetIndex`. Returns the
 * settled end state and the per-frame `virtualScroll` trace (empty if no
 * flick was needed, i.e. `targetIndex` was already centered). Mirrors
 * choreography.py:_run_flick.
 */
function runFlick(
  state: SpringState,
  targetIndex: number,
  snaps: readonly number[],
  snapportWidth: number,
  fps: number,
  bounds: readonly [number, number] | undefined,
): { state: SpringState; scrolls: number[] } {
  const gestureState = resetForNewGesture(state);
  const k = solveFlickScale(gestureState, targetIndex, snaps, snapportWidth, fps, bounds);
  if (k === 0.0) {
    return { state: { ...state, isDragging: false }, scrolls: [] };
  }

  const deltas = CANONICAL_FLICK.dragDeltasPx.map((d) => d * k);
  const { finalState, frames } = simulateFrom(gestureState, deltas, snaps, snapportWidth, fps, {
    bounds,
  });
  return { state: finalState, scrolls: frames.map((sf) => sf.virtualScroll) };
}

export interface BuildTimelineOptions {
  readonly focusMoments?: readonly FocusMoment[];
  readonly fps?: number;
  readonly leadInS?: number;
  readonly settlePadS?: number;
  readonly seed?: number;
  readonly manualTiming?: boolean;
  readonly moveDurationS?: number;
}

/**
 * Author a FOCUS CHOREOGRAPHY timeline: lead-in hold, then for each focus
 * moment (sorted by cardIndex) — flick to center it, settle-pad hold,
 * ease-in to fullscreen (focusT 0->1, dim ramps 0->DIM_MAX), hold at
 * fullscreen, ease back out (mirror), settle-pad hold — then one final
 * flick onward (next card, or back one if the last moment was already the
 * final card) and a trailing settle pad so the segment ends in
 * motion-rest, not frozen on a focus.
 *
 * Uses `easeOutCubic` (not a `damp()` exponential) for the focusT ramp:
 * damp's per-frame delta is LARGEST on frame 1, which blows well past a
 * smooth continuity budget; `easeOutCubic` sampled at uniform time steps
 * scales its largest per-frame delta down with more frames and reaches
 * exactly 1.0/0.0 on its literal last sample with no patch-up needed.
 *
 * Deterministic for a given `seed`: only hold/pad durations are jittered
 * (±JITTER_FRAC, via a seeded PythonRandom) — flick physics and `zoomS`
 * itself are untouched. Mirrors choreography.py:build_timeline.
 */
export function buildTimeline(
  nCards: number,
  geo: CardGeometry,
  viewportW: number,
  options: BuildTimelineOptions = {},
): FrameState[] {
  const {
    focusMoments = [],
    fps = 30,
    leadInS = 0.4,
    settlePadS = 0.3,
    seed = 0,
    manualTiming = false,
    moveDurationS,
  } = options;

  const dt = 1.0 / fps;
  const snaps = flatSnapPositions(nCards, geo);
  const bounds = effects.snapBounds(nCards, geo, viewportW);
  const rng = new PythonRandom(seed);

  const frames: FrameState[] = [];
  let tCursor = 0.0;

  const jitter = (baseS: number): number =>
    manualTiming ? baseS : baseS * (1.0 + rng.uniform(-JITTER_FRAC, JITTER_FRAC));

  const retimeScrolls = (scrolls: readonly number[]): readonly number[] => {
    if (moveDurationS == null || scrolls.length === 0) return scrolls;
    const targetN = Math.max(1, pythonRound(moveDurationS * fps));
    if (targetN === 1) return [scrolls[scrolls.length - 1]];
    const last = scrolls.length - 1;
    return Array.from({ length: targetN }, (_, i) => scrolls[pythonRound((i * last) / (targetN - 1))]);
  };

  const hold = (scrollX: number, seconds: number): void => {
    const n = Math.max(0, pythonRound(seconds * fps));
    for (let i = 0; i < n; i += 1) {
      tCursor += dt;
      frames.push(createFrameState({ tS: tCursor, scrollX }));
    }
  };

  const appendScrolls = (scrolls: readonly number[]): void => {
    for (const sx of scrolls) {
      tCursor += dt;
      frames.push(createFrameState({ tS: tCursor, scrollX: sx }));
    }
  };

  const startScroll = snaps.length > 0 ? snaps[0] : 0.0;
  let state = createSpringState({
    virtualScroll: startScroll,
    target: startScroll,
    velocity: 0.0,
    isDragging: false,
  });

  hold(state.virtualScroll, jitter(leadInS));

  const orderedMoments = manualTiming
    ? [...focusMoments]
    : [...focusMoments].sort((a, b) => a.cardIndex - b.cardIndex);

  for (const moment of orderedMoments) {
    const targetIndex = Math.max(0, Math.min(nCards - 1, moment.cardIndex));

    const flickResult = runFlick(state, targetIndex, snaps, viewportW, fps, bounds);
    state = flickResult.state;
    appendScrolls(retimeScrolls(flickResult.scrolls));

    const centeredScroll = snaps.length > 0 ? snaps[targetIndex] : 0.0;
    state = { ...state, virtualScroll: centeredScroll, target: centeredScroll, velocity: 0.0 };

    hold(centeredScroll, jitter(settlePadS));

    const nZoom = Math.max(2, pythonRound(moment.zoomS * fps));
    for (let i = 1; i <= nZoom; i += 1) {
      tCursor += dt;
      const ft = easeOutCubic(i / nZoom);
      frames.push(
        createFrameState({
          tS: tCursor,
          scrollX: centeredScroll,
          focusCard: targetIndex,
          focusT: ft,
          dim: DIM_MAX * ft,
        }),
      );
    }

    const nHold = Math.max(1, pythonRound(jitter(moment.holdS) * fps));
    for (let i = 0; i < nHold; i += 1) {
      tCursor += dt;
      frames.push(
        createFrameState({
          tS: tCursor,
          scrollX: centeredScroll,
          focusCard: targetIndex,
          focusT: 1.0,
          dim: DIM_MAX,
        }),
      );
    }

    for (let i = 1; i <= nZoom; i += 1) {
      tCursor += dt;
      const ft = 1.0 - (i / nZoom) ** 3; // mirror of easeOutCubic: 1 -> 0, decelerating into 0
      frames.push(
        createFrameState({
          tS: tCursor,
          scrollX: centeredScroll,
          focusCard: targetIndex,
          focusT: ft,
          dim: DIM_MAX * ft,
        }),
      );
    }

    hold(centeredScroll, jitter(settlePadS));
  }

  if (orderedMoments.length > 0 && !manualTiming) {
    const lastIndex = Math.max(
      0,
      Math.min(nCards - 1, orderedMoments[orderedMoments.length - 1].cardIndex),
    );
    const nextIndex = lastIndex + 1 < nCards ? lastIndex + 1 : Math.max(0, lastIndex - 1);
    if (nextIndex !== lastIndex) {
      const flickResult = runFlick(state, nextIndex, snaps, viewportW, fps, bounds);
      state = flickResult.state;
      appendScrolls(flickResult.scrolls);
    }
    hold(state.virtualScroll, jitter(settlePadS));
  }

  return frames;
}

export interface RollingTimelineOptions {
  readonly fps?: number;
  readonly seed?: number;
  readonly sequence?: readonly FocusMoment[];
  readonly moveDurationS?: number;
  readonly manualTiming?: boolean;
}

/**
 * Author a ROLLING timeline: no focus, just a sequence of flicks advancing
 * card by card through the whole set (seeded, slightly jittered hold
 * timing between flicks), trimmed/padded to exactly `round(durationS *
 * fps)` frames — same house convention as `segment._fit_duration`
 * (truncate an over-long trace; pad by repeating the final settled scroll
 * position for an under-long one). Mirrors
 * choreography.py:rolling_timeline.
 */
export function rollingTimeline(
  nCards: number,
  geo: CardGeometry,
  viewportW: number,
  durationS: number,
  options: RollingTimelineOptions = {},
): FrameState[] {
  const {
    fps = 30,
    seed = 0,
    sequence = [],
    moveDurationS,
    manualTiming = false,
  } = options;

  const dt = 1.0 / fps;
  const snaps = flatSnapPositions(nCards, geo);
  const bounds = effects.snapBounds(nCards, geo, viewportW);
  const rng = new PythonRandom(seed);

  let frames: FrameState[] = [];
  let tCursor = 0.0;

  const jitter = (baseS: number): number =>
    manualTiming ? baseS : baseS * (1.0 + rng.uniform(-JITTER_FRAC, JITTER_FRAC));

  const hold = (scrollX: number, seconds: number): void => {
    const n = Math.max(0, pythonRound(seconds * fps));
    for (let i = 0; i < n; i += 1) {
      tCursor += dt;
      frames.push(createFrameState({ tS: tCursor, scrollX }));
    }
  };

  const startScroll = snaps.length > 0 ? snaps[0] : 0.0;
  let state = createSpringState({
    virtualScroll: startScroll,
    target: startScroll,
    velocity: 0.0,
    isDragging: false,
  });

  const targets = manualTiming && sequence.length > 0
    ? [...sequence]
    : Array.from({ length: Math.max(0, nCards - 1) }, (_, i) =>
        createFocusMoment(i + 1, { holdS: 0.3 }),
      );
  if (!manualTiming) hold(state.virtualScroll, jitter(0.3));

  for (const item of targets) {
    if (tCursor >= durationS) break;
    const idx = Math.max(0, Math.min(nCards - 1, item.cardIndex));
    const flickResult = runFlick(state, idx, snaps, viewportW, fps, bounds);
    state = flickResult.state;
    let scrolls = flickResult.scrolls;
    if (manualTiming && moveDurationS != null && scrolls.length > 0) {
      const targetN = Math.max(1, pythonRound(moveDurationS * fps));
      const last = scrolls.length - 1;
      scrolls = targetN === 1
        ? [scrolls[last]]
        : Array.from({ length: targetN }, (_, i) => scrolls[pythonRound((i * last) / (targetN - 1))]);
    }
    for (const sx of scrolls) {
      tCursor += dt;
      frames.push(createFrameState({ tS: tCursor, scrollX: sx }));
    }
    const centeredScroll = snaps.length > 0 ? snaps[idx] : 0.0;
    state = { ...state, virtualScroll: centeredScroll, target: centeredScroll, velocity: 0.0 };
    hold(centeredScroll, jitter(item.holdS));
  }

  const targetN = Math.max(1, pythonRound(durationS * fps));
  if (frames.length < targetN) {
    const lastScroll = frames.length > 0 ? frames[frames.length - 1].scrollX : startScroll;
    const missing = targetN - frames.length;
    for (let i = 0; i < missing; i += 1) {
      tCursor += dt;
      frames.push(createFrameState({ tS: tCursor, scrollX: lastScroll }));
    }
  } else if (frames.length > targetN) {
    frames = frames.slice(0, targetN);
  }

  return frames;
}
