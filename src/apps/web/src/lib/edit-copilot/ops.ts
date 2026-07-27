import fontRegistryJson from "@/data/font-registry.json";
import type { EditorTransition } from "@/lib/generative-api";

export type CopilotOpFamily =
  | "text"
  | "clip"
  | "sfx"
  | "overlay"
  | "caption"
  | "music"
  | "render"
  | "title"
  | "tool"
  | "effect"
  | "transition"
  | "visual";

export const TEXT_STYLE_PATCH_KEYS = [
  "font_family",
  "size_px",
  "color",
  "highlight_color",
  "effect",
  "alignment",
  "text_case",
  "letter_spacing",
  "line_spacing",
  "max_width_frac",
  "stroke_width",
  "position",
  "x_frac",
  "y_frac",
] as const;

export type TextStylePatchKey = (typeof TEXT_STYLE_PATCH_KEYS)[number];

export const OVERLAY_PATCH_KEYS = [
  "start_s",
  "end_s",
  "position",
  "x_frac",
  "y_frac",
  "scale",
  "display_mode",
] as const;

export type OverlayPatchKey = (typeof OVERLAY_PATCH_KEYS)[number];

export const CAPTION_META_KEYS = [
  "enabled",
  "style",
  "font",
  "y_frac",
  "size_px",
  "color",
  "highlight_color",
  "stroke_width",
  "shadow_enabled",
] as const;

export type CaptionMetaKey = (typeof CAPTION_META_KEYS)[number];

export type TextStylePatch = Partial<{
  font_family: string;
  size_px: number;
  color: string;
  highlight_color: string;
  effect: string;
  alignment: string;
  text_case: string;
  letter_spacing: number;
  line_spacing: number;
  max_width_frac: number;
  stroke_width: number;
  position: string;
  x_frac: number | null;
  y_frac: number | null;
}>;

export type OverlayPatch = Partial<{
  start_s: number;
  end_s: number;
  position: "top" | "center" | "bottom" | "custom";
  x_frac: number;
  y_frac: number;
  scale: number;
  display_mode: "pip" | "fullscreen";
}>;

export type CaptionMetaPatch = Partial<{
  enabled: boolean;
  style: "sentence" | "word";
  font: string | null;
  y_frac: number;
  size_px: number;
  color: string;
  highlight_color: string;
  stroke_width: number;
  shadow_enabled: boolean;
}>;

export type CopilotOp =
  | { op: "edit_text"; bar_index: number; text: string }
  | { op: "patch_text_style"; bar_index: number; patch: TextStylePatch }
  | { op: "set_text_timing"; bar_index: number; start_s?: number; end_s?: number }
  | { op: "add_text"; text: string; start_s: number; end_s: number }
  | { op: "remove_text"; bar_index: number }
  | { op: "set_clip_duration"; slot_index: number; duration_s: number }
  | { op: "set_clip_in"; slot_index: number; in_s: number }
  | { op: "reorder_clip"; from_index: number; to_index: number }
  | { op: "remove_clip"; slot_index: number }
  | { op: "split_clip"; slot_index: number; at_s: number }
  | { op: "add_sfx"; effect_id: string; at_s: number; gain: number }
  | { op: "patch_sfx"; sfx_index: number; at_s?: number; gain?: number }
  | { op: "remove_sfx"; sfx_index: number }
  | { op: "patch_overlay"; overlay_index: number; patch: OverlayPatch }
  | { op: "remove_overlay"; overlay_index: number }
  | {
      op: "add_overlay";
      asset_id: string;
      start_s: number;
      end_s: number;
      position?: "top" | "center" | "bottom" | "custom";
      x_frac?: number;
      y_frac?: number;
      scale?: number;
      display_mode?: "pip" | "fullscreen";
    }
  | { op: "accept_overlay_suggestion"; suggestion_id: string }
  | { op: "edit_caption"; cue_index: number; text: string }
  | { op: "set_caption_timing"; cue_index: number; start_s?: number; end_s?: number }
  | { op: "set_caption_meta"; patch: CaptionMetaPatch }
  | { op: "set_caption_emphasis"; cue_index: number; emphasis: boolean }
  | { op: "swap_music"; track_id: string }
  | { op: "set_mix"; music_level: number }
  | { op: "set_intro_layout"; layout: "linear" | "cluster" }
  | { op: "set_title"; title: string }
  | { op: "add_camera_effect"; start_s: number; end_s: number; intensity?: number }
  | {
      op: "patch_camera_effect";
      camera_effect_index: number;
      start_s?: number;
      end_s?: number;
      intensity?: number;
    }
  | { op: "remove_camera_effect"; camera_effect_index: number }
  | {
      op: "set_visual_fade";
      visual_block_index: number;
      transition_in?: "cut" | "fade";
      transition_out?: "cut" | "fade";
    }
  | {
      op: "set_transition";
      boundary_index: number;
      transition: EditorTransition;
      duration_s?: number;
    }
  | {
      op: "insert_generated_asset";
      asset_id: string;
      clip_index: number;
      insert_at_s: number;
      duration_s: number;
    }
  | {
      op: "replace_generated_segment";
      asset_id: string;
      clip_index: number;
      source_clip_index: number;
      source_start_s: number;
      source_end_s: number;
      duration_s: number;
    }
  | {
      op: "open_tool";
      tool: "text" | "visuals" | "sounds" | "overlays" | "styles";
    };

export type CopilotOpName = CopilotOp["op"];

export type OpValidationReason =
  | "unknown_op"
  | "missing_required"
  | "invalid_type"
  | "invalid_value"
  | "invalid_index"
  | "invalid_time"
  | "empty_patch";

