"use client";

/**
 * InspectorPanel — the docked right contextual panel (plan §4, Variant A).
 *
 * The ~320px (w-80) column is PERMANENTLY RESERVED: the canvas never reflows
 * on select/deselect. With nothing selected it shows a quiet empty state — a
 * lucide `MousePointerClick` glyph over "Select anything to edit it" (Lane I,
 * DESIGN.md §15 stock-shadcn chrome pass).
 *
 * Text inspector rows are driven by the PARITY_VERIFIED_FIELDS registry
 * (D9/D17): a control renders editable only for verified fields; fields
 * present in the data without an editable row render read-only — the panel
 * never hides state it preserves. Progressive disclosure: content / font /
 * size / Fill visible; Stroke collapsed behind its + row. B/I/U, case,
 * spacing, background, shadow controls are parity-gated (later task).
 *
 * Edits dispatch PATCH_BAR / EDIT_TEXT on the local reducer via `onPatch` /
 * `onEditText` → the canvas updates instantly. Persistence only on Save.
 */

import { createContext, useContext, useEffect, useId, useMemo, useRef, useState } from "react";
import { MousePointerClick } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Textarea } from "@/components/ui/textarea";
import { TEXT_ELEMENT_ANIMATIONS, THEME_TRANSITIONS } from "@/lib/overlay-constants";
import type { MediaLayerMove } from "./editor-media-visuals";
import {
  LETTER_SPACING_MAX_EM,
  LETTER_SPACING_MIN_EM,
  LINE_SPACING,
  LINE_SPACING_MAX,
  LINE_SPACING_MIN,
  MAX_LINE_W_FRAC,
  MAX_WIDTH_FRAC_MAX,
  MAX_WIDTH_FRAC_MIN,
  inferTextBoxPosition,
  resolveTextElementYFrac,
  xFracForTextAlignment,
  xFracForTextBoxPosition,
  type TextBoxHorizontalPosition,
  type TextHorizontalAlignment,
} from "@/lib/overlay-layout";
import {
  INSPECTOR_INTERNAL_FIELDS,
  isParityVerified,
} from "@/lib/parity-verified-fields";
import { TEXT_PRESETS, type TextPreset } from "@/lib/text-presets";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { CameraEffect, MediaOverlay, MediaVisualBlock, PoolAsset, SoundEffectPlacement } from "@/lib/plan-api";
import type { MotionPresetInstance, MotionPresetPatch } from "@nova/motion-runtime";
import {
  CAMERA_EFFECT_MAX_DURATION_S,
  CAMERA_EFFECT_MAX_INTENSITY,
  CAMERA_EFFECT_MIN_DURATION_S,
} from "@/lib/camera-effects";
import type { MusicTrackSummary } from "@/lib/music-api";
import type { EditorTransition } from "@/lib/generative-api";
import type { EditorCommitBackgroundMusic } from "@/lib/editor-commit";
import TextMotionControls from "@/components/text-motion/TextMotionControls";
import {
  motionPatchForConfig,
  motionPatchForEffect,
  textMotionHasControls,
  type TextMotionConfigV2,
} from "@/lib/text-motion-v2";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { LookAdjustments, LookPreset } from "@/lib/generative-api";
import {
  defaultLookAdjustments,
  isCustomizableLook,
  lookPresetLabel,
  resolveLookAdjustments,
} from "@/lib/look-presets";
import type { EditorSelection } from "./useEditorSelection";
import type { InspectorTab } from "./InspectorRail";
import { normalizeEditableHex } from "./editor-color";
import { FontSelect, HexInput } from "./inspector-fields";
import {
  applyClipSourceWindowDrag,
  CLIP_MIN_DURATION_S,
  type BarDragHandle,
} from "./editor-bar-drag";
import {
  applyMediaOverlaySourceWindowInput,
  clampMediaOverlayScale,
  MEDIA_OVERLAY_MIN_SCALE,
  MEDIA_OVERLAY_MAX_SCALE,
} from "./editor-media-overlays";
import {
  AI_SEQUENCE_BADGE_LABEL,
  AI_SEQUENCE_BADGE_TOOLTIP,
  isAiSequenceBar,
  SMART_ROLE_BADGE_LABELS,
  smartCaptionPreviewSizePx,
  smartStyleForRole,
} from "./editor-bars";
import PresetGrid from "./PresetGrid";
import SongWindowSelector, { type SongWindowControl } from "./SongWindowSelector";
import MotionInspector, { type CreatorBlockMotionControlPatch } from "./MotionInspector";
import CarouselPanel, { type CarouselPanelControl } from "./CarouselPanel";
import { InfoDot } from "@/components/ui/InfoDot";

/** Fields with dedicated (potentially editable) rows in this panel. */
const EDITABLE_ROW_FIELDS = new Set([
  "text",
  "start_s",
  "end_s",
  "font_family",
  "size_px",
  "effect",
  "motion",
  "theme_transition",
  "color",
  "shadow_enabled",
  "shadow_style",
  "stroke_width",
  "text_case",
  "letter_spacing",
  "line_spacing",
  "max_width_frac",
  "alignment",
  "behind_subject",
  // Lane PR-A per-cue overrides ("This caption" section) — have their own
  // dedicated rows below, same reasoning as font_family/size_px/color above.
  "cue_font_family",
  "cue_text_color",
  "cue_size_px",
]);

const EDITOR_TEXT_SIZE_MIN = 8;
const EDITOR_TEXT_SIZE_MAX = 300;

const TEXT_BEHIND_SUBJECT_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_BEHIND_SUBJECT_ENABLED === "true";
const TEXT_MOTION_V2_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED === "true";

/** How the panel is hosted. Every sub-inspector's CloseX reads this so sheet
 *  mode can drop the internal close buttons (the Sheet owns close; deselection
 *  happens on canvas) without threading a prop through each inspector. */
const InspectorPresentationContext = createContext<"panel" | "sheet">("panel");

export interface InspectorClipTiming {
  slot: DraftSlot;
  clipNumber: number;
  durationS: number;
  sourceDurationS: number | null;
  sourceUrl: string | null;
}

const SIZE_OPTIONS = (() => {
  const out: number[] = [];
  for (let s = 8; s <= 96; s += 8) out.push(s);
  out.push(120, 160, 220, 300);
  return out;
})();

