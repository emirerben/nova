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

describe("textReducer ADD_LYRIC_BARS / REMOVE_LYRIC_BARS (lyrics-optional elements model)", () => {
  const nonLyric: TextElementBar = {
    id: "title-1",
    text: "Title",
    start_s: 0,
    end_s: 2,
    role: "generative_intro",
  };

  it("ADD_LYRIC_BARS inserts every seed bar in one undoable step", () => {
    const state = initTextEditorState([nonLyric]);
    const seeds = [lyric({ id: "lyr-L0" }), lyric({ id: "lyr-L1", text: "second line" })];
    const next = textReducer(state, { type: "ADD_LYRIC_BARS", bars: seeds });

    expect(next.bars.map((b) => b.id)).toEqual(["title-1", "lyr-L0", "lyr-L1"]);
    // One history push for both inserted bars — a single undo removes both.
    expect(next.past).toEqual([[nonLyric]]);

    const undone = textReducer(next, { type: "UNDO" });
    expect(undone.bars).toEqual([nonLyric]);
    const redone = textReducer(undone, { type: "REDO" });
    expect(redone.bars.map((b) => b.id)).toEqual(["title-1", "lyr-L0", "lyr-L1"]);
  });

  it("ADD_LYRIC_BARS with an empty list no-ops (no spurious history push)", () => {
    const state = initTextEditorState([nonLyric]);
    const next = textReducer(state, { type: "ADD_LYRIC_BARS", bars: [] });
    expect(next).toBe(state);
  });

  it("REMOVE_LYRIC_BARS strips every lyric_line bar, keeps everything else, in one undoable step", () => {
    const seeds = [lyric({ id: "lyr-L0" }), lyric({ id: "lyr-L1", text: "second line" })];
    const state = initTextEditorState([nonLyric, ...seeds]);
    const next = textReducer(state, { type: "REMOVE_LYRIC_BARS" });

    expect(next.bars).toEqual([nonLyric]);
    expect(next.past).toEqual([[nonLyric, ...seeds]]);

    const undone = textReducer(next, { type: "UNDO" });
    expect(undone.bars.map((b) => b.id)).toEqual(["title-1", "lyr-L0", "lyr-L1"]);
  });

  it("REMOVE_LYRIC_BARS no-ops when there are no lyric bars (no spurious history push)", () => {
    const state = initTextEditorState([nonLyric]);
    const next = textReducer(state, { type: "REMOVE_LYRIC_BARS" });
    expect(next).toBe(state);
  });

  it("DELETE_BAR still can't remove a single lyric bar even after ADD_LYRIC_BARS — REMOVE_LYRIC_BARS is the only removal path", () => {
    const state = initTextEditorState([nonLyric]);
    const withLyrics = textReducer(state, {
      type: "ADD_LYRIC_BARS",
      bars: [lyric({ id: "lyr-L0" })],
    });
    const attempted = textReducer(withLyrics, { type: "DELETE_BAR", id: "lyr-L0" });
    expect(attempted).toBe(withLyrics);
  });
});
