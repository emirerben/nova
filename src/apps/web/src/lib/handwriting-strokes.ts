import handwritingAssetJson from "@/data/handwriting-strokes.json";

type Glyph = {
  advance: number;
  paths: number[][][];
};

type HandwritingAsset = {
  version: number;
  source: string;
  units_per_em: number;
  ascent: number;
  descent: number;
  stroke_width: number;
  glyphs: Record<string, Glyph>;
};

export type HandwritingStroke = {
  points: Array<[number, number]>;
  startProgress: number;
  endProgress: number;
  lineIndex: number;
  glyphIndex: number;
};

export type HandwritingLayout = {
  strokes: HandwritingStroke[];
  lines: string[];
  lineWidthsEm: number[];
  widthEm: number;
  heightEm: number;
  lineStepEm: number;
  ascentEm: number;
  descentEm: number;
  strokeWidthEm: number;
};

export const HANDWRITING_ASSET = handwritingAssetJson as HandwritingAsset;

const BASE_TRACKING_EM = 0.035;
const CHARACTER_PAUSE_EM = 0.055;
const SPACE_PAUSE_EM = 0.14;
const MIN_STROKE_WEIGHT_EM = 0.045;

function glyphFor(char: string): Glyph {
  return (
    HANDWRITING_ASSET.glyphs[char] ??
    HANDWRITING_ASSET.glyphs["?"] ?? { advance: 0.5, paths: [] }
  );
}

export function handwritingGlyphAdvanceEm(
  char: string,
  letterSpacingEm = 0,
): number {
  return glyphFor(char).advance + BASE_TRACKING_EM + letterSpacingEm;
}

export function measureHandwritingLineEm(
  line: string,
  letterSpacingEm = 0,
): number {
  const chars = Array.from(line);
  if (chars.length === 0) return 0;
  const width = chars.reduce(
    (sum, char) => sum + handwritingGlyphAdvanceEm(char, letterSpacingEm),
    0,
  );
  return Math.max(0, width - (BASE_TRACKING_EM + letterSpacingEm));
}

function breakLongToken(
  token: string,
  maxWidthEm: number,
  letterSpacingEm: number,
): string[] {
  const pieces: string[] = [];
  let current = "";
  for (const char of Array.from(token)) {
    const candidate = current + char;
    if (
      current &&
      measureHandwritingLineEm(candidate, letterSpacingEm) > maxWidthEm
    ) {
      pieces.push(current);
      current = char;
    } else {
      current = candidate;
    }
  }
  if (current) pieces.push(current);
  return pieces.length > 0 ? pieces : [""];
}

export function wrapHandwritingText(
  text: string,
  maxWidthEm: number,
  letterSpacingEm = 0,
): string[] {
  const safeMaxWidth = Math.max(0.1, maxWidthEm);
  const output: string[] = [];
  const logicalLines = text.replace(/\r\n?/g, "\n").split("\n");
  for (const rawLine of logicalLines) {
    if (rawLine === "") {
      output.push("");
      continue;
    }
    const words = rawLine.match(/\S+/g) ?? [];
    let current = "";
    for (const word of words) {
      const candidate = current ? `${current} ${word}` : word;
      if (
        measureHandwritingLineEm(candidate, letterSpacingEm) <= safeMaxWidth
      ) {
        current = candidate;
        continue;
      }
      if (current) {
        output.push(current);
        current = "";
      }
      if (measureHandwritingLineEm(word, letterSpacingEm) <= safeMaxWidth) {
        current = word;
        continue;
      }
      const pieces = breakLongToken(word, safeMaxWidth, letterSpacingEm);
      output.push(...pieces.slice(0, -1));
      current = pieces.at(-1) ?? "";
    }
    output.push(current);
  }
  return output.length > 0 ? output : [""];
}

function polylineLength(points: Array<[number, number]>): number {
  let total = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    total += Math.hypot(
      points[index + 1][0] - points[index][0],
      points[index + 1][1] - points[index][1],
    );
  }
  return total;
}

