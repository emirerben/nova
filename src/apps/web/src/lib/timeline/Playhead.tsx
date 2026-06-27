"use client";

/**
 * Shared timeline playhead: a vertical line that tracks the current video time.
 *
 * Driven by `currentTimeS` / `totalDurationS`. Position is a pure percentage
 * so it stretches correctly inside any relative-positioned lane container.
 *
 * Usage: render inside the lane-content area (the part after the label gutter)
 * with `position: relative; overflow: hidden`.
 */
interface PlayheadProps {
  currentTimeS: number;
  totalDurationS: number;
}

export function Playhead({ currentTimeS, totalDurationS }: PlayheadProps) {
  if (totalDurationS <= 0) return null;
  const leftPct = Math.max(0, Math.min(100, (currentTimeS / totalDurationS) * 100));
  return (
    <div
      className="pointer-events-none absolute top-0 bottom-0 w-px bg-white/70 z-20"
      style={{ left: `${leftPct}%` }}
      aria-hidden
    />
  );
}
