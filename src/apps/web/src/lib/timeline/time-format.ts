/**
 * Canonical time-formatting helpers for the unified timeline.
 *
 * Re-exports the existing `formatMSS` from lib/format-time.ts and adds two
 * additional formatters whose implementations mirror app/generative/timeline-math.ts
 * (copied here to avoid importing the heavy beat-grid module).
 *
 * Migrate in-blast-radius inline duplicates (MediaOverlayEditor.tsx:27,
 * page.tsx:105) to these; the full ~12-copy sweep is deferred (TODOS.md).
 */

// Canonical m:ss formatter (already extracted from the many duplicates)
export { formatMSS } from "@/lib/format-time";

/**
 * `m:ss` for eyebrow timecodes — e.g. `0:04`.
 * Identical behaviour to `formatMSS`; kept as an alias for call-site clarity.
 */
export function formatTimecode(s: number): string {
  const total = Math.max(0, Math.floor(s));
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

/**
 * `"2.3s"` — compact derived-seconds chip label.
 * Mirrors `formatSeconds` in timeline-math.ts.
 */
export function formatSeconds(s: number): string {
  return `${(Math.round(s * 10) / 10).toFixed(1)}s`;
}
