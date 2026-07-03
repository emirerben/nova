"use client";

/**
 * PresetGrid — 4-column grid of text-preset thumbnails, shared by the Text
 * drawer and the inspector's Presets tab.
 *
 * Each tile renders in the preset's REAL look (registry font via
 * resolveCssFont, fill color, stroke, highlight chip) on a near-black tile so
 * light fills read like they do over footage. Thumbnail word = the selected
 * text's first word (live), falling back to Nova-voiced samples — never "ART"
 * (plan Pass 4). The applied preset carries a lime selection ring.
 */

import { resolveCssFont } from "@/lib/overlay-constants";
import {
  presetSampleWord,
  type TextPreset,
} from "@/lib/text-presets";

/** A preset counts as "applied" when every field it sets matches the bar. */
export function presetMatchesFields(
  preset: TextPreset,
  current: {
    font_family?: string | null;
    color?: string | null;
    highlight_color?: string | null;
    stroke_width?: number | null;
    effect?: string | null;
  } | null,
): boolean {
  if (!current) return false;
  const f = preset.fields;
  return (
    (f.font_family ?? null) === (current.font_family ?? null) &&
    (f.color ?? null) === (current.color ?? null) &&
    (f.highlight_color ?? null) === (current.highlight_color ?? null) &&
    (f.stroke_width ?? 0) === (current.stroke_width ?? 0) &&
    (f.effect ?? null) === (current.effect ?? null)
  );
}

export default function PresetGrid({
  presets,
  sampleWord,
  appliedPresetId,
  favoritePresetIds = [],
  onToggleFavorite,
  onPick,
}: {
  presets: TextPreset[];
  /** First word of the selected text, or null → Nova sample words. */
  sampleWord: string | null;
  appliedPresetId: string | null;
  favoritePresetIds?: string[];
  onToggleFavorite?: (presetId: string) => void;
  onPick: (preset: TextPreset) => void;
}) {
  const favoriteSet = new Set(favoritePresetIds);

  return (
    <div role="radiogroup" aria-label="Text presets" className="grid grid-cols-4 gap-2.5">
      {presets.map((preset, i) => {
        const word = sampleWord ?? presetSampleWord(i);
        const { family, weight } = resolveCssFont(preset.fields.font_family);
        const applied = appliedPresetId === preset.id;
        const favorite = favoriteSet.has(preset.id);
        return (
          <div key={preset.id} className="group relative aspect-square">
            <button
              type="button"
              role="radio"
              aria-checked={applied}
              aria-label={`Text preset: ${preset.label}`}
              title={preset.label}
              onClick={() => onPick(preset)}
              className={`flex h-full min-h-11 w-full min-w-11 items-center justify-center overflow-hidden rounded-xl border border-zinc-200 bg-[#0c0c0e] px-1 hover:border-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                applied ? "outline outline-2 outline-offset-1 outline-lime-500" : ""
              }`}
            >
              <span
                className="max-w-full truncate text-[15px] leading-tight"
                style={{
                  fontFamily: family,
                  fontWeight: weight,
                  color: preset.fields.color ?? "#FFFFFF",
                  WebkitTextStroke:
                    (preset.fields.stroke_width ?? 0) > 0 ? "0.6px #000000" : undefined,
                  backgroundColor: preset.fields.highlight_color ?? undefined,
                  padding: preset.fields.highlight_color ? "1px 5px" : undefined,
                  borderRadius: preset.fields.highlight_color ? 3 : undefined,
                }}
              >
                {word}
              </span>
            </button>
            {onToggleFavorite && (
              <button
                type="button"
                aria-pressed={favorite}
                aria-label={`${favorite ? "Remove" : "Add"} ${preset.label} favorite`}
                onClick={(event) => {
                  event.stopPropagation();
                  onToggleFavorite(preset.id);
                }}
                className={`absolute right-0 top-0 flex h-11 w-11 items-center justify-center rounded-full text-[12px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-lime-500 ${
                  favorite
                    ? "bg-white text-[#0c0c0e]"
                    : "bg-black/35 text-white/70 group-hover:bg-white group-hover:text-[#0c0c0e]"
                }`}
              >
                ★
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
