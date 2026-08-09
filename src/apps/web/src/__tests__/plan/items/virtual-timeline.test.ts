import { describe, expect, it } from "@jest/globals";
import type { DraftSlot } from "@/app/generative/timeline-math";
import {
  buildVirtualTimeline,
  mapVirtualTimeToMusicTime,
  mapVirtualTime,
  nextVirtualEntry,
  slotsDifferFromBaseline,
  transitionPreviewAtTime,
  virtualDeckLookAdjustmentsAtTime,
  virtualDeckLookPresetsAtTime,
  type VirtualTimelineEntry,
} from "@/app/plan/items/[id]/_editor/virtual-timeline";

/** These fixtures never splice a carousel block in, so every entry is always
 * `kind: "clip"` at runtime — this narrows the type for tests written before
 * the carousel-block union existed. */
function clipEntry(entry: unknown): VirtualTimelineEntry {
  return entry as VirtualTimelineEntry;
}

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

    expect(timeline.entries.map((entry) => clipEntry(entry).slotKey)).toEqual(["a", "c"]);
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
    expect(clipEntry(timeline.entries[0]).sourceUrl).toBeNull();
  });

  it("detects clip-dirty state against the rendered baseline", () => {
    const baseline = [slot({ key: "a", durationS: 4 })];

    expect(slotsDifferFromBaseline(baseline, [slot({ key: "a", durationS: 4 })])).toBe(false);
    expect(slotsDifferFromBaseline(baseline, [slot({ key: "a", durationS: 3.5 })])).toBe(true);
    expect(slotsDifferFromBaseline(baseline, [slot({ key: "a", removed: true })])).toBe(true);
    expect(
      slotsDifferFromBaseline(baseline, [
        slot({ key: "a", lookPreset: "stadium_diffusion" }),
      ]),
    ).toBe(true);
    expect(
      slotsDifferFromBaseline(baseline, [
        slot({ key: "a", transitionAfter: "crossfade", transitionDurationS: 0.3 }),
      ]),
    ).toBe(true);
  });

  it("previews the render-safe transition duration at an active boundary", () => {
    const timeline = buildVirtualTimeline(
      [
        slot({
          key: "a",
          clipIndex: 0,
          durationS: 0.5,
          transitionAfter: "flash",
          transitionDurationS: 0.3,
        }),
        slot({ key: "b", clipIndex: 1, durationS: 2 }),
      ],
      clips,
    );

    expect(transitionPreviewAtTime(timeline, 0.4)).toEqual({
      kind: "flash",
      durationS: 0.15,
      progress: expect.closeTo(1 / 3, 5),
    });
    expect(transitionPreviewAtTime(timeline, 0.2)).toBeNull();
    expect(transitionPreviewAtTime(timeline, 0.5)).toBeNull();
  });

  it("projects multiple mixed transition overlaps into preview timing", () => {
    const timeline = buildVirtualTimeline(
      [
        slot({
          key: "a",
          clipIndex: 0,
          durationS: 2,
          transitionAfter: "crossfade",
          transitionDurationS: 0.2,
        }),
        slot({
          key: "b",
          clipIndex: 1,
          durationS: 3,
          transitionAfter: "dip_to_black",
          transitionDurationS: 0.3,
        }),
        slot({ key: "c", clipIndex: 2, durationS: 4 }),
      ],
      clips,
    );

    expect(timeline.entries.map((entry) => entry.startS)).toEqual([0, 1.8, 4.5]);
    expect(timeline.entries.map((entry) => clipEntry(entry).overlapBeforeS)).toEqual([
      0, 0.2, 0.3,
    ]);
    expect(timeline.totalDurationS).toBe(8.5);
    expect(transitionPreviewAtTime(timeline, 1.9)).toMatchObject({
      kind: "crossfade",
      durationS: 0.2,
      progress: expect.closeTo(0.5, 5),
    });
    expect(transitionPreviewAtTime(timeline, 4.65)).toMatchObject({
      kind: "dip_to_black",
      durationS: 0.3,
      progress: expect.closeTo(0.5, 5),
    });
  });

  it.each([
    ["stadium_diffusion", "none"] as ["stadium_diffusion", "none"],
    ["none", "stadium_diffusion"] as ["none", "stadium_diffusion"],
  ])(
    "keeps %s → %s crossfade looks scoped to their virtual decks",
    (outgoingLook, incomingLook) => {
      const slots = [
        slot({
          key: "a",
          clipIndex: 0,
          durationS: 2,
          transitionAfter: "crossfade",
          transitionDurationS: 0.2,
          lookPreset: outgoingLook,
        }),
        slot({
          key: "b",
          clipIndex: 1,
          durationS: 2,
          lookPreset: incomingLook,
        }),
      ];
      const timeline = buildVirtualTimeline(slots, clips);

      expect(virtualDeckLookPresetsAtTime(timeline, slots, 1.9, "a")).toEqual({
        a: outgoingLook,
        b: incomingLook,
      });
    },
  );

  it("keeps outgoing and incoming look controls scoped to their decks", () => {
    const olive = {
      intensity: 0.8,
      warmth: 0.1,
      contrast: -0.1,
      grain: 0.2,
      vignette: 0.3,
    };
    const smoky = {
      intensity: 1,
      warmth: -0.2,
      contrast: 0.2,
      grain: 0.5,
      vignette: 0.6,
    };
    const slots = [
      slot({
        key: "a",
        clipIndex: 0,
        durationS: 2,
        transitionAfter: "crossfade",
        transitionDurationS: 0.2,
        lookPreset: "olive_film",
        lookAdjustments: olive,
      }),
      slot({
        key: "b",
        clipIndex: 1,
        durationS: 2,
        lookPreset: "smoky_split_tone",
        lookAdjustments: smoky,
      }),
    ];
    const timeline = buildVirtualTimeline(slots, clips);

    expect(virtualDeckLookAdjustmentsAtTime(timeline, slots, 1.9, "a")).toEqual({
      a: olive,
      b: smoky,
    });
  });
});

