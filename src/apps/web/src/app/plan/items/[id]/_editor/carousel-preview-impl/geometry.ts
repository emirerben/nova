/**
 * Pure timeline/geometry math for the carousel live-preview component
 * (CarouselBlockPreviewImpl). No React, no DOM — independently unit
 * testable.
 *
 * Mirrors `app/pipeline/carousel/segment.py`'s moment-authoring layer (the
 * part that turns a `CarouselMoment`-shaped config into a concrete
 * `FrameState[]` timeline via `carousel-preview`'s `buildTimeline`/
 * `rollingTimeline`), NOT `renderer.py`'s Skia painting — this module only
 * decides WHICH frame timeline to author and which frame index to read at a
 * given local time. See `card-style.ts` for the per-frame CSS-pose math.
 */

import type { CardGeometry, EffectName, FocusMoment, FrameState } from "@/lib/carousel-preview";
import {
  CANVAS_W,
  DEFAULT_GEOMETRY,
  buildTimeline,
  createFocusMoment,
  rollingTimeline,
} from "@/lib/carousel-preview";

// Mirrors app/pipeline/carousel/choreography.py (render.FPS) / segment.py's
// hardcoded 30fps frame math — keep in sync.
export const FPS = 30;

// Mirrors app/pipeline/carousel/segment.py:MAX_CARDS — bounds render cost
// (and here, DOM/video-element cost) even when the caller's clip pool is
// bigger.
export const MAX_CARDS = 5;

// Mirrors app/pipeline/carousel/segment.py:MAX_FOCUS_TOTAL_S.
export const MAX_FOCUS_TOTAL_S = 15.0;

// Mirrors CarouselPanel.tsx's prefill defaults (effect default when a
// moment has none set; "focus" is what a brand-new moment starts on).
export const DEFAULT_EFFECT: EffectName = "scale_sweep";
export const DEFAULT_MODE: "focus" | "rolling" = "focus";

export { DEFAULT_GEOMETRY, CANVAS_W };

/**
 * Fit `frames` to exactly `targetN` entries — 1:1 port of
 * `segment.py:_fit_duration`. Too many: truncate (keeps the opening
 * gesture, drops the inert settled tail). Too few: pad by repeating the
 * final frame's full state (scroll/focus/dim), only advancing `tS` — NOT
 * resetting to a default FrameState, which would snap focus off mid-hold.
 */
export function fitDuration(frames: readonly FrameState[], targetN: number): FrameState[] {
  if (frames.length === 0) return [];
  if (frames.length >= targetN) return frames.slice(0, targetN);

  const last = frames[frames.length - 1];
  const padded = frames.slice();
  for (let i = 0; i < targetN - frames.length; i += 1) {
    padded.push({ ...last, tS: last.tS + (i + 1) / FPS });
  }
  return padded;
}

export function resolveEffectiveMode(mode: "focus" | "rolling" | undefined): "focus" | "rolling" {
  return mode === "rolling" ? "rolling" : DEFAULT_MODE;
}

/**
 * `CarouselMoment.focus_clip_index == null` means "let Nova pick" at render
 * time. Mirrors `segment.py:_render_focus_mode`'s fallback:
 * `FocusMoment(card_index=min(1, n_cards - 1))` — the second card (or the
 * only card, for a 1-card pool).
 */
export function resolveFocusMoments(
  focusClipIndex: number | null | undefined,
  nCards: number,
): FocusMoment[] {
  if (nCards <= 0) return [];
  const idx =
    focusClipIndex == null
      ? Math.min(1, nCards - 1)
      : Math.max(0, Math.min(nCards - 1, focusClipIndex));
  return [createFocusMoment(idx)];
}

export interface MomentTimelineConfig {
  mode?: "focus" | "rolling";
  focus_clip_index?: number | null;
}

/**
 * Author the frame-by-frame timeline for one carousel moment.
 *
 * Divergence from the render pipeline (documented, deliberate): the backend
 * (`segment.py:_render_focus_mode`) only fits a `mode="focus"` timeline to
 * an explicit target length when the user set an override `duration_s`;
 * otherwise it just hard-caps at `MAX_FOCUS_TOTAL_S` and leaves the natural
 * choreography length as-is. The editor's virtual timeline, by contrast,
 * always treats a block as occupying an authoritative, fixed `durationS`
 * (Lane C positions it that way) — so this function ALWAYS fits/pads the
 * focus timeline to exactly `durationS` (capped at `MAX_FOCUS_TOTAL_S`).
 * Without this, scrubbing past the natural choreography length would read
 * out of range. `mode="rolling"` already takes `durationS` directly
 * (`rollingTimeline` fits/pads internally), so no divergence there.
 */
