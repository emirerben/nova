"use client";

/**
 * useEditorSelection — the single selection store for the TikTok-parity
 * editor shell (plan §5). One selection at a time, across every surface:
 * canvas text click, timeline bar click, "Add text", preset-apply.
 *
 * Drives: canvas handles-box, inspector content, edge-rail Basic tab
 * enablement — and, when the timeline task lands, the bar's lime ring +
 * toolbar delete/split enablement (the store is exported for it to consume).
 *
 * The interaction rules (escape ladder, overlap click-cycling, delete-key
 * focus guard) live here as PURE functions so they are unit-testable without
 * DOM plumbing (src/__tests__/useEditorSelection.test.ts).
 */

import { useCallback, useState } from "react";

export type EditorSelectionKind =
  | "text"
  | "visual"
  | "clip"
  | "sfx"
  | "overlay"
  | "music";

export interface EditorSelection {
  kind: EditorSelectionKind;
  id: string;
}

// ── Pure interaction logic ────────────────────────────────────────────────────

/** Selection equality (null-safe). */
export function sameSelection(
  a: EditorSelection | null,
  b: EditorSelection | null,
): boolean {
  if (a === null || b === null) return a === b;
  return a.kind === b.kind && a.id === b.id;
}

/**
 * Overlap click-cycling (plan §5, Figma/TikTok convention).
 *
 * `hitsTopFirst` = element ids under the click point, TOPMOST FIRST
 * (render order: last-in-array = top). Returns the id to select:
 * - no hits → null (empty canvas / video surface = deselect)
 * - current selection not among the hits → the topmost hit
 * - current selection among the hits → the next one UNDERNEATH (wrapping),
 *   so repeated clicks at the same point cycle through the stack.
 */
export function cycleHit(
  hitsTopFirst: readonly string[],
  currentId: string | null,
): string | null {
  if (hitsTopFirst.length === 0) return null;
  if (currentId === null) return hitsTopFirst[0];
  const idx = hitsTopFirst.indexOf(currentId);
  if (idx === -1) return hitsTopFirst[0];
  return hitsTopFirst[(idx + 1) % hitsTopFirst.length];
}

/** Round to 0.1s — the reducer's timing grid (MOVE_BAR rounds identically). */
function round1(s: number): number {
  return Math.round(s * 10) / 10;
}

/**
 * Arrow-key nudge math (plan §5/§10): move the selected bar ±deltaS,
 * PRESERVING its duration and clamping to [0, durationS]. Returns the new
 * start_s (feed to MOVE_BAR, which re-derives end_s the same way). Pure so the
 * timing math is unit-testable without keyboard/DOM plumbing.
 *
 *  - ±0.1s per arrow, ±1s with Shift (the caller picks deltaS).
 *  - never lets the bar's start go below 0.
 *  - never lets the bar's END exceed durationS (when a duration is known;
 *    durationS ≤ 0 means "unknown", so only the low clamp applies).
 */
export function nudgeBarStart(
  bar: { start_s: number; end_s: number },
  deltaS: number,
  durationS: number,
): number {
  const length = bar.end_s - bar.start_s;
  let start = round1(bar.start_s + deltaS);
  if (start < 0) start = 0;
  if (durationS > 0) {
    const maxStart = round1(durationS - length);
    if (start > maxStart) start = Math.max(0, maxStart);
  }
  return round1(start);
}

export type EscapeAction = "close-drawer" | "clear-selection" | "none";

/**
 * Escape precedence ladder (plan §9): closes the drawer if open → else
 * clears selection → else nothing. One press, one effect.
 */
export function escapeAction(state: {
  drawerOpen: boolean;
  hasSelection: boolean;
}): EscapeAction {
  if (state.drawerOpen) return "close-drawer";
  if (state.hasSelection) return "clear-selection";
  return "none";
}

/**
 * Delete-key focus guard (plan §5): Delete/Backspace removes the selected
 * element ONLY when keyboard focus is not inside a text-entry surface —
 * otherwise the user is editing text and the keystroke belongs to the field.
 */
export function deleteKeyAllowed(
  target: { tagName?: string; isContentEditable?: boolean } | null,
): boolean {
  if (!target) return true;
  if (target.isContentEditable) return false;
  const tag = (target.tagName ?? "").toUpperCase();
  return tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT";
}

// ── Store ─────────────────────────────────────────────────────────────────────

export interface EditorSelectionStore {
  selection: EditorSelection | null;
  /** Select an element (no-op when already selected — avoids re-render churn). */
  select: (kind: EditorSelectionKind, id: string) => void;
  /** Clear the selection (Escape / empty-canvas click / close X). */
  clear: () => void;
}

export function useEditorSelection(): EditorSelectionStore {
  const [selection, setSelection] = useState<EditorSelection | null>(null);

  const select = useCallback((kind: EditorSelectionKind, id: string) => {
    setSelection((prev) => (sameSelection(prev, { kind, id }) ? prev : { kind, id }));
  }, []);

  const clear = useCallback(() => setSelection(null), []);

  return { selection, select, clear };
}