export function layoutHandwritingText(
  text: string,
  {
    maxWidthEm,
    letterSpacingEm = 0,
    lineSpacing = 1.15,
  }: {
    maxWidthEm: number;
    letterSpacingEm?: number;
    lineSpacing?: number;
  },
): HandwritingLayout {
  const { ascent, descent, stroke_width: strokeWidth } = HANDWRITING_ASSET;
  const lines = wrapHandwritingText(text, maxWidthEm, letterSpacingEm);
  const lineWidthsEm = lines.map((line) =>
    measureHandwritingLineEm(line, letterSpacingEm),
  );
  const lineStepEm = (ascent + descent) * Math.max(0.5, lineSpacing);
  const heightEm =
    ascent + descent + lineStepEm * Math.max(0, lines.length - 1);

  const raw: Array<{
    points: Array<[number, number]>;
    length: number;
    lineIndex: number;
    glyphIndex: number;
  }> = [];
  let totalWeight = 0;

  lines.forEach((line, lineIndex) => {
    let x = 0;
    const baselineY = ascent + lineIndex * lineStepEm;
    Array.from(line).forEach((char, glyphIndex) => {
      const glyph = glyphFor(char);
      if (/\s/.test(char)) {
        totalWeight += SPACE_PAUSE_EM;
        x += handwritingGlyphAdvanceEm(char, letterSpacingEm);
        return;
      }
      glyph.paths.forEach((rawPath) => {
        const points = rawPath.map(
          (point) =>
            [x + Number(point[0]), baselineY + Number(point[1])] as [
              number,
              number,
            ],
        );
        if (points.length < 2) return;
        const length = Math.max(MIN_STROKE_WEIGHT_EM, polylineLength(points));
        raw.push({ points, length, lineIndex, glyphIndex });
        totalWeight += length;
      });
      totalWeight += CHARACTER_PAUSE_EM;
      x += handwritingGlyphAdvanceEm(char, letterSpacingEm);
    });
    if (lineIndex < lines.length - 1) {
      totalWeight += SPACE_PAUSE_EM * 1.5;
    }
  });

  const safeTotal = Math.max(totalWeight, 1e-6);
  let cursorWeight = 0;
  let rawIndex = 0;
  const strokes: HandwritingStroke[] = [];

  lines.forEach((line, lineIndex) => {
    Array.from(line).forEach((char) => {
      const glyph = glyphFor(char);
      if (/\s/.test(char)) {
        cursorWeight += SPACE_PAUSE_EM;
        return;
      }
      glyph.paths.forEach(() => {
        const item = raw[rawIndex];
        rawIndex += 1;
        if (!item) return;
        const startProgress = cursorWeight / safeTotal;
        cursorWeight += item.length;
        strokes.push({
          points: item.points,
          startProgress,
          endProgress: cursorWeight / safeTotal,
          lineIndex: item.lineIndex,
          glyphIndex: item.glyphIndex,
        });
      });
      cursorWeight += CHARACTER_PAUSE_EM;
    });
    if (lineIndex < lines.length - 1) {
      cursorWeight += SPACE_PAUSE_EM * 1.5;
    }
  });

  return {
    strokes,
    lines,
    lineWidthsEm,
    widthEm: Math.max(0, ...lineWidthsEm),
    heightEm,
    lineStepEm,
    ascentEm: ascent,
    descentEm: descent,
    strokeWidthEm: strokeWidth,
  };
}

export function handwritingStrokeLocalProgress(
  stroke: HandwritingStroke,
  revealProgress: number,
): number {
  if (revealProgress <= stroke.startProgress) return 0;
  if (revealProgress >= stroke.endProgress) return 1;
  const span = Math.max(1e-9, stroke.endProgress - stroke.startProgress);
  return (revealProgress - stroke.startProgress) / span;
}

export function handwritingPathD(
  points: Array<[number, number]>,
): string {
  if (points.length === 0) return "";
  return points
    .map(([x, y], index) => `${index === 0 ? "M" : "L"}${x} ${y}`)
    .join(" ");
}
