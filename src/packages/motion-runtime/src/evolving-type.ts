import {
  creatorBlockFrameV2,
  smootherstep,
  staggerOrderRank,
  staggerProgress,
  type MotionV2Frame,
  type MotionV2Like,
  type MotionV2TimingDefinition,
} from "./presets.ts";

export type EvolvingIconStyle = "organic" | "geometric" | "botanical";
export type EvolvingDensity = "low" | "medium" | "high";
export type EvolvingLayout = "compact" | "spread";
export type EvolvingOrder = "forward" | "reverse" | "center-out";

export interface EvolvingTypeParamsLike {
  headline: string;
  subtitle: string;
  icon_count: number;
  icon_style: EvolvingIconStyle;
  text_stagger_ms: number;
  icon_stagger_ms: number;
  morph_amplitude: number;
  density: EvolvingDensity;
  layout: EvolvingLayout;
  order: EvolvingOrder;
  typography_scale: number;
  backdrop_opacity: number;
  split_icons: boolean;
}

export interface OrganicNode {
  x: number;
  y: number;
  inX: number;
  inY: number;
  outX: number;
  outY: number;
}

export interface EvolvingIconFrame {
  x: number;
  y: number;
  grow: number;
  split: number;
  settle: number;
  morph: number;
  scale: number;
  rotation: number;
  splitOffset: number;
  detailCount: number;
  nodes: readonly OrganicNode[];
}

export interface EvolvingTypeFrame {
  timeline: MotionV2Frame;
  headlineReveal: number;
  subtitleReveal: number;
  icons: readonly EvolvingIconFrame[];
}

export interface EvolvingIconCopy {
  direction: -1 | 0 | 1;
  alpha: number;
}

export const EVOLVING_TYPE_TIMING: MotionV2TimingDefinition = Object.freeze({
  base_choreography_frames: 111,
  fixed_exit_frames: 18,
});

/** Crossfades one growing body into two half-alpha branches without a brightness pop. */
export function evolvingIconCopies(splitEnabled: boolean, split: number): readonly EvolvingIconCopy[] {
  if (!splitEnabled) return [{ direction: 0, alpha: 1 }];
  const amount = Math.max(0, Math.min(1, split));
  return [
    { direction: 0, alpha: 1 - amount },
    { direction: -1, alpha: amount * 0.5 },
    { direction: 1, alpha: amount * 0.5 },
  ].filter((copy) => copy.alpha > 0) as EvolvingIconCopy[];
}

const TOPOLOGY_POINTS = 8;
const STYLE_RADII: Readonly<Record<EvolvingIconStyle, readonly (readonly number[])[]>> = {
  organic: [
    [0.82, 1.04, 0.88, 1.12, 0.84, 1.02, 0.9, 1.08],
    [1.08, 0.86, 1.14, 0.82, 1.02, 0.9, 1.1, 0.88],
    [0.94, 1.13, 0.8, 1.06, 0.91, 1.16, 0.83, 1.02],
  ],
  geometric: [
    [1.08, 0.88, 1.08, 0.88, 1.08, 0.88, 1.08, 0.88],
    [0.92, 1.12, 0.92, 1.12, 0.92, 1.12, 0.92, 1.12],
    [1.12, 1.12, 0.84, 0.84, 1.12, 1.12, 0.84, 0.84],
  ],
  botanical: [
    [1.16, 0.78, 1.05, 0.82, 1.16, 0.78, 1.05, 0.82],
    [0.8, 1.18, 0.86, 1.04, 0.8, 1.18, 0.86, 1.04],
    [1.14, 0.76, 0.92, 1.08, 1.14, 0.76, 0.92, 1.08],
  ],
};

const ICON_LAYOUTS: Readonly<Record<EvolvingLayout, Readonly<Record<number, readonly [number, number][]>>>> = {
  compact: {
    2: [[-0.18, 0.08], [0.18, -0.04]],
    3: [[-0.22, 0.08], [0, -0.08], [0.22, 0.08]],
    4: [[-0.2, -0.08], [0.2, -0.08], [-0.2, 0.17], [0.2, 0.17]],
    5: [[-0.24, -0.08], [0, -0.12], [0.24, -0.08], [-0.14, 0.18], [0.14, 0.18]],
  },
  spread: {
    2: [[-0.3, 0.08], [0.3, -0.08]],
    3: [[-0.32, 0.12], [0, -0.16], [0.32, 0.12]],
    4: [[-0.31, -0.13], [0.31, -0.13], [-0.31, 0.22], [0.31, 0.22]],
    5: [[-0.34, -0.11], [0, -0.19], [0.34, -0.11], [-0.2, 0.23], [0.2, 0.23]],
  },
};

function lerp(a: number, b: number, amount: number): number {
  return a + (b - a) * amount;
}

function nodesFromRadii(radii: readonly number[]): OrganicNode[] {
  const anchors = Array.from({ length: TOPOLOGY_POINTS }, (_, index) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / TOPOLOGY_POINTS;
    const radius = radii[index];
    return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
  });
  return anchors.map((anchor, index) => {
    const previous = anchors[(index + anchors.length - 1) % anchors.length];
    const next = anchors[(index + 1) % anchors.length];
    const tangentX = (next.x - previous.x) * 0.18;
    const tangentY = (next.y - previous.y) * 0.18;
    return {
      x: anchor.x,
      y: anchor.y,
      inX: anchor.x - tangentX,
      inY: anchor.y - tangentY,
      outX: anchor.x + tangentX,
      outY: anchor.y + tangentY,
    };
  });
}

