import type { PlanItemVariant, TextPlacementCandidate } from "@/lib/plan-api";
import {
  resolveTextElementYFrac,
  xFracForTextBoxPosition,
  type TextBoxHorizontalPosition,
  type TextHorizontalAlignment,
} from "@/lib/overlay-layout";
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
const MASONRY_PLACEMENT_FRAME_MARGIN_PX = 36;
const MASONRY_PLACEMENT_MIN_WIDTH_FRAC = 0.2;
const MASONRY_PLACEMENT_MIN_HEIGHT_FRAC = 0.055;
const SMART_PLACEMENT_MIN_SIZE_PX = 40;

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
const MASONRY_BOARD_RIGHT_PADDING_PX = 34;
const POLAROID_LAYOUT: Array<[number, number, number, number]> = [
  [27, 77, 248, 442],
  [885, 450, 454, 270],
  [922, 754, 274, 480],
  [1000, 24, 616, 370],
  [1644, 60, 292, 519],
  [632, 1391, 395, 229],
  [389, 162, 462, 822],
  [1236, 1163, 439, 268],
  [1395, 658, 266, 473],
  [1689, 658, 482, 278],
  [44, 562, 238, 422],
  [285, 1012, 580, 351],
  [1073, 1514, 284, 504],
  [1416, 1570, 399, 238],
  [1740, 984, 308, 546],
  [24, 1391, 580, 343],
  [2076, 1103, 256, 427],
  [1843, 1570, 475, 284],
];
const POLAROID_ROTATIONS = [
  -2.6, 1.7, -1.4, 2.1, -3, 1.2, -2.4, 2.8, -1.8, 1.5, 3.2, -1.9, 2.4, -2.2,
  1.1, 2.6, -1.6, 2,
];

type Rect = [number, number, number, number];
type CollagePreset = "masonry" | "polaroid_wall";
type CollagePlacementConfig = {
  layout: Array<[number, number, number, number]>;
  rotations: number[];
  placementMarginPx: number;
  source: string;
  boardWidth: number;
};

function collagePresetForVariant(
  variant: PlanItemVariant | null | undefined,
): CollagePreset | null {
  const preset = variant?.montage_preset_rendered ?? variant?.montage_preset;
  return preset === "masonry" || preset === "polaroid_wall" ? preset : null;
}

function collagePlacementConfig(preset: CollagePreset): CollagePlacementConfig {
  const layout = preset === "polaroid_wall" ? POLAROID_LAYOUT : MASONRY_LAYOUT;
  return {
    layout,
    rotations: preset === "polaroid_wall" ? POLAROID_ROTATIONS : [],
    placementMarginPx: preset === "polaroid_wall" ? 72 : 42,
    source: preset === "polaroid_wall" ? "polaroid_wall_whitespace" : "masonry_whitespace",
    boardWidth:
      Math.max(...layout.map(([x, _y, width]) => x + width)) +
      MASONRY_BOARD_RIGHT_PADDING_PX,
  };
}

export function isMasonryVariant(variant: PlanItemVariant | null | undefined): boolean {
  // Both presets use the masonry moving-board render pipeline.
  return collagePresetForVariant(variant) !== null;
}

function clampDuration(durationS: number): number {
  if (!Number.isFinite(durationS) || durationS <= 0) return MASONRY_MAX_DURATION_S;
  return Math.max(0.1, Math.min(MASONRY_MAX_DURATION_S, durationS));
}

export function masonryMotionForDuration(
  durationS: number,
  boardWidth = collagePlacementConfig("masonry").boardWidth,
): Record<string, unknown> {
  const resolvedBoardWidth =
    Number.isFinite(boardWidth) && boardWidth >= CANVAS_W ? boardWidth : CANVAS_W;
  return {
    mode: "masonry_pan_x",
    duration_s: clampDuration(durationS),
    pan_px: Math.max(0, resolvedBoardWidth - CANVAS_W),
    board_width_px: resolvedBoardWidth,
    frame_width_px: CANVAS_W,
  };
}

