import type { MediaOverlay, MediaVisualBlock, PoolAsset, VisualBlock } from "@/lib/plan-api";

export const EDITOR_CANVAS_ASPECT = 1080 / 1920;

export const DEFAULT_MEDIA_TRANSFORM = {
  fit_mode: "contain" as const,
  focal_x: 0.5,
  focal_y: 0.5,
  zoom: 1,
};

export function mediaOverlayToVisualBlock(
  overlay: MediaOverlay,
  asset?: PoolAsset | null,
): MediaVisualBlock {
  const mediaKind = overlay.kind;
  const duration = mediaKind === "video" ? overlay.clip_duration_s ?? asset?.duration_s ?? null : null;
  return {
    version: 1,
    id: overlay.id,
    start_s: overlay.start_s,
    end_s: overlay.end_s,
    timing_mode: "manual",
    origin: overlay.source === "overlay_suggestion" ? "ai" : "user",
    transition_in: "cut",
    transition_out: overlay.exit_token === "dissolve-out" ? "fade" : "cut",
    audio_policy: { base: "continue", sfx: "continue" },
    kind: "media",
    asset_id: asset?.id ?? overlay.id,
    src_gcs_path: overlay.src_gcs_path,
    preview_gcs_path: overlay.preview_gcs_path ?? undefined,
    media_kind: mediaKind,
    source_duration_s: duration,
    trim_start_s: mediaKind === "video" ? overlay.clip_trim_start_s ?? 0 : null,
    trim_end_s: mediaKind === "video" ? overlay.clip_trim_end_s ?? duration : null,
    display_mode: overlay.display_mode === "fullscreen" ? "fullscreen" : "overlay",
    // Legacy fullscreen cards were always rendered as a centered cover crop.
    // Preserve that framing on first edit; only newly-authored fullscreen
    // media uses the contain default above.
    transform: overlay.display_mode === "fullscreen"
      ? { fit_mode: "cover", focal_x: 0.5, focal_y: 0.5, zoom: 1 }
      : { ...DEFAULT_MEDIA_TRANSFORM },
    x_frac: overlay.x_frac ?? 0.5,
    y_frac: overlay.y_frac ?? 0.5,
    scale: overlay.scale ?? 0.35,
    z: overlay.z ?? 0,
    source: overlay.source,
    effect_group_id: overlay.effect_group_id,
  };
}

/** Duplicate a media layer as an independent user-owned layer. */
export function duplicateMediaVisualBlock(
  source: MediaVisualBlock,
  id: string,
  startS: number,
  endS: number,
): MediaVisualBlock {
  return {
    ...source,
    id,
    start_s: startS,
    end_s: endS,
    timing_mode: "manual",
    origin: "user",
    source: "user",
    effect_group_id: null,
  };
}

export function copyMediaPreviewForDuplicate(
  previews: Record<string, string>,
  source: MediaVisualBlock,
  duplicateId: string,
): Record<string, string> {
  const preview = previews[source.id] ?? previews[source.asset_id];
  return preview ? { ...previews, [duplicateId]: preview } : previews;
}

export function removeMediaPreview(
  previews: Record<string, string>,
  id: string,
): { previews: Record<string, string>; orphanedUrl: string | null } {
  const removed = previews[id];
  if (!removed) return { previews, orphanedUrl: null };
  const remaining = { ...previews };
  delete remaining[id];
  return {
    previews: remaining,
    orphanedUrl: Object.values(remaining).includes(removed) ? null : removed,
  };
}

export function normalizeMediaVisualBlock(block: MediaVisualBlock): MediaVisualBlock {
  return {
    ...block,
    transform: { ...DEFAULT_MEDIA_TRANSFORM, ...(block.transform ?? {}) },
    x_frac: Math.max(0, Math.min(1, block.x_frac ?? 0.5)),
    y_frac: Math.max(0, Math.min(1, block.y_frac ?? 0.5)),
    scale: Math.max(0.05, Math.min(1, block.scale ?? 0.35)),
    z: Number.isFinite(block.z) ? Math.max(0, block.z) : 0,
  };
}

