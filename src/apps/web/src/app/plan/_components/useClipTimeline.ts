/**
 * useClipTimeline — shared data hook for the Clips lane.
 *
 * Owns the getTimeline fetch + timelineReducer state + derived slot windows.
 * Used by both ClipsLane (header bars) and InlineClipsEditor (expanded panel)
 * so they share one draft and avoid duplicate fetches.
 *
 * Usage:
 *   const clipHandle = useClipTimeline(itemId, variantId, "plan-item");
 *   // pass clipHandle to ClipsLane (header bars) and InlineClipsEditor (panel)
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  getTimeline,
  type LookPreset,
  type TimelineBase,
  type TimelineClip,
} from "@/lib/generative-api";
import {
  slotWindows,
  totalDurationS,
} from "../../generative/timeline-math";
import {
  initEditorState,
  timelineReducer,
  type EditorState,
  type EditorAction,
} from "../../generative/timeline-reducer";

// ── Types ─────────────────────────────────────────────────────────────────────

export type { EditorState, EditorAction };

/** The full handle returned by useClipTimeline and accepted by ClipsLane / InlineClipsEditor. */
export interface ClipTimelineHandle {
  state: EditorState;
  dispatch: React.Dispatch<EditorAction>;
  clips: TimelineClip[];
  /** Per-slot assembled-time windows: [{startS, durationS}]. Index-aligned with state.slots. */
  windows: ReturnType<typeof slotWindows>;
  /** Total assembled-video duration in seconds (sum of active slot durations). */
  totalS: number;
  loadState: "loading" | "error" | "ready";
  editWideLookPresets: LookPreset[];
  /** Refetch from the server (call after Apply / Reset). */
  reload: () => void;
}

// ── Empty initial state (avoids hydration mismatch) ──────────────────────────

const EMPTY_EDITOR_STATE: EditorState = {
  grid: [],
  clipDurations: {},
  baseline: [],
  slots: [],
  past: [],
  future: [],
  clampNonce: 0,
  clampedKey: null,
};

// ── Hook ─────────────────────────────────────────────────────────────────────

/**
 * Fetch the clip timeline for a variant and manage its editor state.
 *
 * Re-fetches automatically when ownerId/variantId/base change.
 * Call `reload()` after an Apply/Reset to re-sync from the server.
 */
export function useClipTimeline(
  ownerId: string,
  variantId: string,
  base: TimelineBase,
): ClipTimelineHandle {
  const [loadState, setLoadState] = useState<"loading" | "error" | "ready">(
    "loading",
  );
  const [clips, setClips] = useState<TimelineClip[]>([]);
  const [editWideLookPresets, setEditWideLookPresets] = useState<LookPreset[]>([]);
  const [state, dispatch] = useReducer(timelineReducer, EMPTY_EDITOR_STATE);
  const requestEpochRef = useRef(0);

  const reload = useCallback(async () => {
    const requestEpoch = requestEpochRef.current + 1;
    requestEpochRef.current = requestEpoch;
    setLoadState("loading");
    setEditWideLookPresets([]);
    try {
      const data = await getTimeline(ownerId, variantId, base);
      if (requestEpoch !== requestEpochRef.current) return;
      setClips(data.clips);
      setEditWideLookPresets(data.edit_wide_look_presets ?? []);
      dispatch({ type: "RESET_DRAFT", timeline: data });
      setLoadState("ready");
    } catch {
      if (requestEpoch !== requestEpochRef.current) return;
      setEditWideLookPresets([]);
      setLoadState("error");
    }
  }, [ownerId, variantId, base]);

  useEffect(() => {
    void reload();
    return () => {
      requestEpochRef.current += 1;
    };
  }, [reload]);

  const windows = useMemo(
    () => slotWindows(state.slots, state.grid),
    [state.slots, state.grid],
  );

  const totalS = useMemo(
    () => totalDurationS(state.slots, state.grid),
    [state.slots, state.grid],
  );

  return {
    state,
    dispatch,
    clips,
    windows,
    totalS,
    loadState,
    editWideLookPresets,
    reload,
  };
}
