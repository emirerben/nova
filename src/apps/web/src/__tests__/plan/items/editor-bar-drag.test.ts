import { describe, expect, it } from "@jest/globals";
import {
  applyClipEdgeDrag,
  applyClipTimingInput,
  applySfxMove,
  applyTextBarDrag,
  applyTextTimingInput,
  resolveBarDragHandle,
  secondsDeltaFromTimelineX,
  timelineXFromClient,
} from "@/app/plan/items/[id]/_editor/editor-bar-drag";

describe("editor bar drag math", () => {
  it("resolves 24px edge hit zones with a body center", () => {
    expect(resolveBarDragHandle({ localX: 10, width: 120 })).toBe("left");
    expect(resolveBarDragHandle({ localX: 96, width: 120 })).toBe("right");
    expect(resolveBarDragHandle({ localX: 60, width: 120 })).toBe("body");
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
});
