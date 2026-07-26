import {
  activeMotionInstances,
  type MotionPresetInstanceV1,
} from "./contract.ts";
import {
  ROUTE_TRACE_PATH,
  ROUTE_TRACE_VIEWBOX,
  routeTraceFrame,
} from "./presets.ts";

interface Disposable {
  delete(): void;
}

interface MotionPath extends Disposable {}

interface MotionPaint extends Disposable {
  setStyle(value: unknown): void;
  setStrokeCap(value: unknown): void;
  setStrokeJoin(value: unknown): void;
  setAntiAlias(value: boolean): void;
  setStrokeWidth(value: number): void;
  setColor(value: Float32Array): void;
  setPathEffect(value: Disposable | null): void;
}

export interface MotionCanvas {
  clear(color: Float32Array): void;
  save(): number;
  restore(): void;
  translate(x: number, y: number): void;
  scale(x: number, y: number): void;
  drawPath(path: MotionPath, paint: MotionPaint): void;
}

export interface MotionCanvasKit {
  TRANSPARENT: Float32Array;
  parseColorString(value: string): Float32Array;
  multiplyByAlpha(color: Float32Array, alpha: number): Float32Array;
  PaintStyle: { Stroke: unknown };
  StrokeCap: { Round: unknown };
  StrokeJoin: { Round: unknown };
  Path: { MakeFromSVGString(value: string): MotionPath | null };
  Paint: new () => MotionPaint;
  PathEffect: {
    MakeDash(intervals: number[], phase?: number): Disposable;
  };
}

function colorWithAlpha(
  CanvasKit: MotionCanvasKit,
  hex: string,
  alpha: number,
): Float32Array {
  const color = CanvasKit.parseColorString(hex);
  return CanvasKit.multiplyByAlpha(color, Math.max(0, Math.min(1, alpha)));
}

function drawRouteTrace(
  CanvasKit: MotionCanvasKit,
  canvas: MotionCanvas,
  path: MotionPath,
  paint: MotionPaint,
  instance: MotionPresetInstanceV1,
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
  const glowEffect = CanvasKit.PathEffect.MakeDash(
    [Math.max(1, 1900 * state.progress), 1900],
    0,
  );
  paint.setPathEffect(glowEffect);
  canvas.drawPath(path, paint);
  glowEffect.delete();

  paint.setStrokeWidth(Math.max(3, state.strokeWidth * 0.38));
  paint.setColor(colorWithAlpha(CanvasKit, state.accent, state.opacity));
  const traceEffect = CanvasKit.PathEffect.MakeDash(
    [Math.max(1, 1900 * state.progress), 1900],
    0,
  );
  paint.setPathEffect(traceEffect);
  canvas.drawPath(path, paint);
  traceEffect.delete();
  paint.setPathEffect(null);
  canvas.restore();
}

/**
 * Canonical draw entrypoint used by browser and export runtimes. The caller
 * owns the surface; this function owns and deletes all temporary Skia objects.
 */
export function drawMotionFrame(
  CanvasKit: MotionCanvasKit,
  canvas: MotionCanvas,
  instances: readonly MotionPresetInstanceV1[],
  frame: number,
  width: number,
  height: number,
): void {
  canvas.clear(CanvasKit.TRANSPARENT);
  const active = activeMotionInstances(instances, frame);
  if (active.length === 0) return;

  const path = CanvasKit.Path.MakeFromSVGString(ROUTE_TRACE_PATH);
  if (!path) throw new Error("Built-in route_trace SVG path is invalid");
  const paint = new CanvasKit.Paint();
  try {
    for (const instance of active) {
      drawRouteTrace(CanvasKit, canvas, path, paint, instance, frame, width, height);
    }
  } finally {
    paint.delete();
    path.delete();
  }
}
