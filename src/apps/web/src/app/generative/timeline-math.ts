/**
 * Pure beat-walk / layout math for the clip-timeline editor.
 * NO DOM, NO React — fully Jest-testable without jsdom.
 *
 * Beat math contract (matches the backend): the beat grid is NON-uniform.
 * Slot i's seconds = grid[offset + duration_beats] - grid[offset], where
 * offset is the cumulative beats of all PRIOR non-removed beats-bearing
 * slots. Slots with duration_beats null (no-grid variants, or footage-trimmed
 * slots on grid variants) use duration_s directly and never advance the
 * offset. Nudges step 0.5s with a 0.6s floor; total ≤ 60s.
 */

import type { TimelineResponse, TimelineSlot } from "@/lib/generative-api";

// ── No-grid (original_text) constraints ──────────────────────────────────────
export const SECONDS_STEP = 0.5;
export const SECONDS_FLOOR = 0.6;
export const MAX_TOTAL_SECONDS = 60;

/** Client-side draft slot. `key` is a stable client identity that survives
 * reorder/undo (server slot_id, or a generated key for added slots). */
export interface DraftSlot {
  key: string;
  slotId: string | null;
  clipIndex: number;
  inS: number;
  durationBeats: number | null;
  durationS: number | null;
  removed: boolean;
  /** Why the AI picked this moment (null for user-added slots). */
  momentDescription: string | null;
}

let addCounter = 0;
export function nextAddedKey(): string {
  addCounter += 1;
  return `added-${addCounter}`;
}

export function draftFromTimeline(timeline: TimelineResponse): DraftSlot[] {
  return [...timeline.slots]
    .sort((a, b) => a.order - b.order)
    .map((s: TimelineSlot) => ({
      key: s.slot_id,
      slotId: s.slot_id,
      clipIndex: s.clip_index,
      inS: s.in_s,
      durationBeats: s.duration_beats,
      durationS: s.duration_s,
      removed: s.removed ?? false,
      momentDescription: s.moment_description,
    }));
}

// ── Beat walk ─────────────────────────────────────────────────────────────────

export interface SlotWindow {
  /** Start of the slot in the OUTPUT timeline (null for removed slots). */
  startS: number | null;
  /** Derived seconds this slot occupies (0 for removed slots). */
  durationS: number;
  /** Beat offset into the grid where this slot starts (null in no-grid mode / removed). */
  offsetBeats: number | null;
}

/**
 * Walk the (non-uniform) beat grid across the non-removed slots, in order.
 * Returns one window per slot, aligned by index with the input array.
 * No-grid mode (empty grid): durations come straight from durationS.
 * Null-beats slots on a GRID timeline (footage-trimmed AI slots) also use
 * durationS directly and do NOT advance the grid offset — mirrors the server
 * walk in dispatch_edit_timeline.
 */
export function slotWindows(slots: DraftSlot[], grid: number[]): SlotWindow[] {
  const out: SlotWindow[] = [];
  let startS = 0;
  let offset = 0;
  const hasGrid = grid.length > 0;
  for (const slot of slots) {
    if (slot.removed) {
      out.push({ startS: null, durationS: 0, offsetBeats: null });
      continue;
    }
    if (hasGrid && slot.durationBeats != null) {
      const beats = slot.durationBeats;
      const from = grid[Math.min(offset, grid.length - 1)];
      const to = grid[Math.min(offset + beats, grid.length - 1)];
      const dur = Math.max(0, to - from);
      out.push({ startS, durationS: dur, offsetBeats: offset });
      startS += dur;
      offset += beats;
    } else {
      const dur = slot.durationS ?? 0;
      out.push({ startS, durationS: dur, offsetBeats: null });
      startS += dur;
    }
  }
  return out;
}

/** Total seconds of the current (non-removed) cut. */
export function totalDurationS(slots: DraftSlot[], grid: number[]): number {
  const windows = slotWindows(slots, grid);
  return windows.reduce((acc, w) => acc + w.durationS, 0);
}

/** Total beats consumed by non-removed slots. */
export function totalBeats(slots: DraftSlot[]): number {
  return slots.reduce(
    (acc, s) => acc + (s.removed ? 0 : (s.durationBeats ?? 0)),
    0,
  );
}

/** Max beats the grid can serve: grid[i] indices must exist, so the walk may
 * consume at most grid.length - 1 intervals. */
export function maxGridBeats(grid: number[]): number {
  return Math.max(0, grid.length - 1);
}

/** Beats still unclaimed by the current draft (grid mode). */
export function remainingBeats(slots: DraftSlot[], grid: number[]): number {
  return Math.max(0, maxGridBeats(grid) - totalBeats(slots));
}

