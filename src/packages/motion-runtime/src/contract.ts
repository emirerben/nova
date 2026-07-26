export const MOTION_SCHEMA_VERSION = 1 as const;
export const MOTION_FPS = 30 as const;
export const MOTION_MAX_INSTANCES = 4 as const;
export const MOTION_MAX_ACTIVE_FRAMES = 8 * MOTION_FPS;

export const CANVASKIT_VERSION = "0.40.0";
export const CANVASKIT_JS_SHA256 =
  "b2556106b80c5ff3041f3888d55e602636e1812c98cf77a72e7c328c8036c838";
export const CANVASKIT_WASM_SHA256 =
  "2abfa191f92f0aee6e0c8e3ff9612294a7721a40761216867c1c059e7993c9d3";

/**
 * Rolling-deploy compatibility token. This deliberately includes the Skia
 * payload digests: matching source code with different CanvasKit bytes is not
 * considered renderer parity.
 */
export const MOTION_RUNTIME_HASH =
  "motion-v1:ck0.40.0:b2556106:2abfa191:route-trace-v1";

export type MotionPresetId = "route_trace";

export interface MotionPalette {
  primary: string;
  accent: string;
}

/**
 * The only public motion payload. User and agent input can select immutable
 * presets, but can never submit paths, SVG, shaders, or executable scene data.
 */
export interface MotionPresetInstanceV1 {
  id: string;
  preset_id: MotionPresetId;
  preset_version: 1;
  start_frame: number;
  end_frame_exclusive: number;
  palette: MotionPalette;
  intensity: number;
}

export interface MotionValidationResult {
  ok: boolean;
  errors: string[];
}

const ID_RE = /^[A-Za-z0-9_-]{1,80}$/;
const COLOR_RE = /^#[0-9A-Fa-f]{6}$/;

export function validateMotionInstances(
  value: unknown,
  durationFrames?: number,
): MotionValidationResult {
  const errors: string[] = [];
  if (!Array.isArray(value)) {
    return { ok: false, errors: ["motion_scenes must be an array"] };
  }
  if (value.length > MOTION_MAX_INSTANCES) {
    errors.push(`motion_scenes supports at most ${MOTION_MAX_INSTANCES} instances`);
  }

  let firstFrame = Number.POSITIVE_INFINITY;
  let lastFrameExclusive = 0;
  const ids = new Set<string>();
  value.forEach((raw, index) => {
    const at = `motion_scenes.${index}`;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      errors.push(`${at} must be an object`);
      return;
    }
    const item = raw as Record<string, unknown>;
    const allowed = new Set([
      "id",
      "preset_id",
      "preset_version",
      "start_frame",
      "end_frame_exclusive",
      "palette",
      "intensity",
    ]);
    for (const key of Object.keys(item)) {
      if (!allowed.has(key)) errors.push(`${at}.${key} is not supported`);
    }

    if (typeof item.id !== "string" || !ID_RE.test(item.id)) {
      errors.push(`${at}.id must contain only letters, numbers, _ or -`);
    } else if (ids.has(item.id)) {
      errors.push(`${at}.id must be unique`);
    } else {
      ids.add(item.id);
    }
    if (item.preset_id !== "route_trace" || item.preset_version !== 1) {
      errors.push(`${at} references an unknown preset version`);
    }
    const start = item.start_frame;
    const end = item.end_frame_exclusive;
    if (!Number.isInteger(start) || (start as number) < 0) {
      errors.push(`${at}.start_frame must be a non-negative integer`);
    }
    if (!Number.isInteger(end) || (end as number) <= 0) {
      errors.push(`${at}.end_frame_exclusive must be a positive integer`);
    }
    if (Number.isInteger(start) && Number.isInteger(end)) {
      if ((end as number) <= (start as number)) {
        errors.push(`${at}.end_frame_exclusive must be greater than start_frame`);
      } else {
        firstFrame = Math.min(firstFrame, start as number);
        lastFrameExclusive = Math.max(lastFrameExclusive, end as number);
      }
      if (durationFrames !== undefined && (end as number) > durationFrames) {
        errors.push(`${at}.end_frame_exclusive exceeds the video duration`);
      }
    }
    if (
      typeof item.intensity !== "number" ||
      !Number.isFinite(item.intensity) ||
      item.intensity < 0 ||
      item.intensity > 1
    ) {
      errors.push(`${at}.intensity must be between 0 and 1`);
    }
    const palette = item.palette;
    if (!palette || typeof palette !== "object" || Array.isArray(palette)) {
      errors.push(`${at}.palette must be an object`);
    } else {
      const p = palette as Record<string, unknown>;
      if (typeof p.primary !== "string" || !COLOR_RE.test(p.primary)) {
        errors.push(`${at}.palette.primary must be #RRGGBB`);
      }
      if (typeof p.accent !== "string" || !COLOR_RE.test(p.accent)) {
        errors.push(`${at}.palette.accent must be #RRGGBB`);
      }
      for (const key of Object.keys(p)) {
        if (key !== "primary" && key !== "accent") {
          errors.push(`${at}.palette.${key} is not supported`);
        }
      }
    }
  });
  const activeSpanFrames =
    Number.isFinite(firstFrame) && lastFrameExclusive > firstFrame
      ? lastFrameExclusive - firstFrame
      : 0;
  if (activeSpanFrames > MOTION_MAX_ACTIVE_FRAMES) {
    errors.push(
      `motion_scenes spans ${activeSpanFrames} frames; maximum is ${MOTION_MAX_ACTIVE_FRAMES}`,
    );
  }
  return { ok: errors.length === 0, errors };
}

export function activeMotionInstances(
  instances: readonly MotionPresetInstanceV1[],
  frame: number,
): MotionPresetInstanceV1[] {
  return instances.filter(
    (instance) => frame >= instance.start_frame && frame < instance.end_frame_exclusive,
  );
}
