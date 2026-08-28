"use client";

/**
 * EditorShell — the full-screen TikTok-parity editor at
 * /plan/items/[id]/edit?variant=<id> (plan §1, approved mockup Variant A).
 *
 * Full-viewport grid: 56px top bar / minmax(480px,1fr) canvas row / 260px
 * timeline region. Middle row: ToolRail · ToolDrawer · canvas · InspectorPanel
 * (~320px/w-80, PERMANENTLY reserved — the canvas never reflows on
 * select/deselect, D6). The Basic/Presets switch renders inline at the top
 * of InspectorPanel (Lane I, DESIGN.md §15) — the old floating InspectorRail
 * column is gone.
 *
 * First paint: drawer closed, no selection, inspector empty state, Select
 * tool active, playhead 0:00, video paused on frame 0.
 *
 * Working state = local reducer bars (text-timeline-reducer) + title. No
 * mid-edit server writes; Save persists once via commitEditorSession
 * (lib/editor-commit.ts — endpoint lands with the API task; a local 404
 * surfaces as the quiet retry notice and working state is preserved).
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  applyPlanItemCustomEffect,
  changePlanItemStyle,
  getPlanItem,
  getPlanItemJobStatus,
  getSfxAudioUrl,
  deletePoolAsset,
  editPlanItemVariant,
  getLyricSeeds,
  isBoundedCreatorImageAsset,
  LyricSeedsError,
  NotAuthenticatedError,
  listPoolAssets,
  uploadMediaOverlayFiles,
  reanalyzePoolAsset,
  retimeVisualBlock,
  updatePoolAssetContext,
  type CameraEffect,
  type CarouselMoment,
  type EditCopilotTurnResponse,
  type MediaOverlay,
  type MediaVisualBlock,
  type OverlaySuggestion,
  type PlanItem,
  type PlanItemVariant,
  type PoolAsset,
  type PoolReservationCapacity,
  type CaptionCue,
  type CaptionLanguage,
  setPlanItemCaptionLanguage,
  type SoundEffectPlacement,
  type TextElement,
  type VisualBlock,
} from "@/lib/plan-api";
import { mergePoolAssetsPreservingDisplayUrls } from "@/lib/pool-assets";
import type { CarouselClipThumb } from "./CarouselPanel";
import type { NovaStep } from "@/lib/job-phases";
import { POLL_INTERVAL_MS } from "@/components/progress";
import { normalizeCameraEffect } from "@/lib/camera-effects";
import {
  removeGeneratedEffectGroup,
  removeOverlayEffectGroup,
} from "@/lib/overlay-effect-groups";
import { getSoundEffects, type SoundEffectSummary } from "@/lib/sfx-api";
import { getMusicTracks, type MusicTrackSummary } from "@/lib/music-api";
import { canvasForOrientation } from "@/lib/overlay-constants";
import { type TextBoxHorizontalPosition } from "@/lib/overlay-layout";
import {
  buildEditorCommitRequest,
  commitEditorSession,
  editorCommitBaseGeneration,
  EditorCommitConflictError,
  EditorCommitNetworkError,
  type AcceptedSuggestionRef,
  type EditorCommitBackgroundMusic,
  type EditorCommitLyricsRequest,
} from "@/lib/editor-commit";
import { captionMetaFromVariant } from "@/lib/caption-meta";
import {
  resizeCarouselTiming,
  shouldAutoUpgradeCarouselTiming,
  upgradeCarouselTiming,
} from "@/lib/carousel-timing";
import {
  buildPlanItemEditorReturnHref,
  editorCommitStartedRender,
} from "@/lib/editor-return";
import { FONT_FACES } from "@/lib/font-faces";
import {
  type GenerativeStyleSet,
  type EditorTransition,
  type LookAdjustments,
  type LookPreset,
  resolveCarouselFocusClipIndex,
} from "@/lib/generative-api";
import {
  defaultLookAdjustments,
  lookAdjustmentsEqual,
  resolveLookAdjustments,
} from "@/lib/look-presets";
import { formatTimecode } from "@/lib/timeline/time-format";
import { DEFAULT_TEXT_PRESET, TEXT_PRESETS, type TextPreset } from "@/lib/text-presets";
import {
  applyCopilotOps,
  applyCopilotOpsAtomic,
  type ApplyCopilotOpsContext,
  type ApplyCopilotOpsResult,
} from "@/lib/edit-copilot/apply-ops";
import {
  allowedOpFamiliesFromCapabilities,
  buildCopilotSnapshot,
  type CopilotCaptionMetaSnapshot,
  type CopilotCarouselSnapshot,
  type CopilotHistoryStateSnapshot,
  type CopilotSnapshot,
  type CopilotSnapshotContext,
} from "@/lib/edit-copilot/snapshot";
import {
  COPILOT_UNAVAILABLE_MESSAGE,
  summarizeAppliedTurn,
  useEditCopilot,
} from "@/lib/edit-copilot/useEditCopilot";
import {
  useEditDirector,
  type DirectorApplyPresentation,
  type DirectorPreviewFocus,
} from "@/lib/edit-copilot/useEditDirector";
import type { CaptionMetaPatch, CopilotOp } from "@/lib/edit-copilot/ops";
import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";
import {
  motionPatchForConfig,
  motionPatchForManualEnd,
  motionPatchForEffect,
  motionPatchForText,
  type TextMotionConfigV2,
} from "@/lib/text-motion-v2";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { useFocusTrap } from "@/components/ui/useFocusTrap";
import UnifiedTimeline from "@/app/plan/_components/UnifiedTimeline";
import { useClipTimeline } from "@/app/plan/_components/useClipTimeline";
import { nextAddedKey, type DraftSlot } from "@/app/generative/timeline-math";
import { timelineReducer } from "@/app/generative/timeline-reducer";
import {
  barsToCaptionCues,
  barsToPreviewTextElements,
  barsToTextElements,
  buildLyricLineOverrides,
  buildCaptionTextReplacement,
  captionBarPatchFromMetaPatch,
  captionMetaPatchFromCaptionBarPatch,
  isCaptionBar,
  isLyricBar,
  localCaptionBarPatchFromPatch,
  seedBarsFromLyricSeeds,
  seedBarsFromVariant,
} from "./editor-bars";
import { isCaptionArchetype } from "@/lib/variant-editor/eligibility";
import {
  canEditIntroControls,
  captionToolState,
  computeToolDisabledReasons,
  editorReasonCopy,
  isElementsLyricsModel,
  lyricsFeatureAvailable as computeLyricsFeatureAvailable,
  textElementsLockedCopy,
} from "./editor-capabilities";
import {
  canEditClip,
  canEditLane,
  canEditMusic,
  hasGuidedOperationCapabilities,
  operationDisabledReason,
} from "./editor-operation-capabilities";
import {
  resolveSmartPlacementAssignments,
  isMasonryVariant,
  resolveSmartPlacementCandidate,
  resolveSmartPlacementCandidates,
  smartPlacementPatchForBar,
  collageMotionForTextBar,
  textBoxPositionPatchForBar,
  textBoxScreenXFrac,
} from "./editor-smart-placement";
import {
  buildTimedTextSequence,
  TEXT_ELEMENTS_API_MAX,
} from "./editor-text-composition";
import {
  activeSlotCount,
  canSplitSlotAt,
  deleteSlotEnforceFloor,
  splitSlotAt,
} from "./slot-split";
import {
  applyManualClipTimingPatch,
  applyTextTimingInput,
  minimumClipDurationForSlot,
  outputTimeForSlotBoundary,
  rangesDiffer,
  sequentialSlotLayout,
} from "./editor-bar-drag";
import { placeAfterSelected } from "./editor-bar-drag";
import TransportBar from "./TransportBar";
import type {
  EditorMotionBar,
  EditorTimelineBodyProps,
} from "./EditorTimelineBody";
import EditorCanvas from "./EditorCanvas";
import OverlaySuggestions from "./OverlaySuggestions";
import { usePoolAssetUploader } from "@/app/plan/_hooks/usePoolAssetUploader";
import {
  copyMediaPreviewForDuplicate,
  mediaOverlayPatchToVisualPatch,
  mediaOverlayToVisualBlock,
  duplicateMediaVisualBlock,
  normalizeMediaVisualBlock,
  removeMediaPreview,
  reorderMediaVisualBlocks,
  type MediaLayerMove,
} from "./editor-media-visuals";
import { computeReseedSections } from "./editor-reseed";
import InspectorPanel from "./InspectorPanel";
import type { InspectorTab } from "./InspectorRail";
import ToolDrawer from "./ToolDrawer";
import Sheet from "./Sheet";
import { ToolDock, type DockTool } from "./ToolDock";
import { ContextStrip, type StripSelection } from "./ContextStrip";
import {
  MiniStrip,
  type MiniStripLane,
  type MiniStripLaneItem,
  type MiniStripLaneTimingPatch,
  type MiniStripSegment,
} from "./MiniStrip";
import {
  initialPocketState,
  pocketReducer,
  type PocketTool,
} from "./mobile-editor-state";
import { ArrowLeft as ArrowLeftIcon } from "lucide-react";
import { PauseIcon, PlayIcon, RedoIcon, UndoIcon } from "./editor-icons";
import type {
  CaptionCueRow,
  CaptionsBusyState,
  CaptionsDrawerControl,
} from "./CaptionsDrawer";
import ToolRail, { type EditorTool } from "./ToolRail";
import type { SongWindowState } from "./SongWindowSelector";
import PresetGrid, { presetMatchesFields } from "./PresetGrid";
import { useVirtualPreview } from "./useVirtualPreview";
import {
  createEditorPlaybackClock,
  type EditorPlaybackClock,
} from "./editor-playback-clock";
import { useEditorLayoutMode } from "./useEditorLayoutMode";
import type { EditorLayoutMode } from "./useEditorLayoutMode";
import {
  projectBaseTime,
  projectBaseRange,
  slotsDifferFromBaseline,
  virtualDeckLookAdjustmentsAtTime,
  virtualDeckLookPresetsAtTime,
  unprojectOutputTime,
  type VirtualCarouselSplice,
} from "./virtual-timeline";
import {
  deleteKeyAllowed,
  escapeAction,
  nudgeBarStart,
  type EditorSelectionKind,
  useEditorSelection,
} from "./useEditorSelection";
import {
  draftKey,
  deserializeDraft,
  serializeDraft,
  useEditorHistory,
  type EditorDocument,
} from "./useEditorHistory";
import {
  isUnavailableError,
  SUGGESTION_POLL_INTERVAL_MS,
  useEditorOverlaySuggestions,
} from "./useEditorOverlaySuggestions";
import {
  creatorBlockEntry,
  creatorBlockDurationFramesV2,
  createCreatorBlockInstance,
  MOTION_FPS,
  MOTION_MAX_INSTANCES,
  MOTION_RUNTIME_HASH,
  isCompatiblePersistedMotionRuntimeHash,
  retimeCreatorBlockManualSpan,
  retimeCreatorBlockSpeed,
  upgradeCreatorBlockInstanceToV2,
  validateMotionInstances,
  type MotionPresetId,
  type MotionPresetInstance,
  type MotionPresetPatch,
} from "@nova/motion-runtime";
import type { CreatorBlockMotionControlPatch } from "./MotionInspector";

const ZOOM_OPTIONS = [100, 125, 150] as const;

function revokeLocalObjectUrl(url: string | null | undefined): void {
  if (url?.startsWith("blob:") && typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(url);
  }
}

/** Default duration + look of a freshly added text bar (plan §2). */
const NEW_TEXT_DURATION_S = 2.0;
const NEW_TEXT_CONTENT = "Add a title";
const NEW_TEXT_Y_FRAC = 0.4;
const NEW_TEXT_SIZE_PX = 64;
const COMPOSITION_TEXT_PRESET =
  TEXT_PRESETS.find((preset) => preset.id === "editorial-italic") ?? DEFAULT_TEXT_PRESET;
const COMPOSITION_Y_FRACS = [0.36, 0.44, 0.52] as const;
const COPILOT_SAVE_NOTICE_KEY = "nova-copilot-save-expectation-dismissed";
const MEDIA_OVERLAYS_RAW = (process.env.NEXT_PUBLIC_MEDIA_OVERLAYS_ENABLED ?? "").trim();
const MEDIA_OVERLAYS_UI_ENABLED =
  MEDIA_OVERLAYS_RAW.toLowerCase() === "true" || MEDIA_OVERLAYS_RAW === "1";
const SOUND_EFFECTS_UI_ENABLED = process.env.NEXT_PUBLIC_SOUND_EFFECTS_ENABLED === "true";
const EDIT_TRANSITIONS_UI_ENABLED =
  process.env.NEXT_PUBLIC_EDIT_TRANSITIONS_ENABLED === "true";
const EDIT_DIRECTOR_UI_ENABLED =
  process.env.NEXT_PUBLIC_EDIT_DIRECTOR_ENABLED === "true";
const OMNI_GENERATED_VIDEO_UI_ENABLED =
  process.env.NEXT_PUBLIC_OMNI_GENERATED_VIDEO_ENABLED === "true";
const VISUAL_BLOCKS_UI_ENABLED =
  process.env.NEXT_PUBLIC_VISUAL_BLOCKS_ENABLED === "true";
const MOTION_SCENES_UI_ENABLED =
  process.env.NEXT_PUBLIC_MOTION_SCENES_ENABLED === "true";
const EVOLVING_TYPE_PUBLIC_ENABLED =
  process.env.NEXT_PUBLIC_EVOLVING_TYPE_ENABLED === "true";
const FRAME_DRIVEN_PREVIEW_ENABLED =
  process.env.NEXT_PUBLIC_FRAME_DRIVEN_PREVIEW_ENABLED === "true";
const TEXT_MOTION_V2_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";
// Nova AI sandboxed effect language (PR6, effect-language train). Dual-flag
// with the backend's CUSTOM_EFFECTS_ENABLED (app/config.py) — Fly first,
// then Vercel, per this repo's dual-flag convention.
const CUSTOM_EFFECTS_UI_ENABLED =
  process.env.NEXT_PUBLIC_CUSTOM_EFFECTS_ENABLED === "true";
const LYRICS_EDITOR_UI = process.env.NEXT_PUBLIC_LYRICS_EDITOR_ENABLED === "true";
// Lyrics-optional "elements" model: instant toggle-insert/remove of
// beat-synced lyric bars, no render round-trip. Independent of
// LYRICS_EDITOR_UI (the legacy baked-lyrics bar editor) — see
// lyricsFeatureAvailable/isElementsLyricsModel in editor-capabilities.ts.
const LYRICS_OPTIONAL_UI = process.env.NEXT_PUBLIC_LYRICS_OPTIONAL_ENABLED === "true";
const LANDSCAPE_UI = process.env.NEXT_PUBLIC_LANDSCAPE_OUTPUT_ENABLED === "true";
/** Pocket editor (mobile full-parity light mode). Default ON — the env var is
 * the KILL SWITCH: set NEXT_PUBLIC_MOBILE_EDITOR_ENABLED="false" (+ redeploy;
 * NEXT_PUBLIC vars bake at build time) to restore the legacy light mode
 * (canvas + chat only), byte-identical. */
const POCKET_UI = process.env.NEXT_PUBLIC_MOBILE_EDITOR_ENABLED !== "false";
const POCKET_TOOL_TITLES: Record<PocketTool, string> = {
  text: "Text",
  captions: "Captions",
  visuals: "Visuals",
  sounds: "Sounds",
  overlays: "Overlays",
  styles: "Styles",
};

type EditorOrientation = "portrait" | "landscape";

function patchVisualBlockConcreteTiming(
  block: VisualBlock,
  patch: Partial<VisualBlock>,
): VisualBlock {
  const next = { ...block, ...patch } as VisualBlock;
  if (
    next.kind !== "montage" ||
    (typeof patch.start_s !== "number" && typeof patch.end_s !== "number")
  ) {
    return next;
  }
  const oldDuration = Math.max(0.001, block.end_s - block.start_s);
  const newDuration = Math.max(0.001, next.end_s - next.start_s);
  let offset = 0;
  next.shots = next.shots.map((shot, index) => {
    const duration_s =
      index === next.shots.length - 1
        ? newDuration - offset
        : (shot.duration_s / oldDuration) * newDuration;
    const resized = { ...shot, start_offset_s: offset, duration_s };
    offset += duration_s;
    return resized;
  });
  return next;
}

function retimeLinkedTextBar(
  bar: TextElementBar,
  block: VisualBlock,
  start_s: number,
  end_s: number,
): Pick<TextElementBar, "start_s" | "end_s"> {
  const oldDuration = Math.max(0.001, block.end_s - block.start_s);
  const newDuration = Math.max(0.001, end_s - start_s);
  return {
    start_s:
      start_s +
      Math.max(0, Math.min(1, (bar.start_s - block.start_s) / oldDuration)) *
        newDuration,
    end_s:
      start_s +
      Math.max(0, Math.min(1, (bar.end_s - block.start_s) / oldDuration)) *
        newDuration,
  };
}
type LyricsCapability = NonNullable<
  NonNullable<PlanItemVariant["editor_capabilities"]>["lyrics"]
>;

function textTimingAtPlayhead({
  currentTime,
  previewDuration,
}: {
  currentTime: number;
  previewDuration: number;
}): Pick<TextElementBar, "start_s" | "end_s"> {
  const start = Math.max(0, Math.round(currentTime * 10) / 10);
  const end =
    previewDuration > 0
      ? Math.min(previewDuration, start + NEW_TEXT_DURATION_S)
      : start + NEW_TEXT_DURATION_S;
  return {
    start_s: start,
    end_s: Math.max(end, start + 0.5),
  };
}

function newTextBar({
  id,
  text,
  timing,
  preset,
}: {
  id: string;
  text: string;
  timing: Pick<TextElementBar, "start_s" | "end_s">;
  preset: TextPreset;
}): TextElementBar {
  return {
    id,
    text,
    start_s: timing.start_s,
    end_s: timing.end_s,
    role: "generative_intro",
    x_frac: 0.5,
    y_frac: NEW_TEXT_Y_FRAC,
    position: "custom",
    size_px: NEW_TEXT_SIZE_PX,
    alignment: "center",
    font_family: preset.fields.font_family ?? undefined,
    color: preset.fields.color ?? undefined,
    highlight_color: preset.fields.highlight_color ?? undefined,
    stroke_width: preset.fields.stroke_width ?? undefined,
    shadow_enabled: false,
    effect: preset.fields.effect ?? undefined,
  };
}

function defaultLyricsCapability(variant: PlanItemVariant | null): LyricsCapability {
  return {
    editable: false,
    enabled: variant?.text_mode === "lyrics",
    can_toggle_on: false,
    reason: "disabled",
  };
}

function persistedLyricsEnabled(variant: PlanItemVariant | null): boolean {
  return variant?.lyrics_enabled ?? (variant?.text_mode === "lyrics");
}

function persistedOrientation(variant: PlanItemVariant | null): EditorOrientation {
  // `orientation.value` is the read adapter's authoritative projection of the
  // format that actually rendered. Prefer it so older variants that predate
  // the top-level `orientation` field still open on the correct canvas.
  const capabilityValue = variant?.editor_capabilities?.orientation?.value;
  if (capabilityValue === "landscape" || capabilityValue === "portrait") {
    return capabilityValue;
  }
  return variant?.orientation === "landscape" ? "landscape" : "portrait";
}

function cameraEffectsEqual(
  left: readonly CameraEffect[] | null | undefined,
  right: readonly CameraEffect[] | null | undefined,
): boolean {
  const normalizeList = (effects: readonly CameraEffect[] | null | undefined) =>
    (effects ?? [])
      .map((effect) => normalizeCameraEffect({ ...effect }))
      .sort((a, b) => a.start_s - b.start_s || a.id.localeCompare(b.id));
  return stableJson(normalizeList(left)) === stableJson(normalizeList(right));
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function lyricsToggleHint(reason: "disabled" | "no_track" | "no_renderable_lyrics" | null): string | null {
  if (reason === "no_track") return "Add a song first — use Swap song";
  if (reason === "no_renderable_lyrics") return "This song doesn't have synced lyrics";
  if (reason === "disabled") return "Lyrics editing is turned off right now";
  return null;
}

function orientationDisabledHint(reason: string | null | undefined): string | null {
  if (reason === "orientation_unsupported") {
    return "This edit style doesn't support landscape yet";
  }
  if (reason === "disabled") return "Landscape output is turned off right now";
  return null;
}

export function spaceShortcutAllowed(target: HTMLElement | null): boolean {
  if (!deleteKeyAllowed(target)) return false;
  return (target?.tagName ?? "").toUpperCase() !== "BUTTON";
}

const CAROUSEL_SELECTION_ID = "carousel-block";

export function shouldCloseToolOnSelection({
  layoutMode,
  activeTool,
  preserveOverlayTool,
}: {
  layoutMode: EditorLayoutMode;
  activeTool: EditorTool | null;
  preserveOverlayTool?: boolean;
}): boolean {
  return layoutMode === "overlay" && activeTool !== "nova" && !preserveOverlayTool;
}

export function resolveCopilotApplyFeedback({
  result,
  bars,
  beforeSlots,
  grid,
}: {
  result: ApplyCopilotOpsResult;
  bars: TextElementBar[];
  beforeSlots: DraftSlot[];
  grid: number[];
}): {
  textIds: string[];
  slotIds: string[];
  first:
    | { kind: "text"; id: string; seekS: number }
    | { kind: "clip"; id: string; seekS: number }
    | null;
} {
  const textIds = result.textActions.flatMap((action) => {
    if ("id" in action) return [action.id];
    if (action.type === "ADD_TEXT") return [action.bar.id];
    if (action.type === "PATCH_BARS") return action.patches.map((patch) => patch.id);
    return [];
  });
  const slotIds = result.nextSlots
    ? result.nextSlots
        .filter((slot) => {
          const before = beforeSlots.find((s) => s.key === slot.key);
          return !before || JSON.stringify(before) !== JSON.stringify(slot);
        })
        .map((slot) => slot.key)
    : [];

  // Never select/seek to a just-deleted element — selecting a DELETE_BAR
  // target points at a ghost id (and light mode would open the edit sheet for
  // a bar that no longer exists) (review F6). Deleted targets still flash on
  // the timeline; selection goes to the first SURVIVING changed element.
  const firstTextAction = result.textActions.find((action) => action.type !== "DELETE_BAR");
  if (firstTextAction) {
    const id =
      "id" in firstTextAction
        ? firstTextAction.id
        : firstTextAction.type === "ADD_TEXT"
          ? firstTextAction.bar.id
          : firstTextAction.type === "PATCH_BARS"
            ? firstTextAction.patches[0]?.id ?? null
          : null;
    const bar =
      firstTextAction.type === "ADD_TEXT"
        ? firstTextAction.bar
        : id
          ? bars.find((b) => b.id === id) ?? null
          : null;
    if (id && bar) {
      return { textIds, slotIds, first: { kind: "text", id, seekS: (bar.start_s + bar.end_s) / 2 } };
    }
  }

  if (result.nextSlots) {
    const layout = sequentialSlotLayout(result.nextSlots, grid);
    for (const slotId of slotIds) {
      const nextIndex = result.nextSlots.findIndex((slot) => slot.key === slotId);
      const slot = result.nextSlots[nextIndex];
      if (!slot || slot.removed) continue;
      const win = layout.windows[nextIndex];
      return {
        textIds,
        slotIds,
        first: { kind: "clip", id: slotId, seekS: win?.startS ?? 0 },
      };
    }
  }

  return { textIds, slotIds, first: null };
}

function SelectCursorIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 3l8 18 2.2-7.2L21 11 4 3z" />
      <path d="M13.5 13.5 19 19" />
    </svg>
  );
}

function PanHandIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M8 11V6.5a2 2 0 0 1 4 0V11" />
      <path d="M12 11V5.5a2 2 0 0 1 4 0V12" />
      <path d="M16 12V8.5a2 2 0 0 1 4 0V15" />
      <path d="M8 12.5V10a2 2 0 0 0-4 0v4.5C4 19 7 22 12 22h1c4 0 7-3 7-7" />
    </svg>
  );
}

function SaveSpinner() {
  return (
    <span
      aria-hidden="true"
      className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white"
    />
  );
}

function OrientationToggle({
  value,
  disabled,
  busy,
  disabledHint,
  onChange,
}: {
  value: EditorOrientation;
  disabled: boolean;
  busy: boolean;
  disabledHint: string | null;
  onChange: (orientation: EditorOrientation) => void;
}) {
  const title = disabled ? (disabledHint ?? "Landscape isn't available for this edit.") : "Output format";
  return (
    <div
      role="group"
      aria-label="Output format"
      aria-busy={busy}
      title={title}
      className="flex min-h-11 items-center rounded-md border border-border bg-background p-0.5"
    >
      {(["portrait", "landscape"] as const).map((option) => {
        const selected = value === option;
        return (
          <Button
            key={option}
            type="button"
            variant="ghost"
            aria-label={option === "portrait" ? "Use 9:16 output" : "Use 16:9 output"}
            aria-pressed={selected}
            disabled={disabled}
            onClick={() => onChange(option)}
            className={[
              "h-10 min-h-0 min-w-[54px] rounded-md px-2 text-[12px] font-semibold normal-case tracking-normal",
              selected
                ? "bg-foreground text-background hover:bg-foreground"
                : "text-muted-foreground",
            ].join(" ")}
          >
            {option === "portrait" ? "9:16" : "16:9"}
          </Button>
        );
      })}
    </div>
  );
}

export default function EditorShell({
  itemId,
  variantParam,
}: {
  itemId: string;
  variantParam: string | null;
}) {
  const router = useRouter();
  // Chat steps feed (PR4): server-render turns (set_intro_layout) show a
  // disclosure + live NovaActivityFeed in the drawer instead of a receipt
  // pill. Flag off is byte-identical to today's pill behavior.
  const stepsFeedEnabled = process.env.NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED === "true";

  // ── Data ────────────────────────────────────────────────────────────────────
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [item, setItem] = useState<PlanItem | null>(null);
  const [variants, setVariants] = useState<PlanItemVariant[]>([]);
  const [loadNonce, setLoadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    (async () => {
      try {
        const it = await getPlanItem(itemId);
        const job = it.current_job_id
          ? await getPlanItemJobStatus(it.current_job_id)
          : null;
        if (cancelled) return;
        setItem(it);
        setVariants(job?.variants ?? []);
        setLoading(false);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof NotAuthenticatedError) setNeedsAuth(true);
        else setLoadError("We couldn't load this video. Try again.");
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [itemId, loadNonce]);

  const variant = useMemo(() => {
    if (variants.length === 0) return null;
    return (
      variants.find((v) => v.variant_id === variantParam) ??
      variants.find((v) => v.output_url || v.base_video_url) ??
      variants[0]
    );
  }, [variants, variantParam]);

  // ── Working state ───────────────────────────────────────────────────────────
  const [state, dispatch] = useReducer(textReducer, initTextEditorState([]));
  // Originals by id — Save merges bar edits OVER these so fields the editor
  // doesn't model (reveal_s, word_timings, …) survive untouched.
  const originalsRef = useRef<Map<string, TextElement>>(new Map());
  const captionOriginalsRef = useRef<Map<string, CaptionCue>>(new Map());
  const seededVariantIdRef = useRef<string | null>(null);
  // Conflict-tile Reload: the refetched variant must replace working state in
  // sections the user hasn't touched (an AI auto-apply or another tab moved
  // them), while dirty sections keep the user's edits. Without this, the
  // seeding guard above skips the refetch entirely and the NEXT Save clobbers
  // the other writer's changes with a freshly-blessed baseline.
  const conflictReseedRef = useRef(false);
  const [title, setTitle] = useState("");
  // Last style-set applied via restyle-all — drives the StyleChip ring.
  const [appliedStyleSetId, setAppliedStyleSetId] = useState<string | null>(null);
  const [localSfx, setLocalSfx] = useState<SoundEffectPlacement[]>([]);
  const [localSfxAudioUrls, setLocalSfxAudioUrls] = useState<Record<string, string>>({});
  const [localOverlays, setLocalOverlays] = useState<MediaOverlay[]>([]);
  const [localVisualBlocks, setLocalVisualBlocks] = useState<VisualBlock[]>([]);
  const [localMotionScenes, setLocalMotionScenes] = useState<MotionPresetInstance[]>([]);
  const [localCameraEffects, setLocalCameraEffects] = useState<CameraEffect[]>([]);
  // AI-suggestion provenance (Overlays drawer): accepted envelope id + the
  // overlay card id it staged. Kept OFF the MediaOverlay objects — the save
  // filters these against the staged overlay ids, so an undone accept is
  // never resolved server-side.
  const [acceptedSuggestions, setAcceptedSuggestions] = useState<AcceptedSuggestionRef[]>([]);
  const suggestedOverlayIds = useMemo(
    () => new Set(acceptedSuggestions.map((a) => a.overlayId)),
    [acceptedSuggestions],
  );
  const [localOverlayPreviewUrls, setLocalOverlayPreviewUrls] = useState<Record<string, string>>({});
  const localOverlayPreviewUrlsRef = useRef<Record<string, string>>({});
  const [localVisualPreviewUrls, setLocalVisualPreviewUrls] = useState<Record<string, string>>({});
  const localVisualPreviewUrlsRef = useRef<Record<string, string>>({});
  const [sfxDirty, setSfxDirty] = useState(false);
  const [overlaysDirty, setOverlaysDirty] = useState(false);
  const [visualBlocksDirty, setVisualBlocksDirty] = useState(false);
  const [motionScenesDirty, setMotionScenesDirty] = useState(false);
  const motionControlGestureOriginRef = useRef<{
    document: EditorDocument;
    motionScenesDirty: boolean;
  } | null>(null);
  const [cameraEffectsDirty, setCameraEffectsDirty] = useState(false);
  const [mixLevel, setMixLevel] = useState<number | null>(null);
  const [mixDirty, setMixDirty] = useState(false);
  const [textDirty, setTextDirty] = useState(false);
  const [captionDirty, setCaptionDirty] = useState(false);
  const [lyricsEnabled, setLyricsEnabled] = useState(false);
  const [orientation, setOrientation] = useState<EditorOrientation>("portrait");
  const [titleDirty, setTitleDirty] = useState(false);
  const [captionMeta, setCaptionMeta] = useState<CopilotCaptionMetaSnapshot | null>(null);
  const [captionMetaDirty, setCaptionMetaDirty] = useState(false);
  const [captionMetaPatch, setCaptionMetaPatch] = useState<CaptionMetaPatch>({});
  // Captions drawer async state. Named stages, not one boolean: "Saving your
  // edits" and "Re-transcribing" are two steps of the language switch that fail
  // differently, and the user has to know which one they are in.
  const [captionsBusy, setCaptionsBusy] = useState<CaptionsBusyState>("idle");
  const [captionsError, setCaptionsError] = useState<string | null>(null);
  const lyricsCap = variant?.editor_capabilities?.lyrics ?? defaultLyricsCapability(variant);
  // Lyrics-optional "elements" model: dual-gated by the FE flag AND the
  // variant's lyrics_model — flag-off or legacy (baked) variants take every
  // existing code path below untouched.
  const isNewLyricsModel = isElementsLyricsModel(variant?.editor_capabilities);
  const lyricsOptionalActive = LYRICS_OPTIONAL_UI && isNewLyricsModel;
  // Toggle visibility, unified across both models (see editor-capabilities.ts).
  const lyricsFeatureAvailable = computeLyricsFeatureAvailable(variant?.editor_capabilities);
  // Legacy-only: whether lyric_line elements are projected into text_elements
  // for local bar editing via the OLD lyricOverrides route. Always false on
  // elements-model variants — those bars are inserted/removed by the toggle,
  // not projected from the read adapter.
  const lyricBarsAvailable = LYRICS_EDITOR_UI && !isNewLyricsModel && lyricsCap.editable;
  // Elements-model: cache fetched lyric-seed bars per variant (once per
  // session) + track the in-flight/error state that drives the toggle's
  // loading/disabled copy.
  // Cached as raw TextElement[] (not bars) — inserting re-derives bars AND
  // re-merges into originalsRef so word_timings/reveal_s/fade_out_ms/z (fields
  // the bar type doesn't model) survive the barsToTextElements merge-over-
  // original on Save, exactly like server-seeded bars from a variant read.
  const lyricSeedsCacheRef = useRef<Map<string, TextElement[]>>(new Map());
  const [lyricSeedsLoading, setLyricSeedsLoading] = useState(false);
  const [lyricSeedsError, setLyricSeedsError] = useState<"not_found" | "no_lyrics" | null>(null);
  useEffect(() => {
    setLyricSeedsError(null);
    setLyricSeedsLoading(false);
  }, [variant?.variant_id]);
  const hasLyricBars = useMemo(() => state.bars.some(isLyricBar), [state.bars]);
  // The flag gates mutation, not truth. A landscape variant must always open
  // on a landscape canvas even when the output-format control is rolled back.
  // Use the response value on the first loaded paint too; the working-state
  // seeding effect runs after paint and must not flash a portrait canvas or a
  // false dirty Save state in the meantime.
  const orientationSeeded = seededVariantIdRef.current === variant?.variant_id;
  const previewOrientation = orientationSeeded
    ? orientation
    : persistedOrientation(variant);
  const activeCanvas = useMemo(
    () => canvasForOrientation(previewOrientation),
    [previewOrientation],
  );

  useEffect(() => {
    if (!variant) return;
    const sameVariant = seededVariantIdRef.current === variant.variant_id;
    const conflictReseed = conflictReseedRef.current && sameVariant;
    if (sameVariant && !conflictReseed) return;
    conflictReseedRef.current = false;
    seededVariantIdRef.current = variant.variant_id;
    const sections = computeReseedSections(
      { textDirty: textDirty || captionDirty, sfxDirty, overlaysDirty, mixDirty },
      conflictReseed,
    );
    // Visual blocks and their linked TextElements are one atomic document. On
    // a baseline conflict, preserve or reload them together so neither half can
    // point at state from the other tab.
    const keepCoupledVisualDocument =
      conflictReseed && (visualBlocksDirty || motionScenesDirty || textDirty || captionDirty);
    const keepCameraEffects = conflictReseed && cameraEffectsDirty;
    if (sections.text && !keepCoupledVisualDocument) {
      originalsRef.current = new Map(
        (variant.text_elements ?? []).map((el) => [el.id, el]),
      );
      captionOriginalsRef.current = new Map(
        (variant.caption_cues ?? []).map((cue, index) => [`caption-${index}`, cue]),
      );
      dispatch({
        type: "RESET",
        // Elements-model bars are ordinary persisted text_elements once
        // saved — always include them so a reload reflects a prior toggle-on
        // save without re-fetching seeds. Legacy still gates on
        // lyricBarsAvailable (projection-for-editing only when the old
        // editor UI is on).
        bars: seedBarsFromVariant(variant, {
          includeLyrics: lyricBarsAvailable || lyricsOptionalActive,
        }),
      });
      setLyricsEnabled(lyricsFeatureAvailable && persistedLyricsEnabled(variant));
      setTextDirty(false);
      setCaptionDirty(false);
    }
    if (sections.sfx) {
      setLocalSfx((variant.sound_effects ?? []).map((p) => ({ ...p })));
      setLocalSfxAudioUrls({});
      setSfxDirty(false);
    }
    if (sections.overlays) {
      setLocalOverlays((variant.media_overlays ?? []).map((o) => ({ ...o })));
      setLocalOverlayPreviewUrls((current) => {
        Object.values(current).forEach(revokeLocalObjectUrl);
        return {};
      });
      // Re-seeded from the server ⇒ any accepted-but-unsaved cards are gone.
      setAcceptedSuggestions([]);
      setOverlaysDirty(false);
    }
    if (!keepCoupledVisualDocument) {
      setLocalVisualBlocks((variant.visual_blocks ?? []).map((block) => ({ ...block })));
      Object.values(localVisualPreviewUrlsRef.current).forEach(revokeLocalObjectUrl);
      const persistedPreviewUrls = { ...(variant.visual_block_preview_urls ?? {}) };
      localVisualPreviewUrlsRef.current = persistedPreviewUrls;
      setLocalVisualPreviewUrls(persistedPreviewUrls);
      setVisualBlocksDirty(false);
      setLocalMotionScenes((variant.motion_scenes ?? []).map((scene) => ({ ...scene })));
      setMotionScenesDirty(false);
    }
    if (!keepCameraEffects) {
      setLocalCameraEffects(
        (variant.camera_effects ?? []).map((effect) => normalizeCameraEffect({ ...effect })),
      );
      setCameraEffectsDirty(false);
    }
    if (sections.titleAndStyle) setTitleDirty(false);
    const keepLocalOrientation =
      conflictReseed && orientation !== persistedOrientation(variant);
    if (!keepLocalOrientation) setOrientation(persistedOrientation(variant));
    if (sections.mix) {
      const seededMix =
        typeof variant.mix === "number"
          ? variant.mix
          : typeof variant.voiceover_bed_level === "number"
            ? variant.voiceover_bed_level
            : null;
      setMixLevel(seededMix);
      setMixDirty(false);
      setSoundMuted(seededMix === 0);
    }
    if (!conflictReseed || !captionMetaDirty) {
      setCaptionMeta(captionMetaFromVariant(variant));
      setCaptionMetaDirty(false);
      setCaptionMetaPatch({});
    }
    if (sections.titleAndStyle) setAppliedStyleSetId(null);
    // Dirty flags are read as a snapshot when a (re)seed fires; they must not
    // retrigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant, lyricBarsAvailable, lyricsFeatureAvailable, lyricsOptionalActive, orientation]);

  useEffect(() => {
    localOverlayPreviewUrlsRef.current = localOverlayPreviewUrls;
  }, [localOverlayPreviewUrls]);

  useEffect(() => {
    localVisualPreviewUrlsRef.current = localVisualPreviewUrls;
  }, [localVisualPreviewUrls]);

  useEffect(() => {
    return () => {
      Object.values(localOverlayPreviewUrlsRef.current).forEach(revokeLocalObjectUrl);
      Object.values(localVisualPreviewUrlsRef.current).forEach(revokeLocalObjectUrl);
    };
  }, []);

  useEffect(() => {
    if (!item || titleDirty) return;
    setTitle(item.theme ?? "");
  }, [item, titleDirty]);

  // ── View state ──────────────────────────────────────────────────────────────
  const layoutMode = useEditorLayoutMode();
  const { selection, select, clear } = useEditorSelection();
  // Pocket-editor chrome state (which sheet, which detent). Deliberately NOT
  // in useEditorHistory — chrome state is not undoable document state.
  const [pocket, dispatchPocket] = useReducer(pocketReducer, initialPocketState);
  const pocketActive = POCKET_UI && layoutMode === "light";
  const pocketSheetOpen = pocketActive && pocket.sheet !== null;
  /** True while the save that failed was a network-class failure (drives the
   * one-shot auto-retry when the browser comes back online). */
  const networkSaveErrorRef = useRef(false);
  const [activeTool, setActiveTool] = useState<EditorTool | null>(null); // drawer CLOSED at first paint
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("basic");
  const [lightSheetOpen, setLightSheetOpen] = useState(false);
  const [canvasTool, setCanvasTool] = useState<"select" | "pan">("select");
  const [zoomPct, setZoomPct] = useState<number>(100);
  const [flashTextIds, setFlashTextIds] = useState<Set<string>>(new Set());
  const [flashOverlayIds, setFlashOverlayIds] = useState<Set<string>>(new Set());
  const [flashTimelineIds, setFlashTimelineIds] = useState<Set<string>>(new Set());
  const [sessionHasCopilotEdits, setSessionHasCopilotEdits] = useState(false);
  const [copilotSaveNoticeDismissed, setCopilotSaveNoticeDismissed] = useState(true);
  const panEnabled = zoomPct > 100;
  const playbackClockRef = useRef<EditorPlaybackClock | null>(null);
  if (FRAME_DRIVEN_PREVIEW_ENABLED && playbackClockRef.current == null) {
    playbackClockRef.current = createEditorPlaybackClock(0);
  }
  const playbackClock = playbackClockRef.current;
  const [currentTime, setCommittedCurrentTime] = useState(0);
  const setCurrentTime = useCallback(
    (timeS: number) => {
      playbackClock?.publish(timeS);
      setCommittedCurrentTime(timeS);
    },
    [playbackClock],
  );
  // Playback sources publish decoded-frame time separately. Their native
  // timeupdate cadence only commits transport/scrub state and must not move
  // authored layers ahead of the frame actually on screen.
  const commitPlaybackTime = useCallback((timeS: number) => {
    setCommittedCurrentTime(timeS);
  }, []);
  const outputToBaseTimeRef = useRef<(seconds: number) => number>((seconds) => seconds);
  const baseToOutputTimeRef = useRef<(seconds: number) => number>((seconds) => seconds);
  const [pendingCopilotFocus, setPendingCopilotFocus] =
    useState<DirectorPreviewFocus | null>(null);
  const [duration, setDuration] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const renderedMusicAudioRef = useRef<HTMLAudioElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);

  // ── Timeline view state (plan §6) ───────────────────────────────────────────
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(1); // 1 = fit-to-width
  const [timelineFitRequestKey, setTimelineFitRequestKey] = useState(0);
  const [videoMuted, setVideoMuted] = useState(false);
  const [soundMuted, setSoundMuted] = useState(false);
  // Transient feedback (§15) — sonner owns display/auto-dismiss; this is
  // just a stable-identity callback so call sites read the same as before.
  const notify = useCallback((message: string) => {
    toast(message, { duration: 2600 });
  }, []);
  const [sfxGlossaryEffects, setSfxGlossaryEffects] = useState<SoundEffectSummary[]>([]);
  const [sfxGlossaryLoading, setSfxGlossaryLoading] = useState(false);
  const [musicTracks, setMusicTracks] = useState<MusicTrackSummary[]>([]);
  const [musicTracksLoaded, setMusicTracksLoaded] = useState(false);
  const [musicTracksLoading, setMusicTracksLoading] = useState(false);
  const [selectedMusicTrackId, setSelectedMusicTrackId] = useState<string | null>(
    variant?.music_track_id ?? null,
  );
  // Explicit removed state: selectedMusicTrackId === null alone means
  // "untouched" (falls back to the variant's persisted track), so removal
  // needs its own flag — it drives `remove_music` in the commit payload.
  const [musicRemoved, setMusicRemoved] = useState(false);
  const [musicStartS, setMusicStartS] = useState<number>(
    variant?.music_preview_start_s ?? 0,
  );
  const [musicDirty, setMusicDirty] = useState(false);
  const [backgroundMusic, setBackgroundMusic] = useState<EditorCommitBackgroundMusic | null>(
    variant?.background_music
      ? {
          track_id: variant.background_music.track_id,
          enabled: variant.background_music.enabled,
          start_s: variant.background_music.start_s,
          end_s: variant.background_music.end_s,
          gain_db: variant.background_music.gain_db,
          muted: variant.background_music.muted,
        }
      : null,
  );
  const [backgroundMusicDirty, setBackgroundMusicDirty] = useState(false);
  // Carousel-moment (Blossom carousel), staged like every other batched-Save
  // section — mirrors backgroundMusic/backgroundMusicDirty exactly (an
  // object|null value + a dirty flag). Always holds the CURRENT effective
  // moment (persisted until the user touches the panel, staged after), so
  // prefill is simply "read this state" — never a separate staged-vs-
  // persisted branch.
  const [carouselMoment, setCarouselMoment] = useState<CarouselMoment | null>(
    variant?.carousel_moment ?? null,
  );
  const [carouselMomentDirty, setCarouselMomentDirty] = useState(false);
  const musicHydratedVariantIdRef = useRef<string | null>(null);
  const [overlayUploading, setOverlayUploading] = useState(false);
  const [poolAssets, setPoolAssets] = useState<PoolAsset[]>([]);
  const [serverPoolReservations, setServerPoolReservations] = useState<
    PoolReservationCapacity[]
  >([]);
  const [serverPoolOccupiedCount, setServerPoolOccupiedCount] = useState(0);
  const [maxPoolAssets, setMaxPoolAssets] = useState(20);
  const [poolUnavailable, setPoolUnavailable] = useState(false);
  const [poolError, setPoolError] = useState<string | null>(null);
  const poolListEpoch = useRef(0);
  const poolPollInFlight = useRef(false);
  const editorHistoryRef = useRef<ReturnType<typeof useEditorHistory> | null>(null);
  const poolUploader = usePoolAssetUploader({
    itemId,
    assetCount: poolAssets.length,
    maxAssets: maxPoolAssets,
    onRegistered: (asset, file, intent, context) => {
      if (intent === "overlay") {
        const overlay = context as {
          id: string;
          position: "top" | "center" | "bottom";
          x_frac: number;
          y_frac: number;
          start_s: number;
          end_s: number;
          z: number;
        };
        // Registration proves the immutable object exists. The worker may
        // still be decoding/provisioning a browser preview, so keep the
        // overlay out of the draft until the server reports media readiness.
        const finalize = async () => {
          let current = asset;
          for (let attempt = 0; attempt < 60; attempt += 1) {
            if (
              (current.media_status
                ? current.media_status === "ready"
                : current.status === "ready") &&
              (!current.preview_status ||
                current.preview_status === "ready" ||
                current.preview_status === "not_needed")
            ) {
              const previewUrl = URL.createObjectURL(file);
              const card: MediaOverlay = {
                id: overlay.id,
                kind: current.kind,
                src_gcs_path: current.gcs_path,
                preview_gcs_path: null,
                preview_url: current.preview_url ?? null,
                position: overlay.position,
                x_frac: overlay.x_frac,
                y_frac: overlay.y_frac,
                scale: 0.35,
                start_s: overlay.start_s,
                end_s: overlay.end_s,
                z: overlay.z,
              };
              editorHistoryRef.current?.record();
              if (visualBlocksAllowed) {
                if (current.kind === "video" && !(current.duration_s && current.duration_s > 0)) {
                  throw new Error("Video duration is not ready yet.");
                }
                const block = normalizeMediaVisualBlock({
                  ...mediaOverlayToVisualBlock(card, current),
                  asset_id: current.id,
                  source_duration_s: current.kind === "video" ? current.duration_s ?? null : null,
                  trim_end_s: current.kind === "video" ? current.duration_s ?? null : null,
                  display_mode: "fullscreen",
                });
                setLocalVisualBlocks((cur) => [...cur, block]);
                setLocalVisualPreviewUrls((cur) => ({ ...cur, [block.id]: previewUrl }));
                setVisualBlocksDirty(true);
                select("visual", block.id);
              } else {
                setLocalOverlays((cur) => [...cur, card]);
                setLocalOverlayPreviewUrls((cur) => ({ ...cur, [card.id]: previewUrl }));
                setOverlaysDirty(true);
                select("overlay", card.id);
              }
              setInspectorTab("basic");
              return;
            }
            if (
              current.media_status === "unreadable" ||
              current.media_status === "failed" ||
              current.preview_status === "failed"
            ) {
              throw new Error("Kria couldn't read that visual. Try another file.");
            }
            await new Promise((resolve) => window.setTimeout(resolve, 1000));
            const refreshed = await listPoolAssets(itemId);
            current = refreshed.assets.find((row) => row.id === asset.id) ?? current;
          }
          throw new Error("That visual is taking longer than expected. Try again shortly.");
        };
        void finalize()
          .catch(() => notify("We couldn't add that overlay. Try again."))
          .finally(() => setOverlayUploading(false));
      }
      poolListEpoch.current += 1;
      setPoolAssets((prev) => [...prev.filter((row) => row.id !== asset.id), asset]);
    },
    onFailed: (_file, intent) => {
      if (intent === "overlay") setOverlayUploading(false);
    },
    onUnavailable: () => setPoolUnavailable(true),
    serverReservations: serverPoolReservations,
    serverOccupiedCount: serverPoolOccupiedCount,
    onReservationFinalized: (reservationId, releasedCapacity) => {
      setServerPoolReservations((current) =>
        current.filter((reservation) => reservation.reservation_id !== reservationId),
      );
      if (releasedCapacity) {
        setServerPoolOccupiedCount((current) => Math.max(0, current - 1));
      }
    },
  });
  const pendingPoolUploads = poolUploader.uploads;
  const addPoolFiles = poolUploader.addFiles;

  // Clip slots — the shell's local working state for split/delete (seeded from
  // the shared clip-timeline handle, then edited locally; persisted via
  // editor-commit `timeline_slots`).
  const timelineVariantId = variant?.variant_id ?? variantParam ?? "";
  const clip = useClipTimeline(itemId, timelineVariantId, "plan-item");
  const [localSlots, setLocalSlots] = useState<DraftSlot[] | null>(null);
  const slotsSeededRef = useRef<string | null>(null);
  useEffect(() => {
    if (!variant || timelineVariantId !== variant.variant_id || clip.loadState !== "ready") return;
    if (slotsSeededRef.current === variant?.variant_id) return;
    slotsSeededRef.current = variant?.variant_id ?? null;
    setLocalSlots(clip.state.slots.map((s) => ({ ...s })));
  }, [clip.loadState, clip.state.slots, timelineVariantId, variant]);
  useEffect(() => {
    const nextVariantId = variant?.variant_id ?? null;
    const changedVariant = musicHydratedVariantIdRef.current !== nextVariantId;
    if (!changedVariant && (musicDirty || backgroundMusicDirty || carouselMomentDirty)) return;
    musicHydratedVariantIdRef.current = nextVariantId;
    setSelectedMusicTrackId(variant?.music_track_id ?? null);
    setMusicRemoved(false);
    setMusicStartS(variant?.music_preview_start_s ?? 0);
    setBackgroundMusic(
      variant?.background_music
        ? {
            track_id: variant.background_music.track_id,
            enabled: variant.background_music.enabled,
            start_s: variant.background_music.start_s,
            end_s: variant.background_music.end_s,
            gain_db: variant.background_music.gain_db,
            muted: variant.background_music.muted,
          }
        : null,
    );
    setMusicDirty(false);
    setBackgroundMusicDirty(false);
    setCarouselMoment(variant?.carousel_moment ?? null);
    setCarouselMomentDirty(false);
  }, [
    backgroundMusicDirty,
    musicDirty,
    carouselMomentDirty,
    variant?.background_music,
    variant?.music_preview_start_s,
    variant?.music_track_id,
    variant?.carousel_moment,
    variant?.variant_id,
  ]);
  const slots = localSlots ?? clip.state.slots;
  const reloadClipTimeline = clip.reload;
  const editWideLookPresets = useMemo(
    () => clip.editWideLookPresets ?? [],
    [clip.editWideLookPresets],
  );
  const perClipLookPresets = useMemo(
    () => clip.lookPresets ?? [],
    [clip.lookPresets],
  );
  const selectedEditWideLookPreset = useMemo<LookPreset | null>(() => {
    if (slots.length === 0) return null;
    const first = slots[0].lookPreset ?? "none";
    const uniform = slots.every((slot) => (slot.lookPreset ?? "none") === first);
    return uniform && editWideLookPresets.includes(first) ? first : null;
  }, [editWideLookPresets, slots]);
  const editWideLookPresetMixed = useMemo(() => {
    if (slots.length < 2) return false;
    const first = slots[0].lookPreset ?? "none";
    return slots.some((slot) => (slot.lookPreset ?? "none") !== first);
  }, [slots]);
  const editWideLookPreviewUrl = useMemo(() => {
    for (const slot of slots) {
      if (slot.removed) continue;
      const source = clip.clips.find((candidate) => candidate.clip_index === slot.clipIndex);
      if (source?.signed_url) return source.signed_url;
    }
    return clip.clips.find((candidate) => candidate.signed_url)?.signed_url ?? null;
  }, [clip.clips, slots]);
  // Carousel focus-tile strip: one thumbnail per clip actually in the
  // timeline, in slot order, deduped (a clip can occupy more than one slot).
  // Reuses the same clip.clips (signed_url per clip_index) the Filmstrip
  // draws from — no separate fetch.
  //
  // A carousel-moment segment's synthetic slot (clip id prefix
  // `__carousel_*` server-side) has no entry in `clip.clips` — that list is
  // built strictly from the job's real uploaded clip pool
  // (`job.all_candidates.clip_paths`), never the rendered segment. Skipping
  // slots with no `clip.clips` match keeps the strip (and therefore
  // `focus_clip_index` semantics) limited to real clips, even if the
  // timeline itself still carries a stale synthetic slot.
  const carouselClips = useMemo<CarouselClipThumb[]>(() => {
    const out: CarouselClipThumb[] = [];
    const seen = new Set<number>();
    for (const slot of slots) {
      if (slot.removed || seen.has(slot.clipIndex)) continue;
      const source = clip.clips.find((c) => c.clip_index === slot.clipIndex) ?? null;
      if (!source) continue;
      seen.add(slot.clipIndex);
      out.push({
        clipIndex: slot.clipIndex,
        label: `Clip ${out.length + 1}`,
        signedUrl: source.signed_url ?? null,
      });
      if (out.length === 5) break;
    }
    return out;
  }, [slots, clip.clips]);
  const carouselUpgradeVariantRef = useRef<string | null>(null);
  useEffect(() => {
    if (
      !variant ||
      !carouselMoment ||
      !shouldAutoUpgradeCarouselTiming(carouselMoment) ||
      carouselMomentDirty ||
      clip.loadState !== "ready" ||
      carouselUpgradeVariantRef.current === variant.variant_id
    ) {
      return;
    }
    carouselUpgradeVariantRef.current = variant.variant_id;
    setCarouselMoment(
      upgradeCarouselTiming(
        carouselMoment,
        carouselClips.map((entry) => entry.clipIndex),
      ),
    );
    setCarouselMomentDirty(true);
  }, [carouselClips, carouselMoment, carouselMomentDirty, clip.loadState, variant]);
  const manualDraftPendingExport =
    variant?.manual_draft === true &&
    !variant.output_url &&
    (variant.render_status === "draft" || variant.render_status === "failed");
  const clipDirty = useMemo(
    () => manualDraftPendingExport || slotsDifferFromBaseline(clip.state.baseline, slots),
    [clip.state.baseline, manualDraftPendingExport, slots],
  );
  const [virtualFallback, setVirtualFallback] = useState(false);
  const virtualRefetchAttemptedRef = useRef(false);
  const virtualRefetchInFlightRef = useRef(false);

  // Virtual-preview music recovery state. The retry budget is one refetch per
  // edit session per track — a missing audio blob mints a fresh (still broken)
  // signed URL on every fetch, so re-arming on success would loop forever.
  const [virtualMusicUnavailable, setVirtualMusicUnavailable] = useState(false);
  const musicRefetchAttemptedRef = useRef(false);
  const virtualMusicAutoFetchRef = useRef(false);
  const musicTracksFetchRef = useRef<Promise<void> | null>(null);
  // Local blob copy of the track audio (see the fetch effect below) — declared
  // here so the error handler can drop it before retrying with a fresh URL.
  const [virtualMusicBlob, setVirtualMusicBlob] = useState<{
    trackId: string;
    url: string;
  } | null>(null);

  const refreshMusicTracks = useCallback((): Promise<void> => {
    if (musicTracksFetchRef.current) return musicTracksFetchRef.current;
    setMusicTracksLoading(true);
    const fetchPromise = getMusicTracks()
      .then((res) => {
        setMusicTracks(res.tracks);
        setMusicTracksLoaded(true);
      })
      .catch(() => {
        // Keep whatever tracks we already have and leave `musicTracksLoaded`
        // false so the picker/virtual-preview gates can trigger a retry later.
        notify("Couldn't load music.");
      })
      .finally(() => {
        setMusicTracksLoading(false);
        musicTracksFetchRef.current = null;
      });
    musicTracksFetchRef.current = fetchPromise;
    return fetchPromise;
  }, [notify]);

  useEffect(() => {
    if (!clipDirty && !musicDirty && !backgroundMusicDirty) {
      setVirtualFallback(false);
      virtualRefetchAttemptedRef.current = false;
      virtualRefetchInFlightRef.current = false;
      setVirtualMusicUnavailable(false);
      musicRefetchAttemptedRef.current = false;
      virtualMusicAutoFetchRef.current = false;
    }
  }, [backgroundMusicDirty, clipDirty, musicDirty]);

  // ── Read-only capability gate (plan §9 / E4) ────────────────────────────────
  // A variant whose editor_capabilities are ALL false is read-only: banner +
  // Save disabled + every mutating command no-ops. The server's honest reason
  // is surfaced verbatim.
  const capabilities = variant?.editor_capabilities;
  const guidedStoryV2 = hasGuidedOperationCapabilities(capabilities);
  const clipCan = useCallback(
    (operation: Parameters<typeof canEditClip>[1], legacy: boolean) =>
      canEditClip(capabilities, operation, legacy),
    [capabilities],
  );
  const musicCan = useCallback(
    (operation: Parameters<typeof canEditMusic>[1], legacy: boolean) =>
      canEditMusic(capabilities, operation, legacy),
    [capabilities],
  );
  const textElementsAllowed = canEditLane(
    capabilities,
    "text",
    capabilities?.text_elements !== false,
  );
  const sfxAllowed = canEditLane(capabilities, "sfx", capabilities?.sfx !== false);
  const overlaysAllowed = canEditLane(
    capabilities,
    "overlays",
    capabilities?.overlays !== false,
  );
  const visualBlocksAllowed = canEditLane(
    capabilities,
    "visual_blocks",
    capabilities?.visual_blocks === true,
  );
  const motionScenesAllowed = canEditLane(
    capabilities,
    "motion_scenes",
    capabilities?.motion_scenes === true,
  );
  const evolvingTypeExposureEnabled =
    EVOLVING_TYPE_PUBLIC_ENABLED && capabilities?.evolving_type === true;
  const readOnly =
    !!capabilities &&
    !textElementsAllowed &&
    !lyricsFeatureAvailable &&
    !clipCan("trim", capabilities.timeline !== false) &&
    !clipCan("split", capabilities.split_clips !== false) &&
    capabilities.mix === false &&
    !sfxAllowed &&
    !overlaysAllowed &&
    !visualBlocksAllowed &&
    !motionScenesAllowed &&
    capabilities.camera_effects !== true &&
    capabilities.orientation?.editable !== true &&
    capabilities.music_window?.editable !== true;
  const readOnlyReason = editorReasonCopy(capabilities?.reason);
  const introControlsEditable = canEditIntroControls(capabilities, readOnly);
  // Text-elements gate (plan 010 OV-1): once sfx/overlays flip true on
  // subtitled variants the shell is editable, but optional authored text still
  // respects the rollout flag. Caption cue bars remain directly editable.
  const textElementsLocked =
    !readOnly && !textElementsAllowed && !lyricsFeatureAvailable;
  // Legacy lyrics variants still use the old whole-style-set route when the
  // frontend lyrics editor is off. With the new gate on, projected lyric bars
  // are edited locally and saved through editor-commit's `lyrics` section.
  const isLyrics = variant?.text_mode === "lyrics";
  // Caption archetypes seed caption cues into the same visible timeline as
  // Smart titles/user text, while persisting through caption_cues.
  const isCaptionEdit = !!variant && isCaptionArchetype(variant);
  // ANY server timeline ineligibility locks the clip lane — a reason-whitelist
  // here let lyrics_sync (and any future reason) edit clips freely in the UI
  // only to 422 at save time.
  const clipEditingLocked =
    !clipCan("trim", capabilities?.timeline !== false) ||
    variant?.resolved_archetype === "narrated";
  const clipAddAllowed = clipCan("add", !clipEditingLocked);
  const clipRemoveAllowed = clipCan("remove", !clipEditingLocked);
  const clipReorderAllowed = clipCan("reorder", !clipEditingLocked);
  const clipSplitAllowed = clipCan("split", capabilities?.split_clips !== false);
  const clipLooksAllowed = clipCan("looks", !clipEditingLocked);
  const clipTransitionsAllowed = clipCan("transitions", !clipEditingLocked);
  const clipDisabledReason =
    capabilities?.reason === "voiceover_bed_fit" ||
    capabilities?.reason === "locked_to_voiceover" ||
    variant?.resolved_archetype === "narrated"
      ? "locked to your voiceover"
      : editorReasonCopy(capabilities?.reason);
  const clipTimingDisabledReason =
    operationDisabledReason(capabilities?.clips?.trim) ?? clipDisabledReason;
  const clipAddDisabledReason =
    operationDisabledReason(capabilities?.clips?.add) ?? clipDisabledReason;
  const clipRemoveDisabledReason =
    operationDisabledReason(capabilities?.clips?.remove) ?? clipDisabledReason;
  const clipReorderDisabledReason =
    operationDisabledReason(capabilities?.clips?.reorder) ?? clipDisabledReason;
  const clipSplitDisabledReason =
    operationDisabledReason(capabilities?.clips?.split) ?? clipDisabledReason;
  const clipLooksDisabledReason =
    operationDisabledReason(capabilities?.clips?.looks) ?? clipDisabledReason;
  const clipTransitionsDisabledReason =
    operationDisabledReason(capabilities?.clips?.transitions) ?? clipDisabledReason;
  const textDisabledReason = operationDisabledReason(capabilities?.lanes?.text);
  const sfxDisabledReason = operationDisabledReason(capabilities?.lanes?.sfx);
  const overlaysDisabledReason = operationDisabledReason(capabilities?.lanes?.overlays);
  const visualBlocksDisabledReason = operationDisabledReason(
    capabilities?.lanes?.visual_blocks,
  );
  const motionScenesDisabledReason = operationDisabledReason(
    capabilities?.lanes?.motion_scenes,
  );

  // ── Unified undo/redo (plan §7, task T8) ────────────────────────────────────
  const getCurrent = useCallback(
    (): EditorDocument => ({
      bars: state.bars,
      slots: localSlots,
      sfx: localSfx,
      overlays: localOverlays,
      visualBlocks: localVisualBlocks,
      motionScenes: localMotionScenes,
      cameraEffects: localCameraEffects,
      captionMeta,
      captionMetaDirty,
      captionMetaPatch,
      videoMuted,
      soundMuted,
      mixLevel,
      mixDirty,
      musicTrackId: selectedMusicTrackId,
      musicRemoved,
      musicStartS,
      musicDirty,
      backgroundMusic,
      backgroundMusicDirty,
      carouselMoment,
      carouselMomentDirty,
      lyricsEnabled,
      orientation,
      title,
    }),
    [
      state.bars,
      localSlots,
      localSfx,
      localOverlays,
      localVisualBlocks,
      localMotionScenes,
      localCameraEffects,
      captionMeta,
      captionMetaDirty,
      captionMetaPatch,
      videoMuted,
      soundMuted,
      mixLevel,
      mixDirty,
      selectedMusicTrackId,
      musicRemoved,
      musicStartS,
      musicDirty,
      backgroundMusic,
      backgroundMusicDirty,
      carouselMoment,
      carouselMomentDirty,
      lyricsEnabled,
      orientation,
      title,
    ],
  );

  const applyDocument = useCallback(
    (doc: EditorDocument) => {
      const beforeIds = new Set(state.bars.map((b) => b.id));
      dispatch({ type: "RESET", bars: doc.bars });
      setLocalSlots(doc.slots);
      setLocalSfx(doc.sfx ?? []);
      setLocalOverlays(doc.overlays ?? []);
      setLocalVisualBlocks(doc.visualBlocks ?? []);
      setLocalMotionScenes(doc.motionScenes ?? []);
      setLocalCameraEffects(doc.cameraEffects ?? []);
      setVideoMuted(doc.videoMuted);
      setSoundMuted(doc.soundMuted);
      setMixLevel(doc.mixLevel ?? null);
      setMixDirty(doc.mixDirty ?? false);
      setSelectedMusicTrackId(doc.musicTrackId ?? variant?.music_track_id ?? null);
      setMusicRemoved(doc.musicRemoved ?? false);
      setMusicStartS(doc.musicStartS ?? variant?.music_preview_start_s ?? 0);
      setMusicDirty(doc.musicDirty ?? false);
      setBackgroundMusic(doc.backgroundMusic ?? null);
      setBackgroundMusicDirty(doc.backgroundMusicDirty ?? false);
      setCarouselMoment(doc.carouselMoment ?? null);
      setCarouselMomentDirty(doc.carouselMomentDirty ?? false);
      setLyricsEnabled(doc.lyricsEnabled ?? persistedLyricsEnabled(variant));
      setOrientation(doc.orientation ?? persistedOrientation(variant));
      setCaptionMeta(doc.captionMeta ?? null);
      setCaptionMetaDirty(doc.captionMetaDirty ?? false);
      setCaptionMetaPatch(doc.captionMetaPatch ?? {});
      if (introControlsEditable) setTitle(doc.title);
      setTextDirty(
        doc.bars.some((bar) => !isCaptionBar(bar) && (lyricsOptionalActive || !isLyricBar(bar))),
      );
      setCaptionDirty(doc.bars.some(isCaptionBar));
      // Sections the active variant can't accept (e.g. visual_blocks on a
      // lyrics variant, sfx/overlays gated off) ride along in the undo
      // snapshot as an untouched echo — don't blanket-dirty them, or the next
      // save ships a section the backend editor-commit guard 422s the WHOLE
      // commit for (see agents/DECISIONS.md undo/redo dirty-flag bug).
      if (sfxAllowed) setSfxDirty(true);
      if (overlaysAllowed) setOverlaysDirty(true);
      if (visualBlocksAllowed) setVisualBlocksDirty(true);
      if (motionScenesAllowed) setMotionScenesDirty(true);
      if (capabilities?.camera_effects !== false) {
        setCameraEffectsDirty(!cameraEffectsEqual(doc.cameraEffects, variant?.camera_effects));
      }
      if (introControlsEditable) setTitleDirty(true);
      // Undo of a delete (or redo of an add) resurrects a bar → re-select it
      // (plan §5 — the one selection rule that reaches into undo).
      const resurrected = doc.bars.find((b) => !beforeIds.has(b.id));
      if (resurrected) {
        select("text", resurrected.id);
        setInspectorTab("basic");
      }
    },
    [
      state.bars,
      select,
      variant,
      capabilities?.camera_effects,
      introControlsEditable,
      lyricsOptionalActive,
      motionScenesAllowed,
      overlaysAllowed,
      sfxAllowed,
      visualBlocksAllowed,
    ],
  );

  const history = useEditorHistory({ getCurrent, apply: applyDocument });
  editorHistoryRef.current = history;

  const applyEditWideLook = useCallback(
    (preset: LookPreset) => {
      if (
        readOnly ||
        !clipCan("edit_wide_looks", clipLooksAllowed) ||
        slots.length === 0 ||
        !editWideLookPresets.includes(preset)
      ) {
        return;
      }
      const unchanged = slots.every(
        (slot) => (slot.lookPreset ?? "none") === preset && slot.lookAdjustments == null,
      );
      if (unchanged) return;
      history.record();
      setLocalSlots(
        slots.map((slot) => ({
          ...slot,
          lookPreset: preset,
          lookAdjustments: null,
        })),
      );
    },
    [clipCan, clipLooksAllowed, editWideLookPresets, history, readOnly, slots],
  );

  const motionRuntimeCompatible =
    capabilities?.motion_scenes_reason !== "motion_runtime_mismatch" &&
    (!capabilities?.motion_runtime_hash ||
      isCompatiblePersistedMotionRuntimeHash(
        capabilities.motion_runtime_hash,
        localMotionScenes.every((scene) => scene.preset_id === "route_trace"),
      ));
  const motionPreviewRuntimeHash = motionRuntimeCompatible
    ? MOTION_RUNTIME_HASH
    : (capabilities?.motion_required_runtime_hash ?? "unsupported-motion-runtime");
  const addMotionScene = useCallback((presetId: MotionPresetId) => {
    if (
      readOnly ||
      !MOTION_SCENES_UI_ENABLED ||
      !motionScenesAllowed ||
      !motionRuntimeCompatible ||
      (presetId === "evolving_type" && !evolvingTypeExposureEnabled) ||
      localMotionScenes.length >= MOTION_MAX_INSTANCES
    ) {
      return;
    }
    const baseLayoutDuration = sequentialSlotLayout(slots, clip.state.grid).totalDurationS;
    const motionDuration = baseLayoutDuration > 0
      ? baseLayoutDuration
      : duration > 0
        ? duration
        : Math.max(0, Number(variant?.duration_s ?? 0));
    const durationFrames = Math.max(1, Math.round(motionDuration * MOTION_FPS));
    const baseCurrentTime = outputToBaseTimeRef.current(currentTime);
    let startFrame = Math.max(
      0,
      Math.min(durationFrames - 1, Math.floor(baseCurrentTime * MOTION_FPS)),
    );
    const readyImages = poolAssets.filter(isBoundedCreatorImageAsset);
    const entry = presetId === "route_trace" ? null : creatorBlockEntry(presetId);
    if (entry && readyImages.length < entry.min_assets) return;
    let endFrame = Math.min(
      durationFrames,
      startFrame + (entry?.default_duration_frames ?? 2 * MOTION_FPS),
    );
    if (endFrame <= startFrame) {
      startFrame = Math.max(0, durationFrames - 2 * MOTION_FPS);
      endFrame = durationFrames;
    }
    let candidate: MotionPresetInstance =
      presetId === "route_trace"
        ? {
        id: `motion-${crypto.randomUUID()}`,
        preset_id: "route_trace",
        preset_version: 1,
        start_frame: startFrame,
        end_frame_exclusive: endFrame,
        palette: { primary: "#8B5CF6", accent: "#D9FF43" },
        intensity: 0.8,
          }
        : createCreatorBlockInstance({
            id: `motion-${crypto.randomUUID()}`,
            presetId,
            startFrame,
            endFrameExclusive: endFrame,
            assets: readyImages.slice(0, entry!.min_assets).map((asset) => ({
              asset_id: asset.id,
              gcs_path: asset.gcs_path,
            })),
          });
    while (
      candidate.end_frame_exclusive > candidate.start_frame + 1 &&
      !validateMotionInstances([...localMotionScenes, candidate], durationFrames).ok
    ) {
      candidate = {
        ...candidate,
        end_frame_exclusive: candidate.end_frame_exclusive - 1,
      } as MotionPresetInstance;
    }
    if (!validateMotionInstances([...localMotionScenes, candidate], durationFrames).ok) return;
    history.record();
    setLocalMotionScenes([...localMotionScenes, candidate]);
    setMotionScenesDirty(true);
    select("motion", candidate.id);
    setActiveTool("visuals");
    if (pocketActive) dispatchPocket({ type: "OPEN_INSPECTOR" });
  }, [
    motionScenesAllowed,
    currentTime,
    history,
    localMotionScenes,
    motionRuntimeCompatible,
    evolvingTypeExposureEnabled,
    poolAssets,
    slots,
    clip.state.grid,
    pocketActive,
    readOnly,
    select,
    duration,
    variant?.duration_s,
  ]);

  const patchMotionScene = useCallback(
    (id: string, patch: MotionPresetPatch) => {
      if (readOnly || !motionScenesAllowed) return;
      const target = localMotionScenes.find((scene) => scene.id === id);
      if (target?.preset_id === "evolving_type" && !evolvingTypeExposureEnabled) return;
      const baseLayoutDuration = sequentialSlotLayout(slots, clip.state.grid).totalDurationS;
      const durationFrames = Math.max(
        1,
        Math.round((baseLayoutDuration || duration || variant?.duration_s || 60) * MOTION_FPS),
      );
      const candidate = localMotionScenes.map((scene) => {
        if (scene.id !== id) return scene;
        const {
          start_frame: requestedStart,
          end_frame_exclusive: requestedEnd,
          ...nonTimingPatch
        } = patch;
        const patched = { ...scene, ...nonTimingPatch } as MotionPresetInstance;
        if (
          patched.preset_id !== "route_trace" &&
          (requestedStart !== undefined || requestedEnd !== undefined)
        ) {
          return retimeCreatorBlockManualSpan(
            patched,
            requestedStart ?? scene.start_frame,
            requestedEnd ?? scene.end_frame_exclusive,
            durationFrames,
          ) as MotionPresetInstance;
        }
        return {
          ...patched,
          ...(requestedStart === undefined ? {} : { start_frame: requestedStart }),
          ...(requestedEnd === undefined ? {} : { end_frame_exclusive: requestedEnd }),
        } as MotionPresetInstance;
      });
      const validation = validateMotionInstances(candidate, durationFrames);
      if (!validation.ok) {
        notify(validation.errors[0] ?? "That Creator Block edit is outside the allowed range.");
        return;
      }
      history.record();
      setLocalMotionScenes(candidate);
      setMotionScenesDirty(true);
    },
    [clip.state.grid, duration, evolvingTypeExposureEnabled, history, localMotionScenes, motionScenesAllowed, notify, readOnly, slots, variant?.duration_s],
  );

  const motionDurationFrames = useCallback(() => {
    const baseLayoutDuration = sequentialSlotLayout(slots, clip.state.grid).totalDurationS;
    return Math.max(
      1,
      Math.round((baseLayoutDuration || duration || variant?.duration_s || 60) * MOTION_FPS),
    );
  }, [clip.state.grid, duration, slots, variant?.duration_s]);

  const buildMotionControlScenes = useCallback((
    id: string,
    patch: CreatorBlockMotionControlPatch,
  ): MotionPresetInstance[] | null => {
    const videoEndFrame = motionDurationFrames();
    const index = localMotionScenes.findIndex((scene) => scene.id === id);
    const current = index >= 0 ? localMotionScenes[index] : null;
    if (!current || current.preset_id === "route_trace") return null;
    if (current.preset_id === "evolving_type" && !evolvingTypeExposureEnabled) return null;
    let next = upgradeCreatorBlockInstanceToV2(current);
    if (patch.motion?.speed !== undefined) {
      next = retimeCreatorBlockSpeed(next, patch.motion.speed, videoEndFrame);
    }
    if (patch.motion) {
      const motion = { ...next.motion, ...patch.motion, version: 2 as const };
      next = {
        ...next,
        motion,
        ...(patch.motion.speed !== undefined || patch.motion.hold_frames !== undefined
          ? {
              end_frame_exclusive: Math.max(
                next.start_frame + 1,
                Math.min(videoEndFrame, next.start_frame + creatorBlockDurationFramesV2(next, motion)),
              ),
            }
          : {}),
      };
    }
    if (patch.intensity !== undefined) next = { ...next, intensity: patch.intensity };
    if (patch.params) {
      next = { ...next, params: { ...next.params, ...patch.params } } as typeof next;
    }
    const candidate = localMotionScenes.map((scene, sceneIndex) =>
      sceneIndex === index ? next : scene,
    );
    const validation = validateMotionInstances(candidate, videoEndFrame);
    if (!validation.ok) {
      notify(validation.errors[0] ?? "That Creator Block edit is outside the allowed range.");
      return null;
    }
    return candidate;
  }, [evolvingTypeExposureEnabled, localMotionScenes, motionDurationFrames, notify]);

  const beginMotionControl = useCallback(() => {
    if (readOnly || !motionScenesAllowed) return;
    if (motionControlGestureOriginRef.current) return;
    motionControlGestureOriginRef.current = {
      document: getCurrent(),
      motionScenesDirty,
    };
  }, [getCurrent, motionScenesAllowed, motionScenesDirty, readOnly]);

  const previewMotionControl = useCallback((id: string, patch: CreatorBlockMotionControlPatch) => {
    if (readOnly || !motionScenesAllowed) return;
    const candidate = buildMotionControlScenes(id, patch);
    if (!candidate) return;
    setLocalMotionScenes(candidate);
    setMotionScenesDirty(true);
  }, [buildMotionControlScenes, motionScenesAllowed, readOnly]);

  const commitMotionControl = useCallback((id: string, patch: CreatorBlockMotionControlPatch) => {
    if (readOnly || !motionScenesAllowed) return;
    const origin = motionControlGestureOriginRef.current;
    if (origin) {
      history.recordDocument(origin.document);
      motionControlGestureOriginRef.current = null;
    } else {
      history.record();
    }
    previewMotionControl(id, patch);
  }, [history, motionScenesAllowed, previewMotionControl, readOnly]);

  const cancelMotionControl = useCallback(() => {
    const origin = motionControlGestureOriginRef.current;
    if (!origin) return;
    motionControlGestureOriginRef.current = null;
    setLocalMotionScenes(origin.document.motionScenes ?? []);
    setMotionScenesDirty(origin.motionScenesDirty);
  }, []);

  const patchMotionControl = useCallback((id: string, patch: CreatorBlockMotionControlPatch) => {
    if (readOnly || !motionScenesAllowed) return;
    const candidate = buildMotionControlScenes(id, patch);
    if (!candidate) return;
    history.record();
    setLocalMotionScenes(candidate);
    setMotionScenesDirty(true);
  }, [buildMotionControlScenes, history, motionScenesAllowed, readOnly]);

  const removeMotionScene = useCallback(
    (id: string) => {
      if (readOnly || !motionScenesAllowed) return;
      history.record();
      setLocalMotionScenes((current) => current.filter((scene) => scene.id !== id));
      setMotionScenesDirty(true);
      if (selection?.kind === "motion" && selection.id === id) clear();
    },
    [clear, history, motionScenesAllowed, readOnly, selection],
  );

  const visibleTextBars = useMemo(() => {
    // Elements model: state.bars already IS the source of truth (the toggle
    // inserts/removes lyric bars directly) — nothing to filter.
    if (lyricsOptionalActive) return state.bars;
    return lyricBarsAvailable && lyricsEnabled
      ? state.bars
      : state.bars.filter((bar) => !isLyricBar(bar));
  }, [lyricBarsAvailable, lyricsEnabled, lyricsOptionalActive, state.bars]);
  const lyricLineOverrides = useMemo(
    () =>
      lyricBarsAvailable
        ? buildLyricLineOverrides(state.bars, originalsRef.current)
        : {},
    [lyricBarsAvailable, state.bars],
  );
  const lyricOverridesDirty =
    lyricBarsAvailable &&
    stableJson(lyricLineOverrides) !== stableJson(variant?.lyric_line_overrides ?? {});
  // Elements model never sends the legacy `lyrics` commit section — toggling
  // on/off mutates state.bars via ADD_LYRIC_BARS/REMOVE_LYRIC_BARS, which
  // already drives `dirty` through the normal undo-history + textDirty path.
  const lyricsDirty =
    !lyricsOptionalActive &&
    lyricsFeatureAvailable &&
    (lyricsEnabled !== persistedLyricsEnabled(variant) || lyricOverridesDirty);
  const orientationDirty =
    LANDSCAPE_UI &&
    orientationSeeded &&
    orientation !== persistedOrientation(variant);

  // Every mutation (text, slots, mutes, title) records into the undo stack.
  // A redo-only stack is clean only when the original baseline is still
  // reachable; after the bounded stack evicts it, empty `past` remains dirty.
  const dirty =
    !history.isAtBaseline ||
    musicDirty ||
    backgroundMusicDirty ||
    carouselMomentDirty ||
    captionMetaDirty ||
    lyricsDirty ||
    orientationDirty ||
    cameraEffectsDirty;

  // ── Save / cancel state ─────────────────────────────────────────────────────
  // saveState: idle → saving → {conflict | error | partial} (all preserve
  // working state); full success navigates away.
  const [saveState, setSaveState] = useState<
    "idle" | "saving" | "conflict" | "error" | "partial"
  >("idle");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  // When persistence succeeds but the broker kick fails, the server returns
  // the committed generation. A Retry must advance to that baseline instead of
  // replaying the pre-commit token and tripping a false cross-tab conflict.
  const partialCommitGenerationRef = useRef<string | null>(null);
  const partialGuidedRevisionRef = useRef<number | null>(null);
  const partialHistoryVersionRef = useRef<number | null>(null);
  const saving = saveState === "saving";
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [musicAlignmentPrompt, setMusicAlignmentPrompt] = useState(false);
  // Song-swap + clip-timing collision (§15 — window.confirm is banned; this
  // resumes handleSave with confirmedSongReset=true on confirm).
  const [songResetPrompt, setSongResetPrompt] = useState<{
    musicAlignment?: "preserve_cuts" | "resync_beats";
    opts?: { afterCommit?: () => Promise<void>; afterCommitFailedMessage?: string };
  } | null>(null);
  // Resume-draft notice (plan §9 crash recovery). Non-null → show the notice.
  const [draftDoc, setDraftDoc] = useState<EditorDocument | null>(null);
  // A full save is a terminal navigation handoff. Derived dirty checks still
  // compare against the pre-save variant for one render, so fence draft writes
  // after commit success or they can recreate the draft we just removed.
  const draftPersistenceSuspendedRef = useRef(false);

  // ── Derived ─────────────────────────────────────────────────────────────────
  const elements = useMemo(
    () => barsToPreviewTextElements(visibleTextBars, originalsRef.current),
    [visibleTextBars],
  );

  const selectedBar = useMemo(
    () =>
      selection?.kind === "text"
        ? (visibleTextBars.find((b) => b.id === selection.id) ?? null)
        : null,
    [selection, visibleTextBars],
  );
  // 4b merge-with-neighbor: whether the selected caption cue has an earlier /
  // later caption bar it can fold into. Chronological order (not bar array
  // order) since bars can be reordered by edits.
  const captionMergeAvailability = useMemo(() => {
    if (!selectedBar || !isCaptionBar(selectedBar)) {
      return { canMergePrev: false, canMergeNext: false };
    }
    const captionBars = visibleTextBars
      .filter(isCaptionBar)
      .slice()
      .sort((a, b) => a.start_s - b.start_s);
    const index = captionBars.findIndex((b) => b.id === selectedBar.id);
    return {
      canMergePrev: index > 0,
      canMergeNext: index >= 0 && index < captionBars.length - 1,
    };
  }, [selectedBar, visibleTextBars]);

  const clipSourceDurations = useMemo(() => {
    const out: Record<string, number | null> = {};
    for (const slot of slots) {
      out[slot.key] = clip.state.clipDurations[slot.clipIndex] ?? null;
    }
    return out;
  }, [clip.state.clipDurations, slots]);

  const slotLayout = useMemo(
    () => sequentialSlotLayout(slots, clip.state.grid),
    [clip.state.grid, slots],
  );
  const timelineDuration =
    slotLayout.totalDurationS > 0 ? slotLayout.totalDurationS : duration;
  const selectedClip = useMemo(() => {
    if (selection?.kind !== "clip") return null;
    const idx = slots.findIndex((s) => s.key === selection.id);
    const slot = idx >= 0 ? slots[idx] : null;
    if (!slot) return null;
    const source = clip.clips.find((c) => c.clip_index === slot.clipIndex) ?? null;
    const windowDurationS = slotLayout.windows[idx]?.durationS ?? 0;
    return {
      slot,
      clipNumber: idx + 1,
      durationS: slot.durationS ?? windowDurationS,
      sourceDurationS: source?.duration_s ?? clipSourceDurations[slot.key] ?? null,
      sourceUrl: source?.signed_url ?? null,
      sourceKind: source?.kind ?? null,
    };
  }, [clip.clips, clipSourceDurations, selection, slotLayout.windows, slots]);

  const selectedSfx = useMemo(
    () =>
      selection?.kind === "sfx"
        ? (localSfx.find((s) => s.id === selection.id) ?? null)
        : null,
    [localSfx, selection],
  );

  const previewSfxPlacements = useMemo(
    () => (soundMuted ? [] : localSfx),
    [localSfx, soundMuted],
  );

  const selectedOverlay = useMemo(
    () =>
      selection?.kind === "overlay"
        ? (localOverlays.find((o) => o.id === selection.id) ?? null)
        : null,
    [localOverlays, selection],
  );

  const selectedVisualBlock = useMemo(
    () =>
      selection?.kind === "visual"
        ? (localVisualBlocks.find((block) => block.id === selection.id) ?? null)
        : null,
    [localVisualBlocks, selection],
  );

  const selectedMotionScene = useMemo(
    () =>
      selection?.kind === "motion"
        ? (localMotionScenes.find((scene) => scene.id === selection.id) ?? null)
        : null,
    [localMotionScenes, selection],
  );

  const selectedCameraEffect = useMemo(
    () =>
      selection?.kind === "camera"
        ? (localCameraEffects.find((effect) => effect.id === selection.id) ?? null)
        : null,
    [localCameraEffects, selection],
  );

  const handleVirtualSourceError = useCallback(() => {
    if (virtualRefetchInFlightRef.current) return;
    if (!virtualRefetchAttemptedRef.current) {
      virtualRefetchAttemptedRef.current = true;
      virtualRefetchInFlightRef.current = true;
      void Promise.resolve(reloadClipTimeline()).finally(() => {
        virtualRefetchInFlightRef.current = false;
      });
      return;
    }
    setVirtualFallback(true);
  }, [reloadClipTimeline]);

  // Expired-signature recovery for the virtual-preview music element: one
  // refetch (fresh signed URLs), then give up honestly — decks stay muted and
  // the "preview after Save" hint covers the silent music.
  const handleVirtualMusicError = useCallback(() => {
    // Drop any blob copy first — if it errored (or masked a bad fetch), the
    // retry must go back to a freshly-signed remote URL.
    setVirtualMusicBlob((prev) => {
      if (prev) revokeLocalObjectUrl(prev.url);
      return null;
    });
    if (!musicRefetchAttemptedRef.current) {
      musicRefetchAttemptedRef.current = true;
      void refreshMusicTracks();
      return;
    }
    setVirtualMusicUnavailable(true);
  }, [refreshMusicTracks]);

  const effectiveBackgroundMusicTrackId =
    backgroundMusic?.enabled === false ? null : (backgroundMusic?.track_id ?? null);
  const effectiveMusicTrackId = musicRemoved
    ? null
    : selectedMusicTrackId ?? variant?.music_track_id ?? null;
  const effectiveAudioTrackId = effectiveMusicTrackId ?? effectiveBackgroundMusicTrackId;
  const virtualMusicTrack = effectiveAudioTrackId
    ? musicTracks.find((track) => track.id === effectiveAudioTrackId) ?? null
    : null;
  const musicWindowCapability = capabilities?.music_window;
  const songWindowState = useMemo<SongWindowState | null>(() => {
    if (!musicWindowCapability || !effectiveMusicTrackId) return null;
    const isCurrentTrack = effectiveMusicTrackId === variant?.music_track_id;
    const trackDurationS = isCurrentTrack
      ? musicWindowCapability.track_duration_s
      : (virtualMusicTrack?.duration_s ?? 0);
    const beats = isCurrentTrack
      ? musicWindowCapability.beat_timestamps_s
      : (virtualMusicTrack?.beat_timestamps_s ?? []);
    const videoDurationS =
      clipDirty && timelineDuration > 0
        ? timelineDuration
        : musicWindowCapability.video_duration_s;
    const reason = isCurrentTrack
      ? musicWindowCapability.reason
      : trackDurationS <= 0
        ? "track_duration_unknown"
        : trackDurationS + 0.02 < videoDurationS
          ? "song_shorter_than_video"
          : beats.length === 0
            ? "timing_metadata_unavailable"
            : null;
    return {
      startS: musicStartS,
      videoDurationS,
      trackDurationS,
      recommendedStartS: isCurrentTrack
        ? musicWindowCapability.recommended_start_s
        : (virtualMusicTrack?.preview_start_s ?? 0),
      beatTimestampsS: beats,
      editable: reason === null,
      reason,
    };
  }, [
    effectiveMusicTrackId,
    clipDirty,
    musicStartS,
    musicWindowCapability,
    timelineDuration,
    variant?.music_track_id,
    virtualMusicTrack,
  ]);
  // All mutators and draft/history restores maintain musicDirty against the
  // persisted start. Keying this solely to that flag avoids a one-render false
  // positive while a newly loaded variant hydrates its local start offset.
  const musicWindowDirty = !!songWindowState && musicDirty;
  const virtualPreviewRequested =
    (clipDirty || musicWindowDirty || carouselMomentDirty ||
      (guidedStoryV2 && (sfxDirty || overlaysDirty || visualBlocksDirty || motionScenesDirty || textDirty))) &&
    !virtualFallback &&
    clip.loadState === "ready";
  const musicPreviewRequested =
    musicWindowDirty || backgroundMusicDirty || virtualPreviewRequested;
  const effectiveMusicTitle =
    virtualMusicTrack?.title ?? variant?.background_music?.title ?? variant?.track_title ?? "Music";
  // Fallback for tracks the public gallery doesn't list (the matcher considers
  // unpublished tracks): the status response carries a fresh-signed preview URL
  // for the variant's OWN matched track. Only valid while the effective track
  // is still the variant's — a picker selection must never reuse it.
  const variantMusicFallbackActive =
    !!variant?.music_track_id && effectiveMusicTrackId === variant.music_track_id;
  const backgroundMusicFallbackActive =
    !!variant?.background_music?.track_id &&
    effectiveBackgroundMusicTrackId === variant.background_music.track_id;
  const virtualMusicRemoteUrl = virtualMusicUnavailable
    ? null
    : virtualMusicTrack?.preview_audio_url ??
      (variantMusicFallbackActive ? variant?.music_preview_url ?? null : null) ??
      (backgroundMusicFallbackActive ? variant?.background_music?.preview_url ?? null : null);
  const virtualMusicStartS =
    effectiveMusicTrackId != null ? musicStartS : (backgroundMusic?.start_s ?? 0);

  // Blob-cache the track audio (a few MB of m4a) once per track: streaming the
  // signed GCS URL rebuffers mid-preview on real networks (measured: 5 music
  // `waiting` stalls in an 18s preview), and every rebuffer is an audible gap.
  // A local object URL can never starve. Best-effort — CORS/network failure
  // just keeps streaming from the remote URL.
  useEffect(() => {
    if (!musicPreviewRequested || !effectiveAudioTrackId || !virtualMusicRemoteUrl) return;
    if (virtualMusicBlob?.trackId === effectiveAudioTrackId) return;
    const controller = new AbortController();
    let cancelled = false;
    fetch(virtualMusicRemoteUrl, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`music fetch ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        setVirtualMusicBlob((prev) => {
          if (prev) revokeLocalObjectUrl(prev.url);
          return { trackId: effectiveAudioTrackId, url: URL.createObjectURL(blob) };
        });
      })
      .catch(() => {
        // Keep streaming the remote URL.
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [musicPreviewRequested, effectiveAudioTrackId, virtualMusicRemoteUrl, virtualMusicBlob]);
  useEffect(
    () => () => {
      setVirtualMusicBlob((prev) => {
        if (prev) revokeLocalObjectUrl(prev.url);
        return null;
      });
    },
    [],
  );
  const virtualMusicAudioUrl =
    virtualMusicBlob?.trackId === effectiveAudioTrackId && !virtualMusicUnavailable
      ? virtualMusicBlob.url
      : virtualMusicRemoteUrl;
  const backgroundMusicTrackDurationS =
    effectiveBackgroundMusicTrackId != null
      ? (virtualMusicTrack?.duration_s ?? variant?.background_music?.track_duration_s ?? null)
      : null;
  const backgroundMusicGainLinear =
    backgroundMusic?.muted || backgroundMusic?.enabled === false
      ? 0
      : Math.min(1, Math.max(0, 10 ** ((backgroundMusic?.gain_db ?? -18) / 20)));

  // Picking a different track supplies a brand-new URL — re-arm the retry
  // budget and clear the gave-up flag.
  useEffect(() => {
    setVirtualMusicUnavailable(false);
    musicRefetchAttemptedRef.current = false;
    virtualMusicAutoFetchRef.current = false;
  }, [effectiveAudioTrackId]);

  // The virtual preview starts the moment a clip edit lands, but the music
  // track list loads lazily — make sure the active track's preview URL is
  // being fetched when the preview needs it (once per edit session).
  useEffect(() => {
    if (!musicPreviewRequested || !effectiveAudioTrackId) return;
    if (musicTracksLoaded || musicTracksLoading) return;
    if (virtualMusicAutoFetchRef.current) return;
    virtualMusicAutoFetchRef.current = true;
    void refreshMusicTracks();
  }, [
    musicPreviewRequested,
    effectiveAudioTrackId,
    musicTracksLoaded,
    musicTracksLoading,
    refreshMusicTracks,
  ]);
  const carouselMomentPosition = carouselMoment ? (carouselMoment.position ?? "middle") : null;
  const carouselMomentDurationS = carouselMoment?.duration_s ?? null;
  const carouselTransitionIn = carouselMoment?.transition_in ?? carouselMoment?.transition ?? "none";
  const carouselTransitionOut = carouselMoment?.transition_out ?? carouselMoment?.transition ?? "none";
  // Referentially stable across renders unless the block's position/duration
  // actually changes: `useVirtualPreview`'s `timeline` is a `useMemo` keyed on
  // this object's IDENTITY (see virtual-timeline splice deps), and EditorShell
  // re-renders on throttled committed-time ticks during playback. A fresh
  // object literal here on every render would still break that memo, rebuilding
  // the whole virtual timeline on each transport commit
  // and re-firing useVirtualPreview's mapping effect (keyed on `timeline`) on
  // every tick — which redundantly re-seeks/re-loads the ACTIVE deck outside
  // the carousel window on every render, fighting its own smooth playback.
  // `null` (no staged block) was already stable before this feature; this
  // keeps the non-null case just as stable.
  const carouselSplice = useMemo<VirtualCarouselSplice | null>(
    () =>
      carouselMomentPosition
        ? {
            position: carouselMomentPosition,
            durationS: carouselMomentDurationS ?? 6,
            transitionIn: carouselTransitionIn,
            transitionInDurationS: carouselMoment?.transition_in_duration_s,
            transitionOut: carouselTransitionOut,
            transitionOutDurationS: carouselMoment?.transition_out_duration_s,
          }
        : null,
    [
      carouselMoment,
      carouselMomentPosition,
      carouselMomentDurationS,
      carouselTransitionIn,
      carouselTransitionOut,
    ],
  );
  const virtualPreview = useVirtualPreview({
    enabled: virtualPreviewRequested,
    slots,
    baselineSlots: clip.state.baseline,
    clips: clip.clips,
    grid: clip.state.grid,
    carousel: carouselSplice,
    currentTime,
    muted: videoMuted,
    musicAudioUrl: virtualMusicAudioUrl,
    musicStartS: virtualMusicStartS,
    soundMuted,
    musicTrackActive: effectiveAudioTrackId != null,
    frameDriven: FRAME_DRIVEN_PREVIEW_ENABLED,
    onFrameTimeUpdate: playbackClock?.publish,
    onTimeUpdate: commitPlaybackTime,
    onDuration: () => {},
    onPlayingChange: setPlaying,
    onSourceError: handleVirtualSourceError,
    onMusicError: handleVirtualMusicError,
  });
  const virtualPreviewActive =
    virtualPreviewRequested &&
    !virtualPreview.timeline.hasMissingSource &&
    virtualPreview.timeline.entries.length > 0;
  outputToBaseTimeRef.current = virtualPreviewActive
    ? (seconds) => unprojectOutputTime(virtualPreview.timeline, seconds)
    : (seconds) => seconds;
  baseToOutputTimeRef.current = virtualPreviewActive
    ? (seconds) => projectBaseTime(virtualPreview.timeline, seconds)
    : (seconds) => seconds;
  const virtualDeckLookPresets = useMemo(
    () =>
      virtualPreviewActive
        ? virtualDeckLookPresetsAtTime(
            virtualPreview.timeline,
            slots,
            currentTime,
            virtualPreview.activeDeck,
          )
        : { a: "none" as const, b: "none" as const },
    [
      currentTime,
      slots,
      virtualPreview.activeDeck,
      virtualPreview.timeline,
      virtualPreviewActive,
    ],
  );
  const virtualDeckLookAdjustments = useMemo(
    () =>
      virtualPreviewActive
        ? virtualDeckLookAdjustmentsAtTime(
            virtualPreview.timeline,
            slots,
            currentTime,
            virtualPreview.activeDeck,
          )
        : { a: null, b: null },
    [
      currentTime,
      slots,
      virtualPreview.activeDeck,
      virtualPreview.timeline,
      virtualPreviewActive,
    ],
  );
  const renderedMusicPreviewActive =
    (musicWindowDirty || backgroundMusicDirty) && !virtualPreviewActive && !!virtualMusicAudioUrl;

  // Music-only edits on variants without an editable clip timeline (notably
  // legacy song_lyrics) preview against the rendered video. The baked mix is
  // always muted while a separate audio element follows its transport.
  useEffect(() => {
    const video = videoRef.current;
    const audio = renderedMusicAudioRef.current;
    if (!video) return;
    video.muted = videoMuted || soundMuted || renderedMusicPreviewActive;
    if (!audio || !renderedMusicPreviewActive) {
      audio?.pause();
      return;
    }
    audio.muted = soundMuted;
    audio.volume = effectiveMusicTrackId == null ? backgroundMusicGainLinear : 1;
    const sync = () => {
      const target = Math.max(0, virtualMusicStartS + video.currentTime);
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        audio.currentTime = Math.min(target, Math.max(0, audio.duration - 0.01));
      } else {
        audio.currentTime = target;
      }
    };
    const play = () => {
      sync();
      void audio.play().catch(() => {});
    };
    const pause = () => audio.pause();
    const keepSynced = () => {
      const target = virtualMusicStartS + video.currentTime;
      if (Math.abs(audio.currentTime - target) > 0.15) sync();
    };
    video.addEventListener("play", play);
    video.addEventListener("pause", pause);
    video.addEventListener("seeking", sync);
    video.addEventListener("timeupdate", keepSynced);
    sync();
    if (!video.paused) play();
    return () => {
      video.removeEventListener("play", play);
      video.removeEventListener("pause", pause);
      video.removeEventListener("seeking", sync);
      video.removeEventListener("timeupdate", keepSynced);
      audio.pause();
    };
  }, [
    backgroundMusicGainLinear,
    effectiveMusicTrackId,
    renderedMusicPreviewActive,
    soundMuted,
    variant,
    videoMuted,
    virtualMusicStartS,
    virtualMusicAudioUrl,
  ]);
  const pauseVirtualPreview = virtualPreview.pause;
  const seekVirtualPreview = virtualPreview.seekTo;
  const toggleVirtualPreview = virtualPreview.toggle;
  // Rendered-video playback already has the lyrics burned into the pixels
  // (base and final are both lyric-burned) — DOM-render a lyric bar there only
  // when it's dirty, so unedited lines don't double-display. Virtual preview
  // composes raw source clips (no burned text), so it needs every lyric bar.
  // Elements-model variants are NEVER lyric-burned (product decision: lyrics
  // are not in the render) — every lyric_line element must show in BOTH
  // preview paths, same as any other text element.
  const previewElements = useMemo(() => {
    if (virtualPreviewActive || lyricsOptionalActive) return elements;
    return elements.filter((el) => {
      if (el.role !== "lyric_line") return true;
      const sourceKey = typeof el.source_params?.key === "string" ? el.source_params.key : null;
      const key = sourceKey ?? el.id.match(/^lyric_(L\d+)$/)?.[1] ?? null;
      return key != null && key in lyricLineOverrides;
    });
  }, [elements, virtualPreviewActive, lyricsOptionalActive, lyricLineOverrides]);
  const projectCanvasRange = useCallback(
    (startS: number, endS: number) =>
      virtualPreviewActive
        ? projectBaseRange(virtualPreview.timeline, { startS, endS })
        : { startS, endS },
    [virtualPreview.timeline, virtualPreviewActive],
  );
  const canvasTextBars = useMemo(
    () =>
      visibleTextBars.map((bar) => {
        const range = projectCanvasRange(bar.start_s, bar.end_s);
        return { ...bar, start_s: range.startS, end_s: range.endS };
      }),
    [projectCanvasRange, visibleTextBars],
  );
  const canvasPreviewElements = useMemo(
    () =>
      previewElements.map((element) => {
        const range = projectCanvasRange(element.start_s, element.end_s);
        return { ...element, start_s: range.startS, end_s: range.endS };
      }),
    [previewElements, projectCanvasRange],
  );
  const canvasVisualBlocks = useMemo(
    () =>
      localVisualBlocks.map((block) => {
        const range = projectCanvasRange(block.start_s, block.end_s);
        return { ...block, start_s: range.startS, end_s: range.endS };
      }),
    [localVisualBlocks, projectCanvasRange],
  );
  const canvasMotionScenes = useMemo(
    () =>
      localMotionScenes.map((scene) => {
        const range = projectCanvasRange(
          scene.start_frame / MOTION_FPS,
          scene.end_frame_exclusive / MOTION_FPS,
        );
        return {
          ...scene,
          start_frame: Math.round(range.startS * MOTION_FPS),
          end_frame_exclusive: Math.round(range.endS * MOTION_FPS),
        };
      }),
    [localMotionScenes, projectCanvasRange],
  );
  const canvasCameraEffects = useMemo(
    () =>
      localCameraEffects.map((effect) => {
        const range = projectCanvasRange(effect.start_s, effect.end_s);
        return { ...effect, start_s: range.startS, end_s: range.endS };
      }),
    [localCameraEffects, projectCanvasRange],
  );
  const canvasOverlays = useMemo(
    () =>
      localOverlays.map((overlay) => {
        const range = projectCanvasRange(overlay.start_s, overlay.end_s);
        return { ...overlay, start_s: range.startS, end_s: range.endS };
      }),
    [localOverlays, projectCanvasRange],
  );
  const canvasSfxPlacements = useMemo(
    () =>
      previewSfxPlacements.map((placement) => {
        const durationS = Math.max(
          0,
          (placement.trim_end_s ?? placement.duration_s ?? placement.trim_start_s ?? 0) -
            (placement.trim_start_s ?? 0),
        );
        const range = projectCanvasRange(placement.at_s, placement.at_s + durationS);
        return { ...placement, at_s: range.startS };
      }),
    [previewSfxPlacements, projectCanvasRange],
  );
  // `sequentialSlotLayout` is the canonical staged timeline. Even when the
  // rendered MP4 is the only available visual preview, clip edits must keep
  // the transport, ruler, and seek bounds on the staged total rather than the
  // stale rendered duration. Save will replace the visual source.
  const previewDuration = clipDirty
    ? timelineDuration
    : virtualPreviewActive
      ? virtualPreview.timeline.totalDurationS
      : duration;
  const smartPlacementCandidates = useMemo(() => {
    const targetBars = isMasonryVariant(variant)
      ? visibleTextBars.filter((bar) => bar.role !== "narrated_caption")
      : selectedBar
        ? [selectedBar]
        : [];
    return resolveSmartPlacementCandidates(variant, targetBars, previewDuration);
  }, [previewDuration, selectedBar, visibleTextBars, variant]);
  const smartPlacementCandidate = selectedBar ? (smartPlacementCandidates[0] ?? null) : null;
  const smartPlaceAllAvailable =
    !readOnly &&
    isMasonryVariant(variant) &&
    visibleTextBars.some((bar) => bar.role !== "narrated_caption") &&
    smartPlacementCandidates.length > 0;

  useEffect(() => {
    if (!virtualPreviewRequested) return;
    if (virtualPreview.timeline.hasMissingSource || virtualPreview.timeline.entries.length === 0) {
      handleVirtualSourceError();
    }
  }, [
    handleVirtualSourceError,
    virtualPreview.timeline.entries.length,
    virtualPreview.timeline.hasMissingSource,
    virtualPreviewRequested,
  ]);

  useEffect(() => {
    if (virtualPreviewActive) {
      const rendered = videoRef.current;
      if (rendered && !rendered.paused) rendered.pause();
      if (currentTime > virtualPreview.timeline.totalDurationS) {
        seekVirtualPreview(virtualPreview.timeline.totalDurationS);
      }
      return;
    }
    const rendered = videoRef.current;
    if (!rendered) return;
    const clamped = Math.max(0, Math.min(previewDuration || currentTime, currentTime));
    if (Math.abs(currentTime - clamped) > 0.001) {
      setCurrentTime(clamped);
    }
    if (Math.abs(rendered.currentTime - clamped) > 0.15) {
      rendered.currentTime = clamped;
    }
  }, [
    currentTime,
    previewDuration,
    seekVirtualPreview,
    setCurrentTime,
    virtualPreview.timeline.totalDurationS,
    virtualPreviewActive,
  ]);

  const pausePlayback = useCallback(() => {
    if (virtualPreviewActive) pauseVirtualPreview();
    else {
      const v = videoRef.current;
      if (v && !v.paused) v.pause();
    }
  }, [pauseVirtualPreview, virtualPreviewActive]);

  const seekPlaybackTo = useCallback(
    (seconds: number) => {
      const clamped = Math.max(0, Math.min(previewDuration || seconds, seconds));
      if (virtualPreviewActive) seekVirtualPreview(clamped);
      else {
        const v = videoRef.current;
        if (v) {
          if (!v.paused) v.pause();
          v.currentTime = clamped;
        }
        setCurrentTime(clamped);
      }
    },
    [
      previewDuration,
      seekVirtualPreview,
      setCurrentTime,
      virtualPreviewActive,
    ],
  );

  // Selection on a deleted/vanished bar clears itself.
  useEffect(() => {
    if (selection?.kind === "text" && !visibleTextBars.some((b) => b.id === selection.id)) {
      clear();
      setLightSheetOpen(false);
    }
  }, [selection, visibleTextBars, clear]);

  useEffect(() => {
    if (layoutMode === "light") {
      // Pocket mode routes every tool through sheets instead of force-closing
      // them; legacy light mode keeps the nova-only gate.
      if (!POCKET_UI) setActiveTool((tool) => (tool === "nova" ? tool : null));
      setCanvasTool("select");
    } else {
      setLightSheetOpen(false);
      dispatchPocket({ type: "CLOSE_SHEET" });
    }
  }, [layoutMode]);

  // Pocket: the inspector sheet is selection-scoped — deselection closes it.
  useEffect(() => {
    if (pocketActive && selection === null && pocket.sheet?.kind === "inspector") {
      dispatchPocket({ type: "CLOSE_SHEET" });
    }
  }, [pocketActive, selection, pocket.sheet]);

  // Pocket lifecycle delta (§5): backgrounding pauses playback. The draft
  // already persists continuously on document change; nothing extra to flush.
  useEffect(() => {
    if (!pocketActive) return;
    const onHidden = () => {
      if (document.visibilityState === "hidden") pausePlayback();
    };
    const onPageHide = () => pausePlayback();
    document.addEventListener("visibilitychange", onHidden);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      document.removeEventListener("visibilitychange", onHidden);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [pocketActive, pausePlayback]);

  useEffect(() => {
    if (!panEnabled && canvasTool === "pan") {
      setCanvasTool("select");
    }
  }, [canvasTool, panEnabled]);

  useEffect(() => {
    try {
      setCopilotSaveNoticeDismissed(
        window.localStorage.getItem(COPILOT_SAVE_NOTICE_KEY) === "true",
      );
    } catch {
      setCopilotSaveNoticeDismissed(true);
    }
  }, []);

  useEffect(() => {
    if (
      (activeTool !== "sounds" && activeTool !== "nova" && localSfx.length === 0) ||
      sfxGlossaryEffects.length > 0
    ) {
      return;
    }
    let cancelled = false;
    setSfxGlossaryLoading(true);
    void getSoundEffects()
      .then((effects) => {
        if (!cancelled) setSfxGlossaryEffects(effects);
      })
      .catch(() => {
        if (!cancelled) notify("Couldn't load sound effects.");
      })
      .finally(() => {
        if (!cancelled) setSfxGlossaryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTool, localSfx.length, sfxGlossaryEffects.length, notify]);

  const musicPickerShouldLoad =
    (!!variant?.music_track_id ||
      !!variant?.background_music?.track_id ||
      !!selectedMusicTrackId ||
      !!effectiveBackgroundMusicTrackId ||
      activeTool === "sounds" ||
      activeTool === "nova" ||
      selection?.kind === "music") &&
    !musicTracksLoaded;
  useEffect(() => {
    if (!musicPickerShouldLoad) return;
    void refreshMusicTracks();
  }, [musicPickerShouldLoad, refreshMusicTracks]);

  useEffect(() => {
    if (localSfx.length === 0 || sfxGlossaryEffects.length === 0) return;
    setLocalSfxAudioUrls((current) => {
      const next = { ...current };
      let changed = false;
      const effectsById = new Map(sfxGlossaryEffects.map((effect) => [effect.id, effect]));
      for (const placement of localSfx) {
        const effectId = placement.sound_effect_id ?? null;
        if (!effectId) continue;
        const url = effectsById.get(effectId)?.preview_audio_url;
        if (!url) continue;
        if (next[placement.id] !== url) {
          next[placement.id] = url;
          changed = true;
        }
        if (placement.src_gcs_path && next[placement.src_gcs_path] !== url) {
          next[placement.src_gcs_path] = url;
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [localSfx, sfxGlossaryEffects]);

  useEffect(() => {
    if (localSfx.length === 0) return;
    let cancelled = false;
    const missingUserPaths = Array.from(
      new Set(
        localSfx
          .map((placement) => placement.src_gcs_path ?? "")
          .filter((path) => path.startsWith("users/") && !localSfxAudioUrls[path]),
      ),
    );
    if (missingUserPaths.length === 0) return;
    void Promise.all(
      missingUserPaths.map(async (path) => {
        try {
          const url = await getSfxAudioUrl(itemId, path);
          return { path, url };
        } catch {
          return null;
        }
      }),
    ).then((rows) => {
      if (cancelled) return;
      setLocalSfxAudioUrls((current) => {
        const next = { ...current };
        let changed = false;
        for (const row of rows) {
          if (!row || next[row.path] === row.url) continue;
          next[row.path] = row.url;
          changed = true;
        }
        return changed ? next : current;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [itemId, localSfx, localSfxAudioUrls]);

  const overlayPoolShouldLoad =
    (MEDIA_OVERLAYS_UI_ENABLED &&
      overlaysAllowed &&
      (activeTool === "nova" || activeTool === "overlays")) ||
    (VISUAL_BLOCKS_UI_ENABLED &&
      visualBlocksAllowed &&
      activeTool === "visuals");
  useEffect(() => {
    if (!overlayPoolShouldLoad) return;
    let cancelled = false;
    const startedAtEpoch = poolListEpoch.current;
    listPoolAssets(itemId)
      .then((res) => {
        if (cancelled || poolListEpoch.current !== startedAtEpoch) return;
        setPoolAssets((current) =>
          mergePoolAssetsPreservingDisplayUrls(current, res.assets),
        );
        setMaxPoolAssets(res.max_assets);
        setServerPoolReservations(res.active_reservations ?? []);
        setServerPoolOccupiedCount(res.occupied_assets ?? res.assets.length);
        setPoolUnavailable(false);
        setPoolError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        if (isUnavailableError(err)) setPoolUnavailable(true);
        else setPoolError("We couldn't load your visuals. Try again.");
      });
    return () => {
      cancelled = true;
    };
  }, [itemId, overlayPoolShouldLoad]);

  const hasBusyPoolAssets =
    poolAssets.some(
      (a) =>
        a.status === "queued" ||
        a.status === "analyzing" ||
        a.status === "uploaded" ||
        a.media_status === "pending" ||
        a.preview_status === "pending",
    ) || serverPoolReservations.some((reservation) => reservation.release_at === null);
  useEffect(() => {
    if (!overlayPoolShouldLoad || !hasBusyPoolAssets || poolUnavailable) return;
    const id = setInterval(() => {
      if (poolPollInFlight.current) return;
      poolPollInFlight.current = true;
      const startedAtEpoch = poolListEpoch.current;
      listPoolAssets(itemId)
        .then((res) => {
          if (poolListEpoch.current !== startedAtEpoch) return;
          setPoolAssets((current) =>
            mergePoolAssetsPreservingDisplayUrls(current, res.assets),
          );
          setMaxPoolAssets(res.max_assets);
          setServerPoolReservations(res.active_reservations ?? []);
          setServerPoolOccupiedCount(res.occupied_assets ?? res.assets.length);
        })
        .catch(() => {})
        .finally(() => {
          poolPollInFlight.current = false;
        });
    }, SUGGESTION_POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [hasBusyPoolAssets, itemId, overlayPoolShouldLoad, poolUnavailable]);

  const overlaySuggestionsEnabled =
    process.env.NEXT_PUBLIC_OVERLAY_AUTOPLACE_ENABLED === "true" &&
    capabilities?.suggestions === true &&
    !readOnly;
  const overlaySuggestionsShouldLoad =
    overlaySuggestionsEnabled && (activeTool === "nova" || activeTool === "overlays");
  const overlaySuggestions = useEditorOverlaySuggestions({
    itemId,
    variantId: variant?.variant_id ?? variantParam ?? "",
    enabled: overlaySuggestionsShouldLoad,
  });

  const handlePoolFiles = useCallback(
    (files: FileList | File[] | null) => {
      setPoolError(null);
      addPoolFiles(files);
    },
    [addPoolFiles],
  );

  const handleRemovePoolAsset = useCallback(
    (asset: PoolAsset) => {
      void deletePoolAsset(itemId, asset.id)
        .then(() => {
          poolListEpoch.current += 1;
          setPoolAssets((prev) => prev.filter((a) => a.id !== asset.id));
          setServerPoolOccupiedCount((current) => Math.max(0, current - 1));
        })
        .catch((err) => {
          if (isUnavailableError(err)) setPoolUnavailable(true);
          else setPoolError("We couldn't remove that visual. Try again.");
        });
    },
    [itemId],
  );

  const handleRetryPoolAsset = useCallback(
    (asset: PoolAsset) => {
      void reanalyzePoolAsset(itemId, asset.id)
        .then((updated) => {
          poolListEpoch.current += 1;
          setPoolAssets((prev) =>
            prev.map((row) => (row.id === updated.id ? updated : row)),
          );
        })
        .catch((err) => {
          if (isUnavailableError(err)) setPoolUnavailable(true);
          else setPoolError("We couldn't retry that visual. Try again.");
        });
    },
    [itemId],
  );

  const handleSavePoolAssetContext = useCallback(
    async (asset: PoolAsset, userContext: string) => {
      const updated = await updatePoolAssetContext(itemId, asset.id, userContext || null);
      poolListEpoch.current += 1;
      setPoolAssets((prev) => prev.map((a) => (a.id === updated.id ? updated : a)));
      overlaySuggestions.clearLocal();
    },
    [itemId, overlaySuggestions],
  );

  const sampleWord = useMemo(() => {
    const first = selectedBar?.text.trim().split(/\s+/)[0];
    return first && first.length > 0 ? first.slice(0, 8).toUpperCase() : null;
  }, [selectedBar]);

  // "Applied" is DERIVED (field comparison), not bookkept — a preset ring
  // stays honest even after manual tweaks diverge from the preset.
  const appliedPresetId = useMemo(() => {
    if (!selectedBar) return null;
    return TEXT_PRESETS.find((p) => presetMatchesFields(p, selectedBar))?.id ?? null;
  }, [selectedBar]);

  // ── Actions ─────────────────────────────────────────────────────────────────

  const selectElement = useCallback(
    (
      kind: EditorSelectionKind,
      id: string,
      options: { preserveOverlayTool?: boolean } = {},
    ) => {
      select(kind, id);
      if (
        shouldCloseToolOnSelection({
          layoutMode,
          activeTool,
          preserveOverlayTool: options.preserveOverlayTool,
        })
      ) {
        setActiveTool(null);
      }
      if (kind === "text") {
        setInspectorTab("basic"); // selecting anything activates + switches to Basic (D6)
        // Pocket mode surfaces the context strip on selection instead of
        // auto-opening the edit sheet (legacy light keeps tap-opens-sheet).
        if (layoutMode === "light" && !POCKET_UI) setLightSheetOpen(true);
      } else if (kind === "clip") {
        setInspectorTab("basic");
        const projectedEntry = virtualPreviewActive
          ? virtualPreview.timeline.entries.find(
              (entry) => entry.kind === "clip" && entry.slotKey === id,
            )
          : null;
        const startS =
          projectedEntry?.startS ??
          outputTimeForSlotBoundary({
            slots,
            grid: clip.state.grid,
            key: id,
            boundary: "start",
            rendered: !virtualPreviewActive,
            renderedOutputDurationS: duration,
            fallbackOverlapS: 0,
          });
        if (startS != null) {
          seekPlaybackTo(startS);
        }
      } else if (kind === "sfx") {
        setInspectorTab("basic");
        const sfx = localSfx.find((p) => p.id === id);
        if (sfx) seekPlaybackTo(baseToOutputTimeRef.current(sfx.at_s ?? 0));
      } else if (kind === "overlay") {
        setInspectorTab("basic");
        const overlay = localOverlays.find((o) => o.id === id);
        if (overlay) seekPlaybackTo(baseToOutputTimeRef.current(overlay.start_s));
      } else if (kind === "motion") {
        setInspectorTab("basic");
        const block = localMotionScenes.find((scene) => scene.id === id);
        if (block) seekPlaybackTo(baseToOutputTimeRef.current(block.start_frame / MOTION_FPS));
        setActiveTool("visuals");
        if (layoutMode === "light" && POCKET_UI) {
          dispatchPocket({ type: "OPEN_INSPECTOR" });
        }
      } else if (kind === "visual") {
        setInspectorTab("basic");
        const block = localVisualBlocks.find((candidate) => candidate.id === id);
        if (block) seekPlaybackTo(baseToOutputTimeRef.current(block.start_s));
        setActiveTool("visuals");
        if (layoutMode === "light" && POCKET_UI) dispatchPocket({ type: "OPEN_INSPECTOR" });
      } else if (kind === "carousel") {
        setInspectorTab("basic");
        setActiveTool("visuals");
        const carouselEntry = virtualPreview.timeline.entries.find(
          (entry) => entry.kind === "carousel",
        );
        if (carouselEntry) seekPlaybackTo(carouselEntry.startS);
        if (layoutMode === "light" && POCKET_UI) {
          dispatchPocket({ type: "OPEN_INSPECTOR" });
        }
      }
    },
    [activeTool, clip.state.grid, duration, layoutMode, localMotionScenes, localOverlays, localSfx, localVisualBlocks, seekPlaybackTo, select, slots, virtualPreview.timeline.entries, virtualPreviewActive],
  );

  const selectText = useCallback(
    (id: string) => selectElement("text", id),
    [selectElement],
  );

  const selectCarousel = useCallback(
    () => selectElement("carousel", CAROUSEL_SELECTION_ID),
    [selectElement],
  );

  // Adding a brand-new Carousel stages its default and selects it in the same
  // click. The virtual splice appears on the following render, so seek again
  // once that entry exists; otherwise the inspector opens over unrelated
  // footage and the user cannot see the effect they are configuring.
  useEffect(() => {
    if (selection?.kind !== "carousel" || carouselMoment === null) return;
    const entry = virtualPreview.timeline.entries.find(
      (candidate) => candidate.kind === "carousel",
    );
    if (entry) seekPlaybackTo(entry.startS);
  }, [carouselMoment, seekPlaybackTo, selection?.kind, virtualPreview.timeline.entries]);

  const revealCopilotFocus = useCallback((focus: DirectorPreviewFocus) => {
    setPendingCopilotFocus({ ...focus });
  }, []);

  // Apply handlers enqueue this focus while they enqueue reducer/state updates.
  // Running the reveal after React commits guarantees selection and transport
  // read the new text/SFX/overlay state instead of the pre-apply render.
  useEffect(() => {
    if (!pendingCopilotFocus) return;
    pausePlayback();
    seekPlaybackTo(baseToOutputTimeRef.current(pendingCopilotFocus.seekS));
    selectElement(pendingCopilotFocus.kind, pendingCopilotFocus.id, {
      preserveOverlayTool: true,
    });
    setPendingCopilotFocus((current) =>
      current === pendingCopilotFocus ? null : current,
    );
  }, [
    pausePlayback,
    pendingCopilotFocus,
    seekPlaybackTo,
    selectElement,
  ]);

  const patchBar = useCallback(
    (id: string, patch: Partial<Omit<TextElementBar, "id" | "role">>) => {
      if (readOnly) return;
      const target = state.bars.find((bar) => bar.id === id);
      if (target && !isCaptionBar(target) && !textElementsAllowed) return;
      let patchToApply = patch;
      if (
        target &&
        !isCaptionBar(target) &&
        TEXT_MOTION_V2_UI_ENABLED &&
        typeof patch.effect === "string" &&
        !Object.prototype.hasOwnProperty.call(patch, "motion")
      ) {
        patchToApply = {
          ...patch,
          ...motionPatchForEffect(target, patch.effect, duration),
        };
      }
      if (isCaptionBar(target)) {
        const hasCaptionCuePatch =
          Object.prototype.hasOwnProperty.call(patch, "start_s") ||
          Object.prototype.hasOwnProperty.call(patch, "end_s") ||
          Object.prototype.hasOwnProperty.call(patch, "text") ||
          // 4b Emphasize toggle: sets/clears smart_emphasis + smart_style,
          // which persist through the same cue-list PATCH as text/timing.
          Object.prototype.hasOwnProperty.call(patch, "smart_style") ||
          Object.prototype.hasOwnProperty.call(patch, "smart_emphasis") ||
          // Lane PR-A "This caption" per-cue overrides: same cue-list PATCH,
          // NEVER the variant-level meta patch (captionMetaPatchFromCaptionBarPatch
          // below only reads the OLD font_family/size_px/color keys, which this
          // patch never sets when it's a per-cue-only edit).
          Object.prototype.hasOwnProperty.call(patch, "cue_font_family") ||
          Object.prototype.hasOwnProperty.call(patch, "cue_text_color") ||
          Object.prototype.hasOwnProperty.call(patch, "cue_size_px");
        const metaPatch = captionMetaPatchFromCaptionBarPatch(patch);
        if (!hasCaptionCuePatch && Object.keys(metaPatch).length === 0) {
          return;
        }
        patchToApply = localCaptionBarPatchFromPatch(patch);
        history.record();
        if (hasCaptionCuePatch) {
          setCaptionDirty(true);
        }
        if (Object.keys(metaPatch).length > 0) {
          setCaptionMeta((current) => {
            const base = current ?? (variant ? captionMetaFromVariant(variant) : null);
            return base ? { ...base, ...metaPatch } : base;
          });
          setCaptionMetaPatch((current) => ({ ...current, ...metaPatch }));
          setCaptionMetaDirty(true);
        }
      } else if (lyricsOptionalActive || !isLyricBar(target)) {
        history.record();
        setTextDirty(true);
      } else {
        history.record();
      }
      dispatch({ type: "PATCH_BAR", id, patch: patchToApply });
    },
    [
      history,
      duration,
      lyricsOptionalActive,
      readOnly,
      state.bars,
      textElementsAllowed,
      variant,
    ],
  );

  const editTextBar = useCallback(
    (id: string, text: string) => {
      if (readOnly) return;
      const target = state.bars.find((bar) => bar.id === id);
      if (!target) return;
      if (!isCaptionBar(target) && !textElementsAllowed) return;
      history.record(`text:${id}`);
      if (isCaptionBar(target)) {
        setCaptionDirty(true);
      } else if (lyricsOptionalActive || !isLyricBar(target)) {
        setTextDirty(true);
      }
      dispatch({
        type: "PATCH_BAR",
        id,
        patch: motionPatchForText(target, text, previewDuration),
      });
    },
    [
      history,
      lyricsOptionalActive,
      previewDuration,
      readOnly,
      state.bars,
      textElementsAllowed,
    ],
  );

  const editSelectedText = useCallback(
    (text: string) => {
      if (selectedBar) editTextBar(selectedBar.id, text);
    },
    [editTextBar, selectedBar],
  );

  const beginTextMotionGesture = useCallback(() => {
    if (!readOnly) history.record();
  }, [history, readOnly]);

  const previewSelectedTextMotion = useCallback(
    (motionPatch: Partial<TextMotionConfigV2>) => {
      if (!selectedBar || readOnly) return;
      dispatch({
        type: "PREVIEW_BAR",
        id: selectedBar.id,
        patch: motionPatchForConfig(selectedBar, motionPatch, duration),
      });
    },
    [duration, readOnly, selectedBar],
  );

  const commitSelectedTextMotion = useCallback(
    (motionPatch: Partial<TextMotionConfigV2>) => {
      if (!selectedBar || readOnly) return;
      setTextDirty(true);
      dispatch({
        type: "PREVIEW_BAR",
        id: selectedBar.id,
        patch: motionPatchForConfig(selectedBar, motionPatch, duration),
      });
    },
    [duration, readOnly, selectedBar],
  );

  /**
   * "All captions" globals, from the Captions drawer — the variant-level
   * styling that used to live in the inspector behind a cue selection.
   *
   * Two writes, both required:
   *  1. the meta patch, which is what Save actually sends; and
   *  2. a fan-out of the equivalent styling onto EVERY caption bar, because
   *     the canvas preview and the timeline read styling off the bars. Without
   *     (2) a global font change previews on only the one cue that happened to
   *     be patched — the exact fidelity bug the drawer was built to end.
   *
   * PATCH_BARS (not a PATCH_BAR loop) keeps the fan-out one undo step.
   */
  const patchCaptionMeta = useCallback(
    (patch: CaptionMetaPatch) => {
      if (readOnly) return;
      if (Object.keys(patch).length === 0) return;
      history.record();
      setCaptionMeta((current) => {
        const base = current ?? (variant ? captionMetaFromVariant(variant) : null);
        return base ? { ...base, ...patch } : base;
      });
      setCaptionMetaPatch((current) => ({ ...current, ...patch }));
      setCaptionMetaDirty(true);
      const barPatch = captionBarPatchFromMetaPatch(patch);
      if (Object.keys(barPatch).length === 0) return; // enabled/style: meta-only
      const patches = state.bars
        .filter(isCaptionBar)
        .map((bar) => ({ id: bar.id, patch: barPatch }));
      if (patches.length > 0) dispatch({ type: "PATCH_BARS", patches });
    },
    [history, readOnly, state.bars, variant],
  );

  /**
   * Find-and-replace across the cue list. Returns how many cues changed so the
   * drawer can report it. One PATCH_BARS = one Cmd+Z for the whole sweep,
   * which is the only reason a 12-line replace is safe to offer at all.
   */
  const replaceInCaptions = useCallback(
    (find: string, replace: string): number => {
      if (readOnly) return 0;
      const { patches } = buildCaptionTextReplacement(state.bars, find, replace);
      if (patches.length === 0) return 0;
      history.record();
      setCaptionDirty(true);
      dispatch({ type: "PATCH_BARS", patches });
      return patches.length;
    },
    [history, readOnly, state.bars],
  );

  /** Cue rows for the drawer, chronological (bar array order is insertion order). */
  const captionCueRows = useMemo<CaptionCueRow[]>(
    () =>
      state.bars
        .filter(isCaptionBar)
        .map((bar) => ({
          id: bar.id,
          text: bar.text,
          start_s: bar.start_s,
          end_s: bar.end_s,
        }))
        .sort((a, b) => a.start_s - b.start_s),
    [state.bars],
  );

  // 4b merge-with-neighbor: folds an orphan caption fragment into its
  // chronological prev/next cue (concatenated text, extended end_s), then
  // removes the absorbed bar. One history step, mirroring `deleteSelected`'s
  // record-then-dispatch shape so the merge undoes atomically.
  const mergeCaptionCue = useCallback(
    (direction: "prev" | "next") => {
      if (readOnly) return;
      if (!selection || selection.kind !== "text") return;
      const captionBars = state.bars
        .filter(isCaptionBar)
        .slice()
        .sort((a, b) => a.start_s - b.start_s);
      const index = captionBars.findIndex((b) => b.id === selection.id);
      if (index < 0) return;
      const neighbor = captionBars[direction === "prev" ? index - 1 : index + 1];
      if (!neighbor) return;
      const current = captionBars[index];
      const [earlier, later] = direction === "prev" ? [neighbor, current] : [current, neighbor];
      history.record();
      setCaptionDirty(true);
      dispatch({
        type: "PATCH_BAR",
        id: earlier.id,
        patch: { text: `${earlier.text} ${later.text}`.trim(), end_s: later.end_s },
      });
      dispatch({ type: "DELETE_BAR", id: later.id });
      selectText(earlier.id);
    },
    [history, readOnly, selectText, selection, state.bars],
  );

  const selectedTextMotion = useMemo(
    () => collageMotionForTextBar(variant, duration, selectedBar),
    [duration, selectedBar, variant],
  );
  const selectedTextBaseTime = outputToBaseTimeRef.current(currentTime);
  const selectedTextBoxScreenXFrac = selectedBar
    ? textBoxScreenXFrac(selectedTextMotion, selectedTextBaseTime, selectedBar.x_frac ?? 0.5)
    : undefined;
  const setSelectedTextBoxPosition = useCallback(
    (position: TextBoxHorizontalPosition) => {
      if (!selectedBar) return;
      patchBar(
        selectedBar.id,
        textBoxPositionPatchForBar({
          motion: selectedTextMotion,
          currentTimeS: selectedTextBaseTime,
          bar: selectedBar,
          position,
        }),
      );
    },
    [patchBar, selectedBar, selectedTextBaseTime, selectedTextMotion],
  );

  // Elements-model Lyrics toggle: ON fetches (or reuses the cached) seed bars
  // and inserts them as one undoable ADD_LYRIC_BARS action — no render
  // round-trip, same as adding any other text bar. OFF removes every
  // lyric_line bar in one undoable REMOVE_LYRIC_BARS action. Both flip
  // textDirty so Save ships them through the normal text_elements commit.
  const toggleLyricsOptional = useCallback(
    async (next: boolean) => {
      if (readOnly || !variant) return;
      if (next === hasLyricBars) return;
      if (!next) {
        history.record("lyrics-toggle-off");
        setTextDirty(true);
        dispatch({ type: "REMOVE_LYRIC_BARS" });
        return;
      }
      if (!lyricsCap.can_toggle_on && !lyricsCap.enabled) {
        notify(lyricsToggleHint(lyricsCap.reason) ?? "Lyrics can't be enabled for this edit.");
        return;
      }
      const variantId = variant.variant_id;
      const cached = lyricSeedsCacheRef.current.get(variantId);
      if (cached) {
        cached.forEach((el) => originalsRef.current.set(el.id, el));
        history.record("lyrics-toggle-on");
        setTextDirty(true);
        dispatch({ type: "ADD_LYRIC_BARS", bars: seedBarsFromLyricSeeds(cached) });
        return;
      }
      setLyricSeedsLoading(true);
      setLyricSeedsError(null);
      try {
        const res = await getLyricSeeds(itemId, variantId);
        lyricSeedsCacheRef.current.set(variantId, res.elements);
        // The active variant changed while this request was in flight —
        // don't insert seeds for a variant no longer on screen.
        if (seededVariantIdRef.current !== variantId) return;
        res.elements.forEach((el) => originalsRef.current.set(el.id, el));
        history.record("lyrics-toggle-on");
        setTextDirty(true);
        dispatch({ type: "ADD_LYRIC_BARS", bars: seedBarsFromLyricSeeds(res.elements) });
      } catch (err) {
        if (err instanceof LyricSeedsError) {
          setLyricSeedsError(err.reason);
          notify(
            err.reason === "no_lyrics"
              ? "This song doesn't have synced lyrics."
              : "Lyrics aren't available for this edit.",
          );
        } else {
          notify("We couldn't load lyrics. Try again.");
        }
      } finally {
        setLyricSeedsLoading(false);
      }
    },
    [readOnly, variant, hasLyricBars, lyricsCap, itemId, history, notify],
  );

  const applySmartPlacement = useCallback(() => {
    if (readOnly) return;
    if (isMasonryVariant(variant)) {
      const targetBars = visibleTextBars.filter(
        (bar) => bar.role !== "narrated_caption" && !isLyricBar(bar),
      );
      if (targetBars.length === 0) return;
      const assignments = resolveSmartPlacementAssignments(
        variant,
        targetBars,
        duration,
        outputToBaseTimeRef.current(currentTime),
      );
      if (!assignments) {
        notify("Not enough empty collage pockets for all overlapping text blocks.");
        return;
      }
      history.record();
      setTextDirty(true);
      targetBars.forEach((bar, index) => {
        const candidate = assignments[index];
        dispatch({
          type: "PATCH_BAR",
          id: bar.id,
          patch: smartPlacementPatchForBar(bar, candidate),
        });
      });
      return;
    }
    if (!selectedBar || !smartPlacementCandidate) return;
    patchBar(selectedBar.id, smartPlacementPatchForBar(selectedBar, smartPlacementCandidate));
  }, [
    notify,
    history,
    currentTime,
    patchBar,
    duration,
    readOnly,
    selectedBar,
    smartPlacementCandidate,
    visibleTextBars,
    variant,
  ]);

  const applySelectedSmartPlacement = useCallback(() => {
    if (readOnly || !selectedBar) return;
    const candidate = isMasonryVariant(variant)
      ? resolveSmartPlacementCandidate(
          variant,
          selectedBar,
          duration,
          outputToBaseTimeRef.current(currentTime),
        )
      : smartPlacementCandidate;
    if (!candidate) {
      if (isMasonryVariant(variant)) {
        notify("No visible collage pocket can fit this text at this time.");
      }
      return;
    }
    patchBar(selectedBar.id, smartPlacementPatchForBar(selectedBar, candidate));
  }, [
    currentTime,
    patchBar,
    duration,
    readOnly,
    selectedBar,
    smartPlacementCandidate,
    variant,
    notify,
  ]);

  const pickMusicTrack = useCallback(
    (trackId: string) => {
      if (readOnly || !variant) return;
      const selectedTrack = musicTracks.find((track) => track.id === trackId);
      if (variant.music_track_id) {
        if (trackId === selectedMusicTrackId && !musicRemoved) return;
        history.record();
        setSelectedMusicTrackId(trackId);
        setMusicRemoved(false);
        const nextStartS = selectedTrack?.preview_start_s ?? 0;
        setMusicStartS(nextStartS);
        setMusicDirty(
          trackId !== variant.music_track_id ||
            Math.abs(nextStartS - (variant.music_preview_start_s ?? 0)) > 0.005,
        );
        return;
      }
      if (trackId === backgroundMusic?.track_id && backgroundMusic?.enabled !== false) return;
      history.record();
      const nextStartS = selectedTrack?.preview_start_s ?? 0;
      const trackDurationS = selectedTrack?.duration_s ?? null;
      const nextEndS =
        trackDurationS != null
          ? Math.min(trackDurationS, nextStartS + Math.max(0.1, previewDuration))
          : null;
      setSelectedMusicTrackId(null);
      setMusicDirty(false);
      setBackgroundMusic({
        track_id: trackId,
        enabled: true,
        start_s: nextStartS,
        end_s: nextEndS,
        gain_db: backgroundMusic?.gain_db ?? -18,
        muted: false,
      });
      setBackgroundMusicDirty(true);
      selectElement("music", "background");
    },
    [
      backgroundMusic?.enabled,
      backgroundMusic?.gain_db,
      backgroundMusic?.track_id,
      history,
      musicRemoved,
      musicTracks,
      previewDuration,
      readOnly,
      selectElement,
      selectedMusicTrackId,
      variant,
    ],
  );

  // Remove the variant's song entirely (mirrors pickMusicTrack): explicit
  // removed state (null selectedMusicTrackId alone = "untouched"), musicDirty
  // drives the Save; the commit emits `remove_music: true` and the server
  // re-renders through the track-free path.
  const removeMusic = useCallback(() => {
    if (readOnly || !variant?.music_track_id || musicRemoved) return;
    history.record();
    setSelectedMusicTrackId(null);
    setMusicRemoved(true);
    setMusicStartS(0);
    setMusicDirty(true);
  }, [history, musicRemoved, readOnly, variant?.music_track_id]);

  const patchBackgroundMusic = useCallback(
    (patch: Partial<EditorCommitBackgroundMusic>) => {
      if (readOnly || !backgroundMusic?.track_id) return;
      history.record("background-music");
      setBackgroundMusic((current) =>
        current?.track_id
          ? {
              ...current,
              enabled: current.enabled !== false,
              ...patch,
            }
          : current,
      );
      setBackgroundMusicDirty(true);
    },
    [backgroundMusic?.track_id, history, readOnly],
  );

  const removeBackgroundMusic = useCallback(() => {
    if (readOnly || !backgroundMusic?.track_id) return;
    history.record();
    setBackgroundMusic({ track_id: null, enabled: false });
    setBackgroundMusicDirty(true);
    clear();
  }, [backgroundMusic?.track_id, clear, history, readOnly]);

  const patchMusicStart = useCallback(
    (startS: number) => {
      if (readOnly || !songWindowState?.editable || !Number.isFinite(startS)) return;
      const maxStart = Math.max(
        0,
        songWindowState.trackDurationS - songWindowState.videoDurationS,
      );
      const nextStartS = Math.max(0, Math.min(maxStart, startS));
      setMusicStartS(nextStartS);
      setMusicDirty(
        selectedMusicTrackId !== variant?.music_track_id ||
          Math.abs(nextStartS - (variant?.music_preview_start_s ?? 0)) > 0.005,
      );
    }, [readOnly, selectedMusicTrackId, songWindowState, variant],
  );

  const musicWindowControl = songWindowState
    ? {
        value: songWindowState,
        onPreview: patchMusicStart,
        onChange: patchMusicStart,
        onBegin: () => history.record(),
      }
    : undefined;

  const previewTextTiming = useCallback(
    (
      id: string,
      patch: Pick<TextElementBar, "start_s" | "end_s">,
      handle: "left" | "right" | "body",
      origin: TextElementBar,
    ) => {
      if (readOnly) return;
      if (state.bars.find((bar) => bar.id === id)?.role === "lyric_line") return;
      setTextDirty(true);
      dispatch({
        type: "RESET",
        bars: state.bars.map((bar) => {
          if (bar.id !== id) return bar;
          const next = { ...origin, ...patch };
          return handle === "body"
            ? next
            : { ...next, ...motionPatchForManualEnd(next, next.end_s, duration) };
        }),
      });
    },
    [duration, readOnly, state.bars],
  );

  const patchSelectedTextTiming = useCallback(
    (patch: { start_s?: number; end_s?: number }) => {
      if (!selectedBar || readOnly) return;
      const next = applyTextTimingInput({
        startS: patch.start_s ?? selectedBar.start_s,
        endS: patch.end_s ?? selectedBar.end_s,
        videoDurationS: duration,
      });
      if (!rangesDiffer(selectedBar, next)) return;
      const motionTimingPatch =
        patch.end_s !== undefined || patch.start_s !== undefined
          ? motionPatchForManualEnd(
              { ...selectedBar, start_s: next.start_s },
              next.end_s,
              duration,
            )
          : {};
      patchBar(selectedBar.id, { ...next, ...motionTimingPatch });
    },
    [duration, patchBar, readOnly, selectedBar],
  );

  const previewClipTiming = useCallback(
    (
      key: string,
      patch: Pick<DraftSlot, "inS" | "durationS" | "durationBeats">,
    ) => {
      if (readOnly || !clipCan("trim", true)) return;
      setLocalSlots((cur) =>
        (cur ?? slots).map((s) => (s.key === key ? { ...s, ...patch } : s)),
      );
    },
    [clipCan, readOnly, slots],
  );

  const addClipToTimeline = useCallback(
    (clipIndex: number) => {
      if (readOnly || !clipAddAllowed) return;
      if (
        !guidedStoryV2 &&
        slots.some((slot) => !slot.removed && slot.clipIndex === clipIndex)
      )
        return;

      let nextSlots: DraftSlot[];
      let added: DraftSlot | undefined;
      if (guidedStoryV2) {
        const source = clip.clips.find((candidate) => candidate.clip_index === clipIndex);
        const roomS = Math.max(0, 60 - slotLayout.totalDurationS);
        if (roomS < 0.1) {
          notify("This cut has no room for another clip. Shorten or remove a clip first.");
          return;
        }
        const durationS = Math.min(
          3,
          source?.kind === "video" ? (source.duration_s ?? 3) : 3,
          roomS,
        );
        added = {
          key: nextAddedKey(),
          slotId: null,
          parentSegmentId: null,
          clipIndex,
          inS: 0,
          durationBeats: null,
          durationS,
          removed: false,
          momentDescription: null,
          transitionAfter: "cut",
          transitionDurationS: null,
          lookPreset: "none",
          lookAdjustments: null,
        };
        nextSlots = [...slots, added];
      } else {
        const nextState = timelineReducer(
          {
            ...clip.state,
            slots,
            past: [],
            future: [],
          },
          { type: "ADD", clipIndex },
        );
        if (nextState.slots.length === slots.length) {
          notify("This cut has no room for another clip. Shorten or remove a clip first.");
          return;
        }
        nextSlots = nextState.slots;
        added = nextSlots[nextSlots.length - 1];
      }
      history.record();
      setLocalSlots(nextSlots.map((slot) => ({ ...slot })));
      if (added) select("clip", added.key);
    },
    [
      clip.clips,
      clip.state,
      clipAddAllowed,
      guidedStoryV2,
      history,
      notify, readOnly,
      select,
      slotLayout.totalDurationS,
      slots,
    ],
  );

  const patchSelectedClipTiming = useCallback(
    (patch: { inS?: number; outS?: number; durationS?: number }) => {
      if (!selectedClip || readOnly || !clipCan("trim", true)) return;
      const current = selectedClip.slot;
      const currentDuration = selectedClip.durationS;
      const next = applyManualClipTimingPatch({
        inS: current.inS,
        durationS: currentDuration,
        patch,
        sourceDurationS: selectedClip.sourceDurationS,
      });
      if (
        current.inS === next.inS &&
        current.durationS === next.durationS &&
        current.durationBeats === next.durationBeats
      ) {
        return;
      }
      history.record();
      previewClipTiming(current.key, next);
    },
    [clipCan, history, previewClipTiming, readOnly, selectedClip],
  );

  const patchSelectedClipLook = useCallback(
    (preset: LookPreset) => {
      if (!selectedClip || readOnly || !clipLooksAllowed) return;
      if ((selectedClip.slot.lookPreset ?? "none") === preset) return;
      history.record();
      setLocalSlots((current) =>
        (current ?? slots).map((slot) =>
          slot.key === selectedClip.slot.key
            ? {
                ...slot,
                lookPreset: preset,
                lookAdjustments: defaultLookAdjustments(preset),
              }
            : slot,
        ),
      );
    },
    [clipLooksAllowed, history, readOnly, selectedClip, slots],
  );

  const patchSelectedClipTransition = useCallback(
    (transition: EditorTransition, durationS?: number) => {
      if (!selectedClip || readOnly || !clipTransitionsAllowed) return;
      const nextDuration = transition === "cut" ? null : Math.max(0.1, Math.min(0.3, durationS ?? 0.3));
      if (
        (selectedClip.slot.transitionAfter ?? "cut") === transition &&
        (selectedClip.slot.transitionDurationS ?? null) === nextDuration
      ) return;
      history.record();
      setLocalSlots((current) =>
        (current ?? slots).map((slot) =>
          slot.key === selectedClip.slot.key
            ? { ...slot, transitionAfter: transition, transitionDurationS: nextDuration }
            : slot,
        ),
      );
    },
    [clipTransitionsAllowed, history, readOnly, selectedClip, slots],
  );

  const moveSelectedClip = useCallback(
    (direction: -1 | 1) => {
      if (!selectedClip || readOnly || !clipReorderAllowed) return;
      const index = slots.findIndex((slot) => slot.key === selectedClip.slot.key);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= slots.length) return;
      history.record();
      const next = [...slots];
      const [moved] = next.splice(index, 1);
      next.splice(target, 0, moved);
      setLocalSlots(next);
    },
    [clipReorderAllowed, history, readOnly, selectedClip, slots],
  );

  const patchSelectedClipLookAdjustments = useCallback(
    (patch: Partial<LookAdjustments>) => {
      if (!selectedClip || readOnly || !clipLooksAllowed) return;
      const preset = selectedClip.slot.lookPreset ?? "none";
      const current = resolveLookAdjustments(preset, selectedClip.slot.lookAdjustments);
      if (!current) return;
      const next = { ...current, ...patch };
      if (lookAdjustmentsEqual(current, next)) return;
      setLocalSlots((local) =>
        (local ?? slots).map((slot) =>
          slot.key === selectedClip.slot.key
            ? { ...slot, lookAdjustments: next }
            : slot,
        ),
      );
    },
    [clipLooksAllowed, readOnly, selectedClip, slots],
  );

  const recordSelectedClipLookAdjustment = useCallback(() => {
    if (!selectedClip || readOnly || !clipLooksAllowed) return;
    history.record();
  }, [clipLooksAllowed, history, readOnly, selectedClip]);

  const previewSelectedClipTiming = useCallback(
    (patch: { inS: number; durationS: number }) => {
      if (!selectedClip || readOnly || !clipCan("trim", true)) return;
      previewClipTiming(selectedClip.slot.key, {
        inS: patch.inS,
        durationS: patch.durationS,
        durationBeats: null,
      });
      const projectedEntry = virtualPreview.timeline.entries.find(
        (entry) => entry.kind === "clip" && entry.slotKey === selectedClip.slot.key,
      );
      const slotIndex = slots.findIndex((s) => s.key === selectedClip.slot.key);
      const startS = projectedEntry?.startS ?? slotLayout.windows[slotIndex]?.startS;
      if (startS != null) {
        const boundaryS =
          Math.abs(patch.inS - selectedClip.slot.inS) > 1e-6
            ? startS
            : startS + patch.durationS;
        seekPlaybackTo(boundaryS);
      }
    },
    [
      clipCan,
      previewClipTiming,
      readOnly,
      seekPlaybackTo,
      selectedClip,
      slotLayout.windows,
      slots,
      virtualPreview.timeline.entries,
    ],
  );

  const seekPreviewToOutput = useCallback(
    (seconds: number) => {
      seekPlaybackTo(seconds);
    },
    [seekPlaybackTo],
  );

  const previewSfxTiming = useCallback(
    (id: string, patch: { at_s: number; end_s?: number | null }) => {
      if (readOnly) return;
      setLocalSfx((cur) =>
        cur.map((s) => {
          if (s.id !== id) return s;
          const trimStart = s.trim_start_s ?? 0;
          const sourceEnd = s.duration_s ?? s.trim_end_s ?? null;
          const next: SoundEffectPlacement = { ...s, source: "user", at_s: patch.at_s };
          if (patch.end_s != null && sourceEnd != null) {
            next.trim_end_s = Math.max(trimStart + 0.1, patch.end_s - patch.at_s + trimStart);
          }
          return next;
        }),
      );
      setSfxDirty(true);
    },
    [readOnly],
  );

  const addSfxFromGlossary = useCallback(
    (effect: SoundEffectSummary) => {
      if (readOnly || !sfxAllowed) return;
      history.record();
      const placement: SoundEffectPlacement = {
        id: crypto.randomUUID(),
        sound_effect_id: effect.id,
        src_gcs_path: "",
        source: "user",
        at_s: Math.min(
          Math.max(0, outputToBaseTimeRef.current(currentTime)),
          Math.max(0, duration - 0.1),
        ),
        gain: 1,
        duration_s: effect.duration_s ?? null,
        label: effect.name,
      };
      setLocalSfx((cur) => [...cur, placement]);
      if (effect.preview_audio_url) {
        setLocalSfxAudioUrls((cur) => ({
          ...cur,
          [placement.id]: effect.preview_audio_url as string,
        }));
      }
      setSfxDirty(true);
      select("sfx", placement.id);
      setInspectorTab("basic");
    },
    [currentTime, duration, history, readOnly, select, sfxAllowed],
  );

  const patchSfx = useCallback(
    (id: string, patch: Partial<SoundEffectPlacement>) => {
      if (readOnly || !sfxAllowed) return;
      history.record();
      setLocalSfx((cur) =>
        cur.map((s) => (s.id === id ? { ...s, ...patch, source: "user" } : s)),
      );
      setSfxDirty(true);
    },
    [history, readOnly, sfxAllowed],
  );

  const removeSfx = useCallback(
    (id: string) => {
      if (readOnly || !sfxAllowed) return;
      history.record();
      setLocalSfx((cur) => cur.filter((s) => s.id !== id));
      setSfxDirty(true);
      clear();
    },
    [clear, history, readOnly, sfxAllowed],
  );

  const previewOverlayTiming = useCallback(
    (id: string, patch: Pick<MediaOverlay, "start_s" | "end_s">) => {
      if (readOnly || !overlaysAllowed) return;
      setLocalOverlays((cur) => cur.map((o) => (o.id === id ? { ...o, ...patch } : o)));
      setOverlaysDirty(true);
    },
    [overlaysAllowed, readOnly],
  );

  const previewCameraTiming = useCallback(
    (id: string, patch: Pick<CameraEffect, "start_s" | "end_s">) => {
      if (readOnly || capabilities?.camera_effects === false) return;
      setLocalCameraEffects((effects) =>
        effects.map((effect) =>
          effect.id === id
            ? normalizeCameraEffect({ ...effect, ...patch, source: "user" })
            : effect,
        ),
      );
      setCameraEffectsDirty(true);
    },
    [capabilities?.camera_effects, readOnly],
  );

  const patchCameraEffect = useCallback(
    (id: string, patch: Partial<CameraEffect>) => {
      if (readOnly || capabilities?.camera_effects === false) return;
      history.record();
      setLocalCameraEffects((effects) =>
        effects.map((effect) =>
          effect.id === id
            ? normalizeCameraEffect({ ...effect, ...patch, source: "user" })
            : effect,
        ),
      );
      setCameraEffectsDirty(true);
    },
    [capabilities?.camera_effects, history, readOnly],
  );

  const deleteCameraEffect = useCallback(
    (id: string) => {
      if (readOnly || capabilities?.camera_effects === false) return;
      history.record();
      setLocalCameraEffects((effects) => effects.filter((effect) => effect.id !== id));
      setCameraEffectsDirty(true);
      clear();
    },
    [capabilities?.camera_effects, clear, history, readOnly],
  );

  const previewVisualTiming = useCallback(
    (id: string, patch: Pick<VisualBlock, "start_s" | "end_s">) => {
      if (readOnly || !visualBlocksAllowed) return;
      setLocalVisualBlocks((blocks) =>
        blocks.map((block) =>
          block.id === id
            ? patchVisualBlockConcreteTiming(block, {
                ...patch,
                timing_mode: "manual",
              } as Partial<VisualBlock>)
            : block,
        ),
      );
      setVisualBlocksDirty(true);
      const current = localVisualBlocks.find((block) => block.id === id);
      if (current?.kind === "text_card") {
        state.bars
          .filter((bar) => bar.visual_block_id === id)
          .forEach((bar) =>
            dispatch({
              type: "PATCH_BAR",
              id: bar.id,
              patch: retimeLinkedTextBar(
                bar,
                current,
                patch.start_s,
                patch.end_s,
              ),
            }),
          );
        setTextDirty(true);
      }
    },
    [localVisualBlocks, readOnly, state.bars, visualBlocksAllowed],
  );

  const previewMotionTiming = useCallback(
    (
      id: string,
      patch: { start_s: number; end_s: number },
      origin: EditorMotionBar,
    ) => {
      if (readOnly || !motionScenesAllowed) return;
      const target = origin.sourceScene;
      if (!target || (target.preset_id === "evolving_type" && !evolvingTypeExposureEnabled)) {
        return;
      }
      setLocalMotionScenes((scenes) => {
        const durationFrames = motionDurationFrames();
        const requestedStart = Math.max(0, Math.round(patch.start_s * MOTION_FPS));
        const requestedEnd = Math.max(
          requestedStart + 1,
          Math.round(patch.end_s * MOTION_FPS),
        );
        const candidate = scenes.map((scene) => {
          if (scene.id !== id) return scene;
          if (target.preset_id === "route_trace") {
            return {
              ...target,
              start_frame: requestedStart,
              end_frame_exclusive: requestedEnd,
            } as MotionPresetInstance;
          }
          return retimeCreatorBlockManualSpan(
            target,
            requestedStart,
            requestedEnd,
            durationFrames,
          ) as MotionPresetInstance;
        });
        return validateMotionInstances(candidate, durationFrames).ok ? candidate : scenes;
      });
      setMotionScenesDirty(true);
    },
    [evolvingTypeExposureEnabled, motionDurationFrames, motionScenesAllowed, readOnly],
  );

  const previewOverlayPatch = useCallback(
    (id: string, patch: Partial<MediaOverlay>) => {
      if (readOnly || !overlaysAllowed) return;
      setLocalOverlays((cur) => cur.map((o) => (o.id === id ? { ...o, ...patch } : o)));
      setOverlaysDirty(true);
    },
    [overlaysAllowed, readOnly],
  );

  const patchOverlay = useCallback(
    (id: string, patch: Partial<MediaOverlay>, options: { record?: boolean } = {}) => {
      if (readOnly || !overlaysAllowed) return;
      const legacy = localOverlays.find((overlay) => overlay.id === id);
      if (legacy && visualBlocksAllowed) {
        const asset = poolAssets.find((candidate) => candidate.gcs_path === legacy.src_gcs_path);
        if (legacy.kind === "video" && !(legacy.clip_duration_s ?? asset?.duration_s)) return;
        if (options.record !== false) history.record();
        const converted = normalizeMediaVisualBlock(mediaOverlayToVisualBlock(legacy, asset));
        const next = {
          ...converted,
          ...mediaOverlayPatchToVisualPatch(patch),
        } as MediaVisualBlock;
        setLocalOverlays((overlays) => overlays.filter((overlay) => overlay.id !== id));
        setLocalVisualBlocks((blocks) => [...blocks, next]);
        setOverlaysDirty(true);
        setVisualBlocksDirty(true);
        setLocalVisualPreviewUrls((current) => ({
          ...current,
          [id]: localOverlayPreviewUrls[id] ?? legacy.preview_url ?? "",
        }));
        select("visual", id);
        return;
      }
      const visual = localVisualBlocks.find((block) => block.id === id && block.kind === "media");
      if (visual) {
        if (options.record !== false) history.record();
        setLocalVisualBlocks((blocks) =>
          blocks.map((block) => (block.id === id ? normalizeMediaVisualBlock({ ...block, ...patch } as MediaVisualBlock) : block)),
        );
        setVisualBlocksDirty(true);
        return;
      }
      if (options.record !== false) history.record();
      setLocalOverlays((cur) => cur.map((o) => (o.id === id ? { ...o, ...patch } : o)));
      setOverlaysDirty(true);
    },
    [history, localOverlayPreviewUrls, localOverlays, localVisualBlocks, overlaysAllowed, poolAssets, readOnly, select, visualBlocksAllowed],
  );

  const removeOverlay = useCallback(
    (id: string) => {
      if (readOnly || !overlaysAllowed) return;
      history.record();
      const removed = removeOverlayEffectGroup(
        {
          overlays: localOverlays,
          soundEffects: localSfx,
          cameraEffects: localCameraEffects,
        },
        id,
      );
      setLocalOverlays(removed.overlays);
      if (removed.soundEffects !== localSfx) {
        setLocalSfx(removed.soundEffects);
        setSfxDirty(true);
      }
      if (removed.cameraEffects !== localCameraEffects) {
        setLocalCameraEffects(removed.cameraEffects);
        setCameraEffectsDirty(true);
      }
      setOverlaysDirty(true);
      clear();
    },
    [
      overlaysAllowed,
      clear,
      history,
      localCameraEffects,
      localOverlays,
      localSfx,
      readOnly,
    ],
  );

  const patchMixLevel = useCallback(
    (level: number) => {
      if (readOnly || !musicCan("level", capabilities?.mix !== false)) return;
      const next = Math.max(0, Math.min(1, level));
      history.record("mix");
      setMixLevel(next);
      setSoundMuted(next === 0);
      setMixDirty(true);
    },
    [capabilities?.mix, history, musicCan, readOnly],
  );

  const handleOverlayUpload = useCallback(
    async (
      files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
    ) => {
      if (
        readOnly ||
        files.length === 0 ||
        (!visualBlocksAllowed && !overlaysAllowed)
      ) {
        return;
      }
      setOverlayUploading(true);
      if (visualBlocksAllowed) {
        const accepted = poolUploader.addFiles(files.map((entry) => entry.file));
        setOverlayUploading(false);
        if (accepted > 0) {
          setActiveTool("visuals");
          notify("Media added to Visuals. Choose Full screen, Overlay, or Sequence when it is ready.");
        }
        return;
      }
      const start = Math.min(
        Math.max(0, outputToBaseTimeRef.current(currentTime)),
        Math.max(0, duration - 0.3),
      );
      const positionCycle: {
        position: "top" | "center" | "bottom";
        x_frac: number;
        y_frac: number;
      }[] = [
        { position: "center", x_frac: 0.5, y_frac: 0.5 },
        { position: "top", x_frac: 0.5, y_frac: 0.18 },
        { position: "bottom", x_frac: 0.5, y_frac: 0.82 },
      ];
      const drafts = files.map((entry, index) => {
        const slot = positionCycle[(localOverlays.length + index) % positionCycle.length];
        return {
          id: crypto.randomUUID(),
          kind: entry.content_type.startsWith("video/") ? "video" : "image",
          position: slot.position,
          x_frac: slot.x_frac,
          y_frac: slot.y_frac,
          start_s: start,
          end_s: Math.min(duration || start + 5, start + 5),
          z: localOverlays.length + index,
        } as const;
      });
      if ((capabilities?.overlay_upload_mode ?? "legacy") === "legacy") {
        try {
          const confirmed = await uploadMediaOverlayFiles(itemId, files);
          const cards: MediaOverlay[] = confirmed.map((result, index) => ({
            ...drafts[index],
            src_gcs_path: result.gcs_path,
            preview_gcs_path: result.preview_gcs_path ?? null,
            preview_url: result.preview_url ?? null,
            scale: 0.35,
          }));
          history.record();
          setLocalOverlays((current) => [...current, ...cards]);
          cards.forEach((card) => select("overlay", card.id));
          setOverlaysDirty(true);
        } catch (err) {
          notify("We couldn't upload that overlay. Try again.");
        } finally {
          setOverlayUploading(false);
        }
        return;
      }
      const accepted = poolUploader.addFiles(
        files.map((entry) => entry.file),
        {
          intent: "overlay",
          context: (_file, index) => drafts[index],
        },
      );
      if (accepted === 0) setOverlayUploading(false);
    },
    [
      capabilities?.overlay_upload_mode,
      overlaysAllowed,
      currentTime,
      history,
      itemId,
      localOverlays.length,
      duration,
      poolUploader,
      readOnly,
      select,
      notify,
      visualBlocksAllowed,
    ],
  );

  // Accept an AI overlay suggestion (Overlays drawer): the envelope's card
  // (and sound, when present) joins the working state as ONE undoable command
  // — same record-then-mutate shape as handleOverlayUpload/addSfxFromGlossary.
  // Persistence rides the normal Save (editor-commit accepted_suggestion_ids).
  const handleAcceptSuggestion = useCallback(
    (suggestion: OverlaySuggestion) => {
      if (readOnly || !overlaysAllowed) return;
      history.record();
      const effectGroupId = suggestion.overlay.effect_group_id ?? suggestion.id;
      setLocalOverlays((cur) => [
        ...cur,
        {
          ...suggestion.overlay,
          source: suggestion.overlay.source ?? "overlay_suggestion",
          effect_group_id: effectGroupId,
        },
      ]);
      setOverlaysDirty(true);
      // SFX child rides only when the sfx section can actually commit —
      // staging it with sound effects disabled would 404 the whole Save.
      if (suggestion.sfx && sfxAllowed) {
        const sfx = {
          ...suggestion.sfx,
          source: suggestion.sfx.source ?? "overlay_suggestion",
          effect_group_id: suggestion.sfx.effect_group_id ?? effectGroupId,
        };
        setLocalSfx((cur) => [...cur, sfx]);
        setSfxDirty(true);
      }
      setAcceptedSuggestions((cur) =>
        cur.some((a) => a.id === suggestion.id)
          ? cur
          : [...cur, { id: suggestion.id, overlayId: suggestion.overlay.id }],
      );
      select("overlay", suggestion.overlay.id);
      setInspectorTab("basic");
    },
    [history, overlaysAllowed, readOnly, select, sfxAllowed],
  );

  const recordTimelineDrag = useCallback(() => {
    if (readOnly) return;
    history.record();
  }, [history, readOnly]);

  const focusContent = useCallback(() => {
    // Double-click contract: focus the inspector textarea with select-all.
    // Deferred a frame so the inspector has populated for a fresh selection.
    requestAnimationFrame(() => {
      contentRef.current?.focus({ preventScroll: true });
      contentRef.current?.select();
    });
  }, []);

  const addTextAtPlayhead = useCallback(
    (preset: TextPreset = DEFAULT_TEXT_PRESET) => {
      if (readOnly) return null;
      if (textElementsLocked) {
        // OV-1: the rail disables the Text/Styles buttons, but this callback
        // is also reachable via preset picks — same gate, honest toast. The
        // copy is text-specific (never the whole-shell "can't be edited").
        notify(textElementsLockedCopy(capabilities));
        return null;
      }
      history.record();
      setTextDirty(true);
      const bar = newTextBar({
        id: crypto.randomUUID(),
        text: NEW_TEXT_CONTENT,
        timing: textTimingAtPlayhead({
          currentTime: outputToBaseTimeRef.current(currentTime),
          previewDuration: duration,
        }),
        preset,
      });
      const authoredBar =
        TEXT_MOTION_V2_UI_ENABLED && preset !== DEFAULT_TEXT_PRESET && preset.fields.effect
          ? { ...bar, ...motionPatchForEffect(bar, preset.fields.effect, duration) }
          : bar;
      dispatch({ type: "ADD_TEXT", bar: authoredBar });
      selectText(bar.id);
      return bar.id;
    },
    [
      currentTime,
      duration,
      selectText,
      readOnly,
      textElementsLocked,
      capabilities,
      history,
      notify,
    ],
  );

  const addPocketTextAtPlayhead = useCallback(() => {
    const id = addTextAtPlayhead();
    if (!id) return;
    setActiveTool(null);
    dispatchPocket({ type: "CLOSE_SHEET" });
  }, [addTextAtPlayhead]);

  const splitAndPlaceText = useCallback(
    (text: string): boolean => {
      if (readOnly) return false;
      if (textElementsLocked) {
        notify(textElementsLockedCopy(capabilities));
        return false;
      }
      const draft = text.trim();
      if (!draft) return false;
      const existingElementCount = barsToTextElements(state.bars, originalsRef.current, {
        includeLyrics: lyricsOptionalActive,
      }).length;
      const remainingElementCount = Math.max(0, TEXT_ELEMENTS_API_MAX - existingElementCount);
      const sequence = buildTimedTextSequence(
        draft,
        outputToBaseTimeRef.current(currentTime),
        duration,
        0.5,
        remainingElementCount,
      );
      if (sequence === null) {
        notify(
          `This edit has room for ${remainingElementCount} more text beat${remainingElementCount === 1 ? "" : "s"}.`,
        );
        return false;
      }
      if (sequence.length === 0) return false;
      const bars = sequence.map((item, index) => ({
        ...newTextBar({
          id: crypto.randomUUID(),
          text: item.text,
          timing: item,
          preset: COMPOSITION_TEXT_PRESET,
        }),
        role: "generative_sequence" as const,
        effect: "static",
        y_frac: COMPOSITION_Y_FRACS[index % COMPOSITION_Y_FRACS.length],
      }));
      history.record();
      setTextDirty(true);
      bars.forEach((bar) => dispatch({ type: "ADD_TEXT", bar }));
      selectText(bars[0].id);
      setInspectorTab("basic");
      return true;
    },
    [
      capabilities,
      currentTime,
      history,
      duration,
      readOnly,
      selectText,
      state.bars,
      textElementsLocked,
      lyricsOptionalActive,
      notify,
    ],
  );

  // Restyle ALL text bars with a style set — ONE undoable command with instant
  // canvas update (plan §2 Styles v1, task wiring). record() once, then patch
  // every bar (each PATCH_BAR is a reducer dispatch; the single record collapses
  // them into one undo step).
  const restyleAll = useCallback(
    (styleSet: GenerativeStyleSet) => {
      if (readOnly) return;
      const targetBars = visibleTextBars.filter((bar) => !isCaptionBar(bar));
      if (targetBars.length === 0) {
        notify("Add text first, then apply a style.");
        return;
      }
      const basePatch: Partial<Omit<TextElementBar, "id" | "role">> = {
        font_family: styleSet.font_family ?? styleSet.intro?.font_family ?? undefined,
        color: styleSet.text_color ?? styleSet.intro?.text_color ?? undefined,
        highlight_color:
          styleSet.highlight_color ?? styleSet.intro?.highlight_color ?? undefined,
        stroke_width: styleSet.intro?.stroke_width ?? undefined,
      };
      const nextEffect = styleSet.effect ?? styleSet.intro?.effect ?? undefined;
      history.record();
      if (lyricsOptionalActive || targetBars.some((bar) => !isLyricBar(bar))) {
        setTextDirty(true);
      }
      targetBars.forEach((b) =>
        dispatch({
          type: "PATCH_BAR",
          id: b.id,
          patch: {
            ...basePatch,
            ...(nextEffect
              ? TEXT_MOTION_V2_UI_ENABLED && !isLyricBar(b)
                ? motionPatchForEffect(b, nextEffect, duration)
                : { effect: nextEffect }
              : {}),
          },
        }),
      );
      setAppliedStyleSetId(styleSet.id);
    },
    [duration, readOnly, visibleTextBars, lyricsOptionalActive, history, notify],
  );

  // Legacy lyrics-variant restyle for flag-off clients: route through the
  // existing style endpoint instead of local lyric-bar edits.
  const restyleLyrics = useCallback(
    async (styleSet: GenerativeStyleSet) => {
      if (readOnly || !variant || saveState === "saving") return;
      setSaveState("saving");
      setSaveMessage(null);
      try {
        await changePlanItemStyle(itemId, variant.variant_id, styleSet.id);
        setAppliedStyleSetId(styleSet.id);
        setSaveState("idle");
        router.push(`/plan/items/${itemId}`);
      } catch (err) {
        setSaveState("error");
        setSaveMessage("We couldn't apply that style. Try again.");
      }
    },
    [readOnly, variant, saveState, itemId, router],
  );

  // Single entry point both StylesDrawer instances bind to — branches per
  // variant type so callers don't need to know about the lyrics special case.
  // Elements-model variants always take the normal bar-patch route, even if
  // text_mode happens to read "lyrics" — their lyric_line bars are ordinary
  // text_elements, never the legacy whole-style-set route.
  const onRestyleAll =
    isLyrics && !lyricBarsAvailable && !lyricsOptionalActive ? restyleLyrics : restyleAll;
  const textStyleHandler = textElementsLocked && !isLyrics ? undefined : onRestyleAll;

  const pickPreset = useCallback(
    (preset: TextPreset) => {
      if (selectedBar) {
        // Apply to the selected element.
        const nextEffect = preset.fields.effect ?? undefined;
        patchBar(selectedBar.id, {
          font_family: preset.fields.font_family ?? undefined,
          color: preset.fields.color ?? undefined,
          highlight_color: preset.fields.highlight_color ?? undefined,
          stroke_width: preset.fields.stroke_width ?? 0,
          ...(nextEffect
            ? TEXT_MOTION_V2_UI_ENABLED && !isLyricBar(selectedBar)
              ? motionPatchForEffect(selectedBar, nextEffect, duration)
              : { effect: nextEffect }
            : {}),
        });
      } else {
        // No selection → create a text element at the playhead with this
        // preset and select it (D6).
        addTextAtPlayhead(preset);
      }
    },
    [duration, selectedBar, patchBar, addTextAtPlayhead],
  );

  const nextVisualBlockWindow = useCallback(
    (requestedDuration: number) => {
      const maxDuration = Math.max(0.75, duration || 60);
      let start = Math.max(
        0,
        Math.min(outputToBaseTimeRef.current(currentTime), Math.max(0, maxDuration - 0.75)),
      );
      const ordered = [...localVisualBlocks].sort((a, b) => a.start_s - b.start_s);
      for (const block of ordered) {
        if (start + requestedDuration <= block.start_s) break;
        if (start < block.end_s && start + requestedDuration > block.start_s) {
          start = block.end_s;
        }
      }
      const end = Math.min(maxDuration, start + requestedDuration);
      return { start, end };
    },
    [currentTime, duration, localVisualBlocks],
  );

  const addTextCard = useCallback(
    (preset: "card" | "quote" | "statistic" | "transition") => {
      if (readOnly || !visualBlocksAllowed) return;
      const { start, end } = nextVisualBlockWindow(2.5);
      if (end - start < 0.75) {
        notify("There isn't enough open timeline space for a text card.");
        return;
      }
      const id = crypto.randomUUID();
      const labels = {
        card: "Add a key idea",
        quote: "“Add a quote”",
        statistic: "Add a statistic",
        transition: "New section",
      } as const;
      const block: VisualBlock = {
        version: 1,
        id,
        kind: "text_card",
        start_s: start,
        end_s: end,
        timing_mode: "manual",
        origin: "user",
        transition_in: "cut",
        transition_out: "cut",
        audio_policy: { base: "continue", sfx: "continue" },
        style_preset_id: `nova-${preset}`,
        background: { type: "solid", color: preset === "statistic" ? "#172035" : "#26382F" },
      };
      const bar = {
        ...newTextBar({
          id: crypto.randomUUID(),
          text: labels[preset],
          timing: { start_s: start, end_s: end },
          preset: DEFAULT_TEXT_PRESET,
        }),
        visual_block_id: id,
        font_family: "PlayfairDisplay-Bold",
        color: "#FFFFFF",
        size_px: 72,
        y_frac: 0.5,
        max_width_frac: 0.82,
        effect: "fade-in",
      } satisfies TextElementBar;
      history.record();
      setLocalVisualBlocks((current) => [...current, block]);
      setVisualBlocksDirty(true);
      setTextDirty(true);
      dispatch({ type: "ADD_TEXT", bar });
      selectText(bar.id);
      setActiveTool("visuals");
      seekPlaybackTo(baseToOutputTimeRef.current(start));
    },
    [
      visualBlocksAllowed,
      history,
      nextVisualBlockWindow,
      readOnly,
      seekPlaybackTo,
      selectText,
      notify,
    ],
  );

  const addMontageBlock = useCallback(
    (assetIds: string[]) => {
      if (readOnly || !visualBlocksAllowed) return;
      const selectedAssets = assetIds
        .map((id) => poolAssets.find((asset) => asset.id === id))
        .filter((asset): asset is PoolAsset => !!asset && asset.status === "ready")
        .slice(0, 12);
      if (selectedAssets.length < 3) {
        notify("Choose at least three ready visuals for a montage.");
        return;
      }
      const { start, end } = nextVisualBlockWindow(3.0);
      if (end - start < 1.2) {
        notify("There isn't enough open timeline space for a montage.");
        return;
      }
      const perShot = (end - start) / selectedAssets.length;
      let offset = 0;
      const motions = ["zoom_in", "pan_right", "zoom_out", "pan_left"] as const;
      const block: VisualBlock = {
        version: 1,
        id: crypto.randomUUID(),
        kind: "montage",
        start_s: start,
        end_s: end,
        timing_mode: "auto",
        origin: "user",
        transition_in: "cut",
        transition_out: "cut",
        audio_policy: { base: "continue", sfx: "continue" },
        shots: selectedAssets.map((asset, index) => {
          const shotDuration =
            index === selectedAssets.length - 1 ? end - start - offset : perShot;
          const shot = {
            id: crypto.randomUUID(),
            asset_id: asset.id,
            src_gcs_path: asset.gcs_path,
            kind: asset.kind,
            start_offset_s: Number(offset.toFixed(6)),
            duration_s: Number(shotDuration.toFixed(6)),
            crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
            motion: motions[index % motions.length],
          };
          offset += shotDuration;
          return shot;
        }),
      };
      history.record();
      setLocalVisualBlocks((current) => [...current, block]);
      setVisualBlocksDirty(true);
      seekPlaybackTo(baseToOutputTimeRef.current(start));
    },
    [
      visualBlocksAllowed,
      history,
      nextVisualBlockWindow,
      poolAssets,
      readOnly,
      seekPlaybackTo,
      notify,
    ],
  );

  const addMediaVisualBlocks = useCallback(
    (assetIds: string[], displayMode: "fullscreen" | "overlay", asSequence = false) => {
      if (readOnly || !visualBlocksAllowed) return [];
      const selected = selection?.kind === "visual" ? localVisualBlocks.find((block) => block.id === selection.id) : null;
      // A sequence can start a new media lane at the playhead. When a media
      // layer is selected, preserve the more precise "place after selected"
      // behavior. Requiring a pre-existing layer made the first photo
      // sequence impossible even though the drawer exposed the action.
      let cursorEnd = asSequence
        ? selected?.end_s ?? Math.max(0, outputToBaseTimeRef.current(currentTime))
        : null;
      const additions: MediaVisualBlock[] = [];
      const placedAssetIds: string[] = [];
      let sequenceOutOfSpace = false;
      for (const assetId of assetIds) {
        const asset = poolAssets.find((candidate) => candidate.id === assetId && candidate.status === "ready");
        if (!asset || (asset.kind === "video" && !(asset.duration_s && asset.duration_s > 0))) continue;
        const desiredDuration = asset.kind === "video"
          ? Math.min(2, asset.duration_s ?? 0)
          : 2;
        const adjacent = cursorEnd == null
          ? null
          : placeAfterSelected({
              selected: { end_s: cursorEnd },
              durationS: desiredDuration,
              videoDurationS: duration,
            });
        if (asSequence && adjacent == null) {
          sequenceOutOfSpace = true;
          break;
        }
        const openWindow = !asSequence && displayMode === "fullscreen" && adjacent == null
          ? nextVisualBlockWindow(desiredDuration)
          : null;
        const start = adjacent?.start_s ?? (displayMode === "overlay"
          ? Math.min(
              Math.max(0, outputToBaseTimeRef.current(currentTime)),
              Math.max(0, duration - 0.1),
            )
          : openWindow?.start ?? 0);
        const end = adjacent?.end_s ?? (displayMode === "overlay"
          ? Math.min(duration || start + desiredDuration, start + desiredDuration)
          : openWindow?.end ?? start);
        if (end - start < 0.1) break;
        additions.push({
          version: 1,
          id: crypto.randomUUID(),
          start_s: start,
          end_s: end,
          timing_mode: "manual",
          origin: "user",
          transition_in: "cut",
          transition_out: "cut",
          audio_policy: { base: "continue", sfx: "continue" },
          kind: "media",
          asset_id: asset.id,
          src_gcs_path: asset.gcs_path,
          media_kind: asset.kind,
          source_duration_s: asset.kind === "video" ? asset.duration_s : null,
          trim_start_s: asset.kind === "video" ? 0 : null,
          trim_end_s: asset.kind === "video" ? asset.duration_s : null,
          display_mode: displayMode,
          transform: { fit_mode: "contain", focal_x: 0.5, focal_y: 0.5, zoom: 1 },
          x_frac: 0.5,
          y_frac: 0.5,
          scale: 0.35,
          z: Math.max(-1, ...localVisualBlocks.filter((block) => block.kind === "media").map((block) => block.z)) + 1 + additions.length,
        });
        placedAssetIds.push(asset.id);
        cursorEnd = asSequence ? end : null;
      }
      if (!additions.length) {
        if (asSequence) {
          notify(
            sequenceOutOfSpace
              ? "There isn't enough timeline space for this sequence."
              : "The selected media isn't ready to place yet.",
          );
        }
        return [];
      }
      history.record();
      setLocalVisualBlocks((blocks) => [...blocks, ...additions]);
      setVisualBlocksDirty(true);
      setActiveTool("visuals");
      selectElement("visual", additions[0].id);
      if (asSequence && additions.length < assetIds.length) {
        notify(
          sequenceOutOfSpace
            ? `Placed ${additions.length} of ${assetIds.length}. There isn't enough timeline space for the rest.`
            : `Placed ${additions.length} of ${assetIds.length}. The rest isn't ready yet.`,
        );
      }
      return placedAssetIds;
    },
    [currentTime, duration, history, localVisualBlocks, nextVisualBlockWindow, notify, poolAssets, readOnly, selectElement, selection, visualBlocksAllowed],
  );

  const addVisualBlockText = useCallback(
    (blockId: string) => {
      if (readOnly || textElementsLocked) return;
      const block = localVisualBlocks.find(
        (candidate) => candidate.id === blockId && candidate.kind === "text_card",
      );
      if (!block) return;
      const existingCount = state.bars.filter(
        (bar) => bar.visual_block_id === blockId,
      ).length;
      const bar = {
        ...newTextBar({
          id: crypto.randomUUID(),
          text: existingCount === 0 ? "Add a key idea" : "Add supporting text",
          timing: { start_s: block.start_s, end_s: block.end_s },
          preset: DEFAULT_TEXT_PRESET,
        }),
        visual_block_id: blockId,
        color: "#FFFFFF",
        y_frac: Math.min(0.75, 0.45 + existingCount * 0.12),
        max_width_frac: 0.82,
        effect: "fade-in",
      } satisfies TextElementBar;
      history.record();
      dispatch({ type: "ADD_TEXT", bar });
      setTextDirty(true);
      selectText(bar.id);
      seekPlaybackTo(baseToOutputTimeRef.current(block.start_s));
    },
    [
      history,
      localVisualBlocks,
      readOnly,
      seekPlaybackTo,
      selectText,
      state.bars,
      textElementsLocked,
    ],
  );

  const patchVisualBlock = useCallback(
    (id: string, patch: Partial<VisualBlock>, options: { record?: boolean } = {}) => {
      if (readOnly || !visualBlocksAllowed) return;
      const current = localVisualBlocks.find((block) => block.id === id);
      if (!current) return;
      if (options.record !== false) history.record();
      const next = patchVisualBlockConcreteTiming(current, patch);
      setLocalVisualBlocks((blocks) => blocks.map((block) => (block.id === id ? next : block)));
      setVisualBlocksDirty(true);
      if (current.kind === "text_card") {
        const nextStart = typeof patch.start_s === "number" ? patch.start_s : current.start_s;
        const nextEnd = typeof patch.end_s === "number" ? patch.end_s : current.end_s;
        state.bars
          .filter((bar) => bar.visual_block_id === id)
          .forEach((bar) =>
            dispatch({
              type: "PATCH_BAR",
              id: bar.id,
              patch: retimeLinkedTextBar(bar, current, nextStart, nextEnd),
            }),
          );
        setTextDirty(true);
      }
    },
    [history, localVisualBlocks, readOnly, state.bars, visualBlocksAllowed],
  );

  const previewVisualMediaBlock = useCallback(
    (id: string, patch: Partial<MediaVisualBlock>) => {
      if (readOnly || !visualBlocksAllowed) return;
      setLocalVisualBlocks((blocks) => blocks.map((block) =>
        block.id === id && block.kind === "media"
          ? normalizeMediaVisualBlock({ ...block, ...patch })
          : block,
      ));
      setVisualBlocksDirty(true);
    },
    [readOnly, visualBlocksAllowed],
  );
  const commitVisualMediaBlock = useCallback(
    (id: string, patch: Partial<MediaVisualBlock>) => patchVisualBlock(id, patch, { record: false }),
    [patchVisualBlock],
  );
  const recordVisualMediaBlock = useCallback(() => {
    if (!readOnly && visualBlocksAllowed) history.record();
  }, [history, readOnly, visualBlocksAllowed]);
  const reorderVisualMediaBlock = useCallback(
    (id: string, move: MediaLayerMove) => {
      if (readOnly || !visualBlocksAllowed) return;
      history.record();
      setLocalVisualBlocks((blocks) => reorderMediaVisualBlocks(blocks, id, move));
      setVisualBlocksDirty(true);
    },
    [history, readOnly, visualBlocksAllowed],
  );

  const deleteVisualBlock = useCallback(
    (id: string) => {
      if (readOnly || !visualBlocksAllowed) return;
      const source = localVisualBlocks.find((block) => block.id === id);
      history.record();
      setLocalVisualBlocks((blocks) => blocks.filter((block) => block.id !== id));
      if (source?.kind === "media") {
        const linkedEffects = removeGeneratedEffectGroup(
          localSfx,
          localCameraEffects,
          source.source,
          source.effect_group_id,
        );
        if (linkedEffects.soundEffects !== localSfx) {
          setLocalSfx(linkedEffects.soundEffects);
          setSfxDirty(true);
        }
        if (linkedEffects.cameraEffects !== localCameraEffects) {
          setLocalCameraEffects(linkedEffects.cameraEffects);
          setCameraEffectsDirty(true);
        }
      }
      setLocalVisualPreviewUrls((urls) => {
        const removed = removeMediaPreview(urls, id);
        revokeLocalObjectUrl(removed.orphanedUrl);
        return removed.previews;
      });
      setVisualBlocksDirty(true);
      state.bars
        .filter((bar) => bar.visual_block_id === id)
        .forEach((bar) => dispatch({ type: "DELETE_BAR", id: bar.id }));
      setTextDirty(true);
    },
    [
      history,
      localCameraEffects,
      localSfx,
      localVisualBlocks,
      readOnly,
      state.bars,
      visualBlocksAllowed,
    ],
  );

  const duplicateVisualBlock = useCallback(
    (id: string) => {
      if (readOnly || !visualBlocksAllowed) return;
      const source = localVisualBlocks.find((block) => block.id === id);
      if (!source) return;
      const durationS = source.end_s - source.start_s;
      const { start, end } = nextVisualBlockWindow(durationS);
      if (end - start < durationS - 1 / 30) {
        notify("There isn't enough open timeline space to duplicate this block.");
        return;
      }
      const newId = crypto.randomUUID();
      const copied: VisualBlock = source.kind === "montage"
        ? {
            ...source,
            id: newId,
            start_s: start,
            end_s: end,
            timing_mode: "manual",
            origin: "user",
            rationale: null,
            shots: source.shots.map((shot) => ({ ...shot, id: crypto.randomUUID() })),
          }
        : source.kind === "text_card"
        ? {
            ...source,
            id: newId,
            start_s: start,
            end_s: end,
            timing_mode: "manual",
            origin: "user",
            rationale: null,
            background:
              source.background.type === "asset"
                ? {
                    ...source.background,
                    shot: { ...source.background.shot, id: crypto.randomUUID() },
                  }
                : { ...source.background },
          }
        : duplicateMediaVisualBlock(source, newId, start, end);
      history.record();
      setLocalVisualBlocks((blocks) => [...blocks, copied]);
      if (source.kind === "media") {
        setLocalVisualPreviewUrls((previews) =>
          copyMediaPreviewForDuplicate(previews, source, newId),
        );
      }
      setVisualBlocksDirty(true);
      if (source.kind === "text_card") {
        const sourceDuration = Math.max(0.001, source.end_s - source.start_s);
        state.bars
          .filter((bar) => bar.visual_block_id === source.id)
          .forEach((bar) => {
            const relativeStart = (bar.start_s - source.start_s) / sourceDuration;
            const relativeEnd = (bar.end_s - source.start_s) / sourceDuration;
            dispatch({
              type: "ADD_TEXT",
              bar: {
                ...bar,
                id: crypto.randomUUID(),
                visual_block_id: newId,
                start_s: start + relativeStart * (end - start),
                end_s: start + relativeEnd * (end - start),
              },
            });
          });
        setTextDirty(true);
      }
      seekPlaybackTo(baseToOutputTimeRef.current(start));
    },
    [
      visualBlocksAllowed,
      history,
      localVisualBlocks,
      nextVisualBlockWindow,
      readOnly,
      seekPlaybackTo,
      state.bars,
      notify,
    ],
  );

  const retimeBlock = useCallback(
    (id: string) => {
      const block = localVisualBlocks.find((candidate) => candidate.id === id);
      if (!block || block.kind !== "montage" || !variant) return;
      void retimeVisualBlock(itemId, variant.variant_id, block)
        .then(({ visual_block }) => {
          history.record();
          setLocalVisualBlocks((blocks) =>
            blocks.map((candidate) => (candidate.id === id ? visual_block : candidate)),
          );
          setVisualBlocksDirty(true);
        })
        .catch(() => notify("Kria couldn’t retime that montage. Try again."));
    },
    [history, itemId, localVisualBlocks, notify, variant],
  );

  // Clip-split capability gate (plan §7): missing capabilities → allowed for
  // montage agent_text variants (song_text / original_text), disabled otherwise.
  const splitClipsAllowed = clipSplitAllowed;
  const captionsToolState = useMemo(() => captionToolState(variant), [variant]);
  const toolDisabledReasons = useMemo<Partial<Record<EditorTool, string>>>(
    () =>
      computeToolDisabledReasons({
        capabilities,
        readOnly,
        readOnlyReason,
        isLyrics,
        captions: captionsToolState,
        videoLooksAvailable: editWideLookPresets.length > 0,
      }),
    [
      capabilities,
      captionsToolState,
      editWideLookPresets.length,
      readOnly,
      readOnlyReason,
      isLyrics,
    ],
  );

  const buildCopilotDraftSnapshot = useCallback((context?: CopilotSnapshotContext) => {
    const openTools = (["text", "visuals", "sounds", "overlays", "styles"] as const).filter((tool) => {
      if (toolDisabledReasons[tool]) return false;
      if (tool === "sounds") return SOUND_EFFECTS_UI_ENABLED;
      if (tool === "overlays") return MEDIA_OVERLAYS_UI_ENABLED;
      if (tool === "visuals") return VISUAL_BLOCKS_UI_ENABLED;
      return true;
    });
    // Caption archetypes with a caption-free base can reburn cue text/timing
    // directly from this editor. Without the base, keep the copilot to
    // metadata-only caption controls so Save doesn't promise a 422ing edit.
    const captionCuesEditable =
      !!variant &&
      isCaptionArchetype(variant) &&
      !!variant.base_video_path &&
      captionMeta != null;
    const captionsPresent =
      captionMeta != null &&
      (captionCuesEditable
        ? visibleTextBars.some(isCaptionBar)
        : !!variant && isCaptionArchetype(variant) && (variant.caption_cues?.length ?? 0) > 0);
    const musicSwappable =
      !!variant?.music_track_id && capabilities?.swap_song !== false && !readOnly;
    const musicRemovable =
      musicCan("remove", capabilities?.swap_song !== false) &&
      !readOnly &&
      !musicRemoved &&
      !!effectiveMusicTrackId;
    const titleEditable = introControlsEditable;
    const mixAllowed = musicCan("level", capabilities?.mix !== false) && mixLevel !== undefined;
    const introText = variant?.intro_text?.trim() ?? "";
    const introWordCount = introText ? introText.split(/\s+/).filter(Boolean).length : 0;
    const sequenceCapable = variant?.sequence_synced === true || variant?.intro_mode === "sequence";
    const intro =
      variant?.text_mode === "agent_text" && (introText || sequenceCapable)
        ? {
            layout:
              sequenceCapable || variant.intro_layout === "cluster"
                ? "cluster" as const
                : "linear" as const,
            mode: variant.intro_mode ?? null,
            text: introText || null,
            word_count: introWordCount,
            sequence_capable: sequenceCapable,
            cluster_eligible: sequenceCapable || (introWordCount >= 3 && introWordCount <= 6),
            switch_blocked_reason: readOnly
              ? "read_only" as const
              : variant.render_status === "rendering"
                ? "rendering" as const
                : variant.text_elements_user_edited
                  ? "manual_text_edits" as const
                  : dirty
                    ? "unsaved_edits" as const
                    : null,
          }
        : undefined;
    const renderLayoutSwitchable = intro != null && intro.switch_blocked_reason === null;
    // Carousel-as-a-moment (Blossom carousel): mirrors capabilities.carousel /
    // carousel_reason 1:1 — same source `_editor_capabilities().carousel` the
    // CarouselPanel disabled-state reads (see carouselCapable/carouselReason
    // below). Independent of renderLayoutSwitchable — either one unlocks the
    // shared "render" op family; each op still gates on its own section.
    const carouselMomentAvailable = !readOnly && capabilities?.carousel === true;
    // Nova AI sandboxed effect language (PR6): its own eligibility, not tied
    // to renderLayoutSwitchable — the backend independently re-checks
    // ownership, editability, and the flag on the actual PATCH, so this only
    // needs to be a reasonable client-side gate for exposing the op family to
    // the model at all.
    const customEffectsAvailable = !readOnly && CUSTOM_EFFECTS_UI_ENABLED && variant != null;
    // Staged-first (Lane D): `carouselMoment` state already holds the
    // session's EFFECTIVE moment (staged once touched, else the persisted
    // `variant.carousel_moment` — see its declaration comment), so reading it
    // directly here — instead of `variant?.carousel_moment` — is what makes
    // the copilot see the user's own unsaved panel edits within the same
    // session, not just what's on disk.
    const rawCarouselMoment = carouselMoment;
    const carousel: CopilotCarouselSnapshot = {
      eligible: capabilities?.carousel === true,
      reason: capabilities?.carousel === true ? null : capabilities?.carousel_reason ?? null,
      current: rawCarouselMoment
        ? {
            position: rawCarouselMoment.position ?? null,
            mode: rawCarouselMoment.mode ?? null,
            effect: rawCarouselMoment.effect ?? null,
            // Prefer the flat contract field; fall back to the internal
            // `focus: [{card_index}]` render shape for moments persisted
            // before the backend started writing both (see
            // resolveCarouselFocusClipIndex).
            focus_clip_index: resolveCarouselFocusClipIndex(rawCarouselMoment),
            duration_s: rawCarouselMoment.duration_s ?? null,
            transition: rawCarouselMoment.transition ?? null,
          }
        : null,
      n_clips: carouselClips.length,
    };
    const allowedFamilies = allowedOpFamiliesFromCapabilities(capabilities, {
      sfxEnabled: SOUND_EFFECTS_UI_ENABLED,
      overlaysEnabled: MEDIA_OVERLAYS_UI_ENABLED,
      captionsPresent,
      musicSwappable,
      musicRemovable,
      mixAllowed,
      renderLayoutSwitchable,
      carouselMomentAvailable,
      customEffectsEnabled: customEffectsAvailable,
      cameraEffectsEnabled: capabilities?.camera_effects !== false,
      transitionsEnabled: EDIT_TRANSITIONS_UI_ENABLED,
      visualBlocksEnabled:
        VISUAL_BLOCKS_UI_ENABLED && visualBlocksAllowed,
      motionScenesEnabled:
        MOTION_SCENES_UI_ENABLED && motionScenesAllowed,
      titleEditable,
      openTools,
      readOnly,
    });
    return buildCopilotSnapshot(visibleTextBars, slots, clip.clips, capabilities, clip.state.grid, {
      sourcePool: clip.sourcePool,
      sfxEnabled: SOUND_EFFECTS_UI_ENABLED,
      overlaysEnabled: MEDIA_OVERLAYS_UI_ENABLED,
      captionsPresent,
      musicSwappable,
      musicRemovable,
      mixAllowed,
      titleEditable,
      openTools,
      // Slot-less variants (subtitled) have a 0 layout total — the real video
      // duration keeps every timing clamp from collapsing at_s values to 0.
      videoDurationS: timelineDuration,
      sfxPlacements: localSfx,
      sfxCatalog: sfxGlossaryEffects,
      // Speech marks describe the PERSISTED render's timeline — hide them while
      // local clip edits have shifted it (same staleness discipline the prompt
      // applies to beat marks). Saving refreshes the map.
      speechMap: clipDirty ? null : variant?.speech_map ?? null,
      sfxSuggestions: clipDirty ? null : variant?.pending_sfx_suggestions ?? null,
      overlayCards: localOverlays,
      poolAssets,
      pendingSuggestions: overlaySuggestions.rows,
      captionMeta: captionsPresent ? captionMeta : undefined,
      captionCuesEditable,
      captionTotalCues: captionCuesEditable ? undefined : variant?.caption_cues?.length ?? 0,
      musicState: {
        swappable: musicSwappable,
        removable: musicRemovable,
        currentTrackId: effectiveMusicTrackId,
        currentTrackTitle: effectiveMusicTitle,
        candidates: musicTracks,
      },
      mixLevel,
      intro,
      renderLayoutSwitchable,
      carousel,
      carouselMomentAvailable,
      customEffectsEnabled: customEffectsAvailable,
      title,
      cameraEffects: localCameraEffects,
      visualBlocks: localVisualBlocks,
      motionScenes: localMotionScenes,
      motionScenesEnabled:
        MOTION_SCENES_UI_ENABLED && motionScenesAllowed,
      guidedRevision:
        guidedStoryV2 && clip.revisionNumber != null && clip.baseGeneration
          ? {
              revision_number: clip.revisionNumber,
              base_generation: clip.baseGeneration,
              state_hash: clip.revisionHash,
            }
          : null,
      editDirectionAvailable:
        guidedStoryV2 && clip.revisionNumber != null && !!clip.baseGeneration,
      evolvingTypeEnabled: evolvingTypeExposureEnabled,
      readOnly: readOnly || allowedFamilies.length === 0,
      // PR1 (backend, parallel) wires an actual render-step source into this
      // context; recentEditHistory always comes from the hook's own message
      // log. Both are optional — buildCopilotSnapshot omits either section
      // entirely when its input is absent, never an empty "(none)" block.
      renderStepSummary: context?.renderStepSummary,
      recentEditHistory: context?.recentEditHistory,
      // PR7: sourced from lastAppliedTurnRef (updated in handleCopilotOps),
      // never from the copilot hook's own message log — avoids a circular
      // dependency (buildCopilotDraftSnapshot is what useEditCopilot's
      // buildSnapshot option IS, so it can't read useEditCopilot's own state).
      // Omitted entirely until a local turn has landed this session.
      historyState: lastAppliedTurnRef.current
        ? {
            can_undo_last_turn:
              history.canUndo && lastAppliedTurnRef.current.undoVersion === history.version,
            last_turn_summary: lastAppliedTurnRef.current.summary,
          }
        : undefined,
    });
  }, [
    capabilities,
    captionMeta,
    carouselClips,
    carouselMoment,
    clip.clips,
    clip.sourcePool,
    clip.baseGeneration,
    clip.revisionHash,
    clip.revisionNumber,
    clip.state.grid,
    clipDirty,
    effectiveMusicTitle,
    effectiveMusicTrackId,
    dirty,
    evolvingTypeExposureEnabled,
    guidedStoryV2,
    history.canUndo,
    history.version,
    introControlsEditable,
    localOverlays,
    localCameraEffects,
    localVisualBlocks,
    localMotionScenes,
    localSfx,
    mixLevel,
    musicCan,
    musicTracks,
    musicRemoved,
    motionScenesAllowed,
    overlaySuggestions.rows,
    poolAssets,
    timelineDuration,
    readOnly,
    sfxGlossaryEffects,
    slots,
    visibleTextBars,
    title,
    toolDisabledReasons,
    variant,
    visualBlocksAllowed,
  ]);

  // PR7 (repeat/undo ops): the most recent LOCAL (non-render) copilot turn
  // that mutated the draft — undoVersion pins it to a point in the undo
  // stack (canUndoLastTurn goes false once history moves past it, whether
  // via a manual panel edit, redo, or an explicit undo), ops feeds
  // repeat_last_edit's recursive re-apply, summary feeds the snapshot's
  // history_state for the model. A plain ref (not state) because it's read
  // only inside callbacks at turn-build/apply time, never rendered directly.
  const lastAppliedTurnRef = useRef<{
    undoVersion: number;
    ops: CopilotOp[];
    summary: string;
    receiptId?: string;
  } | null>(null);

  const buildCopilotApplyContext = useCallback(
    (snapshot: CopilotSnapshot): ApplyCopilotOpsContext => ({
        bars: state.bars,
        slots,
        clips: clip.clips,
        sourcePool: clip.sourcePool,
        snapshot,
        capabilities,
        grid: clip.state.grid,
        videoDurationS: timelineDuration,
        evolvingTypeEnabled: evolvingTypeExposureEnabled,
        sfx: localSfx,
        sfxCatalog: sfxGlossaryEffects,
        overlays: localOverlays,
        cameraEffects: localCameraEffects,
        visualBlocks: localVisualBlocks,
        motionScenes: localMotionScenes,
        carouselMoment,
        poolAssets,
        pendingSuggestions: overlaySuggestions.rows,
        musicTrackId: effectiveMusicTrackId,
        musicRemoved,
        mixLevel,
        title,
        captionMeta,
        makeTextBarId: () => crypto.randomUUID(),
        makeSlotKey: (slot) => `${slot.key}-split-${crypto.randomUUID()}`,
        makeSfxPlacementId: () => crypto.randomUUID(),
        makeOverlayId: () => crypto.randomUUID(),
        makeCameraEffectId: () => crypto.randomUUID(),
        makeMotionId: () => crypto.randomUUID(),
        // PR7: repeat_last_edit re-runs these against the CURRENT snapshot
        // (fingerprint validation does the real staleness gating); undo_last_edit
        // rejects unless the ref's undoVersion still matches the live stack —
        // the same staleness check CopilotDrawer's own Undo affordance uses.
        lastAppliedOps: lastAppliedTurnRef.current?.ops,
        canUndoLastTurn:
          history.canUndo && lastAppliedTurnRef.current?.undoVersion === history.version,
      }),
    [
      capabilities,
      captionMeta,
      carouselMoment,
      clip.clips,
      clip.sourcePool,
      clip.state.grid,
      effectiveMusicTrackId,
      evolvingTypeExposureEnabled,
      history.canUndo,
      history.version,
      localOverlays,
      localCameraEffects,
      localVisualBlocks,
      localMotionScenes,
      localSfx,
      mixLevel,
      musicRemoved,
      overlaySuggestions.rows,
      poolAssets,
      timelineDuration,
      sfxGlossaryEffects,
      slots,
      state.bars,
      title,
    ],
  );

  const applyCopilotDraftOps = useCallback(
    (ops: CopilotOp[], snapshot: CopilotSnapshot) =>
      applyCopilotOps(ops, buildCopilotApplyContext(snapshot)),
    [buildCopilotApplyContext],
  );

  const applyDirectorDraftOps = useCallback(
    (ops: CopilotOp[], snapshot: CopilotSnapshot) =>
      applyCopilotOpsAtomic(ops, buildCopilotApplyContext(snapshot)),
    [buildCopilotApplyContext],
  );

  const flashTimerRef = useRef<number | null>(null);
  const copilotRenderNavTimerRef = useRef<number | null>(null);
  const flashCopilotTargets = useCallback(
    (targets: {
      textIds?: string[];
      overlayIds?: string[];
      timelineIds?: string[];
    }) => {
      // One flash timer at a time: a prior turn's timer must not truncate a
      // newer flash mid-animation, and the timer is cleared on unmount (F7).
      if (flashTimerRef.current !== null) window.clearTimeout(flashTimerRef.current);
      setFlashTextIds(new Set(targets.textIds ?? []));
      setFlashOverlayIds(new Set(targets.overlayIds ?? []));
      setFlashTimelineIds(new Set(targets.timelineIds ?? []));
      flashTimerRef.current = window.setTimeout(() => {
        flashTimerRef.current = null;
        setFlashTextIds(new Set());
        setFlashOverlayIds(new Set());
        setFlashTimelineIds(new Set());
      }, 1600);
    },
    [],
  );
  // Chat steps feed (PR4): while a server-render turn (set_intro_layout) is
  // in flight in THIS mount, poll the same status route the item page's
  // ProgressTheater uses (`steps` field, PR1) so CopilotDrawer can show a
  // live compact NovaActivityFeed before navigate-away. Best-effort — the
  // fixed nav delay below is short, so this often shows 0-1 polls' worth of
  // steps before the drawer unmounts and the item page's own polling takes
  // over the narrative (feed continuity, not duplicated polling).
  const [copilotRenderTurnActive, setCopilotRenderTurnActive] = useState(false);
  const [copilotRenderSteps, setCopilotRenderSteps] = useState<NovaStep[] | null>(null);
  const copilotRenderPollTimerRef = useRef<number | null>(null);

  const stopCopilotRenderPoll = useCallback(() => {
    if (copilotRenderPollTimerRef.current !== null) {
      window.clearInterval(copilotRenderPollTimerRef.current);
      copilotRenderPollTimerRef.current = null;
    }
  }, []);

  const startCopilotRenderPoll = useCallback(() => {
    const jobId = item?.current_job_id;
    if (!jobId) return;
    stopCopilotRenderPoll();
    setCopilotRenderTurnActive(true);
    setCopilotRenderSteps(null);
    const poll = () => {
      getPlanItemJobStatus(jobId)
        .then((res) => setCopilotRenderSteps(res.steps ?? null))
        .catch(() => {
          // Best-effort — the item page's own poll (post-navigate) is the
          // authoritative source; a failed chat-side poll just shows the
          // disclosure copy a little longer.
        });
    };
    poll();
    copilotRenderPollTimerRef.current = window.setInterval(poll, POLL_INTERVAL_MS);
  }, [item?.current_job_id, stopCopilotRenderPoll]);

  useEffect(
    () => () => {
      if (flashTimerRef.current !== null) window.clearTimeout(flashTimerRef.current);
      if (copilotRenderNavTimerRef.current !== null) {
        window.clearTimeout(copilotRenderNavTimerRef.current);
      }
      stopCopilotRenderPoll();
    },
    [stopCopilotRenderPoll],
  );

  // Carousel-as-a-moment: staged like every other editor block (Lane C,
  // carousel-blocks train) — every panel control (and, as of Lane D, every
  // copilot set_carousel_moment op) patches `carouselMoment` immediately and
  // records an undo step; nothing renders until the next batched Save (see
  // handleSave's carouselMomentDirty/carouselMoment threading into
  // buildEditorCommitRequest). Split out from `stageCarouselMoment` below so
  // `handleCopilotOps` can apply the mutation WITHOUT a second
  // `history.record()` — the whole copilot bundle (text/overlay/carousel/...)
  // shares the single snapshot recorded at the top of that handler, same as
  // every other draft-result field it applies inline.
  const applyCarouselMoment = useCallback((config: CarouselMoment | null) => {
    setCarouselMoment(config);
    setCarouselMomentDirty(true);
  }, []);

  const handleCopilotOps = useCallback(
    (
      result: ApplyCopilotOpsResult,
      response?: EditCopilotTurnResponse,
    ): DirectorApplyPresentation => {
      if (result.renderRequest) {
        // set_intro_layout and apply_custom_effect (PR6) are the two ops that
        // produce a renderRequest — a discriminated union on `kind` (carousel-
        // as-a-moment is a staged draft mutation as of Lane D — see
        // result.nextCarouselMoment below, applied inline like every other
        // draft field, never through this branch). Both follow the exact same
        // navigate-back+poll flow: PATCH the dedicated variant endpoint, then
        // return to the item page after a short beat so the toast/receipt is
        // readable before the poller takes over.
        if (!readOnly && variant) {
          const request = result.renderRequest;
          const dispatch =
            request.kind === "set_intro_layout"
              ? editPlanItemVariant(itemId, variant.variant_id, { intro_layout: request.layout })
              : applyPlanItemCustomEffect(itemId, variant.variant_id, request.effect);
          const failureMessage =
            request.kind === "set_intro_layout"
              ? "Couldn't update the intro layout."
              : "Couldn't apply that effect.";
          void dispatch
            .then(() => {
              if (stepsFeedEnabled) startCopilotRenderPoll();
              if (copilotRenderNavTimerRef.current !== null) {
                window.clearTimeout(copilotRenderNavTimerRef.current);
              }
              copilotRenderNavTimerRef.current = window.setTimeout(() => {
                copilotRenderNavTimerRef.current = null;
                stopCopilotRenderPoll();
                router.push(`/plan/items/${itemId}`);
              }, 1400);
            })
            .catch((err) => {
              notify(failureMessage);
            });
        }
        // Flag off: byte-identical to today — no isRenderTurn/assistantText
        // override, so the caller falls back to the outcome-derived
        // "Staged: Intro layout: ... Save to render the new video." reply and
        // the lime receipt pill.
        //
        // Flag on: the agent's OWN reply is REQUIRED reading here — the
        // prompt obligates it to carry the feeling-label, the "can't be
        // undone from chat" disclosure, and "current version stays in
        // history" on every render-turn reply (set_intro_layout AND
        // apply_custom_effect). Dropping it in favor of generic copy silently
        // strips those disclosures from the user. The hardcoded fallback
        // exists ONLY for the case the model returns an empty/whitespace
        // reply — never as a routine override.
        return stepsFeedEnabled
          ? {
              isRenderTurn: true,
              assistantText:
                response?.reply?.trim() ||
                "That's a re-render, not an instant edit — starting it now.",
            }
          : {};
      }
      // PR7: undo_last_edit has no local draft representation to route
      // through the generic hasAppliedChanges path below — apply-ops.ts
      // already verified ctx.canUndoLastTurn before setting this signal, so
      // invoking the exact same handler the drawer's own Undo link/chip use
      // (history.undo) is safe. repeat_last_edit needs no branch here: its
      // recursive re-apply already merged real mutation fields (nextSlots,
      // textActions, ...) into `result`, so it flows through the ordinary
      // path below like any other applied turn.
      if (result.historyAction === "undo") {
        history.undo();
        return { assistantText: "Undone — back to the previous version." };
      }
      const hasAppliedChanges =
        result.textActions.length > 0 ||
        result.nextSlots !== null ||
        result.nextSfx != null ||
        result.nextOverlays != null ||
        result.nextCameraEffects != null ||
        result.nextVisualBlocks != null ||
        result.nextMotionScenes != null ||
        result.nextCarouselMoment !== undefined ||
        (result.acceptedSuggestionRefs?.length ?? 0) > 0 ||
        result.nextMusicTrackId !== undefined ||
        result.musicRemoved !== undefined ||
        result.nextMixLevel !== undefined ||
        result.nextTitle !== undefined ||
        result.captionMetaPatch !== undefined;
      if (!hasAppliedChanges) {
        if (result.openTool) setActiveTool(result.openTool);
        return {};
      }
      if (readOnly) return {};

      const version = history.record();
      // PR7: remember this turn as the repeat/undo candidate for a LATER
      // turn. Deliberately placed here (never in the renderRequest branch
      // above) — "chips are absent after a server-render turn" (Phase-0
      // design): nothing to instantly repeat or locally undo once a
      // re-render has started.
      if (result.appliedOps && result.appliedOps.length > 0) {
        const appliedLines = result.applied.map(
          (chip) =>
            `${chip.label}: ${chip.from} → ${chip.to}${(chip.count ?? 1) > 1 ? ` (×${chip.count})` : ""}`,
        );
        lastAppliedTurnRef.current = {
          undoVersion: version,
          ops: result.appliedOps,
          summary: summarizeAppliedTurn(appliedLines),
        };
      }
      const beforeSfxIds = new Set(localSfx.map((sfx) => sfx.id));
      const beforeOverlayById = new Map(localOverlays.map((overlay) => [overlay.id, overlay]));
      result.textActions.forEach((action) => dispatch(action));
      if (result.textActions.some((action) => {
        if ("id" in action) return isCaptionBar(state.bars.find((bar) => bar.id === action.id));
        if (action.type === "PATCH_BARS") {
          return action.patches.some((patch) =>
            isCaptionBar(state.bars.find((bar) => bar.id === patch.id)),
          );
        }
        return false;
      })) {
        setCaptionDirty(true);
      }
      if (
        lyricsOptionalActive ||
        result.textActions.some((action) => {
          if (action.type === "ADD_TEXT") return true;
          if (action.type === "PATCH_BARS") {
            return action.patches.some((patch) => {
              const bar = state.bars.find((candidate) => candidate.id === patch.id);
              return !isCaptionBar(bar) && !isLyricBar(bar);
            });
          }
          if (!("id" in action)) return false;
          const bar = state.bars.find((candidate) => candidate.id === action.id);
          return !isCaptionBar(bar) && !isLyricBar(bar);
        })
      ) {
        setTextDirty(true);
      }
      if (result.nextSlots) {
        setLocalSlots(result.nextSlots);
      }
      if (result.nextSfx) {
        setLocalSfx(result.nextSfx);
        setSfxDirty(true);
      }
      if (result.nextOverlays) {
        setLocalOverlays(result.nextOverlays);
        setOverlaysDirty(true);
      }
      if (result.nextCameraEffects) {
        setLocalCameraEffects(result.nextCameraEffects);
        setCameraEffectsDirty(true);
      }
      if (result.nextVisualBlocks) {
        setLocalVisualBlocks(result.nextVisualBlocks);
        setVisualBlocksDirty(true);
      }
      if (result.nextMotionScenes) {
        setLocalMotionScenes(result.nextMotionScenes);
        setMotionScenesDirty(true);
      }
      if (result.nextCarouselMoment !== undefined) {
        applyCarouselMoment(result.nextCarouselMoment);
        if (result.nextCarouselMoment === null && selection?.kind === "carousel") {
          clear();
        }
      }
      if (result.acceptedSuggestionRefs?.length) {
        setAcceptedSuggestions((cur) => {
          const seen = new Set(cur.map((ref) => ref.id));
          return [
            ...cur,
            ...result.acceptedSuggestionRefs!.filter((ref) => !seen.has(ref.id)),
          ];
        });
        for (const ref of result.acceptedSuggestionRefs) {
          overlaySuggestions.removeRow(ref.id, { accepted: true });
        }
      }
      if (result.nextMusicTrackId !== undefined) {
        setSelectedMusicTrackId(result.nextMusicTrackId);
        setMusicDirty(result.nextMusicTrackId !== variant?.music_track_id);
      }
      if (result.musicRemoved !== undefined) {
        setSelectedMusicTrackId(null);
        setMusicRemoved(true);
        setMusicStartS(0);
        setMusicDirty(true);
      }
      if (result.nextMixLevel !== undefined) {
        setMixLevel(result.nextMixLevel);
        setSoundMuted(result.nextMixLevel === 0);
        setMixDirty(true);
      }
      if (result.nextTitle !== undefined && introControlsEditable) {
        setTitle(result.nextTitle);
        setTitleDirty(true);
      }
      if (result.captionMetaPatch !== undefined) {
        setCaptionMeta((current) => {
          const base = current ?? (variant ? captionMetaFromVariant(variant) : null);
          return base ? { ...base, ...result.captionMetaPatch } : base;
        });
        setCaptionMetaPatch((current) => ({ ...current, ...result.captionMetaPatch }));
        setCaptionMetaDirty(true);
      }
      if (result.openTool) setActiveTool(result.openTool);
      setSessionHasCopilotEdits(true);

      const feedback = resolveCopilotApplyFeedback({
        result,
        bars: state.bars,
        beforeSlots: slots,
        grid: clip.state.grid,
      });
      const changedOverlayIds = result.nextOverlays
        ? result.nextOverlays
            .filter((overlay) => JSON.stringify(beforeOverlayById.get(overlay.id)) !== JSON.stringify(overlay))
            .map((overlay) => overlay.id)
        : [];
      const changedOverlay = result.nextOverlays?.find((overlay) =>
        changedOverlayIds.includes(overlay.id),
      ) ?? null;
      const addedSfx = result.nextSfx?.find((sfx) => !beforeSfxIds.has(sfx.id)) ?? null;
      flashCopilotTargets({
        textIds: feedback.textIds,
        overlayIds: changedOverlayIds,
        timelineIds: [
          ...feedback.textIds,
          ...feedback.slotIds,
          ...(result.nextSfx ? result.nextSfx.map((sfx) => sfx.id) : []),
          ...changedOverlayIds,
          ...(result.nextMotionScenes ? result.nextMotionScenes.map((scene) => scene.id) : []),
        ],
      });

      const previewFocus: DirectorPreviewFocus | undefined = addedSfx
        ? { kind: "sfx", id: addedSfx.id, seekS: addedSfx.at_s ?? 0 }
        : feedback.first ?? (changedOverlay
          ? { kind: "overlay", id: changedOverlay.id, seekS: changedOverlay.start_s }
          : undefined);
      if (previewFocus) revealCopilotFocus(previewFocus);

      return { undoVersion: version, previewFocus };
    },
    [
      applyCarouselMoment,
      clip.state.grid,
      clear,
      flashCopilotTargets,
      history,
      introControlsEditable,
      localOverlays,
      localSfx,
      overlaySuggestions,
      readOnly,
      revealCopilotFocus,
      router,
      selection,
      slots,
      state.bars,
      itemId,
      variant,
      lyricsOptionalActive,
      stepsFeedEnabled,
      startCopilotRenderPoll,
      stopCopilotRenderPoll,
      notify,
    ],
  );

  // CarouselPanel's own entry point: one explicit user action, one undo step
  // (history.record() here, then delegate the actual state write to
  // applyCarouselMoment — the same setter handleCopilotOps calls, but
  // WITHOUT its own history.record(), since a copilot turn already recorded
  // its single snapshot before applying any of its result fields).
  const stageCarouselMoment = useCallback(
    (config: CarouselMoment | null) => {
      if (readOnly) return;
      history.record();
      applyCarouselMoment(config);
    },
    [history, readOnly, applyCarouselMoment],
  );
  const carouselCapable = !readOnly && capabilities?.carousel === true;
  const carouselReason = readOnly
    ? readOnlyReason
    : capabilities?.carousel === true
      ? null
      : editorReasonCopy(capabilities?.carousel_reason ?? "carousel isn't available for this edit");
  const carouselControl = useMemo(
    () => ({
      capable: carouselCapable,
      reason: carouselReason,
      current: carouselMoment,
      clips: carouselClips,
      onChange: (config: CarouselMoment) => {
        if (!carouselCapable) {
          notify(carouselReason ?? "Carousel isn't available for this edit.");
          return;
        }
        stageCarouselMoment(config);
      },
      onRemove: () => {
        if (!carouselCapable) {
          notify(carouselReason ?? "Carousel isn't available for this edit.");
          return;
        }
        stageCarouselMoment(null);
      },
      onDisabledTap: notify,
    }),
    [carouselCapable, carouselReason, carouselMoment, carouselClips, stageCarouselMoment, notify],
  );
  const carouselInspectorControl = useMemo(
    () => ({
      ...carouselControl,
      onRemove: () => {
        carouselControl.onRemove();
        clear();
      },
    }),
    [carouselControl, clear],
  );

  const copilot = useEditCopilot({
    itemId,
    variantId: variant?.variant_id ?? variantParam ?? "",
    buildSnapshot: buildCopilotDraftSnapshot,
    applyOps: applyCopilotDraftOps,
    applyOpsAtomic: applyDirectorDraftOps,
    onApplied: handleCopilotOps,
    onReceiptStaged: (receiptId, undoVersion) => {
      if (lastAppliedTurnRef.current?.undoVersion === undoVersion) {
        lastAppliedTurnRef.current.receiptId = receiptId;
      }
    },
  });
  const director = useEditDirector({
    enabled: EDIT_DIRECTOR_UI_ENABLED && !readOnly,
    omniEnabled: OMNI_GENERATED_VIDEO_UI_ENABLED,
    itemId,
    variantId: variant?.variant_id ?? variantParam ?? "",
    buildSnapshot: buildCopilotDraftSnapshot,
    applyOpsAtomic: applyDirectorDraftOps,
    onApplied: handleCopilotOps,
    onRevealApplied: revealCopilotFocus,
    onGeneratedAssetReady: reloadClipTimeline,
    speechCutRevision: variant?.speech_cut_revision,
    speechCutLastReceipt: variant?.speech_cut_last_receipt,
    speechCutLastError: variant?.speech_cut_last_error,
    serverRenderPending:
      variant?.render_status === "rendering" || Boolean(variant?.speech_cut_in_flight),
    serverOperationsEnabled: !dirty && !saving,
    onServerRenderStarted: () => router.refresh(),
    canRestoreOriginalTiming: Boolean(variant?.silence_cut?.removed?.length),
  });

  // Derived after the hook, not folded into toolDisabledReasons: that memo
  // feeds buildCopilotDraftSnapshot, which the copilot hook itself depends on.
  const railDisabledReasons = useMemo(
    () =>
      copilot.unavailable
        ? { ...toolDisabledReasons, nova: COPILOT_UNAVAILABLE_MESSAGE }
        : toolDisabledReasons,
    [toolDisabledReasons, copilot.unavailable],
  );

  const deleteSelected = useCallback(() => {
    if (!selection || readOnly) return;
    if (selection.kind === "text") {
      const selected = state.bars.find((bar) => bar.id === selection.id);
      if (!isCaptionBar(selected) && !textElementsAllowed) {
        notify(textDisabledReason ?? "Text is locked for this story.");
        return;
      }
      if (
        visualBlocksAllowed &&
        selected?.visual_block_id &&
        state.bars.filter((bar) => bar.visual_block_id === selected.visual_block_id).length <= 1
      ) {
        deleteVisualBlock(selected.visual_block_id);
        clear();
        return;
      }
      history.record();
      if (isCaptionBar(selected)) setCaptionDirty(true);
      else setTextDirty(true);
      dispatch({ type: "DELETE_BAR", id: selection.id });
      clear();
    } else if (selection.kind === "clip" && clipRemoveAllowed) {
      const res = deleteSlotEnforceFloor(slots, selection.id);
      if (res.didDelete) {
        history.record();
        setLocalSlots(res.slots);
        clear();
      } else {
        notify("Keep at least one clip.");
      }
    } else if (selection.kind === "sfx") {
      if (sfxAllowed) removeSfx(selection.id);
      else notify(sfxDisabledReason ?? "Sound effects aren't available for this edit.");
    } else if (selection.kind === "overlay") {
      if (overlaysAllowed) removeOverlay(selection.id);
      else notify(overlaysDisabledReason ?? "Overlays aren't available for this edit.");
    } else if (selection.kind === "visual") {
      if (visualBlocksAllowed) {
        deleteVisualBlock(selection.id);
        clear();
      } else {
        notify(visualBlocksDisabledReason ?? "Visual blocks aren't available for this edit.");
      }
    } else if (selection.kind === "motion") {
      if (motionScenesAllowed) {
        removeMotionScene(selection.id);
        clear();
      } else {
        notify(motionScenesDisabledReason ?? "Motion isn't available for this edit.");
      }
    } else if (selection.kind === "carousel") {
      if (!carouselCapable || carouselMoment === null) {
        notify(
          carouselReason ??
            (carouselMoment === null
              ? "Add a Carousel before removing it."
              : "Carousel isn't available for this edit."),
        );
        return;
      }
      carouselControl.onRemove();
      clear();
    } else if (selection.kind === "camera") {
      deleteCameraEffect(selection.id);
    }
  }, [
    clipRemoveAllowed,
    selection,
    clear,
    slots,
    readOnly,
    history,
    removeSfx,
    removeOverlay,
    deleteVisualBlock,
    removeMotionScene,
    carouselCapable,
    carouselControl,
    carouselMoment,
    carouselReason,
    deleteCameraEffect,
    state.bars,
    textElementsAllowed,
    textDisabledReason,
    sfxAllowed,
    sfxDisabledReason,
    overlaysAllowed,
    overlaysDisabledReason,
    visualBlocksAllowed,
    visualBlocksDisabledReason,
    motionScenesAllowed,
    motionScenesDisabledReason,
    notify,
  ]);

  const splitAtPlayhead = useCallback(() => {
    if (!selection || readOnly) return;
    const baseCurrentTime = outputToBaseTimeRef.current(currentTime);
    if (selection.kind === "text") {
      // Guard before recording so an out-of-bounds split (reducer no-op) never
      // pushes a spurious undo step.
      const bar = selectedBar;
      if (!bar) return;
      if (isLyricBar(bar)) {
        notify("Lyric timing is locked to the vocal.");
        return;
      }
      const at = Math.round(baseCurrentTime * 10) / 10;
      const MIN = 0.2;
      if (at <= bar.start_s + MIN - 1e-9 || at >= bar.end_s - MIN + 1e-9) {
        notify("Move the playhead over the text to split it.");
        return;
      }
      history.record();
      if (isCaptionBar(bar)) setCaptionDirty(true);
      else setTextDirty(true);
      dispatch({
        type: "SPLIT_BAR",
        id: selection.id,
        at_s: baseCurrentTime,
        newId: crypto.randomUUID(),
      });
    } else if (selection.kind === "clip") {
      if (!splitClipsAllowed) return;
      if (guidedStoryV2 && selectedClip?.sourceKind !== "video") {
        notify("Images can be resized, but they can’t be split.");
        return;
      }
      const res = splitSlotAt(
        slots,
        clip.state.grid,
        selection.id,
        baseCurrentTime,
        `split-${crypto.randomUUID()}`,
      );
      if (res.didSplit) {
        history.record();
        setLocalSlots(res.slots);
      } else {
        notify("Move the playhead over the clip to split it.");
      }
    }
  }, [
    selection,
    currentTime,
    slots,
    clip.state.grid,
    splitClipsAllowed,
    guidedStoryV2,
    readOnly,
    selectedBar,
    selectedClip,
    history,
    notify,
  ]);

  const togglePlay = useCallback(() => {
    if (virtualPreviewActive) {
      toggleVirtualPreview();
      return;
    }
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }, [toggleVirtualPreview, virtualPreviewActive]);

  const seekTo = useCallback((sec: number) => {
    seekPlaybackTo(sec);
  }, [seekPlaybackTo]);

  const nudgeSelectedText = useCallback(
    (deltaS: number) => {
      if (readOnly || selection?.kind !== "text") return;
      const bar = selectedBar;
      if (!bar) return;
      if (isLyricBar(bar)) return;
      const start_s = nudgeBarStart(bar, deltaS, duration);
      if (start_s === bar.start_s) return;
      history.record();
      if (isCaptionBar(bar)) setCaptionDirty(true);
      else setTextDirty(true);
      dispatch({ type: "MOVE_BAR", id: bar.id, start_s });
    },
    [duration, history, readOnly, selection, selectedBar],
  );

  const nudgeSelectedMotion = useCallback(
    (deltaS: number) => {
      if (readOnly || selection?.kind !== "motion") return;
      const scene = localMotionScenes.find((item) => item.id === selection.id);
      if (!scene) return;
      const deltaFrames = Math.round(deltaS * MOTION_FPS);
      const span = scene.end_frame_exclusive - scene.start_frame;
      const durationFrames = motionDurationFrames();
      const startFrame = Math.max(
        0,
        Math.min(durationFrames - span, scene.start_frame + deltaFrames),
      );
      if (startFrame === scene.start_frame) return;
      patchMotionScene(scene.id, {
        start_frame: startFrame,
        end_frame_exclusive: startFrame + span,
      });
    },
    [localMotionScenes, motionDurationFrames, patchMotionScene, readOnly, selection],
  );

  // Transport enablement (plan §6).
  const splitBaseTime = outputToBaseTimeRef.current(currentTime);
  const selectedTextCanSplitAtPlayhead =
    selection?.kind === "text" &&
    !!selectedBar &&
    !isLyricBar(selectedBar) &&
    Math.round(splitBaseTime * 10) / 10 > selectedBar.start_s + 0.2 - 1e-9 &&
    Math.round(splitBaseTime * 10) / 10 < selectedBar.end_s - 0.2 + 1e-9;
  const selectedClipCanSplitAtPlayhead =
    selection?.kind === "clip" &&
    canSplitSlotAt(slots, clip.state.grid, selection.id, splitBaseTime);
  const canSplit =
    selectedTextCanSplitAtPlayhead ||
    (selection?.kind === "clip" &&
      splitClipsAllowed &&
      (!guidedStoryV2 || selectedClip?.sourceKind === "video") &&
      selectedClipCanSplitAtPlayhead);
  let splitReason: string | undefined;
  if (selection?.kind === "music") {
    splitReason = "Music fits the cut automatically";
  } else if (selection?.kind === "text" && selectedBar && isLyricBar(selectedBar)) {
    splitReason = "Lyric timing is locked to the vocal.";
  } else if (selection?.kind === "text" && !selectedTextCanSplitAtPlayhead) {
    splitReason = "Move the playhead over the text to split it.";
  } else if (selection?.kind === "clip" && !splitClipsAllowed) {
    splitReason = clipSplitDisabledReason ?? "This variant's clips can't be split";
  } else if (
    selection?.kind === "clip" &&
    guidedStoryV2 &&
    selectedClip?.sourceKind !== "video"
  ) {
    splitReason = "Images can be resized, but they can’t be split.";
  } else if (selection?.kind === "clip" && !selectedClipCanSplitAtPlayhead) {
    splitReason = "Move the playhead inside this clip to split.";
  }
  const canDelete =
    (selection?.kind === "text" && !!selectedBar && !isLyricBar(selectedBar)) ||
    (selection?.kind === "clip" && clipRemoveAllowed && activeSlotCount(slots) > 1) ||
    selection?.kind === "sfx" ||
    selection?.kind === "overlay" ||
    (selection?.kind === "visual" && visualBlocksAllowed) ||
    (selection?.kind === "motion" && motionScenesAllowed) ||
    (selection?.kind === "carousel" && carouselCapable && carouselMoment !== null) ||
    selection?.kind === "camera";
  const selectedClipDeleteDisabledReason = !clipRemoveAllowed
    ? (clipRemoveDisabledReason ?? "This variant's clips can't be deleted")
    : activeSlotCount(slots) <= 1
      ? "At least one clip must remain"
      : null;

  // ── Keyboard: Escape ladder + Delete with focus guard (plan §5/§9) ──────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ⌘Z / ⇧⌘Z (⌃Z / ⌃⇧Z, ⌘Y) — document undo/redo. Guarded: when focus is
      // in a text field, let the browser's native text undo win.
      if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        if (e.shiftKey) history.redo();
        else history.undo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "y" || e.key === "Y")) {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        history.redo();
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        if (selection?.kind !== "text" && selection?.kind !== "motion") return;
        e.preventDefault();
        const step = selection.kind === "motion"
          ? e.shiftKey ? 10 / MOTION_FPS : 1 / MOTION_FPS
          : e.shiftKey ? 1 : 0.1;
        const delta = e.key === "ArrowLeft" ? -step : step;
        if (selection.kind === "motion") nudgeSelectedMotion(delta);
        else nudgeSelectedText(delta);
        return;
      }
      if (e.key === " " || e.key === "Spacebar") {
        if (!spaceShortcutAllowed(e.target as HTMLElement | null)) return;
        e.preventDefault();
        togglePlay();
        return;
      }
      if (e.key === "Escape") {
        // Pocket sheets own their Escape (one press, one effect): the Sheet
        // closes itself; the shell must not ALSO clear the selection.
        if (pocketSheetOpen) return;
        if (layoutMode === "light" && lightSheetOpen) {
          e.preventDefault();
          setLightSheetOpen(false);
          return;
        }
        const target = e.target as HTMLElement | null;
        // One press, one effect: leaving a text field is that effect.
        if (target && !deleteKeyAllowed(target)) {
          target.blur();
          return;
        }
        const action = escapeAction({
          drawerOpen: activeTool !== null,
          hasSelection: selection !== null,
        });
        if (action === "close-drawer") setActiveTool(null);
        else if (action === "clear-selection") clear();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        if (!deleteKeyAllowed(e.target as HTMLElement | null)) return;
        if (canDelete) {
          e.preventDefault();
          deleteSelected();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [
    activeTool,
    selection,
    clear,
    deleteSelected,
    canDelete,
    history,
    pocketSheetOpen,
    layoutMode,
    lightSheetOpen,
    nudgeSelectedText,
    nudgeSelectedMotion,
    togglePlay,
  ]);

  // ── Save / leave ────────────────────────────────────────────────────────────

  const clearDraft = useCallback(() => {
    if (!variant) return;
    try {
      window.sessionStorage.removeItem(draftKey(itemId, variant.variant_id));
    } catch {
      /* privacy mode / quota — nothing to clean up */
    }
  }, [itemId, variant]);

  const handleSave = useCallback(async (
    musicAlignment?: "preserve_cuts" | "resync_beats",
    /**
     * Runs AFTER the commit lands but BEFORE the redirect to the item page.
     *
     * The Captions drawer's "Save & re-transcribe" needs exactly this seam:
     * re-transcribing replaces every cue server-side while the session may hold
     * unsaved edits in every lane, so the commit has to go first — but the
     * redirect has to go last, or the language request would fire after the
     * editor already unmounted. A throw here reports honestly ("saved, but…")
     * and suppresses the redirect so the user can retry.
     */
    opts?: { afterCommit?: () => Promise<void>; afterCommitFailedMessage?: string },
    confirmedSongReset = false,
  ) => {
    if (!variant || saveState === "saving" || readOnly) return;
    const commitMusicWindow = musicWindowDirty && !!songWindowState?.editable;
    if (commitMusicWindow && !musicAlignment) {
      setMusicAlignmentPrompt(true);
      return;
    }
    if (!commitMusicWindow && musicDirty && clipDirty && !confirmedSongReset) {
      setSongResetPrompt({ musicAlignment, opts });
      return;
    }
    setSongResetPrompt(null);
    setMusicAlignmentPrompt(false);
    setSaveState("saving");
    setSaveMessage(null);
    try {
      const captionCues = barsToCaptionCues(state.bars, captionOriginalsRef.current);
      const lyricsRequest: EditorCommitLyricsRequest = {
        ...(lyricsEnabled !== persistedLyricsEnabled(variant) ? { enabled: lyricsEnabled } : {}),
        ...(lyricOverridesDirty ? { line_overrides: lyricLineOverrides } : {}),
      };
      let commitRequest = buildEditorCommitRequest({
        // Elements-model lyric_line bars ride the normal text_elements
        // section (they're ordinary persisted elements on that model);
        // legacy leaves them out — they persist via the `lyrics` section
        // below instead (lyricsRequest / lyricsDirty, forced off above).
        elements: barsToTextElements(state.bars, originalsRef.current, {
          includeLyrics: lyricsOptionalActive,
        }),
        captionCues,
        captionMeta: captionMetaPatch,
        textDirty,
        captionDirty,
        captionMetaDirty,
        timelineDirty: clipDirty,
        slots,
        mixDirty,
        mixLevel,
        musicDirty,
        musicTrackId: selectedMusicTrackId,
        musicRemoved,
        musicWindow:
          commitMusicWindow && musicAlignment
            ? { startS: songWindowState.startS, alignment: musicAlignment }
            : undefined,
        backgroundMusicDirty,
        backgroundMusic: backgroundMusic ?? { track_id: null, enabled: false },
        sfxDirty,
        soundEffects: localSfx,
        overlaysDirty,
        mediaOverlays: localOverlays,
        visualBlocksDirty,
        visualBlocks: localVisualBlocks,
        motionScenesDirty,
        motionScenes: localMotionScenes,
        motionRuntimeHash: capabilities?.motion_runtime_hash ?? MOTION_RUNTIME_HASH,
        cameraEffectsDirty,
        cameraEffects: localCameraEffects,
        carouselMomentDirty,
        carouselMoment,
        // Filtered against the staged overlay ids inside the builder — an
        // accepted suggestion the user undid must not be resolved.
        acceptedSuggestions,
        titleDirty,
        title,
        lyricsDirty,
        lyrics: lyricsRequest,
        orientationDirty,
        orientation,
        guidedRevisionNumber: guidedStoryV2 ? clip.revisionNumber : undefined,
        variant,
      });
      const latestCopilotTurn = lastAppliedTurnRef.current;
      if (
        latestCopilotTurn?.receiptId &&
        latestCopilotTurn.undoVersion === history.version
      ) {
        commitRequest.copilot_receipt_ids = [latestCopilotTurn.receiptId];
      }
      if (partialCommitGenerationRef.current) {
        const draftChangedAfterPartial =
          partialHistoryVersionRef.current != null &&
          history.version !== partialHistoryVersionRef.current;
        if (
          guidedStoryV2 &&
          partialGuidedRevisionRef.current != null &&
          !draftChangedAfterPartial
        ) {
          commitRequest = {
            base_generation: partialCommitGenerationRef.current,
            guided_revision_number: partialGuidedRevisionRef.current,
            retry_guided_revision: true,
          };
        } else {
          commitRequest.base_generation = partialCommitGenerationRef.current;
          if (guidedStoryV2 && partialGuidedRevisionRef.current != null) {
            commitRequest.guided_revision_number = partialGuidedRevisionRef.current;
          }
        }
      }
      const res = await commitEditorSession(
        itemId,
        variant.variant_id,
        commitRequest,
      );
      // Partial: persist landed (we got a 2xx) but the render kick failed —
      // the response's `ok` flag tells us. Working state stays, Retry re-kicks.
      if (res && res.ok === false) {
        partialCommitGenerationRef.current = res.generation;
        partialGuidedRevisionRef.current = res.revision_number ?? null;
        partialHistoryVersionRef.current = history.version;
        setSaveState("partial");
        setSaveMessage("Saved, but rendering didn't start.");
        return;
      }
      // Post-commit side effect (today: re-transcribe in a new caption
      // language). Runs before any local state is cleared so a failure leaves
      // the session exactly as the commit left it.
      if (opts?.afterCommit) {
        try {
          await opts.afterCommit();
        } catch (err) {
          setSaveState("partial");
          setSaveMessage(
            opts.afterCommitFailedMessage ??
              "Saved, but the follow-up step failed. Try again.",
          );
          return;
        }
      }
      // Full success: the stack is void (no undoing into a pre-persist world),
      // the draft is spent, and the item-page hero shows the rendering state.
      draftPersistenceSuspendedRef.current = true;
      partialCommitGenerationRef.current = null;
      partialGuidedRevisionRef.current = null;
      partialHistoryVersionRef.current = null;
      history.clear();
      clearDraft();
      setDraftDoc(null);
      setTextDirty(false);
      setCaptionDirty(false);
      setSfxDirty(false);
      setOverlaysDirty(false);
      setVisualBlocksDirty(false);
      setMotionScenesDirty(false);
      setCameraEffectsDirty(false);
      setTitleDirty(false);
      setMixDirty(false);
      setMusicDirty(false);
      setMusicRemoved(false);
      setBackgroundMusicDirty(false);
      setCarouselMomentDirty(false);
      setCaptionMetaDirty(false);
      setCaptionMetaPatch({});
      setSaveState("idle");
      const renderStarted = editorCommitStartedRender(res.sections);
      setSaveMessage(renderStarted ? "Saved — rendering your latest version" : "Saved");
      router.push(
        buildPlanItemEditorReturnHref(itemId, {
          variantId: variant.variant_id,
          generation: res.generation,
          priorFinishedAt: variant.render_finished_at ?? null,
          renderStarted,
          expectedDurationS: renderStarted ? res.expected_duration_s ?? null : null,
          revisionHash: renderStarted ? res.revision_hash ?? null : null,
        }),
      );
    } catch (err) {
      if (err instanceof EditorCommitConflictError) {
        setSaveState("conflict");
        setSaveMessage("This edit changed elsewhere. Refresh and try again.");
      } else {
        // Network-class failures (typed by editor-commit) arm the one-shot
        // auto-retry-on-online below; their message is already the fixed copy.
        networkSaveErrorRef.current = err instanceof EditorCommitNetworkError;
        setSaveState("error");
        setSaveMessage(
          "We couldn't save your edits. Try again.",
        );
      }
    }
  }, [
    variant,
    saveState,
    readOnly,
    itemId,
    state.bars,
    title,
    router,
    clipDirty,
    clip.revisionNumber,
    slots,
    mixDirty,
    mixLevel,
    musicDirty,
    musicRemoved,
    backgroundMusic,
    backgroundMusicDirty,
    selectedMusicTrackId,
    musicWindowDirty,
    songWindowState,
    captionMetaDirty,
    captionMetaPatch,
    textDirty,
    captionDirty,
    lyricsDirty,
    orientationDirty,
    orientation,
    lyricOverridesDirty,
    lyricsEnabled,
    lyricLineOverrides,
    lyricsOptionalActive,
    sfxDirty,
    localSfx,
    overlaysDirty,
    localOverlays,
    visualBlocksDirty,
    localVisualBlocks,
    motionScenesDirty,
    localMotionScenes,
    capabilities?.motion_runtime_hash,
    cameraEffectsDirty,
    localCameraEffects,
    carouselMomentDirty,
    carouselMoment,
    acceptedSuggestions,
    titleDirty,
    history,
    clearDraft,
    guidedStoryV2,
  ]);

  /**
   * Language switch = Save & re-transcribe, in that order, as ONE user action.
   *
   * Re-transcribing rewrites every cue server-side, and the session can hold
   * unsaved work in every lane — so committing first is what makes the switch
   * non-destructive to everything except the caption text it is explicitly
   * replacing. Riding `handleSave`'s afterCommit seam (rather than calling save
   * and then the language route separately) matters because a successful save
   * redirects to the item page; firing the language request after that would
   * race an unmounting editor.
   */
  const handleChangeCaptionLanguage = useCallback(
    async (language: CaptionLanguage) => {
      if (!variant || readOnly || captionsBusy !== "idle") return;
      setCaptionsError(null);
      setCaptionsBusy("saving");
      try {
        await handleSave(undefined, {
          afterCommit: async () => {
            setCaptionsBusy("transcribing");
            await setPlanItemCaptionLanguage(itemId, variant.variant_id, language);
          },
          afterCommitFailedMessage:
            "Couldn't re-transcribe. Your edits were saved.",
        });
      } catch (err) {
        setCaptionsError(
          "We couldn't re-transcribe. Try again.",
        );
      } finally {
        setCaptionsBusy("idle");
      }
    },
    [captionsBusy, handleSave, itemId, readOnly, variant],
  );

  const captionsControl = useMemo<CaptionsDrawerControl | undefined>(() => {
    if (captionsToolState !== "editable") return undefined;
    return {
      cues: captionCueRows,
      selectedId: selection?.kind === "text" ? selection.id : null,
      currentTime: outputToBaseTimeRef.current(currentTime),
      meta: captionMeta,
      language: variant?.caption_language ?? null,
      readOnly,
      busy: captionsBusy,
      error: captionsError,
      onSelectCue: (id: string) => {
        const cue = captionCueRows.find((c) => c.id === id);
        if (cue) seekPlaybackTo(baseToOutputTimeRef.current(cue.start_s + 0.02));
        // preserveOverlayTool: at 1024-1280px selecting anything normally closes
        // the drawer (shouldCloseToolOnSelection). Clicking a cue IN the drawer
        // must not destroy the list you are working through.
        selectElement("text", id, { preserveOverlayTool: true });
      },
      onEditCueText: (id: string, text: string) => patchBar(id, { text }),
      onPatchMeta: patchCaptionMeta,
      onReplaceAll: replaceInCaptions,
      onChangeLanguage: variant?.caption_language
        ? (language) => void handleChangeCaptionLanguage(language)
        : undefined,
      // Zero-cue recovery: re-run transcription in the SAME language. The route
      // re-transcribes unconditionally (no unchanged-language short-circuit), so
      // this genuinely retries a transcript that came back empty. Subtitled-only
      // — the route 422s anything else, and a narrated variant has no language
      // to re-run with, so it correctly gets no button rather than a broken one.
      onRetranscribe: variant?.caption_language
        ? () =>
            void handleChangeCaptionLanguage(
              variant.caption_language as CaptionLanguage,
            )
        : undefined,
    };
  }, [
    captionCueRows,
    captionMeta,
    captionsBusy,
    captionsError,
    captionsToolState,
    currentTime,
    handleChangeCaptionLanguage,
    patchBar,
    patchCaptionMeta,
    readOnly,
    replaceInCaptions,
    seekPlaybackTo,
    selectElement,
    selection,
    variant,
  ]);

  // ── Draft recovery (plan §9) ────────────────────────────────────────────────
  // Mirror the working document to sessionStorage on every command push (any
  // document change while dirty). Failures degrade draft safety silently.
  const dirtyDraftIdentityRef = useRef<string | null>(null);
  useEffect(() => {
    if (draftPersistenceSuspendedRef.current) return;
    // During client-side [id] navigation React can retain this shell while the
    // next item loads. Never write the previous item's working document under
    // the next item's draft key in that gap.
    if (!variant || item?.id !== itemId) return;
    const identity = `${itemId}:${variant.variant_id}`;
    if (!dirty) {
      if (dirtyDraftIdentityRef.current === identity) {
        clearDraft();
        dirtyDraftIdentityRef.current = null;
      }
      return;
    }
    try {
      window.sessionStorage.setItem(
        draftKey(itemId, variant.variant_id),
        serializeDraft(
          itemId,
          item?.current_job_id ?? "",
          variant.variant_id,
          editorCommitBaseGeneration(variant),
          getCurrent(),
        ),
      );
      dirtyDraftIdentityRef.current = identity;
    } catch {
      /* quota full / privacy mode — editing continues, draft safety only */
    }
  }, [
    variant,
    itemId,
    item?.id,
    item?.current_job_id,
    dirty,
    state.bars,
    localSlots,
    localSfx,
    localOverlays,
    localVisualBlocks,
    localMotionScenes,
    captionMeta,
    captionMetaDirty,
    captionMetaPatch,
    videoMuted,
    soundMuted,
    mixLevel,
    mixDirty,
    selectedMusicTrackId,
    musicRemoved,
    musicStartS,
    musicDirty,
    title,
    orientation,
    getCurrent,
    clearDraft,
  ]);

  // On open, surface a matching unsaved draft as a quiet Resume/Discard notice
  // (once per variant, after seeding so a Resume overrides the seeded bars).
  const draftCheckedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!variant || item?.id !== itemId) return;
    const jobId = item.current_job_id ?? "";
    const baseGeneration = editorCommitBaseGeneration(variant);
    const identity = `${itemId}:${jobId}:${variant.variant_id}:${baseGeneration}`;
    if (draftCheckedRef.current === identity) return;
    draftCheckedRef.current = identity;
    try {
      const parsed = deserializeDraft(
        window.sessionStorage.getItem(draftKey(itemId, variant.variant_id)),
      );
      if (
        parsed &&
        parsed.planItemId === itemId &&
        parsed.jobId === jobId &&
        parsed.variantId === variant.variant_id &&
        parsed.baseGeneration === baseGeneration
      ) {
        setDraftDoc(parsed.doc);
      } else {
        setDraftDoc(null);
      }
    } catch {
      /* unreadable draft — skip the notice */
      setDraftDoc(null);
    }
  }, [item?.current_job_id, item?.id, itemId, variant]);

  const resumeDraft = useCallback(() => {
    if (!draftDoc) return;
    // Record the seeded baseline first so Resume itself is undoable, then
    // restore the draft as the working document.
    history.record();
    applyDocument(draftDoc);
    setDraftDoc(null);
  }, [draftDoc, history, applyDocument]);

  const discardDraft = useCallback(() => {
    clearDraft();
    setDraftDoc(null);
  }, [clearDraft]);

  const requestLeave = useCallback(() => {
    if (dirty) setConfirmLeave(true);
    else router.push(`/plan/items/${itemId}`);
  }, [dirty, router, itemId]);

  // ── Render ──────────────────────────────────────────────────────────────────

  // §5: one silent auto-retry when the browser comes back online after a
  // network-failed save. If the original POST actually landed, the stale
  // base_generation 409s into the conflict tile — expected and safe.
  // (Placed before the loading/auth early returns per rules-of-hooks.)
  useEffect(() => {
    if (!POCKET_UI) return;
    if (saveState !== "error" || !networkSaveErrorRef.current) return;
    const onOnline = () => {
      networkSaveErrorRef.current = false;
      void handleSave();
    };
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [saveState, handleSave]);

  if (needsAuth) {
    return (
      <Frame>
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-[#3f3f46]">
            Please{" "}
            <a href="/api/auth/signin" className="underline underline-offset-4">
              sign in
            </a>{" "}
            to edit this video.
          </p>
        </div>
      </Frame>
    );
  }

  if (loading) {
    // The old skeleton hardcoded `grid-cols-[92px_1fr_320px_72px]` — 484px of
    // columns — so on a phone the 1fr canvas collapsed to zero and the row
    // overflowed. Since the real editor only mounts after the async variant
    // load resolves, this skeleton is the FIRST thing a phone user sees.
    //
    // Gated in CSS rather than on `layoutMode`: useEditorLayoutMode's server
    // snapshot is "full", so a JS branch would paint the 484px columns on the
    // hydration render and only correct afterwards — a flash of exactly the
    // overflow this fixes. The skeleton is decorative, so it needs no JS state.
    // `xl:` matches the hook's FULL_QUERY (1280px).
    return (
      <Frame>
        <div className="flex min-h-0 flex-1">
          <div className="hidden w-[92px] shrink-0 border-r border-zinc-200 bg-white xl:block" />
          <div className="flex min-w-0 flex-1 items-center justify-center px-5">
            <div
              className="h-[70%] w-auto max-w-full rounded-xl border border-zinc-200 bg-zinc-100 motion-safe:animate-pulse"
              style={{ aspectRatio: "9 / 16" }}
            />
          </div>
          <div className="hidden w-[320px] shrink-0 border-l border-zinc-200 bg-white xl:block" />
          <div className="hidden w-[72px] shrink-0 border-l border-zinc-200 bg-white xl:block" />
        </div>
        {/* Below xl there is no docked timeline to stand in for (the heavy
            timeline never mounts under 1024px) — just the transport strip. */}
        <div className="h-16 flex-none border-t border-zinc-200 bg-white xl:h-[260px]" />
      </Frame>
    );
  }

  if (loadError || !variant) {
    return (
      <Frame>
        <div className="flex flex-1 items-center justify-center p-8">
          <div className="max-w-[420px] rounded-xl border border-dashed border-zinc-300 bg-white p-6 text-center">
            <p className="text-sm text-[#3f3f46]">
              {loadError ?? "This video doesn't have an editable version yet."}
            </p>
            <div className="mt-4 flex items-center justify-center gap-3">
              {loadError && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setLoadNonce((n) => n + 1)}
                >
                  Retry
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                onClick={() => router.push(`/plan/items/${itemId}`)}
              >
                Back to the video
              </Button>
            </div>
          </div>
        </div>
      </Frame>
    );
  }

  const isVoiceoverVariant = variant.variant_id.startsWith("voiceover");
  const musicSwapEditable = musicCan("swap", capabilities?.swap_song !== false) && !readOnly;
  const musicRemoveEditable = musicCan("remove", capabilities?.swap_song !== false) && !readOnly;
  const musicLevelEditable = musicCan("level", capabilities?.mix !== false) && !readOnly;
  const musicSwapDisabledReason = operationDisabledReason(capabilities?.music_operations?.swap);
  const musicRemoveDisabledReason = operationDisabledReason(capabilities?.music_operations?.remove);
  const musicLevelDisabledReason = operationDisabledReason(capabilities?.music_operations?.level);
  const hasPlayableMusic =
    !!effectiveAudioTrackId &&
    (!!virtualMusicAudioUrl ||
      !!virtualMusicTrack?.preview_audio_url ||
      !!variant.music_preview_url ||
      !!variant.background_music?.preview_url);
  const soundBedLabel = isVoiceoverVariant
    ? effectiveMusicTrackId
      ? `Narration + ${effectiveMusicTitle}`
      : "Narration"
    : effectiveMusicTitle;
  const soundLaneTitle = isVoiceoverVariant ? "Narration bed" : "Music + effects";
  const hasUnbakedSfx = sfxDirty || localSfx.length > 0;
  const clipPreviewHint = (() => {
    if (!virtualPreviewActive) return "Clip changes preview after Save";
    const missing: string[] = [];
    if (effectiveAudioTrackId && !virtualMusicAudioUrl) missing.push("Music");
    missing.push(missing.length > 0 ? "transitions" : "Transitions");
    if (hasUnbakedSfx) missing.push("sound effects");
    return `${missing.join(", ").replace(/, ([^,]*)$/, " and $1")} preview after Save`;
  })();

  const releasingPoolSlots = Math.max(
    0,
    poolUploader.reservedSlots - pendingPoolUploads.length,
  );
  const poolAtCapacity = poolAssets.length + poolUploader.reservedSlots >= maxPoolAssets;
  const visualUploadFeedback =
    poolError ||
    poolUploader.batchMessage ||
    poolUploader.summary ||
    releasingPoolSlots > 0 ||
    pendingPoolUploads.length > 0 ||
    poolAssets.some((asset) => asset.status !== "ready") ? (
      <div className="mb-3 space-y-2 text-[12px] text-[#71717a]">
        {poolError && (
          <p className="rounded border border-zinc-200 bg-white px-3 py-2 text-[#3f3f46]">
            {poolError}
          </p>
        )}
        {poolUploader.batchMessage && (
          <p className="rounded border border-zinc-200 bg-white px-3 py-2 text-[#3f3f46]">
            {poolUploader.batchMessage}
          </p>
        )}
        {poolUploader.summary && <p aria-live="polite">{poolUploader.summary}</p>}
        {releasingPoolSlots > 0 && (
          <p className="rounded border border-zinc-200 bg-white px-3 py-2 text-[#3f3f46]">
            Kria is releasing a removed upload slot. You can add another visual when cleanup finishes.
          </p>
        )}
        {pendingPoolUploads.map((upload) => (
          <div key={upload.localId} className="rounded border border-dashed border-zinc-300 p-2">
            <p className="truncate font-medium text-[#3f3f46]">{upload.filename}</p>
            <p>
              {upload.stage === "failed"
                ? upload.message
                : upload.stage === "preparing"
                  ? "Preparing upload…"
                  : upload.stage === "registering"
                    ? "Adding to your visuals…"
                    : "Uploading…"}
            </p>
            {upload.stage === "failed" && (
              <div className="mt-1 flex gap-3">
                {upload.retryable && (
                  <Button
                    type="button"
                    variant="link"
                    aria-label={`Retry ${upload.filename}`}
                    onClick={() => poolUploader.retry(upload.localId)}
                    className="h-auto min-h-7 p-0 text-[13px] text-lime-700 underline underline-offset-2 hover:text-lime-700"
                  >
                    Retry
                  </Button>
                )}
                <Button
                  type="button"
                  variant="link"
                  aria-label={`Remove ${upload.filename}`}
                  onClick={() => poolUploader.remove(upload.localId)}
                  className="h-auto min-h-7 p-0 text-[13px] underline underline-offset-2"
                >
                  Remove
                </Button>
              </div>
            )}
          </div>
        ))}
        {poolAssets
          .filter((asset) => asset.status !== "ready")
          .map((asset) => (
            <div key={asset.id} className="rounded border border-dashed border-zinc-300 p-2">
              <p className="truncate font-medium text-[#3f3f46]">
                {asset.source_filename ?? "This visual"}
              </p>
              <p>
                {asset.status === "queued"
                  ? "Queued for analysis…"
                  : asset.status === "analyzing" || asset.status === "uploaded"
                    ? "Analyzing…"
                    : "This visual couldn't be analyzed. Try again."}
              </p>
              {asset.status === "failed" && (
                <div className="mt-1 flex gap-3">
                  {asset.retryable !== false && (
                    <Button
                      type="button"
                      variant="link"
                      aria-label={`Retry analysis ${asset.source_filename ?? "visual"}`}
                      onClick={() => handleRetryPoolAsset(asset)}
                      className="h-auto min-h-7 p-0 text-[13px] text-lime-700 underline underline-offset-2 hover:text-lime-700"
                    >
                      Retry analysis
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="link"
                    aria-label={`Remove ${asset.source_filename ?? "visual"}`}
                    onClick={() => handleRemovePoolAsset(asset)}
                    className="h-auto min-h-7 p-0 text-[13px] underline underline-offset-2"
                  >
                    Remove
                  </Button>
                </div>
              )}
            </div>
          ))}
      </div>
    ) : null;

  // AI suggestions inside the Overlays drawer — dual-gated (frontend flag +
  // the variant's honest capability). A false capability (e.g.
  // song_or_lyric_variant) renders NOTHING: no dead chrome in the drawer.
  const overlaySuggestionsNode = overlaySuggestionsEnabled ? (
    <OverlaySuggestions
      suggestions={overlaySuggestions}
      assets={poolAssets}
      maxAssets={maxPoolAssets}
      pending={pendingPoolUploads}
      reservedSlots={poolUploader.reservedSlots}
      poolUnavailable={poolUnavailable}
      poolError={poolError}
      poolMessage={poolUploader.batchMessage}
      poolSummary={poolUploader.summary}
      onFiles={handlePoolFiles}
      onRetryPending={poolUploader.retry}
      onRemovePending={poolUploader.remove}
      onRemoveAsset={handleRemovePoolAsset}
      onRetryAsset={handleRetryPoolAsset}
      onAccept={handleAcceptSuggestion}
      onSeek={seekPlaybackTo}
    />
  ) : null;
  const activeGuidedTombstones = (() => {
    const activeByLane: Record<string, Set<string>> = {
      text_elements: new Set(state.bars.map((value) => value.id)),
      sound_effects: new Set(localSfx.map((value) => value.id)),
      media_overlays: new Set(localOverlays.map((value) => value.id)),
      visual_blocks: new Set(localVisualBlocks.map((value) => value.id)),
      motion_scenes: new Set(localMotionScenes.map((value) => value.id)),
    };
    return (clip.tombstones ?? []).filter(
      (value) => !activeByLane[value.lane]?.has(value.record_id),
    );
  })();
  const restoreGuidedTombstones = () => {
    if (!variant || activeGuidedTombstones.length === 0) return;
    history.record("restore-guided-tombstones");
    for (const tombstone of activeGuidedTombstones) {
      const record = tombstone.record;
      if (tombstone.lane === "text_elements") {
        const element = record as unknown as TextElement;
        const [bar] = seedBarsFromVariant(
          {
            ...variant,
            text_elements: [element],
            caption_cues: [],
            text_elements_user_edited: true,
          },
          { includeLyrics: true },
        );
        if (bar) {
          originalsRef.current.set(bar.id, element);
          dispatch({ type: "ADD_TEXT", bar });
          setTextDirty(true);
        }
      } else if (tombstone.lane === "sound_effects") {
        setLocalSfx((current) => [...current, record as unknown as SoundEffectPlacement]);
        setSfxDirty(true);
      } else if (tombstone.lane === "media_overlays") {
        setLocalOverlays((current) => [...current, record as unknown as MediaOverlay]);
        setOverlaysDirty(true);
      } else if (tombstone.lane === "visual_blocks") {
        setLocalVisualBlocks((current) => [...current, record as unknown as VisualBlock]);
        setVisualBlocksDirty(true);
      } else if (tombstone.lane === "motion_scenes") {
        setLocalMotionScenes((current) => [
          ...current,
          record as unknown as MotionPresetInstance,
        ]);
        setMotionScenesDirty(true);
      }
    }
    notify(
      `Restored ${activeGuidedTombstones.length} item${activeGuidedTombstones.length === 1 ? "" : "s"}.`,
    );
  };
  const showCopilotSaveNotice = sessionHasCopilotEdits && !copilotSaveNoticeDismissed;
  const lyricsToggle = {
    visible: !!variant && lyricsFeatureAvailable,
    enabled: lyricsOptionalActive ? hasLyricBars : lyricsEnabled,
    disabled: lyricsOptionalActive
      ? lyricSeedsLoading || lyricSeedsError != null || (!hasLyricBars && !lyricsCap.can_toggle_on)
      : !lyricsEnabled && !lyricsCap.can_toggle_on,
    hint: lyricsOptionalActive
      ? lyricSeedsLoading
        ? "Loading lyrics…"
        : lyricSeedsError === "no_lyrics"
          ? "This song doesn't have synced lyrics"
          : lyricSeedsError === "not_found"
            ? "Lyrics aren't available for this edit"
            : lyricsToggleHint(lyricsCap.reason)
      : lyricsToggleHint(lyricsCap.reason),
    onToggle: lyricsOptionalActive
      ? (next: boolean) => void toggleLyricsOptional(next)
      : (next: boolean) => {
          if (readOnly) return;
          if (next && !lyricsCap.can_toggle_on && !lyricsCap.enabled) {
            notify(lyricsToggleHint(lyricsCap.reason) ?? "Lyrics can't be enabled for this edit.");
            return;
          }
          if (next === lyricsEnabled) return;
          history.record("lyrics-toggle");
          setLyricsEnabled(next);
        },
  };
  const orientationCap = capabilities?.orientation;
  const orientationToggleVisible = LANDSCAPE_UI && orientationCap != null;
  const orientationToggleDisabled = saving || readOnly || orientationCap?.editable !== true;
  const orientationToggleHint = saving
    ? "Saving your edits…"
    : orientationDisabledHint(orientationCap?.reason);
  const orientationToggle = orientationToggleVisible ? (
    <OrientationToggle
      value={previewOrientation}
      disabled={orientationToggleDisabled}
      busy={saving}
      disabledHint={orientationToggleHint}
      onChange={(next) => {
        if (orientationToggleDisabled) {
          notify(orientationToggleHint ?? "Landscape isn't available for this edit.");
          return;
        }
        if (next === orientation) return;
        history.record("orientation");
        setOrientation(next);
      }}
    />
  ) : null;

  const editorModeProps: EditorTimelineBodyProps = {
    durationS: timelineDuration,
    timelineProjection: virtualPreview.timeline,
    renderedOutputDurationS: duration,
    currentTimeS: currentTime,
    playbackClock,
    zoom,
    fitRequestKey: timelineFitRequestKey,
    scaleResetKey: timelineVariantId,
    selection,
    onSelect: (kind, id) => {
      selectElement(kind, id);
    },
    onClear: clear,
    textBars: visibleTextBars,
    captionsExpanded: activeTool === "captions",
    captionsEnabled: captionMeta?.enabled ?? true,
    onOpenCaptionCue: (id: string) => {
      setActiveTool("captions");
      const cue = captionCueRows.find((c) => c.id === id);
      if (cue) seekPlaybackTo(baseToOutputTimeRef.current(cue.start_s + 0.02));
      selectElement("text", id, { preserveOverlayTool: true });
    },
    readOnly,
    textReadOnly: !textElementsAllowed,
    textDisabledReason,
    onRecordTimelineEdit: recordTimelineDrag,
    onPreviewTextTiming: previewTextTiming,
    visualBlocks: localVisualBlocks.map((block) => ({
      id: block.id,
      kind: block.kind,
      start_s: block.start_s,
      end_s: block.end_s,
      ...(block.kind === "media"
        ? {
            media_kind: block.media_kind,
            source_duration_s: block.source_duration_s,
            trim_start_s: block.trim_start_s,
            trim_end_s: block.trim_end_s,
            display_mode: block.display_mode,
            z: block.z,
          }
        : {}),
    })),
    showVisualBlocks:
      VISUAL_BLOCKS_UI_ENABLED && visualBlocksAllowed,
    visualBlocksReadOnly: !visualBlocksAllowed,
    visualBlocksDisabledReason,
    onPreviewVisualTiming: previewVisualTiming,
    motionBlocks: localMotionScenes.map((scene) => ({
      id: scene.id,
      label:
        scene.preset_id === "route_trace"
          ? "Route trace"
          : creatorBlockEntry(scene.preset_id).label,
      start_s: scene.start_frame / MOTION_FPS,
      end_s: scene.end_frame_exclusive / MOTION_FPS,
      sourceScene: scene,
      readOnly: scene.preset_id === "evolving_type" && !evolvingTypeExposureEnabled,
    })),
    showMotionBlocks:
      MOTION_SCENES_UI_ENABLED && motionScenesAllowed,
    motionBlocksReadOnly: !motionScenesAllowed,
    motionBlocksDisabledReason: motionScenesDisabledReason,
    onPreviewMotionTiming: previewMotionTiming,
    cameraEffects:
      capabilities?.camera_effects === false
        ? []
        : localCameraEffects.map((effect) => normalizeCameraEffect(effect)),
    onPreviewCameraTiming: previewCameraTiming,
    slots,
    clipReadOnly: clipEditingLocked,
    clipAddReadOnly: !clipAddAllowed,
    clipDisabledReason,
    clipAddDisabledReason,
    clipSourceDurations,
    onPreviewClipTiming: previewClipTiming,
    onPreviewSeek: seekPreviewToOutput,
    grid: clip.state.grid,
    clipPreviewMode: virtualPreviewActive ? "virtual" : "rendered",
    clipsLoading: clip.loadState === "loading",
    filmstripClips: clip.clips,
    allowRepeatedSources: guidedStoryV2,
    onAddClip: addClipToTimeline,
    carouselBlock: carouselMoment
      ? {
          id: CAROUSEL_SELECTION_ID,
          effectLabel: (carouselMoment.effect ?? "scale_sweep").replace(/_/g, " "),
          durationS: carouselMoment.duration_s ?? 6,
          position: carouselMoment.position ?? "middle",
        }
      : null,
    carouselReadOnly: !carouselCapable,
    carouselDisabledReason: carouselReason,
    onSelectCarousel: selectCarousel,
    onSetCarouselPosition: (position) => {
      if (!carouselMoment || !carouselCapable) {
        if (!carouselCapable) {
          notify(carouselReason ?? "Carousel isn't available for this edit.");
        }
        return;
      }
      stageCarouselMoment({ ...carouselMoment, position });
    },
    onPreviewCarouselDuration: (durationS) => {
      if (!carouselMoment || !carouselCapable) return;
      const resized = resizeCarouselTiming(
        carouselMoment,
        durationS,
        carouselClips.map((clip) => clip.clipIndex),
      );
      applyCarouselMoment(resized);
      return resized.duration_s;
    },
    sfx: localSfx.map((p) => {
      const trimStart = p.trim_start_s ?? 0;
      const trimEnd = p.trim_end_s ?? p.duration_s ?? null;
      return {
        id: p.id,
        at_s: p.at_s ?? 0,
        end_s:
          trimEnd == null
            ? null
            : (p.at_s ?? 0) + Math.max(0, trimEnd - trimStart),
        label: p.label ?? null,
      };
    }),
    sfxReadOnly: !sfxAllowed,
    sfxDisabledReason,
    onPreviewSfxTiming: previewSfxTiming,
    hasMusic: hasPlayableMusic,
    musicLabel: effectiveMusicTitle,
    soundLaneTitle,
    soundBedLabel,
    soundBedTitle: isVoiceoverVariant
      ? "Balance this bed against your narration in the inspector"
      : "The song auto-fits your cut",
    videoMuted,
    onToggleVideoMute: () => {
      if (readOnly) return;
      history.record();
      setVideoMuted((m) => !m);
    },
    soundMuted,
    onToggleSoundMute: () => {
      if (readOnly) return;
      const nextMuted = !soundMuted;
      if (musicCan("level", capabilities?.mix !== false) && mixLevel != null) {
        patchMixLevel(nextMuted ? 0 : Math.max(mixLevel, variant.mix ?? 0.2));
      } else {
        history.record();
        setSoundMuted(nextMuted);
      }
    },
    overlays: localOverlays.map((o) => ({
      id: o.id,
      start_s: o.start_s,
      end_s: o.end_s,
      label: o.kind === "video" ? "Video" : "Image",
      media_kind: o.kind,
      source_duration_s: o.kind === "video" ? o.clip_duration_s : null,
      trim_start_s: o.kind === "video" ? o.clip_trim_start_s : null,
      trim_end_s: o.kind === "video" ? o.clip_trim_end_s : null,
      z: o.z,
      // Provenance until Save: accepted AI suggestions get the dashed ✦ bar.
      suggested: suggestedOverlayIds.has(o.id),
    })),
    overlaysReadOnly: !overlaysAllowed,
    overlaysDisabledReason,
    onPreviewOverlayTiming: previewOverlayTiming,
    onOpenSounds: () => setActiveTool("sounds"),
    onScrub: seekTo,
    onScrubStart: () => {
      pausePlayback();
    },
    flashIds: flashTimelineIds,
  };

  // ── Pocket editor derivations (light mode + NEXT_PUBLIC_MOBILE_EDITOR_ENABLED) ──
  const pocketStripSelection: StripSelection | null =
    !pocketActive || pocketSheetOpen || readOnly
      ? null
      : (() => {
          if (selection?.kind === "text" && selectedBar) {
            if (isCaptionBar(selectedBar)) {
              return {
                type: "caption" as const,
                onEditCue: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
                onAllCaptions: () =>
                  dispatchPocket({ type: "OPEN_TOOL", tool: "captions" }),
              };
            }
            return {
              type: "text" as const,
              onEdit: () => {
                dispatchPocket({ type: "CLOSE_SHEET" });
                requestAnimationFrame(() => {
                  document
                    .querySelector<HTMLElement>(
                      `[data-text-id="${selectedBar.id}"] [role="textbox"]`,
                    )
                    ?.focus({ preventScroll: true });
                });
              },
              onStyle: () => {
                setInspectorTab("presets");
                dispatchPocket({ type: "OPEN_INSPECTOR" });
              },
              onTiming: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onDelete: deleteSelected,
            };
          }
          if (selection?.kind === "overlay") {
            return {
              type: "overlay" as const,
              onEdit: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onTiming: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onDelete: deleteSelected,
            };
          }
          if (selection?.kind === "motion") {
            return {
              type: "motion" as const,
              onEdit: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onTiming: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onDelete: deleteSelected,
            };
          }
          if (selection?.kind === "carousel") {
            return {
              type: "carousel" as const,
              onEdit: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onDelete: deleteSelected,
              deleteDisabledReason:
                carouselCapable && carouselMoment !== null
                  ? null
                  : (carouselReason ?? "Add a Carousel before removing it."),
            };
          }
          if (selection?.kind === "clip") {
            return {
              type: "clip" as const,
              onAdjust: () => dispatchPocket({ type: "OPEN_INSPECTOR" }),
              onSplit: splitAtPlayhead,
              splitDisabledReason: canSplit
                ? null
                : (splitReason ?? "Move the playhead inside this clip to split."),
              muted: videoMuted,
              onToggleMute: () => {
                if (!introControlsEditable) return;
                history.record();
                setVideoMuted((m) => !m);
              },
              onDelete: deleteSelected,
              deleteDisabledReason: selectedClipDeleteDisabledReason,
            };
          }
          return null;
        })();
  // Captions render at the canvas bottom — flip their quick actions to the top.
  // Clip actions live with the direct-manipulation timeline below the preview.
  const pocketStripOnTop = pocketStripSelection?.type === "caption";
  const miniStripClipByIndex = new Map(
    clip.clips.map((source) => [source.clip_index, source]),
  );
  const miniStripSegments: MiniStripSegment[] = pocketActive
    ? virtualPreview.timeline.entries.flatMap((entry) => {
            if (entry.kind !== "clip") return [];
            const startS = entry.startS;
            const endS = entry.startS + entry.durationS;
            const hasMarks =
              visibleTextBars.some(
                (bar) => bar.start_s < endS && bar.end_s > startS,
              ) ||
              localMotionScenes.some(
                (scene) =>
                  scene.start_frame / MOTION_FPS < endS &&
                  scene.end_frame_exclusive / MOTION_FPS > startS,
              );
            const slot = slots[entry.slotIndex];
            const source = miniStripClipByIndex.get(entry.clipIndex);
            const trimReason = readOnly
              ? readOnlyReason
              : clipEditingLocked
                ? clipTimingDisabledReason
                : null;
            return [
              {
                id: entry.slotKey,
                startS,
                endS,
                hasMarks,
                sourceUrl: entry.sourceUrl,
                sourceId: entry.clipIndex,
                sourceStartS: entry.inS,
                sourceDurationS:
                  source?.duration_s ??
                  clipSourceDurations[entry.slotKey] ??
                  null,
                minDurationS: minimumClipDurationForSlot({
                  grid: clip.state.grid,
                  offsetBeats:
                    slotLayout.windows[entry.slotIndex]?.offsetBeats,
                }),
                label:
                  slot?.momentDescription ?? `Clip ${entry.slotIndex + 1}`,
                trimDisabledReason: trimReason,
              },
            ];
          })
    : [];
  const carouselMiniStripEntry = virtualPreview.timeline.entries.find(
    (entry) => entry.kind === "carousel",
  );
  const pocketTimelineLanes: MiniStripLane[] = (() => {
    const lanes: MiniStripLane[] = [];
    const textItems = canvasTextBars
      .filter((bar) => !isCaptionBar(bar))
      .map((bar) => ({
        id: bar.id,
        kind: "text" as const,
        startS: bar.start_s,
        endS: bar.end_s,
        label: bar.text.trim() || "Text",
        resizeDisabledReason: readOnly
          ? readOnlyReason
          : isLyricBar(bar)
            ? "Lyrics timing follows the song."
            : !textElementsAllowed
              ? (textDisabledReason ?? "Text timing is locked for this edit.")
              : null,
      }));
    const captionItems = canvasTextBars
      .filter((bar) => isCaptionBar(bar))
      .map((bar) => ({
        id: bar.id,
        kind: "text" as const,
        startS: bar.start_s,
        endS: bar.end_s,
        label: bar.text.trim() || "Caption",
        resizeDisabledReason: readOnly
          ? readOnlyReason
          : !textElementsAllowed
            ? (textDisabledReason ?? "Caption timing is locked for this edit.")
            : null,
      }));
    const visualItems = canvasVisualBlocks.map((block) => ({
      id: block.id,
      kind: "visual" as const,
      startS: block.start_s,
      endS: block.end_s,
      label:
        block.kind === "montage"
          ? "Montage"
          : block.kind === "text_card"
            ? "Text card"
            : block.media_kind === "video"
              ? "Video"
              : "Image",
      resizeDisabledReason: readOnly
        ? readOnlyReason
        : !visualBlocksAllowed
          ? (visualBlocksDisabledReason ?? "Visual timing is locked for this edit.")
          : null,
    }));
    const blockItems = [
      ...canvasMotionScenes.map((scene) => ({
        id: scene.id,
        kind: "motion" as const,
        startS: scene.start_frame / MOTION_FPS,
        endS: scene.end_frame_exclusive / MOTION_FPS,
        label:
          scene.preset_id === "route_trace"
            ? "Route trace"
            : creatorBlockEntry(scene.preset_id).label,
        resizeDisabledReason: readOnly
          ? readOnlyReason
          : !motionScenesAllowed ||
              (scene.preset_id === "evolving_type" &&
                !evolvingTypeExposureEnabled)
            ? (motionScenesDisabledReason ?? "Effect timing is locked for this edit.")
            : null,
      })),
      ...(carouselMiniStripEntry?.kind === "carousel"
        ? [
            {
              id: CAROUSEL_SELECTION_ID,
              kind: "carousel" as const,
              startS: carouselMiniStripEntry.startS,
              endS:
                carouselMiniStripEntry.startS +
                carouselMiniStripEntry.durationS,
              label: "Carousel",
              resizeDisabledReason: readOnly
                ? readOnlyReason
                : !carouselCapable
                  ? (carouselReason ?? "Carousel timing is locked for this edit.")
                  : null,
            },
          ]
        : []),
    ];
    const cameraItems = canvasCameraEffects.map((effect) => ({
      id: effect.id,
      kind: "camera" as const,
      startS: effect.start_s,
      endS: effect.end_s,
      label:
        effect.token === "semantic_crop_pulse" ? "Crop pulse" : "Camera effect",
      resizeDisabledReason: readOnly
        ? readOnlyReason
        : capabilities?.camera_effects === false
          ? "Camera effects are unavailable for this edit."
          : null,
    }));
    const sfxItems = canvasSfxPlacements.map((placement) => {
      const trimStartS = placement.trim_start_s ?? 0;
      const trimEndS =
        placement.trim_end_s ?? placement.duration_s ?? trimStartS + 0.6;
      return {
        id: placement.id,
        kind: "sfx" as const,
        startS: placement.at_s,
        endS: Math.min(
          previewDuration,
          placement.at_s + Math.max(0.1, trimEndS - trimStartS),
        ),
        label: placement.label?.trim() || "Sound effect",
        resizeDisabledReason: readOnly
          ? readOnlyReason
          : !sfxAllowed
            ? (sfxDisabledReason ?? "Sound effect timing is locked for this edit.")
            : placement.trim_end_s == null && placement.duration_s == null
              ? "This sound effect has no editable source duration."
              : null,
      };
    });
    const overlayItems = canvasOverlays.map((overlay) => ({
      id: overlay.id,
      kind: "overlay" as const,
      startS: overlay.start_s,
      endS: overlay.end_s,
      label: overlay.kind === "video" ? "Video overlay" : "Image overlay",
      resizeDisabledReason: readOnly
        ? readOnlyReason
        : !overlaysAllowed
          ? (overlaysDisabledReason ?? "Overlay timing is locked for this edit.")
          : null,
    }));

    if (textItems.length > 0) {
      lanes.push({ id: "text", label: "Text", items: textItems });
    }
    if (captionItems.length > 0) {
      lanes.push({ id: "captions", label: "Captions", items: captionItems });
    }
    if (visualItems.length > 0) {
      lanes.push({ id: "visuals", label: "Visuals", items: visualItems });
    }
    if (blockItems.length > 0) {
      lanes.push({ id: "blocks", label: "Blocks", items: blockItems });
    }
    if (cameraItems.length > 0) {
      lanes.push({ id: "camera", label: "Camera", items: cameraItems });
    }
    if (sfxItems.length > 0) {
      lanes.push({ id: "sfx", label: "Sound effects", items: sfxItems });
    }
    if (hasPlayableMusic) {
      lanes.push({
        id: "music",
        label: soundLaneTitle,
        items: [
          {
            id: "background",
            kind: "music",
            startS: 0,
            endS: previewDuration,
            label: soundBedLabel || "Music",
            resizable: false,
          },
        ],
      });
    }
    if (overlayItems.length > 0) {
      lanes.push({ id: "overlays", label: "Overlays", items: overlayItems });
    }
    return lanes;
  })();
  const previewPocketLaneTiming = (
    item: MiniStripLaneItem,
    patch: MiniStripLaneTimingPatch,
    handle: "left" | "right",
  ) => {
    if (item.kind === "music") return;
    const start_s = Math.max(
      0,
      outputToBaseTimeRef.current(patch.startS),
    );
    const end_s = Math.max(
      start_s + 0.1,
      outputToBaseTimeRef.current(patch.endS),
    );

    if (item.kind === "text") {
      const origin = state.bars.find((bar) => bar.id === item.id);
      if (origin) {
        previewTextTiming(item.id, { start_s, end_s }, handle, origin);
      }
    } else if (item.kind === "visual") {
      previewVisualTiming(item.id, { start_s, end_s });
    } else if (item.kind === "motion") {
      const scene = localMotionScenes.find((candidate) => candidate.id === item.id);
      if (scene) {
        previewMotionTiming(
          item.id,
          { start_s, end_s },
          {
            id: scene.id,
            label:
              scene.preset_id === "route_trace"
                ? "Route trace"
                : creatorBlockEntry(scene.preset_id).label,
            start_s: scene.start_frame / MOTION_FPS,
            end_s: scene.end_frame_exclusive / MOTION_FPS,
            sourceScene: scene,
          },
        );
      }
    } else if (item.kind === "carousel") {
      if (carouselMoment && carouselCapable) {
        applyCarouselMoment(
          resizeCarouselTiming(
            carouselMoment,
            Math.max(0.1, patch.endS - patch.startS),
            carouselClips.map((clip) => clip.clipIndex),
          ),
        );
      }
    } else if (item.kind === "sfx") {
      previewSfxTiming(item.id, { at_s: start_s, end_s });
    } else if (item.kind === "overlay") {
      previewOverlayTiming(item.id, { start_s, end_s });
    } else if (item.kind === "camera") {
      previewCameraTiming(item.id, { start_s, end_s });
    }
  };
  const pocketTransportSlot = pocketActive ? (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        variant="ink"
        size="icon"
        aria-label={playing ? "Pause video" : "Play video"}
        aria-pressed={playing}
        onClick={togglePlay}
        className="active:opacity-80"
      >
        {playing ? <PauseIcon className="h-5 w-5" /> : <PlayIcon className="h-5 w-5" />}
      </Button>
      <span className="text-[12px] tabular-nums text-[#3f3f46]">
        {formatTimecode(currentTime)}
      </span>
    </div>
  ) : null;
  const pocketInspectorTitle =
    selection?.kind === "text"
      ? selectedBar && isCaptionBar(selectedBar)
        ? "Edit caption"
        : "Edit text"
      : selection?.kind === "clip"
        ? "Edit clip"
        : selection?.kind === "overlay"
          ? "Edit overlay"
          : selection?.kind === "sfx"
            ? "Edit sound"
            : selection?.kind === "camera"
              ? "Edit effect"
              : selection?.kind === "carousel"
                ? "Edit carousel"
              : selection?.kind === "motion"
                ? "Edit block"
              : "Edit";

  return (
    <div
      // The site body is dark-themed (`bg-black text-white`, DESIGN.md §3)
      // for the marketing/landing routes; this fixed overlay is a LIGHT
      // surface (DESIGN.md editor rule — never `.dark`) and must reset the
      // inherited white text color here at the root, or every unstyled
      // icon/label/placeholder in the chrome below renders invisible
      // (white-on-white) instead of just picking up bg-background.
      className="fixed inset-0 z-50 grid overflow-hidden bg-background text-foreground"
      style={{
        // Without an explicit column track, the grid's implicit column sizes
        // to the max-content width of whichever row (e.g. the top bar's
        // copilot-save notice pill) demands the most space, letting the
        // whole overlay — and everything docked to its right, like the
        // mobile Save button — balloon past the viewport instead of
        // wrapping/truncating in place.
        gridTemplateColumns: "minmax(0, 1fr)",
        gridTemplateRows:
          layoutMode === "light"
            ? "56px minmax(0, 1fr) auto"
            : "56px minmax(0, 1fr) clamp(220px, 30dvh, 260px)",
        // Keep the full-screen pocket header below iOS status bars/notches.
        // `env()` resolves to zero on desktop and non-notched viewports.
        paddingTop: "env(safe-area-inset-top)",
        // Half detent is non-modal (§3): canvas + transport squeeze above the
        // sheet so playback stays visible and controllable while editing.
        ...(pocketSheetOpen && pocket.detent === "half"
          ? { paddingBottom: "54dvh" }
          : null),
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: FONT_FACES }} />

      {/* ── Top bar (plan §1) ── */}
      {layoutMode === "light" ? (
        <LightTopBar
          dirty={dirty}
          saving={saving}
          readOnly={readOnly}
          saveState={saveState}
          showCopilotNotice={showCopilotSaveNotice}
          onBack={requestLeave}
          onOpenNova={() => setActiveTool("nova")}
          onDismissCopilotNotice={() => {
            setCopilotSaveNoticeDismissed(true);
            try {
              window.localStorage.setItem(COPILOT_SAVE_NOTICE_KEY, "true");
            } catch {
              /* localStorage unavailable */
            }
          }}
          onSave={() => void handleSave()}
          orientationToggle={orientationToggle}
        />
      ) : (
        <header className="flex h-14 items-center border-b border-border bg-background px-4">
          <div className="flex flex-1 items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Back to the video page"
              onClick={requestLeave}
            >
              <ArrowLeftIcon className="h-4 w-4" />
            </Button>
            <Input
              type="text"
              value={title}
              onChange={(e) => {
                if (readOnly || capabilities?.intro_controls === false) return;
                  // Coalesce typing bursts into one undo step.
                  history.record("title");
                  setTitleDirty(true);
                  setTitle(e.target.value);
              }}
              readOnly={!introControlsEditable}
              placeholder="Untitled video"
              aria-label="Video title"
              className="h-9 w-[260px] border-transparent bg-transparent shadow-none hover:bg-muted focus-visible:border-input focus-visible:bg-background"
            />
          </div>

          {/* Center cluster — visually quiet; the active tool gets the muted chip */}
          <div className="flex items-center gap-2">
            <ToggleGroup
              type="single"
              value={canvasTool}
              onValueChange={(value) => {
                if (!value) return; // one tool always stays selected
                setCanvasTool(value as "select" | "pan");
              }}
              className="gap-0.5 rounded-md border border-border bg-background p-0.5"
            >
              <ToggleGroupItem value="select" size="sm" aria-label="Select" title="Select">
                <SelectCursorIcon />
              </ToggleGroupItem>
              <ToggleGroupItem
                value="pan"
                size="sm"
                aria-label="Pan — drag to move around the canvas when zoomed in"
                title={panEnabled ? "Pan — drag to move around the canvas when zoomed in" : "Zoom in to pan"}
                disabled={!panEnabled}
              >
                <PanHandIcon />
              </ToggleGroupItem>
            </ToggleGroup>
            {/* Undo/redo — unified document command stack (plan §7). */}
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="Undo"
              title="Undo (⌘Z)"
              disabled={!history.canUndo}
              onClick={history.undo}
            >
              <UndoIcon className="h-4 w-4" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="Redo"
              title="Redo (⇧⌘Z)"
              disabled={!history.canRedo}
              onClick={history.redo}
            >
              <RedoIcon className="h-4 w-4" />
            </Button>
            <Select value={String(zoomPct)} onValueChange={(v) => setZoomPct(Number(v))}>
              <SelectTrigger aria-label="Canvas zoom" className="h-9 w-[88px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ZOOM_OPTIONS.map((z) => (
                  <SelectItem key={z} value={String(z)}>
                    {z}%
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {orientationToggle}
          </div>

          <div className="flex flex-1 items-center justify-end gap-2">
            {activeGuidedTombstones.length > 0 && (
              <Button
                type="button"
                variant="outline"
                onClick={restoreGuidedTombstones}
                className="h-auto min-h-8 rounded-lg border-amber-300 bg-amber-50 px-3 text-[12px] text-amber-950 hover:border-amber-500 hover:bg-amber-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                title="These anchored items were removed because their complete clip interval disappeared."
              >
                {activeGuidedTombstones.length} removed · Restore
              </Button>
            )}
            {showCopilotSaveNotice && (
              <div className="flex max-w-[360px] items-center gap-2 rounded-md border border-border bg-background px-3 py-1.5 text-[12px] text-muted-foreground">
                <span className="truncate">
                  Staged edits are previewed here. Save to render the new video exactly.
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Dismiss preview match note"
                  onClick={() => {
                    setCopilotSaveNoticeDismissed(true);
                    try {
                      window.localStorage.setItem(COPILOT_SAVE_NOTICE_KEY, "true");
                    } catch {
                      /* localStorage unavailable */
                    }
                  }}
                  className="h-8 w-8 text-muted-foreground hover:text-foreground"
                >
                  ✕
                </Button>
              </div>
            )}
            {saveState === "idle" && saveMessage && (
              <Badge variant="outline" className="max-w-[280px] truncate font-normal">
                {saveMessage}
              </Badge>
            )}
            {(lyricsDirty || orientationDirty) && (
              <Badge variant="outline">Re-renders on Save</Badge>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="focus-visible:!outline-lime-500"
              onClick={requestLeave}
            >
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              className="gap-2 focus-visible:!outline-lime-500"
              disabled={!dirty || saving || readOnly}
              onClick={() => void handleSave()}
            >
              {saving && <SaveSpinner />}
              {saving ? "Saving" : "Save"}
            </Button>
          </div>
        </header>
      )}

      {renderedMusicPreviewActive && virtualMusicAudioUrl && (
        <audio
          ref={renderedMusicAudioRef}
          data-testid="rendered-music-window-preview"
          src={virtualMusicAudioUrl}
          preload="auto"
          className="hidden"
          onError={handleVirtualMusicError}
          onLoadedMetadata={(event) => {
            const audio = event.currentTarget;
            const video = videoRef.current;
            if (!video) return;
            audio.volume = effectiveMusicTrackId == null ? backgroundMusicGainLinear : 1;
            const target = Math.max(0, virtualMusicStartS + video.currentTime);
            audio.currentTime =
              Number.isFinite(audio.duration) && audio.duration > 0
                ? Math.min(target, Math.max(0, audio.duration - 0.01))
                : target;
            if (!video.paused) void audio.play().catch(() => {});
          }}
        />
      )}

      {/* ── Middle row: rail · drawer · canvas · inspector · edge rail ── */}
      {layoutMode === "light" ? (
        <div className="relative min-h-0">
          <EditorCanvas
            variant={variant}
            elements={canvasPreviewElements}
            bars={canvasTextBars}
            captionsEnabled={captionMeta?.enabled}
            visualBlocks={canvasVisualBlocks}
            motionScenes={canvasMotionScenes}
            motionRuntimeHash={motionPreviewRuntimeHash}
            cameraEffects={canvasCameraEffects}
            visualAssets={poolAssets}
            visualPreviewUrls={localVisualPreviewUrls}
            mediaOverlays={canvasOverlays}
            overlayPreviewUrls={localOverlayPreviewUrls}
            suggestedOverlayIds={suggestedOverlayIds}
            sfxPlacements={canvasSfxPlacements}
            sfxAudioUrls={localSfxAudioUrls}
            selectedTextId={selection?.kind === "text" ? selection.id : null}
            selectedOverlayId={selection?.kind === "overlay" ? selection.id : null}
            selectedVisualBlockId={selection?.kind === "visual" ? selection.id : null}
            flashTextIds={flashTextIds}
            flashOverlayIds={flashOverlayIds}
            currentTime={currentTime}
            playbackClock={playbackClock}
            lookPreset="none"
            virtualDeckLookPresets={virtualDeckLookPresets}
            virtualDeckLookAdjustments={virtualDeckLookAdjustments}
            playing={playing}
            masonryDurationS={previewDuration}
            zoomPct={100}
            tool="select"
            videoRef={videoRef}
            onSelectText={selectText}
            onSelectOverlay={(id) => selectElement("overlay", id)}
            onSelectVisualBlock={(id) => selectElement("visual", id)}
            captionTapSelect={POCKET_UI}
            onClearSelection={() => {
              clear();
              setLightSheetOpen(false);
            }}
            onPatchBar={patchBar}
            onEditText={POCKET_UI ? editTextBar : undefined}
            inlineTextEditing={POCKET_UI}
            textEditable={POCKET_UI && !readOnly && textElementsAllowed}
            onPatchOverlay={POCKET_UI ? patchOverlay : undefined}
            onPreviewVisualBlock={POCKET_UI ? previewVisualMediaBlock : undefined}
            onPatchVisualBlock={POCKET_UI ? commitVisualMediaBlock : undefined}
            onRecordVisualBlock={POCKET_UI ? recordVisualMediaBlock : undefined}
            onFocusContent={() => {
              if (POCKET_UI) {
                dispatchPocket({ type: "OPEN_INSPECTOR" });
                focusContent();
              } else {
                setLightSheetOpen(true);
              }
            }}
            onTimeUpdate={commitPlaybackTime}
            onDuration={setDuration}
            onPlayingChange={setPlaying}
            onReloadSource={() => setLoadNonce((n) => n + 1)}
            virtualPreview={virtualPreviewActive ? virtualPreview : null}
            carouselMoment={carouselMoment}
            carouselClips={carouselClips.map((entry) => ({
              clip_index: entry.clipIndex,
              signed_url: entry.signedUrl,
            }))}
            allowManipulation={POCKET_UI ? !readOnly : false}
            stageHeightCss={
              POCKET_UI
                ? pocketSheetOpen && pocket.detent === "half"
                  ? "46dvh - 128px"
                  : pocketStripSelection?.type === "clip"
                    ? "100dvh - 398px"
                    : "100dvh - 350px"
                : "100dvh - 152px"
            }
            canvas={activeCanvas}
          />
          {pocketActive && !pocketSheetOpen && history.canUndo && (
            <Button
              type="button"
              variant="outline"
              size="icon"
              aria-label="Undo"
              onClick={history.undo}
              className="absolute left-3 top-3 z-20 bg-white/90 text-[#3f3f46] shadow-sm active:opacity-80"
            >
              <UndoIcon className="h-5 w-5" />
            </Button>
          )}
          {pocketActive && !pocketSheetOpen && history.canRedo && (
            <Button
              type="button"
              variant="outline"
              size="icon"
              aria-label="Redo"
              onClick={history.redo}
              className="absolute left-[60px] top-3 z-20 bg-white/90 text-[#3f3f46] shadow-sm active:opacity-80"
            >
              <RedoIcon className="h-5 w-5" />
            </Button>
          )}
          {pocketStripSelection && pocketStripSelection.type !== "clip" && (
            <div
              className={`absolute left-1/2 z-20 -translate-x-1/2 ${
                pocketStripOnTop ? "top-3" : "bottom-3"
              }`}
            >
              <ContextStrip
                selection={pocketStripSelection}
                onDisabledTap={notify}
              />
            </div>
          )}
          {visibleTextBars.length === 0 && !readOnly && !textElementsLocked && (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => addTextAtPlayhead()}
              className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-white shadow-[0_8px_24px_rgba(12,12,14,0.18)] ring-1 ring-zinc-200 hover:bg-zinc-50"
            >
              Add text
            </Button>
          )}
        </div>
      ) : (
        <div
          className={[
            "relative grid min-h-0 grid-rows-[minmax(0,1fr)] overflow-hidden",
            layoutMode === "full"
              ? "grid-cols-[auto_auto_1fr_auto]"
              : "grid-cols-[auto_1fr_auto]",
          ].join(" ")}
        >
        <ToolRail
          activeTool={activeTool}
          disabledTools={railDisabledReasons}
          onToggleTool={(tool) => setActiveTool((cur) => (cur === tool ? null : tool))}
        />
        {layoutMode === "full" &&
          (activeTool !== null ? (
            <ToolDrawer
              tool={activeTool}
              captions={captionsControl}
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              lyricsToggle={lyricsToggle}
              onSplitPlaceText={splitAndPlaceText}
              splitSmartPlaceAvailable={!readOnly && !textElementsLocked}
              onSmartPlaceAll={applySmartPlacement}
              smartPlaceAllAvailable={smartPlaceAllAvailable}
              onPickPreset={pickPreset}
              appliedStyleSetId={appliedStyleSetId}
              onRestyleAll={textStyleHandler}
              availableLookPresets={editWideLookPresets}
              selectedLookPreset={selectedEditWideLookPreset}
              lookPresetMixed={editWideLookPresetMixed}
              lookPreviewUrl={editWideLookPreviewUrl}
              onSelectLook={applyEditWideLook}
              sfxEffects={sfxGlossaryEffects}
              sfxLoading={sfxGlossaryLoading}
              onAddSfx={addSfxFromGlossary}
              musicTracks={musicTracks}
              musicLoading={musicTracksLoading}
              currentMusicTrackId={effectiveAudioTrackId}
              musicEditable={musicSwapEditable}
              musicRemoveEditable={musicRemoveEditable}
              musicRemoveDisabledReason={musicRemoveDisabledReason}
              onPickMusic={pickMusicTrack}
              onRemoveMusic={removeMusic}
              musicWindow={musicWindowControl}
              overlayUploading={overlayUploading}
              onOverlayUpload={handleOverlayUpload}
              overlaySuggestions={overlaySuggestionsNode}
              visualBlocks={localVisualBlocks}
              motionScenes={localMotionScenes}
              selectedMotionId={selection?.kind === "motion" ? selection.id : null}
              motionAvailable={motionScenesAllowed}
              motionRuntimeCompatible={motionRuntimeCompatible}
              evolvingTypeEnabled={evolvingTypeExposureEnabled}
              onAddMotion={addMotionScene}
              onSelectMotion={(id) => selectElement("motion", id)}
              visualAssets={poolAssets}
              visualTextElements={state.bars}
              visualUploading={poolUploader.busy}
              visualUploadDisabled={poolAtCapacity}
              visualUploadFeedback={visualUploadFeedback}
              onVisualUpload={handlePoolFiles}
              onAddMontage={addMontageBlock}
              onAddMediaBlock={(ids, mode) => addMediaVisualBlocks(ids, mode)}
              onAddMediaSequence={(ids) => addMediaVisualBlocks(ids, "fullscreen", true)}
              mediaSequenceAfterSelection={selection?.kind === "visual"}
              onAddTextCard={addTextCard}
              onAddVisualBlockText={addVisualBlockText}
              onSelectVisualBlockText={selectText}
              onSaveVisualAssetContext={handleSavePoolAssetContext}
              onPatchVisualBlock={patchVisualBlock}
              onDuplicateVisualBlock={duplicateVisualBlock}
              onDeleteVisualBlock={deleteVisualBlock}
              onRetimeVisualBlock={retimeBlock}
              carousel={carouselControl}
              carouselSelected={selection?.kind === "carousel"}
              onSelectCarousel={selectCarousel}
              layoutMode={layoutMode}
              copilot={{
                messages: copilot.messages,
                sending: copilot.sending,
                queued: copilot.queued,
                error: copilot.error,
                unavailable: copilot.unavailable,
                restoredInput: copilot.restoredInput,
                suggestions: copilot.suggestions,
                historyVersion: history.version,
                canUndo: history.canUndo,
                onSend: (text) => void copilot.send(text),
                onCancelQueued: copilot.cancelQueued,
                onEditQueued: copilot.editQueued,
                onStop: copilot.stop,
                onUndo: history.undo,
                onClearRestoredInput: copilot.clearRestoredInput,
                director,
                renderTurnActive: copilotRenderTurnActive,
                renderTurnSteps: copilotRenderSteps,
              }}
              onClose={() => setActiveTool(null)}
            />
          ) : (
            <div />
          ))}
        {layoutMode === "overlay" && activeTool !== null && activeTool !== "nova" && (
          <div className="absolute bottom-0 left-[92px] top-0 z-40 shadow-[18px_0_36px_rgba(12,12,14,0.16)]">
            <ToolDrawer
              tool={activeTool}
              captions={captionsControl}
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              lyricsToggle={lyricsToggle}
              onSplitPlaceText={splitAndPlaceText}
              splitSmartPlaceAvailable={!readOnly && !textElementsLocked}
              onSmartPlaceAll={applySmartPlacement}
              smartPlaceAllAvailable={smartPlaceAllAvailable}
              onPickPreset={pickPreset}
	              appliedStyleSetId={appliedStyleSetId}
	              onRestyleAll={textStyleHandler}
	              availableLookPresets={editWideLookPresets}
	              selectedLookPreset={selectedEditWideLookPreset}
	              lookPresetMixed={editWideLookPresetMixed}
	              lookPreviewUrl={editWideLookPreviewUrl}
	              onSelectLook={applyEditWideLook}
	              sfxEffects={sfxGlossaryEffects}
	              sfxLoading={sfxGlossaryLoading}
	              onAddSfx={addSfxFromGlossary}
              musicTracks={musicTracks}
              musicLoading={musicTracksLoading}
              currentMusicTrackId={effectiveAudioTrackId}
              musicEditable={musicSwapEditable}
              musicRemoveEditable={musicRemoveEditable}
              musicRemoveDisabledReason={musicRemoveDisabledReason}
              onPickMusic={pickMusicTrack}
              onRemoveMusic={removeMusic}
              musicWindow={musicWindowControl}
              overlayUploading={overlayUploading}
	              onOverlayUpload={handleOverlayUpload}
	              overlaySuggestions={overlaySuggestionsNode}
              visualBlocks={localVisualBlocks}
              motionScenes={localMotionScenes}
              selectedMotionId={selection?.kind === "motion" ? selection.id : null}
              motionAvailable={motionScenesAllowed}
              motionRuntimeCompatible={motionRuntimeCompatible}
              evolvingTypeEnabled={evolvingTypeExposureEnabled}
              onAddMotion={addMotionScene}
              onSelectMotion={(id) => selectElement("motion", id)}
              visualAssets={poolAssets}
              visualTextElements={state.bars}
              visualUploading={poolUploader.busy}
              visualUploadDisabled={poolAtCapacity}
              visualUploadFeedback={visualUploadFeedback}
              onVisualUpload={handlePoolFiles}
              onAddMontage={addMontageBlock}
              onAddMediaBlock={(ids, mode) => addMediaVisualBlocks(ids, mode)}
              onAddMediaSequence={(ids) => addMediaVisualBlocks(ids, "fullscreen", true)}
              mediaSequenceAfterSelection={selection?.kind === "visual"}
              onAddTextCard={addTextCard}
              onAddVisualBlockText={addVisualBlockText}
              onSelectVisualBlockText={selectText}
              onSaveVisualAssetContext={handleSavePoolAssetContext}
              onPatchVisualBlock={patchVisualBlock}
              onDuplicateVisualBlock={duplicateVisualBlock}
              onDeleteVisualBlock={deleteVisualBlock}
              onRetimeVisualBlock={retimeBlock}
              carousel={carouselControl}
              carouselSelected={selection?.kind === "carousel"}
              onSelectCarousel={selectCarousel}
              layoutMode={layoutMode}
              onClose={() => setActiveTool(null)}
            />
          </div>
        )}
        {layoutMode === "overlay" && activeTool === "nova" && (
          <div className="absolute bottom-4 left-[108px] right-[272px] z-40">
            <ToolDrawer
              tool="nova"
              sampleWord={sampleWord}
              appliedPresetId={appliedPresetId}
              onAddText={() => addTextAtPlayhead()}
              lyricsToggle={lyricsToggle}
              onPickPreset={pickPreset}
              layoutMode={layoutMode}
              copilot={{
                messages: copilot.messages,
                sending: copilot.sending,
                queued: copilot.queued,
                error: copilot.error,
                unavailable: copilot.unavailable,
                restoredInput: copilot.restoredInput,
                suggestions: copilot.suggestions,
                historyVersion: history.version,
                canUndo: history.canUndo,
                onSend: (text) => void copilot.send(text),
                onCancelQueued: copilot.cancelQueued,
                onEditQueued: copilot.editQueued,
                onStop: copilot.stop,
                onUndo: history.undo,
                onClearRestoredInput: copilot.clearRestoredInput,
                director,
                renderTurnActive: copilotRenderTurnActive,
                renderTurnSteps: copilotRenderSteps,
              }}
              onClose={() => setActiveTool(null)}
            />
          </div>
        )}
        <div
          data-region="canvas-cell"
          className="flex min-h-0 min-w-0 items-center justify-center overflow-hidden"
        >
          <EditorCanvas
            variant={variant}
            elements={canvasPreviewElements}
            bars={canvasTextBars}
            captionsEnabled={captionMeta?.enabled}
            visualBlocks={canvasVisualBlocks}
            motionScenes={canvasMotionScenes}
            motionRuntimeHash={motionPreviewRuntimeHash}
            cameraEffects={canvasCameraEffects}
            visualAssets={poolAssets}
            visualPreviewUrls={localVisualPreviewUrls}
            mediaOverlays={canvasOverlays}
            overlayPreviewUrls={localOverlayPreviewUrls}
            suggestedOverlayIds={suggestedOverlayIds}
            sfxPlacements={canvasSfxPlacements}
            sfxAudioUrls={localSfxAudioUrls}
            selectedTextId={selection?.kind === "text" ? selection.id : null}
            selectedOverlayId={selection?.kind === "overlay" ? selection.id : null}
            selectedVisualBlockId={selection?.kind === "visual" ? selection.id : null}
            flashTextIds={flashTextIds}
            flashOverlayIds={flashOverlayIds}
            currentTime={currentTime}
            playbackClock={playbackClock}
            lookPreset="none"
            virtualDeckLookPresets={virtualDeckLookPresets}
            virtualDeckLookAdjustments={virtualDeckLookAdjustments}
            playing={playing}
            masonryDurationS={previewDuration}
            zoomPct={zoomPct}
            tool={canvasTool}
            videoRef={videoRef}
            onSelectText={selectText}
            onSelectOverlay={(id) => selectElement("overlay", id)}
            onSelectVisualBlock={(id) => selectElement("visual", id)}
            onClearSelection={clear}
            onPatchBar={patchBar}
            onPatchOverlay={patchOverlay}
            onPreviewVisualBlock={previewVisualMediaBlock}
            onPatchVisualBlock={commitVisualMediaBlock}
            onRecordVisualBlock={recordVisualMediaBlock}
            onFocusContent={focusContent}
            onTimeUpdate={commitPlaybackTime}
            onDuration={setDuration}
            onPlayingChange={setPlaying}
            onReloadSource={() => setLoadNonce((n) => n + 1)}
            virtualPreview={virtualPreviewActive ? virtualPreview : null}
            carouselMoment={carouselMoment}
            carouselClips={carouselClips.map((entry) => ({
              clip_index: entry.clipIndex,
              signed_url: entry.signedUrl,
            }))}
            canvas={activeCanvas}
          />
        </div>
        <InspectorPanel
          selection={selection}
          bar={selectedBar}
          clipTiming={selectedClip}
          sfx={selectedSfx}
          overlay={selectedOverlay}
          visualBlock={selectedVisualBlock?.kind === "media" ? selectedVisualBlock : null}
          motionScene={selectedMotionScene}
          motionDurationS={timelineDuration}
          motionAssets={poolAssets}
          evolvingTypeEnabled={evolvingTypeExposureEnabled}
          motionEditable={motionScenesAllowed}
          motionDisabledReason={motionScenesDisabledReason}
          cameraEffect={selectedCameraEffect}
          carousel={carouselInspectorControl}
          tab={inspectorTab}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          contentRef={contentRef}
          onEditText={editSelectedText}
          onPatch={(patch) => {
            if (selectedBar) patchBar(selectedBar.id, patch);
          }}
          onPreviewTextMotion={previewSelectedTextMotion}
          onBeginTextMotion={beginTextMotionGesture}
          onCommitTextMotion={commitSelectedTextMotion}
          onSetTextBoxPosition={setSelectedTextBoxPosition}
          boxPositionXFrac={selectedTextBoxScreenXFrac}
          onPatchTextTiming={patchSelectedTextTiming}
          textEditable={textElementsAllowed || (!!selectedBar && isCaptionBar(selectedBar))}
          textDisabledReason={textDisabledReason}
          onPatchClipTiming={patchSelectedClipTiming}
          onPatchClipLook={patchSelectedClipLook}
          onPatchClipTransition={patchSelectedClipTransition}
          onMoveClip={moveSelectedClip}
          clipReorderEditable={clipReorderAllowed}
          clipTimingEditable={clipCan("trim", true)}
          clipLooksEditable={clipLooksAllowed}
          clipTransitionsEditable={clipTransitionsAllowed}
          clipTimingDisabledReason={clipTimingDisabledReason}
          clipReorderDisabledReason={clipReorderDisabledReason}
          clipLooksDisabledReason={clipLooksDisabledReason}
          clipTransitionsDisabledReason={clipTransitionsDisabledReason}
          availableLookPresets={perClipLookPresets}
          onPatchClipLookAdjustments={patchSelectedClipLookAdjustments}
          onRecordClipLookAdjustments={recordSelectedClipLookAdjustment}
          onPreviewClipTiming={previewSelectedClipTiming}
          onRecordClipTiming={recordTimelineDrag}
          onPatchSfx={patchSfx}
          onDeleteSfx={removeSfx}
          sfxEditable={sfxAllowed}
          sfxDisabledReason={sfxDisabledReason}
          onPatchOverlay={patchOverlay}
          onPatchVisualBlock={patchVisualBlock}
          onReorderVisualBlock={reorderVisualMediaBlock}
          onPreviewOverlay={previewOverlayPatch}
          onRecordOverlay={recordTimelineDrag}
          onDeleteOverlay={removeOverlay}
          overlayEditable={overlaysAllowed}
          overlayDisabledReason={overlaysDisabledReason}
          onPatchMotion={patchMotionScene}
          onPatchMotionControl={patchMotionControl}
          onBeginMotionControl={beginMotionControl}
          onPreviewMotionControl={previewMotionControl}
          onCommitMotionControl={commitMotionControl}
          onCancelMotionControl={cancelMotionControl}
          onRemoveMotion={removeMotionScene}
          onPatchCameraEffect={patchCameraEffect}
          onDeleteCameraEffect={deleteCameraEffect}
          mixLevel={mixLevel}
          mixEditable={musicLevelEditable && mixLevel != null}
          mixDisabledReason={musicLevelDisabledReason}
          mixLabel={soundBedLabel}
          musicTracks={musicTracks}
          musicLoading={musicTracksLoading}
          currentMusicTrackId={effectiveAudioTrackId}
          musicEditable={musicSwapEditable}
          musicRemoveEditable={musicRemoveEditable}
          musicSwapDisabledReason={musicSwapDisabledReason}
          musicRemoveDisabledReason={musicRemoveDisabledReason}
          backgroundMusic={backgroundMusic}
          backgroundMusicTrackDurationS={backgroundMusicTrackDurationS}
          onPickMusic={pickMusicTrack}
          onRemoveMusic={removeMusic}
          onPatchBackgroundMusic={patchBackgroundMusic}
          onRemoveBackgroundMusic={removeBackgroundMusic}
          musicWindow={musicWindowControl}
          onPatchMix={patchMixLevel}
          smartPlaceAvailable={
            !!selectedBar && !readOnly && (isMasonryVariant(variant) || !!smartPlacementCandidate)
          }
          onSmartPlace={applySelectedSmartPlacement}
          onMergeCaptionCue={mergeCaptionCue}
          onOpenCaptionsPanel={
            captionsControl ? () => setActiveTool("captions") : undefined
          }
          captionsPanelOpen={activeTool === "captions"}
          canMergeCaptionPrev={!readOnly && captionMergeAvailability.canMergePrev}
          canMergeCaptionNext={!readOnly && captionMergeAvailability.canMergeNext}
          onClose={clear}
          onPickPreset={pickPreset}
          onTab={setInspectorTab}
        />
      </div>
      )}

      {/* ── Timeline region (260px): TransportBar + scale-driven editor
             timeline (Text → Video → Sound → Overlays), plan §6. ── */}
      {layoutMode === "light" ? (
        POCKET_UI ? (
          <div className="relative flex min-h-0 flex-col">
            <LightTransport
              playing={playing}
              currentTime={currentTime}
              duration={previewDuration}
              onPlayPause={togglePlay}
              onScrub={seekTo}
              compact
            />
            {!pocketSheetOpen && miniStripSegments.length > 0 && (
              <div className="bg-white">
                <MiniStrip
                  segments={miniStripSegments}
                  durationS={virtualPreview.timeline.totalDurationS || timelineDuration || previewDuration}
                  currentTimeS={currentTime}
                  playbackClock={playbackClock}
                  selectedClipId={selection?.kind === "clip" ? selection.id : null}
                  lanes={pocketTimelineLanes}
                  selectedLaneItem={
                    selection && selection.kind !== "clip"
                      ? { kind: selection.kind, id: selection.id }
                      : null
                  }
                  onScrubStart={pausePlayback}
                  onScrub={seekTo}
                  onTrimStart={recordTimelineDrag}
                  onPreviewTrim={(id, patch) => {
                    if (selectedClip?.slot.key !== id) return;
                    previewSelectedClipTiming(patch);
                  }}
                  onDisabledTap={notify}
                  onLaneResizeStart={recordTimelineDrag}
                  onPreviewLaneTiming={previewPocketLaneTiming}
                  onSelectClip={(id, seconds) => {
                    selectElement("clip", id);
                    seekTo(seconds);
                  }}
                  onSelectLaneItem={(item, seconds) => {
                    if (item.kind === "carousel") selectCarousel();
                    else selectElement(item.kind, item.id);
                    seekTo(seconds);
                  }}
                />
              </div>
            )}
            {!pocketSheetOpen && pocketStripSelection?.type === "clip" && (
              <ContextStrip
                selection={pocketStripSelection}
                onDisabledTap={notify}
                className="border-t border-border bg-background px-2 py-1"
              />
            )}
            {!pocketSheetOpen && (
              <ToolDock
                activeTool={
                  activeTool === "nova"
                    ? "nova"
                    : pocket.sheet?.kind === "tool"
                      ? pocket.sheet.tool
                      : null
                }
                disabledTools={railDisabledReasons}
                novaEnabled={process.env.NEXT_PUBLIC_EDIT_COPILOT_ENABLED === "true"}
                onToggleTool={(tool: DockTool) => {
                  if (tool === "nova") {
                    dispatchPocket({ type: "CLOSE_SHEET" });
                    setActiveTool((cur) => (cur === "nova" ? null : "nova"));
                  } else if (tool === "text") {
                    addPocketTextAtPlayhead();
                  } else {
                    setActiveTool(null);
                    dispatchPocket({ type: "TOGGLE_TOOL", tool });
                  }
                }}
                onDisabledTap={notify}
              />
            )}
          </div>
        ) : (
          <LightTransport
            playing={playing}
            currentTime={currentTime}
            duration={previewDuration}
            onPlayPause={togglePlay}
            onScrub={seekTo}
          />
        )
      ) : (
      <div
        data-region="timeline"
        className="relative flex min-h-0 flex-col border-t border-zinc-200 bg-white"
      >
        <TransportBar
          playing={playing}
          currentTime={currentTime}
          duration={previewDuration}
          onPlayPause={togglePlay}
          canSplit={canSplit}
          splitReason={splitReason}
          onSplit={splitAtPlayhead}
          canDelete={canDelete}
          onDelete={deleteSelected}
          zoom={zoom}
          onZoom={setZoom}
          onFit={() => {
            setZoom(1);
            setTimelineFitRequestKey((key) => key + 1);
          }}
          clipTimingDirty={clipDirty}
          clipPreviewMode={virtualPreviewActive ? "virtual" : "rendered"}
          clipPreviewHint={clipPreviewHint}
        />
        <div className="min-h-0 flex-1">
          <UnifiedTimeline
            totalDurationS={timelineDuration}
            currentTimeS={currentTime}
            // Item-page-only props — unused in editor mode (UnifiedTimeline
            // early-returns on `editorMode`); passed as inert defaults so the
            // shared component's required contract stays satisfied.
            sfxPlacements={[]}
            sfxGlossaryEffects={[]}
            sfxGlossaryLoading={false}
            sfxRendering={false}
            sfxUploading={false}
            onSfxChange={() => {}}
            onSfxUploadRequest={async () => {}}
            overlayCards={localOverlays}
            overlaysEnabled={overlaysAllowed && !readOnly}
            overlayUploading={overlayUploading}
            localPreviewUrls={localOverlayPreviewUrls}
            onOverlayUploadRequest={handleOverlayUpload}
            onUpdateCard={patchOverlay}
            onRemoveCard={removeOverlay}
            onClearOverlays={() => {
              if (readOnly || !overlaysAllowed) return;
              history.record();
              setLocalOverlays([]);
              setLocalOverlayPreviewUrls((current) => {
                Object.values(current).forEach(revokeLocalObjectUrl);
                return {};
              });
              setOverlaysDirty(true);
              clear();
            }}
            editorMode={editorModeProps}
          />
        </div>
      </div>
      )}

      <LightEditSheet
        open={layoutMode === "light" && !POCKET_UI && lightSheetOpen && !!selectedBar}
        bar={selectedBar}
        sampleWord={sampleWord}
        appliedPresetId={appliedPresetId}
        saveState={saveState}
        saving={saving}
        dirty={dirty}
        readOnly={readOnly}
        onClose={() => setLightSheetOpen(false)}
        onEditText={editSelectedText}
        onPickPreset={pickPreset}
        onSave={() => void handleSave()}
      />

      {layoutMode === "light" && activeTool === "nova" && (
        <ToolDrawer
          tool="nova"
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onAddText={() => addTextAtPlayhead()}
          lyricsToggle={lyricsToggle}
          onPickPreset={pickPreset}
          layoutMode={layoutMode}
          copilot={{
            messages: copilot.messages,
            sending: copilot.sending,
            queued: copilot.queued,
            error: copilot.error,
            unavailable: copilot.unavailable,
            restoredInput: copilot.restoredInput,
            suggestions: copilot.suggestions,
            historyVersion: history.version,
            canUndo: history.canUndo,
            onSend: (text) => void copilot.send(text),
            onCancelQueued: copilot.cancelQueued,
            onEditQueued: copilot.editQueued,
            onStop: copilot.stop,
            onUndo: history.undo,
            onClearRestoredInput: copilot.clearRestoredInput,
            director,
            renderTurnActive: copilotRenderTurnActive,
            renderTurnSteps: copilotRenderSteps,
          }}
          onClose={() => setActiveTool(null)}
        />
      )}

      {/* ── Pocket tool sheet: the whole ToolDrawer hosted in the Sheet
             primitive (presentation="sheet" drops its desktop wrapper). ── */}
      {pocketActive && pocket.sheet?.kind === "tool" && (
        <Sheet
          open
          title={POCKET_TOOL_TITLES[pocket.sheet.tool]}
          detent={pocket.detent}
          onDetentChange={(detent) => dispatchPocket({ type: "SET_DETENT", detent })}
          onClose={() => dispatchPocket({ type: "CLOSE_SHEET" })}
          transportSlot={pocketTransportSlot}
        >
          <ToolDrawer
            tool={pocket.sheet.tool}
            presentation="sheet"
            captions={captionsControl}
            sampleWord={sampleWord}
            appliedPresetId={appliedPresetId}
            onAddText={() => addTextAtPlayhead()}
            lyricsToggle={lyricsToggle}
            onSplitPlaceText={splitAndPlaceText}
            splitSmartPlaceAvailable={!readOnly && !textElementsLocked}
            onSmartPlaceAll={applySmartPlacement}
            smartPlaceAllAvailable={smartPlaceAllAvailable}
            onPickPreset={pickPreset}
            appliedStyleSetId={appliedStyleSetId}
            onRestyleAll={textStyleHandler}
            availableLookPresets={editWideLookPresets}
            selectedLookPreset={selectedEditWideLookPreset}
            lookPresetMixed={editWideLookPresetMixed}
            lookPreviewUrl={editWideLookPreviewUrl}
            onSelectLook={applyEditWideLook}
            sfxEffects={sfxGlossaryEffects}
            sfxLoading={sfxGlossaryLoading}
            onAddSfx={addSfxFromGlossary}
            musicTracks={musicTracks}
            musicLoading={musicTracksLoading}
            currentMusicTrackId={effectiveAudioTrackId}
            musicEditable={musicSwapEditable}
            musicRemoveEditable={musicRemoveEditable}
            musicRemoveDisabledReason={musicRemoveDisabledReason}
            onPickMusic={pickMusicTrack}
            onRemoveMusic={removeMusic}
            musicWindow={musicWindowControl}
            overlayUploading={overlayUploading}
            onOverlayUpload={handleOverlayUpload}
            overlaySuggestions={overlaySuggestionsNode}
            visualBlocks={localVisualBlocks}
            motionScenes={localMotionScenes}
            selectedMotionId={selection?.kind === "motion" ? selection.id : null}
            motionAvailable={motionScenesAllowed}
            motionRuntimeCompatible={motionRuntimeCompatible}
            evolvingTypeEnabled={evolvingTypeExposureEnabled}
            onAddMotion={addMotionScene}
            onSelectMotion={(id) => selectElement("motion", id)}
            visualAssets={poolAssets}
            visualTextElements={state.bars}
            visualUploading={poolUploader.busy}
            visualUploadDisabled={poolAtCapacity}
            visualUploadFeedback={visualUploadFeedback}
            onVisualUpload={handlePoolFiles}
            onAddMontage={addMontageBlock}
            onAddTextCard={addTextCard}
            onAddVisualBlockText={addVisualBlockText}
            onSelectVisualBlockText={selectText}
            onSaveVisualAssetContext={handleSavePoolAssetContext}
            onPatchVisualBlock={patchVisualBlock}
            onDuplicateVisualBlock={duplicateVisualBlock}
            onDeleteVisualBlock={deleteVisualBlock}
            onRetimeVisualBlock={retimeBlock}
            carousel={carouselControl}
            carouselSelected={selection?.kind === "carousel"}
            onSelectCarousel={selectCarousel}
            layoutMode={layoutMode}
            onClose={() => dispatchPocket({ type: "CLOSE_SHEET" })}
          />
        </Sheet>
      )}

      {/* ── Pocket inspector sheet: selection editing (whole InspectorPanel,
             retargets across selection kinds natively). ── */}
      {pocketActive && pocket.sheet?.kind === "inspector" && selection && (
        <Sheet
          open
          title={pocketInspectorTitle}
          detent={pocket.detent}
          onDetentChange={(detent) => dispatchPocket({ type: "SET_DETENT", detent })}
          onClose={() => dispatchPocket({ type: "CLOSE_SHEET" })}
          transportSlot={pocketTransportSlot}
        >
          <InspectorPanel
            presentation="sheet"
            onTab={setInspectorTab}
            selection={selection}
            bar={selectedBar}
            clipTiming={selectedClip}
            sfx={selectedSfx}
            overlay={selectedOverlay}
            visualBlock={selectedVisualBlock?.kind === "media" ? selectedVisualBlock : null}
            motionScene={selectedMotionScene}
            motionDurationS={timelineDuration}
            motionAssets={poolAssets}
            evolvingTypeEnabled={evolvingTypeExposureEnabled}
            motionEditable={motionScenesAllowed}
            motionDisabledReason={motionScenesDisabledReason}
            cameraEffect={selectedCameraEffect}
            carousel={carouselInspectorControl}
            tab={inspectorTab}
            sampleWord={sampleWord}
            appliedPresetId={appliedPresetId}
            contentRef={contentRef}
            onEditText={editSelectedText}
            onPatch={(patch) => {
              if (selectedBar) patchBar(selectedBar.id, patch);
            }}
            onPreviewTextMotion={previewSelectedTextMotion}
            onBeginTextMotion={beginTextMotionGesture}
            onCommitTextMotion={commitSelectedTextMotion}
            onSetTextBoxPosition={setSelectedTextBoxPosition}
            boxPositionXFrac={selectedTextBoxScreenXFrac}
            onPatchTextTiming={patchSelectedTextTiming}
            textEditable={textElementsAllowed || (!!selectedBar && isCaptionBar(selectedBar))}
            textDisabledReason={textDisabledReason}
            onPatchClipTiming={patchSelectedClipTiming}
            onPatchClipLook={patchSelectedClipLook}
            onPatchClipTransition={patchSelectedClipTransition}
            onMoveClip={moveSelectedClip}
            clipReorderEditable={clipReorderAllowed}
            clipTimingEditable={clipCan("trim", true)}
            clipLooksEditable={clipLooksAllowed}
            clipTransitionsEditable={clipTransitionsAllowed}
            clipTimingDisabledReason={clipTimingDisabledReason}
            clipReorderDisabledReason={clipReorderDisabledReason}
            clipLooksDisabledReason={clipLooksDisabledReason}
            clipTransitionsDisabledReason={clipTransitionsDisabledReason}
            availableLookPresets={perClipLookPresets}
            onPatchClipLookAdjustments={patchSelectedClipLookAdjustments}
            onRecordClipLookAdjustments={recordSelectedClipLookAdjustment}
            onPreviewClipTiming={previewSelectedClipTiming}
            onRecordClipTiming={recordTimelineDrag}
            onPatchSfx={patchSfx}
            onDeleteSfx={removeSfx}
            sfxEditable={sfxAllowed}
            sfxDisabledReason={sfxDisabledReason}
            onPatchOverlay={patchOverlay}
            onPatchVisualBlock={patchVisualBlock}
            onReorderVisualBlock={reorderVisualMediaBlock}
            onPreviewOverlay={previewOverlayPatch}
            onRecordOverlay={recordTimelineDrag}
            onDeleteOverlay={removeOverlay}
            overlayEditable={overlaysAllowed}
            overlayDisabledReason={overlaysDisabledReason}
            onPatchMotion={patchMotionScene}
            onPatchMotionControl={patchMotionControl}
            onBeginMotionControl={beginMotionControl}
            onPreviewMotionControl={previewMotionControl}
            onCommitMotionControl={commitMotionControl}
            onCancelMotionControl={cancelMotionControl}
            onRemoveMotion={removeMotionScene}
            onPatchCameraEffect={patchCameraEffect}
            onDeleteCameraEffect={deleteCameraEffect}
            mixLevel={mixLevel}
            mixEditable={musicLevelEditable && mixLevel != null}
            mixDisabledReason={musicLevelDisabledReason}
            mixLabel={soundBedLabel}
            musicTracks={musicTracks}
            musicLoading={musicTracksLoading}
            currentMusicTrackId={effectiveAudioTrackId}
            musicEditable={musicSwapEditable}
            musicRemoveEditable={musicRemoveEditable}
            musicSwapDisabledReason={musicSwapDisabledReason}
            musicRemoveDisabledReason={musicRemoveDisabledReason}
            backgroundMusic={backgroundMusic}
            backgroundMusicTrackDurationS={backgroundMusicTrackDurationS}
            onPickMusic={pickMusicTrack}
            onRemoveMusic={removeMusic}
            onPatchBackgroundMusic={patchBackgroundMusic}
            onRemoveBackgroundMusic={removeBackgroundMusic}
            musicWindow={musicWindowControl}
            onPatchMix={patchMixLevel}
            smartPlaceAvailable={
              !!selectedBar && !readOnly && (isMasonryVariant(variant) || !!smartPlacementCandidate)
            }
            onSmartPlace={applySelectedSmartPlacement}
            onMergeCaptionCue={mergeCaptionCue}
            onOpenCaptionsPanel={
              captionsControl
                ? () => dispatchPocket({ type: "OPEN_TOOL", tool: "captions" })
                : undefined
            }
            captionsPanelOpen={false /* one-sheet rule: never open alongside */}
            onClose={() => dispatchPocket({ type: "CLOSE_SHEET" })}
            onPickPreset={pickPreset}
          />
        </Sheet>
      )}

      {/* ── Read-only banner (ineligible variant, plan §9 / E4) ── */}
      {readOnly && (
        <div className="absolute left-1/2 top-[68px] z-[60] w-[min(560px,90vw)] -translate-x-1/2">
          <div className="rounded-lg border border-zinc-200 bg-white/95 px-4 py-2.5 text-center text-[12px] text-[#3f3f46] shadow-sm">
            This version can&apos;t be edited. {readOnlyReason}
          </div>
        </div>
      )}

      {/* Optional authored text can stay flag-locked while caption cue rows are
          selected and edited directly in the timeline. */}
      {textElementsLocked && !isCaptionEdit && !readOnly && (
        <div className="absolute left-1/2 top-[68px] z-[60] w-[min(560px,90vw)] -translate-x-1/2">
          <div
            data-testid="captions-tab-notice"
            className="rounded-lg border border-zinc-200 bg-white px-4 py-2.5 text-center text-[12px] text-[#3f3f46] shadow-sm"
          >
            {textElementsLockedCopy(capabilities)}.
          </div>
        </div>
      )}

      {/* ── Save micro-states (plan §9): conflict / error / partial tiles.
             All preserve working state; only Reload/Retry act. ── */}
      {(saveState === "conflict" || saveState === "error" || saveState === "partial") && (
        <div className="absolute left-1/2 top-[68px] z-[70] w-[min(520px,90vw)] -translate-x-1/2">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-zinc-300 bg-white px-4 py-3 shadow-sm">
            <p className="text-[12px] text-[#3f3f46]">
              {saveState === "conflict"
                ? "This video changed in another tab — reload to continue."
                : saveState === "partial"
                  ? "Saved, but rendering didn't start."
                  : (saveMessage ?? "Couldn't save your edits.")}
            </p>
            {saveState === "conflict" ? (
              <Button
                type="button"
                size="sm"
                className="flex-shrink-0"
                onClick={() => {
                  setSaveState("idle");
                  setSaveMessage(null);
                  // Re-seed non-dirty sections from the refetch (see
                  // conflictReseedRef) and refresh the slot baseline.
                  conflictReseedRef.current = true;
                  if (!clipDirty) {
                    slotsSeededRef.current = null;
                    reloadClipTimeline();
                  }
                  setLoadNonce((n) => n + 1);
                }}
              >
                Reload
              </Button>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="flex-shrink-0"
                onClick={() => void handleSave()}
              >
                Retry
              </Button>
            )}
          </div>
        </div>
      )}

      {/* ── Resume-draft notice (plan §9): quiet, not a modal. ── */}
      {draftDoc && saveState === "idle" && (
        <div className="absolute left-1/2 top-[68px] z-[65] w-[min(480px,90vw)] -translate-x-1/2">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white px-4 py-2.5 shadow-sm">
            <p className="text-[12px] text-[#3f3f46]">Resume your unsaved edits?</p>
            <div className="flex flex-shrink-0 items-center gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={discardDraft}>
                Discard
              </Button>
              <Button type="button" size="sm" onClick={resumeDraft}>
                Resume
              </Button>
            </div>
          </div>
        </div>
      )}

      <MusicAlignmentDialog
        open={musicAlignmentPrompt}
        preserveAvailable={musicWindowCapability?.preserve_available === true}
        preserveReason={musicWindowCapability?.preserve_reason ?? null}
        onChoose={(alignment) => void handleSave(alignment)}
        onCancel={() => setMusicAlignmentPrompt(false)}
      />

      <ConfirmDialog
        open={confirmLeave}
        question="Discard your edits?"
        detail="Your changes haven't been saved. Leaving now throws them away."
        confirmLabel="Discard"
        cancelLabel="Keep editing"
        onConfirm={() => {
          setConfirmLeave(false);
          router.push(`/plan/items/${itemId}`);
        }}
        onCancel={() => setConfirmLeave(false)}
      />

      <ConfirmDialog
        open={!!songResetPrompt}
        question="Changing the song resets clip cuts to the new beat grid."
        detail="Save with the new song?"
        confirmLabel="Save with new song"
        cancelLabel="Cancel"
        onConfirm={() => {
          const pending = songResetPrompt;
          setSongResetPrompt(null);
          void handleSave(pending?.musicAlignment, pending?.opts, true);
        }}
        onCancel={() => setSongResetPrompt(null)}
      />

    </div>
  );
}

export function MusicAlignmentDialog({
  open,
  preserveAvailable,
  preserveReason,
  onChoose,
  onCancel,
}: {
  open: boolean;
  preserveAvailable: boolean;
  preserveReason: string | null;
  onChoose: (alignment: "preserve_cuts" | "resync_beats") => void;
  onCancel: () => void;
}) {
  // NOTE (DESIGN.md §15 / plan Lane E1): this dialog stays hand-rolled rather
  // than moving to the shadcn `AlertDialog`. Radix's DismissableLayer handles
  // Escape without `stopImmediatePropagation` on the capturing-phase document
  // listener, so an outer `keydown` handler (the editor's global shortcut
  // layer) still observes the Escape that closed this dialog — regressing
  // MusicAlignmentDialog.test.tsx's "handles Escape once without leaking to
  // editor shortcuts" guard. Every other control here (Button, styling) is
  // still on the primitives; only the outer alertdialog shell + focus-trap +
  // capture-phase Escape isolation are pre-existing hand-rolled code.
  const cardRef = useRef<HTMLDivElement>(null);
  const preserveRef = useRef<HTMLButtonElement>(null);
  const resyncRef = useRef<HTMLButtonElement>(null);
  useFocusTrap(cardRef, open);
  useEffect(() => {
    if (!open) return;
    (preserveAvailable ? preserveRef.current : resyncRef.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onCancel, open, preserveAvailable]);
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 px-6"
      onClick={onCancel}
    >
      <div
        ref={cardRef}
        role="alertdialog"
        aria-modal="true"
        aria-label="How should the cuts follow this song section?"
        className="w-full max-w-[460px] rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm"
        onClick={(event) => event.stopPropagation()}
      >
        <p className="font-display text-xl text-[#0c0c0e]">
          How should the cuts follow this song section?
        </p>
        <p className="mt-2 text-sm leading-relaxed text-[#71717a]">
          Your selected song section is saved either way.
        </p>
        <div className="mt-5 space-y-3">
          <Button
            ref={preserveRef}
            type="button"
            variant="outline"
            disabled={!preserveAvailable}
            onClick={() => onChoose("preserve_cuts")}
            className="h-auto min-h-14 w-full flex-col items-start whitespace-normal rounded-xl px-4 py-3 text-left text-sm normal-case tracking-normal"
          >
            Preserve cuts
            <span className="mt-1 block text-[12px] font-normal text-[#71717a]">
              Keep the current clip order and timing.
            </span>
          </Button>
          {!preserveAvailable && preserveReason === "linear_timeline_unavailable" && (
            <p className="px-1 text-[11px] text-[#71717a]">
              Preserve cuts isn’t available for this older render.
            </p>
          )}
          <Button
            ref={resyncRef}
            type="button"
            variant="ink"
            onClick={() => onChoose("resync_beats")}
            className="h-auto min-h-14 w-full flex-col items-start whitespace-normal rounded-xl px-4 py-3 text-left text-sm normal-case tracking-normal"
          >
            Re-sync to beats
            <span className="mt-1 block text-[12px] font-normal text-white/70">
              Rebuild the cuts around the beats in this section.
            </span>
          </Button>
        </div>
        <Button
          type="button"
          variant="link"
          className="mt-5 min-h-11 w-full text-sm text-[#71717a] hover:underline"
          onClick={onCancel}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}

function LightTopBar({
  dirty,
  saving,
  readOnly,
  saveState,
  showCopilotNotice,
  onBack,
  onOpenNova,
  onDismissCopilotNotice,
  onSave,
  orientationToggle,
}: {
  dirty: boolean;
  saving: boolean;
  readOnly: boolean;
  saveState: "idle" | "saving" | "conflict" | "error" | "partial";
  showCopilotNotice: boolean;
  onBack: () => void;
  onOpenNova: () => void;
  onDismissCopilotNotice: () => void;
  onSave: () => void;
  orientationToggle?: React.ReactNode;
}) {
  const copilotEnabled = process.env.NEXT_PUBLIC_EDIT_COPILOT_ENABLED === "true";
  return (
    <header className="flex h-14 items-center justify-between gap-2 border-b border-border bg-background px-3">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Back to the video page"
        onClick={onBack}
      >
        <ArrowLeftIcon className="h-4 w-4" />
      </Button>
      <div className="min-w-0 flex-1 text-center">
        {showCopilotNotice ? (
          <div className="mx-auto flex max-w-[320px] items-center justify-center gap-2 rounded-md border border-border bg-background px-2 py-1 text-[11px] text-muted-foreground">
            <span className="truncate">
              Staged preview — Save renders the new video exactly.
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="Dismiss preview match note"
              onClick={onDismissCopilotNotice}
              className="text-muted-foreground hover:text-foreground"
            >
              ✕
            </Button>
          </div>
        ) : orientationToggle ? (
          <div className="flex justify-center">{orientationToggle}</div>
        ) : (
          <span className="text-[13px] font-semibold text-foreground">Edit video</span>
        )}
      </div>
      {copilotEnabled && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Open Kria"
          disabled={readOnly}
          onClick={onOpenNova}
          className="text-[15px]"
        >
          ✧
        </Button>
      )}
      <Button
        type="button"
        size="sm"
        disabled={!dirty || saving || readOnly}
        onClick={onSave}
      >
        {saveState === "saving" ? "Saving..." : "Save"}
      </Button>
    </header>
  );
}

function LightTransport({
  playing,
  currentTime,
  duration,
  onPlayPause,
  onScrub,
  compact = false,
}: {
  playing: boolean;
  currentTime: number;
  duration: number;
  onPlayPause: () => void;
  onScrub: (seconds: number) => void;
  compact?: boolean;
}) {
  const safeDuration = Math.max(0, duration);
  const safeTime = Math.min(safeDuration || currentTime, Math.max(0, currentTime));
  return (
    <div
      className={
        compact
          ? "border-t border-border bg-background px-3 py-2"
          : "border-t border-border bg-background px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3"
      }
    >
      <div className="mx-auto flex max-w-[720px] items-center gap-3">
        <Button
          type="button"
          size="icon"
          aria-label={playing ? "Pause video" : "Play video"}
          aria-pressed={playing}
          onClick={onPlayPause}
          variant={compact ? "ghost" : "default"}
          className={compact ? "size-11 flex-none" : "flex-none"}
        >
          {playing ? (
            <PauseIcon className="h-5 w-5" />
          ) : (
            <PlayIcon className="h-5 w-5" />
          )}
        </Button>
        {/* Native range (DESIGN.md §15 — "Scrub video" keeps a raw <input
            type=range>; Slider's discrete-thumb model doesn't fit continuous
            video scrubbing, and `range` is excluded from the raw-control
            guard). */}
        <input
          type="range"
          aria-label="Scrub video"
          min={0}
          max={safeDuration || 0}
          step={0.1}
          value={safeDuration > 0 ? safeTime : 0}
          disabled={safeDuration <= 0}
          onChange={(e) => onScrub(Number(e.target.value))}
          className="h-11 min-w-0 flex-1 cursor-pointer accent-lime-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-40"
        />
        <span
          aria-label="Playback position"
          className={`flex-none text-sm tabular-nums text-muted-foreground ${
            compact ? "w-[76px] text-right text-xs" : "w-[92px] text-right"
          }`}
        >
          {formatTimecode(currentTime)}{" "}
          <span className="text-muted-foreground/60">/ {formatTimecode(duration)}</span>
        </span>
      </div>
    </div>
  );
}

function LightEditSheet({
  open,
  bar,
  sampleWord,
  appliedPresetId,
  saveState,
  saving,
  dirty,
  readOnly,
  onClose,
  onEditText,
  onPickPreset,
  onSave,
}: {
  open: boolean;
  bar: TextElementBar | null;
  sampleWord: string | null;
  appliedPresetId: string | null;
  saveState: "idle" | "saving" | "conflict" | "error" | "partial";
  saving: boolean;
  dirty: boolean;
  readOnly: boolean;
  onClose: () => void;
  onEditText: (text: string) => void;
  onPickPreset: (preset: TextPreset) => void;
  onSave: () => void;
}) {
  const trapRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useFocusTrap(trapRef, open);

  useEffect(() => {
    if (!open) return;
    const id = window.requestAnimationFrame(() => {
      textareaRef.current?.focus({ preventScroll: true });
      textareaRef.current?.select();
    });
    return () => window.cancelAnimationFrame(id);
  }, [open, bar?.id]);

  if (!open || !bar) return null;

  return (
    <div
      ref={trapRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="light-edit-title"
      className="fixed inset-0 z-[90] flex flex-col bg-white"
    >
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3">
        <div>
          <h2 id="light-edit-title" className="font-display text-[18px] text-[#0c0c0e]">
            Edit text
          </h2>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Close text editor"
          onClick={onClose}
          className="text-[14px]"
        >
          ✕
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <label className="block text-[12px] font-semibold text-[#3f3f46]" htmlFor="light-edit-textarea">
          Content
        </label>
        <Textarea
          id="light-edit-textarea"
          ref={textareaRef}
          value={bar.text}
          readOnly={readOnly}
          onChange={(e) => onEditText(e.target.value)}
          rows={5}
          className="mt-2 resize-none text-[15px]"
        />
        <p className="mb-3 mt-6 text-[12px] font-semibold text-[#3f3f46]">Presets</p>
        <PresetGrid
          presets={TEXT_PRESETS}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onPick={onPickPreset}
        />
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-3">
        <Button type="button" variant="link" size="sm" onClick={onClose}>
          Close
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={!dirty || saving || readOnly}
          onClick={onSave}
        >
          {saveState === "saving" ? "Saving..." : "Save"}
        </Button>
      </div>
    </div>
  );
}

/** Chrome-less frame for loading / error / auth states (keeps the shell's
 * grid footprint so the transition to the loaded editor doesn't jump). */
function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-[#ffffff]">
      <div className="h-14 flex-none border-b border-zinc-200 bg-white" />
      {children}
    </div>
  );
}
