/**
 * Timeline-editor state machine: draft slots + bounded undo/redo history.
 * Pure (no DOM, no React) — drives `useReducer` in TimelineEditor and is
 * unit-tested in src/__tests__/generative/timeline-reducer.test.ts.
 *
 * Clamping policy: invalid drafts are NEVER produced. An action that would
 * cross a bound is clamped (or rejected outright); rejected/clamped actions
 * bump `clampNonce` + set `clampedKey` so the UI can flash the chip.
 */

import {
  clampInPoint,
  type DraftSlot,
  draftFromTimeline,
  maxGridBeats,
  medianDefaultDuration,
  MAX_TOTAL_SECONDS,
  nextAddedKey,
  SECONDS_FLOOR,
  SECONDS_STEP,
  slotWindows,
  totalBeats,
  totalDurationS,
} from "./timeline-math";
import type { TimelineResponse } from "@/lib/generative-api";

export const HISTORY_LIMIT = 50;

export interface EditorState {
  /** Non-uniform beat grid; [] = seconds mode (original_text). */
  grid: number[];
  /** clip_index → source duration (null when the backend doesn't know it). */
  clipDurations: Record<number, number | null>;
  /** Server baseline the edit count is derived against. */
  baseline: DraftSlot[];
  slots: DraftSlot[];
  past: DraftSlot[][];
  future: DraftSlot[][];
  /** Bumped whenever an action hit a clamp — UI flashes the chip of clampedKey. */
  clampNonce: number;
  clampedKey: string | null;
}

export type EditorAction =
  | { type: "REORDER"; from: number; to: number }
  | { type: "NUDGE"; key: string; delta: 1 | -1 }
  | { type: "SET_IN"; key: string; inS: number; record: boolean }
  | { type: "SWAP"; key: string; clipIndex: number }
  | { type: "REMOVE"; key: string }
  | { type: "RESTORE"; key: string }
  | { type: "ADD"; clipIndex: number }
  | { type: "UNDO" }
  | { type: "REDO" }
  | { type: "RESET_DRAFT"; timeline?: TimelineResponse };

export function initEditorState(timeline: TimelineResponse): EditorState {
  const baseline = draftFromTimeline(timeline);
  const clipDurations: Record<number, number | null> = {};
  for (const c of timeline.clips) clipDurations[c.clip_index] = c.duration_s;
  for (const s of timeline.slots) {
    if (clipDurations[s.clip_index] == null) {
      clipDurations[s.clip_index] = s.source_duration_s;
    }
  }
  return {
    grid: timeline.beat_grid,
    clipDurations,
    baseline,
    slots: baseline.map((s) => ({ ...s })),
    past: [],
    future: [],
    clampNonce: 0,
    clampedKey: null,
  };
}

function withHistory(state: EditorState, nextSlots: DraftSlot[]): EditorState {
  const past = [...state.past, state.slots];
  if (past.length > HISTORY_LIMIT) past.shift();
  return { ...state, slots: nextSlots, past, future: [] };
}

function clampFlash(state: EditorState, key: string | null): EditorState {
  return { ...state, clampNonce: state.clampNonce + 1, clampedKey: key };
}

function sourceDuration(state: EditorState, slot: DraftSlot): number | null {
  return state.clipDurations[slot.clipIndex] ?? null;
}

/** Derived seconds of one slot under the current walk. */
function windowSeconds(state: EditorState, slots: DraftSlot[], key: string): number {
  const idx = slots.findIndex((s) => s.key === key);
  if (idx < 0) return 0;
  return slotWindows(slots, state.grid)[idx].durationS;
}

/** Re-clamp every in-point after a structural change (the beat walk shifts
 * offsets, so derived window lengths — and therefore in-point bounds — move). */
function reclampInPoints(state: EditorState, slots: DraftSlot[]): DraftSlot[] {
  const windows = slotWindows(slots, state.grid);
  return slots.map((slot, i) => {
    if (slot.removed) return slot;
    const clamped = clampInPoint(
      slot.inS,
      windows[i].durationS,
      sourceDuration(state, slot),
    );
    return clamped === slot.inS ? slot : { ...slot, inS: clamped };
  });
}

