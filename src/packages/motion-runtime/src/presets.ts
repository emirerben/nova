import type { MotionPresetInstanceV1 } from "./contract.ts";

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

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

/** Stable cubic smoothstep. No browser easing or clock APIs participate. */
export function smoothstep(value: number): number {
  const t = clamp01(value);
  return t * t * (3 - 2 * t);
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
