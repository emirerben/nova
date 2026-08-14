import {
  activeMotionInstances,
  type CreatorBlockInstanceV2,
  type CardStackInstanceV1,
  type CloudBreakInstanceV1,
  type DonutTextInstanceV1,
  type EvolvingTypeInstanceV2,
  type FilmStripInstanceV1,
  type FlowFieldInstanceV1,
  type KineticWordInstanceV1,
  type MotionPresetInstance,
  type OfferSwapInstanceV1,
  type RouteTraceInstanceV1,
  type TagStackInstanceV1,
} from "./contract.ts";
import { creatorBlockEntry } from "./catalog.ts";
import {
  ROUTE_TRACE_PATH,
  ROUTE_TRACE_VIEWBOX,
  creatorBlockFrame,
  creatorBlockFrameV2,
  continuousCardPose,
  easeInOutCubic,
  routeTraceFrame,
} from "./presets.ts";
import {
  evolvingIconCopies,
  evolvingTypeFrame,
  type EvolvingOrder,
  type OrganicNode,
} from "./evolving-type.ts";

interface Disposable { delete(): void }
interface MotionPath extends Disposable {
  moveTo?(x: number, y: number): void;
  cubicTo?(cx1: number, cy1: number, cx2: number, cy2: number, x: number, y: number): void;
  close?(): void;
}
interface MotionTypeface extends Disposable {}
interface MotionFont extends Disposable {
  getGlyphIDs(text: string, numCodePoints?: number): Uint16Array;
  getGlyphWidths(glyphs: Uint16Array, paint?: MotionPaint | null): Float32Array;
}
interface MotionImage extends Disposable { width(): number; height(): number }
interface MotionPaint extends Disposable {
  setStyle(value: unknown): void;
  setStrokeCap(value: unknown): void;
  setStrokeJoin(value: unknown): void;
  setAntiAlias(value: boolean): void;
  setStrokeWidth(value: number): void;
  setColor(value: Float32Array): void;
  setPathEffect(value: Disposable | null): void;
}

type Rect = Float32Array;
type RRect = Float32Array;

export interface MotionCanvas {
  clear(color: Float32Array): void;
  save(): number;
  restore(): void;
  translate(x: number, y: number): void;
  scale(x: number, y: number): void;
  rotate(degrees: number, px?: number, py?: number): void;
  skew(sx: number, sy: number): void;
  clipRect(rect: Rect, op?: unknown, antiAlias?: boolean): void;
  drawPath(path: MotionPath, paint: MotionPaint): void;
  drawRect(rect: Rect, paint: MotionPaint): void;
  drawRRect(rect: RRect, paint: MotionPaint): void;
  drawCircle(cx: number, cy: number, radius: number, paint: MotionPaint): void;
  drawText(text: string, x: number, y: number, paint: MotionPaint, font: MotionFont): void;
  drawImageRect(image: MotionImage, source: Rect, dest: Rect, paint?: MotionPaint): void;
}

export interface MotionCanvasKit {
  TRANSPARENT: Float32Array;
  parseColorString(value: string): Float32Array;
  multiplyByAlpha(color: Float32Array, alpha: number): Float32Array;
  PaintStyle: { Stroke: unknown; Fill: unknown };
  StrokeCap: { Round: unknown };
  StrokeJoin: { Round: unknown };
  ClipOp?: { Intersect: unknown };
  Path: {
    new (): MotionPath;
    MakeFromSVGString(value: string): MotionPath | null;
  };
  Paint: new () => MotionPaint;
  Font: new (typeface: MotionTypeface | null, size: number) => MotionFont;
  Typeface: { MakeFreeTypeFaceFromData(value: Uint8Array): MotionTypeface | null };
  MakeImageFromEncoded(value: Uint8Array): MotionImage | null;
  XYWHRect(x: number, y: number, width: number, height: number): Rect;
  RRectXY(rect: Rect, rx: number, ry: number): RRect;
  PathEffect: { MakeDash(intervals: number[], phase?: number): Disposable };
}

export interface MotionResourceBytes {
  font: Uint8Array;
  images?: Readonly<Record<string, Uint8Array>>;
}

export interface MotionResources extends Disposable {
  typeface: MotionTypeface;
  images: ReadonlyMap<string, MotionImage>;
  font(size: number): MotionFont;
}

export function createMotionResources(CanvasKitInput: unknown, input: MotionResourceBytes): MotionResources {
  const CanvasKit = CanvasKitInput as MotionCanvasKit;
  const typeface = CanvasKit.Typeface.MakeFreeTypeFaceFromData(input.font);
  if (!typeface) throw new Error("Creator Block font bytes could not be decoded");
  const images = new Map<string, MotionImage>();
  const fonts = new Map<number, MotionFont>();
  try {
    for (const [assetId, bytes] of Object.entries(input.images ?? {})) {
      const image = CanvasKit.MakeImageFromEncoded(bytes);
      if (!image) throw new Error(`Creator Block image ${assetId} could not be decoded`);
      images.set(assetId, image);
    }
  } catch (error) {
    images.forEach((image) => image.delete());
    typeface.delete();
    throw error;
  }
  return {
    typeface,
    images,
    font(size: number) {
      let font = fonts.get(size);
      if (!font) {
        font = new CanvasKit.Font(typeface, size);
        fonts.set(size, font);
      }
      return font;
    },
    delete() {
      fonts.forEach((font) => font.delete());
      images.forEach((image) => image.delete());
      typeface.delete();
    },
  };
}

