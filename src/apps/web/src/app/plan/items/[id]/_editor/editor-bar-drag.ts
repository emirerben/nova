import { slotWindows, type DraftSlot, type SlotWindow } from "@/app/generative/timeline-math";
import {
  renderedSlotLayout,
  type RenderedSlotLayout,
  type RenderedSlotLayoutOptions,
} from "@/lib/timeline/transition-overlap";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

export type BarDragHandle = "left" | "right" | "body";

export const BAR_EDGE_HIT_PX = 24;
export const CLICK_DRAG_THRESHOLD_PX = 3;
export const TEXT_MIN_DURATION_S = 0.3;
export const CLIP_MIN_DURATION_S = 0.1;
/** Shared media/timeline resolution. Keep this in one place so pointer drags,
 * direct timing controls, and adjacent placement agree on the same contract. */
export const TIMELINE_MIN_DURATION_S = 0.1;
export const TIMELINE_TIME_STEP_S = 0.1;

const EPSILON = 1e-6;

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

export function roundTiming(value: number): number {
  return Math.round(value * 1000) / 1000;
}

export function snapTimelineTime(
  value: number,
  stepS = TIMELINE_TIME_STEP_S,
): number {
  if (!Number.isFinite(value) || !Number.isFinite(stepS) || stepS <= 0) {
    return roundTiming(value);
  }
  return roundTiming(Math.round(value / stepS) * stepS);
}

export interface TimelineBarRange {
  start_s: number;
  end_s: number;
}

export function selectedMediaSourceDuration(
  sourceDurationS: number | null | undefined,
  trimStartS: number | null | undefined,
  trimEndS: number | null | undefined,
): number | null {
  if (sourceDurationS == null || !Number.isFinite(sourceDurationS)) return null;
  const sourceEnd = Math.min(sourceDurationS, trimEndS ?? sourceDurationS);
  return Math.max(TIMELINE_MIN_DURATION_S, sourceEnd - (trimStartS ?? 0));
}

/** Pure range math shared by overlay and visual-media timeline rows. Source
 * duration is a duration budget (not an output timestamp), so a right trim
 * cannot silently grow beyond the file. The final clamp happens after snap,
 * which makes a source/video boundary exact instead of snapping back later. */
export function applyTimelineBarDrag({
  bar,
  handle,
  deltaS,
  videoDurationS,
  sourceDurationS,
  minDurationS = TIMELINE_MIN_DURATION_S,
  stepS = TIMELINE_TIME_STEP_S,
}: {
  bar: TimelineBarRange;
  handle: BarDragHandle;
  deltaS: number;
  videoDurationS: number;
  sourceDurationS?: number | null;
  minDurationS?: number;
  stepS?: number;
}): TimelineBarRange {
  const min = Math.max(0.001, minDurationS);
  const videoEnd = Math.max(min, videoDurationS);
  const sourceBudget =
    sourceDurationS != null && Number.isFinite(sourceDurationS)
      ? Math.max(min, sourceDurationS)
      : Number.POSITIVE_INFINITY;
  const duration = Math.max(min, Math.min(bar.end_s - bar.start_s, sourceBudget));
  const snap = (value: number) => snapTimelineTime(value, stepS);

  if (handle === "body") {
    // Source duration limits the media window length, not where that window
    // may be placed on the project timeline.
    const maxStart = Math.max(0, videoEnd - duration);
    const start = clamp(snap(bar.start_s + deltaS), 0, maxStart);
    return {
      start_s: roundTiming(start),
      end_s: roundTiming(Math.min(videoEnd, start + duration)),
    };
  }

  if (handle === "left") {
    const earliestStart = Math.max(0, bar.end_s - sourceBudget);
    const latestStart = Math.min(bar.end_s - min, videoEnd - min);
    const start = clamp(snap(bar.start_s + deltaS), earliestStart, latestStart);
    return {
      start_s: roundTiming(start),
      end_s: roundTiming(bar.end_s),
    };
  }

  const maxEnd = Math.min(videoEnd, bar.start_s + sourceBudget);
  const requestedEnd = snap(bar.end_s + deltaS);
  const end = clamp(requestedEnd, bar.start_s + min, maxEnd);
  return {
    start_s: roundTiming(bar.start_s),
    end_s: roundTiming(end),
  };
}

/** Place a new media item directly after the selected item. Returning null
 * when there is no selection keeps callers from inventing an ordering policy. */
