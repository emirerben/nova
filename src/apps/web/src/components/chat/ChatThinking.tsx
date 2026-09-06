"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

export const WAIT_LADDER_MS = {
  QUIET: 1500,
  SPECIFIC: 8000,
  LONG: 20000,
} as const;

/**
 * Progressive status adapted from AICSS Thinking State.
 * Source: https://www.aicss.dev/components/thinking-state (MIT, AICSS 2026).
 * Only operational status is shown; internal reasoning is never exposed.
 */
export function ChatThinking({
  active = true,
  elapsedMs,
  initialLabel = "Kria is thinking",
  label = "Reading your direction…",
  specificLabel = "Shaping the edit around your clips…",
  longLabel = "Still working — your direction is saved.",
  onStop,
  stopAfterMs = 5000,
  stopLabel = "Stop",
  className,
}: {
  active?: boolean;
  /** Deterministic elapsed time for visual fixtures and tests. */
  elapsedMs?: number;
  initialLabel?: string;
  label?: string;
  specificLabel?: string;
  longLabel?: string;
  onStop?: () => void;
  stopAfterMs?: number;
  stopLabel?: string;
  className?: string;
}) {
  const [measuredElapsed, setMeasuredElapsed] = useState(0);
  const hasStopAction = onStop !== undefined;
  useEffect(() => {
    if (!active || elapsedMs !== undefined) {
      setMeasuredElapsed(0);
      return;
    }
    setMeasuredElapsed(0);
    const thresholds = [
      WAIT_LADDER_MS.QUIET,
      WAIT_LADDER_MS.SPECIFIC,
      WAIT_LADDER_MS.LONG,
      ...(hasStopAction ? [stopAfterMs] : []),
    ].filter((threshold, index, values) => threshold >= 0 && values.indexOf(threshold) === index);
    const timers = thresholds.map((threshold) =>
      window.setTimeout(() => setMeasuredElapsed(threshold), threshold),
    );
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [active, elapsedMs, hasStopAction, stopAfterMs]);

  if (!active) return null;
  const elapsed = elapsedMs ?? measuredElapsed;

  const text = elapsed >= WAIT_LADDER_MS.LONG
    ? longLabel
    : elapsed >= WAIT_LADDER_MS.SPECIFIC
      ? specificLabel
      : elapsed >= WAIT_LADDER_MS.QUIET
        ? label
        : initialLabel;

  return (
    <div className={cn("mr-auto flex min-h-8 items-center gap-2 text-sm", className)}>
      <span role="status" aria-live="polite">
        <span
          className="bg-gradient-to-r from-muted-foreground via-foreground to-muted-foreground bg-[length:200%_100%] bg-clip-text text-transparent motion-safe:animate-shimmer motion-reduce:bg-none motion-reduce:text-muted-foreground"
        >
          {text}
        </span>
      </span>
      {onStop && elapsed >= stopAfterMs ? (
        <Button type="button" variant="ghost" size="sm" onClick={onStop} className="min-h-11 px-3">
          {stopLabel}
        </Button>
      ) : null}
    </div>
  );
}