/** Clamp an in-point so the [inS, inS + windowS] window fits the source clip. */
export function clampInPoint(
  inS: number,
  windowS: number,
  sourceDurationS: number | null,
): number {
  if (sourceDurationS == null) return Math.max(0, inS);
  return Math.min(Math.max(0, inS), Math.max(0, sourceDurationS - windowS));
}

/** Default duration for an added slot: median of the current non-removed slots.
 * Returns beats in grid mode, seconds otherwise. */
export function medianDefaultDuration(
  slots: DraftSlot[],
  grid: number[],
): { durationBeats: number | null; durationS: number | null } {
  const live = slots.filter((s) => !s.removed);
  if (grid.length > 0) {
    const beats = live
      .map((s) => s.durationBeats ?? 0)
      .filter((b) => b > 0)
      .sort((a, b) => a - b);
    const median = beats.length > 0 ? beats[Math.floor(beats.length / 2)] : 2;
    return { durationBeats: median, durationS: null };
  }
  const secs = live
    .map((s) => s.durationS ?? 0)
    .filter((d) => d > 0)
    .sort((a, b) => a - b);
  const median = secs.length > 0 ? secs[Math.floor(secs.length / 2)] : 2;
  return { durationBeats: null, durationS: Math.max(SECONDS_FLOOR, median) };
}

// ── Edit counting (CTA label "Re-render N edits") ────────────────────────────

export function fieldsDiffer(a: DraftSlot, b: DraftSlot): boolean {
  return (
    a.clipIndex !== b.clipIndex ||
    Math.abs(a.inS - b.inS) > 1e-6 ||
    a.durationBeats !== b.durationBeats ||
    (a.durationBeats == null &&
      Math.abs((a.durationS ?? 0) - (b.durationS ?? 0)) > 1e-6) ||
    a.removed !== b.removed
  );
}

/** LCS length over two key sequences — moved slots = shared - LCS. */
function lcsLength(a: string[], b: string[]): number {
  const dp: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] =
        a[i - 1] === b[j - 1]
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  return dp[a.length][b.length];
}

/**
 * Count edits vs the server baseline:
 * - +1 per added slot (key not in baseline)
 * - +1 per slot whose fields changed (trim, swap, remove/restore)
 * - +1 per MOVED slot (minimal move count via LCS over shared keys)
 * A slot that both moved and changed counts twice — both are user actions.
 */
export function countEdits(baseline: DraftSlot[], slots: DraftSlot[]): number {
  const baseByKey = new Map(baseline.map((s) => [s.key, s]));
  const draftKeys = new Set(slots.map((s) => s.key));
  let edits = 0;
  for (const slot of slots) {
    const base = baseByKey.get(slot.key);
    if (!base) {
      edits += 1; // added
    } else if (fieldsDiffer(base, slot)) {
      edits += 1;
    }
  }
  const baseShared = baseline.map((s) => s.key).filter((k) => draftKeys.has(k));
  const draftShared = slots.map((s) => s.key).filter((k) => baseByKey.has(k));
  edits += baseShared.length - lcsLength(baseShared, draftShared);
  return edits;
}

/**
 * Beat-snap for the right-edge drag on grid slots.
 * Returns the beat count k ∈ [1, min(maxK, maxGridBeats−offsetBeats)] whose
 * grid window length best matches targetWindowS (= pointer source-time − inS).
 * Linear scan; grid is typically < 64 entries.
 */
export function beatsForWindowSeconds(
  grid: number[],
  offsetBeats: number,
  targetWindowS: number,
  maxK: number,
): number {
  const limit = Math.min(maxK, maxGridBeats(grid) - offsetBeats);
  if (limit < 1) return 1;
  const base = grid[Math.min(offsetBeats, grid.length - 1)];
  let best = 1;
  let bestDiff = Infinity;
  for (let k = 1; k <= limit; k++) {
    const windowS = grid[Math.min(offsetBeats + k, grid.length - 1)] - base;
    const diff = Math.abs(windowS - targetWindowS);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = k;
    }
  }
  return best;
}

// ── Formatting ────────────────────────────────────────────────────────────────

/** m:ss for eyebrow timecodes ("0:04"). */
export function formatTimecode(s: number): string {
  const total = Math.max(0, Math.floor(s));
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

/** m:ss.s for the in-point readout ("0:04.2"). */
export function formatInPoint(s: number): string {
  const clamped = Math.max(0, s);
  const m = Math.floor(clamped / 60);
  const rest = clamped - m * 60;
  const tenths = Math.floor(rest * 10) / 10;
  const whole = Math.floor(tenths);
  const frac = Math.round((tenths - whole) * 10);
  return `${m}:${String(whole).padStart(2, "0")}.${frac}`;
}

/** "2.3s" derived-seconds chip text. */
export function formatSeconds(s: number): string {
  return `${(Math.round(s * 10) / 10).toFixed(1)}s`;
}
