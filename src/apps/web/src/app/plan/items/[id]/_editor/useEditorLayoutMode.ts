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

import type { EditorTool } from "./ToolRail";

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

/** Embedded editor panes are intentionally feature-complete: the chat shell
 * constrains width, so the drawer overlays the canvas instead of switching the
 * editor down to its mobile/light surface. Direct editor breakpoints remain
 * unchanged. */
export function isEmbeddedEditor(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("embedded") === "1";
}

function readMode(): EditorLayoutMode {
  if (!hasMatchMedia()) return "full";
  if (isEmbeddedEditor()) return "overlay";
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

function readIsDesktopWidth(): boolean {
  if (!hasMatchMedia()) return true;
  return window.matchMedia(DESKTOP_QUERY).matches;
}

function subscribeDesktopWidth(onChange: () => void): () => void {
  if (!hasMatchMedia()) return () => {};
  const desktop = window.matchMedia(DESKTOP_QUERY);
  desktop.addEventListener("change", onChange);
  return () => desktop.removeEventListener("change", onChange);
}

/**
 * ≥1024px of ACTUAL pane width — distinct from `layoutMode`, which an
 * embedded pane (`isEmbeddedEditor()`) forces to "overlay" regardless of its
 * real width. Backs `overlayInspectorDocked` below (KRI-18 Defect B): a
 * narrow embedded overlay pane doesn't have room for both a floating tool
 * drawer and a docked inspector column at once. Reuses `DESKTOP_QUERY` — no
 * new media-query string.
 */
export function useIsDesktopWidth(): boolean {
  return useSyncExternalStore(subscribeDesktopWidth, readIsDesktopWidth, () => true);
}

/**
 * Should the docked InspectorPanel column render in overlay layout mode?
 * Pure and unit-testable without a DOM (KRI-18 Defect B).
 *
 * "full" and "light" are unaffected: "full" already docks the tool drawer as
 * its own grid column (no float, no overlap possible), and "light" never
 * renders the docked inspector column at all (its own bottom Sheet takes
 * over). Only "overlay" is in play, where the floating tool drawer (360px,
 * no right boundary) and the docked inspector column (320px) can overlap.
 *
 * Non-embedded overlay mode only spans 1024-1280px of real width (rail 92 +
 * drawer 360 + inspector 320 = 772px fits), so `isDesktopWidth` is always
 * true there and this is a no-op for every non-embedded pane. Only a
 * narrower EMBEDDED overlay pane — which `isEmbeddedEditor()` forces into
 * "overlay" regardless of its real width — can be under 1024px, and only
 * then, while a drawer is actually open, does this hide the docked column so
 * the open drawer has the canvas + inspector width to itself instead of
 * overlapping it.
 */
export function overlayInspectorDocked({
  layoutMode,
  activeTool,
  isDesktopWidth,
}: {
  layoutMode: EditorLayoutMode;
  activeTool: EditorTool | null;
  isDesktopWidth: boolean;
}): boolean {
  if (layoutMode !== "overlay") return true;
  if (activeTool === null || activeTool === "nova") return true;
  return isDesktopWidth;
}