function colorWithAlpha(CanvasKit: MotionCanvasKit, hex: string, alpha: number): Float32Array {
  return CanvasKit.multiplyByAlpha(CanvasKit.parseColorString(hex), Math.max(0, Math.min(1, alpha)));
}

function localLayout(width: number, height: number): { unit: number; cx: number; cy: number } {
  return { unit: Math.min(width, height), cx: width / 2, cy: height / 2 };
}

function withFill(CanvasKit: MotionCanvasKit, paint: MotionPaint, color: string, alpha: number): void {
  paint.setStyle(CanvasKit.PaintStyle.Fill);
  paint.setAntiAlias(true);
  paint.setColor(colorWithAlpha(CanvasKit, color, alpha));
}

function textWidth(resources: MotionResources, text: string, size: number): number {
  const font = resources.font(size);
  const glyphs = font.getGlyphIDs(text, Array.from(text).length);
  return font.getGlyphWidths(glyphs).reduce((total, width) => total + width, 0);
}

function fittedTextSize(
  resources: MotionResources,
  text: string,
  preferredSize: number,
  maxWidth: number,
): number {
  const width = textWidth(resources, text, preferredSize);
  return width > maxWidth && width > 0
    ? preferredSize * maxWidth / width
    : preferredSize;
}

function drawCenteredText(
  CanvasKit: MotionCanvasKit,
  canvas: MotionCanvas,
  resources: MotionResources,
  paint: MotionPaint,
  text: string,
  x: number,
  baseline: number,
  size: number,
  color: string,
  alpha: number,
): void {
  withFill(CanvasKit, paint, color, alpha);
  canvas.drawText(
    text,
    x - textWidth(resources, text, size) / 2,
    baseline,
    paint,
    resources.font(size),
  );
}

function drawRouteTrace(
  CanvasKit: MotionCanvasKit,
  canvas: MotionCanvas,
  path: MotionPath,
  paint: MotionPaint,
  instance: RouteTraceInstanceV1,
  frame: number,
  width: number,
  height: number,
): void {
  const state = routeTraceFrame(instance, frame);
  const sx = width / ROUTE_TRACE_VIEWBOX.width;
  const sy = height / ROUTE_TRACE_VIEWBOX.height;
  const centerX = width / 2;
  const centerY = height / 2;
  canvas.save();
  canvas.translate(centerX, centerY);
  canvas.scale(state.scale, state.scale);
  canvas.translate(-centerX, -centerY);
  canvas.scale(sx, sy);
  paint.setStyle(CanvasKit.PaintStyle.Stroke);
  paint.setStrokeCap(CanvasKit.StrokeCap.Round);
  paint.setStrokeJoin(CanvasKit.StrokeJoin.Round);
  paint.setAntiAlias(true);
  paint.setStrokeWidth(state.strokeWidth);
  paint.setColor(colorWithAlpha(CanvasKit, state.primary, state.opacity * 0.32));
  const glowEffect = CanvasKit.PathEffect.MakeDash([Math.max(1, 1900 * state.progress), 1900], 0);
  paint.setPathEffect(glowEffect);
  canvas.drawPath(path, paint);
  glowEffect.delete();
  paint.setStrokeWidth(Math.max(3, state.strokeWidth * 0.38));
  paint.setColor(colorWithAlpha(CanvasKit, state.accent, state.opacity));
  const traceEffect = CanvasKit.PathEffect.MakeDash([Math.max(1, 1900 * state.progress), 1900], 0);
  paint.setPathEffect(traceEffect);
  canvas.drawPath(path, paint);
  traceEffect.delete();
  paint.setPathEffect(null);
  canvas.restore();
}

function drawKineticWord(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: KineticWordInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const text = instance.params.text.toUpperCase();
  const size = fittedTextSize(resources, text, unit * 0.145, width - unit * 0.16);
  const overshoot = state.local < 0.18 ? 2.4 - state.enter * 1.4 : state.scale;
  const collapseSkew = state.exit * (-0.18 - instance.intensity * 0.18);
  canvas.save();
  canvas.translate(cx, cy);
  canvas.rotate(state.rotation * 1.7, 0, 0);
  canvas.skew((1 - state.enter) * 0.22 + collapseSkew, 0);
  canvas.scale(overshoot, overshoot);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, size * 0.055, size * 0.395, size, instance.palette.primary, state.opacity * 0.92);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, 0, size * 0.34, size, instance.palette.accent, state.opacity);
  canvas.restore();
}

function drawTagStack(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: TagStackInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const maxLabelWidth = width - unit * 0.28;
  const longestLabel = instance.params.labels.reduce(
    (longest, label) => Array.from(label).length > Array.from(longest).length ? label : longest,
    "",
  ).toUpperCase();
  const verticalSize = (height - unit * 0.16) /
    (instance.params.labels.length * 1.5 + (instance.params.labels.length - 1) * 0.5);
  const size = Math.min(
    verticalSize,
    fittedTextSize(resources, longestLabel, unit * 0.055, maxLabelWidth - unit * 0.08),
  );
  const rowHeight = size * 1.5;
  const rowGap = size * 0.5;
  const rowStep = rowHeight + rowGap;
  const stackHeight = instance.params.labels.length * rowHeight
    + (instance.params.labels.length - 1) * rowGap;
  instance.params.labels.forEach((label, index) => {
    const motionOffset = Math.sin(state.cycle * Math.PI * 2 + index * 0.72)
      * size * 0.06 * instance.intensity;
    const top = cy - stackHeight / 2 + index * rowStep + motionOffset;
    const centerY = top + rowHeight / 2;
    const w = Math.min(maxLabelWidth, textWidth(resources, label.toUpperCase(), size) + size * 1.5);
    const active = 0.86 + 0.14 * Math.cos(state.cycle * Math.PI * 2 - index * 0.72);
    withFill(CanvasKit, paint, index % 2 ? instance.palette.primary : instance.palette.accent, state.opacity * active);
    canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(cx - w / 2, top, w, rowHeight), rowHeight / 2, rowHeight / 2), paint);
    drawCenteredText(CanvasKit, canvas, resources, paint, label.toUpperCase(), cx, centerY + size * 0.34, size, index % 2 ? instance.palette.accent : instance.palette.primary, state.opacity);
  });
}

