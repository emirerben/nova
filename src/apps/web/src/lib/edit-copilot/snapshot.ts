import { beatMarks, type DraftSlot } from "@/app/generative/timeline-math";
import { isBoundedCreatorImageAsset, type CameraEffect, type EditorCapabilities, type MediaOverlay, type OverlaySuggestion, type PendingSfxSuggestion, type PoolAsset, type SoundEffectPlacement, type VariantSpeechMap, type VisualBlock } from "@/lib/plan-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import type { EditorTransition } from "@/lib/generative-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { FONT_SIZE_MAP } from "@/lib/overlay-constants";
import {
  resolveLetterSpacingEm,
  resolveLineSpacing,
  resolveMaxWidthFrac,
} from "@/lib/overlay-layout";
import { sequentialSlotLayout } from "@/app/plan/items/[id]/_editor/editor-bar-drag";
import type { CopilotOpFamily } from "./ops";
import {
  CREATOR_BLOCK_CATALOG,
  creatorBlockEntry,
  type MotionPresetInstance,
} from "@nova/motion-runtime";

export const COPILOT_SNAPSHOT_MAX_BYTES = 18000;
export const COPILOT_BEAT_MARKS_MAX = 60;
/** Tighter fallback applied by trimSnapshotToBudget when the snapshot exceeds
 * the byte budget — a second, coarser sampling of the already-capped list. */
const BEAT_MARKS_TRIM_MAX = 30;
/** Speech caps mirror `_SPEECH_WORDS_SHOWN_MAX` / `_PAUSE_MARKS_SHOWN_MAX` in
 * the server renderer (app/agents/edit_copilot.py). Head-biased, never strided:
 * early words carry the hook window ("the first 4 seconds") and phrases must
 * stay contiguous for punchline reading. */
export const COPILOT_SPEECH_WORDS_MAX = 150;
export const COPILOT_PAUSE_MARKS_MAX = 40;
/** Budget-pressure fallback: head-cap words before dropping them entirely. */
const SPEECH_WORDS_TRIM_MAX = 60;
const COPILOT_SFX_SUGGESTIONS_MAX = 6;

function stableMutationValue(value: unknown): unknown {
  if (value === undefined) return { __nova_undefined__: true };
  if (Array.isArray(value)) return value.map(stableMutationValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableMutationValue(item)]),
    );
  }
  return value;
}

/** Compact opaque fingerprint for stale-target protection. It deliberately
 * covers editor persistence fields that are too noisy or sensitive to put in
 * the model-facing prose, while remaining deterministic across object-key order. */
function mutationFingerprint(parts: readonly unknown[]): string {
  const value = JSON.stringify(stableMutationValue(parts));
  let left = 2166136261;
  let right = 3339675911;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    left = Math.imul(left ^ code, 16777619);
    right = Math.imul(right ^ code, 2246822519);
  }
  return `m1-${(left >>> 0).toString(16).padStart(8, "0")}${(right >>> 0).toString(16).padStart(8, "0")}`;
}

function attachMutationFingerprint(
  target: { mutation_fingerprint?: string },
  fingerprint: string,
): void {
  // Keep the guard local-only: JSON.stringify, request payloads, prompt
  // formatters, and byte-budget accounting must never expose or count it.
  Object.defineProperty(target, "mutation_fingerprint", {
    value: fingerprint,
    enumerable: false,
    configurable: false,
    writable: false,
  });
}

export function textMutationFingerprint(bar: TextElementBar): string {
  return mutationFingerprint([bar]);
}

export function slotMutationFingerprint(slot: DraftSlot): string {
  return mutationFingerprint([slot]);
}

export function sfxMutationFingerprint(placement: SoundEffectPlacement): string {
  return mutationFingerprint([placement]);
}

export function overlayMutationFingerprint(card: MediaOverlay): string {
  const persisted = { ...card };
  // Signed URLs rotate independently of user edits and must not stale a card.
  delete persisted.preview_url;
  return mutationFingerprint([persisted]);
}

export function cameraEffectMutationFingerprint(effect: CameraEffect): string {
  return mutationFingerprint([effect]);
}

export function motionMutationFingerprint(scene: MotionPresetInstance): string {
  return mutationFingerprint([scene]);
}

/** Full local caption state, not the prompt's capped/80-character cue view. */
export function captionMutationFingerprint(bars: readonly TextElementBar[]): string {
  return mutationFingerprint(bars.filter((bar) => bar.role === "narrated_caption"));
}

/** Cap by even sampling, never by truncation — the FIRST and LAST marks are
 * always retained so late-video beats stay addressable for accents near the
 * end of the cut. Mirrored by `_BEAT_MARKS_SHOWN_MAX` in the server renderer
 * (app/agents/edit_copilot.py). */