/** Trusted fixed-topology cubic points; only built-in arrays participate. */
export function organicPathPoints(
  style: EvolvingIconStyle,
  iconIndex: number,
  morph: number,
  amplitude: number,
): readonly OrganicNode[] {
  const variants = STYLE_RADII[style];
  const source = nodesFromRadii(variants[iconIndex % variants.length]);
  const target = nodesFromRadii(variants[(iconIndex + 1) % variants.length]);
  const amount = smootherstep(morph) * Math.max(0, Math.min(1, amplitude));
  return source.map((node, index) => ({
    x: lerp(node.x, target[index].x, amount),
    y: lerp(node.y, target[index].y, amount),
    inX: lerp(node.inX, target[index].inX, amount),
    inY: lerp(node.inY, target[index].inY, amount),
    outX: lerp(node.outX, target[index].outX, amount),
    outY: lerp(node.outY, target[index].outY, amount),
  }));
}

/** Small deterministic grapheme grouping for masks; keeps marks/ZWJ/emoji modifiers attached. */
export function stableGraphemes(text: string): string[] {
  const clusters: string[] = [];
  let joinNext = false;
  for (const codePoint of Array.from(text)) {
    const combining = /\p{Mark}/u.test(codePoint)
      || /[\uFE00-\uFE0F]/u.test(codePoint)
      || /[\u{1F3FB}-\u{1F3FF}]/u.test(codePoint);
    const regional = /[\u{1F1E6}-\u{1F1FF}]/u.test(codePoint);
    const previous = clusters.at(-1) ?? "";
    const pairRegional = regional
      && Array.from(previous).length === 1
      && /[\u{1F1E6}-\u{1F1FF}]/u.test(previous);
    if (
      clusters.length === 0 ||
      (!combining && !joinNext && codePoint !== "\u200D" && !pairRegional)
    ) {
      clusters.push(codePoint);
    } else {
      clusters[clusters.length - 1] += codePoint;
    }
    joinNext = codePoint === "\u200D";
  }
  return clusters;
}

function revealFraction(
  text: string,
  authoredFrame: number,
  staggerFrames: number,
  order: EvolvingOrder,
): number {
  const clusters = stableGraphemes(text);
  if (clusters.length === 0) return 1;
  const revealed = clusters.reduce(
    (total, _cluster, index) => total + staggerProgress(
      authoredFrame,
      index,
      clusters.length,
      staggerFrames,
      4,
      order,
    ),
    0,
  );
  return Math.max(0, Math.min(1, revealed / clusters.length));
}

export function evolvingTypeFrame(
  instance: MotionV2Like & { params: EvolvingTypeParamsLike },
  frame: number,
  timing: MotionV2TimingDefinition = EVOLVING_TYPE_TIMING,
): EvolvingTypeFrame {
  const timeline = creatorBlockFrameV2(instance, frame, timing);
  const params = instance.params;
  const textStaggerFrames = Math.max(0, Math.round(params.text_stagger_ms * 30 / 1000));
  const iconStaggerFrames = Math.max(0, Math.round(params.icon_stagger_ms * 30 / 1000));
  const iconCount = Math.max(2, Math.min(5, Math.round(params.icon_count)));
  const positions = ICON_LAYOUTS[params.layout][iconCount];
  const headlineReveal = revealFraction(
    params.headline,
    timeline.authoredFrame,
    textStaggerFrames,
    params.order,
  );
  const subtitleStart = Math.min(36, Math.max(10, stableGraphemes(params.headline).length * textStaggerFrames * 0.55));
  const subtitleReveal = revealFraction(
    params.subtitle,
    timeline.authoredFrame - subtitleStart,
    textStaggerFrames,
    params.order,
  );
  const detailCount = params.density === "high" ? 5 : params.density === "medium" ? 3 : 1;
  const icons = positions.map(([x, y], index): EvolvingIconFrame => {
    const delay = staggerOrderRank(index, iconCount, params.order) * iconStaggerFrames;
    const authored = timeline.authoredFrame - delay;
    const grow = smootherstep((authored - 12) / 18);
    const split = params.split_icons ? smootherstep((authored - 31) / 17) : 0;
    const settle = smootherstep((authored - 67) / 22);
    const morph = smootherstep((authored - 46) / 48);
    return {
      x,
      y,
      grow,
      split,
      settle,
      morph,
      scale: grow * (1 + 0.08 * split * (1 - settle)),
      rotation: (index % 2 ? -1 : 1) * (1 - settle) * split * 8 * instance.intensity,
      splitOffset: split * (1 - settle * 0.68) * (0.025 + instance.intensity * 0.025),
      detailCount,
      nodes: organicPathPoints(params.icon_style, index, morph, params.morph_amplitude),
    };
  });
  return { timeline, headlineReveal, subtitleReveal, icons };
}
