import type { TimelineClip } from "@/lib/generative-api";
import type { DraftSlot } from "@/app/generative/timeline-math";

const EPSILON = 1e-6;

export interface VirtualTimelineEntry {
  slotIndex: number;
  slotKey: string;
  clipIndex: number;
  startS: number;
  durationS: number;
  inS: number;
  sourceUrl: string | null;
}

export interface VirtualTimeline {
  entries: VirtualTimelineEntry[];
  totalDurationS: number;
  hasMissingSource: boolean;
}

export interface VirtualTimeMapping {
  entry: VirtualTimelineEntry;
  entryIndex: number;
  virtualTimeS: number;
  localOffsetS: number;
  sourceTimeS: number;
}

function durationForSlot(slot: DraftSlot, grid: number[], offsetBeats: number): number {
  if (slot.removed) return 0;
  if (grid.length > 0 && slot.durationBeats != null) {
    const from = grid[Math.min(offsetBeats, grid.length - 1)] ?? 0;
    const to = grid[Math.min(offsetBeats + slot.durationBeats, grid.length - 1)] ?? from;
    return Math.max(0, to - from);
  }
  return Math.max(0, slot.durationS ?? 0);
}

export function buildVirtualTimeline(
  slots: DraftSlot[],
  clips: Pick<TimelineClip, "clip_index" | "signed_url">[],
  grid: number[] = [],
): VirtualTimeline {
  const clipUrlByIndex = new Map(clips.map((clip) => [clip.clip_index, clip.signed_url]));
  const entries: VirtualTimelineEntry[] = [];
  let startS = 0;
  let offsetBeats = 0;

  slots.forEach((slot, slotIndex) => {
    const durationS = durationForSlot(slot, grid, offsetBeats);
    if (slot.removed || durationS <= 0) {
      return;
    }
    entries.push({
      slotIndex,
      slotKey: slot.key,
      clipIndex: slot.clipIndex,
      startS,
      durationS,
      inS: Math.max(0, slot.inS),
      sourceUrl: clipUrlByIndex.get(slot.clipIndex) ?? null,
    });
    startS += durationS;
    if (grid.length > 0 && slot.durationBeats != null) {
      offsetBeats += slot.durationBeats;
    }
  });

  return {
    entries,
    totalDurationS: startS,
    hasMissingSource: entries.some((entry) => !entry.sourceUrl),
  };
}

export function mapVirtualTime(
  timeline: VirtualTimeline,
  timeS: number,
): VirtualTimeMapping | null {
  if (timeline.entries.length === 0 || timeline.totalDurationS <= 0) return null;

  const virtualTimeS = Math.max(0, Math.min(timeline.totalDurationS, timeS));
  const endIndex = timeline.entries.length - 1;

  for (let i = 0; i < timeline.entries.length; i += 1) {
    const entry = timeline.entries[i];
    const endS = entry.startS + entry.durationS;
    const contains =
      virtualTimeS >= entry.startS - EPSILON &&
      (virtualTimeS < endS - EPSILON || (i === endIndex && virtualTimeS <= endS + EPSILON));
    if (!contains) continue;

    const localOffsetS = Math.max(0, Math.min(entry.durationS, virtualTimeS - entry.startS));
    return {
      entry,
      entryIndex: i,
      virtualTimeS,
      localOffsetS,
      sourceTimeS: entry.inS + localOffsetS,
    };
  }

  const last = timeline.entries[endIndex];
  return {
    entry: last,
    entryIndex: endIndex,
    virtualTimeS: timeline.totalDurationS,
    localOffsetS: last.durationS,
    sourceTimeS: last.inS + last.durationS,
  };
}

export function nextVirtualEntry(
  timeline: VirtualTimeline,
  entryIndex: number,
): VirtualTimelineEntry | null {
  return timeline.entries[entryIndex + 1] ?? null;
}

export function slotsDifferFromBaseline(
  baseline: DraftSlot[],
  slots: DraftSlot[],
): boolean {
  if (baseline.length !== slots.length) return true;
  for (let i = 0; i < slots.length; i += 1) {
    const a = baseline[i];
    const b = slots[i];
    if (
      a.key !== b.key ||
      a.slotId !== b.slotId ||
      a.clipIndex !== b.clipIndex ||
      Math.abs(a.inS - b.inS) > EPSILON ||
      Math.abs((a.durationS ?? 0) - (b.durationS ?? 0)) > EPSILON ||
      a.durationBeats !== b.durationBeats ||
      a.removed !== b.removed
    ) {
      return true;
    }
  }
  return false;
}
