import { describe, expect, it } from "@jest/globals";
import {
  carouselChoreographyDuration,
  effectiveBoundaryDuration,
  resizeCarouselTiming,
  shouldAutoUpgradeCarouselTiming,
  upgradeCarouselTiming,
} from "@/lib/carousel-timing";

describe("carousel ripple timing", () => {
  it("upgrades legacy configuration with ordered manual defaults", () => {
    const upgraded = upgradeCarouselTiming(
      { mode: "rolling", duration_s: 6, transition: "none" },
      [2, 0, 1],
    );
    expect(upgraded.timing_model).toBe("ripple_v1");
    expect(upgraded.sequence?.map((item) => item.clip_index)).toEqual([2, 0, 1]);
    expect(upgraded.transition_in).toBe("none");
    expect(upgraded.transition_out).toBe("none");
  });

  it("stretches both edges by scaling holds, movement and zoom proportionally", () => {
    const resized = resizeCarouselTiming(
      {
        timing_model: "ripple_v1",
        duration_s: 2.3,
        sequence: [{ clip_index: 1, hold_s: 1 }],
        move_duration_s: 0.5,
        zoom_duration_s: 0.4,
      },
      4.6,
      [0],
    );
    expect(resized).toMatchObject({
      duration_s: 4.6,
      sequence: [{ clip_index: 1, hold_s: 2 }],
      move_duration_s: 1,
      zoom_duration_s: 0.8,
    });
  });

  it("never contracts below the duration needed to complete the choreography", () => {
    const resized = resizeCarouselTiming(
      {
        timing_model: "ripple_v1",
        mode: "focus",
        duration_s: 5.3,
        sequence: [0, 1, 2, 3, 4].map((clipIndex) => ({
          clip_index: clipIndex,
          hold_s: 0.5,
        })),
        move_duration_s: 0.2,
        zoom_duration_s: 0.2,
      },
      2,
      [0, 1, 2, 3, 4],
    );

    expect(resized.duration_s).toBeCloseTo(5.3);
    expect(carouselChoreographyDuration(resized)).toBeLessThanOrEqual(
      resized.duration_s ?? 0,
    );
  });

  it("stops expansion at the longest complete choreography", () => {
    const resized = resizeCarouselTiming(
      {
        timing_model: "ripple_v1",
        mode: "rolling",
        duration_s: 2,
        sequence: [{ clip_index: 4, hold_s: 2 }],
        move_duration_s: 0.6,
      },
      15,
      [4],
    );

    expect(resized.duration_s).toBe(5);
    expect(resized.sequence).toEqual([{ clip_index: 4, hold_s: 5 }]);
  });

  it("keeps static legacy moments untouched until a mode is chosen", () => {
    expect(
      shouldAutoUpgradeCarouselTiming({
        mode: "stills" as unknown as "focus",
        duration_s: 4,
      }),
    ).toBe(false);
    expect(shouldAutoUpgradeCarouselTiming({ mode: "focus", duration_s: 4 })).toBe(true);
  });

  it("caps each crossfade to thirty percent of either neighbor", () => {
    expect(effectiveBoundaryDuration(1, 1, 5)).toBeCloseTo(0.3);
    expect(effectiveBoundaryDuration(undefined, 5, 5)).toBe(0.4);
  });

  it("counts movement from the actual first active source identity", () => {
    const rolling = {
      timing_model: "ripple_v1" as const,
      mode: "rolling" as const,
      sequence: [
        { clip_index: 2, hold_s: 1 },
        { clip_index: 5, hold_s: 1 },
      ],
      move_duration_s: 0.5,
    };
    expect(carouselChoreographyDuration(rolling, [2, 5])).toBe(2.5);
    expect(
      carouselChoreographyDuration(
        { ...rolling, sequence: [rolling.sequence[1], rolling.sequence[0]] },
        [2, 5],
      ),
    ).toBe(3);
  });
});