export interface OpValidationRejection {
  reason: OpValidationReason;
  message: string;
  op?: string;
}

export type OpValidationResult =
  | { ok: true; op: CopilotOp }
  | { ok: false; rejection: OpValidationRejection };

type StylePatchValidation =
  | { ok: true; patch: TextStylePatch }
  | { ok: false; rejection: OpValidationRejection };

type OverlayPatchValidation =
  | { ok: true; patch: OverlayPatch }
  | { ok: false; rejection: OpValidationRejection };

type CaptionMetaPatchValidation =
  | { ok: true; patch: CaptionMetaPatch }
  | { ok: false; rejection: OpValidationRejection };

export interface CopilotValidationSnapshot {
  total_duration_s?: number | null;
  text_bars?: unknown[];
  slots?: Array<{
    key?: string;
    clip_index?: number;
    in_s?: number;
    duration_s?: number;
    output_start_s?: number | null;
    output_end_s?: number | null;
    removed?: boolean;
    transition_after?: string | null;
    transition_duration_s?: number | null;
  }>;
  camera_effects?: unknown[];
  visual_blocks?: unknown[];
  sfx?: {
    placements?: unknown[];
  };
  overlays?: {
    cards?: unknown[];
    pending_suggestions?: unknown[];
  };
  captions?: {
    cues?: unknown[];
    /** false = meta-only captions (subtitled): cue text/timing ops are invalid. */
    cues_editable?: boolean;
  };
}

interface FontRegistryFile {
  fonts: Record<string, unknown>;
}

const FONT_REGISTRY = (fontRegistryJson as FontRegistryFile).fonts;
const LEGACY_FONT_ALIASES = new Set([
  "PlayfairDisplay-Bold",
  "PlayfairDisplay-Regular",
  "Inter-Bold",
  "Inter-Regular",
]);

const ALLOWED_EFFECTS = new Set([
  "static",
  "none",
  "fade-in",
  "slide-up",
  "slide-down",
  "karaoke-line",
  "pop-in",
  "scale-up",
  "typewriter",
  "stream-in",
  "staggered-slice",
  "ink-reveal",
  "handwriting",
  "dissolve-out",
  "bounce",
  "slide-in",
]);

