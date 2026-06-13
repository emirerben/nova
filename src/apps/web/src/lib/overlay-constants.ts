/**
 * Shared overlay rendering constants — mirrors the backend renderer's source of
 * truth (src/apps/api/app/pipeline/text_overlay.py / text_overlay_skia.py).
 *
 * Lives in src/lib (not app/admin/...) so PUBLIC pages — the generative instant
 * editor — can import it without reaching into admin component internals. The
 * admin overlay editor re-exports these from its overlay-constants.ts so the two
 * surfaces can never drift.
 */

import fontRegistryJson from "@/data/font-registry.json";

// Output canvas dimensions (9:16, 1080x1920) — all overlay math is expressed at
// this scale and projected down to the preview box by the consumer.
export const CANVAS_W = 1080;
export const CANVAS_H = 1920;

// Must match _POSITION_Y in src/apps/api/app/pipeline/text_overlay.py
export const POSITION_Y_MAP: Record<string, number> = {
  top: 0.15,
  "center-above": 0.42,
  center: 0.45,
  "center-label": 0.4720,
  "center-below": 0.55,
  bottom: 0.85,
};

// Must match _FONT_SIZE_MAP in src/apps/api/app/pipeline/text_overlay.py
export const FONT_SIZE_MAP: Record<string, number> = {
  small: 36,
  medium: 72,
  large: 120,
  xlarge: 150,
  xxlarge: 250,
  jumbo: 199,
};

// ── Font registry (sourced from src/data/font-registry.json) ─────────────────
// The JSON is a byte-identical mirror of src/apps/api/assets/fonts/font-registry.json,
// kept in sync by `scripts/sync-font-registry.mjs` (runs on `npm run dev`,
// checked on `npm run build`). Edit the backend file; the web copy follows.

export const FONT_VIBES = [
  "viral_headlines",
  "clean_captions",
  "editorial",
  "handwritten",
  "script",
] as const;
export type FontVibe = (typeof FONT_VIBES)[number];

export interface FontRegistryEntry {
  file: string;
  ass_name: string;
  weight: number;
  category: string;
  css_family: string;
  cycle_role?: string;
  vibe?: FontVibe;
  deprecated?: true;
}

interface FontRegistryFile {
  fonts: Record<string, FontRegistryEntry>;
  style_defaults: Record<string, string>;
}

const _registry = fontRegistryJson as FontRegistryFile;
export const FONT_REGISTRY: Record<string, FontRegistryEntry> = _registry.fonts;
export const STYLE_DEFAULTS: Record<string, string> = _registry.style_defaults;

/**
 * Resolve a registry font name to its CSS family + weight.
 * Mirrors the server's font fallback: an unknown/missing family lands on
 * Playfair Display Bold (text_overlay_skia._typeface_for_overlay last resort).
 */
export function resolveCssFont(fontFamily: string | null | undefined): {
  family: string;
  weight: number;
} {
  if (fontFamily) {
    const entry = FONT_REGISTRY[fontFamily];
    if (entry) return { family: entry.css_family, weight: entry.weight };
  }
  const fallback = FONT_REGISTRY["Playfair Display"];
  return fallback
    ? { family: fallback.css_family, weight: fallback.weight }
    : { family: "'Playfair Display', serif", weight: 700 };
}

/**
 * Resolve a registry font name to CSS family + weight + STYLE — the editorial
 * cluster preview needs the italic flag (`Playfair Display Italic` shares its
 * family + weight with the Regular sibling and is addressable only via
 * `font-style: italic`; the matching @font-face is emitted by font-faces.ts,
 * which detects italic from the TTF file name). The italic signal comes from the
 * registry entry's `file` for the same reason. Unknown families fall back to
 * Playfair Display Bold (upright), matching the server.
 */
export function resolveClusterCssFont(fontFamily: string | null | undefined): {
  family: string;
  weight: number;
  style: "normal" | "italic";
} {
  const entry = fontFamily ? FONT_REGISTRY[fontFamily] : undefined;
  if (entry) {
    return {
      family: entry.css_family,
      weight: entry.weight,
      style: /italic/i.test(entry.file) ? "italic" : "normal",
    };
  }
  const { family, weight } = resolveCssFont(fontFamily);
  return { family, weight, style: "normal" };
}

// ── Instant-editor animation + typography picker constants ───────────────────

/**
 * Maximum intro reveal window (seconds) — mirrors MAX_INTRO_S in
 * generative_build.py. Used as `durationS` for the animation preview so the
 * entrance matches the burn (all entrance effects saturate ≤0.6s regardless).
 */
export const MAX_INTRO_S = 4.0;

/**
 * Core entrance effects exposed in the instant-editor animation picker.
 * Must be a subset of effects animationStateAt handles non-trivially.
 * Mirror of _INTRO_ANIMATION_EFFECTS in style_sets.py.
 */
export interface IntroAnimation {
  value: string;
  label: string;
}
export const INTRO_ANIMATIONS: IntroAnimation[] = [
  { value: "fade-in",    label: "Fade in"    },
  { value: "pop-in",     label: "Pop in"     },
  { value: "scale-up",   label: "Scale up"   },
  { value: "slide-up",   label: "Slide up"   },
  { value: "slide-down", label: "Slide down" },
  { value: "bounce",     label: "Bounce"     },
  { value: "typewriter", label: "Typewriter" },
  { value: "stream-in",  label: "Stream in"  },
  { value: "none",       label: "None"       },
];

/**
 * Live (non-deprecated) fonts from the registry for the instant-editor Font picker.
 * Derived from FONT_REGISTRY so every entry is guaranteed to have a bundled
 * @font-face (font-faces.ts iterates the same registry) AND be a valid backend font.
 */
export const INTRO_FONTS: Array<{ name: string; cssFamily: string; weight: number }> =
  Object.entries(FONT_REGISTRY)
    .filter(([, entry]) => !entry.deprecated)
    .map(([name, entry]) => ({
      name,
      cssFamily: entry.css_family,
      weight: entry.weight,
    }));

/**
 * Curated text color swatches for the instant-editor Color picker.
 * High-contrast over dark video footage.
 */
export interface IntroColor {
  hex: string;
  label: string;
}
export const INTRO_COLORS: IntroColor[] = [
  { hex: "#FFFFFF", label: "White"      },
  { hex: "#F5F5DC", label: "Cream"      },
  { hex: "#FFD24A", label: "Gold"       },
  { hex: "#FF6B6B", label: "Coral"      },
  { hex: "#7FFFD4", label: "Aqua"       },
  { hex: "#E0E0E0", label: "Light grey" },
  { hex: "#000000", label: "Black"      },
];
