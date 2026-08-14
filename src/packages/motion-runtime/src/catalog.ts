import type {
  CreatorBlockEasing,
  CreatorBlockInstanceAny,
  CreatorBlockInstanceV2,
  CreatorBlockMotionConfigV2,
  CreatorBlockParameterDefinition,
  CreatorBlockParameterType,
  CreatorBlockPresetId,
  CreatorBlockPresetVersion,
  MotionAssetRef,
  MotionPalette,
  MotionPresetInstance,
} from "./contract.ts";
import catalogJson from "../creator-blocks.catalog.json" with { type: "json" };

export type CreatorBlockParameter = CreatorBlockParameterDefinition;

export interface CreatorBlockControlDefinition {
  key: string;
  type: "number" | "enum" | "boolean";
  required: boolean;
  default: number | string | boolean;
  minimum?: number;
  maximum?: number;
  step?: number;
  integer?: boolean;
  values?: string[];
  storage: "instance" | "motion";
}

export interface CreatorBlockControlOverride {
  minimum?: number;
  maximum?: number;
  step?: number;
  values?: string[];
  default?: number | string | boolean;
}

export interface CreatorBlockMotionDefaults {
  speed: number;
  easing: CreatorBlockEasing;
  intensity: number;
  hold_frames: number;
}

export interface CreatorBlockCatalogEntry {
  preset_id: CreatorBlockPresetId;
  preset_version: 2;
  legacy_versions: 1[];
  label: string;
  default_duration_frames: number;
  base_choreography_frames: number;
  fixed_exit_frames: number;
  kind: "text" | "media";
  min_assets: number;
  ai_exposed: boolean;
  complexity_weight: number;
  supported_controls: string[];
  control_overrides: Record<string, CreatorBlockControlOverride>;
  palette_defaults: MotionPalette;
  parameters: Array<CreatorBlockParameterDefinition & {
    type: CreatorBlockParameterType;
  }>;
  defaults: Record<string, unknown>;
  motion_defaults: CreatorBlockMotionDefaults;
}

interface CreatorBlockCatalogDocument {
  catalog_version: 2;
  schema_version: 3;
  control_definitions: CreatorBlockControlDefinition[];
  presets: CreatorBlockCatalogEntry[];
}

const catalog = catalogJson as unknown as CreatorBlockCatalogDocument;

/** Immutable current catalog shared by editor discovery, validation, schema, and AI. */
export const CREATOR_BLOCK_CATALOG = Object.freeze(
  catalog.presets.map((entry) => Object.freeze(entry)),
) as readonly CreatorBlockCatalogEntry[];

export const CREATOR_BLOCK_CONTROL_DEFINITIONS = Object.freeze(
  catalog.control_definitions.map((control) => Object.freeze(control)),
) as readonly CreatorBlockControlDefinition[];

export const CREATOR_BLOCK_IDS = Object.freeze(
  CREATOR_BLOCK_CATALOG.map((entry) => entry.preset_id),
);

export function creatorBlockEntry(
  presetId: CreatorBlockPresetId,
  presetVersion?: CreatorBlockPresetVersion,
): CreatorBlockCatalogEntry {
  const entry = CREATOR_BLOCK_CATALOG.find((candidate) => candidate.preset_id === presetId);
  if (
    !entry ||
    (presetVersion !== undefined &&
      presetVersion !== entry.preset_version &&
      !entry.legacy_versions.includes(presetVersion as 1))
  ) {
    throw new Error(
      `Unknown Creator Block: ${presetId}${presetVersion === undefined ? "" : ` v${presetVersion}`}`,
    );
  }
  return entry;
}

export function creatorBlockControl(
  entryOrId: CreatorBlockCatalogEntry | CreatorBlockPresetId,
  key: string,
): CreatorBlockControlDefinition | undefined {
  const entry = typeof entryOrId === "string" ? creatorBlockEntry(entryOrId) : entryOrId;
  if (!entry.supported_controls.includes(key)) return undefined;
  const definition = CREATOR_BLOCK_CONTROL_DEFINITIONS.find((control) => control.key === key);
  if (!definition) throw new Error(`Unknown Creator Block control: ${key}`);
  return {
    ...definition,
    ...(entry.control_overrides[key] ?? {}),
  };
}

export function creatorBlockMotionDefaults(
  entryOrId: CreatorBlockCatalogEntry | CreatorBlockPresetId,
): CreatorBlockMotionConfigV2 {
  const entry = typeof entryOrId === "string" ? creatorBlockEntry(entryOrId) : entryOrId;
  return {
    version: 2,
    speed: entry.motion_defaults.speed,
    easing: entry.motion_defaults.easing,
    hold_frames: entry.motion_defaults.hold_frames,
  };
}

