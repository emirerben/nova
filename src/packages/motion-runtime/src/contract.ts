export const MOTION_SCHEMA_VERSION = 2 as const;
export const MOTION_FPS = 30 as const;
export const MOTION_MAX_INSTANCES = 8 as const;
export const MOTION_MAX_INSTANCE_FRAMES = 8 * MOTION_FPS;
export const MOTION_MAX_ACTIVE_FRAMES = 8 * MOTION_FPS;

export const CANVASKIT_VERSION = "0.40.0";
export const CANVASKIT_JS_SHA256 =
  "b2556106b80c5ff3041f3888d55e602636e1812c98cf77a72e7c328c8036c838";
export const CANVASKIT_WASM_SHA256 =
  "2abfa191f92f0aee6e0c8e3ff9612294a7721a40761216867c1c059e7993c9d3";

export const LEGACY_MOTION_RUNTIME_HASH =
  "motion-v1:ck0.40.0:b2556106:2abfa191:route-trace-v1";
export const MOTION_RUNTIME_HASH =
  "motion-v2:ck0.40.0:b2556106:2abfa191:creator-blocks-v1";

export type CreatorBlockPresetId =
  | "kinetic_word"
  | "tag_stack"
  | "flow_field"
  | "cloud_break"
  | "offer_swap"
  | "card_stack"
  | "film_strip"
  | "donut_text";
export type MotionPresetId = "route_trace" | CreatorBlockPresetId;

export interface MotionPalette {
  primary: string;
  accent: string;
}

export interface MotionAssetRef {
  asset_id: string;
  gcs_path: string;
}

interface MotionInstanceBase<TPreset extends MotionPresetId> {
  id: string;
  preset_id: TPreset;
  preset_version: 1;
  start_frame: number;
  end_frame_exclusive: number;
  palette: MotionPalette;
  intensity: number;
}

export type RouteTraceInstanceV1 = MotionInstanceBase<"route_trace">;
export interface KineticWordParams { text: string }
export interface TagStackParams { labels: string[] }
export interface FlowFieldParams { headline: string; kicker?: string }
export interface CloudBreakParams { lines: string[] }
export interface OfferSwapParams { primary_text: string; alternate_text: string }
export interface CardStackParams { assets: MotionAssetRef[] }
export interface FilmStripParams { assets: MotionAssetRef[] }
export interface DonutTextParams { left_text: string; right_text: string }

export type KineticWordInstanceV1 = MotionInstanceBase<"kinetic_word"> & { params: KineticWordParams };
export type TagStackInstanceV1 = MotionInstanceBase<"tag_stack"> & { params: TagStackParams };
export type FlowFieldInstanceV1 = MotionInstanceBase<"flow_field"> & { params: FlowFieldParams };
export type CloudBreakInstanceV1 = MotionInstanceBase<"cloud_break"> & { params: CloudBreakParams };
export type OfferSwapInstanceV1 = MotionInstanceBase<"offer_swap"> & { params: OfferSwapParams };
export type CardStackInstanceV1 = MotionInstanceBase<"card_stack"> & { params: CardStackParams };
export type FilmStripInstanceV1 = MotionInstanceBase<"film_strip"> & { params: FilmStripParams };
export type DonutTextInstanceV1 = MotionInstanceBase<"donut_text"> & { params: DonutTextParams };

export type CreatorBlockInstanceV1 =
  | KineticWordInstanceV1
  | TagStackInstanceV1
  | FlowFieldInstanceV1
  | CloudBreakInstanceV1
  | OfferSwapInstanceV1
  | CardStackInstanceV1
  | FilmStripInstanceV1
  | DonutTextInstanceV1;

export type MotionPresetInstance = RouteTraceInstanceV1 | CreatorBlockInstanceV1;
/** Backwards-compatible import name used by the first route-trace integration. */
export type MotionPresetInstanceV1 = MotionPresetInstance;
export type MotionPresetParams = CreatorBlockInstanceV1["params"];
export interface MotionPresetPatch {
  start_frame?: number;
  end_frame_exclusive?: number;
  palette?: MotionPalette;
  intensity?: number;
  params?: MotionPresetParams;
}

export interface MotionValidationResult {
  ok: boolean;
  errors: string[];
}

