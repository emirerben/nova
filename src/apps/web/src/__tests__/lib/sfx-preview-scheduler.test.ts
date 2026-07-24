import {
  sfxPlaybackOffsetAt,
  sfxPlacementsStartingInWindow,
} from "@/lib/sfx-preview-scheduler";
import { describe, expect, it } from "@jest/globals";

describe("sfx-preview-scheduler", () => {
  const placements = [
    { id: "before", at_s: 0.8 },
    { id: "hit-a", at_s: 1.25 },
    { id: "hit-b", at_s: 3.0 },
    { id: "after", at_s: 4.2 },
  ];

  it("returns placements that start in the playback window after a seek", () => {
    expect(sfxPlacementsStartingInWindow(placements, 1.0, 3.0).map((p) => p.id)).toEqual([
      "hit-a",
      "hit-b",
    ]);
  });

  it("treats a backward time jump as a loop-wrap window", () => {
    expect(sfxPlacementsStartingInWindow(placements, 3.8, 1.0).map((p) => p.id)).toEqual([
      "before",
      "after",
    ]);
  });

  it("computes the offset for an effect that is already active after a seek", () => {
    expect(sfxPlaybackOffsetAt({ at_s: 2, duration_s: 1.5 }, 2.75)).toBeCloseTo(0.75);
    expect(sfxPlaybackOffsetAt({ at_s: 2, duration_s: 1.5 }, 3.75)).toBeNull();
  });

  it("honors trim bounds when computing active playback offset", () => {
    const placement = {
      at_s: 2,
      duration_s: 5,
      trim_start_s: 1.25,
      trim_end_s: 2.75,
    };
    expect(sfxPlaybackOffsetAt(placement, 1.99)).toBeNull();
    expect(sfxPlaybackOffsetAt(placement, 2)).toBeCloseTo(1.25);
    expect(sfxPlaybackOffsetAt(placement, 2.5)).toBeCloseTo(1.75);
    expect(sfxPlaybackOffsetAt(placement, 3.5)).toBeNull();
  });
});