function fieldLabel(key: string): string {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export default function InspectorPanel({
  selection,
  bar,
  clipTiming,
  sfx,
  overlay,
  visualBlock = null,
  motionScene = null,
  motionDurationS = 0,
  motionAssets = [],
  evolvingTypeEnabled = false,
  motionEditable = true,
  motionDisabledReason = null,
  cameraEffect = null,
  carousel = null,
  tab,
  presentation = "panel",
  onTab,
  sampleWord,
  appliedPresetId,
  contentRef,
  onEditText,
  onPatch,
  onPreviewTextMotion,
  onBeginTextMotion,
  onCommitTextMotion,
  onSetTextBoxPosition,
  boxPositionXFrac,
  onPatchTextTiming,
  textEditable = true,
  textDisabledReason = null,
  onPatchClipTiming,
  onPatchClipLook,
  onPatchClipTransition,
  onMoveClip,
  clipReorderEditable = true,
  clipTimingEditable = true,
  clipLooksEditable = true,
  clipTransitionsEditable = true,
  clipTimingDisabledReason = null,
  clipReorderDisabledReason = null,
  clipLooksDisabledReason = null,
  clipTransitionsDisabledReason = null,
  availableLookPresets = [],
  onPatchClipLookAdjustments,
  onRecordClipLookAdjustments,
  onPreviewClipTiming,
  onRecordClipTiming,
  onPatchSfx,
  onDeleteSfx,
  sfxEditable = true,
  sfxDisabledReason = null,
  onPatchOverlay,
  onPatchVisualBlock,
  onReorderVisualBlock,
  onPreviewOverlay,
  onRecordOverlay,
  onDeleteOverlay,
  overlayEditable = true,
  overlayDisabledReason = null,
  onPatchMotion = () => {},
  onPatchMotionControl = () => {},
  onBeginMotionControl = () => {},
  onPreviewMotionControl = () => {},
  onCommitMotionControl = () => {},
  onCancelMotionControl = () => {},
  onRemoveMotion = () => {},
  onPatchCameraEffect = () => {},
  onDeleteCameraEffect = () => {},
  mixLevel,
  mixEditable,
  mixDisabledReason = null,
  mixLabel,
  musicTracks = [],
  musicLoading = false,
  currentMusicTrackId = null,
  musicEditable = false,
  musicRemoveEditable = musicEditable,
  musicSwapDisabledReason = null,
  musicRemoveDisabledReason = null,
  backgroundMusic = null,
  backgroundMusicTrackDurationS = null,
  onPatchMix,
  sourceAudioMix,
  sourceAudioOptions = [],
  onSourceAudioMix,
  onPickMusic,
  onRemoveMusic,
  onPatchBackgroundMusic,
  onRemoveBackgroundMusic,
  musicWindow,
  smartPlaceAvailable = false,
  onSmartPlace,
  onMergeCaptionCue,
  onOpenCaptionsPanel,
  captionsPanelOpen = false,
  canMergeCaptionPrev = false,
  canMergeCaptionNext = false,
  onClose,
  onPickPreset,
}: {
  selection: EditorSelection | null;
  /** The selected text bar (null when selection is empty or non-text). */
  bar: TextElementBar | null;
  clipTiming: InspectorClipTiming | null;
  sfx: SoundEffectPlacement | null;
  overlay: MediaOverlay | null;
  visualBlock?: MediaVisualBlock | null;
  motionScene?: MotionPresetInstance | null;
  motionDurationS?: number;
  motionAssets?: PoolAsset[];
  evolvingTypeEnabled?: boolean;
  motionEditable?: boolean;
  motionDisabledReason?: string | null;
  cameraEffect?: CameraEffect | null;
  carousel?: CarouselPanelControl | null;
  tab: InspectorTab;
  /** "sheet" when hosted inside the mobile bottom-sheet primitive, which owns
   *  the chrome (width, close). Default "panel" renders the docked desktop
   *  column unchanged. */
  presentation?: "panel" | "sheet";
  /** Sheet mode only: renders a Basic/Presets segmented control at the top of
   *  the panel (the desktop tab switch lives in InspectorRail, which the sheet
   *  doesn't show). Never rendered in panel mode. */
  onTab?: (tab: InspectorTab) => void;
  sampleWord: string | null;
  appliedPresetId: string | null;
  /** Exposed so double-click-on-canvas can focus + select-all (plan §5). */
  contentRef: React.RefObject<HTMLTextAreaElement>;
  onEditText: (text: string) => void;
  onPatch: (patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onPreviewTextMotion?: (patch: Partial<TextMotionConfigV2>) => void;
  onBeginTextMotion?: () => void;
  onCommitTextMotion?: (patch: Partial<TextMotionConfigV2>) => void;
  onSetTextBoxPosition?: (position: TextBoxHorizontalPosition) => void;
  boxPositionXFrac?: number;
  onPatchTextTiming: (patch: { start_s?: number; end_s?: number }) => void;
  textEditable?: boolean;
  textDisabledReason?: string | null;
  onPatchClipTiming: (patch: { inS?: number; outS?: number; durationS?: number }) => void;
  onPatchClipLook?: (preset: LookPreset) => void;
  onPatchClipTransition?: (transition: EditorTransition, durationS?: number) => void;
  onMoveClip?: (direction: -1 | 1) => void;
  clipReorderEditable?: boolean;
  clipTimingEditable?: boolean;
  clipLooksEditable?: boolean;
  clipTransitionsEditable?: boolean;
  clipTimingDisabledReason?: string | null;
  clipReorderDisabledReason?: string | null;
  clipLooksDisabledReason?: string | null;
  clipTransitionsDisabledReason?: string | null;
  availableLookPresets?: LookPreset[];
  onPatchClipLookAdjustments?: (patch: Partial<LookAdjustments>) => void;
  onRecordClipLookAdjustments?: () => void;
  onPreviewClipTiming: (patch: { inS: number; durationS: number }) => void;
  onRecordClipTiming: () => void;
  onPatchSfx: (id: string, patch: Partial<SoundEffectPlacement>) => void;
  onDeleteSfx: (id: string) => void;
  sfxEditable?: boolean;
  sfxDisabledReason?: string | null;
  onPatchOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onPatchVisualBlock?: (id: string, patch: Partial<MediaVisualBlock>) => void;
  onReorderVisualBlock?: (id: string, move: MediaLayerMove) => void;
  onPreviewOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onRecordOverlay: () => void;
  onDeleteOverlay: (id: string) => void;
  overlayEditable?: boolean;
  overlayDisabledReason?: string | null;
  onPatchMotion?: (id: string, patch: MotionPresetPatch) => void;
  onPatchMotionControl?: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onBeginMotionControl?: () => void;
  onPreviewMotionControl?: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onCommitMotionControl?: (id: string, patch: CreatorBlockMotionControlPatch) => void;
  onCancelMotionControl?: () => void;
  onRemoveMotion?: (id: string) => void;
  onPatchCameraEffect?: (id: string, patch: Partial<CameraEffect>) => void;
  onDeleteCameraEffect?: (id: string) => void;
  mixLevel?: number | null;
  mixEditable?: boolean;
  mixDisabledReason?: string | null;
  mixLabel?: string;
  musicTracks?: MusicTrackSummary[];
  musicLoading?: boolean;
  currentMusicTrackId?: string | null;
  musicEditable?: boolean;
  musicRemoveEditable?: boolean;
  musicSwapDisabledReason?: string | null;
  musicRemoveDisabledReason?: string | null;
  backgroundMusic?: EditorCommitBackgroundMusic | null;
  backgroundMusicTrackDurationS?: number | null;
  onPatchMix?: (level: number) => void;
  sourceAudioMix?: "interleaved" | "source_a" | "source_b" | null;
  sourceAudioOptions?: Array<{
    mix: "interleaved" | "source_a" | "source_b";
    audio_path: string;
    audio_url: string;
    duration_s: number;
  }>;
  onSourceAudioMix?: (mix: "interleaved" | "source_a" | "source_b") => void;
  onPickMusic?: (trackId: string) => void;
  onRemoveMusic?: () => void;
  onPatchBackgroundMusic?: (patch: Partial<EditorCommitBackgroundMusic>) => void;
  onRemoveBackgroundMusic?: () => void;
  musicWindow?: SongWindowControl;
  smartPlaceAvailable?: boolean;
  onSmartPlace?: () => void;
  /** 4b merge-with-neighbor: folds the selected caption cue into its
   * chronological prev/next cue. Availability flags computed by the shell
   * (needs the full bar list, which this panel doesn't have). */
  onMergeCaptionCue?: (direction: "prev" | "next") => void;
  /** Opens the Captions rail tool, which owns the variant-wide caption
   *  styling this panel used to duplicate. */
  onOpenCaptionsPanel?: () => void;
  /** True when that panel is already open, so this panel stops advertising it. */
  captionsPanelOpen?: boolean;
  canMergeCaptionPrev?: boolean;
  canMergeCaptionNext?: boolean;
  /** Close X clears the selection — the column stays (D6). */
  onClose: () => void;
  onPickPreset: (preset: TextPreset) => void;
}) {
  const hasSelection = selection !== null;
  return (
    <InspectorPresentationContext.Provider value={presentation}>
    <div
      data-region="inspector"
      className={
        presentation === "sheet"
          ? "flex w-full flex-col bg-background"
          : "flex w-80 flex-col border-l border-border bg-background"
      }
    >
      {/* Basic/Presets switch (plan §4, decision D6 — moved in-panel off the
          old floating InspectorRail column, Lane I). "Basic" stays disabled
          until a selection exists; "Presets" is always browsable. Styled as
          a TabsList but kept as plain buttons (not Radix TabsTrigger, which
          activates on mousedown — incompatible with the suite's
          fireEvent.click-driven interaction tests). */}
      {onTab && (
        <div
          className={
            presentation === "sheet"
              ? "flex flex-none px-5 pb-3 pt-1"
              : "flex flex-none border-b border-border px-4 py-3"
          }
        >
          <div
            role="group"
            aria-label="Inspector sections"
            className="flex w-full items-center gap-1 rounded-md bg-muted p-1 text-muted-foreground"
          >
            {(["basic", "presets"] as const).map((nextTab) => {
              const label = nextTab === "basic" ? "Basic" : "Presets";
              const disabled = nextTab === "basic" && !hasSelection;
              const active = tab === nextTab;
              return (
                <Button
                  key={nextTab}
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={disabled}
                  aria-pressed={active}
                  aria-label={`${label} inspector tab`}
                  title={disabled ? "Select something to edit its properties" : label}
                  onClick={() => onTab(nextTab)}
                  className={
                    active
                      ? "flex-1 bg-background text-foreground shadow hover:bg-background"
                      : "flex-1 hover:bg-background/60"
                  }
                >
                  {label}
                </Button>
              );
            })}
          </div>
        </div>
      )}
      {tab === "presets" ? (
        <PresetsTab
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onPickPreset={onPickPreset}
        />
      ) : selection === null ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 pt-16">
          <MousePointerClick className="h-6 w-6 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">
            Select a clip, caption, or overlay to edit it.
          </p>
        </div>
      ) : selection.kind === "text" && bar ? (
        <div className="flex min-h-0 flex-1 flex-col">
          {!textEditable && textDisabledReason && (
            <p className="mx-5 mt-4 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
              {textDisabledReason}
            </p>
          )}
          <fieldset
            disabled={!textEditable}
            className="contents disabled:opacity-60"
            title={!textEditable ? (textDisabledReason ?? undefined) : undefined}
          >
            <TextInspector
              key={bar.id}
              bar={bar}
              contentRef={contentRef}
              onEditText={onEditText}
              onPatch={onPatch}
              onPreviewTextMotion={onPreviewTextMotion}
              onBeginTextMotion={onBeginTextMotion}
              onCommitTextMotion={onCommitTextMotion}
              onSetTextBoxPosition={onSetTextBoxPosition}
              boxPositionXFrac={boxPositionXFrac}
              onPatchTiming={onPatchTextTiming}
              videoDurationS={motionDurationS}
              smartPlaceAvailable={smartPlaceAvailable}
              onSmartPlace={onSmartPlace}
              onMergeCaptionCue={onMergeCaptionCue}
              onOpenCaptionsPanel={onOpenCaptionsPanel}
              captionsPanelOpen={captionsPanelOpen}
              canMergeCaptionPrev={canMergeCaptionPrev}
              canMergeCaptionNext={canMergeCaptionNext}
              onClose={onClose}
            />
          </fieldset>
        </div>
      ) : selection.kind === "clip" && clipTiming ? (
        <ClipInspector
          timing={clipTiming}
          onPatchTiming={onPatchClipTiming}
          onPatchLook={onPatchClipLook}
          onPatchTransition={onPatchClipTransition}
          onMoveClip={onMoveClip}
          reorderEditable={clipReorderEditable}
          timingEditable={clipTimingEditable}
          looksEditable={clipLooksEditable}
          transitionsEditable={clipTransitionsEditable}
          timingDisabledReason={clipTimingDisabledReason}
          reorderDisabledReason={clipReorderDisabledReason}
          looksDisabledReason={clipLooksDisabledReason}
          transitionsDisabledReason={clipTransitionsDisabledReason}
          availableLookPresets={availableLookPresets}
          onPatchLookAdjustments={onPatchClipLookAdjustments}
          onRecordLookAdjustments={onRecordClipLookAdjustments}
          onPreviewTiming={onPreviewClipTiming}
          onRecordTimingEdit={onRecordClipTiming}
          onClose={onClose}
        />
      ) : selection.kind === "sfx" && sfx ? (
        <SfxInspector
          placement={sfx}
          onPatch={onPatchSfx}
          onDelete={onDeleteSfx}
          editable={sfxEditable}
          disabledReason={sfxDisabledReason}
          onClose={onClose}
        />
      ) : selection.kind === "overlay" && overlay ? (
        <OverlayInspector
          overlay={overlay}
          onPatch={onPatchOverlay}
          onPreview={onPreviewOverlay}
          onRecord={onRecordOverlay}
          onDelete={onDeleteOverlay}
          editable={overlayEditable}
          disabledReason={overlayDisabledReason}
          onClose={onClose}
        />
      ) : selection.kind === "visual" && visualBlock?.kind === "media" ? (
        <MediaVisualInspector
          block={visualBlock}
          projectDurationS={motionDurationS}
          onPatch={onPatchVisualBlock}
          onReorder={onReorderVisualBlock}
          onClose={onClose}
        />
      ) : selection.kind === "motion" && motionScene ? (
        <MotionInspector
          scene={motionScene}
          durationS={motionDurationS}
          assets={motionAssets}
          evolvingTypeEnabled={evolvingTypeEnabled}
          editable={motionEditable}
          disabledReason={motionDisabledReason}
          showClose={presentation === "panel"}
          onPatch={onPatchMotion}
          onPatchMotionControl={onPatchMotionControl}
          onBeginMotionControl={onBeginMotionControl}
          onPreviewMotionControl={onPreviewMotionControl}
          onCommitMotionControl={onCommitMotionControl}
          onCancelMotionControl={onCancelMotionControl}
          onRemove={onRemoveMotion}
          onClose={onClose}
        />
      ) : selection.kind === "carousel" && carousel ? (
        <div data-testid="carousel-inspector" className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex items-center justify-between px-5 pb-4 pt-5">
            <div>
              <h2 className="font-display text-[18px] text-[#0c0c0e]">Carousel</h2>
            </div>
            <CloseX onClose={onClose} />
          </div>
          {carousel.capable ? (
            <CarouselPanel control={carousel} />
          ) : (
            <div
              className="mx-5 rounded-lg border border-zinc-200 bg-zinc-50 px-4 py-3 text-[13px] leading-5 text-[#3f3f46]"
              role="status"
            >
              {carousel.reason ?? "Carousel isn't available for this edit."}
            </div>
          )}
        </div>
      ) : selection.kind === "camera" && cameraEffect ? (
        <CameraInspector
          effect={cameraEffect}
          onPatch={onPatchCameraEffect}
          onDelete={onDeleteCameraEffect}
          onClose={onClose}
        />
      ) : selection.kind === "music" ? (
        <MixInspector
          level={mixLevel}
          editable={mixEditable ?? false}
          disabledReason={mixDisabledReason}
          label={mixLabel ?? "Music"}
          musicTracks={musicTracks}
          musicLoading={musicLoading}
          currentMusicTrackId={currentMusicTrackId}
          musicEditable={musicEditable}
          musicRemoveEditable={musicRemoveEditable}
          musicSwapDisabledReason={musicSwapDisabledReason}
          musicRemoveDisabledReason={musicRemoveDisabledReason}
          backgroundMusic={backgroundMusic}
          backgroundMusicTrackDurationS={backgroundMusicTrackDurationS}
          onPickMusic={onPickMusic}
          onRemoveMusic={onRemoveMusic}
          onPatchBackgroundMusic={onPatchBackgroundMusic}
          onRemoveBackgroundMusic={onRemoveBackgroundMusic}
          musicWindow={musicWindow}
          onPatch={onPatchMix}
          sourceAudioMix={sourceAudioMix}
          sourceAudioOptions={sourceAudioOptions}
          onSourceAudioMix={onSourceAudioMix}
          onClose={onClose}
        />
      ) : (
        // sfx / clip / overlay selections get their minimal inspectors with
        // the timeline task — never a dead end, but nothing to edit yet here.
        <div className="px-5 pt-5">
          <div className="flex items-center justify-between">
            <h2 className="font-display text-[18px] capitalize text-[#0c0c0e]">
              {selection.kind}
            </h2>
            <CloseX onClose={onClose} />
          </div>
        </div>
      )}
    </div>
    </InspectorPresentationContext.Provider>
  );
}

function MixInspector({
  level,
  editable,
  disabledReason,
  label,
  musicTracks,
  musicLoading,
  currentMusicTrackId,
  musicEditable,
  musicRemoveEditable = musicEditable,
  musicSwapDisabledReason,
  musicRemoveDisabledReason,
  backgroundMusic,
  backgroundMusicTrackDurationS,
  onPatch,
  sourceAudioMix,
  sourceAudioOptions,
  onSourceAudioMix,
  onPickMusic,
  onRemoveMusic,
  onPatchBackgroundMusic,
  onRemoveBackgroundMusic,
  musicWindow,
  onClose,
}: {
  level?: number | null;
  editable: boolean;
  disabledReason?: string | null;
  label: string;
  musicTracks: MusicTrackSummary[];
  musicLoading: boolean;
  currentMusicTrackId: string | null;
  musicEditable: boolean;
  musicRemoveEditable?: boolean;
  musicSwapDisabledReason?: string | null;
  musicRemoveDisabledReason?: string | null;
  backgroundMusic?: EditorCommitBackgroundMusic | null;
  backgroundMusicTrackDurationS?: number | null;
  onPatch?: (level: number) => void;
  sourceAudioMix?: "interleaved" | "source_a" | "source_b" | null;
  sourceAudioOptions: Array<{
    mix: "interleaved" | "source_a" | "source_b";
    audio_path: string;
    audio_url: string;
    duration_s: number;
  }>;
  onSourceAudioMix?: (mix: "interleaved" | "source_a" | "source_b") => void;
  onPickMusic?: (trackId: string) => void;
  onRemoveMusic?: () => void;
  onPatchBackgroundMusic?: (patch: Partial<EditorCommitBackgroundMusic>) => void;
  onRemoveBackgroundMusic?: () => void;
  musicWindow?: SongWindowControl;
  onClose: () => void;
}) {
  const safeLevel = Math.max(0, Math.min(1, level ?? 0));
  const hasBackgroundMusic = !!backgroundMusic?.track_id && backgroundMusic.enabled !== false;
  const bedMuted = Boolean(backgroundMusic?.muted);
  const gainDb = Math.max(-40, Math.min(0, backgroundMusic?.gain_db ?? -18));
  const trimStartS = Math.max(0, backgroundMusic?.start_s ?? 0);
  const trimEndS =
    typeof backgroundMusic?.end_s === "number" && Number.isFinite(backgroundMusic.end_s)
      ? Math.max(trimStartS, backgroundMusic.end_s)
      : backgroundMusicTrackDurationS != null
        ? Math.max(trimStartS, backgroundMusicTrackDurationS)
        : trimStartS;
  const clampTrimStart = (value: number) => {
    const maxStart =
      backgroundMusicTrackDurationS != null
        ? Math.max(0, backgroundMusicTrackDurationS - 0.1)
        : Number.POSITIVE_INFINITY;
    return Math.max(0, Math.min(maxStart, value));
  };
  const clampTrimEnd = (value: number) => {
    const maxEnd = backgroundMusicTrackDurationS ?? Number.POSITIVE_INFINITY;
    return Math.max(trimStartS + 0.1, Math.min(maxEnd, value));
  };
  return (
    <div className="px-5 pt-5">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">{label}</h2>
        <CloseX onClose={onClose} />
      </div>
      <div className="mt-4">
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Song</p>
        {sourceAudioOptions.length > 0 && (
          <div className="mb-4 border-b border-zinc-200 pb-4">
            <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Match audio</p>
            <div className="grid grid-cols-3 gap-1 rounded-lg bg-zinc-100 p-1" role="group" aria-label="Match audio mix">
              {sourceAudioOptions.map((option) => (
                <Button
                  key={option.mix}
                  type="button"
                  variant="ghost"
                  aria-pressed={(sourceAudioMix ?? "interleaved") === option.mix}
                  onClick={() => onSourceAudioMix?.(option.mix)}
                  className={(sourceAudioMix ?? "interleaved") === option.mix ? "min-h-9 rounded-md bg-white px-2 text-[11px] font-semibold shadow-sm" : "min-h-9 rounded-md px-2 text-[11px] text-[#71717a]"}
                >
                  {option.mix === "interleaved" ? "A + B" : option.mix === "source_a" ? "Match A" : "Match B"}
                </Button>
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-4 text-[#71717a]">
              Switches the prepared original audio without rerendering the video.
            </p>
          </div>
        )}
        {currentMusicTrackId && (
          <Button
            type="button"
            variant="outline"
            disabled={!musicRemoveEditable}
            title={!musicRemoveEditable ? musicRemoveDisabledReason ?? undefined : undefined}
            onClick={() => onRemoveMusic?.()}
            className="mb-2 h-auto min-h-10 w-full justify-center rounded-lg border-zinc-200 bg-white px-3 text-[12px] font-semibold text-[#71717a] hover:border-zinc-400 hover:bg-white hover:text-[#0c0c0e] disabled:cursor-not-allowed disabled:opacity-45 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Remove music
          </Button>
        )}
        {!musicRemoveEditable && currentMusicTrackId && musicRemoveDisabledReason && (
          <p className="mb-2 text-[11px] leading-4 text-[#71717a]" role="status">
            {musicRemoveDisabledReason}
          </p>
        )}
        {musicEditable ? (
          musicLoading ? (
            <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
              Loading songs...
            </div>
          ) : (
            <div className="max-h-44 space-y-2 overflow-y-auto pr-1">
              {musicTracks.map((track) => {
                const selected = track.id === currentMusicTrackId;
                return (
                  <Button
                    key={track.id}
                    type="button"
                    variant={selected ? "ink" : "outline"}
                    onClick={() => onPickMusic?.(track.id)}
                    className="flex min-h-11 w-full items-center justify-between rounded-lg px-3 text-left text-[13px] font-normal"
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-semibold">{track.title}</span>
                      <span
                        className={
                          selected
                            ? "block truncate text-[11px] text-white/70"
                            : "block truncate text-[11px] text-[#71717a]"
                        }
                      >
                        {track.artist || "Music"}
                      </span>
                    </span>
                    <span className="ml-2 shrink-0 text-[11px]">
                      {track.user_slot_count ? `${track.user_slot_count} clips` : "Song"}
                    </span>
                  </Button>
                );
              })}
              {musicTracks.length === 0 && (
                <div className="rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-[12px] text-[#71717a]">
                  No ready songs found.
                </div>
              )}
            </div>
          )
        ) : (
          <p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-[13px] leading-relaxed text-[#52525b]">
            {musicSwapDisabledReason ?? "Music cannot be changed for this version."}
          </p>
        )}
      </div>
      {musicWindow && (
        <div className="mt-4">
          <SongWindowSelector {...musicWindow} />
        </div>
      )}
      {hasBackgroundMusic && (
        <div className="mt-4 border-t border-zinc-200 pt-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-[12px] font-semibold text-[#3f3f46]">Background bed</p>
            <Button
              type="button"
              variant="outline"
              onClick={onRemoveBackgroundMusic}
              className="min-h-9 rounded-lg px-3 text-[12px] font-semibold text-[#3f3f46]"
            >
              Remove
            </Button>
          </div>
          <label className="mt-3 flex min-h-11 items-center justify-between rounded-lg border border-zinc-200 px-3 text-[13px] text-[#3f3f46]">
            Mute
            <input
              type="checkbox"
              checked={bedMuted}
              onChange={(event) => onPatchBackgroundMusic?.({ muted: event.target.checked })}
              className="h-4 w-4 accent-lime-500"
            />
          </label>
          <div className="mt-4">
            <div className="flex items-center justify-between text-[12px] font-semibold text-[#3f3f46]">
              <span id="editor-background-music-volume-label">Volume</span>
              <span>{Math.round(((gainDb + 40) / 40) * 100)}%</span>
            </div>
            <Slider
              aria-labelledby="editor-background-music-volume-label"
              min={-40}
              max={0}
              step={0.5}
              value={[gainDb]}
              onValueChange={([value]) =>
                onPatchBackgroundMusic?.({ gain_db: value })
              }
              className="mt-2 h-11 cursor-pointer"
            />
          </div>
          <FieldNumber
            label="Trim start"
            value={trimStartS}
            min={0}
            step={0.1}
            onCommit={(value) => {
              const nextStart = clampTrimStart(value);
              onPatchBackgroundMusic?.({
                start_s: nextStart,
                end_s: Math.max(nextStart + 0.1, trimEndS),
              });
            }}
          />
          <FieldNumber
            label="Trim end"
            value={trimEndS}
            min={0.1}
            step={0.1}
            onCommit={(value) => onPatchBackgroundMusic?.({ end_s: clampTrimEnd(value) })}
          />
        </div>
      )}
      {editable ? (
        <div className="mt-4">
          <div className="flex items-center justify-between text-[12px] font-semibold text-[#3f3f46]">
            <span className="flex items-center gap-1">
              <span id="editor-mix-level-label">Bed level</span>
              <InfoDot label="Bed level" size="compact">
                Balances the background bed against your narration.
              </InfoDot>
            </span>
            <span>{Math.round(safeLevel * 100)}%</span>
          </div>
          <Slider
            aria-labelledby="editor-mix-level-label"
            min={0}
            max={1}
            step={0.01}
            value={[safeLevel]}
            onValueChange={([value]) => onPatch?.(value)}
            className="mt-2 h-11 cursor-pointer"
          />
        </div>
      ) : (
        <p className="mt-3 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-[13px] leading-relaxed text-[#52525b]">
          {disabledReason ?? "Bed level is fixed for this edit."}
        </p>
      )}
    </div>
  );
}

function CloseX({ onClose }: { onClose: () => void }) {
  // Sheet mode: the Sheet's own 44px close button is the close affordance;
  // rendering a second ✕ here would duplicate chrome.
  const presentation = useContext(InspectorPresentationContext);
  if (presentation === "sheet") return null;
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label="Close (clears selection)"
      onClick={onClose}
      className="h-7 w-7 rounded-lg text-[13px] font-normal text-[#71717a]"
    >
      ✕
    </Button>
  );
}

function PresetsTab({
  sampleWord,
  appliedPresetId,
  onPickPreset,
}: {
  sampleWord: string | null;
  appliedPresetId: string | null;
  onPickPreset: (preset: TextPreset) => void;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <h2 className="font-display text-[18px] text-[#0c0c0e]">Presets</h2>
      <div className="mt-4">
        <PresetGrid
          presets={TEXT_PRESETS}
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onPick={onPickPreset}
        />
      </div>
    </div>
  );
}

function SfxInspector({
  placement,
  onPatch,
  onDelete,
  onClose,
  editable,
  disabledReason,
}: {
  placement: SoundEffectPlacement;
  onPatch: (id: string, patch: Partial<SoundEffectPlacement>) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
  editable: boolean;
  disabledReason: string | null;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Sound</h2>
        <CloseX onClose={onClose} />
      </div>
      {!editable && disabledReason && (
        <p className="mt-2 text-[11px] leading-4 text-[#71717a]" role="status">
          {disabledReason}
        </p>
      )}
      <fieldset disabled={!editable} className="m-0 min-w-0 border-0 p-0">
      <p className="mt-1 truncate text-[12px] text-[#71717a]">{placement.label ?? "Sound effect"}</p>
      <FieldNumber
        label="Start"
        value={placement.at_s ?? 0}
        min={0}
        step={0.1}
        onCommit={(value) => onPatch(placement.id, { at_s: value })}
      />
      <div className="mt-4 block text-[12px] font-semibold text-[#3f3f46]">
        Volume
        <Slider
          aria-label="Volume"
          disabled={!editable}
          min={0}
          max={2}
          step={0.05}
          value={[placement.gain ?? 1]}
          onValueChange={([value]) => onPatch(placement.id, { gain: value })}
          className="mt-2"
        />
      </div>
      <div className="mt-1 text-right text-[12px] tabular-nums text-[#71717a]">
        {(placement.gain ?? 1).toFixed(2)}x
      </div>
      <DangerButton onClick={() => onDelete(placement.id)}>Delete sound</DangerButton>
      </fieldset>
    </div>
  );
}

function MediaVisualInspector({
  block,
  projectDurationS,
  onPatch,
  onReorder,
  onClose,
}: {
  block: MediaVisualBlock;
  projectDurationS: number;
  onPatch?: (id: string, patch: Partial<MediaVisualBlock>) => void;
  onReorder?: (id: string, move: MediaLayerMove) => void;
  onClose: () => void;
}) {
  const transform = block.transform ?? {
    fit_mode: "contain" as const,
    focal_x: 0.5,
    focal_y: 0.5,
    zoom: 1,
  };
  const patch = (next: Partial<MediaVisualBlock>) => onPatch?.(block.id, next);
  const patchTransform = (next: Partial<MediaVisualBlock["transform"]>) =>
    patch({ transform: { ...transform, ...next } });
  const trimStart = block.trim_start_s ?? 0;
  const trimEnd = block.trim_end_s ?? block.source_duration_s ?? Number.POSITIVE_INFINITY;
  const sourceBudget = Math.max(0.1, trimEnd - trimStart);
  const maximumEnd = Math.min(
    Math.max(block.start_s + 0.1, projectDurationS),
    block.start_s + sourceBudget,
  );
  const clampWindowToSource = (nextTrimStart: number, nextTrimEnd: number) => {
    const available = Math.max(0.1, nextTrimEnd - nextTrimStart);
    return Math.min(block.end_s, block.start_s + available, projectDurationS);
  };
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Media</h2>
        <CloseX onClose={onClose} />
      </div>
      <p className="mt-1 text-[12px] capitalize text-[#71717a]">
        {block.media_kind} · {block.display_mode}
      </p>
      <TimingSection label="Timing (seconds)">
        <TimingNumberInput
          label="Start"
          value={block.start_s}
          min={0}
          onChange={(value) => {
            const earliest = Math.max(0, block.end_s - sourceBudget);
            patch({ start_s: Math.max(earliest, Math.min(value, block.end_s - 0.1)) });
          }}
        />
        <TimingNumberInput
          label="End"
          value={block.end_s}
          min={0.1}
          onChange={(value) =>
            patch({ end_s: Math.max(block.start_s + 0.1, Math.min(maximumEnd, value)) })
          }
        />
      </TimingSection>
      <div className="mt-3 grid grid-cols-2 gap-2 border-b border-zinc-100 pb-3">
        {(["fullscreen", "overlay"] as const).map((mode) => (
          <Button
            key={mode}
            type="button"
            size="sm"
            className="min-h-11"
            variant={block.display_mode === mode ? "default" : "outline"}
            onClick={() => patch({ display_mode: mode })}
          >
            {mode === "fullscreen" ? "Full screen" : "Overlay"}
          </Button>
        ))}
      </div>
      {block.display_mode === "fullscreen" && (
        <>
      <div className="mt-4 border-b border-zinc-100 pb-3">
        <span className="text-[13px] font-bold text-[#0c0c0e]">Fit</span>
        <div className="mt-2 flex gap-2">
          {(["contain", "cover"] as const).map((fit) => (
            <Button
              key={fit}
              type="button"
              size="sm"
              className="min-h-11"
              variant={transform.fit_mode === fit ? "default" : "outline"}
              onClick={() => patchTransform({ fit_mode: fit })}
            >
              {fit === "contain" ? "Fit" : "Fill"}
            </Button>
          ))}
        </div>
      </div>
      <TimingSection label="Focal point">
        <PercentNumberInput label="X" value={Math.round(transform.focal_x * 100)} onChange={(value) => patchTransform({ focal_x: Math.max(0, Math.min(100, value) / 100) })} />
        <PercentNumberInput label="Y" value={Math.round(transform.focal_y * 100)} onChange={(value) => patchTransform({ focal_y: Math.max(0, Math.min(100, value) / 100) })} />
      </TimingSection>
      <div className="mt-3 border-b border-zinc-100 pb-3">
        <div className="mb-2 flex justify-between text-[13px] font-bold text-[#0c0c0e]">
          <span>Zoom</span><span>{transform.zoom.toFixed(1)}×</span>
        </div>
        <Slider
          aria-label="Media zoom"
          min={100}
          max={400}
          step={5}
          value={[Math.round(transform.zoom * 100)]}
          onValueChange={([value]) => patchTransform({ zoom: value / 100 })}
        />
      </div>
        </>
      )}
      {block.media_kind === "video" && block.source_duration_s != null && (
        <TimingSection label={`Source trim · ${block.source_duration_s.toFixed(1)}s available`}>
          <TimingNumberInput
            label="In"
            value={trimStart}
            min={0}
            onChange={(value) => {
              const nextTrimStart = Math.max(0, Math.min(value, trimEnd - 0.1));
              patch({
                trim_start_s: nextTrimStart,
                end_s: clampWindowToSource(nextTrimStart, trimEnd),
              });
            }}
          />
          <TimingNumberInput
            label="Out"
            value={trimEnd}
            min={0.1}
            onChange={(value) => {
              const nextTrimEnd = Math.min(
                block.source_duration_s!,
                Math.max(trimStart + 0.1, value),
              );
              patch({
                trim_end_s: nextTrimEnd,
                end_s: clampWindowToSource(trimStart, nextTrimEnd),
              });
            }}
          />
          <p className="col-span-2 text-[12px] leading-4 text-[#71717a]">
            Timeline handles stop at {maximumEnd.toFixed(1)}s because this video cannot extend
            beyond the selected source range.
          </p>
        </TimingSection>
      )}
      {block.display_mode === "overlay" && (
        <>
      <TimingSection label="Placement">
        <PercentNumberInput label="X" value={Math.round(block.x_frac * 100)} onChange={(value) => patch({ x_frac: Math.max(0, Math.min(100, value) / 100) })} />
        <PercentNumberInput label="Y" value={Math.round(block.y_frac * 100)} onChange={(value) => patch({ y_frac: Math.max(0, Math.min(100, value) / 100) })} />
      </TimingSection>
      <div className="mt-3 border-b border-zinc-100 pb-3">
        <div className="mb-2 flex justify-between text-[13px] font-bold text-[#0c0c0e]">
          <span>Overlay size</span><span>{Math.round(block.scale * 100)}%</span>
        </div>
        <Slider
          aria-label="Media overlay size"
          min={5}
          max={100}
          step={1}
          value={[Math.round(block.scale * 100)]}
          onValueChange={([value]) => patch({ scale: value / 100 })}
        />
      </div>
        </>
      )}
      <div className="mt-3 grid grid-cols-2 gap-2">
        <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => onReorder?.(block.id, "backward")}>Send backward</Button>
        <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => onReorder?.(block.id, "forward")}>Bring forward</Button>
        <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => onReorder?.(block.id, "back")}>Send to back</Button>
        <Button type="button" size="sm" variant="outline" className="min-h-11" onClick={() => onReorder?.(block.id, "front")}>Bring to front</Button>
      </div>
    </div>
  );
}

function OverlayInspector({
  overlay,
  onPatch,
  onPreview,
  onRecord,
  onDelete,
  onClose,
  editable,
  disabledReason,
}: {
  overlay: MediaOverlay;
  onPatch: (id: string, patch: Partial<MediaOverlay>) => void;
  onPreview: (id: string, patch: Partial<MediaOverlay>) => void;
  onRecord: () => void;
  onDelete: (id: string) => void;
  onClose: () => void;
  editable: boolean;
  disabledReason: string | null;
}) {
  const scalePct = Math.round(clampMediaOverlayScale(overlay.scale ?? 0.35) * 100);
  const xPct = Math.round((overlay.x_frac ?? 0.5) * 100);
  const yPct = Math.round((overlay.y_frac ?? 0.5) * 100);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Overlay</h2>
        <CloseX onClose={onClose} />
      </div>
      {!editable && disabledReason && (
        <p className="mt-2 text-[11px] leading-4 text-[#71717a]" role="status">
          {disabledReason}
        </p>
      )}
      <fieldset disabled={!editable} className="m-0 min-w-0 border-0 p-0">
      <p className="mt-1 text-[12px] capitalize text-[#71717a]">{overlay.kind}</p>

      <TimingSection label="Timing">
        <TimingNumberInput
          label="Start"
          value={overlay.start_s}
          min={0}
          onChange={(value) =>
            onPatch(overlay.id, { start_s: Math.min(value, overlay.end_s - 0.3) })
          }
        />
        <TimingNumberInput
          label="End"
          value={overlay.end_s}
          min={0.3}
          onChange={(value) =>
            onPatch(overlay.id, { end_s: Math.max(value, overlay.start_s + 0.3) })
          }
        />
      </TimingSection>

      <TimingSection label="Position">
        <PercentNumberInput
          label="X"
          value={xPct}
          onChange={(value) =>
            onPatch(overlay.id, {
              x_frac: Math.min(100, Math.max(0, value)) / 100,
              position: "custom",
            })
          }
        />
        <PercentNumberInput
          label="Y"
          value={yPct}
          onChange={(value) =>
            onPatch(overlay.id, {
              y_frac: Math.min(100, Math.max(0, value)) / 100,
              position: "custom",
            })
          }
        />
      </TimingSection>

      <div className="mt-3 border-b border-zinc-100 pb-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[13px] font-bold text-[#0c0c0e]">Size</span>
          <span className="text-[12px] tabular-nums text-[#71717a]">{scalePct}%</span>
        </div>
        <div className="flex items-center gap-2">
          <Slider
            aria-label="Overlay scale"
            min={Math.round(MEDIA_OVERLAY_MIN_SCALE * 100)}
            max={Math.round(MEDIA_OVERLAY_MAX_SCALE * 100)}
            step={1}
            value={[scalePct]}
            onValueChange={([value]) =>
              onPatch(overlay.id, { scale: clampMediaOverlayScale(value / 100) })
            }
            className="min-w-0 flex-1"
          />
          <Input
            type="number"
            aria-label="Overlay scale percent"
            min={Math.round(MEDIA_OVERLAY_MIN_SCALE * 100)}
            max={Math.round(MEDIA_OVERLAY_MAX_SCALE * 100)}
            step={1}
            value={scalePct}
            onChange={(e) =>
              onPatch(overlay.id, { scale: clampMediaOverlayScale(Number(e.target.value) / 100) })
            }
            className="h-8 w-16 rounded-lg px-2 text-[12px] tabular-nums text-[#0c0c0e]"
          />
        </div>
      </div>

      <div className="mt-3 block border-b border-zinc-100 pb-3 text-[12px] font-semibold text-[#3f3f46]">
        Exit
        <Select
          value={overlay.exit_token ?? "none"}
          onValueChange={(value) =>
            onPatch(overlay.id, { exit_token: value as MediaOverlay["exit_token"] })
          }
        >
          <SelectTrigger aria-label="Overlay exit" className="mt-1 h-9 px-2 text-[13px] font-normal">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">None</SelectItem>
            <SelectItem value="dissolve-out">Dissolve out</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {overlay.kind === "video" && overlay.clip_duration_s != null && overlay.clip_duration_s > 0 && (
        <VideoOverlaySourceWindow
          overlay={overlay}
          onPatch={onPatch}
          onPreview={onPreview}
          onRecord={onRecord}
        />
      )}
      <DangerButton onClick={() => onDelete(overlay.id)}>Delete overlay</DangerButton>
      </fieldset>
    </div>
  );
}

function PercentNumberInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="min-w-0 text-[12px] text-[#3f3f46]">
      {label}
      <div className="mt-1 flex h-8 items-center rounded-lg border border-zinc-200 px-2 focus-within:border-lime-500/60">
        <Input
          type="number"
          aria-label={`Overlay ${label} percent`}
          min={0}
          max={100}
          step={1}
          value={Number.isFinite(value) ? value : 0}
          onChange={(e) => onChange(Number(e.target.value))}
          className="h-auto min-w-0 flex-1 border-0 bg-transparent p-0 text-[12px] tabular-nums text-[#0c0c0e] focus-visible:ring-0"
        />
        <span className="pl-1 text-[11px] text-[#71717a]">%</span>
      </div>
    </label>
  );
}

function CameraInspector({
  effect,
  onPatch,
  onDelete,
  onClose,
}: {
  effect: CameraEffect;
  onPatch: (id: string, patch: Partial<CameraEffect>) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  const duration = Math.max(
    CAMERA_EFFECT_MIN_DURATION_S,
    Math.min(CAMERA_EFFECT_MAX_DURATION_S, effect.end_s - effect.start_s),
  );
  const intensityPct = Math.round(
    Math.max(0, Math.min(CAMERA_EFFECT_MAX_INTENSITY, effect.intensity)) * 100,
  );
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Camera</h2>
        <CloseX onClose={onClose} />
      </div>

      <TimingSection label="Timing">
        <TimingNumberInput
          label="Start"
          value={effect.start_s}
          min={0}
          onChange={(value) =>
            onPatch(effect.id, {
              start_s: Math.max(0, value),
              end_s: Math.max(0, value) + duration,
            })
          }
        />
        <TimingNumberInput
          label="End"
          value={effect.end_s}
          min={effect.start_s + CAMERA_EFFECT_MIN_DURATION_S}
          onChange={(value) =>
            onPatch(effect.id, {
              end_s: Math.max(effect.start_s + CAMERA_EFFECT_MIN_DURATION_S, value),
            })
          }
        />
      </TimingSection>

      <div className="mt-3 border-b border-zinc-100 pb-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[13px] font-bold text-[#0c0c0e]">Duration</span>
          <span className="text-[12px] tabular-nums text-[#71717a]">
            {duration.toFixed(1)}s
          </span>
        </div>
        <input
          type="range"
          aria-label="Camera focus duration"
          min={CAMERA_EFFECT_MIN_DURATION_S}
          max={CAMERA_EFFECT_MAX_DURATION_S}
          step={0.1}
          value={duration}
          onChange={(e) => {
            const next = Number(e.target.value);
            if (Number.isFinite(next)) {
              onPatch(effect.id, { end_s: effect.start_s + next });
            }
          }}
          className="w-full accent-[#0c0c0e]"
        />
      </div>

      <div className="mt-3 border-b border-zinc-100 pb-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[13px] font-bold text-[#0c0c0e]">Intensity</span>
          <span className="text-[12px] tabular-nums text-[#71717a]">{intensityPct}%</span>
        </div>
        <input
          type="range"
          aria-label="Camera focus intensity"
          min={0}
          max={Math.round(CAMERA_EFFECT_MAX_INTENSITY * 100)}
          step={1}
          value={intensityPct}
          onChange={(e) => {
            const next = Number(e.target.value);
            if (Number.isFinite(next)) {
              onPatch(effect.id, { intensity: next / 100 });
            }
          }}
          className="w-full accent-[#0c0c0e]"
        />
      </div>

      <DangerButton onClick={() => onDelete(effect.id)}>Delete camera effect</DangerButton>
    </div>
  );
}

