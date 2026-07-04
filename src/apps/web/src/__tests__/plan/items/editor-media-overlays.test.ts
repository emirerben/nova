import { describe, expect, it } from "@jest/globals";
import type { MediaOverlay } from "@/lib/plan-api";
import {
  applyMediaOverlaySourceWindowInput,
  clampMediaOverlayPosition,
  clampMediaOverlayScale,
  isMediaOverlayVisibleAtTime,
  mediaOverlayDisplayUrl,
  visibleMediaOverlaysAtTime,
} from "@/app/plan/items/[id]/_editor/editor-media-overlays";

function card(overrides: Partial<MediaOverlay> = {}): MediaOverlay {
  return {
    id: "overlay-1",
    kind: "image",
    src_gcs_path: "media-uploads/overlay-1.png",
    preview_url: "https://signed.example/overlay-1.png",
    position: "center",
    x_frac: 0.5,
    y_frac: 0.5,
    scale: 0.35,
    start_s: 1,
    end_s: 3,
    z: 0,
    ...overrides,
  };
}

describe("editor media overlays", () => {
  it("uses inclusive playback windows", () => {
    const overlay = card({ start_s: 1, end_s: 3 });
    expect(isMediaOverlayVisibleAtTime(overlay, 0.99)).toBe(false);
    expect(isMediaOverlayVisibleAtTime(overlay, 1)).toBe(true);
    expect(isMediaOverlayVisibleAtTime(overlay, 2)).toBe(true);
    expect(isMediaOverlayVisibleAtTime(overlay, 3)).toBe(true);
    expect(isMediaOverlayVisibleAtTime(overlay, 3.01)).toBe(false);
  });

  it("prefers local object URLs over persisted signed URLs", () => {
    const overlay = card({ id: "fresh", preview_url: "https://signed.example/fresh.png" });
    expect(mediaOverlayDisplayUrl(overlay, { fresh: "blob:local-preview" })).toBe(
      "blob:local-preview",
    );
  });

  it("falls back to preview_url for persisted overlays", () => {
    expect(mediaOverlayDisplayUrl(card(), {})).toBe("https://signed.example/overlay-1.png");
  });

  it("returns only renderable cards at the playhead sorted by z", () => {
    const visibleHigh = card({ id: "high", z: 2, preview_url: "https://signed.example/high.png" });
    const visibleLow = card({ id: "low", z: 1, preview_url: "https://signed.example/low.png" });
    const hidden = card({ id: "hidden", start_s: 4, end_s: 5 });
    const missingUrl = card({ id: "missing", preview_url: null });

    expect(
      visibleMediaOverlaysAtTime([visibleHigh, hidden, missingUrl, visibleLow], 2, {}),
    ).toEqual([
      { card: visibleLow, displayUrl: "https://signed.example/low.png" },
      { card: visibleHigh, displayUrl: "https://signed.example/high.png" },
    ]);
  });

  it("clamps canvas movement by the rendered card bounds", () => {
    expect(
      clampMediaOverlayPosition({
        xFrac: -0.5,
        yFrac: 1.5,
        widthFrac: 0.3,
        heightFrac: 0.2,
      }),
    ).toEqual({ x_frac: 0.15, y_frac: 0.9 });

    expect(
      clampMediaOverlayPosition({
        xFrac: 0.5,
        yFrac: 0.5,
        widthFrac: 0,
        heightFrac: 0,
      }),
    ).toEqual({ x_frac: 0.5, y_frac: 0.5 });
  });

  it("clamps canvas scaling to the overlay renderer bounds", () => {
    expect(clampMediaOverlayScale(0.001)).toBe(0.05);
    expect(clampMediaOverlayScale(0.42)).toBe(0.42);
    expect(clampMediaOverlayScale(3)).toBe(1);
    expect(clampMediaOverlayScale(Number.NaN)).toBe(0.35);
  });

  it("clamps video overlay source crop to the source duration", () => {
    expect(
      applyMediaOverlaySourceWindowInput({
        trimStartS: 9.9,
        trimEndS: 20,
        clipDurationS: 10,
      }),
    ).toEqual({ clip_trim_start_s: 9.7, clip_trim_end_s: 10 });

    expect(
      applyMediaOverlaySourceWindowInput({
        trimStartS: -2,
        trimEndS: 0.1,
        clipDurationS: 5,
      }),
    ).toEqual({ clip_trim_start_s: 0, clip_trim_end_s: 0.3 });
  });
});