export function placeAfterSelected({
  selected,
  durationS,
  videoDurationS,
  minDurationS = TIMELINE_MIN_DURATION_S,
}: {
  selected: Pick<TimelineBarRange, "end_s"> | null | undefined;
  durationS: number;
  videoDurationS?: number | null;
  minDurationS?: number;
}): TimelineBarRange | null {
  if (!selected) return null;
  const min = Math.max(0.001, minDurationS);
  // Preserve the selected endpoint exactly (to the storage precision). Only
  // the new item's own duration follows the 100ms step; rounding the start
  // would introduce a visible gap when the selected item has a non-grid end.
  const start = roundTiming(Math.max(0, selected.end_s));
  const requestedDuration = Math.max(min, snapTimelineTime(durationS));
  const projectEnd =
    videoDurationS == null || !Number.isFinite(videoDurationS)
      ? Number.POSITIVE_INFINITY
      : videoDurationS;
  if (projectEnd - start < min) return null;
  const end = Math.min(start + requestedDuration, projectEnd);
  return { start_s: start, end_s: roundTiming(end) };
}

export interface SequentialSlotLayout {
  windows: SlotWindow[];
  totalDurationS: number;
  sourceRangeKey: string;
}

export function sequentialSlotLayout(
  slots: DraftSlot[],
  grid: number[],
): SequentialSlotLayout {
  const baseWindows = slotWindows(slots, grid);
  const windows: SlotWindow[] = [];
  const rangeParts: string[] = [];
  slots.forEach((slot, index) => {
    const base = baseWindows[index] ?? {
      startS: null,
      durationS: 0,
      offsetBeats: null,
    };

    if (slot.removed || base.durationS <= 0) {
      windows.push({ ...base, startS: null, durationS: 0 });
      rangeParts.push(`${slot.key}:removed`);
      return;
    }

    const durationS = roundTiming(base.durationS);
    windows.push({
      startS: base.startS == null ? null : roundTiming(base.startS),
      durationS,
      offsetBeats: base.offsetBeats,
    });
    rangeParts.push(
      [
        slot.key,
        roundTiming(slot.inS),
        durationS,
        slot.durationBeats ?? "s",
        slot.transitionAfter ?? "cut",
        roundTiming(slot.transitionDurationS ?? 0),
      ].join(":"),
    );
  });

  const last = [...windows].reverse().find((window) => window.startS != null);
  return {
    windows,
    totalDurationS: last?.startS == null ? 0 : roundTiming(last.startS + last.durationS),
    sourceRangeKey: rangeParts.join("|"),
  };
}

export function renderedSequentialSlotLayout(
  slots: DraftSlot[],
  grid: number[],
  options: RenderedSlotLayoutOptions = {},
): RenderedSlotLayout & { sourceRangeKey: string } {
  const layout = renderedSlotLayout(slots, grid, options);
  const rangeParts = slots.map((slot, index) => {
    const win = layout.windows[index];
    if (!win || slot.removed || win.startS == null || win.durationS <= 0) {
      return `${slot.key}:removed`;
    }
    return [
      slot.key,
      roundTiming(slot.inS),
      roundTiming(win.durationS),
      slot.durationBeats ?? "s",
      roundTiming(win.overlapBeforeS),
    ].join(":");
  });
  return {
    ...layout,
    sourceRangeKey: rangeParts.join("|"),
  };
}

export function resolveBarDragHandle({
  localX,
  width,
  edgePx = BAR_EDGE_HIT_PX,
}: {
  localX: number;
  width: number;
  edgePx?: number;
}): BarDragHandle {
  const effectiveEdgePx = effectiveBarEdgeHitPx(width, edgePx);
  if (localX <= effectiveEdgePx) return "left";
  if (localX >= width - effectiveEdgePx) return "right";
  return "body";
}

export function effectiveBarEdgeHitPx(width: number, edgePx = BAR_EDGE_HIT_PX): number {
  return Math.min(edgePx, Math.max(1, width / 3));
}

export function timelineXFromClient({
  clientX,
  scrollRectLeft,
  scrollLeft,
}: {
  clientX: number;
  scrollRectLeft: number;
  scrollLeft: number;
}): number {
  return clientX - scrollRectLeft + scrollLeft;
}

export function secondsDeltaFromTimelineX({
  currentTimelineX,
  startTimelineX,
  pxPerSecond,
}: {
  currentTimelineX: number;
  startTimelineX: number;
  pxPerSecond: number;
}): number {
  return pxPerSecond > 0 ? (currentTimelineX - startTimelineX) / pxPerSecond : 0;
}

