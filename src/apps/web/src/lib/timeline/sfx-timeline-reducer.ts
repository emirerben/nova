/**
 * SFX timeline reducer: bounded undo/redo over SoundEffectPlacement[].
 *
 * Pure (no DOM, no React) — drives `useReducer` in UnifiedTimeline and is
 * unit-tested in __tests__/sfx-timeline-reducer.test.ts.
 *
 * Modeled on app/generative/timeline-reducer.ts (HISTORY_LIMIT, withHistory
 * pattern, past/future arrays). Simpler because SFX placements are a flat list
 * with no beat-grid math.
 */

import type { SoundEffectPlacement } from "@/lib/plan-api";

export const SFX_HISTORY_LIMIT = 50;

export interface SfxEditorState {
  placements: SoundEffectPlacement[];
  /** Snapshots of placements before each mutating action (most recent last). */
  past: SoundEffectPlacement[][];
  /** Snapshots for redo (most recent first). */
  future: SoundEffectPlacement[][];
}

export type SfxEditorAction =
  /** Add a new placement (e.g. from glossary picker or upload). */
  | { type: "ADD"; placement: SoundEffectPlacement }
  /** Move a placement's video-time trigger point. */
  | { type: "MOVE"; id: string; atS: number }
  /** Set gain (0–2). */
  | { type: "SET_GAIN"; id: string; gain: number }
  /** Update trim in/out points (null = no trim on that side). */
  | { type: "TRIM"; id: string; trimStartS: number | null; trimEndS: number | null }
  /** Update display label. */
  | { type: "SET_LABEL"; id: string; label: string }
  /** Remove a placement. */
  | { type: "REMOVE"; id: string }
  /** Step back one mutation in history. */
  | { type: "UNDO" }
  /** Step forward one mutation in history. */
  | { type: "REDO" }
  /** Replace the entire state (e.g. when the parent refreshes from an upload). */
  | { type: "RESET"; placements: SoundEffectPlacement[] };

export function initSfxEditorState(
  placements: SoundEffectPlacement[],
): SfxEditorState {
  return { placements, past: [], future: [] };
}

/** Push current state to past, clear future. Caps history at SFX_HISTORY_LIMIT. */
function withHistory(
  state: SfxEditorState,
  next: SoundEffectPlacement[],
): SfxEditorState {
  const past = [...state.past, state.placements];
  if (past.length > SFX_HISTORY_LIMIT) past.shift();
  return { placements: next, past, future: [] };
}

export function sfxReducer(
  state: SfxEditorState,
  action: SfxEditorAction,
): SfxEditorState {
  switch (action.type) {
    case "ADD":
      return withHistory(state, [...state.placements, action.placement]);

    case "MOVE": {
      const next = state.placements.map((p) =>
        p.id === action.id ? { ...p, at_s: Math.max(0, action.atS) } : p,
      );
      return withHistory(state, next);
    }

    case "SET_GAIN": {
      const gain = Math.min(2, Math.max(0, action.gain));
      const next = state.placements.map((p) =>
        p.id === action.id ? { ...p, gain } : p,
      );
      return withHistory(state, next);
    }

    case "TRIM": {
      const next = state.placements.map((p) =>
        p.id === action.id
          ? { ...p, trim_start_s: action.trimStartS, trim_end_s: action.trimEndS }
          : p,
      );
      return withHistory(state, next);
    }

    case "SET_LABEL": {
      const next = state.placements.map((p) =>
        p.id === action.id ? { ...p, label: action.label } : p,
      );
      return withHistory(state, next);
    }

    case "REMOVE":
      return withHistory(
        state,
        state.placements.filter((p) => p.id !== action.id),
      );

    case "UNDO": {
      if (state.past.length === 0) return state;
      const prev = state.past[state.past.length - 1];
      return {
        placements: prev,
        past: state.past.slice(0, -1),
        future: [state.placements, ...state.future],
      };
    }

    case "REDO": {
      if (state.future.length === 0) return state;
      const next = state.future[0];
      return {
        placements: next,
        past: [...state.past, state.placements],
        future: state.future.slice(1),
      };
    }

    case "RESET":
      return initSfxEditorState(action.placements);

    default:
      return state;
  }
}