function VideoOverlaySourceWindow({
  overlay,
  onPatch,
  onPreview,
  onRecord,
}: {
  overlay: MediaOverlay;
  onPatch: (id: string, patch: Partial<MediaOverlay>) => void;
  onPreview: (id: string, patch: Partial<MediaOverlay>) => void;
  onRecord: () => void;
}) {
  const dragRef = useRef<{
    handle: BarDragHandle;
    startClientX: number;
    barWidth: number;
    origin: { inS: number; durationS: number };
  } | null>(null);
  const sourceDurationS = Math.max(overlay.clip_duration_s ?? 0, 0.3);
  const inS = overlay.clip_trim_start_s ?? 0;
  const outS = overlay.clip_trim_end_s ?? sourceDurationS;
  const durationS = Math.max(0.3, outS - inS);
  const rangeLeftPct = sourceDurationS > 0 ? (inS / sourceDurationS) * 100 : 0;
  const rangeWidthPct = sourceDurationS > 0 ? (durationS / sourceDurationS) * 100 : 100;

  function patchWindow(trimStartS: number, trimEndS: number, preview: boolean) {
    const next = applyMediaOverlaySourceWindowInput({
      trimStartS,
      trimEndS,
      clipDurationS: sourceDurationS,
    });
    const nextDuration = Math.max(
      0.3,
      (next.clip_trim_end_s ?? sourceDurationS) - (next.clip_trim_start_s ?? 0),
    );
    const patch = {
      ...next,
      end_s: Math.round((overlay.start_s + nextDuration) * 10) / 10,
    };
    if (preview) onPreview(overlay.id, patch);
    else onPatch(overlay.id, patch);
  }

  function startRangeDrag(
    e: React.PointerEvent<HTMLElement>,
    handle: BarDragHandle,
  ) {
    e.preventDefault();
    e.stopPropagation();
    const bar = e.currentTarget.closest<HTMLElement>("[data-overlay-source-range]");
    if (!bar) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      handle,
      startClientX: e.clientX,
      barWidth: Math.max(1, bar.getBoundingClientRect().width),
      origin: { inS, durationS },
    };
    onRecord();
  }

  function updateRangeDrag(e: React.PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const deltaS = ((e.clientX - drag.startClientX) / drag.barWidth) * sourceDurationS;
    const next = applyClipSourceWindowDrag({
      slot: drag.origin,
      handle: drag.handle,
      deltaS,
      sourceDurationS,
      minDurationS: 0.3,
    });
    const nextIn = next.inS;
    const nextOut = next.inS + (next.durationS ?? drag.origin.durationS);
    patchWindow(nextIn, nextOut, true);
  }

  function finishRangeDrag(e: React.PointerEvent<HTMLElement>) {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    dragRef.current = null;
  }

  return (
    <div className="mt-3 border-b border-zinc-100 pb-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[13px] font-bold text-[#0c0c0e]">Source crop</span>
        <span className="text-[11px] tabular-nums text-[#71717a]">
          {durationS.toFixed(1)}s of {sourceDurationS.toFixed(1)}s
        </span>
      </div>
      <div className="mb-1 flex items-center justify-between text-[11px] tabular-nums text-[#71717a]">
        <span>{formatTimecode(0)}</span>
        <span>{formatTimecode(sourceDurationS)}</span>
      </div>
      <div
        data-overlay-source-range
        className="relative h-11 rounded-lg border border-zinc-200 bg-zinc-100"
        aria-label="Overlay source range"
      >
        <div
          className="absolute top-1/2 h-7 -translate-y-1/2 rounded-md bg-[#0c0c0e] shadow-sm"
          style={{
            left: `${Math.max(0, Math.min(100, rangeLeftPct))}%`,
            width: `${Math.max(2, Math.min(100, rangeWidthPct))}%`,
          }}
        >
          <Button
            type="button"
            variant="ghost"
            aria-label="Slide overlay source window"
            onPointerDown={(e) => startRangeDrag(e, "body")}
            onPointerMove={updateRangeDrag}
            onPointerUp={finishRangeDrag}
            onPointerCancel={finishRangeDrag}
            className="absolute inset-0 h-auto w-auto cursor-grab rounded-md bg-transparent p-0 hover:bg-transparent active:cursor-grabbing"
          />
          <RangeHandle
            side="left"
            onPointerDown={(e) => startRangeDrag(e, "left")}
            onPointerMove={updateRangeDrag}
            onPointerUp={finishRangeDrag}
            onPointerCancel={finishRangeDrag}
          />
          <RangeHandle
            side="right"
            onPointerDown={(e) => startRangeDrag(e, "right")}
            onPointerMove={updateRangeDrag}
            onPointerUp={finishRangeDrag}
            onPointerCancel={finishRangeDrag}
          />
        </div>
        <div
          className="pointer-events-none absolute top-1/2 h-7 -translate-y-1/2 rounded-md border-2 border-lime-500"
          style={{
            left: `${Math.max(0, Math.min(100, rangeLeftPct))}%`,
            width: `${Math.max(2, Math.min(100, rangeWidthPct))}%`,
          }}
          aria-hidden
        />
      </div>
      <TimingSection label="Source">
        <TimingNumberInput
          label="In"
          value={inS}
          min={0}
          max={sourceDurationS}
          onChange={(value) => patchWindow(value, outS, false)}
        />
        <TimingNumberInput
          label="Out"
          value={outS}
          min={0}
          max={sourceDurationS}
          onChange={(value) => patchWindow(inS, value, false)}
        />
      </TimingSection>
    </div>
  );
}