export function collageMotionForVariant(
  variant: PlanItemVariant | null | undefined,
  durationS: number,
): Record<string, unknown> | null {
  const preset = collagePresetForVariant(variant);
  return preset
    ? masonryMotionForDuration(durationS, collagePlacementConfig(preset).boardWidth)
    : null;
}

function candidateFromRect({
  left,
  top,
  width,
  height,
  durationS,
  rotationDeg = 0,
  maxWidthFrac,
  confidence,
  source = "masonry_whitespace",
  boardWidth = collagePlacementConfig("masonry").boardWidth,
}: {
  left: number;
  top: number;
  width: number;
  height: number;
  durationS: number;
  rotationDeg?: number;
  maxWidthFrac?: number;
  confidence: number;
  source?: string;
  boardWidth?: number;
}): TextPlacementCandidate {
  const centerX = left + width / 2;
  const layerOriginPx =
    left >= 0 && left + width <= CANVAS_W
      ? 0
      : Math.max(0, Math.min(centerX - CANVAS_W / 2, boardWidth - CANVAS_W));
  const resolvedMaxWidth =
    maxWidthFrac ??
    (rotationDeg
      ? Math.min(0.82, Math.max(0.36, (height / CANVAS_W) * 0.86))
      : Math.max(MASONRY_PLACEMENT_MIN_WIDTH_FRAC, Math.min(0.9, (width / CANVAS_W) * 0.92)));
  return {
    source,
    x_frac: round4((centerX - layerOriginPx) / CANVAS_W),
    y_frac: round4((top + height / 2) / CANVAS_H),
    max_width_frac: round4(Math.max(0.2, Math.min(0.9, resolvedMaxWidth))),
    rotation_deg: rotationDeg,
    confidence: round3(Math.max(0, Math.min(0.98, confidence))),
    masonry_motion: {
      ...masonryMotionForDuration(durationS, boardWidth),
      layer_origin_px: round3(layerOriginPx),
      pocket_left_px: round3(left),
      pocket_top_px: round3(top),
      pocket_right_px: round3(left + width),
      pocket_bottom_px: round3(top + height),
    },
  };
}

