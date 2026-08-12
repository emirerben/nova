import { describe, expect, it } from "@jest/globals";
import validFixture from "../../../../api/tests/fixtures/copilot-ops/valid.json";
import invalidFixture from "../../../../api/tests/fixtures/copilot-ops/invalid.json";
import {
  copilotOpFamily,
  validateCopilotOp,
  type CopilotValidationSnapshot,
} from "@/lib/edit-copilot/ops";

const validationSnapshot: CopilotValidationSnapshot = {
  total_duration_s: 10,
  text_bars: [{ id: "bar-0" }, { id: "bar-1" }],
  slots: [
    { key: "a", clip_index: 0, in_s: 0, duration_s: 3, output_start_s: 0, output_end_s: 3 },
    { key: "b", clip_index: 1, in_s: 1, duration_s: 3, output_start_s: 3, output_end_s: 6 },
    { key: "c", clip_index: 2, in_s: 0, duration_s: 2, output_start_s: 6, output_end_s: 8 },
  ],
  sfx: { placements: [{ id: "sfx-1" }] },
  overlays: {
    cards: [{ id: "overlay-1" }],
    pending_suggestions: [{ id: "suggestion-1" }],
  },
  captions: { cues: [{ id: "caption-1" }] },
  camera_effects: [{ start_s: 0.5, end_s: 2, intensity: 1 }],
  visual_blocks: [{ id: "visual-1" }],
  motion: {
    blocks: [{ id: "motion-1" }],
    asset_pool: [{ id: "image-1" }, { id: "image-2" }],
  },
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
  it.each(["staggered-slice", "ink-reveal", "handwriting"])(
    "accepts the editor-directed %s effect",
    (effect) => {
      expect(
        validateCopilotOp(
          {
            op: "patch_text_style",
            bar_index: 0,
            patch: { effect },
          },
          validationSnapshot,
        ),
      ).toMatchObject({ ok: true, op: { patch: { effect } } });
    },
  );

  it.each(["none", "slide-down"])("accepts the shared-picker %s effect", (effect) => {
    expect(
      validateCopilotOp(
        {
          op: "patch_text_style",
          bar_index: 0,
          patch: { effect },
        },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { patch: { effect } } });
  });

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

  it("accepts only Original and Stadium Diffusion as AI clip looks", () => {
    expect(
      validateCopilotOp(
        { op: "set_look_preset", slot_index: 1, look_preset: "stadium_diffusion" },
        validationSnapshot,
      ),
    ).toMatchObject({
      ok: true,
      op: { op: "set_look_preset", slot_index: 1, look_preset: "stadium_diffusion" },
    });
    expect(
      validateCopilotOp(
        { op: "set_look_preset", slot_index: 1, look_preset: "none" },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true });
    expect(
      validateCopilotOp(
        { op: "set_look_preset", slot_index: 1, look_preset: "olive_film" },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(
      validateCopilotOp(
        { op: "set_look_preset", slot_index: 99, look_preset: "stadium_diffusion" },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
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
      validateCopilotOp(
        { op: "set_transition", boundary_index: 0, transition: "crossfade", duration_s: 0.12 },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { duration_s: 0.12 } });
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
    expect(validateCopilotOp({ op: "replace_caption_text", find: "  Kriya  ", replace: "$& $1" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { find: "Kriya", replace: "$& $1" } });
    expect(validateCopilotOp({ op: "replace_caption_text", find: " ", replace: "Kria" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(validateCopilotOp({ op: "replace_caption_text", find: "Kriya", replace: "" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { replace: "" } });
    expect(validateCopilotOp({ op: "set_caption_timing", cue_index: 0, start_s: 3, end_s: 2 }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_time" } });
    expect(
      validateCopilotOp({ op: "set_caption_meta", patch: { enabled: true, style: "word", font: null, y_frac: 2, junk: 1 } }, validationSnapshot),
    ).toMatchObject({ ok: true, op: { patch: { enabled: true, style: "word", font: null, y_frac: 0.9 } } });
    expect(validateCopilotOp({ op: "set_caption_meta", patch: { junk: 1 } }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "empty_patch" } });
    expect(validateCopilotOp({ op: "set_caption_emphasis", cue_index: 0, emphasis: true }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "set_caption_emphasis", cue_index: 0, emphasis: true } });
    expect(validateCopilotOp({ op: "set_caption_emphasis", cue_index: 0, emphasis: false }, validationSnapshot))
      .toMatchObject({ ok: true, op: { emphasis: false } });
    expect(validateCopilotOp({ op: "set_caption_emphasis", cue_index: 0, emphasis: "yes" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "missing_required" } });
    expect(validateCopilotOp({ op: "set_caption_emphasis", cue_index: 5, emphasis: true }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
    expect(
      validateCopilotOp(
        { op: "set_caption_emphasis", cue_index: 0, emphasis: true },
        { ...validationSnapshot, captions: { cues: [{ id: "caption-1" }], cues_editable: false } },
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
    expect(
      validateCopilotOp(
        { op: "replace_caption_text", find: "Kriya", replace: "Kria" },
        { ...validationSnapshot, captions: { cues: [], cues_editable: false } },
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
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

  it("validates history ops (undo_last_edit/repeat_last_edit) as fieldless and maps them to the history family", () => {
    expect(validateCopilotOp({ op: "undo_last_edit" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "undo_last_edit" } });
    expect(validateCopilotOp({ op: "repeat_last_edit" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "repeat_last_edit" } });
    // No payload fields to strip/validate — any extra keys the model sends
    // are simply ignored, same as open_tool's tool-only shape but with none.
    expect(validateCopilotOp({ op: "undo_last_edit", stray: "field" }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "undo_last_edit" } });

    expect(copilotOpFamily({ op: "undo_last_edit" })).toBe("history");
    expect(copilotOpFamily({ op: "repeat_last_edit" })).toBe("history");
  });

  it("validates apply_custom_effect shape-only — deep filter/param checks are server-side", () => {
    const effect = {
      id: "vintage_1",
      label: "Vintage film",
      filters: [{ name: "curves", params: { preset: "vintage" } }],
      start_s: 0,
      end_s: 5,
      target: "full_frame",
    };
    expect(validateCopilotOp({ op: "apply_custom_effect", effect }, validationSnapshot))
      .toMatchObject({ ok: true, op: { op: "apply_custom_effect", effect } });
    expect(validateCopilotOp({ op: "apply_custom_effect" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "missing_required" } });
    expect(validateCopilotOp({ op: "apply_custom_effect", effect: "not-an-object" }, validationSnapshot))
      .toMatchObject({ ok: false, rejection: { reason: "missing_required" } });
    expect(copilotOpFamily({ op: "apply_custom_effect" })).toBe("custom_effect");
    // Deliberately distinct from set_intro_layout's "render" family.
    expect(copilotOpFamily({ op: "apply_custom_effect" })).not.toBe(
      copilotOpFamily({ op: "set_intro_layout" }),
    );
  });

  it("validates carousel-moment config shape and clamps duration_s", () => {
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { position: "intro", mode: "focus" } },
        validationSnapshot,
      ),
    ).toMatchObject({
      ok: true,
      op: { op: "set_carousel_moment", config: { position: "intro", mode: "focus" } },
    });
    expect(
      validateCopilotOp({ op: "set_carousel_moment", config: null }, validationSnapshot),
    ).toMatchObject({ ok: true, op: { op: "set_carousel_moment", config: null } });
    expect(validateCopilotOp({ op: "set_carousel_moment" }, validationSnapshot)).toMatchObject({
      ok: false,
      rejection: { reason: "missing_required" },
    });
    expect(
      validateCopilotOp({ op: "set_carousel_moment", config: "intro" }, validationSnapshot),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_type" } });
    expect(
      validateCopilotOp({ op: "set_carousel_moment", config: {} }, validationSnapshot),
    ).toMatchObject({ ok: false, rejection: { reason: "empty_patch" } });
    // "stills" is a legally persisted mode (auto-authored moments) but the
    // copilot must never be able to WRITE it — not in the op vocabulary.
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { mode: "stills" } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { position: "sideways" } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { duration_s: 100 } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { config: { duration_s: 15 } } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { duration_s: 0.1 } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { config: { duration_s: 2 } } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { focus_clip_index: 2 } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { config: { focus_clip_index: 2 } } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { focus_clip_index: null } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { config: { focus_clip_index: null } } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { focus_clip_index: -1 } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_type" } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { effect: "cover_flow" } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { config: { effect: "cover_flow" } } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { effect: "spin" } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { transition: "crossfade" } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { config: { transition: "crossfade" } } });
    expect(
      validateCopilotOp(
        { op: "set_carousel_moment", config: { transition: "wipe" } },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
  });

  it("clamps camera effects and validates effect patches against the snapshot", () => {
    expect(
      validateCopilotOp(
        { op: "add_camera_effect", start_s: 0.5, end_s: 2, intensity: 4 },
        validationSnapshot,
      ),
    ).toMatchObject({
      ok: true,
      op: { op: "add_camera_effect", start_s: 0.5, end_s: 2, intensity: 0.08 },
    });
    expect(
      validateCopilotOp(
        { op: "patch_camera_effect", camera_effect_index: 0, intensity: -2 },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { intensity: 0.01 } });
    expect(
      validateCopilotOp(
        { op: "remove_camera_effect", camera_effect_index: 2 },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
    expect(
      validateCopilotOp(
        { op: "add_camera_effect", start_s: 9.95, end_s: 12 },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_time" } });
  });

  it("uses the shared render-safe transition contract", () => {
    expect(
      validateCopilotOp(
        { op: "set_transition", boundary_index: 0, transition: "dip_to_black", duration_s: 2 },
        validationSnapshot,
      ),
    ).toMatchObject({
      ok: true,
      op: { boundary_index: 0, transition: "dip_to_black", duration_s: 0.3 },
    });
    expect(
      validateCopilotOp(
        { op: "set_transition", boundary_index: 2, transition: "flash" },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_index" } });
    expect(
      validateCopilotOp(
        { op: "set_transition", boundary_index: 0, transition: "spin" },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_value" } });
    expect(
      validateCopilotOp(
        { op: "set_transition", boundary_index: 0, transition: "crossfade" },
        {
          ...validationSnapshot,
          slots: [
            { output_start_s: 0, output_end_s: 0.2 },
            { output_start_s: 0.2, output_end_s: 2 },
          ],
        },
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "invalid_time" } });

    expect(
      validateCopilotOp(
        { op: "set_transition", boundary_index: 0, transition: "crossfade" },
        {
          ...validationSnapshot,
          slots: [
            { output_start_s: 0, output_end_s: 2 },
            { removed: true, output_start_s: null, output_end_s: null },
            { output_start_s: 2, output_end_s: 4 },
          ],
        },
      ),
    ).toMatchObject({ ok: true, op: { boundary_index: 0, duration_s: 0.3 } });
  });

  it("accepts only complete normalized generated-asset operations", () => {
    expect(
      validateCopilotOp(
        {
          op: "insert_generated_asset",
          asset_id: "asset-1",
          clip_index: 3,
          insert_at_s: 4,
          duration_s: 5,
        },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { clip_index: 3, duration_s: 5 } });
    expect(
      validateCopilotOp(
        { op: "insert_generated_asset", asset_id: "asset-1", insert_at_s: 4 },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false, rejection: { reason: "missing_required" } });
    expect(
      validateCopilotOp(
        {
          op: "replace_generated_segment",
          asset_id: "asset-2",
          clip_index: 3,
          source_clip_index: 1,
          source_start_s: 1,
          source_end_s: 4,
          duration_s: 3.5,
        },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { source_clip_index: 1, duration_s: 3.5 } });
  });

  it("validates visual-block fade patches", () => {
    expect(
      validateCopilotOp(
        {
          op: "set_visual_fade",
          visual_block_index: 0,
          transition_in: "fade",
        },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: true, op: { transition_in: "fade" } });
    expect(
      validateCopilotOp(
        { op: "set_visual_fade", visual_block_index: 0, transition_out: "spin" },
        validationSnapshot,
      ),
    ).toMatchObject({ ok: false });
  });

  it("validates Creator Block IDs, assets, timing, palettes, patch, and remove", () => {
    expect(validateCopilotOp({
      op: "add_motion_block",
      preset_id: "card_stack",
      start_s: 1,
      end_s: 5,
      params: { asset_ids: ["image-1", "image-2"] },
      palette: { primary: "#101010", accent: "#C7FF3D" },
    }, validationSnapshot)).toMatchObject({ ok: true });
    expect(validateCopilotOp({
      op: "add_motion_block",
      preset_id: "copied_name",
      start_s: 1,
      end_s: 2,
      params: {},
    }, validationSnapshot)).toMatchObject({ ok: false });
    expect(validateCopilotOp({
      op: "add_motion_block",
      preset_id: "card_stack",
      start_s: 1,
      end_s: 5,
      params: { asset_ids: ["image-1", "not-eligible"] },
    }, validationSnapshot)).toMatchObject({ ok: false });
    expect(validateCopilotOp({
      op: "patch_motion_block",
      motion_id: "motion-1",
      patch: { params: { text: "NEW" }, intensity: 0.4 },
    }, validationSnapshot)).toMatchObject({ ok: true });
    expect(validateCopilotOp({
      op: "remove_motion_block",
      motion_id: "motion-1",
    }, validationSnapshot)).toMatchObject({ ok: true });
  });
});

describe("clampAtS with unknown duration", () => {
  it("keeps at_s when total_duration_s is 0 (slot-less subtitled variant)", () => {
    const zeroTotal: CopilotValidationSnapshot = { ...validationSnapshot, total_duration_s: 0 };
    const res = validateCopilotOp(
      { op: "add_sfx", effect_id: "fx", at_s: 46.22, gain: 0.7 },
      zeroTotal,
    );
    expect(res).toMatchObject({ ok: true, op: { at_s: 46.22 } });
  });
});