function drawFlowField(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: FlowFieldInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const headline = instance.params.headline.toUpperCase();
  const size = fittedTextSize(resources, headline, unit * 0.105, width - unit * 0.16);
  const baseline = cy + size * 0.28;
  const glyphTop = baseline - size * 0.82;
  const glyphHeight = size * 1.08;
  const sliceCount = 8;
  drawCenteredText(CanvasKit, canvas, resources, paint, headline, cx, baseline, size, instance.palette.primary, state.opacity * 0.36);
  for (let slice = 0; slice < sliceCount; slice += 1) {
    const offset = Math.sin(state.local * Math.PI * 4 + slice * 0.8) * unit * 0.018 * instance.intensity;
    canvas.save();
    const top = glyphTop + slice * glyphHeight / sliceCount;
    canvas.clipRect(CanvasKit.XYWHRect(0, top, width, glyphHeight / sliceCount + 1), CanvasKit.ClipOp?.Intersect, true);
    drawCenteredText(CanvasKit, canvas, resources, paint, headline, cx + offset, baseline, size, instance.palette.accent, state.opacity);
    canvas.restore();
  }
  if (instance.params.kicker) {
    const kicker = instance.params.kicker.toUpperCase();
    const kickerSize = fittedTextSize(resources, kicker, size * 0.28, width - unit * 0.2);
    drawCenteredText(CanvasKit, canvas, resources, paint, kicker, cx, baseline + size * 0.92, kickerSize, instance.palette.primary, state.opacity * 0.9);
  }
}

function drawCloudBreak(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: CloudBreakInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const motionAmount = 0.45 + instance.intensity * 0.75;
  const blob = easeInOutCubic(Math.min(1, state.local / 0.45)) * motionAmount;
  for (let i = 0; i < 5; i += 1) {
    const angle = i * 2.17 + state.local * (0.7 + instance.intensity * 1.4);
    const radius = unit * (0.10 + (i % 2) * 0.035) * blob;
    withFill(CanvasKit, paint, i % 2 ? instance.palette.primary : instance.palette.accent, state.opacity * 0.78);
    canvas.drawCircle(cx + Math.cos(angle) * unit * 0.12, cy + Math.sin(angle) * unit * 0.1, radius, paint);
  }
  const longestLine = instance.params.lines.reduce(
    (longest, line) => Array.from(line).length > Array.from(longest).length ? line : longest,
    "",
  ).toUpperCase();
  const lineSize = fittedTextSize(
    resources,
    longestLine,
    unit * Math.min(0.08, 0.27 / instance.params.lines.length),
    width - unit * 0.16,
  );
  instance.params.lines.forEach((line, index) => {
    const y = cy + (index - (instance.params.lines.length - 1) / 2) * lineSize * 1.2;
    const text = line.toUpperCase();
    drawCenteredText(CanvasKit, canvas, resources, paint, text, cx + lineSize * 0.055, y + lineSize * 0.395, lineSize, instance.palette.primary, state.opacity * 0.94);
    drawCenteredText(CanvasKit, canvas, resources, paint, text, cx, y + lineSize * 0.34, lineSize, instance.palette.accent, state.opacity);
  });
}

function drawOfferSwap(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: OfferSwapInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const alternate = state.local >= 0.48;
  const phase = alternate ? (state.local - 0.48) / 0.52 : state.local / 0.48;
  const swapScale = 0.78 + easeInOutCubic(Math.min(1, phase / 0.28)) * 0.22;
  const slideIn = 1 - easeInOutCubic(Math.min(1, phase / 0.3));
  const slideOut = easeInOutCubic(Math.max(0, (phase - 0.72) / 0.28));
  const direction = alternate ? 1 : -1;
  const boxWidth = Math.min(width * 0.72, unit * 1.08);
  const requestedSlideX = direction * (slideIn - slideOut) * unit * (0.16 + instance.intensity * 0.22);
  const maxSlideX = Math.max(0, (width - boxWidth) / 2 - unit * 0.04);
  const slideX = Math.max(-maxSlideX, Math.min(maxSlideX, requestedSlideX));
  const text = (alternate ? instance.params.alternate_text : instance.params.primary_text).toUpperCase();
  const textSize = fittedTextSize(resources, text, unit * 0.09, boxWidth - unit * 0.12);
  canvas.save();
  canvas.translate(cx + slideX, cy);
  canvas.scale(swapScale, swapScale);
  withFill(CanvasKit, paint, alternate ? instance.palette.primary : instance.palette.accent, state.opacity);
  canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(-boxWidth / 2, -unit * 0.1, boxWidth, unit * 0.2), unit * 0.035, unit * 0.035), paint);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, 0, textSize * 0.36, textSize, alternate ? instance.palette.accent : instance.palette.primary, state.opacity);
  canvas.restore();
}

