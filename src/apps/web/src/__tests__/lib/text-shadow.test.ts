import {
  HIGH_VISIBILITY_TEXT_SHADOW_LAYERS,
  STANDARD_TEXT_SHADOW_LAYERS,
  textShadowBleedPx,
  textShadowCss,
  textShadowStyle,
} from "@/lib/text-shadow";

describe("text shadow styles", () => {
  it("keeps the standard renderer shadow for unspecified values", () => {
    expect(STANDARD_TEXT_SHADOW_LAYERS).toEqual([
      { alpha: 160, sigmaPx: 12, cssBlurPx: 24, dxPx: 0, dyPx: 6 },
    ]);
    expect(textShadowStyle(undefined)).toBe("standard");
    expect(textShadowStyle(null)).toBe("standard");
    expect(textShadowStyle(true)).toBe("standard");
    expect(textShadowBleedPx("standard")).toEqual({
      left: 36,
      top: 36,
      right: 36,
      bottom: 42,
    });
    expect(textShadowCss((pixels) => `${pixels}px`)).toBe(
      `0 6px 24px rgba(0, 0, 0, ${160 / 255})`,
    );
  });

  it("mirrors the opt-in high-visibility profile and emits contact above ambient", () => {
    expect(HIGH_VISIBILITY_TEXT_SHADOW_LAYERS).toEqual([
      { alpha: 115, sigmaPx: 14, cssBlurPx: 28, dxPx: 0, dyPx: 8 },
      { alpha: 200, sigmaPx: 3, cssBlurPx: 6, dxPx: 0, dyPx: 2 },
    ]);
    expect(textShadowStyle(true, "high_visibility")).toBe("high_visibility");
    expect(textShadowBleedPx("high_visibility")).toEqual({
      left: 42,
      top: 42,
      right: 42,
      bottom: 50,
    });
    expect(textShadowCss((pixels) => `${pixels}px`, "high_visibility")).toBe(
      `0 2px 6px rgba(0, 0, 0, ${200 / 255}), ` +
        `0 8px 28px rgba(0, 0, 0, ${115 / 255})`,
    );
  });

  it("disables every layer together", () => {
    expect(textShadowStyle(false)).toBe("none");
    expect(textShadowStyle(false, "high_visibility")).toBe("none");
    expect(textShadowBleedPx("none")).toEqual({ left: 0, top: 0, right: 0, bottom: 0 });
    expect(textShadowCss((pixels) => `${pixels}px`, "none")).toBeUndefined();
  });
});
