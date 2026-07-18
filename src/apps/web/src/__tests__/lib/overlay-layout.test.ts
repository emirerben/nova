/**
 * Parity tests for the TS port of the server's intro-overlay layout math.
 *
 * Mirrors the algorithms in src/apps/api/app/pipeline/text_overlay_skia.py
 * (_wrap_text_to_lines, _shrink_to_fit, _measure_block) and text_wrap.py
 * (balanced_word_wrap_indices). Measurement is a fake monospace — width
 * scales linearly with text length and font size — so expectations are
 * hand-computable and identical to what the Python code produces on the
 * same inputs.
 */

import {
  balancedWordWrapIndices,
  blockMetrics,
  greedyWrapLines,
  layoutIntroHold,
  resolveAnchorFrac,
  resolveFontSizePx,
  resolveTextElementsLayout,
  settledColor,
  shrinkToFit,
  verticalBlockTop,
  LINE_SPACING,
  MAX_LINE_W_FRAC,
  MIN_FONT_SIZE,
  type IntroOverlayParams,
  type MeasureAtSize,
  type MeasureText,
} from "@/lib/overlay-layout";
import { CANVAS_H, CANVAS_W } from "@/lib/overlay-constants";
import type { TextElement } from "@/lib/plan-api";

// Fake monospace: every char is (size × 0.5) px wide — same shape as the
// Python tests' stub measure, so wrap decisions match exactly.
const mono =
  (size: number): MeasureText =>
  (text: string) =>
    text.length * size * 0.5;
const monoAt: MeasureAtSize = (size) => mono(size);

const baseParams: IntroOverlayParams = {
  text: "hello world",
  effect: "karaoke-line",
  textColor: "#FFFFFF",
  highlightColor: "#FFD24A",
  fontFamily: "Playfair Display",
  textSizePx: 60,
  position: "center",
  positionXFrac: null,
  positionYFrac: null,
  textAnchor: "center",
  strokeWidth: 0,
};

describe("greedyWrapLines", () => {
  it("keeps text on one line when it fits", () => {
    expect(greedyWrapLines("hello world", mono(20), 1000)).toEqual(["hello world"]);
  });

  it("wraps greedily at maxWidth", () => {
    // size 20 → 10px/char. "aaa bbb ccc" → "aaa bbb"=70px fits in 75, +ccc=110 no.
    expect(greedyWrapLines("aaa bbb ccc", mono(20), 75)).toEqual(["aaa bbb", "ccc"]);
  });

  it("keeps an overlong single word on its own line", () => {
    expect(greedyWrapLines("supercalifragilistic", mono(20), 50)).toEqual([
      "supercalifragilistic",
    ]);
  });

  it("wraps each explicit-newline segment separately", () => {
    expect(greedyWrapLines("aaa bbb\nccc", mono(20), 75)).toEqual(["aaa bbb", "ccc"]);
    expect(greedyWrapLines("a\n\nb", mono(20), 1000)).toEqual(["a", "", "b"]);
  });
});

describe("balancedWordWrapIndices", () => {
  it("returns [] for no words and one-per-line for non-positive width", () => {
    expect(balancedWordWrapIndices([], mono(20), 100)).toEqual([]);
    expect(balancedWordWrapIndices(["a", "b"], mono(20), 0)).toEqual([[0], [1]]);
  });

  it("keeps everything on one line when feasible", () => {
    expect(balancedWordWrapIndices(["a", "b", "c"], mono(20), 1000)).toEqual([[0, 1, 2]]);
  });

  it("avoids an orphan word where greedy would leave one", () => {
    // 8 words of 3 chars @10px/char: greedy fills 7 on line 1 ("aaa ..."×7=310>...)
    // Use width that fits 7 words (3×7+6 spaces=27 chars=270px) → greedy = 7+1 orphan.
    const words = Array(8).fill("aaa");
    const lines = balancedWordWrapIndices(words, mono(20), 270);
    // Balanced result must not end with a single-word line (orphan penalty 1000).
    expect(lines.length).toBe(2);
    expect(lines[lines.length - 1].length).toBeGreaterThan(1);
    // All indices preserved in order.
    expect(lines.flat()).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  });

  it("keeps a single overlong word feasible", () => {
    expect(balancedWordWrapIndices(["aaaaaaaaaa"], mono(20), 10)).toEqual([[0]]);
  });
});

