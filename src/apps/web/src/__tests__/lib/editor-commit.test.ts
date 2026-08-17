import {
  buildEditorCommitRequest,
  commitEditorSession,
  editorCommitBaseGeneration,
  EditorCommitConflictError,
  formatEditorCommitError,
  type EditorCommitResponse,
} from "@/lib/editor-commit";
import type { TextElement, VisualBlock } from "@/lib/plan-api";
import {
  MOTION_RUNTIME_HASH,
  type MotionPresetInstance,
} from "@nova/motion-runtime";

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
          lookPreset: "olive_film",
          lookAdjustments: {
            intensity: 0.75,
            warmth: 0.2,
            contrast: -0.1,
            grain: 0.3,
            vignette: 0.4,
          },
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
      caption_meta: undefined,
      timeline_slots: [
        {
          slot_id: "slot-a",
          clip_index: 1,
          in_s: 0.5,
          duration_s: 1.2,
          duration_beats: null,
          removed: false,
          transition_after: "cut",
          transition_duration_s: null,
          look_preset: "olive_film",
          look_adjustments: {
            intensity: 0.75,
            warmth: 0.2,
            contrast: -0.1,
            grain: 0.3,
            vignette: 0.4,
          },
        },
        {
          slot_id: null,
          clip_index: 2,
          in_s: 0,
          duration_s: null,
          duration_beats: 4,
          removed: true,
          transition_after: "cut",
          transition_duration_s: null,
          look_preset: "none",
        },
      ],
      mix: { music_level: 0.0 },
      music_track_id: undefined,
      remove_music: undefined,
      music_window: undefined,
      background_music: undefined,
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
      visual_blocks: undefined,
      motion_scenes: undefined,
      motion_runtime_hash: undefined,
      camera_effects: undefined,
      accepted_suggestion_ids: undefined,
      title: "Fresh title",
      lyrics: undefined,
      orientation: undefined,
      base_generation: "gen-current",
    });
  });

  it("can send text_elements and caption_cues in the same commit", () => {
    const cue = {
      text: "Edited caption",
      start_s: 0,
      end_s: 1.2,
      words: [{ text: "Original", start_s: 0, end_s: 0.4 }],
      smart_role: "hook" as const,
      smart_word_ids: ["w1"],
    };

    const body = buildEditorCommitRequest({
      elements: [element],
      captionCues: [cue],
      textDirty: true,
      captionDirty: true,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current" },
    });

    expect(body.text_elements).toEqual([element]);
    expect(body.caption_cues).toEqual([cue]);
  });

  it("omits lyrics unless dirty and emits toggle-off when requested", () => {
    const clean = buildEditorCommitRequest({
      elements: [],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      lyricsDirty: false,
      lyrics: { enabled: false },
      variant: { render_generation_id: "gen-current" },
    });
    expect(clean.lyrics).toBeUndefined();

    const dirty = buildEditorCommitRequest({
      elements: [],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      lyricsDirty: true,
      lyrics: { enabled: false },
      variant: { render_generation_id: "gen-current" },
    });
    expect(dirty.lyrics).toEqual({ enabled: false });
  });

  it("omits orientation unless dirty and emits the selected orientation when dirty", () => {
    const clean = buildEditorCommitRequest({
      elements: [],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      orientationDirty: false,
      orientation: "landscape",
      variant: { render_generation_id: "gen-current" },
    });
    expect(clean.orientation).toBeUndefined();

    const dirty = buildEditorCommitRequest({
      elements: [],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      orientationDirty: true,
      orientation: "landscape",
      variant: { render_generation_id: "gen-current" },
    });
    expect(dirty.orientation).toBe("landscape");
  });

  it("types orientation as an editor-commit response section", () => {
    const response: EditorCommitResponse = {
      ok: true,
      generation: "gen-next",
      sections: { orientation: true },
    };

    expect(response.sections.orientation).toBe(true);
  });

  it("builds combined text_elements and lyrics sections", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: true,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      lyricsDirty: true,
      lyrics: {
        enabled: true,
        line_overrides: {
          L0: {
            text: "New lyric",
            orig_text: "Old lyric",
            orig_start_s: 1.2,
          },
        },
      },
      variant: { render_generation_id: "gen-current" },
    });
    expect(body.text_elements).toEqual([element]);
    expect(body.lyrics).toEqual({
      enabled: true,
      line_overrides: {
        L0: {
          text: "New lyric",
          orig_text: "Old lyric",
          orig_start_s: 1.2,
        },
      },
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
    expect(body.caption_meta).toBeUndefined();
    expect(body.timeline_slots).toBeUndefined();
    expect(body.mix).toBeUndefined();
    expect(body.sound_effects).toBeUndefined();
    expect(body.media_overlays).toBeUndefined();
    expect(body.title).toBeUndefined();
    expect(body.base_generation).toBe("");
  });

  it("sends visual blocks only as an explicitly dirty atomic section", () => {
    const block: VisualBlock = {
      version: 1,
      id: "card-1",
      kind: "text_card",
      start_s: 1,
      end_s: 3,
      timing_mode: "manual",
      origin: "user",
      transition_in: "cut",
      transition_out: "fade",
      audio_policy: { base: "continue", sfx: "mute" },
      background: { type: "solid", color: "#26382F" },
    };
    const body = buildEditorCommitRequest({
      elements: [{ ...element, visual_block_id: "card-1", start_s: 1, end_s: 3 }],
      textDirty: true,
      visualBlocksDirty: true,
      visualBlocks: [block],
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: {
        render_generation_id: "gen-current",
        music_track_id: "track-current",
      },
    });

    expect(body.visual_blocks).toEqual([block]);
    expect(body.text_elements?.[0].visual_block_id).toBe("card-1");
  });

  it("sends motion scenes with the exact runtime compatibility token", () => {
    const scene: MotionPresetInstance = {
      id: "motion-1",
      preset_id: "card_stack",
      preset_version: 1,
      start_frame: 15,
      end_frame_exclusive: 75,
      palette: { primary: "#F5F5F4", accent: "#A3E635" },
      intensity: 0.8,
      params: {
        assets: [
          { asset_id: "image-1", gcs_path: "users/u/plan/i/pool/image-1.png" },
          { asset_id: "image-2", gcs_path: "users/u/plan/i/pool/image-2.png" },
        ],
      },
    };
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      motionScenesDirty: true,
      motionScenes: [scene],
      motionRuntimeHash: MOTION_RUNTIME_HASH,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current" },
    });

    expect(body.motion_scenes).toEqual([scene]);
    expect(body.motion_runtime_hash).toBe(MOTION_RUNTIME_HASH);
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

  it("stages music changes and omits stale timeline cuts", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: true,
      slots: [
        {
          slotId: "slot-a",
          clipIndex: 0,
          inS: 1,
          durationS: 2,
          durationBeats: null,
          removed: false,
        },
      ],
      musicDirty: true,
      musicTrackId: "track-new",
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current" },
    });

    expect(body.music_track_id).toBe("track-new");
    expect(body.timeline_slots).toBeUndefined();
    expect(body.base_generation).toBe("gen-current");
  });

  it("emits remove_music (never music_track_id: null) when the user removed the song", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      musicDirty: true,
      musicTrackId: null,
      musicRemoved: true,
      titleDirty: false,
      title: "",
      variant: {
        render_generation_id: "gen-current",
        music_track_id: "track-current",
      },
    });

    expect(body.remove_music).toBe(true);
    // Removal is its own flag — a null music_track_id is indistinguishable
    // from "omitted" server-side and must never be sent.
    expect(body.music_track_id).toBeUndefined();
  });

  it("does not emit remove_music when untouched or when the variant has no song", () => {
    const untouched = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: {
        render_generation_id: "gen-current",
        music_track_id: "track-current",
      },
    });
    expect(untouched.remove_music).toBeUndefined();
    expect(untouched.music_track_id).toBeUndefined();

    const noSong = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      musicDirty: true,
      musicTrackId: null,
      musicRemoved: true,
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current", music_track_id: null },
    });
    expect(noSong.remove_music).toBeUndefined();
  });

  it("sends background music only as an explicitly dirty audio section", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      backgroundMusicDirty: true,
      backgroundMusic: {
        track_id: "bed-new",
        enabled: true,
        start_s: 2.5,
        end_s: 12,
        gain_db: -16,
        muted: false,
      },
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current" },
    });

    expect(body.background_music).toEqual({
      track_id: "bed-new",
      enabled: true,
      start_s: 2.5,
      end_s: 12,
      gain_db: -16,
      muted: false,
    });
    expect(body.music_track_id).toBeUndefined();
    expect(body.timeline_slots).toBeUndefined();
  });

  it("atomically preserves unsaved timeline edits with a song window", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: true,
      slots: [
        {
          slotId: "slot-a",
          clipIndex: 0,
          inS: 1,
          durationS: 2,
          durationBeats: null,
          removed: false,
        },
      ],
      musicDirty: true,
      musicTrackId: "track-current",
      musicWindow: { startS: 14.25, alignment: "preserve_cuts" },
      titleDirty: false,
      title: "",
      variant: {
        render_generation_id: "gen-current",
        music_track_id: "track-current",
      },
    });

    expect(body.music_window).toEqual({
      start_s: 14.25,
      alignment: "preserve_cuts",
    });
    expect(body.timeline_slots).toEqual([
      {
        slot_id: "slot-a",
        clip_index: 0,
        in_s: 1,
        duration_s: 2,
        duration_beats: null,
        removed: false,
        look_preset: "none",
        transition_after: "cut",
        transition_duration_s: null,
      },
    ]);
    expect(body.music_track_id).toBeUndefined();
  });

  it("omits unsaved timeline edits when the song window re-syncs cuts", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: true,
      slots: [
        {
          slotId: "slot-a",
          clipIndex: 0,
          inS: 1,
          durationS: 2,
          durationBeats: null,
          removed: false,
        },
      ],
      musicWindow: { startS: 14.25, alignment: "resync_beats" },
      titleDirty: false,
      title: "",
      variant: {
        render_generation_id: "gen-current",
        music_track_id: "track-current",
      },
    });

    expect(body.timeline_slots).toBeUndefined();
  });

  it("emits caption_meta only when dirty", () => {
    const clean = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      captionMeta: { enabled: false, style: "word", font: null, y_frac: 0.72 },
      captionMetaDirty: false,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current" },
    });
    expect(clean.caption_meta).toBeUndefined();

    const dirty = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      captionMeta: {
        enabled: false,
        style: "word",
        font: null,
        y_frac: 0.72,
        size_px: 92,
        color: "#112233",
        highlight_color: "#A3E635",
        stroke_width: 7,
        shadow_enabled: false,
      },
      captionMetaDirty: true,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: { render_generation_id: "gen-current" },
    });
    expect(dirty.caption_meta).toEqual({
      enabled: false,
      style: "word",
      font: null,
      font_set: true,
      y_frac: 0.72,
      size_px: 92,
      color: "#112233",
      highlight_color: "#A3E635",
      stroke_width: 7,
      shadow_enabled: false,
    });
    expect(dirty.text_elements).toBeUndefined();
    expect(dirty.caption_cues).toBeUndefined();
  });

  it("sets caption_meta font_set only when font is present", () => {
    const withoutFont = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      captionMeta: { enabled: true, style: "sentence" },
      captionMetaDirty: true,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: {},
    });
    expect(withoutFont.caption_meta).toEqual({
      enabled: true,
      style: "sentence",
      font_set: false,
    });

    const resetFont = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      captionMeta: { font: null },
      captionMetaDirty: true,
      timelineDirty: false,
      slots: [],
      titleDirty: false,
      title: "",
      variant: {},
    });
    expect(resetFont.caption_meta).toEqual({ font: null, font_set: true });
  });
});

