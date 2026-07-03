/**
 * cardMedia — the ONE place that turns a MediaOverlay display_mode into the
 * media element classes (plan 009 T4 media-markup consolidation).
 *
 * Consumed by the FOUR duplicated card-media sites:
 *   - LiveOverlayCardsLayer's <img> (image cards)
 *   - LiveOverlayCardsLayer's TrimmedVideoPreview <video> (video cards)
 *   - SuggestionRail's in-card 9:16 mini-preview media
 *   - HeroOverlayEditor's suggestion card media
 *
 * Only the CLASS LOGIC is deduplicated here — per-site props (refs, sync
 * effects, draggable, data-testid, onLoadedMetadata, pointer-events/select
 * utility classes) stay at each site.
 *
 *   pip        → fit-width card with rounded corners (today's behavior).
 *   fullscreen → cover-crop takeover, zero chrome (no rounded corners, no
 *                shadow) — CSS parity with the FFmpeg bake's
 *                `scale=1080:1920:force_original_aspect_ratio=increase,crop`.
 *                The card WRAPPER must stretch to the full frame
 *                (overlayCardStyle's fullscreen branch pins all four insets
 *                to 0) so `h-full object-cover` actually fills.
 */

import type { MediaOverlay } from "@/lib/plan-api";

export function mediaClassFor(displayMode: MediaOverlay["display_mode"]): string {
  return displayMode === "fullscreen"
    ? "w-full h-full object-cover"
    : "w-full h-auto rounded";
}
