import {
  mapVirtualTime,
  type VirtualTimeline,
  type VirtualTimelineEntry,
} from "@/app/plan/items/[id]/_editor/virtual-timeline";

export interface MobilePreviewSegment {
  id: string;
  startS: number;
  endS: number;
  sourceStartS: number;
  sourceUrl: string | null;
}

export function buildMobilePreviewTimeline(
  segments: MobilePreviewSegment[],
): VirtualTimeline {
  const entries: VirtualTimelineEntry[] = segments.map((segment, index) => ({
    kind: "clip",
    slotIndex: index,
    slotKey: segment.id,
    clipIndex: index,
    startS: segment.startS,
    durationS: Math.max(0, segment.endS - segment.startS),
    inS: segment.sourceStartS,
    sourceUrl: segment.sourceUrl,
    transitionAfter: "cut",
    transitionDurationS: null,
    overlapBeforeS: 0,
  }));
  return {
    entries,
    totalDurationS: entries.at(-1)
      ? entries.at(-1)!.startS + entries.at(-1)!.durationS
      : 0,
    hasMissingSource: entries.some((entry) => !entry.sourceUrl),
    carouselProjection: null,
  };
}

export function mobilePreviewSourceAtOutput(
  timeline: VirtualTimeline,
  outputTimeS: number,
): { entryIndex: number; sourceTimeS: number } | null {
  const mapping = mapVirtualTime(timeline, outputTimeS);
  if (!mapping || mapping.entry.kind !== "clip" || mapping.sourceTimeS == null) {
    return null;
  }
  return {
    entryIndex: mapping.entryIndex,
    sourceTimeS: mapping.sourceTimeS,
  };
}

export function mobilePreviewOutputAtSource(
  timeline: VirtualTimeline,
  entryIndex: number,
  sourceTimeS: number,
): { outputTimeS: number; reachedEnd: boolean } | null {
  const entry = timeline.entries[entryIndex];
  if (!entry || entry.kind !== "clip") return null;
  const localOffsetS = Math.max(0, sourceTimeS - entry.inS);
  return {
    outputTimeS: Math.min(
      timeline.totalDurationS,
      entry.startS + Math.min(entry.durationS, localOffsetS),
    ),
    reachedEnd: localOffsetS >= entry.durationS - 1 / 120,
  };
}
