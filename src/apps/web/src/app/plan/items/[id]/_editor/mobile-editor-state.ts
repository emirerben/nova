/**
 * Pocket-editor chrome state (mobile editor Lane A).
 *
 * Pure reducer — no React. One sheet at a time: any OPEN_* replaces the
 * current sheet and resets the detent to "half". No-op actions return the
 * SAME state object so React bails out of re-rendering.
 */

import type { SheetDetent } from "./Sheet";

export type PocketTool =
  | "text"
  | "captions"
  | "visuals"
  | "sounds"
  | "overlays"
  | "styles";

export type PocketSheet =
  | { kind: "tool"; tool: PocketTool }
  | { kind: "inspector" }
  | null;

export interface PocketState {
  sheet: PocketSheet;
  detent: SheetDetent;
}

export const initialPocketState: PocketState = { sheet: null, detent: "half" };

export type PocketAction =
  | { type: "TOGGLE_TOOL"; tool: PocketTool }
  | { type: "OPEN_TOOL"; tool: PocketTool }
  | { type: "OPEN_INSPECTOR" }
  | { type: "CLOSE_SHEET" }
  | { type: "SET_DETENT"; detent: SheetDetent };

export function pocketReducer(
  state: PocketState,
  action: PocketAction,
): PocketState {
  switch (action.type) {
    case "TOGGLE_TOOL": {
      if (state.sheet?.kind === "tool" && state.sheet.tool === action.tool) {
        // Toggling the open tool closes its sheet.
        return { sheet: null, detent: "half" };
      }
      return { sheet: { kind: "tool", tool: action.tool }, detent: "half" };
    }
    case "OPEN_TOOL": {
      if (
        state.sheet?.kind === "tool" &&
        state.sheet.tool === action.tool &&
        state.detent === "half"
      ) {
        return state; // already open at the reset detent — no-op
      }
      return { sheet: { kind: "tool", tool: action.tool }, detent: "half" };
    }
    case "OPEN_INSPECTOR": {
      if (state.sheet?.kind === "inspector" && state.detent === "half") {
        return state; // already open at the reset detent — no-op
      }
      return { sheet: { kind: "inspector" }, detent: "half" };
    }
    case "CLOSE_SHEET": {
      if (state.sheet === null && state.detent === "half") return state;
      return { sheet: null, detent: "half" };
    }
    case "SET_DETENT": {
      if (state.sheet === null) return state; // no sheet — nothing to re-detent
      if (state.detent === action.detent) return state;
      return { sheet: state.sheet, detent: action.detent };
    }
    default:
      return state;
  }
}