export function timelineReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case "REORDER": {
      const { from, to } = action;
      if (
        from === to ||
        from < 0 ||
        to < 0 ||
        from >= state.slots.length ||
        to >= state.slots.length
      ) {
        return state;
      }
      const next = [...state.slots];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return withHistory(state, reclampInPoints(state, next));
    }

    case "NUDGE": {
      const slot = state.slots.find((s) => s.key === action.key);
      if (!slot || slot.removed) return state;

      if (state.grid.length > 0 && slot.durationBeats != null) {
        const nextBeats = slot.durationBeats + action.delta;
        if (nextBeats < 1) return clampFlash(state, action.key);
        if (
          action.delta > 0 &&
          totalBeats(state.slots) + 1 > maxGridBeats(state.grid)
        ) {
          return clampFlash(state, action.key); // grid end — beats exhausted
        }
        const next = state.slots.map((s) =>
          s.key === action.key ? { ...s, durationBeats: nextBeats } : s,
        );
        // Does the grown window still fit the source clip (after in-point clamp)?
        const newWindow = windowSeconds(state, next, action.key);
        const src = sourceDuration(state, slot);
        if (src != null && newWindow > src + 1e-6) {
          return clampFlash(state, action.key);
        }
        // The 60s product cap holds in grid mode too.
        if (
          action.delta > 0 &&
          totalDurationS(next, state.grid) > MAX_TOTAL_SECONDS + 1e-6
        ) {
          return clampFlash(state, action.key);
        }
        return withHistory(state, reclampInPoints(state, next));
      }

      // Seconds slot (no-grid variants, or null-beats footage-trimmed slots on
      // grid variants): ±0.5s from the CURRENT value — no multiple-of-0.5
      // requirement, so AI bases like 1.137 nudge to 1.637. 0.6s floor, source
      // fit, total ≤ 60s.
      const dur = slot.durationS ?? 0;
      const nextDur = Math.round((dur + action.delta * SECONDS_STEP) * 1000) / 1000;
      if (nextDur < SECONDS_FLOOR) return clampFlash(state, action.key);
      const src = sourceDuration(state, slot);
      if (src != null && nextDur > src + 1e-6) return clampFlash(state, action.key);
      if (
        action.delta > 0 &&
        totalDurationS(state.slots, state.grid) + SECONDS_STEP > MAX_TOTAL_SECONDS
      ) {
        return clampFlash(state, action.key);
      }
      const next = state.slots.map((s) =>
        s.key === action.key ? { ...s, durationS: nextDur } : s,
      );
      return withHistory(state, reclampInPoints(state, next));
    }

    case "SET_IN": {
      const slot = state.slots.find((s) => s.key === action.key);
      if (!slot || slot.removed) return state;
      const win = windowSeconds(state, state.slots, action.key);
      const clamped = clampInPoint(action.inS, win, sourceDuration(state, slot));
      const next = state.slots.map((s) =>
        s.key === action.key ? { ...s, inS: clamped } : s,
      );
      if (action.record) return withHistory(state, next);
      return { ...state, slots: next };
    }

    case "SWAP": {
      const slot = state.slots.find((s) => s.key === action.key);
      if (!slot || slot.clipIndex === action.clipIndex) return state;
      const next = state.slots.map((s) =>
        s.key === action.key ? { ...s, clipIndex: action.clipIndex, inS: 0 } : s,
      );
      return withHistory(state, reclampInPoints(state, next));
    }

    case "REMOVE": {
      const slot = state.slots.find((s) => s.key === action.key);
      if (!slot || slot.removed) return state;
      const next = state.slots.map((s) =>
        s.key === action.key ? { ...s, removed: true } : s,
      );
      return withHistory(state, reclampInPoints(state, next));
    }

    case "RESTORE": {
      const slot = state.slots.find((s) => s.key === action.key);
      if (!slot || !slot.removed) return state;
      let restored = { ...slot, removed: false };
      if (state.grid.length > 0) {
        const available = maxGridBeats(state.grid) - totalBeats(state.slots);
        const beats = restored.durationBeats ?? 0;
        if (available < 1) return clampFlash(state, action.key);
        if (beats > available) restored = { ...restored, durationBeats: available };
      }
      const next = state.slots.map((s) => (s.key === action.key ? restored : s));
      return withHistory(state, reclampInPoints(state, next));
    }

    case "ADD": {
      const dflt = medianDefaultDuration(state.slots, state.grid);
      let durationBeats = dflt.durationBeats;
      let durationS = dflt.durationS;
      if (state.grid.length > 0) {
        const available = maxGridBeats(state.grid) - totalBeats(state.slots);
        if (available < 1) return clampFlash(state, null);
        let beats = Math.min(durationBeats ?? 1, available);
        // The appended slot starts where the walk ends (null-beats slots never
        // advance the offset).
        const offset = totalBeats(state.slots);
        const windowAt = (b: number) =>
          state.grid[Math.min(offset + b, state.grid.length - 1)] -
          state.grid[Math.min(offset, state.grid.length - 1)];
        // Source fit: clamp the default window to the chosen clip's duration
        // (consistent with NUDGE's source-fit policy).
        const srcDur = state.clipDurations[action.clipIndex];
        if (srcDur != null) {
          while (beats > 1 && windowAt(beats) > srcDur + 1e-6) beats -= 1;
          if (windowAt(beats) > srcDur + 1e-6) return clampFlash(state, null);
        }
        // The 60s product cap holds in grid mode too.
        if (
          totalDurationS(state.slots, state.grid) + windowAt(beats) >
          MAX_TOTAL_SECONDS + 1e-6
        ) {
          return clampFlash(state, null);
        }
        durationBeats = beats;
      } else {
        const room = MAX_TOTAL_SECONDS - totalDurationS(state.slots, state.grid);
        if (room < SECONDS_FLOOR) return clampFlash(state, null);
        durationS = Math.min(durationS ?? SECONDS_FLOOR, Math.floor(room / SECONDS_STEP) * SECONDS_STEP);
        const src = state.clipDurations[action.clipIndex];
        if (src != null) durationS = Math.min(durationS, src);
        durationS = Math.max(SECONDS_FLOOR, Math.round(durationS * 10) / 10);
      }
      const added: DraftSlot = {
        key: nextAddedKey(),
        slotId: null,
        clipIndex: action.clipIndex,
        inS: 0,
        durationBeats,
        durationS,
        removed: false,
        momentDescription: null,
      };
      return withHistory(state, reclampInPoints(state, [...state.slots, added]));
    }

    case "UNDO": {
      if (state.past.length === 0) return state;
      const past = [...state.past];
      const prev = past.pop()!;
      return {
        ...state,
        slots: prev,
        past,
        future: [state.slots, ...state.future],
      };
    }

    case "REDO": {
      if (state.future.length === 0) return state;
      const [next, ...future] = state.future;
      return {
        ...state,
        slots: next,
        past: [...state.past, state.slots],
        future,
      };
    }

    case "RESET_DRAFT": {
      if (action.timeline) return initEditorState(action.timeline);
      return {
        ...state,
        slots: state.baseline.map((s) => ({ ...s })),
        past: [],
        future: [],
      };
    }

    default:
      return state;
  }
}
