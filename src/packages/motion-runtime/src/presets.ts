import type { MotionPresetInstance, MotionPresetInstanceV1 } from "./contract.ts";

export interface RouteTraceFrame {
  progress: number;
  opacity: number;
  scale: number;
  strokeWidth: number;
  primary: string;
  accent: string;
}

/**
 * Trusted, build-owned SVG geometry. It is intentionally a path string rather
 * than runtime SVG markup: scripts, external references, SMIL, fonts, filters,
 * and foreignObject can never enter the renderer.
 */
export const ROUTE_TRACE_PATH =
  "M 92 1496 C 212 1380 288 1346 386 1240 C 500 1118 444 996 562 884 C 676 776 824 824 898 690 C 982 538 846 420 956 268";

export const ROUTE_TRACE_VIEWBOX = Object.freeze({
  width: 1080,
  height: 1920,
});

export function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

/** Stable cubic smoothstep. No browser easing or clock APIs participate. */
export function smoothstep(value: number): number {
  const t = clamp01(value);
  return t * t * (3 - 2 * t);
}

export function easeOutCubic(value: number): number {
  const t = clamp01(value);
  return 1 - (1 - t) ** 3;
}

export function easeInOutCubic(value: number): number {
  const t = clamp01(value);
  return t < 0.5 ? 4 * t ** 3 : 1 - ((-2 * t + 2) ** 3) / 2;
}

export interface CreatorBlockFrame {
  local: number;
  enter: number;
  exit: number;
  opacity: number;
  scale: number;
  rotation: number;
  pulse: number;
  cycle: number;
}

/**
 * Deterministic keyframe state shared by browser and export. The 18/76/24%
 * beats are Motion-style keyframe times, sampled locally instead of relying
 * on a browser animation clock.
 */
export function creatorBlockFrame(
  instance: MotionPresetInstance,
  frame: number,
): CreatorBlockFrame {
  const duration = Math.max(1, instance.end_frame_exclusive - instance.start_frame);
  const local = clamp01((frame - instance.start_frame) / Math.max(1, duration - 1));
  const enter = easeOutCubic(local / 0.18);
  const exit = smoothstep((local - 0.76) / 0.24);
  const intensity = clamp01(instance.intensity);
  return {
    local,
    enter,
    exit,
    opacity: enter * (1 - exit),
    scale: (0.72 + 0.28 * enter) * (1 - exit * (0.1 + intensity * 0.12)),
    rotation: (1 - enter) * (-12 * intensity) + exit * (8 * intensity),
    pulse: 0.5 + 0.5 * Math.sin(local * Math.PI * 4),
    cycle: (local * (1.4 + intensity * 0.8)) % 1,
  };
}

export function routeTraceFrame(
  instance: MotionPresetInstanceV1,
  frame: number,
): RouteTraceFrame {
  const duration = instance.end_frame_exclusive - instance.start_frame;
  const local = clamp01((frame - instance.start_frame) / Math.max(1, duration - 1));
  const draw = smoothstep(clamp01(local / 0.76));
  const fadeOut = 1 - smoothstep(clamp01((local - 0.84) / 0.16));
  const intensity = clamp01(instance.intensity);
  return {
    progress: draw,
    opacity: fadeOut * (0.45 + intensity * 0.55),
    scale: 0.985 + smoothstep(local) * 0.015,
    strokeWidth: 8 + intensity * 10,
    primary: instance.palette.primary.toUpperCase(),
    accent: instance.palette.accent.toUpperCase(),
  };
}

export type MotionEasingName = "ease-out-cubic" | "ease-in-out-cubic";

export interface MotionV2TimingDefinition {
  base_choreography_frames: number;
  fixed_exit_frames: number;
}

export interface MotionV2Like {
  start_frame: number;
  end_frame_exclusive: number;
  intensity: number;
  motion?: {
    version?: 2;
    speed?: number;
    easing?: MotionEasingName;
    hold_frames?: number;
  };
}

export type MotionPhase = "enter" | "choreography" | "hold" | "exit";

export interface MotionV2Frame extends CreatorBlockFrame {
  /** Continuous authored-frame position before output retiming. */
  authoredFrame: number;
  choreography: number;
  hold: number;
  phase: MotionPhase;
  choreographyFrames: number;
  holdFrames: number;
  exitFrames: number;
  /** The only allowed v2 hard cut. */
  choreographyEvent: "offer-swap" | null;
}

export function evaluateMotionEasing(name: MotionEasingName, value: number): number {
  if (name === "ease-out-cubic") return easeOutCubic(value);
  return easeInOutCubic(value);
}

/**
 * Deterministic preset-v2 output timeline. Catalog timing is authored at 30fps;
 * speed retimes choreography only; hold and the fixed exit remain output-frame phases.
 * Every phase boundary lands on an integer output frame.
 */