export type MediaPreviewGeometry = {
  leftPct: number;
  topPct: number;
  widthPct: number;
  heightPct: number;
};

export type MediaLayerMove = "backward" | "forward" | "back" | "front";

export function reorderMediaVisualBlocks(
  blocks: VisualBlock[],
  id: string,
  move: MediaLayerMove,
): VisualBlock[] {
  const ordered = blocks
    .filter((block): block is MediaVisualBlock => block.kind === "media")
    .sort((a, b) => a.z - b.z || a.start_s - b.start_s || a.id.localeCompare(b.id));
  const from = ordered.findIndex((block) => block.id === id);
  if (from < 0 || ordered.length < 2) return blocks;
  const [selected] = ordered.splice(from, 1);
  const to = move === "back"
    ? 0
    : move === "front"
      ? ordered.length
      : move === "backward"
        ? Math.max(0, from - 1)
        : Math.min(ordered.length, from + 1);
  ordered.splice(to, 0, selected);
  const zById = new Map(ordered.map((block, index) => [block.id, index]));
  return blocks.map((block) =>
    block.kind === "media" ? { ...block, z: zById.get(block.id) ?? block.z } : block,
  );
}

/**
 * Project the renderer's scale + overlay/crop math into canvas percentages.
 * FFmpeg scales against a zoomed 9:16 target, then positions the resulting
 * source with `(canvas - source) * focal`. Keeping this explicit avoids the
 * subtly different semantics of CSS object-position + transform: scale().
 */
export function mediaPreviewGeometry(
  block: MediaVisualBlock,
  sourceAspect: number | null | undefined,
  canvasAspect = EDITOR_CANVAS_ASPECT,
): MediaPreviewGeometry {
  const aspect = sourceAspect && sourceAspect > 0 ? sourceAspect : canvasAspect;
  if (block.display_mode === "overlay") {
    const widthPct = Math.max(0.05, Math.min(1, block.scale)) * 100;
    const heightPct = (widthPct * canvasAspect) / aspect;
    return {
      leftPct: block.x_frac * 100 - widthPct / 2,
      topPct: block.y_frac * 100 - heightPct / 2,
      widthPct,
      heightPct,
    };
  }

  const zoom = Math.max(1, block.transform.zoom);
  const sourceIsWider = aspect >= canvasAspect;
  const contain = block.transform.fit_mode === "contain";
  const widthPct = (contain === sourceIsWider ? 1 : aspect / canvasAspect) * zoom * 100;
  const heightPct = (contain === sourceIsWider ? canvasAspect / aspect : 1) * zoom * 100;
  return {
    leftPct: (100 - widthPct) * block.transform.focal_x,
    topPct: (100 - heightPct) * block.transform.focal_y,
    widthPct,
    heightPct,
  };
}

export function mediaOverlayPatchToVisualPatch(
  patch: Partial<MediaOverlay>,
): Partial<MediaVisualBlock> {
  const next: Partial<MediaVisualBlock> = {};
  if (typeof patch.start_s === "number") next.start_s = patch.start_s;
  if (typeof patch.end_s === "number") next.end_s = patch.end_s;
  if (typeof patch.x_frac === "number") next.x_frac = patch.x_frac;
  if (typeof patch.y_frac === "number") next.y_frac = patch.y_frac;
  if (typeof patch.scale === "number") next.scale = patch.scale;
  if (typeof patch.z === "number") next.z = patch.z;
  if (patch.display_mode === "fullscreen" || patch.display_mode === "pip") {
    next.display_mode = patch.display_mode === "fullscreen" ? "fullscreen" : "overlay";
  }
  if (patch.exit_token !== undefined) next.transition_out = patch.exit_token === "dissolve-out" ? "fade" : "cut";
  return next;
}