export function applyTextBarDrag({
  bar,
  handle,
  deltaS,
  videoDurationS,
  minDurationS = TEXT_MIN_DURATION_S,
}: {
  bar: Pick<TextElementBar, "start_s" | "end_s">;
  handle: BarDragHandle;
  deltaS: number;
  videoDurationS: number;
  minDurationS?: number;
}): Pick<TextElementBar, "start_s" | "end_s"> {
  const duration = Math.max(minDurationS, bar.end_s - bar.start_s);
  const maxEnd = Math.max(minDurationS, videoDurationS);

  if (handle === "body") {
    const maxStart = Math.max(0, maxEnd - duration);
    const start = clamp(bar.start_s + deltaS, 0, maxStart);
    return {
      start_s: roundTiming(start),
      end_s: roundTiming(start + duration),
    };
  }

  if (handle === "left") {
    const latestStart = Math.min(maxEnd - minDurationS, bar.end_s - minDurationS);
    return {
      start_s: roundTiming(clamp(bar.start_s + deltaS, 0, latestStart)),
      end_s: roundTiming(bar.end_s),
    };
  }

  return {
    start_s: roundTiming(bar.start_s),
    end_s: roundTiming(
      clamp(bar.end_s + deltaS, bar.start_s + minDurationS, maxEnd),
    ),
  };
}

export function applyTextTimingInput({
  startS,
  endS,
  videoDurationS,
  minDurationS = TEXT_MIN_DURATION_S,
}: {
  startS: number;
  endS: number;
  videoDurationS: number;
  minDurationS?: number;
}): Pick<TextElementBar, "start_s" | "end_s"> {
  const maxEnd = Math.max(minDurationS, videoDurationS);
  const start = clamp(startS, 0, Math.max(0, maxEnd - minDurationS));
  const end = clamp(endS, start + minDurationS, maxEnd);
  return { start_s: roundTiming(start), end_s: roundTiming(end) };
}

export function applyClipEdgeDrag({
  slot,
  handle,
  deltaS,
  sourceDurationS,
  minDurationS = CLIP_MIN_DURATION_S,
}: {
  slot: Pick<DraftSlot, "inS" | "durationS">;
  handle: "left" | "right";
  deltaS: number;
  sourceDurationS: number | null;
  minDurationS?: number;
}): Pick<DraftSlot, "inS" | "durationS" | "durationBeats"> {
  const startIn = Math.max(0, slot.inS);
  const startDuration = Math.max(minDurationS, slot.durationS ?? minDurationS);

  if (handle === "left") {
    const sourceOut = sourceDurationS == null
      ? startIn + startDuration
      : Math.min(sourceDurationS, startIn + startDuration);
    const nextIn = clamp(startIn + deltaS, 0, sourceOut - minDurationS);
    return {
      inS: roundTiming(nextIn),
      durationS: roundTiming(sourceOut - nextIn),
      durationBeats: null,
    };
  }

  const maxDuration =
    sourceDurationS == null
      ? Number.POSITIVE_INFINITY
      : Math.max(minDurationS, sourceDurationS - startIn);
  const nextDuration = clamp(
    startDuration + deltaS,
    minDurationS,
    maxDuration,
  );
  return {
    inS: roundTiming(startIn),
    durationS: roundTiming(nextDuration),
    durationBeats: null,
  };
}

export function applyClipSourceWindowDrag({
  slot,
  handle,
  deltaS,
  sourceDurationS,
  minDurationS = CLIP_MIN_DURATION_S,
}: {
  slot: Pick<DraftSlot, "inS" | "durationS">;
  handle: BarDragHandle;
  deltaS: number;
  sourceDurationS: number | null;
  minDurationS?: number;
}): Pick<DraftSlot, "inS" | "durationS" | "durationBeats"> {
  if (handle === "left" || handle === "right") {
    return applyClipEdgeDrag({
      slot,
      handle,
      deltaS,
      sourceDurationS,
      minDurationS,
    });
  }

  const startIn = Math.max(0, slot.inS);
  const duration = Math.max(minDurationS, slot.durationS ?? minDurationS);
  const maxIn =
    sourceDurationS == null
      ? Number.POSITIVE_INFINITY
      : Math.max(0, sourceDurationS - duration);

  return {
    inS: roundTiming(clamp(startIn + deltaS, 0, maxIn)),
    durationS: roundTiming(duration),
    durationBeats: null,
  };
}

export function minimumClipDurationForSlot({
  grid,
  offsetBeats,
  baseMinDurationS = CLIP_MIN_DURATION_S,
}: {
  grid: number[];
  offsetBeats: number | null | undefined;
  baseMinDurationS?: number;
}): number {
  if (!grid.length || offsetBeats == null || offsetBeats < 0) return baseMinDurationS;
  const start = grid[offsetBeats];
  const next = grid[offsetBeats + 1];
  if (start == null || next == null) return baseMinDurationS;
  return roundTiming(Math.max(Number.EPSILON, next - start));
}