export function masonryWhitespaceCandidates({
  durationS = MASONRY_MAX_DURATION_S,
  revealWindowS = 4,
  maxCandidates = 3,
  anchorTimeS,
  preset = "masonry",
}: {
  durationS?: number;
  revealWindowS?: number;
  maxCandidates?: number;
  anchorTimeS?: number;
  preset?: CollagePreset;
} = {}): TextPlacementCandidate[] {
  const duration = clampDuration(durationS);
  const wanted = Math.max(1, Math.floor(maxCandidates));
  const config = collagePlacementConfig(preset);
  const boardWidth = config.boardWidth;
  const panPx = Math.max(0, boardWidth - CANVAS_W);
  const windowS = Math.max(0.1, Math.min(revealWindowS, duration));
  const defaultAnchorS = windowS / 2;
  const anchorS = Math.max(
    0,
    Math.min(
      duration,
      Number.isFinite(anchorTimeS) ? (anchorTimeS as number) : defaultAnchorS,
    ),
  );
  const windowStartS = Math.min(
    Math.max(0, anchorS - windowS / 2),
    duration - windowS,
  );
  const samples = Array.from({ length: MASONRY_PLACEMENT_SAMPLE_COUNT }, (_unused, idx) =>
    windowStartS + (windowS * idx) / (MASONRY_PLACEMENT_SAMPLE_COUNT - 1),
  );
  const anchorScroll = (panPx * anchorS) / duration;

  const obstacles: Rect[] = config.layout.map(([x, y, w, h], index) => {
    const rotationDeg = config.rotations[index] ?? 0;
    const radians = Math.abs(rotationDeg) * Math.PI / 180;
    const rotatedWidth = Math.abs(rotationDeg) < 0.001
      ? w
      : Math.ceil(w * Math.cos(radians) + h * Math.sin(radians));
    const rotatedHeight = Math.abs(rotationDeg) < 0.001
      ? h
      : Math.ceil(w * Math.sin(radians) + h * Math.cos(radians));
    const visualLeft = x - (rotatedWidth - w) / 2;
    const visualTop = y - (rotatedHeight - h) / 2;
    return [
      visualLeft - config.placementMarginPx,
      visualTop - config.placementMarginPx,
      visualLeft + rotatedWidth + config.placementMarginPx,
      visualTop + rotatedHeight + config.placementMarginPx,
    ];
  });

  const ranked = largestEmptyRects(
    obstacles,
    anchorScroll + MASONRY_PLACEMENT_FRAME_MARGIN_PX,
    anchorScroll + CANVAS_W - MASONRY_PLACEMENT_FRAME_MARGIN_PX,
  ).flatMap(
    ([areaScore, rect]) => {
      const [left, _top, right] = rect;
      const width = Math.max(1, right - left);
      const revealVisibility =
        samples.reduce((total, t) => {
          const progress = Math.max(0, Math.min(1, t / duration));
          const scroll = panPx * progress;
          const visibleWidth = Math.max(
            0,
            Math.min(right - scroll, CANVAS_W) - Math.max(left - scroll, 0),
          );
          return total + visibleWidth / width;
        }, 0) / samples.length;
      const anchorVisibleWidth = Math.max(
        0,
        Math.min(right - anchorScroll, CANVAS_W - MASONRY_PLACEMENT_FRAME_MARGIN_PX) -
          Math.max(left - anchorScroll, MASONRY_PLACEMENT_FRAME_MARGIN_PX),
      );
      if (anchorVisibleWidth / width < 0.98) return [];
      const centerX = ((left + right) / 2 - anchorScroll) / CANVAS_W;
      return [{ revealVisibility, areaScore, spatialScore: Math.abs(centerX - 0.5), rect }];
    },
  );
  ranked.sort(
    (a, b) =>
      b.revealVisibility - a.revealVisibility ||
      b.areaScore - a.areaScore ||
      b.spatialScore - a.spatialScore ||
      a.rect[1] - b.rect[1] ||
      a.rect[0] - b.rect[0],
  );

  const candidates: TextPlacementCandidate[] = [];
  const selectedRects: Rect[] = [];
  for (const { revealVisibility, rect } of ranked) {
    if (selectedRects.some((selected) => rectsOverlap(rect, selected))) continue;
    const [left, top, right, bottom] = rect;
    const width = right - left;
    const height = bottom - top;
    const areaRatio = (width * height) / Math.max(1, CANVAS_W * CANVAS_H);
    candidates.push(candidateFromRect({
      left,
      top,
      width,
      height,
      durationS: duration,
      rotationDeg: rotationForEmptyPocket(width, height),
      confidence: Math.max(
        0.35,
        Math.min(0.98, 0.42 + revealVisibility * 0.38 + areaRatio * 2.2),
      ),
      source: config.source,
      boardWidth,
    }));
    selectedRects.push(rect);
    if (candidates.length >= wanted) break;
  }
  return candidates;
}

function rotationForEmptyPocket(width: number, height: number): number {
  if (width <= 0 || height <= 0) return 0;
  return height >= width * 1.75 && height >= CANVAS_H * 0.18 ? 90 : 0;
}

