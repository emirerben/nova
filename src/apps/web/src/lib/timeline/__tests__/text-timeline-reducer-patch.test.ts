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

describe("textReducer PATCH_BAR — behind_subject", () => {
  it("previews a motion gesture live and records only its baseline on commit", () => {
    const before = bar({ motion: { version: 2, speed: 1 } });
    const state = initTextEditorState([before]);
    const previewOne = textReducer(state, {
      type: "PREVIEW_BAR",
      id: "a",
      patch: { motion: { version: 2, speed: 2 } },
    });
    const previewTwo = textReducer(previewOne, {
      type: "PREVIEW_BAR",
      id: "a",
      patch: { motion: { version: 2, speed: 3 } },
    });
    expect(previewTwo.bars[0].motion?.speed).toBe(3);
    expect(previewTwo.past).toHaveLength(0);

    const committed = textReducer(previewTwo, {
      type: "COMMIT_PREVIEW_BAR",
      id: "a",
      before,
      patch: { motion: { version: 2, speed: 3 } },
    });
    expect(committed.past).toHaveLength(1);
    expect(textReducer(committed, { type: "UNDO" }).bars[0].motion?.speed).toBe(1);
  });

  it("sets behind_subject on the targeted bar, leaving others untouched", () => {
    const state = initTextEditorState([bar({ id: "a" }), bar({ id: "b" })]);
    const next = textReducer(state, {
      type: "PATCH_BAR",
      id: "a",
      patch: { behind_subject: true },
    });

    const [a, b] = next.bars;
    expect(a.behind_subject).toBe(true);
    expect(b.behind_subject).toBeUndefined();
  });

  it("toggles behind_subject back off", () => {
    const state = initTextEditorState([bar({ behind_subject: true })]);
    const next = textReducer(state, {
      type: "PATCH_BAR",
      id: "a",
      patch: { behind_subject: false },
    });

    expect(next.bars[0].behind_subject).toBe(false);
  });

  it("pushes history so undo restores the pre-toggle value", () => {
    const state = initTextEditorState([bar()]);
    const next = textReducer(state, {
      type: "PATCH_BAR",
      id: "a",
      patch: { behind_subject: true },
    });
    const undone = textReducer(next, { type: "UNDO" });

    expect(undone.bars[0].behind_subject).toBeUndefined();
  });
});

describe("textReducer SPLIT_BAR — behind_subject carries to both halves", () => {
  it("shares behind_subject across the split", () => {
    const state = initTextEditorState([bar({ behind_subject: true })]);
    const next = textReducer(state, {
      type: "SPLIT_BAR",
      id: "a",
      at_s: 3,
      newId: "b",
    });

    const [left, right] = next.bars;
    expect(left.behind_subject).toBe(true);
    expect(right.behind_subject).toBe(true);
  });
});