function FieldNumber({
  label,
  value,
  min,
  step,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  step: number;
  onCommit: (value: number) => void;
}) {
  return (
    <label className="mt-4 block text-[12px] font-semibold text-[#3f3f46]">
      {label}
      <Input
        type="number"
        min={min}
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(e) => onCommit(Number(e.target.value))}
        className="mt-2 min-h-10 rounded-lg px-3 text-[13px] text-[#0c0c0e]"
      />
    </label>
  );
}

function DangerButton({
  children,
  onClick,
}: {
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <Button
      type="button"
      variant="outline"
      onClick={onClick}
      className="mt-5 min-h-11 w-full rounded-lg border-red-200 text-[13px] font-semibold text-red-600 hover:bg-red-50 focus-visible:outline-red-500"
    >
      {children}
    </Button>
  );
}

// ── Text inspector ────────────────────────────────────────────────────────────

function TextInspector({
  bar,
  contentRef,
  onEditText,
  onPatch,
  onPreviewTextMotion,
  onBeginTextMotion,
  onCommitTextMotion,
  onSetTextBoxPosition,
  boxPositionXFrac,
  onPatchTiming,
  videoDurationS,
  smartPlaceAvailable,
  onSmartPlace,
  onMergeCaptionCue,
  onOpenCaptionsPanel,
  captionsPanelOpen = false,
  canMergeCaptionPrev = false,
  canMergeCaptionNext = false,
  onClose,
}: {
  bar: TextElementBar;
  contentRef: React.RefObject<HTMLTextAreaElement>;
  onEditText: (text: string) => void;
  onPatch: (patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onPreviewTextMotion?: (patch: Partial<TextMotionConfigV2>) => void;
  onBeginTextMotion?: () => void;
  onCommitTextMotion?: (patch: Partial<TextMotionConfigV2>) => void;
  onSetTextBoxPosition?: (position: TextBoxHorizontalPosition) => void;
  boxPositionXFrac?: number;
  onPatchTiming: (patch: { start_s?: number; end_s?: number }) => void;
  videoDurationS: number;
  smartPlaceAvailable: boolean;
  onSmartPlace?: () => void;
  onMergeCaptionCue?: (direction: "prev" | "next") => void;
  onOpenCaptionsPanel?: () => void;
  captionsPanelOpen?: boolean;
  canMergeCaptionPrev?: boolean;
  canMergeCaptionNext?: boolean;
  onClose: () => void;
}) {
  // Stroke row starts expanded when the bar already carries a stroke.
  const [strokeOpen, setStrokeOpen] = useState((bar.stroke_width ?? 0) > 0);
  const isLyric = bar.role === "lyric_line";
  const isCaption = bar.role === "narrated_caption";
  // 4b: role badge + Emphasize toggle. smart_role is server-authored/read-only;
  // the toggle only ever writes smart_style/smart_emphasis (see editor-bars.ts
  // smartStyleForRole).
  const smartRoleBadge = isCaption && bar.smart_role ? SMART_ROLE_BADGE_LABELS[bar.smart_role] : null;
  const isEmphasized = isCaption && bar.smart_emphasis === true;
  // AI sequence badge: the transcript-synced editorial sequence / rhythm-mode
  // quote is server-authored, not user-typed — surface provenance so users
  // aren't confused by text they never wrote (see isAiSequenceBar's doc comment
  // for why role alone can't distinguish it from user-composed sequence bars).
  const isAiSequence = isAiSequenceBar(bar);

  const sizeValue = Math.round(bar.size_px ?? 64);
  // Lane PR-A per-cue overrides ("This caption" section): effective value is
  // the override when set, else the "All captions" variant default (`bar.*`
  // above, on a caption bar, already holds that global preview — see
  // convertCaptionCues). `!= null` treats both undefined (never touched) and
  // null (explicitly cleared) as "no active override".
  const hasAnyCueOverride =
    bar.cue_font_family != null || bar.cue_text_color != null || bar.cue_size_px != null;
  const cueEffectiveSizePx = Math.round(bar.cue_size_px ?? sizeValue);
  // Read-only preview: how much bigger this cue burns relative to the base
  // caption size, given its smart_style. Not editable here — the chunker/burn
  // owns the real geometry (see smartCaptionPreviewSizePx's doc comment).
  const smartPreviewSizePx = isCaption
    ? smartCaptionPreviewSizePx(sizeValue, bar.smart_style)
    : sizeValue;
  const clampedSlider = Math.min(
    EDITOR_TEXT_SIZE_MAX,
    Math.max(EDITOR_TEXT_SIZE_MIN, sizeValue),
  );
  const canEditTextCase = isParityVerified("text_case");
  const canEditLetterSpacing = isParityVerified("letter_spacing");
  const canEditLineSpacing = isParityVerified("line_spacing");
  const canEditMaxWidth = isParityVerified("max_width_frac");
  const widthPct = Math.round((bar.max_width_frac ?? MAX_LINE_W_FRAC) * 100);
  const alignment = (bar.alignment ?? "center") as TextHorizontalAlignment;
  const boxPosition = inferTextBoxPosition({
    alignment,
    xFrac: boxPositionXFrac ?? bar.x_frac,
    maxWidthFrac: bar.max_width_frac,
  });

  // Read-only rows: any bar field carrying a value that has no editable row
  // here and isn't plumbing. Unverified fields (future server data) also land
  // here — the panel shows what it preserves (D17).
  const readOnlyRows = useMemo(() => {
    const rows: Array<{ key: string; value: string; verified: boolean }> = [];
    for (const [key, value] of Object.entries(bar)) {
      if (value === undefined || value === null || value === "") continue;
      if (EDITABLE_ROW_FIELDS.has(key)) continue;
      if (INSPECTOR_INTERNAL_FIELDS.has(key)) continue;
      rows.push({
        key,
        value: typeof value === "object" ? JSON.stringify(value) : String(value),
        verified: isParityVerified(key),
      });
    }
    return rows;
  }, [bar]);

  return (
    // Populate motion: 150ms fade/slide-in on selection (plan's motion #1),
    // motion-safe guarded. Keyed by bar.id in the parent so it re-runs per
    // selection, not per keystroke.
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-6 pt-4 motion-safe:animate-fade-up motion-safe:[animation-duration:150ms]">
      <div className="flex items-center justify-between">
        <h2 className="flex items-center gap-2 font-display text-[18px] text-[#0c0c0e]">
          {isCaption ? "Captions" : "Text"}
          {smartRoleBadge && (
            <span
              aria-label={`Caption role: ${smartRoleBadge}`}
              title="AI-assigned caption role"
              className="rounded border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[#71717a]"
            >
              {smartRoleBadge}
            </span>
          )}
          {isAiSequence && (
            <span
              aria-label={AI_SEQUENCE_BADGE_LABEL}
              title={AI_SEQUENCE_BADGE_TOOLTIP}
              className="rounded border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[#71717a]"
            >
              {AI_SEQUENCE_BADGE_LABEL}
            </span>
          )}
          {isLyric && (
            <span
              aria-label="Lyric timing locked"
              title="Lyric timing is locked to the vocal"
              className="rounded border border-zinc-200 px-1.5 py-0.5 text-[10px] font-semibold text-[#71717a]"
            >
              {"\u{1F512}"} Locked
            </span>
          )}
        </h2>
        <CloseX onClose={onClose} />
      </div>

      {/* Content */}
      <Textarea
        ref={contentRef}
        value={bar.text}
        onChange={(e) => onEditText(e.target.value)}
        rows={3}
        aria-label="Text content"
        className="mt-3 min-h-0 resize-none rounded-lg px-3 py-2 text-[13px] text-[#0c0c0e]"
      />
      {!isLyric && (
        <>
          {!isCaption && (
            <Button
              type="button"
              variant="outline"
              disabled={!smartPlaceAvailable}
              onClick={onSmartPlace}
              className="mt-2 min-h-9 w-full rounded-lg px-3 text-[12px] font-semibold text-[#0c0c0e] disabled:bg-zinc-50 disabled:text-[#a1a1aa]"
            >
              Smart place
            </Button>
          )}

          <TimingSection label="Timing">
            <TimingNumberInput
              label="Start"
              value={bar.start_s}
              min={0}
              onChange={(value) => onPatchTiming({ start_s: value })}
            />
            <TimingNumberInput
              label="End"
              value={bar.end_s}
              min={0}
              onChange={(value) => onPatchTiming({ end_s: value })}
            />
          </TimingSection>
        </>
      )}

      {/* 4b: Smart Captions emphasis + orphan-fragment merge, PLUS (Lane PR-A)
          per-cue Font/Color/Size overrides — everything here applies to THIS
          caption line only. Read-only role badge lives in the heading above. */}
      {isCaption && (
        <div className="mt-4 border-b border-zinc-100 pb-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1">
              <span className="text-[13px] font-bold text-[#0c0c0e]">This caption</span>
              <InfoDot label="This caption" size="compact">
                Changes only this line. Use &ldquo;Match all captions&rdquo; to clear it.
              </InfoDot>
            </span>
            {hasAnyCueOverride && (
              <Button
                type="button"
                variant="link"
                onClick={() =>
                  onPatch({ cue_font_family: null, cue_text_color: null, cue_size_px: null })
                }
                className="h-auto p-0 text-[11px] font-semibold text-[#71717a] underline hover:text-[#0c0c0e]"
              >
                Match all captions
              </Button>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              aria-pressed={isEmphasized}
              onClick={() =>
                onPatch(
                  isEmphasized
                    ? { smart_emphasis: false, smart_style: null }
                    : { smart_emphasis: true, smart_style: smartStyleForRole(bar.smart_role) },
                )
              }
              className={`min-h-8 rounded-full px-3 text-[12px] font-semibold ${
                isEmphasized
                  ? "border-lime-600 bg-lime-50 text-lime-700 hover:bg-lime-100"
                  : "text-[#3f3f46]"
              }`}
            >
              {isEmphasized ? "★ Emphasized" : "Emphasize"}
            </Button>
            {onMergeCaptionCue && (canMergeCaptionPrev || canMergeCaptionNext) && (
              <>
                <Button
                  type="button"
                  variant="outline"
                  disabled={!canMergeCaptionPrev}
                  onClick={() => onMergeCaptionCue("prev")}
                  title="Merge with the previous caption"
                  className="min-h-8 rounded-full px-3 text-[12px] font-semibold text-[#3f3f46]"
                >
                  Merge previous
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  disabled={!canMergeCaptionNext}
                  onClick={() => onMergeCaptionCue("next")}
                  title="Merge with the next caption"
                  className="min-h-8 rounded-full px-3 text-[12px] font-semibold text-[#3f3f46]"
                >
                  Merge next
                </Button>
              </>
            )}
          </div>

          <div className="mt-3">
            <FontSelect
              value={bar.cue_font_family ?? bar.font_family ?? null}
              onChange={(name) => onPatch({ cue_font_family: name })}
              ariaLabelPrefix="This caption's font"
            />
          </div>
          <div className="mt-2 flex items-center gap-2">
            <input
              type="color"
              aria-label="This caption's fill color"
              value={
                normalizeEditableHex(bar.cue_text_color ?? bar.color) ?? "#FFFFFF"
              }
              onChange={(e) =>
                onPatch({ cue_text_color: e.target.value.toUpperCase() })
              }
              className="h-6 w-8 cursor-pointer rounded border border-zinc-300 bg-white p-0"
            />
            <HexInput
              value={bar.cue_text_color ?? bar.color ?? "#FFFFFF"}
              onChange={(hex) => onPatch({ cue_text_color: hex })}
              ariaLabel="This caption's fill color hex"
            />
            <span className="text-[11px] text-[#71717a]">Color</span>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <Slider
              aria-label="This caption's font size"
              min={36}
              max={160}
              step={1}
              value={[cueEffectiveSizePx]}
              onValueChange={([value]) => onPatch({ cue_size_px: value })}
              className="min-w-0 flex-1"
            />
            <span className="w-10 text-right text-[12px] tabular-nums text-[#71717a]">
              {cueEffectiveSizePx}
            </span>
            <span className="text-[11px] text-[#71717a]">Size</span>
          </div>
        </div>
      )}

      {/* Variant-wide caption styling lives in the Captions rail tool: it is
          GLOBAL, so requiring a cue selection to reach it inverted the
          hierarchy.

          Shown ONLY when that panel is closed. With the panel already open on
          the left, "Open the Captions panel" points at something the user is
          demonstrably already looking at — it reads as a broken instruction,
          not a shortcut. Nothing to say in that case: the controls are on
          screen. */}
      {isCaption && !captionsPanelOpen && onOpenCaptionsPanel && (
        <div className="mt-6 border-t border-zinc-100 pt-3">
          <Button
            type="button"
            variant="link"
            onClick={onOpenCaptionsPanel}
            className="h-auto p-0 text-[12px] font-semibold text-lime-700 underline underline-offset-2 hover:text-lime-800"
          >
            Edit all captions
          </Button>
        </div>
      )}

      {/* Font + size */}
      {!isCaption && (
        <>
          <div className="mt-3">
            <FontSelect
              value={bar.font_family ?? null}
              onChange={(name) => onPatch({ font_family: name })}
            />
          </div>
          <div className="mt-2 flex items-center gap-2">
            <Select
              value={SIZE_OPTIONS.includes(sizeValue) ? String(sizeValue) : "custom"}
              onValueChange={(value) => {
                const v = Number(value);
                if (Number.isFinite(v) && v > 0) onPatch({ size_px: v, size_class: undefined });
              }}
            >
              <SelectTrigger aria-label="Font size" className="h-9 w-[72px] px-2 text-[13px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {!SIZE_OPTIONS.includes(sizeValue) && (
                  <SelectItem value="custom">{sizeValue}</SelectItem>
                )}
                {SIZE_OPTIONS.map((s) => (
                  <SelectItem key={s} value={String(s)}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <input
              type="range"
              aria-label="Font size (fine)"
              min={EDITOR_TEXT_SIZE_MIN}
              max={EDITOR_TEXT_SIZE_MAX}
              step={1}
              value={clampedSlider}
              onChange={(e) => onPatch({ size_px: Number(e.target.value), size_class: undefined })}
              className="min-w-0 flex-1 accent-[#0c0c0e]"
            />
          </div>
        </>
      )}
      {isCaption && smartPreviewSizePx !== sizeValue && (
        <p className="mt-1 text-[11px] text-[#71717a]">
          Burns bigger for this role — ~{smartPreviewSizePx}px, not editable here
        </p>
      )}

      {canEditMaxWidth && !isLyric && !isCaption && (
        <div className="mt-2 flex items-center gap-2">
          <span className="w-[44px] text-[12px] font-semibold text-[#3f3f46]">Width</span>
          <input
            type="range"
            aria-label="Text width"
            min={MAX_WIDTH_FRAC_MIN * 100}
            max={MAX_WIDTH_FRAC_MAX * 100}
            step={1}
            value={widthPct}
            onChange={(e) => onPatch({ max_width_frac: Number(e.target.value) / 100 })}
            className="min-w-0 flex-1 accent-[#0c0c0e]"
          />
          <Input
            type="number"
            aria-label="Text width percent"
            min={MAX_WIDTH_FRAC_MIN * 100}
            max={MAX_WIDTH_FRAC_MAX * 100}
            step={1}
            value={widthPct}
            onChange={(e) => onPatch({ max_width_frac: Number(e.target.value) / 100 })}
            className="h-9 w-[64px] rounded-lg px-2 text-right text-[12px] tabular-nums text-[#0c0c0e]"
          />
        </div>
      )}

      {!isLyric && !isCaption && (
        <div className="mt-4">
          <span className="block text-[12px] font-semibold text-[#3f3f46]">
            Text alignment
          </span>
          <div className="mt-1 flex gap-1" role="group" aria-label="Text alignment">
            {(["left", "center", "right"] as const).map((nextAlignment) => (
              <Button
                key={nextAlignment}
                type="button"
                variant={alignment === nextAlignment ? "ink" : "outline"}
                onClick={() => {
                  if (nextAlignment === alignment) return;
                  onPatch({
                    alignment: nextAlignment,
                    x_frac: xFracForTextAlignment({
                      alignment,
                      nextAlignment,
                      xFrac: bar.x_frac,
                      maxWidthFrac: bar.max_width_frac,
                    }),
                    position: "custom",
                    y_frac: resolveTextElementYFrac(bar.position, bar.y_frac),
                  });
                }}
                aria-pressed={alignment === nextAlignment}
                aria-label={`Align text ${nextAlignment}`}
                className={`min-h-11 flex-1 rounded-lg px-2 text-[12px] ${
                  alignment === nextAlignment ? "font-semibold" : "font-normal text-[#3f3f46]"
                }`}
              >
                {nextAlignment[0].toUpperCase() + nextAlignment.slice(1)}
              </Button>
            ))}
          </div>
        </div>
      )}

      {!isLyric && !isCaption && (
        <div className="mt-3">
          <span className="block text-[12px] font-semibold text-[#3f3f46]">
            Box position
          </span>
          <div className="mt-1 flex gap-1" role="group" aria-label="Box position">
            {(["left", "center", "right"] as const).map((nextPosition) => (
              <Button
                key={nextPosition}
                type="button"
                variant={boxPosition === nextPosition ? "ink" : "outline"}
                onClick={() => {
                  if (boxPosition === nextPosition) return;
                  if (onSetTextBoxPosition) {
                    onSetTextBoxPosition(nextPosition);
                    return;
                  }
                  onPatch({
                    x_frac: xFracForTextBoxPosition({
                      alignment,
                      position: nextPosition,
                      maxWidthFrac: bar.max_width_frac,
                    }),
                    position: "custom",
                    y_frac: resolveTextElementYFrac(bar.position, bar.y_frac),
                  });
                }}
                aria-pressed={boxPosition === nextPosition}
                aria-label={`Place box ${nextPosition}`}
                className={`min-h-11 flex-1 rounded-lg px-2 text-[12px] ${
                  boxPosition === nextPosition ? "font-semibold" : "font-normal text-[#3f3f46]"
                }`}
              >
                {nextPosition[0].toUpperCase() + nextPosition.slice(1)}
              </Button>
            ))}
          </div>
        </div>
      )}

      {!isLyric && !isCaption && (
        <>
          <div className="mt-4 block text-[12px] font-semibold text-[#3f3f46]">
            Animation
            <Select
              value={bar.effect ?? "none"}
              onValueChange={(value) =>
                onPatch(
                  TEXT_MOTION_V2_UI_ENABLED
                    ? motionPatchForEffect(bar, value, videoDurationS)
                    : { effect: value },
                )
              }
            >
              <SelectTrigger aria-label="Animation" className="mt-1 h-9 px-2 text-[13px] font-normal">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {bar.effect === "smooth-type" && !TEXT_MOTION_V2_UI_ENABLED && (
                  <SelectItem value="smooth-type" disabled>
                    Smooth type (saved; unavailable)
                  </SelectItem>
                )}
                {/* Preserve an effect value outside the visible picker list (e.g. "static"). */}
                {bar.effect &&
                  bar.effect !== "smooth-type" &&
                  !TEXT_ELEMENT_ANIMATIONS.some((a) => a.value === bar.effect) && (
                  <SelectItem value={bar.effect}>{bar.effect}</SelectItem>
                )}
                {TEXT_ELEMENT_ANIMATIONS.filter(
                  (animation) =>
                    animation.value !== "smooth-type" || TEXT_MOTION_V2_UI_ENABLED,
                ).map((a) => (
                  <SelectItem key={a.value} value={a.value}>
                    {a.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {TEXT_MOTION_V2_UI_ENABLED && bar.motion?.version === 2 &&
            bar.effect && textMotionHasControls(bar.effect) && (
              <TextMotionControls
                effect={bar.effect}
                motion={bar.motion}
                onChange={(motionPatch) =>
                  (onCommitTextMotion ?? ((patch) =>
                    onPatch(motionPatchForConfig(bar, patch, videoDurationS))))(motionPatch)
                }
                onPreview={onPreviewTextMotion}
                onBegin={onBeginTextMotion}
                onResetLegacy={() =>
                  onPatch(
                    bar.effect === "smooth-type"
                      ? { effect: "static", motion: null }
                      : { motion: null },
                  )
                }
              />
            )}

          <div className="mt-3 block text-[12px] font-semibold text-[#3f3f46]">
            Theme transition
            <Select
              value={bar.theme_transition?.type ?? "none"}
              onValueChange={(value) =>
                onPatch({
                  theme_transition:
                    value === "none" ? null : { type: "giant-title-wipe" },
                })
              }
            >
              <SelectTrigger aria-label="Theme transition" className="mt-1 h-9 px-2 text-[13px] font-normal">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                {THEME_TRANSITIONS.map((transition) => (
                  <SelectItem key={transition.value} value={transition.value}>
                    {transition.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {bar.theme_transition?.type === "giant-title-wipe" && (
            <label className="mt-3 block text-[12px] font-semibold text-[#3f3f46]">
              Target glyph
              <Input
                aria-label="Target glyph"
                type="text"
                maxLength={1}
                value={bar.theme_transition.target_glyph ?? ""}
                onChange={(e) =>
                  onPatch({
                    theme_transition: {
                      type: "giant-title-wipe",
                      target_glyph: e.target.value.slice(0, 1) || null,
                    },
                  })
                }
                placeholder="center"
                className="mt-1 h-9 px-2 text-[13px] font-normal"
              />
            </label>
          )}
        </>
      )}

      {/* Style — for a caption bar the "All captions" header above already
          labels this same contiguous block (Font/size through Stroke below),
          so it doesn't get a second, redundant header here. */}
      {!isCaption && (
        <div className="mt-6 flex items-center justify-between border-b border-zinc-100 pb-2">
          <span className="text-[13px] font-bold text-[#0c0c0e]">Style</span>
        </div>
      )}

      {canEditTextCase && !isLyric && !isCaption && (
        <div className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Aa case</span>
          <Select
            value={bar.text_case ?? "none"}
            onValueChange={(value) => onPatch({ text_case: value })}
          >
            <SelectTrigger aria-label="Text case" className="h-8 w-[116px] px-2 text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">None</SelectItem>
              <SelectItem value="upper">Upper</SelectItem>
              <SelectItem value="lower">Lower</SelectItem>
              <SelectItem value="title">Title</SelectItem>
            </SelectContent>
          </Select>
        </div>
      )}

      {(canEditLetterSpacing || canEditLineSpacing) && !isLyric && !isCaption && (
        <div className="flex min-h-11 items-center justify-between gap-3 border-b border-zinc-100 py-2">
          {canEditLetterSpacing && (
            <label className="min-w-0 flex-1 text-[12px] text-[#3f3f46]">
              Letter
              <Input
                type="number"
                aria-label="Letter spacing"
                min={LETTER_SPACING_MIN_EM}
                max={LETTER_SPACING_MAX_EM}
                step={0.01}
                value={bar.letter_spacing ?? 0}
                onChange={(e) => onPatch({ letter_spacing: Number(e.target.value) })}
                className="mt-1 h-8 px-2 text-[12px] tabular-nums text-[#0c0c0e]"
              />
            </label>
          )}
          {canEditLineSpacing && (
            <label className="min-w-0 flex-1 text-[12px] text-[#3f3f46]">
              Line
              <Input
                type="number"
                aria-label="Line spacing"
                min={LINE_SPACING_MIN}
                max={LINE_SPACING_MAX}
                step={0.05}
                value={bar.line_spacing ?? LINE_SPACING}
                onChange={(e) => onPatch({ line_spacing: Number(e.target.value) })}
                className="mt-1 h-8 px-2 text-[12px] tabular-nums text-[#0c0c0e]"
              />
            </label>
          )}
        </div>
      )}

      {/* Fill — visible by default (progressive disclosure).
          Caption bars are excluded: on a caption these write the VARIANT-level
          globals, which the Captions panel owns. Per-cue colour lives in the
          "This caption" section above. */}
      {!isCaption && (
        <div className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Fill</span>
          <span className="flex items-center gap-2">
            <input
              type="color"
              aria-label="Fill color"
              value={normalizeEditableHex(bar.color) ?? "#FFFFFF"}
              onChange={(e) => onPatch({ color: e.target.value.toUpperCase() })}
              className="h-6 w-8 cursor-pointer rounded border border-zinc-300 bg-white p-0"
            />
            <HexInput
              value={bar.color ?? "#FFFFFF"}
              onChange={(hex) => onPatch({ color: hex })}
            />
          </span>
        </div>
      )}

      {!isCaption && (
        <div className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Highlight</span>
          <span className="flex items-center gap-2">
            <input
              type="color"
              aria-label="Highlight color"
              value={normalizeEditableHex(bar.highlight_color) ?? "#A3E635"}
              onChange={(e) => onPatch({ highlight_color: e.target.value.toUpperCase() })}
              className="h-6 w-8 cursor-pointer rounded border border-zinc-300 bg-white p-0"
            />
            <HexInput
              value={bar.highlight_color ?? "#A3E635"}
              onChange={(hex) => onPatch({ highlight_color: hex })}
            />
          </span>
        </div>
      )}

      {!isCaption && (
        <div className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Shadow</span>
          <Select
            value={
              bar.shadow_enabled === false
                ? "off"
                : bar.shadow_style === "high_visibility"
                  ? "high_visibility"
                  : "standard"
            }
            onValueChange={(value) => {
              onPatch({
                shadow_enabled: value !== "off",
                shadow_style:
                  value === "high_visibility" ? "high_visibility" : "standard",
              });
            }}
          >
            <SelectTrigger aria-label="Shadow effect" className="h-8 px-2 text-[12px] text-[#3f3f46]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="standard">Standard</SelectItem>
              <SelectItem value="high_visibility">High visibility</SelectItem>
              <SelectItem value="off">Off</SelectItem>
            </SelectContent>
          </Select>
        </div>
      )}

      {TEXT_BEHIND_SUBJECT_UI_ENABLED && !isLyric && !isCaption && (
        <label className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Behind subject</span>
          <input
            type="checkbox"
            checked={bar.behind_subject ?? false}
            onChange={(e) => onPatch({ behind_subject: e.target.checked })}
            className="h-4 w-4 accent-[#0c0c0e]"
          />
        </label>
      )}

      {/* Stroke — collapsed behind + */}
      {!isLyric && !isCaption && (
        <div className="border-b border-zinc-100">
          <div className="flex h-11 items-center justify-between">
            <span className="text-[13px] text-[#3f3f46]">Stroke</span>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              aria-label={strokeOpen ? "Collapse stroke" : "Add stroke"}
              aria-expanded={strokeOpen}
              onClick={() => setStrokeOpen((o) => !o)}
              className="h-6 w-6 rounded-md text-[13px] font-normal leading-none text-[#3f3f46]"
            >
              {strokeOpen ? "–" : "+"}
            </Button>
          </div>
          {strokeOpen && (
            <div className="flex items-center gap-2 pb-3">
              <Slider
                aria-label="Stroke width"
                min={0}
                max={12}
                step={1}
                value={[bar.stroke_width ?? 0]}
                onValueChange={([value]) => onPatch({ stroke_width: value })}
                className="min-w-0 flex-1"
              />
              <span className="w-8 text-right text-[12px] tabular-nums text-[#71717a]">
                {bar.stroke_width ?? 0}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Read-only fields the editor preserves but doesn't edit yet (D17). */}
      {!isLyric && readOnlyRows.length > 0 && (
        <div className="mt-4">
          {readOnlyRows.map((row) => (
            <div
              key={row.key}
              className="flex h-9 items-center justify-between border-b border-zinc-100"
              title={
                row.verified
                  ? "Editable control arrives in a later update"
                  : "Preserved as-is — not yet verified for editing"
              }
            >
              <span className="text-[12px] text-[#a1a1aa]">{fieldLabel(row.key)}</span>
              <span className="max-w-[160px] truncate text-[12px] text-[#71717a]">
                {row.value}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ClipInspector({
  timing,
  onPatchTiming,
  onPatchLook,
  onPatchTransition,
  onMoveClip,
  reorderEditable,
  timingEditable,
  looksEditable,
  transitionsEditable,
  timingDisabledReason,
  reorderDisabledReason,
  looksDisabledReason,
  transitionsDisabledReason,
  availableLookPresets,
  onPatchLookAdjustments,
  onRecordLookAdjustments,
  onPreviewTiming,
  onRecordTimingEdit,
  onClose,
}: {
  timing: InspectorClipTiming;
  onPatchTiming: (patch: { inS?: number; outS?: number; durationS?: number }) => void;
  onPatchLook?: (preset: LookPreset) => void;
  onPatchTransition?: (transition: EditorTransition, durationS?: number) => void;
  onMoveClip?: (direction: -1 | 1) => void;
  reorderEditable: boolean;
  timingEditable: boolean;
  looksEditable: boolean;
  transitionsEditable: boolean;
  timingDisabledReason?: string | null;
  reorderDisabledReason?: string | null;
  looksDisabledReason?: string | null;
  transitionsDisabledReason?: string | null;
  availableLookPresets: LookPreset[];
  onPatchLookAdjustments?: (patch: Partial<LookAdjustments>) => void;
  onRecordLookAdjustments?: () => void;
  onPreviewTiming: (patch: { inS: number; durationS: number }) => void;
  onRecordTimingEdit: () => void;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const lookGroupName = useId();
  const dragRef = useRef<{
    handle: BarDragHandle;
    startClientX: number;
    barWidth: number;
    origin: { inS: number; durationS: number };
  } | null>(null);
  const inS = timing.slot.inS;
  const durationS = timing.durationS;
  const outS = inS + durationS;
  const sourceDurationS =
    timing.sourceDurationS == null
      ? Math.max(outS, CLIP_MIN_DURATION_S)
      : Math.max(timing.sourceDurationS, CLIP_MIN_DURATION_S);
  const rangeLeftPct = sourceDurationS > 0 ? (inS / sourceDurationS) * 100 : 0;
  const rangeWidthPct =
    sourceDurationS > 0 ? (durationS / sourceDurationS) * 100 : 100;
  const selectedLook = timing.slot.lookPreset ?? "none";
  const lookControls = resolveLookAdjustments(
    selectedLook,
    timing.slot.lookAdjustments,
  );
  const lookOptions = useMemo(() => {
    const values: LookPreset[] = ["none"];
    for (const preset of [...availableLookPresets, selectedLook]) {
      if (!values.includes(preset)) values.push(preset);
    }
    return values.map((preset) => [preset, lookPresetLabel(preset)] as const);
  }, [availableLookPresets, selectedLook]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(0, Math.min(inS, sourceDurationS));
  }, [inS, sourceDurationS, timing.sourceUrl]);

  function seekSource(seconds: number) {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(0, Math.min(seconds, sourceDurationS));
  }

  function startRangeDrag(
    e: React.PointerEvent<HTMLElement>,
    handle: BarDragHandle,
  ) {
    if (!timingEditable) return;
    e.preventDefault();
    e.stopPropagation();
    const bar = e.currentTarget.closest<HTMLElement>("[data-source-range-bar]");
    if (!bar) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      handle,
      startClientX: e.clientX,
      barWidth: Math.max(1, bar.getBoundingClientRect().width),
      origin: { inS, durationS },
    };
    onRecordTimingEdit();
    seekSource(handle === "right" ? outS : inS);
  }

  function updateRangeDrag(e: React.PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const deltaS = ((e.clientX - drag.startClientX) / drag.barWidth) * sourceDurationS;
    const next = applyClipSourceWindowDrag({
      slot: drag.origin,
      handle: drag.handle,
      deltaS,
      sourceDurationS,
    });
    onPreviewTiming({
      inS: next.inS,
      durationS: next.durationS ?? drag.origin.durationS,
    });
    const edge =
      drag.handle === "right"
        ? next.inS + (next.durationS ?? drag.origin.durationS)
        : next.inS;
    seekSource(edge);
  }

  function finishRangeDrag(e: React.PointerEvent<HTMLElement>) {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    dragRef.current = null;
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-6 pt-4 motion-safe:animate-fade-up motion-safe:[animation-duration:150ms]">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">
          Clip {timing.clipNumber}
        </h2>
        <CloseX onClose={onClose} />
      </div>
      <div className="mt-3 flex gap-2">
        <Button type="button" variant="outline" disabled={!reorderEditable || !onMoveClip} onClick={() => onMoveClip?.(-1)} className="h-auto min-h-9 flex-1 rounded-lg border-zinc-200 text-[11px] font-semibold disabled:cursor-not-allowed disabled:opacity-45">Move earlier</Button>
        <Button type="button" variant="outline" disabled={!reorderEditable || !onMoveClip} onClick={() => onMoveClip?.(1)} className="h-auto min-h-9 flex-1 rounded-lg border-zinc-200 text-[11px] font-semibold disabled:cursor-not-allowed disabled:opacity-45">Move later</Button>
      </div>
      {!reorderEditable && reorderDisabledReason && (
        <p className="mt-2 text-[11px] leading-4 text-[#71717a]" role="status">
          {reorderDisabledReason}
        </p>
      )}
      <p className="mt-1 text-[12px] font-medium text-[#3f3f46]">
        {durationS.toFixed(1)}s of {sourceDurationS.toFixed(1)}s used · changes
        render on Save
      </p>

      <fieldset className="mt-5">
        <legend className="flex items-center gap-1 text-[12px] font-semibold text-[#3f3f46]">
          <span>Look</span>
          <InfoDot label="Look" size="compact">
            Each look is a color grade applied before captions and graphics. Thumbnails show
            the treatment.
          </InfoDot>
        </legend>
        <div className="mt-2 grid grid-cols-2 gap-2">
          {lookOptions.map(([preset, label]) => {
            const selected = selectedLook === preset;
            return (
              <label
                key={preset}
                className={[
                  "flex min-h-11 cursor-pointer items-center rounded-lg border px-3 py-2 text-left text-[12px] font-semibold",
                  "focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-500",
                  selected
                    ? "border-[#0c0c0e] bg-[#0c0c0e] text-white"
                    : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400",
                ].join(" ")}
              >
                <input
                  className="sr-only"
                  type="radio"
                  name={lookGroupName}
                  value={preset}
                  checked={selected}
                  disabled={!looksEditable}
                  onChange={() => onPatchLook?.(preset)}
                />
                {label}
              </label>
            );
          })}
        </div>
        {!looksEditable && looksDisabledReason && (
          <p className="mt-2 text-[11px] leading-4 text-[#71717a]" role="status">
            {looksDisabledReason}
          </p>
        )}

        {isCustomizableLook(selectedLook) && lookControls && (
          <div className="mt-4 rounded-xl border border-zinc-200 bg-zinc-50/70 p-3">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[#71717a]">
                Customize
              </span>
              <Button
                type="button"
                variant="link"
                disabled={!looksEditable}
                title={!looksEditable ? (looksDisabledReason ?? undefined) : undefined}
                className="h-auto p-0 text-[11px] font-semibold text-[#52525b] underline decoration-zinc-300 underline-offset-2 hover:text-[#0c0c0e]"
                onClick={() => {
                  const defaults = defaultLookAdjustments(selectedLook);
                  if (defaults) {
                    onRecordLookAdjustments?.();
                    onPatchLookAdjustments?.(defaults);
                  }
                }}
              >
                Reset
              </Button>
            </div>
            <div className="mt-3 space-y-3">
              {(
                [
                  ["intensity", "Strength", 0, 100, Math.round(lookControls.intensity * 100), "%"],
                  ["warmth", "Warmth", -100, 100, Math.round(lookControls.warmth * 100), ""],
                  ["contrast", "Contrast", -100, 100, Math.round(lookControls.contrast * 100), ""],
                  ["grain", "Grain", 0, 100, Math.round(lookControls.grain * 100), "%"],
                  ["vignette", "Vignette", 0, 100, Math.round(lookControls.vignette * 100), "%"],
                ] as const
              ).map(([key, label, min, max, value, suffix]) => (
                <div key={key} className="block">
                  <span className="mb-1 flex items-center justify-between text-[11px] text-[#52525b]">
                    <span className="font-medium">{label}</span>
                    <span className="tabular-nums text-[#71717a]">
                      {value > 0 && (key === "warmth" || key === "contrast") ? "+" : ""}
                      {value}
                      {suffix}
                    </span>
                  </span>
                  <Slider
                    aria-label={`Look ${label.toLowerCase()}`}
                    min={min}
                    max={max}
                    step={1}
                    value={[value]}
                    onPointerDown={() => onRecordLookAdjustments?.()}
                    onKeyDown={(event) => {
                      if (
                        ["ArrowDown", "ArrowLeft", "ArrowRight", "ArrowUp", "End", "Home", "PageDown", "PageUp"].includes(
                          event.key,
                        )
                      ) {
                        onRecordLookAdjustments?.();
                      }
                    }}
                    disabled={!looksEditable}
                    onValueChange={([nextValue]) =>
                      onPatchLookAdjustments?.({
                        [key]: nextValue / 100,
                      })
                    }
                    className="block h-11 cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                  />
                </div>
              ))}
            </div>
          </div>
        )}
      </fieldset>

      <fieldset className="mt-5">
        <legend className="text-[12px] font-semibold text-[#3f3f46]">Transition out</legend>
        <div className="mt-2 flex gap-2">
          {(["cut", "crossfade", "dip_to_black", "flash"] as const).map((transition) => (
            <Button
              key={transition}
              type="button"
              variant="outline"
              disabled={!transitionsEditable}
              aria-pressed={(timing.slot.transitionAfter ?? "cut") === transition}
              onClick={() => onPatchTransition?.(transition, transition === "cut" ? undefined : timing.slot.transitionDurationS ?? 0.3)}
              className="h-auto min-h-10 flex-1 rounded-lg border-zinc-200 px-3 text-[12px] font-semibold capitalize disabled:cursor-not-allowed disabled:opacity-45"
            >
              {transition}
            </Button>
          ))}
        </div>
        {!transitionsEditable && transitionsDisabledReason && (
          <p className="mt-2 text-[11px] leading-4 text-[#71717a]" role="status">
            {transitionsDisabledReason}
          </p>
        )}
        {(timing.slot.transitionAfter ?? "cut") !== "cut" && (
          <label className="mt-3 block text-[11px] text-[#52525b]">
            Duration
            <Input
              type="number"
              min={0.1}
              max={0.3}
              step={0.1}
              disabled={!transitionsEditable}
              value={timing.slot.transitionDurationS ?? 0.3}
              onChange={(event) => onPatchTransition?.(timing.slot.transitionAfter ?? "crossfade", Number(event.target.value))}
              className="mt-1 h-10 rounded-lg border-zinc-200 px-3 text-[12px] disabled:opacity-45"
            />
          </label>
        )}
      </fieldset>

      <div className="mt-4">
        <div className="overflow-hidden rounded-lg border border-zinc-200 bg-black">
          {timing.sourceUrl ? (
            <video
              key={timing.sourceUrl}
              ref={videoRef}
              src={timing.sourceUrl}
              muted
              playsInline
              controls
              preload="metadata"
              className="aspect-video w-full bg-black object-contain"
              aria-label={`Clip ${timing.clipNumber} source preview`}
            />
          ) : (
            <div className="flex aspect-video items-center justify-center px-4 text-center text-[12px] text-zinc-300">
              Source preview unavailable
            </div>
          )}
        </div>

        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between text-[11px] tabular-nums text-[#71717a]">
            <span>{formatTimecode(0)}</span>
            <span>{formatTimecode(sourceDurationS)}</span>
          </div>
          <div
            data-source-range-bar
            className="relative h-11 rounded-lg border border-zinc-200 bg-zinc-100 px-0"
            aria-label="Source range"
          >
            <div
              className="absolute top-1/2 h-7 -translate-y-1/2 rounded-md bg-[#0c0c0e] shadow-sm"
              style={{
                left: `${Math.max(0, Math.min(100, rangeLeftPct))}%`,
                width: `${Math.max(2, Math.min(100, rangeWidthPct))}%`,
              }}
            >
              <Button
                type="button"
                variant="ghost"
              aria-label="Slide source window"
              disabled={!timingEditable}
                onPointerDown={(e) => startRangeDrag(e, "body")}
                onPointerMove={updateRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
                className="absolute inset-0 h-auto w-auto cursor-grab rounded-md bg-transparent p-0 hover:bg-transparent active:cursor-grabbing"
              />
              <RangeHandle
                side="left"
                disabled={!timingEditable}
                onPointerDown={(e) => startRangeDrag(e, "left")}
                onPointerMove={updateRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
              />
              <RangeHandle
                side="right"
                disabled={!timingEditable}
                onPointerDown={(e) => startRangeDrag(e, "right")}
                onPointerMove={updateRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
              />
            </div>
            <div
              className="pointer-events-none absolute top-1/2 h-7 -translate-y-1/2 rounded-md border-2 border-lime-500"
              style={{
                left: `${Math.max(0, Math.min(100, rangeLeftPct))}%`,
                width: `${Math.max(2, Math.min(100, rangeWidthPct))}%`,
              }}
              aria-hidden
            />
          </div>
          <div className="mt-1 flex items-center justify-between text-[11px] tabular-nums text-[#3f3f46]">
            <span>In {inS.toFixed(1)}s</span>
            <span>Out {outS.toFixed(1)}s</span>
          </div>
        </div>
      </div>

      <TimingSection label="Timing">
        <TimingNumberInput
          label="In"
          value={inS}
          min={0}
          max={timing.sourceDurationS ?? undefined}
          disabled={!timingEditable}
          onChange={(value) => onPatchTiming({ inS: value })}
        />
        <TimingNumberInput
          label="Out"
          value={outS}
          min={0}
          max={timing.sourceDurationS ?? undefined}
          disabled={!timingEditable}
          onChange={(value) => onPatchTiming({ outS: value })}
        />
        <TimingNumberInput
          label="Dur"
          value={durationS}
          min={CLIP_MIN_DURATION_S}
          disabled={!timingEditable}
          onChange={(value) => onPatchTiming({ durationS: value })}
        />
      </TimingSection>
      {!timingEditable && timingDisabledReason && (
        <p className="mt-2 text-[11px] leading-4 text-[#71717a]" role="status">
          {timingDisabledReason}
        </p>
      )}
    </div>
  );
}

function RangeHandle({
  side,
  disabled = false,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerCancel,
}: {
  side: "left" | "right";
  disabled?: boolean;
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerUp: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerCancel: (e: React.PointerEvent<HTMLElement>) => void;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      disabled={disabled}
      aria-label={side === "left" ? "Trim source in" : "Trim source out"}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      className={`absolute top-1/2 h-8 w-3 -translate-y-1/2 cursor-ew-resize rounded bg-white p-0 text-[#0c0c0e] shadow-sm hover:bg-white disabled:cursor-not-allowed disabled:opacity-45 ${
        side === "left" ? "-left-1.5" : "-right-1.5"
      }`}
    >
      <span className="flex flex-col gap-0.5" aria-hidden>
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
      </span>
    </Button>
  );
}

// ── Small controls ────────────────────────────────────────────────────────────

function TimingSection({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mt-3 border-b border-zinc-100 pb-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[13px] font-bold text-[#0c0c0e]">{label}</span>
      </div>
      <div className="grid grid-cols-2 gap-2">{children}</div>
    </div>
  );
}

function TimingNumberInput({
  label,
  value,
  min,
  max,
  disabled = false,
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <label className="min-w-0 text-[12px] text-[#3f3f46]">
      {label}
      <Input
        type="number"
        aria-label={`${label} seconds`}
        min={min}
        max={max}
        disabled={disabled}
        step={0.1}
        value={Number.isFinite(value) ? value.toFixed(1) : "0.0"}
        onChange={(e) => {
          const next = Number(e.target.value);
          if (Number.isFinite(next)) onChange(next);
        }}
        className="mt-1 h-8 px-2 text-[12px] tabular-nums text-[#0c0c0e]"
      />
    </label>
  );
}