export function applyClipTimingInput({
  inS,
  outS,
  durationS,
  sourceDurationS,
  minDurationS = CLIP_MIN_DURATION_S,
}: {
  inS: number;
  outS?: number;
  durationS?: number;
  sourceDurationS: number | null;
  minDurationS?: number;
}): Pick<DraftSlot, "inS" | "durationS" | "durationBeats"> {
  const maxSource = sourceDurationS ?? Number.POSITIVE_INFINITY;
  const nextIn = clamp(inS, 0, Math.max(0, maxSource - minDurationS));
  const requestedDuration =
    durationS ?? (outS == null ? minDurationS : outS - nextIn);
  const maxDuration = Math.max(minDurationS, maxSource - nextIn);
  return {
    inS: roundTiming(nextIn),
    durationS: roundTiming(clamp(requestedDuration, minDurationS, maxDuration)),
    durationBeats: null,
  };
}

/** Manual inspector semantics: changing In is a left trim (Out fixed), while
 * changing Duration alone keeps In fixed. Copilot's `set_clip_in` remains the
 * distinct source-window slip operation and does not call this helper. */
export function applyManualClipTimingPatch({
  inS,
  durationS,
  patch,
  sourceDurationS,
}: {
  inS: number;
  durationS: number;
  patch: { inS?: number; outS?: number; durationS?: number };
  sourceDurationS: number | null;
}): Pick<DraftSlot, "inS" | "durationS" | "durationBeats"> {
  const preservesCurrentOut = patch.inS !== undefined && patch.outS === undefined;
  return applyClipTimingInput({
    inS: patch.inS ?? inS,
    outS: patch.outS ?? (preservesCurrentOut ? inS + durationS : undefined),
    durationS:
      patch.durationS ??
      (patch.outS == null && !preservesCurrentOut ? durationS : undefined),
    sourceDurationS,
  });
}

export function applySfxMove({
  atS,
  endS,
  deltaS,
  videoDurationS,
}: {
  atS: number;
  endS?: number | null;
  deltaS: number;
  videoDurationS: number;
}): { at_s: number; end_s?: number | null } {
  const duration = Math.max(0, (endS ?? atS + 0.6) - atS);
  const maxStart = Math.max(0, videoDurationS - duration);
  const nextStart = clamp(atS + deltaS, 0, maxStart);
  return {
    at_s: roundTiming(nextStart),
    end_s: endS == null ? endS : roundTiming(nextStart + duration),
  };
}

export function applySfxBarDrag({
  bar,
  handle,
  deltaS,
  videoDurationS,
  minDurationS = TEXT_MIN_DURATION_S,
}: {
  bar: { at_s: number; end_s?: number | null };
  handle: BarDragHandle;
  deltaS: number;
  videoDurationS: number;
  minDurationS?: number;
}): { at_s: number; end_s?: number | null } {
  if (handle === "body") {
    return applySfxMove({
      atS: bar.at_s,
      endS: bar.end_s,
      deltaS,
      videoDurationS,
    });
  }

  const currentEnd = bar.end_s ?? bar.at_s + minDurationS;
  if (handle === "left") {
    const at_s = clamp(bar.at_s + deltaS, 0, currentEnd - minDurationS);
    return { at_s: roundTiming(at_s), end_s: roundTiming(currentEnd) };
  }

  const end_s = clamp(currentEnd + deltaS, bar.at_s + minDurationS, videoDurationS);
  return { at_s: roundTiming(bar.at_s), end_s: roundTiming(end_s) };
}

export function outputTimeForSlotBoundary({
  slots,
  grid,
  key,
  boundary = "start",
  rendered = false,
  renderedOutputDurationS,
  fallbackOverlapS,
}: {
  slots: DraftSlot[];
  grid: number[];
  key: string;
  boundary?: "start" | "end";
  rendered?: boolean;
  renderedOutputDurationS?: number | null;
  fallbackOverlapS?: number;
}): number | null {
  const idx = slots.findIndex((s) => s.key === key);
  if (idx < 0) return null;
  const layout = rendered
    ? renderedSequentialSlotLayout(slots, grid, {
        outputDurationS: renderedOutputDurationS,
        fallbackOverlapS,
      })
    : sequentialSlotLayout(slots, grid);
  const win = layout.windows[idx];
  if (!win || win.startS == null) return null;
  return roundTiming(
    boundary === "end" ? win.startS + win.durationS : win.startS,
  );
}

export function rangesDiffer(
  a: { start_s?: number; end_s?: number; inS?: number; durationS?: number | null; at_s?: number },
  b: { start_s?: number; end_s?: number; inS?: number; durationS?: number | null; at_s?: number },
): boolean {
  return (
    Math.abs((a.start_s ?? 0) - (b.start_s ?? 0)) > EPSILON ||
    Math.abs((a.end_s ?? 0) - (b.end_s ?? 0)) > EPSILON ||
    Math.abs((a.inS ?? 0) - (b.inS ?? 0)) > EPSILON ||
    Math.abs((a.durationS ?? 0) - (b.durationS ?? 0)) > EPSILON ||
    Math.abs((a.at_s ?? 0) - (b.at_s ?? 0)) > EPSILON
  );
}
