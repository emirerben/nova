/**
 * TS port of the server's intro-overlay text layout — the math behind the
 * generative instant-edit preview (DOM text over the fast-reburn base video).
 *
 * Mirrors, constant-for-constant and branch-for-branch:
 * - `_wrap_text_to_lines` + `_shrink_to_fit` + `_measure_block` + `_resolve_anchor`
 *   + `_anchored_left_x` + `_vertical_block_top` in
 *   src/apps/api/app/pipeline/text_overlay_skia.py
 * - `balanced_word_wrap_indices` in src/apps/api/app/pipeline/text_wrap.py
 * - the settled-color rule in
 *   src/apps/api/app/pipeline/generative_overlays.py (build_persistent_intro_overlays)
 *
 * Measurement is injected (`MeasureText`) so the module is pure and Jest-testable;
 * the browser supplies canvas `measureText` over the SAME TTFs the server burns
 * (the registry fonts are byte-identical mirrors — see scripts/sync-font-registry.mjs).
 * Canvas vs Skia metrics can drift ~1%, so a borderline line MAY wrap differently
 * than the committed render — the preview is advisory; the burned video is
 * authoritative.
 */

import { CANVAS_H, CANVAS_W, FONT_SIZE_MAP, POSITION_Y_MAP } from "@/lib/overlay-constants";

// Must match text_overlay_skia.py: _MAX_LINE_W_FRAC / _LINE_SPACING / _MIN_FONT_SIZE
export const MAX_LINE_W_FRAC = 0.9;
export const LINE_SPACING = 1.15;
export const MIN_FONT_SIZE = 24;

/** Width of `text` in px at an implicit font size (the factory binds the size). */
export type MeasureText = (text: string) => number;
/** Measurement factory: returns a MeasureText bound to `sizePx`. */
export type MeasureAtSize = (sizePx: number) => MeasureText;

/** The resolved intro-overlay look — mirror of the burn params produced by
 * `_resolve_intro_overlay_params` (generative_build.py). */
export interface IntroOverlayParams {
  text: string;
  effect: string;
  textColor: string;
  highlightColor: string;
  fontFamily: string | null;
  textSizePx: number | null;
  /** Size-class bucket fallback (legacy variants without a px size). */
  textSize?: string | null;
  position: string;
  positionXFrac: number | null;
  positionYFrac: number | null;
  textAnchor: "left" | "right" | "center";
  strokeWidth: number | null;
  /** Per-role font overrides for the editorial cluster layout. */
  clusterHeroFont?: string | null;
  clusterBodyFont?: string | null;
  clusterAccentFont?: string | null;
  /** Per-role size overrides (absolute px) for the editorial cluster layout. */
  clusterHeroSizePx?: number | null;
  clusterBodySizePx?: number | null;
  clusterAccentSizePx?: number | null;
}

/** Greedy word-wrap — exact port of `_wrap_text_to_lines`. Explicit newlines wrap
 * each segment separately; an overlong single word is kept on its own line. */
export function greedyWrapLines(
  text: string,
  measure: MeasureText,
  maxWidth: number,
): string[] {
  const out: string[] = [];
  for (const rawLine of text.split("\n")) {
    const words = rawLine.split(/\s+/).filter((w) => w.length > 0);
    if (words.length === 0) {
      out.push("");
      continue;
    }
    let current: string[] = [];
    for (const word of words) {
      const candidate = current.length ? [...current, word].join(" ") : word;
      if (measure(candidate) <= maxWidth || current.length === 0) {
        current.push(word);
      } else {
        out.push(current.join(" "));
        current = [word];
      }
    }
    if (current.length) out.push(current.join(" "));
  }
  return out;
}

/** Balanced word-wrap — exact port of `balanced_word_wrap_indices` (text_wrap.py):
 * minimum feasible line count, then a cost DP balancing word count + width with an
 * orphan penalty. Used by the karaoke reveal; the settled hold uses greedy wrap. */
export function balancedWordWrapIndices(
  words: string[],
  measure: MeasureText,
  maxWidth: number,
): number[][] {
  const n = words.length;
  if (n === 0) return [];
  if (maxWidth <= 0) return words.map((_, i) => [i]);

  const widthCache = new Map<string, number>();
  const width = (start: number, end: number): number => {
    const key = `${start},${end}`;
    const cached = widthCache.get(key);
    if (cached !== undefined) return cached;
    const measured = measure(words.slice(start, end).join(" "));
    widthCache.set(key, measured);
    return measured;
  };
  const feasible = (start: number, end: number): boolean =>
    end === start + 1 || width(start, end) <= maxWidth;

  // Minimum line count, independent of balance scoring.
  const inf = n + 1;
  const minLinesTo: number[] = Array(n + 1).fill(inf);
  minLinesTo[0] = 0;
  for (let end = 1; end <= n; end++) {
    for (let start = 0; start < end; start++) {
      if (minLinesTo[start] !== inf && feasible(start, end)) {
        minLinesTo[end] = Math.min(minLinesTo[end], minLinesTo[start] + 1);
      }
    }
  }

  const lineCount = minLinesTo[n];
  if (lineCount === inf) return words.map((_, i) => [i]);
  if (lineCount === 1) return [words.map((_, i) => i)];

  const idealCount = n / lineCount;
  const segmentCost = (start: number, end: number): number => {
    const count = end - start;
    const segmentWidth = width(start, end);
    const slackRatio = Math.max(0, maxWidth - segmentWidth) / maxWidth;
    const countRatio = (count - idealCount) / Math.max(1, idealCount);
    const orphanPenalty = n > 3 && count === 1 ? 1000 : 0;
    return orphanPenalty + 8 * countRatio * countRatio + slackRatio * slackRatio;
  };

  // dp[end][linesUsed] = { cost, partition }
  const dp = new Map<string, { cost: number; partition: number[][] }>();
  dp.set("0,0", { cost: 0, partition: [] });
  for (let end = 1; end <= n; end++) {
    for (let linesUsed = 1; linesUsed <= Math.min(lineCount, end); linesUsed++) {
      let best: { cost: number; partition: number[][] } | null = null;
      for (let start = linesUsed - 1; start < end; start++) {
        const prev = dp.get(`${start},${linesUsed - 1}`);
        if (!prev || !feasible(start, end)) continue;
        const seg: number[] = [];
        for (let i = start; i < end; i++) seg.push(i);
        const cost = prev.cost + segmentCost(start, end);
        if (best === null || cost < best.cost) {
          best = { cost, partition: [...prev.partition, seg] };
        }
      }
      if (best !== null) dp.set(`${end},${linesUsed}`, best);
    }
  }

  const result = dp.get(`${n},${lineCount}`);
  return result ? result.partition : words.map((_, i) => [i]);
}

