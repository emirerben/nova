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
import type { EditorTool } from "./ToolRail";

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
  onPickPreset,
  appliedStyleSetId = null,
  onRestyleAll,
  onClose,
}: {
  tool: EditorTool;
  sampleWord: string | null;
  appliedPresetId: string | null;
  onAddText: () => void;
  onPickPreset: (preset: TextPreset) => void;
  appliedStyleSetId?: string | null;
  onRestyleAll?: (styleSet: GenerativeStyleSet) => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState<TextPresetCategory>("basic");
  const [favoritePresetIds, setFavoritePresetIds] = useState<string[]>([]);

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
    tool === "text"
      ? "Text"
      : tool === "styles"
        ? "Styles"
        : tool[0].toUpperCase() + tool.slice(1);

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
    </div>
  );
}
