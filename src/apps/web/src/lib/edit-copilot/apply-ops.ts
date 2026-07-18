import { isParityVerified } from "@/lib/parity-verified-fields";
import type { TextEditorAction, TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type {
  EditorCapabilities,
  MediaOverlay,
  OverlaySuggestion,
  PoolAsset,
  SoundEffectPlacement,
} from "@/lib/plan-api";
import type { AcceptedSuggestionRef } from "@/lib/editor-commit";
import type { SoundEffectSummary } from "@/lib/sfx-api";
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
  type CaptionMetaPatch,
  type CopilotOp,
  type OverlayPatchKey,
  type TextStylePatch,
  type TextStylePatchKey,
} from "./ops";
import {
  roundCopilotNumber,
  type CopilotSnapshot,
  type CopilotCaptionCueSnapshot,
  type CopilotSlotSnapshot,
  type CopilotTextSnapshotBar,
  type CopilotSfxPlacementSnapshot,
  type CopilotOverlayCardSnapshot,
  type CopilotCaptionMetaSnapshot,
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
  /** How many identical changes this chip aggregates (e.g. same effect on 4 bars). */
  count?: number;
}

/**
 * Receipt hygiene: drop no-op chips (from === to — e.g. a size patch that also
 * clears size_class emits "default → default") and collapse identical chips
 * from multi-bar ops into one chip with a count.
 */
export function consolidateChips(chips: ChangeChip[]): ChangeChip[] {
  const out: ChangeChip[] = [];
  const seen = new Map<string, ChangeChip>();
  for (const chip of chips) {
    if (chip.from === chip.to) continue;
    const key = `${chip.label}\u0000${chip.from}\u0000${chip.to}`;
    const existing = seen.get(key);
    if (existing) {
      existing.count = (existing.count ?? 1) + 1;
    } else {
      const copy = { ...chip };
      seen.set(key, copy);
      out.push(copy);
    }
  }
  return out;
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
  nextSfx?: SoundEffectPlacement[] | null;
  nextOverlays?: MediaOverlay[] | null;
  acceptedSuggestionRefs?: AcceptedSuggestionRef[];
  nextMusicTrackId?: string;
  nextMixLevel?: number;
  renderRequest?: { kind: "set_intro_layout"; layout: "linear" | "cluster" };
  nextTitle?: string;
  captionMetaPatch?: CaptionMetaPatch;
  openTool?: "text" | "visuals" | "sounds" | "overlays" | "styles";
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
  sfx?: SoundEffectPlacement[];
  sfxCatalog?: SoundEffectSummary[];
  overlays?: MediaOverlay[];
  poolAssets?: PoolAsset[];
  pendingSuggestions?: OverlaySuggestion[];
  musicTrackId?: string | null;
  mixLevel?: number | null;
  title?: string;
  captionMeta?: CopilotCaptionMetaSnapshot | null;
  makeTextBarId?: () => string;
  makeSlotKey?: (slot: DraftSlot) => string;
  makeSfxPlacementId?: () => string;
  makeOverlayId?: () => string;
}

let textIdCounter = 0;
let slotKeyCounter = 0;
let sfxIdCounter = 0;
let overlayIdCounter = 0;

function defaultTextBarId(): string {
  textIdCounter += 1;
  return `copilot-text-${textIdCounter}`;
}

function defaultSlotKey(slot: DraftSlot): string {
  slotKeyCounter += 1;
  return `${slot.key}-split-${slotKeyCounter}`;
}

function round(value: number): number {
  return roundCopilotNumber(value);
}

function defaultSfxPlacementId(): string {
  sfxIdCounter += 1;
  return globalThis.crypto?.randomUUID?.() ?? `copilot-sfx-${sfxIdCounter}`;
}

