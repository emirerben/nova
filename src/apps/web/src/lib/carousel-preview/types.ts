/**
 * Shared types for the carousel-preview TS port.
 *
 * Field names are camelCased ports of the Python dataclasses' snake_case
 * fields (e.g. `virtual_scroll` -> `virtualScroll`). See each source file
 * cited per-field for the authoritative shape.
 */

// Mirrors app/pipeline/carousel/effects.py:EFFECTS — keep in sync (golden
// trace test pins this).
export const EFFECTS = ["scale_sweep", "cover_flow", "cards_stack", "flipbook"] as const;
export type EffectName = (typeof EFFECTS)[number];

/** Mirrors app/pipeline/carousel/effects.py:CardGeometry. */
export interface CardGeometry {
  readonly cardW: number;
  readonly cardH: number;
  readonly gap: number;
  readonly cornerRadius: number;
}

/** Default geometry used everywhere in this package unless overridden.
 * Mirrors app/pipeline/carousel/segment.py:DEFAULT_GEOMETRY — keep in sync. */
export const DEFAULT_GEOMETRY: CardGeometry = {
  cardW: 540,
  cardH: 720,
  gap: 48,
  cornerRadius: 24,
};

/**
 * Mirrors app/pipeline/carousel/effects.py:CardTransform.
 *
 * CSS 3D transforms compose in the ORDER the functions are LISTED, applied
 * innermost-first from the *right* end of the list (`transform: A B` means
 * B applies to the point first, then A). `cover_flow`'s keyframes list
 * `rotateY(...) translateZ(...) scale(...)` -> apply order scale, then
 * translateZ, then rotateY LAST (project-corners.ts's default: rotate the
 * already-translated (x, z) pair as one unit). `flipbook`'s keyframes list
 * `translateZ(...) rotateY(...)` -> the OPPOSITE order: rotateY applies
 * FIRST (to (x, z=0)), then translateZ adds to z UNROTATED.
 * `rotateBeforeTranslate` tells `projectCardCorners` which of the two to
 * use.
 */
export interface CardTransform {
  /** Left edge of the card on the 1080-wide canvas, px, pre-scale. */
  readonly x: number;
  readonly y: number;
  readonly scale: number;
  readonly opacity: number;
  readonly rotateYDeg: number;
  readonly translateZPx: number;
  readonly zIndex: number;
  readonly shadowAlpha: number;
  readonly rotateBeforeTranslate: boolean;
}

export function createCardTransform(overrides: Partial<CardTransform> = {}): CardTransform {
  return {
    x: 0,
    y: 0,
    scale: 1.0,
    opacity: 1.0,
    rotateYDeg: 0.0,
    translateZPx: 0.0,
    zIndex: 0,
    shadowAlpha: 0.0,
    rotateBeforeTranslate: false,
    ...overrides,
  };
}

/** Mirrors app/pipeline/carousel/spring.py:SpringState. */
export interface SpringState {
  readonly virtualScroll: number;
  readonly target: number;
  readonly velocity: number;
  readonly isDragging: boolean;
  // Cumulative |pointermove delta| since the current pointerdown (bundle's
  // `_.x`). Drives DRAG_ACTIVATION_PX gating in simulate()/release().
  readonly totalDragPx: number;
  // Whether the tick loop has been requested yet (bundle's `z.value`).
  readonly tickActive: boolean;
}

export function createSpringState(overrides: Partial<SpringState> = {}): SpringState {
  return {
    virtualScroll: 0.0,
    target: 0.0,
    velocity: 0.0,
    isDragging: false,
    totalDragPx: 0.0,
    tickActive: false,
    ...overrides,
  };
}

/** Mirrors app/pipeline/carousel/spring.py:SpringFrame. */
export interface SpringFrame {
  readonly tS: number;
  readonly virtualScroll: number;
  readonly velocity: number;
  readonly target: number;
}

/** Mirrors app/pipeline/carousel/gesture.py:GestureTrace. */
export interface GestureTrace {
  readonly dragDeltasPx: readonly number[];
  readonly fps: number;
}

/** Mirrors app/pipeline/carousel/choreography.py:FocusMoment. */
export interface FocusMoment {
  readonly cardIndex: number;
  readonly holdS: number;
  readonly zoomS: number; // each direction
}

export function createFocusMoment(
  cardIndex: number,
  overrides: Partial<Omit<FocusMoment, "cardIndex">> = {},
): FocusMoment {
  return { cardIndex, holdS: 2.0, zoomS: 0.6, ...overrides };
}

/** Mirrors app/pipeline/carousel/choreography.py:FrameState. */
export interface FrameState {
  readonly tS: number;
  readonly scrollX: number;
  readonly focusCard: number | null;
  readonly focusT: number; // 0 in-carousel, 1 fullscreen
  readonly dim: number; // 0..1 dim applied to non-focused cards
}

export function createFrameState(overrides: Partial<FrameState> & { tS: number; scrollX: number }): FrameState {
  return { focusCard: null, focusT: 0.0, dim: 0.0, ...overrides };
}
