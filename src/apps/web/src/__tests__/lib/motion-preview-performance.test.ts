import { performance } from "node:perf_hooks";
import { readFileSync } from "node:fs";
import CanvasKitInit from "canvaskit-wasm";
import { createMotionResources, drawMotionFrame } from "@nova/motion-runtime/canvaskit";
import {
  activeMotionComplexity,
  createCreatorBlockInstance,
  MOTION_MAX_CONCURRENT_COMPLEXITY,
  peakMotionComplexity,
  validateMotionInstances,
  type MotionPresetInstance,
} from "@nova/motion-runtime";

/**
 * Creator Block preview draw cost, measured RELATIVE to the machine it runs on.
 *
 * This assertion used to be an absolute wall-clock budget ("no more than one
 * draw over 50ms"), which made it a detector of how busy the CI runner was
 * rather than of how expensive our drawing code is. Per-frame draw cost is
 * deterministic — the same frame costs the same every pass — but the whole
 * profile scales with machine speed, and the heaviest frames land right on the
 * 50ms line on a contended shared runner. That is why retrying never helped:
 * all three attempts returned the identical count, because the machine was
 * uniformly slow rather than momentarily interrupted.
 *
 * So instead of asking "was this draw under N milliseconds", we ask "was this
 * draw expensive compared to a fixed reference workload on the same machine, in
 * the same process, at the same moment". Machine speed divides out of that
 * ratio; a genuine slowdown in Creator Block drawing does not.
 */
const CALIBRATION_ITERATIONS = 80;
const SAMPLE_COUNT = 24;
/**
 * The measurement block is repeated, and each side keeps its own CHEAPEST
 * result. Interference can only ever make a block look slower, so the minimum
 * is the sample least polluted by whatever else the runner was doing — and
 * minimising the two costs independently (rather than picking the block with
 * the smallest ratio) keeps the ratio centred on the same value whether the
 * machine is idle or thrashing. A genuine regression raises the draw cost in
 * every block, including the cheapest one.
 */
const MEASUREMENT_BLOCKS = 3;
/** Samples dropped from each end before averaging, so a scheduler preemption
 *  cannot decide the result on its own. */
const TRIM = 2;
/**
 * Ceiling for `min(drawCost) / min(calibrationCost)`, chosen from measurement
 * rather than taste. Sampled on an idle machine and under 3x and 10x CPU
 * contention (the contended runs are exactly the ones that used to fail the old
 * absolute budget, with 3-7 draws over 50ms):
 *
 *   clean, idle          0.510 - 0.518
 *   clean, 3x contention 0.422 - 0.589
 *   clean, 10x           0.405 - 0.513
 *   2x slower draw code  0.866 - 1.149   (across all three load levels)
 *
 * 0.8 sits in the gap: 1.36x above the worst clean sample, and below every
 * sample taken with a doubled draw cost. Because the ratio moves linearly with
 * draw cost, that puts the detection threshold at roughly a 1.55x slowdown.
 *
 * The one thing this could not be measured against locally is CI's
 * architecture — the envelope above is arm64, CI is x86_64, where the two
 * workloads may sit at slightly different relative costs. The observed ratio is
 * logged on every run so the ceiling can be tightened from real CI samples.
 */
const MAX_DRAW_COST_RATIO = 0.8;

