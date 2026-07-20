import { describe, expect, it } from "@jest/globals";
import validFixture from "../../../../api/tests/fixtures/copilot-ops/valid.json";
import invalidFixture from "../../../../api/tests/fixtures/copilot-ops/invalid.json";
import { validateCopilotOp, type CopilotValidationSnapshot } from "@/lib/edit-copilot/ops";

const validationSnapshot: CopilotValidationSnapshot = {
  total_duration_s: 10,
  text_bars: [{ id: "bar-0" }, { id: "bar-1" }],
  slots: [
    { output_start_s: 0, output_end_s: 3 },
    { output_start_s: 3, output_end_s: 6 },
    { output_start_s: 6, output_end_s: 8 },
  ],
  sfx: { placements: [{ id: "sfx-1" }] },
  overlays: {
    cards: [{ id: "overlay-1" }],
    pending_suggestions: [{ id: "suggestion-1" }],
  },
  captions: { cues: [{ id: "caption-1" }] },
};

describe("edit-copilot op contract fixtures", () => {
  it("accepts every shared valid op fixture", () => {
    for (const testCase of validFixture.cases) {
      expect(validateCopilotOp(testCase.op, validationSnapshot)).toMatchObject({
        ok: true,
      });
    }
  });

  it("rejects every shared invalid op fixture", () => {
    for (const testCase of invalidFixture.cases) {
      expect(validateCopilotOp(testCase.op, validationSnapshot)).toMatchObject({
        ok: false,
      });
    }
  });
});

describe("edit-copilot extended op validation", () => {
  it("accepts sub-0.6s positive clip durations and rejects non-positive values", () => {
    expect(
      validateCopilotOp(
        { op: "set_clip_duration", slot_index: 0, duration_s: 0.2 },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { duration_s: 0.2 } });
    for (const duration_s of [0, -0.1]) {
      expect(
        validateCopilotOp(
          { op: "set_clip_duration", slot_index: 0, duration_s },
          validationSnapshot,
        ),
      ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    }
  });

  it("rejects timing and removal for lyric bars", () => {
    const lyricSnapshot = {
      ...validationSnapshot,
      text_bars: [{ id: "lyric_L0", role: "lyric_line" }],
    };
    expect(
      validateCopilotOp(
        { op: "set_text_timing", bar_index: 0, start_s: 1 },
        lyricSnapshot,
      ),
    ).toMatchObject({
      ok: false,
      rejection: { message: "Lyric timing is locked to the vocal." },
    });
    expect(
      validateCopilotOp({ op: "remove_text", bar_index: 0 }, lyricSnapshot),
    ).toMatchObject({
      ok: false,
      rejection: { message: "Lyric timing is locked to the vocal." },
    });
    expect(
      validateCopilotOp({ op: "edit_text", bar_index: 0, text: "new" }, lyricSnapshot),
    ).toMatchObject({ ok: true });
  });

  it("normalizes valid sfx ops and rejects missing required fields", () => {
    expect(validateCopilotOp({ op: "add_sfx", effect_id: "whoosh", at_s: 99, gain: 3 }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "add_sfx", at_s: 9.9, gain: 2 } });
    expect(validateCopilotOp({ op: "patch_sfx", sfx_index: 0, at_s: -2, gain: -1 }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "patch_sfx", at_s: 0, gain: 0 } });
    expect(validateCopilotOp({ op: "patch_sfx", sfx_index: 0 }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "missing_required" } });
    expect(validateCopilotOp({ op: "remove_sfx", sfx_index: 2 }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
  });

  it("validates overlay ops, strips unknown patch keys, clamps numbers, and enforces enums", () => {
    expect(
      validateCopilotOp(
        {
          op: "patch_overlay",
          overlay_index: 0,
          patch: { x_frac: 2, y_frac: -1, scale: 2, display_mode: "fullscreen", ignored: true },
        },
        validationSnapshot,
      ),
    ).toMatchObject({
      ok: true,
      op: { patch: { x_frac: 1, y_frac: 0, scale: 1, display_mode: "fullscreen" } },
    });
    expect(validateCopilotOp({ op: "patch_overlay", overlay_index: 0, patch: { ignored: true } }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "empty_patch" } });
    expect(validateCopilotOp({ op: "patch_overlay", overlay_index: 0, patch: { position: "middle" } }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(validateCopilotOp({ op: "add_overlay", asset_id: "asset-1", start_s: 2, end_s: 1 }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_time" } });
    expect(validateCopilotOp({ op: "accept_overlay_suggestion", suggestion_id: "suggestion-1" }, validationSnapshot))
      .toMatchObject({ ok: true });
  });

  it("validates caption, music, title, mix, and tool ops", () => {
    expect(validateCopilotOp({ op: "edit_caption", cue_index: 0, text: "  hi\nthere  " }, validationSnapshot))
      .toMatchObject({ ok: true, op: { text: "hi there" } });
    expect(validateCopilotOp({ op: "edit_caption", cue_index: 0, text: "   " }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(validateCopilotOp({ op: "set_caption_timing", cue_index: 0, start_s: 3, end_s: 2 }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_time" } });
    expect(
      validateCopilotOp({ op: "set_caption_meta", patch: { enabled: true, style: "word", font: null, y_frac: 2, junk: 1 } }, validationSnapshot),
    ).toMatchObject({ ok: true, op: { patch: { enabled: true, style: "word", font: null, y_frac: 0.9 } } });
    expect(validateCopilotOp({ op: "set_caption_meta", patch: { junk: 1 } }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "empty_patch" } });
    expect(validateCopilotOp({ op: "swap_music", track_id: "track-1" }, validationSnapshot)).toMatchObject({ ok: true });
    expect(validateCopilotOp({ op: "set_mix", music_level: 2 }, validationSnapshot))
      .toMatchObject({ ok: true, op: { music_level: 1 } });
    expect(validateCopilotOp({ op: "set_title", title: "  Launch\nDay  " }, validationSnapshot))
      .toMatchObject({ ok: true, op: { title: "Launch Day" } });
    expect(validateCopilotOp({ op: "open_tool", tool: "sounds" }, validationSnapshot)).toMatchObject({ ok: true });
    expect(validateCopilotOp({ op: "open_tool", tool: "timeline" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
  });

  it("validates render layout ops shape-only", () => {
    expect(validateCopilotOp({ op: "set_intro_layout", layout: "cluster" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "set_intro_layout", layout: "cluster" } });
    expect(validateCopilotOp({ op: "set_intro_layout", layout: "linear" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "set_intro_layout", layout: "linear" } });
    expect(validateCopilotOp({ op: "set_intro_layout" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "missing_required" } });
    expect(validateCopilotOp({ op: "set_intro_layout", layout: "stacked" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
  });
});
