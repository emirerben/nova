/**
 * timeline-scale — px-per-second scale math for the editor-shell timeline
 * (plan §6 "Zoom"). ALL bar / playhead / scrub math routes through these pure
 * helpers so a single scale drives geometry, scroll width, and the ruler.
 *
 * Pure (no DOM, no React) — unit-tested in __tests__/timeline-scale.test.ts
 * (px↔seconds round-trip, fit math, adaptive tick density).
 *
 * "fit" = the scale at which the whole clip exactly fills the visible track
 * width: fitPxPerSecond(width, duration) * duration === width.
 */

/** Zoom envelope. Fit can fall below MIN for very long clips; callers that
 * start at fit clamp with `clampPxPerSecond` only for user-driven zoom steps,
 * never for the fit baseline itself. */
export const MIN_PX_PER_SECOND = 4;
export const MAX_PX_PER_SECOND = 480;

/** Candidate ruler intervals (seconds). Adaptive density picks the smallest
 * whose on-screen label spacing clears `minLabelPx` — so sub-10s clips get 1s
 * (or finer) labels, and "every 5s" is just the mid-range case, never a
 * constant (plan §5 filmstrip/ruler note). */
const TICK_CANDIDATES = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300] as const;

/** Scale (px/s) at which `durationS` exactly fills `viewportWidth`. */
export function fitPxPerSecond(viewportWidth: number, durationS: number): number {
  if (durationS <= 0 || viewportWidth <= 0) return MIN_PX_PER_SECOND;
  return viewportWidth / durationS;
}

/** Clamp a user-driven scale into the zoom envelope. */
export function clampPxPerSecond(pps: number): number {
  return Math.min(MAX_PX_PER_SECOND, Math.max(MIN_PX_PER_SECOND, pps));
}

/** Seconds → pixels at the given scale. */
export function secondsToPx(seconds: number, pxPerSecond: number): number {
  return seconds * pxPerSecond;
}

/** Pixels → seconds at the given scale (inverse of `secondsToPx`). */
export function pxToSeconds(px: number, pxPerSecond: number): number {
  return pxPerSecond > 0 ? px / pxPerSecond : 0;
}

/** Total track pixel width for a clip at the given scale (drives the
 * horizontal-scroll content width when zoomed). */
export function scaledTrackWidth(durationS: number, pxPerSecond: number): number {
  return Math.max(0, durationS * pxPerSecond);
}

export interface EditorTimelineScaleInput {
  viewportWidth: number;
  durationS: number;
  zoom: number;
  frozenFitPxPerSecond: number | null;
  refit?: boolean;
}

export interface EditorTimelineScale {
  fitPxPerSecond: number;
  pxPerSecond: number;
}

/**
 * Editor timelines freeze the fit baseline after initial load. Duration edits
 * then shorten/lengthen the track instead of reactively rescaling every bar.
 */
export function resolveEditorTimelineScale({
  viewportWidth,
  durationS,
  zoom,
  frozenFitPxPerSecond,
  refit = false,
}: EditorTimelineScaleInput): EditorTimelineScale {
  const liveFit = fitPxPerSecond(viewportWidth, durationS);
  const fit = refit || frozenFitPxPerSecond == null ? liveFit : frozenFitPxPerSecond;
  return {
    fitPxPerSecond: fit,
    pxPerSecond: clampPxPerSecond(fit * Math.max(1, zoom)),
  };
}

/**
 * Adaptive ruler interval (seconds) for the current scale: the finest
 * candidate whose label pitch is at least `minLabelPx` on screen. Falls back
 * to the coarsest candidate when even that is too dense.
 */
export function tickIntervalForScale(
  pxPerSecond: number,
  minLabelPx = 52,
): number {
  for (const c of TICK_CANDIDATES) {
    if (secondsToPx(c, pxPerSecond) >= minLabelPx) return c;
  }
  return TICK_CANDIDATES[TICK_CANDIDATES.length - 1];
}

/** Ordered tick seconds for `[0, durationS]` at the adaptive interval. */
export function rulerTicks(
  durationS: number,
  pxPerSecond: number,
  minLabelPx = 52,
): number[] {
  if (durationS <= 0) return [0];
  const interval = tickIntervalForScale(pxPerSecond, minLabelPx);
  const count = Math.floor(durationS / interval);
  return Array.from({ length: count + 1 }, (_, i) => i * interval);
}
