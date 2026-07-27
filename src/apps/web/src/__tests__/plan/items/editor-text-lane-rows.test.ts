import { describe, expect, it } from "@jest/globals";
import {
  AI_SEQUENCE_BADGE_LABEL,
  barsToCaptionCues,
  barsToTextElements,
  captionMetaPatchFromCaptionBarPatch,
  convertCaptionCues,
  deriveLaneRows,
  deriveTextLaneRows,
  isAiSequenceBar,
  localCaptionBarPatchFromPatch,
  seedBarsFromVariant,
  smartCaptionPreviewSizePx,
  smartStyleForRole,
  SMART_ROLE_BADGE_LABELS,
  TEXT_LANE_BASE_HEIGHT_PX,
} from "@/app/plan/items/[id]/_editor/editor-bars";
import {
  EDITOR_HISTORY_DEPTH,
  initEditorHistoryState,
  recordSnapshot,
  undoSnapshot,
  type EditorDocument,
} from "@/app/plan/items/[id]/_editor/useEditorHistory";
import type {
  CaptionCue,
  MediaOverlay,
  PlanItemVariant,
  SoundEffectPlacement,
  TextElement,
} from "@/lib/plan-api";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

const SFX_SUB_LANE_BASE_HEIGHT_PX = 32;

function bar(id: string): TextElementBar {
  return {
    id,
    text: id,
    start_s: 0,
    end_s: 2,
    role: "generative_intro",
  };
}

function doc(bars: TextElementBar[]): EditorDocument {
  return {
    bars,
    slots: null,
    videoMuted: false,
    soundMuted: false,
    title: "",
  };
}

function sfx(id: string): SoundEffectPlacement {
  return {
    id,
    src_gcs_path: `sound-effects/${id}.wav`,
    at_s: 0,
    gain: 1,
  };
}

function overlay(id: string): MediaOverlay {
  return {
    id,
    kind: "image",
    src_gcs_path: `media-uploads/${id}.png`,
    preview_url: `https://signed.example/${id}.png`,
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 0,
    end_s: 2,
    z: 0,
  };
}

function rowIds<T extends { id: string }>(
  items: T[],
  baseHeightPx: number,
): Array<[string, number]> {
  return deriveLaneRows(items, { baseHeightPx }).rows.map((row) => [
    row.item.id,
    row.rowIndex,
  ]);
}

describe("deriveTextLaneRows", () => {
  it("assigns appended text bars to the next compacted row", () => {
    const rows = deriveTextLaneRows([
      bar("first"),
      bar("second"),
      bar("third"),
    ]);

    expect(rows.rows.map((row) => [row.bar.id, row.rowIndex])).toEqual([
      ["first", 0],
      ["second", 1],
      ["third", 2],
    ]);
  });

  it("compacts rows after a middle bar is deleted", () => {
    const rows = deriveTextLaneRows([bar("first"), bar("third")]);

    expect(rows.totalHeightPx).toBe(TEXT_LANE_BASE_HEIGHT_PX);
    expect(rows.rows.map((row) => [row.bar.id, row.rowIndex])).toEqual([
      ["first", 0],
      ["third", 1],
    ]);
  });

  it("restores the former row order when undo brings back a deleted bar", () => {
    const beforeDelete = doc([bar("first"), bar("second"), bar("third")]);
    const afterDelete = doc([beforeDelete.bars[0], beforeDelete.bars[2]]);
    const history = recordSnapshot(
      { past: [], future: [], lastTag: null },
      beforeDelete,
    );

    const undo = undoSnapshot(history, afterDelete);

    expect(undo).not.toBeNull();
    expect(
      deriveTextLaneRows(undo?.doc.bars ?? []).rows.map((row) => [
        row.bar.id,
        row.rowIndex,
      ]),
    ).toEqual([
      ["first", 0],
      ["second", 1],
      ["third", 2],
    ]);
  });
});

