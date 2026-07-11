"use client";

import { useMemo, useReducer } from "react";
import { InlineClipsEditor } from "@/app/plan/_components/InlineClipsEditor";
import { initEditorState, timelineReducer } from "@/app/generative/timeline-reducer";
import { slotWindows } from "@/app/generative/timeline-math";
import type { TimelineResponse } from "@/lib/generative-api";

const TIMELINE: TimelineResponse = {
  editable: true,
  reason: null,
  beat_grid: [],
  total_duration_s: 6,
  has_user_edits: false,
  clips: [
    { clip_index: 0, signed_url: null, duration_s: 8, used: true },
    { clip_index: 1, signed_url: null, duration_s: 8, used: true },
    { clip_index: 2, signed_url: null, duration_s: 8, used: true },
  ],
  slots: [
    {
      slot_id: "s1",
      clip_index: 0,
      source_gcs_path: "fixtures/clip-0.mp4",
      source_duration_s: 8,
      in_s: 0,
      duration_s: 2,
      duration_beats: null,
      order: 0,
      moment_energy: null,
      moment_description: "Opening clip",
    },
    {
      slot_id: "s2",
      clip_index: 1,
      source_gcs_path: "fixtures/clip-1.mp4",
      source_duration_s: 8,
      in_s: 0,
      duration_s: 2,
      duration_beats: null,
      order: 1,
      moment_energy: null,
      moment_description: "Middle clip",
    },
    {
      slot_id: "s3",
      clip_index: 2,
      source_gcs_path: "fixtures/clip-2.mp4",
      source_duration_s: 8,
      in_s: 0,
      duration_s: 2,
      duration_beats: null,
      order: 2,
      moment_energy: null,
      moment_description: "Closing clip",
    },
  ],
};

export default function ClipsFixture() {
  const initial = useMemo(() => initEditorState(TIMELINE), []);
  const [state, dispatch] = useReducer(timelineReducer, initial);
  const windows = slotWindows(state.slots, state.grid);
  const slots = state.slots.map((slot, index) => ({
    key: slot.key,
    inS: slot.inS,
    durationS: windows[index]?.durationS ?? slot.durationS ?? 0,
    removed: slot.removed,
  }));

  return (
    <main className="min-h-screen bg-[#fafaf8] px-4 py-6 text-[#0c0c0e]">
      <div className="mx-auto max-w-[760px]">
        <InlineClipsEditor
          ownerId="dev-qa-owner"
          variantId="dev-qa-variant"
          base="generative"
          onRenderEnqueued={() => undefined}
          externalState={state}
          externalDispatch={dispatch}
          externalClips={TIMELINE.clips}
        />
        <div
          id="qa-state"
          data-slots={JSON.stringify(slots)}
          data-past-len={state.past.length}
          aria-hidden="true"
        />
        <div className="min-h-[200vh]" aria-hidden="true" />
      </div>
    </main>
  );
}
