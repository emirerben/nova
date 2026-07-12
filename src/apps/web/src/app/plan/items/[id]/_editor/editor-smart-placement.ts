import type { PlanItemVariant, TextPlacementCandidate } from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

const DEFAULT_SMART_PLACE: TextPlacementCandidate = {
  source: "editor_fallback",
  x_frac: 0.5,
  y_frac: 0.18,
  max_width_frac: 0.72,
  confidence: 0.35,
};

const CANVAS_W = 1080;
const CANVAS_H = 1920;
const MASONRY_MAX_DURATION_S = 15;
const MASONRY_PLACEMENT_SAMPLE_COUNT = 7;
const MASONRY_PLACEMENT_MARGIN_PX = 42;
const MASONRY_PLACEMENT_FRAME_MARGIN_PX = 36;
const MASONRY_PLACEMENT_MIN_WIDTH_FRAC = 0.2;
const MASONRY_PLACEMENT_MIN_HEIGHT_FRAC = 0.055;

const MASONRY_LAYOUT: Array<[number, number, number, number]> = [
  [34, 46, 270, 480],
  [334, 28, 420, 250],
  [784, 64, 285, 500],
  [1098, 24, 440, 264],
  [1568, 74, 265, 472],
  [26, 568, 420, 244],
  [474, 330, 280, 498],
  [784, 600, 410, 250],
  [1224, 330, 292, 520],
  [1548, 600, 430, 248],
  [48, 850, 270, 480],
  [348, 872, 430, 260],
  [808, 892, 270, 480],
  [1110, 904, 420, 250],
  [1560, 886, 285, 506],
  [28, 1372, 430, 254],
  [488, 1412, 284, 474],
  [804, 1414, 424, 254],
];

type Rect = [number, number, number, number];

function isMasonryVariant(variant: PlanItemVariant | null | undefined): boolean {
  return (
    variant?.montage_preset === "masonry" ||
    variant?.montage_preset_rendered === "masonry"
  );
}

function clampDuration(durationS: number): number {
  if (!Number.isFinite(durationS) || durationS <= 0) return MASONRY_MAX_DURATION_S;
  return Math.max(0.1, Math.min(MASONRY_MAX_DURATION_S, durationS));
}

function masonryWhitespaceCandidates({
  durationS = MASONRY_MAX_DURATION_S,
  revealWindowS = 4,
  maxCandidates = 3,
}: {
  durationS?: number;
  revealWindowS?: number;
  maxCandidates?: number;
} = {}): TextPlacementCandidate[] {
  const boardWidth = Math.max(...MASONRY_LAYOUT.map(([x, _y, w]) => x + w)) + 34;
  const panPx = Math.max(0, boardWidth - CANVAS_W);
  const duration = clampDuration(durationS);
  const windowS = Math.max(0.1, Math.min(revealWindowS, duration));
  const samples = Array.from({ length: MASONRY_PLACEMENT_SAMPLE_COUNT }, (_unused, idx) =>
    (windowS * idx) / (MASONRY_PLACEMENT_SAMPLE_COUNT - 1),
  );

  const obstacles: Rect[] = [];
  for (const t of samples) {
    const progress = Math.max(0, Math.min(1, t / duration));
    const scroll = panPx * progress;
    for (const [x, y, w, h] of MASONRY_LAYOUT) {
      const left = x - scroll - MASONRY_PLACEMENT_MARGIN_PX;
      const top = y - MASONRY_PLACEMENT_MARGIN_PX;
      const right = x - scroll + w + MASONRY_PLACEMENT_MARGIN_PX;
      const bottom = y + h + MASONRY_PLACEMENT_MARGIN_PX;
      if (right <= 0 || left >= CANVAS_W || bottom <= 0 || top >= CANVAS_H) continue;
      obstacles.push([
        Math.max(0, left),
        Math.max(0, top),
        Math.min(CANVAS_W, right),
        Math.min(CANVAS_H, bottom),
      ]);
    }
  }

  return largestEmptyRects(obstacles, maxCandidates).map(([score, rect]) => {
    const [left, top, right, bottom] = rect;
    const width = right - left;
    const height = bottom - top;
    const yCenter = Math.min(
      bottom - Math.min(height * 0.28, CANVAS_H * 0.035),
      (top + bottom) / 2,
    );
    const areaRatio = (width * height) / Math.max(1, CANVAS_W * CANVAS_H);
    return {
      source: "editor_fallback_masonry",
      x_frac: round4((left + right) / 2 / CANVAS_W),
      y_frac: round4(Math.max(0.12, Math.min(0.9, yCenter / CANVAS_H))),
      max_width_frac: round4(
        Math.max(
          MASONRY_PLACEMENT_MIN_WIDTH_FRAC,
          Math.min(0.9, (width / CANVAS_W) * 0.92),
        ),
      ),
      confidence: round3(
        Math.max(0.35, Math.min(0.98, 0.55 + areaRatio * 8 + score * 0.08)),
      ),
    };
  });
}