const ALLOWED_ALIGNMENTS = new Set(["left", "center", "right"]);
const ALLOWED_TEXT_CASES = new Set(["none", "upper", "lower", "title"]);
const ALLOWED_POSITIONS = new Set(["top", "middle", "bottom", "custom"]);
const ALLOWED_OVERLAY_POSITIONS = new Set(["top", "center", "bottom", "custom"]);
const ALLOWED_DISPLAY_MODES = new Set(["pip", "fullscreen"]);
const ALLOWED_CAPTION_STYLES = new Set(["sentence", "word"]);
const ALLOWED_TOOLS = new Set(["text", "sounds", "overlays", "styles"]);
const HEX_COLOR = /^#[0-9A-Fa-f]{6}$/;
const STYLE_PATCH_KEY_SET = new Set<string>(TEXT_STYLE_PATCH_KEYS);
const OVERLAY_PATCH_KEY_SET = new Set<string>(OVERLAY_PATCH_KEYS);
const CAPTION_META_KEY_SET = new Set<string>(CAPTION_META_KEYS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function nonNegativeNumber(value: unknown): value is number {
  return finiteNumber(value) && value >= 0;
}

function integerIndex(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function reject(
  reason: OpValidationReason,
  message: string,
  op?: string,
): OpValidationResult {
  return { ok: false, rejection: { reason, message, op } };
}

function rejectStyle(reason: OpValidationReason, message: string): StylePatchValidation {
  return { ok: false, rejection: { reason, message } };
}

function rejectOverlayPatch(reason: OpValidationReason, message: string): OverlayPatchValidation {
  return { ok: false, rejection: { reason, message } };
}

function rejectCaptionMetaPatch(reason: OpValidationReason, message: string): CaptionMetaPatchValidation {
  return { ok: false, rejection: { reason, message } };
}

function hasIndex(
  snapshot: CopilotValidationSnapshot | undefined,
  kind: "text" | "slot" | "sfx" | "overlay" | "caption" | "suggestion",
  index: number,
) {
  const arr =
    kind === "text"
      ? snapshot?.text_bars
      : kind === "slot"
        ? snapshot?.slots
        : kind === "sfx"
          ? snapshot?.sfx?.placements
          : kind === "overlay"
            ? snapshot?.overlays?.cards
            : kind === "suggestion"
              ? snapshot?.overlays?.pending_suggestions
              : snapshot?.captions?.cues;
  return !arr || index < arr.length;
}

function textBarRole(
  snapshot: CopilotValidationSnapshot | undefined,
  index: number,
): string | null {
  const bar = snapshot?.text_bars?.[index];
  return isRecord(bar) && typeof bar.role === "string" ? bar.role : null;
}

function validFont(name: string): boolean {
  return Object.prototype.hasOwnProperty.call(FONT_REGISTRY, name) || LEGACY_FONT_ALIASES.has(name);
}

function validateStylePatch(raw: unknown): StylePatchValidation {
  if (!isRecord(raw)) return rejectStyle("invalid_type", "patch must be an object");
  const patch: TextStylePatch = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!STYLE_PATCH_KEY_SET.has(key)) continue;
    if (key === "font_family") {
      if (typeof value !== "string" || !validFont(value)) {
        return rejectStyle("invalid_value", "font_family must be in the Nova font registry");
      }
      patch.font_family = value;
    } else if (key === "color" || key === "highlight_color") {
      if (typeof value !== "string" || !HEX_COLOR.test(value)) {
        return rejectStyle("invalid_value", `${key} must be #RRGGBB`);
      }
      patch[key] = value;
    } else if (key === "effect") {
      if (typeof value !== "string" || !ALLOWED_EFFECTS.has(value)) {
        return rejectStyle("invalid_value", "effect is not supported by text elements");
      }
      patch.effect = value;
    } else if (key === "alignment") {
      if (typeof value !== "string" || !ALLOWED_ALIGNMENTS.has(value)) {
        return rejectStyle("invalid_value", "alignment must be left, center, or right");
      }
      patch.alignment = value;
    } else if (key === "text_case") {
      if (typeof value !== "string" || !ALLOWED_TEXT_CASES.has(value)) {
        return rejectStyle("invalid_value", "text_case is not supported");
      }
      patch.text_case = value;
    } else if (key === "position") {
      if (typeof value !== "string" || !ALLOWED_POSITIONS.has(value)) {
        return rejectStyle("invalid_value", "position is not supported");
      }
      patch.position = value;
    } else if (key === "x_frac" || key === "y_frac") {
      if (value !== null && !finiteNumber(value)) {
        return rejectStyle("invalid_type", `${key} must be a number or null`);
      }
      patch[key] = value;
    } else {
      if (!finiteNumber(value)) return rejectStyle("invalid_type", `${key} must be a number`);
      patch[key as Exclude<TextStylePatchKey, "font_family" | "color" | "highlight_color" | "effect" | "alignment" | "text_case" | "position" | "x_frac" | "y_frac">] = value;
    }
  }
  if (Object.keys(patch).length === 0) return rejectStyle("empty_patch", "patch contains no v1 style fields");
  return { ok: true, patch };
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

function clampAtS(value: number, snapshot: CopilotValidationSnapshot | undefined): number {
  const total = snapshot?.total_duration_s;
  // total <= 0 means the duration is unknown (slot-less variant before the
  // video metadata loads) — clamping against it would collapse every
  // placement to second 0. Keep the lower bound only.
  if (!finiteNumber(total) || total <= 0) return Math.max(0, value);
  return clamp(value, 0, Math.max(0, total - 0.1));
}

function cleanUserText(text: string, maxLength: number): string {
  return text.replace(/\s+/g, " ").replace(/[\u0000-\u001F\u007F]/g, "").trim().slice(0, maxLength);
}

function validateOverlayPatch(raw: unknown): OverlayPatchValidation {
  if (!isRecord(raw)) return rejectOverlayPatch("invalid_type", "patch must be an object");
  const patch: OverlayPatch = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!OVERLAY_PATCH_KEY_SET.has(key)) continue;
    if (key === "start_s" || key === "end_s") {
      if (!nonNegativeNumber(value)) return rejectOverlayPatch("invalid_time", `${key} must be non-negative seconds`);
      patch[key] = value;
    } else if (key === "position") {
      if (typeof value !== "string" || !ALLOWED_OVERLAY_POSITIONS.has(value)) {
        return rejectOverlayPatch("invalid_value", "position is not supported");
      }
      patch.position = value as OverlayPatch["position"];
    } else if (key === "display_mode") {
      if (typeof value !== "string" || !ALLOWED_DISPLAY_MODES.has(value)) {
        return rejectOverlayPatch("invalid_value", "display_mode must be pip or fullscreen");
      }
      patch.display_mode = value as OverlayPatch["display_mode"];
    } else {
      if (!finiteNumber(value)) return rejectOverlayPatch("invalid_type", `${key} must be a number`);
      patch[key as "x_frac" | "y_frac" | "scale"] =
        key === "scale" ? clamp(value, 0.05, 1) : clamp(value, 0, 1);
    }
  }
  if (patch.start_s !== undefined && patch.end_s !== undefined && patch.end_s <= patch.start_s) {
    return rejectOverlayPatch("invalid_time", "overlay end_s must be after start_s");
  }
  if (Object.keys(patch).length === 0) return rejectOverlayPatch("empty_patch", "patch contains no overlay fields");
  return { ok: true, patch };
}

function validateCaptionMetaPatch(raw: unknown): CaptionMetaPatchValidation {
  if (!isRecord(raw)) return rejectCaptionMetaPatch("invalid_type", "patch must be an object");
  const patch: CaptionMetaPatch = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!CAPTION_META_KEY_SET.has(key)) continue;
    if (key === "enabled") {
      if (typeof value !== "boolean") return rejectCaptionMetaPatch("invalid_type", "enabled must be boolean");
      patch.enabled = value;
    } else if (key === "style") {
      if (typeof value !== "string" || !ALLOWED_CAPTION_STYLES.has(value)) {
        return rejectCaptionMetaPatch("invalid_value", "style must be sentence or word");
      }
      patch.style = value as CaptionMetaPatch["style"];
    } else if (key === "font") {
      if (value !== null && (typeof value !== "string" || value.trim() === "")) {
        return rejectCaptionMetaPatch("invalid_value", "font must be a non-empty string or null");
      }
      patch.font = value === null ? null : (value as string).trim();
    } else if (key === "y_frac") {
      if (!finiteNumber(value)) return rejectCaptionMetaPatch("invalid_type", "y_frac must be a number");
      patch.y_frac = clamp(value, 0.3, 0.9);
    } else if (key === "size_px") {
      if (!finiteNumber(value)) return rejectCaptionMetaPatch("invalid_type", "size_px must be a number");
      patch.size_px = Math.round(clamp(value, 36, 160));
    } else if (key === "color" || key === "highlight_color") {
      if (typeof value !== "string" || !/^#[0-9A-Fa-f]{6}$/.test(value.trim())) {
        return rejectCaptionMetaPatch("invalid_value", `${key} must be a #RRGGBB hex color`);
      }
      patch[key] = value.trim().toUpperCase();
    } else if (key === "stroke_width") {
      if (!finiteNumber(value)) return rejectCaptionMetaPatch("invalid_type", "stroke_width must be a number");
      patch.stroke_width = Math.round(clamp(value, 0, 12));
    } else if (key === "shadow_enabled") {
      if (typeof value !== "boolean") return rejectCaptionMetaPatch("invalid_type", "shadow_enabled must be boolean");
      patch.shadow_enabled = value;
    }
  }
  if (Object.keys(patch).length === 0) return rejectCaptionMetaPatch("empty_patch", "patch contains no caption meta fields");
  return { ok: true, patch };
}

