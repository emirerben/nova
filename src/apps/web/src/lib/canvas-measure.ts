/**
 * Browser-side text measurement for the instant-edit preview.
 *
 * Provides the `MeasureAtSize` factory that overlay-layout.ts's pure math
 * consumes, backed by an offscreen canvas 2d context over the SAME registry
 * TTFs the server burns with (loaded via @font-face — see lib/font-faces.ts).
 * Kept separate from overlay-layout.ts so the layout module stays DOM-free
 * and unit-testable with injected measures.
 */

import type { MeasureAtSize } from "@/lib/overlay-layout";
import { resolveClusterCssFont } from "@/lib/overlay-constants";
import type { MeasureCluster } from "@/lib/variant-editor/overlay-cluster-layout";

let _ctx: CanvasRenderingContext2D | null = null;

function context(): CanvasRenderingContext2D | null {
  if (_ctx) return _ctx;
  if (typeof document === "undefined") return null;
  const canvas = document.createElement("canvas");
  _ctx = canvas.getContext("2d");
  return _ctx;
}

/**
 * Measurement factory bound to a CSS family + weight. `cssFamily` is the
 * registry's `css_family` value (e.g. `'Playfair Display', serif`) — the
 * canvas font shorthand accepts the full fallback list.
 */
export function makeCanvasMeasureAt(cssFamily: string, weight: number): MeasureAtSize {
  return (sizePx: number) => {
    const ctx = context();
    if (!ctx) return (text: string) => text.length * sizePx * 0.6; // SSR/jsdom fallback
    const fontSpec = `${weight} ${sizePx}px ${cssFamily}`;
    return (text: string) => {
      // Re-assert per call — the context is shared, so an interleaved factory
      // (e.g. two previews measuring different faces) must not corrupt widths.
      ctx.font = fontSpec;
      return ctx.measureText(text).width;
    };
  };
}

/**
 * Raw line height (ascent + descent) of the face at `sizePx` — the browser
 * analogue of Skia's `fDescent - fAscent` that `_measure_block` uses. Falls
 * back to 1.2 × size where fontBoundingBox metrics are unavailable (jsdom).
 */
export function fontLineHeight(cssFamily: string, weight: number, sizePx: number): number {
  const ctx = context();
  if (!ctx) return sizePx * 1.2;
  ctx.font = `${weight} ${sizePx}px ${cssFamily}`;
  const m = ctx.measureText("Mg");
  const ascent = m.fontBoundingBoxAscent;
  const descent = m.fontBoundingBoxDescent;
  if (typeof ascent === "number" && typeof descent === "number" && ascent + descent > 0) {
    return ascent + descent;
  }
  return sizePx * 1.2;
}

/**
 * Cluster measurement — backs the `MeasureCluster` the editorial-cluster layout
 * (overlay-cluster-layout.ts) injects. `family` is a REGISTRY name (e.g.
 * "Great Vibes", "Playfair Display Italic"); it resolves to the css family +
 * weight + style so the canvas measures the SAME face the server burns (the
 * italic accent face in particular — see resolveClusterCssFont). Width comes
 * from `measureText().width`; height (ascent+descent) from `fontBoundingBox*`,
 * the browser analogue of Skia's `fDescent - fAscent`. SSR/jsdom (no real
 * metrics) falls back to length-based widths so layout never crashes.
 */
export function makeCanvasClusterMeasure(): MeasureCluster {
  return (family: string, text: string, px: number) => {
    const { family: cssFamily, weight, style } = resolveClusterCssFont(family);
    const ctx = context();
    if (!ctx) {
      // jsdom/SSR: a coarse stub keeps the pure layout running; the real
      // preview always has a canvas context.
      return { wPx: text.length * px * 0.5, hPx: px * 1.2 };
    }
    ctx.font = `${style} ${weight} ${px}px ${cssFamily}`;
    const m = ctx.measureText(text);
    const ascent = m.fontBoundingBoxAscent;
    const descent = m.fontBoundingBoxDescent;
    const hPx =
      typeof ascent === "number" && typeof descent === "number" && ascent + descent > 0
        ? ascent + descent
        : px * 1.2;
    return { wPx: m.width, hPx };
  };
}

/** Ensure an editorial cluster face (resolved by registry name) is loaded for
 * measurement/display — resolves the css family + weight + style so the italic
 * accent face actually lands before re-measure. Never rejects. */
export async function ensureClusterFontLoaded(family: string, sizePx = 64): Promise<void> {
  if (typeof document === "undefined" || !("fonts" in document)) return;
  const { family: cssFamily, weight, style } = resolveClusterCssFont(family);
  try {
    await document.fonts.load(`${style} ${weight} ${sizePx}px ${cssFamily}`);
  } catch {
    // font stays on fallback metrics — preview degrades, never crashes
  }
}

/**
 * Resolve when the font is usable for measurement/display. Layout runs once
 * immediately (fallback metrics) and re-runs when this resolves so the preview
 * snaps to the real face. Never rejects — a load failure just keeps fallback.
 */
export async function ensureFontLoaded(
  cssFamily: string,
  weight: number,
  sizePx = 64,
): Promise<void> {
  if (typeof document === "undefined" || !("fonts" in document)) return;
  try {
    await document.fonts.load(`${weight} ${sizePx}px ${cssFamily}`);
  } catch {
    // font stays on fallback metrics — preview degrades, never crashes
  }
}
