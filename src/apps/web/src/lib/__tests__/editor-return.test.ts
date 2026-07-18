import {
  buildPlanItemEditorReturnHref,
  editorCommitStartedRender,
  parsePlanItemEditorReturnSignal,
  stripPlanItemEditorReturnParams,
} from "../editor-return";

describe("editor return signal", () => {
  it("round-trips a render-started save signal", () => {
    const href = buildPlanItemEditorReturnHref("item-1", {
      variantId: "song_text",
      generation: "gen-123",
      priorFinishedAt: "2026-07-05T10:00:00Z",
      renderStarted: true,
    });

    expect(href).toBe(
      "/plan/items/item-1?editor_saved=1&editor_variant=song_text&editor_generation=gen-123&editor_render=1&editor_prior_finished_at=2026-07-05T10%3A00%3A00Z",
    );

    const parsed = parsePlanItemEditorReturnSignal(
      new URLSearchParams(href.split("?")[1]),
    );
    expect(parsed).toEqual({
      variantId: "song_text",
      generation: "gen-123",
      priorFinishedAt: "2026-07-05T10:00:00Z",
      renderStarted: true,
      key: "song_text:gen-123:2026-07-05T10:00:00Z:1",
    });
  });

  it("keeps title-only saves fresh without marking render started", () => {
    const href = buildPlanItemEditorReturnHref("item-1", {
      variantId: "song_text",
      generation: "old-gen",
      priorFinishedAt: null,
      renderStarted: false,
    });

    const parsed = parsePlanItemEditorReturnSignal(
      new URLSearchParams(href.split("?")[1]),
    );
    expect(parsed?.renderStarted).toBe(false);
    expect(parsed?.priorFinishedAt).toBeNull();
  });

  it("strips only editor return params and preserves other params", () => {
    expect(
      stripPlanItemEditorReturnParams(
        "?foo=1&editor_saved=1&editor_variant=v1&editor_generation=g1&editor_prior_finished_at=t&editor_render=1&bar=2",
      ),
    ).toBe("?foo=1&bar=2");
  });

  it("detects render-affecting editor sections", () => {
    expect(editorCommitStartedRender({ title: true })).toBe(false);
    expect(editorCommitStartedRender({ text_elements: true, title: true })).toBe(true);
    expect(editorCommitStartedRender({ caption_cues: true })).toBe(true);
    expect(editorCommitStartedRender({ media_overlays: true })).toBe(true);
    expect(editorCommitStartedRender({ orientation: true })).toBe(true);
  });

  it("ignores incomplete or absent signals", () => {
    expect(parsePlanItemEditorReturnSignal(new URLSearchParams(""))).toBeNull();
    expect(
      parsePlanItemEditorReturnSignal(new URLSearchParams("editor_saved=1")),
    ).toBeNull();
  });
});
