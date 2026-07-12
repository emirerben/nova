import fontRegistryJson from "@/data/font-registry.json";

export type CopilotOpFamily = "text" | "clip";

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
  | { op: "split_clip"; slot_index: number; at_s: number };

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

export interface CopilotValidationSnapshot {
  text_bars?: unknown[];
  slots?: Array<{
    output_start_s?: number | null;
    output_end_s?: number | null;
  }>;
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
const HEX_COLOR = /^#[0-9A-Fa-f]{6}$/;
const STYLE_PATCH_KEY_SET = new Set<string>(TEXT_STYLE_PATCH_KEYS);

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

function hasIndex(snapshot: CopilotValidationSnapshot | undefined, kind: "text" | "slot", index: number) {
  const arr = kind === "text" ? snapshot?.text_bars : snapshot?.slots;
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
