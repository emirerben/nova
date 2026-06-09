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
    ctx.font = `${weight} ${sizePx}px ${cssFamily}`;
    return (text: string) => ctx.measureText(text).width;
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
