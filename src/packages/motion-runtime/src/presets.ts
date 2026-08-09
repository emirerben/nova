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