function largestEmptyRects(obstacles: Rect[], maxRects: number): Array<[number, Rect]> {
  const safeLeft = MASONRY_PLACEMENT_FRAME_MARGIN_PX;
  const safeTop = MASONRY_PLACEMENT_FRAME_MARGIN_PX;
  const safeRight = CANVAS_W - MASONRY_PLACEMENT_FRAME_MARGIN_PX;
  const safeBottom = CANVAS_H - MASONRY_PLACEMENT_FRAME_MARGIN_PX;
  const clipped = obstacles
    .map(([left, top, right, bottom]): Rect => [
      Math.max(safeLeft, Math.min(safeRight, left)),
      Math.max(safeTop, Math.min(safeBottom, top)),
      Math.max(safeLeft, Math.min(safeRight, right)),
      Math.max(safeTop, Math.min(safeBottom, bottom)),
    ])
    .filter(([left, top, right, bottom]) => right > left && bottom > top);

  const xEdges = Array.from(
    new Set([
      safeLeft,
      safeRight,
      ...clipped.flatMap(([left, _top, right]) => [left, right]),
    ]),
  ).sort((a, b) => a - b);
  const yEdges = Array.from(
    new Set([
      safeTop,
      safeBottom,
      ...clipped.flatMap(([_left, top, _right, bottom]) => [top, bottom]),
    ]),
  ).sort((a, b) => a - b);
  const cols = xEdges.length - 1;
  const rows = yEdges.length - 1;
  if (cols <= 0 || rows <= 0) return [];

  const free = Array.from({ length: rows }, () =>
    Array.from({ length: cols }, () => true),
  );
  for (const [left, top, right, bottom] of clipped) {
    for (let yIdx = 0; yIdx < rows; yIdx += 1) {
      if (yEdges[yIdx] >= bottom || yEdges[yIdx + 1] <= top) continue;
      for (let xIdx = 0; xIdx < cols; xIdx += 1) {
        if (xEdges[xIdx] >= right || xEdges[xIdx + 1] <= left) continue;
        free[yIdx][xIdx] = false;
      }
    }
  }

  const xPrefix = [0];
  for (let xIdx = 0; xIdx < cols; xIdx += 1) {
    xPrefix.push(xPrefix[xPrefix.length - 1] + xEdges[xIdx + 1] - xEdges[xIdx]);
  }

  const minWidth = CANVAS_W * MASONRY_PLACEMENT_MIN_WIDTH_FRAC;
  const minHeight = CANVAS_H * MASONRY_PLACEMENT_MIN_HEIGHT_FRAC;
  const heights = Array.from({ length: cols }, () => 0);
  const scored: Array<[number, Rect]> = [];

  for (let yIdx = 0; yIdx < rows; yIdx += 1) {
    const rowH = yEdges[yIdx + 1] - yEdges[yIdx];
    for (let xIdx = 0; xIdx < cols; xIdx += 1) {
      heights[xIdx] = free[yIdx][xIdx] ? heights[xIdx] + rowH : 0;
    }

    const stack: number[] = [];
    for (let scanIdx = 0; scanIdx <= cols; scanIdx += 1) {
      const currentH = scanIdx < cols ? heights[scanIdx] : 0;
      while (stack.length > 0 && currentH < heights[stack[stack.length - 1]]) {
        const height = heights[stack.pop() as number];
        const leftIdx = stack.length > 0 ? stack[stack.length - 1] + 1 : 0;
        const rightIdx = scanIdx;
        const width = xPrefix[rightIdx] - xPrefix[leftIdx];
        if (width >= minWidth && height >= minHeight) {
          const bottom = yEdges[yIdx + 1];
          const top = bottom - height;
          const rect: Rect = [xEdges[leftIdx], top, xEdges[rightIdx], bottom];
          const centerX = (rect[0] + rect[2]) / 2 / CANVAS_W;
          const sideBias = Math.abs(centerX - 0.5) * 0.18;
          scored.push([(width * height) / (CANVAS_W * CANVAS_H) + sideBias, rect]);
        }
      }
      stack.push(scanIdx);
    }
  }

  scored.sort((a, b) => b[0] - a[0]);
  const selected: Array<[number, Rect]> = [];
  for (const candidate of scored) {
    if (selected.some(([_score, rect]) => rectIou(candidate[1], rect) > 0.72)) continue;
    selected.push(candidate);
    if (selected.length >= maxRects) break;
  }
  return selected;
}

