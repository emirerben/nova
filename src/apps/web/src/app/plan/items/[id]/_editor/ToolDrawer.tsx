"use client";

/**
 * ToolDrawer — the left drawer that opens for the active rail tool (plan §2).
 *
 * Text tool: "Basic" section with a full-width "Add text" button (creates a
 * default 2.0s bar at the playhead, first Basic preset, selects it → the
 * inspector populates), then "Presets" with category chips (dark ink pill =
 * selected) above the 4-column preset grid.
 */

import { useEffect, useMemo, useState } from "react";
import type { GenerativeStyleSet } from "@/lib/generative-api";
import type { MusicTrackSummary } from "@/lib/music-api";
import type { SoundEffectSummary } from "@/lib/sfx-api";
import type { PoolAsset, VisualBlock } from "@/lib/plan-api";
import {
  filterTextPresetsByCategory,
  PRESET_CATEGORIES,
  readTextPresetFavorites,
  TEXT_PRESETS,
  toggleTextPresetFavorite,
  type TextPreset,
  type TextPresetCategory,
  writeTextPresetFavorites,
} from "@/lib/text-presets";
import PresetGrid from "./PresetGrid";
import StylesDrawer from "./StylesDrawer";
import CopilotDrawer from "./CopilotDrawer";
import SongWindowSelector, { type SongWindowControl } from "./SongWindowSelector";
import type { EditorTool } from "./ToolRail";
import type { EditorLayoutMode } from "./useEditorLayoutMode";
import type {
  CopilotMessage,
  QueuedCopilotMessage,
} from "@/lib/edit-copilot/useEditCopilot";

const CATEGORY_LABEL: Record<TextPresetCategory, string> = {
  favorite: "Favorite",
  basic: "Basic",
  trending: "Trending",
};