/** Wrap + iteratively shrink until every line fits — exact port of `_shrink_to_fit`:
 * ≤6 iterations, ×0.85 per step truncated toward zero (Python `int()`), 24px floor. */
export function shrinkToFit(
  text: string,
  measureAt: MeasureAtSize,
  initialSize: number,
  maxWidth: number,
): { sizePx: number; lines: string[] } {
  let size = initialSize;
  let lines = greedyWrapLines(text, measureAt(size), maxWidth);

  let iterations = 0;
  while (iterations < 6 && size > MIN_FONT_SIZE) {
    const measure = measureAt(size);
    const widest = lines.reduce((acc, ln) => Math.max(acc, measure(ln)), 0);
    if (widest <= maxWidth) break;
    size = Math.max(MIN_FONT_SIZE, Math.trunc(size * 0.85));
    lines = greedyWrapLines(text, measureAt(size), maxWidth);
    iterations++;
  }

  return { sizePx: size, lines };
}

/** Mirror of `_resolve_font_size_px`: explicit px wins, else size-class bucket,
 * floored at MIN_FONT_SIZE. */
export function resolveFontSizePx(p: IntroOverlayParams): number {
  if (p.textSizePx) return Math.max(MIN_FONT_SIZE, Math.trunc(p.textSizePx));
  const bucket = p.textSize ?? "medium";
  return Math.max(MIN_FONT_SIZE, FONT_SIZE_MAP[bucket] ?? 72);
}

/** Mirror of `_resolve_anchor`'s position resolution: explicit fracs win, else the
 * named-position map (default "center" → y 0.45); x defaults to 0.5. */
export function resolveAnchorFrac(p: IntroOverlayParams): { xFrac: number; yFrac: number } {
  const yFrac = p.positionYFrac ?? POSITION_Y_MAP[p.position ?? "center"] ?? 0.5;
  const xFrac = p.positionXFrac ?? 0.5;
  return { xFrac, yFrac };
}

/** The settled (post-reveal) fill color — mirror of the hold-overlay rule in
 * `build_persistent_intro_overlays`: karaoke sweeps every word to the highlight
 * color; every other effect settles on text_color. The preview shows the hold
 * state (what the text looks like for ~95% of the video). */
export function settledColor(p: IntroOverlayParams): string {
  return p.effect === "karaoke-line" ? p.highlightColor : p.textColor;
}

export interface IntroHoldLayout {
  lines: string[];
  sizePx: number;
  xFrac: number;
  yFrac: number;
  anchor: "left" | "right" | "center";
  color: string;
  strokeWidth: number;
}

/** Lay out the settled hold overlay exactly as `_draw_centered_text` would:
 * greedy wrap + shrink-to-fit at 90% canvas width, anchor + settled color. All
 * px values are at 1080×1920 canvas scale — the component scales to its box. */
export function layoutIntroHold(
  p: IntroOverlayParams,
  measureAt: MeasureAtSize,
): IntroHoldLayout | null {
  const text = (p.text ?? "").trim();
  if (!text) return null;

  const maxWidth = CANVAS_W * MAX_LINE_W_FRAC;
  const { sizePx, lines } = shrinkToFit(text, measureAt, resolveFontSizePx(p), maxWidth);
  const { xFrac, yFrac } = resolveAnchorFrac(p);

  return {
    lines,
    sizePx,
    xFrac,
    yFrac,
    anchor: p.textAnchor ?? "center",
    color: settledColor(p),
    strokeWidth: p.strokeWidth ?? 0,
  };
}

/** Block metrics mirror of `_measure_block`: line step = trunc(lineHeight × 1.15),
 * block height = step × (n−1) + trunc(lineHeight). `lineHeightPx` is the raw
 * ascent+descent of the face at `sizePx` (canvas fontBoundingBox in the browser). */
export function blockMetrics(
  lineCount: number,
  lineHeightPx: number,
): { lineStep: number; blockH: number } {
  const lineStep = Math.trunc(lineHeightPx * LINE_SPACING);
  const blockH = lineCount > 0 ? lineStep * (lineCount - 1) + Math.trunc(lineHeightPx) : 0;
  return { lineStep, blockH };
}

/** Vertical block top — mirror of `_vertical_block_top`: a left-anchored block
 * treats the y anchor as its TOP (grows downward); center/right center on it. */
export function verticalBlockTop(
  anchor: "left" | "right" | "center",
  cyPx: number,
  blockH: number,
): number {
  return anchor === "left" ? cyPx : cyPx - blockH / 2;
}

export { CANVAS_H, CANVAS_W };
