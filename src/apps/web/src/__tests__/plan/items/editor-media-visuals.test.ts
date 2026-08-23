import {
  copyMediaPreviewForDuplicate,
  duplicateMediaVisualBlock,
  mediaOverlayPatchToVisualPatch,
  mediaOverlayToVisualBlock,
  normalizeMediaVisualBlock,
  removeMediaPreview,
  reorderMediaVisualBlocks,
} from "@/app/plan/items/[id]/_editor/editor-media-visuals";
import type { MediaOverlay, PoolAsset } from "@/lib/plan-api";
import { placeAfterSelected } from "@/app/plan/items/[id]/_editor/editor-bar-drag";

const asset: PoolAsset = {
  id: "pool-video",
  kind: "video",
  status: "ready",
  source_filename: "clip.mp4",
  duration_s: 8.5,
  aspect: 0.5625,
  subject: null,
  user_context: "",
  display_url: "https://signed/clip.mp4",
  preview_url: "https://signed/clip.jpg",
  deduped: false,
  gcs_path: "users/u/plan/pool/clip.mp4",
};

const overlay: MediaOverlay = {
  id: "legacy-1",
  kind: "video",
  src_gcs_path: asset.gcs_path,
  preview_gcs_path: null,
  preview_url: asset.preview_url,
  position: "center",
  x_frac: 0.5,
  y_frac: 0.5,
  scale: 0.4,
  start_s: 1,
  end_s: 4,
  z: 2,
};

describe("editor media visual contract", () => {
  it("falls back to ready pool duration when legacy video trim metadata is absent", () => {
    const block = mediaOverlayToVisualBlock(overlay, asset);
    expect(block.source_duration_s).toBe(8.5);
    expect(block.trim_start_s).toBe(0);
    expect(block.trim_end_s).toBe(8.5);
    expect(block.display_mode).toBe("overlay");
  });

  it("preserves the legacy fullscreen center-cover crop and translates only shared editable fields", () => {
    const block = mediaOverlayToVisualBlock({ ...overlay, display_mode: "fullscreen" }, asset);
    expect(block.transform).toEqual({ fit_mode: "cover", focal_x: 0.5, focal_y: 0.5, zoom: 1 });
    expect(mediaOverlayPatchToVisualPatch({ x_frac: 0.2, y_frac: 0.8, scale: 0.6, display_mode: "pip", clip_trim_start_s: 3 })).toEqual({
      x_frac: 0.2,
      y_frac: 0.8,
      scale: 0.6,
      display_mode: "overlay",
    });
  });

  it("duplicates converted media without sharing generated-effect ownership", () => {
    const source = mediaOverlayToVisualBlock({
      ...overlay,
      source: "overlay_suggestion",
      effect_group_id: "effect-1",
    }, asset);
    expect(duplicateMediaVisualBlock(source, "copy", 4, 7)).toMatchObject({
      id: "copy",
      start_s: 4,
      end_s: 7,
      origin: "user",
      source: "user",
      effect_group_id: null,
    });
    const copiedPreviews = copyMediaPreviewForDuplicate(
      { [source.id]: "blob:legacy-preview" },
      source,
      "copy",
    );
    expect(copiedPreviews.copy).toBe("blob:legacy-preview");
    const firstRemoval = removeMediaPreview(copiedPreviews, source.id);
    expect(firstRemoval.orphanedUrl).toBeNull();
    expect(firstRemoval.previews.copy).toBe("blob:legacy-preview");
    expect(removeMediaPreview(firstRemoval.previews, "copy").orphanedUrl).toBe(
      "blob:legacy-preview",
    );
  });

  it("does not create an invalid video block without a source duration", () => {
    const noDuration = mediaOverlayToVisualBlock({ ...overlay, clip_duration_s: undefined }, { ...asset, duration_s: null });
    expect(noDuration.source_duration_s).toBeNull();
    expect(normalizeMediaVisualBlock({ ...noDuration, z: Number.NaN }).z).toBe(0);
  });

  it("clamps adjacent sequence placement to a short project", () => {
    expect(placeAfterSelected({ selected: { end_s: 0.8 }, durationS: 2, videoDurationS: 1 })).toEqual({ start_s: 0.8, end_s: 1 });
    expect(placeAfterSelected({ selected: { end_s: 1 }, durationS: 2, videoDurationS: 1 })).toBeNull();
  });

  it("normalizes z order for exact front, back, and one-step moves", () => {
    const first = mediaOverlayToVisualBlock({ ...overlay, id: "first", z: 1001 }, asset);
    const second = mediaOverlayToVisualBlock({ ...overlay, id: "second", z: 2 }, asset);
    const third = mediaOverlayToVisualBlock({ ...overlay, id: "third", z: 2 }, asset);
    const moved = reorderMediaVisualBlocks([first, second, third], "second", "front");
    expect(
      moved
        .filter((block) => block.kind === "media")
        .sort((a, b) => a.z - b.z)
        .map((block) => [block.id, block.z]),
    ).toEqual([["third", 0], ["first", 1], ["second", 2]]);
    expect(reorderMediaVisualBlocks(moved, "second", "back").find((block) => block.id === "second"))
      .toMatchObject({ z: 0 });
  });
});
