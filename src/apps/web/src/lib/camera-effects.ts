import type { CameraEffect } from "@/lib/plan-api";

export const CAMERA_EFFECT_MIN_DURATION_S = 0.4;
export const CAMERA_EFFECT_MAX_DURATION_S = 2.0;
export const CAMERA_EFFECT_MAX_INTENSITY = 0.08;

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function normalizeCameraEffect(effect: CameraEffect): CameraEffect {
  const start = Math.max(0, finiteNumber(effect.start_s, 0));
  const requestedEnd = finiteNumber(effect.end_s, start + 1.2);
  const end = Math.min(
    start + CAMERA_EFFECT_MAX_DURATION_S,
    Math.max(start + CAMERA_EFFECT_MIN_DURATION_S, requestedEnd),
  );
  const intensity = Math.min(
    CAMERA_EFFECT_MAX_INTENSITY,
    Math.max(0, finiteNumber(effect.intensity, 0.04)),
  );
  return {
    ...effect,
    token: "semantic_crop_pulse",
    start_s: Math.round(start * 1000) / 1000,
    end_s: Math.round(end * 1000) / 1000,
    intensity: Math.round(intensity * 10000) / 10000,
    easing: "sine_pulse",
    source: effect.source || "user",
  };
}

export function cameraScaleAt(
  effects: readonly CameraEffect[] | null | undefined,
  timeS: number,
): number {
  if (!effects?.length || !Number.isFinite(timeS)) return 1;
  let amount = 0;
  for (const raw of effects) {
    const effect = normalizeCameraEffect(raw);
    const duration = effect.end_s - effect.start_s;
    if (duration <= 0 || timeS < effect.start_s || timeS > effect.end_s) continue;
    const phase = Math.PI * ((timeS - effect.start_s) / duration);
    amount += effect.intensity * Math.sin(phase) ** 2;
  }
  return 1 + Math.min(0.12, amount);
}