export function creatorBlockAssetRefs(instance: MotionPresetInstance): MotionAssetRef[] {
  return instance.preset_id === "card_stack" || instance.preset_id === "film_strip"
    ? instance.params.assets
    : [];
}

export function createCreatorBlockInstance(args: {
  id: string;
  presetId: CreatorBlockPresetId;
  startFrame: number;
  endFrameExclusive: number;
  palette?: MotionPalette;
  assets?: MotionAssetRef[];
}): CreatorBlockInstanceV2 {
  const entry = creatorBlockEntry(args.presetId, 2);
  const params = JSON.parse(JSON.stringify(entry.defaults)) as Record<string, unknown>;
  if (entry.kind === "media") params.assets = args.assets ?? [];
  return {
    id: args.id,
    preset_id: args.presetId,
    preset_version: 2,
    start_frame: args.startFrame,
    end_frame_exclusive: args.endFrameExclusive,
    palette: args.palette ?? { ...entry.palette_defaults },
    intensity: entry.motion_defaults.intensity,
    motion: creatorBlockMotionDefaults(entry),
    params,
  } as unknown as CreatorBlockInstanceV2;
}

/**
 * Explicit migration used only after a user touches a v2 motion control.
 * Content, palette, timing, assets, and legacy intensity are retained exactly;
 * only newly-supported advanced params and the v2 motion object are initialized.
 */
export function upgradeCreatorBlockInstanceToV2(
  instance: CreatorBlockInstanceAny,
): CreatorBlockInstanceV2 {
  if (instance.preset_version === 2) {
    return {
      ...instance,
      palette: { ...instance.palette },
      params: JSON.parse(JSON.stringify(instance.params)) as typeof instance.params,
      motion: { ...instance.motion },
    } as CreatorBlockInstanceV2;
  }
  const entry = creatorBlockEntry(instance.preset_id, 1);
  const params = {
    ...(JSON.parse(JSON.stringify(entry.defaults)) as Record<string, unknown>),
    ...(JSON.parse(JSON.stringify(instance.params)) as Record<string, unknown>),
  };
  return {
    ...instance,
    preset_version: 2,
    palette: { ...instance.palette },
    params,
    motion: creatorBlockMotionDefaults(entry),
  } as unknown as CreatorBlockInstanceV2;
}

export function creatorBlockDurationFramesV2(
  instanceOrEntry: CreatorBlockInstanceV2 | CreatorBlockCatalogEntry,
  motionOverride?: CreatorBlockMotionConfigV2,
): number {
  const isEntry = "base_choreography_frames" in instanceOrEntry;
  const entry = isEntry
    ? instanceOrEntry
    : creatorBlockEntry(instanceOrEntry.preset_id, 2);
  const motion = motionOverride ?? (isEntry
    ? creatorBlockMotionDefaults(entry)
    : instanceOrEntry.motion);
  if (
    !Number.isFinite(motion.speed) ||
    motion.speed <= 0 ||
    !Number.isInteger(motion.hold_frames) ||
    motion.hold_frames < 0
  ) {
    throw new Error("Creator Block v2 timing is invalid");
  }
  return Math.max(
    1,
    Math.round(entry.base_choreography_frames / motion.speed) +
      motion.hold_frames +
      entry.fixed_exit_frames,
  );
}

/** Retime only the selected block, keeping start fixed and clamping at the video boundary. */
export function retimeCreatorBlockSpeed(
  scene: CreatorBlockInstanceAny,
  nextSpeed: number,
  videoEndFrame: number,
): CreatorBlockInstanceV2 {
  const upgraded = upgradeCreatorBlockInstanceToV2(scene);
  const speedControl = creatorBlockControl(upgraded.preset_id, "speed");
  if (!speedControl || !Number.isFinite(nextSpeed)) {
    throw new Error("Creator Block speed is unavailable or invalid");
  }
  if (!Number.isInteger(videoEndFrame) || videoEndFrame <= upgraded.start_frame) {
    throw new Error("Creator Block video boundary must be after its start frame");
  }
  const speed = Math.max(
    speedControl.minimum ?? 0.25,
    Math.min(speedControl.maximum ?? 4, nextSpeed),
  );
  const motion = { ...upgraded.motion, speed };
  const requestedEnd = upgraded.start_frame + creatorBlockDurationFramesV2(upgraded, motion);
  return {
    ...upgraded,
    motion,
    end_frame_exclusive: Math.max(
      upgraded.start_frame + 1,
      Math.min(videoEndFrame, requestedEnd),
    ),
  } as CreatorBlockInstanceV2;
}

function creatorBlockSpeedGrid(
  control: CreatorBlockControlDefinition,
): number[] {
  const minimum = control.minimum ?? 0.25;
  const maximum = control.maximum ?? 4;
  const step = control.step ?? 0.05;
  const steps = Math.floor((maximum - minimum) / step + 1e-8);
  return Array.from({ length: steps + 1 }, (_, index) =>
    Number((minimum + index * step).toFixed(8)),
  );
}

