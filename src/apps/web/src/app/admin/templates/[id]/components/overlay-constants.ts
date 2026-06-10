import type { RecipeTextOverlay, TextSpan, OverlayPosition, OverlayRole, TextSize } from "./recipe-types";

// ── Backend-to-frontend mapping ─────────────────────────────────────────────
// Canvas dims, position/size maps and the font registry are shared with the
// public generative instant editor — single-sourced in @/lib/overlay-constants
// (must match src/apps/api/app/pipeline/text_overlay.py). Re-exported here so
// the admin editor's many importers keep their import paths.
import {
  CANVAS_W,
  FONT_REGISTRY,
  FONT_SIZE_MAP,
  FONT_VIBES,
  POSITION_Y_MAP,
  STYLE_DEFAULTS,
  type FontRegistryEntry,
  type FontVibe,
} from "@/lib/overlay-constants";

export { CANVAS_W, FONT_REGISTRY, FONT_SIZE_MAP, FONT_VIBES, POSITION_Y_MAP };
export type { FontRegistryEntry, FontVibe };

export const PREVIEW_W = 280;
export const SCALE = PREVIEW_W / CANVAS_W; // 0.259

export const FONT_NAMES = Object.keys(FONT_REGISTRY);
export const ACTIVE_FONT_NAMES = FONT_NAMES.filter(
  (name) => !FONT_REGISTRY[name]?.deprecated,
);

export const FONT_VIBE_LABELS: Record<FontVibe, string> = {
  viral_headlines: "Viral Headlines",
  clean_captions: "Clean Captions",
  editorial: "Editorial",
  handwritten: "Handwritten",
  script: "Script",
};

export function isDeprecatedFont(family: string | null | undefined): boolean {
  return !!family && FONT_REGISTRY[family]?.deprecated === true;
}

export function isActiveFont(family: string | null | undefined): boolean {
  return !!family && !!FONT_REGISTRY[family] && !isDeprecatedFont(family);
}

export function fontPreviewSlug(family: string): string {
  return family.toLowerCase().replace(/\s+/g, "-");
}

/** Legacy map — kept for code that still references it by font_style key */
export const FONT_FAMILY_MAP: Record<string, { family: string; weight: number; italic?: boolean }> = {
  display: { family: "'Playfair Display', serif", weight: 700 },
  sans: { family: "'Montserrat', sans-serif", weight: 800 },
  serif: { family: "'Playfair Display', serif", weight: 400 },
  serif_italic: { family: "'Instrument Serif', serif", weight: 400, italic: true },
  script: { family: "'Fraunces', serif", weight: 700 },
};

/**
 * Resolve CSS font-family and weight for an overlay.
 * Priority: font_family (registry) > font_style (legacy map)
 */
export function getFontCssFamily(overlay: RecipeTextOverlay): {
  family: string;
  weight: number;
  italic?: boolean;
} {
  // 1. Direct font_family lookup
  if (overlay.font_family) {
    const entry = FONT_REGISTRY[overlay.font_family];
    if (entry) {
      return { family: entry.css_family, weight: entry.weight };
    }
  }
  // 2. Legacy font_style lookup
  const style = FONT_FAMILY_MAP[overlay.font_style] ?? FONT_FAMILY_MAP.sans;
  return { family: style.family, weight: style.weight, italic: style.italic };
}

/**
 * Get the inferred font name for an overlay (for the font picker placeholder).
 * If font_family is set, return it. Otherwise, infer from style_defaults.
 */
export function getInferredFontName(overlay: RecipeTextOverlay): string {
  if (overlay.font_family) return overlay.font_family;
  return STYLE_DEFAULTS[overlay.font_style] ?? "Montserrat";
}

export const OVERLAY_ROLE_COLORS: Record<OverlayRole, string> = {
  hook: "#F59E0B",
  reaction: "#3B82F6",
  cta: "#EF4444",
  label: "#8B5CF6",
};

export const MAX_OVERLAY_TEXT_LEN = 40;

// Snap zones for vertical drag (fraction of container height)
export const SNAP_ZONES: { position: OverlayPosition; y: number }[] = [
  { position: "top", y: POSITION_Y_MAP.top },
  { position: "center-above", y: POSITION_Y_MAP["center-above"] },
  { position: "center", y: POSITION_Y_MAP.center },
  { position: "center-label", y: POSITION_Y_MAP["center-label"] },
  { position: "center-below", y: POSITION_Y_MAP["center-below"] },
  { position: "bottom", y: POSITION_Y_MAP.bottom },
];

