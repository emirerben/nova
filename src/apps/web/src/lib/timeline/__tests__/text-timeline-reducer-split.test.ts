import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "../text-timeline-reducer";

function bar(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "a",
    text: "hello world",
    start_s: 1,
    end_s: 5,
    role: "generative_intro",
    color: "#ffffff",
    font_family: "Inter",
    size_px: 64,
    ...over,
  };
}

describe("textReducer SPLIT_BAR", () => {
  it("splits one bar into two at the playhead, sharing style", () => {
    const state = initTextEditorState([bar()]);
    const next = textReducer(state, {
      type: "SPLIT_BAR",
      id: "a",
      at_s: 3,
      newId: "b",
    });

    expect(next.bars).toHaveLength(2);
    const [left, right] = next.bars;

    // Timings cut at the playhead.
    expect(left.start_s).toBe(1);
    expect(left.end_s).toBe(3);
    expect(right.start_s).toBe(3);
    expect(right.end_s).toBe(5);

    // Distinct identities.
    expect(left.id).toBe("a");
    expect(right.id).toBe("b");
    expect(left.id).not.toBe(right.id);

    // Shared style fields.
    expect(right.text).toBe(left.text);
    expect(right.color).toBe("#ffffff");
    expect(right.font_family).toBe("Inter");
    expect(right.size_px).toBe(64);
    expect(right.role).toBe("generative_intro");
  });

  it("keeps the two halves adjacent in array order (z-order preserved)", () => {
    const state = initTextEditorState([
      bar({ id: "x" }),
      bar({ id: "a" }),
      bar({ id: "z" }),
    ]);
    const next = textReducer(state, {
      type: "SPLIT_BAR",
      id: "a",
      at_s: 3,
      newId: "b",
    });
    expect(next.bars.map((b) => b.id)).toEqual(["x", "a", "b", "z"]);
  });

  it("pushes exactly one history entry (one undo step)", () => {
    const state = initTextEditorState([bar()]);
    const next = textReducer(state, {
      type: "SPLIT_BAR",
      id: "a",
      at_s: 3,
      newId: "b",
    });
    expect(next.past).toHaveLength(1);
    const undone = textReducer(next, { type: "UNDO" });
    expect(undone.bars).toHaveLength(1);
    expect(undone.bars[0].end_s).toBe(5);
  });

  it("is a no-op when the playhead is outside the bar", () => {
    const state = initTextEditorState([bar()]);
    expect(
      textReducer(state, { type: "SPLIT_BAR", id: "a", at_s: 0.5, newId: "b" }).bars,
    ).toHaveLength(1);
    expect(
      textReducer(state, { type: "SPLIT_BAR", id: "a", at_s: 9, newId: "b" }).bars,
    ).toHaveLength(1);
  });

  it("is a no-op when a half would fall below the minimum duration", () => {
    const state = initTextEditorState([bar()]);
    // at_s = 1.1 leaves the left half only 0.1s (< 0.2 MIN).
    const next = textReducer(state, {
      type: "SPLIT_BAR",
      id: "a",
      at_s: 1.1,
      newId: "b",
    });
    expect(next.bars).toHaveLength(1);
    expect(next.past).toHaveLength(0);
  });

  it("is a no-op for an unknown bar id", () => {
    const state = initTextEditorState([bar()]);
    const next = textReducer(state, {
      type: "SPLIT_BAR",
      id: "nope",
      at_s: 3,
      newId: "b",
    });
    expect(next).toBe(state);
  });
});
