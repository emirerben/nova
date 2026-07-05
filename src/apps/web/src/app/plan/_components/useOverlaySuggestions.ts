"use client";

/**
 * useOverlaySuggestionState — lifted working state for AI overlay suggestions
 * (plans/006 T3, implementing 005-4A's lane rendering).
 *
 * The item page owns this state and shares it between:
 *   - SuggestionRail (controlled `rows`/`keptIds`): fetch/stage/Apply/Dismiss
 *     review index — Apply POSTs the CURRENT envelopes, so lane edits ride
 *     along automatically.
 *   - UnifiedTimeline lanes (`laneEntries` + `onSuggestionEdit`): pending
 *     suggestions render as editable provenance cards; any lane edit patches
 *     the embedded overlay dict AND implicitly stages the row (005-4A).
 *
 * Stage-fires-no-network contract: nothing here talks to the API. Suggestions
 * only persist via the rail's Apply.
 */

import { useCallback, useMemo, useState } from "react";
import type { MediaOverlay, OverlaySuggestion } from "@/lib/plan-api";
import type { SuggestionLaneEntry } from "./UnifiedTimelineTypes";

export interface OverlaySuggestionState {
  /** Working copy of the pending (non-rejected) suggestions. */
  rows: OverlaySuggestion[];
  setRows: React.Dispatch<React.SetStateAction<OverlaySuggestion[]>>;
  /** Rows the user ✓-staged (or lane-edited) — solid styling in rail + lanes. */
  keptIds: Set<string>;
  setKeptIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  /** Lane edit → patch the embedded overlay + implicitly stage the row. */
  onSuggestionEdit: (suggestionId: string, patch: Partial<MediaOverlay>) => void;
  /** View model for the timeline lanes (provenance cards + sfx diamonds). */
  laneEntries: SuggestionLaneEntry[];
}

export function useOverlaySuggestionState(): OverlaySuggestionState {
  const [rows, setRows] = useState<OverlaySuggestion[]>([]);
  const [keptIds, setKeptIds] = useState<Set<string>>(new Set());

  const onSuggestionEdit = useCallback(
    (suggestionId: string, patch: Partial<MediaOverlay>) => {
      setRows((prev) =>
        prev.map((r) =>
          r.id === suggestionId ? { ...r, overlay: { ...r.overlay, ...patch } } : r,
        ),
      );
      // Editing implicitly stages the suggestion (005-4A semantics):
      // dashed→solid + ✦ fade in both the rail row and the lane card.
      setKeptIds((prev) => {
        if (prev.has(suggestionId)) return prev;
        const next = new Set(prev);
        next.add(suggestionId);
        return next;
      });
    },
    [],
  );

  const laneEntries = useMemo<SuggestionLaneEntry[]>(
    () =>
      rows.map((r) => ({
        id: r.id,
        overlay: r.overlay,
        sfx: r.sfx,
        staged: keptIds.has(r.id),
      })),
    [rows, keptIds],
  );

  return { rows, setRows, keptIds, setKeptIds, onSuggestionEdit, laneEntries };
}
