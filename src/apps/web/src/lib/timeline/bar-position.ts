/**
 * Bar-position math: converts a time window [startS, endS] within a total
 * duration to percentage CSS values (`left`, `width`) suitable for an
 * absolutely-positioned element inside a relative timeline container.
 *
 * This is the canonical version of the inline `lPct / wPct` math currently
 * duplicated across MediaOverlayEditor.tsx, TimelineEditor.tsx, and
 * admin/templates/[id]/components/overlay-constants.ts.
 */

/** Minimum visual width so a narrow bar stays clickable/visible. */
const MIN_WIDTH_PCT = 1;

/**
 * Convert [startS, endS] within totalS to CSS left/width percentages.
 *
 * - Clamps start to [0, totalS].
 * - Clamps end  to [start, totalS].
 * - Applies MIN_WIDTH_PCT floor so zero-duration bars are still visible.
 * - Returns { leftPct: 0, widthPct: 100 } when totalS ≤ 0 (safe fallback).
 */
export function computeBarPosition(
  startS: number,
  endS: number,
  totalS: number,
): { leftPct: number; widthPct: number } {
  if (totalS <= 0) return { leftPct: 0, widthPct: 100 };
  const s = Math.max(0, Math.min(startS, totalS));
  const e = Math.max(s, Math.min(endS, totalS));
  const leftPct = (s / totalS) * 100;
  const rawWidth = ((e - s) / totalS) * 100;
  return { leftPct, widthPct: Math.max(MIN_WIDTH_PCT, rawWidth) };
}