function strideCapBeatMarks(marks: number[], cap: number): number[] {
  if (marks.length <= cap) return marks;
  const lastIndex = marks.length - 1;
  const sampled: number[] = [];
  for (let i = 0; i < cap; i++) {
    const value = marks[Math.round((i * lastIndex) / (cap - 1))];
    if (sampled.length === 0 || value !== sampled[sampled.length - 1]) {
      sampled.push(value);
    }
  }
  return sampled;
}

export interface CopilotClipLike {
  source_duration_s?: number | null;
  duration_s?: number | null;
  durationS?: number | null;
  moment?: string | null;
  moment_description?: string | null;
}

export interface CopilotTextSnapshotBar {
  index: number;
  id: string;
  text: string;
  start_s: number;
  end_s: number;
  role: Exclude<TextElementBar["role"], "narrated_caption">;
  font_family: string;
  size_px: number;
  color: string;
  highlight_color: string | null;
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
  /** Opaque, local stale-target guard; never rendered into model prose. */
  mutation_fingerprint?: string;
}

export interface CopilotSlotSnapshot {
  index: number;
  key: string;
  slot_id: string | null;
  clip_index: number;
  in_s: number;
  duration_s: number;
  removed: boolean;
  source_duration_s: number | null;
  moment: string | null;
  output_start_s: number | null;
  output_end_s: number | null;
  transition_after?: EditorTransition;
  transition_duration_s?: number | null;
  mutation_fingerprint?: string;
}

export interface CopilotCameraEffectSnapshot {
  index: number;
  id: string;
  start_s: number;
  end_s: number;
  intensity: number;
  effect_group_id?: string | null;
  mutation_fingerprint?: string;
}

export interface CopilotVisualBlockSnapshot {
  index: number;
  id: string;
  kind: VisualBlock["kind"];
  start_s: number;
  end_s: number;
  transition_in: "cut" | "fade";
  transition_out: "cut" | "fade";
}

export interface CopilotMotionBlockSnapshot {
  id: string;
  preset_id: MotionPresetInstance["preset_id"];
  label: string;
  start_s: number;
  end_s: number;
  palette: MotionPresetInstance["palette"];
  intensity: number;
  params: Record<string, unknown>;
  mutation_fingerprint?: string;
}

export interface CopilotMotionCatalogSnapshot {
  preset_id: string;
  label: string;
  kind: "text" | "media";
  default_duration_s: number;
  min_assets: number;
  defaults: Record<string, unknown>;
}

export interface CopilotSfxPlacementSnapshot {
  index: number;
  id: string;
  label: string | null;
  at_s: number;
  gain: number;
  duration_s: number | null;
  effect_group_id?: string | null;
  mutation_fingerprint?: string;
}

export interface CopilotSfxCatalogSnapshot {
  id: string;
  name: string;
  duration_s: number | null;
  /** Closed-vocabulary roles ("keyword_typewriter_tick", ...) — pick-by-fit. */
  role_tags?: string[];
}

export interface CopilotSfxSuggestionSnapshot {
  effect_id: string;
  at_s: number;
  gain: number | null;
  reason: string;
}

export interface CopilotSpeechWordSnapshot {
  text: string;
  start_s: number;
  end_s: number;
}

export interface CopilotSpeechPauseSnapshot {
  start_s: number;
  end_s: number;
  /** Word spoken just before the pause; null for a leading silence. */
  after: string | null;
}

export interface CopilotSpeechSnapshot {
  source: string;
  words: CopilotSpeechWordSnapshot[];
  pauses: CopilotSpeechPauseSnapshot[];
}

export interface CopilotOverlayCardSnapshot {
  index: number;
  id: string;
  kind: "image" | "video";
  start_s: number;
  end_s: number;
  position: "top" | "center" | "bottom" | "custom";
  x_frac: number;
  y_frac: number;
  scale: number;
  display_mode: "pip" | "fullscreen";
  source?: string | null;
  effect_group_id?: string | null;
  mutation_fingerprint?: string;
}

export interface CopilotOverlayAssetSnapshot {
  id: string;
  kind: "image" | "video";
  subject: string | null;
  duration_s: number | null;
}

export interface CopilotOverlaySuggestionSnapshot {
  id: string;
  reason: string;
  start_s: number;
  end_s: number;
}

export interface CopilotCaptionCueSnapshot {
  index: number;
  id: string;
  text: string;
  start_s: number;
  end_s: number;
  /** Server-authored role (chunker's SemanticRole vocabulary) — read-only,
   * never settable via a copilot op. Mirrors CaptionCue.smart_role. */
  smart_role?: "hook" | "context_shift" | "list_item" | "example" | "payoff" | "cta" | null;
  /** Toggled by set_caption_emphasis. Mirrors CaptionCue.smart_emphasis. */
  smart_emphasis?: boolean | null;
}