function largestEmptyRects(
  obstacles: Rect[],
  safeLeft = MASONRY_PLACEMENT_FRAME_MARGIN_PX,
  safeRight = CANVAS_W - MASONRY_PLACEMENT_FRAME_MARGIN_PX,
): Array<[number, Rect]> {
  const safeTop = MASONRY_PLACEMENT_FRAME_MARGIN_PX;
  const safeBottom = CANVAS_H - MASONRY_PLACEMENT_FRAME_MARGIN_PX;
  if (safeRight <= safeLeft || safeBottom <= safeTop) return [];
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
          scored.push([(width * height) / (CANVAS_W * CANVAS_H), rect]);
        }
      }
      stack.push(scanIdx);
    }
  }

  scored.sort((a, b) => b[0] - a[0] || a[1][1] - b[1][1] || a[1][0] - b[1][0]);
  const unique = new Map<string, [number, Rect]>();
  for (const candidate of scored) unique.set(candidate[1].join(":"), candidate);
  return Array.from(unique.values()).sort(
    (a, b) => b[0] - a[0] || a[1][1] - b[1][1] || a[1][0] - b[1][0],
  );
}

function rectsOverlap(a: Rect, b: Rect): boolean {
  const interW = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0]));
  const interH = Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
  return interW > 0 && interH > 0;
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
  durationS = MASONRY_MAX_DURATION_S,
  anchorTimeS?: number,
): TextPlacementCandidate | null {
  if (!selectedBar) return null;
  return (
    resolveSmartPlacementCandidates(variant, [selectedBar], durationS, anchorTimeS).find(
      (candidate) => smartPlacementCandidateFitsBar(selectedBar, candidate),
    ) ?? null
  );
}

export function resolveSmartPlacementCandidates(
  variant: PlanItemVariant | null | undefined,
  bars: TextElementBar[],
  durationS = MASONRY_MAX_DURATION_S,
  anchorTimeS?: number,
): TextPlacementCandidate[] {
  if (bars.length === 0) return [];
  const serverCandidates = variant?.text_placement_candidates?.filter(Boolean) ?? [];
  const wanted = Math.max(3, bars.length);
  if (isMasonryVariant(variant)) {
    // Locally discovered board pockets are authoritative. This deliberately
    // replaces persisted candidates from variants created before geometry-driven
    // placement, whose curated coordinates overlap the actual masonry tiles.
    const defaultAnchorS = Math.min(2, clampDuration(durationS));
    const firstStartS = Math.min(...bars.map((bar) => bar.start_s));
    const lastEndS = Math.max(...bars.map((bar) => bar.end_s));
    const barsMidpointS = (firstStartS + lastEndS) / 2;
    const anchorIsActive =
      Number.isFinite(anchorTimeS) &&
      bars.every((bar) => (anchorTimeS as number) >= bar.start_s && (anchorTimeS as number) < bar.end_s);
    const requestedAnchorS = anchorIsActive
      ? (anchorTimeS as number)
      : Math.max(defaultAnchorS, barsMidpointS);
    return masonryWhitespaceCandidates({
      durationS,
      maxCandidates: wanted,
      anchorTimeS: requestedAnchorS,
      preset: collagePresetForVariant(variant) ?? "masonry",
    });
  }
  if (serverCandidates.length > 0) return serverCandidates;
  return [DEFAULT_SMART_PLACE];
}

export function allocateSmartPlacementCandidates(
  bars: TextElementBar[],
  candidates: TextPlacementCandidate[] | TextPlacementCandidate[][],
): TextPlacementCandidate[] | null {
  if (bars.length === 0) return [];
  if (candidates.length === 0) return null;
  const assignments = Array<TextPlacementCandidate | null>(bars.length).fill(null);
  const candidatesByBar = Array.isArray(candidates[0])
    ? (candidates as TextPlacementCandidate[][])
    : bars.map(() => candidates as TextPlacementCandidate[]);
  const order = bars
    .map((bar, index) => ({
      bar,
      index,
      candidates: (candidatesByBar[index] ?? []).filter((candidate) =>
        smartPlacementCandidateFitsBar(bar, candidate),
      ),
    }))
    .sort(
      (a, b) =>
        a.candidates.length - b.candidates.length ||
        a.bar.start_s - b.bar.start_s ||
        a.bar.end_s - b.bar.end_s ||
        a.index - b.index,
    );

  const assign = (orderIndex: number): boolean => {
    if (orderIndex >= order.length) return true;
    const { bar, index, candidates: availableCandidates } = order[orderIndex];
    for (const candidate of availableCandidates) {
      const isAvailable = bars.every((previous, previousIndex) => {
          const previousCandidate = assignments[previousIndex];
          if (!previousCandidate) return true;
          const overlapsInTime =
            bar.start_s < previous.end_s && previous.start_s < bar.end_s;
          return !overlapsInTime || !smartPlacementCandidatesOverlap(candidate, previousCandidate);
        });
      if (!isAvailable) continue;
      assignments[index] = candidate;
      if (assign(orderIndex + 1)) return true;
      assignments[index] = null;
    }
    return false;
  };

  return assign(0) ? (assignments as TextPlacementCandidate[]) : null;
}

