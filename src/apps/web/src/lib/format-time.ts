/**
 * Time-formatting helpers shared across admin surfaces.
 *
 * Historically the codebase had ~7 near-duplicate seconds→m:ss helpers across
 * `admin/music/[id]/page.tsx`, `admin/music/[id]/components/LyricsTab.tsx`,
 * `admin/music/[id]/components/TestTab.tsx`, `admin/jobs/page.tsx`,
 * `admin/jobs/[id]/Timeline.tsx`, etc. — each with slightly different
 * precision and clamping. This module is the canonical version for new
 * call sites; legacy duplicates can be migrated in a separate sweep.
 */

/**
 * Format a non-negative number of seconds as `m:ss`. Negative inputs clamp to
 * `0:00`. Floors the seconds component so `59.9 → '0:59'` (not `'1:00'`).
 * Non-finite inputs (NaN, ±Infinity) render as `'0:00'` rather than the
 * default `'NaN:NaN'` — defense in depth against a backend that ever ships
 * non-finite floats inside a JSON payload (the Python side rejects them at
 * `_first_line_start_s`, but a future LLM-based lyric extractor or a stale
 * JSONB row could still leak one through).
 *
 * Examples: 0 → "0:00", 28.80 → "0:28", 48.80 → "0:48", 60 → "1:00",
 *           NaN → "0:00", Infinity → "0:00".
 */
export function formatMSS(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds)) return "0:00";
  const safe = Math.max(0, totalSeconds);
  const minutes = Math.floor(safe / 60);
  const seconds = Math.floor(safe % 60);
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}