const ID_RE = /^[A-Za-z0-9_-]{1,80}$/;
const COLOR_RE = /^#[0-9A-Fa-f]{6}$/;
const GCS_PATH_RE = /^(?:users|dev-user|generative-jobs|slot-uploads|music-uploads)\/[A-Za-z0-9_./-]+$/;
const PRESETS = new Set<MotionPresetId>([
  "route_trace", "kinetic_word", "tag_stack", "flow_field", "cloud_break",
  "offer_swap", "card_stack", "film_strip", "donut_text",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function textLength(value: string): number {
  return Array.from(value).length;
}

function validateText(value: unknown, at: string, min: number, max: number, errors: string[]): void {
  if (typeof value !== "string" || textLength(value.trim()) < min || textLength(value) > max) {
    errors.push(`${at} must contain ${min}-${max} Unicode characters`);
  }
}

function rejectUnknown(record: Record<string, unknown>, allowed: readonly string[], at: string, errors: string[]): void {
  const keys = new Set(allowed);
  for (const key of Object.keys(record)) {
    if (!keys.has(key)) errors.push(`${at}.${key} is not supported`);
  }
}

function validateStringList(value: unknown, at: string, minItems: number, maxItems: number, maxChars: number, errors: string[]): void {
  if (!Array.isArray(value) || value.length < minItems || value.length > maxItems) {
    errors.push(`${at} must contain ${minItems}-${maxItems} items`);
    return;
  }
  value.forEach((item, index) => validateText(item, `${at}.${index}`, 1, maxChars, errors));
}

function validateAssets(value: unknown, at: string, minItems: number, maxItems: number, errors: string[]): void {
  if (!Array.isArray(value) || value.length < minItems || value.length > maxItems) {
    errors.push(`${at} must contain ${minItems}-${maxItems} image assets`);
    return;
  }
  const ids = new Set<string>();
  value.forEach((raw, index) => {
    const itemAt = `${at}.${index}`;
    if (!isRecord(raw)) {
      errors.push(`${itemAt} must be an object`);
      return;
    }
    rejectUnknown(raw, ["asset_id", "gcs_path"], itemAt, errors);
    if (typeof raw.asset_id !== "string" || !ID_RE.test(raw.asset_id)) {
      errors.push(`${itemAt}.asset_id is invalid`);
    } else if (ids.has(raw.asset_id)) {
      errors.push(`${itemAt}.asset_id must be unique`);
    } else {
      ids.add(raw.asset_id);
    }
    if (
      typeof raw.gcs_path !== "string" ||
      raw.gcs_path.length > 900 ||
      !GCS_PATH_RE.test(raw.gcs_path)
    ) {
      errors.push(`${itemAt}.gcs_path is not an allowed storage path`);
    }
  });
}

function validateParams(preset: MotionPresetId, value: unknown, at: string, errors: string[]): void {
  if (preset === "route_trace") {
    if (value !== undefined) errors.push(`${at} is not supported for route_trace`);
    return;
  }
  if (!isRecord(value)) {
    errors.push(`${at} must be an object`);
    return;
  }
  switch (preset) {
    case "kinetic_word":
      rejectUnknown(value, ["text"], at, errors);
      validateText(value.text, `${at}.text`, 1, 32, errors);
      break;
    case "tag_stack":
      rejectUnknown(value, ["labels"], at, errors);
      validateStringList(value.labels, `${at}.labels`, 2, 6, 24, errors);
      break;
    case "flow_field":
      rejectUnknown(value, ["headline", "kicker"], at, errors);
      validateText(value.headline, `${at}.headline`, 1, 40, errors);
      if (value.kicker !== undefined) validateText(value.kicker, `${at}.kicker`, 0, 24, errors);
      break;
    case "cloud_break":
      rejectUnknown(value, ["lines"], at, errors);
      validateStringList(value.lines, `${at}.lines`, 2, 5, 28, errors);
      break;
    case "offer_swap":
      rejectUnknown(value, ["primary_text", "alternate_text"], at, errors);
      validateText(value.primary_text, `${at}.primary_text`, 1, 24, errors);
      validateText(value.alternate_text, `${at}.alternate_text`, 1, 24, errors);
      break;
    case "card_stack":
      rejectUnknown(value, ["assets"], at, errors);
      validateAssets(value.assets, `${at}.assets`, 2, 6, errors);
      break;
    case "film_strip":
      rejectUnknown(value, ["assets"], at, errors);
      validateAssets(value.assets, `${at}.assets`, 3, 8, errors);
      break;
    case "donut_text":
      rejectUnknown(value, ["left_text", "right_text"], at, errors);
      validateText(value.left_text, `${at}.left_text`, 1, 20, errors);
      validateText(value.right_text, `${at}.right_text`, 1, 20, errors);
      break;
  }
}

export function activeMotionFrameCount(instances: readonly Pick<MotionPresetInstance, "start_frame" | "end_frame_exclusive">[]): number {
  const intervals = instances
    .map((item) => [item.start_frame, item.end_frame_exclusive] as const)
    .sort((a, b) => a[0] - b[0]);
  let total = 0;
  let start = -1;
  let end = -1;
  for (const [nextStart, nextEnd] of intervals) {
    if (start < 0) {
      start = nextStart;
      end = nextEnd;
    } else if (nextStart <= end) {
      end = Math.max(end, nextEnd);
    } else {
      total += end - start;
      start = nextStart;
      end = nextEnd;
    }
  }
  return start < 0 ? 0 : total + end - start;
}

export function validateMotionInstances(value: unknown, durationFrames?: number): MotionValidationResult {
  const errors: string[] = [];
  if (!Array.isArray(value)) return { ok: false, errors: ["motion_scenes must be an array"] };
  if (value.length > MOTION_MAX_INSTANCES) errors.push(`motion_scenes supports at most ${MOTION_MAX_INSTANCES} instances`);
  const ids = new Set<string>();
  const timings: Array<Pick<MotionPresetInstance, "start_frame" | "end_frame_exclusive">> = [];
  value.forEach((raw, index) => {
    const at = `motion_scenes.${index}`;
    if (!isRecord(raw)) {
      errors.push(`${at} must be an object`);
      return;
    }
    const preset = raw.preset_id as MotionPresetId;
    const allowed = preset === "route_trace"
      ? ["id", "preset_id", "preset_version", "start_frame", "end_frame_exclusive", "palette", "intensity"]
      : ["id", "preset_id", "preset_version", "start_frame", "end_frame_exclusive", "palette", "intensity", "params"];
    rejectUnknown(raw, allowed, at, errors);
    if (typeof raw.id !== "string" || !ID_RE.test(raw.id)) errors.push(`${at}.id must contain only letters, numbers, _ or -`);
    else if (ids.has(raw.id)) errors.push(`${at}.id must be unique`);
    else ids.add(raw.id);
    if (!PRESETS.has(preset) || raw.preset_version !== 1) errors.push(`${at} references an unknown preset version`);
    const start = raw.start_frame;
    const end = raw.end_frame_exclusive;
    if (!Number.isInteger(start) || (start as number) < 0) errors.push(`${at}.start_frame must be a non-negative integer`);
    if (!Number.isInteger(end) || (end as number) <= 0) errors.push(`${at}.end_frame_exclusive must be a positive integer`);
    if (Number.isInteger(start) && Number.isInteger(end)) {
      const span = (end as number) - (start as number);
      if (span <= 0) errors.push(`${at}.end_frame_exclusive must be greater than start_frame`);
      else if (span > MOTION_MAX_INSTANCE_FRAMES) errors.push(`${at} exceeds the 8 second instance limit`);
      else timings.push({ start_frame: start as number, end_frame_exclusive: end as number });
      if (durationFrames !== undefined && (end as number) > durationFrames) errors.push(`${at}.end_frame_exclusive exceeds the video duration`);
    }
    if (typeof raw.intensity !== "number" || !Number.isFinite(raw.intensity) || raw.intensity < 0 || raw.intensity > 1) {
      errors.push(`${at}.intensity must be between 0 and 1`);
    }
    if (!isRecord(raw.palette)) errors.push(`${at}.palette must be an object`);
    else {
      rejectUnknown(raw.palette, ["primary", "accent"], `${at}.palette`, errors);
      if (typeof raw.palette.primary !== "string" || !COLOR_RE.test(raw.palette.primary)) errors.push(`${at}.palette.primary must be #RRGGBB`);
      if (typeof raw.palette.accent !== "string" || !COLOR_RE.test(raw.palette.accent)) errors.push(`${at}.palette.accent must be #RRGGBB`);
    }
    if (PRESETS.has(preset)) validateParams(preset, raw.params, `${at}.params`, errors);
  });
  const activeFrames = activeMotionFrameCount(timings);
  if (activeFrames > MOTION_MAX_ACTIVE_FRAMES) errors.push(`motion_scenes has ${activeFrames} active frames; maximum is ${MOTION_MAX_ACTIVE_FRAMES}`);
  return { ok: errors.length === 0, errors };
}

export function activeMotionInstances(instances: readonly MotionPresetInstance[], frame: number): MotionPresetInstance[] {
  return instances.filter((instance) => frame >= instance.start_frame && frame < instance.end_frame_exclusive);
}

export function isLegacyRouteTracePayload(instances: readonly MotionPresetInstance[]): boolean {
  return instances.every((instance) => instance.preset_id === "route_trace" && instance.preset_version === 1);
}