export function resolveSmartPlacementAssignments(
  variant: PlanItemVariant | null | undefined,
  bars: TextElementBar[],
  durationS = MASONRY_MAX_DURATION_S,
  anchorTimeS?: number,
): TextPlacementCandidate[] | null {
  if (!isMasonryVariant(variant)) {
    return allocateSmartPlacementCandidates(
      bars,
      resolveSmartPlacementCandidates(variant, bars, durationS, anchorTimeS),
    );
  }
  const wanted = Math.max(3, bars.length);
  const candidatesByBar = bars.map((bar) => {
    const anchorIsActive =
      Number.isFinite(anchorTimeS) &&
      (anchorTimeS as number) >= bar.start_s &&
      (anchorTimeS as number) < bar.end_s;
    const barAnchorS = anchorIsActive
      ? (anchorTimeS as number)
      : (bar.start_s + bar.end_s) / 2;
    return masonryWhitespaceCandidates({
      durationS,
      maxCandidates: wanted,
      anchorTimeS: barAnchorS,
      preset: collagePresetForVariant(variant) ?? "masonry",
    });
  });
  return allocateSmartPlacementCandidates(bars, candidatesByBar);
}

function smartPlacementCandidatesOverlap(
  first: TextPlacementCandidate,
  second: TextPlacementCandidate,
): boolean {
  const firstPocket = smartPlacementPocket(first);
  const secondPocket = smartPlacementPocket(second);
  if (firstPocket && secondPocket) {
    return (
      firstPocket.left < secondPocket.right &&
      secondPocket.left < firstPocket.right &&
      firstPocket.top < secondPocket.bottom &&
      secondPocket.top < firstPocket.bottom
    );
  }
  return (
    Math.abs(first.x_frac - second.x_frac) < 0.001 &&
    Math.abs(first.y_frac - second.y_frac) < 0.001 &&
    Math.abs((first.rotation_deg ?? 0) - (second.rotation_deg ?? 0)) < 0.001
  );
}

function smartPlacementPocket(candidate: TextPlacementCandidate): {
  left: number;
  top: number;
  right: number;
  bottom: number;
} | null {
  const motion = candidate.masonry_motion;
  const left = motion?.pocket_left_px;
  const top = motion?.pocket_top_px;
  const right = motion?.pocket_right_px;
  const bottom = motion?.pocket_bottom_px;
  if (
    typeof left !== "number" ||
    typeof top !== "number" ||
    typeof right !== "number" ||
    typeof bottom !== "number" ||
    ![left, top, right, bottom].every(Number.isFinite)
  ) {
    return null;
  }
  return { left, top, right, bottom };
}

type ParsedMasonryMotion = {
  durationS: number;
  panPx: number;
  boardWidthPx: number;
  frameWidthPx: number;
  layerOriginPx: number;
};

