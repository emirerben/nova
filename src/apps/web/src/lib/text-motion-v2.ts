/**
 * Text Motion v2 timing and normalization.
 *
 * This module is deliberately DOM-free. The editor, timeline, and preview all
 * consume the same normalized values, while the Python mirror in
 * app/pipeline/text_motion_v2.py is sampled by the shared parity fixtures.
 */

export const TEXT_MOTION_SPEED_MIN = 0.25;
export const TEXT_MOTION_SPEED_MAX = 4;
export const TEXT_MOTION_STAGGER_MAX_MS = 250;
export const TEXT_MOTION_BLUR_MAX_PX = 12;
export const TEXT_MOTION_TRAVEL_MAX_PX = 300;
export const TEXT_MOTION_CURSOR_BLINK_MIN_MS = 100;
export const TEXT_MOTION_CURSOR_BLINK_MAX_MS = 2000;
export const TEXT_MOTION_HOLD_CONTROL_MAX_S = 10;
export const TEXT_MOTION_HOLD_MAX_S = 3600;
export const TEXT_MOTION_EXIT_MAX_S = 2;
export const TEXT_MOTION_REVEAL_RAMP_MIN_MS = 40;
export const TEXT_MOTION_REVEAL_RAMP_MAX_MS = 400;
export const TEXT_MOTION_TIMELINE_SNAP_S = 0.1;
export const TEXT_MOTION_RENDER_FPS = 30;

export type TextMotionEasing = "linear" | "ease-out-cubic" | "ease-in-out-cubic";
export type TextMotionOrder = "forward" | "reverse" | "center-out";
export type TextMotionDirection = "none" | "up" | "down" | "left" | "right";
export type TextMotionCursorStyle = "none" | "bar" | "block" | "underscore";

/** Unknown fields are retained so an older editor never strips newer config. */
export interface TextMotionConfigV2 {
  version: 2;
  speed?: number;
  intensity?: number;
  easing?: TextMotionEasing;
  stagger_ms?: number;
  order?: TextMotionOrder;
  direction?: TextMotionDirection;
  travel_px?: number;
  overshoot?: number;
  blur_px?: number;
  cursor_style?: TextMotionCursorStyle;
  cursor_blink_ms?: number;
  hold_s?: number;
  exit_s?: number;
  reveal_ramp_ms?: number;
  [key: string]: unknown;
}

export interface NormalizedTextMotionV2 {
  version: 2;
  speed: number;
  intensity: number;
  easing: TextMotionEasing;
  stagger_ms: number;
  order: TextMotionOrder;
  direction: TextMotionDirection;
  travel_px: number;
  overshoot: number;
  blur_px: number;
  cursor_style: TextMotionCursorStyle;
  cursor_blink_ms: number;
  hold_s: number;
  exit_s: number;
  reveal_ramp_ms: number;
  [key: string]: unknown;
}

export interface TextMotionCapabilities {
  easing?: boolean;
  stagger?: boolean;
  order?: boolean;
  direction?: boolean;
  travel?: boolean;
  overshoot?: boolean;
  blur?: boolean;
  cursor?: boolean;
  hold?: boolean;
  revealRamp?: boolean;
}

const EASINGS = new Set<TextMotionEasing>(["linear", "ease-out-cubic", "ease-in-out-cubic"]);
const ORDERS = new Set<TextMotionOrder>(["forward", "reverse", "center-out"]);
const DIRECTIONS = new Set<TextMotionDirection>(["none", "up", "down", "left", "right"]);
const CURSORS = new Set<TextMotionCursorStyle>(["none", "bar", "block", "underscore"]);

