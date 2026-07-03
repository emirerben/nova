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
 * word live, falling back to Nova-voiced samples — never "ART" (plan Pass 4).
 */

import { FONT_REGISTRY } from "@/lib/overlay-constants";
import type { TextElement } from "@/lib/plan-api";

/** Preset categories shown as pill chips above the grid. `favorite` is
 * user-persisted (T7, localStorage v1) and not represented in the registry. */
export type TextPresetCategory = "basic" | "trending";

/** The subset of TextElement style fields a preset is allowed to set. */
export type TextPresetFields = Pick<
  TextElement,
  "font_family" | "color" | "highlight_color" | "stroke_width" | "effect"
>;

export interface TextPreset {
  id: string;
  label: string;
  category: TextPresetCategory;
  fields: TextPresetFields;
}

/** Fallback thumbnail words when no text element is selected — Nova-voiced,
 * rotated per grid index (plan Pass 4: brand shows up where personality lives). */
export const PRESET_SAMPLE_WORDS = ["HOOK", "THIS", "WAIT"] as const;

export function presetSampleWord(index: number): string {
  return PRESET_SAMPLE_WORDS[index % PRESET_SAMPLE_WORDS.length];
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
    fields: { font_family: "Playfair Display", color: "#FFFFFF", stroke_width: 0, effect: "fade-in" },
  },
  {
    id: "clean-caption",
    label: "Clean caption",
    category: "basic",
    fields: { font_family: "TikTok Sans Bold", color: "#FFFFFF", stroke_width: 0, effect: "static" },
  },
  {
    id: "bold-punch",
    label: "Bold punch",
    category: "basic",
    fields: { font_family: "Anton", color: "#FFFFFF", stroke_width: 4, effect: "static" },
  },
  {
    id: "editorial-italic",
    label: "Editorial",
    category: "basic",
    fields: { font_family: "Instrument Serif", color: "#F5F5DC", stroke_width: 0, effect: "fade-in" },
  },
  {
    id: "headline-heavy",
    label: "Headline",
    category: "trending",
    fields: { font_family: "Archivo Black", color: "#FFFFFF", stroke_width: 3, effect: "slide-up" },
  },
  {
    id: "gold-karaoke",
    label: "Gold karaoke",
    category: "trending",
    fields: {
      font_family: "Montserrat",
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
    fields: { font_family: "Permanent Marker", color: "#FFD24A", stroke_width: 0, effect: "static" },
  },
  {
    id: "ink-outline",
    label: "Ink outline",
    category: "trending",
    fields: { font_family: "Bebas Neue", color: "#FFFFFF", stroke_width: 5, effect: "slide-up" },
  },
];

/** The default look for a freshly added text element ("Add text" button):
 * the first Basic preset, per the plan's drawer spec. */
export const DEFAULT_TEXT_PRESET: TextPreset =
  TEXT_PRESETS.find((p) => p.category === "basic") ?? TEXT_PRESETS[0];

export const PRESET_CATEGORIES: TextPresetCategory[] = ["basic", "trending"];

/** Dev-time sanity: every preset font must exist in the registry (a typo here
 * would silently fall back to Playfair in previews AND the burn). */
if (process.env.NODE_ENV !== "production") {
  for (const p of TEXT_PRESETS) {
    if (p.fields.font_family && !FONT_REGISTRY[p.fields.font_family]) {
      // eslint-disable-next-line no-console
      console.warn(`text-presets: unknown font_family "${p.fields.font_family}" in preset ${p.id}`);
    }
  }
}
