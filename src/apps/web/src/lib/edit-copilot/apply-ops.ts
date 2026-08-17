import { isParityVerified } from "@/lib/parity-verified-fields";
import type { TextEditorAction, TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { slotWindows, type DraftSlot } from "@/app/generative/timeline-math";
import type {
  CameraEffect,
  CarouselMoment,
  EditorCapabilities,
  MediaOverlay,
  OverlaySuggestion,
  PoolAsset,
  SoundEffectPlacement,
  VisualBlock,
} from "@/lib/plan-api";
import { normalizeCameraEffect } from "@/lib/camera-effects";
import { removeOverlayEffectGroup } from "@/lib/overlay-effect-groups";
import { defaultLookAdjustments } from "@/lib/look-presets";
import {
  motionPatchForEffect,
  motionPatchForManualEnd,
  motionPatchForText,
} from "@/lib/text-motion-v2";
import type { AcceptedSuggestionRef } from "@/lib/editor-commit";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import {
  applyClipTimingInput,
  applyTextTimingInput,
  sequentialSlotLayout,
} from "@/app/plan/items/[id]/_editor/editor-bar-drag";
import {
  buildCaptionTextReplacement,
  smartStyleForRole,
} from "@/app/plan/items/[id]/_editor/editor-bars";
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
  cameraEffectMutationFingerprint,
  captionMutationFingerprint,
  overlayMutationFingerprint,
  roundCopilotNumber,
  sfxMutationFingerprint,
  slotMutationFingerprint,
  textMutationFingerprint,
  type CopilotSnapshot,
  type CopilotCarouselSnapshot,
  type CopilotCaptionCueSnapshot,
  type CopilotSlotSnapshot,
  type CopilotTextSnapshotBar,
  type CopilotSfxPlacementSnapshot,
  type CopilotOverlayCardSnapshot,
  type CopilotCaptionMetaSnapshot,
  motionMutationFingerprint,
} from "./snapshot";
import {
  createCreatorBlockInstance,
  creatorBlockDurationFramesV2,
  creatorBlockEntry,
  retimeCreatorBlockManualSpan,
  retimeCreatorBlockSpeed,
  upgradeCreatorBlockInstanceToV2,
  validateMotionInstances,
  type MotionAssetRef,
  type MotionPresetInstance,
} from "@nova/motion-runtime";

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
  nextCameraEffects?: CameraEffect[] | null;
  nextVisualBlocks?: VisualBlock[] | null;
  nextMotionScenes?: MotionPresetInstance[] | null;
  /** Tri-state, mirrors nextVisualBlocks conventions but for a single nullable
   * value: `undefined` = untouched this turn, `null` = staged removal, an
   * object = staged add/edit (Lane D — carousel-as-a-moment is a first-class
   * draft mutation, not a render dispatch). */
  nextCarouselMoment?: CarouselMoment | null;
  acceptedSuggestionRefs?: AcceptedSuggestionRef[];
  nextMusicTrackId?: string;
  nextMixLevel?: number;
  renderRequest?:
    | { kind: "set_intro_layout"; layout: "linear" | "cluster" }
    | { kind: "apply_custom_effect"; effect: Record<string, unknown> };
  nextTitle?: string;
  captionMetaPatch?: CaptionMetaPatch;
  openTool?: "text" | "visuals" | "sounds" | "overlays" | "styles";
  applied: ChangeChip[];
  rejected: RejectedOp[];
  /** The exact validated ops (in CopilotOp form) that mutated the draft this
   *  call — the producer for `ctx.lastAppliedOps`, which `repeat_last_edit`
   *  re-runs on a later turn. Every branch that pushes an `applied` chip is
   *  captured automatically (see the per-op loop in applyCopilotOps); never
   *  includes rejected ops. Optional like every other result field so
   *  existing test fixtures/callers that construct this object by hand don't
   *  need updating — `applyCopilotOps`/`applyCopilotOpsAtomic` always
   *  populate it (empty array, never omitted). */
  appliedOps?: CopilotOp[];
  /** Signal for history ops — neither mutates the draft itself (`undo_last_edit`
   *  has no local draft representation; `repeat_last_edit`'s actual mutation,
   *  if any, is merged into the fields above via a recursive
   *  `applyCopilotOpsAtomic` call against `ctx.lastAppliedOps`). The caller
   *  (EditorShell's handleCopilotOps) inspects this to invoke `history.undo()`
   *  for "undo", or to label a "repeat" turn distinctly from an ordinary one. */
  historyAction?: "undo" | { kind: "repeat"; ops: CopilotOp[] };
}

export interface ApplyCopilotOpsContext {
  bars: TextElementBar[];
  slots: DraftSlot[];
  snapshot: CopilotSnapshot;
  capabilities?: EditorCapabilities | null;
  grid?: number[];
  videoDurationS?: number;
  /** Explicit for deterministic tests; defaults to the build-time rollout flag. */
  textMotionV2Enabled?: boolean;
  /** Explicit for deterministic tests; defaults to the build-time rollout flag. */
  evolvingTypeEnabled?: boolean;
  sfx?: SoundEffectPlacement[];
  sfxCatalog?: SoundEffectSummary[];
  overlays?: MediaOverlay[];
  cameraEffects?: CameraEffect[];
  visualBlocks?: VisualBlock[];
  motionScenes?: MotionPresetInstance[];
  /** The shell's live staged-or-persisted carousel moment (Lane C's
   * `carouselMoment` state — always the session's EFFECTIVE moment, staged
   * once touched, else the persisted `variant.carousel_moment`, else null).
   * `undefined` means the caller hasn't threaded it (falls back to
   * reconstructing a seed from `snapshot.carousel.current`). */
  carouselMoment?: CarouselMoment | null;
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
  makeCameraEffectId?: () => string;
  makeMotionId?: () => string;
  /** The op list `repeat_last_edit` re-runs (the previous turn's
   *  `ApplyCopilotOpsResult.appliedOps`). Sourced by the caller from its own
   *  turn history — never persisted here. Undefined/empty ⇒ nothing to
   *  repeat, so the op rejects with "nothing to repeat yet". */
  lastAppliedOps?: CopilotOp[];
  /** Whether the most recent locally-applied turn is still at the top of the
   *  undo stack — mirrors CopilotDrawer's own staleness check
   *  (`latestChanged.undoVersion === historyVersion && canUndo`). Used
   *  exclusively by `undo_last_edit` so a stale "undo that" (issued after a
   *  manual panel edit already moved the stack) rejects instead of undoing
   *  the wrong thing. */
  canUndoLastTurn?: boolean;
}

let textIdCounter = 0;
let slotKeyCounter = 0;
let sfxIdCounter = 0;
let overlayIdCounter = 0;
let cameraEffectIdCounter = 0;
let motionIdCounter = 0;

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

function defaultCameraEffectId(): string {
  cameraEffectIdCounter += 1;
  return globalThis.crypto?.randomUUID?.() ?? `copilot-camera-${cameraEffectIdCounter}`;
}

function defaultMotionId(): string {
  motionIdCounter += 1;
  return globalThis.crypto?.randomUUID?.() ?? `copilot-motion-${motionIdCounter}`;
}

function fmt(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? `${value}` : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  if (value === null || value === undefined || value === "") return "default";
  return String(value);
}

function fmtSeconds(value: number): string {
  return `${round(value).toFixed(1)}s`;
}

/** Reconstructs a CarouselMoment-shaped seed from the snapshot's persisted
 * `carousel.current` (flattened contract fields) — used only when the caller
 * hasn't threaded the shell's live `ctx.carouselMoment` (e.g. a test context
 * built directly against `applyCopilotOps`). "stills" is a legal persisted
 * mode (auto-authoring only) but isn't part of the CarouselMoment contract
 * type, so it's dropped from the seed rather than carried into a merge
 * target — mirrors CarouselPanel's isPickableMode gate. */
