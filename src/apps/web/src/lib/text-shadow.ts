export interface TextShadowLayer {
  readonly alpha: number;
  readonly sigmaPx: number;
  readonly cssBlurPx: number;
  readonly dxPx: number;
  readonly dyPx: number;
}

/** Mirrors `_TEXT_SHADOW_LAYERS` in the Skia renderer, back to front. */
export const TEXT_SHADOW_LAYERS: readonly TextShadowLayer[] = [
  { alpha: 115, sigmaPx: 14, cssBlurPx: 28, dxPx: 0, dyPx: 8 },
  { alpha: 200, sigmaPx: 3, cssBlurPx: 6, dxPx: 0, dyPx: 2 },
] as const;

function directionalBleedPx(axis: "x" | "y", direction: -1 | 1): number {
  return Math.max(
    ...TEXT_SHADOW_LAYERS.map((layer) => {
      const offset = axis === "x" ? layer.dxPx : layer.dyPx;
      return Math.ceil(3 * layer.sigmaPx + Math.max(0, direction * offset));
    }),
  );
}

/** Conservative 3σ clipping extents, derived from the shared profile. */
export const TEXT_SHADOW_BLEED_PX = {
  left: directionalBleedPx("x", -1),
  top: directionalBleedPx("y", -1),
  right: directionalBleedPx("x", 1),
  bottom: directionalBleedPx("y", 1),
} as const;

export type CanvasPixelFormatter = (pixels: number) => string;

/**
 * Build the browser preview equivalent of the Skia profile. CSS paints the
 * first listed shadow on top, so emit contact before ambient.
 */
export function textShadowCss(
  canvasPx: CanvasPixelFormatter,
  enabled = true,
): string | undefined {
  if (!enabled) return undefined;
  return [...TEXT_SHADOW_LAYERS]
    .reverse()
    .map((layer) => {
      const x = layer.dxPx === 0 ? "0" : canvasPx(layer.dxPx);
      return `${x} ${canvasPx(layer.dyPx)} ${canvasPx(layer.cssBlurPx)} rgba(0, 0, 0, ${layer.alpha / 255})`;
    })
    .join(", ");
}
