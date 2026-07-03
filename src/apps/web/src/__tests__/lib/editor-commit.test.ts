import {
  buildEditorCommitRequest,
  editorCommitBaseGeneration,
} from "@/lib/editor-commit";
import type { TextElement } from "@/lib/plan-api";

const element: TextElement = {
  id: "txt-1",
  text: "Hello",
  start_s: 0,
  end_s: 2,
  role: "generative_intro",
  position: "middle",
};

describe("buildEditorCommitRequest", () => {
  it("maps shell state to the backend editor-commit body", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      timelineDirty: true,
      slots: [
        {
          slotId: "slot-a",
          clipIndex: 1,
          inS: 0.5,
          durationS: 1.2,
          durationBeats: null,
          removed: false,
        },
        {
          slotId: null,
          clipIndex: 2,
          inS: 0,
          durationS: null,
          durationBeats: 4,
          removed: true,
        },
      ],
      soundMuted: true,
      title: "  Fresh title  ",
      variant: {
        render_generation_id: "gen-current",
        render_finished_at: "2026-07-01T00:00:00Z",
      },
    });

    expect(body).toEqual({
      text_elements: [element],
      timeline_slots: [
        {
          slot_id: "slot-a",
          clip_index: 1,
          in_s: 0.5,
          duration_s: 1.2,
          duration_beats: null,
          removed: false,
        },
        {
          slot_id: null,
          clip_index: 2,
          in_s: 0,
          duration_s: null,
          duration_beats: 4,
          removed: true,
        },
      ],
      mix: { music_level: 0.0 },
      title: "Fresh title",
      base_generation: "gen-current",
    });
  });

  it("omits untouched optional sections and falls back to an empty baseline", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      timelineDirty: false,
      slots: [],
      soundMuted: false,
      title: " ",
      variant: {},
    });

    expect(body.timeline_slots).toBeUndefined();
    expect(body.mix).toBeUndefined();
    expect(body.title).toBeNull();
    expect(body.base_generation).toBe("");
  });
});

describe("editorCommitBaseGeneration", () => {
  it("prefers render_generation_id, then render_finished_at, then empty string", () => {
    expect(
      editorCommitBaseGeneration({
        render_generation_id: "gen",
        render_finished_at: "finished",
      }),
    ).toBe("gen");
    expect(editorCommitBaseGeneration({ render_finished_at: "finished" })).toBe(
      "finished",
    );
    expect(editorCommitBaseGeneration({})).toBe("");
  });
});
