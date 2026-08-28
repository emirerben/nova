import { describe, expect, it } from "@jest/globals";
import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";
import Filmstrip, {
  FILMSTRIP_MAX_SEEKS,
  FILMSTRIP_TILE_W,
  allocateFilmstripDensityBudget,
  allocateFilmstripSeekBudget,
  filmstripCoverCrop,
  filmstripDecodeKey,
  filmstripFallbackLabel,
  filmstripPoolKey,
  filmstripRasterWidth,
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

    expect(key).toBe("slot-2:7:3.333:1.667:4:224x40");
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

  it("isolates pooled decoders by both source and clip", () => {
    const base = filmstripPoolKey("source-a.mp4", "slot-1");

    expect(filmstripPoolKey("source-a.mp4", "slot-2")).not.toBe(base);
    expect(filmstripPoolKey("source-b.mp4", "slot-1")).not.toBe(base);
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
    expect(filmstripZoomBucket(FILMSTRIP_TILE_W * 3, 8, 8)).toBe(8);
  });

  it("caps the backing raster to the decoded thumbnail density", () => {
    expect(filmstripRasterWidth(11_520, 8)).toBe(8 * FILMSTRIP_TILE_W);
    expect(filmstripRasterWidth(173, 8)).toBe(173);
  });

  it("renders an explicit labelled fallback when clip media is unavailable", async () => {
    render(
      React.createElement(Filmstrip, {
        src: null,
        clipId: "missing-clip",
        sourceId: "missing-source",
        sourceStartS: 0,
        durationS: 3.2,
        widthPx: 180,
        label: "Missing clip",
      }),
    );

    expect(await screen.findByText("Missing clip")).not.toBeNull();
    expect(
      screen.getByTestId("editor-filmstrip").getAttribute("data-clip-key"),
    ).toBe("missing-clip");
  });

  it("allocates visible tiles for a prod-shaped 17-slot song timeline", () => {
    const budgets = allocateFilmstripSeekBudget(new Array(17).fill(FILMSTRIP_TILE_W));

    expect(budgets).toHaveLength(17);
    expect(budgets.every((value) => value > 0)).toBe(true);
  });

  it("matches desktop-density sampling for a three-clip mobile timeline", () => {
    expect(allocateFilmstripDensityBudget([173, 149, 182], 1)).toEqual([
      8, 8, 8,
    ]);
    expect(
      allocateFilmstripDensityBudget(new Array(17).fill(FILMSTRIP_TILE_W), 1),
    ).toEqual(new Array(17).fill(1));
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

  it("redistributes final samples across the drawable source bound", () => {
    const samples = filmstripSampleTimes({
      sourceStartS: 10.9,
      durationS: 1,
      sourceDurationS: 11.21,
      tiles: 3,
    });
    expect(samples[0]).toBeCloseTo(10.943333, 6);
    expect(samples[1]).toBeCloseTo(11.03, 6);
    expect(samples[2]).toBeCloseTo(11.116667, 6);
    expect(samples[1]).toBeGreaterThan(samples[0]);
    expect(samples[2]).toBeGreaterThan(samples[1]);
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
