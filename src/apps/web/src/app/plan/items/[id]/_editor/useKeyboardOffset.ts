"use client";

// Shared visualViewport keyboard-offset hook for the pocket editor (Lane A).
// Adapted from CopilotDrawer's private copy — kept separate so the drawer's
// shipped behavior is untouched. Returns how many px of the layout viewport
// the on-screen keyboard currently hides (0 when closed / unsupported / SSR).

import { useEffect, useState } from "react";

export function useKeyboardOffset(active: boolean): number {
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    if (!active || typeof window === "undefined" || !window.visualViewport) {
      // Unsupported browsers (and jsdom) simply report "keyboard closed".
      setOffset(0);
      return;
    }
    const viewport = window.visualViewport;
    const update = () => {
      const hidden = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
      setOffset(hidden);
    };
    update();
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, [active]);
  return offset;
}
