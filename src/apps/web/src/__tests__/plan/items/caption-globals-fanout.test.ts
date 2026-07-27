/**
 * The two pure pieces behind the Captions drawer's "All captions" section:
 *
 *  1. `captionBarPatchFromMetaPatch` — variant meta → the local bar fields that
 *     PREVIEW it. Without this mapping the meta patch only changes what Save
 *     sends, so a global font change previews on nothing.
 *  2. `PATCH_BARS` — applies that mapping to every caption bar in ONE undo step.
 *     A PATCH_BAR loop would make undoing a global change take N presses, and
 *     undoing a 12-line replace-all take 12.
 *
 * Pure modules — no DOM, no React renderer.
 */

import {
  captionBarPatchFromMetaPatch,
  captionMetaPatchFromCaptionBarPatch,
} from "@/app/plan/items/[id]/_editor/editor-bars";
import {
  initTextEditorState,
  textReducer,
  type TextElementBar,
} from "@/lib/timeline/text-timeline-reducer";

function captionBar(i: number, overrides: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: `caption-${i}`,
    role: "narrated_caption",
    text: `line ${i}`,
    start_s: i,
    end_s: i + 1,
    ...overrides,
  };
}

describe("captionBarPatchFromMetaPatch", () => {
  it("maps every styling field the canvas preview reads off the bar", () => {
    expect(
      captionBarPatchFromMetaPatch({
        font: "Montserrat Bold",
        size_px: 96,
        color: "#FF0000",
        highlight_color: "#00FF00",
        stroke_width: 8,
        shadow_enabled: false,
        y_frac: 0.66,
      }),
    ).toEqual({
      font_family: "Montserrat Bold",
      size_px: 96,
      color: "#FF0000",
      highlight_color: "#00FF00",
      stroke_width: 8,
      shadow_enabled: false,
      y_frac: 0.66,
    });
  });

  it("carries `font: null` through as a real edit — resetting to the default face is not a no-op", () => {
    expect(captionBarPatchFromMetaPatch({ font: null })).toEqual({
      font_family: undefined,
    });
    expect(
      Object.prototype.hasOwnProperty.call(
        captionBarPatchFromMetaPatch({ font: null }),
        "font_family",
      ),
    ).toBe(true);
  });

  it("drops enabled/style — they have no per-bar equivalent, so mapping them would invent fields", () => {
    expect(captionBarPatchFromMetaPatch({ enabled: false, style: "word" })).toEqual({});
  });

  it("round-trips against its inverse", () => {
    const meta = { font: "Inter-Bold", size_px: 80, color: "#ABCDEF", stroke_width: 6 };
    expect(captionMetaPatchFromCaptionBarPatch(captionBarPatchFromMetaPatch(meta))).toEqual(meta);
  });
});

describe("PATCH_BARS", () => {
  const bars = [captionBar(0), captionBar(1), captionBar(2)];

  it("fans one styling patch across every caption bar", () => {
    const patch = captionBarPatchFromMetaPatch({ font: "Montserrat Bold", size_px: 96 });
    const next = textReducer(initTextEditorState(bars), {
      type: "PATCH_BARS",
      patches: bars.map((b) => ({ id: b.id, patch })),
    });
    for (const bar of next.bars) {
      expect(bar.font_family).toBe("Montserrat Bold");
      expect(bar.size_px).toBe(96);
    }
  });

  it("is ONE undo step for the whole fan-out, not one per bar", () => {
    const start = initTextEditorState(bars);
    const patched = textReducer(start, {
      type: "PATCH_BARS",
      patches: bars.map((b) => ({ id: b.id, patch: { size_px: 96 } })),
    });
    const undone = textReducer(patched, { type: "UNDO" });
    expect(undone.bars.map((b) => b.size_px)).toEqual(start.bars.map((b) => b.size_px));
  });

  it("applies a DIFFERENT patch per bar — the shape replace-all needs", () => {
    const next = textReducer(initTextEditorState(bars), {
      type: "PATCH_BARS",
      patches: [
        { id: "caption-0", patch: { text: "fixed zero" } },
        { id: "caption-2", patch: { text: "fixed two" } },
      ],
    });
    expect(next.bars.map((b) => b.text)).toEqual(["fixed zero", "line 1", "fixed two"]);
  });

  it("reverses a multi-bar replace-all in a single undo", () => {
    const start = initTextEditorState(bars);
    const replaced = textReducer(start, {
      type: "PATCH_BARS",
      patches: [
        { id: "caption-0", patch: { text: "a" } },
        { id: "caption-1", patch: { text: "b" } },
        { id: "caption-2", patch: { text: "c" } },
      ],
    });
    expect(textReducer(replaced, { type: "UNDO" }).bars.map((b) => b.text)).toEqual([
      "line 0",
      "line 1",
      "line 2",
    ]);
  });

  it("ignores unknown ids instead of throwing", () => {
    const state = initTextEditorState(bars);
    const next = textReducer(state, {
      type: "PATCH_BARS",
      patches: [{ id: "caption-99", patch: { size_px: 96 } }],
    });
    expect(next).toBe(state);
  });

  it("treats `$` sequences in a replacement as literal text, not substitution patterns", () => {
    // String.replace expands `$&` / `` $` `` / `$'` / `$1` in the REPLACEMENT.
    // Unescaped, replacing "marka" with "$`" rewrites the cue to everything
    // *before* the match — a silent corruption across every cue at once.
    const escape = (s: string) => s.replace(/\$/g, "$$$$");
    const pattern = /marka/gi;
    expect("markalar oyuncak".replace(pattern, escape("$`"))).toBe("$`lar oyuncak");
    expect("markalar oyuncak".replace(pattern, escape("$&"))).toBe("$&lar oyuncak");
    expect("markalar oyuncak".replace(pattern, escape("firma"))).toBe("firmalar oyuncak");
  });

  it("leaves the undo stack alone for an empty or no-op patch set", () => {
    const state = initTextEditorState(bars);
    expect(textReducer(state, { type: "PATCH_BARS", patches: [] })).toBe(state);
    expect(
      textReducer(state, {
        type: "PATCH_BARS",
        patches: [{ id: "caption-0", patch: {} }],
      }),
    ).toBe(state);
  });
});
