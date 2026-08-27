import catalogJson from "../creator-blocks.catalog.json" with { type: "json" };
import {
  MOTION_MAX_ACTIVE_FRAMES,
  MOTION_MAX_COMPLEXITY_MULTIPLIER,
  MOTION_FPS,
  MOTION_MAX_INSTANCE_FRAMES,
  MOTION_MAX_INSTANCES,
  MOTION_MAX_WEIGHTED_ACTIVE_FRAMES,
} from "./limits.ts";

export const MOTION_SCHEMA_VERSION = 3 as const;
/** Weighted frame-work budget; maximum-cost scenes may occupy the full 12s union. */
export { MOTION_MAX_ACTIVE_FRAMES, MOTION_MAX_COMPLEXITY_MULTIPLIER,
  MOTION_FPS, MOTION_MAX_INSTANCE_FRAMES, MOTION_MAX_INSTANCES,
  MOTION_MAX_WEIGHTED_ACTIVE_FRAMES };

export const CANVASKIT_VERSION = "0.40.0";
export const CANVASKIT_JS_SHA256 =
  "b2556106b80c5ff3041f3888d55e602636e1812c98cf77a72e7c328c8036c838";
export const CANVASKIT_WASM_SHA256 =
  "2abfa191f92f0aee6e0c8e3ff9612294a7721a40761216867c1c059e7993c9d3";

export const ROUTE_TRACE_RUNTIME_HASH_V1 =
  "motion-v1:ck0.40.0:b2556106:2abfa191:route-trace-v1";
export const CREATOR_MOTION_RUNTIME_HASH_V2 =
  "motion-v2:ck0.40.0:b2556106:2abfa191:creator-blocks-v1";
export const CREATOR_MOTION_RUNTIME_HASH_V3 =
  "motion-v3:ck0.40.0:b2556106:2abfa191:creator-blocks-v2";
export const CREATOR_MOTION_RUNTIME_HASH_V4 =
  "motion-v4:ck0.40.0:b2556106:2abfa191:creator-blocks-v3";
export const MOTION_RUNTIME_HASH =
  "motion-v5:ck0.40.0:b2556106:2abfa191:creator-blocks-v4-capacity";

/** Compatibility aliases retained for clients compiled against prior runtimes. */
export const LEGACY_MOTION_RUNTIME_HASH = ROUTE_TRACE_RUNTIME_HASH_V1;
export const PREVIOUS_MOTION_RUNTIME_HASH = CREATOR_MOTION_RUNTIME_HASH_V4;
export const COMPATIBLE_CREATOR_MOTION_RUNTIME_HASHES = Object.freeze([
  CREATOR_MOTION_RUNTIME_HASH_V2,
  CREATOR_MOTION_RUNTIME_HASH_V3,
  CREATOR_MOTION_RUNTIME_HASH_V4,
  MOTION_RUNTIME_HASH,
] as const);

export function isCompatibleCreatorMotionRuntimeHash(value: unknown): boolean {
  return typeof value === "string" &&
    (COMPATIBLE_CREATOR_MOTION_RUNTIME_HASHES as readonly string[]).includes(value);
}

export function isCompatiblePersistedMotionRuntimeHash(
  value: unknown,
  routeTraceOnly = false,
): boolean {
  return isCompatibleCreatorMotionRuntimeHash(value) ||
    (routeTraceOnly && value === ROUTE_TRACE_RUNTIME_HASH_V1);
}

export type CreatorBlockPresetId =
  | "kinetic_word"
  | "tag_stack"
  | "flow_field"
  | "cloud_break"
  | "offer_swap"
  | "card_stack"
  | "film_strip"
  | "donut_text"
  | "evolving_type";
export type MotionPresetId = "route_trace" | CreatorBlockPresetId;
export type CreatorBlockPresetVersion = 1 | 2;
export type CreatorBlockEasing = "ease-out-cubic" | "ease-in-out-cubic";

export interface MotionPalette {
  primary: string;
  accent: string;
}

export interface MotionAssetRef {
  asset_id: string;
  gcs_path: string;
}

export interface CreatorBlockMotionConfigV2 {
  version: 2;
  speed: number;
  easing: CreatorBlockEasing;
  hold_frames: number;
}

interface MotionInstanceBase<
  TPreset extends MotionPresetId,
  TVersion extends CreatorBlockPresetVersion,
> {
  id: string;
  preset_id: TPreset;
  preset_version: TVersion;
  start_frame: number;
  end_frame_exclusive: number;
  palette: MotionPalette;
  /** Kept at the instance root for v1 wire compatibility. */
  intensity: number;
}

