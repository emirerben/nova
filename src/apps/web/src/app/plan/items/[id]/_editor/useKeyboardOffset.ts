"use client";

// Shared visualViewport keyboard-offset hook for the pocket editor (Lane A).
// Returns how many px of the layout viewport the on-screen keyboard currently
// hides (0 when closed / unsupported / SSR / below KEYBOARD_OFFSET_THRESHOLD_PX).
//
// This is the ONLY implementation — CopilotDrawer used to keep a private copy
// that never reset to 0 on deactivation; that copy is gone (KRI-19). Consumers
// that gate "is the keyboard open" on `offset > 0` (e.g. Sheet's half→full
// auto-promote) would otherwise auto-promote on iOS Safari toolbar-collapse /
// rubber-band noise, which is nonzero but has nothing to do with the keyboard.

import { useEffect, useState } from "react";

/**
 * Minimum hidden-viewport height (px) before we treat it as "the keyboard is
 * open". iOS Safari toolbar collapse and momentum-scroll overshoot produce a
 * few tens of px of `visualViewport` noise with no keyboard present; even the
 * shortest software keyboards are well over 200px. 120px keeps real keyboards
 * fully covered with margin below the noise floor.
 */
export const KEYBOARD_OFFSET_THRESHOLD_PX = 120;

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
      setOffset(hidden < KEYBOARD_OFFSET_THRESHOLD_PX ? 0 : hidden);
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
