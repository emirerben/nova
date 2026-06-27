import {
  initSfxEditorState,
  sfxReducer,
  SFX_HISTORY_LIMIT,
  type SfxEditorState,
} from "../sfx-timeline-reducer";
import type { SoundEffectPlacement } from "@/lib/plan-api";

function makePlacement(override: Partial<SoundEffectPlacement> = {}): SoundEffectPlacement {
  return {
    id: crypto.randomUUID(),
    src_gcs_path: "sound-effects/test/fah.mp3",
    at_s: 0,
    gain: 1.0,
    ...override,
  };
}

describe("sfxReducer — ADD", () => {
  it("appends a new placement", () => {
    const p = makePlacement({ id: "a", at_s: 3 });
    const state = initSfxEditorState([]);
    const next = sfxReducer(state, { type: "ADD", placement: p });
    expect(next.placements).toHaveLength(1);
    expect(next.placements[0].id).toBe("a");
  });

  it("pushes current state into past", () => {
    const existing = makePlacement({ id: "existing" });
    const state = initSfxEditorState([existing]);
    const next = sfxReducer(state, { type: "ADD", placement: makePlacement({ id: "new" }) });
    expect(next.past).toHaveLength(1);
    expect(next.past[0]).toContainEqual(existing);
    expect(next.future).toHaveLength(0);
  });
});

describe("sfxReducer — MOVE", () => {
  it("updates at_s for the matching id", () => {
    const p = makePlacement({ id: "p1", at_s: 0 });
    const state = initSfxEditorState([p]);
    const next = sfxReducer(state, { type: "MOVE", id: "p1", atS: 5.5 });
    expect(next.placements[0].at_s).toBe(5.5);
  });

  it("clamps at_s to 0", () => {
    const p = makePlacement({ id: "p1", at_s: 2 });
    const state = initSfxEditorState([p]);
    const next = sfxReducer(state, { type: "MOVE", id: "p1", atS: -1 });
    expect(next.placements[0].at_s).toBe(0);
  });

  it("does not affect other placements", () => {
    const p1 = makePlacement({ id: "p1", at_s: 0 });
    const p2 = makePlacement({ id: "p2", at_s: 10 });
    const state = initSfxEditorState([p1, p2]);
    const next = sfxReducer(state, { type: "MOVE", id: "p1", atS: 5 });
    expect(next.placements[1].at_s).toBe(10);
  });
});

describe("sfxReducer — SET_GAIN", () => {
  it("clamps gain to [0, 2]", () => {
    const p = makePlacement({ id: "p1", gain: 1 });
    const state = initSfxEditorState([p]);
    expect(sfxReducer(state, { type: "SET_GAIN", id: "p1", gain: 3 }).placements[0].gain).toBe(2);
    expect(sfxReducer(state, { type: "SET_GAIN", id: "p1", gain: -0.5 }).placements[0].gain).toBe(0);
  });
});

describe("sfxReducer — TRIM", () => {
  it("sets trim_start_s and trim_end_s", () => {
    const p = makePlacement({ id: "p1", duration_s: 5 });
    const state = initSfxEditorState([p]);
    const next = sfxReducer(state, { type: "TRIM", id: "p1", trimStartS: 0.5, trimEndS: 3.5 });
    expect(next.placements[0].trim_start_s).toBe(0.5);
    expect(next.placements[0].trim_end_s).toBe(3.5);
  });

  it("accepts null to clear trim", () => {
    const p = makePlacement({ id: "p1", trim_start_s: 0.5, trim_end_s: 3.0 });
    const state = initSfxEditorState([p]);
    const next = sfxReducer(state, { type: "TRIM", id: "p1", trimStartS: null, trimEndS: null });
    expect(next.placements[0].trim_start_s).toBeNull();
    expect(next.placements[0].trim_end_s).toBeNull();
  });
});

describe("sfxReducer — SET_LABEL", () => {
  it("updates the label", () => {
    const p = makePlacement({ id: "p1", label: "old" });
    const state = initSfxEditorState([p]);
    const next = sfxReducer(state, { type: "SET_LABEL", id: "p1", label: "new" });
    expect(next.placements[0].label).toBe("new");
  });
});

describe("sfxReducer — REMOVE", () => {
  it("removes the matching placement", () => {
    const p1 = makePlacement({ id: "p1" });
    const p2 = makePlacement({ id: "p2" });
    const state = initSfxEditorState([p1, p2]);
    const next = sfxReducer(state, { type: "REMOVE", id: "p1" });
    expect(next.placements).toHaveLength(1);
    expect(next.placements[0].id).toBe("p2");
  });
});

describe("sfxReducer — UNDO / REDO", () => {
  function stateAfterMutations(n: number): SfxEditorState {
    let state = initSfxEditorState([]);
    for (let i = 0; i < n; i++) {
      state = sfxReducer(state, { type: "ADD", placement: makePlacement({ id: `p${i}`, at_s: i }) });
    }
    return state;
  }

  it("UNDO restores prior state", () => {
    const state = stateAfterMutations(2);
    const undone = sfxReducer(state, { type: "UNDO" });
    expect(undone.placements).toHaveLength(1);
  });

  it("UNDO twice restores two steps back", () => {
    const state = stateAfterMutations(3);
    const once = sfxReducer(state, { type: "UNDO" });
    const twice = sfxReducer(once, { type: "UNDO" });
    expect(twice.placements).toHaveLength(1);
  });

  it("UNDO on empty past is a no-op", () => {
    const state = initSfxEditorState([makePlacement()]);
    const unchanged = sfxReducer(state, { type: "UNDO" });
    expect(unchanged).toBe(state);
  });

  it("REDO after UNDO re-applies the mutation", () => {
    const state = stateAfterMutations(2);
    const undone = sfxReducer(state, { type: "UNDO" });
    const redone = sfxReducer(undone, { type: "REDO" });
    expect(redone.placements).toHaveLength(2);
  });

  it("new mutation clears redo stack", () => {
    const state = stateAfterMutations(2);
    const undone = sfxReducer(state, { type: "UNDO" });
    expect(undone.future).toHaveLength(1);
    const withNew = sfxReducer(undone, { type: "ADD", placement: makePlacement() });
    expect(withNew.future).toHaveLength(0);
  });
});

describe("sfxReducer — history limit", () => {
  it(`caps past at ${SFX_HISTORY_LIMIT}`, () => {
    let state = initSfxEditorState([]);
    for (let i = 0; i < SFX_HISTORY_LIMIT + 10; i++) {
      state = sfxReducer(state, { type: "ADD", placement: makePlacement({ id: `p${i}` }) });
    }
    expect(state.past.length).toBeLessThanOrEqual(SFX_HISTORY_LIMIT);
  });
});

describe("sfxReducer — RESET", () => {
  it("replaces all state and clears history", () => {
    const state = stateAfterMutations(3);
    function stateAfterMutations(n: number): SfxEditorState {
      let s = initSfxEditorState([]);
      for (let i = 0; i < n; i++) {
        s = sfxReducer(s, { type: "ADD", placement: makePlacement({ id: `p${i}` }) });
      }
      return s;
    }
    const fresh = [makePlacement({ id: "fresh" })];
    const reset = sfxReducer(state, { type: "RESET", placements: fresh });
    expect(reset.placements).toBe(fresh);
    expect(reset.past).toHaveLength(0);
    expect(reset.future).toHaveLength(0);
  });
});
