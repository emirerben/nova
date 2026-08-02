export interface TextShadowLayer {
  readonly alpha: number;
  readonly sigmaPx: number;
  readonly cssBlurPx: number;
  readonly dxPx: number;
  readonly dyPx: number;
}

export type TextShadowStyle = "none" | "standard" | "high_visibility";

/** Mirrors `_STANDARD_TEXT_SHADOW_LAYERS` in the Skia renderer. */
export const STANDARD_TEXT_SHADOW_LAYERS: readonly TextShadowLayer[] = [
  { alpha: 160, sigmaPx: 12, cssBlurPx: 24, dxPx: 0, dyPx: 6 },
] as const;

/** Mirrors `_HIGH_VISIBILITY_TEXT_SHADOW_LAYERS`, back to front. */
export const HIGH_VISIBILITY_TEXT_SHADOW_LAYERS: readonly TextShadowLayer[] = [
  { alpha: 115, sigmaPx: 14, cssBlurPx: 28, dxPx: 0, dyPx: 8 },
  { alpha: 200, sigmaPx: 3, cssBlurPx: 6, dxPx: 0, dyPx: 2 },
] as const;

export function textShadowStyle(
  shadowEnabled: boolean | null | undefined,
  shadowStyle?: "standard" | "high_visibility" | null,
): TextShadowStyle {
  if (shadowEnabled === false) return "none";
  if (shadowStyle === "high_visibility") return "high_visibility";
  return "standard";
}

export function textShadowLayers(
  style: TextShadowStyle,
): readonly TextShadowLayer[] {
  if (style === "none") return [];
  return style === "high_visibility"
    ? HIGH_VISIBILITY_TEXT_SHADOW_LAYERS
    : STANDARD_TEXT_SHADOW_LAYERS;
}

function directionalBleedPx(
  layers: readonly TextShadowLayer[],
  axis: "x" | "y",
  direction: -1 | 1,
): number {
  if (layers.length === 0) return 0;
  return Math.max(
    ...layers.map((layer) => {
      const offset = axis === "x" ? layer.dxPx : layer.dyPx;
      return Math.ceil(3 * layer.sigmaPx + Math.max(0, direction * offset));
    }),
  );
}

/** Conservative 3σ clipping extents, derived from the shared profile. */
export function textShadowBleedPx(style: TextShadowStyle) {
  const layers = textShadowLayers(style);
  return {
    left: directionalBleedPx(layers, "x", -1),
    top: directionalBleedPx(layers, "y", -1),
    right: directionalBleedPx(layers, "x", 1),
    bottom: directionalBleedPx(layers, "y", 1),
  } as const;
}

export type CanvasPixelFormatter = (pixels: number) => string;

/**
 * Build the browser preview equivalent of the Skia profile. CSS paints the
 * first listed shadow on top, so emit contact before ambient.
 */
export function textShadowCss(
  canvasPx: CanvasPixelFormatter,
  style: TextShadowStyle = "standard",
): string | undefined {
  const layers = textShadowLayers(style);
  if (layers.length === 0) return undefined;
  return [...layers]
    .reverse()
    .map((layer) => {
      const x = layer.dxPx === 0 ? "0" : canvasPx(layer.dxPx);
      return `${x} ${canvasPx(layer.dyPx)} ${canvasPx(layer.cssBlurPx)} rgba(0, 0, 0, ${layer.alpha / 255})`;
    })
    .join(", ");
}
