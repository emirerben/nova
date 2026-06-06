/**
 * Inline "we're working on it" state for async generation (persona + plan).
 * Replaces the old full-page blank takeover — the stepper stays visible above
 * this, so the user keeps their place in the flow. Shimmering skeleton lines
 * preview the shape of what's coming.
 */
import { useEffect, useState } from "react";
import { formatElapsed } from "@/components/progress/logic";

export default function GeneratingState({
  title,
  subtitle,
  lines = 4,
  startedAt,
}: {
  title: string;
  subtitle: string;
  lines?: number;
  startedAt?: string | null;
}) {
  const [elapsed, setElapsed] = useState<string | null>(null);

  useEffect(() => {
    if (!startedAt) return;
    const tick = () => {
      const ms = Date.now() - new Date(startedAt).getTime();
      setElapsed(formatElapsed(ms));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  return (
    <div className="motion-safe:animate-fade-up py-6">
      {/* Announce the working state once to screen readers (scoped to the heading
          region so polling re-renders elsewhere don't re-trigger it). */}
      <div className="mb-6 flex items-center gap-3" role="status" aria-live="polite">
        <span className="relative flex h-3 w-3" aria-hidden="true">
          <span className="absolute inline-flex h-full w-full motion-safe:animate-ping rounded-full bg-amber-400/70" />
          <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-400" />
        </span>
        <h1 className="font-display text-2xl text-white">{title}</h1>
      </div>
      <p className="mb-8 text-zinc-400">{subtitle}</p>
      {elapsed && (
        <p className="mt-2 text-xs text-zinc-500 tabular-nums">{elapsed}</p>
      )}
      <div className="space-y-3">
        {Array.from({ length: lines }).map((_, i) => (
          <div
            key={i}
            className="h-4 rounded bg-[length:200%_100%] bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900 motion-safe:animate-shimmer"
            style={{ width: `${90 - i * 12}%` }}
          />
        ))}
      </div>
    </div>
  );
}
