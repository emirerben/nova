"use client";

import { useEffect, useRef, type RefObject } from "react";

/**
 * Scrolls the returned ref's element to its bottom whenever `deps` change.
 * Shared by every bubble-thread surface (CopilotDrawer, EditProposalCard's
 * ConversationThread, AskKriaPanel).
 *
 * The ref can be attached either to a plain scrollable div OR to a shadcn
 * `ScrollArea` (`@/components/ui/scroll-area`) — Radix's ScrollArea root has
 * `overflow-hidden`; the actual scrollable node is its Viewport child,
 * marked `[data-radix-scroll-area-viewport]`. This hook looks for that
 * descendant first and falls back to the ref target itself, so both
 * container styles work without the caller needing to know which one it is.
 */
export function useAutoScrollToEnd<T extends HTMLElement>(
  deps: unknown[],
): RefObject<T> {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const viewport =
      el.querySelector<HTMLElement>("[data-radix-scroll-area-viewport]") ?? el;
    viewport.scrollTop = viewport.scrollHeight;
    // deps is caller-controlled and intentionally spread as the effect's dep list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