function maximumPreviewScenes(): MotionPresetInstance[] {
  return Array.from({ length: 2 }, (_, index) => {
    const scene = createCreatorBlockInstance({
      id: `preview-evolving-${index}`,
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

/**
 * Fixed reference workload built from raw CanvasKit primitives only — no
 * motion-runtime code. It measures what this machine costs per unit of
 * rasterisation work right now, so a regression in our drawing code moves the
 * ratio while a slow or contended runner moves both sides equally.
 */
function drawCalibrationWorkload(
  CanvasKit: any,
  canvas: any,
  font: any,
): void {
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

function timed(run: () => void): number {
  const started = performance.now();
  run();
  return performance.now() - started;
}

describe("Creator Block browser preview performance", () => {
  it("keeps max-budget preview draws cheap relative to the machine's own speed", async () => {
    const scenes = maximumPreviewScenes();
    expect(validateMotionInstances(scenes, 360).ok).toBe(true);
    expect(activeMotionComplexity(scenes)).toBe(960);
    expect(peakMotionComplexity(scenes)).toBe(MOTION_MAX_CONCURRENT_COMPLEXITY);

    const CanvasKit = await CanvasKitInit({
      locateFile: () => `${process.cwd()}/node_modules/canvaskit-wasm/bin/canvaskit.wasm`,
    });
    const surface = CanvasKit.MakeSurface(360, 640);
    expect(surface).not.toBeNull();
    const fontBytes = new Uint8Array(readFileSync(`${process.cwd()}/public/fonts/Inter-Bold.ttf`));
    const resources = createMotionResources(CanvasKit, { font: fontBytes });
    const canvas = surface!.getCanvas();
    const calibrationTypeface = (CanvasKit as any).Typeface.MakeFreeTypeFaceFromData(fontBytes);
    const calibrationFont = new (CanvasKit as any).Font(calibrationTypeface, 24);

    try {
      // Warm both paths so neither statistic pays first-call costs.
      for (const frame of [0, 30, 60]) {
        drawMotionFrame(CanvasKit, canvas, scenes, frame, 360, 640, resources);
        surface!.flush();
      }
      for (let index = 0; index < 5; index += 1) {
        drawCalibrationWorkload(CanvasKit, canvas, calibrationFont);
        surface!.flush();
      }

      const blocks = Array.from({ length: MEASUREMENT_BLOCKS }, () => {
        const calibrations = Array.from({ length: SAMPLE_COUNT }, () =>
          timed(() => {
            drawCalibrationWorkload(CanvasKit, canvas, calibrationFont);
            surface!.flush();
          }),
        );
        const draws = Array.from({ length: SAMPLE_COUNT }, (_, index) =>
          timed(() => {
            drawMotionFrame(CanvasKit, canvas, scenes, (index * 5) % 360, 360, 640, resources);
            surface!.flush();
          }),
        );
        return { calibrationCost: median(calibrations), drawCost: trimmedMean(draws) };
      });

      const drawCost = Math.min(...blocks.map((block) => block.drawCost));
      const calibrationCost = Math.min(...blocks.map((block) => block.calibrationCost));
      const ratio = drawCost / calibrationCost;

      // Logged on every run so the envelope above can be re-derived from real
      // CI samples instead of guessed at.
      console.log(
        `[motion-preview] draw ${drawCost.toFixed(2)}ms / calibration ` +
          `${calibrationCost.toFixed(2)}ms = ratio ${ratio.toFixed(3)} ` +
          `(ceiling ${MAX_DRAW_COST_RATIO}; blocks ` +
          `${blocks.map((b) => `${b.drawCost.toFixed(2)}/${b.calibrationCost.toFixed(2)}`).join(", ")})`,
      );

      // The expected object is the measured one with only the verdict forced,
      // so a failure diff pins the verdict while printing every measured number
      // beside it — a regression is diagnosable from the CI log alone.
      const measured = {
        verdict: ratio <= MAX_DRAW_COST_RATIO ? "within budget" : "over budget",
        ratio: Number(ratio.toFixed(3)),
        ceiling: MAX_DRAW_COST_RATIO,
        drawCostMs: Number(drawCost.toFixed(2)),
        calibrationMs: Number(calibrationCost.toFixed(2)),
      };
      expect(measured).toEqual({ ...measured, verdict: "within budget" });
    } finally {
      calibrationFont.delete();
      calibrationTypeface?.delete();
      resources.delete();
      surface!.delete();
    }
  }, 60_000);
});
