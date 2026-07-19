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

import { useEffect, useMemo, useRef, useState } from "react";
import {
  INTRO_ANIMATIONS,
  INTRO_FONTS,
  resolveCssFont,
} from "@/lib/overlay-constants";
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
import type { MediaOverlay, SoundEffectPlacement } from "@/lib/plan-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { EditorSelection } from "./useEditorSelection";
import type { InspectorTab } from "./InspectorRail";
import { normalizeEditableHex } from "./editor-color";
import {
  applyClipSourceWindowDrag,
  type BarDragHandle,
} from "./editor-bar-drag";
import {
  applyMediaOverlaySourceWindowInput,
  clampMediaOverlayScale,
  MEDIA_OVERLAY_MIN_SCALE,
  MEDIA_OVERLAY_MAX_SCALE,
} from "./editor-media-overlays";
import PresetGrid from "./PresetGrid";

/** Fields with dedicated (potentially editable) rows in this panel. */
const EDITABLE_ROW_FIELDS = new Set([
  "text",
  "start_s",
  "end_s",
  "font_family",
  "size_px",
  "effect",
  "color",
  "shadow_enabled",
  "stroke_width",
  "text_case",
  "letter_spacing",
  "line_spacing",
  "max_width_frac",
  "alignment",
  "behind_subject",
]);

const EDITOR_TEXT_SIZE_MIN = 8;
const EDITOR_TEXT_SIZE_MAX = 300;