export type RouteTraceInstanceV1 = MotionInstanceBase<"route_trace", 1>;
export interface KineticWordParams { text: string }
export interface TagStackParams { labels: string[] }
export interface FlowFieldParams { headline: string; kicker?: string }
export interface CloudBreakParams { lines: string[] }
export interface OfferSwapParams { primary_text: string; alternate_text: string }
export interface CardStackParams { assets: MotionAssetRef[] }
export interface FilmStripParams { assets: MotionAssetRef[] }
export interface DonutTextParams { left_text: string; right_text: string }
export interface EvolvingTypeParams {
  headline: string;
  subtitle: string;
  icon_count: number;
  icon_style: "organic" | "geometric" | "botanical";
  text_stagger_ms: number;
  icon_stagger_ms: number;
  morph_amplitude: number;
  density: "low" | "medium" | "high";
  layout: "compact" | "spread";
  order: "forward" | "reverse" | "center-out";
  typography_scale: number;
  backdrop_opacity: number;
  split_icons: boolean;
}

type CreatorBlockInstance<
  TPreset extends CreatorBlockPresetId,
  TVersion extends CreatorBlockPresetVersion,
  TParams,
> = MotionInstanceBase<TPreset, TVersion> & {
  params: TParams;
} & (TVersion extends 2 ? { motion: CreatorBlockMotionConfigV2 } : Record<never, never>);

export type KineticWordInstanceV1 = CreatorBlockInstance<"kinetic_word", 1, KineticWordParams>;
export type TagStackInstanceV1 = CreatorBlockInstance<"tag_stack", 1, TagStackParams>;
export type FlowFieldInstanceV1 = CreatorBlockInstance<"flow_field", 1, FlowFieldParams>;
export type CloudBreakInstanceV1 = CreatorBlockInstance<"cloud_break", 1, CloudBreakParams>;
export type OfferSwapInstanceV1 = CreatorBlockInstance<"offer_swap", 1, OfferSwapParams>;
export type CardStackInstanceV1 = CreatorBlockInstance<"card_stack", 1, CardStackParams>;
export type FilmStripInstanceV1 = CreatorBlockInstance<"film_strip", 1, FilmStripParams>;
export type DonutTextInstanceV1 = CreatorBlockInstance<"donut_text", 1, DonutTextParams>;

export type CreatorBlockInstanceV1 =
  | KineticWordInstanceV1
  | TagStackInstanceV1
  | FlowFieldInstanceV1
  | CloudBreakInstanceV1
  | OfferSwapInstanceV1
  | CardStackInstanceV1
  | FilmStripInstanceV1
  | DonutTextInstanceV1;

export type KineticWordInstanceV2 = CreatorBlockInstance<"kinetic_word", 2, KineticWordParams>;
export type TagStackInstanceV2 = CreatorBlockInstance<"tag_stack", 2, TagStackParams>;
export type FlowFieldInstanceV2 = CreatorBlockInstance<"flow_field", 2, FlowFieldParams>;
export type CloudBreakInstanceV2 = CreatorBlockInstance<"cloud_break", 2, CloudBreakParams>;
export type OfferSwapInstanceV2 = CreatorBlockInstance<"offer_swap", 2, OfferSwapParams>;
export type CardStackInstanceV2 = CreatorBlockInstance<"card_stack", 2, CardStackParams>;
export type FilmStripInstanceV2 = CreatorBlockInstance<"film_strip", 2, FilmStripParams>;
export type DonutTextInstanceV2 = CreatorBlockInstance<"donut_text", 2, DonutTextParams>;
export type EvolvingTypeInstanceV2 = CreatorBlockInstance<"evolving_type", 2, EvolvingTypeParams>;

export type CreatorBlockInstanceV2 =
  | KineticWordInstanceV2
  | TagStackInstanceV2
  | FlowFieldInstanceV2
  | CloudBreakInstanceV2
  | OfferSwapInstanceV2
  | CardStackInstanceV2
  | FilmStripInstanceV2
  | DonutTextInstanceV2
  | EvolvingTypeInstanceV2;

export type CreatorBlockInstanceAny = CreatorBlockInstanceV1 | CreatorBlockInstanceV2;
export type MotionPresetInstance = RouteTraceInstanceV1 | CreatorBlockInstanceAny;
/** Backwards-compatible import name used throughout the first runtime integration. */
export type MotionPresetInstanceV1 = MotionPresetInstance;
export type MotionPresetParams = CreatorBlockInstanceAny["params"];
export interface MotionPresetPatch {
  start_frame?: number;
  end_frame_exclusive?: number;
  palette?: MotionPalette;
  intensity?: number;
  motion?: CreatorBlockMotionConfigV2;
  params?: MotionPresetParams;
}

