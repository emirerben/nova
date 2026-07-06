import { slotWindows, type DraftSlot, type SlotWindow } from "@/app/generative/timeline-math";

export const DEFAULT_RENDERED_TRANSITION_OVERLAP_S = 0.3;

export interface RenderedSlotWindow extends SlotWindow {
  overlapBeforeS: number;
}

export interface RenderedSlotLayout {
  windows: RenderedSlotWindow[];
  totalDurationS: number;
}

export interface RenderedSlotLayoutOptions {
  /**
   * The real rendered video duration, when metadata has loaded. If the render is
   * shorter than the plain slot sum, distribute that delta over boundaries. If
   * it is equal/longer, do not invent overlap.
   */
  outputDurationS?: number | null;
  /** Maximum overlap budget per boundary when the real output proves shorter. */
  baseOverlapS?: number;
  /** Overlap used only before outputDurationS is known. */
  fallbackOverlapS?: number;
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
  options: RenderedSlotLayoutOptions | number = {},
): RenderedSlotLayout {
  const opts =
    typeof options === "number" ? { baseOverlapS: options, fallbackOverlapS: options } : options;
  const baseOverlapS =
    opts.baseOverlapS ?? DEFAULT_RENDERED_TRANSITION_OVERLAP_S;
  const fallbackOverlapS =
    opts.fallbackOverlapS ?? baseOverlapS;
  const outputDurationS =
    typeof opts.outputDurationS === "number" &&
    Number.isFinite(opts.outputDurationS) &&
    opts.outputDurationS > 0
      ? opts.outputDurationS
      : null;
  const baseWindows = slotWindows(slots, grid);
  const windows: RenderedSlotWindow[] = [];
  const activeIndexes: number[] = [];
  const activeDurations: number[] = [];

  slots.forEach((slot, index) => {
    const base = baseWindows[index];
    if (!slot.removed && base?.durationS != null && base.durationS > 0) {
      activeIndexes.push(index);
      activeDurations.push(roundTiming(base.durationS));
    }
  });

  const plainTotalS = roundTiming(
    activeDurations.reduce((sum, durationS) => sum + durationS, 0),
  );
  const maxBoundaryCaps = activeDurations.map((durationS, activeIndex) => {
    if (activeIndex === 0) return 0;
    return transitionOverlapForBoundary(
      activeDurations[activeIndex - 1],
      durationS,
      baseOverlapS,
    );
  });
  const fallbackBoundaryCaps =
    fallbackOverlapS === baseOverlapS
      ? maxBoundaryCaps
      : activeDurations.map((durationS, activeIndex) => {
          if (activeIndex === 0) return 0;
          return transitionOverlapForBoundary(
            activeDurations[activeIndex - 1],
            durationS,
            fallbackOverlapS,
          );
        });
  const boundaryCaps = outputDurationS == null ? fallbackBoundaryCaps : maxBoundaryCaps;
  const capTotalS = boundaryCaps.reduce((sum, cap) => sum + cap, 0);
  const targetOverlapS =
    outputDurationS == null ? capTotalS : Math.max(0, plainTotalS - outputDurationS);
  const overlapScale =
    capTotalS > 0 ? Math.min(1, targetOverlapS / capTotalS) : 0;
  const overlapBySlotIndex = new Map<number, number>();
  activeIndexes.forEach((slotIndex, activeIndex) => {
    overlapBySlotIndex.set(
      slotIndex,
      roundTiming(boundaryCaps[activeIndex] * overlapScale),
    );
  });

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
      previousActiveDurationS == null ? 0 : (overlapBySlotIndex.get(index) ?? 0);
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
    totalDurationS: roundTiming(Math.max(outputCursorS, outputDurationS ?? 0)),
  };
}

export function outputTimeToPlainTime(
  outputTimeS: number,
  slots: DraftSlot[],
  grid: number[],
  options: RenderedSlotLayoutOptions | number = {},
): number {
  const rendered = renderedSlotLayout(slots, grid, options);
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