describe("bounded editor history", () => {
  it("does not report the saved baseline after the oldest snapshot is evicted", () => {
    let history = initEditorHistoryState();
    let current = doc([]);
    for (let index = 0; index <= EDITOR_HISTORY_DEPTH; index += 1) {
      history = recordSnapshot(history, current);
      current = doc([bar(`edit-${index}`)]);
    }

    while (history.past.length > 0) {
      const undo = undoSnapshot(history, current);
      expect(undo).not.toBeNull();
      history = undo!.history;
      current = undo!.doc;
    }

    expect(history.baselineReachable).toBe(false);
    expect(current.bars[0]?.id).toBe("edit-0");
  });
});

describe("seedBarsFromVariant", () => {
  it("prefers projected text_elements over lossy scene_timings for generated sequences", () => {
    const variant = {
      variant_id: "original_text",
      text_elements_user_edited: false,
      scene_timings: [{ text: "", start_s: 0.3, end_s: 1.8 }],
      text_elements: [
        {
          id: "sequence-1",
          text: "This is what it's all about.",
          start_s: 0.3,
          end_s: 1.8,
          role: "generative_sequence",
          position: "custom",
          x_frac: 0.49,
          y_frac: 0.44,
          size_px: 122,
          font_family: "Great Vibes",
          color: "#FFFFFF",
          fade_out_ms: 350,
          glow_color: "#7CFF8A",
          glow_strength: 0.8,
        },
      ],
    } as unknown as PlanItemVariant;

    expect(seedBarsFromVariant(variant)).toEqual([
      expect.objectContaining({
        id: "sequence-1",
        text: "This is what it's all about.",
        x_frac: 0.49,
        y_frac: 0.44,
        size_px: 122,
        font_family: "Great Vibes",
        fade_out_ms: 350,
        glow_color: "#7CFF8A",
        glow_strength: 0.8,
      }),
    ]);
  });

  it("loads subtitled caption cues and Smart title text as separate timeline bars", () => {
    const variant = {
      variant_id: "subtitled",
      resolved_archetype: "subtitled",
      text_elements_user_edited: false,
      caption_cues: [{ text: "caption words", start_s: 0, end_s: 1 }],
      voiceover_caption_font: "Playfair Display",
      caption_size_px: 92,
      caption_text_color: "#112233",
      caption_highlight_color: "#A3E635",
      caption_stroke_width: 7,
      caption_shadow_enabled: false,
      text_elements: [
        {
          id: "title",
          text: "Big title",
          start_s: 0,
          end_s: 2,
          role: "generative_intro",
          position: "middle",
        },
      ],
    } as unknown as PlanItemVariant;

    expect(seedBarsFromVariant(variant)).toEqual([
      expect.objectContaining({
        id: "caption-0",
        text: "caption words",
        role: "narrated_caption",
        font_family: "Playfair Display",
        size_px: 92,
        color: "#112233",
        highlight_color: "#A3E635",
        stroke_width: 7,
        shadow_enabled: false,
      }),
      expect.objectContaining({ id: "title", text: "Big title", role: "generative_intro" }),
    ]);
  });

  it("ignores projected caption text_elements when real caption cues are loaded", () => {
    const variant = {
      variant_id: "subtitled",
      resolved_archetype: "subtitled",
      caption_cues: [{ text: "real cue", start_s: 0, end_s: 1 }],
      text_elements: [
        {
          id: "projected-caption",
          text: "duplicate cue",
          start_s: 0,
          end_s: 1,
          role: "generative_sequence",
          source_params: { source: "caption_cue" },
        },
        {
          id: "smart-title",
          text: "Smart title",
          start_s: 0,
          end_s: 2,
          role: "generative_intro",
        },
      ],
    } as unknown as PlanItemVariant;

    expect(seedBarsFromVariant(variant).map((bar) => bar.id)).toEqual([
      "caption-0",
      "smart-title",
    ]);
  });
});