function carouselMomentFromSnapshot(
  current: CopilotCarouselSnapshot["current"],
): CarouselMoment | null {
  if (!current) return null;
  const moment: CarouselMoment = {};
  if (current.position != null) moment.position = current.position;
  if (current.mode === "focus" || current.mode === "rolling") moment.mode = current.mode;
  if (current.effect != null) moment.effect = current.effect;
  if (current.focus_clip_index !== undefined) moment.focus_clip_index = current.focus_clip_index;
  if (current.duration_s != null) moment.duration_s = current.duration_s;
  if (current.transition != null) moment.transition = current.transition;
  return moment;
}

/** Human-readable summary of a carousel config for chip receipts (from/to). */
function describeCarouselMoment(moment: CarouselMoment | null): string {
  if (!moment) return "none";
  const parts: string[] = [];
  if (moment.position) parts.push(moment.position);
  if (moment.mode) parts.push(moment.mode);
  if (moment.effect) parts.push(moment.effect.replaceAll("_", " "));
  if (moment.focus_clip_index != null) parts.push(`clip ${moment.focus_clip_index + 1}`);
  if (moment.duration_s != null) parts.push(fmtSeconds(moment.duration_s));
  if (moment.transition) parts.push(moment.transition === "crossfade" ? "crossfade" : "hard cut");
  return parts.length > 0 ? parts.join(" · ") : "carousel";
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

const COMPLETE_TEXT_FINGERPRINT_FIELDS: Array<
  TextStylePatchKey | "text" | "start_s" | "end_s"
> = [
  "text",
  "start_s",
  "end_s",
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
];

function completeTextFingerprintMatches(
  bar: TextElementBar,
  snap: CopilotTextSnapshotBar,
): boolean {
  if (snap.mutation_fingerprint) {
    return textMutationFingerprint(bar) === snap.mutation_fingerprint;
  }
  return bar.role === snap.role &&
    textFingerprintMatches(bar, snap, COMPLETE_TEXT_FINGERPRINT_FIELDS);
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

function completeSlotFingerprintMatches(
  slots: DraftSlot[],
  grid: number[],
  slot: DraftSlot,
  index: number,
  snap: CopilotSlotSnapshot,
): boolean {
  if (snap.mutation_fingerprint) {
    return slotMutationFingerprint(slot) === snap.mutation_fingerprint &&
      index === snap.index &&
      slotFingerprintMatches(
        slots,
        grid,
        slot,
        index,
        snap,
        ["output_start_s", "output_end_s"],
      );
  }
  return slotFingerprintMatches(
    slots,
    grid,
    slot,
    index,
    snap,
    ["in_s", "duration_s", "removed", "output_start_s", "output_end_s"],
  ) &&
    slot.slotId === snap.slot_id &&
    slot.clipIndex === snap.clip_index &&
    (slot.momentDescription == null || slot.momentDescription === snap.moment) &&
    (slot.transitionAfter ?? "cut") === (snap.transition_after ?? "cut") &&
    sameValue(slot.transitionDurationS ?? null, snap.transition_duration_s ?? null);
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
  if (op.op === "set_look_preset") return `Clip ${op.slot_index + 1} look`;
  if (op.op === "insert_generated_asset") return "Insert generated clip";
  if (op.op === "replace_generated_segment") return "Restyle clip";
  if (op.op === "add_sfx") return "Add sound";
  if (op.op === "patch_sfx") return `Sound ${op.sfx_index + 1}`;
  if (op.op === "remove_sfx") return `Remove sound ${op.sfx_index + 1}`;
  if (op.op === "patch_overlay") return `Overlay ${op.overlay_index + 1}`;
  if (op.op === "remove_overlay") return `Remove overlay ${op.overlay_index + 1}`;
  if (op.op === "add_overlay") return "Add overlay";
  if (op.op === "accept_overlay_suggestion") return "Accept overlay suggestion";
  if (op.op === "edit_caption") return `Caption ${op.cue_index + 1} edited`;
  if (op.op === "replace_caption_text") return "Replace caption text";
  if (op.op === "set_caption_timing") return `Caption ${op.cue_index + 1} timing`;
  if (op.op === "set_caption_meta") return "Captions";
  if (op.op === "set_caption_emphasis") return `Caption ${op.cue_index + 1} emphasis`;
  if (op.op === "swap_music") return "Swapped song";
  if (op.op === "set_mix") return "Music volume";
  if (op.op === "set_intro_layout") return "Intro layout";
  if (op.op === "apply_custom_effect") return "Custom effect";
  if (op.op === "set_carousel_moment") return "Carousel";
  if (op.op === "set_title") return "Title set";
  if (op.op === "add_camera_effect") return "Add camera effect";
  if (op.op === "patch_camera_effect") return `Camera effect ${op.camera_effect_index + 1}`;
  if (op.op === "remove_camera_effect") return `Remove camera effect ${op.camera_effect_index + 1}`;
  if (op.op === "set_transition") return `Transition ${op.boundary_index + 1}`;
  if (op.op === "set_visual_fade") return `Visual block ${op.visual_block_index + 1}`;
  if (op.op === "add_motion_block") return `Add ${creatorBlockEntry(op.preset_id).label}`;
  if (op.op === "patch_motion_block") return "Edit Creator Block";
  if (op.op === "remove_motion_block") return "Remove Creator Block";
  if (op.op === "open_tool") return `Opened ${op.tool[0].toUpperCase()}${op.tool.slice(1)}`;
  if (op.op === "undo_last_edit") return "Undo";
  if (op.op === "repeat_last_edit") return "Repeat";
  const _exhaustive: never = op;
  return _exhaustive;
}

function lookPresetLabel(preset: DraftSlot["lookPreset"]): string {
  if (preset === "stadium_diffusion") return "Stadium Diffusion";
  if (preset === "olive_film") return "Olive Film";
  if (preset === "smoky_split_tone") return "Smoky Split-Tone";
  return "Original";
}

/** Beat fidelity is not prompt-only: a model-proposed timing within this window
 * of a snapshot beat mark snaps exactly onto it. Times farther away are treated
 * as deliberate non-beat placements and pass through untouched. */
export const BEAT_SNAP_EPSILON_S = 0.12;

export function snapToBeatMark(value: number, marks: number[] | undefined): number {
  if (!marks || marks.length === 0) return value;
  let best = value;
  let bestDiff = Number.POSITIVE_INFINITY;
  for (const mark of marks) {
    const diff = Math.abs(mark - value);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = mark;
    }
  }
  return bestDiff <= BEAT_SNAP_EPSILON_S ? best : value;
}

function snapOptToBeatMark(
  value: number | undefined,
  marks: number[] | undefined,
): number | undefined {
  return value === undefined ? undefined : snapToBeatMark(value, marks);
}

/** Snap a start/end span. Snapping both edges onto the SAME mark would collapse
 * a deliberately short span (min-duration clamps then push the end off-beat),
 * so when that happens the raw end is kept instead. */
function snapSpanToBeatMarks(
  startS: number | undefined,
  endS: number | undefined,
  marks: number[] | undefined,
): { startS: number | undefined; endS: number | undefined } {
  const snappedStart = snapOptToBeatMark(startS, marks);
  let snappedEnd = snapOptToBeatMark(endS, marks);
  if (snappedStart !== undefined && snappedEnd !== undefined && snappedStart === snappedEnd) {
    snappedEnd = endS;
  }
  return { startS: snappedStart, endS: snappedEnd };
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
  key: "at_s" | "gain" | "duration_s" | "label",
): unknown {
  if (key === "at_s") return round(placement.at_s);
  if (key === "gain") return round(placement.gain ?? snap.gain);
  if (key === "duration_s") {
    return placement.duration_s == null ? null : round(placement.duration_s);
  }
  if (key === "label") return placement.label?.slice(0, 60) ?? null;
  return undefined;
}

function sfxFingerprintMatches(
  placement: SoundEffectPlacement,
  snap: CopilotSfxPlacementSnapshot,
  fields: Array<"at_s" | "gain" | "duration_s" | "label">,
): boolean {
  return fields.every((field) => sameValue(sfxValue(placement, snap, field), snap[field]));
}

function completeSfxFingerprintMatches(
  placement: SoundEffectPlacement,
  snap: CopilotSfxPlacementSnapshot,
): boolean {
  if (snap.mutation_fingerprint) {
    return sfxMutationFingerprint(placement) === snap.mutation_fingerprint;
  }
  return sfxFingerprintMatches(placement, snap, ["at_s", "gain", "duration_s", "label"]);
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

const COMPLETE_OVERLAY_FINGERPRINT_FIELDS: OverlayPatchKey[] = [
  "start_s",
  "end_s",
  "position",
  "x_frac",
  "y_frac",
  "scale",
  "display_mode",
];

function completeOverlayFingerprintMatches(
  card: MediaOverlay,
  snap: CopilotOverlayCardSnapshot,
): boolean {
  if (snap.mutation_fingerprint) {
    return overlayMutationFingerprint(card) === snap.mutation_fingerprint;
  }
  return card.kind === snap.kind &&
    overlayFingerprintMatches(card, snap, COMPLETE_OVERLAY_FINGERPRINT_FIELDS);
}

function completeCameraEffectFingerprintMatches(
  effect: CameraEffect,
  snap: NonNullable<CopilotSnapshot["camera_effects"]>[number],
): boolean {
  if (snap.mutation_fingerprint) {
    return cameraEffectMutationFingerprint(effect) === snap.mutation_fingerprint;
  }
  return sameValue(round(effect.start_s), snap.start_s) &&
    sameValue(round(effect.end_s), snap.end_s) &&
    sameValue(round(effect.intensity), snap.intensity);
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

const LYRIC_STYLE_PATCH_KEYS = new Set<TextStylePatchKey>([
  "color",
  "highlight_color",
  "font_family",
  "size_px",
]);

function clampLyricStylePatch(input: {
  patch: TextStylePatch;
  stripped: string[];
}): { patch: TextStylePatch; stripped: string[] } {
  const patch: TextStylePatch = {};
  const stripped = [...input.stripped];
  for (const [key, value] of Object.entries(input.patch)) {
    if (LYRIC_STYLE_PATCH_KEYS.has(key as TextStylePatchKey)) {
      (patch as Record<string, unknown>)[key] = value;
    } else if (key !== "size_class") {
      stripped.push(key);
    }
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
  const appliedOps: CopilotOp[] = [];
  let historyAction: ApplyCopilotOpsResult["historyAction"];
  const grid = ctx.grid ?? [];
  const videoDurationS = ctx.videoDurationS ?? Math.max(60, ctx.snapshot.total_duration_s);
  const textMotionV2Enabled =
    ctx.textMotionV2Enabled ?? process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";
  const allowedFamilies = new Set(ctx.snapshot.allowed_op_families);
  let nextSlots: DraftSlot[] | null = null;
  let workingSlots = ctx.slots;
  // Any clip-timeline mutation this turn shifts the output timeline and stales
  // every beat mark the snapshot carried, so snapping stops for later ops in
  // the same bundle (the prompt forbids mixing the two; this enforces it).
  let timelineMutated = false;
  const beatMarksNow = (): number[] | undefined =>
    timelineMutated ? undefined : ctx.snapshot.beat_marks;
  // The validator's clampAtS runs BEFORE snapping, so a snap onto the terminal
  // beat mark (== video end) could escape the end clamp and place an inaudible
  // SFX at the exact last instant. Re-apply the clamp after snapping.
  const snapAtS = (value: number): number => {
    const snapped = snapToBeatMark(value, beatMarksNow());
    const total = ctx.snapshot.total_duration_s;
    // A 0/absent total (slot-less subtitled variant) must not clamp everything
    // to second 0 — fall back to the real video duration (already 60s-floored).
    const cap = Number.isFinite(total) && total > 0 ? total : videoDurationS;
    return Number.isFinite(cap) ? Math.min(snapped, Math.max(0, cap - 0.1)) : snapped;
  };
  let nextSfx: SoundEffectPlacement[] | undefined;
  let workingSfx = ctx.sfx ?? [];
  let nextOverlays: MediaOverlay[] | undefined;
  let workingOverlays = ctx.overlays ?? [];
  let nextCameraEffects: CameraEffect[] | undefined;
  let workingCameraEffects = ctx.cameraEffects ?? [];
  let nextVisualBlocks: VisualBlock[] | undefined;
  let workingVisualBlocks = ctx.visualBlocks ?? [];
  let nextMotionScenes: MotionPresetInstance[] | undefined;
  let workingMotionScenes = ctx.motionScenes ?? [];
  let nextCarouselMoment: CarouselMoment | null | undefined;
  let acceptedSuggestionRefs: AcceptedSuggestionRef[] | undefined;
  let nextMusicTrackId: string | undefined;
  let nextMixLevel: number | undefined;
  let renderRequest: ApplyCopilotOpsResult["renderRequest"];
  let nextTitle: string | undefined;
  let captionMetaPatch: CaptionMetaPatch | undefined;
  let openTool: ApplyCopilotOpsResult["openTool"];
  // Director suggestions and Copilot edits share this applier. Only explicit
  // model-authored bundle IDs establish ownership; response co-membership is
  // not linkage because independent requests may legitimately add one overlay
  // and unrelated effects together.
  const validBundleOps: CopilotOp[] = [];
  for (const raw of rawOps) {
    const result = validateCopilotOp(raw, ctx.snapshot);
    if (result.ok) validBundleOps.push(result.op);
  }
  const bundleEffectGroupIds = new Map<string, string>();
  const bundleIds = new Set(
    validBundleOps.flatMap((op) =>
      "effect_bundle_id" in op && op.effect_bundle_id ? [op.effect_bundle_id] : [],
    ),
  );
  for (const bundleId of Array.from(bundleIds)) {
    const members = validBundleOps.filter(
      (op) => "effect_bundle_id" in op && op.effect_bundle_id === bundleId,
    );
    const allMembersCanApply = members.every((op) => {
      const family = copilotOpFamily(op);
      if (family && !allowedFamilies.has(family)) return false;
      if (op.op === "add_overlay") {
        const snapAsset = ctx.snapshot.overlays?.asset_pool.some(
          (asset) => asset.id === op.asset_id,
        );
        const currentAsset = ctx.poolAssets?.some(
          (asset) => asset.id === op.asset_id && asset.status === "ready",
        );
        return Boolean(snapAsset && currentAsset);
      }
      if (op.op === "add_sfx") {
        const snapEffect = ctx.snapshot.sfx?.catalog.some(
          (effect) => effect.id === op.effect_id,
        );
        const currentEffect = (ctx.sfxCatalog ?? ctx.snapshot.sfx?.catalog ?? []).some(
          (effect) => effect.id === op.effect_id,
        );
        return Boolean(snapEffect && currentEffect);
      }
      return op.op === "add_camera_effect";
    });
    if (
      allMembersCanApply &&
      members.filter((op) => op.op === "add_overlay").length === 1 &&
      members.some((op) => op.op === "add_sfx" || op.op === "add_camera_effect")
    ) {
      bundleEffectGroupIds.set(bundleId, `ai-bundle-${defaultOverlayId()}`);
    }
  }

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
      nextCameraEffects !== undefined ||
      nextVisualBlocks !== undefined ||
      nextMotionScenes !== undefined ||
      nextCarouselMoment !== undefined ||
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
    const appliedCountBeforeOp = applied.length;
    const bundleEffectGroupId =
      "effect_bundle_id" in op && op.effect_bundle_id
        ? bundleEffectGroupIds.get(op.effect_bundle_id)
        : undefined;
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
      textActions.push(
        bar.role === "lyric_line" || !textMotionV2Enabled || bar.motion?.version !== 2
          ? { type: "EDIT_TEXT", id: bar.id, text: op.text }
          : {
              type: "PATCH_BAR",
              id: bar.id,
              patch: motionPatchForText(bar, op.text, videoDurationS),
            },
      );
      applied.push({ label: `Text ${op.bar_index + 1}`, from: bar.text, to: op.text });
    } else if (op.op === "patch_text_style") {
      const snap = textSnapAt(ctx.snapshot, op.bar_index);
      const bar = snap ? textBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "text bar no longer exists"));
        continue;
      }
      const stylePatch = applyStylePatch(op.patch);
      const { patch, stripped } =
        bar.role === "lyric_line" ? clampLyricStylePatch(stylePatch) : stylePatch;
      const motionPatch =
        textMotionV2Enabled && typeof patch.effect === "string" && bar.role !== "lyric_line"
          ? motionPatchForEffect(bar, patch.effect, videoDurationS)
          : null;
      if (patch.effect === "smooth-type" && !textMotionV2Enabled) {
        rejected.push(
          reject(op.op, labelForOp(op), "capability_disabled", "Text Motion v2 is disabled."),
        );
        continue;
      }
      const effectivePatch = motionPatch ? { ...patch, ...motionPatch } : patch;
      const patchKeys = Object.keys(patch) as TextStylePatchKey[];
      if (patchKeys.length === 0) {
        rejected.push(
          reject(
            op.op,
            labelForOp(op),
            "unsupported_field",
            bar.role === "lyric_line"
              ? "Lyric style supports color, highlight color, font, and size."
              : `style fields are not parity verified: ${stripped.join(", ")}`,
          ),
        );
        continue;
      }
      if (!textFingerprintMatches(bar, snap, patchKeys)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "style was changed after Nova read it"));
        continue;
      }
      textActions.push({ type: "PATCH_BAR", id: bar.id, patch: effectivePatch });
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
      if (bar.role === "lyric_line") {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", "Lyric timing is locked to the vocal."));
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
      const span = snapSpanToBeatMarks(op.start_s, op.end_s, beatMarksNow());
      const next = applyTextTimingInput({
        startS: span.startS ?? bar.start_s,
        endS: span.endS ?? bar.end_s,
        videoDurationS,
      });
      const retimed = textMotionV2Enabled
        ? motionPatchForManualEnd({ ...bar, ...next }, next.end_s, videoDurationS)
        : { end_s: next.end_s };
      textActions.push({
        type: "PATCH_BAR",
        id: bar.id,
        patch: { ...next, ...retimed },
      });
      applied.push({
        label: `Text ${op.bar_index + 1} timing`,
        from: `${fmtSeconds(bar.start_s)}-${fmtSeconds(bar.end_s)}`,
        to: `${fmtSeconds(next.start_s)}-${fmtSeconds(next.end_s)}`,
      });
    } else if (op.op === "add_text") {
      const addSpan = snapSpanToBeatMarks(op.start_s, op.end_s, beatMarksNow());
      const timing = applyTextTimingInput({
        startS: addSpan.startS ?? op.start_s,
        endS: addSpan.endS ?? op.end_s,
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
      if (bar.role === "lyric_line") {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", "Lyric timing is locked to the vocal."));
        continue;
      }
      if (!completeTextFingerprintMatches(bar, snap)) {
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
      timelineMutated = true;
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
      timelineMutated = true;
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
      timelineMutated = true;
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
      if (!completeSlotFingerprintMatches(slots, grid, slot, index, snap)) {
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
      timelineMutated = true;
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
      timelineMutated = true;
      applied.push({ label: `Split clip ${op.slot_index + 1}`, from: "one clip", to: "two clips" });
    } else if (op.op === "set_look_preset") {
      const snap = slotSnapAt(ctx.snapshot, op.slot_index);
      const slots = currentSlots();
      const index = snap ? currentSlotIndex(slots, snap.key) : -1;
      const slot = index >= 0 ? slots[index] : null;
      if (!snap || !slot) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "clip slot no longer exists"));
        continue;
      }
      if (!completeSlotFingerprintMatches(slots, grid, slot, index, snap)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip was changed after Nova read it"));
        continue;
      }
      const before = slot.lookPreset ?? "none";
      if (before === op.look_preset) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "clip already uses that look"));
        continue;
      }
      workingSlots = slots.map((candidate) =>
        candidate.key === slot.key
          ? {
              ...candidate,
              lookPreset: op.look_preset,
              lookAdjustments: defaultLookAdjustments(op.look_preset),
            }
          : candidate,
      );
      nextSlots = workingSlots;
      applied.push({
        label: `Clip ${op.slot_index + 1} look`,
        from: lookPresetLabel(before),
        to: lookPresetLabel(op.look_preset),
      });
    } else if (op.op === "add_sfx") {
      const snapCatalogEntry = ctx.snapshot.sfx?.catalog.find((effect) => effect.id === op.effect_id) ?? null;
      const catalogEntry = (ctx.sfxCatalog ?? ctx.snapshot.sfx?.catalog ?? []).find((effect) => effect.id === op.effect_id) ?? null;
      if (!snapCatalogEntry || !catalogEntry) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "sound effect is no longer available"));
        continue;
      }
      const label = "name" in catalogEntry ? catalogEntry.name : snapCatalogEntry.name;
      const duration = catalogEntry.duration_s ?? snapCatalogEntry.duration_s ?? null;
      const atS = snapAtS(op.at_s);
      const placement: SoundEffectPlacement = {
        id: ctx.makeSfxPlacementId?.() ?? defaultSfxPlacementId(),
        sound_effect_id: op.effect_id,
        src_gcs_path: "",
        at_s: atS,
        gain: op.gain,
        duration_s: duration,
        label,
        ...(bundleEffectGroupId
          ? { source: "edit_ai", effect_group_id: bundleEffectGroupId }
          : {}),
      };
      workingSfx = [...currentSfx(), placement];
      nextSfx = workingSfx;
      applied.push({ label: `Added "${label}"`, from: "none", to: fmtSeconds(atS) });
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
      const snappedAtS = op.at_s === undefined ? undefined : snapAtS(op.at_s);
      const patch: Partial<SoundEffectPlacement> = {
        ...(snappedAtS !== undefined ? { at_s: snappedAtS } : {}),
        ...(op.gain !== undefined ? { gain: op.gain } : {}),
      };
      workingSfx = placements.map((sfx) => (sfx.id === placement.id ? { ...sfx, ...patch } : sfx));
      nextSfx = workingSfx;
      for (const field of fields) {
        applied.push({
          label: field === "at_s" ? "Moved sound" : "Sound volume",
          from: field === "at_s" ? fmtSeconds(placement.at_s) : fmt(placement.gain),
          to: field === "at_s" ? fmtSeconds(snappedAtS ?? placement.at_s) : fmt(op.gain ?? placement.gain),
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
      if (!completeSfxFingerprintMatches(placement, snap)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "sound placement changed after Nova read it"));
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
      const overlaySpan = snapSpanToBeatMarks(op.patch.start_s, op.patch.end_s, beatMarksNow());
      const overlayPatch = {
        ...op.patch,
        ...(overlaySpan.startS !== undefined ? { start_s: overlaySpan.startS } : {}),
        ...(overlaySpan.endS !== undefined ? { end_s: overlaySpan.endS } : {}),
      };
      workingOverlays = overlays.map((overlay) => (overlay.id === card.id ? { ...overlay, ...overlayPatch } : overlay));
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
      if (!completeOverlayFingerprintMatches(card, snap)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "overlay changed after Nova read it"));
        continue;
      }
      const removed = removeOverlayEffectGroup(
        {
          overlays,
          soundEffects: currentSfx(),
          cameraEffects: workingCameraEffects,
        },
        card.id,
      );
      const previousSfxCount = currentSfx().length;
      const previousCameraCount = workingCameraEffects.length;
      workingOverlays = removed.overlays;
      workingSfx = removed.soundEffects;
      workingCameraEffects = removed.cameraEffects;
      nextOverlays = workingOverlays;
      if (removed.soundEffects.length !== previousSfxCount) nextSfx = workingSfx;
      if (removed.cameraEffects.length !== previousCameraCount) {
        nextCameraEffects = workingCameraEffects;
      }
      applied.push({ label: "Removed overlay", from: "present", to: "removed" });
    } else if (op.op === "add_overlay") {
      const snapAsset = ctx.snapshot.overlays?.asset_pool.find((asset) => asset.id === op.asset_id) ?? null;
      const asset = (ctx.poolAssets ?? []).find((poolAsset) => poolAsset.id === op.asset_id && poolAsset.status === "ready") ?? null;
      if (!snapAsset || !asset) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "overlay asset is no longer available"));
        continue;
      }
      const overlays = currentOverlays();
      const addOverlaySpan = snapSpanToBeatMarks(op.start_s, op.end_s, beatMarksNow());
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
        start_s: addOverlaySpan.startS ?? op.start_s,
        end_s: addOverlaySpan.endS ?? op.end_s,
        z: overlays.length,
        ...(bundleEffectGroupId
          ? { source: "edit_ai", effect_group_id: bundleEffectGroupId }
          : {}),
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
      const effectGroupId = suggestion.overlay.effect_group_id ?? suggestion.id;
      workingOverlays = [
        ...overlays,
        {
          ...suggestion.overlay,
          source: suggestion.overlay.source ?? "overlay_suggestion",
          effect_group_id: effectGroupId,
        },
      ];
      nextOverlays = workingOverlays;
      acceptedSuggestionRefs = acceptedSuggestionRefs ?? [];
      if (!acceptedSuggestionRefs.some((ref) => ref.id === suggestion.id)) {
        acceptedSuggestionRefs.push({ id: suggestion.id, overlayId: suggestion.overlay.id });
      }
      if (suggestion.sfx && allowedFamilies.has("sfx")) {
        workingSfx = [
          ...currentSfx(),
          {
            ...suggestion.sfx,
            source: suggestion.sfx.source ?? "overlay_suggestion",
            effect_group_id: suggestion.sfx.effect_group_id ?? effectGroupId,
          },
        ];
        nextSfx = workingSfx;
      }
      applied.push({ label: "Accepted overlay suggestion", from: "pending", to: snapSuggestion.reason || "accepted" });
    } else if (op.op === "edit_caption") {
      if (ctx.snapshot.captions?.cues_editable === false) {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", "This draft has caption settings but no editable cue list."));
        continue;
      }
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
    } else if (op.op === "replace_caption_text") {
      if (ctx.snapshot.captions?.cues_editable === false) {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", "This draft has caption settings but no editable cue list."));
        continue;
      }
      if (!ctx.snapshot.captions) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "caption cues are no longer available"));
        continue;
      }
      const expectedFingerprint = ctx.snapshot.captions.mutation_fingerprint;
      if (
        expectedFingerprint &&
        captionMutationFingerprint(ctx.bars) !== expectedFingerprint
      ) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "captions were changed after Nova read them"));
        continue;
      }
      const replacement = buildCaptionTextReplacement(ctx.bars, op.find, op.replace);
      if (replacement.patches.length === 0) {
        const detail = replacement.foundMatchCount > 0
          ? `Every “${op.find}” match already reads “${op.replace}”.`
          : `No captions contain “${op.find}”.`;
        rejected.push(reject(op.op, labelForOp(op), "no_effect", detail));
        continue;
      }
      textActions.push({ type: "PATCH_BARS", patches: replacement.patches });
      applied.push({
        label: "Caption text replaced",
        from: op.find,
        to: `${op.replace || "(removed)"} · ${replacement.matchCount} ${replacement.matchCount === 1 ? "match" : "matches"} in ${replacement.lineCount} ${replacement.lineCount === 1 ? "line" : "lines"}`,
      });
    } else if (op.op === "set_caption_timing") {
      if (ctx.snapshot.captions?.cues_editable === false) {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", "This draft has caption settings but no editable cue list."));
        continue;
      }
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
    } else if (op.op === "set_caption_emphasis") {
      if (ctx.snapshot.captions?.cues_editable === false) {
        rejected.push(reject(op.op, labelForOp(op), "unsupported_field", "This draft has caption settings but no editable cue list."));
        continue;
      }
      const snap = captionSnapAt(ctx.snapshot, op.cue_index);
      const bar = snap ? captionBarForSnap(ctx.bars, snap) : null;
      if (!snap || !bar) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "caption cue no longer exists"));
        continue;
      }
      const currentEmphasis = bar.smart_emphasis === true;
      const snapEmphasis = snap.smart_emphasis === true;
      if (currentEmphasis !== snapEmphasis) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "caption emphasis was changed after Nova read it"));
        continue;
      }
      // smart_style follows the cue's own role (hook/context/list_item/example/
      // payoff/cta) — same mapping the editor's Emphasize toggle uses
      // (editor-bars.ts smartStyleForRole) — clearing resets both.
      const patch = op.emphasis
        ? { smart_emphasis: true, smart_style: smartStyleForRole(bar.smart_role ?? snap.smart_role) }
        : { smart_emphasis: false, smart_style: null };
      textActions.push({ type: "PATCH_BAR", id: bar.id, patch });
      applied.push({
        label: `Caption ${op.cue_index + 1} emphasis`,
        from: currentEmphasis ? "emphasized" : "normal",
        to: op.emphasis ? "emphasized" : "normal",
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
    } else if (op.op === "apply_custom_effect") {
      // Same single-op-per-turn contract as set_intro_layout: a custom
      // effect re-renders the video, so it can't share a turn with a local
      // draft mutation or another render request. Deep filter/param
      // validation already happened server-side (edit_copilot.py's parse
      // step) — this branch only enforces the turn-shape rule and stages the
      // dispatch; EditorShell PATCHes /custom-effect on renderRequest, same
      // as it PATCHes intro_layout.
      if (renderRequest || hasDraftMutation() || rawOps.length > 1) {
        rejected.push(reject(
          op.op,
          labelForOp(op),
          "capability_disabled",
          "a custom effect re-renders the video — ask for it on its own",
        ));
        continue;
      }
      renderRequest = { kind: "apply_custom_effect", effect: op.effect };
      applied.push({ label: "Custom effect", from: "current look", to: "new look (re-rendering)" });
    } else if (op.op === "set_carousel_moment") {
      // Carousel-as-a-moment (Lane D, carousel-blocks train): a first-class
      // staged/undoable draft mutation, same model as every other editor
      // block — no render dispatch, no single-op-per-turn restriction, and
      // it composes with any other op in the same bundle. Seeds from the
      // shell's live staged-or-persisted value (ctx.carouselMoment — Lane
      // C's invariant: that state always holds the session's EFFECTIVE
      // moment) and falls back to reconstructing one from the snapshot's
      // persisted `carousel.current` when the caller hasn't threaded it.
      const carousel = ctx.snapshot.carousel;
      if (!carousel) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "carousel is not available"));
        continue;
      }
      const seed: CarouselMoment | null =
        ctx.carouselMoment !== undefined
          ? ctx.carouselMoment
          : carouselMomentFromSnapshot(carousel.current);
      if (op.config === null) {
        if (seed == null) {
          rejected.push(reject(op.op, labelForOp(op), "no_effect", "no carousel to remove"));
          continue;
        }
        nextCarouselMoment = null;
        applied.push({ label: "Carousel", from: describeCarouselMoment(seed), to: "removed" });
      } else {
        if (!carousel.eligible) {
          rejected.push(reject(
            op.op,
            labelForOp(op),
            "capability_disabled",
            carousel.reason ?? "carousel is not available for this edit",
          ));
          continue;
        }
        const seedValueAt = (key: keyof CarouselMoment): unknown =>
          seed ? (seed[key] ?? null) : null;
        const isNoOp =
          seed != null &&
          (Object.entries(op.config) as Array<[keyof CarouselMoment, unknown]>).every(([key, value]) =>
            sameValue(seedValueAt(key), value ?? null),
          );
        if (isNoOp) {
          rejected.push(reject(op.op, labelForOp(op), "no_effect", "carousel already matches this configuration"));
          continue;
        }
        const merged: CarouselMoment = { ...(seed ?? {}), ...op.config };
        nextCarouselMoment = merged;
        applied.push({
          label: "Carousel",
          from: describeCarouselMoment(seed),
          to: describeCarouselMoment(merged),
        });
      }
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
    } else if (op.op === "add_camera_effect") {
      const effect = normalizeCameraEffect({
        id: ctx.makeCameraEffectId?.() ?? defaultCameraEffectId(),
        token: "semantic_crop_pulse",
        start_s: op.start_s,
        end_s: op.end_s,
        intensity: op.intensity ?? 0.04,
        easing: "sine_pulse",
        source: bundleEffectGroupId ? "edit_ai" : "user",
        ...(bundleEffectGroupId ? { effect_group_id: bundleEffectGroupId } : {}),
      });
      workingCameraEffects = [...workingCameraEffects, effect];
      nextCameraEffects = workingCameraEffects;
      applied.push({
        label: "Camera pulse",
        from: "none",
        to: `${fmtSeconds(effect.start_s)}-${fmtSeconds(effect.end_s)}`,
      });
    } else if (op.op === "patch_camera_effect") {
      const snap = ctx.snapshot.camera_effects?.[op.camera_effect_index];
      const effect = snap
        ? workingCameraEffects.find((candidate) => candidate.id === snap.id)
        : null;
      if (!snap || !effect) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "camera effect no longer exists"));
        continue;
      }
      if (!completeCameraEffectFingerprintMatches(effect, snap)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "camera effect changed after Nova read it"));
        continue;
      }
      const patched = normalizeCameraEffect({
        ...effect,
        ...(op.start_s !== undefined ? { start_s: op.start_s } : {}),
        ...(op.end_s !== undefined ? { end_s: op.end_s } : {}),
        ...(op.intensity !== undefined ? { intensity: op.intensity } : {}),
        source: "user",
      });
      workingCameraEffects = workingCameraEffects.map((candidate) =>
        candidate.id === effect.id ? patched : candidate,
      );
      nextCameraEffects = workingCameraEffects;
      applied.push({ label: "Camera pulse", from: "previous", to: "updated" });
    } else if (op.op === "remove_camera_effect") {
      const snap = ctx.snapshot.camera_effects?.[op.camera_effect_index];
      const effect = snap
        ? workingCameraEffects.find((candidate) => candidate.id === snap.id)
        : null;
      if (!snap || !effect) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "camera effect no longer exists"));
        continue;
      }
      if (!completeCameraEffectFingerprintMatches(effect, snap)) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "camera effect changed after Nova read it"));
        continue;
      }
      workingCameraEffects = workingCameraEffects.filter((candidate) => candidate.id !== effect.id);
      nextCameraEffects = workingCameraEffects;
      applied.push({ label: "Camera pulse", from: "present", to: "removed" });
    } else if (op.op === "set_transition") {
      const slots = currentSlots();
      const activeSnapshotSlots = ctx.snapshot.slots.filter((slot) => !slot.removed);
      const leftSnap = activeSnapshotSlots[op.boundary_index];
      const rightSnap = activeSnapshotSlots[op.boundary_index + 1];
      const leftIndex = leftSnap ? currentSlotIndex(slots, leftSnap.key) : -1;
      const rightIndex = rightSnap ? currentSlotIndex(slots, rightSnap.key) : -1;
      const activeCurrentSlots = slots.filter((slot) => !slot.removed);
      const activeLeftIndex = leftSnap
        ? currentSlotIndex(activeCurrentSlots, leftSnap.key)
        : -1;
      const activeRightIndex = rightSnap
        ? currentSlotIndex(activeCurrentSlots, rightSnap.key)
        : -1;
      if (
        leftIndex < 0 ||
        rightIndex < 0 ||
        activeLeftIndex < 0 ||
        activeRightIndex !== activeLeftIndex + 1
      ) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "clip order changed after Nova read it"));
        continue;
      }
      const left = slots[leftIndex];
      const currentTransition = left.transitionAfter ?? "cut";
      const currentDuration = left.transitionDurationS ?? null;
      if (
        currentTransition !== (leftSnap.transition_after ?? "cut") ||
        !sameValue(currentDuration, leftSnap.transition_duration_s ?? null)
      ) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "transition changed after Nova read it"));
        continue;
      }
      const duration = op.transition === "cut" ? null : (op.duration_s ?? 0.3);
      workingSlots = slots.map((slot, index) =>
        index === leftIndex
          ? { ...slot, transitionAfter: op.transition, transitionDurationS: duration }
          : slot,
      );
      nextSlots = workingSlots;
      applied.push({
        label: `Transition ${op.boundary_index + 1}`,
        from: currentTransition.replaceAll("_", " "),
        to: op.transition.replaceAll("_", " "),
      });
    } else if (op.op === "set_visual_fade") {
      const snap = ctx.snapshot.visual_blocks?.[op.visual_block_index];
      const block = snap
        ? workingVisualBlocks.find((candidate) => candidate.id === snap.id)
        : null;
      if (!snap || !block) {
        rejected.push(
          reject(op.op, labelForOp(op), "target_missing", "visual block no longer exists"),
        );
        continue;
      }
      if (
        block.transition_in !== snap.transition_in ||
        block.transition_out !== snap.transition_out
      ) {
        rejected.push(
          reject(op.op, labelForOp(op), "user_changed", "visual fade changed after Nova read it"),
        );
        continue;
      }
      const patched = {
        ...block,
        ...(op.transition_in ? { transition_in: op.transition_in } : {}),
        ...(op.transition_out ? { transition_out: op.transition_out } : {}),
      } as VisualBlock;
      workingVisualBlocks = workingVisualBlocks.map((candidate) =>
        candidate.id === block.id ? patched : candidate,
      );
      nextVisualBlocks = workingVisualBlocks;
      applied.push({
        label: "Visual fade",
        from: `${block.transition_in}/${block.transition_out}`,
        to: `${patched.transition_in}/${patched.transition_out}`,
      });
    } else if (op.op === "insert_generated_asset") {
      const slots = currentSlots();
      const layout = sequentialSlotLayout(slots, ctx.grid ?? []);
      const boundaries: Array<{ at: number; index: number }> = [];
      const firstActive = slots.findIndex((slot) => !slot.removed);
      boundaries.push({ at: 0, index: firstActive >= 0 ? firstActive : slots.length });
      layout.windows.forEach((window, index) => {
        if (window.startS == null || window.durationS <= 0) return;
        boundaries.push({ at: window.startS + window.durationS, index: index + 1 });
      });
      const target = boundaries.reduce((best, candidate) =>
        Math.abs(candidate.at - op.insert_at_s) < Math.abs(best.at - op.insert_at_s)
          ? candidate
          : best,
      );
      const generatedSlot: DraftSlot = {
        key: `generated-${op.asset_id}`,
        slotId: null,
        clipIndex: op.clip_index,
        inS: 0,
        durationBeats: null,
        durationS: op.duration_s,
        removed: false,
        momentDescription: "Generated by Nova",
        transitionAfter: "cut",
        transitionDurationS: null,
      };
      workingSlots = [
        ...slots.slice(0, target.index),
        generatedSlot,
        ...slots.slice(target.index),
      ];
      nextSlots = workingSlots;
      applied.push({
        label: "Generated clip",
        from: "not in timeline",
        to: `${fmtSeconds(target.at)} · ${fmtSeconds(op.duration_s)}`,
      });
    } else if (op.op === "replace_generated_segment") {
      const slots = currentSlots();
      const sourceSnap = ctx.snapshot.slots.find(
        (slot) =>
          !slot.removed &&
          slot.clip_index === op.source_clip_index &&
          Math.abs(slot.in_s - op.source_start_s) <= 0.05 &&
          Math.abs(slot.in_s + slot.duration_s - op.source_end_s) <= 0.05,
      );
      const sourceIndex = sourceSnap ? currentSlotIndex(slots, sourceSnap.key) : -1;
      const source = sourceIndex >= 0 ? slots[sourceIndex] : null;
      const sourceWindow = sourceIndex >= 0 ? slotWindows(slots, grid)[sourceIndex] : null;
      if (
        !sourceSnap ||
        !source ||
        !sourceWindow ||
        source.removed ||
        source.clipIndex !== op.source_clip_index ||
        Math.abs(source.inS - op.source_start_s) > 0.05 ||
        Math.abs(source.inS + sourceWindow.durationS - op.source_end_s) > 0.05
      ) {
        rejected.push(
          reject(op.op, labelForOp(op), "user_changed", "restyle source changed after Nova read it"),
        );
        continue;
      }
      const replacement: DraftSlot = {
        key: `generated-${op.asset_id}`,
        slotId: null,
        clipIndex: op.clip_index,
        inS: 0,
        durationBeats: null,
        durationS: op.duration_s,
        removed: false,
        momentDescription: "Restyled by Nova",
        transitionAfter: source.transitionAfter ?? "cut",
        transitionDurationS: source.transitionDurationS ?? null,
        lookPreset: source.lookPreset ?? "none",
        lookAdjustments: source.lookAdjustments
          ? { ...source.lookAdjustments }
          : null,
      };
      workingSlots = slots.map((slot, index) => (index === sourceIndex ? replacement : slot));
      nextSlots = workingSlots;
      applied.push({
        label: "Restyled clip",
        from: `${fmtSeconds(sourceSnap.output_start_s ?? 0)} · ${fmtSeconds(sourceSnap.duration_s)}`,
        to: `${fmtSeconds(sourceSnap.output_start_s ?? 0)} · ${fmtSeconds(op.duration_s)}`,
      });
    } else if (op.op === "open_tool") {
      if (!ctx.snapshot.open_tools?.includes(op.tool)) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "tool is not available"));
        continue;
      }
      openTool = op.tool;
      applied.push({ label: `Opened ${op.tool[0].toUpperCase()}${op.tool.slice(1)}`, from: "closed", to: "open" });
    } else if (op.op === "add_motion_block") {
      const evolvingTypeEnabled = ctx.evolvingTypeEnabled ??
        process.env.NEXT_PUBLIC_EVOLVING_TYPE_ENABLED === "true";
      if (op.preset_id === "evolving_type" && !evolvingTypeEnabled) {
        rejected.push(reject(op.op, labelForOp(op), "capability_disabled", "Evolving Type is not enabled."));
        continue;
      }
      const entry = creatorBlockEntry(op.preset_id);
      const assetIds = Array.isArray(op.params.asset_ids)
        ? op.params.asset_ids.filter((value): value is string => typeof value === "string")
        : [];
      const assets: MotionAssetRef[] = assetIds.flatMap((id) => {
        const asset = (ctx.poolAssets ?? []).find(
          (candidate) => candidate.id === id && candidate.kind === "image" && candidate.status === "ready",
        );
        return asset ? [{ asset_id: asset.id, gcs_path: asset.gcs_path }] : [];
      });
      if (assets.length !== assetIds.length || (entry.kind === "media" && assets.length < entry.min_assets)) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "eligible image assets changed after Nova read them"));
        continue;
      }
      const startFrame = Math.round(op.start_s * 30);
      const requestedEndFrame = Math.round(op.end_s * 30);
      let scene = createCreatorBlockInstance({
        id: ctx.makeMotionId?.() ?? defaultMotionId(),
        presetId: op.preset_id,
        startFrame,
        endFrameExclusive: startFrame + entry.default_duration_frames,
        palette: op.palette,
        assets,
      });
      const rawParams = { ...op.params };
      delete rawParams.asset_ids;
      (scene as MotionPresetInstance & { params: Record<string, unknown> }).params = {
        ...((scene as MotionPresetInstance & { params: Record<string, unknown> }).params),
        ...rawParams,
        ...(entry.kind === "media" ? { assets } : {}),
      } as never;
      scene.intensity = op.intensity ?? scene.intensity;
      scene.motion = {
        ...scene.motion,
        ...(op.speed === undefined ? {} : { speed: op.speed }),
        ...(op.easing === undefined ? {} : { easing: op.easing }),
        ...(op.hold_frames === undefined ? {} : { hold_frames: op.hold_frames }),
      };
      if (op.speed !== undefined || op.hold_frames !== undefined) {
        scene.end_frame_exclusive = Math.max(
          scene.start_frame + 1,
          Math.min(
            Math.ceil(videoDurationS * 30),
            scene.start_frame + creatorBlockDurationFramesV2(scene, scene.motion),
          ),
        );
      } else {
        scene = retimeCreatorBlockManualSpan(
          scene,
          startFrame,
          requestedEndFrame,
          Math.ceil(videoDurationS * 30),
        ) as typeof scene;
      }
      const candidate = [...workingMotionScenes, scene];
      const validationResult = validateMotionInstances(candidate, Math.ceil(videoDurationS * 30));
      if (!validationResult.ok) {
        rejected.push(reject(op.op, labelForOp(op), "invalid_op", validationResult.errors.join("; ")));
        continue;
      }
      workingMotionScenes = candidate;
      nextMotionScenes = candidate;
      applied.push({ label: entry.label, from: "not in edit", to: `${fmtSeconds(scene.start_frame / 30)}–${fmtSeconds(scene.end_frame_exclusive / 30)}` });
    } else if (op.op === "patch_motion_block") {
      const snap = ctx.snapshot.motion?.blocks.find((block) => block.id === op.motion_id);
      const index = workingMotionScenes.findIndex((scene) => scene.id === op.motion_id);
      const scene = index >= 0 ? workingMotionScenes[index] : null;
      if (!snap || !scene) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "Creator Block no longer exists"));
        continue;
      }
      const evolvingTypeEnabled = ctx.evolvingTypeEnabled ??
        process.env.NEXT_PUBLIC_EVOLVING_TYPE_ENABLED === "true";
      if (scene.preset_id === "evolving_type" && !evolvingTypeEnabled) {
        rejected.push(reject(op.op, labelForOp(op), "capability_disabled", "Evolving Type is not enabled."));
        continue;
      }
      if (snap.mutation_fingerprint && motionMutationFingerprint(scene) !== snap.mutation_fingerprint) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "Creator Block changed after Nova read it"));
        continue;
      }
      const rawParams = op.patch.params ? { ...op.patch.params } : undefined;
      const assetIds = Array.isArray(rawParams?.asset_ids)
        ? rawParams.asset_ids.filter((value): value is string => typeof value === "string")
        : null;
      const assets = assetIds?.flatMap((id) => {
        const asset = (ctx.poolAssets ?? []).find(
          (candidate) => candidate.id === id && candidate.kind === "image" && candidate.status === "ready",
        );
        return asset ? [{ asset_id: asset.id, gcs_path: asset.gcs_path }] : [];
      });
      if (assetIds && assets?.length !== assetIds.length) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "eligible image assets changed after Nova read them"));
        continue;
      }
      if (rawParams) delete rawParams.asset_ids;
      const entry = scene.preset_id === "route_trace" ? null : creatorBlockEntry(scene.preset_id);
      const advancedParamKeys = new Set(
        entry?.parameters
          .filter((parameter) => parameter.type === "number" || parameter.type === "enum" || parameter.type === "boolean")
          .map((parameter) => parameter.key) ?? [],
      );
      const touchesAdvancedParam = rawParams
        ? Object.keys(rawParams).some((key) => advancedParamKeys.has(key))
        : false;
      const touchesV2Motion =
        op.patch.speed !== undefined ||
        op.patch.easing !== undefined ||
        op.patch.hold_frames !== undefined ||
        op.patch.intensity !== undefined ||
        touchesAdvancedParam;
      let base = scene;
      if (touchesV2Motion && scene.preset_id !== "route_trace") {
        base = upgradeCreatorBlockInstanceToV2(scene);
      }
      if (op.patch.speed !== undefined && base.preset_id !== "route_trace") {
        base = retimeCreatorBlockSpeed(base, op.patch.speed, Math.ceil(videoDurationS * 30));
      }
      if (
        base.preset_id !== "route_trace" &&
        base.preset_version === 2 &&
        (op.patch.easing !== undefined || op.patch.hold_frames !== undefined)
      ) {
        const motion = {
          ...base.motion,
          ...(op.patch.easing === undefined ? {} : { easing: op.patch.easing }),
          ...(op.patch.hold_frames === undefined ? {} : { hold_frames: op.patch.hold_frames }),
        };
        base = {
          ...base,
          motion,
          ...(op.patch.hold_frames === undefined
            ? {}
            : {
                end_frame_exclusive: Math.max(
                  base.start_frame + 1,
                  Math.min(
                    Math.ceil(videoDurationS * 30),
                    base.start_frame + creatorBlockDurationFramesV2(base, motion),
                  ),
                ),
              }),
        };
      }
      let patched = {
        ...base,
        palette: op.patch.palette ?? scene.palette,
        intensity: op.patch.intensity ?? scene.intensity,
        ...(rawParams
          ? { params: { ...(("params" in base ? base.params : {}) as Record<string, unknown>), ...rawParams, ...(assets ? { assets } : {}) } }
          : {}),
      } as MotionPresetInstance;
      const requestedStart = op.patch.start_s === undefined
        ? scene.start_frame
        : Math.round(op.patch.start_s * 30);
      const requestedEnd = op.patch.end_s === undefined
        ? base.end_frame_exclusive
        : Math.round(op.patch.end_s * 30);
      if (patched.preset_id === "route_trace") {
        patched = {
          ...patched,
          start_frame: requestedStart,
          end_frame_exclusive: requestedEnd,
        };
      } else if (op.patch.start_s !== undefined || op.patch.end_s !== undefined) {
        patched = retimeCreatorBlockManualSpan(
          patched,
          requestedStart,
          requestedEnd,
          Math.ceil(videoDurationS * 30),
        ) as MotionPresetInstance;
      }
      const candidate = workingMotionScenes.map((item, itemIndex) => itemIndex === index ? patched : item);
      const validationResult = validateMotionInstances(candidate, Math.ceil(videoDurationS * 30));
      if (!validationResult.ok) {
        rejected.push(reject(op.op, labelForOp(op), "invalid_op", validationResult.errors.join("; ")));
        continue;
      }
      workingMotionScenes = candidate;
      nextMotionScenes = candidate;
      applied.push({ label: snap.label, from: `${fmtSeconds(scene.start_frame / 30)}–${fmtSeconds(scene.end_frame_exclusive / 30)}`, to: `${fmtSeconds(patched.start_frame / 30)}–${fmtSeconds(patched.end_frame_exclusive / 30)}` });
    } else if (op.op === "remove_motion_block") {
      const snap = ctx.snapshot.motion?.blocks.find((block) => block.id === op.motion_id);
      const scene = workingMotionScenes.find((item) => item.id === op.motion_id);
      if (!snap || !scene) {
        rejected.push(reject(op.op, labelForOp(op), "target_missing", "Creator Block no longer exists"));
        continue;
      }
      if (snap.mutation_fingerprint && motionMutationFingerprint(scene) !== snap.mutation_fingerprint) {
        rejected.push(reject(op.op, labelForOp(op), "user_changed", "Creator Block changed after Nova read it"));
        continue;
      }
      workingMotionScenes = workingMotionScenes.filter((item) => item.id !== op.motion_id);
      nextMotionScenes = workingMotionScenes;
      applied.push({ label: snap.label, from: "in edit", to: "removed" });
    } else if (op.op === "undo_last_edit") {
      // Mirrors set_intro_layout's single-op-only enforcement: undo can't
      // compose with anything else in the same turn, and there is no server
      // draft state to validate against — the staleness gate is entirely
      // client-side (ctx.canUndoLastTurn, threaded from EditorShell).
      if (rawOps.length > 1 || historyAction || renderRequest || hasDraftMutation()) {
        rejected.push(reject(op.op, labelForOp(op), "capability_disabled", "undo needs to be the only thing asked for"));
        continue;
      }
      if (!ctx.canUndoLastTurn) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "nothing safe to undo"));
        continue;
      }
      historyAction = "undo";
    } else if (op.op === "repeat_last_edit") {
      if (rawOps.length > 1 || historyAction || renderRequest || hasDraftMutation()) {
        rejected.push(reject(op.op, labelForOp(op), "capability_disabled", "repeat needs to be the only thing asked for"));
        continue;
      }
      const lastOps = ctx.lastAppliedOps ?? [];
      if (lastOps.length === 0) {
        rejected.push(reject(op.op, labelForOp(op), "no_effect", "nothing to repeat yet"));
        continue;
      }
      // Re-run the prior turn's applied ops against the CURRENT snapshot —
      // atomic, so per-op fingerprint staleness rejects the WHOLE repeat and
      // every individual rejection still surfaces normally via `rejected`.
      const rerun = applyCopilotOpsAtomic(lastOps, ctx);
      if (rerun.rejected.length > 0) {
        rejected.push(...rerun.rejected);
        continue;
      }
      textActions.push(...rerun.textActions);
      if (rerun.nextSlots !== null) nextSlots = rerun.nextSlots;
      if (rerun.nextSfx != null) nextSfx = rerun.nextSfx;
      if (rerun.nextOverlays != null) nextOverlays = rerun.nextOverlays;
      if (rerun.nextCameraEffects != null) nextCameraEffects = rerun.nextCameraEffects;
      if (rerun.nextVisualBlocks != null) nextVisualBlocks = rerun.nextVisualBlocks;
      if (rerun.nextMotionScenes != null) nextMotionScenes = rerun.nextMotionScenes;
      if (rerun.nextCarouselMoment !== undefined) nextCarouselMoment = rerun.nextCarouselMoment;
      if (rerun.acceptedSuggestionRefs?.length) {
        acceptedSuggestionRefs = [...(acceptedSuggestionRefs ?? []), ...rerun.acceptedSuggestionRefs];
      }
      if (rerun.nextMusicTrackId !== undefined) nextMusicTrackId = rerun.nextMusicTrackId;
      if (rerun.nextMixLevel !== undefined) nextMixLevel = rerun.nextMixLevel;
      if (rerun.renderRequest) renderRequest = rerun.renderRequest;
      if (rerun.nextTitle !== undefined) nextTitle = rerun.nextTitle;
      if (rerun.captionMetaPatch !== undefined) captionMetaPatch = rerun.captionMetaPatch;
      if (rerun.openTool) openTool = rerun.openTool;
      // Provenance stays flat: record the ops that actually reapplied, never
      // the repeat_last_edit wrapper itself — otherwise a second "do that
      // again" would try to repeat a single self-referential op and recurse
      // forever.
      appliedOps.push(...(rerun.appliedOps ?? []));
      applied.push(...rerun.applied);
      historyAction = { kind: "repeat", ops: lastOps };
    }
    if (op.op !== "repeat_last_edit" && applied.length > appliedCountBeforeOp) {
      appliedOps.push(op);
    }
  }

  return {
    textActions,
    nextSlots,
    nextSfx,
    nextOverlays,
    nextCameraEffects,
    nextVisualBlocks,
    nextMotionScenes,
    nextCarouselMoment,
    acceptedSuggestionRefs,
    nextMusicTrackId,
    nextMixLevel,
    renderRequest,
    nextTitle,
    captionMetaPatch,
    openTool,
    applied: consolidateChips(applied),
    rejected,
    appliedOps,
    historyAction,
  };
}

/** Director suggestions are transactional: one invalid or stale operation
 * rejects the entire bundle. `applyCopilotOps` is pure, so discarding its
 * staged result guarantees no partial draft mutation. */
export function applyCopilotOpsAtomic(
  rawOps: readonly unknown[],
  ctx: ApplyCopilotOpsContext,
): ApplyCopilotOpsResult {
  const result = applyCopilotOps(rawOps, ctx);
  if (result.rejected.length === 0) return result;
  return {
    textActions: [],
    nextSlots: null,
    applied: [],
    rejected: result.rejected,
    appliedOps: [],
  };
}