/**
 * Apply a manual timeline move/resize without migrating legacy scenes.
 *
 * A same-span move preserves v2 motion exactly. A v2 resize consumes or
 * extends hold first, then selects the nearest supported speed in the resize
 * direction. Requests shorter/longer than the representable timing window are
 * clamped at the edited edge.
 */
export function retimeCreatorBlockManualSpan(
  scene: CreatorBlockInstanceAny,
  requestedStartFrame: number,
  requestedEndFrameExclusive: number,
  videoEndFrame: number,
): CreatorBlockInstanceAny {
  if (
    !Number.isInteger(requestedStartFrame) ||
    !Number.isInteger(requestedEndFrameExclusive) ||
    !Number.isInteger(videoEndFrame) ||
    videoEndFrame <= 0
  ) {
    throw new Error("Creator Block manual timing must use integer frames");
  }

  const startFrame = Math.max(0, Math.min(videoEndFrame - 1, requestedStartFrame));
  const endFrameExclusive = Math.max(
    startFrame + 1,
    Math.min(videoEndFrame, requestedEndFrameExclusive),
  );
  const previousSpan = scene.end_frame_exclusive - scene.start_frame;
  const requestedSpan = endFrameExclusive - startFrame;

  if (scene.preset_version === 1 || requestedSpan === previousSpan) {
    return {
      ...scene,
      start_frame: startFrame,
      end_frame_exclusive: endFrameExclusive,
    } as CreatorBlockInstanceAny;
  }

  const entry = creatorBlockEntry(scene.preset_id, 2);
  const speedControl = creatorBlockControl(entry, "speed");
  const holdControl = creatorBlockControl(entry, "hold_frames");
  if (!speedControl || !holdControl) {
    throw new Error("Creator Block manual timing controls are unavailable");
  }

  const speeds = creatorBlockSpeedGrid(speedControl);
  const minimumHold = holdControl.minimum ?? 0;
  const maximumHold = holdControl.maximum ?? 240;
  const currentSpeed = scene.motion.speed;
  const candidates = speeds
    .map((speed) => ({
      speed,
      settleFrames: Math.round(entry.base_choreography_frames / speed),
    }))
    .filter(({ settleFrames }) => {
      const hold = requestedSpan - entry.fixed_exit_frames - settleFrames;
      return hold >= minimumHold && hold <= maximumHold;
    });

  let selected = candidates.find(({ speed }) => Math.abs(speed - currentSpeed) < 1e-8);
  if (!selected && requestedSpan < previousSpan) {
    selected = candidates.find(({ speed }) => speed >= currentSpeed - 1e-8);
  }
  if (!selected && requestedSpan > previousSpan) {
    selected = [...candidates].reverse().find(({ speed }) => speed <= currentSpeed + 1e-8);
  }
  selected ??= candidates.reduce<typeof candidates[number] | undefined>((best, candidate) =>
    !best || Math.abs(candidate.speed - currentSpeed) < Math.abs(best.speed - currentSpeed)
      ? candidate
      : best,
  undefined);

  let resolvedSpan = requestedSpan;
  let speed: number;
  let holdFrames: number;
  if (selected) {
    speed = selected.speed;
    holdFrames = requestedSpan - entry.fixed_exit_frames - selected.settleFrames;
  } else {
    const fastest = speeds[speeds.length - 1];
    const slowest = speeds[0];
    const minimumSpan = Math.round(entry.base_choreography_frames / fastest) +
      minimumHold + entry.fixed_exit_frames;
    const maximumSpan = Math.round(entry.base_choreography_frames / slowest) +
      maximumHold + entry.fixed_exit_frames;
    if (requestedSpan < minimumSpan) {
      resolvedSpan = minimumSpan;
      speed = fastest;
      holdFrames = minimumHold;
    } else {
      resolvedSpan = maximumSpan;
      speed = slowest;
      holdFrames = maximumHold;
    }
  }

  const editedStart = requestedStartFrame !== scene.start_frame;
  const editedEnd = requestedEndFrameExclusive !== scene.end_frame_exclusive;
  const anchorEnd = editedStart && !editedEnd;
  const resolvedStart = anchorEnd
    ? Math.max(0, endFrameExclusive - resolvedSpan)
    : startFrame;
  const resolvedEnd = anchorEnd
    ? endFrameExclusive
    : Math.min(videoEndFrame, resolvedStart + resolvedSpan);

  return {
    ...scene,
    start_frame: resolvedStart,
    end_frame_exclusive: resolvedEnd,
    motion: {
      ...scene.motion,
      speed,
      hold_frames: holdFrames,
    },
  } as CreatorBlockInstanceV2;
}