describe("barsToCaptionCues", () => {
  it("preserves original cue words and Smart Caption metadata", () => {
    const original: CaptionCue = {
      text: "old words",
      start_s: 0,
      end_s: 1,
      words: [{ text: "old", start_s: 0, end_s: 0.4 }],
      smart_role: "hook",
      smart_word_ids: ["w1"],
      smart_keep_together: [[0, 1]],
    };

    expect(
      barsToCaptionCues(
        [
          {
            id: "caption-0",
            role: "narrated_caption",
            text: "edited words",
            start_s: 0.1,
            end_s: 1.2,
          },
        ],
        new Map([["caption-0", original]]),
      ),
    ).toEqual([
      {
        ...original,
        text: "edited words",
        start_s: 0.1,
        end_s: 1.2,
      },
    ]);
  });

  it("ignores legacy synthetic subtitled timeline rows", () => {
    expect(
      barsToCaptionCues([
        {
          id: "caption-0",
          role: "narrated_caption",
          text: "real editable cue",
          start_s: 0,
          end_s: 1,
        },
        {
          id: "subtitled-caption-0",
          role: "narrated_caption",
          text: "stale duplicate row",
          start_s: 0,
          end_s: 1,
        },
      ]),
    ).toEqual([
      {
        text: "real editable cue",
        start_s: 0,
        end_s: 1,
      },
    ]);
  });

  it("4b: writes a user-set smart_style/smart_emphasis, overriding the original", () => {
    const original: CaptionCue = {
      text: "we flew to Turkey",
      start_s: 0,
      end_s: 1,
      smart_role: "hook",
      smart_style: null,
      smart_emphasis: false,
    };

    expect(
      barsToCaptionCues(
        [
          {
            id: "caption-0",
            role: "narrated_caption",
            text: "we flew to Turkey",
            start_s: 0,
            end_s: 1,
            // Emphasize toggle ON: editor sets both fields explicitly.
            smart_style: "hook",
            smart_emphasis: true,
          },
        ],
        new Map([["caption-0", original]]),
      ),
    ).toEqual([{ ...original, smart_style: "hook", smart_emphasis: true }]);
  });

  it("4b: an untouched bar (smart_style/smart_emphasis undefined) preserves the original cue", () => {
    const original: CaptionCue = {
      text: "we flew to Turkey",
      start_s: 0,
      end_s: 1,
      smart_role: "hook",
      smart_style: "hook",
      smart_emphasis: true,
    };

    expect(
      barsToCaptionCues(
        [
          {
            id: "caption-0",
            role: "narrated_caption",
            text: "we flew to Turkey, honestly",
            start_s: 0,
            end_s: 1.4,
          },
        ],
        new Map([["caption-0", original]]),
      ),
    ).toEqual([{ ...original, text: "we flew to Turkey, honestly", end_s: 1.4 }]);
  });
});

describe("convertCaptionCues smart-caption fields (4b)", () => {
  it("seeds role/style/emphasis onto the bar for the badge + toggle", () => {
    const cues: CaptionCue[] = [
      {
        text: "we flew to Turkey",
        start_s: 0,
        end_s: 1,
        smart_role: "hook",
        smart_style: "hook",
        smart_emphasis: true,
      },
    ];
    const [bar] = convertCaptionCues(cues);
    expect(bar.smart_role).toBe("hook");
    expect(bar.smart_style).toBe("hook");
    expect(bar.smart_emphasis).toBe(true);
  });

  it("leaves the fields undefined (not null) for a plain cue", () => {
    const [bar] = convertCaptionCues([{ text: "plain", start_s: 0, end_s: 1 }]);
    expect(bar.smart_role).toBeUndefined();
    expect(bar.smart_style).toBeUndefined();
    expect(bar.smart_emphasis).toBeUndefined();
  });
});

describe("smartStyleForRole (4b)", () => {
  it("maps every SemanticRole to its closed smart_style token", () => {
    expect(smartStyleForRole("hook")).toBe("hook");
    expect(smartStyleForRole("context_shift")).toBe("context");
    expect(smartStyleForRole("list_item")).toBe("list_item");
    expect(smartStyleForRole("example")).toBe("example");
    expect(smartStyleForRole("payoff")).toBe("payoff");
    expect(smartStyleForRole("cta")).toBe("cta");
  });

  it("falls back to hook for a role-less cue", () => {
    expect(smartStyleForRole(null)).toBe("hook");
    expect(smartStyleForRole(undefined)).toBe("hook");
  });

  it("has a badge label for every role smartStyleForRole accepts", () => {
    for (const role of ["hook", "context_shift", "list_item", "example", "payoff", "cta"] as const) {
      expect(SMART_ROLE_BADGE_LABELS[role]).toEqual(expect.any(String));
    }
  });
});

