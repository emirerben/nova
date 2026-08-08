import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "../text-timeline-reducer";

function scheduledBar(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "scheduled",
    text: "Kria",
    start_s: 1,
    end_s: 3,
    reveal_s: 1.25,
    role: "generative_intro",
    effect: "typewriter",
    source_params: { reveal_schedule_s: [1, 1.083, 1.167, 1.25] },
    ...over,
  };
}

function revealSchedule(bar: TextElementBar): unknown {
  return bar.source_params?.reveal_schedule_s;
}

describe("textReducer generated reveal timing", () => {
  it("moves the absolute endpoint and schedule with the bar, including undo/redo", () => {
    const initial = initTextEditorState([scheduledBar()]);
    const moved = textReducer(initial, { type: "MOVE_BAR", id: "scheduled", start_s: 2 });

    expect(moved.bars[0]).toMatchObject({ start_s: 2, end_s: 4, reveal_s: 2.25 });
    expect(revealSchedule(moved.bars[0])).toEqual([2, 2.083, 2.167, 2.25]);

    const undone = textReducer(moved, { type: "UNDO" });
    expect(undone.bars[0]).toMatchObject({ start_s: 1, end_s: 3, reveal_s: 1.25 });
    expect(revealSchedule(undone.bars[0])).toEqual([1, 1.083, 1.167, 1.25]);

    const redone = textReducer(undone, { type: "REDO" });
    expect(redone.bars[0]).toEqual(moved.bars[0]);
  });

  it("keeps reveal timing local when trimming or patching the start", () => {
    const initial = initTextEditorState([scheduledBar()]);
    const trimmed = textReducer(initial, {
      type: "TRIM_START",
      id: "scheduled",
      start_s: 1.5,
    });
    expect(trimmed.bars[0].reveal_s).toBe(1.75);
    expect(revealSchedule(trimmed.bars[0])).toEqual([1.5, 1.583, 1.667, 1.75]);

    const patched = textReducer(initial, {
      type: "PATCH_BAR",
      id: "scheduled",
      patch: { start_s: 2.2 },
    });
    expect(patched.bars[0].reveal_s).toBe(2.45);
    expect(revealSchedule(patched.bars[0])).toEqual([2.2, 2.283, 2.367, 2.45]);
  });

  it("normalizes a split bar's right-half schedule to its new start", () => {
    const split = textReducer(initTextEditorState([scheduledBar({ end_s: 5 })]), {
      type: "SPLIT_BAR",
      id: "scheduled",
      at_s: 3,
      newId: "right",
    });

    expect(split.bars[0].start_s).toBe(1);
    expect(split.bars[1].start_s).toBe(3);
    expect(split.bars[1].reveal_s).toBe(3.25);
    expect(revealSchedule(split.bars[1])).toEqual([3, 3.083, 3.167, 3.25]);
  });

  it("leaves generic typewriter elements without schedules unchanged", () => {
    const initial = initTextEditorState([
      scheduledBar({ reveal_s: undefined, source_params: undefined }),
    ]);
    const moved = textReducer(initial, { type: "MOVE_BAR", id: "scheduled", start_s: 2 });

    expect(moved.bars[0].reveal_s).toBeUndefined();
    expect(moved.bars[0].source_params).toBeUndefined();
  });
});
