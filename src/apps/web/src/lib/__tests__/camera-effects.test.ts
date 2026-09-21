import {
  cameraEasingBounds,
  cameraScaleAt,
  normalizeCameraEffect,
  resolveCameraEasing,
} from "@/lib/camera-effects";
import type { CameraEffect } from "@/lib/plan-api";

const effect = {
  id: "camera-list-1",
  start_s: 3,
  end_s: 4.2,
  intensity: 0.04,
  easing: "sine_pulse",
  source: "smart_captions",
} satisfies CameraEffect;

const zoom = {
  id: "camera-zoom-1",
  start_s: 2,
  end_s: 5,
  intensity: 0.06,
  easing: "ease_in_hold",
  source: "smart_captions",
} satisfies CameraEffect;

describe("cameraScaleAt", () => {
  it("matches the smooth sine-pulse render curve", () => {
    expect(cameraScaleAt([effect], 2.9)).toBe(1);
    expect(cameraScaleAt([effect], 3)).toBeCloseTo(1, 6);
    expect(cameraScaleAt([effect], 3.6)).toBeCloseTo(1.04, 6);
    expect(cameraScaleAt([effect], 4.2)).toBeCloseTo(1, 6);
    expect(cameraScaleAt([effect], 4.3)).toBe(1);
  });

  it("adds overlapping pulses and caps total scale", () => {
    const overlapping = {
      ...effect,
      id: "camera-list-2",
      start_s: 3,
      end_s: 4.2,
      intensity: 0.08,
    } satisfies CameraEffect;

    expect(cameraScaleAt([effect, overlapping], 3.6)).toBeCloseTo(1.12, 6);
  });

  // KRI-7. These numbers are the SAME ones asserted against the ffmpeg export in
  // tests/pipeline/test_camera_emphasis_effect.py — the preview and the render
  // agree, or the creator approves a zoom they don't get.
  it("eases the zoom in, holds it, then eases out", () => {
    expect(cameraScaleAt([zoom], 1.99)).toBe(1);
    expect(cameraScaleAt([zoom], 2)).toBeCloseTo(1, 6);
    // Ramp in = min(0.5, 35% of 3s) = 0.5s.
    expect(cameraScaleAt([zoom], 2.25)).toBeGreaterThan(1);
    expect(cameraScaleAt([zoom], 2.25)).toBeLessThan(1.06);
    expect(cameraScaleAt([zoom], 2.5)).toBeCloseTo(1.06, 6);
    expect(cameraScaleAt([zoom], 3.5)).toBeCloseTo(1.06, 6);
    // Ramp out = min(0.4, 25% of 3s) = 0.4s.
    expect(cameraScaleAt([zoom], 4.6)).toBeCloseTo(1.06, 6);
    expect(cameraScaleAt([zoom], 4.8)).toBeLessThan(1.06);
    expect(cameraScaleAt([zoom], 5)).toBeCloseTo(1, 6);
    expect(cameraScaleAt([zoom], 5.01)).toBe(1);
  });

  // The steepest moment is the 0.4s release, which moves the frame by ~0.8% of
  // its width per frame at 30fps. Past ~1% a zoom starts to read as a step.
  it("never jumps between adjacent frames", () => {
    let previous = cameraScaleAt([zoom], 1.9);
    for (let frame = 58; frame <= 155; frame += 1) {
      const value = cameraScaleAt([zoom], frame / 30);
      expect(Math.abs(value - previous)).toBeLessThan(0.008);
      previous = value;
    }
  });
});

describe("normalizeCameraEffect", () => {
  it("lets a hold run longer than a pulse", () => {
    expect(normalizeCameraEffect({ ...zoom, end_s: 30 }).end_s).toBe(8);
    expect(normalizeCameraEffect({ ...effect, end_s: 30 }).end_s).toBe(5);
  });

  it("falls back to the pulse for an easing it does not know", () => {
    const unknown = normalizeCameraEffect({
      ...zoom,
      easing: "swirl" as CameraEffect["easing"],
      end_s: 9,
    });
    expect(unknown.easing).toBe("sine_pulse");
    expect(unknown.end_s).toBe(4);
    expect(resolveCameraEasing(undefined)).toBe("sine_pulse");
  });

  it("clamps intensity to the shared ceiling", () => {
    expect(normalizeCameraEffect({ ...zoom, intensity: 5 }).intensity).toBe(0.08);
    expect(cameraEasingBounds("ease_in_hold").defaultIntensity).toBe(0.06);
  });
});