function coverSource(CanvasKit: MotionCanvasKit, image: MotionImage, destWidth: number, destHeight: number): Rect {
  const iw = image.width();
  const ih = image.height();
  const targetRatio = destWidth / destHeight;
  const sourceRatio = iw / ih;
  if (sourceRatio > targetRatio) {
    const width = ih * targetRatio;
    return CanvasKit.XYWHRect((iw - width) / 2, 0, width, ih);
  }
  const height = iw / targetRatio;
  return CanvasKit.XYWHRect(0, (ih - height) / 2, iw, height);
}

function requireImage(resources: MotionResources, assetId: string): MotionImage {
  const image = resources.images.get(assetId);
  if (!image) throw new Error(`Creator Block resource ${assetId} is missing`);
  return image;
}

function drawCardStack(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: CardStackInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const cardW = unit * 0.46;
  const cardH = unit * 0.58;
  const activeIndex = Math.floor(state.cycle * instance.params.assets.length) % instance.params.assets.length;
  [...instance.params.assets].reverse().forEach((asset, reverseIndex) => {
    const index = instance.params.assets.length - 1 - reverseIndex;
    const relative = (index - activeIndex + instance.params.assets.length) % instance.params.assets.length;
    const image = requireImage(resources, asset.asset_id);
    const depth = Math.min(relative, 3);
    const slideX = (1 - state.enter) * -unit * (0.28 + instance.intensity * 0.24)
      + state.exit * unit * (0.3 + instance.intensity * 0.32);
    canvas.save();
    canvas.translate(cx + slideX + depth * unit * 0.025, cy - depth * unit * 0.02);
    canvas.rotate((depth - 1) * 4 + (index === activeIndex ? state.rotation * 0.3 : 0), 0, 0);
    canvas.scale(state.scale * (1 - depth * 0.045), state.scale * (1 - depth * 0.045));
    withFill(CanvasKit, paint, instance.palette.primary, state.opacity);
    canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(-cardW / 2 - unit * 0.012, -cardH / 2 - unit * 0.012, cardW + unit * 0.024, cardH + unit * 0.024), unit * 0.025, unit * 0.025), paint);
    withFill(CanvasKit, paint, instance.palette.accent, state.opacity);
    canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(-cardW / 2 - unit * 0.004, -cardH / 2 - unit * 0.004, cardW + unit * 0.008, cardH + unit * 0.008), unit * 0.02, unit * 0.02), paint);
    const imageInset = unit * 0.006;
    const imageW = cardW - imageInset * 2;
    const imageH = cardH - imageInset * 2;
    canvas.clipRect(CanvasKit.XYWHRect(-imageW / 2, -imageH / 2, imageW, imageH), CanvasKit.ClipOp?.Intersect, true);
    canvas.drawImageRect(image, coverSource(CanvasKit, image, imageW, imageH), CanvasKit.XYWHRect(-imageW / 2, -imageH / 2, imageW, imageH), paint);
    canvas.restore();
  });
}

function drawFilmStrip(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: FilmStripInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const stripW = unit * 0.28;
  const stripH = Math.min(height - unit * 0.1, unit * 0.92);
  const stripTop = cy - stripH / 2;
  const cellH = stripH / 3;
  const travel = state.cycle * cellH * instance.params.assets.length;
  canvas.save();
  canvas.clipRect(CanvasKit.XYWHRect(cx - stripW / 2, stripTop, stripW, stripH), CanvasKit.ClipOp?.Intersect, true);
  for (let loop = -1; loop <= 1; loop += 1) {
    instance.params.assets.forEach((asset, index) => {
      const image = requireImage(resources, asset.asset_id);
      const y = stripTop + index * cellH + loop * instance.params.assets.length * cellH - travel;
      canvas.drawImageRect(image, coverSource(CanvasKit, image, stripW, cellH - unit * 0.012), CanvasKit.XYWHRect(cx - stripW / 2, y, stripW, cellH - unit * 0.012), paint);
    });
  }
  canvas.restore();
  withFill(CanvasKit, paint, instance.palette.accent, state.opacity);
  canvas.drawRect(CanvasKit.XYWHRect(cx - stripW / 2 - unit * 0.012, stripTop, unit * 0.008, stripH), paint);
  canvas.drawRect(CanvasKit.XYWHRect(cx + stripW / 2 + unit * 0.004, stripTop, unit * 0.008, stripH), paint);
  withFill(CanvasKit, paint, instance.palette.primary, state.opacity);
  canvas.drawRect(CanvasKit.XYWHRect(cx - stripW / 2 - unit * 0.012, stripTop, stripW + unit * 0.024, unit * 0.008), paint);
  canvas.drawRect(CanvasKit.XYWHRect(cx - stripW / 2 - unit * 0.012, stripTop + stripH - unit * 0.008, stripW + unit * 0.024, unit * 0.008), paint);
}

function drawArcText(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, text: string, cx: number, cy: number, radius: number, startDegrees: number, size: number, color: string, alpha: number): void {
  const chars = Array.from(text.toUpperCase());
  const step = Math.min(15, 150 / Math.max(1, chars.length - 1));
  const font = resources.font(size);
  withFill(CanvasKit, paint, color, alpha);
  chars.forEach((char, index) => {
    const angle = startDegrees + (index - (chars.length - 1) / 2) * step;
    canvas.save();
    canvas.translate(cx, cy);
    canvas.rotate(angle, 0, 0);
    canvas.translate(0, -radius);
    canvas.drawText(char, -size * 0.29, size * 0.32, paint, font);
    canvas.restore();
  });
}

