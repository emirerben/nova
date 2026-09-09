import type { EditorCapabilities } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { creatorBlockEntry, type MotionPresetInstance } from "@nova/motion-runtime";
import { mutationFingerprint } from "./mutation-fingerprint";
import type { TextAppearancePatch, TextAppearanceSelector } from "./ops";

export const TEXT_APPEARANCE_FIELDS = ["stroke_width", "shadow_enabled"] as const;
export const CAPTION_META_TARGET_ID = "caption-meta";
export interface TextAppearanceTarget {
  id: string;
  kind: "text" | "caption" | "motion";
  supported_fields: Array<keyof TextAppearancePatch>;
  values: { stroke_width: number | null; shadow_enabled: boolean | null };
  /** Opaque draft fingerprint, never raw component data or an authority token. */
  identity: string;
}
export interface TextAppearanceInventory {
  version: 1;
  caption_cues_editable?: boolean;
  targets: TextAppearanceTarget[];
}
interface AppearanceMeta {
  stroke_width?: number | null;
  shadow_enabled?: boolean | null;
}
export interface TextAppearanceInventoryInput {
  bars: readonly TextElementBar[];
  motionScenes?: readonly MotionPresetInstance[];
  captionMeta?: AppearanceMeta | null;
  captionsPresent?: boolean;
  captionCuesEditable?: boolean;
  allowedFamilies: readonly string[];
  capabilities?: EditorCapabilities | null;
}

/** One inventory definition for prompt context and the live local apply guard.
 * Read-only targets stay visible with no supported fields: "all" cannot omit them.
 * Renderer-inert geometry/media blocks are not text targets.
 */
export function buildTextAppearanceInventory(input: TextAppearanceInventoryInput): TextAppearanceInventory {
  const families = new Set(input.allowedFamilies);
  const canCaption = families.has("caption");
  const canText = families.has("text") && input.capabilities?.text_elements !== false;
  const canMotion = families.has("motion") && input.capabilities?.motion_scenes !== false;
  const captionBars = input.bars.filter((bar) => bar.role === "narrated_caption");
  const metaOnly = input.captionCuesEditable === false;
  const targets: TextAppearanceTarget[] = input.bars
    .filter((bar) => !metaOnly || bar.role !== "narrated_caption")
    .map((bar) => {
      const caption = bar.role === "narrated_caption";
      // Baked lyrics have a narrower legacy Save contract. Never pretend their
      // preview-only bars can accept an unpersistable shadow/stroke patch.
      const editable = caption ? canCaption : canText && (
        bar.role !== "lyric_line" || input.capabilities?.lyrics?.lyrics_model === "elements"
      );
      return {
        id: bar.id,
        kind: caption ? "caption" : "text",
        supported_fields: editable ? [...TEXT_APPEARANCE_FIELDS] : [],
        values: {
          stroke_width: caption
            ? bar.cue_stroke_width ?? bar.stroke_width ?? input.captionMeta?.stroke_width ?? null
            : bar.stroke_width ?? null,
          shadow_enabled: caption
            ? bar.cue_shadow_enabled ?? bar.shadow_enabled ?? input.captionMeta?.shadow_enabled ?? true
            : bar.shadow_enabled ?? true,
        },
        identity: mutationFingerprint([bar, caption ? input.captionMeta ?? null : null]),
      };
    });
  if (input.captionMeta && (captionBars.length > 0 || input.captionsPresent)) {
    targets.push({
      id: CAPTION_META_TARGET_ID,
      kind: "caption",
      supported_fields: canCaption ? [...TEXT_APPEARANCE_FIELDS] : [],
      values: {
        stroke_width: input.captionMeta.stroke_width ?? null,
        shadow_enabled: input.captionMeta.shadow_enabled ?? true,
      },
      identity: mutationFingerprint([CAPTION_META_TARGET_ID, input.captionMeta]),
    });
  }
  for (const scene of input.motionScenes ?? []) {
    if (scene.preset_id === "route_trace" || creatorBlockEntry(scene.preset_id).kind !== "text") continue;
    const hasDepth = ["kinetic_word", "flow_field", "cloud_break"].includes(scene.preset_id);
    targets.push({
      id: scene.id,
      kind: "motion",
      supported_fields: canMotion ? (hasDepth ? [...TEXT_APPEARANCE_FIELDS] : ["stroke_width"]) : [],
      values: {
        stroke_width: scene.text_appearance?.stroke_width ?? 0,
        shadow_enabled: hasDepth ? scene.text_appearance?.shadow_enabled ?? true : false,
      },
      identity: mutationFingerprint([scene]),
    });
  }
  return { version: 1, caption_cues_editable: input.captionCuesEditable !== false, targets };
}

/** null means an invalid/incomplete selection; [] means a valid empty scope. */
export function selectTextAppearanceTargets(
  inventory: TextAppearanceInventory,
  selector: TextAppearanceSelector,
): TextAppearanceTarget[] | null {
  const allIds = inventory.targets.map((target) => target.id);
  if (new Set(allIds).size !== allIds.length) return null;
  const targets = inventory.targets.filter((target) => !selector.category || target.kind === selector.category);
  if (!selector.target_ids) return targets;
  const ids = new Set(selector.target_ids);
  const selected = targets.filter((target) => ids.has(target.id));
  return selected.length === ids.size ? selected : null;
}
