"use client";

import { useEffect, useRef, useState } from "react";
import CanvasKitInit, { type Surface } from "canvaskit-wasm";
import { createMotionResources, drawMotionFrame, type MotionResources } from "@nova/motion-runtime/canvaskit";
import {
  activeMotionComplexity,
  createCreatorBlockInstance,
  MOTION_MAX_CONCURRENT_COMPLEXITY,
  peakMotionComplexity,
  validateMotionInstances,
  type MotionPresetInstance,
} from "@nova/motion-runtime";

const LONG_TASK_MS = 50;
// Match motion-preview-performance.test.ts: fixed raw CanvasKit work normalizes
// runner speed without normalizing away a regression in drawMotionFrame.
// Minimize each side independently; taking the smallest ratio would favor
// a preempted calibration sample and could conceal a real slowdown.
const CALIBRATION_ITERATIONS = 80;
const SAMPLE_COUNT = 24;
const MEASUREMENT_BLOCKS = 3;
const TRIM = 2;
const MAX_DRAW_COST_RATIO = 0.8;

function maximumPreviewScenes(): MotionPresetInstance[] {
  return Array.from({ length: 2 }, (_, index) => {
    const scene = createCreatorBlockInstance({
      id: `browser-preview-evolving-${index}`,
      presetId: "evolving_type",
      startFrame: 0,
      endFrameExclusive: 120,
    });
    if (scene.preset_id !== "evolving_type") throw new Error("Expected Evolving Type");
    return {
      ...scene,
      intensity: 1,
      params: {
        ...scene.params,
        headline: "W".repeat(48),
        subtitle: "M".repeat(72),
        icon_count: 5,
        icon_style: "botanical",
        text_stagger_ms: 45,
        icon_stagger_ms: 100,
        morph_amplitude: 1,
        density: "high",
        layout: "spread",
        order: "center-out",
        typography_scale: 2,
        backdrop_opacity: 1,
        split_icons: true,
      },
    };
  });
}

interface BenchmarkResult {
  status: "running" | "ready" | "failed" | "unsupported";
  longTasksDuringBenchmark: number;
  maxDrawMs: number;
  drawCostMs: number;
  calibrationMs: number;
  drawCostRatio: number;
  drawCostCeiling: number;
  drawMultiplier: number;
}

function drawCalibrationWorkload(CanvasKit: any, canvas: any, font: any): void {
  const paint = new CanvasKit.Paint();
  paint.setAntiAlias(true);
  try {
    for (let index = 0; index < CALIBRATION_ITERATIONS; index += 1) {
      paint.setStyle(CanvasKit.PaintStyle.Fill);
      paint.setColor(CanvasKit.parseColorString("#3344ff"));
      canvas.drawRect(CanvasKit.XYWHRect(index % 300, index % 500, 40, 40), paint);
      canvas.drawCircle(index % 320, index % 600, 12, paint);
      const path = new CanvasKit.Path();
      path.moveTo(0, 0);
      path.cubicTo(20, 30, 60, 10, 90, 70);
      path.close();
      canvas.drawPath(path, paint);
      path.delete();
      canvas.drawText("CALIBRATION", index % 200, index % 600, paint, font);
    }
  } finally {
    paint.delete();
  }
}

function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function trimmedMean(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const kept = sorted.slice(TRIM, sorted.length - TRIM);
  return kept.reduce((total, value) => total + value, 0) / kept.length;
}

