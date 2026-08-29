import {
  buildMobilePreviewTimeline,
  mobilePreviewOutputAtSource,
  mobilePreviewSourceAtOutput,
} from "@/app/dev-qa/mobile-editor/mobile-editor-preview";

describe("mobile editor preview mapping", () => {
  const timeline = buildMobilePreviewTimeline([
    {
      id: "trimmed-a",
      startS: 0,
      endS: 2,
      sourceStartS: 1.25,
      sourceUrl: "/source.mp4",
    },
    {
      id: "reordered-b",
      startS: 2,
      endS: 5,
      sourceStartS: 7,
      sourceUrl: "/source.mp4",
    },
  ]);

  it("maps output time into the active clip's trimmed source window", () => {
    expect(mobilePreviewSourceAtOutput(timeline, 0.5)).toEqual({
      entryIndex: 0,
      sourceTimeS: 1.75,
    });
    expect(mobilePreviewSourceAtOutput(timeline, 2.5)).toEqual({
      entryIndex: 1,
      sourceTimeS: 7.5,
    });
  });

  it("right-biases shared boundaries and maps decoded frames back to output", () => {
    expect(mobilePreviewSourceAtOutput(timeline, 2)).toEqual({
      entryIndex: 1,
      sourceTimeS: 7,
    });
    expect(mobilePreviewOutputAtSource(timeline, 1, 8.25)).toEqual({
      outputTimeS: 3.25,
      reachedEnd: false,
    });
    expect(mobilePreviewOutputAtSource(timeline, 1, 10)).toEqual({
      outputTimeS: 5,
      reachedEnd: true,
    });
  });
});