function rectIou(a: Rect, b: Rect): number {
  const interW = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0]));
  const interH = Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
  const inter = interW * interH;
  if (inter <= 0) return 0;
  const areaA = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
  const areaB = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
  return inter / Math.max(1, areaA + areaB - inter);
}

function round4(value: number): number {
  return Math.round(value * 10000) / 10000;
}

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

export function resolveSmartPlacementCandidate(
  variant: PlanItemVariant | null | undefined,
  selectedBar: TextElementBar | null | undefined,
): TextPlacementCandidate | null {
  if (!selectedBar) return null;
  const serverCandidate = variant?.text_placement_candidates?.[0] ?? null;
  if (serverCandidate) return serverCandidate;
  if (isMasonryVariant(variant)) return masonryWhitespaceCandidates()[0] ?? null;
  return DEFAULT_SMART_PLACE;
}

export function reflowTextForSmartPlacement(
  text: string,
  candidate: TextPlacementCandidate,
): string {
  if (candidate.max_width_frac > 0.36 || text.includes("\n")) return text;
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length < 4) return text;
  const targetLines =
    candidate.max_width_frac <= 0.22 && words.length >= 5
      ? Math.min(3, words.length)
      : Math.min(2, words.length);
  const lines = balancedLines(words, targetLines);
  return lines.length > 1 ? lines.join("\n") : text;
}

function balancedLines(words: string[], lineCount: number): string[] {
  const n = words.length;
  const lines = Math.max(1, Math.min(lineCount, n));
  if (lines === 1) return [words.join(" ")];
  const lengths = Array.from({ length: n + 1 }, () =>
    Array.from({ length: n + 1 }, () => 0),
  );
  for (let start = 0; start < n; start += 1) {
    let len = 0;
    for (let end = start + 1; end <= n; end += 1) {
      len += words[end - 1].length + (end - start > 1 ? 1 : 0);
      lengths[start][end] = len;
    }
  }

  const totalLen = lengths[0][n];
  const idealLen = totalLen / lines;
  const memo = new Map<string, { cost: number; breaks: number[] }>();
  const solve = (
    start: number,
    remaining: number,
  ): { cost: number; breaks: number[] } => {
    const key = `${start}:${remaining}`;
    const cached = memo.get(key);
    if (cached) return cached;
    if (remaining === 1) {
      const count = n - start;
      const orphan = count === 1 && n > 3 ? 1000 : 0;
      return { cost: Math.abs(lengths[start][n] - idealLen) + orphan, breaks: [n] };
    }
    let best: { cost: number; breaks: number[] } | null = null;
    for (let end = start + 1; end <= n - remaining + 1; end += 1) {
      const count = end - start;
      const orphan = count === 1 && n > 3 ? 1000 : 0;
      const rest = solve(end, remaining - 1);
      const cost = Math.abs(lengths[start][end] - idealLen) + orphan + rest.cost;
      if (!best || cost < best.cost) best = { cost, breaks: [end, ...rest.breaks] };
    }
    const result = best ?? { cost: 0, breaks: [n] };
    memo.set(key, result);
    return result;
  };

  const breaks = solve(0, lines).breaks;
  let start = 0;
  return breaks.map((end) => {
    const line = words.slice(start, end).join(" ");
    start = end;
    return line;
  });
}
