import fontRegistryJson from "@/data/font-registry.json";

export type CopilotOpFamily =
  | "text"
  | "clip"
  | "sfx"
  | "overlay"
  | "caption"
  | "music"
  | "render"
  | "title"
  | "tool";

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
  | { op: "swap_music"; track_id: string }
  | { op: "set_mix"; music_level: number }
  | { op: "set_intro_layout"; layout: "linear" | "cluster" }
  | { op: "set_title"; title: string }
  | { op: "open_tool"; tool: "text" | "sounds" | "overlays" | "styles" };

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
    output_start_s?: number | null;
    output_end_s?: number | null;
  }>;
  sfx?: {
    placements?: unknown[];
  };
  overlays?: {
    cards?: unknown[];
    pending_suggestions?: unknown[];
  };
  captions?: {
    cues?: unknown[];
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
  "fade-in",
  "slide-up",
  "karaoke-line",
  "pop-in",
  "scale-up",
  "typewriter",
  "stream-in",
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
  if (!finiteNumber(total)) return Math.max(0, value);
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
    op.op === "split_clip"
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
  if (op.op === "edit_caption" || op.op === "set_caption_timing" || op.op === "set_caption_meta") {
    return "caption";
  }
  if (op.op === "swap_music" || op.op === "set_mix") return "music";
  if (op.op === "set_intro_layout") return "render";
  if (op.op === "set_title") return "title";
  if (op.op === "open_tool") return "tool";
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
      return { ok: true, op: { op: opName, bar_index: raw.bar_index } };
    }
    case "set_clip_duration": {
      if ("duration_beats" in raw || !integerIndex(raw.slot_index) || !finiteNumber(raw.duration_s)) {
        return reject("missing_required", "set_clip_duration requires slot_index and duration_s seconds", opName);
      }
      if (!hasIndex(snapshot, "slot", raw.slot_index)) {
        return reject("invalid_index", "slot_index must point into snapshot slots", opName);
      }
      if (raw.duration_s < 0.6) return reject("invalid_value", "duration_s must be at least 0.6", opName);
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
