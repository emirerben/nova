"use client";

import { useEffect, useState } from "react";
import { ChatBubble } from "./ChatBubble";
import { Skeleton } from "@/components/ui/skeleton";

const TIER_2_MS = 5000;
const TIER_3_MS = 8000;
const SHIMMER_MS = 2000;

/**
 * Progressive-disclosure "thinking" indicator, modeled on CopilotDrawer's
 * `Thinking` (2s/5s/8s copy escalation + skeleton line), rendered inside a
 * shared assistant `ChatBubble` so it sits in the thread like any other
 * turn.
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

  const showSkeleton = elapsed >= SHIMMER_MS;
  const text =
    elapsed >= TIER_3_MS
      ? "Still working — you can keep typing below."
      : elapsed >= TIER_2_MS
        ? "This is taking a little longer than usual…"
        : label;

  return (
    <ChatBubble role="assistant">
      <div role="status" aria-live="polite" className="space-y-2">
        <div className="flex items-center gap-1.5">
          <Skeleton aria-hidden className="h-1.5 w-1.5 rounded-full" />
          <Skeleton aria-hidden className="h-1.5 w-1.5 rounded-full" />
          <Skeleton aria-hidden className="h-1.5 w-1.5 rounded-full" />
          <span className="ml-1">{text}</span>
        </div>
        {showSkeleton && <Skeleton className="h-4 w-24" />}
      </div>
    </ChatBubble>
  );
}
