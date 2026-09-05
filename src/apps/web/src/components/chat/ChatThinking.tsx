"use client";

import { useEffect, useState } from "react";
import { ChatBubble } from "./ChatBubble";
import { Skeleton } from "@/components/ui/skeleton";

export const WAIT_LADDER_MS = {
  QUIET: 1500,
  SPECIFIC: 8000,
  LONG: 20000,
} as const;

/**
 * Progressive-disclosure "thinking" indicator, rendered inside a
 * shared assistant `ChatBubble` so it sits in the thread like any other
 * turn.
 *
 * The first 1.5 seconds intentionally stay quiet: a generic status line has
 * no useful meaning during the normal request latency. Copy becomes specific
 * to the work as the wait grows, and only the 20s tier says it is taking
 * longer than usual. `onStop` is intentionally omitted — no cancel affordance
 * on this surface yet.
 */
export function ChatThinking({
  active = true,
  label = "Reading your direction…",
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

  const text = elapsed >= WAIT_LADDER_MS.LONG
    ? "Still working — your direction is saved."
    : elapsed >= WAIT_LADDER_MS.SPECIFIC
      ? "Shaping the edit around your clips…"
      : elapsed >= WAIT_LADDER_MS.QUIET
        ? label
        : null;

  return (
    <ChatBubble role="assistant">
      <div role="status" aria-live="polite" className="space-y-2">
        <div className="flex items-center gap-1.5">
          <Skeleton aria-hidden className="h-1.5 w-1.5 rounded-full motion-reduce:animate-none" />
          <Skeleton aria-hidden className="h-1.5 w-1.5 rounded-full motion-reduce:animate-none" />
          <Skeleton aria-hidden className="h-1.5 w-1.5 rounded-full motion-reduce:animate-none" />
          {text ? <span className="ml-1">{text}</span> : <span className="sr-only">Kria is thinking</span>}
        </div>
      </div>
    </ChatBubble>
  );
}