function drawDonutText(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: DonutTextInstanceV1, frame: number, width: number, height: number): void {
  const state = creatorBlockFrame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const radius = unit * (0.22 + state.pulse * 0.015 * instance.intensity);
  paint.setStyle(CanvasKit.PaintStyle.Stroke);
  paint.setAntiAlias(true);
  paint.setStrokeWidth(unit * 0.045);
  paint.setColor(colorWithAlpha(CanvasKit, instance.palette.accent, state.opacity));
  canvas.drawCircle(cx, cy, radius, paint);
  const phaseAmount = 12 + instance.intensity * 18;
  const leftPhase = Math.sin(state.local * Math.PI * 2) * phaseAmount;
  const rightPhase = Math.sin(state.local * Math.PI * 2 + Math.PI / 2) * phaseAmount * 0.72;
  const arcSize = fittedTextSize(
    resources,
    Array.from(instance.params.left_text).length >= Array.from(instance.params.right_text).length
      ? instance.params.left_text
      : instance.params.right_text,
    unit * 0.045,
    Math.PI * radius * 1.4,
  );
  drawArcText(CanvasKit, canvas, resources, paint, instance.params.left_text, cx, cy, radius * 1.23, leftPhase - 90, arcSize, instance.palette.primary, state.opacity);
  drawArcText(CanvasKit, canvas, resources, paint, instance.params.right_text, cx, cy, radius * 1.23, rightPhase + 90, arcSize, instance.palette.accent, state.opacity);
}

type V2Preset<TPreset extends CreatorBlockInstanceV2["preset_id"]> = Extract<
  CreatorBlockInstanceV2,
  { preset_id: TPreset }
>;

function v2Frame(instance: CreatorBlockInstanceV2, frame: number, offerSwapEvent = false) {
  return creatorBlockFrameV2(
    instance,
    frame,
    creatorBlockEntry(instance.preset_id, 2),
    { offerSwapEvent },
  );
}

function drawKineticWordV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"kinetic_word">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const text = instance.params.text.toUpperCase();
  const size = fittedTextSize(resources, text, unit * 0.145, width - unit * 0.16);
  const entranceScale = 1 + (1 - state.enter) * (0.28 + instance.intensity * 0.2);
  canvas.save();
  canvas.translate(cx, cy);
  canvas.rotate(state.rotation * 1.4, 0, 0);
  canvas.skew((1 - state.enter) * 0.12 - state.exit * 0.12 * instance.intensity, 0);
  canvas.scale(entranceScale * state.scale, entranceScale * state.scale);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, size * 0.045, size * 0.39, size, instance.palette.primary, state.opacity * 0.9);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, 0, size * 0.34, size, instance.palette.accent, state.opacity);
  canvas.restore();
}

function drawTagStackV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"tag_stack">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const maxLabelWidth = width - unit * 0.28;
  const longest = instance.params.labels.reduce((a, b) => Array.from(a).length > Array.from(b).length ? a : b, "").toUpperCase();
  const verticalSize = (height - unit * 0.16) /
    (instance.params.labels.length * 1.5 + (instance.params.labels.length - 1) * 0.5);
  const size = Math.min(verticalSize, fittedTextSize(resources, longest, unit * 0.055, maxLabelWidth - unit * 0.08));
  const rowHeight = size * 1.5;
  const rowStep = size * 2;
  const stackHeight = instance.params.labels.length * rowHeight + (instance.params.labels.length - 1) * size * 0.5;
  instance.params.labels.forEach((label, index) => {
    const rowEnter = easeInOutCubic((state.authoredFrame - index * 3) / 12);
    const drift = Math.sin(state.choreography * Math.PI * 2 + index * 0.72)
      * size * 0.045 * instance.intensity;
    const top = cy - stackHeight / 2 + index * rowStep + drift + (1 - rowEnter) * size * 0.55;
    const w = Math.min(maxLabelWidth, textWidth(resources, label.toUpperCase(), size) + size * 1.5);
    const alpha = state.opacity * rowEnter;
    withFill(CanvasKit, paint, index % 2 ? instance.palette.primary : instance.palette.accent, alpha);
    canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(cx - w / 2, top, w, rowHeight), rowHeight / 2, rowHeight / 2), paint);
    drawCenteredText(CanvasKit, canvas, resources, paint, label.toUpperCase(), cx, top + rowHeight / 2 + size * 0.34, size, index % 2 ? instance.palette.accent : instance.palette.primary, alpha);
  });
}

function drawFlowFieldV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"flow_field">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const headline = instance.params.headline.toUpperCase();
  const size = fittedTextSize(resources, headline, unit * 0.105, width - unit * 0.16);
  const baseline = cy + size * 0.28;
  const glyphTop = baseline - size * 0.82;
  const glyphHeight = size * 1.08;
  drawCenteredText(CanvasKit, canvas, resources, paint, headline, cx, baseline, size, instance.palette.primary, state.opacity * 0.32);
  for (let slice = 0; slice < 8; slice += 1) {
    const offset = Math.sin(state.choreography * Math.PI * 2 + slice * 0.8)
      * unit * 0.018 * instance.intensity * state.enter;
    canvas.save();
    const top = glyphTop + slice * glyphHeight / 8;
    canvas.clipRect(CanvasKit.XYWHRect(0, top, width, glyphHeight / 8 + 1), CanvasKit.ClipOp?.Intersect, true);
    drawCenteredText(CanvasKit, canvas, resources, paint, headline, cx + offset, baseline, size, instance.palette.accent, state.opacity);
    canvas.restore();
  }
  if (instance.params.kicker) {
    const kicker = instance.params.kicker.toUpperCase();
    const kickerSize = fittedTextSize(resources, kicker, size * 0.28, width - unit * 0.2);
    drawCenteredText(CanvasKit, canvas, resources, paint, kicker, cx, baseline + size * 0.92, kickerSize, instance.palette.primary, state.opacity * 0.9);
  }
}

