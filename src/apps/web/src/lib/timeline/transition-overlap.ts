import { slotWindows, type DraftSlot, type SlotWindow } from "@/app/generative/timeline-math";

export const DEFAULT_RENDERED_TRANSITION_OVERLAP_S = 0.3;

export interface RenderedSlotWindow extends SlotWindow {
  overlapBeforeS: number;
}

export interface RenderedSlotLayout {
  windows: RenderedSlotWindow[];
  totalDurationS: number;
}

function roundTiming(value: number): number {
  return Math.round(value * 1000) / 1000;
}

export function transitionOverlapForBoundary(
  prevDurationS: number,
  nextDurationS: number,
  baseOverlapS = DEFAULT_RENDERED_TRANSITION_OVERLAP_S,
): number {
  if (prevDurationS <= 0 || nextDurationS <= 0 || baseOverlapS <= 0) return 0;
  return roundTiming(Math.min(baseOverlapS, Math.min(prevDurationS, nextDurationS) * 0.3));
}

export function renderedSlotLayout(
  slots: DraftSlot[],
  grid: number[],
  baseOverlapS = DEFAULT_RENDERED_TRANSITION_OVERLAP_S,
): RenderedSlotLayout {
  const baseWindows = slotWindows(slots, grid);
  const windows: RenderedSlotWindow[] = [];
  let outputCursorS = 0;
  let previousActiveDurationS: number | null = null;

  slots.forEach((slot, index) => {
    const base = baseWindows[index] ?? {
      startS: null,
      durationS: 0,
      offsetBeats: null,
    };

    if (slot.removed || base.durationS <= 0) {
      windows.push({ ...base, startS: null, durationS: 0, overlapBeforeS: 0 });
      return;
    }

    const durationS = roundTiming(base.durationS);
    const overlapBeforeS =
      previousActiveDurationS == null
        ? 0
        : transitionOverlapForBoundary(previousActiveDurationS, durationS, baseOverlapS);
    const startS = roundTiming(outputCursorS - overlapBeforeS);
    windows.push({
      startS,
      durationS,
      offsetBeats: base.offsetBeats,
      overlapBeforeS,
    });
    outputCursorS = roundTiming(startS + durationS);
    previousActiveDurationS = durationS;
  });

  return {
    windows,
    totalDurationS: roundTiming(outputCursorS),
  };
}

export function outputTimeToPlainTime(
  outputTimeS: number,
  slots: DraftSlot[],
  grid: number[],
  baseOverlapS = DEFAULT_RENDERED_TRANSITION_OVERLAP_S,
): number {
  const rendered = renderedSlotLayout(slots, grid, baseOverlapS);
  const plain = slotWindows(slots, grid);
  const activeRendered = rendered.windows
    .map((win, index) => ({ win, plain: plain[index] }))
    .filter(({ win }) => win.startS != null && win.durationS > 0);
  if (activeRendered.length === 0) return Math.max(0, outputTimeS);

  const clamped = Math.max(0, Math.min(rendered.totalDurationS, outputTimeS));
  let best = activeRendered[0];
  for (const candidate of activeRendered) {
    const startS = candidate.win.startS ?? 0;
    if (clamped + 1e-6 >= startS) best = candidate;
    else break;
  }

  const renderedStart = best.win.startS ?? 0;
  const plainStart = best.plain.startS ?? 0;
  const local = Math.max(0, Math.min(best.win.durationS, clamped - renderedStart));
  return roundTiming(plainStart + local);
}
