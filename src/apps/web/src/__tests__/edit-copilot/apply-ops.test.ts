import { applyCopilotOps } from "@/lib/edit-copilot/apply-ops";
import { buildCopilotSnapshot } from "@/lib/edit-copilot/snapshot";
import type { DraftSlot } from "@/app/generative/timeline-math";
import type { TextElementBar } from "@/lib/timeline/text-timeline-reducer";

function bar(over: Partial<TextElementBar> = {}): TextElementBar {
  return {
    id: "bar-1",
    text: "old hook",
    start_s: 0,
    end_s: 3,
    role: "generative_intro",
    font_family: "Inter",
    size_px: 64,
    color: "#FFFFFF",
    effect: "static",
    alignment: "center",
    position: "middle",
    ...over,
  };
}

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "slot-1",
    slotId: "slot-1",
    clipIndex: 0,
    inS: 0,
    durationS: 4,
    durationBeats: null,
    removed: false,
    momentDescription: null,
    ...over,
  };
}

const clips = [
  { source_duration_s: 10 },
  { source_duration_s: 8 },
  { source_duration_s: 7 },
];

function ctx(over: {
  bars?: TextElementBar[];
  slots?: DraftSlot[];
  capabilities?: Parameters<typeof buildCopilotSnapshot>[3];
} = {}) {
  const bars = over.bars ?? [bar(), bar({ id: "bar-2", text: "second", start_s: 3, end_s: 5 })];
  const slots = over.slots ?? [
    slot({ key: "a", slotId: "a", durationS: 3 }),
    slot({ key: "b", slotId: "b", clipIndex: 1, inS: 1, durationS: 4 }),
    slot({ key: "c", slotId: "c", clipIndex: 2, durationS: 2 }),
  ];
  const capabilities = over.capabilities ?? { text_elements: true, timeline: true, split_clips: true };
  return {
    bars,
    slots,
    snapshot: buildCopilotSnapshot(bars, slots, clips, capabilities),
    capabilities,
    makeTextBarId: () => "new-text",
    makeSlotKey: (s: DraftSlot) => `${s.key}-split`,
  };
}

describe("applyCopilotOps", () => {
  it("maps every text op to the expected text action", () => {
    expect(applyCopilotOps([{ op: "edit_text", bar_index: 0, text: "new hook" }], ctx()).textActions)
      .toEqual([{ type: "EDIT_TEXT", id: "bar-1", text: "new hook" }]);

    expect(
      applyCopilotOps(
        [{ op: "patch_text_style", bar_index: 0, patch: { size_px: 54, font_family: "Playfair Display" } }],
        ctx(),
      ).textActions,
    ).toEqual([
      {
        type: "PATCH_BAR",
        id: "bar-1",
        patch: { size_px: 54, font_family: "Playfair Display", size_class: undefined },
      },
    ]);

    expect(
      applyCopilotOps([{ op: "set_text_timing", bar_index: 0, start_s: 0.2, end_s: 2.8 }], ctx()).textActions,
    ).toEqual([{ type: "PATCH_BAR", id: "bar-1", patch: { start_s: 0.2, end_s: 2.8 } }]);

    expect(applyCopilotOps([{ op: "add_text", text: "day 1", start_s: 5, end_s: 7 }], ctx()).textActions)
      .toEqual([
        {
          type: "ADD_TEXT",
          bar: expect.objectContaining({
            id: "new-text",
            text: "day 1",
            start_s: 5,
            end_s: 7,
          }),
        },
      ]);

    expect(applyCopilotOps([{ op: "remove_text", bar_index: 1 }], ctx()).textActions)
      .toEqual([{ type: "DELETE_BAR", id: "bar-2" }]);
  });

  it("maps clip timing, reorder, remove, and split ops to slot transforms", () => {
    const duration = applyCopilotOps([{ op: "set_clip_duration", slot_index: 1, duration_s: 3 }], ctx());
    expect(duration.nextSlots?.find((s) => s.key === "b")).toMatchObject({
      inS: 1,
      durationS: 3,
      durationBeats: null,
    });

    const clipIn = applyCopilotOps([{ op: "set_clip_in", slot_index: 1, in_s: 0.4 }], ctx());
    expect(clipIn.nextSlots?.find((s) => s.key === "b")).toMatchObject({
      inS: 0.4,
      durationS: 4,
      durationBeats: null,
    });

    const reordered = applyCopilotOps([{ op: "reorder_clip", from_index: 2, to_index: 0 }], ctx());
    expect(reordered.nextSlots?.map((s) => s.key)).toEqual(["c", "a", "b"]);

    const removed = applyCopilotOps([{ op: "remove_clip", slot_index: 2 }], ctx());
    expect(removed.nextSlots?.find((s) => s.key === "c")?.removed).toBe(true);

    const split = applyCopilotOps([{ op: "split_clip", slot_index: 1, at_s: 5 }], ctx());
    expect(split.nextSlots?.map((s) => s.key)).toEqual(["a", "b", "b-split", "c"]);
    expect(split.nextSlots?.find((s) => s.key === "b")?.durationS).toBe(2);
    expect(split.nextSlots?.find((s) => s.key === "b-split")?.inS).toBe(3);
  });

  it("resolves indices through the snapshotted slot array including removed slots", () => {
    const slots = [
      slot({ key: "a", slotId: "a", durationS: 3 }),
      slot({ key: "removed", slotId: "removed", clipIndex: 1, removed: true, durationS: 4 }),
      slot({ key: "c", slotId: "c", clipIndex: 2, durationS: 2 }),
    ];
    const res = applyCopilotOps([{ op: "set_clip_in", slot_index: 2, in_s: 1.2 }], ctx({ slots }));

    expect(res.nextSlots?.find((s) => s.key === "c")?.inS).toBe(1.2);
  });

  it("rejects unknown and out-of-bounds ops", () => {
    const res = applyCopilotOps(
      [{ op: "swap_song" }, { op: "remove_text", bar_index: 99 }],
      ctx(),
    );

    expect(res.rejected.map((r) => r.reason)).toEqual(["invalid_op", "invalid_op"]);
  });

  it("strips non-vocabulary style keys before applying a patch", () => {
    const res = applyCopilotOps(
      [
        {
          op: "patch_text_style",
          bar_index: 0,
          patch: { size_px: 50, shadow_enabled: false },
        },
      ],
      ctx(),
    );

    expect(res.textActions).toEqual([
      { type: "PATCH_BAR", id: "bar-1", patch: { size_px: 50, size_class: undefined } },
    ]);
  });

  it("soft-fails when the user changed the patched field after the snapshot", () => {
    const base = ctx();
    const res = applyCopilotOps(
      [{ op: "patch_text_style", bar_index: 0, patch: { size_px: 54 } }],
      { ...base, bars: [bar({ size_px: 70 }), base.bars[1]] },
    );

    expect(res.textActions).toEqual([]);
    expect(res.rejected).toMatchObject([{ reason: "user_changed" }]);
  });

  it("rejects an op family disabled by capabilities", () => {
    const res = applyCopilotOps(
      [{ op: "edit_text", bar_index: 0, text: "nope" }],
      ctx({ capabilities: { text_elements: false, timeline: true } }),
    );

    expect(res.textActions).toEqual([]);
    expect(res.rejected).toMatchObject([{ reason: "capability_disabled" }]);
  });
});
