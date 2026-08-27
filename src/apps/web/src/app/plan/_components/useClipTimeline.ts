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

export interface GuidedTimelineTombstone {
  lane: "text_elements" | "sound_effects" | "media_overlays" | "visual_blocks" | "motion_scenes";
  record_id: string;
  segment_id?: string | null;
  reason?: string | null;
  record: Record<string, unknown>;
}

/** The full handle returned by useClipTimeline and accepted by ClipsLane / InlineClipsEditor. */
export interface ClipTimelineHandle {
  state: EditorState;
  dispatch: React.Dispatch<EditorAction>;
  clips: TimelineClip[];
  /** Complete guided source catalog, including unused uploaded media. */
  sourcePool: Array<Record<string, unknown>>;
  /** Per-slot assembled-time windows: [{startS, durationS}]. Index-aligned with state.slots. */
  windows: ReturnType<typeof slotWindows>;
  /** Total assembled-video duration in seconds (sum of active slot durations). */
  totalS: number;
  loadState: "loading" | "error" | "ready";
  editWideLookPresets: LookPreset[];
  lookPresets: LookPreset[];
  /** Guided Story V2 compare-and-swap token; null on legacy timelines. */
  revisionNumber: number | null;
  /** Render baseline paired with revisionNumber for guided direct-write CAS. */
  baseGeneration: string | null;
  /** Canonical hash of the loaded guided revision. */
  revisionHash: string | null;
  /** Records whose complete segment-relative interval was removed. */
  tombstones: GuidedTimelineTombstone[];
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
  const [sourcePool, setSourcePool] = useState<Array<Record<string, unknown>>>([]);
  const [editWideLookPresets, setEditWideLookPresets] = useState<LookPreset[]>([]);
  const [lookPresets, setLookPresets] = useState<LookPreset[]>([]);
  const [revisionNumber, setRevisionNumber] = useState<number | null>(null);
  const [baseGeneration, setBaseGeneration] = useState<string | null>(null);
  const [revisionHash, setRevisionHash] = useState<string | null>(null);
  const [tombstones, setTombstones] = useState<GuidedTimelineTombstone[]>([]);
  const [state, dispatch] = useReducer(timelineReducer, EMPTY_EDITOR_STATE);
  const requestEpochRef = useRef(0);

  const reload = useCallback(async () => {
    const requestEpoch = requestEpochRef.current + 1;
    requestEpochRef.current = requestEpoch;
    setLoadState("loading");
    setEditWideLookPresets([]);
    setLookPresets([]);
    setRevisionNumber(null);
    setBaseGeneration(null);
    setRevisionHash(null);
    setTombstones([]);
    setSourcePool([]);
    try {
      const data = await getTimeline(ownerId, variantId, base);
      if (requestEpoch !== requestEpochRef.current) return;
      setClips(data.clips);
      setSourcePool(data.source_pool ?? []);
      setEditWideLookPresets(data.edit_wide_look_presets ?? []);
      setLookPresets(data.look_presets ?? data.edit_wide_look_presets ?? []);
      setRevisionNumber(data.revision_number ?? null);
      setBaseGeneration(data.base_generation ?? null);
      setRevisionHash(data.revision_hash ?? null);
      setTombstones((data.tombstones ?? []) as unknown as GuidedTimelineTombstone[]);
      dispatch({ type: "RESET_DRAFT", timeline: data });
      setLoadState("ready");
    } catch {
      if (requestEpoch !== requestEpochRef.current) return;
      setEditWideLookPresets([]);
      setLookPresets([]);
      setRevisionNumber(null);
      setBaseGeneration(null);
      setRevisionHash(null);
      setTombstones([]);
      setSourcePool([]);
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
    sourcePool,
    windows,
    totalS,
    loadState,
    editWideLookPresets,
    lookPresets,
    revisionNumber,
    baseGeneration,
    revisionHash,
    tombstones,
    reload,
  };
}
