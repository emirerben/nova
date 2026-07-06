import {
  buildEditorCommitRequest,
  editorCommitBaseGeneration,
  formatEditorCommitError,
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
      mixDirty: true,
      mixLevel: 0,
      sfxDirty: true,
      soundEffects: [
        {
          id: "sfx-1",
          src_gcs_path: "users/u/plan/i/sfx/pop.mp3",
          at_s: 1,
          gain: 0.8,
        },
      ],
      overlaysDirty: true,
      mediaOverlays: [
        {
          id: "ov-1",
          kind: "video",
          src_gcs_path: "users/u/plan/i/overlays/card.mp4",
          position: "custom",
          x_frac: 0.24,
          y_frac: 0.68,
          scale: 0.72,
          start_s: 0.4,
          end_s: 2.4,
          clip_trim_start_s: 1.1,
          clip_trim_end_s: 3.1,
          clip_duration_s: 8.2,
          display_mode: "fullscreen",
          z: 0,
        },
      ],
      title: "  Fresh title  ",
      variant: {
        render_generation_id: "gen-current",
        render_finished_at: "2026-07-01T00:00:00Z",
      },
    });

    expect(body).toEqual({
      text_elements: [element],
      caption_cues: undefined,
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
      sound_effects: [
        {
          id: "sfx-1",
          src_gcs_path: "users/u/plan/i/sfx/pop.mp3",
          at_s: 1,
          gain: 0.8,
        },
      ],
      media_overlays: [
        {
          id: "ov-1",
          kind: "video",
          src_gcs_path: "users/u/plan/i/overlays/card.mp4",
          position: "custom",
          x_frac: 0.24,
          y_frac: 0.68,
          scale: 0.72,
          start_s: 0.4,
          end_s: 2.4,
          clip_trim_start_s: 1.1,
          clip_trim_end_s: 3.1,
          clip_duration_s: 8.2,
          display_mode: "fullscreen",
          z: 0,
        },
      ],
      title: "Fresh title",
      base_generation: "gen-current",
    });
  });

  it("omits untouched optional sections and falls back to an empty baseline", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: " ",
      variant: {},
    });

    expect(body.text_elements).toBeUndefined();
    expect(body.timeline_slots).toBeUndefined();
    expect(body.mix).toBeUndefined();
    expect(body.sound_effects).toBeUndefined();
    expect(body.media_overlays).toBeUndefined();
    expect(body.title).toBeUndefined();
    expect(body.base_generation).toBe("");
  });

  it("does not send mix for non-mixable variants even when mix changed", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      mixDirty: true,
      mixLevel: 0,
      titleDirty: false,
      title: "",
      variant: {
        render_generation_id: "prod-gen",
        editor_capabilities: { mix: false },
      },
    });

    expect(body.mix).toBeUndefined();
    expect(body.base_generation).toBe("prod-gen");
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

describe("formatEditorCommitError", () => {
  it("formats FastAPI detail shapes without object stringification", () => {
    const cases: Array<[string, unknown, string]> = [
      ["plain string", "No cached base video", "No cached base video"],
      [
        "detail string",
        { detail: "This edit has no voiceover to mix." },
        "This edit has no voiceover to mix.",
      ],
      [
        "validation errors",
        {
          detail: [
            {
              loc: ["body", "timeline_slots", 0, "duration_s"],
              msg: "Input should be greater than 0",
            },
            {
              loc: ["body", "text_elements", 1, "font_family"],
              msg: "Input should be a valid font",
            },
          ],
        },
        "timeline_slots.0.duration_s: Input should be greater than 0\ntext_elements.1.font_family: Input should be a valid font",
      ],
      [
        "named text element",
        {
          detail:
            "Text element bad-font: field font_family has invalid value 'illegal': Value error, unknown font_family",
        },
        "Text bad-font: field font_family — Value error, unknown font_family",
      ],
      ["timeline code object", { detail: { code: "TIMELINE_TOO_SHORT" } }, "TIMELINE_TOO_SHORT"],
    ];

    for (const [name, payload, expected] of cases) {
      expect(formatEditorCommitError(payload, 422)).toBe(expected);
      expect(formatEditorCommitError(payload, 422)).not.toBe("[object Object]");
      expect(name).toBeTruthy();
    }
  });
});
