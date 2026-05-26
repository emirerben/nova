"use client";

import { useEffect, useMemo, useState } from "react";

import {
  type LyricsConfig,
  type LyricsConfigOverride,
  adminPatchLyricsConfig,
} from "@/lib/music-api";

type TimingAction = "preview" | "full_test";

interface LyricsTimingPanelProps {
  trackId: string;
  savedConfig: Partial<LyricsConfig>;
  fullTestDisabled?: boolean;
  fullTestHint?: string | null;
  onSubmit: (action: TimingAction, override: LyricsConfigOverride) => void;
  onWorkingChange?: (override: LyricsConfigOverride) => void;
  onSaved?: (savedConfig: Partial<LyricsConfig>) => void;
}

const DEFAULTS: Required<Omit<LyricsConfigOverride, "font_family">> = {
  pre_roll_s: 0.1,
  post_dwell_s: 1.0,
  next_line_gap_s: 0.1,
  fade_in_ms: 150,
  fade_out_ms: 250,
  hold_to_next_threshold_ms: 500,
};

const FIELDS: Array<{
  key: keyof typeof DEFAULTS;
  label: string;
  min: number;
  max: number;
  step: number;
}> = [
  { key: "pre_roll_s", label: "Pre-roll", min: 0, max: 0.5, step: 0.01 },
  { key: "post_dwell_s", label: "Post-dwell", min: 0, max: 2, step: 0.01 },
  { key: "next_line_gap_s", label: "Next-line gap", min: 0, max: 0.5, step: 0.01 },
  { key: "fade_in_ms", label: "Fade in (solo / legacy only)", min: 0, max: 500, step: 10 },
  { key: "fade_out_ms", label: "Fade out (solo / legacy only)", min: 0, max: 800, step: 10 },
  {
    key: "hold_to_next_threshold_ms",
    label: "Hold-to-next",
    min: 0,
    max: 1500,
    step: 25,
  },
];

type NormalizedLyricsConfig = {
  pre_roll_s: string;
  post_dwell_s: string;
  next_line_gap_s: string;
  fade_in_ms: number;
  fade_out_ms: number;
  hold_to_next_threshold_ms: number;
};

function coerceTimingConfig(cfg: Partial<LyricsConfig>): LyricsConfigOverride {
  return {
    pre_roll_s: Number(cfg.pre_roll_s ?? DEFAULTS.pre_roll_s),
    post_dwell_s: Number(cfg.post_dwell_s ?? DEFAULTS.post_dwell_s),
    next_line_gap_s: Number(cfg.next_line_gap_s ?? DEFAULTS.next_line_gap_s),
    fade_in_ms: Math.round(Number(cfg.fade_in_ms ?? DEFAULTS.fade_in_ms)),
    fade_out_ms: Math.round(Number(cfg.fade_out_ms ?? DEFAULTS.fade_out_ms)),
    hold_to_next_threshold_ms: Math.round(
      Number(cfg.hold_to_next_threshold_ms ?? DEFAULTS.hold_to_next_threshold_ms),
    ),
  };
}

export function normalizeLyricsConfig(
  cfg: Partial<LyricsConfig>,
): NormalizedLyricsConfig {
  const coerced = coerceTimingConfig(cfg);
  return {
    pre_roll_s: Number(coerced.pre_roll_s).toFixed(3),
    post_dwell_s: Number(coerced.post_dwell_s).toFixed(3),
    next_line_gap_s: Number(coerced.next_line_gap_s).toFixed(3),
    fade_in_ms: Math.round(Number(coerced.fade_in_ms)),
    fade_out_ms: Math.round(Number(coerced.fade_out_ms)),
    hold_to_next_threshold_ms: Math.round(
      Number(coerced.hold_to_next_threshold_ms),
    ),
  };
}

export function LyricsTimingPanel({
  trackId,
  savedConfig,
  fullTestDisabled = false,
  fullTestHint = null,
  onSubmit,
  onWorkingChange,
  onSaved,
}: LyricsTimingPanelProps): JSX.Element {
  const [saved, setSaved] = useState<Partial<LyricsConfig>>(savedConfig);
  const [working, setWorking] = useState<LyricsConfigOverride>(() =>
    coerceTimingConfig(savedConfig),
  );
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    setSaved(savedConfig);
    setWorking(coerceTimingConfig(savedConfig));
  }, [savedConfig]);

  const dirty = useMemo(
    () =>
      JSON.stringify(normalizeLyricsConfig(working)) !==
      JSON.stringify(normalizeLyricsConfig(saved)),
    [working, saved],
  );

  useEffect(() => {
    onWorkingChange?.({ ...working });
  }, [working, onWorkingChange]);

  function setField(key: keyof typeof DEFAULTS, value: string) {
    const numeric = value === "" ? DEFAULTS[key] : Number(value);
    setWorking((prev) => ({
      ...prev,
      [key]: Number.isFinite(numeric) ? numeric : DEFAULTS[key],
    }));
  }

  async function saveDefaults() {
    setSaving(true);
    setMessage(null);
    try {
      const resp = await adminPatchLyricsConfig(trackId, { ...working });
      setSaved(resp.lyrics_config);
      setWorking(coerceTimingConfig(resp.lyrics_config));
      onSaved?.(resp.lyrics_config);
      setMessage("Saved.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="bg-zinc-900 rounded-xl border border-zinc-700 p-5">
      <div className="flex items-center justify-between gap-3 mb-4">
        <div>
          <h2 className="font-semibold text-sm uppercase tracking-wide text-zinc-400">
            Lyrics timing
          </h2>
        </div>
        {dirty && (
          <span className="text-xs rounded border border-amber-700 bg-amber-950/40 px-2 py-1 text-amber-200">
            Rendering with unsaved lyric timing overrides.
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {FIELDS.map((field) => (
          <label key={field.key} className="block">
            <span className="mb-1 block text-xs text-zinc-500">{field.label}</span>
            <input
              type="number"
              min={field.min}
              max={field.max}
              step={field.step}
              value={working[field.key] ?? DEFAULTS[field.key]}
              onChange={(e) => setField(field.key, e.target.value)}
              className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
            />
          </label>
        ))}
      </div>

      <p
        data-testid="lyrics-timing-crossfade-note"
        className="mt-3 text-xs text-zinc-500"
      >
        Inter-line lyric transitions use automatic crossfade timing to prevent
        stacked text. The fade sliders above apply only to solo / last-line
        fades and to the kill-switch-off legacy path.
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => onSubmit("preview", { ...working })}
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-emerald-600"
        >
          Preview lyrics only
        </button>
        <button
          type="button"
          disabled={fullTestDisabled}
          onClick={() => onSubmit("full_test", { ...working })}
          className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Render full test job
        </button>
        <button
          type="button"
          disabled={!dirty || saving}
          onClick={saveDefaults}
          className="rounded-lg bg-zinc-700 px-4 py-2 text-sm font-semibold text-zinc-100 transition-colors hover:bg-zinc-600 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save as track defaults"}
        </button>
        {dirty && (
          <button
            type="button"
            onClick={() => setWorking(coerceTimingConfig(saved))}
            className="text-xs text-zinc-400 hover:text-zinc-100"
          >
            Reset to saved
          </button>
        )}
        {fullTestHint && <span className="text-xs text-amber-400">{fullTestHint}</span>}
        {message && <span className="text-xs text-zinc-400">{message}</span>}
      </div>
    </div>
  );
}