describe("smartCaptionPreviewSizePx (4b)", () => {
  it("scales up for emphasized-hierarchy roles and down for example", () => {
    expect(smartCaptionPreviewSizePx(64, "hook")).toBeGreaterThan(64);
    expect(smartCaptionPreviewSizePx(64, "example")).toBeLessThan(64);
  });

  it("is a no-op preview for a role-less/absent style", () => {
    expect(smartCaptionPreviewSizePx(64, null)).toBe(64);
    expect(smartCaptionPreviewSizePx(64, undefined)).toBe(64);
  });
});

describe("isAiSequenceBar (PR-B: AI sequence badge)", () => {
  const aiSequenceBar: TextElementBar = {
    id: "sequence-1",
    text: "edits and I didn't really like CapCut",
    start_s: 0.3,
    end_s: 1.8,
    role: "generative_sequence",
    source_params: { source: "sequence_scene", key: "0:1" },
  };

  it("is true for a generative_sequence bar carrying the sequence_scene provenance marker", () => {
    expect(isAiSequenceBar(aiSequenceBar)).toBe(true);
  });

  it("is false for a generative_sequence bar with no source_params (user-typed split & place text)", () => {
    expect(
      isAiSequenceBar({
        id: "user-sequence-1",
        text: "my own composed beat",
        start_s: 0,
        end_s: 2,
        role: "generative_sequence",
      }),
    ).toBe(false);
  });

  it("is false for a generative_sequence bar whose source_params carries an unrelated source", () => {
    expect(
      isAiSequenceBar({
        id: "projected-caption",
        text: "duplicate cue",
        start_s: 0,
        end_s: 1,
        role: "generative_sequence",
        source_params: { source: "caption_cue" },
      }),
    ).toBe(false);
  });

  it("is false for a plain text bar (generative_intro)", () => {
    expect(
      isAiSequenceBar({
        id: "title-1",
        text: "Big title",
        start_s: 0,
        end_s: 2,
        role: "generative_intro",
      }),
    ).toBe(false);
  });

  it("is false for a caption bar", () => {
    expect(
      isAiSequenceBar({
        id: "caption-0",
        text: "we flew to Turkey",
        start_s: 0,
        end_s: 1,
        role: "narrated_caption",
      }),
    ).toBe(false);
  });

  it("is false for null/undefined", () => {
    expect(isAiSequenceBar(null)).toBe(false);
    expect(isAiSequenceBar(undefined)).toBe(false);
  });

  it("has a non-empty badge label", () => {
    expect(AI_SEQUENCE_BADGE_LABEL).toEqual(expect.any(String));
    expect(AI_SEQUENCE_BADGE_LABEL.length).toBeGreaterThan(0);
  });

  it("survives the Save round-trip untouched (display-only, no payload change)", () => {
    const original: TextElement = {
      id: "sequence-1",
      text: "edits and I didn't really like CapCut",
      start_s: 0.3,
      end_s: 1.8,
      role: "generative_sequence",
      source_params: { source: "sequence_scene", key: "0:1" },
    };
    const [saved] = barsToTextElements([aiSequenceBar], new Map([[original.id, original]]));
    expect(saved.source_params).toEqual(original.source_params);
    expect(saved.role).toBe("generative_sequence");
    expect(isAiSequenceBar(aiSequenceBar)).toBe(true);
  });
});