function finite(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clamp(value: unknown, min: number, max: number, fallback: number): number {
  return Math.max(min, Math.min(max, finite(value, fallback)));
}

function oneOf<T extends string>(value: unknown, allowed: ReadonlySet<T>, fallback: T): T {
  return typeof value === "string" && allowed.has(value as T) ? (value as T) : fallback;
}

export function defaultTextMotion(effect: string): NormalizedTextMotionV2 {
  if (effect === "smooth-type") {
    return {
      version: 2,
      speed: 1,
      intensity: 0.7,
      easing: "ease-out-cubic",
      stagger_ms: 45,
      order: "forward",
      direction: "up",
      travel_px: 18,
      overshoot: 0,
      blur_px: 4,
      cursor_style: "none",
      cursor_blink_ms: 500,
      hold_s: 1,
      exit_s: 0,
      reveal_ramp_ms: 120,
    };
  }
  return {
    version: 2,
    speed: 1,
    intensity: 1,
    easing: "ease-out-cubic",
    stagger_ms: 0,
    order: "forward",
    direction: effect === "slide-down" ? "down" : effect === "slide-up" ? "up" : "none",
    travel_px: effect === "slide-up" || effect === "slide-down" ? 220 : 0,
    overshoot: effect === "pop-in" || effect === "bounce" ? 0.15 : 0,
    blur_px: 0,
    cursor_style: effect === "stream-in" ? "bar" : "none",
    cursor_blink_ms: 500,
    hold_s: 1,
    exit_s: effect === "dissolve-out" ? 1 : 0,
    reveal_ramp_ms: 120,
  };
}

export function normalizeTextMotion(
  effect: string,
  motion: TextMotionConfigV2 | NormalizedTextMotionV2 | null | undefined,
): NormalizedTextMotionV2 {
  const defaults = defaultTextMotion(effect);
  if (!motion || motion.version !== 2) return defaults;
  return {
    version: 2,
    speed: clamp(motion.speed, TEXT_MOTION_SPEED_MIN, TEXT_MOTION_SPEED_MAX, defaults.speed),
    intensity: clamp(motion.intensity, 0, 1, defaults.intensity),
    easing: oneOf(motion.easing, EASINGS, defaults.easing),
    stagger_ms: clamp(motion.stagger_ms, 0, TEXT_MOTION_STAGGER_MAX_MS, defaults.stagger_ms),
    order: oneOf(motion.order, ORDERS, defaults.order),
    direction: oneOf(motion.direction, DIRECTIONS, defaults.direction),
    travel_px: clamp(motion.travel_px, 0, 600, defaults.travel_px),
    overshoot: clamp(motion.overshoot, 0, 1, defaults.overshoot),
    blur_px: clamp(motion.blur_px, 0, TEXT_MOTION_BLUR_MAX_PX, defaults.blur_px),
    cursor_style: oneOf(motion.cursor_style, CURSORS, defaults.cursor_style),
    cursor_blink_ms: clamp(motion.cursor_blink_ms, 100, 2000, defaults.cursor_blink_ms),
    hold_s: clamp(motion.hold_s, 0, TEXT_MOTION_HOLD_MAX_S, defaults.hold_s),
    exit_s: clamp(motion.exit_s, 0, TEXT_MOTION_EXIT_MAX_S, defaults.exit_s),
    reveal_ramp_ms: clamp(
      motion.reveal_ramp_ms,
      TEXT_MOTION_REVEAL_RAMP_MIN_MS,
      TEXT_MOTION_REVEAL_RAMP_MAX_MS,
      defaults.reveal_ramp_ms,
    ),
  };
}

export function textMotionPreviewDurationS(
  actualDurationS: number,
  motion: TextMotionConfigV2 | null | undefined,
  enabled: boolean,
  legacyCapS: number,
): number {
  const durationS = Math.max(0.01, actualDurationS);
  return enabled && motion?.version === 2 ? durationS : Math.min(legacyCapS, durationS);
}

export function textMotionCapabilities(effect: string): TextMotionCapabilities {
  switch (effect) {
    case "smooth-type":
      return {
        easing: true,
        stagger: true,
        order: true,
        direction: true,
        travel: true,
        blur: true,
        hold: true,
        revealRamp: true,
      };
    case "slide-up":
    case "slide-down":
      return { easing: true, direction: true, travel: true, hold: true };
    case "fade-in":
    case "scale-up":
      return { easing: true, hold: true };
    case "pop-in":
    case "bounce":
      return { overshoot: true, hold: true };
    case "typewriter":
    case "stream-in":
      return { cursor: true, hold: true };
    case "staggered-slice":
      return { hold: true };
    case "ink-reveal":
    case "handwriting":
      return { easing: true, hold: true };
    default:
      return {};
  }
}

export function textMotionHasControls(effect: string): boolean {
  return Object.values(textMotionCapabilities(effect)).some(Boolean);
}

const segmenter =
  typeof Intl !== "undefined" && "Segmenter" in Intl
    ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
    : null;

export function textMotionGraphemeCount(text: string): number {
  return textMotionGraphemes(text).length;
}

export function textMotionGraphemes(text: string): string[] {
  if (segmenter) return Array.from(segmenter.segment(text), (part) => part.segment);
  return Array.from(text);
}

export function effectBaseDurationS(
  effect: string,
  text: string,
  motion?: TextMotionConfigV2 | NormalizedTextMotionV2 | null,
): number {
  const normalized = normalizeTextMotion(effect, motion);
  switch (effect) {
    case "smooth-type": {
      const count = Math.max(1, textMotionGraphemeCount(text));
      return Math.max(
        0.12,
        ((count - 1) * normalized.stagger_ms + normalized.reveal_ramp_ms) / 1000,
      );
    }
    case "typewriter":
      return Math.max(0.12, textMotionGraphemeCount(text) / 12);
    case "stream-in":
      return Math.max(0.12, (text.match(/\S+/g)?.length ?? 1) / 6);
    case "staggered-slice":
      {
        const lineCount = text.split("\n").length;
        return lineCount <= 1
          ? 1.35
          : Math.min(2.4, 1.5 + Math.max(0, lineCount - 2) * 0.12 + 0.35);
      }
    case "handwriting":
    case "ink-reveal":
      return 2.2;
    case "scale-up":
      return 0.6;
    case "bounce":
      return 0.5;
    case "fade-in":
      return 0.4;
    case "slide-up":
    case "slide-down":
      return 0.35;
    case "pop-in":
      return 0.25;
    default:
      return 0;
  }
}

export function textMotionSettleS(
  effect: string,
  text: string,
  motion?: TextMotionConfigV2 | NormalizedTextMotionV2 | null,
): number {
  const normalized = normalizeTextMotion(effect, motion);
  return effectBaseDurationS(effect, text, normalized) / normalized.speed;
}

export function textMotionDurationS(
  effect: string,
  text: string,
  motion?: TextMotionConfigV2 | NormalizedTextMotionV2 | null,
): number {
  const normalized = normalizeTextMotion(effect, motion);
  return textMotionSettleS(effect, text, normalized) + normalized.hold_s + normalized.exit_s;
}

export function snapTextMotionTime(value: number): number {
  return Math.round(value / TEXT_MOTION_TIMELINE_SNAP_S) * TEXT_MOTION_TIMELINE_SNAP_S;
}

export function roundTextMotionFrame(value: number): number {
  return Math.floor(Math.max(0, value) * TEXT_MOTION_RENDER_FPS + 0.5) / TEXT_MOTION_RENDER_FPS;
}

/** Renderer-only settle boundary. Authored duration math remains continuous. */
export function textMotionRendererSettleS(
  effect: string,
  text: string,
  motion?: TextMotionConfigV2 | NormalizedTextMotionV2 | null,
): number {
  return Math.max(
    1 / TEXT_MOTION_RENDER_FPS,
    roundTextMotionFrame(textMotionSettleS(effect, text, motion)),
  );
}

/** Map output time onto the authored curve with its settle boundary frame-snapped. */
export function authoredTextMotionTimeS(
  effect: string,
  text: string,
  tLocal: number,
  motion?: TextMotionConfigV2 | NormalizedTextMotionV2 | null,
): number {
  const normalized = normalizeTextMotion(effect, motion);
  const base = effectBaseDurationS(effect, text, normalized);
  if (base <= 0) return Math.max(0, tLocal) * normalized.speed;
  return Math.max(0, tLocal) * (base / textMotionRendererSettleS(effect, text, normalized));
}

export function easeTextMotion(progress: number, easing: TextMotionEasing): number {
  const t = Math.max(0, Math.min(1, progress));
  if (easing === "linear") return t;
  if (easing === "ease-in-out-cubic") {
    return t < 0.5 ? 4 * Math.pow(t, 3) : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }
  return 1 - Math.pow(1 - t, 3);
}

export interface SmoothTypeState {
  alpha: number;
  xTranslate: number;
  yTranslate: number;
  blurPx: number;
  revealProgress: number;
  revealOrigin: TextMotionOrder;
  settled: boolean;
}

/** Stable full-run reveal state mirrored by Python smooth_type_state_at. */
export function smoothTypeStateAt(
  text: string,
  tLocal: number,
  raw?: TextMotionConfigV2 | null,
): SmoothTypeState {
  const motion = normalizeTextMotion("smooth-type", raw);
  const count = Math.max(1, textMotionGraphemeCount(text));
  const staggerS = motion.stagger_ms / 1000;
  const rampS = motion.reveal_ramp_ms / 1000;
  const base = effectBaseDurationS("smooth-type", text, motion);
  const settleS = textMotionRendererSettleS("smooth-type", text, motion);
  const authoredT = authoredTextMotionTimeS("smooth-type", text, tLocal, motion);
  // Averaging complete cluster ramps keeps both value and velocity continuous
  // as a new cluster begins; an active-index shortcut creates a visible jump.
  let revealedClusters = 0;
  for (let index = 0; index < count; index += 1) {
    revealedClusters += easeTextMotion(
      (authoredT - index * staggerS) / Math.max(rampS, 1e-6),
      motion.easing,
    );
  }
  const revealProgress = Math.min(1, revealedClusters / count);
  const entrance = easeTextMotion(authoredT / Math.max(base, 1e-6), motion.easing);
  const remaining = 1 - entrance;
  const distance = motion.travel_px * motion.intensity * remaining;
  const xTranslate = distance === 0 ? 0 : motion.direction === "left" ? -distance : motion.direction === "right" ? distance : 0;
  const yTranslate = distance === 0 ? 0 : motion.direction === "up" ? -distance : motion.direction === "down" ? distance : 0;
  const alpha = 1 - motion.intensity * (1 - entrance);
  return {
    alpha: Math.max(0, Math.min(1, alpha)),
    xTranslate,
    yTranslate,
    blurPx: motion.blur_px * motion.intensity * remaining,
    revealProgress,
    revealOrigin: motion.order,
    settled: Math.max(0, tLocal) + 1e-9 >= settleS,
  };
}

export function smoothTypeLineProgresses(
  lines: string[],
  tLocal: number,
  raw?: TextMotionConfigV2 | null,
): number[] {
  const motion = normalizeTextMotion("smooth-type", raw);
  const clustersByLine = lines.map(textMotionGraphemes);
  // Each visual-line boundary replaces one source separator (an authored
  // newline or the space consumed by wrapping). Keep that invisible cluster
  // in the global schedule so line masks settle on the same output frame as
  // the complete-run evaluator.
  const separatorCount = Math.max(0, lines.length - 1);
  const total = Math.max(
    1,
    clustersByLine.reduce((sum, clusters) => sum + clusters.length, 0) + separatorCount,
  );
  const authoredT = authoredTextMotionTimeS("smooth-type", lines.join("\n"), tLocal, raw);
  const staggerS = motion.stagger_ms / 1000;
  const rampS = Math.max(motion.reveal_ramp_ms / 1000, 1e-6);
  const rankedIndices = Array.from({ length: total }, (_, index) => index);
  if (motion.order === "center-out") {
    rankedIndices.sort((a, b) => Math.abs(a - (total - 1) / 2) - Math.abs(b - (total - 1) / 2) || a - b);
  } else if (motion.order === "reverse") {
    rankedIndices.reverse();
  }
  const ranks = new Map(rankedIndices.map((index, rank) => [index, rank]));
  let offset = 0;
  return clustersByLine.map((clusters, lineIndex) => {
    if (clusters.length === 0) {
      offset += lineIndex < clustersByLine.length - 1 ? 1 : 0;
      return 1;
    }
    const progress = clusters.reduce(
      (sum, _cluster, index) =>
        sum + easeTextMotion((authoredT - (ranks.get(offset + index) ?? 0) * staggerS) / rampS, motion.easing),
      0,
    ) / clusters.length;
    offset += clusters.length + (lineIndex < clustersByLine.length - 1 ? 1 : 0);
    return Math.max(0, Math.min(1, progress));
  });
}

export interface MotionTimedText {
  text: string;
  start_s: number;
  end_s: number;
  effect?: string | null;
  motion?: TextMotionConfigV2 | null;
  reveal_s?: number | null;
}

export function motionPatchForEffect(
  element: MotionTimedText,
  effect: string,
  videoDurationS: number,
): { effect: string; motion: TextMotionConfigV2 | null; end_s: number; reveal_s: null } {
  if (effect === "none" || effect === "static" || !textMotionHasControls(effect)) {
    return { effect, motion: null, end_s: element.end_s, reveal_s: null };
  }
  const motion: TextMotionConfigV2 = { ...defaultTextMotion(effect) };
  const end = Math.min(
    videoDurationS,
    element.start_s + textMotionDurationS(effect, element.text, motion),
  );
  return {
    effect,
    motion,
    reveal_s: null,
    end_s: Math.min(
      videoDurationS,
      snapTextMotionTime(Math.max(element.start_s + 0.1, end)),
    ),
  };
}

export function motionPatchForConfig(
  element: MotionTimedText,
  patch: Partial<TextMotionConfigV2>,
  videoDurationS: number,
): { motion: TextMotionConfigV2; end_s: number } {
  const effect = element.effect ?? "static";
  const existing = element.motion?.version === 2 ? element.motion : defaultTextMotion(effect);
  const motion: TextMotionConfigV2 = { ...existing, ...patch, version: 2 };
  const affectsDuration = ["speed", "stagger_ms", "hold_s", "exit_s", "reveal_ramp_ms"].some(
    (field) => Object.prototype.hasOwnProperty.call(patch, field),
  );
  if (!affectsDuration) return { motion, end_s: element.end_s };
  const end = Math.min(
    videoDurationS,
    element.start_s + textMotionDurationS(effect, element.text, motion),
  );
  return {
    motion,
    end_s: Math.min(
      videoDurationS,
      snapTextMotionTime(Math.max(element.start_s + 0.1, end)),
    ),
  };
}

export function motionPatchForText(
  element: MotionTimedText,
  text: string,
  videoDurationS: number,
): { text: string; end_s?: number } {
  if (!element.motion || element.motion.version !== 2) return { text };
  const effect = element.effect ?? "static";
  const end = Math.min(
    videoDurationS,
    element.start_s + textMotionDurationS(effect, text, element.motion),
  );
  return {
    text,
    end_s: Math.min(
      videoDurationS,
      snapTextMotionTime(Math.max(element.start_s + 0.1, end)),
    ),
  };
}

/** Manual trims consume/extend hold first, then accelerate settle up to 4×. */
export function motionPatchForManualEnd(
  element: MotionTimedText,
  requestedEndS: number,
  videoDurationS = Number.POSITIVE_INFINITY,
): { end_s: number; motion?: TextMotionConfigV2 } {
  if (!element.motion || element.motion.version !== 2) return { end_s: requestedEndS };
  const effect = element.effect ?? "static";
  const current = normalizeTextMotion(effect, element.motion);
  const base = effectBaseDurationS(effect, element.text, current);
  const minimumAvailable = base / TEXT_MOTION_SPEED_MAX + current.exit_s;
  const minimumEndS = Math.ceil((element.start_s + minimumAvailable) * 10 - 1e-9) / 10;
  const endS = Math.min(videoDurationS, Math.max(requestedEndS, minimumEndS));
  const available = Math.max(1 / TEXT_MOTION_RENDER_FPS, endS - element.start_s);
  const exit = Math.min(current.exit_s, available);
  const settleAtCurrentSpeed = base / current.speed;
  const roomBeforeExit = Math.max(1 / TEXT_MOTION_RENDER_FPS, available - exit);
  let speed = current.speed;
  let hold = Math.max(0, roomBeforeExit - settleAtCurrentSpeed);
  if (roomBeforeExit < settleAtCurrentSpeed) {
    speed = Math.min(TEXT_MOTION_SPEED_MAX, Math.max(current.speed, base / roomBeforeExit));
    hold = Math.max(0, roomBeforeExit - base / speed);
  }
  return {
    end_s: endS,
    motion: { ...element.motion, version: 2, speed, hold_s: hold },
  };
}
