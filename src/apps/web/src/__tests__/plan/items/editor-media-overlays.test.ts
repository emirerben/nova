import { describe, expect, it } from "@jest/globals";
import type { MediaOverlay } from "@/lib/plan-api";
import {
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
});