export function buildMomentTimeline(
  config: MomentTimelineConfig,
  nCards: number,
  durationS: number,
  geo: CardGeometry = DEFAULT_GEOMETRY,
  viewportW: number = CANVAS_W,
  seed = 0,
): FrameState[] {
  if (nCards <= 0) return [];
  const safeDurationS = Number.isFinite(durationS) && durationS > 0 ? durationS : 0.1;
  const mode = resolveEffectiveMode(config.mode);

  if (mode === "rolling") {
    return rollingTimeline(nCards, geo, viewportW, safeDurationS, { fps: FPS, seed });
  }

  const focusMoments = resolveFocusMoments(config.focus_clip_index, nCards);
  const frames = buildTimeline(nCards, geo, viewportW, { focusMoments, fps: FPS, seed });
  const targetN = Math.max(1, Math.round(Math.min(safeDurationS, MAX_FOCUS_TOTAL_S) * FPS));
  return fitDuration(frames, targetN);
}

/**
 * Natural (UNFITTED) duration of a focus-mode choreography for `nCards`
 * cards — how long the full lead-in + flick + settle + zoom-in + hold +
 * zoom-out + settle arc actually runs before `buildMomentTimeline`'s
 * fit/pad-to-`durationS` step truncates or pads it. Calls `buildTimeline`
 * directly (bypassing the fit step) so the result reflects the choreography
 * engine's own pacing, not whatever `durationS` a caller happens to pass.
 *
 * Used by CarouselPanel to default/inform the Length slider so a short
 * `duration_s` doesn't visibly cut off the zoom (see that file's docblock on
 * the duration-slider default + hint).
 */
export function naturalFocusTimelineLengthS(
  nCards: number,
  focusClipIndex: number | null | undefined,
  geo: CardGeometry = DEFAULT_GEOMETRY,
  viewportW: number = CANVAS_W,
  seed = 0,
): number {
  if (nCards <= 0) return 0;
  const focusMoments = resolveFocusMoments(focusClipIndex, nCards);
  const frames = buildTimeline(nCards, geo, viewportW, { focusMoments, fps: FPS, seed });
  return frames.length / FPS;
}

/** Clamp+round a block-local time (seconds) to a frame index into `frames`. */
export function resolveFrameIndex(frameCount: number, localTimeS: number): number {
  if (frameCount <= 0) return -1;
  const idx = Math.round(localTimeS * FPS);
  return Math.max(0, Math.min(frameCount - 1, idx));
}

/**
 * For every frame, the `tS` at which the CURRENTLY ACTIVE focus streak on
 * that frame began — i.e. the last frame where `focusCard` transitioned
 * from "not this card" (or unfocused) to "this card, focusT > 0". `null`
 * when the frame isn't focused at all.
 *
 * Generalizes `renderer.py`'s `focus_start_frame` dict (which resets a
 * card's full-tier video playback origin on every not-focused -> focused
 * transition, "so a card focused twice in one timeline restarts its
 * playback each time") from a frame-index lookup to a `tS` lookup, since
 * this TS port drives real `<video>` elements by time, not by discrete
 * decoded frame index. A single forward pass over the already-built
 * timeline — no wall-clock/ref state needed, so this stays a pure,
 * independently-testable function.
 */
export function computeFocusStartTimeline(frames: readonly FrameState[]): Array<number | null> {
  let currentFocusStartTS: number | null = null;
  let prevFocusCard: number | null = null;

  return frames.map((f) => {
    const isFocused = f.focusCard != null && f.focusT > 0;
    if (!isFocused) {
      prevFocusCard = null;
      currentFocusStartTS = null;
      return null;
    }
    const wasFocused = prevFocusCard === f.focusCard;
    if (!wasFocused) {
      currentFocusStartTS = f.tS;
    }
    prevFocusCard = f.focusCard;
    return currentFocusStartTS;
  });
}