function defaultOverlayId(): string {
  overlayIdCounter += 1;
  return globalThis.crypto?.randomUUID?.() ?? `copilot-overlay-${overlayIdCounter}`;
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
  // Truncate like the snapshot builder does — a fractional size_px (possible
  // via a prior copilot float patch) must not permanently fail fingerprints
  // against the truncated snapshot value (review A7).
  if (key === "size_px") return Math.trunc(bar.size_px ?? snap.size_px);
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
  if (op.op === "add_sfx") return "Add sound";
  if (op.op === "patch_sfx") return `Sound ${op.sfx_index + 1}`;
  if (op.op === "remove_sfx") return `Remove sound ${op.sfx_index + 1}`;
  if (op.op === "patch_overlay") return `Overlay ${op.overlay_index + 1}`;
  if (op.op === "remove_overlay") return `Remove overlay ${op.overlay_index + 1}`;
  if (op.op === "add_overlay") return "Add overlay";
  if (op.op === "accept_overlay_suggestion") return "Accept overlay suggestion";
  if (op.op === "edit_caption") return `Caption ${op.cue_index + 1} edited`;
  if (op.op === "set_caption_timing") return `Caption ${op.cue_index + 1} timing`;
  if (op.op === "set_caption_meta") return "Captions";
  if (op.op === "swap_music") return "Swapped song";
  if (op.op === "set_mix") return "Music volume";
  if (op.op === "set_intro_layout") return "Intro layout";
  if (op.op === "set_title") return "Title set";
  if (op.op === "open_tool") return `Opened ${op.tool[0].toUpperCase()}${op.tool.slice(1)}`;
  const _exhaustive: never = op;
  return _exhaustive;
}

function textSnapAt(snapshot: CopilotSnapshot, index: number): CopilotTextSnapshotBar | null {
  return snapshot.text_bars[index] ?? null;
}

function slotSnapAt(snapshot: CopilotSnapshot, index: number): CopilotSlotSnapshot | null {
  return snapshot.slots[index] ?? null;
}

function sfxSnapAt(snapshot: CopilotSnapshot, index: number): CopilotSfxPlacementSnapshot | null {
  return snapshot.sfx?.placements[index] ?? null;
}

function overlaySnapAt(snapshot: CopilotSnapshot, index: number): CopilotOverlayCardSnapshot | null {
  return snapshot.overlays?.cards[index] ?? null;
}

function captionSnapAt(snapshot: CopilotSnapshot, index: number): CopilotCaptionCueSnapshot | null {
  return snapshot.captions?.cues[index] ?? null;
}

function textBarForSnap(bars: TextElementBar[], snap: CopilotTextSnapshotBar): TextElementBar | null {
  return bars.find((bar) => bar.id === snap.id) ?? null;
}

function captionBarForSnap(bars: TextElementBar[], snap: CopilotCaptionCueSnapshot): TextElementBar | null {
  return bars.find((bar) => bar.id === snap.id && bar.role === "narrated_caption") ?? null;
}

function sfxForSnap(placements: SoundEffectPlacement[], snap: CopilotSfxPlacementSnapshot): SoundEffectPlacement | null {
  return placements.find((placement) => placement.id === snap.id) ?? null;
}

function overlayForSnap(cards: MediaOverlay[], snap: CopilotOverlayCardSnapshot): MediaOverlay | null {
  return cards.find((card) => card.id === snap.id) ?? null;
}

function sfxValue(
  placement: SoundEffectPlacement,
  snap: CopilotSfxPlacementSnapshot,
  key: "at_s" | "gain",
): unknown {
  if (key === "at_s") return round(placement.at_s);
  if (key === "gain") return round(placement.gain ?? snap.gain);
  return undefined;
}

function sfxFingerprintMatches(
  placement: SoundEffectPlacement,
  snap: CopilotSfxPlacementSnapshot,
  fields: Array<"at_s" | "gain">,
): boolean {
  return fields.every((field) => sameValue(sfxValue(placement, snap, field), snap[field]));
}

