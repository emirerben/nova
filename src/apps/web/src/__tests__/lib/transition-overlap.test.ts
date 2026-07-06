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

  it("does not apply fallback overlap when the real output is not shorter", () => {
    const slots = [
      slot({ key: "a", durationS: 0.469 }),
      slot({ key: "b", durationS: 0.469 }),
      slot({ key: "c", durationS: 0.982 }),
    ];

    const layout = renderedSlotLayout(slots, [], { outputDurationS: 2.5 });

    expect(layout.windows.map((w) => w.startS)).toEqual([0, 0.469, 0.938]);
    expect(layout.totalDurationS).toBe(2.5);
  });

  it("calibrates overlap from the real rendered duration when it is shorter", () => {
    const slots = [
      slot({ key: "a", durationS: 4 }),
      slot({ key: "b", durationS: 3 }),
      slot({ key: "c", durationS: 2 }),
    ];

    const layout = renderedSlotLayout(slots, [], { outputDurationS: 8.7 });

    expect(layout.windows.map((w) => w.startS)).toEqual([0, 3.85, 6.7]);
    expect(layout.totalDurationS).toBe(8.7);
  });
});