describe("shrinkToFit", () => {
  it("does not shrink when text fits", () => {
    const { sizePx, lines } = shrinkToFit("hi", monoAt, 80, 1000);
    expect(sizePx).toBe(80);
    expect(lines).toEqual(["hi"]);
  });

  it("shrinks by ×0.85 truncated (Python int()) until fitting", () => {
    // Single word, 10 chars: width = 10 × size × 0.5 = 5×size. Fits when 5×size ≤ 300
    // → size ≤ 60. From 80: 80→68→57 (int(68*.85)=57.8→57); 57×5=285 ≤ 300. Stops.
    const { sizePx } = shrinkToFit("aaaaaaaaaa", monoAt, 80, 300);
    expect(sizePx).toBe(57);
  });

  it("caps at 6 iterations even when still overflowing (80 → 28)", () => {
    // 80→68→57→48→40→34→28: six shrinks, then stop — exactly Python's loop cap.
    const { sizePx } = shrinkToFit("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", monoAt, 80, 10);
    expect(sizePx).toBe(28);
  });

  it("floors at MIN_FONT_SIZE and stops shrinking there", () => {
    // 30→25→24(floor): loop exits when size is no longer > MIN_FONT_SIZE.
    const { sizePx } = shrinkToFit("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", monoAt, 30, 10);
    expect(sizePx).toBe(MIN_FONT_SIZE);
  });

  it("matches the exact Python truncation sequence", () => {
    // int(x*0.85): 80→68→57→48→40→34→28
    const seq: number[] = [];
    let s = 80;
    for (let i = 0; i < 6; i++) {
      s = Math.max(MIN_FONT_SIZE, Math.trunc(s * 0.85));
      seq.push(s);
    }
    expect(seq).toEqual([68, 57, 48, 40, 34, 28]);
  });
});

describe("param resolution", () => {
  it("explicit px wins over bucket; bucket falls back to FONT_SIZE_MAP", () => {
    expect(resolveFontSizePx(baseParams)).toBe(60);
    expect(resolveFontSizePx({ ...baseParams, textSizePx: null, textSize: "small" })).toBe(36);
    expect(resolveFontSizePx({ ...baseParams, textSizePx: null, textSize: null })).toBe(72);
  });

  it("resolves center position to y 0.45 and defaults x to 0.5", () => {
    expect(resolveAnchorFrac(baseParams)).toEqual({ xFrac: 0.5, yFrac: 0.45 });
  });

  it("explicit fracs win over the named position", () => {
    expect(
      resolveAnchorFrac({ ...baseParams, positionXFrac: 0.06, positionYFrac: 0.2 }),
    ).toEqual({ xFrac: 0.06, yFrac: 0.2 });
  });

  it("karaoke settles to highlight color, others to text color", () => {
    expect(settledColor(baseParams)).toBe("#FFD24A");
    expect(settledColor({ ...baseParams, effect: "fade-in" })).toBe("#FFFFFF");
  });
});

describe("layoutIntroHold", () => {
  it("returns null for empty text", () => {
    expect(layoutIntroHold({ ...baseParams, text: "  " }, monoAt)).toBeNull();
  });

  it("lays out at 90% canvas width with settled color + anchor", () => {
    const layout = layoutIntroHold(baseParams, monoAt);
    expect(layout).not.toBeNull();
    expect(layout!.sizePx).toBe(60);
    expect(layout!.lines).toEqual(["hello world"]);
    expect(layout!.color).toBe("#FFD24A");
    expect(layout!.anchor).toBe("center");
    expect(layout!.xFrac).toBe(0.5);
    expect(layout!.yFrac).toBe(0.45);
    // maxWidth constant sanity: 0.9 × 1080 = 972
    expect(CANVAS_W * MAX_LINE_W_FRAC).toBe(972);
  });
});

describe("resolveTextElementsLayout canvas dimensions", () => {
  const element: TextElement = {
    id: "txt-landscape",
    text: "Landscape title",
    role: "generative_intro",
    start_s: 0,
    end_s: 3,
    x_frac: 0.25,
    y_frac: 0.75,
    max_width_frac: 0.5,
    size_px: 72,
  };

  it("keeps default callers on portrait canvas math", () => {
    const [defaultLayout] = resolveTextElementsLayout([element]);
    const [portraitLayout] = resolveTextElementsLayout(
      [element],
      { w: CANVAS_W, h: CANVAS_H },
    );

    expect(defaultLayout).toEqual(portraitLayout);
    expect(defaultLayout.xPx).toBe(270);
    expect(defaultLayout.yPx).toBe(1440);
    expect(defaultLayout.maxWidthPx).toBe(540);
  });

  it("scales fraction-to-pixel layout against a landscape canvas", () => {
    const [layout] = resolveTextElementsLayout([element], { w: 1920, h: 1080 });

    expect(layout.xFrac).toBe(0.25);
    expect(layout.yFrac).toBe(0.75);
    expect(layout.xPx).toBe(480);
    expect(layout.yPx).toBe(810);
    expect(layout.maxWidthPx).toBe(960);
  });
});

describe("block metrics + vertical anchoring", () => {
  it("mirrors _measure_block: step = trunc(h × 1.15), block = step×(n−1)+trunc(h)", () => {
    const { lineStep, blockH } = blockMetrics(3, 69.4);
    expect(lineStep).toBe(Math.trunc(69.4 * LINE_SPACING)); // 79
    expect(blockH).toBe(79 * 2 + 69);
  });

  it("left anchor pins block top at cy; center/right center on cy", () => {
    expect(verticalBlockTop("left", 900, 200)).toBe(900);
    expect(verticalBlockTop("center", 900, 200)).toBe(800);
    expect(verticalBlockTop("right", 900, 200)).toBe(800);
  });
});
