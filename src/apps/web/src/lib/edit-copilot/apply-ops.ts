import { isParityVerified } from "@/lib/parity-verified-fields";
import type { TextEditorAction, TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { EditorCapabilities } from "@/lib/plan-api";
import {
  applyClipTimingInput,
  applyTextTimingInput,
  sequentialSlotLayout,
} from "@/app/plan/items/[id]/_editor/editor-bar-drag";
import {
  deleteSlotEnforceFloor,
  splitSlotAt,
} from "@/app/plan/items/[id]/_editor/slot-split";
import {
  copilotOpFamily,
  validateCopilotOp,
  type CopilotOp,
  type TextStylePatch,
  type TextStylePatchKey,
} from "./ops";
import {
  allowedOpFamiliesFromCapabilities,
  type CopilotSnapshot,
  type CopilotSlotSnapshot,
  type CopilotTextSnapshotBar,
} from "./snapshot";

export type RejectedOpReason =
  | "invalid_op"
  | "capability_disabled"
  | "target_missing"
  | "user_changed"
  | "unsupported_field"
  | "no_effect";

export interface ChangeChip {
  label: string;
  from: string;
  to: string;
}

export interface RejectedOp {
  op: string;
  label: string;
  reason: RejectedOpReason;
  detail: string;
}

export interface ApplyCopilotOpsResult {
  textActions: TextEditorAction[];
  nextSlots: DraftSlot[] | null;
  applied: ChangeChip[];
  rejected: RejectedOp[];
}

export interface ApplyCopilotOpsContext {
  bars: TextElementBar[];
  slots: DraftSlot[];
  snapshot: CopilotSnapshot;
  capabilities?: EditorCapabilities | null;
  grid?: number[];
  videoDurationS?: number;
  makeTextBarId?: () => string;
  makeSlotKey?: (slot: DraftSlot) => string;
}

let textIdCounter = 0;
let slotKeyCounter = 0;

function defaultTextBarId(): string {
  textIdCounter += 1;
  return `copilot-text-${textIdCounter}`;
}

function defaultSlotKey(slot: DraftSlot): string {
  slotKeyCounter += 1;
  return `${slot.key}-split-${slotKeyCounter}`;
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function fmt(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? `${value}` : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  if (value === null || value === undefined || value === "") return "default";
  return String(value);
}

function fmtSeconds(value: number): string {
  return `${round(value).toFixed(1)}s`;
}

function textValue(bar: TextElementBar, snap: CopilotTextSnapshotBar, key: TextStylePatchKey | "text" | "start_s" | "end_s"): unknown {
  if (key === "text") return bar.text;
  if (key === "start_s") return round(bar.start_s);
  if (key === "end_s") return round(bar.end_s);
  if (key === "font_family") return bar.font_family ?? "PlayfairDisplay-Bold";
  if (key === "size_px") return bar.size_px ?? snap.size_px;
  if (key === "color") return bar.color ?? "#FFFFFF";
  if (key === "highlight_color") return bar.highlight_color ?? null;
  if (key === "effect") return bar.effect ?? "static";
  if (key === "alignment") return bar.alignment ?? "center";
  if (key === "text_case") return bar.text_case ?? "none";
  if (key === "letter_spacing") return bar.letter_spacing ?? snap.letter_spacing;
  if (key === "line_spacing") return bar.line_spacing ?? snap.line_spacing;
  if (key === "max_width_frac") return bar.max_width_frac ?? snap.max_width_frac;
  if (key === "stroke_width") return bar.stroke_width ?? 0;
  if (key === "position") return bar.position ?? "middle";
  if (key === "x_frac") return bar.x_frac ?? null;
  if (key === "y_frac") return bar.y_frac ?? null;
  return undefined;
}

function sameValue(a: unknown, b: unknown): boolean {
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-6;
  return a === b;
}

function textFingerprintMatches(
  bar: TextElementBar,
  snap: CopilotTextSnapshotBar,
  fields: Array<TextStylePatchKey | "text" | "start_s" | "end_s">,
): boolean {
  return fields.every((field) => sameValue(textValue(bar, snap, field), snap[field]));
}

function slotDuration(slots: DraftSlot[], grid: number[], index: number): number {
  return round(sequentialSlotLayout(slots, grid).windows[index]?.durationS ?? slots[index]?.durationS ?? 0);
}

function currentSlotIndex(slots: DraftSlot[], key: string): number {
  return slots.findIndex((slot) => slot.key === key);
}

function slotFingerprintMatches(
  slots: DraftSlot[],
  grid: number[],
  slot: DraftSlot,
  index: number,
  snap: CopilotSlotSnapshot,
  fields: Array<"in_s" | "duration_s" | "removed" | "output_start_s" | "output_end_s">,
): boolean {
  const layout = sequentialSlotLayout(slots, grid);
  const win = layout.windows[index];
  const values = {
    in_s: round(slot.inS),
    duration_s: round(win?.durationS ?? slot.durationS ?? 0),
    removed: slot.removed,
    output_start_s: win?.startS == null ? null : round(win.startS),
    output_end_s: win?.startS == null ? null : round(win.startS + win.durationS),
  };
  return fields.every((field) => sameValue(values[field], snap[field]));
}

function reject(op: string, label: string, reason: RejectedOpReason, detail: string): RejectedOp {
  return { op, label, reason, detail };
}

function labelForOp(op: CopilotOp): string {
  if (op.op === "edit_text") return `Text ${op.bar_index + 1}`;
  if (op.op === "patch_text_style") return `Text ${op.bar_index + 1} style`;
  if (op.op === "set_text_timing") return `Text ${op.bar_index + 1} timing`;
  if (op.op === "add_text") return "Add text";
  if (op.op === "remove_text") return `Remove text ${op.bar_index + 1}`;
  if (op.op === "set_clip_duration") return `Clip ${op.slot_index + 1} duration`;
  if (op.op === "set_clip_in") return `Clip ${op.slot_index + 1} in`;
  if (op.op === "reorder_clip") return `Move clip ${op.from_index + 1}`;
  if (op.op === "remove_clip") return `Remove clip ${op.slot_index + 1}`;
  if (op.op === "split_clip") return `Split clip ${op.slot_index + 1}`;
  const _exhaustive: never = op;
  return _exhaustive;
}

function textSnapAt(snapshot: CopilotSnapshot, index: number): CopilotTextSnapshotBar | null {
  return snapshot.text_bars[index] ?? null;
}

function slotSnapAt(snapshot: CopilotSnapshot, index: number): CopilotSlotSnapshot | null {
  return snapshot.slots[index] ?? null;
}

function textBarForSnap(bars: TextElementBar[], snap: CopilotTextSnapshotBar): TextElementBar | null {
  return bars.find((bar) => bar.id === snap.id) ?? null;
}

function applyStylePatch(
  rawPatch: TextStylePatch,
): { patch: TextStylePatch; stripped: string[] } {
  const patch: TextStylePatch = {};
  const stripped: string[] = [];
  for (const [key, value] of Object.entries(rawPatch) as Array<[TextStylePatchKey, unknown]>) {
    if (!isParityVerified(key)) {
      stripped.push(key);
      continue;
    }
    (patch as Record<string, unknown>)[key] = value;
  }
  if (Object.prototype.hasOwnProperty.call(patch, "size_px")) {
    (patch as TextStylePatch & { size_class?: undefined }).size_class = undefined;
  }
  return { patch, stripped };
}

function slotOrderMatches(slots: DraftSlot[], snapshot: CopilotSnapshot): boolean {
  return snapshot.slots.every((snapSlot, index) => slots[index]?.key === snapSlot.key);
}

export function applyCopilotOps(
  rawOps: readonly unknown[],
  ctx: ApplyCopilotOpsContext,
): ApplyCopilotOpsResult {
  const textActions: TextEditorAction[] = [];
  const applied: ChangeChip[] = [];
  const rejected: RejectedOp[] = [];
  const grid = ctx.grid ?? [];
  const videoDurationS = ctx.videoDurationS ?? Math.max(60, ctx.snapshot.total_duration_s);
  const allowedFamilies = new Set(
    ctx.capabilities
      ? allowedOpFamiliesFromCapabilities(ctx.capabilities)
      : ctx.snapshot.allowed_op_families,
  );
  let nextSlots: DraftSlot[] | null = null;
  let workingSlots = ctx.slots;

  function currentSlots(): DraftSlot[] {
    return nextSlots ?? workingSlots;
  }

  for (const raw of rawOps) {
    const validation = validateCopilotOp(raw, ctx.snapshot);
    if (!validation.ok) {
      rejected.push(reject(validation.rejection.op ?? "unknown", validation.rejection.op ?? "Unknown op", "invalid_op", validation.rejection.message));
      continue;
    }

    const op = validation.op;
    const family = copilotOpFamily(op);
    if (family && !allowedFamilies.has(family)) {
      rejected.push(reject(op.op, labelForOp(op), "capability_disabled", `${family} edits are disabled for this variant`));
      continue;
    }
    if (op.op === "split_clip" && ctx.capabilities?.split_clips === false) {
      rejected.push(reject(op.op, labelForOp(op), "capability_disabled", "clip splitting is disabled for this variant"));
      continue;
    }

    if (op.op === "edit_text") {
      const snap = textSnapAt(ctx.snapshot, op.bar_index);
      const bar = snap ? textBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "text bar no longer exists"));
        continue;
      }
      if (!textFingerprintMatches(bar, snap, ["text"])) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "text was changed after Nova read it"));
        continue;
      }
      textActions.push({ type: "EDIT_TEXT", id: bar.id, text: op.text });
      applied.push({ label: `Text ${op.bar_index + 1}`, from: bar.text, to: op.text });
    } else if (op.op === "patch_text_style") {
      const snap = textSnapAt(ctx.snapshot, op.bar_index);
      const bar = snap ? textBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "text bar no longer exists"));
        continue;
      }
      const { patch, stripped } = applyStylePatch(op.patch);
      const patchKeys = Object.keys(patch) as TextStylePatchKey[];
      if (patchKeys.length === 0) {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", `style fields are not parity verified: ${stripped.join(", ")}`));
        continue;
      }
      if (!textFingerprintMatches(bar, snap, patchKeys)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "style was changed after Nova read it"));
        continue;
      }
      textActions.push({ type: "PATCH_BAR", id: bar.id, patch });
      for (const key of patchKeys) {
        applied.push({
          label: key === "size_px" ? "Size" : key,
          from: fmt(textValue(bar, snap, key)),
          to: fmt(patch[key]),
        });
      }
    } else if (op.op === "set_text_timing") {
      const snap = textSnapAt(ctx.snapshot, op.bar_index);
      const bar = snap ? textBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "text bar no longer exists"));
        continue;
      }
      const fields: Array<"start_s" | "end_s"> = [
        ...(op.start_s !== undefined ? (["start_s"] as const) : []),
        ...(op.end_s !== undefined ? (["end_s"] as const) : []),
      ];
      if (!textFingerprintMatches(bar, snap, fields)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "timing was changed after Nova read it"));
        continue;
      }
      const next = applyTextTimingInput({
        startS: op.start_s ?? bar.start_s,
        endS: op.end_s ?? bar.end_s,
        videoDurationS,
      });
      textActions.push({ type: "PATCH_BAR", id: bar.id, patch: next });
      applied.push({
        label: `Text ${op.bar_index + 1} timing`,
        from: `${fmtSeconds(bar.start_s)}-${fmtSeconds(bar.end_s)}`,
        to: `${fmtSeconds(next.start_s)}-${fmtSeconds(next.end_s)}`,
      });
    } else if (op.op === "add_text") {
      const timing = applyTextTimingInput({
        startS: op.start_s,
        endS: op.end_s,
        videoDurationS,
      });
      const bar: TextElementBar = {
        id: ctx.makeTextBarId?.() ?? defaultTextBarId(),
        text: op.text,
        start_s: timing.start_s,
        end_s: timing.end_s,
        role: "generative_intro",
        font_family: "Playfair Display",
        size_px: 72,
        color: "#FFFFFF",
        effect: "static",
        alignment: "center",
        position: "middle",
      };
      textActions.push({ type: "ADD_TEXT", bar });
      applied.push({ label: "Add text", from: "none", to: op.text });
    } else if (op.op === "remove_text") {
      const snap = textSnapAt(ctx.snapshot, op.bar_index);
      const bar = snap ? textBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "text bar no longer exists"));
        continue;
      }
      if (!textFingerprintMatches(bar, snap, ["text"])) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "text was changed after Nova read it"));
        continue;
      }
      textActions.push({ type: "DELETE_BAR", id: bar.id });
      applied.push({ label: `Remove text ${op.bar_index + 1}`, from: bar.text, to: "removed" });
    } else if (op.op === "set_clip_duration") {
      const snap = slotSnapAt(ctx.snapshot, op.slot_index);
      const slots = currentSlots();
      const index = snap ? currentSlotIndex(slots, snap.key) : -1;
      const slot = index >= 0 ? slots[index] : null;
      if (!snap || !slot) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "clip slot no longer exists"));
        continue;
      }
      if (!slotFingerprintMatches(slots, grid, slot, index, snap, ["duration_s"])) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip duration was changed after Nova read it"));
        continue;
      }
      const patch = applyClipTimingInput({
        inS: slot.inS,
        durationS: op.duration_s,
        sourceDurationS: snap.source_duration_s,
      });
      const before = slotDuration(slots, grid, index);
      workingSlots = slots.map((s) => (s.key === slot.key ? { ...s, ...patch } : s));
      nextSlots = workingSlots;
      applied.push({ label: `Clip ${op.slot_index + 1}`, from: fmtSeconds(before), to: fmtSeconds(patch.durationS ?? before) });
    } else if (op.op === "set_clip_in") {
      const snap = slotSnapAt(ctx.snapshot, op.slot_index);
      const slots = currentSlots();
      const index = snap ? currentSlotIndex(slots, snap.key) : -1;
      const slot = index >= 0 ? slots[index] : null;
      if (!snap || !slot) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "clip slot no longer exists"));
        continue;
      }
      if (!slotFingerprintMatches(slots, grid, slot, index, snap, ["in_s"])) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip in-point was changed after Nova read it"));
        continue;
      }
      const duration = slotDuration(slots, grid, index);
      const patch = applyClipTimingInput({
        inS: op.in_s,
        durationS: duration,
        sourceDurationS: snap.source_duration_s,
      });
      workingSlots = slots.map((s) => (s.key === slot.key ? { ...s, ...patch } : s));
      nextSlots = workingSlots;
      applied.push({ label: `Clip ${op.slot_index + 1} in`, from: fmtSeconds(slot.inS), to: fmtSeconds(patch.inS) });
    } else if (op.op === "reorder_clip") {
      const slots = currentSlots();
      const fromSnap = slotSnapAt(ctx.snapshot, op.from_index);
      const toSnap = slotSnapAt(ctx.snapshot, op.to_index);
      if (!fromSnap || !toSnap || !slots.some((slot) => slot.key === fromSnap.key)) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "clip slot no longer exists"));
        continue;
      }
      if (!slotOrderMatches(slots, ctx.snapshot)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip order was changed after Nova read it"));
        continue;
      }
      const from = currentSlotIndex(slots, fromSnap.key);
      const to = currentSlotIndex(slots, toSnap.key);
      const reordered = [...slots];
      const [moved] = reordered.splice(from, 1);
      reordered.splice(to, 0, moved);
      sequentialSlotLayout(reordered, grid);
      workingSlots = reordered;
      nextSlots = reordered;
      applied.push({ label: "Clip order", from: `${op.from_index + 1}`, to: `${op.to_index + 1}` });
    } else if (op.op === "remove_clip") {
      const snap = slotSnapAt(ctx.snapshot, op.slot_index);
      const slots = currentSlots();
      const index = snap ? currentSlotIndex(slots, snap.key) : -1;
      const slot = index >= 0 ? slots[index] : null;
      if (!snap || !slot) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "clip slot no longer exists"));
        continue;
      }
      if (!slotFingerprintMatches(slots, grid, slot, index, snap, ["removed"])) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip was changed after Nova read it"));
        continue;
      }
      const res = deleteSlotEnforceFloor(slots, slot.key);
      if (!res.didDelete) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "clip could not be removed"));
        continue;
      }
      workingSlots = res.slots;
      nextSlots = res.slots;
      applied.push({ label: `Clip ${op.slot_index + 1}`, from: "present", to: "removed" });
    } else if (op.op === "split_clip") {
      const snap = slotSnapAt(ctx.snapshot, op.slot_index);
      const slots = currentSlots();
      const index = snap ? currentSlotIndex(slots, snap.key) : -1;
      const slot = index >= 0 ? slots[index] : null;
      if (!snap || !slot) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "clip slot no longer exists"));
        continue;
      }
      if (!slotFingerprintMatches(slots, grid, slot, index, snap, ["in_s", "duration_s", "output_start_s", "output_end_s"])) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip was changed after Nova read it"));
        continue;
      }
      const res = splitSlotAt(slots, grid, slot.key, op.at_s, ctx.makeSlotKey?.(slot) ?? defaultSlotKey(slot));
      if (!res.didSplit) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "clip could not be split at that time"));
        continue;
      }
      workingSlots = res.slots;
      nextSlots = res.slots;
      applied.push({ label: `Split clip ${op.slot_index + 1}`, from: "one clip", to: "two clips" });
    }
  }

  return { textActions, nextSlots, applied, rejected };
}
