// Shared matcher for "is this section the active form window?" used by both
// the admin music page's top metadata Row (displays "Section #N" or "Custom
// window") AND the AudioPlayer's per-band ✓ + thicker stroke indicator.
//
// Single source of truth for the tolerance — if both surfaces compute their
// own version, they can drift and the user sees "✓ on band #2" while the top
// label says "Custom window", which is exactly the kind of UI lie this whole
// feature is trying to kill.

import type { SongSection } from "@/lib/music-api";

// Tolerance for matching form-state bounds against a section's bounds.
// A single beat at 120 BPM is 0.5s, so 0.5s is well below any musical
// boundary (two distinct sections never collide) but absorbs float drift
// introduced by string ↔ parseFloat round-trips through the form inputs.
export const SELECTED_TOLERANCE_S = 0.5;

/**
 * Returns the first section whose start_s / end_s match the given form-state
 * bounds within SELECTED_TOLERANCE_S. Returns undefined when no section
 * matches (the user has typed a custom window) or when `sections` is null.
 *
 * Pure function, no React, no audio — safe to call from any render path.
 */
export function matchSectionByBounds(
  sections: SongSection[] | null | undefined,
  start: number,
  end: number,
): SongSection | undefined {
  if (!sections || sections.length === 0) return undefined;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return undefined;
  return sections.find(
    (s) =>
      Math.abs(s.start_s - start) < SELECTED_TOLERANCE_S &&
      Math.abs(s.end_s - end) < SELECTED_TOLERANCE_S,
  );
}
