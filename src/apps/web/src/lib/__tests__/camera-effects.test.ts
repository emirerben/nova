import { cameraScaleAt } from "@/lib/camera-effects";
import type { CameraEffect } from "@/lib/plan-api";

const effect = {
  id: "camera-list-1",
  start_s: 3,
  end_s: 4.2,
  intensity: 0.04,
  easing: "sine_pulse",
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
});
