import { describe, expect, it } from "@jest/globals";
import {
  applyClipEdgeDrag,
  applyClipSourceWindowDrag,
  applyClipTimingInput,
  applySfxBarDrag,
  applySfxMove,
  applyTextBarDrag,
  applyTextTimingInput,
  effectiveBarEdgeHitPx,
  outputTimeForSlotBoundary,
  resolveBarDragHandle,
  secondsDeltaFromTimelineX,
  sequentialSlotLayout,
  timelineXFromClient,
} from "@/app/plan/items/[id]/_editor/editor-bar-drag";
import type { DraftSlot } from "@/app/generative/timeline-math";

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

describe("editor bar drag math", () => {
  it("resolves 24px edge hit zones with a body center", () => {
    expect(resolveBarDragHandle({ localX: 10, width: 120 })).toBe("left");
    expect(resolveBarDragHandle({ localX: 96, width: 120 })).toBe("right");
    expect(resolveBarDragHandle({ localX: 60, width: 120 })).toBe("body");
  });

  it("keeps a grab body zone on narrow bars", () => {
    expect(effectiveBarEdgeHitPx(45)).toBe(15);
    expect(resolveBarDragHandle({ localX: 4, width: 45 })).toBe("left");
    expect(resolveBarDragHandle({ localX: 22.5, width: 45 })).toBe("body");
    expect(resolveBarDragHandle({ localX: 41, width: 45 })).toBe("right");
  });

  it("converts pointer coordinates through horizontal scroll", () => {
    const start = timelineXFromClient({
      clientX: 300,
      scrollRectLeft: 100,
      scrollLeft: 240,
    });
    const current = timelineXFromClient({
      clientX: 350,
      scrollRectLeft: 100,
      scrollLeft: 260,
    });
    expect(start).toBe(440);
    expect(current).toBe(510);
    expect(
      secondsDeltaFromTimelineX({
        startTimelineX: start,
        currentTimelineX: current,
        pxPerSecond: 20,
      }),
    ).toBe(3.5);
  });

  it("moves a text bar while preserving duration and clamping to the video", () => {
    expect(
      applyTextBarDrag({
        bar: { start_s: 2, end_s: 4 },
        handle: "body",
        deltaS: 3,
        videoDurationS: 6,
      }),
    ).toEqual({ start_s: 4, end_s: 6 });

    expect(
      applyTextBarDrag({
        bar: { start_s: 2, end_s: 4 },
        handle: "body",
        deltaS: -5,
        videoDurationS: 6,
      }),
    ).toEqual({ start_s: 0, end_s: 2 });
  });

  it("trims text both ways with a 0.3s floor", () => {
    expect(
      applyTextBarDrag({
        bar: { start_s: 1, end_s: 3 },
        handle: "left",
        deltaS: 5,
        videoDurationS: 10,
      }),
    ).toEqual({ start_s: 2.7, end_s: 3 });

    expect(
      applyTextBarDrag({
        bar: { start_s: 1, end_s: 3 },
        handle: "right",
        deltaS: -5,
        videoDurationS: 10,
      }),
    ).toEqual({ start_s: 1, end_s: 1.3 });
  });

  it("validates direct text timing input", () => {
    expect(
      applyTextTimingInput({
        startS: 9.9,
        endS: 20,
        videoDurationS: 10,
      }),
    ).toEqual({ start_s: 9.7, end_s: 10 });
  });

  it("trims a clip left by moving in-point and preserving source out", () => {
    expect(
      applyClipEdgeDrag({
        slot: { inS: 2, durationS: 4 },
        handle: "left",
        deltaS: 1.25,
        sourceDurationS: 12,
      }),
    ).toEqual({ inS: 3.25, durationS: 2.75, durationBeats: null });
  });

  it("extends a clip left only to the source start", () => {
    expect(
      applyClipEdgeDrag({
        slot: { inS: 2, durationS: 4 },
        handle: "left",
        deltaS: -8,
        sourceDurationS: 12,
      }),
    ).toEqual({ inS: 0, durationS: 6, durationBeats: null });
  });

  it("trims a clip right with source bounds and a 0.6s floor", () => {
    expect(
      applyClipEdgeDrag({
        slot: { inS: 8, durationS: 2 },
        handle: "right",
        deltaS: 8,
        sourceDurationS: 11,
      }),
    ).toEqual({ inS: 8, durationS: 3, durationBeats: null });

    expect(
      applyClipEdgeDrag({
        slot: { inS: 8, durationS: 2 },
        handle: "right",
        deltaS: -8,
        sourceDurationS: 11,
      }),
    ).toEqual({ inS: 8, durationS: 0.6, durationBeats: null });
  });

  it("allows optimistic clip duration when source duration is unknown", () => {
    expect(
      applyClipEdgeDrag({
        slot: { inS: 8, durationS: 2 },
        handle: "right",
        deltaS: 8,
        sourceDurationS: null,
      }),
    ).toEqual({ inS: 8, durationS: 10, durationBeats: null });
  });

  it("validates direct clip timing input", () => {
    expect(
      applyClipTimingInput({
        inS: 5,
        outS: 5.2,
        sourceDurationS: 9,
      }),
    ).toEqual({ inS: 5, durationS: 0.6, durationBeats: null });
  });

  it("slides a source window while preserving duration and clamping to source bounds", () => {
    expect(
      applyClipSourceWindowDrag({
        slot: { inS: 2, durationS: 3 },
        handle: "body",
        deltaS: 4,
        sourceDurationS: 8,
      }),
    ).toEqual({ inS: 5, durationS: 3, durationBeats: null });

    expect(
      applyClipSourceWindowDrag({
        slot: { inS: 2, durationS: 3 },
        handle: "body",
        deltaS: -9,
        sourceDurationS: 8,
      }),
    ).toEqual({ inS: 0, durationS: 3, durationBeats: null });
  });

  it("uses the 0.6s floor for source-window edge trims", () => {
    expect(
      applyClipSourceWindowDrag({
        slot: { inS: 2, durationS: 3 },
        handle: "right",
        deltaS: -9,
        sourceDurationS: 8,
      }),
    ).toEqual({ inS: 2, durationS: 0.6, durationBeats: null });
  });

  it("moves SFX starts while keeping duration inside the video", () => {
    expect(
      applySfxMove({
        atS: 4,
        endS: 5,
        deltaS: 20,
        videoDurationS: 10,
      }),
    ).toEqual({ at_s: 9, end_s: 10 });
  });

  it("trims SFX bars from the edge hit zones", () => {
    expect(
      applySfxBarDrag({
        bar: { at_s: 2, end_s: 4 },
        handle: "left",
        deltaS: 1.5,
        videoDurationS: 10,
      }),
    ).toEqual({ at_s: 3.5, end_s: 4 });

    expect(
      applySfxBarDrag({
        bar: { at_s: 2, end_s: 4 },
        handle: "right",
        deltaS: -5,
        videoDurationS: 10,
      }),
    ).toEqual({ at_s: 2, end_s: 2.3 });
  });
});

