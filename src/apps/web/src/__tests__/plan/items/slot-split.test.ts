import {
  splitSlotAt,
  deleteSlotEnforceFloor,
  activeSlotCount,
  MIN_SLOT_SPLIT_S,
} from "@/app/plan/items/[id]/_editor/slot-split";
import type { DraftSlot } from "@/app/generative/timeline-math";

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "s1",
    slotId: "s1",
    clipIndex: 0,
    inS: 2,
    durationBeats: null,
    durationS: 4,
    removed: false,
    momentDescription: null,
    ...over,
  };
}

describe("splitSlotAt (seconds mode)", () => {
  it("cuts one slot into two from the same source at the playhead", () => {
    // single slot, window = [0, 4]. Cut at 1.5s.
    const slots = [slot()];
    const { slots: next, didSplit } = splitSlotAt(slots, [], "s1", 1.5, "s1b");

    expect(didSplit).toBe(true);
    expect(next).toHaveLength(2);
    const [left, right] = next;

    // Left keeps the source in-point, shortened to the cut.
    expect(left.inS).toBe(2);
    expect(left.durationS).toBe(1.5);

    // Right continues from where the left half ended, in BOTH source + output.
    expect(right.inS).toBeCloseTo(3.5, 6); // 2 + 1.5
    expect(right.durationS).toBeCloseTo(2.5, 6); // 4 - 1.5
    expect(right.clipIndex).toBe(0);
    expect(right.key).toBe("s1b");
    expect(right.slotId).toBeNull();
  });

  it("splits the correct slot when it starts mid-timeline", () => {
    // Two slots: [0,4] then [4,6]. Cut the second at 5s → offset 1s.
    const slots = [
      slot({ key: "a", slotId: "a", durationS: 4 }),
      slot({ key: "b", slotId: "b", inS: 0, durationS: 2 }),
    ];
    const { slots: next, didSplit } = splitSlotAt(slots, [], "b", 5, "b2");
    expect(didSplit).toBe(true);
    expect(next.map((s) => s.key)).toEqual(["a", "b", "b2"]);
    const b = next[1];
    const b2 = next[2];
    expect(b.durationS).toBeCloseTo(1, 6);
    expect(b2.durationS).toBeCloseTo(1, 6);
    expect(b2.inS).toBeCloseTo(1, 6); // 0 + 1
  });

  it("refuses a cut that leaves a half below the minimum", () => {
    const slots = [slot()]; // window [0,4]
    const tooEarly = splitSlotAt(slots, [], "s1", MIN_SLOT_SPLIT_S / 2, "x");
    expect(tooEarly.didSplit).toBe(false);
    expect(tooEarly.slots).toHaveLength(1);
  });

  it("refuses to split a beats-gridded slot (no-grid split only)", () => {
    const slots = [slot({ durationBeats: 8 })];
    const res = splitSlotAt(slots, [1, 2, 3], "s1", 1.5, "x");
    expect(res.didSplit).toBe(false);
  });

  it("is a no-op for an unknown key", () => {
    const slots = [slot()];
    const res = splitSlotAt(slots, [], "nope", 1.5, "x");
    expect(res.didSplit).toBe(false);
    expect(res.slots).toBe(slots);
  });
});

describe("deleteSlotEnforceFloor (≥1-slot floor)", () => {
  it("soft-deletes a slot when others remain", () => {
    const slots = [
      slot({ key: "a", slotId: "a" }),
      slot({ key: "b", slotId: "b" }),
    ];
    const { slots: next, didDelete } = deleteSlotEnforceFloor(slots, "a");
    expect(didDelete).toBe(true);
    expect(next.find((s) => s.key === "a")?.removed).toBe(true);
    expect(activeSlotCount(next)).toBe(1);
  });

  it("refuses to remove the last active slot", () => {
    const slots = [
      slot({ key: "a", slotId: "a", removed: true }),
      slot({ key: "b", slotId: "b" }),
    ];
    const { slots: next, didDelete } = deleteSlotEnforceFloor(slots, "b");
    expect(didDelete).toBe(false);
    expect(next.find((s) => s.key === "b")?.removed).toBe(false);
    expect(activeSlotCount(next)).toBe(1);
  });

  it("is a no-op for an already-removed or unknown slot", () => {
    const slots = [slot({ key: "a" }), slot({ key: "b", removed: true })];
    expect(deleteSlotEnforceFloor(slots, "b").didDelete).toBe(false);
    expect(deleteSlotEnforceFloor(slots, "zzz").didDelete).toBe(false);
  });
});