export function creatorBlockFrameV2(
  instance: MotionV2Like,
  frame: number,
  timing: MotionV2TimingDefinition,
  options: { offerSwapEvent?: boolean } = {},
): MotionV2Frame {
  const speed = Math.max(0.25, Math.min(4, instance.motion?.speed ?? 1));
  const easing = instance.motion?.easing ?? "ease-in-out-cubic";
  const span = Math.max(1, instance.end_frame_exclusive - instance.start_frame);
  const requestedChoreography = Math.max(1, Math.round(timing.base_choreography_frames / speed));
  const requestedExit = Math.max(1, Math.round(timing.fixed_exit_frames));
  const requestedHold = Math.max(0, Math.round(instance.motion?.hold_frames ?? 0));
  const exitFrames = Math.min(requestedExit, Math.max(1, span - 1));
  const beforeExit = Math.max(1, span - exitFrames);
  const choreographyFrames = Math.min(requestedChoreography, beforeExit);
  const holdFrames = Math.min(requestedHold, Math.max(0, beforeExit - choreographyFrames));
  const localFrame = Math.max(0, Math.min(span - 1, frame - instance.start_frame));
  const choreographyRaw = clamp01(localFrame / Math.max(1, choreographyFrames - 1));
  const choreography = evaluateMotionEasing(easing, choreographyRaw);
  const enterWindow = Math.max(1, Math.min(choreographyFrames, Math.round(choreographyFrames * 0.28)));
  const degenerateStatic = span <= 2;
  const enter = degenerateStatic
    ? 1
    : evaluateMotionEasing(easing, localFrame / Math.max(1, enterWindow - 1));
  const exitStart = span - exitFrames;
  const exit = degenerateStatic
    ? 0
    : localFrame < exitStart
    ? 0
    : easeInOutCubic((localFrame - exitStart) / Math.max(1, exitFrames - 1));
  const holdStart = choreographyFrames;
  const hold = holdFrames === 0
    ? 0
    : clamp01((localFrame - holdStart) / Math.max(1, holdFrames));
  const phase: MotionPhase = degenerateStatic
    ? "hold"
    : localFrame < enterWindow
    ? "enter"
    : localFrame < choreographyFrames
      ? "choreography"
      : localFrame < exitStart
        ? "hold"
        : "exit";
  const intensity = clamp01(instance.intensity);
  const authoredFrame = choreography * Math.max(0, timing.base_choreography_frames - 1);
  return {
    local: clamp01(localFrame / Math.max(1, span - 1)),
    enter,
    exit,
    opacity: enter * (1 - exit),
    scale: (0.94 + 0.06 * enter) * (1 - exit * (0.04 + intensity * 0.06)),
    rotation: (1 - enter) * (-5 * intensity) + exit * (3 * intensity),
    pulse: 0.5 - 0.5 * Math.cos(choreography * Math.PI * 2),
    cycle: choreography,
    authoredFrame,
    choreography,
    hold,
    phase,
    choreographyFrames,
    holdFrames,
    exitFrames,
    choreographyEvent:
      options.offerSwapEvent && authoredFrame >= timing.base_choreography_frames * 0.48
        ? "offer-swap"
        : null,
  };
}

export function staggerProgress(
  authoredFrame: number,
  index: number,
  count: number,
  staggerFrames: number,
  rampFrames: number,
  order: "forward" | "reverse" | "center-out" = "forward",
): number {
  const safeCount = Math.max(1, count);
  const rank = staggerOrderRank(index, safeCount, order);
  return smootherstep((authoredFrame - rank * staggerFrames) / Math.max(1, rampFrames));
}

export function staggerOrderRank(
  index: number,
  count: number,
  order: "forward" | "reverse" | "center-out" = "forward",
): number {
  const safeCount = Math.max(1, count);
  return order === "reverse"
    ? safeCount - 1 - index
    : order === "center-out"
      ? Math.abs(index - (safeCount - 1) / 2) * 2
      : index;
}

/** Quintic phase curve with zero velocity and acceleration at both joins. */
export function smootherstep(value: number): number {
  const t = clamp01(value);
  return t * t * t * (t * (t * 6 - 15) + 10);
}

export interface ContinuousCardPose {
  angle: number;
  depth: number;
  x: number;
}

/** Continuous replacement for v1's floor(cycle * count) active-card snap. */
export function continuousCardPose(
  choreography: number,
  index: number,
  count: number,
): ContinuousCardPose {
  const safeCount = Math.max(1, count);
  const angle = index / safeCount * Math.PI * 2 - clamp01(choreography) * Math.PI * 2;
  return {
    angle,
    depth: (1 - Math.cos(angle)) / 2,
    x: Math.sin(angle),
  };
}