function parseMasonryMotion(
  motion: Record<string, unknown> | null | undefined,
): ParsedMasonryMotion | null {
  if (motion?.mode !== "masonry_pan_x") return null;
  const durationS = motion.duration_s;
  const panPx = motion.pan_px;
  const boardWidthPx = motion.board_width_px;
  const frameWidthPx = motion.frame_width_px;
  const layerOriginPx = motion.layer_origin_px ?? 0;
  if (
    typeof durationS !== "number" ||
    !Number.isFinite(durationS) ||
    durationS <= 0 ||
    typeof panPx !== "number" ||
    !Number.isFinite(panPx) ||
    panPx < 0 ||
    typeof boardWidthPx !== "number" ||
    !Number.isFinite(boardWidthPx) ||
    typeof frameWidthPx !== "number" ||
    !Number.isFinite(frameWidthPx) ||
    frameWidthPx <= 0 ||
    boardWidthPx < frameWidthPx ||
    panPx > boardWidthPx - frameWidthPx ||
    typeof layerOriginPx !== "number" ||
    !Number.isFinite(layerOriginPx) ||
    layerOriginPx < 0 ||
    layerOriginPx > boardWidthPx - frameWidthPx
  ) {
    return null;
  }
  return { durationS, panPx, boardWidthPx, frameWidthPx, layerOriginPx };
}

export function masonryMotionOffsetFrac(
  motion: Record<string, unknown> | null | undefined,
  currentTimeS: number,
): number {
  const parsed = parseMasonryMotion(motion);
  if (!parsed) return 0;
  const { durationS, panPx, frameWidthPx } = parsed;
  const timeS = Number.isFinite(currentTimeS) ? Math.max(0, currentTimeS) : 0;
  const offset = (panPx * Math.min(timeS, durationS)) / durationS / frameWidthPx;
  return Number.isFinite(offset) ? offset : 0;
}

export function masonryBoardXFrac(
  motion: Record<string, unknown> | null | undefined,
  localXFrac: number,
): number {
  const parsed = parseMasonryMotion(motion);
  if (!parsed || !Number.isFinite(localXFrac)) return localXFrac;
  return localXFrac + parsed.layerOriginPx / parsed.frameWidthPx;
}

export function masonryLayerPositionForBoardX(
  motion: Record<string, unknown> | null | undefined,
  boardXFrac: number,
  edgeInsetFrac = 0.02,
): { xFrac: number; layerOriginPx: number } {
  const parsed = parseMasonryMotion(motion);
  if (!parsed || !Number.isFinite(boardXFrac)) {
    return {
      xFrac: Math.max(edgeInsetFrac, Math.min(1 - edgeInsetFrac, boardXFrac || 0.5)),
      layerOriginPx: 0,
    };
  }
  const boardXpx = Math.max(
    parsed.frameWidthPx * edgeInsetFrac,
    Math.min(
      boardXFrac * parsed.frameWidthPx,
      parsed.boardWidthPx - parsed.frameWidthPx * edgeInsetFrac,
    ),
  );
  const layerOriginPx = Math.max(
    0,
    Math.min(
      boardXpx - parsed.frameWidthPx / 2,
      parsed.boardWidthPx - parsed.frameWidthPx,
    ),
  );
  return {
    xFrac: (boardXpx - layerOriginPx) / parsed.frameWidthPx,
    layerOriginPx,
  };
}

export function textBoxScreenXFrac(
  motion: Record<string, unknown> | null | undefined,
  currentTimeS: number,
  localXFrac: number,
): number {
  return (
    masonryBoardXFrac(motion, localXFrac) -
    masonryMotionOffsetFrac(motion, currentTimeS)
  );
}

