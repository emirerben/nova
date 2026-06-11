/**
 * Timeline-editor reducer + beat-walk math tests.
 * Pure module — no DOM, no React renderer.
 */

import {
  countEdits,
  formatInPoint,
  formatSeconds,
  formatTimecode,
  medianDefaultDuration,
  remainingBeats,
  slotWindows,
  totalDurationS,
  type DraftSlot,
} from "@/app/generative/timeline-math";
import {
  HISTORY_LIMIT,
  initEditorState,
  timelineReducer,
  type EditorAction,
  type EditorState,
} from "@/app/generative/timeline-reducer";
import type { TimelineResponse } from "@/lib/generative-api";

// ── Fixtures ──────────────────────────────────────────────────────────────────

/** NON-uniform grid: intervals 0.5, 0.7, 0.4, 0.9, 0.5, 0.6, 0.4, 1.0 (9 stamps, 8 beats). */
const GRID = [0, 0.5, 1.2, 1.6, 2.5, 3.0, 3.6, 4.0, 5.0];

function gridTimeline(): TimelineResponse {
  return {
    editable: true,
    reason: null,
    beat_grid: GRID,
    total_duration_s: 3.0,
    has_user_edits: false,
    slots: [
      {
        slot_id: "s1",
        clip_index: 0,
        source_gcs_path: "music-uploads/a.mp4",
        source_duration_s: 10,
        in_s: 1.0,
        duration_s: 1.2,
        duration_beats: 2,
        order: 0,
        moment_energy: 0.8,
        moment_description: "Best wave",
      },
      {
        slot_id: "s2",
        clip_index: 1,
        source_gcs_path: "music-uploads/b.mp4",
        source_duration_s: 6,
        in_s: 0.5,
        duration_s: 1.3,
        duration_beats: 2,
        order: 1,
        moment_energy: 0.5,
        moment_description: "Sunset pan",
      },
      {
        slot_id: "s3",
        clip_index: 2,
        source_gcs_path: "music-uploads/c.mp4",
        source_duration_s: 4,
        in_s: 0,
        duration_s: 1.1,
        duration_beats: 2,
        order: 2,
        moment_energy: null,
        moment_description: null,
      },
    ],
    clips: [
      { clip_index: 0, signed_url: "https://x/0", duration_s: 10, used: true },
      { clip_index: 1, signed_url: "https://x/1", duration_s: 6, used: true },
      { clip_index: 2, signed_url: "https://x/2", duration_s: 4, used: true },
      { clip_index: 3, signed_url: "https://x/3", duration_s: 8, used: false },
    ],
  };
}

function secondsTimeline(): TimelineResponse {
  const t = gridTimeline();
  return {
    ...t,
    beat_grid: [],
    slots: t.slots.map((s) => ({ ...s, duration_beats: null, duration_s: 2.0 })),
  };
}

function init(t: TimelineResponse = gridTimeline()): EditorState {
  return initEditorState(t);
}

function run(state: EditorState, ...actions: EditorAction[]): EditorState {
  return actions.reduce(timelineReducer, state);
}

const keys = (s: EditorState) => s.slots.map((x) => x.key);

// ── Beat walk on a NON-uniform grid ───────────────────────────────────────────

describe("slotWindows beat walk (non-uniform grid)", () => {
  it("derives each slot's seconds from its cumulative offset", () => {
    const state = init();
    const windows = slotWindows(state.slots, GRID);
    // slot 1: offset 0, 2 beats → grid[2]-grid[0] = 1.2
    expect(windows[0]).toEqual({ startS: 0, durationS: 1.2, offsetBeats: 0 });
    // slot 2: offset 2, 2 beats → grid[4]-grid[2] = 2.5-1.2 = 1.3
    expect(windows[1].durationS).toBeCloseTo(1.3);
    expect(windows[1].startS).toBeCloseTo(1.2);
    expect(windows[1].offsetBeats).toBe(2);
    // slot 3: offset 4, 2 beats → grid[6]-grid[4] = 3.6-2.5 = 1.1
    expect(windows[2].durationS).toBeCloseTo(1.1);
    expect(windows[2].startS).toBeCloseTo(2.5);
  });

  it("re-walks offsets when a slot is removed (downstream slots change seconds)", () => {
    const state = run(init(), { type: "REMOVE", key: "s1" });
    const windows = slotWindows(state.slots, GRID);
    expect(windows[0].durationS).toBe(0); // removed
    // s2 now starts at offset 0 → grid[2]-grid[0] = 1.2 (was 1.3)
    expect(windows[1].durationS).toBeCloseTo(1.2);
    expect(windows[1].offsetBeats).toBe(0);
    // s3 now offset 2 → 1.3 (was 1.1)
    expect(windows[2].durationS).toBeCloseTo(1.3);
  });

  it("re-walks offsets on reorder", () => {
    const state = run(init(), { type: "REORDER", from: 2, to: 0 });
    const windows = slotWindows(state.slots, GRID);
    expect(keys(state)).toEqual(["s3", "s1", "s2"]);
    // s3 first: offset 0 → 1.2; s1: offset 2 → 1.3; s2: offset 4 → 1.1
    expect(windows[0].durationS).toBeCloseTo(1.2);
    expect(windows[1].durationS).toBeCloseTo(1.3);
    expect(windows[2].durationS).toBeCloseTo(1.1);
  });

  it("seconds mode (empty grid) uses duration_s directly", () => {
    const state = init(secondsTimeline());
    const windows = slotWindows(state.slots, []);
    expect(windows.map((w) => w.durationS)).toEqual([2, 2, 2]);
    expect(windows[2].startS).toBeCloseTo(4);
    expect(totalDurationS(state.slots, [])).toBeCloseTo(6);
  });
});

