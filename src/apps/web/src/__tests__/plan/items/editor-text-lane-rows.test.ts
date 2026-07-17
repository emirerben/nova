import { describe, expect, it } from "@jest/globals";
import {
  deriveLaneRows,
  deriveTextLaneRows,
  seedBarsFromVariant,
  TEXT_LANE_BASE_HEIGHT_PX,
} from "@/app/plan/items/[id]/_editor/editor-bars";
import {
  EDITOR_HISTORY_DEPTH,
  initEditorHistoryState,
  recordSnapshot,
  undoSnapshot,
  type EditorDocument,
} from "@/app/plan/items/[id]/_editor/useEditorHistory";
import type { MediaOverlay, PlanItemVariant, SoundEffectPlacement } from "@/lib/plan-api";
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
      }),
    ]);
  });

  it("uses subtitled text_elements instead of caption cues for the text lane", () => {
    const variant = {
      variant_id: "subtitled",
      resolved_archetype: "subtitled",
      text_elements_user_edited: false,
      caption_cues: [{ text: "caption words", start_s: 0, end_s: 1 }],
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
      expect.objectContaining({ id: "title", text: "Big title", role: "generative_intro" }),
    ]);
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
