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
  onSplitSmartPlaceText,
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
  overlayUploading = false,
  onOverlayUpload,
  overlaySuggestions = null,
  layoutMode = "full",
  copilot,
  onClose,
}: {
  tool: EditorTool;
  sampleWord: string | null;
  appliedPresetId: string | null;
  onAddText: () => void;
  onSplitSmartPlaceText?: (text: string) => boolean;
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
  overlayUploading?: boolean;
  onOverlayUpload?: (
    files: { file: File; filename: string; content_type: string; file_size_bytes: number }[],
  ) => void;
  /** "AI suggestions" section for the Overlays pane (EditorShell gates it on
   *  the autoplace flag + the variant's `suggestions` capability). */
  overlaySuggestions?: React.ReactNode;
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
        <h2 className="font-display text-[18px] text-[#0c0c0e]">
          {title}
        </h2>
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
              rows={3}
              aria-label="Composition text"
              placeholder="One full title"
              className="w-full resize-none rounded-lg border border-zinc-200 px-3 py-2 text-[13px] text-[#0c0c0e] placeholder:text-[#a1a1aa] focus:border-lime-500/60 focus:outline-none"
            />
            <button
              type="button"
              onClick={() => {
                if (onSplitSmartPlaceText?.(smartTextDraft)) {
                  setSmartTextDraft("");
                }
              }}
              disabled={
                !splitSmartPlaceAvailable ||
                !onSplitSmartPlaceText ||
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

function SoundsDrawer({
  effects,
  loading,
  onAddSfx,
  musicTracks,
  musicLoading,
  currentMusicTrackId,
  musicEditable,
  onPickMusic,
}: {
  effects: SoundEffectSummary[];
  loading: boolean;
  onAddSfx?: (effect: SoundEffectSummary) => void;
  musicTracks: MusicTrackSummary[];
  musicLoading: boolean;
  currentMusicTrackId: string | null;
  musicEditable: boolean;
  onPickMusic?: (trackId: string) => void;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
      <p className="mb-2 text-[12px] font-semibold text-[#3f3f46]">Music</p>
      {!musicEditable ? (
        <div className="mb-5 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[12px] text-[#71717a]">
          This edit has no swappable song.
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
