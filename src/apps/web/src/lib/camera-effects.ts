import type { CameraEffect, CameraEffectEasing } from "@/lib/plan-api";

/**
 * Mirror of `app/pipeline/camera_effects.py`. The editor preview and the
 * ffmpeg export must agree frame-for-frame, so every constant and the whole
 * amount curve below is a deliberate copy of that module — change them
 * together (see the note at the top of the Python file).
 */
export const CAMERA_EFFECT_EASINGS = ["sine_pulse", "ease_in_hold"] as const;
export const CAMERA_EFFECT_MAX_INTENSITY = 0.08;
export const CAMERA_EFFECT_MAX_STACKED_AMOUNT = 0.12;

type EasingBounds = {
  minDurationS: number;
  maxDurationS: number;
  defaultDurationS: number;
  defaultIntensity: number;
};

const EASING_BOUNDS: Record<CameraEffectEasing, EasingBounds> = {
  sine_pulse: {
    minDurationS: 0.4,
    maxDurationS: 2.0,
    defaultDurationS: 1.2,
    defaultIntensity: 0.04,
  },
  ease_in_hold: {
    minDurationS: 0.6,
    maxDurationS: 6.0,
    defaultDurationS: 1.8,
    defaultIntensity: 0.06,
  },
};

/** Legacy names kept for call sites that predate the second easing. */
export const CAMERA_EFFECT_MIN_DURATION_S = EASING_BOUNDS.sine_pulse.minDurationS;
export const CAMERA_EFFECT_MAX_DURATION_S = EASING_BOUNDS.sine_pulse.maxDurationS;
/** Longest window any easing may occupy (timeline drag clamps). */
export const CAMERA_EFFECT_ABS_MAX_DURATION_S = Math.max(
  ...Object.values(EASING_BOUNDS).map((bounds) => bounds.maxDurationS),
);

const HOLD_RAMP_IN_S = 0.5;
const HOLD_RAMP_OUT_S = 0.4;
const HOLD_RAMP_IN_RATIO = 0.35;
const HOLD_RAMP_OUT_RATIO = 0.25;

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function resolveCameraEasing(value: unknown): CameraEffectEasing {
  return CAMERA_EFFECT_EASINGS.includes(value as CameraEffectEasing)
    ? (value as CameraEffectEasing)
    : "sine_pulse";
}

export function cameraEasingBounds(easing: unknown): EasingBounds {
  return EASING_BOUNDS[resolveCameraEasing(easing)];
}

export function cameraHoldRamps(durationS: number): [number, number] {
  const duration = Math.max(1e-3, durationS);
  return [
    Math.max(1e-3, Math.min(HOLD_RAMP_IN_S, duration * HOLD_RAMP_IN_RATIO)),
    Math.max(1e-3, Math.min(HOLD_RAMP_OUT_S, duration * HOLD_RAMP_OUT_RATIO)),
  ];
}

export function normalizeCameraEffect(effect: CameraEffect): CameraEffect {
  const easing = resolveCameraEasing(effect.easing);
  const bounds = EASING_BOUNDS[easing];
  const start = Math.max(0, finiteNumber(effect.start_s, 0));
  const requestedEnd = finiteNumber(effect.end_s, start + bounds.defaultDurationS);
  const end = Math.min(
    start + bounds.maxDurationS,
    Math.max(start + bounds.minDurationS, requestedEnd),
  );
  const intensity = Math.min(
    CAMERA_EFFECT_MAX_INTENSITY,
    Math.max(0, finiteNumber(effect.intensity, bounds.defaultIntensity)),
  );
  return {
    ...effect,
    token: "semantic_crop_pulse",
    start_s: Math.round(start * 1000) / 1000,
    end_s: Math.round(end * 1000) / 1000,
    intensity: Math.round(intensity * 10000) / 10000,
    easing,
    source: effect.source || "user",
  };
}

/** Crop-scale increase contributed by one normalized effect at `timeS`. */
export function cameraEffectAmount(effect: CameraEffect, timeS: number): number {
  const duration = effect.end_s - effect.start_s;
  if (duration <= 0 || timeS < effect.start_s || timeS > effect.end_s) return 0;
  const elapsed = timeS - effect.start_s;
  if (effect.easing === "ease_in_hold") {
    const [rampIn, rampOut] = cameraHoldRamps(duration);
    const rise = Math.sin((Math.PI / 2) * Math.min(1, elapsed / rampIn)) ** 2;
    const fall = Math.sin((Math.PI / 2) * Math.min(1, (duration - elapsed) / rampOut)) ** 2;
    return effect.intensity * Math.min(rise, fall);
  }
  return effect.intensity * Math.sin(Math.PI * (elapsed / duration)) ** 2;
}

export function cameraScaleAt(
  effects: readonly CameraEffect[] | null | undefined,
  timeS: number,
): number {
  if (!effects?.length || !Number.isFinite(timeS)) return 1;
  let amount = 0;
  for (const raw of effects) {
    amount += cameraEffectAmount(normalizeCameraEffect(raw), timeS);
  }
  return 1 + Math.min(CAMERA_EFFECT_MAX_STACKED_AMOUNT, amount);
}