export function copilotOpFamily(op: Pick<CopilotOp, "op"> | { op: string }): CopilotOpFamily | null {
  if (
    op.op === "edit_text" ||
    op.op === "patch_text_style" ||
    op.op === "set_text_timing" ||
    op.op === "add_text" ||
    op.op === "remove_text"
  ) {
    return "text";
  }
  if (
    op.op === "set_clip_duration" ||
    op.op === "set_clip_in" ||
    op.op === "reorder_clip" ||
    op.op === "remove_clip" ||
    op.op === "split_clip" ||
    op.op === "insert_generated_asset" ||
    op.op === "replace_generated_segment"
  ) {
    return "clip";
  }
  if (op.op === "add_sfx" || op.op === "patch_sfx" || op.op === "remove_sfx") return "sfx";
  if (
    op.op === "patch_overlay" ||
    op.op === "remove_overlay" ||
    op.op === "add_overlay" ||
    op.op === "accept_overlay_suggestion"
  ) {
    return "overlay";
  }
  if (
    op.op === "edit_caption" ||
    op.op === "set_caption_timing" ||
    op.op === "set_caption_meta" ||
    op.op === "set_caption_emphasis"
  ) {
    return "caption";
  }
  if (op.op === "swap_music" || op.op === "set_mix") return "music";
  if (op.op === "set_intro_layout") return "render";
  if (op.op === "set_title") return "title";
  if (op.op === "open_tool") return "tool";
  if (
    op.op === "add_camera_effect" ||
    op.op === "patch_camera_effect" ||
    op.op === "remove_camera_effect"
  ) return "effect";
  if (op.op === "set_transition") return "transition";
  if (op.op === "set_visual_fade") return "visual";
  return null;
}