function overlayValue(
  card: MediaOverlay,
  snap: CopilotOverlayCardSnapshot,
  key: OverlayPatchKey,
): unknown {
  if (key === "display_mode") return card.display_mode ?? "pip";
  if (key === "position") return card.position;
  if (key === "start_s" || key === "end_s" || key === "x_frac" || key === "y_frac" || key === "scale") {
    return round(card[key] ?? snap[key]);
  }
  return undefined;
}

function overlayFingerprintMatches(
  card: MediaOverlay,
  snap: CopilotOverlayCardSnapshot,
  fields: OverlayPatchKey[],
): boolean {
  return fields.every((field) => sameValue(overlayValue(card, snap, field), snap[field]));
}

function captionMetaValue(
  meta: CopilotCaptionMetaSnapshot | null | undefined,
  key: keyof CopilotCaptionMetaSnapshot,
): unknown {
  if (!meta) return undefined;
  if (key === "y_frac") return round(meta.y_frac);
  return meta[key];
}

function captionMetaFingerprintMatches(
  meta: CopilotCaptionMetaSnapshot | null | undefined,
  snap: CopilotCaptionMetaSnapshot,
  fields: Array<keyof CopilotCaptionMetaSnapshot>,
): boolean {
  return fields.every((field) => sameValue(captionMetaValue(meta, field), snap[field]));
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
  const allowedFamilies = new Set(ctx.snapshot.allowed_op_families);
  let nextSlots: DraftSlot[] | null = null;
  let workingSlots = ctx.slots;
  let nextSfx: SoundEffectPlacement[] | undefined;
  let workingSfx = ctx.sfx ?? [];
  let nextOverlays: MediaOverlay[] | undefined;
  let workingOverlays = ctx.overlays ?? [];
  let acceptedSuggestionRefs: AcceptedSuggestionRef[] | undefined;
  let nextMusicTrackId: string | undefined;
  let nextMixLevel: number | undefined;
  let renderRequest: ApplyCopilotOpsResult["renderRequest"];
  let nextTitle: string | undefined;
  let captionMetaPatch: CaptionMetaPatch | undefined;
  let openTool: ApplyCopilotOpsResult["openTool"];

  function currentSlots(): DraftSlot[] {
    return nextSlots ?? workingSlots;
  }

  function currentSfx(): SoundEffectPlacement[] {
    return nextSfx ?? workingSfx;
  }

  function currentOverlays(): MediaOverlay[] {
    return nextOverlays ?? workingOverlays;
  }

  function hasDraftMutation(): boolean {
    return (
      textActions.length > 0 ||
      nextSlots !== null ||
      nextSfx !== undefined ||
      nextOverlays !== undefined ||
      (acceptedSuggestionRefs?.length ?? 0) > 0 ||
      nextMusicTrackId !== undefined ||
      nextMixLevel !== undefined ||
      nextTitle !== undefined ||
      captionMetaPatch !== undefined ||
      openTool !== undefined
    );
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
    } else if (op.op === "add_sfx") {
      const snapCatalogEntry = ctx.snapshot.sfx?.catalog.find((effect) => effect.id === op.effect_id) ?? null;
      const catalogEntry = (ctx.sfxCatalog ?? ctx.snapshot.sfx?.catalog ?? []).find((effect) => effect.id === op.effect_id) ?? null;
      if (!snapCatalogEntry || !catalogEntry) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "sound effect is no longer available"));
        continue;
      }
      const label = "name" in catalogEntry ? catalogEntry.name : snapCatalogEntry.name;
      const duration = catalogEntry.duration_s ?? snapCatalogEntry.duration_s ?? null;
      const placement: SoundEffectPlacement = {
        id: ctx.makeSfxPlacementId?.() ?? defaultSfxPlacementId(),
        sound_effect_id: op.effect_id,
        src_gcs_path: "",
        at_s: op.at_s,
        gain: op.gain,
        duration_s: duration,
        label,
      };
      workingSfx = [...currentSfx(), placement];
      nextSfx = workingSfx;
      applied.push({ label: `Added "${label}"`, from: "none", to: fmtSeconds(op.at_s) });
    } else if (op.op === "patch_sfx") {
      const snap = sfxSnapAt(ctx.snapshot, op.sfx_index);
      const placements = currentSfx();
      const placement = snap ? sfxForSnap(placements, snap) : null;
      if (!snap || !placement) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "sound placement no longer exists"));
        continue;
      }
      const fields = [
        ...(op.at_s !== undefined ? (["at_s"] as const) : []),
        ...(op.gain !== undefined ? (["gain"] as const) : []),
      ];
      if (!sfxFingerprintMatches(placement, snap, fields)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "sound placement changed after Nova read it"));
        continue;
      }
      const patch: Partial<SoundEffectPlacement> = {
        ...(op.at_s !== undefined ? { at_s: op.at_s } : {}),
        ...(op.gain !== undefined ? { gain: op.gain } : {}),
      };
      workingSfx = placements.map((sfx) => (sfx.id === placement.id ? { ...sfx, ...patch } : sfx));
      nextSfx = workingSfx;
      for (const field of fields) {
        applied.push({
          label: field === "at_s" ? "Moved sound" : "Sound volume",
          from: field === "at_s" ? fmtSeconds(placement.at_s) : fmt(placement.gain),
          to: field === "at_s" ? fmtSeconds(op.at_s ?? placement.at_s) : fmt(op.gain ?? placement.gain),
        });
      }
    } else if (op.op === "remove_sfx") {
      const snap = sfxSnapAt(ctx.snapshot, op.sfx_index);
      const placements = currentSfx();
      const placement = snap ? sfxForSnap(placements, snap) : null;
      if (!snap || !placement) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "sound placement no longer exists"));
        continue;
      }
      workingSfx = placements.filter((sfx) => sfx.id !== placement.id);
      nextSfx = workingSfx;
      applied.push({ label: `Removed "${placement.label ?? snap.label ?? "sound"}"`, from: "present", to: "removed" });
    } else if (op.op === "patch_overlay") {
      const snap = overlaySnapAt(ctx.snapshot, op.overlay_index);
      const overlays = currentOverlays();
      const card = snap ? overlayForSnap(overlays, snap) : null;
      if (!snap || !card) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "overlay no longer exists"));
        continue;
      }
      const patchKeys = Object.keys(op.patch) as OverlayPatchKey[];
      if (!overlayFingerprintMatches(card, snap, patchKeys)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "overlay changed after Nova read it"));
        continue;
      }
      workingOverlays = overlays.map((overlay) => (overlay.id === card.id ? { ...overlay, ...op.patch } : overlay));
      nextOverlays = workingOverlays;
      applied.push({
        label: patchKeys.some((key) => key === "start_s" || key === "end_s") ? "Moved overlay" : "Overlay updated",
        from: "previous",
        to: "updated",
      });
    } else if (op.op === "remove_overlay") {
      const snap = overlaySnapAt(ctx.snapshot, op.overlay_index);
      const overlays = currentOverlays();
      const card = snap ? overlayForSnap(overlays, snap) : null;
      if (!snap || !card) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "overlay no longer exists"));
        continue;
      }
      workingOverlays = overlays.filter((overlay) => overlay.id !== card.id);
      nextOverlays = workingOverlays;
      applied.push({ label: "Removed overlay", from: "present", to: "removed" });
    } else if (op.op === "add_overlay") {
      const snapAsset = ctx.snapshot.overlays?.asset_pool.find((asset) => asset.id === op.asset_id) ?? null;
      const asset = (ctx.poolAssets ?? []).find((poolAsset) => poolAsset.id === op.asset_id && poolAsset.status === "ready") ?? null;
      if (!snapAsset || !asset) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "overlay asset is no longer available"));
        continue;
      }
      const overlays = currentOverlays();
      const card: MediaOverlay = {
        id: ctx.makeOverlayId?.() ?? defaultOverlayId(),
        kind: asset.kind,
        src_gcs_path: asset.gcs_path,
        preview_url: asset.display_url,
        preview_gcs_path: null,
        position: op.position ?? "custom",
        x_frac: op.x_frac ?? 0.5,
        y_frac: op.y_frac ?? 0.5,
        scale: op.scale ?? 0.35,
        display_mode: op.display_mode ?? "pip",
        start_s: op.start_s,
        end_s: op.end_s,
        z: overlays.length,
      };
      workingOverlays = [...overlays, card];
      nextOverlays = workingOverlays;
      applied.push({ label: "Added overlay", from: "none", to: asset.subject ?? asset.source_filename ?? asset.id });
    } else if (op.op === "accept_overlay_suggestion") {
      const snapSuggestion = ctx.snapshot.overlays?.pending_suggestions.find((suggestion) => suggestion.id === op.suggestion_id) ?? null;
      const suggestion = (ctx.pendingSuggestions ?? []).find((pending) => pending.id === op.suggestion_id) ?? null;
      if (!snapSuggestion || !suggestion) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "overlay suggestion is no longer available"));
        continue;
      }
      const overlays = currentOverlays();
      workingOverlays = [...overlays, { ...suggestion.overlay }];
      nextOverlays = workingOverlays;
      acceptedSuggestionRefs = acceptedSuggestionRefs ?? [];
      if (!acceptedSuggestionRefs.some((ref) => ref.id === suggestion.id)) {
        acceptedSuggestionRefs.push({ id: suggestion.id, overlayId: suggestion.overlay.id });
      }
      if (suggestion.sfx && allowedFamilies.has("sfx")) {
        workingSfx = [...currentSfx(), { ...suggestion.sfx }];
        nextSfx = workingSfx;
      }
      applied.push({ label: "Accepted overlay suggestion", from: "pending", to: snapSuggestion.reason || "accepted" });
    } else if (op.op === "edit_caption") {
      const snap = captionSnapAt(ctx.snapshot, op.cue_index);
      const bar = snap ? captionBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "caption cue no longer exists"));
        continue;
      }
      if (bar.text !== snap.text) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "caption text was changed after Nova read it"));
        continue;
      }
      textActions.push({ type: "EDIT_TEXT", id: bar.id, text: op.text });
      applied.push({ label: `Caption ${op.cue_index + 1} edited`, from: bar.text, to: op.text });
    } else if (op.op === "set_caption_timing") {
      const snap = captionSnapAt(ctx.snapshot, op.cue_index);
      const bar = snap ? captionBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "caption cue no longer exists"));
        continue;
      }
      const fields: Array<"start_s" | "end_s"> = [
        ...(op.start_s !== undefined ? (["start_s"] as const) : []),
        ...(op.end_s !== undefined ? (["end_s"] as const) : []),
      ];
      const matches = fields.every((field) => sameValue(round(bar[field]), snap[field]));
      if (!matches) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "caption timing was changed after Nova read it"));
        continue;
      }
      const next = applyTextTimingInput({
        startS: op.start_s ?? bar.start_s,
        endS: op.end_s ?? bar.end_s,
        videoDurationS,
      });
      textActions.push({ type: "PATCH_BAR", id: bar.id, patch: next });
      applied.push({
        label: `Caption ${op.cue_index + 1} timing`,
        from: `${fmtSeconds(bar.start_s)}-${fmtSeconds(bar.end_s)}`,
        to: `${fmtSeconds(next.start_s)}-${fmtSeconds(next.end_s)}`,
      });
    } else if (op.op === "set_caption_meta") {
      const snap = ctx.snapshot.captions?.meta ?? null;
      if (!snap || !ctx.captionMeta) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "caption metadata no longer exists"));
        continue;
      }
      const patchKeys = Object.keys(op.patch) as Array<keyof CopilotCaptionMetaSnapshot>;
      if (!captionMetaFingerprintMatches(ctx.captionMeta, snap, patchKeys)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "caption settings changed after Nova read them"));
        continue;
      }
      captionMetaPatch = { ...(captionMetaPatch ?? {}), ...op.patch };
      for (const key of patchKeys) {
        applied.push({
          label: key === "style" && op.patch.style === "word" ? "Captions: word-by-word" : "Captions",
          from: fmt(ctx.captionMeta[key]),
          to: fmt(op.patch[key]),
        });
      }
    } else if (op.op === "swap_music") {
      const music = ctx.snapshot.music;
      if (!music?.swappable || !music.candidates.some((track) => track.id === op.track_id)) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "song is no longer available"));
        continue;
      }
      if ((ctx.musicTrackId ?? null) !== music.current_track_id) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "song changed after Nova read it"));
        continue;
      }
      if (op.track_id === (ctx.musicTrackId ?? null)) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "song is already selected"));
        continue;
      }
      nextMusicTrackId = op.track_id;
      applied.push({ label: "Swapped song", from: music.current_track_title ?? "current", to: music.candidates.find((t) => t.id === op.track_id)?.title ?? op.track_id });
    } else if (op.op === "set_mix") {
      if (!ctx.snapshot.mix) {
        rejected.push(reject(op.op, labelForOp(op), "capability_disabled", "music mix is disabled for this variant"));
        continue;
      }
      if (!sameValue(ctx.mixLevel ?? null, ctx.snapshot.mix.music_level)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "music volume changed after Nova read it"));
        continue;
      }
      if (sameValue(op.music_level, ctx.mixLevel ?? null)) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "music volume is already set"));
        continue;
      }
      nextMixLevel = op.music_level;
      applied.push({ label: `Music volume ${Math.round(op.music_level * 100)}%`, from: fmt(ctx.mixLevel), to: fmt(op.music_level) });
    } else if (op.op === "set_intro_layout") {
      if (!ctx.snapshot.intro) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "intro layout is not available"));
        continue;
      }
      if (renderRequest || hasDraftMutation() || rawOps.length > 1) {
        rejected.push(reject(
          op.op,
          labelForOp(op),
          "capability_disabled",
          "a layout change re-renders the video — ask for it on its own",
        ));
        continue;
      }
      if (op.layout === ctx.snapshot.intro.layout) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "intro already uses this layout"));
        continue;
      }
      if (op.layout === "cluster" && !ctx.snapshot.intro.cluster_eligible) {
        rejected.push(reject(op.op, labelForOp(op), "invalid_op", "the editorial layout needs a 3-6 word hook"));
        continue;
      }
      const label = (layout: "linear" | "cluster") => (layout === "cluster" ? "Editorial" : "Classic");
      renderRequest = { kind: "set_intro_layout", layout: op.layout };
      applied.push({
        label: "Intro layout",
        from: label(ctx.snapshot.intro.layout),
        to: `${label(op.layout)} (re-rendering)`,
      });
    } else if (op.op === "set_title") {
      if (ctx.snapshot.title === undefined || ctx.title === undefined) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "title is no longer available"));
        continue;
      }
      if (ctx.title !== ctx.snapshot.title) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "title changed after Nova read it"));
        continue;
      }
      if (op.title === ctx.title) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "title is already set"));
        continue;
      }
      nextTitle = op.title;
      applied.push({ label: "Title set", from: ctx.title, to: op.title });
    } else if (op.op === "open_tool") {
      if (!ctx.snapshot.open_tools?.includes(op.tool)) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "tool is not available"));
        continue;
      }
      openTool = op.tool;
      applied.push({ label: `Opened ${op.tool[0].toUpperCase()}${op.tool.slice(1)}`, from: "closed", to: "open" });
    }
  }

  return {
    textActions,
    nextSlots,
    nextSfx,
    nextOverlays,
    acceptedSuggestionRefs,
    nextMusicTrackId,
    nextMixLevel,
    renderRequest,
    nextTitle,
    captionMetaPatch,
    openTool,
    applied: consolidateChips(applied),
    rejected,
  };
}