export default function MotionPreviewPerformanceFixture() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [result, setResult] = useState<BenchmarkResult>({
    status: "running",
    longTasksDuringBenchmark: 0,
    maxDrawMs: 0,
    drawCostMs: 0,
    calibrationMs: 0,
    drawCostRatio: 0,
    drawCostCeiling: MAX_DRAW_COST_RATIO,
    drawMultiplier: 1,
  });

  useEffect(() => {
    let cancelled = false;
    let surface: Surface | null = null;
    let resources: MotionResources | null = null;
    let observer: PerformanceObserver | null = null;
    let calibrationTypeface: any = null;
    let calibrationFont: any = null;
    const drawMultiplier = new URLSearchParams(window.location.search).get("benchmark") === "2x" ? 2 : 1;
    void (async () => {
      try {
        if (!PerformanceObserver.supportedEntryTypes.includes("longtask")) {
          setResult((current) => ({ ...current, status: "unsupported" }));
          return;
        }
        const scenes = maximumPreviewScenes();
        if (
          !validateMotionInstances(scenes, 360).ok ||
          activeMotionComplexity(scenes) !== 960 ||
          peakMotionComplexity(scenes) !== MOTION_MAX_CONCURRENT_COMPLEXITY
        ) {
          throw new Error("Preview benchmark scene is not at the accepted complexity limit");
        }
        const [CanvasKit, fontResponse] = await Promise.all([
          CanvasKitInit({ locateFile: () => "/_motion/canvaskit.wasm" }),
          fetch("/fonts/Inter-Bold.ttf"),
        ]);
        if (!fontResponse.ok) throw new Error("Benchmark font failed to load");
        const font = new Uint8Array(await fontResponse.arrayBuffer());
        if (cancelled || !canvasRef.current) return;
        surface = CanvasKit.MakeSWCanvasSurface(canvasRef.current);
        if (!surface) throw new Error("Benchmark CanvasKit surface failed");
        resources = createMotionResources(CanvasKit, { font });
        const canvas = surface.getCanvas();
        const calibrationCanvasKit = CanvasKit as any;
        calibrationTypeface = calibrationCanvasKit.Typeface.MakeFreeTypeFaceFromData(font.buffer);
        calibrationFont = new calibrationCanvasKit.Font(calibrationTypeface, 24);
        for (const frame of [0, 30, 60]) {
          drawMotionFrame(CanvasKit, canvas, scenes, frame, 360, 640, resources);
          surface.flush();
        }
        for (let index = 0; index < 5; index += 1) {
          drawCalibrationWorkload(CanvasKit, canvas, calibrationFont);
          surface.flush();
        }

        const observedDurations: number[] = [];
        observer = new PerformanceObserver((list) => {
          observedDurations.push(...list.getEntries().map((entry) => entry.duration));
        });
        observer.observe({ type: "longtask", buffered: false });
        const drawDurations: number[] = [];
        const blocks = [] as Array<{ calibrationCost: number; drawCost: number }>;
        for (let block = 0; block < MEASUREMENT_BLOCKS; block += 1) {
          const calibrationDurations: number[] = [];
          for (let index = 0; index < SAMPLE_COUNT; index += 1) {
            await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
            if (cancelled) return;
            const started = performance.now();
            drawCalibrationWorkload(CanvasKit, canvas, calibrationFont);
            surface.flush();
            calibrationDurations.push(performance.now() - started);
          }
          drawDurations.length = 0;
          for (let index = 0; index < SAMPLE_COUNT; index += 1) {
            await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
            if (cancelled) return;
            const started = performance.now();
            for (let repeat = 0; repeat < drawMultiplier; repeat += 1) {
              drawMotionFrame(CanvasKit, canvas, scenes, (index * 5) % 360, 360, 640, resources);
            }
            surface.flush();
            drawDurations.push(performance.now() - started);
          }
          blocks.push({ calibrationCost: median(calibrationDurations), drawCost: trimmedMean(drawDurations) });
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (cancelled) return;
        const drawCostMs = Math.min(...blocks.map((block) => block.drawCost));
        const calibrationMs = Math.min(...blocks.map((block) => block.calibrationCost));
        const drawCostRatio = drawCostMs / calibrationMs;
        setResult({
          status: "ready",
          longTasksDuringBenchmark: observedDurations.filter((duration) => duration > LONG_TASK_MS).length,
          maxDrawMs: Math.max(...drawDurations),
          drawCostMs,
          calibrationMs,
          drawCostRatio,
          drawCostCeiling: MAX_DRAW_COST_RATIO,
          drawMultiplier,
        });
      } catch {
        if (!cancelled) setResult((current) => ({ ...current, status: "failed" }));
      } finally {
        calibrationFont?.delete();
        calibrationTypeface?.delete();
      }
    })();
    return () => {
      cancelled = true;
      observer?.disconnect();
      resources?.delete();
      surface?.delete();
    };
  }, []);

  return (
    <main className="min-h-screen bg-zinc-950 p-6 text-white">
      <canvas ref={canvasRef} width={360} height={640} className="h-[640px] w-[360px]" />
      <div
        id="qa-state"
        data-status={result.status}
        data-long-tasks-during-benchmark={result.longTasksDuringBenchmark}
        data-max-draw-ms={result.maxDrawMs.toFixed(3)}
        data-draw-cost-ms={result.drawCostMs.toFixed(3)}
        data-calibration-ms={result.calibrationMs.toFixed(3)}
        data-draw-cost-ratio={result.drawCostRatio.toFixed(3)}
        data-draw-cost-ceiling={result.drawCostCeiling.toFixed(3)}
        data-draw-multiplier={result.drawMultiplier}
        aria-hidden="true"
      />
    </main>
  );
}