// ── Reducer actions ───────────────────────────────────────────────────────────

describe("timelineReducer", () => {
  it("REORDER moves a slot and records history", () => {
    const state = run(init(), { type: "REORDER", from: 0, to: 2 });
    expect(keys(state)).toEqual(["s2", "s3", "s1"]);
    expect(state.past).toHaveLength(1);
  });

  it("NUDGE +1 beat consumes remaining grid beats and clamps at grid end", () => {
    let state = init(); // 6 of 8 beats used
    expect(remainingBeats(state.slots, GRID)).toBe(2);
    state = run(
      state,
      { type: "NUDGE", key: "s3", delta: 1 },
      { type: "NUDGE", key: "s3", delta: 1 },
    );
    expect(state.slots[2].durationBeats).toBe(4);
    expect(remainingBeats(state.slots, GRID)).toBe(0);
    // Grid exhausted → clamp (no change, flash nonce bumps)
    const before = state.clampNonce;
    state = run(state, { type: "NUDGE", key: "s3", delta: 1 });
    expect(state.slots[2].durationBeats).toBe(4);
    expect(state.clampNonce).toBe(before + 1);
    expect(state.clampedKey).toBe("s3");
  });

  it("NUDGE -1 beat clamps at the 1-beat floor", () => {
    let state = run(init(), { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationBeats).toBe(1);
    const before = state.clampNonce;
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationBeats).toBe(1);
    expect(state.clampNonce).toBe(before + 1);
  });

  it("NUDGE in seconds mode steps 0.5s with a 0.6s floor and 60s ceiling", () => {
    let state = init(secondsTimeline());
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationS).toBeCloseTo(1.5);
    // Walk down to the floor
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationS).toBeCloseTo(1.0);
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    // 1.0 - 0.5 = 0.5 < 0.6 floor → clamped, unchanged
    expect(state.slots[0].durationS).toBeCloseTo(1.0);
    expect(state.clampedKey).toBe("s1");
  });

  it("SET_IN clamps the in-point to the source clip bounds", () => {
    let state = run(init(), { type: "SET_IN", key: "s2", inS: 99, record: true });
    // source 6s, window 1.3s → max in = 4.7
    expect(state.slots[1].inS).toBeCloseTo(4.7);
    state = run(state, { type: "SET_IN", key: "s2", inS: -5, record: true });
    expect(state.slots[1].inS).toBe(0);
  });

  it("SET_IN with record=false does not push history (scrub ticks)", () => {
    let state = init();
    state = run(state, { type: "SET_IN", key: "s1", inS: 2, record: true });
    state = run(state, { type: "SET_IN", key: "s1", inS: 3, record: false });
    state = run(state, { type: "SET_IN", key: "s1", inS: 4, record: false });
    expect(state.past).toHaveLength(1);
    state = run(state, { type: "UNDO" });
    expect(state.slots[0].inS).toBe(1.0); // back to baseline, not 3
  });

  it("REMOVE then RESTORE round-trips", () => {
    let state = run(init(), { type: "REMOVE", key: "s2" });
    expect(state.slots[1].removed).toBe(true);
    expect(totalDurationS(state.slots, GRID)).toBeCloseTo(1.2 + 1.3); // s1 + s3 re-walked
    state = run(state, { type: "RESTORE", key: "s2" });
    expect(state.slots[1].removed).toBe(false);
    expect(totalDurationS(state.slots, GRID)).toBeCloseTo(3.6);
  });

  it("RESTORE shrinks the slot's beats when the grid can't fit it whole", () => {
    let state = run(
      init(),
      { type: "REMOVE", key: "s1" }, // frees 2 beats (4 used, 4 free)
      { type: "NUDGE", key: "s3", delta: 1 },
      { type: "NUDGE", key: "s3", delta: 1 },
      { type: "NUDGE", key: "s3", delta: 1 }, // s3 now 5 beats; 7 used, 1 free
    );
    state = run(state, { type: "RESTORE", key: "s1" });
    expect(state.slots[0].removed).toBe(false);
    expect(state.slots[0].durationBeats).toBe(1); // shrunk from 2 to fit
  });

  it("ADD appends a slot with the median duration of current slots", () => {
    let state = run(init(), { type: "NUDGE", key: "s1", delta: -1 }); // beats: 1,2,2
    state = run(state, { type: "ADD", clipIndex: 3 });
    const added = state.slots[3];
    expect(added.slotId).toBeNull();
    expect(added.clipIndex).toBe(3);
    expect(added.durationBeats).toBe(2); // median of [1,2,2]
    expect(added.inS).toBe(0);
  });

  it("ADD clamps to the remaining grid beats and rejects when exhausted", () => {
    let state = run(
      init(),
      { type: "NUDGE", key: "s3", delta: 1 }, // 7 of 8 beats used
    );
    state = run(state, { type: "ADD", clipIndex: 3 });
    expect(state.slots[3].durationBeats).toBe(1); // clamped from median 2
    const before = state.clampNonce;
    state = run(state, { type: "ADD", clipIndex: 0 });
    expect(state.slots).toHaveLength(4); // rejected
    expect(state.clampNonce).toBe(before + 1);
  });

  it("UNDO/REDO walk the history and clear redo on a new edit", () => {
    let state = run(
      init(),
      { type: "REORDER", from: 0, to: 1 },
      { type: "REMOVE", key: "s3" },
    );
    state = run(state, { type: "UNDO" });
    expect(state.slots.find((s) => s.key === "s3")!.removed).toBe(false);
    expect(keys(state)).toEqual(["s2", "s1", "s3"]);
    state = run(state, { type: "REDO" });
    expect(state.slots.find((s) => s.key === "s3")!.removed).toBe(true);
    state = run(state, { type: "UNDO" }, { type: "UNDO" });
    expect(keys(state)).toEqual(["s1", "s2", "s3"]); // baseline
    expect(timelineReducer(state, { type: "UNDO" })).toBe(state); // no-op at floor
    // New edit clears redo
    state = run(state, { type: "REMOVE", key: "s1" });
    expect(state.future).toHaveLength(0);
  });

  it("caps history at HISTORY_LIMIT entries", () => {
    let state = init();
    for (let i = 0; i < HISTORY_LIMIT + 10; i++) {
      state = run(state, { type: "REORDER", from: 0, to: 1 });
    }
    expect(state.past.length).toBe(HISTORY_LIMIT);
  });

  it("RESET_DRAFT returns to the baseline and clears history", () => {
    let state = run(
      init(),
      { type: "REORDER", from: 0, to: 2 },
      { type: "REMOVE", key: "s2" },
      { type: "RESET_DRAFT" },
    );
    expect(keys(state)).toEqual(["s1", "s2", "s3"]);
    expect(state.slots.every((s) => !s.removed)).toBe(true);
    expect(state.past).toHaveLength(0);
    expect(state.future).toHaveLength(0);
  });
});

