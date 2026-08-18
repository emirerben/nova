import {
  defaultLookAdjustments,
  lookAdjustmentsEqual,
  lookPreviewStyles,
  resolveLookAdjustments,
} from "@/lib/look-presets";

describe("customizable video looks", () => {
  it("pins distinct reference-derived defaults", () => {
    expect(defaultLookAdjustments("olive_film")).toEqual({
      intensity: 1,
      warmth: 0,
      contrast: 0,
      grain: 0.18,
      vignette: 0.22,
    });
    expect(defaultLookAdjustments("smoky_split_tone")).toEqual({
      intensity: 1,
      warmth: 0,
      contrast: 0,
      grain: 0.36,
      vignette: 0.55,
    });
    expect(defaultLookAdjustments("none")).toBeNull();
  });

  it("clamps persisted controls at the preview boundary", () => {
    expect(
      resolveLookAdjustments("olive_film", {
        intensity: 2,
        warmth: -2,
        contrast: 0.25,
        grain: -1,
        vignette: 3,
      }),
    ).toEqual({
      intensity: 1,
      warmth: -1,
      contrast: 0.25,
      grain: 0,
      vignette: 1,
    });
  });

  it("mirrors the two grades with distinct filters, tint, grain, and vignette", () => {
    const olive = lookPreviewStyles("olive_film");
    const smoky = lookPreviewStyles("smoky_split_tone");

    expect(olive.video.filter).toContain("contrast(0.955)");
    expect(String(olive.tint?.background)).toContain("132,93,28");
    expect(olive.grain?.opacity).toBeCloseTo(0.0504);
    expect(smoky.video.filter).toContain("contrast(1.055)");
    expect(String(smoky.tint?.background)).toContain("8,61,67");
    expect(smoky.grain?.opacity).toBeCloseTo(0.1008);
    expect(olive.video.filter).not.toEqual(smoky.video.filter);
  });

  it("keeps the approved fixed edit-wide looks visually distinct", () => {
    const golden = lookPreviewStyles("golden_hour");
    const faded = lookPreviewStyles("faded_analog");

    expect(golden.video.filter).toContain("saturate(1.22)");
    expect(golden.grain).toBeNull();
    expect(faded.video.filter).toContain("saturate(.76)");
    expect(faded.grain?.opacity).toBe(0.12);
    expect(golden.video.filter).not.toEqual(faded.video.filter);
    expect(defaultLookAdjustments("golden_hour")).toBeNull();
    expect(defaultLookAdjustments("faded_analog")).toBeNull();
  });

  it("keeps Original an exact preview bypass and compares every control", () => {
    expect(lookPreviewStyles("none")).toEqual({
      video: {},
      tint: null,
      grain: null,
    });
    expect(
      lookAdjustmentsEqual(
        defaultLookAdjustments("olive_film"),
        defaultLookAdjustments("olive_film"),
      ),
    ).toBe(true);
    expect(
      lookAdjustmentsEqual(
        defaultLookAdjustments("olive_film"),
        defaultLookAdjustments("smoky_split_tone"),
      ),
    ).toBe(false);
  });
});