export function textBoxPositionPatchForBar({
  motion,
  currentTimeS,
  bar,
  position,
}: {
  motion: Record<string, unknown> | null | undefined;
  currentTimeS: number;
  bar: TextElementBar;
  position: TextBoxHorizontalPosition;
}): Partial<Omit<TextElementBar, "id" | "role">> {
  const alignment = (bar.alignment ?? "center") as TextHorizontalAlignment;
  const screenXFrac = xFracForTextBoxPosition({
    alignment,
    position,
    maxWidthFrac: bar.max_width_frac,
  });
  const patch: Partial<Omit<TextElementBar, "id" | "role">> = {
    x_frac: screenXFrac,
    position: "custom",
    y_frac: resolveTextElementYFrac(bar.position, bar.y_frac),
  };
  if (!parseMasonryMotion(motion)) return patch;

  const parsed = parseMasonryMotion(motion);
  if (!parsed) return patch;
  const timeS = Number.isFinite(currentTimeS) ? Math.max(0, currentTimeS) : 0;
  const layerOriginPx =
    (parsed.panPx * Math.min(timeS, parsed.durationS)) / parsed.durationS;
  const nextMotion = {
    ...motion,
    layer_origin_px: layerOriginPx,
  } as Record<string, unknown>;
  delete nextMotion.pocket_left_px;
  delete nextMotion.pocket_top_px;
  delete nextMotion.pocket_right_px;
  delete nextMotion.pocket_bottom_px;
  patch.source_params = {
    ...(bar.source_params ?? {}),
    masonry_motion: nextMotion,
  };
  return patch;
}

export function collageMotionForTextBar(
  variant: PlanItemVariant | null | undefined,
  durationS: number,
  bar: TextElementBar | null | undefined,
): Record<string, unknown> | null {
  const base = collageMotionForVariant(variant, durationS);
  if (!base) return null;
  const storedMotion = bar?.source_params?.masonry_motion;
  if (!storedMotion || typeof storedMotion !== "object") return base;
  const layerOriginPx = (storedMotion as Record<string, unknown>).layer_origin_px;
  const candidate = { ...base, layer_origin_px: layerOriginPx };
  return parseMasonryMotion(candidate) ? candidate : base;
}

export function smartPlacementPatchForBar(
  bar: TextElementBar,
  candidate: TextPlacementCandidate,
): Partial<Omit<TextElementBar, "id" | "role">> {
  const smartText = reflowTextForSmartPlacement(bar.text, candidate);
  const sourceParams = { ...(bar.source_params ?? {}) };
  if (candidate.masonry_motion) sourceParams.masonry_motion = candidate.masonry_motion;
  const fittedSizePx = fittedSmartPlacementSizePx(bar, smartText, candidate);
  return {
    ...(smartText !== bar.text ? { text: smartText } : {}),
    ...(fittedSizePx !== bar.size_px ? { size_px: fittedSizePx } : {}),
    x_frac: candidate.x_frac,
    y_frac: candidate.y_frac,
    max_width_frac: candidate.max_width_frac,
    rotation_deg: candidate.rotation_deg ?? null,
    position: "custom",
    source_params: Object.keys(sourceParams).length > 0 ? sourceParams : undefined,
  };
}

export function smartPlacementCandidateFitsBar(
  bar: TextElementBar,
  candidate: TextPlacementCandidate,
): boolean {
  const smartText = reflowTextForSmartPlacement(bar.text, candidate);
  const fit = smartPlacementFit(bar, smartText, candidate);
  return fit === null || fit.maxSizePx >= SMART_PLACEMENT_MIN_SIZE_PX;
}

function fittedSmartPlacementSizePx(
  bar: TextElementBar,
  text: string,
  candidate: TextPlacementCandidate,
): number | undefined {
  const currentSize = typeof bar.size_px === "number" && Number.isFinite(bar.size_px)
    ? bar.size_px
    : undefined;
  const fit = smartPlacementFit(bar, text, candidate);
  if (fit === null) return currentSize;
  const safeSize = Math.max(SMART_PLACEMENT_MIN_SIZE_PX, fit.maxSizePx);
  return currentSize === undefined ? Math.min(96, safeSize) : Math.min(currentSize, safeSize);
}

