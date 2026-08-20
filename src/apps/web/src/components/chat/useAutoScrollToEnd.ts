"use client";

import { useEffect, useRef, type RefObject } from "react";

/**
 * Scrolls the returned ref's element to its bottom whenever `deps` change —
 * same pattern as CopilotDrawer's thread-scroll effect, extracted for reuse
 * by any bubble-thread surface (CopilotDrawer keeps its own inline copy).
 */
export function useAutoScrollToEnd<T extends HTMLElement>(
  deps: unknown[],
): RefObject<T> {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    // deps is caller-controlled and intentionally spread as the effect's dep list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
