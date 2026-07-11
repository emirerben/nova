/**
 * Text preset registry for the TikTok-parity editor (T3/T7).
 *
 * A preset is a NAMED COMBINATION OF EXISTING TextElement fields — nothing
 * here introduces a field the renderer parity seam doesn't already honor
 * (font_family / color / highlight_color / stroke_width / effect only).
 * New style fields (background, shadow, weight, …) arrive via the
 * PARITY_VERIFIED_FIELDS gate (lib/parity-verified-fields.ts) in a later
 * task; presets may only reference fields that are already verified.
 *
 * Consumed by the editor drawer's preset grid and the inspector's Presets
 * tab (_editor/PresetGrid.tsx). Thumbnails render the SELECTED text's first
 * word live, falling back to Kria-voiced samples — never "ART" (plan Pass 4).
 */

import { FONT_REGISTRY, TEXT_ELEMENT_FONT_ALIASES } from "@/lib/overlay-constants";
import type { TextElement } from "@/lib/plan-api";

/** Preset categories shown as pill chips above the grid. `favorite` is
 * user-persisted (T7, localStorage v1) and not represented in the registry. */
export type TextPresetCategory = "favorite" | "trending" | "basic";

export const TEXT_PRESET_FAVORITES_STORAGE_KEY = "nova.text-preset-favorites";

/** The subset of TextElement style fields a preset is allowed to set. */
export type TextPresetFields = Pick<
  TextElement,
  "font_family" | "color" | "highlight_color" | "stroke_width"
> & {
  effect?: string | null;
};

export interface TextPreset {
  id: string;
  label: string;
  category: Exclude<TextPresetCategory, "favorite">;
  trending: boolean;
  fields: TextPresetFields;
}

/** Fallback thumbnail words when no text element is selected — Kria-voiced,
 * rotated per grid index (plan Pass 4: brand shows up where personality lives). */
export const PRESET_SAMPLE_WORDS = ["HOOK", "THIS", "WAIT"] as const;

export function presetSampleWord(index: number): string {
  return PRESET_SAMPLE_WORDS[index % PRESET_SAMPLE_WORDS.length];
}

export function normalizeTextPresetFavorites(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(value.filter((id): id is string => typeof id === "string" && id.length > 0)),
  );
}

export function readTextPresetFavorites(storage: Pick<Storage, "getItem"> | null): string[] {
  if (!storage) return [];
  try {
    return normalizeTextPresetFavorites(
      JSON.parse(storage.getItem(TEXT_PRESET_FAVORITES_STORAGE_KEY) ?? "[]"),
    );
  } catch {
    return [];
  }
}

export function writeTextPresetFavorites(
  storage: Pick<Storage, "setItem"> | null,
  favoriteIds: string[],
): void {
  if (!storage) return;
  storage.setItem(
    TEXT_PRESET_FAVORITES_STORAGE_KEY,
    JSON.stringify(normalizeTextPresetFavorites(favoriteIds)),
  );
}

export function toggleTextPresetFavorite(favoriteIds: string[], presetId: string): string[] {
  const normalized = normalizeTextPresetFavorites(favoriteIds);
  return normalized.includes(presetId)
    ? normalized.filter((id) => id !== presetId)
    : [...normalized, presetId];
}

export function filterTextPresetsByCategory(
  presets: TextPreset[],
  category: TextPresetCategory,
  favoriteIds: string[],
): TextPreset[] {
  if (category === "favorite") {
    const favorites = new Set(favoriteIds);
    return presets.filter((preset) => favorites.has(preset.id));
  }
  if (category === "trending") {
    return presets.filter((preset) => preset.trending);
  }
  return presets.filter((preset) => !preset.trending);
}

/**
 * Starter set. font_family values are font-registry keys (overlay-constants
 * FONT_REGISTRY / INTRO_FONTS derive from the same registry, so every family
 * here has a bundled @font-face AND is a valid backend font).
 */