function drawCloudBreakV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"cloud_break">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const settle = easeInOutCubic(state.choreography / 0.72);
  for (let index = 0; index < 5; index += 1) {
    const angle = index * 2.17 + state.choreography * Math.PI * (0.45 + instance.intensity * 0.4);
    const radius = unit * (0.08 + (index % 2) * 0.028) * state.enter;
    const orbit = unit * (0.16 - settle * 0.045);
    withFill(CanvasKit, paint, index % 2 ? instance.palette.primary : instance.palette.accent, state.opacity * 0.78);
    canvas.drawCircle(cx + Math.cos(angle) * orbit, cy + Math.sin(angle) * orbit * 0.82, radius, paint);
  }
  const longest = instance.params.lines.reduce((a, b) => Array.from(a).length > Array.from(b).length ? a : b, "").toUpperCase();
  const lineSize = fittedTextSize(resources, longest, unit * Math.min(0.08, 0.27 / instance.params.lines.length), width - unit * 0.16);
  instance.params.lines.forEach((line, index) => {
    const y = cy + (index - (instance.params.lines.length - 1) / 2) * lineSize * 1.2;
    drawCenteredText(CanvasKit, canvas, resources, paint, line.toUpperCase(), cx + lineSize * 0.045, y + lineSize * 0.39, lineSize, instance.palette.primary, state.opacity * 0.92);
    drawCenteredText(CanvasKit, canvas, resources, paint, line.toUpperCase(), cx, y + lineSize * 0.34, lineSize, instance.palette.accent, state.opacity);
  });
}

function drawOfferSwapV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"offer_swap">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame, true);
  const { unit, cx, cy } = localLayout(width, height);
  const alternate = state.choreographyEvent === "offer-swap";
  const cutAt = creatorBlockEntry("offer_swap", 2).base_choreography_frames * 0.48;
  const phase = alternate
    ? (state.authoredFrame - cutAt) / Math.max(1, creatorBlockEntry("offer_swap", 2).base_choreography_frames - cutAt)
    : state.authoredFrame / Math.max(1, cutAt);
  const swapScale = 0.78 + easeInOutCubic(Math.min(1, phase / 0.28)) * 0.22;
  const slideIn = 1 - easeInOutCubic(Math.min(1, phase / 0.3));
  const slideOut = easeInOutCubic(Math.max(0, (phase - 0.72) / 0.28));
  const direction = alternate ? 1 : -1;
  const boxWidth = Math.min(width * 0.72, unit * 1.08);
  const maxSlideX = Math.max(0, (width - boxWidth) / 2 - unit * 0.04);
  const requestedSlideX = direction * (slideIn - slideOut) * unit * (0.16 + instance.intensity * 0.22);
  const slideX = Math.max(-maxSlideX, Math.min(maxSlideX, requestedSlideX));
  const text = (alternate ? instance.params.alternate_text : instance.params.primary_text).toUpperCase();
  const size = fittedTextSize(resources, text, unit * 0.09, boxWidth - unit * 0.12);
  canvas.save();
  canvas.translate(cx + slideX, cy);
  canvas.scale(swapScale, swapScale);
  withFill(CanvasKit, paint, alternate ? instance.palette.primary : instance.palette.accent, state.opacity);
  canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(-boxWidth / 2, -unit * 0.1, boxWidth, unit * 0.2), unit * 0.035, unit * 0.035), paint);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, 0, size * 0.36, size, alternate ? instance.palette.accent : instance.palette.primary, state.opacity);
  canvas.restore();
}

function drawCardStackV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"card_stack">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const cardW = unit * 0.46;
  const cardH = unit * 0.58;
  const count = instance.params.assets.length;
  [...instance.params.assets].reverse().forEach((asset, reverseIndex) => {
    const index = count - 1 - reverseIndex;
    const pose = continuousCardPose(state.choreography, index, count);
    const { angle, depth } = pose;
    const image = requireImage(resources, asset.asset_id);
    const slideX = (1 - state.enter) * -unit * (0.18 + instance.intensity * 0.16)
      + state.exit * unit * (0.2 + instance.intensity * 0.18);
    canvas.save();
    canvas.translate(cx + slideX + pose.x * unit * 0.065, cy - depth * unit * 0.055);
    canvas.rotate(Math.sin(angle) * 4 * instance.intensity, 0, 0);
    const scale = state.scale * (1 - depth * 0.12);
    canvas.scale(scale, scale);
    withFill(CanvasKit, paint, instance.palette.primary, state.opacity);
    canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(-cardW / 2 - unit * 0.012, -cardH / 2 - unit * 0.012, cardW + unit * 0.024, cardH + unit * 0.024), unit * 0.025, unit * 0.025), paint);
    withFill(CanvasKit, paint, "#FFFFFF", state.opacity);
    const inset = unit * 0.006;
    const imageW = cardW - inset * 2;
    const imageH = cardH - inset * 2;
    canvas.clipRect(CanvasKit.XYWHRect(-imageW / 2, -imageH / 2, imageW, imageH), CanvasKit.ClipOp?.Intersect, true);
    canvas.drawImageRect(image, coverSource(CanvasKit, image, imageW, imageH), CanvasKit.XYWHRect(-imageW / 2, -imageH / 2, imageW, imageH), paint);
    canvas.restore();
  });
}