// ── Pure helper functions (testable) ────────────────────────────────────────

export function getEffectiveTiming(overlay: RecipeTextOverlay): {
  start: number;
  end: number;
} {
  return {
    start: overlay.start_s_override ?? overlay.start_s,
    end: overlay.end_s_override ?? overlay.end_s,
  };
}

export function isOverlayVisible(
  currentTimeInSlot: number,
  overlay: RecipeTextOverlay,
): boolean {
  const { start, end } = getEffectiveTiming(overlay);
  return currentTimeInSlot >= start && currentTimeInSlot <= end;
}

export function snapToNearestZone(yFraction: number): OverlayPosition {
  let closest = SNAP_ZONES[0];
  let minDist = Math.abs(yFraction - closest.y);
  for (let i = 1; i < SNAP_ZONES.length; i++) {
    const dist = Math.abs(yFraction - SNAP_ZONES[i].y);
    if (dist < minDist) {
      minDist = dist;
      closest = SNAP_ZONES[i];
    }
  }
  return closest.position;
}

export function computeBarPosition(
  overlay: RecipeTextOverlay,
  slotDuration: number,
): { leftPct: number; widthPct: number } {
  if (slotDuration <= 0) return { leftPct: 0, widthPct: 0 };
  const { start, end } = getEffectiveTiming(overlay);
  const clampedStart = Math.max(0, start);
  const clampedEnd = Math.min(slotDuration, end);
  return {
    leftPct: (clampedStart / slotDuration) * 100,
    widthPct: (Math.max(0, clampedEnd - clampedStart) / slotDuration) * 100,
  };
}

// Must match _is_subject_placeholder() in template_orchestrate.py:1368
// See also: Role-Aware Preview Resolution TODO in TODOS.md for known false-positive edge cases
export function isSubjectPlaceholder(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return false;
  // Match Python's str.isupper() semantics: requires at least one letter
  if (!/[a-zA-Z]/.test(trimmed)) return false;
  const words = trimmed.split(/\s+/);
  // ALL-CAPS up to 3 words: "PERU", "NEW YORK", "SAN JUAN PR"
  if (trimmed === trimmed.toUpperCase() && words.length <= 3) return true;
  // Title-cased 1-2 words: "Peru", "New York"
  if (words.length <= 2 && words.every(w => w[0] === w[0].toUpperCase())) return true;
  return false;
}

// Must match _resolve_overlay_text() in template_orchestrate.py:1390
export function resolveOverlayPreview(
  overlay: RecipeTextOverlay,
  previewSubject: string,
): string {
  const sample = overlay.sample_text || "";
  if (overlay.role === "cta") return "";
  if (previewSubject && isSubjectPlaceholder(sample)) {
    return sample === sample.toUpperCase()
      ? previewSubject.toUpperCase()
      : previewSubject;
  }
  return sample;
}

// ── Span helpers ────────────────────────────────────────────────────────────

/**
 * Resolve the CSS font-family for a single span.
 * Priority: span.font_family → overlay font_family → overlay font_style fallback.
 */
export function resolveSpanFont(span: TextSpan, overlay: RecipeTextOverlay): {
  family: string;
  weight: number;
  italic?: boolean;
} {
  // 1. Span-level font_family
  if (span.font_family) {
    const entry = FONT_REGISTRY[span.font_family];
    if (entry) {
      return { family: entry.css_family, weight: entry.weight };
    }
  }
  // 2. Overlay-level resolution
  return getFontCssFamily(overlay);
}

/**
 * Resolve the text color for a span, falling back to overlay color.
 */
export function resolveSpanColor(span: TextSpan, overlay: RecipeTextOverlay): string {
  return span.text_color || overlay.text_color || "#FFFFFF";
}

/**
 * Resolve the font size (px) for a span, falling back to overlay size.
 */
export function resolveSpanSize(span: TextSpan, overlay: RecipeTextOverlay): number {
  const sizeKey = span.text_size || overlay.text_size || "medium";
  return FONT_SIZE_MAP[sizeKey] ?? FONT_SIZE_MAP.medium;
}