export interface CopilotCaptionMetaSnapshot {
  enabled: boolean;
  style: "sentence" | "word";
  font: string | null;
  y_frac: number;
  size_px?: number | null;
  color?: string | null;
  highlight_color?: string | null;
  stroke_width?: number | null;
  shadow_enabled?: boolean | null;
}

export interface CopilotMusicCandidateSnapshot {
  id: string;
  title: string;
}

export interface CopilotIntroSnapshot {
  layout: "linear" | "cluster";
  mode: string | null;
  text: string | null;
  word_count: number;
  sequence_capable: boolean;
  cluster_eligible: boolean;
  switch_blocked_reason: null | "unsaved_edits" | "manual_text_edits" | "read_only" | "rendering";
}

/**
 * Carousel-as-a-moment (Blossom carousel) — mirrors the editor's
 * `_editor_capabilities().carousel`/`carousel_reason` pair server-side. `current`
 * reflects whatever fields the variant's persisted `carousel_moment` actually
 * carries (an auto-authored moment can be sparse — mode/effect are the
 * director's choice at render time); absent fields are null. `mode` on
 * `current` may legally read "stills" (auto-authoring only) even though the
 * copilot itself must never emit that value.
 */
export interface CopilotCarouselSnapshot {
  eligible: boolean;
  reason: string | null;
  current: {
    position: "intro" | "middle" | "outro" | null;
    mode: "focus" | "rolling" | "stills" | null;
    effect: "scale_sweep" | "cover_flow" | "cards_stack" | "flipbook" | null;
    focus_clip_index: number | null;
    duration_s: number | null;
    transition: "crossfade" | "none" | null;
  } | null;
  n_clips: number;
}

export interface CopilotSnapshot {
  text_bars: CopilotTextSnapshotBar[];
  slots: CopilotSlotSnapshot[];
  has_narrated_captions: boolean;
  total_duration_s: number;
  max_duration_s: 60;
  remaining_duration_s: number;
  /** Beat positions projected into assembled-output seconds (grid variants only). */
  beat_marks?: number[];
  /** Spoken words + pauses (assembled-output seconds). Omitted when the variant
   * has no speech source or the local clip timeline is dirty (marks would be
   * stale against the shifted timeline — same discipline as beat marks). */
  speech?: CopilotSpeechSnapshot;
  sfx?: {
    placements: CopilotSfxPlacementSnapshot[];
    catalog: CopilotSfxCatalogSnapshot[];
    /** Advisory server suggestions, realizable via ordinary add_sfx ops. */
    suggestions?: CopilotSfxSuggestionSnapshot[];
  };
  overlays?: {
    cards: CopilotOverlayCardSnapshot[];
    asset_pool: CopilotOverlayAssetSnapshot[];
    pending_suggestions: CopilotOverlaySuggestionSnapshot[];
  };
  captions?: {
    total_cues: number;
    truncated: boolean;
    /** false = meta-only captions (subtitled talk-to-camera): style/font/enabled/
     * position apply via set_caption_meta, but cue text/timing edits belong to
     * the current draft and no cues are listed. */
    cues_editable: boolean;
    cues: CopilotCaptionCueSnapshot[];
    meta: CopilotCaptionMetaSnapshot;
    /** Opaque full-caption stale guard; never serialized to the model. */
    mutation_fingerprint?: string;
  };
  music?: {
    swappable: boolean;
    current_track_id: string | null;
    current_track_title: string | null;
    candidates: CopilotMusicCandidateSnapshot[];
  };
  mix?: {
    music_level: number | null;
  };
  intro?: CopilotIntroSnapshot;
  carousel?: CopilotCarouselSnapshot;
  title?: string;
  camera_effects?: CopilotCameraEffectSnapshot[];
  visual_blocks?: CopilotVisualBlockSnapshot[];
  motion?: {
    available: boolean;
    catalog: CopilotMotionCatalogSnapshot[];
    blocks: CopilotMotionBlockSnapshot[];
    asset_pool: Array<{ id: string; subject: string | null }>;
  };
  open_tools?: Array<"text" | "visuals" | "sounds" | "overlays" | "styles">;
  allowed_op_families: CopilotOpFamily[];
}

export function roundCopilotNumber(value: number): number {
  return Math.round(value * 1000) / 1000;
}

