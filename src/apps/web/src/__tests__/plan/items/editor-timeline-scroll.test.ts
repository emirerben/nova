import { describe, expect, it } from "@jest/globals";
import { anchoredTimelineScrollLeft } from "@/app/plan/items/[id]/_editor/editor-timeline-scroll";

describe("anchoredTimelineScrollLeft", () => {
  it("keeps a visible playhead at the same viewport offset while zooming", () => {
    expect(
      anchoredTimelineScrollLeft({
        previousScrollLeft: 100,
        viewportWidth: 500,
        previousPxPerSecond: 20,
        nextPxPerSecond: 40,
        durationS: 60,
        currentTimeS: 10,
      }),
    ).toBe(300);
  });

  it("anchors around the visible center when the playhead is outside the viewport", () => {
    expect(
      anchoredTimelineScrollLeft({
        previousScrollLeft: 300,
        viewportWidth: 400,
        previousPxPerSecond: 10,
        nextPxPerSecond: 20,
        durationS: 100,
        currentTimeS: 90,
      }),
    ).toBe(800);
  });

  it("clamps to the available scroll range", () => {
    expect(
      anchoredTimelineScrollLeft({
        previousScrollLeft: 800,
        viewportWidth: 500,
        previousPxPerSecond: 10,
        nextPxPerSecond: 6,
        durationS: 100,
        currentTimeS: 95,
      }),
    ).toBe(100);
  });
});