// ── Edit count vs server baseline ─────────────────────────────────────────────

describe("countEdits", () => {
  const base = () => init().baseline;

  it("is 0 for an untouched draft", () => {
    const state = init();
    expect(countEdits(state.baseline, state.slots)).toBe(0);
  });

  it("counts a single move as 1 edit (LCS, not positional diff)", () => {
    const state = run(init(), { type: "REORDER", from: 0, to: 2 });
    expect(countEdits(state.baseline, state.slots)).toBe(1);
  });

  it("counts trims, removes, swaps and adds per slot", () => {
    const state = run(
      init(),
      { type: "SET_IN", key: "s1", inS: 3, record: true }, // 1 (trim)
      { type: "REMOVE", key: "s2" }, // 1 (remove)
      { type: "SWAP", key: "s3", clipIndex: 3 }, // 1 (swap; also resets in_s)
      { type: "ADD", clipIndex: 0 }, // 1 (add)
    );
    expect(countEdits(state.baseline, state.slots)).toBe(4);
  });

  it("returns to 0 after undoing everything", () => {
    let state = run(init(), { type: "REORDER", from: 0, to: 1 });
    expect(countEdits(state.baseline, state.slots)).toBe(1);
    state = run(state, { type: "UNDO" });
    expect(countEdits(state.baseline, state.slots)).toBe(0);
  });

  it("does not double-count a NUDGE'd slot nudged twice", () => {
    const state = run(
      init(),
      { type: "NUDGE", key: "s3", delta: 1 },
      { type: "NUDGE", key: "s3", delta: 1 },
    );
    expect(countEdits(state.baseline, state.slots)).toBe(1);
  });

  it("baseline helper sanity: keys are stable slot ids", () => {
    expect(base().map((s) => s.key)).toEqual(["s1", "s2", "s3"]);
  });
});

// ── Median + formatting ───────────────────────────────────────────────────────

describe("helpers", () => {
  it("medianDefaultDuration picks the median beats (grid) / seconds (no grid)", () => {
    const state = init();
    expect(medianDefaultDuration(state.slots, GRID)).toEqual({
      durationBeats: 2,
      durationS: null,
    });
    const sec = init(secondsTimeline());
    expect(medianDefaultDuration(sec.slots, [])).toEqual({
      durationBeats: null,
      durationS: 2.0,
    });
  });

  it("formats timecodes and seconds", () => {
    expect(formatTimecode(0)).toBe("0:00");
    expect(formatTimecode(67.8)).toBe("1:07");
    expect(formatSeconds(1.25)).toBe("1.3s");
    expect(formatInPoint(64.25)).toBe("1:04.2");
    expect(formatInPoint(0)).toBe("0:00.0");
  });
});