function drawFilmStripV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"film_strip">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const stripW = unit * 0.28;
  const stripH = Math.min(height - unit * 0.1, unit * 0.92);
  const stripTop = cy - stripH / 2;
  const cellH = stripH / 3;
  const travel = state.choreography * cellH * instance.params.assets.length;
  const imagePaint = new CanvasKit.Paint();
  try {
    withFill(CanvasKit, imagePaint, "#FFFFFF", state.opacity);
    canvas.save();
    canvas.clipRect(CanvasKit.XYWHRect(cx - stripW / 2, stripTop, stripW, stripH), CanvasKit.ClipOp?.Intersect, true);
    for (let loop = -1; loop <= 2; loop += 1) {
      instance.params.assets.forEach((asset, index) => {
        const image = requireImage(resources, asset.asset_id);
        const y = stripTop + index * cellH + loop * instance.params.assets.length * cellH - travel;
        canvas.drawImageRect(image, coverSource(CanvasKit, image, stripW, cellH - unit * 0.012), CanvasKit.XYWHRect(cx - stripW / 2, y, stripW, cellH - unit * 0.012), imagePaint);
      });
    }
    canvas.restore();
  } finally {
    imagePaint.delete();
  }
  withFill(CanvasKit, paint, instance.palette.accent, state.opacity);
  canvas.drawRect(CanvasKit.XYWHRect(cx - stripW / 2 - unit * 0.012, stripTop, unit * 0.008, stripH), paint);
  canvas.drawRect(CanvasKit.XYWHRect(cx + stripW / 2 + unit * 0.004, stripTop, unit * 0.008, stripH), paint);
  withFill(CanvasKit, paint, instance.palette.primary, state.opacity);
  canvas.drawRect(CanvasKit.XYWHRect(cx - stripW / 2 - unit * 0.012, stripTop, stripW + unit * 0.024, unit * 0.008), paint);
  canvas.drawRect(CanvasKit.XYWHRect(cx - stripW / 2 - unit * 0.012, stripTop + stripH - unit * 0.008, stripW + unit * 0.024, unit * 0.008), paint);
}

function drawDonutTextV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: V2Preset<"donut_text">, frame: number, width: number, height: number): void {
  const state = v2Frame(instance, frame);
  const { unit, cx, cy } = localLayout(width, height);
  const radius = unit * (0.22 + state.pulse * 0.012 * instance.intensity);
  paint.setStyle(CanvasKit.PaintStyle.Stroke);
  paint.setAntiAlias(true);
  paint.setStrokeWidth(unit * 0.045);
  paint.setColor(colorWithAlpha(CanvasKit, instance.palette.accent, state.opacity));
  canvas.drawCircle(cx, cy, radius, paint);
  const phase = Math.sin(state.choreography * Math.PI * 2) * (10 + instance.intensity * 16);
  const arcSize = fittedTextSize(resources, Array.from(instance.params.left_text).length >= Array.from(instance.params.right_text).length ? instance.params.left_text : instance.params.right_text, unit * 0.045, Math.PI * radius * 1.4);
  drawArcText(CanvasKit, canvas, resources, paint, instance.params.left_text, cx, cy, radius * 1.23, phase - 90, arcSize, instance.palette.primary, state.opacity);
  drawArcText(CanvasKit, canvas, resources, paint, instance.params.right_text, cx, cy, radius * 1.23, -phase * 0.72 + 90, arcSize, instance.palette.accent, state.opacity);
}

function drawMaskedRun(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, text: string, cx: number, baseline: number, size: number, color: string, alpha: number, reveal: number, order: EvolvingOrder): void {
  if (reveal <= 0 || alpha <= 0) return;
  const width = textWidth(resources, text, size);
  const left = cx - width / 2;
  const shown = width * Math.max(0, Math.min(1, reveal));
  const clipLeft = order === "reverse" ? left + width - shown : order === "center-out" ? cx - shown / 2 : left;
  canvas.save();
  canvas.clipRect(CanvasKit.XYWHRect(clipLeft - 1, baseline - size, shown + 2, size * 1.3), CanvasKit.ClipOp?.Intersect, true);
  drawCenteredText(CanvasKit, canvas, resources, paint, text, cx, baseline, size, color, alpha);
  canvas.restore();
}

function makeOrganicPath(CanvasKit: MotionCanvasKit, nodes: readonly OrganicNode[], radius: number): MotionPath {
  const path = new CanvasKit.Path();
  if (!path.moveTo || !path.cubicTo || !path.close || nodes.length === 0) {
    path.delete();
    throw new Error("CanvasKit mutable path API is unavailable");
  }
  path.moveTo(nodes[0].x * radius, nodes[0].y * radius);
  for (let index = 0; index < nodes.length; index += 1) {
    const current = nodes[index];
    const next = nodes[(index + 1) % nodes.length];
    path.cubicTo(
      current.outX * radius,
      current.outY * radius,
      next.inX * radius,
      next.inY * radius,
      next.x * radius,
      next.y * radius,
    );
  }
  path.close();
  return path;
}

