import { describe, expect, it } from "@jest/globals";
import {
  FILMSTRIP_MAX_SEEKS,
  FILMSTRIP_TILE_W,
  allocateFilmstripSeekBudget,
  filmstripCoverCrop,
  filmstripDecodeKey,
  filmstripFallbackLabel,
  filmstripSampleTimes,
  filmstripZoomBucket,
} from "@/app/plan/items/[id]/_editor/Filmstrip";

describe("editor source filmstrip helpers", () => {
  it("keys decodes by clip source window and zoom bucket only", () => {
    const key = filmstripDecodeKey({
      clipId: "slot-2",
      sourceId: 7,
      inS: 3.33333,
      durationS: 1.66666,
      zoomBucket: 4,
    });

    expect(key).toBe("slot-2:7:3.333:1.667:4");
  });

  it("changes the decode key for source-window edits", () => {
    const base = filmstripDecodeKey({
      clipId: "slot-2",
      sourceId: 7,
      inS: 3,
      durationS: 2,
      zoomBucket: 4,
    });

    expect(
      filmstripDecodeKey({
        clipId: "slot-2",
        sourceId: 7,
        inS: 3.5,
        durationS: 2,
        zoomBucket: 4,
      }),
    ).not.toBe(base);
    expect(
      filmstripDecodeKey({
        clipId: "slot-2",
        sourceId: 7,
        inS: 3,
        durationS: 1.5,
        zoomBucket: 4,
      }),
    ).not.toBe(base);
  });

  it("keeps the global seek budget at or under 24 frames", () => {
    const budgets = allocateFilmstripSeekBudget([
      FILMSTRIP_TILE_W * 12,
      FILMSTRIP_TILE_W * 10,
      FILMSTRIP_TILE_W * 8,
      FILMSTRIP_TILE_W * 6,
    ]);

    expect(budgets.reduce((sum, value) => sum + value, 0)).toBeLessThanOrEqual(
      FILMSTRIP_MAX_SEEKS,
    );
    expect(budgets.every((value) => value > 0)).toBe(true);
  });

  it("can leave very crowded tracks unsampled instead of exceeding the cap", () => {
    const budgets = allocateFilmstripSeekBudget(new Array(30).fill(FILMSTRIP_TILE_W));

    expect(budgets.reduce((sum, value) => sum + value, 0)).toBe(
      FILMSTRIP_MAX_SEEKS,
    );
    expect(budgets.filter((value) => value === 0)).toHaveLength(6);
  });

  it("buckets by allocated seek count", () => {
    expect(filmstripZoomBucket(FILMSTRIP_TILE_W * 6, 3)).toBe(3);
    expect(filmstripZoomBucket(FILMSTRIP_TILE_W * 0.4, 3)).toBe(1);
    expect(filmstripZoomBucket(FILMSTRIP_TILE_W, 0)).toBe(0);
  });

  it("allocates visible tiles for a prod-shaped 17-slot song timeline", () => {
    const budgets = allocateFilmstripSeekBudget(new Array(17).fill(FILMSTRIP_TILE_W));

    expect(budgets).toHaveLength(17);
    expect(budgets.every((value) => value > 0)).toBe(true);
  });

  it("falls back to duration text when the caller passes an empty label", () => {
    expect(filmstripFallbackLabel("", 0.469)).toBe("0.5s");
    expect(filmstripFallbackLabel("  ", 3.2)).toBe("3.2s");
    expect(filmstripFallbackLabel("Clip 1", 3.2)).toBe("Clip 1");
  });

  it("samples the temporal center of each tile inside the exact source window", () => {
    expect(
      filmstripSampleTimes({
        sourceStartS: 3.6,
        durationS: 3,
        sourceDurationS: 11.21,
        tiles: 3,
      }),
    ).toEqual([4.1, 5.1, 6.1]);
  });

  it("clamps final samples to the drawable source bound", () => {
    expect(
      filmstripSampleTimes({
        sourceStartS: 10.9,
        durationS: 1,
        sourceDurationS: 11.21,
        tiles: 2,
      }),
    ).toEqual([11.15, 11.16]);
  });

  it("center-crops source frames instead of distorting them", () => {
    expect(
      filmstripCoverCrop({
        sourceWidth: 1920,
        sourceHeight: 1080,
        targetWidth: 56,
        targetHeight: 40,
      }),
    ).toEqual({ sx: 204, sy: 0, sw: 1512, sh: 1080 });
    expect(
      filmstripCoverCrop({
        sourceWidth: 1080,
        sourceHeight: 1920,
        targetWidth: 56,
        targetHeight: 40,
      }),
    ).toEqual({
      sx: 0,
      sy: 574.2857142857142,
      sw: 1080,
      sh: 771.4285714285714,
    });
  });
});