describe("sequentialSlotLayout", () => {
  it("ripples following slots after a trim changes duration", () => {
    const slots = [
      slot({ key: "a", slotId: "a", durationS: 4 }),
      slot({ key: "b", slotId: "b", durationS: 3 }),
      slot({ key: "c", slotId: "c", durationS: 2 }),
    ];
    const before = sequentialSlotLayout(slots, []);
    const after = sequentialSlotLayout(
      slots.map((s) => (s.key === "a" ? { ...s, durationS: 1.5 } : s)),
      [],
    );

    expect(before.windows.map((w) => w.startS)).toEqual([0, 4, 7]);
    expect(after.windows.map((w) => w.startS)).toEqual([0, 1.5, 4.5]);
    expect(after.totalDurationS).toBe(6.5);
  });

  it("skips removed slots when computing downstream positions", () => {
    const layout = sequentialSlotLayout(
      [
        slot({ key: "a", slotId: "a", durationS: 4 }),
        slot({ key: "b", slotId: "b", durationS: 3, removed: true }),
        slot({ key: "c", slotId: "c", durationS: 2 }),
      ],
      [],
    );

    expect(layout.windows.map((w) => w.startS)).toEqual([0, null, 4]);
    expect(layout.windows.map((w) => w.durationS)).toEqual([4, 0, 2]);
    expect(layout.totalDurationS).toBe(6);
  });

  it("maps slot output offsets while skipping removed slots", () => {
    const slots = [
      slot({ key: "a", slotId: "a", durationS: 4 }),
      slot({ key: "b", slotId: "b", durationS: 3, removed: true }),
      slot({ key: "c", slotId: "c", durationS: 2 }),
    ];

    expect(
      outputTimeForSlotBoundary({ slots, grid: [], key: "c", boundary: "start" }),
    ).toBe(4);
    expect(
      outputTimeForSlotBoundary({ slots, grid: [], key: "c", boundary: "end" }),
    ).toBe(6);
    expect(
      outputTimeForSlotBoundary({ slots, grid: [], key: "b", boundary: "start" }),
    ).toBeNull();
  });

  it("ripples a split slot by cumulative seconds", () => {
    const layout = sequentialSlotLayout(
      [
        slot({ key: "a", slotId: "a", durationS: 1.5 }),
        slot({ key: "a2", slotId: null, inS: 1.5, durationS: 2.5 }),
        slot({ key: "b", slotId: "b", durationS: 3 }),
      ],
      [],
    );

    expect(layout.windows.map((w) => w.startS)).toEqual([0, 1.5, 4]);
    expect(layout.totalDurationS).toBe(7);
  });

  it("includes source ranges in the filmstrip invalidation key", () => {
    const before = sequentialSlotLayout([slot({ key: "a", inS: 1, durationS: 3 })], []);
    const after = sequentialSlotLayout([slot({ key: "a", inS: 2, durationS: 3 })], []);

    expect(before.sourceRangeKey).not.toBe(after.sourceRangeKey);
  });
});