describe("buildEditorCommitRequest — accepted suggestion ids", () => {
  const overlay = {
    id: "ov-1",
    kind: "image" as const,
    src_gcs_path: "users/u/plan/i/pool/shot.png",
    position: "custom" as const,
    x_frac: 0.5,
    y_frac: 0.3,
    scale: 0.4,
    start_s: 2,
    end_s: 6,
    z: 0,
  };

  it("includes accepted ids only alongside the media_overlays section", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      overlaysDirty: true,
      mediaOverlays: [overlay],
      acceptedSuggestions: [{ id: "sug-1", overlayId: "ov-1" }],
      titleDirty: false,
      title: "",
      variant: {},
    });

    expect(body.media_overlays).toEqual([overlay]);
    expect(body.accepted_suggestion_ids).toEqual(["sug-1"]);
  });

  it("omits accepted ids when the overlays section is not being sent (422 guard)", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: true,
      timelineDirty: false,
      slots: [],
      overlaysDirty: false,
      mediaOverlays: [overlay],
      acceptedSuggestions: [{ id: "sug-1", overlayId: "ov-1" }],
      titleDirty: false,
      title: "",
      variant: {},
    });

    expect(body.media_overlays).toBeUndefined();
    expect(body.accepted_suggestion_ids).toBeUndefined();
  });

  it("filters accepted ids against the staged overlay ids (undone accepts drop out)", () => {
    const body = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      overlaysDirty: true,
      mediaOverlays: [overlay],
      acceptedSuggestions: [
        { id: "sug-1", overlayId: "ov-1" },
        // Undo removed this suggestion's card from the working overlays —
        // its envelope must NOT be resolved server-side.
        { id: "sug-2", overlayId: "ov-gone" },
      ],
      titleDirty: false,
      title: "",
      variant: {},
    });

    expect(body.accepted_suggestion_ids).toEqual(["sug-1"]);
  });

  it("omits the field entirely when the filter leaves no ids or none were accepted", () => {
    const allUndone = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      overlaysDirty: true,
      mediaOverlays: [overlay],
      acceptedSuggestions: [{ id: "sug-2", overlayId: "ov-gone" }],
      titleDirty: false,
      title: "",
      variant: {},
    });
    expect(allUndone.accepted_suggestion_ids).toBeUndefined();

    const noneAccepted = buildEditorCommitRequest({
      elements: [element],
      textDirty: false,
      timelineDirty: false,
      slots: [],
      overlaysDirty: true,
      mediaOverlays: [overlay],
      titleDirty: false,
      title: "",
      variant: {},
    });
    expect(noneAccepted.accepted_suggestion_ids).toBeUndefined();
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
      [
        "out-of-bounds timeline code object",
        { detail: { code: "TIMELINE_OUT_OF_BOUNDS" } },
        "One of the clips ran out of footage for this edit. Try trimming it or picking a different clip.",
      ],
      [
        "beat-aware out-of-bounds detail",
        {
          detail: {
            code: "TIMELINE_OUT_OF_BOUNDS",
            reason: "source_window_too_short",
            slot_order: 6,
            available_duration_s: 0.18,
            required_duration_s: 0.51,
            minimum_beat_duration_s: 0.32,
          },
        },
        "Clip 7 has 0.18s of footage after its start point, but the next song beat needs 0.32s. Move the start earlier or choose a longer clip.",
      ],
      [
        "seconds out-of-bounds detail",
        {
          detail: {
            code: "TIMELINE_OUT_OF_BOUNDS",
            slot_order: 2,
            available_duration_s: 0.5,
            required_duration_s: 1,
          },
        },
        "Clip 3 has 0.5s of footage after its start point, but this edit needs 1s. Shorten it, move the start earlier, or choose another clip.",
      ],
      [
        "negative in-point detail",
        {
          detail: {
            code: "TIMELINE_OUT_OF_BOUNDS",
            reason: "negative_in_point",
            slot_order: 1,
          },
        },
        "Clip 2 starts before its source footage. Set its start to 0s or later.",
      ],
      [
        "unavailable song code object",
        { detail: { code: "music_track_unavailable" } },
        "That song is no longer available. Choose another song and try again.",
      ],
      [
        "legacy preserve code object",
        { detail: { code: "linear_timeline_unavailable" } },
        "This older edit cannot preserve its cuts. Choose Re-sync to beats instead.",
      ],
      [
        "motion asset unavailable",
        { detail: { code: "motion_asset_unavailable" } },
        "One of this Creator Block's images is no longer available. Choose another ready image.",
      ],
      [
        "motion clean base unavailable",
        { detail: { code: "motion_clean_base_unavailable" } },
        "Creator Blocks need a clean video base, which is unavailable for this edit.",
      ],
      [
        "guided story format rebuild unsupported",
        { detail: { code: "guided_story_edit_unsupported" } },
        "This story couldn't be rebuilt in that format. Your current video is unchanged.",
      ],
      [
        "orientation unsupported",
        { detail: { code: "orientation_unsupported" } },
        "This edit can't be rebuilt in that format yet.",
      ],
    ];

    for (const [name, payload, expected] of cases) {
      expect(formatEditorCommitError(payload, 422)).toBe(expected);
      expect(formatEditorCommitError(payload, 422)).not.toBe("[object Object]");
      expect(name).toBeTruthy();
    }
  });

  it("formats structured motion runtime conflicts instead of stringifying the object", async () => {
    const originalFetch = global.fetch;
    global.fetch = jest.fn().mockResolvedValue({
      status: 409,
      ok: false,
      json: async () => ({ detail: { code: "motion_runtime_mismatch" } }),
    });
    try {
      await expect(
        commitEditorSession("item", "variant", { base_generation: "generation" }),
      ).rejects.toEqual(
        expect.objectContaining<Partial<EditorCommitConflictError>>({
          message: "Kria's motion renderer changed. Refresh before saving Creator Blocks.",
        }),
      );
    } finally {
      global.fetch = originalFetch;
    }
  });
});
