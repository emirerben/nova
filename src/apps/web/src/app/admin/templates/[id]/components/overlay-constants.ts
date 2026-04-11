import type { RecipeTextOverlay, OverlayPosition, OverlayRole } from "./recipe-types";

// ── Backend-to-frontend mapping ─────────────────────────────────────────────
// Must match src/apps/api/app/pipeline/text_overlay.py constants

export const CANVAS_W = 1080;
export const PREVIEW_W = 280;
export const SCALE = PREVIEW_W / CANVAS_W; // 0.259

export const POSITION_Y_MAP: Record<OverlayPosition, number> = {
  top: 0.15,
  center: 0.50,
  bottom: 0.85,
};

export const FONT_SIZE_MAP: Record<string, number> = {
  small: 48,
  medium: 72,
  large: 120,
  xlarge: 150,
};

export const FONT_FAMILY_MAP: Record<string, { family: string; italic?: boolean }> = {
  display: { family: "'Playfair Display', serif" },
  sans: { family: "'Montserrat', sans-serif" },
  serif: { family: "'Playfair Display', serif" },
  serif_italic: { family: "'Playfair Display', serif", italic: true },
  script: { family: "cursive" },
};

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
  { position: "center", y: POSITION_Y_MAP.center },
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
