"use client";

/**
 * ToolDrawer — the left drawer that opens for the active rail tool (plan §2).
 *
 * Text tool: "Basic" section with a full-width "Add text" button (creates a
 * default 2.0s bar at the playhead, first Basic preset, selects it → the
 * inspector populates), then "Presets" with category chips (dark ink pill =
 * selected) above the 4-column preset grid.
 */

import { useMemo, useState } from "react";
import {
  PRESET_CATEGORIES,
  TEXT_PRESETS,
  type TextPreset,
  type TextPresetCategory,
} from "@/lib/text-presets";
import PresetGrid from "./PresetGrid";
import type { EditorTool } from "./ToolRail";

const CATEGORY_LABEL: Record<TextPresetCategory, string> = {
  basic: "Basic",
  trending: "Trending",
};

export default function ToolDrawer({
  tool,
  sampleWord,
  appliedPresetId,
  onAddText,
  onPickPreset,
  onClose,
}: {
  tool: EditorTool;
  sampleWord: string | null;
  appliedPresetId: string | null;
  onAddText: () => void;
  onPickPreset: (preset: TextPreset) => void;
  onClose: () => void;
}) {
  const [category, setCategory] = useState<TextPresetCategory>("basic");

  const presets = useMemo(
    () => TEXT_PRESETS.filter((p) => p.category === category),
    [category],
  );

  return (
    <div
      data-region="tool-drawer"
      className="flex w-[360px] flex-col border-r border-zinc-200 bg-white motion-safe:animate-fade-up"
    >
      <div className="flex flex-none items-center justify-between px-5 pb-3 pt-4">
        <h2 className="font-display text-[18px] text-[#0c0c0e]">
          {tool === "text" ? "Text" : tool[0].toUpperCase() + tool.slice(1)}
        </h2>
        <button
          type="button"
          aria-label="Close drawer"
          onClick={onClose}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-[13px] text-[#71717a] hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
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
            className="h-10 w-full rounded-lg bg-zinc-100 text-[13px] font-semibold text-[#0c0c0e] hover:bg-zinc-200 focus-visible:outline-2 focus-visible:outline-[#0c0c0e]"
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
                  className={`inline-flex h-7 items-center rounded-full px-3 text-[12px] ${
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
            onPick={onPickPreset}
          />
        </div>
      )}
    </div>
  );
}