export interface AllowedOpFamilyOptions {
  sfxEnabled?: boolean;
  overlaysEnabled?: boolean;
  captionsPresent?: boolean;
  musicSwappable?: boolean;
  mixAllowed?: boolean;
  titleEditable?: boolean;
  openTools?: Array<"text" | "visuals" | "sounds" | "overlays" | "styles">;
  readOnly?: boolean;
  renderLayoutSwitchable?: boolean;
  /** Carousel-as-a-moment (Blossom carousel) is eligible for THIS variant right
   * now (mirrors capabilities.carousel === true, not read-only). Unlocks the
   * "render" family alongside (or independently of) renderLayoutSwitchable. */
  carouselMomentAvailable?: boolean;
  cameraEffectsEnabled?: boolean;
  transitionsEnabled?: boolean;
  visualBlocksEnabled?: boolean;
  motionScenesEnabled?: boolean;
}

export interface CaptionCueLike {
  id?: string | null;
  text: string;
  start_s: number;
  end_s: number;
  smart_role?: "hook" | "context_shift" | "list_item" | "example" | "payoff" | "cta" | null;
  smart_emphasis?: boolean | null;
}

export interface BuildCopilotSnapshotOptions extends AllowedOpFamilyOptions {
  /** Real video duration (seconds) — the total_duration_s fallback for
   * slot-less variants (subtitled talk-to-camera), whose layout total is 0. */
  videoDurationS?: number | null;
  sfxPlacements?: SoundEffectPlacement[];
  sfxCatalog?: SoundEffectSummary[];
  /** Server-derived spoken-word/pause map. Pass null (not the map) while the
   * local clip timeline is dirty — the persisted times no longer match. */
  speechMap?: VariantSpeechMap | null;
  /** Advisory SFX suggestions from the auto sound-design pass. */
  sfxSuggestions?: PendingSfxSuggestion[] | null;
  overlayCards?: MediaOverlay[];
  poolAssets?: PoolAsset[];
  pendingSuggestions?: OverlaySuggestion[];
  captionCues?: CaptionCueLike[];
  captionMeta?: CopilotCaptionMetaSnapshot;
  /** Default true. false = emit a meta-only captions section (no addressable
   * cues). `captionTotalCues` supplies the real cue count for display. */
  captionCuesEditable?: boolean;
  captionTotalCues?: number;
  musicState?: {
    swappable: boolean;
    currentTrackId: string | null;
    currentTrackTitle: string | null;
    candidates: MusicTrackSummary[] | CopilotMusicCandidateSnapshot[];
  };
  mixLevel?: number | null;
  intro?: CopilotIntroSnapshot;
  carousel?: CopilotCarouselSnapshot;
  title?: string | null;
  cameraEffects?: CameraEffect[];
  visualBlocks?: VisualBlock[];
  motionScenes?: MotionPresetInstance[];
}

function effectiveSizePx(bar: TextElementBar): number {
  if (typeof bar.size_px === "number" && Number.isFinite(bar.size_px)) {
    return Math.max(1, Math.trunc(bar.size_px));
  }
  return FONT_SIZE_MAP[bar.size_class ?? "medium"] ?? 72;
}

function allCoreCapabilitiesFalse(capabilities: EditorCapabilities | null | undefined): boolean {
  return !!capabilities &&
    capabilities.text_elements === false &&
    capabilities.timeline === false &&
    capabilities.split_clips === false &&
    capabilities.mix === false &&
    capabilities.sfx === false &&
    capabilities.overlays === false &&
    capabilities.motion_scenes !== true &&
    capabilities.visual_blocks !== true &&
    capabilities.camera_effects !== true;
}

export function allowedOpFamiliesFromCapabilities(
  capabilities: EditorCapabilities | null | undefined,
  options: AllowedOpFamilyOptions = {},
): CopilotOpFamily[] {
  if (options.readOnly) return [];
  if (allCoreCapabilitiesFalse(capabilities)) {
    return options.renderLayoutSwitchable || options.carouselMomentAvailable ? ["render"] : [];
  }
  const families: CopilotOpFamily[] = [];
  if (capabilities?.text_elements !== false) families.push("text");
  if (capabilities?.timeline !== false) families.push("clip");
  if (capabilities?.sfx !== false && options.sfxEnabled) families.push("sfx");
  if (capabilities?.overlays !== false && options.overlaysEnabled) families.push("overlay");
  if (options.captionsPresent) families.push("caption");
  if (options.musicSwappable || options.mixAllowed) families.push("music");
  if (options.renderLayoutSwitchable || options.carouselMomentAvailable) families.push("render");
  if (options.titleEditable !== false) families.push("title");
  if (capabilities?.camera_effects !== false && options.cameraEffectsEnabled) {
    families.push("effect");
  }
  if (capabilities?.timeline !== false && options.transitionsEnabled) {
    families.push("transition");
  }
  if (capabilities?.visual_blocks !== false && options.visualBlocksEnabled) {
    families.push("visual");
  }
  if (capabilities?.motion_scenes === true && options.motionScenesEnabled) {
    families.push("motion");
  }
  if ((options.openTools?.length ?? 0) > 0) families.push("tool");
  return families;
}

