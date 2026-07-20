/**
 * slot-split — pure clip-slot split/delete math for the editor-shell Video
 * lane (plan §7). Operates on the shell's LOCAL timeline-slots working state
 * (DraftSlot[], the same shape useClipTimeline produces), never mutating the
 * clip-handle reducer directly. Persisted via editor-commit `timeline_slots`.
 *
 * Pure (no DOM, no React) — unit-tested in __tests__/slot-split.test.ts
 * (in_s/duration split math, ≥1-slot floor).
 *
 * Split is seconds-mode only: a beats-gridded slot (durationBeats != null)
 * can't be cut at an arbitrary second without redefining the beat grid, so we
 * refuse it (returns the input unchanged) — an honest no-op the caller gates
 * behind `variant.editor_capabilities?.split_clips`.
 */

import { slotWindows, type DraftSlot } from "@/app/generative/timeline-math";

/** Smallest half a split may produce (seconds). Matches editor input precision. */
export const MIN_SLOT_SPLIT_S = 0.1;

export interface SlotSplitResult {
  slots: DraftSlot[];
  /** True when the split actually happened. */
  didSplit: boolean;
}

export interface SlotDeleteResult {
  slots: DraftSlot[];
  /** False when refused because it would empty the cut (≥1-slot floor). */
  didDelete: boolean;
}

/** Count of slots still contributing footage (drives the ≥1 floor). */
export function activeSlotCount(slots: DraftSlot[]): number {
  return slots.filter((s) => !s.removed).length;
}

/**
 * Split the slot with `key` at assembled-time `atS`, producing two slots from
 * the same source: [in_s, offset] and [in_s + offset, duration]. `newKey` is
 * the client identity for the trailing half. No-op (didSplit: false) when the
 * slot is missing, beats-gridded, removed, or the cut leaves either half
 * below MIN_SLOT_SPLIT_S.
 */
export function splitSlotAt(
  slots: DraftSlot[],
  grid: number[],
  key: string,
  atS: number,
  newKey: string,
): SlotSplitResult {
  const idx = slots.findIndex((s) => s.key === key);
  if (idx === -1) return { slots, didSplit: false };
  const slot = slots[idx];
  if (slot.removed || slot.durationBeats != null) {
    return { slots, didSplit: false };
  }

  const windows = slotWindows(slots, grid);
  const win = windows[idx];
  if (win.startS == null || win.durationS <= 0) return { slots, didSplit: false };

  const localOffset = Math.round((atS - win.startS) * 10) / 10;
  const leftDur = localOffset;
  const rightDur = Math.round((win.durationS - localOffset) * 10) / 10;
  if (leftDur < MIN_SLOT_SPLIT_S || rightDur < MIN_SLOT_SPLIT_S) {
    return { slots, didSplit: false };
  }

  const left: DraftSlot = { ...slot, durationS: leftDur, durationBeats: null };
  const right: DraftSlot = {
    ...slot,
    key: newKey,
    slotId: null, // trailing half is a new (server-unknown) slot
    inS: slot.inS + leftDur,
    durationS: rightDur,
    durationBeats: null,
  };

  const next = slots.flatMap((s, i) => (i === idx ? [left, right] : [s]));
  return { slots: next, didSplit: true };
}

/**
 * Mark the slot `key` removed (soft-delete → `removed: true`, the
 * timeline-override contract). Enforces the ≥1-slot floor: refuses to remove
 * the last active slot (didDelete: false → the caller shows the quiet toast).
 */
export function deleteSlotEnforceFloor(
  slots: DraftSlot[],
  key: string,
): SlotDeleteResult {
  const target = slots.find((s) => s.key === key);
  if (!target || target.removed) return { slots, didDelete: false };
  if (activeSlotCount(slots) <= 1) return { slots, didDelete: false };
  const next = slots.map((s) => (s.key === key ? { ...s, removed: true } : s));
  return { slots: next, didDelete: true };
}
