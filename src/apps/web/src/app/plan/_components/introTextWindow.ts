/**
 * introTextWindow — the ONE place that derives the intro-text keep-out window
 * from a variant's text elements (plan 009 T5 page wiring).
 *
 * Consumed by page.tsx to feed UnifiedTimeline's `introTextWindow` prop (the
 * hatched zinc band in the Overlays lane + the "Covers your intro text"
 * fullscreen warning). The timeline never derives its own copy — this helper
 * is the single upstream source, unit-tested in isolation.
 *
 * Rule: the window is the FIRST element's {start_s, end_s} unioned with any
 * other element that STARTS before INTRO_CUTOFF_S (i.e. min start → max end
 * across that set). Later elements (sequence scenes deep in the video) never
 * widen it. Null when the variant has no text elements.
 */

export const INTRO_CUTOFF_S = 4;

export function computeIntroTextWindow(
  elements: Array<{ start_s: number; end_s: number }> | null | undefined,
): { start_s: number; end_s: number } | null {
  if (!elements || elements.length === 0) return null;
  const pool = [
    elements[0],
    ...elements.slice(1).filter((el) => el.start_s < INTRO_CUTOFF_S),
  ];
  return {
    start_s: Math.min(...pool.map((el) => el.start_s)),
    end_s: Math.max(...pool.map((el) => el.end_s)),
  };
}
