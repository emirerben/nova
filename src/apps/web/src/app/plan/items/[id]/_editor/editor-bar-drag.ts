import { slotWindows, type DraftSlot, type SlotWindow } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

export type BarDragHandle = "left" | "right" | "body";

export const BAR_EDGE_HIT_PX = 24;
export const CLICK_DRAG_THRESHOLD_PX = 3;
export const TEXT_MIN_DURATION_S = 0.3;
export const CLIP_MIN_DURATION_S = 0.6;

const EPSILON = 1e-6;

function clamp(value: number, min: number, max: number): number {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

export function roundTiming(value: number): number {
  return Math.round(value * 1000) / 1000;
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
  let startS = 0;

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
      startS: roundTiming(startS),
      durationS,
      offsetBeats: base.offsetBeats,
    });
    rangeParts.push(
      [
        slot.key,
        roundTiming(slot.inS),
        durationS,
        slot.durationBeats ?? "s",
      ].join(":"),
    );
    startS += durationS;
  });

  return {
    windows,
    totalDurationS: roundTiming(startS),
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
}: {
  slots: DraftSlot[];
  grid: number[];
  key: string;
  boundary?: "start" | "end";
}): number | null {
  const idx = slots.findIndex((s) => s.key === key);
  if (idx < 0) return null;
  const win = sequentialSlotLayout(slots, grid).windows[idx];
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
