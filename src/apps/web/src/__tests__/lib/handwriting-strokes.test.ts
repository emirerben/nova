import {
  HANDWRITING_ASSET,
  handwritingPathD,
  handwritingStrokeLocalProgress,
  layoutHandwritingText,
} from "@/lib/handwriting-strokes";

describe("handwriting stroke font", () => {
  it("covers ASCII and Turkish text with authored paths", () => {
    for (const char of Array.from("AaZz09!? ÇçĞğİıÖöŞşÜü")) {
      expect(HANDWRITING_ASSET.glyphs[char]).toBeDefined();
      if (!/\s/.test(char)) {
        expect(HANDWRITING_ASSET.glyphs[char].paths.length).toBeGreaterThan(0);
      }
    }
  });

  it("wraps, tracks, and sequences strokes monotonically", () => {
    const layout = layoutHandwritingText("WRITE THIS NOW", {
      maxWidthEm: 4,
      letterSpacingEm: 0.04,
      lineSpacing: 1.4,
    });

    expect(layout.lines.length).toBeGreaterThanOrEqual(2);
    expect(layout.widthEm).toBeLessThanOrEqual(4.000001);
    expect(layout.lineStepEm).toBeGreaterThan(layout.ascentEm);
    expect(layout.strokes.length).toBeGreaterThan(10);
    for (let index = 0; index < layout.strokes.length - 1; index += 1) {
      expect(layout.strokes[index].endProgress).toBeLessThanOrEqual(
        layout.strokes[index + 1].startProgress,
      );
    }
    expect(layout.strokes.at(-1)?.endProgress).toBeLessThan(1);
  });

  it("maps shared progress to a partial SVG path", () => {
    const stroke = layoutHandwritingText("A", { maxWidthEm: 8 }).strokes[0];
    const midpoint = (stroke.startProgress + stroke.endProgress) / 2;

    expect(handwritingStrokeLocalProgress(stroke, stroke.startProgress)).toBe(0);
    expect(handwritingStrokeLocalProgress(stroke, midpoint)).toBeCloseTo(0.5);
    expect(handwritingStrokeLocalProgress(stroke, stroke.endProgress)).toBe(1);
    expect(handwritingPathD(stroke.points)).toMatch(/^M/);
    expect(handwritingPathD(stroke.points)).toContain("L");
  });
});
