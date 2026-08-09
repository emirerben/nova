import type {
  CreatorBlockPresetId,
  MotionAssetRef,
  MotionPalette,
  MotionPresetInstance,
} from "./contract.ts";
import catalogJson from "../creator-blocks.catalog.json";

export interface CreatorBlockParameter {
  key: string;
  type: "string" | "string_list" | "asset_list";
  required: boolean;
  min_length?: number;
  max_length?: number;
  min_items?: number;
  max_items?: number;
}

export interface CreatorBlockCatalogEntry {
  preset_id: CreatorBlockPresetId;
  preset_version: 1;
  label: string;
  default_duration_frames: number;
  kind: "text" | "media";
  min_assets: number;
  ai_exposed: boolean;
  parameters: CreatorBlockParameter[];
  defaults: Record<string, unknown>;
}

/** Immutable catalog shared by the editor, validator tests, and AI snapshot. */
export const CREATOR_BLOCK_CATALOG = Object.freeze(
  catalogJson.presets.map((entry) => Object.freeze(entry)),
) as readonly CreatorBlockCatalogEntry[];

export const CREATOR_BLOCK_IDS = Object.freeze(CREATOR_BLOCK_CATALOG.map((entry) => entry.preset_id));

export function creatorBlockEntry(presetId: CreatorBlockPresetId): CreatorBlockCatalogEntry {
  const entry = CREATOR_BLOCK_CATALOG.find((candidate) => candidate.preset_id === presetId);
  if (!entry) throw new Error(`Unknown Creator Block: ${presetId}`);
  return entry;
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
}): MotionPresetInstance {
  const entry = creatorBlockEntry(args.presetId);
  const params = JSON.parse(JSON.stringify(entry.defaults)) as Record<string, unknown>;
  if (entry.kind === "media") params.assets = args.assets ?? [];
  return {
    id: args.id,
    preset_id: args.presetId,
    preset_version: 1,
    start_frame: args.startFrame,
    end_frame_exclusive: args.endFrameExclusive,
    palette: args.palette ?? { primary: "#0C0C0E", accent: "#C7FF3D" },
    intensity: 0.72,
    params,
  } as unknown as MotionPresetInstance;
}
