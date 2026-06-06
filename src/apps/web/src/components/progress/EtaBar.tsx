"use client";

import { BAR_TRANSITION_MS } from "./constants";

interface EtaBarProps {
  /**
   * Bar fill position from 0 to 1, pre-computed by computeBarPosition.
   * aria-valuenow updates ONLY on phase events — this value is passed from
   * the parent which controls when to sample it.
   */
  barPosition: number;
  /** Elapsed milliseconds since job started — displayed as elapsed label. */
  elapsedMs: number;
  /** ETA string from etaLadder, or null when unavailable. */
  etaText: string | null;
}

/**
 * Single amber progress bar with shimmer-tipped fill.
 *
 * D14: 500ms linear width transition (motion-safe:).
 * Accessibility: role="progressbar" with aria-valuenow, aria-valuemin, aria-valuemax.
 * aria-valuenow is derived from barPosition — the parent controls when to update it.
 */
export function EtaBar({ barPosition, elapsedMs, etaText }: EtaBarProps) {
  const pct = Math.round(barPosition * 100);
  const fillPct = `${(barPosition * 100).toFixed(2)}%`;

  const elapsedLabel = formatElapsedDisplay(elapsedMs);

  return (
    <div className="space-y-1.5">
      {/* Bar track */}
      <div
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Render progress"
        className="relative h-1 w-full overflow-hidden rounded-full bg-zinc-800"
      >
        {/* Filled portion */}
        <div
          className="relative h-full rounded-full bg-amber-400 motion-safe:transition-[width] motion-safe:ease-linear"
          style={{
            width: fillPct,
            transitionDuration: `${BAR_TRANSITION_MS}ms`,
          }}
        >
          {/* Shimmer tip — sweeps right at the leading edge */}
          <div
            className="absolute inset-y-0 right-0 w-16 bg-[length:200%_100%] bg-gradient-to-r from-transparent via-amber-200/60 to-transparent motion-safe:animate-shimmer"
            aria-hidden="true"
          />
        </div>
      </div>

      {/* Elapsed + ETA labels */}
      <div className="flex items-center justify-between text-xs text-zinc-500">
        <span>{elapsedLabel}</span>
        {etaText && <span>{etaText}</span>}
      </div>
    </div>
  );
}

function formatElapsedDisplay(elapsedMs: number): string {
  const totalS = Math.floor(Math.max(0, elapsedMs) / 1000);
  const m = Math.floor(totalS / 60);
  const s = totalS % 60;
  if (m === 0) return `${s}s`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}
