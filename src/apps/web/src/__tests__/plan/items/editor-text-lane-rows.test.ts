import { describe, expect, it } from "@jest/globals";
import {
  deriveTextLaneRows,
  TEXT_LANE_BASE_HEIGHT_PX,
} from "@/app/plan/items/[id]/_editor/editor-bars";
import {
  recordSnapshot,
  undoSnapshot,
  type EditorDocument,
} from "@/app/plan/items/[id]/_editor/useEditorHistory";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

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
