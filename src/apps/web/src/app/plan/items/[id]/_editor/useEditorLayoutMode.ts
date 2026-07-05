"use client";

/**
 * useEditorLayoutMode — the responsive-mode switch for the editor shell
 * (plan §10, decision D12). Three modes, breakpoint-driven:
 *
 *  - "full"    (≥1280px): the docked 5-column shell (rail · drawer · canvas ·
 *              inspector · edge rail).
 *  - "overlay" (1024–1280px): the drawer OVERLAYS the canvas instead of
 *              docking; the inspector column stays docked; selecting anything
 *              auto-closes the overlaying drawer.
 *  - "light"   (<1024px): the light-edit surface — canvas + transport +
 *              tap-text-to-edit only. The heavy timeline must NEVER mount here,
 *              so this is a matchMedia hook (SSR-safe via useSyncExternalStore),
 *              NOT CSS-only hiding.
 *
 * The pure `resolveLayoutMode` is exported so the breakpoint logic is unit
 * testable without a DOM, and the hook is driven by matchMedia change events
 * so it flips live on resize / device rotation.
 */

import { useSyncExternalStore } from "react";

export type EditorLayoutMode = "light" | "overlay" | "full";

/** ≥1280 → the full docked shell. */
export const FULL_QUERY = "(min-width: 1280px)";
/** ≥1024 → at least the overlay-drawer desktop shell (below → light). */
export const DESKTOP_QUERY = "(min-width: 1024px)";

/** Pure breakpoint resolution (testable without a DOM). */
export function resolveLayoutMode(isFull: boolean, isDesktop: boolean): EditorLayoutMode {
  if (isFull) return "full";
  if (isDesktop) return "overlay";
  return "light";
}

function hasMatchMedia(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function";
}

function readMode(): EditorLayoutMode {
  if (!hasMatchMedia()) return "full";
  return resolveLayoutMode(
    window.matchMedia(FULL_QUERY).matches,
    window.matchMedia(DESKTOP_QUERY).matches,
  );
}

function subscribe(onChange: () => void): () => void {
  if (!hasMatchMedia()) return () => {};
  const full = window.matchMedia(FULL_QUERY);
  const desktop = window.matchMedia(DESKTOP_QUERY);
  full.addEventListener("change", onChange);
  desktop.addEventListener("change", onChange);
  return () => {
    full.removeEventListener("change", onChange);
    desktop.removeEventListener("change", onChange);
  };
}

/**
 * SSR-safe: the server snapshot is "full" so the first paint on any device
 * assumes the desktop shell; because the real editor only renders AFTER the
 * async variant load resolves (well past hydration), matchMedia has already
 * settled by then and the heavy timeline never mounts on a phone.
 */
export function useEditorLayoutMode(): EditorLayoutMode {
  return useSyncExternalStore(subscribe, readMode, () => "full");
}
