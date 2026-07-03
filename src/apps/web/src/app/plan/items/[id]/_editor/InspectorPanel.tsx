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
} from "@/lib/overlay-layout";
import { INTRO_SIZE_MAX, INTRO_SIZE_MIN, INTRO_SIZE_STEP } from "@/lib/generative-api";
import {
  INSPECTOR_INTERNAL_FIELDS,
  isParityVerified,
} from "@/lib/parity-verified-fields";
import { TEXT_PRESETS, type TextPreset } from "@/lib/text-presets";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";
import { formatTimecode } from "@/lib/timeline/time-format";
import type { MediaOverlay, SoundEffectPlacement } from "@/lib/plan-api";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { EditorSelection } from "./useEditorSelection";
import type { InspectorTab } from "./InspectorRail";
import { normalizeEditableHex } from "./editor-color";
import {
  applyClipSourceWindowDrag,
  type BarDragHandle,
} from "./editor-bar-drag";
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
  "stroke_width",
  "text_case",
  "letter_spacing",
  "line_spacing",
]);

export interface InspectorClipTiming {
  slot: DraftSlot;
  clipNumber: number;
  durationS: number;
  sourceDurationS: number | null;
  sourceUrl: string | null;
}

const SIZE_OPTIONS = (() => {
  const out: number[] = [];
  for (let s = INTRO_SIZE_MIN; s <= INTRO_SIZE_MAX; s += INTRO_SIZE_STEP) out.push(s);
  if (out[out.length - 1] !== INTRO_SIZE_MAX) out.push(INTRO_SIZE_MAX);
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
  contentRef,
  onEditText,
  onPatch,
  onPatchTextTiming,
  onPatchClipTiming,
  onPreviewClipTiming,
  onRecordClipTiming,
  onPatchSfx,
  onDeleteSfx,
  onPatchOverlay,
  onDeleteOverlay,
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
  /** Exposed so double-click-on-canvas can focus + select-all (plan §5). */
  contentRef: React.RefObject<HTMLTextAreaElement>;
  onEditText: (text: string) => void;
  onPatch: (patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onPatchTextTiming: (patch: { start_s?: number; end_s?: number }) => void;
  onPatchClipTiming: (patch: { inS?: number; outS?: number; durationS?: number }) => void;
  onPreviewClipTiming: (patch: { inS: number; durationS: number }) => void;
  onRecordClipTiming: () => void;
  onPatchSfx: (id: string, patch: Partial<SoundEffectPlacement>) => void;
  onDeleteSfx: (id: string) => void;
  onPatchOverlay: (id: string, patch: Partial<MediaOverlay>) => void;
  onDeleteOverlay: (id: string) => void;
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
          onPatchTiming={onPatchTextTiming}
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
          onDelete={onDeleteOverlay}
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
  onDelete,
  onClose,
}: {
  overlay: MediaOverlay;
  onPatch: (id: string, patch: Partial<MediaOverlay>) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 pt-4">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Overlay</h2>
        <CloseX onClose={onClose} />
      </div>
      <p className="mt-1 text-[12px] capitalize text-[#71717a]">{overlay.kind}</p>
      <FieldNumber
        label="Start"
        value={overlay.start_s}
        min={0}
        step={0.1}
        onCommit={(value) => onPatch(overlay.id, { start_s: Math.min(value, overlay.end_s - 0.3) })}
      />
      <FieldNumber
        label="End"
        value={overlay.end_s}
        min={0.3}
        step={0.1}
        onCommit={(value) => onPatch(overlay.id, { end_s: Math.max(value, overlay.start_s + 0.3) })}
      />
      <p className="mt-4 rounded-lg border border-dashed border-zinc-300 px-3 py-2 text-[12px] leading-relaxed text-[#71717a]">
        Position and scale controls remain on the item page overlay editor.
      </p>
      <DangerButton onClick={() => onDelete(overlay.id)}>Delete overlay</DangerButton>
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
  onPatchTiming,
  onClose,
}: {
  bar: TextElementBar;
  contentRef: React.RefObject<HTMLTextAreaElement>;
  onEditText: (text: string) => void;
  onPatch: (patch: Partial<Omit<TextElementBar, "id" | "role">>) => void;
  onPatchTiming: (patch: { start_s?: number; end_s?: number }) => void;
  onClose: () => void;
}) {
  // Stroke row starts expanded when the bar already carries a stroke.
  const [strokeOpen, setStrokeOpen] = useState((bar.stroke_width ?? 0) > 0);

  const sizeValue = Math.round(bar.size_px ?? 64);
  const clampedSlider = Math.min(INTRO_SIZE_MAX, Math.max(INTRO_SIZE_MIN, sizeValue));
  const canEditTextCase = isParityVerified("text_case");
  const canEditLetterSpacing = isParityVerified("letter_spacing");
  const canEditLineSpacing = isParityVerified("line_spacing");

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
        <h2 className="font-display text-[18px] text-[#0c0c0e]">Text</h2>
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
          min={INTRO_SIZE_MIN}
          max={INTRO_SIZE_MAX}
          step={1}
          value={clampedSlider}
          onChange={(e) => onPatch({ size_px: Number(e.target.value), size_class: undefined })}
          className="min-w-0 flex-1 accent-[#0c0c0e]"
        />
      </div>

      {/* Animation */}
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

      {/* Style */}
      <div className="mt-6 flex items-center justify-between border-b border-zinc-100 pb-2">
        <span className="text-[13px] font-bold text-[#0c0c0e]">Style</span>
      </div>

      {canEditTextCase && (
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

      {(canEditLetterSpacing || canEditLineSpacing) && (
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

      {/* Stroke — collapsed behind + */}
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

      {/* Read-only fields the editor preserves but doesn't edit yet (D17). */}
      {readOnlyRows.length > 0 && (
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
