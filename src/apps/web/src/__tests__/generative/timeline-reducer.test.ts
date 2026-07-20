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
  totalBeats,
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

// ── Null-beats slots on a grid timeline (footage-trimmed AI slots) ────────────

describe("null-beats slots on a grid timeline", () => {
  /** s2 becomes a footage-trimmed slot: exact seconds window, no beat count —
   * the worker's `duration_beats: null` shape on song variants. */
  function mixedTimeline(): TimelineResponse {
    const t = gridTimeline();
    t.slots[1] = { ...t.slots[1], duration_beats: null, duration_s: 1.137 };
    return t;
  }

  it("walks at duration_s without consuming grid beats", () => {
    const state = init(mixedTimeline());
    const windows = slotWindows(state.slots, GRID);
    // s1: offset 0, 2 beats → 1.2
    expect(windows[0].durationS).toBeCloseTo(1.2);
    // s2: exact window straight from duration_s; off the grid
    expect(windows[1].durationS).toBeCloseTo(1.137);
    expect(windows[1].offsetBeats).toBeNull();
    expect(windows[1].startS).toBeCloseTo(1.2);
    // s3 keeps walking from offset 2 — s2 consumed NO beats: grid[4]-grid[2] = 1.3
    expect(windows[2].durationS).toBeCloseTo(1.3);
    expect(windows[2].offsetBeats).toBe(2);
    expect(totalBeats(state.slots)).toBe(4);
    expect(totalDurationS(state.slots, GRID)).toBeCloseTo(1.2 + 1.137 + 1.3);
  });

  it("NUDGE steps duration_s ±0.5 from the AI base (no 0.5-multiple requirement)", () => {
    let state = init(mixedTimeline());
    state = run(state, { type: "NUDGE", key: "s2", delta: 1 });
    expect(state.slots[1].durationS).toBeCloseTo(1.637);
    expect(state.slots[1].durationBeats).toBeNull();
    state = run(state, { type: "NUDGE", key: "s2", delta: -1 }, { type: "NUDGE", key: "s2", delta: -1 });
    expect(state.slots[1].durationS).toBeCloseTo(0.637);
    // Sub-0.6 remains valid as long as it stays positive.
    const before = state.clampNonce;
    state = run(state, { type: "NUDGE", key: "s2", delta: -1 });
    expect(state.slots[1].durationS).toBeCloseTo(0.137);
    expect(state.clampNonce).toBe(before);
    state = run(state, { type: "NUDGE", key: "s2", delta: -1 });
    expect(state.slots[1].durationS).toBeCloseTo(0.137);
    expect(state.clampNonce).toBe(before + 1);
    expect(state.clampedKey).toBe("s2");
  });

  it("NUDGE + clamps at the source clip duration", () => {
    let state = init(mixedTimeline());
    // s2's source clip is 6s; growing from 1.137 stops at 5.637.
    for (let i = 0; i < 12; i++) {
      state = run(state, { type: "NUDGE", key: "s2", delta: 1 });
    }
    expect(state.slots[1].durationS).toBeCloseTo(5.637);
    expect(state.clampedKey).toBe("s2");
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

  it("NUDGE in seconds mode steps 0.5s while duration stays positive", () => {
    let state = init(secondsTimeline());
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationS).toBeCloseTo(1.5);
    // Walk down to the floor
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationS).toBeCloseTo(1.0);
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationS).toBeCloseTo(0.5);
    state = run(state, { type: "NUDGE", key: "s1", delta: -1 });
    expect(state.slots[0].durationS).toBeCloseTo(0.5);
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

// ── SET_DURATION_BEATS (grid right-edge drag) ─────────────────────────────────

describe("SET_DURATION_BEATS", () => {
  it("sets an absolute beat count and re-clamps in-points", () => {
    const state = run(
      init(),
      { type: "SET_DURATION_BEATS", key: "s1", beats: 3, record: true },
    );
    expect(state.slots[0].durationBeats).toBe(3);
    expect(state.past).toHaveLength(1);
  });

  it("clamps to 1-beat floor and flashes", () => {
    const before = init().clampNonce;
    const state = run(init(), { type: "SET_DURATION_BEATS", key: "s1", beats: 0, record: true });
    expect(state.slots[0].durationBeats).toBe(1); // stepped down to 1
    expect(state.clampNonce).toBeGreaterThan(before);
    expect(state.clampedKey).toBe("s1");
  });

  it("clamps to available grid beats (step-down, not hard-reject)", () => {
    // Fixture has 6 beats used of 8. s1 = 2 beats. maxAvail for s1 = 8 - (6 - 2) = 4.
    const state = run(init(), { type: "SET_DURATION_BEATS", key: "s1", beats: 99, record: true });
    expect(state.slots[0].durationBeats).toBeLessThanOrEqual(4);
    expect(state.clampNonce).toBeGreaterThan(0);
  });

  it("steps down on source-clip overflow and flashes", () => {
    // s3: clip index 2, source 4s. Grid from offset 4:
    //   k=1 → grid[5]-grid[4] = 3.0-2.5 = 0.5s
    //   k=2 → grid[6]-grid[4] = 3.6-2.5 = 1.1s
    //   k=3 → grid[7]-grid[4] = 4.0-2.5 = 1.5s
    //   k=4 → grid[8]-grid[4] = 5.0-2.5 = 2.5s → still fits 4s source (inS=0)
    // Only when window > source (4s) would it step down. Window at k=4 = 2.5s which fits.
    // Let's try with a very short source: create a custom fixture
    const t = gridTimeline();
    t.slots[2] = { ...t.slots[2], source_duration_s: 0.6 }; // 0.6s source
    t.clips[2] = { ...t.clips[2], duration_s: 0.6 };
    const state = init(t);
    // k=2 would give 1.1s window > 0.6s source → step down to k=1 (0.5s ≤ 0.6s)
    const after = run(state, { type: "SET_DURATION_BEATS", key: "s3", beats: 2, record: true });
    expect(after.slots[2].durationBeats).toBe(1);
    expect(after.clampNonce).toBeGreaterThan(0);
  });

  it("record=false does not push history", () => {
    const state = init();
    const after1 = timelineReducer(state, { type: "SET_DURATION_BEATS", key: "s1", beats: 1, record: true });
    const after2 = timelineReducer(after1, { type: "SET_DURATION_BEATS", key: "s1", beats: 2, record: false });
    const after3 = timelineReducer(after2, { type: "SET_DURATION_BEATS", key: "s1", beats: 3, record: false });
    expect(after3.past).toHaveLength(1); // only the record=true pushed
    const undone = timelineReducer(after3, { type: "UNDO" });
    expect(undone.slots[0].durationBeats).toBe(2); // back to baseline (2 beats)
  });

  it("is a no-op on a seconds (null-beats) slot", () => {
    const state = init(secondsTimeline());
    const after = run(state, { type: "SET_DURATION_BEATS", key: "s1", beats: 3, record: true });
    expect(after).toBe(state); // reference equality → no-op
  });

  it("countEdits registers a beat change as 1 edit", () => {
    const state = run(init(), { type: "SET_DURATION_BEATS", key: "s1", beats: 3, record: true });
    expect(countEdits(state.baseline, state.slots)).toBe(1);
  });
});

// ── SET_WINDOW (seconds both-edge drag) ───────────────────────────────────────

describe("SET_WINDOW", () => {
  it("sets inS and durationS together", () => {
    const state = run(
      init(secondsTimeline()),
      { type: "SET_WINDOW", key: "s1", inS: 0.5, durationS: 1.5, record: true },
    );
    expect(state.slots[0].inS).toBeCloseTo(0.5);
    expect(state.slots[0].durationS).toBeCloseTo(1.5);
    expect(state.past).toHaveLength(1);
  });

  it("clamps inS to 0 and flashes", () => {
    const state = run(
      init(secondsTimeline()),
      { type: "SET_WINDOW", key: "s1", inS: -1, durationS: 2.0, record: true },
    );
    expect(state.slots[0].inS).toBe(0);
    expect(state.clampNonce).toBeGreaterThan(0);
    expect(state.clampedKey).toBe("s1");
  });

  it("accepts sub-0.6s positive durationS", () => {
    const state = run(
      init(secondsTimeline()),
      { type: "SET_WINDOW", key: "s1", inS: 0, durationS: 0.3, record: true },
    );
    expect(state.slots[0].durationS).toBeCloseTo(0.3);
    expect(state.clampNonce).toBe(0);
  });

  it("enforces source-fit and flashes", () => {
    // s1: clip index 0, source 10s. Window 8s starting at inS=5 would run to 13s > 10s.
    const state = run(
      init(secondsTimeline()),
      { type: "SET_WINDOW", key: "s1", inS: 5, durationS: 8, record: true },
    );
    expect(state.slots[0].inS + (state.slots[0].durationS ?? 0)).toBeLessThanOrEqual(10 + 1e-6);
    expect(state.clampNonce).toBeGreaterThan(0);
  });

  it("enforces 60s total and flashes", () => {
    // seconds timeline: 3 slots × 2s = 6s total.
    // Set s1 to a 56s window → total would be 56+2+2=60, ok. 57s → 61s > 60.
    const state = run(
      init(secondsTimeline()),
      { type: "SET_WINDOW", key: "s1", inS: 0, durationS: 57, record: true },
    );
    const total = (state.slots[0].durationS ?? 0) + (state.slots[1].durationS ?? 0) + (state.slots[2].durationS ?? 0);
    expect(total).toBeLessThanOrEqual(60 + 1e-6);
    expect(state.clampNonce).toBeGreaterThan(0);
  });

  it("left-edge drag keeps out-point fixed (out = inS + dur must stay constant)", () => {
    // Simulate left-edge drag: capturedOut is fixed at pointer-down, inS moves left,
    // durationS = capturedOut - inS.
    const base = init(secondsTimeline());
    const capturedOut = base.slots[0].inS + (base.slots[0].durationS ?? 0); // = 1.0 + 2.0 = 3.0
    // Drag left: new inS=0.5 → dur = 3.0 - 0.5 = 2.5
    const state = run(
      base,
      { type: "SET_WINDOW", key: "s1", inS: 0.5, durationS: capturedOut - 0.5, record: true },
    );
    expect(state.slots[0].inS).toBeCloseTo(0.5);
    expect(state.slots[0].inS + (state.slots[0].durationS ?? 0)).toBeCloseTo(capturedOut);
  });

  it("record=false does not push history (one-gesture-one-undo)", () => {
    const state = init(secondsTimeline());
    const after1 = timelineReducer(state, { type: "SET_WINDOW", key: "s1", inS: 0, durationS: 1.0, record: true });
    const after2 = timelineReducer(after1, { type: "SET_WINDOW", key: "s1", inS: 0, durationS: 1.5, record: false });
    const after3 = timelineReducer(after2, { type: "SET_WINDOW", key: "s1", inS: 0, durationS: 2.0, record: false });
    expect(after3.past).toHaveLength(1);
    const undone = timelineReducer(after3, { type: "UNDO" });
    expect(undone.slots[0].durationS).toBeCloseTo(2.0); // back to baseline
  });

  it("is a no-op on a grid (beats-bearing) slot", () => {
    const state = init(); // grid timeline — all slots have durationBeats
    const after = run(state, { type: "SET_WINDOW", key: "s1", inS: 0, durationS: 2.0, record: true });
    expect(after).toBe(state); // reference equality → no-op
  });

  it("countEdits registers both-field change as exactly 1 edit", () => {
    const state = run(
      init(secondsTimeline()),
      { type: "SET_WINDOW", key: "s1", inS: 0.5, durationS: 1.5, record: true },
    );
    expect(countEdits(state.baseline, state.slots)).toBe(1);
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