describe("carousel-moment splice (Lane C staged block)", () => {
  function fourClipSlots(): DraftSlot[] {
    return [
      slot({ key: "a", clipIndex: 0, durationS: 2 }),
      slot({ key: "b", clipIndex: 1, durationS: 2 }),
      slot({ key: "c", clipIndex: 2, durationS: 2 }),
      slot({ key: "d", clipIndex: 0, durationS: 2 }),
    ];
  }

  it("splices at the intro: index 0, before every clip", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "intro",
      durationS: 3,
    });
    expect(timeline.entries.map((e) => e.kind)).toEqual([
      "carousel",
      "clip",
      "clip",
      "clip",
      "clip",
    ]);
    expect(timeline.entries[0]).toMatchObject({ kind: "carousel", startS: 0, durationS: 3 });
    // Every clip shifted later by the block's duration.
    expect(timeline.entries.map((e) => e.startS)).toEqual([0, 3, 5, 7, 9]);
    expect(timeline.totalDurationS).toBe(11);
  });

  it("splices at the outro: appended after every clip, clips untouched", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "outro",
      durationS: 3,
    });
    expect(timeline.entries.map((e) => e.kind)).toEqual([
      "clip",
      "clip",
      "clip",
      "clip",
      "carousel",
    ]);
    expect(timeline.entries.slice(0, 4).map((e) => e.startS)).toEqual([0, 2, 4, 6]);
    expect(timeline.entries[4]).toMatchObject({ kind: "carousel", startS: 8, durationS: 3 });
    expect(timeline.totalDurationS).toBe(11);
  });

  it("splices at the middle: floor(n/2) splits an even clip count evenly", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "middle",
      durationS: 3,
    });
    // n=4 -> insertion index 2: 2 clips before, 2 after.
    expect(timeline.entries.map((e) => e.kind)).toEqual([
      "clip",
      "clip",
      "carousel",
      "clip",
      "clip",
    ]);
    expect(timeline.entries.map((e) => e.startS)).toEqual([0, 2, 4, 7, 9]);
    expect(timeline.totalDurationS).toBe(11);
  });

  it("splices at the middle: floor(n/2) leaves an odd remainder AFTER the block", () => {
    const fiveSlots = [...fourClipSlots(), slot({ key: "e", clipIndex: 1, durationS: 2 })];
    const timeline = buildVirtualTimeline(fiveSlots, clips, [], {
      position: "middle",
      durationS: 3,
    });
    // n=5 -> insertion index floor(5/2)=2: 2 clips before, 3 after — the
    // Python `len(steps) // 2` never leaves the extra clip before the block.
    expect(timeline.entries.map((e) => e.kind)).toEqual([
      "clip",
      "clip",
      "carousel",
      "clip",
      "clip",
      "clip",
    ]);
  });

  it("maps time inside the carousel window to a kind-tagged, source-less mapping", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "middle",
      durationS: 3,
    });
    const mapping = mapVirtualTime(timeline, 5); // inside the block's [4, 7) window
    expect(mapping?.entry.kind).toBe("carousel");
    expect(mapping?.sourceTimeS).toBeNull();
  });

  it("maps time outside the carousel window to the correct (shifted) clip entry", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "middle",
      durationS: 3,
    });
    const before = mapVirtualTime(timeline, 1);
    expect(before?.entry.kind).toBe("clip");
    expect(clipEntry(before!.entry).slotKey).toBe("a");
    const after = mapVirtualTime(timeline, 8);
    expect(after?.entry.kind).toBe("clip");
    expect(clipEntry(after!.entry).slotKey).toBe("c");
  });

  it("nextVirtualEntry past the last clip before the block returns the carousel entry", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "middle",
      durationS: 3,
    });
    const next = nextVirtualEntry(timeline, 1); // index of the 2nd clip ("b")
    expect(next?.kind).toBe("carousel");
  });

  it("passes through byte-identical with no carousel param (undefined or null)", () => {
    const withoutParam = buildVirtualTimeline(fourClipSlots(), clips);
    const withUndefined = buildVirtualTimeline(fourClipSlots(), clips, [], undefined);
    const withNull = buildVirtualTimeline(fourClipSlots(), clips, [], null);
    expect(withoutParam.entries.every((e) => e.kind === "clip")).toBe(true);
    expect(withoutParam).toEqual(withUndefined);
    expect(withoutParam).toEqual(withNull);
  });

  it("passes through unchanged with a zero-duration carousel", () => {
    const timeline = buildVirtualTimeline(fourClipSlots(), clips, [], {
      position: "intro",
      durationS: 0,
    });
    expect(timeline.entries.every((e) => e.kind === "clip")).toBe(true);
    expect(timeline.totalDurationS).toBe(8);
  });
});
