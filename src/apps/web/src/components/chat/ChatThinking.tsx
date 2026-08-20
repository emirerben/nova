"use client";

import { useEffect, useState } from "react";

const TIER_2_MS = 5000;
const TIER_3_MS = 8000;
const SHIMMER_MS = 2000;

/**
 * Progressive-disclosure "thinking" indicator, modeled on CopilotDrawer's
 * `Thinking` (2s/5s/8s copy escalation + shimmer skeleton). Standalone —
 * CopilotDrawer keeps its own inline implementation unchanged.
 *
 * Unlike CopilotDrawer's version, the base copy is visible immediately (t=0):
 * a guided-edit turn can be resumed mid-flight after a page reload, where we
 * don't know how long the server has already been working, so an unexplained
 * silent dot for the first 2s would read as stalled. Escalation still kicks
 * in the longer it runs. `onStop` is intentionally omitted — no cancel
 * affordance on this surface yet.
 */
export function ChatThinking({
  active = true,
  label = "Thinking it through…",
}: {
  active?: boolean;
  label?: string;
}) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const id = window.setInterval(() => setElapsed(Date.now() - started), 250);
    return () => window.clearInterval(id);
  }, [active]);

  const showShimmer = elapsed >= SHIMMER_MS;
  const text =
    elapsed >= TIER_3_MS
      ? "Still working — you can keep typing below."
      : elapsed >= TIER_2_MS
        ? "This is taking a little longer than usual…"
        : label;

  return (
    <div
      role="status"
      aria-live="polite"
      className="mr-auto max-w-[85%] space-y-2 text-[13px] text-[#71717a]"
    >
      <div className="flex items-center gap-2">
        <span aria-hidden className="h-2 w-2 rounded-full bg-lime-600 motion-safe:animate-ping" />
        <span>{text}</span>
      </div>
      {showShimmer && (
        <div className="space-y-1">
          <div className="h-2.5 w-4/5 rounded-full bg-[linear-gradient(90deg,#f4f4f5_25%,#fff_50%,#f4f4f5_75%)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
          <div className="h-2.5 w-1/2 rounded-full bg-[linear-gradient(90deg,#f4f4f5_25%,#fff_50%,#f4f4f5_75%)] bg-[length:200%_100%] motion-safe:animate-shimmer" />
        </div>
      )}
    </div>
  );
}
