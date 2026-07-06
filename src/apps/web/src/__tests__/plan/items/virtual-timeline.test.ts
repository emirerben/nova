import { describe, expect, it } from "@jest/globals";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  buildVirtualTimeline,
  mapVirtualTimeToMusicTime,
  mapVirtualTime,
  slotsDifferFromBaseline,
} from "@/app/plan/items/[id]/_editor/virtual-timeline";

function slot(over: Partial<DraftSlot> = {}): DraftSlot {
  return {
    key: "a",
    slotId: "a",
    clipIndex: 0,
    inS: 0,
    durationBeats: null,
    durationS: 4,
    removed: false,
    momentDescription: null,
    ...over,
  };
}

const clips = [
  { clip_index: 0, signed_url: "https://source.example/a.mp4" },
  { clip_index: 1, signed_url: "https://source.example/b.mp4" },
  { clip_index: 2, signed_url: "https://source.example/c.mp4" },
];

describe("virtual timeline", () => {
  it("maps virtual time to slot index and source offset", () => {
    const timeline = buildVirtualTimeline(
      [
        slot({ key: "a", clipIndex: 0, inS: 2, durationS: 3 }),
        slot({ key: "b", clipIndex: 1, inS: 10, durationS: 4 }),
      ],
      clips,
    );

    expect(timeline.totalDurationS).toBe(7);
    expect(mapVirtualTime(timeline, 1.25)).toMatchObject({
      entry: { slotIndex: 0, slotKey: "a", startS: 0 },
      localOffsetS: 1.25,
      sourceTimeS: 3.25,
    });
    expect(mapVirtualTime(timeline, 4)).toMatchObject({
      entry: { slotIndex: 1, slotKey: "b", startS: 3 },
      localOffsetS: 1,
      sourceTimeS: 11,
    });
  });

  it("maps virtual time onto the selected song section", () => {
    expect(mapVirtualTimeToMusicTime(2.25, 14.5)).toBe(16.75);
    expect(mapVirtualTimeToMusicTime(-2, 14.5)).toBe(14.5);
  });

  it("uses the next slot at an exact boundary", () => {
    const timeline = buildVirtualTimeline(
      [
        slot({ key: "a", clipIndex: 0, inS: 1, durationS: 2 }),
        slot({ key: "b", clipIndex: 1, inS: 5, durationS: 2 }),
      ],
      clips,
    );

    expect(mapVirtualTime(timeline, 2)).toMatchObject({
      entry: { slotIndex: 1, slotKey: "b" },
      localOffsetS: 0,
      sourceTimeS: 5,
    });
  });

  it("skips removed slots in cumulative starts", () => {
    const timeline = buildVirtualTimeline(
      [
        slot({ key: "a", clipIndex: 0, durationS: 2 }),
        slot({ key: "removed", clipIndex: 1, durationS: 10, removed: true }),
        slot({ key: "c", clipIndex: 2, inS: 7, durationS: 3 }),
      ],
      clips,
    );

    expect(timeline.entries.map((entry) => entry.slotKey)).toEqual(["a", "c"]);
    expect(timeline.totalDurationS).toBe(5);
    expect(mapVirtualTime(timeline, 2.5)).toMatchObject({
      entry: { slotIndex: 2, slotKey: "c", startS: 2 },
      sourceTimeS: 7.5,
    });
  });

  it("keeps a later clip's playback duration stable when an earlier clip is trimmed", () => {
    const before = buildVirtualTimeline(
      [
        slot({ key: "a", clipIndex: 0, durationS: 4 }),
        slot({ key: "b", clipIndex: 1, inS: 10, durationS: 5 }),
      ],
      clips,
    );
    const after = buildVirtualTimeline(
      [
        slot({ key: "a", clipIndex: 0, durationS: 2 }),
        slot({ key: "b", clipIndex: 1, inS: 10, durationS: 5 }),
      ],
      clips,
    );

    expect(before.entries[1]).toMatchObject({
      slotKey: "b",
      startS: 4,
      durationS: 5,
      inS: 10,
    });
    expect(after.entries[1]).toMatchObject({
      slotKey: "b",
      startS: 2,
      durationS: 5,
      inS: 10,
    });
    expect(mapVirtualTime(after, 6.5)).toMatchObject({
      entry: { slotKey: "b" },
      localOffsetS: 4.5,
      sourceTimeS: 14.5,
    });
  });

  it("clamps before the start and at the final frame", () => {
    const timeline = buildVirtualTimeline(
      [slot({ key: "a", clipIndex: 0, inS: 3, durationS: 2 })],
      clips,
    );

    expect(mapVirtualTime(timeline, -10)).toMatchObject({
      virtualTimeS: 0,
      sourceTimeS: 3,
    });
    expect(mapVirtualTime(timeline, 99)).toMatchObject({
      virtualTimeS: 2,
      localOffsetS: 2,
      sourceTimeS: 5,
    });
  });

  it("flags missing source URLs", () => {
    const timeline = buildVirtualTimeline(
      [slot({ key: "a", clipIndex: 9, durationS: 1 })],
      clips,
    );

    expect(timeline.hasMissingSource).toBe(true);
    expect(timeline.entries[0].sourceUrl).toBeNull();
  });

  it("detects clip-dirty state against the rendered baseline", () => {
    const baseline = [slot({ key: "a", durationS: 4 })];

    expect(slotsDifferFromBaseline(baseline, [slot({ key: "a", durationS: 4 })])).toBe(false);
    expect(slotsDifferFromBaseline(baseline, [slot({ key: "a", durationS: 3.5 })])).toBe(true);
    expect(slotsDifferFromBaseline(baseline, [slot({ key: "a", removed: true })])).toBe(true);
  });
});
