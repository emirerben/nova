import {
  TEXT_SHADOW_BLEED_PX,
  TEXT_SHADOW_LAYERS,
  textShadowCss,
} from "@/lib/text-shadow";

describe("high-visibility text shadow", () => {
  it("mirrors the renderer profile and emits contact above ambient", () => {
    expect(TEXT_SHADOW_LAYERS).toEqual([
      { alpha: 115, sigmaPx: 14, cssBlurPx: 28, dxPx: 0, dyPx: 8 },
      { alpha: 200, sigmaPx: 3, cssBlurPx: 6, dxPx: 0, dyPx: 2 },
    ]);
    expect(TEXT_SHADOW_BLEED_PX).toEqual({ left: 42, top: 42, right: 42, bottom: 50 });
    expect(textShadowCss((pixels) => `${pixels}px`)).toBe(
      `0 2px 6px rgba(0, 0, 0, ${200 / 255}), ` +
        `0 8px 28px rgba(0, 0, 0, ${115 / 255})`,
    );
  });

  it("disables the full profile together", () => {
    expect(textShadowCss((pixels) => `${pixels}px`, false)).toBeUndefined();
  });
});