export const TEXT_PRESETS: TextPreset[] = [
  {
    id: "classic-serif",
    label: "Classic serif",
    category: "basic",
    trending: false,
    fields: { font_family: "PlayfairDisplay-Bold", color: "#FFFFFF", stroke_width: 0, effect: "fade-in" },
  },
  {
    id: "clean-caption",
    label: "Clean caption",
    category: "basic",
    trending: false,
    fields: { font_family: "Inter-Bold", color: "#FFFFFF", stroke_width: 0, effect: "static" },
  },
  {
    id: "bold-punch",
    label: "Bold punch",
    category: "trending",
    trending: true,
    fields: { font_family: "Inter-Bold", color: "#FFFFFF", stroke_width: 4, effect: "slide-up" },
  },
  {
    id: "editorial-italic",
    label: "Editorial",
    category: "basic",
    trending: false,
    fields: { font_family: "PlayfairDisplay-Regular", color: "#F5F5DC", stroke_width: 0, effect: "fade-in" },
  },
  {
    id: "headline-heavy",
    label: "Headline",
    category: "trending",
    trending: true,
    fields: { font_family: "Inter-Bold", color: "#FFFFFF", stroke_width: 3, effect: "slide-up" },
  },
  {
    id: "gold-pop",
    label: "Gold pop",
    category: "trending",
    trending: true,
    fields: {
      font_family: "Inter-Bold",
      color: "#FFFFFF",
      highlight_color: "#FFD24A",
      stroke_width: 0,
      effect: "karaoke-line",
    },
  },
  {
    id: "marker-note",
    label: "Marker",
    category: "trending",
    trending: true,
    fields: { font_family: "PlayfairDisplay-Bold", color: "#FFD24A", stroke_width: 0, effect: "fade-in" },
  },
  {
    id: "ink-outline",
    label: "Ink outline",
    category: "trending",
    trending: true,
    fields: { font_family: "Inter-Bold", color: "#FFFFFF", stroke_width: 5, effect: "slide-up" },
  },
  {
    id: "space-clean",
    label: "Space clean",
    category: "basic",
    trending: false,
    fields: { font_family: "Inter-Regular", color: "#FFFFFF", stroke_width: 0, effect: "static" },
  },
  {
    id: "cream-serif",
    label: "Cream serif",
    category: "basic",
    trending: false,
    fields: { font_family: "PlayfairDisplay-Bold", color: "#F5F5DC", stroke_width: 0, effect: "fade-in" },
  },
  {
    id: "creator-sans",
    label: "Creator sans",
    category: "basic",
    trending: false,
    fields: { font_family: "Inter-Regular", color: "#FFFFFF", stroke_width: 2, effect: "static" },
  },
  {
    id: "viral-comic",
    label: "Viral comic",
    category: "trending",
    trending: true,
    fields: { font_family: "Inter-Bold", color: "#FFD24A", stroke_width: 4, effect: "slide-up" },
  },
];

/** The default look for a freshly added text element ("Add text" button):
 * the first Basic preset, per the plan's drawer spec. */
export const DEFAULT_TEXT_PRESET: TextPreset =
  TEXT_PRESETS.find((p) => p.category === "basic") ?? TEXT_PRESETS[0];

export const PRESET_CATEGORIES: TextPresetCategory[] = ["favorite", "trending", "basic"];

/** Dev-time sanity: every preset font must exist in the registry (a typo here
 * would silently fall back to Playfair in previews AND the burn). */
if (process.env.NODE_ENV !== "production") {
  for (const p of TEXT_PRESETS) {
    if (
      p.fields.font_family &&
      !FONT_REGISTRY[p.fields.font_family] &&
      !TEXT_ELEMENT_FONT_ALIASES[p.fields.font_family]
    ) {
      // eslint-disable-next-line no-console
      console.warn(`text-presets: unknown font_family "${p.fields.font_family}" in preset ${p.id}`);
    }
  }
}