function drawEvolvingTypeV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: EvolvingTypeInstanceV2, frame: number, width: number, height: number): void {
  const state = evolvingTypeFrame(instance, frame, creatorBlockEntry("evolving_type", 2));
  const { unit, cx, cy } = localLayout(width, height);
  withFill(CanvasKit, paint, instance.palette.primary, state.timeline.opacity * instance.params.backdrop_opacity);
  canvas.drawRRect(CanvasKit.RRectXY(CanvasKit.XYWHRect(unit * 0.05, unit * 0.05, width - unit * 0.1, height - unit * 0.1), unit * 0.045, unit * 0.045), paint);
  const headline = instance.params.headline.toUpperCase();
  const headlineSize = fittedTextSize(resources, headline, unit * 0.092 * instance.params.typography_scale, width - unit * 0.18);
  const subtitleSize = fittedTextSize(resources, instance.params.subtitle, headlineSize * 0.3, width - unit * 0.2);
  drawMaskedRun(CanvasKit, canvas, resources, paint, headline, cx, cy - unit * 0.19, headlineSize, instance.palette.accent, state.timeline.opacity, state.headlineReveal, instance.params.order);
  drawMaskedRun(CanvasKit, canvas, resources, paint, instance.params.subtitle, cx, cy - unit * 0.12, subtitleSize, instance.palette.accent, state.timeline.opacity * 0.82, state.subtitleReveal, instance.params.order);
  const radius = unit * (instance.params.layout === "compact" ? 0.07 : 0.06);
  state.icons.forEach((icon, index) => {
    if (icon.scale <= 0) return;
    const iconX = cx + icon.x * unit;
    const iconY = cy + (icon.y + 0.1) * unit;
    const path = makeOrganicPath(CanvasKit, icon.nodes, radius);
    try {
      evolvingIconCopies(instance.params.split_icons, icon.split).forEach(({ direction, alpha }) => {
        canvas.save();
        canvas.translate(iconX + direction * icon.splitOffset * unit, iconY);
        canvas.rotate(icon.rotation * direction, 0, 0);
        canvas.scale(icon.scale, icon.scale);
        withFill(CanvasKit, paint, instance.palette.accent, state.timeline.opacity * alpha);
        canvas.drawPath(path, paint);
        for (let detail = 0; detail < icon.detailCount; detail += 1) {
          const angle = detail / icon.detailCount * Math.PI * 2 + index * 0.7;
          withFill(CanvasKit, paint, instance.palette.primary, state.timeline.opacity * alpha * 0.72);
          canvas.drawCircle(Math.cos(angle) * radius * 0.42, Math.sin(angle) * radius * 0.42, radius * 0.055, paint);
        }
        canvas.restore();
      });
    } finally {
      path.delete();
    }
  });
}

function drawCreatorBlockV2(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: CreatorBlockInstanceV2, frame: number, width: number, height: number): void {
  switch (instance.preset_id) {
    case "kinetic_word": return drawKineticWordV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "tag_stack": return drawTagStackV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "flow_field": return drawFlowFieldV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "cloud_break": return drawCloudBreakV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "offer_swap": return drawOfferSwapV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "card_stack": return drawCardStackV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "film_strip": return drawFilmStripV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "donut_text": return drawDonutTextV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "evolving_type": return drawEvolvingTypeV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
  }
}

function drawCreatorBlock(CanvasKit: MotionCanvasKit, canvas: MotionCanvas, resources: MotionResources, paint: MotionPaint, instance: Exclude<MotionPresetInstance, RouteTraceInstanceV1>, frame: number, width: number, height: number): void {
  if (instance.preset_version === 2) {
    return drawCreatorBlockV2(CanvasKit, canvas, resources, paint, instance, frame, width, height);
  }
  switch (instance.preset_id) {
    case "kinetic_word": return drawKineticWord(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "tag_stack": return drawTagStack(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "flow_field": return drawFlowField(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "cloud_break": return drawCloudBreak(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "offer_swap": return drawOfferSwap(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "card_stack": return drawCardStack(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "film_strip": return drawFilmStrip(CanvasKit, canvas, resources, paint, instance, frame, width, height);
    case "donut_text": return drawDonutText(CanvasKit, canvas, resources, paint, instance, frame, width, height);
  }
}

/** Canonical browser/export draw entrypoint. */
export function drawMotionFrame(
  CanvasKitInput: unknown,
  canvas: MotionCanvas,
  instances: readonly MotionPresetInstance[],
  frame: number,
  width: number,
  height: number,
  resources?: MotionResources,
): void {
  const CanvasKit = CanvasKitInput as MotionCanvasKit;
  canvas.clear(CanvasKit.TRANSPARENT);
  const active = activeMotionInstances(instances, frame);
  if (active.length === 0) return;
  const paint = new CanvasKit.Paint();
  let routePath: MotionPath | null = null;
  try {
    for (const instance of active) {
      if (instance.preset_id === "route_trace") {
        routePath ??= CanvasKit.Path.MakeFromSVGString(ROUTE_TRACE_PATH);
        if (!routePath) throw new Error("Built-in route_trace SVG path is invalid");
        drawRouteTrace(CanvasKit, canvas, routePath, paint, instance, frame, width, height);
      } else {
        if (!resources) throw new Error(`Creator Block ${instance.preset_id} requires trusted resources`);
        drawCreatorBlock(CanvasKit, canvas, resources, paint, instance, frame, width, height);
      }
    }
  } finally {
    paint.delete();
    routePath?.delete();
  }
}
