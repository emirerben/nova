import type { MediaOverlay } from "@/lib/plan-api";

export interface VisibleMediaOverlay {
  card: MediaOverlay;
  displayUrl: string;
}

export function mediaOverlayDisplayUrl(
  card: MediaOverlay,
  localPreviewUrls: Record<string, string>,
): string | null {
  return localPreviewUrls[card.id] ?? card.preview_url ?? null;
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