export type CreatorBlockParameterType =
  | "string"
  | "string_list"
  | "asset_list"
  | "number"
  | "enum"
  | "boolean";

export interface CreatorBlockParameterDefinition {
  key: string;
  type: CreatorBlockParameterType;
  required: boolean;
  min_length?: number;
  max_length?: number;
  min_items?: number;
  max_items?: number;
  minimum?: number;
  maximum?: number;
  step?: number;
  integer?: boolean;
  values?: string[];
  since_version?: CreatorBlockPresetVersion;
}

interface CreatorBlockControlDefinition extends CreatorBlockParameterDefinition {
  default: unknown;
  storage: "instance" | "motion";
}

interface RuntimeCatalogEntry {
  preset_id: CreatorBlockPresetId;
  preset_version: 2;
  legacy_versions: 1[];
  complexity_weight: number;
  supported_controls: string[];
  control_overrides: Record<string, Partial<CreatorBlockControlDefinition>>;
  parameters: CreatorBlockParameterDefinition[];
}

interface RuntimeCatalog {
  control_definitions: CreatorBlockControlDefinition[];
  presets: RuntimeCatalogEntry[];
}

const runtimeCatalog = catalogJson as unknown as RuntimeCatalog;
const runtimeEntries = new Map(
  runtimeCatalog.presets.map((entry) => [entry.preset_id, entry]),
);
const runtimeControls = new Map(
  runtimeCatalog.control_definitions.map((control) => [control.key, control]),
);

export interface MotionValidationResult {
  ok: boolean;
  errors: string[];
}

const ID_RE = /^[A-Za-z0-9_-]{1,80}$/;
const COLOR_RE = /^#[0-9A-Fa-f]{6}$/;
const GCS_PATH_RE = /^(?:users|dev-user|generative-jobs|slot-uploads|music-uploads)\/[A-Za-z0-9_./-]+$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function textLength(value: string): number {
  return Array.from(value).length;
}

function rejectUnknown(
  record: Record<string, unknown>,
  allowed: readonly string[],
  at: string,
  errors: string[],
): void {
  const keys = new Set(allowed);
  for (const key of Object.keys(record)) {
    if (!keys.has(key)) errors.push(`${at}.${key} is not supported`);
  }
}

function effectiveControl(
  entry: RuntimeCatalogEntry,
  key: string,
): CreatorBlockControlDefinition | undefined {
  const base = runtimeControls.get(key);
  if (!base || !entry.supported_controls.includes(key)) return undefined;
  return { ...base, ...(entry.control_overrides[key] ?? {}) };
}

function validateDefinition(
  definition: CreatorBlockParameterDefinition,
  value: unknown,
  at: string,
  errors: string[],
): void {
  if (definition.type === "string") {
    const min = definition.min_length ?? 0;
    const max = definition.max_length ?? Number.MAX_SAFE_INTEGER;
    if (
      typeof value !== "string" ||
      textLength(value.trim()) < min ||
      textLength(value) > max
    ) {
      errors.push(`${at} must contain ${min}-${max} Unicode characters`);
    }
    return;
  }
  if (definition.type === "string_list") {
    if (
      !Array.isArray(value) ||
      value.length < (definition.min_items ?? 0) ||
      value.length > (definition.max_items ?? Number.MAX_SAFE_INTEGER)
    ) {
      errors.push(
        `${at} must contain ${definition.min_items ?? 0}-${definition.max_items ?? "bounded"} items`,
      );
      return;
    }
    value.forEach((item, index) => validateDefinition({
      key: `${definition.key}.${index}`,
      type: "string",
      required: true,
      min_length: 1,
      max_length: definition.max_length,
    }, item, `${at}.${index}`, errors));
    return;
  }
  if (definition.type === "asset_list") {
    validateAssets(
      value,
      at,
      definition.min_items ?? 0,
      definition.max_items ?? Number.MAX_SAFE_INTEGER,
      errors,
    );
    return;
  }
  if (definition.type === "number") {
    const step = definition.step;
    const minimum = definition.minimum ?? 0;
    if (
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      (definition.integer === true && !Number.isInteger(value)) ||
      (definition.minimum !== undefined && value < definition.minimum) ||
      (definition.maximum !== undefined && value > definition.maximum) ||
      (step !== undefined &&
        Math.abs(Math.round((value - minimum) / step) * step + minimum - value) > 1e-8)
    ) {
      errors.push(
        `${at} must be ${definition.integer ? "an integer" : "a number"} between ` +
        `${definition.minimum ?? "-Infinity"} and ${definition.maximum ?? "Infinity"}`,
      );
    }
    return;
  }
  if (definition.type === "enum") {
    if (typeof value !== "string" || !definition.values?.includes(value)) {
      errors.push(`${at} must be one of ${(definition.values ?? []).join(", ")}`);
    }
    return;
  }
  if (typeof value !== "boolean") errors.push(`${at} must be a boolean`);
}