describe("caption bar style patches", () => {
  it("routes renderer-backed caption appearance to caption_meta and drops unsupported text fields", () => {
    const patch = {
      font_family: "Playfair Display",
      size_px: 92,
      color: "#112233",
      highlight_color: "#A3E635",
      stroke_width: 7,
      shadow_enabled: false,
      y_frac: 0.66,
      effect: "float",
      max_width_frac: 0.7,
      behind_subject: true,
    } as Partial<Omit<TextElementBar, "id" | "role">>;

    expect(captionMetaPatchFromCaptionBarPatch(patch)).toEqual({
      font: "Playfair Display",
      size_px: 92,
      color: "#112233",
      highlight_color: "#A3E635",
      stroke_width: 7,
      shadow_enabled: false,
      y_frac: 0.66,
    });
    expect(localCaptionBarPatchFromPatch(patch)).toEqual({
      font_family: "Playfair Display",
      size_px: 92,
      color: "#112233",
      highlight_color: "#A3E635",
      stroke_width: 7,
      shadow_enabled: false,
      y_frac: 0.66,
    });
  });
});

describe("deriveLaneRows", () => {
  it("assigns appended SFX to the next compacted row", () => {
    const rows = deriveLaneRows([sfx("first"), sfx("second"), sfx("third")], {
      baseHeightPx: SFX_SUB_LANE_BASE_HEIGHT_PX,
    });

    expect(rows.rows.map((row) => [row.item.id, row.rowIndex])).toEqual([
      ["first", 0],
      ["second", 1],
      ["third", 2],
    ]);
  });

  it("compacts SFX rows after a middle effect is deleted", () => {
    const rows = deriveLaneRows([sfx("first"), sfx("third")], {
      baseHeightPx: SFX_SUB_LANE_BASE_HEIGHT_PX,
    });

    expect(rows.totalHeightPx).toBe(SFX_SUB_LANE_BASE_HEIGHT_PX);
    expect(rows.rows.map((row) => [row.item.id, row.rowIndex])).toEqual([
      ["first", 0],
      ["third", 1],
    ]);
  });

  it("restores the former SFX row order when undo brings back a deleted effect", () => {
    const beforeDelete: EditorDocument = {
      ...doc([]),
      sfx: [sfx("first"), sfx("second"), sfx("third")],
    };
    const afterDelete: EditorDocument = {
      ...beforeDelete,
      sfx: [beforeDelete.sfx![0], beforeDelete.sfx![2]],
    };
    const history = recordSnapshot(
      { past: [], future: [], lastTag: null },
      beforeDelete,
    );

    const undo = undoSnapshot(history, afterDelete);

    expect(undo).not.toBeNull();
    expect(
      rowIds(undo?.doc.sfx ?? [], SFX_SUB_LANE_BASE_HEIGHT_PX),
    ).toEqual([
      ["first", 0],
      ["second", 1],
      ["third", 2],
    ]);
  });

  it("assigns appended overlays to the next compacted row", () => {
    expect(
      rowIds(
        [overlay("first"), overlay("second"), overlay("third")],
        TEXT_LANE_BASE_HEIGHT_PX,
      ),
    ).toEqual([
      ["first", 0],
      ["second", 1],
      ["third", 2],
    ]);
  });

  it("compacts overlay rows after a middle overlay is deleted", () => {
    const rows = deriveLaneRows([overlay("first"), overlay("third")], {
      baseHeightPx: TEXT_LANE_BASE_HEIGHT_PX,
    });

    expect(rows.totalHeightPx).toBe(TEXT_LANE_BASE_HEIGHT_PX);
    expect(rows.rows.map((row) => [row.item.id, row.rowIndex])).toEqual([
      ["first", 0],
      ["third", 1],
    ]);
  });

  it("restores the former overlay row order when undo brings back a deleted overlay", () => {
    const beforeDelete: EditorDocument = {
      ...doc([]),
      overlays: [overlay("first"), overlay("second"), overlay("third")],
    };
    const afterDelete: EditorDocument = {
      ...beforeDelete,
      overlays: [beforeDelete.overlays![0], beforeDelete.overlays![2]],
    };
    const history = recordSnapshot(
      { past: [], future: [], lastTag: null },
      beforeDelete,
    );

    const undo = undoSnapshot(history, afterDelete);

    expect(undo).not.toBeNull();
    expect(rowIds(undo?.doc.overlays ?? [], TEXT_LANE_BASE_HEIGHT_PX)).toEqual([
      ["first", 0],
      ["second", 1],
      ["third", 2],
    ]);
  });
});
