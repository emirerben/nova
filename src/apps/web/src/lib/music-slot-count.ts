/**
 * Byte-for-byte TS mirror of `count_slots` in
 * `src/apps/api/app/pipeline/music_recipe.py`. Used by the admin Timing
 * config form (live "would produce N slots" badge + Save disable) and by
 * AudioPlayer section bands (incompatible-band warning) so the user sees
 * the same slot-count verdict the backend PATCH validator would return.
 *
 * Parity with the Python is pinned by
 * `src/apps/web/src/__tests__/lib/music-slot-count.test.ts`, which replays
 * the same fixtures used in `tests/pipeline/test_music_recipe.py`.
 *
 * The Python uses `len(range(0, L - n, n))` for `L > n`, which equals
 * `floor((L - n - 1) / n) + 1`. Both expressions are tested side-by-side
 * in the parity suite.
 */
export function countSlotsClient(
  beats: number[],
  startS: number,
  endS: number,
  n: number,
): number {
  // Defense-in-depth: Python's `range(_, _, 0)` raises ValueError, and a
  // negative or non-finite n would render "Would produce Infinity slots"
  // in the live badge with Save enabled. Treat any pathological n as
  // 0-slot so the form stays correctly disabled. NaN bounds (parseFloat
  // of cleared inputs) make every comparison false → window is empty →
  // returns 0 naturally.
  if (!Number.isFinite(n) || n < 1) return 0;
  const windowBeats = beats.filter((b) => startS <= b && b <= endS);
  if (windowBeats.length <= n) return 0;
  return Math.floor((windowBeats.length - n - 1) / n) + 1;
}
