import type { CSSProperties } from "react";

import type { LookAdjustments, LookPreset } from "@/lib/generative-api";

export const CUSTOMIZABLE_LOOK_PRESETS = [
  "olive_film",
  "smoky_split_tone",
] as const satisfies readonly LookPreset[];

export const LOOK_PRESET_LABELS: Record<LookPreset, string> = {
  none: "Original",
  stadium_diffusion: "Stadium Diffusion",
  olive_film: "Olive Film",
  smoky_split_tone: "Smoky Split-Tone",
  golden_hour: "Golden Hour",
  faded_analog: "Faded Analog",
};

export function lookPresetLabel(preset: LookPreset): string {
  return LOOK_PRESET_LABELS[preset];
}

const DEFAULTS: Record<(typeof CUSTOMIZABLE_LOOK_PRESETS)[number], LookAdjustments> = {
  olive_film: {
    intensity: 1,
    warmth: 0,
    contrast: 0,
    grain: 0.18,
    vignette: 0.22,
  },
  smoky_split_tone: {
    intensity: 1,
    warmth: 0,
    contrast: 0,
    grain: 0.36,
    vignette: 0.55,
  },
};

const GRAIN_IMAGE =
  "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.86' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.72'/%3E%3C/svg%3E\")";

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function isCustomizableLook(
  preset: LookPreset,
): preset is (typeof CUSTOMIZABLE_LOOK_PRESETS)[number] {
  return CUSTOMIZABLE_LOOK_PRESETS.includes(
    preset as (typeof CUSTOMIZABLE_LOOK_PRESETS)[number],
  );
}

export function defaultLookAdjustments(preset: LookPreset): LookAdjustments | null {
  if (!isCustomizableLook(preset)) return null;
  return { ...DEFAULTS[preset] };
}

export function resolveLookAdjustments(
  preset: LookPreset,
  value?: LookAdjustments | null,
): LookAdjustments | null {
  const fallback = defaultLookAdjustments(preset);
  if (!fallback) return null;
  if (!value) return fallback;
  return {
    intensity: clamp(value.intensity, 0, 1),
    warmth: clamp(value.warmth, -1, 1),
    contrast: clamp(value.contrast, -1, 1),
    grain: clamp(value.grain, 0, 1),
    vignette: clamp(value.vignette, 0, 1),
  };
}

export function lookAdjustmentsEqual(
  left?: LookAdjustments | null,
  right?: LookAdjustments | null,
): boolean {
  if (left == null || right == null) return left == null && right == null;
  return (
    left.intensity === right.intensity &&
    left.warmth === right.warmth &&
    left.contrast === right.contrast &&
    left.grain === right.grain &&
    left.vignette === right.vignette
  );
}

export interface LookPreviewStyles {
  video: CSSProperties;
  tint: CSSProperties | null;
  grain: CSSProperties | null;
}

/** Browser approximation of the FFmpeg grade, used only before Save. */
export function lookPreviewStyles(
  preset: LookPreset,
  value?: LookAdjustments | null,
): LookPreviewStyles {
  if (preset === "none") return { video: {}, tint: null, grain: null };
  if (preset === "stadium_diffusion") {
    return {
      video: { filter: "contrast(0.94) saturate(0.95) brightness(0.985)" },
      tint: {
        background:
          "radial-gradient(ellipse at 50% 42%, rgba(255,224,190,.055) 0 30%, transparent 52%, rgba(7,24,29,.20) 100%)",
        boxShadow: "inset 0 0 42px rgba(3,12,15,.22)",
      },
      grain: null,
    };
  }
  if (preset === "golden_hour") {
    return {
      video: {
        filter: "brightness(1.025) contrast(1.08) saturate(1.22) sepia(.07)",
      },
      tint: {
        background:
          "linear-gradient(145deg, rgba(255,184,92,.08), transparent 48%, rgba(116,54,12,.06))",
        mixBlendMode: "soft-light",
      },
      grain: null,
    };
  }
  if (preset === "faded_analog") {
    return {
      video: {
        filter: "brightness(1.025) contrast(.93) saturate(.76) sepia(.055)",
      },
      tint: {
        background:
          "linear-gradient(155deg, rgba(46,72,82,.08), rgba(174,126,77,.055) 58%, rgba(87,47,31,.08))",
        boxShadow: "inset 0 0 36px rgba(17,12,10,.18)",
        mixBlendMode: "soft-light",
      },
      grain: {
        backgroundImage: GRAIN_IMAGE,
        backgroundSize: "140px 140px",
        mixBlendMode: "soft-light",
        opacity: 0.12,
      },
    };
  }

  const controls = resolveLookAdjustments(preset, value);
  if (!controls) return { video: {}, tint: null, grain: null };
  const { intensity, warmth, contrast, grain, vignette } = controls;
  const warmthSepia = Math.max(0, warmth) * 0.1;
  const coolHue = Math.min(0, warmth) * 18;
  const vignetteAlpha = 0.34 * vignette;
  const grainStyle: CSSProperties | null =
    grain > 0
      ? {
          backgroundImage: GRAIN_IMAGE,
          backgroundSize: "140px 140px",
          mixBlendMode: "soft-light",
          opacity: 0.28 * grain,
        }
      : null;

  if (preset === "olive_film") {
    return {
      video: {
        filter: [
          `brightness(${1 - 0.012 * intensity})`,
          `contrast(${0.955 + 0.2 * contrast})`,
          `saturate(${1 - 0.11 * intensity})`,
          `sepia(${0.16 * intensity + warmthSepia})`,
          `hue-rotate(${-10 * intensity + coolHue - 8 * warmth}deg)`,
          `blur(${0.22 * intensity}px)`,
        ].join(" "),
      },
      tint: {
        background: `linear-gradient(155deg, rgba(132,93,28,${0.14 * intensity}) 0%, rgba(96,104,35,${0.08 * intensity}) 48%, rgba(18,57,50,${0.10 * intensity}) 100%)`,
        boxShadow: `inset 0 0 48px rgba(17,31,17,${vignetteAlpha})`,
        mixBlendMode: "soft-light",
      },
      grain: grainStyle,
    };
  }

  return {
    video: {
      filter: [
        `brightness(${1 - 0.018 * intensity})`,
        `contrast(${1.055 + 0.2 * contrast})`,
        `saturate(${1 - 0.08 * intensity})`,
        `sepia(${0.10 * intensity + warmthSepia})`,
        `hue-rotate(${-4 * intensity + coolHue - 7 * warmth}deg)`,
        `blur(${0.34 * intensity}px)`,
      ].join(" "),
    },
    tint: {
      background: `linear-gradient(112deg, rgba(128,55,22,${0.18 * intensity}) 0%, rgba(84,57,37,${0.04 * intensity}) 44%, rgba(8,61,67,${0.22 * intensity}) 100%)`,
      boxShadow: `inset 0 0 58px rgba(2,15,18,${vignetteAlpha})`,
      mixBlendMode: "soft-light",
    },
    grain: grainStyle,
  };
}
