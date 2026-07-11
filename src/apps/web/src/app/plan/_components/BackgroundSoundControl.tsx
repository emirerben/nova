"use client";

import { useCallback, useRef, useState } from "react";
import { setPlanItemNarratedBedLevel } from "@/lib/plan-api";

// Commit only on drag-release or after a quiet period — each commit dispatches a
// REAL Celery re-render (re-mixes the audio bed, re-runs the clip assembly), not a
// cheap local update. A live onChange-per-tick handler would spam that reburn and
// queue wasted renders (flagged in the eng review's Performance finding).
const COMMIT_DEBOUNCE_MS = 800;

/**
 * Post-gen editor control for a narrated variant's background-sound (voice/bed)
 * level. Narrated-only — talking-to-camera keeps its own clip audio as the only
 * track, so there is nothing to duck under a voice that doesn't exist.
 */
export default function BackgroundSoundControl({
  itemId,
  variantId,
  initialBedLevel,
  rendering = false,
  onCommitted,
}: {
  itemId: string;
  variantId: string;
  /** Persisted `voiceover_bed_level` — null means Kria's render-time default. */
  initialBedLevel: number | null;
  rendering?: boolean;
  /** Called once the reburn actually dispatches (parent flips render_status). */
  onCommitted?: () => void;
}) {
  const DEFAULT_LEVEL = 0.25;
  const [level, setLevel] = useState(initialBedLevel ?? DEFAULT_LEVEL);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const commit = useCallback(
    (value: number) => {
      setSaving(true);
      setError(null);
      setPlanItemNarratedBedLevel(itemId, variantId, value)
        .then(() => onCommitted?.())
        .catch((e) => setError(e instanceof Error ? e.message : "Couldn't update background sound"))
        .finally(() => setSaving(false));
    },
    [itemId, variantId, onCommitted],
  );

  const handleChange = useCallback(
    (value: number) => {
      setLevel(value);
      if (commitTimer.current) clearTimeout(commitTimer.current);
      commitTimer.current = setTimeout(() => commit(value), COMMIT_DEBOUNCE_MS);
    },
    [commit],
  );

  const handleRelease = useCallback(() => {
    if (commitTimer.current) clearTimeout(commitTimer.current);
    commit(level);
  }, [commit, level]);

  const busy = saving || rendering;

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-400">
        Background sound
      </p>
      <p className="mb-3 text-sm text-[#71717a]">
        How loud your original clip audio plays under your voice. Kria ducks it
        automatically while you&apos;re talking.
      </p>
      <div className="flex items-center gap-3">
        <span className="text-xs text-zinc-400">Off</span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={level}
          disabled={busy}
          onChange={(e) => handleChange(Number(e.target.value))}
          // Commit immediately on release — the debounce above is a safety net for
          // keyboard/touch paths that don't fire pointerUp cleanly.
          onPointerUp={handleRelease}
          onKeyUp={handleRelease}
          className="h-1 flex-1 cursor-pointer accent-lime-600 disabled:cursor-not-allowed disabled:opacity-50"
          aria-label="Original video background sound level"
        />
        <span className="text-xs text-zinc-400">Loud</span>
      </div>
      <p className="mt-1 text-xs text-lime-700">
        {rendering
          ? "Applying…"
          : saving
            ? "Saving…"
            : `Original audio at ${Math.round(level * 100)}%.`}
      </p>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}
