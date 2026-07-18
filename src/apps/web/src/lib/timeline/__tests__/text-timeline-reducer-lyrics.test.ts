import { describe, expect, it } from "@jest/globals";
import {
  initTextEditorState,
  textReducer,
  type TextEditorAction,
  type TextElementBar,
} from "../text-timeline-reducer";

function lyric(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "lyric_L0",
    text: "hello lyric",
    start_s: 1,
    end_s: 3,
    role: "lyric_line",
    color: "#FFFFFF",
    highlight_color: "#A3E635",
    font_family: "Inter",
    size_px: 64,
    position: "custom",
    x_frac: 0.5,
    y_frac: 0.7,
    ...over,
  };
}

describe("textReducer lyric_line lock", () => {
  it.each<TextEditorAction>([
    { type: "MOVE_BAR", id: "lyric_L0", start_s: 2 },
    { type: "TRIM_START", id: "lyric_L0", start_s: 1.5 },
    { type: "TRIM_END", id: "lyric_L0", end_s: 4 },
    { type: "SPLIT_BAR", id: "lyric_L0", at_s: 2, newId: "split" },
    { type: "DELETE_BAR", id: "lyric_L0" },
    { type: "REORDER", id: "lyric_L0", direction: "up" },
  ])("no-ops timing or geometry action %#", (action) => {
    const state = initTextEditorState([lyric()]);
    expect(textReducer(state, action)).toBe(state);
  });

  it("allows text edits and undo restores them", () => {
    const state = initTextEditorState([lyric()]);
    const edited = textReducer(state, {
      type: "EDIT_TEXT",
      id: "lyric_L0",
      text: "new lyric",
    });
    expect(edited.bars[0].text).toBe("new lyric");
    expect(textReducer(edited, { type: "UNDO" }).bars[0].text).toBe("hello lyric");
  });

  it("strips disallowed style and geometry patches", () => {
    const state = initTextEditorState([lyric()]);
    const patched = textReducer(state, {
      type: "PATCH_BAR",
      id: "lyric_L0",
      patch: {
        color: "#000000",
        highlight_color: "#FF0000",
        font_family: "Playfair Display",
        size_px: 80,
        x_frac: 0.1,
        y_frac: 0.2,
        rotation_deg: 8,
        effect: "slide-up",
      },
    });
    expect(patched.bars[0]).toMatchObject({
      color: "#000000",
      highlight_color: "#FF0000",
      font_family: "Playfair Display",
      size_px: 80,
      x_frac: 0.5,
      y_frac: 0.7,
    });
    expect(patched.bars[0].rotation_deg).toBeUndefined();
    expect(patched.bars[0].effect).toBeUndefined();
  });
});