function validateAssets(
  value: unknown,
  at: string,
  minItems: number,
  maxItems: number,
  errors: string[],
): void {
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

function validateCreatorParams(
  entry: RuntimeCatalogEntry,
  presetVersion: CreatorBlockPresetVersion,
  value: unknown,
  at: string,
  errors: string[],
): void {
  if (!isRecord(value)) {
    errors.push(`${at} must be an object`);
    return;
  }
  const parameters = entry.parameters.filter(
    (parameter) => (parameter.since_version ?? 1) <= presetVersion,
  );
  rejectUnknown(value, parameters.map((parameter) => parameter.key), at, errors);
  for (const parameter of parameters) {
    const parameterValue = value[parameter.key];
    if (parameterValue === undefined && !parameter.required) continue;
    if (parameterValue === undefined) {
      errors.push(`${at}.${parameter.key} is required`);
      continue;
    }
    validateDefinition(parameter, parameterValue, `${at}.${parameter.key}`, errors);
  }
}

function validateV2Motion(
  entry: RuntimeCatalogEntry,
  value: unknown,
  at: string,
  errors: string[],
): void {
  if (!isRecord(value)) {
    errors.push(`${at} must be an object`);
    return;
  }
  const controls = entry.supported_controls
    .map((key) => effectiveControl(entry, key))
    .filter((control): control is CreatorBlockControlDefinition =>
      control !== undefined && control.storage === "motion");
  rejectUnknown(value, ["version", ...controls.map((control) => control.key)], at, errors);
  if (value.version !== 2) errors.push(`${at}.version must be 2`);
  for (const control of controls) {
    if (value[control.key] === undefined && control.required) {
      errors.push(`${at}.${control.key} is required`);
    } else if (value[control.key] !== undefined) {
      validateDefinition(control, value[control.key], `${at}.${control.key}`, errors);
    }
  }
}

export function activeMotionFrameCount(
  instances: readonly Pick<MotionPresetInstance, "start_frame" | "end_frame_exclusive">[],
): number {
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

export function activeMotionComplexity(
  instances: readonly Pick<
    MotionPresetInstance,
    "preset_id" | "preset_version" | "start_frame" | "end_frame_exclusive"
  >[],
): number {
  const deltas = new Map<number, number>();
  for (const instance of instances) {
    const weight = instance.preset_version !== 2 || instance.preset_id === "route_trace"
      ? 1
      : (runtimeEntries.get(instance.preset_id)?.complexity_weight ?? MOTION_MAX_INSTANCES + 1);
    deltas.set(instance.start_frame, (deltas.get(instance.start_frame) ?? 0) + weight);
    deltas.set(instance.end_frame_exclusive, (deltas.get(instance.end_frame_exclusive) ?? 0) - weight);
  }
  let activeWeight = 0;
  let previousFrame: number | null = null;
  let weightedFrames = 0;
  for (const [frame, delta] of [...deltas.entries()].sort((a, b) => a[0] - b[0])) {
    if (previousFrame !== null) weightedFrames += (frame - previousFrame) * activeWeight;
    activeWeight += delta;
    previousFrame = frame;
  }
  return weightedFrames;
}

export function validateMotionInstances(value: unknown, durationFrames?: number): MotionValidationResult {
  const errors: string[] = [];
  if (!Array.isArray(value)) return { ok: false, errors: ["motion_scenes must be an array"] };
  if (value.length > MOTION_MAX_INSTANCES) {
    errors.push(`motion_scenes supports at most ${MOTION_MAX_INSTANCES} instances`);
  }
  const ids = new Set<string>();
  const timings: MotionPresetInstance[] = [];
  value.forEach((raw, index) => {
    const at = `motion_scenes.${index}`;
    if (!isRecord(raw)) {
      errors.push(`${at} must be an object`);
      return;
    }
    const preset = raw.preset_id as MotionPresetId;
    const version = raw.preset_version as CreatorBlockPresetVersion;
    const entry = preset === "route_trace" ? undefined : runtimeEntries.get(preset as CreatorBlockPresetId);
    const routeTrace = preset === "route_trace" && version === 1;
    const creatorVersion = !!entry && (
      version === entry.preset_version || entry.legacy_versions.includes(version as 1)
    );
    const allowed = routeTrace
      ? ["id", "preset_id", "preset_version", "start_frame", "end_frame_exclusive", "palette", "intensity"]
      : [
          "id", "preset_id", "preset_version", "start_frame", "end_frame_exclusive",
          "palette", "intensity", "params", ...(version === 2 ? ["motion"] : []),
        ];
    rejectUnknown(raw, allowed, at, errors);
    if (typeof raw.id !== "string" || !ID_RE.test(raw.id)) {
      errors.push(`${at}.id must contain only letters, numbers, _ or -`);
    } else if (ids.has(raw.id)) {
      errors.push(`${at}.id must be unique`);
    } else {
      ids.add(raw.id);
    }
    if (!routeTrace && !creatorVersion) errors.push(`${at} references an unknown preset version`);
    const start = raw.start_frame;
    const end = raw.end_frame_exclusive;
    if (!Number.isInteger(start) || (start as number) < 0) {
      errors.push(`${at}.start_frame must be a non-negative integer`);
    } else if ((start as number) >= 60 * MOTION_FPS) {
      errors.push(`${at}.start_frame exceeds the 60 second motion timeline`);
    }
    if (!Number.isInteger(end) || (end as number) <= 0) {
      errors.push(`${at}.end_frame_exclusive must be a positive integer`);
    } else if ((end as number) > 60 * MOTION_FPS) {
      errors.push(`${at}.end_frame_exclusive exceeds the 60 second motion timeline`);
    }
    let timingValid = false;
    if (Number.isInteger(start) && Number.isInteger(end)) {
      const span = (end as number) - (start as number);
      if (span <= 0) errors.push(`${at}.end_frame_exclusive must be greater than start_frame`);
      else if (span > MOTION_MAX_INSTANCE_FRAMES) {
        errors.push(`${at} exceeds the 8 second instance limit`);
      } else {
        timingValid = true;
      }
      if (durationFrames !== undefined && (end as number) > durationFrames) {
        errors.push(`${at}.end_frame_exclusive exceeds the video duration`);
      }
    }
    validateDefinition(
      { key: "intensity", type: "number", required: true, minimum: 0, maximum: 1 },
      raw.intensity,
      `${at}.intensity`,
      errors,
    );
    if (!isRecord(raw.palette)) {
      errors.push(`${at}.palette must be an object`);
    } else {
      rejectUnknown(raw.palette, ["primary", "accent"], `${at}.palette`, errors);
      if (typeof raw.palette.primary !== "string" || !COLOR_RE.test(raw.palette.primary)) {
        errors.push(`${at}.palette.primary must be #RRGGBB`);
      }
      if (typeof raw.palette.accent !== "string" || !COLOR_RE.test(raw.palette.accent)) {
        errors.push(`${at}.palette.accent must be #RRGGBB`);
      }
    }
    if (entry && creatorVersion) {
      validateCreatorParams(entry, version, raw.params, `${at}.params`, errors);
      if (version === 2) validateV2Motion(entry, raw.motion, `${at}.motion`, errors);
    } else if (routeTrace && raw.params !== undefined) {
      errors.push(`${at}.params is not supported for route_trace`);
    }
    if ((routeTrace || creatorVersion) && timingValid) timings.push(raw as unknown as MotionPresetInstance);
  });
  const activeFrames = activeMotionFrameCount(timings);
  if (activeFrames > MOTION_MAX_ACTIVE_FRAMES) {
    errors.push(`motion_scenes has ${activeFrames} active frames; maximum is ${MOTION_MAX_ACTIVE_FRAMES}`);
  }
  const weightedFrames = activeMotionComplexity(timings);
  if (weightedFrames > MOTION_MAX_WEIGHTED_ACTIVE_FRAMES) {
    errors.push(
      `motion_scenes has ${weightedFrames} weighted active frames; ` +
      `maximum is ${MOTION_MAX_WEIGHTED_ACTIVE_FRAMES}`,
    );
  }
  return { ok: errors.length === 0, errors };
}

export function activeMotionInstances(
  instances: readonly MotionPresetInstance[],
  frame: number,
): MotionPresetInstance[] {
  return instances.filter(
    (instance) => frame >= instance.start_frame && frame < instance.end_frame_exclusive,
  );
}

export function isLegacyRouteTracePayload(instances: readonly MotionPresetInstance[]): boolean {
  return instances.every(
    (instance) => instance.preset_id === "route_trace" && instance.preset_version === 1,
  );
}
