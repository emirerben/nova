import type { MediaOverlay } from "@/lib/plan-api";

export const MEDIA_OVERLAY_MIN_SCALE = 0.05;
export const MEDIA_OVERLAY_MAX_SCALE = 1.0;
export const MEDIA_OVERLAY_MIN_DURATION_S = 0.3;

export const EDITOR_STAGE_Z = {
  video: 0,
  mediaOverlay: 20,
  textOverlay: 40,
  selectionHandle: 60,
  chrome: 80,
  error: 90,
} as const;

const MEDIA_OVERLAY_Z_MAX_OFFSET = EDITOR_STAGE_Z.textOverlay - EDITOR_STAGE_Z.mediaOverlay - 1;

export interface VisibleMediaOverlay {
  card: MediaOverlay;
  displayUrl: string;
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) return (min + max) / 2;
  return Math.min(max, Math.max(min, value));
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

export function clampMediaOverlayScale(scale: number): number {
  if (!Number.isFinite(scale)) return 0.35;
  return round(clamp(scale, MEDIA_OVERLAY_MIN_SCALE, MEDIA_OVERLAY_MAX_SCALE));
}

export function clampMediaOverlayPosition({
  xFrac,
  yFrac,
  widthFrac,
  heightFrac,
}: {
  xFrac: number;
  yFrac: number;
  widthFrac: number;
  heightFrac: number;
}): Pick<MediaOverlay, "x_frac" | "y_frac"> {
  const halfW = Math.max(0.02, Math.min(0.5, widthFrac / 2));
  const halfH = Math.max(0.02, Math.min(0.5, heightFrac / 2));
  return {
    x_frac: round(clamp(xFrac, halfW, 1 - halfW)),
    y_frac: round(clamp(yFrac, halfH, 1 - halfH)),
  };
}

export function applyMediaOverlaySourceWindowInput({
  trimStartS,
  trimEndS,
  clipDurationS,
  minDurationS = MEDIA_OVERLAY_MIN_DURATION_S,
}: {
  trimStartS: number;
  trimEndS: number;
  clipDurationS: number;
  minDurationS?: number;
}): Pick<MediaOverlay, "clip_trim_start_s" | "clip_trim_end_s"> {
  const sourceDuration = Math.max(minDurationS, clipDurationS);
  const start = clamp(trimStartS, 0, Math.max(0, sourceDuration - minDurationS));
  const end = clamp(trimEndS, start + minDurationS, sourceDuration);
  return {
    clip_trim_start_s: Math.round(start * 10) / 10,
    clip_trim_end_s: Math.round(end * 10) / 10,
  };
}

export function mediaOverlayDisplayUrl(
  card: MediaOverlay,
  localPreviewUrls: Record<string, string>,
): string | null {
  return localPreviewUrls[card.id] ?? card.preview_url ?? null;
}

export function mediaOverlayStackZIndex(cardZ: number, selected: boolean): number {
  if (selected) return EDITOR_STAGE_Z.selectionHandle;
  const normalized = Number.isFinite(cardZ) ? Math.trunc(cardZ) : 0;
  return EDITOR_STAGE_Z.mediaOverlay + clamp(normalized, 0, MEDIA_OVERLAY_Z_MAX_OFFSET);
}

export function isMediaOverlayVisibleAtTime(
  card: Pick<MediaOverlay, "start_s" | "end_s">,
  currentTimeS: number,
): boolean {
  return currentTimeS >= card.start_s && currentTimeS <= card.end_s;
}

export function visibleMediaOverlaysAtTime(
  cards: MediaOverlay[],
  currentTimeS: number,
  localPreviewUrls: Record<string, string>,
): VisibleMediaOverlay[] {
  return cards
    .filter((card) => isMediaOverlayVisibleAtTime(card, currentTimeS))
    .map((card) => {
      const displayUrl = mediaOverlayDisplayUrl(card, localPreviewUrls);
      return displayUrl ? { card, displayUrl } : null;
    })
    .filter((card): card is VisibleMediaOverlay => card !== null)
    .sort((a, b) => a.card.z - b.card.z);
}
