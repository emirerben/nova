import type { EditorTransition, LookPreset, TimelineClip } from "@/lib/generative-api";
import { slotWindows, type DraftSlot } from "@/app/generative/timeline-math";

const EPSILON = 1e-6;

export interface VirtualTimelineEntry {
  slotIndex: number;
  slotKey: string;
  clipIndex: number;
  startS: number;
  durationS: number;
  inS: number;
  sourceUrl: string | null;
  transitionAfter: EditorTransition;
  transitionDurationS: number | null;
  overlapBeforeS: number;
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

export interface VirtualTransitionPreview {
  kind: Exclude<EditorTransition, "cut">;
  durationS: number;
  progress: number;
}

export function virtualDeckLookPresetsAtTime(
  timeline: VirtualTimeline,
  slots: DraftSlot[],
  timeS: number,
  activeDeck: "a" | "b",
): Record<"a" | "b", LookPreset> {
  const result: Record<"a" | "b", LookPreset> = { a: "none", b: "none" };
  const mapping = mapVirtualTime(timeline, timeS);
  if (!mapping) return result;

  result[activeDeck] = slots[mapping.entry.slotIndex]?.lookPreset ?? "none";
  if (transitionPreviewAtTime(timeline, timeS)) {
    const incoming = timeline.entries[mapping.entryIndex + 1];
    if (incoming) {
      result[activeDeck === "a" ? "b" : "a"] =
        slots[incoming.slotIndex]?.lookPreset ?? "none";
    }
  }
  return result;
}

export function mapVirtualTimeToMusicTime(
  virtualTimeS: number,
  sectionStartS: number,
): number {
  return Math.max(0, sectionStartS) + Math.max(0, virtualTimeS);
}

export function buildVirtualTimeline(
  slots: DraftSlot[],
  clips: Pick<TimelineClip, "clip_index" | "signed_url">[],
  grid: number[] = [],
): VirtualTimeline {
  const clipUrlByIndex = new Map(clips.map((clip) => [clip.clip_index, clip.signed_url]));
  const windows = slotWindows(slots, grid);
  const entries: VirtualTimelineEntry[] = [];

  slots.forEach((slot, slotIndex) => {
    const window = windows[slotIndex];
    if (slot.removed || !window || window.startS == null || window.durationS <= 0) {
      return;
    }
    const previous = entries.at(-1);
    const overlapBeforeS = previous
      ? Math.round(
          Math.max(0, previous.startS + previous.durationS - window.startS) * 1000,
        ) / 1000
      : 0;
    entries.push({
      slotIndex,
      slotKey: slot.key,
      clipIndex: slot.clipIndex,
      startS: window.startS,
      durationS: window.durationS,
      inS: Math.max(0, slot.inS),
      sourceUrl: clipUrlByIndex.get(slot.clipIndex) ?? null,
      transitionAfter: slot.transitionAfter ?? "cut",
      transitionDurationS: slot.transitionDurationS ?? null,
      overlapBeforeS,
    });
  });
  const last = entries.at(-1);

  return {
    entries,
    totalDurationS: last ? last.startS + last.durationS : 0,
    hasMissingSource: entries.some((entry) => !entry.sourceUrl),
  };
}

/**
 * Preview the same render-safe boundary contract used by FFmpeg: at most
 * 300ms and at most 30% of either adjacent active clip. The next deck is
 * already preloaded, so the canvas can blend it under the outgoing deck.
 */
export function transitionPreviewAtTime(
  timeline: VirtualTimeline,
  timeS: number,
): VirtualTransitionPreview | null {
  for (let index = 0; index < timeline.entries.length - 1; index += 1) {
    const entry = timeline.entries[index];
    const next = timeline.entries[index + 1];
    if (entry.transitionAfter === "cut") continue;
    const durationS = Math.min(
      0.3,
      entry.transitionDurationS ?? 0.3,
      entry.durationS * 0.3,
      next.durationS * 0.3,
    );
    if (durationS < 0.1) continue;
    const boundaryS = entry.startS + entry.durationS;
    const startS = next.startS;
    if (timeS < startS || timeS >= boundaryS) continue;
    return {
      kind: entry.transitionAfter,
      durationS,
      progress: Math.max(0, Math.min(1, (timeS - startS) / durationS)),
    };
  }
  return null;
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
      a.removed !== b.removed ||
      (a.lookPreset ?? "none") !== (b.lookPreset ?? "none") ||
      (a.transitionAfter ?? "cut") !== (b.transitionAfter ?? "cut") ||
      Math.abs((a.transitionDurationS ?? 0) - (b.transitionDurationS ?? 0)) > EPSILON
    ) {
      return true;
    }
  }
  return false;
}
