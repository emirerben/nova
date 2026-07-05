"use client";

/**
 * Shared timeline playhead: a vertical line that tracks the current video time.
 *
 * Driven by `currentTimeS` / `totalDurationS`. Position is a pure percentage
 * so it stretches correctly inside any relative-positioned lane container.
 *
 * Usage: render inside the lane-content area (the part after the label gutter)
 * with `position: relative; overflow: hidden`.
 *
 * Restyled for the editor shell (plan §6): ink line on the light lanes (the
 * old white/70 line was near-invisible on zinc-50). Pass `withHead` on the
 * top-most lane to draw the rounded ink head that reads as the scrub grip;
 * inner lanes render the line only so the head isn't repeated per row.
 */
interface PlayheadProps {
  currentTimeS: number;
  totalDurationS: number;
  /** Draw the rounded ink head above the line (top lane / ruler only). */
  withHead?: boolean;
}

export function Playhead({ currentTimeS, totalDurationS, withHead = false }: PlayheadProps) {
  if (totalDurationS <= 0) return null;
  const leftPct = Math.max(0, Math.min(100, (currentTimeS / totalDurationS) * 100));
  return (
    <div
      className="pointer-events-none absolute top-0 bottom-0 z-20 w-px bg-[#0c0c0e]/80"
      style={{ left: `${leftPct}%` }}
      aria-hidden
    >
      {withHead && (
        <div className="absolute -top-1 left-1/2 h-2 w-2 -translate-x-1/2 rounded-[2px] bg-[#0c0c0e]" />
      )}
    </div>
  );
}
