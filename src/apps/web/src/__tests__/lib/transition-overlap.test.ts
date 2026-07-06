import { describe, expect, it } from "@jest/globals";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  outputTimeToPlainTime,
  renderedSlotLayout,
  transitionOverlapForBoundary,
} from "@/lib/timeline/transition-overlap";

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "s1",
    slotId: "s1",
    clipIndex: 0,
    inS: 0,
    durationBeats: null,
    durationS: 4,
    removed: false,
    momentDescription: null,
    ...over,
  };
}

describe("rendered transition overlap math", () => {
  it("clamps each boundary to 0.3s or 30 percent of the shorter neighbor", () => {
    expect(transitionOverlapForBoundary(4, 5)).toBe(0.3);
    expect(transitionOverlapForBoundary(0.5, 5)).toBe(0.15);
  });

  it("lays out rendered clips with per-boundary overlap", () => {
    const layout = renderedSlotLayout(
      [
        slot({ key: "a", durationS: 4 }),
        slot({ key: "b", durationS: 3 }),
        slot({ key: "c", durationS: 2 }),
      ],
      [],
    );

    expect(layout.windows.map((w) => w.startS)).toEqual([0, 3.7, 6.4]);
    expect(layout.windows.map((w) => w.durationS)).toEqual([4, 3, 2]);
    expect(layout.totalDurationS).toBe(8.4);
  });

  it("maps output time back to the plain slot clock across two boundaries", () => {
    const slots = [
      slot({ key: "a", durationS: 4 }),
      slot({ key: "b", durationS: 3 }),
      slot({ key: "c", durationS: 2 }),
    ];

    expect(outputTimeToPlainTime(3.75, slots, [])).toBe(4.05);
    expect(outputTimeToPlainTime(6.45, slots, [])).toBe(7.05);
  });
});