export function validateCopilotOp(
  raw: unknown,
  snapshot?: CopilotValidationSnapshot,
): OpValidationResult {
  if (!isRecord(raw) || typeof raw.op !== "string") {
    return reject("unknown_op", "op name is required");
  }

  const opName = raw.op;
  switch (opName) {
    case "edit_text": {
      if (!integerIndex(raw.bar_index) || typeof raw.text !== "string") {
        return reject("missing_required", "edit_text requires bar_index and text", opName);
      }
      if (!hasIndex(snapshot, "text", raw.bar_index)) {
        return reject("invalid_index", "bar_index must point into snapshot text bars", opName);
      }
      return { ok: true, op: { op: opName, bar_index: raw.bar_index, text: raw.text } };
    }
    case "patch_text_style": {
      if (!integerIndex(raw.bar_index)) {
        return reject("missing_required", "patch_text_style requires bar_index", opName);
      }
      if (!hasIndex(snapshot, "text", raw.bar_index)) {
        return reject("invalid_index", "bar_index must point into snapshot text bars", opName);
      }
      const patch = validateStylePatch(raw.patch);
      if (!patch.ok) return patch;
      return { ok: true, op: { op: opName, bar_index: raw.bar_index, patch: patch.patch } };
    }
    case "set_text_timing": {
      if (!integerIndex(raw.bar_index)) {
        return reject("missing_required", "set_text_timing requires bar_index", opName);
      }
      if (!hasIndex(snapshot, "text", raw.bar_index)) {
        return reject("invalid_index", "bar_index must point into snapshot text bars", opName);
      }
      if (textBarRole(snapshot, raw.bar_index) === "lyric_line") {
        return reject("invalid_value", "Lyric timing is locked to the vocal.", opName);
      }
      const hasStart = raw.start_s !== undefined;
      const hasEnd = raw.end_s !== undefined;
      if (!hasStart && !hasEnd) {
        return reject("missing_required", "set_text_timing requires start_s or end_s", opName);
      }
      if ((hasStart && !nonNegativeNumber(raw.start_s)) || (hasEnd && !nonNegativeNumber(raw.end_s))) {
        return reject("invalid_time", "text timing values must be non-negative seconds", opName);
      }
      return {
        ok: true,
        op: {
          op: opName,
          bar_index: raw.bar_index,
          ...(hasStart ? { start_s: raw.start_s as number } : {}),
          ...(hasEnd ? { end_s: raw.end_s as number } : {}),
        },
      };
    }
    case "add_text": {
      if (typeof raw.text !== "string" || !nonNegativeNumber(raw.start_s) || !nonNegativeNumber(raw.end_s)) {
        return reject("missing_required", "add_text requires text, start_s, and end_s", opName);
      }
      if (raw.end_s <= raw.start_s) {
        return reject("invalid_time", "add_text end_s must be after start_s", opName);
      }
      return { ok: true, op: { op: opName, text: raw.text, start_s: raw.start_s, end_s: raw.end_s } };
    }
    case "remove_text": {
      if (!integerIndex(raw.bar_index)) return reject("missing_required", "remove_text requires bar_index", opName);
      if (!hasIndex(snapshot, "text", raw.bar_index)) {
        return reject("invalid_index", "bar_index must point into snapshot text bars", opName);
      }
      if (textBarRole(snapshot, raw.bar_index) === "lyric_line") {
        return reject("invalid_value", "Lyric timing is locked to the vocal.", opName);
      }
      return { ok: true, op: { op: opName, bar_index: raw.bar_index } };
    }
    case "set_clip_duration": {
      if ("duration_beats" in raw || !integerIndex(raw.slot_index) || !finiteNumber(raw.duration_s)) {
        return reject("missing_required", "set_clip_duration requires slot_index and duration_s seconds", opName);
      }
      if (!hasIndex(snapshot, "slot", raw.slot_index)) {
        return reject("invalid_index", "slot_index must point into snapshot slots", opName);
      }
      if (raw.duration_s <= 0) return reject("invalid_value", "duration_s must be positive", opName);
      return { ok: true, op: { op: opName, slot_index: raw.slot_index, duration_s: raw.duration_s } };
    }
    case "set_clip_in": {
      if (!integerIndex(raw.slot_index) || !nonNegativeNumber(raw.in_s)) {
        return reject("missing_required", "set_clip_in requires slot_index and in_s", opName);
      }
      if (!hasIndex(snapshot, "slot", raw.slot_index)) {
        return reject("invalid_index", "slot_index must point into snapshot slots", opName);
      }
      return { ok: true, op: { op: opName, slot_index: raw.slot_index, in_s: raw.in_s } };
    }
    case "reorder_clip": {
      if (!integerIndex(raw.from_index) || !integerIndex(raw.to_index)) {
        return reject("missing_required", "reorder_clip requires from_index and to_index", opName);
      }
      if (!hasIndex(snapshot, "slot", raw.from_index) || !hasIndex(snapshot, "slot", raw.to_index)) {
        return reject("invalid_index", "clip indices must point into snapshot slots", opName);
      }
      return { ok: true, op: { op: opName, from_index: raw.from_index, to_index: raw.to_index } };
    }
    case "remove_clip": {
      if (!integerIndex(raw.slot_index)) return reject("missing_required", "remove_clip requires slot_index", opName);
      if (!hasIndex(snapshot, "slot", raw.slot_index)) {
        return reject("invalid_index", "slot_index must point into snapshot slots", opName);
      }
      return { ok: true, op: { op: opName, slot_index: raw.slot_index } };
    }
    case "split_clip": {
      if (!integerIndex(raw.slot_index) || !finiteNumber(raw.at_s)) {
        return reject("missing_required", "split_clip requires slot_index and at_s", opName);
      }
      if (!hasIndex(snapshot, "slot", raw.slot_index)) {
        return reject("invalid_index", "slot_index must point into snapshot slots", opName);
      }
      const slot = snapshot?.slots?.[raw.slot_index];
      if (
        slot &&
        finiteNumber(slot.output_start_s) &&
        finiteNumber(slot.output_end_s) &&
        (raw.at_s <= slot.output_start_s || raw.at_s >= slot.output_end_s)
      ) {
        return reject("invalid_time", "split_clip.at_s must be inside the slot output window", opName);
      }
      return { ok: true, op: { op: opName, slot_index: raw.slot_index, at_s: raw.at_s } };
    }
    case "add_sfx": {
      if (typeof raw.effect_id !== "string" || raw.effect_id.trim() === "" || !finiteNumber(raw.at_s)) {
        return reject("missing_required", "add_sfx requires effect_id and at_s", opName);
      }
      const gain = raw.gain === undefined ? 1 : raw.gain;
      if (!finiteNumber(gain)) return reject("invalid_type", "gain must be a number", opName);
      return {
        ok: true,
        op: {
          op: opName,
          effect_id: raw.effect_id,
          at_s: clampAtS(raw.at_s, snapshot),
          gain: clamp(gain, 0, 2),
        },
      };
    }
    case "patch_sfx": {
      if (!integerIndex(raw.sfx_index)) return reject("missing_required", "patch_sfx requires sfx_index", opName);
      if (!hasIndex(snapshot, "sfx", raw.sfx_index)) {
        return reject("invalid_index", "sfx_index must point into snapshot sfx placements", opName);
      }
      const hasAt = raw.at_s !== undefined;
      const hasGain = raw.gain !== undefined;
      if (!hasAt && !hasGain) return reject("missing_required", "patch_sfx requires at_s or gain", opName);
      if ((hasAt && !finiteNumber(raw.at_s)) || (hasGain && !finiteNumber(raw.gain))) {
        return reject("invalid_type", "sfx patch values must be numbers", opName);
      }
      return {
        ok: true,
        op: {
          op: opName,
          sfx_index: raw.sfx_index,
          ...(hasAt ? { at_s: clampAtS(raw.at_s as number, snapshot) } : {}),
          ...(hasGain ? { gain: clamp(raw.gain as number, 0, 2) } : {}),
        },
      };
    }
    case "remove_sfx": {
      if (!integerIndex(raw.sfx_index)) return reject("missing_required", "remove_sfx requires sfx_index", opName);
      if (!hasIndex(snapshot, "sfx", raw.sfx_index)) {
        return reject("invalid_index", "sfx_index must point into snapshot sfx placements", opName);
      }
      return { ok: true, op: { op: opName, sfx_index: raw.sfx_index } };
    }
    case "patch_overlay": {
      if (!integerIndex(raw.overlay_index)) {
        return reject("missing_required", "patch_overlay requires overlay_index", opName);
      }
      if (!hasIndex(snapshot, "overlay", raw.overlay_index)) {
        return reject("invalid_index", "overlay_index must point into snapshot overlay cards", opName);
      }
      const patch = validateOverlayPatch(raw.patch);
      if (!patch.ok) return patch;
      return { ok: true, op: { op: opName, overlay_index: raw.overlay_index, patch: patch.patch } };
    }
    case "remove_overlay": {
      if (!integerIndex(raw.overlay_index)) return reject("missing_required", "remove_overlay requires overlay_index", opName);
      if (!hasIndex(snapshot, "overlay", raw.overlay_index)) {
        return reject("invalid_index", "overlay_index must point into snapshot overlay cards", opName);
      }
      return { ok: true, op: { op: opName, overlay_index: raw.overlay_index } };
    }
    case "add_overlay": {
      if (
        typeof raw.asset_id !== "string" ||
        raw.asset_id.trim() === "" ||
        !nonNegativeNumber(raw.start_s) ||
        !nonNegativeNumber(raw.end_s)
      ) {
        return reject("missing_required", "add_overlay requires asset_id, start_s, and end_s", opName);
      }
      if (raw.end_s <= raw.start_s) return reject("invalid_time", "add_overlay end_s must be after start_s", opName);
      const op: Extract<CopilotOp, { op: "add_overlay" }> = {
        op: opName,
        asset_id: raw.asset_id,
        start_s: raw.start_s,
        end_s: raw.end_s,
      };
      if (raw.position !== undefined) {
        if (typeof raw.position !== "string" || !ALLOWED_OVERLAY_POSITIONS.has(raw.position)) {
          return reject("invalid_value", "position is not supported", opName);
        }
        op.position = raw.position as typeof op.position;
      }
      if (raw.display_mode !== undefined) {
        if (typeof raw.display_mode !== "string" || !ALLOWED_DISPLAY_MODES.has(raw.display_mode)) {
          return reject("invalid_value", "display_mode must be pip or fullscreen", opName);
        }
        op.display_mode = raw.display_mode as typeof op.display_mode;
      }
      for (const key of ["x_frac", "y_frac", "scale"] as const) {
        if (raw[key] === undefined) continue;
        if (!finiteNumber(raw[key])) return reject("invalid_type", `${key} must be a number`, opName);
        op[key] = key === "scale" ? clamp(raw[key], 0.05, 1) : clamp(raw[key], 0, 1);
      }
      return { ok: true, op };
    }
    case "accept_overlay_suggestion": {
      if (typeof raw.suggestion_id !== "string" || raw.suggestion_id.trim() === "") {
        return reject("missing_required", "accept_overlay_suggestion requires suggestion_id", opName);
      }
      return { ok: true, op: { op: opName, suggestion_id: raw.suggestion_id } };
    }
    case "edit_caption": {
      if (!integerIndex(raw.cue_index) || typeof raw.text !== "string") {
        return reject("missing_required", "edit_caption requires cue_index and text", opName);
      }
      if (snapshot?.captions?.cues_editable === false) {
        return reject("invalid_index", "This draft has caption settings but no editable cue list.", opName);
      }
      if (!hasIndex(snapshot, "caption", raw.cue_index)) {
        return reject("invalid_index", "cue_index must point into snapshot caption cues", opName);
      }
      const text = cleanUserText(raw.text, 500);
      if (!text) return reject("invalid_value", "caption text must be non-empty", opName);
      return { ok: true, op: { op: opName, cue_index: raw.cue_index, text } };
    }
    case "set_caption_timing": {
      if (!integerIndex(raw.cue_index)) {
        return reject("missing_required", "set_caption_timing requires cue_index", opName);
      }
      if (snapshot?.captions?.cues_editable === false) {
        return reject("invalid_index", "This draft has caption settings but no editable cue list.", opName);
      }
      if (!hasIndex(snapshot, "caption", raw.cue_index)) {
        return reject("invalid_index", "cue_index must point into snapshot caption cues", opName);
      }
      const hasStart = raw.start_s !== undefined;
      const hasEnd = raw.end_s !== undefined;
      if (!hasStart && !hasEnd) {
        return reject("missing_required", "set_caption_timing requires start_s or end_s", opName);
      }
      if ((hasStart && !nonNegativeNumber(raw.start_s)) || (hasEnd && !nonNegativeNumber(raw.end_s))) {
        return reject("invalid_time", "caption timing values must be non-negative seconds", opName);
      }
      if (hasStart && hasEnd && (raw.end_s as number) <= (raw.start_s as number)) {
        return reject("invalid_time", "caption end_s must be after start_s", opName);
      }
      return {
        ok: true,
        op: {
          op: opName,
          cue_index: raw.cue_index,
          ...(hasStart ? { start_s: raw.start_s as number } : {}),
          ...(hasEnd ? { end_s: raw.end_s as number } : {}),
        },
      };
    }
    case "set_caption_meta": {
      const patch = validateCaptionMetaPatch(raw.patch);
      if (!patch.ok) return patch;
      return { ok: true, op: { op: opName, patch: patch.patch } };
    }
    case "set_caption_emphasis": {
      if (!integerIndex(raw.cue_index) || typeof raw.emphasis !== "boolean") {
        return reject(
          "missing_required",
          "set_caption_emphasis requires cue_index and a boolean emphasis",
          opName,
        );
      }
      if (snapshot?.captions?.cues_editable === false) {
        return reject("invalid_index", "This draft has caption settings but no editable cue list.", opName);
      }
      if (!hasIndex(snapshot, "caption", raw.cue_index)) {
        return reject("invalid_index", "cue_index must point into snapshot caption cues", opName);
      }
      return { ok: true, op: { op: opName, cue_index: raw.cue_index, emphasis: raw.emphasis } };
    }
    case "swap_music": {
      if (typeof raw.track_id !== "string" || raw.track_id.trim() === "") {
        return reject("missing_required", "swap_music requires track_id", opName);
      }
      return { ok: true, op: { op: opName, track_id: raw.track_id } };
    }
    case "set_mix": {
      if (!finiteNumber(raw.music_level)) return reject("missing_required", "set_mix requires music_level", opName);
      return { ok: true, op: { op: opName, music_level: clamp(raw.music_level, 0, 1) } };
    }
    case "set_intro_layout": {
      if (raw.layout === undefined) return reject("missing_required", "set_intro_layout requires layout", opName);
      if (raw.layout !== "linear" && raw.layout !== "cluster") {
        return reject("invalid_value", "layout must be linear or cluster", opName);
      }
      return { ok: true, op: { op: opName, layout: raw.layout } };
    }
    case "set_title": {
      if (typeof raw.title !== "string") return reject("missing_required", "set_title requires title", opName);
      const title = cleanUserText(raw.title, 300);
      if (!title) return reject("invalid_value", "title must be non-empty", opName);
      return { ok: true, op: { op: opName, title } };
    }
    case "add_camera_effect": {
      if (!nonNegativeNumber(raw.start_s) || !nonNegativeNumber(raw.end_s)) {
        return reject("missing_required", "add_camera_effect requires start_s and end_s", opName);
      }
      if (raw.end_s <= raw.start_s) {
        return reject("invalid_time", "camera effect end_s must be after start_s", opName);
      }
      const intensity = raw.intensity === undefined ? 0.04 : raw.intensity;
      if (!finiteNumber(intensity)) {
        return reject("invalid_type", "camera effect intensity must be a number", opName);
      }
      const startS = clampAtS(raw.start_s, snapshot);
      const endS = clampAtS(raw.end_s, snapshot);
      if (endS <= startS) {
        return reject(
          "invalid_time",
          "camera effect timing collapses outside the current timeline",
          opName,
        );
      }
      return {
        ok: true,
        op: {
          op: opName,
          start_s: startS,
          end_s: endS,
          intensity: clamp(intensity, 0.01, 0.08),
        },
      };
    }
    case "patch_camera_effect": {
      if (!integerIndex(raw.camera_effect_index)) {
        return reject("missing_required", "patch_camera_effect requires camera_effect_index", opName);
      }
      if (snapshot?.camera_effects && raw.camera_effect_index >= snapshot.camera_effects.length) {
        return reject("invalid_index", "camera_effect_index is outside the snapshot", opName);
      }
      const hasStart = raw.start_s !== undefined;
      const hasEnd = raw.end_s !== undefined;
      const hasIntensity = raw.intensity !== undefined;
      if (!hasStart && !hasEnd && !hasIntensity) {
        return reject("missing_required", "patch_camera_effect requires a changed field", opName);
      }
      if (
        (hasStart && !nonNegativeNumber(raw.start_s)) ||
        (hasEnd && !nonNegativeNumber(raw.end_s)) ||
        (hasIntensity && !finiteNumber(raw.intensity))
      ) {
        return reject("invalid_type", "camera effect patch values must be numbers", opName);
      }
      return {
        ok: true,
        op: {
          op: opName,
          camera_effect_index: raw.camera_effect_index,
          ...(hasStart ? { start_s: clampAtS(raw.start_s as number, snapshot) } : {}),
          ...(hasEnd ? { end_s: clampAtS(raw.end_s as number, snapshot) } : {}),
          ...(hasIntensity ? { intensity: clamp(raw.intensity as number, 0.01, 0.08) } : {}),
        },
      };
    }
    case "remove_camera_effect": {
      if (!integerIndex(raw.camera_effect_index)) {
        return reject("missing_required", "remove_camera_effect requires camera_effect_index", opName);
      }
      if (snapshot?.camera_effects && raw.camera_effect_index >= snapshot.camera_effects.length) {
        return reject("invalid_index", "camera_effect_index is outside the snapshot", opName);
      }
      return { ok: true, op: { op: opName, camera_effect_index: raw.camera_effect_index } };
    }
    case "set_transition": {
      if (!integerIndex(raw.boundary_index)) {
        return reject("missing_required", "set_transition requires boundary_index", opName);
      }
      const activeSlots = snapshot?.slots?.filter((slot) => !slot.removed);
      if (!activeSlots || raw.boundary_index >= Math.max(0, activeSlots.length - 1)) {
        return reject("invalid_index", "boundary_index must point between two clips", opName);
      }
      const allowed = new Set(["cut", "crossfade", "dip_to_black", "flash"]);
      if (typeof raw.transition !== "string" || !allowed.has(raw.transition)) {
        return reject("invalid_value", "transition is not supported", opName);
      }
      if (raw.duration_s !== undefined && !finiteNumber(raw.duration_s)) {
        return reject("invalid_type", "transition duration_s must be a number", opName);
      }
      const left = activeSlots[raw.boundary_index];
      const right = activeSlots[raw.boundary_index + 1];
      const leftDuration =
        finiteNumber(left?.output_start_s) && finiteNumber(left?.output_end_s)
          ? left.output_end_s - left.output_start_s
          : Infinity;
      const rightDuration =
        finiteNumber(right?.output_start_s) && finiteNumber(right?.output_end_s)
          ? right.output_end_s - right.output_start_s
          : Infinity;
      if (!Number.isFinite(leftDuration) || !Number.isFinite(rightDuration)) {
        return reject(
          "invalid_time",
          "both adjacent clip durations are required for a transition",
          opName,
        );
      }
      const maxDuration = Math.min(0.3, leftDuration * 0.3, rightDuration * 0.3);
      if (raw.transition !== "cut" && maxDuration < 0.1) {
        return reject(
          "invalid_time",
          "adjacent clips are too short for a render-safe transition",
          opName,
        );
      }
      const requestedDuration =
        raw.duration_s === undefined ? 0.3 : Math.max(0.1, raw.duration_s);
      return {
        ok: true,
        op: {
          op: opName,
          boundary_index: raw.boundary_index,
          transition: raw.transition as Extract<CopilotOp, { op: "set_transition" }>["transition"],
          // FFmpeg's render-safe transition contract uses a single 300ms
          // boundary and clamps it further for very short adjacent clips.
          duration_s:
            raw.transition === "cut"
              ? 0.3
              : Math.min(0.3, maxDuration, requestedDuration),
        },
      };
    }
    case "set_visual_fade": {
      if (!integerIndex(raw.visual_block_index)) {
        return reject("missing_required", "set_visual_fade requires visual_block_index", opName);
      }
      if (
        snapshot?.visual_blocks &&
        raw.visual_block_index >= snapshot.visual_blocks.length
      ) {
        return reject("invalid_index", "visual_block_index is outside the snapshot", opName);
      }
      const valid = new Set(["cut", "fade"]);
      const hasIn = raw.transition_in !== undefined;
      const hasOut = raw.transition_out !== undefined;
      if (!hasIn && !hasOut) {
        return reject("missing_required", "set_visual_fade requires an entrance or exit", opName);
      }
      if (
        (hasIn && (typeof raw.transition_in !== "string" || !valid.has(raw.transition_in))) ||
        (hasOut && (typeof raw.transition_out !== "string" || !valid.has(raw.transition_out)))
      ) {
        return reject("invalid_value", "visual fade must be cut or fade", opName);
      }
      return {
        ok: true,
        op: {
          op: opName,
          visual_block_index: raw.visual_block_index,
          ...(hasIn ? { transition_in: raw.transition_in as "cut" | "fade" } : {}),
          ...(hasOut ? { transition_out: raw.transition_out as "cut" | "fade" } : {}),
        },
      };
    }
    case "insert_generated_asset": {
      if (
        typeof raw.asset_id !== "string" ||
        raw.asset_id.trim() === "" ||
        !integerIndex(raw.clip_index) ||
        !nonNegativeNumber(raw.insert_at_s) ||
        !finiteNumber(raw.duration_s)
      ) {
        return reject(
          "missing_required",
          "insert_generated_asset requires asset_id, clip_index, insert_at_s, and duration_s",
          opName,
        );
      }
      if (raw.duration_s <= 0 || raw.duration_s > 10) {
        return reject("invalid_value", "generated asset duration must be 0-10 seconds", opName);
      }
      return {
        ok: true,
        op: {
          op: opName,
          asset_id: raw.asset_id.trim().slice(0, 100),
          clip_index: raw.clip_index,
          insert_at_s: clampAtS(raw.insert_at_s, snapshot),
          duration_s: raw.duration_s,
        },
      };
    }
    case "replace_generated_segment": {
      if (
        typeof raw.asset_id !== "string" ||
        raw.asset_id.trim() === "" ||
        !integerIndex(raw.clip_index) ||
        !integerIndex(raw.source_clip_index) ||
        !nonNegativeNumber(raw.source_start_s) ||
        !nonNegativeNumber(raw.source_end_s) ||
        !finiteNumber(raw.duration_s)
      ) {
        return reject(
          "missing_required",
          "replace_generated_segment requires source and generated clip fields",
          opName,
        );
      }
      if (
        raw.source_end_s <= raw.source_start_s ||
        raw.duration_s <= 0 ||
        raw.duration_s > 10
      ) {
        return reject("invalid_time", "generated replacement timing is invalid", opName);
      }
      const sourceStartS = raw.source_start_s as number;
      const sourceEndS = raw.source_end_s as number;
      const target = snapshot?.slots?.find(
        (slot) =>
          !slot.removed &&
          slot.clip_index === raw.source_clip_index &&
          finiteNumber(slot.in_s) &&
          finiteNumber(slot.duration_s) &&
          Math.abs(slot.in_s - sourceStartS) <= 0.05 &&
          Math.abs(slot.in_s + slot.duration_s - sourceEndS) <= 0.05,
      );
      if (!target) {
        return reject(
          "invalid_index",
          "restyle source no longer matches a complete timeline clip",
          opName,
        );
      }
      return {
        ok: true,
        op: {
          op: opName,
          asset_id: raw.asset_id.trim().slice(0, 100),
          clip_index: raw.clip_index,
          source_clip_index: raw.source_clip_index,
          source_start_s: sourceStartS,
          source_end_s: sourceEndS,
          duration_s: raw.duration_s,
        },
      };
    }
    case "open_tool": {
      if (typeof raw.tool !== "string" || !ALLOWED_TOOLS.has(raw.tool)) {
        return reject("invalid_value", "open_tool.tool is not supported", opName);
      }
      return { ok: true, op: { op: opName, tool: raw.tool as Extract<CopilotOp, { op: "open_tool" }>["tool"] } };
    }
    default:
      return reject("unknown_op", "op name is not in the v1 vocabulary", opName);
  }
}

export function validateCopilotOps(
  rawOps: unknown[],
  snapshot?: CopilotValidationSnapshot,
): { ops: CopilotOp[]; rejected: OpValidationRejection[] } {
  const ops: CopilotOp[] = [];
  const rejected: OpValidationRejection[] = [];
  for (const raw of rawOps) {
    const result = validateCopilotOp(raw, snapshot);
    if (result.ok) ops.push(result.op);
    else rejected.push(result.rejection);
  }
  return { ops, rejected };
}