const TEXT_BEHIND_SUBJECT_UI_ENABLED =
  process.env.NEXT_PUBLIC_TEXT_BEHIND_SUBJECT_ENABLED === "true";

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
  tab,
  sampleWord,
  appliedPresetId,
  captionsTabHref,
  contentRef,
  onEditText,
  onPatch,
  onSetTextBoxPosition,
  boxPositionXFrac,
  onPatchTextTiming,
  onPatchClipTiming,
  onPreviewClipTiming,
  onRecordClipTiming,
  onPatchSfx,
  onDeleteSfx,
  onPatchOverlay,
  onPreviewOverlay,
  onRecordOverlay,
  onDeleteOverlay,
  mixLevel,
  mixEditable,
  mixLabel,
  musicTracks = [],
  musicLoading = false,
  currentMusicTrackId = null,
  musicEditable = false,
  onPatchMix,
  onPickMusic,
  smartPlaceAvailable = false,
  onSmartPlace,
  onClose,
  onPickPreset,
}: {
  selection: EditorSelection | null;
  /** The selected text bar (null when selection is empty or non-text). */
  bar: TextElementBar | null;
  clipTiming: InspectorClipTiming | null;
  sfx: SoundEffectPlacement | null;
  overlay: MediaOverlay | null;
  tab: InspectorTab;
  sampleWord: string | null;
  appliedPresetId: string | null;
  /**
   * When set (a caption archetype whose captions are edited on the item page),
   * the empty inspector state becomes a caption-specific CTA linking here
   * instead of the generic "Select anything to edit it". Null on normal edits.
   */
  captionsTabHref?: string | null;
  /** Exposed so double-click-on-canvas can focus + select-all (plan §5). */
  contentRef: React.RefObject<HTMLTextAreaElement>;
  onEditText: (text: string) => void;
  onPatch: (patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onSetTextBoxPosition?: (position: TextBoxHorizontalPosition) => void;
  boxPositionXFrac?: number;
  onPatchTextTiming: (patch: { start_s?: number; end_s?: number }) => void;
  onPatchClipTiming: (patch: { inS?: number; outS?: number; durationS?: number }) => void;
  onPreviewClipTiming: (patch: { inS: number; durationS: number }) => void;
  onRecordClipTiming: () => void;
  onPatchSfx: (id: string, patch: Partial<SoundEffectPlacement>) => void;
  onDeleteSfx: (id: string) => void;
  onPatchOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onPreviewOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onRecordOverlay: () => void;
  onDeleteOverlay: (id: string) => void;
  mixLevel?: number | null;
  mixEditable?: boolean;
  mixLabel?: string;
  musicTracks?: MusicTrackSummary[];
  musicLoading?: boolean;
  currentMusicTrackId?: string | null;
  musicEditable?: boolean;
  onPatchMix?: (level: number) => void;
  onPickMusic?: (trackId: string) => void;
  smartPlaceAvailable?: boolean;
  onSmartPlace?: () => void;
  /** Close X clears the selection — the column stays (D6). */
  onClose: () => void;
  onPickPreset: (preset: TextPreset) => void;
}) {
  return (
    <div
      data-region="inspector"
      className="flex w-[320px] flex-col border-l border-zinc-200 bg-white"
    >
      {tab === "presets" ? (
        <PresetsTab
          sampleWord={sampleWord}
          appliedPresetId={appliedPresetId}
          onPickPreset={onPickPreset}
        />
      ) : selection === null ? (
        <div className="flex flex-1 items-start justify-center px-6 pt-16">
          {captionsTabHref ? (
            // Caption archetype: this shell can't edit caption text — point the
            // user at the Captions tab instead of the generic empty state, which
            // otherwise reads as "editing is broken" (the reported bug).
            <div className="text-center" data-testid="inspector-captions-cta">
              <p className="font-display text-[16px] leading-relaxed text-[#71717a]">
                Captions are edited on the item page.
              </p>
              <a
                href={captionsTabHref}
                className="mt-2 inline-block text-[13px] font-semibold text-[#0c0c0e] underline decoration-zinc-300 underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Edit captions →
              </a>
            </div>
          ) : (
            <p className="font-display text-[16px] leading-relaxed text-[#71717a]">
              Select anything to edit it
            </p>
          )}
        </div>
      ) : selection.kind === "text" && bar ? (
        <TextInspector
          key={bar.id}
          bar={bar}
          contentRef={contentRef}
          onEditText={onEditText}
          onPatch={onPatch}
          onSetTextBoxPosition={onSetTextBoxPosition}
          boxPositionXFrac={boxPositionXFrac}
          onPatchTiming={onPatchTextTiming}
          smartPlaceAvailable={smartPlaceAvailable}
          onSmartPlace={onSmartPlace}
          onClose={onClose}
        />
      ) : selection.kind === "clip" && clipTiming ? (
        <ClipInspector
          timing={clipTiming}
          onPatchTiming={onPatchClipTiming}
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
      ) : selection.kind === "music" ? (
        <MixInspector
          level={mixLevel}
          editable={mixEditable ?? false}
          label={mixLabel ?? "Music"}
          musicTracks={musicTracks}
          musicLoading={musicLoading}
          currentMusicTrackId={currentMusicTrackId}
          musicEditable={musicEditable}
          onPickMusic={onPickMusic}
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
          <p className="mt-3 text-[13px] text-[#71717a]">
            Controls for this element arrive with the timeline update.
          </p>
        </div>
      )}
    </div>
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
  onPatch,
  onPickMusic,
  onClose,
}: {
  level?: number | null;
  editable: boolean;
  label: string;
  musicTracks: MusicTrackSummary[];
  musicLoading: boolean;
  currentMusicTrackId: string | null;
  musicEditable: boolean;
  onPatch?: (level: number) => void;
  onPickMusic?: (trackId: string) => void;
  onClose: () => void;
}) {
  const safeLevel = Math.max(0, Math.min(1, level ?? 0));
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
            This edit has no swappable song.
          </p>
        )}
      </div>
      {editable ? (
        <div className="mt-4">
          <div className="flex items-center justify-between text-[12px] font-semibold text-[#3f3f46]">
            <label htmlFor="editor-mix-level">Bed level</label>
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
          <p className="mt-2 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-3 text-[13px] leading-relaxed text-[#52525b]">
            Balance the background bed against your voiceover.
          </p>
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
  onSetTextBoxPosition,
  boxPositionXFrac,
  onPatchTiming,
  smartPlaceAvailable,
  onSmartPlace,
  onClose,
}: {
  bar: TextElementBar;
  contentRef: React.RefObject<HTMLTextAreaElement>;
  onEditText: (text: string) => void;
  onPatch: (patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onSetTextBoxPosition?: (position: TextBoxHorizontalPosition) => void;
  boxPositionXFrac?: number;
  onPatchTiming: (patch: { start_s?: number; end_s?: number }) => void;
  smartPlaceAvailable: boolean;
  onSmartPlace?: () => void;
  onClose: () => void;
}) {
  // Stroke row starts expanded when the bar already carries a stroke.
  const [strokeOpen, setStrokeOpen] = useState((bar.stroke_width ?? 0) > 0);
  const isLyric = bar.role === "lyric_line";

  const sizeValue = Math.round(bar.size_px ?? 64);
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
          Text
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
          <button
            type="button"
            disabled={!smartPlaceAvailable}
            onClick={onSmartPlace}
            className="mt-2 min-h-9 w-full rounded-lg border border-zinc-200 bg-white px-3 text-[12px] font-semibold text-[#0c0c0e] hover:border-zinc-400 disabled:cursor-not-allowed disabled:bg-zinc-50 disabled:text-[#a1a1aa] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Smart place
          </button>

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

      {/* Font + size */}
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

      {canEditMaxWidth && !isLyric && (
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

      {!isLyric && (
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

      {!isLyric && (
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

      {!isLyric && (
        <label className="mt-4 block text-[12px] font-semibold text-[#3f3f46]">
          Animation
          <select
            aria-label="Animation"
            value={bar.effect ?? "none"}
            onChange={(e) => onPatch({ effect: e.target.value })}
            className="mt-1 h-9 w-full rounded-lg border border-zinc-200 bg-white px-2 text-[13px] font-normal text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
          >
            {/* Preserve an effect value outside the picker list (e.g. "static"). */}
            {bar.effect && !INTRO_ANIMATIONS.some((a) => a.value === bar.effect) && (
              <option value={bar.effect}>{bar.effect}</option>
            )}
            {INTRO_ANIMATIONS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
      )}

      {/* Style */}
      <div className="mt-6 flex items-center justify-between border-b border-zinc-100 pb-2">
        <span className="text-[13px] font-bold text-[#0c0c0e]">Style</span>
      </div>

      {canEditTextCase && !isLyric && (
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

      {(canEditLetterSpacing || canEditLineSpacing) && !isLyric && (
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

      {/* Fill — visible by default (progressive disclosure) */}
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

      {!isLyric && (
        <label className="flex h-11 items-center justify-between border-b border-zinc-100">
          <span className="text-[13px] text-[#3f3f46]">Shadow</span>
          <input
            type="checkbox"
            checked={bar.shadow_enabled !== false}
            onChange={(e) => onPatch({ shadow_enabled: e.target.checked })}
            className="h-4 w-4 accent-[#0c0c0e]"
          />
        </label>
      )}

      {TEXT_BEHIND_SUBJECT_UI_ENABLED && !isLyric && (
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
      {!isLyric && (
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
  onPreviewTiming,
  onRecordTimingEdit,
  onClose,
}: {
  timing: InspectorClipTiming;
  onPatchTiming: (patch: { inS?: number; outS?: number; durationS?: number }) => void;
  onPreviewTiming: (patch: { inS: number; durationS: number }) => void;
  onRecordTimingEdit: () => void;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
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
      ? Math.max(outS, 0.6)
      : Math.max(timing.sourceDurationS, 0.6);
  const rangeLeftPct = sourceDurationS > 0 ? (inS / sourceDurationS) * 100 : 0;
  const rangeWidthPct =
    sourceDurationS > 0 ? (durationS / sourceDurationS) * 100 : 100;

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
          min={0.6}
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

function HexInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (hex: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  // Follow external changes (e.g. the swatch or a preset).
  useEffect(() => setDraft(value), [value]);
  function commit() {
    const hex = normalizeEditableHex(draft);
    if (hex) onChange(hex);
    else setDraft(value);
  }
  return (
    <input
      type="text"
      aria-label="Fill color hex"
      value={draft}
      onChange={(e) => {
        const next = e.target.value;
        setDraft(next);
        const hex = normalizeEditableHex(next);
        if (hex) onChange(hex);
      }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
      }}
      className="h-7 w-[76px] rounded-md border border-zinc-200 px-2 text-[12px] uppercase text-[#0c0c0e] focus:border-lime-500/60 focus:outline-none"
    />
  );
}

/** Font picker: button showing the current family in its REAL face, opening a
 * CSS-previewed option list (each INTRO_FONTS entry rendered in itself). */
function FontSelect({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  const current = value ?? "Playfair Display";
  const { family, weight } = resolveCssFont(current);

  return (
    <div className="relative">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Font: ${current}`}
        onClick={() => setOpen((o) => !o)}
        className="flex h-9 w-full items-center justify-between rounded-lg border border-zinc-200 bg-white px-3 text-left text-[13px] text-[#0c0c0e] hover:border-zinc-400 focus:border-lime-500/60 focus:outline-none"
      >
        <span className="truncate" style={{ fontFamily: family, fontWeight: weight }}>
          {current}
        </span>
        <span aria-hidden className="text-[9px] text-[#a1a1aa]">
          ⌄
        </span>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" aria-hidden onClick={() => setOpen(false)} />
          <div
            ref={listRef}
            role="listbox"
            aria-label="Fonts"
            className="absolute left-0 right-0 z-20 mt-1 max-h-64 overflow-y-auto rounded-lg border border-zinc-200 bg-white py-1 shadow-lg"
          >
            {INTRO_FONTS.map((f) => {
              const selected = f.name === current;
              return (
                <button
                  key={f.name}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => {
                    onChange(f.name);
                    setOpen(false);
                  }}
                  className={`block w-full truncate px-3 py-1.5 text-left text-[14px] hover:bg-zinc-50 ${
                    selected ? "bg-lime-50 text-[#0c0c0e]" : "text-[#3f3f46]"
                  }`}
                  style={{ fontFamily: f.cssFamily, fontWeight: f.weight }}
                >
                  {f.name}
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
