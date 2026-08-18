"use client";

/**
 * InspectorPanel — the docked right contextual panel (plan §4, Variant A).
 *
 * The ~320px column is PERMANENTLY RESERVED: the canvas never reflows on
 * select/deselect. With nothing selected it shows the quiet serif empty state
 * ("Select anything to edit it") — no icon, per DESIGN.md §9.
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
import { TEXT_ELEMENT_ANIMATIONS, THEME_TRANSITIONS } from "@/lib/overlay-constants";
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
import type { CameraEffect, MediaOverlay, PoolAsset, SoundEffectPlacement } from "@/lib/plan-api";
import type { MotionPresetInstance, MotionPresetPatch } from "@nova/motion-runtime";
import {
  CAMERA_EFFECT_MAX_DURATION_S,
  CAMERA_EFFECT_MAX_INTENSITY,
  CAMERA_EFFECT_MIN_DURATION_S,
} from "@/lib/camera-effects";
import type { MusicTrackSummary } from "@/lib/music-api";
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
  motionScene = null,
  motionDurationS = 0,
  motionAssets = [],
  evolvingTypeEnabled = false,
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
  onPatchClipTiming,
  onPatchClipLook,
  availableLookPresets = [],
  onPatchClipLookAdjustments,
  onRecordClipLookAdjustments,
  onPreviewClipTiming,
  onRecordClipTiming,
  onPatchSfx,
  onDeleteSfx,
  onPatchOverlay,
  onPreviewOverlay,
  onRecordOverlay,
  onDeleteOverlay,
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
  mixLabel,
  musicTracks = [],
  musicLoading = false,
  currentMusicTrackId = null,
  musicEditable = false,
  backgroundMusic = null,
  backgroundMusicTrackDurationS = null,
  onPatchMix,
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
  motionScene?: MotionPresetInstance | null;
  motionDurationS?: number;
  motionAssets?: PoolAsset[];
  evolvingTypeEnabled?: boolean;
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
  onPatchClipTiming: (patch: { inS?: number; outS?: number; durationS?: number }) => void;
  onPatchClipLook?: (preset: LookPreset) => void;
  availableLookPresets?: LookPreset[];
  onPatchClipLookAdjustments?: (patch: Partial<LookAdjustments>) => void;
  onRecordClipLookAdjustments?: () => void;
  onPreviewClipTiming: (patch: { inS: number; durationS: number }) => void;
  onRecordClipTiming: () => void;
  onPatchSfx: (id: string, patch: Partial<SoundEffectPlacement>) => void;
  onDeleteSfx: (id: string) => void;
  onPatchOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onPreviewOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onRecordOverlay: () => void;
  onDeleteOverlay: (id: string) => void;
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
  mixLabel?: string;
  musicTracks?: MusicTrackSummary[];
  musicLoading?: boolean;
  currentMusicTrackId?: string | null;
  musicEditable?: boolean;
  backgroundMusic?: EditorCommitBackgroundMusic | null;
  backgroundMusicTrackDurationS?: number | null;
  onPatchMix?: (level: number) => void;
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
  return (
    <InspectorPresentationContext.Provider value={presentation}>
    <div
      data-region="inspector"
      className={
        presentation === "sheet"
          ? "flex w-full flex-col bg-white"
          : "flex w-[320px] flex-col border-l border-zinc-200 bg-white"
      }
    >
      {/* Sheet mode only: the Basic/Presets switch normally lives in the
          desktop InspectorRail, unreachable from the sheet. Pattern copied
          from ToolDrawer's preset-category tablist. */}
      {presentation === "sheet" && onTab && (
        <div className="flex flex-none px-5 pb-3 pt-1">
          <div
            role="tablist"
            aria-label="Inspector sections"
            className="flex w-full gap-1 rounded-full border border-zinc-200 p-1"
          >
            {(["basic", "presets"] as const).map((nextTab) => {
              const selected = tab === nextTab;
              return (
                <button
                  key={nextTab}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  onClick={() => onTab(nextTab)}
                  className={`inline-flex min-h-11 flex-1 items-center justify-center rounded-full px-4 text-[12px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                    selected
                      ? "bg-[#0c0c0e] font-semibold text-white"
                      : "text-[#3f3f46] hover:bg-zinc-100"
                  }`}
                >
                  {nextTab === "basic" ? "Basic" : "Presets"}
                </button>
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
        <div className="flex flex-1 items-start justify-center px-6 pt-16">
          <p className="font-display text-[16px] leading-relaxed text-[#71717a]">
            Select anything to edit it
          </p>
        </div>
      ) : selection.kind === "text" && bar ? (
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
      ) : selection.kind === "clip" && clipTiming ? (
        <ClipInspector
          timing={clipTiming}
          onPatchTiming={onPatchClipTiming}
          onPatchLook={onPatchClipLook}
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
          onClose={onClose}
        />
      ) : selection.kind === "overlay" && overlay ? (
        <OverlayInspector
          overlay={overlay}
          onPatch={onPatchOverlay}
          onPreview={onPreviewOverlay}
          onRecord={onRecordOverlay}
          onDelete={onDeleteOverlay}
          onClose={onClose}
        />
      ) : selection.kind === "motion" && motionScene ? (
        <MotionInspector
          scene={motionScene}
          durationS={motionDurationS}
          assets={motionAssets}
          evolvingTypeEnabled={evolvingTypeEnabled}
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
          label={mixLabel ?? "Music"}
          musicTracks={musicTracks}
          musicLoading={musicLoading}
          currentMusicTrackId={currentMusicTrackId}
          musicEditable={musicEditable}
          backgroundMusic={backgroundMusic}
          backgroundMusicTrackDurationS={backgroundMusicTrackDurationS}
          onPickMusic={onPickMusic}
          onRemoveMusic={onRemoveMusic}
          onPatchBackgroundMusic={onPatchBackgroundMusic}
          onRemoveBackgroundMusic={onRemoveBackgroundMusic}
          musicWindow={musicWindow}
          onPatch={onPatchMix}
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
  label,
  musicTracks,
  musicLoading,
  currentMusicTrackId,
  musicEditable,
  backgroundMusic,
  backgroundMusicTrackDurationS,
  onPatch,
  onPickMusic,
  onRemoveMusic,
  onPatchBackgroundMusic,
  onRemoveBackgroundMusic,
  musicWindow,
  onClose,
}: {
  level?: number | null;
  editable: boolean;
  label: string;
  musicTracks: MusicTrackSummary[];
  musicLoading: boolean;
  currentMusicTrackId: string | null;
  musicEditable: boolean;
  backgroundMusic?: EditorCommitBackgroundMusic | null;
  backgroundMusicTrackDurationS?: number | null;
  onPatch?: (level: number) => void;
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
        {musicEditable ? (
          musicLoading ? (
            <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
              Loading songs...
            </div>
          ) : (
            <div className="max-h-44 space-y-2 overflow-y-auto pr-1">
              {currentMusicTrackId && (
                <button
                  type="button"
                  onClick={() => onRemoveMusic?.()}
                  className="flex min-h-10 w-full items-center justify-center rounded-lg border border-zinc-200 bg-white px-3 text-[12px] font-semibold text-[#71717a] hover:border-zinc-400 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  Remove music
                </button>
              )}
              {musicTracks.map((track) => {
                const selected = track.id === currentMusicTrackId;
                return (
                  <button
                    key={track.id}
                    type="button"
                    onClick={() => onPickMusic?.(track.id)}
                    className={[
                      "flex min-h-11 w-full items-center justify-between rounded-lg border px-3 text-left text-[13px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500",
                      selected
                        ? "border-[#0c0c0e] bg-[#0c0c0e] text-white"
                        : "border-zinc-200 bg-white text-[#0c0c0e] hover:border-zinc-400",
                    ].join(" ")}
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
                  </button>
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
            Music cannot be edited for this version.
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
            <button
              type="button"
              onClick={onRemoveBackgroundMusic}
              className="min-h-9 rounded-lg border border-zinc-200 px-3 text-[12px] font-semibold text-[#3f3f46] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Remove
            </button>
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
              <label htmlFor="editor-background-music-volume">Volume</label>
              <span>{Math.round(((gainDb + 40) / 40) * 100)}%</span>
            </div>
            <input
              id="editor-background-music-volume"
              type="range"
              min={-40}
              max={0}
              step={0.5}
              value={gainDb}
              onChange={(event) =>
                onPatchBackgroundMusic?.({ gain_db: Number(event.target.value) })
              }
              className="mt-2 h-11 w-full cursor-pointer accent-lime-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
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
              <label htmlFor="editor-mix-level">Bed level</label>
              <InfoDot label="Bed level" size="compact">
                Balances the background bed against your voiceover.
              </InfoDot>
            </span>
            <span>{Math.round(safeLevel * 100)}%</span>
          </div>
          <input
            id="editor-mix-level"
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={safeLevel}
            onChange={(e) => onPatch?.(Number(e.target.value))}
            className="mt-2 h-11 w-full cursor-pointer accent-lime-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          />
        </div>
      ) : (
        <p className="mt-3 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-[13px] leading-relaxed text-[#52525b]">
          Bed level is fixed for this edit.
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
    <button
      type="button"
      aria-label="Close (clears selection)"
      onClick={onClose}
      className="flex h-7 w-7 items-center justify-center rounded-lg text-[13px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
    >
      ✕
    </button>
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
}: {
  placement: SoundEffectPlacement;
  onPatch: (id: string, patch: Partial<SoundEffectPlacement>) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Sound</h2>
        <CloseX onClose={onClose} />
      </div>
      <p className="mt-1 truncate text-[12px] text-[#71717a]">{placement.label ?? "Sound effect"}</p>
      <FieldNumber
        label="Start"
        value={placement.at_s ?? 0}
        min={0}
        step={0.1}
        onCommit={(value) => onPatch(placement.id, { at_s: value })}
      />
      <label className="mt-4 block text-[12px] font-semibold text-[#3f3f46]">
        Volume
        <input
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={placement.gain ?? 1}
          onChange={(e) => onPatch(placement.id, { gain: Number(e.target.value) })}
          className="mt-2 w-full accent-lime-500"
        />
      </label>
      <div className="mt-1 text-right text-[12px] tabular-nums text-[#71717a]">
        {(placement.gain ?? 1).toFixed(2)}x
      </div>
      <DangerButton onClick={() => onDelete(placement.id)}>Delete sound</DangerButton>
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
}: {
  overlay: MediaOverlay;
  onPatch: (id: string, patch: Partial<MediaOverlay>) => void;
  onPreview: (id: string, patch: Partial<MediaOverlay>) => void;
  onRecord: () => void;
  onDelete: (id: string) => void;
  onClose: () => void;
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
          <input
            type="range"
            aria-label="Overlay scale"
            min={Math.round(MEDIA_OVERLAY_MIN_SCALE * 100)}
            max={Math.round(MEDIA_OVERLAY_MAX_SCALE * 100)}
            step={1}
            value={scalePct}
            onChange={(e) =>
              onPatch(overlay.id, { scale: clampMediaOverlayScale(Number(e.target.value) / 100) })
            }
            className="min-w-0 flex-1 accent-[#0c0c0e]"
          />
          <input
            type="number"
            aria-label="Overlay scale percent"
            min={Math.round(MEDIA_OVERLAY_MIN_SCALE * 100)}
            max={Math.round(MEDIA_OVERLAY_MAX_SCALE * 100)}
            step={1}
            value={scalePct}
            onChange={(e) =>
              onPatch(overlay.id, { scale: clampMediaOverlayScale(Number(e.target.value) / 100) })
            }
            className="h-8 w-16 rounded-lg border border-zinc-200 px-2 text-[12px] tabular-nums text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
          />
        </div>
      </div>

      <label className="mt-3 block border-b border-zinc-100 pb-3 text-[12px] font-semibold text-[#3f3f46]">
        Exit
        <select
          aria-label="Overlay exit"
          value={overlay.exit_token ?? "none"}
          onChange={(e) =>
            onPatch(overlay.id, { exit_token: e.target.value as MediaOverlay["exit_token"] })
          }
          className="mt-1 h-9 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px] font-normal text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
        >
          <option value="none">None</option>
          <option value="dissolve-out">Dissolve out</option>
        </select>
      </label>

      {overlay.kind === "video" && overlay.clip_duration_s != null && overlay.clip_duration_s > 0 && (
        <VideoOverlaySourceWindow
          overlay={overlay}
          onPatch={onPatch}
          onPreview={onPreview}
          onRecord={onRecord}
        />
      )}
      <DangerButton onClick={() => onDelete(overlay.id)}>Delete overlay</DangerButton>
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
        <input
          type="number"
          aria-label={`Overlay ${label} percent`}
          min={0}
          max={100}
          step={1}
          value={Number.isFinite(value) ? value : 0}
          onChange={(e) => onChange(Number(e.target.value))}
          className="min-w-0 flex-1 border-0 bg-transparent p-0 text-[12px] tabular-nums text-[#0c0c0e] outline-none"
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
          <button
            type="button"
            aria-label="Slide overlay source window"
            onPointerDown={(e) => startRangeDrag(e, "body")}
            onPointerMove={updateRangeDrag}
            onPointerUp={finishRangeDrag}
            onPointerCancel={finishRangeDrag}
            className="absolute inset-0 cursor-grab rounded-md active:cursor-grabbing"
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
      <input
        type="number"
        min={min}
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(e) => onCommit(Number(e.target.value))}
        className="mt-2 min-h-10 w-full rounded-lg border border-zinc-200 px-3 text-[13px] text-[#0c0c0e] focus:border-lime-500 focus:outline-none focus:ring-2 focus:ring-lime-500/25"
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
    <button
      type="button"
      onClick={onClick}
      className="mt-5 min-h-11 w-full rounded-lg border border-red-200 text-[13px] font-semibold text-red-600 hover:bg-red-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500"
    >
      {children}
    </button>
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
      <textarea
        ref={contentRef}
        value={bar.text}
        onChange={(e) => onEditText(e.target.value)}
        rows={3}
        aria-label="Text content"
        className="mt-3 w-full resize-none rounded-lg border border-zinc-200 px-3 py-2 text-[13px] text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
      />
      {!isLyric && (
        <>
          {!isCaption && (
            <button
              type="button"
              disabled={!smartPlaceAvailable}
              onClick={onSmartPlace}
              className="mt-2 min-h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-[12px] font-semibold text-[#0c0c0e] hover:border-zinc-400 disabled:cursor-not-allowed disabled:bg-zinc-50 disabled:text-[#a1a1aa] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Smart place
            </button>
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
              <button
                type="button"
                onClick={() =>
                  onPatch({ cue_font_family: null, cue_text_color: null, cue_size_px: null })
                }
                className="text-[11px] font-semibold text-[#71717a] underline hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Match all captions
              </button>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              aria-pressed={isEmphasized}
              onClick={() =>
                onPatch(
                  isEmphasized
                    ? { smart_emphasis: false, smart_style: null }
                    : { smart_emphasis: true, smart_style: smartStyleForRole(bar.smart_role) },
                )
              }
              className={`min-h-8 rounded-full border px-3 text-[12px] font-semibold transition-colors ${
                isEmphasized
                  ? "border-lime-600 bg-lime-50 text-lime-700"
                  : "border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
              } focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500`}
            >
              {isEmphasized ? "★ Emphasized" : "Emphasize"}
            </button>
            {onMergeCaptionCue && (canMergeCaptionPrev || canMergeCaptionNext) && (
              <>
                <button
                  type="button"
                  disabled={!canMergeCaptionPrev}
                  onClick={() => onMergeCaptionCue("prev")}
                  title="Merge with the previous caption"
                  className="min-h-8 rounded-full border border-zinc-200 bg-white px-3 text-[12px] font-semibold text-[#3f3f46] hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  {"←"} Merge
                </button>
                <button
                  type="button"
                  disabled={!canMergeCaptionNext}
                  onClick={() => onMergeCaptionCue("next")}
                  title="Merge with the next caption"
                  className="min-h-8 rounded-full border border-zinc-200 bg-white px-3 text-[12px] font-semibold text-[#3f3f46] hover:border-zinc-400 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                >
                  Merge {"→"}
                </button>
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
            <input
              type="range"
              aria-label="This caption's font size"
              min={36}
              max={160}
              step={1}
              value={cueEffectiveSizePx}
              onChange={(e) => onPatch({ cue_size_px: Number(e.target.value) })}
              className="min-w-0 flex-1 accent-[#0c0c0e]"
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
          <button
            type="button"
            onClick={onOpenCaptionsPanel}
            className="text-[12px] font-semibold text-lime-700 underline underline-offset-2 hover:text-lime-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Change font, size or colour for every caption →
          </button>
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
            <select
              aria-label="Font size"
              value={SIZE_OPTIONS.includes(sizeValue) ? sizeValue : ""}
              onChange={(e) => {
                const v = Number(e.target.value);
                if (Number.isFinite(v) && v > 0) onPatch({ size_px: v, size_class: undefined });
              }}
              className="h-9 w-[72px] rounded-lg border border-zinc-200 bg-white px-2 text-[13px] text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
            >
              {!SIZE_OPTIONS.includes(sizeValue) && <option value="">{sizeValue}</option>}
              {SIZE_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
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
          <input
            type="number"
            aria-label="Text width percent"
            min={MAX_WIDTH_FRAC_MIN * 100}
            max={MAX_WIDTH_FRAC_MAX * 100}
            step={1}
            value={widthPct}
            onChange={(e) => onPatch({ max_width_frac: Number(e.target.value) / 100 })}
            className="h-9 w-[64px] rounded-lg border border-zinc-200 px-2 text-right text-[12px] tabular-nums text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
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
              <button
                key={nextAlignment}
                type="button"
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
                className={`min-h-11 flex-1 rounded-lg px-2 text-[12px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                  alignment === nextAlignment
                    ? "bg-[#0c0c0e] font-semibold text-white"
                    : "border border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                }`}
              >
                {nextAlignment[0].toUpperCase() + nextAlignment.slice(1)}
              </button>
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
              <button
                key={nextPosition}
                type="button"
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
                className={`min-h-11 flex-1 rounded-lg px-2 text-[12px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                  boxPosition === nextPosition
                    ? "bg-[#0c0c0e] font-semibold text-white"
                    : "border border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                }`}
              >
                {nextPosition[0].toUpperCase() + nextPosition.slice(1)}
              </button>
            ))}
          </div>
        </div>
      )}

      {!isLyric && !isCaption && (
        <>
          <label className="mt-4 block text-[12px] font-semibold text-[#3f3f46]">
            Animation
            <select
              aria-label="Animation"
              value={bar.effect ?? "none"}
              onChange={(e) =>
                onPatch(
                  TEXT_MOTION_V2_UI_ENABLED
                    ? motionPatchForEffect(bar, e.target.value, videoDurationS)
                    : { effect: e.target.value },
                )
              }
              className="mt-1 h-9 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px] font-normal text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
            >
              {bar.effect === "smooth-type" && !TEXT_MOTION_V2_UI_ENABLED && (
                <option value="smooth-type" disabled>Smooth type (saved; unavailable)</option>
              )}
              {/* Preserve an effect value outside the visible picker list (e.g. "static"). */}
              {bar.effect &&
                bar.effect !== "smooth-type" &&
                !TEXT_ELEMENT_ANIMATIONS.some((a) => a.value === bar.effect) && (
                <option value={bar.effect}>{bar.effect}</option>
              )}
              {TEXT_ELEMENT_ANIMATIONS.filter(
                (animation) =>
                  animation.value !== "smooth-type" || TEXT_MOTION_V2_UI_ENABLED,
              ).map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>

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

          <label className="mt-3 block text-[12px] font-semibold text-[#3f3f46]">
            Theme transition
            <select
              aria-label="Theme transition"
              value={bar.theme_transition?.type ?? "none"}
              onChange={(e) =>
                onPatch({
                  theme_transition:
                    e.target.value === "none" ? null : { type: "giant-title-wipe" },
                })
              }
              className="mt-1 h-9 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px] font-normal text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
            >
              <option value="none">None</option>
              {THEME_TRANSITIONS.map((transition) => (
                <option key={transition.value} value={transition.value}>
                  {transition.label}
                </option>
                ))}
            </select>
          </label>
          {bar.theme_transition?.type === "giant-title-wipe" && (
            <label className="mt-3 block text-[12px] font-semibold text-[#3f3f46]">
              Target glyph
              <input
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
                className="mt-1 h-9 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px] font-normal text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-500/60 focus:outline-none"
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
        <label className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Aa case</span>
          <select
            aria-label="Text case"
            value={bar.text_case ?? "none"}
            onChange={(e) => onPatch({ text_case: e.target.value })}
            className="h-8 w-[116px] rounded-lg border border-zinc-200 bg-white px-2 text-[12px] text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
          >
            <option value="none">None</option>
            <option value="upper">Upper</option>
            <option value="lower">Lower</option>
            <option value="title">Title</option>
          </select>
        </label>
      )}

      {(canEditLetterSpacing || canEditLineSpacing) && !isLyric && !isCaption && (
        <div className="flex min-h-11 items-center justify-between gap-3 border-b border-zinc-100 py-2">
          {canEditLetterSpacing && (
            <label className="min-w-0 flex-1 text-[12px] text-[#3f3f46]">
              Letter
              <input
                type="number"
                aria-label="Letter spacing"
                min={LETTER_SPACING_MIN_EM}
                max={LETTER_SPACING_MAX_EM}
                step={0.01}
                value={bar.letter_spacing ?? 0}
                onChange={(e) => onPatch({ letter_spacing: Number(e.target.value) })}
                className="mt-1 h-8 w-full rounded-lg border border-zinc-200 px-2 text-[12px] tabular-nums text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
              />
            </label>
          )}
          {canEditLineSpacing && (
            <label className="min-w-0 flex-1 text-[12px] text-[#3f3f46]">
              Line
              <input
                type="number"
                aria-label="Line spacing"
                min={LINE_SPACING_MIN}
                max={LINE_SPACING_MAX}
                step={0.05}
                value={bar.line_spacing ?? LINE_SPACING}
                onChange={(e) => onPatch({ line_spacing: Number(e.target.value) })}
                className="mt-1 h-8 w-full rounded-lg border border-zinc-200 px-2 text-[12px] tabular-nums text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
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
        <label className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Shadow</span>
          <select
            aria-label="Shadow effect"
            value={
              bar.shadow_enabled === false
                ? "off"
                : bar.shadow_style === "high_visibility"
                  ? "high_visibility"
                  : "standard"
            }
            onChange={(e) => {
              const value = e.target.value;
              onPatch({
                shadow_enabled: value !== "off",
                shadow_style:
                  value === "high_visibility" ? "high_visibility" : "standard",
              });
            }}
            className="h-8 rounded-lg border border-zinc-200 bg-white px-2 text-[12px] text-[#3f3f46] focus:border-lime-500/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            <option value="standard">Standard</option>
            <option value="high_visibility">High visibility</option>
            <option value="off">Off</option>
          </select>
        </label>
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
            <button
              type="button"
              aria-label={strokeOpen ? "Collapse stroke" : "Add stroke"}
              aria-expanded={strokeOpen}
              onClick={() => setStrokeOpen((o) => !o)}
              className="flex h-6 w-6 items-center justify-center rounded-md border border-zinc-200 text-[13px] leading-none text-[#3f3f46] hover:border-zinc-400"
            >
              {strokeOpen ? "–" : "+"}
            </button>
          </div>
          {strokeOpen && (
            <div className="flex items-center gap-2 pb-3">
              <input
                type="range"
                aria-label="Stroke width"
                min={0}
                max={12}
                step={1}
                value={bar.stroke_width ?? 0}
                onChange={(e) => onPatch({ stroke_width: Number(e.target.value) })}
                className="min-w-0 flex-1 accent-[#0c0c0e]"
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
    const values: LookPreset[] = [
      "none",
      "olive_film",
      "smoky_split_tone",
      "stadium_diffusion",
    ];
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
                  onChange={() => onPatchLook?.(preset)}
                />
                {label}
              </label>
            );
          })}
        </div>

        {isCustomizableLook(selectedLook) && lookControls && (
          <div className="mt-4 rounded-xl border border-zinc-200 bg-zinc-50/70 p-3">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[#71717a]">
                Customize
              </span>
              <button
                type="button"
                className="text-[11px] font-semibold text-[#52525b] underline decoration-zinc-300 underline-offset-2 hover:text-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                onClick={() => {
                  const defaults = defaultLookAdjustments(selectedLook);
                  if (defaults) {
                    onRecordLookAdjustments?.();
                    onPatchLookAdjustments?.(defaults);
                  }
                }}
              >
                Reset
              </button>
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
                <label key={key} className="block">
                  <span className="mb-1 flex items-center justify-between text-[11px] text-[#52525b]">
                    <span className="font-medium">{label}</span>
                    <span className="tabular-nums text-[#71717a]">
                      {value > 0 && (key === "warmth" || key === "contrast") ? "+" : ""}
                      {value}
                      {suffix}
                    </span>
                  </span>
                  <input
                    type="range"
                    aria-label={`Look ${label.toLowerCase()}`}
                    min={min}
                    max={max}
                    step={1}
                    value={value}
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
                    onChange={(event) =>
                      onPatchLookAdjustments?.({
                        [key]: Number(event.target.value) / 100,
                      })
                    }
                    className="block h-11 w-full cursor-pointer accent-[#0c0c0e] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
                  />
                </label>
              ))}
            </div>
          </div>
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
              <button
                type="button"
                aria-label="Slide source window"
                onPointerDown={(e) => startRangeDrag(e, "body")}
                onPointerMove={updateRangeDrag}
                onPointerUp={finishRangeDrag}
                onPointerCancel={finishRangeDrag}
                className="absolute inset-0 cursor-grab rounded-md active:cursor-grabbing"
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
          onChange={(value) => onPatchTiming({ inS: value })}
        />
        <TimingNumberInput
          label="Out"
          value={outS}
          min={0}
          max={timing.sourceDurationS ?? undefined}
          onChange={(value) => onPatchTiming({ outS: value })}
        />
        <TimingNumberInput
          label="Dur"
          value={durationS}
          min={CLIP_MIN_DURATION_S}
          onChange={(value) => onPatchTiming({ durationS: value })}
        />
      </TimingSection>
    </div>
  );
}

function RangeHandle({
  side,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerCancel,
}: {
  side: "left" | "right";
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerUp: (e: React.PointerEvent<HTMLElement>) => void;
  onPointerCancel: (e: React.PointerEvent<HTMLElement>) => void;
}) {
  return (
    <button
      type="button"
      aria-label={side === "left" ? "Trim source in" : "Trim source out"}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      className={`absolute top-1/2 flex h-8 w-3 -translate-y-1/2 cursor-ew-resize items-center justify-center rounded bg-white text-[#0c0c0e] shadow-sm ${
        side === "left" ? "-left-1.5" : "-right-1.5"
      }`}
    >
      <span className="flex flex-col gap-0.5" aria-hidden>
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
        <span className="h-0.5 w-0.5 rounded-full bg-[#0c0c0e]" />
      </span>
    </button>
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
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="min-w-0 text-[12px] text-[#3f3f46]">
      {label}
      <input
        type="number"
        aria-label={`${label} seconds`}
        min={min}
        max={max}
        step={0.1}
        value={Number.isFinite(value) ? value.toFixed(1) : "0.0"}
        onChange={(e) => {
          const next = Number(e.target.value);
          if (Number.isFinite(next)) onChange(next);
        }}
        className="mt-1 h-8 w-full rounded-lg border border-zinc-200 px-2 text-[12px] tabular-nums text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
      />
    </label>
  );
}
