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
  measuredLongTasks: number;
  observedLongTasks: number;
  maxDrawMs: number;
}

export default function MotionPreviewPerformanceFixture() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [result, setResult] = useState<BenchmarkResult>({
    status: "running",
    measuredLongTasks: 0,
    observedLongTasks: 0,
    maxDrawMs: 0,
  });

  useEffect(() => {
    let cancelled = false;
    let surface: Surface | null = null;
    let resources: MotionResources | null = null;
    let observer: PerformanceObserver | null = null;
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
        for (const frame of [0, 30, 60]) {
          drawMotionFrame(CanvasKit, surface.getCanvas(), scenes, frame, 360, 640, resources);
          surface.flush();
        }

        const observedDurations: number[] = [];
        observer = new PerformanceObserver((list) => {
          observedDurations.push(...list.getEntries().map((entry) => entry.duration));
        });
        observer.observe({ type: "longtask", buffered: false });
        const drawDurations: number[] = [];
        for (let index = 0; index < 24; index += 1) {
          await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
          if (cancelled) return;
          const started = performance.now();
          drawMotionFrame(
            CanvasKit,
            surface.getCanvas(),
            scenes,
            (index * 5) % 120,
            360,
            640,
            resources,
          );
          surface.flush();
          drawDurations.push(performance.now() - started);
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (cancelled) return;
        setResult({
          status: "ready",
          measuredLongTasks: drawDurations.filter((duration) => duration > LONG_TASK_MS).length,
          observedLongTasks: observedDurations.filter((duration) => duration > LONG_TASK_MS).length,
          maxDrawMs: Math.max(...drawDurations),
        });
      } catch {
        if (!cancelled) setResult((current) => ({ ...current, status: "failed" }));
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
        data-measured-long-tasks={result.measuredLongTasks}
        data-observed-long-tasks={result.observedLongTasks}
        data-max-draw-ms={result.maxDrawMs.toFixed(3)}
        aria-hidden="true"
      />
    </main>
  );
}