function sourceDurationForSlot(slot: DraftSlot, clips: CopilotClipLike[]): number | null {
  const clip = clips[slot.clipIndex];
  const source = clip?.source_duration_s ?? clip?.duration_s ?? clip?.durationS ?? null;
  return typeof source === "number" && Number.isFinite(source) ? source : null;
}

function truncate(value: string | null | undefined, max: number): string | null {
  if (value == null) return null;
  return value.slice(0, max);
}

function compactByteLength(value: unknown): number {
  const json = JSON.stringify(value);
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(json).length;
  return encodeURIComponent(json).replace(/%[0-9A-F]{2}/g, "x").length;
}

function trimSnapshotToBudget(snapshot: CopilotSnapshot): CopilotSnapshot {
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.captions && snapshot.captions.cues.length > 24) {
    snapshot.captions.cues = snapshot.captions.cues.slice(0, 24);
    snapshot.captions.truncated = true;
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.overlays && snapshot.overlays.asset_pool.length > 6) {
    snapshot.overlays.asset_pool = snapshot.overlays.asset_pool.slice(0, 6);
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.sfx && snapshot.sfx.catalog.length > 12) {
    snapshot.sfx.catalog = snapshot.sfx.catalog.slice(0, 12);
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.music && snapshot.music.candidates.length > 10) {
    snapshot.music.candidates = snapshot.music.candidates.slice(0, 10);
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.beat_marks && snapshot.beat_marks.length > BEAT_MARKS_TRIM_MAX) {
    snapshot.beat_marks = strideCapBeatMarks(snapshot.beat_marks, BEAT_MARKS_TRIM_MAX);
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.overlays && snapshot.overlays.pending_suggestions.length > 3) {
    snapshot.overlays.pending_suggestions = snapshot.overlays.pending_suggestions.slice(0, 3);
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  // Speech trims are staged: head-cap words (hook window survives) → drop words
  // but keep pauses (pause placement stays possible) → drop the section.
  if (snapshot.speech && snapshot.speech.words.length > SPEECH_WORDS_TRIM_MAX) {
    snapshot.speech.words = snapshot.speech.words.slice(0, SPEECH_WORDS_TRIM_MAX);
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.speech && snapshot.speech.words.length > 0) {
    snapshot.speech.words = [];
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  if (snapshot.speech) {
    delete snapshot.speech;
  }
  if (compactByteLength(snapshot) <= COPILOT_SNAPSHOT_MAX_BYTES) return snapshot;
  snapshot.slots = snapshot.slots.map((slot) => ({
    ...slot,
    moment: slot.moment == null ? null : slot.moment.slice(0, 40),
  }));
  return snapshot;
}

function optionsFromGridArg(
  gridOrOptions: number[] | BuildCopilotSnapshotOptions,
  maybeOptions?: BuildCopilotSnapshotOptions,
): { grid: number[]; options: BuildCopilotSnapshotOptions } {
  if (Array.isArray(gridOrOptions)) return { grid: gridOrOptions, options: maybeOptions ?? {} };
  return { grid: [], options: gridOrOptions ?? {} };
}

export function buildCopilotSnapshot(
  bars: TextElementBar[],
  slots: DraftSlot[],
  clips: CopilotClipLike[],
  capabilities?: EditorCapabilities | null,
  gridOrOptions: number[] | BuildCopilotSnapshotOptions = [],
  maybeOptions?: BuildCopilotSnapshotOptions,
): CopilotSnapshot {
  const { grid, options } = optionsFromGridArg(gridOrOptions, maybeOptions);
  const visibleBars = bars.filter(
    (bar): bar is TextElementBar & { role: Exclude<TextElementBar["role"], "narrated_caption"> } =>
      bar.role !== "narrated_caption",
  );
  const textBars: CopilotTextSnapshotBar[] = visibleBars.map((bar, index) => ({
    index,
    id: bar.id,
    text: bar.text,
    start_s: roundCopilotNumber(bar.start_s),
    end_s: roundCopilotNumber(bar.end_s),
    role: bar.role,
    font_family: bar.font_family ?? "PlayfairDisplay-Bold",
    size_px: effectiveSizePx(bar),
    color: bar.color ?? "#FFFFFF",
    highlight_color: bar.highlight_color ?? null,
    effect: bar.effect ?? "static",
    alignment: bar.alignment ?? "center",
    text_case: bar.text_case ?? "none",
    letter_spacing: resolveLetterSpacingEm(bar.letter_spacing),
    line_spacing: resolveLineSpacing(bar.line_spacing),
    max_width_frac: resolveMaxWidthFrac(bar.max_width_frac),
    stroke_width: bar.stroke_width ?? 0,
    position: bar.position ?? "middle",
    x_frac: bar.x_frac ?? null,
    y_frac: bar.y_frac ?? null,
  }));

  const layout = sequentialSlotLayout(slots, grid);
  const snapSlots: CopilotSlotSnapshot[] = slots.map((slot, index) => {
    const win = layout.windows[index];
    const durationS = roundCopilotNumber(win?.durationS ?? slot.durationS ?? 0);
    const outputStartS = win?.startS == null ? null : roundCopilotNumber(win.startS);
    return {
      index,
      key: slot.key,
      slot_id: slot.slotId,
      clip_index: slot.clipIndex,
      in_s: roundCopilotNumber(slot.inS),
      duration_s: durationS,
      removed: slot.removed,
      source_duration_s: sourceDurationForSlot(slot, clips),
      moment:
        slot.momentDescription ??
        clips[slot.clipIndex]?.moment ??
        clips[slot.clipIndex]?.moment_description ??
        null,
      output_start_s: outputStartS,
      output_end_s: outputStartS == null ? null : roundCopilotNumber(outputStartS + durationS),
      ...(slot.transitionAfter && slot.transitionAfter !== "cut"
        ? { transition_after: slot.transitionAfter }
        : {}),
      ...(slot.transitionDurationS == null
        ? {}
        : { transition_duration_s: roundCopilotNumber(slot.transitionDurationS) }),
    };
  });

  const captionBars = bars.filter((bar) => bar.role === "narrated_caption");
  const captionCues = options.captionCues ?? captionBars.map((bar) => ({
    id: bar.id,
    text: bar.text,
    start_s: bar.start_s,
    end_s: bar.end_s,
    smart_role: bar.smart_role ?? null,
    smart_emphasis: bar.smart_emphasis ?? null,
  }));
  const allowedOptions: AllowedOpFamilyOptions = {
    ...options,
    captionsPresent: options.captionsPresent ?? captionCues.length > 0,
    musicSwappable: options.musicState?.swappable ?? options.musicSwappable,
    mixAllowed: options.mixLevel !== undefined || options.mixAllowed,
    openTools: options.openTools,
  };
  const allowedFamilies = allowedOpFamiliesFromCapabilities(capabilities, allowedOptions);
  const allowed = new Set<CopilotOpFamily>(allowedFamilies);
  // Slot-less variants (subtitled talk-to-camera) have no clip timeline, so the
  // layout total is 0 — fall back to the real video duration. A 0 total is
  // poison downstream: every at_s/timing clamp collapses to second 0 (the
  // "all SFX placed at 0:00" bug) and the model reads a zero-length video.
  const layoutTotal = layout.totalDurationS;
  const fallbackTotal =
    typeof options.videoDurationS === "number" && Number.isFinite(options.videoDurationS)
      ? Math.max(0, options.videoDurationS)
      : 0;
  const total = roundCopilotNumber(layoutTotal > 0 ? layoutTotal : fallbackTotal);
  const snapshot: CopilotSnapshot = {
    text_bars: textBars,
    slots: snapSlots,
    has_narrated_captions: captionBars.length > 0,
    total_duration_s: total,
    max_duration_s: 60,
    remaining_duration_s: roundCopilotNumber(Math.max(0, 60 - total)),
    allowed_op_families: allowedFamilies,
  };
  const marks = strideCapBeatMarks(beatMarks(slots, grid), COPILOT_BEAT_MARKS_MAX);
  if (marks.length > 0) {
    snapshot.beat_marks = marks;
  }
  if (options.speechMap && options.speechMap.words.length > 0) {
    const speechWords = options.speechMap.words
      .slice(0, COPILOT_SPEECH_WORDS_MAX)
      .map((w) => ({
        text: truncate(w.w, 40) ?? "",
        start_s: roundCopilotNumber(w.s),
        end_s: roundCopilotNumber(w.e),
      }))
      .filter((w) => w.text.length > 0);
    if (speechWords.length > 0) {
      snapshot.speech = {
        source: options.speechMap.source,
        words: speechWords,
        pauses: (options.speechMap.pauses ?? [])
          .slice(0, COPILOT_PAUSE_MARKS_MAX)
          .map((p) => ({
            start_s: roundCopilotNumber(p.s),
            end_s: roundCopilotNumber(p.e),
            after: p.after == null ? null : truncate(p.after, 40),
          })),
      };
    }
  }
  if (
    allowed.has("sfx") &&
    (options.sfxPlacements || options.sfxCatalog || options.sfxSuggestions?.length)
  ) {
    snapshot.sfx = {
      placements: (options.sfxPlacements ?? []).slice(0, 15).map((placement, index) => ({
        index,
        id: placement.id,
        label: truncate(placement.label, 60),
        at_s: roundCopilotNumber(placement.at_s),
        gain: roundCopilotNumber(placement.gain),
        duration_s: placement.duration_s == null ? null : roundCopilotNumber(placement.duration_s),
        effect_group_id: placement.effect_group_id ?? null,
      })),
      catalog: (options.sfxCatalog ?? []).slice(0, 20).map((effect) => ({
        id: effect.id,
        name: truncate(effect.name, 32) ?? "",
        duration_s: effect.duration_s == null ? null : roundCopilotNumber(effect.duration_s),
        ...(effect.role_tags?.length ? { role_tags: effect.role_tags.slice(0, 6) } : {}),
      })),
    };
    const sfxSuggestions = (options.sfxSuggestions ?? [])
      .slice(0, COPILOT_SFX_SUGGESTIONS_MAX)
      .map((s) => ({
        effect_id: s.effect_id,
        at_s: roundCopilotNumber(s.at_s),
        gain: s.gain == null ? null : roundCopilotNumber(s.gain),
        reason: truncate(s.reason ?? "", 80) ?? "",
      }));
    if (sfxSuggestions.length > 0) {
      snapshot.sfx.suggestions = sfxSuggestions;
    }
  }
  if (allowed.has("overlay") && (options.overlayCards || options.poolAssets || options.pendingSuggestions)) {
    snapshot.overlays = {
      cards: (options.overlayCards ?? []).slice(0, 12).map((card, index) => ({
        index,
        id: card.id,
        kind: card.kind,
        start_s: roundCopilotNumber(card.start_s),
        end_s: roundCopilotNumber(card.end_s),
        position: card.position,
        x_frac: roundCopilotNumber(card.x_frac),
        y_frac: roundCopilotNumber(card.y_frac),
        scale: roundCopilotNumber(card.scale),
        display_mode: card.display_mode ?? "pip",
        source: card.source ?? null,
        effect_group_id: card.effect_group_id ?? null,
      })),
      asset_pool: (options.poolAssets ?? [])
        .filter((asset) => asset.status === "ready")
        .slice(0, 12)
        .map((asset) => ({
          id: asset.id,
          kind: asset.kind,
          subject: truncate(asset.subject, 60),
          duration_s: asset.duration_s == null ? null : roundCopilotNumber(asset.duration_s),
        })),
      pending_suggestions: (options.pendingSuggestions ?? []).slice(0, 6).map((suggestion) => ({
        id: suggestion.id,
        reason: truncate(suggestion.reason, 80) ?? "",
        start_s: roundCopilotNumber(suggestion.overlay.start_s),
        end_s: roundCopilotNumber(suggestion.overlay.end_s),
      })),
    };
  }
  if (allowed.has("visual") && options.visualBlocks) {
    snapshot.visual_blocks = options.visualBlocks.slice(0, 20).map((block, index) => ({
      index,
      id: block.id,
      kind: block.kind,
      start_s: roundCopilotNumber(block.start_s),
      end_s: roundCopilotNumber(block.end_s),
      transition_in: block.transition_in,
      transition_out: block.transition_out,
    }));
  }
  if (allowed.has("motion")) {
    snapshot.motion = {
      available: true,
      catalog: CREATOR_BLOCK_CATALOG.map((entry) => ({
        preset_id: entry.preset_id,
        label: entry.label,
        kind: entry.kind,
        default_duration_s: roundCopilotNumber(entry.default_duration_frames / 30),
        min_assets: entry.min_assets,
        defaults: JSON.parse(JSON.stringify(entry.defaults)) as Record<string, unknown>,
      })),
      blocks: (options.motionScenes ?? []).slice(0, 8).map((scene) => ({
        id: scene.id,
        preset_id: scene.preset_id,
        label: scene.preset_id === "route_trace" ? "Route trace" : creatorBlockEntry(scene.preset_id).label,
        start_s: roundCopilotNumber(scene.start_frame / 30),
        end_s: roundCopilotNumber(scene.end_frame_exclusive / 30),
        palette: { ...scene.palette },
        intensity: roundCopilotNumber(scene.intensity),
        params:
          scene.preset_id === "card_stack" || scene.preset_id === "film_strip"
            ? { asset_ids: scene.params.assets.map((asset) => asset.asset_id) }
            : (JSON.parse(
                JSON.stringify("params" in scene ? scene.params : {}),
              ) as Record<string, unknown>),
      })),
      asset_pool: (options.poolAssets ?? [])
        .filter(isBoundedCreatorImageAsset)
        .slice(0, 20)
        .map((asset) => ({ id: asset.id, subject: truncate(asset.subject, 60) })),
    };
  }
  const captionCuesEditable = options.captionCuesEditable !== false;
  if (
    allowed.has("caption") &&
    options.captionMeta &&
    (captionCues.length > 0 || !captionCuesEditable)
  ) {
    snapshot.captions = {
      total_cues: options.captionTotalCues ?? captionCues.length,
      truncated: captionCues.length > 40,
      cues_editable: captionCuesEditable,
      cues: captionCues.slice(0, 40).map((cue, index) => ({
        index,
        id: cue.id ?? `caption-${index}`,
        text: cue.text.slice(0, 80),
        start_s: roundCopilotNumber(cue.start_s),
        end_s: roundCopilotNumber(cue.end_s),
        smart_role: cue.smart_role ?? null,
        smart_emphasis: cue.smart_emphasis ?? null,
      })),
      meta: {
        enabled: options.captionMeta.enabled,
        style: options.captionMeta.style,
        font: options.captionMeta.font,
        y_frac: roundCopilotNumber(options.captionMeta.y_frac),
        size_px:
          typeof options.captionMeta.size_px === "number"
            ? Math.round(options.captionMeta.size_px)
            : null,
        color: options.captionMeta.color ?? null,
        highlight_color: options.captionMeta.highlight_color ?? null,
        stroke_width:
          typeof options.captionMeta.stroke_width === "number"
            ? Math.round(options.captionMeta.stroke_width)
            : null,
        shadow_enabled: options.captionMeta.shadow_enabled ?? null,
      },
    };
  }
  if (allowed.has("music") && options.musicState) {
    snapshot.music = {
      swappable: options.musicState.swappable,
      current_track_id: options.musicState.currentTrackId,
      current_track_title: truncate(options.musicState.currentTrackTitle, 40),
      candidates: options.musicState.candidates.slice(0, 20).map((track) => ({
        id: track.id,
        title: truncate(track.title, 40) ?? "",
      })),
    };
  }
  if (allowed.has("music") && options.mixLevel !== undefined) {
    snapshot.mix = {
      music_level: options.mixLevel == null ? null : roundCopilotNumber(options.mixLevel),
    };
  }
  if (options.intro) {
    snapshot.intro = {
      ...options.intro,
      text: truncate(options.intro.text, 300),
    };
  }
  if (options.carousel) {
    snapshot.carousel = {
      ...options.carousel,
      reason: truncate(options.carousel.reason, 200),
    };
  }
  if (allowed.has("title") && options.title != null) {
    snapshot.title = options.title.slice(0, 300);
  }
  if (allowed.has("effect")) {
    snapshot.camera_effects = (options.cameraEffects ?? []).slice(0, 20).map((effect, index) => ({
      index,
      id: effect.id,
      start_s: roundCopilotNumber(effect.start_s),
      end_s: roundCopilotNumber(effect.end_s),
      intensity: roundCopilotNumber(effect.intensity),
      effect_group_id: effect.effect_group_id ?? null,
    }));
  }
  if (allowed.has("tool") && options.openTools) {
    snapshot.open_tools = options.openTools.filter((tool, index, arr) => arr.indexOf(tool) === index);
  }
  const trimmed = trimSnapshotToBudget(snapshot);
  const textById = new Map(visibleBars.map((bar) => [bar.id, bar]));
  for (const item of trimmed.text_bars) {
    const bar = textById.get(item.id);
    if (bar) attachMutationFingerprint(item, textMutationFingerprint(bar));
  }
  const slotByKey = new Map(slots.map((slot) => [slot.key, slot]));
  for (const item of trimmed.slots) {
    const slot = slotByKey.get(item.key);
    if (slot) attachMutationFingerprint(item, slotMutationFingerprint(slot));
  }
  const sfxById = new Map((options.sfxPlacements ?? []).map((item) => [item.id, item]));
  for (const item of trimmed.sfx?.placements ?? []) {
    const placement = sfxById.get(item.id);
    if (placement) attachMutationFingerprint(item, sfxMutationFingerprint(placement));
  }
  const overlayById = new Map((options.overlayCards ?? []).map((item) => [item.id, item]));
  for (const item of trimmed.overlays?.cards ?? []) {
    const card = overlayById.get(item.id);
    if (card) attachMutationFingerprint(item, overlayMutationFingerprint(card));
  }
  const cameraById = new Map((options.cameraEffects ?? []).map((item) => [item.id, item]));
  for (const item of trimmed.camera_effects ?? []) {
    const effect = cameraById.get(item.id);
    if (effect) attachMutationFingerprint(item, cameraEffectMutationFingerprint(effect));
  }
  const motionById = new Map((options.motionScenes ?? []).map((item) => [item.id, item]));
  for (const item of trimmed.motion?.blocks ?? []) {
    const scene = motionById.get(item.id);
    if (scene) attachMutationFingerprint(item, motionMutationFingerprint(scene));
  }
  if (trimmed.captions) {
    attachMutationFingerprint(trimmed.captions, captionMutationFingerprint(captionBars));
  }
  return trimmed;
}