function smartPlacementFit(
  bar: TextElementBar,
  text: string,
  candidate: TextPlacementCandidate,
): { maxSizePx: number } | null {
  const motion = candidate.masonry_motion;
  if (!motion) return null;
  const top = motion.pocket_top_px;
  const bottom = motion.pocket_bottom_px;
  const left = motion.pocket_left_px;
  const right = motion.pocket_right_px;
  if (
    typeof top !== "number" || !Number.isFinite(top) ||
    typeof bottom !== "number" || !Number.isFinite(bottom) || bottom <= top ||
    typeof left !== "number" || !Number.isFinite(left) ||
    typeof right !== "number" || !Number.isFinite(right) || right <= left
  ) return null;

  const lineSpacing =
    typeof bar.line_spacing === "number" && Number.isFinite(bar.line_spacing)
      ? Math.max(0.8, Math.min(2, bar.line_spacing))
      : 1.15;
  const lines = text.split("\n");
  const lineCount = Math.max(1, lines.length);
  const longestLine = Math.max(...lines.map((line) => line.length), 1);
  const pocketWidth = right - left;
  const pocketHeight = bottom - top;
  const horizontalHeightLimit = (pocketHeight * 0.82) / (lineCount * lineSpacing);
  const horizontalWidthLimit = (pocketWidth * 0.84) / (longestLine * 0.55);
  const maxSizePx = candidate.rotation_deg
    ? Math.min(
        (pocketWidth * 0.82) / lineSpacing,
        (pocketHeight * 0.84) / (longestLine * 0.55),
      )
    : Math.min(horizontalHeightLimit, horizontalWidthLimit);
  return { maxSizePx: Math.max(0, Math.floor(maxSizePx)) };
}

export function splitTextForSmartPlacement(
  text: string,
  candidates: TextPlacementCandidate[],
): string[] {
  const normalized = normalizeSmartText(text);
  if (!normalized) return [];
  const maxChunks = Math.max(1, candidates.length || 1);
  if (maxChunks === 1) return [normalized];

  const manualLines = text
    .split(/\n+/)
    .map(normalizeSmartText)
    .filter(Boolean);
  if (manualLines.length >= 2) {
    return balanceTextSegments(manualLines, Math.min(maxChunks, manualLines.length));
  }

  const sentenceParts = (normalized.match(/[^.!?]+[.!?]?/g) ?? [normalized])
    .map(normalizeSmartText)
    .filter(Boolean);
  if (sentenceParts.length >= 2) {
    return balanceTextSegments(sentenceParts, Math.min(maxChunks, sentenceParts.length));
  }

  const words = normalized.split(/\s+/).filter(Boolean);
  const targetChunks = smartChunkCountForWords(words.length, maxChunks);
  if (targetChunks <= 1) return [normalized];

  if (candidates[0]?.rotation_deg) {
    const rotatedWords = Math.min(
      words.length - (targetChunks - 1),
      words.length <= 6 ? 2 : 3,
    );
    const first = words.slice(0, rotatedWords).join(" ");
    const rest = balancedLines(words.slice(rotatedWords), targetChunks - 1);
    return [first, ...rest].map(normalizeSmartText).filter(Boolean);
  }

  return balancedLines(words, targetChunks).map(normalizeSmartText).filter(Boolean);
}

function normalizeSmartText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function smartChunkCountForWords(wordCount: number, maxChunks: number): number {
  if (wordCount <= 3) return 1;
  if (wordCount <= 6) return Math.min(2, maxChunks);
  if (wordCount <= 12) return Math.min(3, maxChunks);
  return Math.min(4, maxChunks);
}

function balanceTextSegments(segments: string[], lineCount: number): string[] {
  if (segments.length <= lineCount) return segments;
  return balancedLines(segments, lineCount);
}

export function reflowTextForSmartPlacement(
  text: string,
  candidate: TextPlacementCandidate,
): string {
  if (candidate.rotation_deg) return text.trim().split(/\s+/).filter(Boolean).join(" ");
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