export default function ToolDrawer({
  tool,
  sampleWord,
  appliedPresetId,
  onAddText,
  lyricsToggle,
  onSplitPlaceText,
  splitSmartPlaceAvailable = false,
  onSmartPlaceAll,
  smartPlaceAllAvailable = false,
  onPickPreset,
  appliedStyleSetId = null,
  onRestyleAll,
  sfxEffects = [],
  sfxLoading = false,
  onAddSfx,
  musicTracks = [],
  musicLoading = false,
  currentMusicTrackId = null,
  musicEditable = false,
  onPickMusic,
  musicWindow,
  overlayUploading = false,
  onOverlayUpload,
  overlaySuggestions = null,
  visualBlocks = [],
  visualAssets = [],
  visualTextElements = [],
  visualUploading = false,
  onVisualUpload,
  onAddMontage,
  onAddTextCard,
  onAddVisualBlockText,
  onSelectVisualBlockText,
  onPatchVisualBlock,
  onDuplicateVisualBlock,
  onDeleteVisualBlock,
  onRetimeVisualBlock,
  layoutMode = "full",
  copilot,
  onClose,
}: {
  tool: EditorTool;
  sampleWord: string | null;
  appliedPresetId: string | null;
  onAddText: () => void;
  lyricsToggle?: {
    visible: boolean;
    enabled: boolean;
    disabled: boolean;
    hint: string | null;
    onToggle: (enabled: boolean) => void;
  };
  onSplitPlaceText?: (text: string) => boolean;
  splitSmartPlaceAvailable?: boolean;
  onSmartPlaceAll?: () => void;
  smartPlaceAllAvailable?: boolean;
  onPickPreset: (preset: TextPreset) => void;
  appliedStyleSetId?: string | null;
  onRestyleAll?: (styleSet: GenerativeStyleSet) => void;
  sfxEffects?: SoundEffectSummary[];
  sfxLoading?: boolean;
  onAddSfx?: (effect: SoundEffectSummary) => void;
  musicTracks?: MusicTrackSummary[];
  musicLoading?: boolean;
  currentMusicTrackId?: string | null;
  musicEditable?: boolean;
  onPickMusic?: (trackId: string) => void;
  musicWindow?: SongWindowControl;
  overlayUploading?: boolean;
  onOverlayUpload?: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => void;
  /** "AI suggestions" section for the Overlays pane (EditorShell gates it on
   *  the autoplace flag + the variant's `suggestions` capability). */
  overlaySuggestions?: React.ReactNode;
  visualBlocks?: VisualBlock[];
  visualAssets?: PoolAsset[];
  visualTextElements?: Array<{
    id?: string;
    visual_block_id?: string | null;
    text: string;
    start_s: number;
    end_s: number;
    color?: string;
  }>;
  visualUploading?: boolean;
  onVisualUpload?: (files: File[]) => void;
  onAddMontage?: (assetIds: string[]) => void;
  onAddTextCard?: (preset: "card" | "quote" | "statistic" | "transition") => void;
  onAddVisualBlockText?: (blockId: string) => void;
  onSelectVisualBlockText?: (textId: string) => void;
  onPatchVisualBlock?: (id: string, patch: Partial<VisualBlock>) => void;
  onDuplicateVisualBlock?: (id: string) => void;
  onDeleteVisualBlock?: (id: string) => void;
  onRetimeVisualBlock?: (id: string) => void;
  layoutMode?: EditorLayoutMode;
  copilot?: {
    messages: CopilotMessage[];
    sending: boolean;
    queued: QueuedCopilotMessage | null;
    error: string | null;
    restoredInput: string;
    suggestions: string[];
    historyVersion: number;
    canUndo: boolean;
    onSend: (text: string) => void;
    onCancelQueued: () => void;
    onEditQueued: (text: string) => void;
    onStop: () => void;
    onUndo: () => void;
    onClearRestoredInput: () => void;
  };
  onClose: () => void;
}) {
  const [category, setCategory] = useState<TextPresetCategory>("basic");
  const [favoritePresetIds, setFavoritePresetIds] = useState<string[]>([]);
  const [smartTextDraft, setSmartTextDraft] = useState("");

  useEffect(() => {
    setFavoritePresetIds(readTextPresetFavorites(window.localStorage));
  }, []);

  const presets = useMemo(
    () => filterTextPresetsByCategory(TEXT_PRESETS, category, favoritePresetIds),
    [category, favoritePresetIds],
  );

  const toggleFavorite = (presetId: string) => {
    setFavoritePresetIds((current) => {
      const next = toggleTextPresetFavorite(current, presetId);
      writeTextPresetFavorites(window.localStorage, next);
      return next;
    });
  };

  const title =
    tool === "nova"
      ? "Nova"
      : tool === "text"
      ? "Text"
      : tool === "styles"
        ? "Styles"
        : tool[0].toUpperCase() + tool.slice(1);

  if (tool === "nova") {
    if (!copilot) return null;
    return (
      <CopilotDrawer
        layoutMode={layoutMode}
        messages={copilot.messages}
        sending={copilot.sending}
        queued={copilot.queued}
        error={copilot.error}
        restoredInput={copilot.restoredInput}
        suggestions={copilot.suggestions}
        historyVersion={copilot.historyVersion}
        canUndo={copilot.canUndo}
        onSend={copilot.onSend}
        onCancelQueued={copilot.onCancelQueued}
        onEditQueued={copilot.onEditQueued}
        onStop={copilot.onStop}
        onUndo={copilot.onUndo}
        onClearRestoredInput={copilot.onClearRestoredInput}
        onClose={onClose}
      />
    );
  }

  return (
    <div
      data-region="tool-drawer"
      className="flex h-full w-[360px] flex-col border-r border-zinc-200 bg-white motion-safe:animate-fade-up"
    >
      <div className="flex flex-none items-center justify-between px-5 pb-3 pt-4">
        <div className="flex min-w-0 items-center gap-3">
          <h2 className="font-display text-[18px] text-[#0c0c0e]">
            {title}
          </h2>
          {tool === "text" && lyricsToggle?.visible && (
            <div
              className="flex min-h-11 items-center gap-2 rounded-lg px-1"
              title={lyricsToggle.disabled ? lyricsToggle.hint ?? undefined : undefined}
            >
              <span className="text-[12px] font-semibold text-[#3f3f46]">Lyrics</span>
              <button
                type="button"
                role="switch"
                aria-checked={lyricsToggle.enabled}
                aria-label="Lyrics"
                disabled={lyricsToggle.disabled}
                onClick={() => lyricsToggle.onToggle(!lyricsToggle.enabled)}
                className={`relative h-6 w-11 rounded-full transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 disabled:cursor-not-allowed disabled:opacity-50 ${
                  lyricsToggle.enabled ? "bg-[#0c0c0e]" : "bg-zinc-200"
                }`}
              >
                <span
                  aria-hidden
                  className={`absolute top-1 h-4 w-4 rounded-full bg-white transition-transform ${
                    lyricsToggle.enabled ? "translate-x-6" : "translate-x-1"
                  }`}
                />
              </button>
            </div>
          )}
        </div>
        <button
          type="button"
          aria-label="Close drawer"
          onClick={onClose}
          className="flex h-11 w-11 items-center justify-center rounded-lg text-[13px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          ✕
        </button>
      </div>

      {tool === "text" && (
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
          <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Basic</p>
          <button
            type="button"
            onClick={onAddText}
            className="min-h-11 w-full rounded-lg bg-zinc-100 text-[13px] font-semibold text-[#0c0c0e] hover:bg-zinc-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
          >
            Add text
          </button>

          <div className="mt-4 border-t border-zinc-100 pt-4">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-[12px] font-semibold text-[#3f3f46]">Composition</p>
              <button
                type="button"
                onClick={onSmartPlaceAll}
                disabled={!smartPlaceAllAvailable || !onSmartPlaceAll}
                className="min-h-8 rounded-lg border border-zinc-200 bg-white px-2.5 text-[11px] font-semibold text-[#0c0c0e] hover:border-zinc-400 disabled:cursor-not-allowed disabled:bg-zinc-50 disabled:text-[#a1a1aa] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
              >
                Smart place all
              </button>
            </div>
            <textarea
              value={smartTextDraft}
              onChange={(event) => setSmartTextDraft(event.target.value)}
              maxLength={5000}
              rows={3}
              aria-label="Composition text"
              placeholder="Paste lines or a paragraph"
              className="w-full resize-none rounded-lg border border-zinc-200 px-3 py-2 text-[13px] text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-500/60 focus:outline-none"
            />
            <button
              type="button"
              onClick={() => {
                if (onSplitPlaceText?.(smartTextDraft)) {
                  setSmartTextDraft("");
                }
              }}
              disabled={
                !splitSmartPlaceAvailable ||
                !onSplitPlaceText ||
                smartTextDraft.trim().length === 0
              }
              className="mt-2 min-h-10 w-full rounded-lg bg-[#0c0c0e] px-3 text-[12px] font-semibold text-white hover:bg-[#27272a] disabled:cursor-not-allowed disabled:bg-zinc-100 disabled:text-[#a1a1aa] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              Split & place
            </button>
          </div>

          <p className="mb-2 mt-5 text-[12px] font-semibold text-[#3f3f46]">Presets</p>
          <div className="mb-3 flex flex-wrap gap-1.5" role="tablist" aria-label="Preset categories">
            {PRESET_CATEGORIES.map((cat) => {
              const selected = category === cat;
              return (
                <button
                  key={cat}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  onClick={() => setCategory(cat)}
                  className={`inline-flex min-h-11 items-center rounded-full px-4 text-[12px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                    selected
                      ? "bg-[#0c0c0e] font-semibold text-white"
                      : "border border-zinc-200 bg-white text-[#3f3f46] hover:border-zinc-400"
                  }`}
                >
                  {CATEGORY_LABEL[cat]}
                </button>
              );
            })}
          </div>
          <PresetGrid
            presets={presets}
            sampleWord={sampleWord}
            appliedPresetId={appliedPresetId}
            favoritePresetIds={favoritePresetIds}
            onToggleFavorite={toggleFavorite}
            onPick={onPickPreset}
          />
        </div>
      )}

      {tool === "styles" && (
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <StylesDrawer
            sampleText={sampleWord}
            appliedStyleSetId={appliedStyleSetId}
            onRestyleAll={onRestyleAll}
          />
        </div>
      )}

      {tool === "visuals" && (
        <VisualsDrawer
          blocks={visualBlocks}
          assets={visualAssets}
          textElements={visualTextElements}
          uploading={visualUploading}
          onUpload={onVisualUpload}
          onAddMontage={onAddMontage}
          onAddTextCard={onAddTextCard}
          onAddBlockText={onAddVisualBlockText}
          onSelectBlockText={onSelectVisualBlockText}
          onPatchBlock={onPatchVisualBlock}
          onDuplicateBlock={onDuplicateVisualBlock}
          onDeleteBlock={onDeleteVisualBlock}
          onRetimeBlock={onRetimeVisualBlock}
        />
      )}

      {tool === "sounds" && (
        <SoundsDrawer
          effects={sfxEffects}
          loading={sfxLoading}
          onAddSfx={onAddSfx}
          musicTracks={musicTracks}
          musicLoading={musicLoading}
          currentMusicTrackId={currentMusicTrackId}
          musicEditable={musicEditable}
          onPickMusic={onPickMusic}
          musicWindow={musicWindow}
        />
      )}

      {tool === "overlays" && (
        <OverlaysDrawer
          uploading={overlayUploading}
          onOverlayUpload={onOverlayUpload}
          suggestions={overlaySuggestions}
        />
      )}
    </div>
  );
}

function VisualsDrawer({
  blocks,
  assets,
  textElements,
  uploading,
  onUpload,
  onAddMontage,
  onAddTextCard,
  onAddBlockText,
  onSelectBlockText,
  onPatchBlock,
  onDuplicateBlock,
  onDeleteBlock,
  onRetimeBlock,
}: {
  blocks: VisualBlock[];
  assets: PoolAsset[];
  textElements: Array<{
    id?: string;
    visual_block_id?: string | null;
    text: string;
    start_s: number;
    end_s: number;
    color?: string;
  }>;
  uploading: boolean;
  onUpload?: (files: File[]) => void;
  onAddMontage?: (assetIds: string[]) => void;
  onAddTextCard?: (preset: "card" | "quote" | "statistic" | "transition") => void;
  onAddBlockText?: (blockId: string) => void;
  onSelectBlockText?: (textId: string) => void;
  onPatchBlock?: (id: string, patch: Partial<VisualBlock>) => void;
  onDuplicateBlock?: (id: string) => void;
  onDeleteBlock?: (id: string) => void;
  onRetimeBlock?: (id: string) => void;
}) {
  const ready = assets.filter((asset) => asset.status === "ready");
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [draggedShot, setDraggedShot] = useState<{ blockId: string; shotId: string } | null>(null);

  function contrastRatio(foreground: string, background: string): number | null {
    const parse = (value: string) => {
      if (!/^#[0-9a-f]{6}$/i.test(value)) return null;
      return [1, 3, 5].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16) / 255);
    };
    const luminance = (rgb: number[]) =>
      rgb.reduce((sum, channel, index) => {
        const linear = channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        return sum + linear * [0.2126, 0.7152, 0.0722][index];
      }, 0);
    const fg = parse(foreground);
    const bg = parse(background);
    if (!fg || !bg) return null;
    const [lighter, darker] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    return (lighter + 0.05) / (darker + 0.05);
  }

  function patchShots(
    block: Extract<VisualBlock, { kind: "montage" }>,
    shots: Extract<VisualBlock, { kind: "montage" }>["shots"],
  ) {
    let offset = 0;
    const total = block.end_s - block.start_s;
    const weightTotal = shots.reduce(
      (sum, shot) => sum + Math.max(0.05, shot.duration_s),
      0,
    );
    const normalized = shots.map((shot, index) => {
      const duration =
        index === shots.length - 1
          ? Math.max(0.05, total - offset)
          : Math.max(0.05, (Math.max(0.05, shot.duration_s) / weightTotal) * total);
      const next = { ...shot, start_offset_s: offset, duration_s: duration };
      offset += duration;
      return next;
    });
    onPatchBlock?.(block.id, {
      shots: normalized,
      timing_mode: "manual",
    } as Partial<VisualBlock>);
  }

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 pb-5">
      <section>
        <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Add a block</p>
        <div className="grid grid-cols-2 gap-2">
          {(["card", "quote", "statistic", "transition"] as const).map((preset) => (
            <button
              key={preset}
              type="button"
              onClick={() => onAddTextCard?.(preset)}
              className="min-h-11 rounded-lg border border-zinc-200 bg-white px-2 text-[12px] font-semibold capitalize text-[#0c0c0e] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              {preset === "card" ? "Text card" : preset}
            </button>
          ))}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <p className="text-[12px] font-semibold text-[#3f3f46]">Montage assets</p>
          <span className="text-[11px] text-[#71717a]">Choose 3–12</span>
        </div>
        <label className="mb-3 flex min-h-11 cursor-pointer items-center justify-center rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-3 text-[12px] font-semibold text-[#3f3f46] hover:border-zinc-400 focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-lime-500">
          {uploading ? "Uploading visuals…" : "Upload images or videos"}
          <input
            type="file"
            multiple
            accept={OVERLAY_MIME_TYPES.join(",")}
            className="sr-only"
            disabled={uploading}
            onChange={(event) => {
              onUpload?.(Array.from(event.target.files ?? []));
              event.currentTarget.value = "";
            }}
          />
        </label>
        <div className="grid grid-cols-3 gap-2">
          {ready.map((asset) => {
            const selected = selectedAssetIds.includes(asset.id);
            return (
              <button
                key={asset.id}
                type="button"
                aria-label={`Select ${asset.source_filename || asset.kind}`}
                aria-pressed={selected}
                onClick={() =>
                  setSelectedAssetIds((current) =>
                    selected
                      ? current.filter((id) => id !== asset.id)
                      : current.length < 12
                        ? [...current, asset.id]
                        : current,
                  )
                }
                className={`relative aspect-[9/12] overflow-hidden rounded-lg border-2 ${
                  selected ? "border-lime-500" : "border-transparent"
                } focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500`}
              >
                {asset.display_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={asset.display_url} alt="" className="h-full w-full object-cover" />
                ) : (
                  <span className="flex h-full items-center justify-center bg-zinc-100 text-[10px] text-zinc-500">
                    {asset.kind}
                  </span>
                )}
                {selected && (
                  <span className="absolute right-1 top-1 rounded-full bg-lime-500 px-1.5 text-[10px] font-bold text-black">
                    {selectedAssetIds.indexOf(asset.id) + 1}
                  </span>
                )}
                {asset.source_type === "extracted_frame" && (
                  <span className="absolute bottom-1 left-1 right-1 truncate rounded bg-black/70 px-1 py-0.5 text-[8px] font-semibold text-white">
                    Extracted frame
                  </span>
                )}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          disabled={selectedAssetIds.length < 3}
          onClick={() => {
            onAddMontage?.(selectedAssetIds);
            setSelectedAssetIds([]);
          }}
          className="mt-3 min-h-11 w-full rounded-lg bg-[#0c0c0e] px-3 text-[12px] font-semibold text-white hover:bg-zinc-800 disabled:bg-zinc-100 disabled:text-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
        >
          Add montage ({selectedAssetIds.length})
        </button>
      </section>

      <section className="space-y-3 border-t border-zinc-100 pt-4">
        <p className="text-[12px] font-semibold text-[#3f3f46]">Timeline blocks</p>
        {blocks.map((block) => (
          <div key={block.id} className="rounded-xl border border-zinc-200 p-3">
            {(() => {
              const linkedText = textElements.filter(
                (element) => element.visual_block_id === block.id,
              );
              const excessiveCopy = linkedText.some(
                (element) =>
                  element.text.length > 140 ||
                  element.text.length / Math.max(0.1, element.end_s - element.start_s) > 20,
              );
              const backgroundColors =
                block.kind !== "text_card"
                  ? []
                  : block.background.type === "solid"
                    ? [block.background.color]
                    : block.background.type === "gradient"
                      ? [block.background.from, block.background.to]
                      : [];
              const lowContrast = linkedText.some((element) =>
                backgroundColors.some((color) => {
                  const ratio = contrastRatio(element.color || "#FFFFFF", color);
                  return ratio !== null && ratio < 4.5;
                }),
              );
              const ordered = [...blocks].sort((a, b) => a.start_s - b.start_s);
              const blockIndex = ordered.findIndex((candidate) => candidate.id === block.id);
              const previous = blockIndex > 0 ? ordered[blockIndex - 1] : null;
              const repetitive =
                previous?.kind === block.kind && block.start_s - previous.end_s < 0.5;
              if (!excessiveCopy && !lowContrast && !repetitive) return null;
              return (
                <div className="mb-3 space-y-1">
                  {excessiveCopy && (
                    <p className="rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-[10px] text-[#3f3f46]">
                      This card has a dense reading load for its duration.
                    </p>
                  )}
                  {lowContrast && (
                    <p className="rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-[10px] text-[#3f3f46]">
                      Text contrast may be too low for comfortable reading.
                    </p>
                  )}
                  {repetitive && (
                    <p className="rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-[10px] text-[#3f3f46]">
                      Adjacent blocks repeat the same visual treatment.
                    </p>
                  )}
                </div>
              );
            })()}
            <div className="flex items-center justify-between gap-2">
              <div>
                <p className="text-[12px] font-semibold text-[#0c0c0e]">
                  {block.kind === "montage" ? `Montage · ${block.shots.length} shots` : "Text card"}
                </p>
                <p className="text-[11px] text-[#71717a]">
                  {block.start_s.toFixed(1)}s–{block.end_s.toFixed(1)}s · {block.timing_mode}
                </p>
              </div>
              <div className="flex gap-1">
                <button
                  type="button"
                  onClick={() => onDuplicateBlock?.(block.id)}
                  className="min-h-9 rounded-lg px-2 text-[12px] text-[#3f3f46] hover:bg-zinc-100"
                >
                  Duplicate
                </button>
                <button
                  type="button"
                  onClick={() => onDeleteBlock?.(block.id)}
                  className="min-h-9 rounded-lg px-2 text-[12px] text-red-700 hover:bg-red-50"
                >
                  Delete
                </button>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <label className="text-[11px] text-[#71717a]">
                Start
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={block.start_s}
                  onChange={(event) =>
                    onPatchBlock?.(block.id, { start_s: Number(event.target.value), timing_mode: "manual" })
                  }
                  className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-1.5 text-[#0c0c0e]"
                />
              </label>
              <label className="text-[11px] text-[#71717a]">
                End
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={block.end_s}
                  onChange={(event) =>
                    onPatchBlock?.(block.id, { end_s: Number(event.target.value), timing_mode: "manual" })
                  }
                  className="mt-1 w-full rounded-md border border-zinc-200 px-2 py-1.5 text-[#0c0c0e]"
                />
              </label>
            </div>
            {block.kind === "montage" && (
              <div className="mt-3 space-y-2">
                <button
                  type="button"
                  onClick={() => onRetimeBlock?.(block.id)}
                  className="min-h-9 w-full rounded-lg border border-zinc-200 text-[11px] font-semibold hover:border-zinc-400"
                >
                  Recalculate automatic pacing
                </button>
                {block.shots.map((shot, index) => (
                  <div
                    key={shot.id}
                    draggable
                    onDragStart={() => setDraggedShot({ blockId: block.id, shotId: shot.id })}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={(event) => {
                      event.preventDefault();
                      if (!draggedShot || draggedShot.blockId !== block.id) return;
                      const fromIndex = block.shots.findIndex(
                        (candidate) => candidate.id === draggedShot.shotId,
                      );
                      if (fromIndex < 0 || fromIndex === index) return;
                      const shots = [...block.shots];
                      const [moved] = shots.splice(fromIndex, 1);
                      shots.splice(index, 0, moved);
                      patchShots(block, shots);
                      setDraggedShot(null);
                    }}
                    onDragEnd={() => setDraggedShot(null)}
                    className="rounded-lg border border-zinc-100 bg-zinc-50 p-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-[11px] font-semibold text-[#3f3f46]">
                        Shot {index + 1} · {shot.duration_s.toFixed(2)}s
                      </span>
                      <div className="flex gap-1">
                        <button
                          type="button"
                          aria-label={`Move shot ${index + 1} earlier`}
                          disabled={index === 0}
                          onClick={() => {
                            const shots = [...block.shots];
                            [shots[index - 1], shots[index]] = [shots[index], shots[index - 1]];
                            patchShots(block, shots);
                          }}
                          className="h-8 w-8 rounded border border-zinc-200 disabled:opacity-30"
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          aria-label={`Move shot ${index + 1} later`}
                          disabled={index === block.shots.length - 1}
                          onClick={() => {
                            const shots = [...block.shots];
                            [shots[index], shots[index + 1]] = [shots[index + 1], shots[index]];
                            patchShots(block, shots);
                          }}
                          className="h-8 w-8 rounded border border-zinc-200 disabled:opacity-30"
                        >
                          ↓
                        </button>
                      </div>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-2">
                      <label className="text-[10px] text-[#71717a]">
                        Duration
                        <input
                          type="number"
                          min={0.1}
                          max={Math.max(0.1, block.end_s - block.start_s - 0.1 * (block.shots.length - 1))}
                          step={0.05}
                          value={shot.duration_s}
                          onChange={(event) => {
                            const total = block.end_s - block.start_s;
                            const requested = Math.max(0.1, Number(event.target.value));
                            const others = block.shots.filter((entry) => entry.id !== shot.id);
                            const remaining = Math.max(0.1 * others.length, total - requested);
                            const oldOtherTotal = others.reduce((sum, entry) => sum + entry.duration_s, 0);
                            const shots = block.shots.map((entry) =>
                              entry.id === shot.id
                                ? { ...entry, duration_s: Math.min(requested, total - 0.1 * others.length) }
                                : {
                                    ...entry,
                                    duration_s:
                                      oldOtherTotal > 0
                                        ? (entry.duration_s / oldOtherTotal) * remaining
                                        : remaining / others.length,
                                  },
                            );
                            patchShots(block, shots);
                          }}
                          className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                        />
                      </label>
                      <label className="text-[10px] text-[#71717a]">
                        Replace
                        <select
                          value={shot.asset_id}
                          onChange={(event) => {
                            const asset = ready.find((candidate) => candidate.id === event.target.value);
                            if (!asset) return;
                            patchShots(
                              block,
                              block.shots.map((entry) =>
                                entry.id === shot.id
                                  ? {
                                      ...entry,
                                      asset_id: asset.id,
                                      src_gcs_path: asset.gcs_path,
                                      kind: asset.kind,
                                    }
                                  : entry,
                              ),
                            );
                          }}
                          className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                        >
                          {ready.map((asset) => (
                            <option key={asset.id} value={asset.id}>
                              {asset.source_filename || asset.kind}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <label className="mt-2 block text-[10px] text-[#71717a]">
                      Motion
                      <select
                        value={shot.motion}
                        onChange={(event) => {
                          const shots = block.shots.map((entry) =>
                            entry.id === shot.id
                              ? {
                                  ...entry,
                                  motion: event.target.value as typeof entry.motion,
                                }
                              : entry,
                          );
                          onPatchBlock?.(block.id, {
                            shots,
                            timing_mode: "manual",
                          } as Partial<VisualBlock>);
                        }}
                        className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                      >
                        <option value="none">None</option>
                        <option value="zoom_in">Zoom in</option>
                        <option value="zoom_out">Zoom out</option>
                        <option value="pan_left">Pan left</option>
                        <option value="pan_right">Pan right</option>
                      </select>
                    </label>
                    <label className="mt-2 block text-[10px] text-[#71717a]">
                      Crop scale · {shot.crop.scale.toFixed(2)}×
                      <input
                        type="range"
                        min={1}
                        max={4}
                        step={0.05}
                        value={shot.crop.scale}
                        onChange={(event) =>
                          patchShots(
                            block,
                            block.shots.map((entry) =>
                              entry.id === shot.id
                                ? {
                                    ...entry,
                                    crop: {
                                      ...entry.crop,
                                      scale: Number(event.target.value),
                                    },
                                  }
                                : entry,
                            ),
                          )
                        }
                        className="mt-1 w-full accent-lime-500"
                      />
                    </label>
                    <div className="mt-2 grid grid-cols-2 gap-2">
                      <label className="text-[10px] text-[#71717a]">
                        Focal X · {Math.round(shot.crop.x_frac * 100)}%
                        <input
                          type="range"
                          min={0}
                          max={1}
                          step={0.01}
                          value={shot.crop.x_frac}
                          onChange={(event) =>
                            patchShots(
                              block,
                              block.shots.map((entry) =>
                                entry.id === shot.id
                                  ? {
                                      ...entry,
                                      crop: { ...entry.crop, x_frac: Number(event.target.value) },
                                    }
                                  : entry,
                              ),
                            )
                          }
                          className="mt-1 w-full accent-lime-500"
                        />
                      </label>
                      <label className="text-[10px] text-[#71717a]">
                        Focal Y · {Math.round(shot.crop.y_frac * 100)}%
                        <input
                          type="range"
                          min={0}
                          max={1}
                          step={0.01}
                          value={shot.crop.y_frac}
                          onChange={(event) =>
                            patchShots(
                              block,
                              block.shots.map((entry) =>
                                entry.id === shot.id
                                  ? {
                                      ...entry,
                                      crop: { ...entry.crop, y_frac: Number(event.target.value) },
                                    }
                                  : entry,
                              ),
                            )
                          }
                          className="mt-1 w-full accent-lime-500"
                        />
                      </label>
                    </div>
                    {shot.kind === "video" && (
                      <label className="mt-2 block text-[10px] text-[#71717a]">
                        Source trim start
                        <input
                          type="number"
                          min={0}
                          step={0.05}
                          value={shot.trim_start_s ?? 0}
                          onChange={(event) =>
                            patchShots(
                              block,
                              block.shots.map((entry) =>
                                entry.id === shot.id
                                  ? { ...entry, trim_start_s: Math.max(0, Number(event.target.value)) }
                                  : entry,
                              ),
                            )
                          }
                          className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                        />
                      </label>
                    )}
                    {block.shots.length > 3 && (
                      <button
                        type="button"
                        onClick={() =>
                          patchShots(
                            block,
                            block.shots.filter((entry) => entry.id !== shot.id),
                          )
                        }
                        className="mt-2 min-h-8 text-[10px] font-semibold text-red-700"
                      >
                        Remove shot
                      </button>
                    )}
                  </div>
                ))}
                {block.shots.some((shot) => {
                  const asset = assets.find((candidate) => candidate.id === shot.asset_id);
                  return asset?.width && asset?.height && Math.min(asset.width, asset.height) < 720;
                }) && (
                  <p className="rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-[10px] text-[#3f3f46]">
                    One or more shots may look soft at 1080×1920.
                  </p>
                )}
              </div>
            )}
            {block.kind === "text_card" && (
              <div className="mt-3 space-y-2">
                <div>
                  <div className="mb-1 flex items-center justify-between">
                    <p className="text-[10px] font-semibold text-[#71717a]">Linked text</p>
                    <button
                      type="button"
                      onClick={() => onAddBlockText?.(block.id)}
                      className="min-h-8 rounded-md px-2 text-[10px] font-semibold text-[#3f3f46] hover:bg-zinc-100"
                    >
                      Add text
                    </button>
                  </div>
                  <div className="space-y-1">
                    {textElements
                      .filter((element) => element.visual_block_id === block.id)
                      .map((element, index) => (
                        <button
                          key={element.id || `${block.id}-${index}`}
                          type="button"
                          disabled={!element.id}
                          onClick={() => element.id && onSelectBlockText?.(element.id)}
                          className="block min-h-8 w-full truncate rounded-md bg-zinc-50 px-2 text-left text-[10px] text-[#3f3f46] hover:bg-zinc-100"
                        >
                          {element.text || "Untitled text"}
                        </button>
                      ))}
                  </div>
                </div>
                <label className="block text-[10px] text-[#71717a]">
                  Background type
                  <select
                    value={block.background.type}
                    onChange={(event) => {
                      const type = event.target.value;
                      const firstAsset = ready[0];
                      const background =
                        type === "gradient"
                          ? { type: "gradient" as const, from: "#172035", to: "#26382F", angle_deg: 135 }
                          : type === "blur_previous"
                            ? { type: "blur_previous" as const, blur_px: 28 }
                            : type === "asset" && firstAsset
                              ? {
                                  type: "asset" as const,
                                  shot: {
                                    id: crypto.randomUUID(),
                                    asset_id: firstAsset.id,
                                    src_gcs_path: firstAsset.gcs_path,
                                    kind: firstAsset.kind,
                                    start_offset_s: 0,
                                    duration_s: block.end_s - block.start_s,
                                    crop: { x_frac: 0.5, y_frac: 0.5, scale: 1 },
                                    motion: "none" as const,
                                  },
                                }
                              : { type: "solid" as const, color: "#26382F" };
                      onPatchBlock?.(block.id, { background } as Partial<VisualBlock>);
                    }}
                    className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                  >
                    <option value="solid">Solid colour</option>
                    <option value="gradient">Gradient</option>
                    <option value="blur_previous">Blur previous frame</option>
                    <option value="asset" disabled={ready.length === 0}>Image or video</option>
                  </select>
                </label>
                {block.background.type === "solid" && (
                  <label className="flex items-center justify-between text-[11px] text-[#71717a]">
                    Colour
                    <input
                      type="color"
                      value={block.background.color}
                      onChange={(event) =>
                        onPatchBlock?.(block.id, {
                          background: { type: "solid", color: event.target.value },
                        } as Partial<VisualBlock>)
                      }
                      className="h-9 w-12 rounded border border-zinc-200"
                    />
                  </label>
                )}
                {block.background.type === "gradient" && (
                  <div className="grid grid-cols-3 gap-2">
                    <input
                      aria-label="Gradient start colour"
                      type="color"
                      value={block.background.from}
                      onChange={(event) => onPatchBlock?.(block.id, {
                        background: { ...block.background, from: event.target.value },
                      } as Partial<VisualBlock>)}
                      className="h-9 w-full rounded border border-zinc-200"
                    />
                    <input
                      aria-label="Gradient end colour"
                      type="color"
                      value={block.background.to}
                      onChange={(event) => onPatchBlock?.(block.id, {
                        background: { ...block.background, to: event.target.value },
                      } as Partial<VisualBlock>)}
                      className="h-9 w-full rounded border border-zinc-200"
                    />
                    <input
                      aria-label="Gradient angle"
                      type="number"
                      min={0}
                      max={360}
                      value={block.background.angle_deg}
                      onChange={(event) => onPatchBlock?.(block.id, {
                        background: { ...block.background, angle_deg: Number(event.target.value) },
                      } as Partial<VisualBlock>)}
                      className="h-9 w-full rounded border border-zinc-200 px-2 text-[11px]"
                    />
                  </div>
                )}
                {block.background.type === "blur_previous" && (
                  <label className="block text-[10px] text-[#71717a]">
                    Blur · {block.background.blur_px}px
                    <input
                      type="range"
                      min={4}
                      max={64}
                      value={block.background.blur_px}
                      onChange={(event) => onPatchBlock?.(block.id, {
                        background: { type: "blur_previous", blur_px: Number(event.target.value) },
                      } as Partial<VisualBlock>)}
                      className="mt-1 w-full accent-lime-500"
                    />
                  </label>
                )}
                {block.background.type === "asset" && (
                  <label className="block text-[10px] text-[#71717a]">
                    Background asset
                    <select
                      value={block.background.shot.asset_id}
                      onChange={(event) => {
                        const asset = ready.find((candidate) => candidate.id === event.target.value);
                        if (!asset || block.background.type !== "asset") return;
                        onPatchBlock?.(block.id, {
                          background: {
                            ...block.background,
                            shot: {
                              ...block.background.shot,
                              asset_id: asset.id,
                              src_gcs_path: asset.gcs_path,
                              kind: asset.kind,
                            },
                          },
                        } as Partial<VisualBlock>);
                      }}
                      className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                    >
                      {ready.map((asset) => (
                        <option key={asset.id} value={asset.id}>{asset.source_filename || asset.kind}</option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
            )}
            {block.kind === "text_card" && block.end_s - block.start_s < 1 && (
              <p className="mt-2 rounded-lg border border-zinc-200 bg-white px-2 py-1.5 text-[10px] text-[#3f3f46]">
                This card may be too brief to read comfortably.
              </p>
            )}
            <div className="mt-3 grid grid-cols-2 gap-2">
              <label className="text-[10px] text-[#71717a]">
                Entrance
                <select
                  value={block.transition_in}
                  onChange={(event) => onPatchBlock?.(block.id, {
                    transition_in: event.target.value as "cut" | "fade",
                  })}
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                >
                  <option value="cut">Cut</option>
                  <option value="fade">Fade</option>
                </select>
              </label>
              <label className="text-[10px] text-[#71717a]">
                Exit
                <select
                  value={block.transition_out}
                  onChange={(event) => onPatchBlock?.(block.id, {
                    transition_out: event.target.value as "cut" | "fade",
                  })}
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                >
                  <option value="cut">Cut</option>
                  <option value="fade">Fade</option>
                </select>
              </label>
              <label className="text-[10px] text-[#71717a]">
                Base audio
                <select
                  value={block.audio_policy.base}
                  onChange={(event) =>
                    onPatchBlock?.(block.id, {
                      audio_policy: {
                        ...block.audio_policy,
                        base: event.target.value as "continue" | "mute",
                      },
                    } as Partial<VisualBlock>)
                  }
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                >
                  <option value="continue">Continue</option>
                  <option value="mute">Mute</option>
                </select>
              </label>
              <label className="text-[10px] text-[#71717a]">
                Sound effects
                <select
                  value={block.audio_policy.sfx}
                  onChange={(event) =>
                    onPatchBlock?.(block.id, {
                      audio_policy: {
                        ...block.audio_policy,
                        sfx: event.target.value as "continue" | "mute",
                      },
                    } as Partial<VisualBlock>)
                  }
                  className="mt-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-[11px] text-[#0c0c0e]"
                >
                  <option value="continue">Continue</option>
                  <option value="mute">Mute</option>
                </select>
              </label>
            </div>
          </div>
        ))}
        {blocks.length === 0 && (
          <p className="rounded-lg border border-dashed border-zinc-300 px-3 py-4 text-center text-[12px] text-[#71717a]">
            No visual blocks yet.
          </p>
        )}
      </section>
    </div>
  );
}

function SoundsDrawer({
  effects,
  loading,
  onAddSfx,
  musicTracks,
  musicLoading,
  currentMusicTrackId,
  musicEditable,
  onPickMusic,
  musicWindow,
}: {
  effects: SoundEffectSummary[];
  loading: boolean;
  onAddSfx?: (effect: SoundEffectSummary) => void;
  musicTracks: MusicTrackSummary[];
  musicLoading: boolean;
  currentMusicTrackId: string | null;
  musicEditable: boolean;
  onPickMusic?: (trackId: string) => void;
  musicWindow?: SongWindowControl;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
      <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Music</p>
      {!musicEditable ? (
        <div className="mb-5 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
          Music cannot be edited for this version.
        </div>
      ) : musicLoading ? (
        <div className="mb-5 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
          Loading songs...
        </div>
      ) : (
        <div className="mb-5 max-h-48 space-y-2 overflow-y-auto pr-1">
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
                  <span className={selected ? "block truncate text-[11px] text-white/70" : "block truncate text-[11px] text-[#71717a]"}>
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
      )}
      {musicWindow && (
        <div className="mb-5">
          <SongWindowSelector {...musicWindow} />
        </div>
      )}
      <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Effects</p>
      {loading ? (
        <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
          Loading effects...
        </div>
      ) : (
        <div className="space-y-2">
          {effects.map((effect) => (
            <button
              key={effect.id}
              type="button"
              onClick={() => onAddSfx?.(effect)}
              className="flex min-h-11 w-full items-center justify-between rounded-lg border border-zinc-200 bg-white px-3 text-left text-[13px] text-[#0c0c0e] hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500"
            >
              <span className="truncate">{effect.name}</span>
              <span className="ml-2 shrink-0 text-[11px] text-[#71717a]">
                {effect.duration_s != null ? `${effect.duration_s.toFixed(1)}s` : "SFX"}
              </span>
            </button>
          ))}
          {effects.length === 0 && (
            <div className="rounded-lg border border-dashed border-zinc-300 px-3 py-3 text-[12px] text-[#71717a]">
              No published sound effects found.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const OVERLAY_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "video/mp4",
  "video/quicktime",
];

function OverlaysDrawer({
  uploading,
  onOverlayUpload,
  suggestions,
}: {
  uploading: boolean;
  onOverlayUpload?: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => void;
  suggestions?: React.ReactNode;
}) {
  const [dragOver, setDragOver] = useState(false);

  function handleFiles(fileList: FileList | null) {
    if (!fileList) return;
    const files = Array.from(fileList)
      .filter((file) => OVERLAY_MIME_TYPES.includes(file.type))
      .map((file) => ({
        file,
        filename: file.name,
        content_type: file.type,
        file_size_bytes: file.size,
      }));
    if (files.length > 0) onOverlayUpload?.(files);
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
      <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Media overlay</p>
      <label
        className={`flex min-h-[128px] cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed px-4 text-center text-[13px] transition-colors ${
          dragOver
            ? "border-[#0c0c0e] bg-zinc-100 text-[#0c0c0e]"
            : "border-zinc-300 bg-zinc-50 text-[#71717a] hover:border-zinc-400"
        } ${uploading ? "pointer-events-none opacity-50" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          handleFiles(e.dataTransfer.files);
        }}
      >
        <input
          type="file"
          multiple
          accept={OVERLAY_MIME_TYPES.join(",")}
          className="hidden"
          onChange={(e) => {
            handleFiles(e.target.files);
            e.target.value = "";
          }}
        />
        {uploading ? "Uploading..." : "Drop image/video or click to upload"}
      </label>
      {suggestions}
    </div>
  );
}
